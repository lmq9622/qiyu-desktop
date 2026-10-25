# -*- coding: utf-8 -*-
"""KEK 三级回退：env → 显式口令 → 内置常量派生（零配置）。

移植自 `ai-companion/characters/qy3.py` 第 60-280 行，常量与派生顺序**一字未改**，
否则线上已发出的 `.qy3` 会全部解不开。

⚠️ 定性（不许在文档里说反）：`BUILTIN_QY3_SEED` 是**格式级封装**，不是密码学秘密。
常量就在代码里，能读代码的人就能解开。它保证的是"只有 qy3 引擎能解析这个容器"，
不承担对攻击者的保密职责。真正要保密，用 `QIYU_QY3_MASTER_KEY`（env，KEK_MODE_ENV）。
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

KEY_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 16
KDF_ITERATIONS = 200_000
KDF_PBKDF2 = "pbkdf2-sha256"

# v1（.qy3f / 开源版）的口令是**公开常量**：它是格式的一部分，不承担任何安全职责。
DEFAULT_PASSKEY = "Qiyu-QY3-Portable-Role-v1"

# v2（.qy3 / 服务器版）内置派生种子：零配置方案的核心。与 v1 刻意不同值。
BUILTIN_QY3_SEED = "Qiyu-QY3-Server-Container-v2|2026-10|builtin-kek"
BUILTIN_SALT_LABEL = "QY3-KEK-builtin-v2"

KEK_MODE_BUILTIN = "builtin"
KEK_MODE_ENV = "env"

ENV_MASTER_KEY = "QIYU_QY3_MASTER_KEY"      # 可选：32B base64 / 64 hex / 任意口令
ENV_PASSPHRASE = "QIYU_QY3_KEY"             # 可选：v1 公开口令的覆盖（一般不设）


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(text: Any) -> bytes:
    return base64.b64decode(str(text or ""))


def v1_key(password: str = "") -> bytes:
    """v1 容器口令 → 32B 密钥（公开口令，等同不加密）。"""
    material = password or os.getenv(ENV_PASSPHRASE, "") or DEFAULT_PASSKEY
    return hashlib.sha256(("QIY3|" + material).encode("utf-8")).digest()


def env_master_key() -> bytes:
    """环境变量里显式配置的 KEK（可选覆盖）。返回 b"" 表示没配。"""
    raw = str(os.getenv(ENV_MASTER_KEY, "") or "").strip()
    if not raw:
        return b""
    try:
        data = b64d(raw)
        if len(data) == KEY_BYTES:
            return data
    except Exception:
        pass
    try:
        if len(raw) == KEY_BYTES * 2:
            return bytes.fromhex(raw)
    except Exception:
        pass
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=KEY_BYTES,
                      salt=b"QY3-KEK-v2", iterations=KDF_ITERATIONS
                      ).derive(raw.encode("utf-8"))


def builtin_kek(salt: bytes = b"") -> bytes:
    """内置常量派生：sha256(种子|标签|每文件 salt)[:32]。零配置主路径。"""
    h = hashlib.sha256()
    h.update(BUILTIN_QY3_SEED.encode("utf-8"))
    h.update(b"|")
    h.update(BUILTIN_SALT_LABEL.encode("utf-8"))
    h.update(b"|")
    h.update(salt or b"")
    return h.digest()[:KEY_BYTES]


def master_key(header: dict | None = None) -> bytes:
    """取 KEK；b"" 表示既没配也没法派生。

    读包时**按 header 声明的来源走**，不跨来源猜：
      - kek_mode=builtin → 内置派生（用 header 里的 builtin_salt）
      - 其它（kek_mode=env / 老包没这字段）→ env
    导出时（header=None）：env 优先，其次内置派生（空 salt）。
    """
    if header is not None:
        if str(header.get("kek_mode") or "") == KEK_MODE_BUILTIN:
            try:
                return builtin_kek(b64d(header.get("builtin_salt")))
            except Exception:
                return builtin_kek(b"")
        return env_master_key()
    return env_master_key() or builtin_kek(b"")


def derive_passphrase_key(password: str, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=KEY_BYTES,
                      salt=salt,
                      iterations=max(1, int(iterations or KDF_ITERATIONS))
                      ).derive(str(password).encode("utf-8"))
