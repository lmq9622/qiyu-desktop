# -*- coding: utf-8 -*-
"""Qiyu 运行时状态与配置（从 demo.py 迁移，M1）。"""
import os
import sys
import json
from loguru import logger
from pathlib import Path

def get_resource_path(relative_path: str = "") -> Path:
    if hasattr(sys, '_MEIPASS'):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent
    if relative_path:
        return base / relative_path
    return base

def get_data_dir() -> Path:
    """可写数据目录（exe 环境用用户目录，避免写入临时解压目录）"""
    env_data = os.getenv("QIYU_DATA_DIR", "")
    if env_data:
        d = Path(env_data)
    elif hasattr(sys, "_MEIPASS"):
        d = Path(os.path.expanduser("~")) / ".ai_companion" / "data"
    else:
        d = PROJECT_DIR / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


import re as _re


def get_users_dir() -> Path:
    """全部按用户隔离的目录根：/data/users/<user_key>/"""
    d = get_data_dir() / "users"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_user_name(user_key: str) -> str:
    s = _re.sub(r"[^0-9A-Za-z_\-]+", "_", str(user_key or "").strip())
    return (s or "anon")[:64]


def user_dir(user_key: str, sub: str = "") -> Path:
    d = get_users_dir() / safe_user_name(user_key)
    if sub:
        d = d / sub
    d.mkdir(parents=True, exist_ok=True)
    return d

PROJECT_DIR = get_resource_path()

STATIC_DIR = PROJECT_DIR / "gateway" / "static"

DEMO_PORT = int(os.getenv("QIYU_PORT", "8765"))

DEMO_HOST = os.getenv("QIYU_HOST", "0.0.0.0")

LLM_URL = os.getenv("LLM_BASE_URL", "http://192.168.2.6:8081/v1")

LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.6-35b-a3b-uncensored-heretic")

LLM_ROUTE_URL = os.getenv("LLM_ROUTE_URL", "")

LLM_ROUTE_MODEL = os.getenv("LLM_ROUTE_MODEL", "")

DEFAULT_AVATAR_DIR = STATIC_DIR / "avatars"

SETTINGS_JSON = get_data_dir() / "_runtime_settings.json"

AVATAR_UPLOAD_DIR = get_data_dir() / "avatars"

RELATIONS_JSON = get_data_dir() / "relations.json"

PROACTIVE_JSON = get_data_dir() / "proactive.json"

SCHEDULES_JSON = get_data_dir() / "schedules.json"

CONV_STATE_JSON = get_data_dir() / "conv_state.json"

SHARED_EVENTS_JSON = get_data_dir() / "shared_events.json"

EMOTIONS_JSON = get_data_dir() / "emotions.json"

def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ============ 按用户物理隔离的状态存储 ============
# 以前 relations/emotions/conv_state/proactive/schedules/shared_events 都是"全站一个大 JSON"，
# 靠 key 隔离。现在每个用户单独一个文件：/data/users/<user_key>/<kind>.json
def _key_user(k: str) -> str:
    return (str(k).split(":", 1)[0] or "anon") if k else "anon"


def _atomic_write(path: Path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[状态] 写盘失败 {path}: {e}")


def save_user_store(kind: str, store: dict) -> None:
    """把扁平 store（key = 用户 或 用户:角色）按用户拆开，各自写自己目录。"""
    groups: dict = {}
    for k, v in (store or {}).items():
        groups.setdefault(_key_user(k), {})[k] = v
    for u, part in groups.items():
        _atomic_write(user_dir(u) / f"{kind}.json", part)


def _load_user_store(kind: str) -> dict:
    """加载所有用户各自的 <kind>.json（老的全站大文件也会读进来，供一次性迁移）"""
    out: dict = {}
    legacy = {"relations": RELATIONS_JSON, "proactive": PROACTIVE_JSON,
              "schedules": SCHEDULES_JSON, "conv_state": CONV_STATE_JSON,
              "shared_events": SHARED_EVENTS_JSON, "emotions": EMOTIONS_JSON}.get(kind)
    if legacy is not None:
        out.update(_load_json(legacy))
    try:
        for p in get_users_dir().glob(f"*/{kind}.json"):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    out.update(d)
            except Exception:
                pass
    except Exception:
        pass
    return out


def _migrate_user_store(kind: str, legacy: Path) -> None:
    """一次性把老的全站大文件拆成按用户文件，然后把老文件改名 .migrated"""
    try:
        if not legacy.exists():
            return
        data = _load_json(legacy)
        if isinstance(data, dict) and data:
            save_user_store(kind, data)
        legacy.rename(legacy.with_suffix(legacy.suffix + ".migrated"))
        logger.info(f"[状态] {legacy.name} 已按用户拆分到 users/<用户>/{kind}.json")
    except Exception as e:
        logger.warning(f"[状态] {kind} 迁移失败: {e}")


_relations_store: dict = _load_user_store("relations")

_proactive_store: dict = _load_user_store("proactive")

_schedules_store: dict = _load_user_store("schedules")

_conv_store: dict = _load_user_store("conv_state")

_shared_events: dict = _load_user_store("shared_events")

_emotions_store: dict = _load_user_store("emotions")

for _kind, _path in (("relations", RELATIONS_JSON), ("proactive", PROACTIVE_JSON),
                     ("schedules", SCHEDULES_JSON), ("conv_state", CONV_STATE_JSON),
                     ("shared_events", SHARED_EVENTS_JSON), ("emotions", EMOTIONS_JSON)):
    _migrate_user_store(_kind, _path)

_EVIDENCE_CACHE = {}

_USER_NET_CACHE = {"at": 0.0, "text": ""}

user_states: dict = {}

llm_client = None


__all__ = [
    "get_users_dir",
    "user_dir",
    "save_user_store",
    "AVATAR_UPLOAD_DIR",
    "CONV_STATE_JSON",
    "DEFAULT_AVATAR_DIR",
    "DEMO_HOST",
    "DEMO_PORT",
    "EMOTIONS_JSON",
    "LLM_MODEL",
    "LLM_ROUTE_MODEL",
    "LLM_ROUTE_URL",
    "LLM_URL",
    "PROACTIVE_JSON",
    "PROJECT_DIR",
    "RELATIONS_JSON",
    "SCHEDULES_JSON",
    "SETTINGS_JSON",
    "SHARED_EVENTS_JSON",
    "STATIC_DIR",
    "_EVIDENCE_CACHE",
    "_USER_NET_CACHE",
    "_conv_store",
    "_emotions_store",
    "_load_json",
    "_proactive_store",
    "_relations_store",
    "_schedules_store",
    "_shared_events",
    "get_data_dir",
    "get_resource_path",
    "llm_client",
    "user_states",
]
