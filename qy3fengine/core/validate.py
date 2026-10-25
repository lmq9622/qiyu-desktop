# -*- coding: utf-8 -*-
"""校验：`validate_qy3` —— 解密 + schema 校验 + 计数报告（不落库、不改文件）。

移植来源：`ai-companion/characters/qy3.py:944-978`（QY3Validator）。

比线上多返回的部分（线上只回 ok/format_version/schema_version/role_id/role_name/
memory_records）：magic / kek_mode / size / sha256 与 `payload_counts()` 的全量计数。
只增不改，老调用方读到的键都还在。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..container import decode_payload, kek_mode_of, magic_name, read_header
from ..schema import QY3Error, payload_counts, validate_payload


def validate_payload_file(path: str | Path, password: str = "",
                          *, open_format: bool | None = False) -> dict:
    """只做 schema 校验（不返回计数）。校验失败抛 QY3Error。"""
    data = Path(path).read_bytes()
    payload, header = decode_payload(data, password, open_format=open_format)
    validate_payload(payload, header)
    return {"ok": True,
            "format_version": header.get("format_version"),
            "schema_version": header.get("schema_version"),
            "role_id": (payload.get("role") or {}).get("id"),
            "role_name": (payload.get("role") or {}).get("name"),
            "memory_records": len(payload.get("memory_records") or [])}


def validate_qy3(path: str | Path, password: str = "",
                 *, open_format: bool | None = False,
                 with_counts: bool = True) -> dict:
    """完整校验 + 计数报告。

    返回（失败时才抛异常，与线上一致——调用方按 `QY3Error.code` 分支）：
      ok / magic / format_version / schema_version / kek_mode / size / sha256
      counts: persona_params / memory_records / daily_memory_days+events /
              voice_states / continual_rag / rag_documents / chunks / self_tune …
    """
    try:
        data = Path(path).read_bytes()
    except Exception as exc:
        raise QY3Error("file_error", f"无法读取文件: {exc}") from exc
    header = read_header(data)[1]
    payload, header = decode_payload(data, password, open_format=open_format)
    validate_payload(payload, header)
    out = {
        "ok": True,
        "path": str(path),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "magic": magic_name(data),
        "format_version": header.get("format_version"),
        "schema_version": header.get("schema_version"),
        "kek_mode": kek_mode_of(header),
        "key_mode": header.get("key_mode"),
        "cipher": header.get("cipher"),
        "exported_at": header.get("exported_at"),
        "legacy_format": bool(header.get("_legacy_v1")),
        "role_id": (payload.get("role") or {}).get("id"),
        "role_name": (payload.get("role") or {}).get("name"),
        "memory_records": len(payload.get("memory_records") or []),
    }
    if with_counts:
        out["counts"] = payload_counts(payload)
    return out


__all__ = ["validate_qy3", "validate_payload_file"]
