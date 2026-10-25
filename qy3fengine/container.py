# -*- coding: utf-8 -*-
"""容器读写：`.qy3`(v2) / `.qy3f`(v1) 的 magic、header、nonce、payload。

移植自 `ai-companion/characters/qy3.py:27-427`（常量、header 规范化、AEAD 的 AAD 用
**原始字节**这几处必须逐字一致，否则线上已发出的包全部认证失败）。

字节布局（详见 docs/FORMAT_SPEC.md）：

    magic(4) | header_len(4, big-endian) | header_utf8(json) | nonce(12) | ciphertext

  v1 `.qy3f`：MAGIC=QY3\\x01，AES-256-GCM，key=sha256("QIY3|"+公开口令)，
              AAD=MAGIC；header 不参与认证（历史格式，不能改）。
  v2 `.qy3` ：MAGIC=QY3\\x02，AES-256-GCM + 信封 KEK（DEK 每包随机），
              AAD=header 原始字节。

发行版边界（判据是 magic + format_version，不是 soft 标记）：
  open 版：只写 v1、只收 v1；遇到 v2 → QY3Error("format_mismatch")
  server 版：只写 v2（除非显式 open_format=True）；默认只收 v2，
             open_format=None 时两种都收（便于从 v1 迁移）
"""
from __future__ import annotations

import json
import os
import struct
from datetime import datetime
from typing import Any

import msgpack
import zstandard
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .edition import is_open
from .keys import (
    ENV_MASTER_KEY, KEK_MODE_BUILTIN, KEK_MODE_ENV, KEY_BYTES, KDF_ITERATIONS,
    KDF_PBKDF2, NONCE_BYTES, SALT_BYTES, b64d, b64e, builtin_kek,
    derive_passphrase_key, env_master_key, master_key, v1_key,
)
from .schema import (
    CIPHER, CIPHER_NONE, COMPRESSION, ENGINE_MIN_VERSION, FORMAT_VERSION,
    FORMAT_VERSION_V2, PAYLOAD_ENCODING, QY3Error, SCHEMA_VERSION,
)

MAGIC = b"QY3\x01"          # v1：桌面开源版 .qy3f（= 老服务器格式）
MAGIC_V2 = b"QY3\x02"       # v2：服务器版 .qy3
ALL_MAGICS = (MAGIC_V2, MAGIC)


# ============ header ============
def header_bytes(header: dict) -> bytes:
    """header 的规范化字节。v2 用它做 AEAD 的 AAD —— 序列化参数不许改：
    sort_keys / 分隔符 / ensure_ascii 任何一处变了，解密端重算就对不上。"""
    return json.dumps(header, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def read_header(data: bytes) -> tuple[bytes, dict, bytes, bytes]:
    """解析容器头，返回 (body, header, header_raw, magic)。

    v2 起 header 参与 AEAD 认证，所以必须把**原始字节**一并吐出，
    不能用重新序列化的结果顶替。
    """
    magic = b""
    for m in ALL_MAGICS:
        if data[:len(m)] == m:
            magic = m
            break
    if not magic:
        raise QY3Error("bad_magic", "文件损坏：不是 QY3 格式")
    head = len(magic)
    if len(data) < head + 4:
        raise QY3Error("bad_header", "文件损坏：QY3 header 不完整")
    header_len = struct.unpack(">I", data[head:head + 4])[0]
    start = head + 4
    end = start + header_len
    if header_len <= 0 or end > len(data):
        raise QY3Error("bad_header", "文件损坏：QY3 header 长度非法")
    raw = data[start:end]
    try:
        header = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise QY3Error("bad_header", "文件损坏：QY3 header 不是合法 JSON") from exc
    if not isinstance(header, dict):
        raise QY3Error("bad_header", "文件损坏：QY3 header 必须是对象")
    return data[end:], header, raw, magic


def kek_mode_of(header: dict) -> str:
    """header 声明的 KEK 来源（v1 没有这个概念，返回 "public-constant"）。"""
    mode = str(header.get("key_mode") or "")
    if mode == "envelope":
        return str(header.get("kek_mode") or KEK_MODE_ENV)
    if mode == "passphrase":
        return "passphrase"
    return "public-constant" if header.get("open_format") else mode


# ============ 加密计划与信封 ============
def wrap_dek(kek: bytes, extra: dict) -> tuple[str, bytes, dict]:
    """每包随机 DEK，用 KEK 包裹；额外字段并进 header。"""
    dek = os.urandom(KEY_BYTES)
    wrap_nonce = os.urandom(NONCE_BYTES)
    wrapped = AESGCM(kek).encrypt(wrap_nonce, dek, b"QY3-DEK-v2")
    out = {"cipher": CIPHER, "key_mode": "envelope", "kdf": "none",
           "wrapped_key": b64e(wrapped), "wrap_nonce": b64e(wrap_nonce)}
    out.update(extra)
    return "envelope", dek, out


def encryption_plan(password: str = "", builtin_kek_enabled: bool = True) -> tuple[str, bytes, dict]:
    """决定怎么加密，返回 (mode, key, header 附加字段)。

    优先级（服务器版零配置即可导出）：
      1. QIYU_QY3_MASTER_KEY      → 信封加密，header 记 kek_mode=env
      2. 调用方给了 password / QIYU_QY3_KEY → PBKDF2 派生
      3. 内置常量派生（默认路径）  → 每文件随机 salt 写进 header，kek_mode=builtin
      4. builtin_kek_enabled=False 时可能返回 ("", b"", {})，交调用方决定
    """
    master = env_master_key()
    if master:
        return wrap_dek(master, {"kek_mode": KEK_MODE_ENV})
    passphrase = str(password or os.getenv("QIYU_QY3_KEY", "") or "")
    if passphrase:
        salt = os.urandom(SALT_BYTES)
        key = derive_passphrase_key(passphrase, salt, KDF_ITERATIONS)
        return "passphrase", key, {
            "cipher": CIPHER, "key_mode": "passphrase", "kdf": KDF_PBKDF2,
            "kdf_iterations": KDF_ITERATIONS, "salt": b64e(salt),
        }
    if builtin_kek_enabled:
        salt = os.urandom(SALT_BYTES)
        return wrap_dek(builtin_kek(salt), {
            "kek_mode": KEK_MODE_BUILTIN, "builtin_salt": b64e(salt),
        })
    return "", b"", {}


def unwrap_key(header: dict, password: str = "") -> bytes:
    """按 header 声明的方式取回内容密钥。拿不到就抛——绝不猜、绝不降级。"""
    mode = str(header.get("key_mode") or "")
    if mode == "envelope":
        kek = master_key(header)
        if not kek:
            raise QY3Error(
                "no_master_key",
                f"这个包是用环境变量 KEK（{ENV_MASTER_KEY}）封装的：本机没有配置它，无法解开")
        try:
            return AESGCM(kek).decrypt(b64d(header.get("wrap_nonce")),
                                       b64d(header.get("wrapped_key")),
                                       b"QY3-DEK-v2")
        except InvalidTag as exc:
            raise QY3Error(
                "auth_failed",
                "认证失败：主密钥/内置派生对不上，或文件被篡改") from exc
    if mode == "passphrase":
        passphrase = str(password or os.getenv("QIYU_QY3_KEY", "") or "")
        if not passphrase:
            raise QY3Error("password_required", "这个包需要密码")
        return derive_passphrase_key(passphrase, b64d(header.get("salt")),
                                     int(header.get("kdf_iterations") or KDF_ITERATIONS))
    raise QY3Error("unsupported_algorithm", f"不支持的 key_mode: {mode or '(空)'}")


# ============ payload 编解码 ============
def unpack_payload(compressed: bytes) -> dict:
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
    return payload


def _base_header() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "engine_min_version": ENGINE_MIN_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "compression": COMPRESSION,
        "payload_encoding": PAYLOAD_ENCODING,
    }


def pack_v1(compressed: bytes, password: str = "", warning: str = "") -> bytes:
    """写 v1 容器（桌面开源版 / 老服务器格式）。口令是公开常量，不提供保密性。"""
    header = dict(_base_header(), format_version=FORMAT_VERSION, cipher=CIPHER,
                  open_format=True)
    if warning:
        header["warning"] = warning
    raw = header_bytes(header)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(v1_key(password)).encrypt(nonce, compressed, MAGIC)
    return MAGIC + struct.pack(">I", len(raw)) + raw + nonce + ciphertext


def encode_payload(payload: dict, password: str = "", *,
                   open_format: bool = False,
                   allow_open_fallback: bool = False,
                   builtin_kek_enabled: bool = True) -> bytes:
    """写容器。两套格式互不兼容，这就是发行版边界。"""
    try:
        packed = msgpack.packb(payload, use_bin_type=True)
        compressed = zstandard.ZstdCompressor(level=3).compress(packed)
    except Exception as exc:
        raise QY3Error("encode_failed", f"QY3 编码失败: {exc}") from exc

    # ---- v1：桌面开源版（.qy3f）/ 老服务器格式 ----
    if open_format:
        return pack_v1(compressed, password)

    if is_open():
        raise QY3Error(
            "format_mismatch",
            "开源版只能导出 .qy3f（v1）；v2 加密容器只在服务器版 qy3engine 里提供")

    # ---- v2：服务器版容器（零配置可导出）----
    mode, key, extra = encryption_plan(password, builtin_kek_enabled)
    if not mode:
        if not allow_open_fallback:
            raise QY3Error(
                "no_key",
                f"服务器版导出必须加密：请配置 {ENV_MASTER_KEY} 或传入导出密码；"
                "桌面版请用 .qy3f 后缀（那套格式本来就不加密）")
        return pack_v1(compressed, password, warning="no_key_material_local_only")

    header = dict(_base_header(), format_version=FORMAT_VERSION_V2,
                  cipher=CIPHER_NONE, key_mode="none", kdf="none",
                  aad="header", open_format=False)
    header.update(extra)
    raw = header_bytes(header)
    nonce = os.urandom(NONCE_BYTES)
    try:
        ciphertext = AESGCM(key).encrypt(nonce, compressed, raw)
    except Exception as exc:
        raise QY3Error("encode_failed", f"QY3 加密失败: {exc}") from exc
    return MAGIC_V2 + struct.pack(">I", len(raw)) + raw + nonce + ciphertext


def decode_payload(data: bytes, password: str = "",
                   *, open_format: bool | None = False) -> tuple[dict, dict]:
    """读容器。两套格式不互通，这里就是那道门。

    open_format：
      False（服务器版默认）：只收 v2，v1 一律拒
      True ：只收 v1，v2 一律拒
      None ：两种都收，按 magic 自动识别（迁移/内部件用）
    另外：open 发行版**永远**只收 v1（它没有 KEK），与 open_format 无关。
    """
    body, header, raw, magic = read_header(data)
    if int(header.get("format_version") or 0) > FORMAT_VERSION_V2:
        raise QY3Error("version_too_new", "QY3 版本过高，当前客户端无法导入")
    if header.get("compression") != COMPRESSION:
        raise QY3Error("unsupported_algorithm", "不支持的 QY3 压缩算法")

    is_v1 = magic == MAGIC
    if is_open() and not is_v1:
        raise QY3Error(
            "format_mismatch",
            "这是服务器版的 v2 加密角色包（.qy3）；开源版只认 .qy3f（v1），"
            "两套格式不互通 —— 请让对方用开源版重新导出")
    if open_format is True and not is_v1:
        raise QY3Error(
            "format_mismatch",
            "这是服务器版的 v2 加密角色包；开源格式只认 .qy3f（v1），两套格式不互通")
    if open_format is False and is_v1:
        raise QY3Error(
            "format_mismatch",
            "这是开源版 / 老服务器导出的 v1 角色包（.qy3f）；服务器版只收 v2 加密包，"
            "两套格式不互通 —— 请对方用服务器版重新导出 .qy3（或显式 open_format=None 迁移）")

    # ---- v1：开源版 / 老服务器格式（公开口令，非保密载体）----
    if is_v1:
        if header.get("cipher") != CIPHER:
            raise QY3Error("unsupported_algorithm", "不支持的 QY3 加密算法")
        nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
        if len(nonce) != NONCE_BYTES or len(ciphertext) < 16:
            raise QY3Error("bad_payload", "文件损坏：QY3 密文不完整")
        try:
            compressed = AESGCM(v1_key(password)).decrypt(nonce, ciphertext, MAGIC)
        except InvalidTag as exc:
            raise QY3Error("auth_failed", "认证失败：密码错误或文件被篡改") from exc
        header["_legacy_v1"] = True
        return unpack_payload(compressed), header

    # ---- v2：服务器版加密格式（header 参与认证）----
    cipher = str(header.get("cipher") or "")
    if cipher != CIPHER:
        raise QY3Error("unsupported_algorithm", f"不支持的加密算法: {cipher or '(空)'}")
    if header.get("aad") != "header":
        raise QY3Error("unsupported_algorithm", "不支持的 AAD 方式")
    nonce, ciphertext = body[:NONCE_BYTES], body[NONCE_BYTES:]
    if len(nonce) != NONCE_BYTES or len(ciphertext) < 16:
        raise QY3Error("bad_payload", "文件损坏：QY3 密文不完整")
    key = unwrap_key(header, password)
    try:
        compressed = AESGCM(key).decrypt(nonce, ciphertext, raw)
    except InvalidTag as exc:
        raise QY3Error(
            "auth_failed", "认证失败：密码错误 / 主密钥不匹配 / 文件被篡改") from exc
    return unpack_payload(compressed), header


def magic_name(data: bytes) -> str:
    """给日志/报告用的 magic 文本（不打印正文）。"""
    for m in ALL_MAGICS:
        if data[:len(m)] == m:
            return "QY3\\x0%d" % m[-1]
    return repr(data[:4])


def describe(path: str | os.PathLike) -> dict:
    """容器体检：size / sha256 / magic / header 摘要（不解 payload）。"""
    import hashlib
    from pathlib import Path

    data = Path(path).read_bytes()
    header = read_header(data)[1]
    return {
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
    }


__all__ = [
    "MAGIC", "MAGIC_V2", "ALL_MAGICS",
    "header_bytes", "read_header", "kek_mode_of",
    "wrap_dek", "encryption_plan", "unwrap_key",
    "pack_v1", "encode_payload", "decode_payload", "unpack_payload",
    "magic_name", "describe",
]
