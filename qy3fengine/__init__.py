# -*- coding: utf-8 -*-
"""qy3engine / qy3fengine：栖语 `.qy3`(v2) / `.qy3f`(v1) 角色包读写引擎。

同一份源码树打两个发行版（见 docs/EDITION_MATRIX.md）：

  qy3engine   （server，不对外分发）：容器 v2 + 基础 RAG + 行为偏好(behavior/)
  qy3fengine  （open，MIT，打进桌面端 qyuchat）：容器 v1 + 基础 RAG

顶层 API：

  export_role(role_id, path, source=...)        # v2 .qy3（服务器版）
  export_open_role(role_id, path, source=...)   # v1 .qy3f（桌面版）
  import_role(path, sink=..., owner=...)        # 两版都有；open 版只收 v1
  validate_qy3(path)                            # 校验 + 计数报告
  QY3Error(code, message)                       # 唯一异常类型
"""
from __future__ import annotations

from .container import (
    MAGIC, MAGIC_V2, decode_payload, describe, encode_payload, kek_mode_of,
    magic_name, read_header,
)
from .core.export import (
    RoleSource, build_payload, collect_payload, export_open_role, export_role,
    read_payload,
)
from .core.import_ import RoleSink, group_daily, import_role, peek
from .core.validate import validate_payload_file, validate_qy3
from .edition import (
    EDITION, IS_OPEN, IS_SERVER, EditionError, edition_info, package_name,
    require_server,
)
from .schema import (
    CIPHER, COMPRESSION, ENGINE_MIN_VERSION, FORMAT_VERSION, FORMAT_VERSION_V2,
    MEMORY_TYPES, OPEN_SUFFIX, QY3_SUFFIX, SCHEMA_VERSION, VOICE_STATE_KEYS,
    MemoryRecord, QY3Error, RoleProfile, derive_mbti, normalize_metadata,
    payload_counts, validate_payload,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    # edition
    "EDITION", "IS_SERVER", "IS_OPEN", "EditionError", "edition_info",
    "package_name", "require_server",
    # container
    "MAGIC", "MAGIC_V2", "describe", "encode_payload", "decode_payload",
    "read_header", "kek_mode_of", "magic_name",
    # schema
    "FORMAT_VERSION", "FORMAT_VERSION_V2", "SCHEMA_VERSION", "ENGINE_MIN_VERSION",
    "COMPRESSION", "CIPHER", "QY3_SUFFIX", "OPEN_SUFFIX", "VOICE_STATE_KEYS",
    "QY3Error", "MemoryRecord", "MEMORY_TYPES", "RoleProfile", "derive_mbti",
    "normalize_metadata", "validate_payload", "payload_counts",
    # core
    "RoleSource", "RoleSink", "build_payload", "collect_payload",
    "export_role", "export_open_role", "read_payload",
    "import_role", "group_daily", "peek",
    "validate_qy3", "validate_payload_file",
]


def has_behavior() -> bool:
    """当前安装是否含行为偏好层（open 版恒为 False —— 目录物理不存在）。"""
    if not IS_SERVER:
        return False
    try:
        import importlib
        importlib.import_module(__name__ + ".behavior")
        return True
    except Exception:
        return False
