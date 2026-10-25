# -*- coding: utf-8 -*-
"""轻量 TF-IDF 向量存储（纯 numpy，无外部向量库）。

移植来源：`ai-companion/memory/__init__.py:144-427`
（`_atomic_write_text` / `SimpleVectorizer` / `MemoryEntry` / `VectorStore`）。

唯一改动：`loguru` → 标准库 `logging`（去掉一条第三方依赖，算法一行未改）。
`VectorStore.add()` 的语义保持"append + 全量 fit_transform 重建"——上层
（`core.import_`）已经依赖这个语义（导入时不要重复 add，见 `_restore_reindex` 注释）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


def atomic_write_text(path: Path | str, text: str) -> None:
    """原子写文本：先写同目录临时文件，再 os.replace 顶替。

    蓝绿滚动发布时新旧进程共用一个数据卷，直接 open(w) 会被读到半截文件。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


class SimpleVectorizer:
    """轻量级文本向量化器，基于词频统计（纯 numpy，无需 sklearn）。"""

    def __init__(self, max_features: int = 1000):
        self.max_features = max_features
        self.vocab: Dict[str, int] = {}
        self.idf: Dict[str, float] = {}
        self._doc_count = 0

    def _tokenize(self, text: str) -> List[str]:
        """中文分词：按字和短词切分（中文单字 + 英文词 + 2/3 字滑窗）。"""
        text = text.lower()
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
        english_words = re.findall(r'[a-z]+', text)
        bigrams = []
        chars = re.findall(r'[\u4e00-\u9fff]', text)
        for i in range(len(chars) - 1):
            bigrams.append(chars[i] + chars[i + 1])
        for i in range(len(chars) - 2):
            bigrams.append(chars[i] + chars[i + 1] + chars[i + 2])
        return chinese_chars + english_words + bigrams

    def fit(self, texts: List[str]):
        """构建词表和 IDF"""
        doc_freq = {}
        for text in texts:
            tokens = set(self._tokenize(text))
            for token in tokens:
                doc_freq[token] = doc_freq.get(token, 0) + 1
        self._doc_count = len(texts)
        sorted_tokens = sorted(doc_freq.items(), key=lambda x: x[1], reverse=True)
        self.vocab = {token: idx for idx, (token, _) in enumerate(sorted_tokens[:self.max_features])}
        for token, freq in doc_freq.items():
            if token in self.vocab:
                self.idf[token] = math.log((1 + self._doc_count) / (1 + freq)) + 1

    def transform(self, texts: List[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), len(self.vocab)), dtype=np.float32)
        for i, text in enumerate(texts):
            tokens = self._tokenize(text)
            token_counts = {}
            for token in tokens:
                token_counts[token] = token_counts.get(token, 0) + 1
            for token, count in token_counts.items():
                if token in self.vocab:
                    idx = self.vocab[token]
                    tf = math.log(1 + count)
                    idf = self.idf.get(token, 1.0)
                    vectors[i, idx] = tf * idf
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return vectors / norms

    def transform_one(self, text: str) -> np.ndarray:
        return self.transform([text])[0]

    def fit_transform(self, texts: List[str]) -> np.ndarray:
        self.fit(texts)
        return self.transform(texts)


@dataclass
class MemoryEntry:
    """记忆条目"""
    id: str
    text: str
    metadata: dict
    created_at: str
    memory_type: str  # "short_term" | "long_term" | "summary" | "knowledge"


class VectorStore:
    """轻量向量存储：numpy 数组 + cosine similarity 检索，按名字落一个 JSON 文件。"""

    def __init__(self, name: str, storage_dir: Path | str):
        self.name = name
        self.storage_dir = Path(storage_dir)
        self.vectorizer = SimpleVectorizer(max_features=2000)
        self.entries: List[MemoryEntry] = []
        self.vectors: Optional[np.ndarray] = None
        self._lock = threading.RLock()
        self._dirty = False
        self._load()

    def _get_storage_path(self) -> Path:
        return self.storage_dir / f"{self.name}.json"

    def _load(self):
        path = self._get_storage_path()
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.entries = [
                MemoryEntry(
                    id=e["id"],
                    text=e["text"],
                    metadata=e.get("metadata", {}),
                    created_at=e.get("created_at", datetime.now().isoformat()),
                    memory_type=e.get("memory_type", "short_term"),
                )
                for e in data.get("entries", [])
            ]
            if self.entries:
                self.vectors = self.vectorizer.fit_transform([e.text for e in self.entries])
            logger.info("[%s] 加载了 %d 条记忆", self.name, len(self.entries))
        except Exception as exc:
            logger.error("[%s] 加载失败: %s", self.name, exc)

    def _save(self):
        try:
            data = {
                "name": self.name,
                "updated_at": datetime.now().isoformat(),
                "entries": [
                    {"id": e.id, "text": e.text, "metadata": e.metadata,
                     "created_at": e.created_at, "memory_type": e.memory_type}
                    for e in self.entries
                ],
            }
            atomic_write_text(self._get_storage_path(),
                              json.dumps(data, ensure_ascii=False, indent=2))
            self._dirty = False
        except Exception as exc:
            logger.error("[%s] 保存失败: %s", self.name, exc)

    def add(self, text: str, metadata: dict = None, memory_type: str = "short_term",
            entry_id: str = None) -> str:
        """添加记忆（append + 全量重建向量；无 id 去重，调用方自己判断）。"""
        with self._lock:
            entry_id = entry_id or f"mem_{int(time.time() * 1000)}_{len(self.entries)}"
            entry = MemoryEntry(
                id=entry_id,
                text=text,
                metadata=metadata or {},
                created_at=datetime.now().isoformat(),
                memory_type=memory_type,
            )
            self.entries.append(entry)
            self.vectors = self.vectorizer.fit_transform([e.text for e in self.entries])
            self._dirty = True
            self._save()
            logger.info("[%s] 添加记忆: %s…", self.name, text[:24])
            return entry_id

    def search(self, query: str, top_k: int = 5, memory_type: str = None) -> List[dict]:
        with self._lock:
            if not self.entries or self.vectors is None:
                return []
            query_vec = self.vectorizer.transform_one(query)
            similarities = self.vectors @ query_vec
            results = []
            for idx, sim in enumerate(similarities):
                entry = self.entries[idx]
                if memory_type and entry.memory_type != memory_type:
                    continue
                if sim > 0.1:
                    results.append({
                        "id": entry.id,
                        "text": entry.text,
                        "similarity": float(sim),
                        "metadata": entry.metadata,
                        "created_at": entry.created_at,
                        "memory_type": entry.memory_type,
                    })
            results.sort(key=lambda x: x["similarity"], reverse=True)
            return results[:top_k]

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            for i, entry in enumerate(self.entries):
                if entry.id == entry_id:
                    self.entries.pop(i)
                    if self.entries:
                        self.vectors = self.vectorizer.fit_transform([e.text for e in self.entries])
                    else:
                        self.vectors = None
                    self._save()
                    return True
            return False

    def list_all(self, memory_type: str = None) -> List[dict]:
        with self._lock:
            results = []
            for entry in self.entries:
                if memory_type and entry.memory_type != memory_type:
                    continue
                results.append({
                    "id": entry.id,
                    "text": entry.text,
                    "metadata": entry.metadata,
                    "created_at": entry.created_at,
                    "memory_type": entry.memory_type,
                })
            return results

    def clear(self, memory_type: str = None):
        with self._lock:
            if memory_type:
                self.entries = [e for e in self.entries if e.memory_type != memory_type]
            else:
                self.entries = []
            if self.entries:
                self.vectors = self.vectorizer.fit_transform([e.text for e in self.entries])
            else:
                self.vectors = None
            self._save()


__all__ = ["SimpleVectorizer", "MemoryEntry", "VectorStore", "atomic_write_text"]
