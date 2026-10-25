# -*- coding: utf-8 -*-
"""导出：`export_role`（服务器版 v2 / .qy3）与 `export_open_role`（桌面版 v1 / .qy3f）。

移植来源：`ai-companion/characters/qy3.py:647-758`（QY3Exporter）与
`qy3.py:430-646`（各 `_*_payload` 采集函数）。

**唯一的结构性改动**：不再 `import` `characters.* / memory.* / companion.*`，
所有读取动作都通过调用方传入的 `source` 对象（见 `RoleSource`）完成。
采集顺序、字段名、缺省值、异常吞掉的位置都与线上一致 —— 由
`examples/companion_source.py`（把现网 `companion.*` 包成 source）负责取值。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..container import decode_payload, encode_payload, kek_mode_of, read_header
from ..edition import is_server
from ..schema import (
    ENGINE_MIN_VERSION, FORMAT_VERSION_V2, OPEN_SUFFIX, QY3_SUFFIX, SCHEMA_VERSION,
    RoleProfile, QY3Error,
)


@runtime_checkable
class RoleSource(Protocol):
    """导出数据源（宿主持有存储层，引擎只负责打包）。

    只有 `get_role` + `memory_records` 必需，其余缺省即"这一段没有"：

      get_role(role_id) -> dict | None            角色卡（char.to_dict()），必需
      owner_of(role) -> str                       可选；缺省用 role["owner"]
      persona_params(role, owner) -> dict          可选；缺省用 role["persona_params"]
      compiled_behavior(role) -> (str, str)       可选；返回 (behavior, persona_hash)
      memory_records(owner, role_id) -> list[dict]必需，MemoryRecord.to_dict() 形状
      daily_memory(owner, role_id, days) -> list  可选，[{...event, "date": "YYYY-MM-DD"}]
      rag(owner, role_id) -> (docs, chunks)       可选
      voice(role_id) -> dict                      可选，{"states": {name: {...}}}
      self_notes(owner, role_id) -> str           可选
      continual_rag(owner, role_id) -> dict       可选，{"bucket": {...}, "buffer": [...]}
      self_tune(owner, role_id) -> dict           可选，{"log": [...], "persona": {...}}
      world_state(owner, role_id) -> dict         可选
      context(owner) -> dict                      可选，{external_references, commitments, events}
    """

    def get_role(self, role_id: str) -> dict | None: ...  # pragma: no cover

    def memory_records(self, owner: str, role_id: str) -> list[dict]: ...  # pragma: no cover


def _need(source: Any, name: str, *args: Any) -> Any:
    """必需方法：没有就抛，别静默导出半个人。"""
    fn = getattr(source, name, None)
    if fn is None:
        raise QY3Error("source_missing", f"source 缺少必需方法 {name}()")
    return fn(*args)


def _opt(source: Any, name: str, default: Any, *args: Any) -> Any:
    """可选方法：现网的每个采集都在 try/except 里，缺失/异常一律降级为默认值。"""
    fn = getattr(source, name, None)
    if fn is None:
        return default
    try:
        out = fn(*args)
    except Exception:
        return default
    return default if out is None else out


def build_payload(role: dict, *, owner: str,
                  params: dict | None = None,
                  compiled_behavior: tuple[str, str] | None = None,
                  world_state: dict | None = None,
                  context: dict | None = None,
                  daily_memory: list[dict] | None = None,
                  memory_records: list[dict] | None = None,
                  rag_documents: list[dict] | None = None,
                  chunks: list[dict] | None = None,
                  voice: dict | None = None,
                  self_notes: str = "",
                  continual_rag: dict | None = None,
                  self_tune: dict | None = None) -> dict:
    """把角色与所有附属数据拼成 payload。

    字段顺序/缺省与 `qy3.py:697-726` 完全对应（顺序无功能意义，但 diff 时要能一眼对上）。
    """
    raw_params = dict(params if params is not None else (role.get("persona_params") or {}))
    behavior, persona_hash = compiled_behavior or ("", "")
    records = list(memory_records or [])
    ctx = dict(context or {})
    return {
        "format_version": FORMAT_VERSION_V2,
        "schema_version": SCHEMA_VERSION,
        "engine_min_version": ENGINE_MIN_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "role": dict(role),
        "personality": RoleProfile.from_mapping(raw_params).to_dict(),
        "compiled_persona_behavior": behavior,
        "persona_hash": persona_hash,
        "world_state": dict(world_state or {
            "relationship_state": {}, "emotion_state": {}, "current_topic": "",
            "unfinished_topics": [], "recent_events": [],
        }),
        "events": list(ctx.get("events") or []),
        "commitments": list(ctx.get("commitments") or []),
        "daily_memory": list(daily_memory or []),
        "short_term_memory": [x for x in records if x.get("storage_tier") == "short_term"],
        "long_term_memory": [x for x in records if x.get("storage_tier") == "long_term"],
        "memory_records": records,
        "rag_documents": list(rag_documents or []),
        "chunks": list(chunks or []),
        "voice": dict(voice or {}),
        "self_notes": str(self_notes or ""),
        "continual_rag": dict(continual_rag or {"bucket": {}, "buffer": []}),
        "self_tune": dict(self_tune or {"log": [], "persona": {}}),
        "needs_reindex": True,
        "metadata": {"owner": owner, "role_id": str(role.get("id") or ""),
                     "scope": "normalized"},
        "external_references": list(ctx.get("external_references") or []),
        "unresolved_references": [
            x for x in (ctx.get("external_references") or [])
            if isinstance(x, dict) and x.get("content_status") == "unreadable"
        ],
    }


def collect_payload(role_id: str, source: RoleSource, *,
                    owner: str = "", daily_days: int = 60) -> tuple[dict, str, str]:
    """按线上顺序从 source 采集并组装 payload，返回 (payload, role_id, owner)。"""
    role = _need(source, "get_role", role_id)
    if not isinstance(role, dict) or not role:
        raise QY3Error("missing_role", "角色不存在，无法导出")
    effective_owner = (owner
                       or str(_opt(source, "owner_of", role.get("owner") or "", role) or "")
                       or "web_user")
    params = _opt(source, "persona_params", None, role, effective_owner)
    compiled = _opt(source, "compiled_behavior", None, role)
    docs_chunks = _opt(source, "rag", ([], []), effective_owner, str(role.get("id") or ""))
    try:
        rag_documents, chunks = docs_chunks
    except Exception:
        rag_documents, chunks = [], []
    payload = build_payload(
        role,
        owner=effective_owner,
        params=params if isinstance(params, dict) else (role.get("persona_params") or {}),
        compiled_behavior=compiled if isinstance(compiled, tuple) else None,
        world_state=_opt(source, "world_state", None, effective_owner, str(role.get("id") or "")),
        context=_opt(source, "context", None, effective_owner),
        daily_memory=_opt(source, "daily_memory", [], effective_owner,
                          str(role.get("id") or ""), daily_days),
        memory_records=_need(source, "memory_records", effective_owner, str(role.get("id") or "")),
        rag_documents=rag_documents,
        chunks=chunks,
        voice=_opt(source, "voice", {}, str(role.get("id") or "")),
        self_notes=_opt(source, "self_notes", "", effective_owner, str(role.get("id") or "")),
        continual_rag=_opt(source, "continual_rag", {"bucket": {}, "buffer": []},
                           effective_owner, str(role.get("id") or "")),
        self_tune=_opt(source, "self_tune", {"log": [], "persona": {}},
                       effective_owner, str(role.get("id") or "")),
    )
    return payload, str(role.get("id") or ""), effective_owner


def export_role(role_id: str, path: str | Path, *, source: RoleSource,
                owner: str = "", password: str = "", daily_days: int = 60,
                open_format: bool = False) -> dict:
    """导出角色包。

    open_format=False（默认，服务器版）→ v2 `.qy3` 加密容器
    open_format=True                     → v1 `.qy3f`（= 桌面开源版）
    后缀必须与发行版对得上：`.qy3f` ⟺ open_format=True，其余 ⟺ v2。
    """
    out_path = Path(path)
    wants_open = str(out_path).lower().endswith(OPEN_SUFFIX)
    if wants_open != bool(open_format):
        raise QY3Error(
            "format_mismatch",
            ("开源版只能导出 .qy3f（v1）" if open_format
             else "服务器版只能导出 .qy3（v2 加密），不产出开源格式 .qy3f")
            + "：两套格式不互通")
    if open_format and is_server() is False and not wants_open:
        raise QY3Error("format_mismatch", "开源版只产出 .qy3f")

    payload, role_id_out, owner_out = collect_payload(
        role_id, source, owner=owner, daily_days=daily_days)
    data = encode_payload(payload, password, open_format=open_format)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    header = read_header(data)[1]
    return {
        "ok": True,
        "path": str(out_path),
        "bytes": len(data),
        "role_id": role_id_out,
        "owner": owner_out,
        "schema_version": SCHEMA_VERSION,
        "format": "qy3f" if open_format else "qy3",
        "format_version": header.get("format_version"),
        "cipher": header.get("cipher"),
        "key_mode": header.get("key_mode"),
        "kek_mode": kek_mode_of(header),
    }


def export_open_role(role_id: str, path: str | Path, *, source: RoleSource,
                     owner: str = "", password: str = "", daily_days: int = 60) -> dict:
    """桌面开源版的唯一出口：写 v1（`.qy3f`）。"""
    if not str(path).lower().endswith(OPEN_SUFFIX):
        raise QY3Error("format_mismatch", "开源版只能导出 .qy3f（v1）：两套格式不互通")
    return export_role(role_id, path, source=source, owner=owner,
                       password=password, daily_days=daily_days, open_format=True)


def read_payload(path: str | Path, password: str = "",
                 *, open_format: bool | None = False) -> tuple[dict, dict]:
    """便捷入口：直接拿 (payload, header)（供宿主导入前预览/改字段）。"""
    return decode_payload(Path(path).read_bytes(), password, open_format=open_format)


__all__ = ["RoleSource", "build_payload", "collect_payload",
           "export_role", "export_open_role", "read_payload", "QY3_SUFFIX"]
