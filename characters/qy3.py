# -*- coding: utf-8 -*-
"""QY3 单文件角色包 · **桌面版（不加密）**。

桌面端独立于服务器版，这里只做一件事：把「一个人」打包成一个文件带得走。

## 格式 = 老的服务器 qy3（v1）

    QY3\\x01 | header_len(u32) | header(json) | nonce(12) | AES-256-GCM(zstd(msgpack))

- 口令是**写死在代码里的常量**（`DEFAULT_QY3_KEY`），所以这一步**不提供任何保密性** ——
  它只保证"包结构对得上"，任何拿到源码的人都能解开。这是刻意的：桌面版要开源、可移植、
  可被第三方工具直接读写。
- 服务器版用的是 v2（信封加密，没有主密钥解不开），**两套格式互不导入**：
  桌面版只认 v1，服务器版只认 v2。老服务器导出的 `.qy3` 改后缀成 `.qy3f` 就能在桌面端打开。

## 依赖

`msgpack` / `zstandard` / `cryptography`。缺任意一个都会在 import 时就报出明确原因，
而不是等到用户点导出才炸。`pip install msgpack zstandard cryptography`。

## payload（v1 容器里装的东西）

    role / personality / resume / soul+boundaries（在 role 里）
    memory_records / short_term_memory / long_term_memory / daily_memory
    knowledge     ← 该角色可见的 RAG 文档切片（角色专属 + 全局）
    world_state   ← 关系值 / 情绪 / 未完成话题 / 承诺（来自 companion 的运行时状态）
    voice / metadata / stats

`knowledge` 与 `world_state` 是**可选新增字段**，只加不改：
- 老 `.qy3f` 包没有这两块 → 读进来就是空，`has_knowledge/has_world_state` 为 False；
- 老客户端读到这两块 → 直接忽略，别的字段照旧。
所以三条 `schema_version` 都还是 1，容器格式一个字都没动。

## API

    export_role("char_id", "out/她.qy3f")     -> {"ok", "path", "bytes", ...}
    import_role("out/她.qy3f", replace=False)  -> {"ok", "role_id", "memory_records",
                                                  "knowledge_written", "knowledge_skipped",
                                                  "world_state_restored", ...}
    validate_qy3("out/她.qy3f")                -> {"ok", "schema_version", "role",
                                                  "knowledge_chunks", "has_world_state", ...}
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import struct
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import msgpack
    import zstandard
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception as _exc:  # pragma: no cover - 只在缺依赖时走到
    raise ImportError(
        "QY3 需要 msgpack / zstandard / cryptography 三个包："
        "pip install msgpack zstandard cryptography"
    ) from _exc

# qy3fengine：角色包引擎（vendor 目录 / pip 安装均可）。
# 在这里真正 import 一次有两个作用：
#   1) 校验/导出优先走引擎（schema 比老实现更严，能提前发现缺字段）
#   2) PyInstaller 靠静态分析收集包，不 import 就不会打进桌面端
try:
    import qy3fengine as _engine
    _HAS_ENGINE = True
except Exception:  # pragma: no cover
    _engine = None
    _HAS_ENGINE = False

MAGIC = b"QY3\x01"
FORMAT_VERSION = 1
SCHEMA_VERSION = 1
ENGINE_MIN_VERSION = "0.0.22"
COMPRESSION = "zstd"
CIPHER = "AES-256-GCM"
NONCE_BYTES = 12

# 公开常量口令：它是**格式的一部分**，不承担安全职责（见模块 docstring）。
DEFAULT_QY3_KEY = "Qiyu-QY3-Portable-Role-v1"

DEFAULT_USER = "web_user"
DAILY_DAYS = 30


class QY3Error(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 编码层
def _key(password: str = "") -> bytes:
    material = password or os.getenv("QIYU_QY3_KEY", "") or DEFAULT_QY3_KEY
    return hashlib.sha256(("QIY3|" + material).encode("utf-8")).digest()


def _header_bytes(header: dict) -> bytes:
    return json.dumps(header, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _encode_legacy(payload: dict, password: str = "") -> bytes:
    header = {
        "format_version": FORMAT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "engine_min_version": ENGINE_MIN_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "compression": COMPRESSION,
        "cipher": CIPHER,
        "payload_encoding": "msgpack",
        "open_format": True,          # 明确标记：这是不加密的桌面版容器
    }
    try:
        packed = msgpack.packb(payload, use_bin_type=True)
        compressed = zstandard.ZstdCompressor(level=3).compress(packed)
    except Exception as exc:
        raise QY3Error("encode_failed", f"QY3 编码失败: {exc}") from exc
    raw = _header_bytes(header)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(_key(password)).encrypt(nonce, compressed, MAGIC)
    return MAGIC + struct.pack(">I", len(raw)) + raw + nonce + ciphertext


def _decode_legacy(data: bytes, password: str = "") -> tuple[dict, dict]:
    if not data.startswith(MAGIC):
        if data[:4] == b"QY3\x02":
            raise QY3Error(
                "format_mismatch",
                "这是网站版（v2 加密）的角色包，桌面版只认 .qy3f（v1）格式")
        raise QY3Error("bad_magic", "文件损坏：不是 QY3 格式")
    head = len(MAGIC)
    if len(data) < head + 4:
        raise QY3Error("bad_header", "文件损坏：QY3 header 不完整")
    header_len = struct.unpack(">I", data[head:head + 4])[0]
    start = head + 4
    end = start + header_len
    if header_len <= 0 or end + NONCE_BYTES + 16 > len(data):
        raise QY3Error("bad_header", "文件损坏：QY3 header 长度非法")
    try:
        header = json.loads(data[start:end].decode("utf-8"))
    except Exception as exc:
        raise QY3Error("bad_header", "文件损坏：QY3 header 不是合法 JSON") from exc
    if not isinstance(header, dict):
        raise QY3Error("bad_header", "文件损坏：QY3 header 必须是对象")
    if int(header.get("format_version") or 0) > FORMAT_VERSION:
        raise QY3Error("version_too_new", "QY3 版本过高，当前客户端无法导入")
    if header.get("compression") != COMPRESSION or header.get("cipher") != CIPHER:
        raise QY3Error("unsupported_algorithm", "不支持的 QY3 压缩或加密算法")

    body = data[end:]
    nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
    if len(nonce) != NONCE_BYTES or len(ciphertext) < 16:
        raise QY3Error("bad_payload", "文件损坏：QY3 密文不完整")
    try:
        compressed = AESGCM(_key(password)).decrypt(nonce, ciphertext, MAGIC)
    except InvalidTag as exc:
        raise QY3Error("auth_failed", "认证失败：包被改过，或不是这个版本导出的") from exc
    try:
        unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
        unpacker.feed(zstandard.ZstdDecompressor().decompress(compressed))
        payload = unpacker.unpack()
    except zstandard.ZstdError as exc:
        raise QY3Error("corrupt_payload", "文件损坏：QY3 解压失败") from exc
    except Exception as exc:
        raise QY3Error("corrupt_payload", f"文件损坏：QY3 数据无法解析（{exc}）") from exc
    if not isinstance(payload, dict):
        raise QY3Error("schema_error", "schema 错误：角色包根节点必须是对象")
    return payload, header


def _encode(payload: dict, password: str = "") -> bytes:
    """写容器：优先走 qy3fengine，引擎缺失才回退老实现。

    容器层（header/压缩/加密/错误码）交给引擎，引擎侧的格式修复才能真的生效；
    老实现保留为 `_encode_legacy`，只在 vendor 目录被裁掉时兜底。
    """
    if _HAS_ENGINE:
        try:
            return _engine.encode_payload(payload, password, open_format=True)
        except _engine.QY3Error as exc:
            raise QY3Error(exc.code, str(exc)) from exc
        except Exception as exc:
            raise QY3Error("encode_failed", f"QY3 编码失败: {exc}") from exc
    return _encode_legacy(payload, password)


def _decode(data: bytes, password: str = "") -> tuple[dict, dict]:
    """读容器：同上，引擎优先；异常统一成本模块的 QY3Error（带 .code）。"""
    # v2 魔数先行拦截：截断/损坏的服务器包也应提示"这是服务器版打不开"，
    # 而不是被引擎的 header 解析判成 bad_header（用户看不懂）。
    if data[:4] == b"QY3\x02":
        raise QY3Error(
            "format_mismatch",
            "这是网站版（v2 加密）的角色包，桌面版只认 .qy3f（v1）格式")
    if _HAS_ENGINE:
        try:
            return _engine.decode_payload(data, password, open_format=True)
        except _engine.QY3Error as exc:
            raise QY3Error(exc.code, str(exc)) from exc
    return _decode_legacy(data, password)


# ---------------------------------------------------------------- 采集
def _owner_of(char: Any) -> str:
    return str(getattr(char, "owner", "") or "") or DEFAULT_USER


def _memory_records(mem: Any, owner: str, char_id: str) -> list[dict]:
    """把 VectorStore 里的条目拍平成 dict（桌面端没有 memory/records.py）。"""
    try:
        store = mem._get_store(owner, char_id)
    except Exception:
        return []
    out: list[dict] = []
    for entry in list(getattr(store, "entries", []) or []):
        try:
            out.append({
                "id": str(getattr(entry, "id", "") or ""),
                "text": str(getattr(entry, "text", "") or ""),
                "metadata": dict(getattr(entry, "metadata", {}) or {}),
                "created_at": str(getattr(entry, "created_at", "") or ""),
                "memory_type": str(getattr(entry, "memory_type", "short_term") or "short_term"),
            })
        except Exception:
            continue
    return out


def _daily_memory(mem: Any, owner: str, char_id: str, days: int = DAILY_DAYS) -> list[dict]:
    out: list[dict] = []
    today = datetime.now().date()
    for offset in range(max(1, int(days or DAILY_DAYS))):
        day = today.fromordinal(today.toordinal() - offset).isoformat()
        try:
            for event in mem.get_daily_events(owner, day, char_id) or []:
                out.append(dict(event, date=day))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 知识库（RAG）
def _infer_char_from_source(source: str) -> str:
    """从切片来源文件名反推归属角色。

    口径与 `memory.KnowledgeBase.search` 一致：早年入库的切片没有 metadata.char_id，
    只靠 `char_<id>_自述.md` / `resume_<id>.md` / `self_said_<id>.md` 认人。
    导出时用同一套规则，才不会出现"检索认为属于 A、导出认为不属于任何人"的分歧。
    """
    src = str(source or "")
    i = src.find("char_")
    if i >= 0:
        j = i + 5
        while j < len(src) and (src[j].isalnum() or src[j] == "_"):
            j += 1
        return src[i:j]
    base = os.path.basename(src)
    for _pre in ("self_said_", "resume_"):
        if base.startswith(_pre):
            return base[len(_pre):].rsplit(".", 1)[0]
    return ""


def _knowledge_bases(owner: str) -> list:
    """要读取的知识库：桌面聊天真正检索的那个（公共库）+ 该用户独立库（有数据才算）。

    `memory.get_knowledge_base("")` 是 demo.py / companion.llm 里共用的检索库；
    `get_knowledge_base(user_key)` 是服务器版多用户才用的独立库。桌面版只要后者存在
    实际数据也一并读进来，避免"用户自己塞进独立库的知识带不走"。
    """
    from memory import get_knowledge_base
    out = [get_knowledge_base()]
    if owner:
        from pathutil import get_users_dir, safe_user_name
        user_kb_dir = get_users_dir() / safe_user_name(owner) / "knowledge"
        try:
            if any(user_kb_dir.glob("*.json")):
                out.append(get_knowledge_base(owner))
        except Exception:
            pass
    return out


def _knowledge_payload(owner: str, role_id: str) -> dict:
    """采集「该角色可见」的 RAG 知识：角色专属切片 + 全局切片。

    别的角色的专属知识**不跟着走**（否则导出一个角色会把另一个角色的人设一起带走）。
    返回的 `chunks` 是导入时的权威数据，`documents` 只是给人看的分组视图。
    """
    chunks: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    for kb in _knowledge_bases(owner):
        try:
            entries = kb.store.list_all(memory_type="knowledge")
        except Exception:
            continue
        for e in entries:
            meta = dict(e.get("metadata") or {})
            text = str(e.get("text") or "")
            if not text.strip():
                continue
            owner_char = str(meta.get("char_id") or "") or _infer_char_from_source(meta.get("source"))
            if owner_char and owner_char != role_id:
                continue                     # 别人的专属知识，不带走
            source = str(meta.get("source") or "")
            try:
                idx = int(meta.get("chunk_index") or 0)
            except Exception:
                idx = 0
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
            if (source, idx, digest) in seen:
                continue                      # 公共库与用户库可能是同一份，去重
            seen.add((source, idx, digest))
            chunks.append({
                "id": str(e.get("id") or ""),
                "text": text,
                "created_at": str(e.get("created_at") or ""),
                "source": source,
                "chunk_index": idx,
                "total_chunks": int(meta.get("total_chunks") or 0),
                "scope": "role" if owner_char else "global",
                "metadata": meta,
            })
    chunks.sort(key=lambda c: (c["source"], c["chunk_index"]))

    documents: dict[str, dict] = {}
    for c in chunks:
        doc = documents.setdefault(
            c["source"], {"source": c["source"], "scope": c["scope"], "chunk_count": 0, "texts": []})
        doc["chunk_count"] += 1
        doc["texts"].append(c["text"])
    return {
        "scope": "role_visible",              # 角色专属 + 全局
        "documents": list(documents.values()),
        "chunks": chunks,
        "counts": {"documents": len(documents), "chunks": len(chunks),
                   "chars": sum(len(c["text"]) for c in chunks)},
    }


def _import_knowledge(knowledge: dict, role_id: str) -> dict:
    """把包里的知识切片写回 RAG 存储（桌面聊天真正检索的那个库）。

    幂等键是 `source + chunk_index + 文本哈希 + 归属角色`：
    - 覆盖导入（角色 id 不变）→ 键相同，直接跳过，不会长出重复切片；
    - 改名导入（新角色 id）→ 键不同，写一份归属新角色的切片，新角色才检索得到自己的知识；
    - 全局切片（scope=global）归属恒为 ""，任何角色都能检索，且只留一份。
    角色专属切片的 char_id 改成**导入后**的角色 id；`source` 保留原文件名，方便溯源。
    写不进去时 written=0、skipped=切片总数、error=原因 —— 调用方据此回 `knowledge_skipped`。
    """
    out: dict[str, Any] = {"written": 0, "skipped": 0, "documents": 0,
                           "verified": False, "error": ""}
    knowledge = knowledge if isinstance(knowledge, dict) else {}
    chunks = [c for c in (knowledge.get("chunks") or []) if isinstance(c, dict)]
    if not chunks:
        # 兼容只有 documents（手搓/精简过的包）：把 texts 当成切片补回来
        for doc in knowledge.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            src = str(doc.get("source") or "")
            for i, text in enumerate(doc.get("texts") or []):
                chunks.append({"text": text, "source": src, "chunk_index": i,
                               "scope": doc.get("scope") or "role"})
    if not chunks:
        return out

    try:
        from memory import MemoryEntry, get_knowledge_base
        kb = get_knowledge_base()             # 与 demo.py / companion.llm 检索的是同一个库
        store = kb.store

        def _sig(source: str, idx: int, digest: str, char_id: str) -> tuple[str, int, str, str]:
            return (source, idx, digest, char_id)

        existing: set[tuple[str, int, str, str]] = set()
        for e in store.list_all(memory_type="knowledge"):
            meta = e.get("metadata") or {}
            try:
                idx = int(meta.get("chunk_index") or 0)
            except Exception:
                idx = 0
            existing.add(_sig(str(meta.get("source") or ""), idx,
                              hashlib.sha1(str(e.get("text") or "").encode("utf-8")).hexdigest(),
                              str(meta.get("char_id") or "")))

        new_entries: list[Any] = []
        sources: set[str] = set()
        for c in chunks:
            text = str(c.get("text") or "")
            if not text.strip():
                out["skipped"] += 1
                continue
            meta_in = dict(c.get("metadata") or {})
            source = str(c.get("source") or meta_in.get("source") or "")
            try:
                idx = int(c.get("chunk_index", meta_in.get("chunk_index") or 0) or 0)
            except Exception:
                idx = 0
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
            scope = str(c.get("scope") or ("role" if meta_in.get("char_id") else "global"))
            target_char = role_id if scope == "role" else ""
            sources.add(source)
            if _sig(source, idx, digest, target_char) in existing:
                out["skipped"] += 1
                continue
            meta = dict(meta_in)
            meta.update({
                "source": source,
                "chunk_index": idx,
                "total_chunks": int(c.get("total_chunks") or meta_in.get("total_chunks") or 0),
                "char_id": target_char,
                "imported_from": "qy3f",
            })
            new_entries.append(MemoryEntry(
                id="knowledge_" + hashlib.sha1(
                    f"{source}|{idx}|{digest}|{target_char}".encode("utf-8")).hexdigest()[:16],
                text=text,
                metadata=meta,
                created_at=datetime.now().isoformat(),
                memory_type="knowledge",
            ))
            existing.add(_sig(source, idx, digest, target_char))
            out["written"] += 1

        if new_entries:
            # 批量入库后只重算一次向量：逐条 store.add 会每加一条就全量 fit_transform（O(n²)）
            store.entries.extend(new_entries)
            store.vectors = store.vectorizer.fit_transform([e.text for e in store.entries])
            store._save()                     # 注意：_save 内部吞异常只记日志，所以下面要回读校验
        out["documents"] = len({s for s in sources if s})

        if out["written"]:
            # 回读校验：_save 失败不抛异常，只能靠"重新从磁盘加载数一遍"确认真的落盘了
            try:
                fresh = type(kb)(storage_dir=kb.storage_dir)
                hit = {(str((e.get("metadata") or {}).get("source") or ""),
                        int((e.get("metadata") or {}).get("chunk_index") or 0),
                        str((e.get("metadata") or {}).get("char_id") or ""))
                       for e in fresh.store.list_all(memory_type="knowledge")}
                out["verified"] = all(
                    (str((n.metadata or {}).get("source") or ""),
                     int((n.metadata or {}).get("chunk_index") or 0),
                     str((n.metadata or {}).get("char_id") or "")) in hit
                    for n in new_entries)
            except Exception:
                out["verified"] = False
            if not out["verified"]:
                # 写进内存但没落盘（磁盘满/权限/被杀毒软件占住）：不许静默成功，
                # 计入 written 的切片一律退回 skipped，让调用方看到"其实没写成"
                out["error"] = "verify_failed: 切片没能在磁盘上回读到，可能没真的保存"
                out["skipped"] += out["written"]
                out["written"] = 0
        return out
    except Exception as exc:                  # 写不进去要说清楚，不假装成功
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["written"] = 0
        out["skipped"] = len(chunks)
        return out


# ---------------------------------------------------------------- 世界状态
# 真实存放位置（读代码确认，都是 companion 的进程内 store，落盘在 /data/users/<用户>/<kind>.json）：
#   关系值/层级/关系定位   → _relations_store[f"{用户}:{角色}"]        （companion.relations 在用）
#   情绪向量 + 当天基调    → _emotions_store[f"{用户}:{角色}"]         （companion.emotions 在用）
#   未完成话题/上一话题/场景 → _conv_store[f"{用户}:{角色}"]            （companion.conv 在用）
#   承诺                   → **桌面端没有专门的承诺表**，用未完成的 reminder 任务近似
#                            _schedules_store[f"{用户}"] 里 kind=reminder 且未 done 的条目
#   主动消息受挫状态        → _proactive_store[f"{用户}::{角色}"]（注意是双冒号）
_PROMISE_KINDS = ("reminder",)
_CONV_STATE_KEEP = ("conv_state", "scene", "last_topic", "previous_topic", "unfinished_topic",
                    "unfinished_topic_at", "recent_facts", "last_user_at", "last_ai_at",
                    "updated_at")
# kind → (真正读写这份 dict 的子模块, 模块里的属性名, companion.state 上的同名属性)
_COMPANION_STORES = {
    "relations": ("companion.relations", "_relations_store"),
    "emotions": ("companion.emotions", "_emotions_store"),
    "conv_state": ("companion.conv", "_conv_store"),
    "proactive": ("companion.relations", "_proactive_store"),
    "schedules": ("companion.relations", "_schedules_store"),
}


def _companion_store(kind: str) -> dict:
    """取 companion 的运行时 store（进程内 dict）。

    优先用**业务子模块**里那份：正常运行时它和 `companion.state._*` 是同一个对象；
    但函数被 `importlib.reload(companion.state)` 重载过时（见 tests/test_desktop_core.py），
    state 上的属性会被重新赋成空 dict，而 relations/emotions/conv 持有的才是真正在被读写的
    那份 —— 读错副本会出现"导出的世界状态永远是空的"或"写回后业务看不到"。
    """
    mod_name, attr = _COMPANION_STORES[kind]
    try:
        mod = importlib.import_module(mod_name)
        store = getattr(mod, attr, None)
        if isinstance(store, dict):
            return store
    except Exception:
        pass
    from companion import state as _cs
    store = getattr(_cs, attr, None)
    if not isinstance(store, dict):
        raise QY3Error("world_state_unavailable", f"companion 状态 {kind} 不可用")
    return store


def _world_state(owner: str, role_id: str) -> dict:
    """采集运行时世界状态：关系值 / 情绪 / 未完成话题 / 承诺。

    companion 不在（比如被裁掉的桌面精简版）时返回 available=False + error，不抛异常：
    世界状态是附加块，缺了也不该让整包导不出去。
    """
    state: dict[str, Any] = {"relations": {}, "emotions": {}, "conversation": {},
                             "promises": [], "proactive": {}, "sources": [],
                             "available": False}
    try:
        stores = {kind: _companion_store(kind) for kind in _COMPANION_STORES}
    except Exception as exc:
        state["error"] = f"companion_state_unavailable: {type(exc).__name__}: {exc}"
        return state

    key = f"{owner}:{role_id}"
    try:
        for section, kind, store_key in (
            ("relations", "relations", key),
            ("emotions", "emotions", key),
            ("proactive", "proactive", f"{owner}::{role_id}"),
        ):
            val = stores[kind].get(store_key)
            if isinstance(val, dict) and val:
                state[section] = dict(val)
                mod_name, attr = _COMPANION_STORES[kind]
                state["sources"].append(f"{mod_name}.{attr}")

        conv = stores["conv_state"].get(key) or {}
        if isinstance(conv, dict) and conv:
            state["conversation"] = {k: conv[k] for k in _CONV_STATE_KEEP if k in conv}
            state["sources"].append("companion.conv._conv_store")

        for task in stores["schedules"].get(owner) or []:
            if not isinstance(task, dict) or task.get("done"):
                continue
            if str(task.get("kind") or "") not in _PROMISE_KINDS:
                continue
            payload = dict(task.get("payload") or {})
            if str(payload.get("char_id") or "") not in ("", role_id):
                continue
            text = str(payload.get("text") or "").strip()
            if not text:
                continue
            state["promises"].append({
                "text": text[:200],
                "due_at": float(task.get("due_at") or 0),
                "created_at": float(task.get("created_at") or 0),
                "kind": "reminder",
            })
        if state["promises"]:
            state["promises"].sort(key=lambda p: p["due_at"])
            state["sources"].append("companion.relations._schedules_store")
    except Exception as exc:
        state["error"] = f"world_state_read_failed: {type(exc).__name__}: {exc}"
        return state

    state["available"] = bool(state["relations"] or state["emotions"] or state["conversation"]
                              or state["promises"] or state["proactive"])
    return state


def _restore_world_state(world: dict, owner: str, role_id: str) -> dict:
    """把 world_state 写回 companion 的运行时存储（关系 / 情绪 / 会话 / 承诺 / 主动状态）。

    逐块 try：某一块写不进去只影响那一块，其余照常，失败原因进 `errors`（不静默成功）。
    """
    result: dict[str, Any] = {"relations": False, "emotions": False, "conversation": False,
                              "proactive": False, "promises": 0, "errors": []}
    world = world if isinstance(world, dict) else {}
    if not world:
        return result
    try:
        from companion.state import save_user_store
    except Exception as exc:
        result["errors"].append(f"companion_state_unavailable: {type(exc).__name__}: {exc}")
        return result

    key = f"{owner}:{role_id}"
    for section, kind, store_key in (
        ("relations", "relations", key),
        ("emotions", "emotions", key),
        ("proactive", "proactive", f"{owner}::{role_id}"),
    ):
        val = world.get(section)
        if not isinstance(val, dict) or not val:
            continue
        try:
            store = _companion_store(kind)
            store[store_key] = dict(val)
            save_user_store(kind, store)
            result[section] = True
        except Exception as exc:
            result["errors"].append(f"{section}: {type(exc).__name__}: {exc}")

    conv_in = world.get("conversation")
    if isinstance(conv_in, dict) and conv_in:
        try:
            from companion import conv as _conv          # 走它拿带默认值的完整会话状态
            conv_store = _companion_store("conv_state")  # 与 _conv_state 内部用的是同一份
            cs = (_conv._conv_state(owner, role_id) if hasattr(_conv, "_conv_state")
                  else conv_store.setdefault(f"{owner}:{role_id}", {}))
            for k, v in conv_in.items():
                if k in cs:
                    cs[k] = v
            save_user_store("conv_state", conv_store)
            result["conversation"] = True
        except Exception as exc:
            result["errors"].append(f"conversation: {type(exc).__name__}: {exc}")

    now = time.time()
    added = 0
    try:
        tasks = _companion_store("schedules").setdefault(owner, [])
        existed = {str((t.get("payload") or {}).get("text") or "")
                   for t in tasks if isinstance(t, dict)}
        for p in world.get("promises") or []:
            if not isinstance(p, dict):
                continue
            text = str(p.get("text") or "").strip()
            try:
                due = float(p.get("due_at") or 0)
            except Exception:
                due = 0.0
            if not text or due <= now - 60:
                continue            # 早就过期（或没有时间）的承诺不复活，免得一导入就弹提醒
            if text in existed:
                continue
            tasks.append({
                "id": uuid.uuid4().hex[:10], "kind": "reminder", "done": False,
                "created_at": float(p.get("created_at") or now), "due_at": due,
                "payload": {"char_id": role_id, "text": text, "source": "qy3f_import"},
            })
            existed.add(text)
            added += 1
        if added:
            save_user_store("schedules", _companion_store("schedules"))
        result["promises"] = added
    except Exception as exc:
        result["errors"].append(f"promises: {type(exc).__name__}: {exc}")
    return result


def build_payload(role_id: str, *, user_id: str = "", days: int = DAILY_DAYS) -> dict:
    """把一个角色拍成 payload（不落盘，方便测试/校验）。

    除了原有的角色卡 + 记忆 + 当天事件，再带两块**可选新增**字段：
      - `knowledge`   ：该角色可见的 RAG 文档切片（角色专属 + 全局）
      - `world_state` ：关系值 / 情绪 / 未完成话题 / 承诺（companion 运行时状态）
    这两块采集失败只记进 `stats.*_error`、不中断导出（角色和记忆才是主内容）。
    """
    from characters import get_character_manager
    manager = get_character_manager()
    char = manager.get_character(role_id)
    if not char:
        raise QY3Error("missing_role", "角色不存在，无法导出")

    owner = user_id or _owner_of(char)
    records: list[dict] = []
    daily: list[dict] = []
    memory_error = ""
    try:
        from memory import get_memory_manager
        mem = get_memory_manager()
        records = _memory_records(mem, owner, char.id)
        daily = _daily_memory(mem, owner, char.id, days)
    except Exception as exc:                  # 记忆库坏了不该让整包导不出去，但要留痕
        memory_error = f"{type(exc).__name__}: {exc}"

    try:
        knowledge = _knowledge_payload(owner, char.id)
    except Exception as exc:
        knowledge = {"scope": "role_visible", "documents": [], "chunks": [],
                     "counts": {"documents": 0, "chunks": 0, "chars": 0},
                     "error": f"{type(exc).__name__}: {exc}"}
    world = _world_state(owner, char.id)

    role = {}
    try:
        role = dict(char.to_dict() or {})
    except Exception:
        role = {"id": char.id, "name": getattr(char, "name", "")}

    # qy3fengine 的 schema 要求 role 四个字段齐（id/name/persona_params/virtual_schedule）。
    # 桌面端角色卡默认没有 virtual_schedule（服务器版才有作息表），这里补空对象，
    # 保证「老实现导出的包 → 新引擎能 validate」（2026-10-21 接入验收实测缺这个字段）。
    role.setdefault("virtual_schedule", {})
    role.setdefault("persona_params", dict(getattr(char, "persona_params", {}) or {}))

    by_type: dict[str, int] = {}
    for rec in records:
        tier = str(rec.get("memory_type") or "short_term")
        by_type[tier] = by_type.get(tier, 0) + 1

    exported_at = datetime.now().isoformat(timespec="seconds")
    payload = {
        "format_version": FORMAT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "engine_min_version": ENGINE_MIN_VERSION,
        "exported_at": exported_at,
        "role": role,
        "personality": dict(getattr(char, "persona_params", {}) or {}),
        "resume": dict(getattr(char, "resume", {}) or {}),
        "memory_records": records,
        "short_term_memory": [r for r in records if r["memory_type"] == "short_term"],
        "long_term_memory": [r for r in records if r["memory_type"] == "long_term"],
        "daily_memory": daily,
        "voice": {},                       # 桌面端没有声音段（语音链在服务器版）
        "knowledge": knowledge,            # ← 新增：RAG 文档切片
        "world_state": world,              # ← 新增：关系 / 情绪 / 未完成话题 / 承诺
        "metadata": {"owner": owner, "role_id": char.id, "scope": "desktop"},
    }
    # 汇总统计：导出前就能知道带走了多少东西。字节数要编码后才知道，放在 export_role 的返回里。
    payload["stats"] = {
        "memory_records": len(records),
        "memory_by_type": by_type,
        "daily_memory": len(daily),
        "knowledge_documents": int((knowledge.get("counts") or {}).get("documents") or 0),
        "knowledge_chunks": int((knowledge.get("counts") or {}).get("chunks") or 0),
        "knowledge_chars": int((knowledge.get("counts") or {}).get("chars") or 0),
        "world_state": bool(world.get("available")),
        "promises": len(world.get("promises") or []),
        "exported_at": exported_at,
    }
    if memory_error:
        payload["stats"]["memory_error"] = memory_error
    if knowledge.get("error"):
        payload["stats"]["knowledge_error"] = knowledge["error"]
    if world.get("error"):
        payload["stats"]["world_state_error"] = world["error"]
    return payload


# ---------------------------------------------------------------- 对外
def export_role(role_id: str, path: str | Path, password: str = "",
                user_id: str = "", days: int = DAILY_DAYS) -> dict:
    """导出成 `.qy3f`（v1，不加密）：角色卡 + 记忆 + 当天事件 + 知识库 + 世界状态。"""
    target = Path(path)
    payload = build_payload(role_id, user_id=user_id, days=days)
    data = _encode(payload, password)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
    tmp.write_bytes(data)
    tmp.replace(target)
    stats = payload.get("stats") or {}
    return {
        "ok": True, "path": str(target), "bytes": len(data),
        "role_id": payload["role"].get("id") or role_id,
        "format": "qy3f", "cipher": CIPHER, "encrypted": False,
        "memory_records": len(payload["memory_records"]),
        "daily_memory": len(payload["daily_memory"]),
        # ---- 新增块的真实条数（前端/日志想显示"带走了多少"时直接读这里）----
        "knowledge_documents": int(stats.get("knowledge_documents") or 0),
        "knowledge_chunks": int(stats.get("knowledge_chunks") or 0),
        "world_state": bool(stats.get("world_state")),
        "promises": int(stats.get("promises") or 0),
        "stats": stats,
    }


def import_role(path: str | Path, password: str = "", replace: bool = False,
                owner: str = "", *, restore_world_state: bool = True) -> dict:
    """从 `.qy3f` 导入一个角色（含记忆 / 知识库 / 世界状态）。

    - 同 id 已存在且 `replace=False` → 自动改名（`<id>_import_<hash>`），不覆盖；
    - `replace=True` → 覆盖同 id 那个角色（记忆按条目 id 去重，不会翻倍）；
    - 知识库写回 RAG 存储；写不进去时 `knowledge_written=0`、`knowledge_skipped=N`；
    - `restore_world_state=False` 可只导角色/记忆，不碰关系与情绪。
    老包（没有 knowledge / world_state 字段）照样能读：两个字段缺省即视为空。
    """
    data = Path(path).read_bytes()
    payload, _header = _decode(data, password)

    role_data = dict(payload.get("role") or {})
    if not role_data.get("id") or not role_data.get("name"):
        raise QY3Error("missing_role", "内容缺失：角色 id/name")
    personality = payload.get("personality")
    if isinstance(personality, dict):
        role_data["persona_params"] = personality
    resume = payload.get("resume")
    if isinstance(resume, dict) and resume:
        role_data["resume"] = resume
    if owner:
        role_data["owner"] = owner

    from characters import get_character_manager
    manager = get_character_manager()
    original_id = str(role_data["id"])
    role_data["id"] = original_id

    existed = manager.get_character(original_id) is not None
    if existed and not replace:
        suffix = hashlib.sha1(
            f"{original_id}|{datetime.now().isoformat()}".encode("utf-8")).hexdigest()[:8]
        role_data["extensions"] = dict(role_data.get("extensions") or {})
        role_data["extensions"]["qy3_original_role_id"] = original_id
        role_data["id"] = f"{original_id}_import_{suffix}"

    if existed and replace:
        char = manager.update_character(original_id, role_data)
        if char is None:
            raise QY3Error("import_failed", "覆盖已有角色失败")
    else:
        char = manager.create_character(role_data)

    # ---- 记忆 ----
    mem_owner = owner or _owner_of(char)
    mem_n = 0
    mem_error = ""
    try:
        from memory import get_memory_manager
        mem = get_memory_manager()
        store = mem._get_store(mem_owner, char.id)
        existing = {getattr(e, "id", "") for e in (getattr(store, "entries", []) or [])}
        for rec in payload.get("memory_records") or []:
            if not isinstance(rec, dict) or not rec.get("text"):
                continue
            rid = str(rec.get("id") or "")
            if rid and rid in existing:
                continue
            meta = dict(rec.get("metadata") or {})
            tier = str(rec.get("memory_type") or "short_term")
            if tier not in ("short_term", "long_term", "summary", "knowledge"):
                tier = "short_term"
            store.add(str(rec["text"]), meta, tier, entry_id=rid or None)
            if rid:
                existing.add(rid)
            mem_n += 1
    except Exception as exc:              # 记忆写失败要报出来，不能只回一个 0
        mem_n = 0
        mem_error = f"{type(exc).__name__}: {exc}"

    # ---- 当天事件 ----
    daily_n = 0
    daily_error = ""
    grouped: dict[str, list] = {}
    for ev in payload.get("daily_memory") or []:
        if not isinstance(ev, dict):
            continue
        day = str(ev.pop("date", "") or "")
        if not day:
            continue
        grouped.setdefault(day, []).append(ev)
    try:
        from memory import get_memory_manager
        mem = get_memory_manager()
        for day, events in grouped.items():
            mem.save_daily_events(mem_owner, day, char.id, events)
            daily_n += len(events)
    except Exception as exc:
        daily_n = 0
        daily_error = f"{type(exc).__name__}: {exc}"

    # ---- 知识库（RAG 切片）----
    knowledge_in = payload.get("knowledge") if isinstance(payload.get("knowledge"), dict) else {}
    knowledge_res = _import_knowledge(knowledge_in, char.id)

    # ---- 世界状态（关系 / 情绪 / 未完成话题 / 承诺）----
    world_in = payload.get("world_state") if isinstance(payload.get("world_state"), dict) else {}
    if restore_world_state:
        world_res = _restore_world_state(world_in, mem_owner, char.id)
    else:
        world_res = {"relations": False, "emotions": False, "conversation": False,
                     "proactive": False, "promises": 0,
                     "errors": ["skipped_by_caller: restore_world_state=False"]}

    return {
        "ok": True, "role_id": char.id, "original_role_id": original_id,
        "name": getattr(char, "name", ""),
        "memory_records": mem_n, "daily_memory": daily_n,
        "replaced": bool(existed and replace),
        "self_notes": 0,   # 桌面端还没有相处笔记层
        # ---- 新增：知识库回写结果（写不进去时必须显式回 knowledge_skipped）----
        "has_knowledge": bool(knowledge_in.get("chunks") or knowledge_in.get("documents")),
        "knowledge_written": int(knowledge_res["written"]),
        "knowledge_skipped": int(knowledge_res["skipped"]),
        "knowledge_documents": int(knowledge_res["documents"]),
        "knowledge_verified": bool(knowledge_res["verified"]),
        "knowledge_error": str(knowledge_res["error"] or ""),
        # ---- 新增：世界状态回写结果 ----
        "has_world_state": bool(world_in.get("available")),
        "world_state_restored": {k: v for k, v in world_res.items() if k != "errors"},
        "world_state_errors": list(world_res.get("errors") or []),
        # ---- 落盘失败留痕（以前这两处是静默 0）----
        "memory_error": mem_error,
        "daily_error": daily_error,
    }


def validate_qy3(path: str | Path, password: str = "") -> dict:
    """只校验不导入：给人"选文件后先看包里是谁、带了多少东西"用。"""
    data = Path(path).read_bytes()
    payload, header = _decode(data, password)
    role = payload.get("role") or {}
    if not isinstance(role, dict) or not role.get("id") or not role.get("name"):
        raise QY3Error("schema_error", "schema 错误：缺少 role.id / role.name")
    knowledge = payload.get("knowledge") if isinstance(payload.get("knowledge"), dict) else {}
    world = payload.get("world_state") if isinstance(payload.get("world_state"), dict) else {}
    k_counts = knowledge.get("counts") or {}
    return {
        "ok": True,
        "format": "qy3f",
        "encrypted": False,
        "format_version": header.get("format_version"),
        "schema_version": header.get("schema_version"),
        "role": {"id": role.get("id"), "name": role.get("name")},
        "memory_records": len(payload.get("memory_records") or []),
        "daily_memory": len(payload.get("daily_memory") or []),
        # ---- 新增块：老包这里是 0/False，不会报错 ----
        "has_knowledge": bool(knowledge.get("chunks") or knowledge.get("documents")),
        "knowledge_documents": int(k_counts.get("documents")
                                   or len(knowledge.get("documents") or [])),
        "knowledge_chunks": int(k_counts.get("chunks") or len(knowledge.get("chunks") or [])),
        "has_world_state": bool(world.get("available")),
        "promises": len(world.get("promises") or []),
        "stats": payload.get("stats") or {},
        "bytes": len(data),
    }


__all__ = [
    "MAGIC", "FORMAT_VERSION", "SCHEMA_VERSION", "CIPHER", "DEFAULT_QY3_KEY",
    "QY3Error", "build_payload", "export_role", "import_role", "validate_qy3",
]
