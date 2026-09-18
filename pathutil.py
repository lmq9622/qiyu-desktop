"""
栖语 (Qiyu) - 数据目录统一解析
开发模式：项目根 /data
exe 模式（PyInstaller）：~/.ai_companion/data（持久化，避免写入临时解压目录）
可用环境变量 QIYU_DATA_DIR 覆盖
"""
import os
import sys
from pathlib import Path


def get_data_dir() -> Path:
    env_data = os.getenv("QIYU_DATA_DIR", "")
    if env_data:
        d = Path(env_data)
    elif hasattr(sys, "_MEIPASS"):
        d = Path(os.path.expanduser("~")) / ".ai_companion" / "data"
    else:
        d = Path(__file__).resolve().parent / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============ 按用户隔离的数据目录（服务器版多用户必须物理分开） ============
# 约定：/data/users/<user_key>/<kind>/...
#   chat_history/  memory/  chat_logs/  outlines/  knowledge/  relations.json ...
import re as _re


def get_users_dir() -> Path:
    d = get_data_dir() / "users"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_user_name(user_key: str) -> str:
    s = _re.sub(r"[^0-9A-Za-z_\-]+", "_", str(user_key or "").strip())
    return (s or "anon")[:64]


def user_dir(user_key: str, sub: str = "") -> Path:
    """取某个用户自己的数据目录（自动创建）。sub 为空就是用户根目录。"""
    d = get_users_dir() / safe_user_name(user_key)
    if sub:
        d = d / sub
    d.mkdir(parents=True, exist_ok=True)
    return d
