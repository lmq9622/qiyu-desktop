"""
栖语 (Qiyu) - 轻量向量记忆存储
基于 numpy 实现，无需外部向量数据库
支持：短期记忆、长期记忆、记忆压缩、RAG 知识库
"""

import os
import json
import difflib
import re
import math
import time
import threading
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

import numpy as np
from loguru import logger

# ============ 配置 ============
from pathutil import get_data_dir, user_dir, get_users_dir

_DATA_DIR = get_data_dir()
MEMORY_DIR = _DATA_DIR / "memory"
KNOWLEDGE_DIR = _DATA_DIR / "knowledge"
CHAT_HISTORY_DIR = _DATA_DIR / "chat_history"
DAY_LOG_DIR = _DATA_DIR / "chat_logs"
OUTLINE_DIR = _DATA_DIR / "outlines"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
for _d in (CHAT_HISTORY_DIR, DAY_LOG_DIR, OUTLINE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 双记忆层配置：短记忆 TTL（3 天）、高频提及转长期阈值、高权重转长期阈值
SHORT_MEM_TTL = 3 * 86400
PROMOTE_MENTIONS = 3
PROMOTE_WEIGHT = 0.85


# 纯骂人/玩笑话的特征（这些里面出现的"死/妈/去世"是脏话，不是丧亲）
_INSULT_MARKERS = ("你妈", "你全家", "他妈", "妈的", "妈卖", "死妈", "妈死", "操你", "草你", "艹你",
                   "傻逼", "煞笔", "沙比", "脑残", "狗叫", "你爹", "你爸死", "死你", "死一户口本",
                   "滚你", "去死", "炸了")


def _looks_like_insult(text: str) -> bool:
    """看起来就是骂人/互怼的一句：这种话里出现的亲属、生死、病痛都是脏话，不能当事实记。"""
    t = (text or "").strip()
    if not t or len(t) > 48:
        return False
    return any(k in t for k in _INSULT_MARKERS)


def _promote_score(meta: dict, now: float = None) -> float:
    """记忆综合晋升分（不单看 weight）：
    importance(权重)*0.40 + frequency(提及次数)*0.25 + stability(存在天数)*0.15
    + recency(最近提及)*0.10 + explicitness(是否明确表达)*0.10
    满足综合分阈值才允许晋升长期。"""
    now = now or time.time()
    try:
        weight = max(0.0, min(1.0, float(meta.get("weight", 0.5) or 0.5)))
    except Exception:
        weight = 0.5
    try:
        mentions = max(1, int(meta.get("mention_count", 1) or 1))
    except Exception:
        mentions = 1
    explicit = 1.0 if meta.get("explicit") in (1, "1", True, "true") else 0.5
    try:
        first = float(meta.get("first_seen") or 0) or now
    except Exception:
        first = now
    try:
        last = float(meta.get("last_seen") or 0) or now
    except Exception:
        last = now
    age_days = max(0.0, (now - first) / 86400.0)
    recency = max(0.0, min(1.0, 1.0 - (now - last) / (3 * 86400.0)))
    return (weight * 0.40
            + min(1.0, mentions / 5.0) * 0.25
            + min(1.0, age_days / 7.0) * 0.15
            + recency * 0.10
            + explicit * 0.10)


# 权重语义：推测用户喜好 → 强化记忆 → 让模型更了解用户。
# 初始权重低（~0.30）；随时间按反比例曲线衰减（约 0.5 天→0.15，次日→0.10，一个月→0.05）；
# 再次被提及则上涨（0.30→0.50→0.70→…直到 1.00 永久记住）。
MEM_INITIAL_WEIGHT = 0.30
MEM_MAX_EVENT_WEIGHT = 0.45   # 当天事件初始权重上限：宁低勿高，靠重复提及爬升
MEM_REMENTION_BUMP = 0.20     # 每次再次提及 +20
MEM_DECAY_FLOOR = 0.02


def _decay_weight(weight, now: float = None, last_seen: float = None) -> float:
    """反比例衰减：w / (1 + 1.0*sqrt(天数))。
    约 0.5 天→×0.58，1 天→×0.5，30 天→×0.15，最终停在 MEM_DECAY_FLOOR。"""
    """反比例衰减：w / (1 + 1.5*sqrt(天数))。
    约 6 小时→×0.57（30→17），1 天→×0.40（30→12），30 天→×0.11（30→3），最终停在 MEM_DECAY_FLOOR。"""
    try:
        w = max(0.0, min(1.0, float(weight))) if weight is not None else MEM_INITIAL_WEIGHT
    except Exception:
        w = MEM_INITIAL_WEIGHT
    if not last_seen:
        return max(MEM_DECAY_FLOOR, w)
    now = now or time.time()
    try:
        days = max(0.0, (now - float(last_seen)) / 86400.0)
    except Exception:
        days = 0.0
    if days <= 0:
        return w
    return max(MEM_DECAY_FLOOR, w / (1.0 + 1.5 * math.sqrt(days)))


def _bump_weight(weight) -> float:
    """再次提及：权重上涨（30→50→70→…→100），越接近 100 涨幅越小。"""
    try:
        w = max(0.0, min(1.0, float(weight)))
    except Exception:
        w = MEM_INITIAL_WEIGHT
    if w >= 0.95:
        return min(1.0, w + 0.03)
    if w >= 0.8:
        return min(1.0, w + 0.10)
    return min(1.0, w + MEM_REMENTION_BUMP)


def _text_sim(a: str, b: str) -> float:
    """去标点后的轻量文本相似度（0~1），用于当天事件合并/短记忆去重。"""
    a = re.sub(r"[\s，。！？、,.!?；;：:\"'“”‘’（）()【】\[\]～~…]", "", a or "")
    b = re.sub(r"[\s，。！？、,.!?；;：:\"'“”‘’（）()【】\[\]～~…]", "", b or "")
    if not a or not b:
        return 0.0
    try:
        return difflib.SequenceMatcher(None, a, b).ratio()
    except Exception:
        return 0.0


# ============ 简单 TF-IDF 向量器 ============

def _atomic_write_text(path, text: str) -> None:
    """原子写文本：先写同目录临时文件，再 os.replace 顶替。

    为什么必须有：直接 open(path,"w") 会先把文件截断成 0 字节再慢慢写，
    这期间进程被 kill / 断电 / 崩一下，落盘的就成了半截文件——聊天历史、
    记忆库都这么丢过内容。临时文件名带 pid，避免同目录撞名。
    桌面版本机单进程，但崩溃/断电一样会读到半截文件，所以照服务器版统一原子写。
    """
    p = Path(path)
    tmp = p.with_name(f"{p.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    # Windows 上目标文件正被别的进程/杀毒软件/编辑器短暂占住时，os.replace 会抛
    # PermissionError。这里退避重试几次（0.05→0.10→0.15→0.20s），别把这次写入直接丢掉。
    last_err = None
    for attempt in range(5):
        try:
            os.replace(tmp, p)
            return
        except PermissionError as e:
            last_err = e
            if attempt == 4:
                break
            time.sleep(0.05 * (attempt + 1))
    # 重试耗尽：清掉临时文件再把异常抛给调用方（各自的 except 会记日志），不静默吞掉。
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass
    raise last_err


class SimpleVectorizer:
    """轻量级文本向量化器，基于词频统计
    
    无需 sklearn/sentence-transformers，纯 numpy 实现
    """
    
    def __init__(self, max_features: int = 1000):
        self.max_features = max_features
        self.vocab: Dict[str, int] = {}
        self.idf: Dict[str, float] = {}
        self._doc_count = 0
    
    def _tokenize(self, text: str) -> List[str]:
        """中文分词：按字和短词切分"""
        # 提取中文字符、英文单词、数字
        text = text.lower()
        # 中文字
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
        # 英文单词
        english_words = re.findall(r'[a-z]+', text)
        # 2-3 字中文词（简单滑动窗口）
        bigrams = []
        chars = re.findall(r'[\u4e00-\u9fff]', text)
        for i in range(len(chars) - 1):
            bigrams.append(chars[i] + chars[i+1])
        for i in range(len(chars) - 2):
            bigrams.append(chars[i] + chars[i+1] + chars[i+2])
        
        return chinese_chars + english_words + bigrams
    
    def fit(self, texts: List[str]):
        """构建词表和 IDF"""
        doc_freq = {}
        for text in texts:
            tokens = set(self._tokenize(text))
            for token in tokens:
                doc_freq[token] = doc_freq.get(token, 0) + 1
        
        self._doc_count = len(texts)
        # 选择高频词作为词表
        sorted_tokens = sorted(doc_freq.items(), key=lambda x: x[1], reverse=True)
        self.vocab = {token: idx for idx, (token, _) in enumerate(sorted_tokens[:self.max_features])}
        
        # 计算 IDF
        for token, freq in doc_freq.items():
            if token in self.vocab:
                self.idf[token] = math.log((1 + self._doc_count) / (1 + freq)) + 1
    
    def transform(self, texts: List[str]) -> np.ndarray:
        """将文本转换为向量"""
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
        
        # L2 归一化
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1
        vectors = vectors / norms
        return vectors
    
    def transform_one(self, text: str) -> np.ndarray:
        """单文本向量化"""
        return self.transform([text])[0]
    
    def fit_transform(self, texts: List[str]) -> np.ndarray:
        self.fit(texts)
        return self.transform(texts)


# ============ 向量存储 ============

@dataclass
class MemoryEntry:
    """记忆条目"""
    id: str
    text: str
    metadata: dict
    created_at: str
    memory_type: str  # "short_term" | "long_term" | "summary" | "knowledge"


class VectorStore:
    """轻量向量存储
    
    用 numpy 数组存储向量，支持 cosine similarity 检索
    """
    
    def __init__(self, name: str, storage_dir: Path = MEMORY_DIR):
        self.name = name
        self.storage_dir = storage_dir
        self.vectorizer = SimpleVectorizer(max_features=2000)
        self.entries: List[MemoryEntry] = []
        self.vectors: Optional[np.ndarray] = None
        self._lock = threading.RLock()
        self._dirty = False
        
        self._load()
    
    def _get_storage_path(self) -> Path:
        return self.storage_dir / f"{self.name}.json"
    
    def _load(self):
        """从文件加载"""
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
            
            # 重建向量
            if self.entries:
                texts = [e.text for e in self.entries]
                self.vectors = self.vectorizer.fit_transform(texts)
            
            logger.info(f"[{self.name}] 加载了 {len(self.entries)} 条记忆")
        except Exception as e:
            logger.error(f"[{self.name}] 加载失败: {e}")
    
    def _save(self):
        """保存到文件"""
        path = self._get_storage_path()
        try:
            data = {
                "name": self.name,
                "updated_at": datetime.now().isoformat(),
                "entries": [
                    {
                        "id": e.id,
                        "text": e.text,
                        "metadata": e.metadata,
                        "created_at": e.created_at,
                        "memory_type": e.memory_type,
                    }
                    for e in self.entries
                ]
            }
            _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
            self._dirty = False
        except Exception as e:
            logger.error(f"[{self.name}] 保存失败: {e}")
    
    def add(self, text: str, metadata: dict = None, memory_type: str = "short_term", entry_id: str = None) -> str:
        """添加记忆"""
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
            
            # 重新构建所有向量（简单方案，数据量大时需优化为增量更新）
            texts = [e.text for e in self.entries]
            self.vectors = self.vectorizer.fit_transform(texts)
            
            self._dirty = True
            self._save()
            
            logger.info(f"[{self.name}] 添加记忆: {text[:50]}...")
            return entry_id
    
    def search(self, query: str, top_k: int = 5, memory_type: str = None) -> List[dict]:
        """搜索相关记忆"""
        with self._lock:
            if not self.entries or self.vectors is None:
                return []
            
            # 向量化查询
            query_vec = self.vectorizer.transform_one(query)
            
            # Cosine similarity（向量已归一化，点积即 cosine）
            similarities = self.vectors @ query_vec
            
            # 过滤和排序
            results = []
            for idx, sim in enumerate(similarities):
                entry = self.entries[idx]
                if memory_type and entry.memory_type != memory_type:
                    continue
                if sim > 0.1:  # 阈值过滤
                    results.append({
                        "id": entry.id,
                        "text": entry.text,
                        "similarity": float(sim),
                        "metadata": entry.metadata,
                        "created_at": entry.created_at,
                        "memory_type": entry.memory_type,
                    })
            
            # 按相似度排序
            results.sort(key=lambda x: x["similarity"], reverse=True)
            return results[:top_k]
    
    def delete(self, entry_id: str) -> bool:
        """删除记忆"""
        with self._lock:
            for i, entry in enumerate(self.entries):
                if entry.id == entry_id:
                    self.entries.pop(i)
                    # 重建向量
                    if self.entries:
                        texts = [e.text for e in self.entries]
                        self.vectors = self.vectorizer.fit_transform(texts)
                    else:
                        self.vectors = None
                    self._save()
                    return True
            return False
    
    def list_all(self, memory_type: str = None) -> List[dict]:
        """列出所有记忆"""
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
        """清空记忆"""
        with self._lock:
            if memory_type:
                self.entries = [e for e in self.entries if e.memory_type != memory_type]
            else:
                self.entries = []
            
            if self.entries:
                texts = [e.text for e in self.entries]
                self.vectors = self.vectorizer.fit_transform(texts)
            else:
                self.vectors = None
            
            self._save()


# ============ 记忆管理器 ============

class MemoryManager:
    """记忆管理器
    
    管理多用户的短期记忆、长期记忆和对话摘要
    """
    
    def __init__(self):
        self._stores: Dict[str, VectorStore] = {}
        self._chat_histories: Dict[str, List[dict]] = {}  # user_id -> messages
        self._day_logs: Dict[str, List[dict]] = {}  # "hkey|date" -> messages（按天快照，供每日大纲使用）
        self._summary_interval = 10  # 每10轮对话生成摘要
        self._pipeline_lock = threading.RLock()  # 记忆流水线专用锁，避免并发重复压缩
        self._lock = threading.RLock()
        self._classified_days: set = set()
        self._sum_up_to: Dict[str, int] = {}   # 可选：本段话题结束时的整理边界（不设时按保留窗口兜底）
        self._sum_done: Dict[str, int] = {}    # 已经整理到第几条（只整理新增内容，不重复跑，也不删原文）
        self._load_classified_days()
        self._migrate_legacy_layout()
        self._load_histories()

    # ---- 当天/短期/长期三档：某天的聊天记录先在"当天记忆"，4点/空闲时总结分级 ----
    # -*- coding: utf-8 -*-
    def _classified_file(self) -> Path:
        return MEMORY_DIR / "_day_summary_done.json"

    def _load_classified_days(self):
        try:
            if self._classified_file().exists():
                data = json.loads(self._classified_file().read_text(encoding="utf-8"))
                self._classified_days = set(data if isinstance(data, list) else [])
        except Exception:
            self._classified_days = set()

    def _persist_classified_days(self):
        try:
            _atomic_write_text(self._classified_file(),
                               json.dumps(sorted(self._classified_days), ensure_ascii=False))
        except Exception:
            pass

    def has_classified_day(self, user_id: str, char_id: str, date_str: str) -> bool:
        return f"{self._hkey(user_id, char_id)}___{date_str}" in self._classified_days

    def mark_classified_day(self, user_id: str, char_id: str, date_str: str):
        self._classified_days.add(f"{self._hkey(user_id, char_id)}___{date_str}")
        self._persist_classified_days()

    def get_day_messages(self, user_id: str, char_id: str = "", date_str: str = "") -> List[dict]:
        """某天的聊天记录（当天记忆 = 原始记录 + 权重，4点分级后才沉淀进短/长期）"""
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            return list(self._day_log(self._hkey(user_id, char_id), date_str))

    def day_message_count(self, user_id: str, char_id: str = "", date_str: str = "") -> int:
        """当天消息总数（当天记忆的权重来源：聊得越多权重越高）"""
        return len(self.get_day_messages(user_id, char_id, date_str))

    async def classify_day_memories(self, user_id: str, char_id: str, date_str: str, llm_async_fn=None) -> dict:
        """凌晨 4 点/空闲时把某天记忆分级入库：
        优先读取当天分条事件（带权重）作为事实来源，模型只负责补充属性：
        - type=value（三观/长期偏好/稳定个人情况）→ 长期记忆（可随聊天修改）
        - type=event（具体事件/临时状态）→ 短期记忆（初始权重低，随时间衰减，再次提及上涨）
        幂等：同一 (用户,角色,日期) 只执行一次。"""
        if self.has_classified_day(user_id, char_id, date_str):
            return {"skipped": True, "reason": "already_done"}
        msgs = self.get_day_messages(user_id, char_id, date_str)
        msgs = [m for m in msgs if (m.get("content") or "").strip() and m.get("content") != "(无回复)"]
        if len(msgs) < 4 or not llm_async_fn:
            return {"skipped": True, "reason": "too_few"}
        events = self.get_daily_events(user_id, date_str, char_id)
        convo = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content'][:300]}" for m in msgs[-60:]
        )
        if events:
            ev_text = "\n".join(f"- {e.get('text', '')}（权重 {e.get('weight', 0.3)}）" for e in events[:30])
            prompt = (
                "你是记忆分类器。下面是某天聊天的分条事件（每条已带权重，权重只表示'对用户的了解程度'）。\n"
                "请把每条事件归类并只输出一个 JSON 数组，每项格式：\n"
                '{"text": "事件原文或更精炼的改写", "type": "value 或 event", "explicit": 0 或 1}\n'
                "- type=value：三观、长期性格/偏好、稳定的个人情况（以后会反复用到，进长期记忆）\n"
                "- type=event：具体发生的事、临时状态、一次性信息（进短期记忆）\n"
                "- explicit=1 表示用户明确说出来的，0 表示推断\n"
                "意思相同的事件合并成一条，只输出 JSON 数组，不要解释、不要客套。\n\n"
                f"日期：{date_str}\n事件：\n{ev_text}"
            )
        else:
            prompt = (
                "你是记忆分类器。下面是某天的微信聊天记录，请提取值得记住的 3~6 条，只输出一个 JSON 数组，每项格式：\n"
                '{"text": "内容", "type": "value 或 event", "explicit": 0 或 1, "weight": 0~1}\n'
                "- type=value：三观/长期偏好/稳定个人情况（进长期记忆）\n"
                "- type=event：具体发生的事/临时状态（进短期记忆）\n"
                "- weight 0~1：对了解用户的重要性，初始权重别给高（0.2~0.45 即可）\n"
                "只输出 JSON 数组。\n\n"
                f"日期：{date_str}\n聊天记录：\n{convo}"
            )
        raw = ""
        try:
            raw = ((await llm_async_fn(prompt)) if llm_async_fn else "").strip()
        except Exception as e:
            logger.warning(f"[记忆分级] 生成失败: {e}")
            return {"skipped": True, "reason": "llm_error"}
        items = self._parse_json_memory_items(raw)
        if not items:
            return {"skipped": True, "reason": "empty"}
        store = self._get_store(user_id, char_id)
        long_n = short_n = 0
        now = time.time()
        with store._lock:
            for it in items:
                text = (it.get("text") or "").strip()[:300]
                if len(text) < 4:
                    continue
                typ = str(it.get("type") or "event").strip().lower()
                explicit = 1 if it.get("explicit") in (1, "1", True, "true", "是", "明确") else 0
                try:
                    w = max(0.0, min(1.0, float(it.get("weight", 0.3) or 0.3)))
                except Exception:
                    w = MEM_INITIAL_WEIGHT
                if typ == "value" or typ.startswith("value"):
                    dup = store.search(text, top_k=1, memory_type="long_term")
                    if dup and dup[0]["similarity"] > 0.6:
                        for e in store.entries:
                            if e.id != dup[0]["id"]:
                                continue
                            meta = dict(e.metadata)
                            old_w = float(meta.get("weight", 0.9) or 0.9)
                            meta["weight"] = round(min(1.0, _bump_weight(max(old_w, 0.9))), 2) if old_w < 1.0 else 1.0
                            meta["mention_count"] = int(meta.get("mention_count", 1)) + 1
                            meta["last_seen"] = now
                            e.metadata = meta
                            break
                        long_n += 1
                        continue
                    store.add(
                        text=text,
                        metadata={"type": "long_fact", "source": "day_summary",
                                  "weight": round(min(0.95, 0.6 + 0.3 * w + 0.2 * explicit), 2),
                                  "mention_count": 1, "explicit": explicit, "first_seen": now, "last_seen": now},
                        memory_type="long_term",
                    )
                    long_n += 1
                else:
                    self._store_short(store, text, max(MEM_INITIAL_WEIGHT, min(0.5, w)), explicit=explicit)
                    short_n += 1
            short_n += self._merge_duplicate_short(store)
            store._save()
        # 只有**真的存进去东西**才算"这天分过级"：否则模型回了一堆能解析但都入不了库的
        # 碎片（text<4 字）时，这天会被永久标记跳过、再也不重试。
        # （LLM 异常 / 解析为空这两条路本来就提前 return，不会标记 —— 那是原有的正确行为。）
        if long_n or short_n:
            self.mark_classified_day(user_id, char_id, date_str)
        return {"day_messages": len(msgs), "long": long_n, "short": short_n}

    @staticmethod
    def _parse_json_memory_items(raw: str) -> list:
        """从模型输出里稳健地解析 JSON 数组（容忍 ```json 包裹、前后杂文本）。"""
        text = (raw or "").strip()
        if not text:
            return []
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        data = None
        try:
            data = json.loads(m.group(0))
        except Exception:
            for c in re.findall(r"\[[^\]]*\]", text)[::-1]:
                try:
                    data = json.loads(c)
                    break
                except Exception:
                    continue
        if not isinstance(data, list):
            return []
        items = []
        for it in data:
            if isinstance(it, dict) and (it.get("text") or "").strip():
                items.append(it)
            elif isinstance(it, str) and it.strip():
                items.append({"text": it.strip(), "type": "event", "explicit": 0})
        return items

    def _merge_duplicate_short(self, store: "VectorStore") -> int:
        """合并短记忆库里的近重复条目（同一件事再次提及时合并累计权重）。返回合并条数。"""
        entries = [e for e in store.entries if e.memory_type == "short_term"]
        if len(entries) < 2:
            return 0
        remove_ids = set()
        merged = 0
        for i in range(len(entries)):
            if entries[i].id in remove_ids:
                continue
            for j in range(i + 1, len(entries)):
                if entries[j].id in remove_ids:
                    continue
                if _text_sim(entries[i].text, entries[j].text) >= 0.62:
                    ei, ej = entries[i], entries[j]
                    mi, mj = dict(ei.metadata), dict(ej.metadata)
                    wi = max(float(mi.get("weight", MEM_INITIAL_WEIGHT) or MEM_INITIAL_WEIGHT),
                             float(mj.get("weight", MEM_INITIAL_WEIGHT) or MEM_INITIAL_WEIGHT))
                    mi["weight"] = round(_bump_weight(wi), 2)
                    mi["mention_count"] = int(mi.get("mention_count", 1)) + int(mj.get("mention_count", 1))
                    mi["last_seen"] = max(float(mi.get("last_seen", 0) or 0), float(mj.get("last_seen", 0) or 0))
                    mi["expires_at"] = time.time() + SHORT_MEM_TTL
                    if len(ej.text) > len(ei.text):
                        ei.text = ej.text[:300]
                    ei.metadata = mi
                    remove_ids.add(ej.id)
                    merged += 1
        if remove_ids:
            store.entries = [e for e in store.entries if e.id not in remove_ids]
            if store.entries:
                store.vectors = store.vectorizer.fit_transform([x.text for x in store.entries])
            else:
                store.vectors = None
        return merged
    def _get_store(self, user_id: str, char_id: str = "") -> VectorStore:
        """获取用户的记忆存储；传入 char_id 时按角色隔离（不同智能体互不串记忆/上下文）"""
        key = f"{user_id}__{char_id}" if char_id else user_id
        if key not in self._stores:
            self._stores[key] = VectorStore(f"user_{key}", storage_dir=user_dir(user_id or "anon", "memory"))
        return self._stores[key]

    @staticmethod
    def _hkey(user_id: str, char_id: str = "") -> str:
        return f"{user_id}__{char_id}" if char_id else user_id

    @staticmethod
    def _safe_name(hk: str) -> str:
        return hk.replace("/", "_").replace("\\", "_").replace(":", "_")

    @staticmethod
    def _hk_user(hk: str) -> str:
        return (hk.split("__", 1)[0] if hk else "") or "anon"

    def _history_file(self, hk: str) -> Path:
        # 按用户物理分开：/data/users/<用户>/chat_history/<用户__角色>.json
        return user_dir(self._hk_user(hk), "chat_history") / f"{self._safe_name(hk)}.json"

    def _migrate_legacy_layout(self):
        """老版本所有用户挤在一个目录 → 搬进 /data/users/<用户>/<类型>/（原文件留 .moved）"""
        import shutil as _sh
        moved = 0
        try:
            for sub, sep, legacy_dir in (("chat_history", "__", CHAT_HISTORY_DIR),
                                         ("chat_logs", "___", DAY_LOG_DIR),
                                         ("outlines", "___", OUTLINE_DIR)):
                if not legacy_dir.exists():
                    continue
                for p in legacy_dir.glob("*.json"):
                    hk = p.stem.rsplit(sep, 1)[0] if sep else p.stem
                    dst = user_dir(self._hk_user(hk), sub) / p.name
                    try:
                        if not dst.exists():
                            _sh.copy2(p, dst)
                            moved += 1
                        p.rename(p.with_suffix(".json.moved"))
                    except Exception:
                        pass
            if MEMORY_DIR.exists():
                for p in MEMORY_DIR.glob("user_*.json"):
                    hk = p.stem[len("user_"):]
                    dst = user_dir(self._hk_user(hk), "memory") / p.name
                    try:
                        if not dst.exists():
                            _sh.copy2(p, dst)
                            moved += 1
                        p.rename(p.with_suffix(".json.moved"))
                    except Exception:
                        pass
            if moved:
                logger.info(f"[记忆] 已把 {moved} 个文件按用户拆到 users/<用户>/ 下")
        except Exception as e:
            logger.warning(f"[记忆] 老目录迁移失败: {e}")

    def _load_histories(self):
        """启动时从每个用户自己的目录恢复对话历史（/data/users/<用户>/chat_history/**）

        同时读回压缩水位 `_sum_done`（存在同目录 <hk>.sum.json）。水位只存内存时，
        重启就丢了 → 历史不再裁剪后，每次重启都会把整段历史重新摘要/抽事实一遍。
        """
        try:
            for up in get_users_dir().glob("*/chat_history/*.json"):
                try:
                    if up.name.endswith(".sum.json"):
                        continue   # 这是压缩水位文件，不是历史，别当历史读
                    data = json.loads(up.read_text(encoding="utf-8"))
                    if not isinstance(data, list):
                        continue   # 兼容旧格式：历史文件本来就是 list，非 list 一律跳过
                    hk = up.stem
                    self._chat_histories[hk] = data
                    sp = up.with_suffix(".sum.json")
                    if sp.exists():
                        try:
                            sd = json.loads(sp.read_text(encoding="utf-8"))
                            self._sum_done[hk] = int((sd or {}).get("sum_done") or 0)
                        except Exception:
                            # 水位文件坏了：保守当成"已全部整理过"，别导致重压整段历史
                            self._sum_done[hk] = len(data)
                    else:
                        # 旧数据（本改动之前的历史文件）没有水位文件。首次加载默认视为
                        # 「这段历史已经全部整理过」= len(history)：否则升级后第一次启动
                        # 会把几个月的历史整段重新摘要+抽事实，既慢又烧钱，还会把陈年旧事
                        # 当新信息再记一遍。新写入的历史都会带水位文件，不受此默认影响。
                        self._sum_done[hk] = len(data)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[记忆] 对话历史恢复失败: {e}")

    def _persist_history(self, hk: str):
        try:
            _atomic_write_text(self._history_file(hk),
                               json.dumps(self._history_for_disk(hk), ensure_ascii=False))
            # 顺带落盘压缩水位：全新用户写第一条消息就会生成水位文件（值 0），
            # 重启后才不会被当成"没有水位的旧数据"而误判成"已全部整理过"。
            self._persist_sum_done(hk)
        except Exception as e:
            logger.warning(f"[记忆] 对话历史持久化失败: {e}")

    # 落盘历史里最多保留最近这么多条消息的图片原文，更早的只留占位符。
    HISTORY_KEEP_IMG_MSGS = 20

    def _history_for_disk(self, hk: str) -> list:
        """落盘用的历史副本：把较早消息里的图片 base64 换成占位符。

        图片 data URL 一条可达几万字符，而历史是"每条消息都整文件重写"，
        留着会让 _persist_history 随历史线性变慢、文件无限膨胀。
        内存里的原文不动：前端本次会话翻历史照旧有图；只有重启后从文件恢复时，
        早期图片显示为 [图片]。最近 HISTORY_KEEP_IMG_MSGS 条仍保留原文
        （刚发/刚看的图重启后还在）。
        """
        msgs = self._chat_histories.get(hk, [])
        n = len(msgs)
        out = []
        for i, m in enumerate(msgs):
            if "images" not in m or (n - i) <= self.HISTORY_KEEP_IMG_MSGS:
                out.append(m)
                continue
            m2 = dict(m)
            m2["images"] = ["[图片]" for _ in (m.get("images") or [])]
            out.append(m2)
        return out

    def _sum_file(self, hk: str) -> Path:
        """压缩水位文件：与历史文件同目录、同用户，按用户物理分开。"""
        return user_dir(self._hk_user(hk), "chat_history") / f"{self._safe_name(hk)}.sum.json"

    def _persist_sum_done(self, hk: str):
        try:
            _atomic_write_text(self._sum_file(hk),
                               json.dumps({"sum_done": int(self._sum_done.get(hk) or 0)}))
        except Exception as e:
            logger.warning(f"[记忆] 压缩水位持久化失败: {e}")

    def _day_log_file(self, hk: str, date_str: str) -> Path:
        return user_dir(self._hk_user(hk), "chat_logs") / f"{self._safe_name(hk)}___{date_str}.json"

    def _day_log(self, hk: str, date_str: str) -> List[dict]:
        key = f"{hk}|{date_str}"
        if key not in self._day_logs:
            p = self._day_log_file(hk, date_str)
            if p.exists():
                try:
                    self._day_logs[key] = json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    self._day_logs[key] = []
            else:
                self._day_logs[key] = []
        return self._day_logs[key]

    def _persist_day_log(self, hk: str, date_str: str):
        key = f"{hk}|{date_str}"
        try:
            _atomic_write_text(self._day_log_file(hk, date_str),
                               json.dumps(self._day_logs.get(key, []), ensure_ascii=False))
        except Exception:
            pass

    def get_messages_for_date(self, user_id: str, date_str: str, char_id: str = "") -> List[dict]:
        """某一天该 (用户,角色) 的全部聊天消息（按天快照，不受历史裁剪影响）"""
        with self._lock:
            return list(self._day_log(self._hkey(user_id, char_id), date_str))

    def _outline_file(self, hk: str, date_str: str) -> Path:
        return user_dir(self._hk_user(hk), "outlines") / f"{self._safe_name(hk)}___{date_str}.json"

    def has_daily_outline(self, user_id: str, date_str: str, char_id: str = "") -> bool:
        return self._outline_file(self._hkey(user_id, char_id), date_str).exists()

    def get_daily_outline(self, user_id: str, date_str: str, char_id: str = "") -> str:
        p = self._outline_file(self._hkey(user_id, char_id), date_str)
        if p.exists():
            try:
                return (json.loads(p.read_text(encoding="utf-8")) or {}).get("outline", "")
            except Exception:
                pass
        return ""

    def save_daily_outline(self, user_id: str, date_str: str, outline: str, char_id: str = ""):
        """保存某天聊天大纲：写文件（幂等防重复）+ 同步进 RAG 记忆库（summary 型，可被检索）"""
        outline = (outline or "").strip()
        if not outline:
            return
        hk = self._hkey(user_id, char_id)
        try:
            _atomic_write_text(self._outline_file(hk, date_str),
                               json.dumps({"date": date_str, "char_id": char_id, "outline": outline}, ensure_ascii=False))
            store = self._get_store(user_id, char_id)
            # 同一天同一角色只保留最新一条大纲，避免自动总结器每次重跑都堆一条
            try:
                store.entries = [
                    e for e in store.entries
                    if not (e.memory_type == "summary"
                            and (e.metadata or {}).get("type") == "daily_outline"
                            and (e.metadata or {}).get("date") == date_str
                            and (e.metadata or {}).get("character_id") == char_id)
                ]
            except Exception:
                pass
            store.add(
                text=f"[{date_str} 聊天大纲] {outline[:800]}",
                metadata={"type": "daily_outline", "date": date_str, "character_id": char_id},
                memory_type="summary",
            )
            store._save()
        except Exception as e:
            logger.warning(f"[记忆] 每日大纲保存失败: {e}")

    # ============ 当天事件分条（带权重，影响后续记忆归纳） ============

    def _events_file(self, hk: str, date_str: str) -> Path:
        return user_dir(self._hk_user(hk), "outlines") / f"{self._safe_name(hk)}__events___{date_str}.json"

    def has_daily_events(self, user_id: str, date_str: str, char_id: str = "") -> bool:
        return self._events_file(self._hkey(user_id, char_id), date_str).exists()

    def get_daily_events(self, user_id: str, date_str: str, char_id: str = "") -> list:
        """返回当天分条事件 [{text, weight, ts, topic}]，按时间正序。"""
        p = self._events_file(self._hkey(user_id, char_id), date_str)
        if p.exists():
            try:
                evs = (json.loads(p.read_text(encoding="utf-8")) or {}).get("events", []) or []
                # 旧版生成器可能给出 0.7~1.0 的高初始权重，读取时收敛到新上限 0.45（新格式本来就不会超）
                for e in evs:
                    try:
                        w = float(e.get("weight", 0.3) or 0.3)
                        if w > MEM_MAX_EVENT_WEIGHT:
                            e["weight"] = MEM_MAX_EVENT_WEIGHT
                    except Exception:
                        e["weight"] = MEM_INITIAL_WEIGHT
                return evs
            except Exception:
                pass
        return []

    def save_daily_events(self, user_id: str, date_str: str, char_id: str, events: list):
        """保存当天分条事件（带权重）。与当天已有事件按语义相似度合并：
        同一件事再次被提起 → 权重上涨（30→50→70…），而不是重复堆一条。
        初始权重不超过 MEM_MAX_EVENT_WEIGHT，靠重复提及自然爬升。"""
        events = [e for e in (events or []) if (e.get("text") or "").strip()]
        if not events:
            return
        hk = self._hkey(user_id, char_id)
        try:
            merged = list(self.get_daily_events(user_id, date_str, char_id))
            now = time.time()
            for ev in events:
                text = ev.get("text", "").strip()[:200]
                if not text:
                    continue
                try:
                    w = max(0.0, min(1.0, float(ev.get("weight", MEM_INITIAL_WEIGHT) or MEM_INITIAL_WEIGHT)))
                except Exception:
                    w = MEM_INITIAL_WEIGHT
                target, best = None, 0.0
                for e in merged:
                    sim = _text_sim(e.get("text", ""), text)
                    if sim > best:
                        best, target = sim, e
                if target and best >= 0.45:
                    # 同一天再次聊到同一件事 → 权重上涨，刷新时间
                    target["weight"] = round(_bump_weight(target.get("weight", w)), 2)
                    target["ts"] = now
                    if len(text) > len(target.get("text", "")):
                        target["text"] = text
                else:
                    merged.append({"text": text, "weight": round(min(MEM_MAX_EVENT_WEIGHT, w), 2), "ts": now})
            merged.sort(key=lambda e: e.get("ts", 0))
            _atomic_write_text(self._events_file(hk, date_str),
                               json.dumps({"date": date_str, "char_id": char_id, "events": merged}, ensure_ascii=False, indent=2))
            logger.info(f"[记忆] {user_id}/{char_id}/{date_str} 当天事件已保存 {len(merged)} 条（输入 {len(events)} 条）")
        except Exception as e:
            logger.warning(f"[记忆] 当天事件保存失败: {e}")

    def save_full_day_summary(self, user_id: str, char_id: str = "", date_str: str = "") -> bool:
        """更新"全天记忆总结"：把当天的分条事件合成一条（幂等，同一天只保留一条最新）。

        和"每轮小结/每日大纲"分开：大纲记流水，全天总结记今天整体发生了什么、关系/情绪走向。
        同一天重复调用只会覆盖那一条旧的全天总结，不会越堆越多。
        """
        try:
            date_str = date_str or datetime.now().strftime("%Y-%m-%d")
            events = self.get_daily_events(user_id, date_str, char_id) or []
            outline = self.get_daily_outline(user_id, date_str, char_id) or ""
            if not events and not outline:
                return False
            lines = []
            for ev in events[:20]:
                t = (ev.get("text") or "").strip()
                if t:
                    lines.append(f"- {t}")
            body = "\n".join(lines) or f"- {outline[:200]}"
            _nl = "\\n" if outline else ""
            text = f"【{date_str} 全天总结】{outline[:200]}{_nl}{body}"
            store = self._get_store(user_id, char_id)
            # 同一天只留一条：先删掉旧的"全天总结"
            try:
                old = [e for e in store.entries
                       if e.memory_type == "summary" and f"【{date_str} 全天总结】" in (e.text or "")]
                if old:
                    store.entries = [e for e in store.entries if e not in old]
            except Exception:
                pass
            store.add(text=text[:1000], metadata={"type": "full_day_summary", "date": date_str},
                      memory_type="summary")
            try:
                store.vectors = store.vectorizer.fit_transform([e.text for e in store.entries])
            except Exception:
                pass
            store._save()
            logger.info(f"[记忆] 全天总结已更新 {user_id}/{char_id}/{date_str}（{len(lines)} 条事件）")
            return True
        except Exception as e:
            logger.warning(f"[记忆] 全天总结失败: {e}")
            return False

    def get_user_char_pairs(self) -> List[tuple]:
        """返回全部出现过聊天的 (user_id, char_id) 对，供后台调度按角色独立触发（不依赖当前前台角色）"""
        pairs = set()

        def add_from(hk: str):
            if "__" in hk:
                u, c = hk.split("__", 1)
                pairs.add((u, c))
            elif hk:
                pairs.add((hk, ""))

        for hk in self._chat_histories:
            add_from(hk)
        for pattern, sep in (("*/chat_logs/*.json", "___"), ("*/outlines/*.json", "___"),
                             ("*/chat_history/*.json", "__")):
            for p in get_users_dir().glob(pattern):
                if p.name.endswith(".sum.json"):
                    continue   # 压缩水位文件不是聊天记录，别从它反推 (用户,角色) 对
                stem = p.stem
                hk = stem.rsplit(sep, 1)[0] if sep else stem
                add_from(hk)
        return sorted(pairs)
    
    def add_message(self, user_id: str, role: str, content: str, character_id: str = "", pieces: list = None, images: list = None):
        """添加对话消息。pieces 可选：AI 多消息回复的分条内容 [{text,type,delay}]，
        存进历史记录，前端翻看历史时能按条还原（而不是合并成一大坨）。
        images 可选：用户/助手消息附带的图片（data URL / http URL），透传到前端与视觉链路。"""
        record = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "character_id": character_id,
        }
        if images:
            record["images"] = [str(i)[:20000] for i in (images or [])[:4]]
        if pieces:
            record["pieces"] = pieces
        with self._lock:
            hk = self._hkey(user_id, character_id)
            if hk not in self._chat_histories:
                self._chat_histories[hk] = []
            self._chat_histories[hk].append(record)
            self._persist_history(hk)
            # 按天快照：每天一个文件，供凌晨4点生成"前一天聊天大纲"，不受历史裁剪影响
            date_str = datetime.now().strftime("%Y-%m-%d")
            day_log = self._day_log(hk, date_str)
            day_log.append(dict(record))
            self._persist_day_log(hk, date_str)

    def save_user_profile(self, user_id: str, profile: str):
        """保存用户人设档案（预先填写的用户信息）"""
        profile = (profile or "").strip()
        if not profile:
            return
        store = self._get_store(user_id)
        # 覆盖式写入：先清掉旧的 user_profile，再存新档案，保证 get 拿到最新
        store.entries = [e for e in store.entries if e.memory_type != "user_profile"]
        store.add(
            text=profile,
            metadata={"type": "user_profile", "source": "manual"},
            memory_type="user_profile",
        )
        if store.entries:
            store.vectors = store.vectorizer.fit_transform([e.text for e in store.entries])
        store._save()

    def get_user_profile(self, user_id: str) -> str:
        """获取用户人设档案（手动填写 + 聊天中自动摘取的事实）"""
        store = self._get_store(user_id)
        parts = [e.text for e in store.entries if e.memory_type == "user_profile"]
        return "\n".join(parts).strip()

    def add_user_fact(self, user_id: str, fact: str):
        """把聊天中自动摘取到的用户事实追加进档案"""
        fact = (fact or "").strip()
        if not fact or len(fact) < 4:
            return
        store = self._get_store(user_id)
        store.add(
            text=fact,
            metadata={"type": "user_profile", "source": "auto"},
            memory_type="user_profile",
        )

    def pending_compress(self, user_id: str, char_id: str = "") -> List[dict]:
        """返回"还没整理过、又已超出保留窗口"的旧对话。

        与老实现的关键区别：**不再靠裁剪历史来推进**，而是用 `_sum_done` 水位记录
        "已经整理到第几条"。整理过的不再重复喂给模型，但原文一律留在历史里。
        （原实现 trim 掉原文后，用户翻聊天记录会发现以前的对话整段消失。）
        """
        with self._lock:
            hk = self._hkey(user_id, char_id)
            history = self._chat_histories.get(hk, [])
            start = int(self._sum_done.get(hk) or 0)      # 上次整理到哪
            end = self._sum_up_to.get(hk)                 # 若上层显式标了话题边界就用它
            if end is None:
                keep = self._summary_interval * 2         # 保留最近 N 条不整理
                end = len(history) - keep
            end = int(min(max(0, end), len(history)))
            if end <= start:
                return []
            return history[start:end]

    HARD_KEEP = 3000      # 只在这个条数以上才允许硬裁剪（防无限增长）
    # 基准（本机，3000 条历史、每 20 条一张 3 万字符的 base64 图）：剥掉早期图片原文后
    # 单次 _persist_history ≈ 6.7ms、文件 ~590KB；不剥图则 ≈ 29ms、文件 ~5MB。
    # 仍 <50ms，故 HARD_KEEP 保持 3000（若将来内容变大导致超 50ms，降到 1500）。

    def trim_history(self, user_id: str, count: int, char_id: str = "", force: bool = False):
        """从对话历史头部移除消息（**除极端长以外不再使用**，历史默认只追加不裁剪）。

        force=False 时只允许把历史裁到 HARD_KEEP 条；其余情况拒绝，避免哪天又被
        某个调用方"顺手裁一下"把用户的历史记录删掉（会打日志指出调用来源）。
        """
        import traceback as _tb
        with self._lock:
            hk = self._hkey(user_id, char_id)
            history = self._chat_histories.get(hk, [])
            if not force and len(history) - int(count) < self.HARD_KEEP:
                caller = "?"
                try:
                    for fr in _tb.extract_stack()[:-1][::-1]:
                        if not (fr.filename.endswith("memory\\__init__.py") or fr.name == "trim_history"):
                            caller = f"{fr.filename.split(chr(92))[-1]}:{fr.lineno}:{fr.name}"
                            break
                except Exception:
                    pass
                logger.warning(f"[记忆] 拒绝裁剪历史 {hk}（要删 {count} 条，剩 {len(history)-int(count)} 条 < {self.HARD_KEEP}）"
                               f" 调用方={caller}")
                return
            if history:
                self._chat_histories[hk] = history[count:]
                if self._sum_up_to.get(hk) is not None:
                    self._sum_up_to[hk] = max(0, int(self._sum_up_to[hk]) - int(count))
                # 水位跟着往前挪：硬裁掉的条数如果不减，"已整理到第几条"就指向了错位的位置，
                # 裁后中间那一段会永远被当成已整理、再也不会进摘要/事实提取。
                self._sum_done[hk] = max(0, int(self._sum_done.get(hk) or 0) - int(count))
                self._persist_history(hk)   # 内部会一并持久化水位

    async def compress_history(self, user_id: str, llm_async_fn=None, char_id: str = ""):
        """记忆流水线：把旧对话压缩成摘要 + 摘取用户事实，再裁剪对话历史。
        llm_async_fn: async callable(prompt: str) -> str，用于调用 LLM 做整理。
        先回复后整理：调用方在拿到回复后再触发本函数，前端显示不受影响。
        返回 {"summary": str, "facts": list[str], "character_id": str}，
        供上层同步到 Letta 等外部记忆后端；无可压缩内容时返回 None。
        """
        with self._pipeline_lock:
            try:
                old = self.pending_compress(user_id, char_id)
                if not old:
                    return None
                pairs = [(old[i], old[i + 1]) for i in range(0, len(old) - 1, 2)]
                convo = "\n".join(
                    f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content'][:400]}"
                    for pair in pairs for m in pair
                )
                if not convo:
                    return None

                # 1) 压缩为关键记忆（摘要）
                summary = ""
                if llm_async_fn:
                    try:
                        summary_prompt = (
                            "你是记忆整理器。把下面这段对话压缩成 2-4 条关键事实记忆，"
                            "只保留：用户的重要个人信息、事件、喜好、情绪状态、双方关系变化。"
                            "用简洁中文要点输出，每条一行，不要解释，不要客套。\n\n对话：\n" + convo
                        )
                        summary = (await llm_async_fn(summary_prompt)).strip()
                    except Exception as e:
                        logger.warning(f"[记忆流水线] 摘要生成失败: {e}")

                if summary:
                    store = self._get_store(user_id, char_id)
                    store.add(
                        text=summary[:800],
                        metadata={"type": "auto_summary", "character_id": old[0].get("character_id", "")},
                        memory_type="summary",
                    )

                # 2) 摘取用户事实（只取用户消息；骂人/互怼的那种整句跳过，供人物档案使用）
                user_msgs = [m["content"] for m in old
                             if m["role"] == "user" and not _looks_like_insult(m.get("content") or "")]
                new_facts: list[str] = []
                if user_msgs and llm_async_fn:
                    try:
                        fact_prompt = (
                            "你是用户画像提取器。从下面的用户发言中提取关于【用户本人】的事实信息，"
                            "例如职业、爱好、家庭、家乡、性格、生活习惯、喜好、健康状况、重大事件。"
                            "只提取明确的、客观的、值得长期记住的信息；不要猜测；没有就输出'无'。"
                            "每条一行，用简洁中文。\n\n用户发言：\n" +
                            "\n".join(u[:400] for u in user_msgs)
                        )
                        facts = (await llm_async_fn(fact_prompt)).strip()
                        # 亲属身故这类敏感事实，模型可能从骂人话（"你妈死了"）里误提取，
                        # 代码层再兜一道：原文里没有"不像骂人"的相关发言就丢弃。
                        _grief_words = ("去世", "丧亲", "过世", "离世", "病逝", "走了", "没了", "死")
                        for line in facts.splitlines():
                            line = line.strip().lstrip("-•·0123456789.、")
                            if not line or line == "无":
                                continue
                            if any(w in line for w in _grief_words) and (line.find("母") >= 0 or line.find("父") >= 0
                                                                        or line.find("家") >= 0 or line.find("亲") >= 0):
                                if not any(not _looks_like_insult(u) and any(w in u for w in _grief_words)
                                           for u in user_msgs):
                                    logger.warning(f"[记忆流水线] 疑似从骂人话里提取的亲属事实，已丢弃: {line[:40]}")
                                    continue
                            self.add_user_fact(user_id, line)
                            new_facts.append(line)
                    except Exception as e:
                        logger.warning(f"[记忆流水线] 用户事实提取失败: {e}")

                # 3) 只推进"已整理到哪"的水位，**不删除原文**——删了用户翻历史就发现聊天记录消失。
                #    原文一直留在主历史里；给模型的上下文由调用方按 get_recent_history(limit=…) 取最近窗口，
                #    不会因为这里不裁剪就把全部历史塞给模型。
                #    空间够（本机磁盘），只在极端长（>HARD_KEEP 条）时才做一次硬裁剪兜底。
                try:
                    with self._lock:
                        hk = self._hkey(user_id, char_id)
                        self._sum_done[hk] = int(self._sum_done.get(hk) or 0) + len(old)
                        self._sum_up_to.pop(hk, None)
                        self._persist_sum_done(hk)   # 水位不能只留内存，否则重启后这段会被重压
                        hist_len = len(self._chat_histories.get(hk) or [])
                        if hist_len > self.HARD_KEEP:
                            self.trim_history(user_id, hist_len - self.HARD_KEEP, char_id)
                except Exception:
                    pass
                logger.info(f"[记忆流水线] 用户 {user_id} 已压缩 {len(old)} 条旧对话入记忆库")
                return {
                    "summary": summary,
                    "facts": new_facts,
                    "character_id": old[0].get("character_id", ""),
                }
            except Exception as e:
                logger.error(f"[记忆流水线] 失败: {e}")
                return None
    
    def get_context(self, user_id: str, query: str, character_id: str = "", top_k: int = 5) -> str:
        """获取相关记忆上下文：相似度检索 + 无条件注入当天大纲/事件/高权重记忆（解决"还记得吗"查不到的问题）"""
        store = self._get_store(user_id, character_id)
        
        # 搜索长期记忆和摘要
        memories = store.search(query, top_k=top_k, memory_type="long_term")
        summaries = store.search(query, top_k=top_k, memory_type="summary")
        short_mems = store.search(query, top_k=top_k, memory_type="short_term")
        
        context_parts = []

        # 当天聊天大纲 + 分条事件：无论相似度高低都带上（用户问"今天/早上/刚才说的那个"时能直接接住）
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            outline = self.get_daily_outline(user_id, today, character_id)
            if outline:
                context_parts.append("【今天的聊天大纲（今天发生过的，别人问起要接得住）】")
                context_parts.append(f"- {outline}")
            events = self.get_daily_events(user_id, today, character_id)
            if events:
                context_parts.append("【今天聊到的具体事件（每条带权重：权重表示你对用户的了解程度，权重低=还不确定/记不牢，聊到相关话题时语气可以带回忆感）】")
                for ev in events[:15]:
                    w = ev.get("weight")
                    wstr = f"{float(w):.0%}" if isinstance(w, (int, float)) else ""
                    context_parts.append(f"- [权重{wstr}] {ev.get('text', '')}")
        except Exception as e:
            logger.warning(f"[记忆] 读取当天大纲/事件失败: {e}")

        # 高权重/反复提及的记忆：不靠相似度，无条件带上（防止泛指问题查不到具体事实）
        try:
            all_mems = store.list_all()
            def _score(m):
                meta = m.get("metadata") or {}
                try:
                    w = float(meta.get("weight", 0.5))
                    cnt = int(meta.get("mention_count", 0))
                    return w * 10 + cnt
                except Exception:
                    return 0
            top = [m for m in all_mems if m.get("memory_type") in ("long_term", "short_term")]
            top.sort(key=_score, reverse=True)
            strong = [m for m in top[:6] if _score(m) >= 6]
            if strong:
                context_parts.append("【你牢牢记着的关于用户的事（直接相关，用户提及时要自然接住）】")
                for m in strong:
                    context_parts.append(f"- {m.get('text', '')}")
        except Exception as e:
            logger.warning(f"[记忆] 读取高权重记忆失败: {e}")
        
        if summaries:
            context_parts.append("【历史摘要】")
            for s in summaries[:3]:
                context_parts.append(f"- {s['text']}")
        
        if memories:
            context_parts.append("【相关记忆】")
            for m in memories[:3]:
                meta = m.get("metadata") or {}
                w = 0.9
                mentions = 0
                try:
                    w = float(meta.get("weight", 0.9))
                    mentions = int(meta.get("mention_count", 0))
                except Exception:
                    pass
                tag = ""
                if w < 0.30:
                    tag = "（这条记不太牢了，想起来时可以带一点'啊我想想…想起来了'的回忆感，别像背资料一样报出来）"
                elif w >= 0.85 and mentions >= 2:
                    tag = "（这个事你已经反复提过很多次了，气氛合适时可以像真人一样自然流露出一点点不耐烦，别客气）"
                context_parts.append(f"- {m['text']}{tag}")

        if short_mems:
            context_parts.append("【近期记忆（可能会随时间淡忘）】")
            for m in short_mems[:3]:
                meta = m.get("metadata") or {}
                try:
                    w = float(meta.get("weight", 0.5))
                    mentions = int(meta.get("mention_count", 1))
                except Exception:
                    w, mentions = 0.5, 1
                tag = ""
                if w < 0.30:
                    tag = "（这条记得不太清了，想起来时可以带一点'啊我想想…想起来了'的回忆感）"
                elif w >= 0.85 or mentions >= PROMOTE_MENTIONS:
                    tag = "（你最近反复提这事，别嫌烦但语气可以自然带点态度）"
                context_parts.append(f"- {m['text']}{tag}")
        
        # 添加最近对话历史
        recent_history = self.get_recent_history(user_id, limit=10, char_id=character_id)
        if recent_history:
            context_parts.append("【最近对话】")
            for msg in recent_history:
                role_label = "用户" if msg["role"] == "user" else "AI"
                context_parts.append(f"{role_label}: {msg['content']}")
        
        out = "\n".join(context_parts) if context_parts else ""
        try:
            from runtime.logging_setup import logger_mem
            logger_mem.info(
                f"[retrieve] user={user_id} char={character_id} query={query[:80]!r} "
                f"chars={len(out)}"
            )
        except Exception:
            pass
        return out
    
    def get_recent_history(self, user_id: str, limit: int = 10, char_id: str = "") -> List[dict]:
        """获取最近对话历史"""
        with self._lock:
            history = self._chat_histories.get(self._hkey(user_id, char_id), [])
            return history[-limit:]
    
    def clear_history(self, user_id: str, char_id: str = ""):
        """只清空该 (用户,角色) 的对话历史（不动记忆库与每日大纲）"""
        with self._lock:
            hk = self._hkey(user_id, char_id)
            self._chat_histories[hk] = []
            # 水位一并归零：不清的话清空历史后，新消息要重新涨到旧水位才会被摘要，
            # 中间很长一段新对话都进不了记忆。
            self._sum_done[hk] = 0
            self._sum_up_to.pop(hk, None)
            try:
                self._history_file(hk).unlink(missing_ok=True)
                self._sum_file(hk).unlink(missing_ok=True)
            except Exception:
                pass

    def clear_store(self, user_id: str, char_id: str = ""):
        """清空该 (用户,角色) 隔离记忆库的全部条目（含长/短记忆与摘要）"""
        with self._lock:
            store = self._get_store(user_id, char_id)
            store.clear()
            store._save()

    def clear_day_log(self, user_id: str, date_str: str = "", char_id: str = ""):
        """清空某天（默认今天）的当天聊天记录（当天记忆）"""
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            hk = self._hkey(user_id, char_id)
            key = f"{hk}|{date_str}"
            self._day_logs[key] = []
            try:
                self._day_log_file(hk, date_str).unlink(missing_ok=True)
            except Exception:
                pass
    
    def generate_summary(self, user_id: str, llm_client=None, char_id: str = "") -> Optional[str]:
        """生成对话摘要（需要调用 LLM）"""
        with self._lock:
            history = self._chat_histories.get(self._hkey(user_id, char_id), [])
            if len(history) < self._summary_interval * 2:
                return None
            
            # 取最近一轮的对话
            recent = history[-self._summary_interval * 2:]
            conversation_text = "\n".join([
                f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content']}"
                for m in recent
            ])
            
            # 构建摘要 prompt
            summary_prompt = f"""请将以下对话总结为 2-3 条关键信息，只保留重要的事实和情感状态：

{conversation_text}

关键信息（用简洁的中文）："""
            
            # 如果有 LLM 客户端，调用它生成摘要
            if llm_client and llm_client.available:
                # 异步调用需要在 async 环境中执行
                # 这里返回 prompt，由调用方执行
                return summary_prompt
            
            # 无 LLM 时，简单提取用户的关键陈述
            key_facts = []
            for msg in recent:
                if msg["role"] == "user" and len(msg["content"]) > 10:
                    key_facts.append(msg["content"][:100])
            
            if key_facts:
                summary = "；".join(key_facts[:3])
                store = self._get_store(user_id, char_id)
                store.add(
                    text=summary,
                    metadata={"type": "auto_summary"},
                    memory_type="summary",
                )
                # 清空已摘要的对话历史
                self._chat_histories[self._hkey(user_id, char_id)] = history[self._summary_interval * 2:]
                return summary
            
            return None
    
    def save_fact(self, user_id: str, fact: str, metadata: dict = None, char_id: str = ""):
        """保存一个事实到长期记忆（传 char_id 时写入该角色的隔离记忆库）"""
        store = self._get_store(user_id, char_id)
        store.add(
            text=fact,
            metadata=metadata or {},
            memory_type="long_term",
        )
        try:
            from runtime.logging_setup import logger_mem
            from runtime.db import unified_store
            logger_mem.info(f"[write] user={user_id} char={char_id} long: {fact[:120]}")
            unified_store.record_memory(user_id, char_id, "long", fact, weight=0.9, source="manual")
        except Exception:
            pass

    # ============ 双记忆层：长/短记忆 + 遗忘 + 高频转长 ============

    def save_dual_memory(self, user_id: str, long_items: list = None, short_items: list = None,
                         char_id: str = "", self_items: list = None):
        """模型每轮 JSON 带出的记忆条目入库：
        long → 长期写死（去重）；short → 短期带权重，同类条目被反复提及会刷新 TTL 并累计次数，够格自动转长期。
        self → 模型 `memory_write.self` 里"她**自己**说过、要保持一致"的事，用 record_type=self
               单独标记（不能混进 fact，否则会被当成"关于用户的事实"注入 → 角色自相矛盾）。
        short_items 元素支持 str 或 {"text": str, "weight": float}
        """
        store = self._get_store(user_id, char_id)
        now0 = time.time()
        # long 是"关于用户的事实"，self 是"角色自己说过的事"，都进长期库但用 type/record_type 区分
        _pairs = ([(t, "long_fact") for t in (long_items or [])]
                  + [(t, "self_fact") for t in (self_items or [])])
        for text, _kind in _pairs:
            text = (text or "").strip()
            if len(text) < 4:
                continue
            dup = store.search(text, top_k=1, memory_type="long_term")
            if dup and dup[0]["similarity"] > 0.6:
                # 长期记忆再次被模型确认 → 刷新时间、权重继续爬升（可随聊天修改）
                for e in store.entries:
                    if e.id != dup[0]["id"]:
                        continue
                    meta = dict(e.metadata)
                    old_w = float(meta.get("weight", 0.9) or 0.9)
                    meta["weight"] = round(min(1.0, _bump_weight(max(old_w, 0.9))), 2) if old_w < 1.0 else 1.0
                    meta["mention_count"] = int(meta.get("mention_count", 1)) + 1
                    meta["last_seen"] = now0
                    e.metadata = meta
                    break
                continue
            store.add(
                text=text[:300],
                metadata={"type": _kind, "source": "model", "weight": 0.9, "mention_count": 1,
                          **({"record_type": "self"} if _kind == "self_fact" else {}),
                          "explicit": 1, "first_seen": now0, "last_seen": now0},
                memory_type="long_term",
            )
            try:
                from runtime.logging_setup import logger_mem
                from runtime.db import unified_store
                logger_mem.info(f"[write] user={user_id} char={char_id} long: {text[:120]}")
                unified_store.record_memory(user_id, char_id, "long", text, weight=0.9, source="model")
            except Exception:
                pass
        for item in (short_items or []):
            if isinstance(item, str):
                text, weight = item.strip(), MEM_INITIAL_WEIGHT
            else:
                text = (item or {}).get("text") or ""
                try:
                    weight = max(0.0, min(1.0, float(item.get("weight", MEM_INITIAL_WEIGHT) or MEM_INITIAL_WEIGHT)))
                except Exception:
                    weight = MEM_INITIAL_WEIGHT
            text = (text or "").strip()
            if len(text) < 4:
                continue
            self._store_short(store, text, weight)
            try:
                from runtime.logging_setup import logger_mem
                from runtime.db import unified_store
                logger_mem.info(f"[write] user={user_id} char={char_id} short(w={weight}): {text[:120]}")
                unified_store.record_memory(user_id, char_id, "short", text, weight=weight, source="model")
            except Exception:
                pass
        store._save()

    def _store_short(self, store: "VectorStore", text: str, weight: float, explicit: int = 0):
        """存一条短记忆：
        - 新条目：初始权重低（默认 ~0.30，最高 0.50），代表"刚听说、还不确定"。
        - 已存在（再次提及）：先按时间衰减，再在衰减后的基础上上涨（30→50→70→…→100）。
        晋升长期用综合分（importance/frequency/stability/recency/explicitness）。"""
        now = time.time()
        try:
            w_in = max(0.0, min(1.0, float(weight))) if weight is not None else MEM_INITIAL_WEIGHT
        except Exception:
            w_in = MEM_INITIAL_WEIGHT
        hits = store.search(text, top_k=1, memory_type="short_term")
        if hits and hits[0]["similarity"] > 0.55:
            eid = hits[0]["id"]
            for e in store.entries:
                if e.id == eid:
                    meta = dict(e.metadata)
                    mentions = int(meta.get("mention_count", 1)) + 1
                    meta["mention_count"] = mentions
                    # 先衰减旧权重（时间久了会掉），再因再次提及而上涨
                    old_w = _decay_weight(meta.get("weight"), now, meta.get("last_seen"))
                    meta["weight"] = round(_bump_weight(max(old_w, w_in)), 4)
                    meta["explicit"] = max(int(meta.get("explicit", 0) or 0), explicit)
                    meta["last_seen"] = now
                    meta["expires_at"] = now + SHORT_MEM_TTL  # 再次提及刷新遗忘时间
                    e.metadata = meta
                    if _promote_score(meta, now) >= 0.62:
                        store.add(
                            text=e.text,
                            metadata={"type": "long_fact", "source": "promoted",
                                      "weight": float(meta["weight"]), "mention_count": mentions,
                                      "explicit": meta.get("explicit", 0),
                                      "first_seen": meta.get("first_seen", now), "last_seen": now},
                            memory_type="long_term",
                        )
                        store.entries = [x for x in store.entries if x.id != eid]
                        if store.entries:
                            store.vectors = store.vectorizer.fit_transform([x.text for x in store.entries])
                        else:
                            store.vectors = None
                    break
            return
        store.add(
            text=text[:300],
            metadata={"type": "short_fact", "source": "model",
                      "weight": round(max(MEM_INITIAL_WEIGHT, min(0.5, w_in)), 4),
                      "mention_count": 1, "explicit": explicit,
                      "first_seen": now, "last_seen": now,
                      "expires_at": now + SHORT_MEM_TTL},
            memory_type="short_term",
        )

    def expire_and_promote(self, user_id: str, char_id: str = "") -> bool:
        """后台清理：过期短记忆遗忘；提及次数够或权重高的短记忆转长期。
        不传 char_id 时遍历该用户全部角色隔离库。返回是否有变化"""
        if char_id:
            stores = [self._get_store(user_id, char_id)]
        else:
            # _stores 的键是 f"{user_id}__{char_id}"（"user_" 前缀只在 VectorStore.name 上），
            # 原来按 "user_{uid}__" 匹配恒假 → 短记忆衰减/过期/晋升/去重从未执行
            stores = [s for k, s in self._stores.items()
                      if k == user_id or str(k).startswith(f"{user_id}__")]
        changed = False
        for store in stores:
            if self._clean_store(store):
                changed = True
        return changed

    def _clean_store(self, store: "VectorStore") -> bool:
        """对单个记忆库执行维护：短记忆权重随时间衰减；过期短记忆遗忘；综合分够则转长期。"""
        with store._lock:
            now = time.time()
            keep = []
            promote = []
            changed = False
            for e in store.entries:
                if e.memory_type == "short_term":
                    meta = dict(e.metadata)
                    old_w = meta.get("weight")
                    decayed = _decay_weight(old_w, now, meta.get("last_seen"))
                    if decayed != old_w:
                        meta["weight"] = round(decayed, 4)
                        e.metadata = meta
                        changed = True
                    try:
                        exp = float(meta.get("expires_at", 0) or 0)
                    except Exception:
                        exp = 0
                    if exp and exp <= now:
                        changed = True
                        continue  # 固定时间后遗忘
                    if _promote_score(meta, now) >= 0.62:
                        promote.append(e)
                        changed = True
                        continue
                keep.append(e)
            for e in promote:
                meta = dict(e.metadata)
                keep.append(MemoryEntry(
                    id=f"mem_{int(time.time() * 1000)}_{len(keep)}",
                    text=e.text[:300],
                    metadata={"type": "long_fact", "source": "promoted",
                              "weight": meta.get("weight", 0.6), "mention_count": meta.get("mention_count", 1),
                              "explicit": meta.get("explicit", 0),
                              "first_seen": meta.get("first_seen", now), "last_seen": now},
                    created_at=datetime.now().isoformat(),
                    memory_type="long_term",
                ))
            if len(keep) != len(store.entries) or changed:
                store.entries = keep
                if keep:
                    store.vectors = store.vectorizer.fit_transform([x.text for x in keep])
                else:
                    store.vectors = None
                store._save()
                return True
        return False
    
    def get_memory_tags(self, user_id: str) -> List[str]:
        """获取用户的记忆标签"""
        store = self._get_store(user_id)
        tags = set()
        for entry in store.entries:
            for tag in entry.metadata.get("tags", []):
                tags.add(tag)
        return sorted(list(tags))
    
    def add_memory_tag(self, user_id: str, tag: str):
        """添加记忆标签"""
        # 标签是虚拟概念，通过搜索实现
        pass


# ============ RAG 知识库 ============

class KnowledgeBase:
    """RAG 知识库"""
    
    def __init__(self, storage_dir: Path = KNOWLEDGE_DIR):
        self.storage_dir = storage_dir
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.store = VectorStore("knowledge", storage_dir=storage_dir)
        self._document_cache: Dict[str, str] = {}  # filepath -> content hash
    
    def _read_file(self, filepath: Path) -> str:
        """读取文件内容"""
        suffix = filepath.suffix.lower()
        
        if suffix in [".txt", ".md", ".py", ".js", ".json", ".yaml", ".yml", ".csv"]:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        
        elif suffix == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(str(filepath))
                return "\n".join(page.extract_text() or "" for page in reader.pages)
            except Exception as e:
                logger.warning(f"读取 PDF 失败 [{filepath}]: {e}")
                return ""
        
        elif suffix in [".docx", ".doc"]:
            try:
                import docx
                doc = docx.Document(str(filepath))
                return "\n".join(p.text for p in doc.paragraphs)
            except Exception as e:
                logger.warning(f"读取 DOCX 失败 [{filepath}]: {e}")
                return ""
        
        return ""
    
    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 100) -> List[str]:
        """文本分块"""
        if len(text) <= chunk_size:
            return [text]
        
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            chunk = text[start:end]
            chunks.append(chunk)
            start = end - overlap
        
        return chunks
    
    def index_document(self, filepath: Path, char_id: str = "") -> int:
        """索引单个文档"""
        content = self._read_file(filepath)
        if not content:
            return 0
        
        # 检查是否已索引且未变化
        import hashlib
        content_hash = hashlib.md5(content.encode()).hexdigest()
        if str(filepath) in self._document_cache and self._document_cache[str(filepath)] == content_hash:
            return 0
        
        # 分块
        chunks = self._chunk_text(content)
        
        # 删除旧索引（如果有）
        self.store.entries = [e for e in self.store.entries if e.metadata.get("source") != str(filepath)]
        
        # 添加新索引
        for i, chunk in enumerate(chunks):
            self.store.add(
                text=chunk,
                metadata={
                    "source": str(filepath),
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                    # 归属角色：空 = 全局知识（所有角色都能检索到）；非空 = 该角色专属（别的角色检索不到）
                    "char_id": str(char_id or ""),
                },
                memory_type="knowledge",
            )
        
        self._document_cache[str(filepath)] = content_hash
        logger.info(f"[RAG] 索引文档: {filepath.name} ({len(chunks)} 块)")
        return len(chunks)
    
    def index_directory(self, dir_path: Path) -> int:
        """索引整个目录"""
        supported = {".txt", ".md", ".pdf", ".docx", ".doc", ".py", ".js", ".json", ".yaml", ".yml", ".csv"}
        total = 0
        
        for filepath in dir_path.rglob("*"):
            if filepath.suffix.lower() in supported:
                total += self.index_document(filepath)
        
        return total
    
    def search(self, query: str, top_k: int = 3, char_id: str = None) -> List[dict]:
        """搜索知识库。

        给了 char_id 就只返回「该角色专属」+「全局」的知识：A 角色的简历/自述
        不会被检索进 B 角色的提示词（RAG 也按角色隔离）。
        """
        res = self.store.search(query, top_k=max(top_k * 4, top_k), memory_type="knowledge")
        if not char_id:
            return res[:top_k]
        out = []
        for r in res:
            meta = {}
            try:
                meta = r.get("metadata") or {}
            except Exception:
                meta = {}
            owner = str(meta.get("char_id") or "")
            if not owner:
                # 兼容旧索引：早年入库的条目没打 char_id，从来源文件名里认角色
                # （resume_char_xxxx.md / char_xxxx_自述.md），否则它们会一直当全局知识串味
                src = str(meta.get("source") or "")
                i = src.find("char_")
                if i >= 0:
                    j = i + 5
                    while j < len(src) and (src[j].isalnum() or src[j] == "_"):
                        j += 1
                    owner = src[i:j]
                else:
                    # 兼容 self_said_<charid>.md / resume_<charid>.md 这类命名，
                    # 这类文件名不含 char_ 前缀，反推不出归属会被当成全局知识。
                    import os as _os
                    base = _os.path.basename(src)
                    for _pre in ("self_said_", "resume_"):
                        if base.startswith(_pre):
                            owner = base[len(_pre):].rsplit(".", 1)[0]
                            break
            if owner and owner != char_id:
                continue
            out.append(r)
            if len(out) >= top_k:
                break
        return out

    def get_knowledge_context(self, query: str, top_k: int = 3, char_id: str = None) -> str:
        """获取知识上下文（char_id 非空时按角色隔离检索）"""
        results = self.search(query, top_k=top_k, char_id=char_id)
        if not results:
            return ""
        
        parts = ["【相关知识】"]
        for r in results:
            parts.append(f"- {r['text'][:300]}...")
        
        return "\n".join(parts)


# ============ 全局实例 ============

_memory_manager: Optional[MemoryManager] = None
_knowledge_base: Optional[KnowledgeBase] = None
# 每个用户一个知识库：/data/users/<用户>/knowledge/
_knowledge_bases: Dict[str, KnowledgeBase] = {}


def get_memory_manager() -> MemoryManager:
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager


def get_knowledge_base(user_key: str = "") -> KnowledgeBase:
    """知识库（RAG）。传 user_key 就返回**该用户自己的**知识库，物理分隔；
    不传则返回公共知识库（只给没有用户上下文的场景用）。"""
    u = str(user_key or "").strip()
    if u:
        kb = _knowledge_bases.get(u)
        if kb is None:
            kb = KnowledgeBase(storage_dir=user_dir(u, "knowledge"))
            _knowledge_bases[u] = kb
        return kb
    global _knowledge_base
    if _knowledge_base is None:
        _knowledge_base = KnowledgeBase()
    return _knowledge_base
