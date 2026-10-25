# -*- coding: utf-8 -*-
"""发行版标记（由构建注入）。

同一份源码树打两个包：

  EDITION="server" → 包名 `qy3engine`   容器 v2(.qy3) + 基础 RAG + 行为偏好(behavior/)
  EDITION="open"   → 包名 `qy3fengine`  容器 v1(.qy3f) + 基础 RAG（物理不含 behavior/）

源码里这一行默认是 "server"（开发/测试态）；`build/build_open.sh` 在暂存目录里
把它改写成 "open"，`build/build_server.sh` 改写成 "server"。
运行期不做"包含哪些文件"的判断——open 包**物理上没有** behavior/ 目录。
"""
from __future__ import annotations

EDITION = "open"

IS_SERVER = EDITION == "server"
IS_OPEN = EDITION == "open"


class EditionError(RuntimeError):
    """当前发行版不提供这个能力（例如 open 版调用 behavior 模块）。"""

    def __init__(self, feature: str, message: str = ""):
        self.feature = feature
        super().__init__(message or f"当前发行版（{EDITION}）不包含 {feature}")


def require_server(feature: str) -> None:
    """server-only 功能的统一入口检查。"""
    if not IS_SERVER:
        raise EditionError(feature)


def is_server() -> bool:
    """运行期判断（读模块全局，构建注入后立刻生效）。"""
    return EDITION == "server"


def is_open() -> bool:
    """open 版判定：EDITION 不是 "server" 就按 open 处理（fail-closed）。"""
    return EDITION != "server"


def edition_info() -> dict:
    return {"edition": EDITION, "is_server": IS_SERVER, "is_open": IS_OPEN}


def package_name() -> str:
    """发行包名（用于错误信息与文档；import 名字由 wheel 决定）。"""
    return "qy3engine" if IS_SERVER else "qy3fengine"
