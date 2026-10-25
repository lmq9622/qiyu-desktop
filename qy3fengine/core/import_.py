# -*- coding: utf-8 -*-
"""导入：`import_role`（含 self_tune 审计日志恢复、当天事件、语音、巩固层、世界状态）。

移植来源：`ai-companion/characters/qy3.py:760-853`（QY3Importer）与
`qy3.py:526-644`（`_restore_continual_rag` / `_restore_self_tune` / `_restore_self_notes`）。

**结构性改动**：不 import `characters.* / memory.* / companion.*`，落库动作全部交给
调用方传入的 `sink`（见 `RoleSink`）。写入顺序、跳过条件、幂等规则与线上一致。

⚠️ 已知有损环节（不是本模块造成的，是现网存储层行为，别在报告里含糊过去）：
   当天事件交给 `sink.save_daily_events()`，现网 `memory.MemoryManager.save_daily_events`
   会**按字面相似度 ≥0.45 合并同日近重复事件**（并把文本截到 200 字）。
   实测 2026-10-21 对 `char_e9faca03` 的往返：3 天都在，事件 61 → 55（6 条被合并）。
   引擎这边只做"按天分组后原样交给 sink"，合并与否由宿主决定；要无损就实现
   一个不合并的 sink（`examples/companion_sink.py` 里 `merge_daily=False`）。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..container import decode_payload, kek_mode_of, read_header
from ..schema import (
    QY3Error, normalize_metadata, validate_payload,
)

VALID_TIERS = ("short_term", "long_term", "summary", "knowledge", "user_profile")


@runtime_checkable
class RoleSink(Protocol):
    """导入落地接口（宿主持有存储层，引擎只负责解包与分发）。

    必需：
      role_exists(role_id) -> bool
      create_role(role_data: dict) -> str          返回新角色 id

    强烈建议实现（缺了就只导入角色卡，内容会少）：
      existing_memory_ids(owner, role_id) -> set[str]
      add_memory(owner, role_id, entry_id, text, metadata, tier) -> bool
      add_knowledge(owner, role_id, entry_id, text, metadata) -> bool
      save_daily_events(owner, role_id, date, events) -> None

    可选（缺了就跳过，与线上 try/except 行为一致）：
      restore_context(owner, data, replace)
      restore_world_state(owner, role_id, state, replace)
      restore_voice(role_id, voice)
      restore_self_notes(owner, role_id, text) -> dict
      restore_continual_rag(owner, role_id, data) -> dict
      restore_self_tune(owner, role_id, data) -> dict
      reindex(owner, role_id) -> int
    """

    def role_exists(self, role_id: str) -> bool: ...  # pragma: no cover

    def create_role(self, role_data: dict) -> str: ...  # pragma: no cover


def _req(sink: Any, name: str, *args: Any) -> Any:
    fn = getattr(sink, name, None)
    if fn is None:
        raise QY3Error("sink_missing", f"sink 缺少必需方法 {name}()")
    return fn(*args)


def _opt(sink: Any, name: str, default: Any, *args: Any) -> Any:
    fn = getattr(sink, name, None)
    if fn is None:
        return default
    try:
        out = fn(*args)
    except Exception:
        return default
    return default if out is None else out


def group_daily(events: list[dict]) -> list[tuple[str, list[dict]]]:
    """按 `date` 分组当天事件。

    与 `qy3.py:_group_daily` 的差别只有一处：这里复制字典而不是 `pop("date")`，
    避免把调用方手里的 payload 就地改掉（对外行为不变）。
    """
    grouped: dict[str, list[dict]] = {}
    for item in events or []:
        if not isinstance(item, dict):
            continue
        body = dict(item)
        day = str(body.pop("date", "") or datetime.now().date().isoformat())
        grouped.setdefault(day, []).append(body)
    return list(grouped.items())


def import_role(path: str | Path, *, sink: RoleSink, owner: str = "",
                password: str = "", replace: bool = False,
                open_format: bool | None = False) -> dict:
    """把一个 `.qy3` / `.qy3f` 导入宿主存储。

    open_format：False=只收 v2（服务器版默认）／True=只收 v1／None=自动识别（迁移用）
    replace=True 时才允许覆盖同 id 角色；否则撞 id 会派生 `_import_<sha1前8>` 新 id，
    并把原始 id 记进 `extensions.qy3_original_role_id`（与线上一致）。
    """
    data = Path(path).read_bytes()
    payload, header = decode_payload(data, password, open_format=open_format)
    validate_payload(payload, header)

    role_data = dict(payload.get("role") or {})
    if not role_data.get("id") or not role_data.get("name"):
        raise QY3Error("missing_role", "内容缺失：角色 id/name")
    original_id = str(role_data["id"])
    if _req(sink, "role_exists", original_id) and not replace:
        suffix = hashlib.sha1(
            f"{original_id}|{datetime.now().isoformat()}".encode("utf-8")
        ).hexdigest()[:8]
        role_data["extensions"] = dict(role_data.get("extensions") or {})
        role_data["extensions"]["qy3_original_role_id"] = original_id
        role_data["id"] = f"{original_id}_import_{suffix}"
    if owner:
        role_data["owner"] = owner
    personality = payload.get("personality")
    if isinstance(personality, dict):
        role_data["persona_params"] = personality

    role_id = str(_req(sink, "create_role", role_data))
    import_owner = (owner or str(role_data.get("owner") or "")
                    or str((payload.get("metadata") or {}).get("owner") or "web_user"))

    existing = _opt(sink, "existing_memory_ids", None, import_owner, role_id)
    existing_ids = set(existing or ())

    added_memory = 0
    for record in payload.get("memory_records") or []:
        if not isinstance(record, dict) or not record.get("text"):
            continue
        entry_id = str(record.get("id") or "")
        if entry_id and entry_id in existing_ids:
            continue
        tier = str(record.get("storage_tier") or "short_term")
        if tier not in VALID_TIERS:
            tier = "short_term"
        metadata = normalize_metadata(
            dict(record.get("metadata") or {}),
            str(record.get("source_message_id") or ""),
            str(record.get("memory_type") or ""),
            float(record.get("confidence") or 1.0),
            str(record.get("status") or "active"),
        )
        metadata["last_referenced_at"] = record.get("last_referenced_at") or ""
        metadata["weight"] = float(record.get("importance") or metadata.get("weight") or 0.5)
        ok = _opt(sink, "add_memory", True, import_owner, role_id, entry_id,
                  str(record["text"]), metadata, tier)
        if ok is not False:
            added_memory += 1
            if entry_id:
                existing_ids.add(entry_id)

    added_docs = 0
    for doc in payload.get("rag_documents") or []:
        if not isinstance(doc, dict) or not doc.get("text"):
            continue
        doc_id = str(doc.get("id") or "")
        if doc_id and doc_id in existing_ids:
            continue
        meta = dict(doc.get("metadata") or {})
        meta["char_id"] = doc.get("char_id") or role_id
        ok = _opt(sink, "add_knowledge", True, import_owner, role_id, doc_id,
                  str(doc["text"]), meta)
        if ok is not False:
            added_docs += 1
            if doc_id:
                existing_ids.add(doc_id)

    daily_in = len(payload.get("daily_memory") or [])
    daily_out = 0
    for day, events in group_daily(payload.get("daily_memory") or []):
        _opt(sink, "save_daily_events", None, import_owner, role_id, day, events)
        daily_out += len(events)

    _opt(sink, "restore_context", None, import_owner, {
        "external_references": payload.get("external_references") or [],
        "commitments": payload.get("commitments") or [],
        "events": payload.get("events") or [],
    }, replace)
    _opt(sink, "restore_world_state", None, import_owner, role_id,
         payload.get("world_state") or {}, replace)
    _opt(sink, "restore_voice", None, role_id, payload.get("voice") or {})
    notes_res = _opt(sink, "restore_self_notes", {"ok": True, "total": 0},
                     import_owner, role_id, payload.get("self_notes") or "")
    continual_res = _opt(sink, "restore_continual_rag", {"files": 0, "buffer": 0},
                         import_owner, role_id, payload.get("continual_rag") or {})
    tune_res = _opt(sink, "restore_self_tune", {"log": 0, "persona": 0},
                    import_owner, role_id, payload.get("self_tune") or {})
    reindex_n = 0
    if payload.get("needs_reindex"):
        reindex_n = _opt(sink, "reindex", 0, import_owner, role_id)

    return {
        "ok": True,
        "role_id": role_id,
        "original_role_id": original_id,
        "owner": import_owner,
        "voice_states": list((payload.get("voice") or {}).get("states") or {}),
        "memory_records": len(payload.get("memory_records") or []),
        "memory_added": added_memory,
        "rag_documents": len(payload.get("rag_documents") or []),
        "rag_added": added_docs,
        "commitments": len(payload.get("commitments") or []),
        "daily_events_in": daily_in,
        "daily_events_out": daily_out,
        "self_notes": int((notes_res or {}).get("total") or 0),
        "continual_rag": dict(continual_res or {}),
        "self_tune": dict(tune_res or {}),
        "reindexed": int(reindex_n or 0),
        "legacy_format": bool(header.get("_legacy_v1")),
        "format_version": header.get("format_version"),
        "kek_mode": kek_mode_of(header),
    }


def peek(path: str | Path, password: str = "",
         *, open_format: bool | None = False) -> dict:
    """不落库，只解出 (header 摘要 + payload 计数)，用于"导入前预览"。"""
    from ..schema import payload_counts

    data = Path(path).read_bytes()
    payload, header = decode_payload(data, password, open_format=open_format)
    validate_payload(payload, header)
    return {"header": {k: header.get(k) for k in
                       ("format_version", "schema_version", "exported_at", "cipher",
                        "key_mode", "kek_mode", "aad", "open_format")},
            "kek_mode": kek_mode_of(header),
            "header_raw_len": len(read_header(data)[2]),
            "counts": payload_counts(payload)}


__all__ = ["RoleSink", "import_role", "group_daily", "peek"]
