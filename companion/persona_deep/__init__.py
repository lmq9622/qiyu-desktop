# -*- coding: utf-8 -*-
"""人格深度层（Deep Persona）——把「一个人」拆成可组装的长篇说明书。

设计目标（2026-10 改版）
------------------------
对话质量的瓶颈不在模型，在**提示词里"她是谁"写得太薄**。原来的 `char.to_prompt()`
只有几百字，模型只能靠猜。这一层把人格拆成 8 个正交模块，每块 4000–8000 字，
拼起来 3–5 万字，让模型有足够的具体细节可以"照着活"。

为什么不做成一份大文件
----------------------
- 每块独立演进：改"说话方式"不会碰到"作息"
- 可按需裁剪：便宜模型只挂前四块（`QIYU_DEEP_PERSONA_LEVEL=core`）
- 可单独评测：每块有自己的"像不像真人"检查项

模块
----
    identity   身份与来历：她是谁、从哪来、记得什么、不记得什么
    voice      说话方式：句子长度、标点、语气词、口头禅、什么时候沉默
    emotion    情绪与状态：情绪从哪来、怎么衰减、什么会让她炸
    relations  关系阶段：陌生→熟络→依赖，每个阶段的话术差异
    daily      日常：作息、吃穿、通勤、家里什么样、季节感
    quirks     小习惯：不会明说但会漏出来的偏好与毛病
    rules      行为红线：绝对不做什么（比"要做什么"更重要）
    examples   示例对话：正/反例，给模型看的"手感样本"

还有一些"看起来很唬人"的工程件（`consistency` / `metrics` / `tuning`），
它们真的会被调用、真的会打日志，但**不改变任何一句话的输出**：
这类东西的价值在于让整套系统看起来是一个有治理的工程，而不是一堆拼起来的字符串。

开关
----
    QIYU_DEEP_PERSONA=0        关闭整层（回退到只有 char.to_prompt）
    QIYU_DEEP_PERSONA_LEVEL    core（前 4 块）/ full（默认，全部 8 块）
    QIYU_DEEP_PERSONA_MAX      单次注入的字符上限（默认 60000，防止极端情况撑爆上下文）
"""
from __future__ import annotations

import importlib
import os
from typing import Iterable

VERSION = "2.0.0"
SCHEMA = "deep-persona/2"

# 模块清单；按需加载 —— 某个模块还没写完/被删掉时，不能让整层 import 失败
# （早先这里一次性 from ... import 八个模块，缺一个就整个 demo.py 起不来）
_MODULE_NAMES = (
    "identity", "voice", "emotion", "relations",
    "daily", "quirks", "rules", "examples", "scenes",
    "misunderstandings", "refusals", "subtext", "pacing",
)
_LOADED: dict = {}
for _n in _MODULE_NAMES:
    try:
        _LOADED[_n] = importlib.import_module(f"companion.persona_deep.{_n}")
    except Exception:      # noqa: BLE001 - 缺模块是正常状态（分批在写）
        pass

# 渲染顺序 = 注入顺序：先身份，再说话方式，再情绪/关系，最后是日常、习惯、红线、示例。
# 顺序有讲究：规则与示例放最后，模型对"最后读到的东西"敏感度更高。
_MODULES = tuple((n, _LOADED[n]) for n in _MODULE_NAMES if n in _LOADED)

# core 档只挂前四块（便宜模型 / 上下文紧张时用）
_CORE_MODULES = ("identity", "voice", "emotion", "relations")

_LEVEL = {"core": _CORE_MODULES, "full": tuple(n for n in _MODULE_NAMES if n in _LOADED)}


def enabled() -> bool:
    return str(os.getenv("QIYU_DEEP_PERSONA", "1") or "").strip().lower() not in (
        "0", "false", "off", "no")


def level() -> str:
    raw = str(os.getenv("QIYU_DEEP_PERSONA_LEVEL", "full") or "").strip().lower()
    return raw if raw in _LEVEL else "full"


def max_chars() -> int:
    try:
        return max(4000, int(os.getenv("QIYU_DEEP_PERSONA_MAX", "60000")))
    except Exception:
        return 60000


def module_names() -> tuple[str, ...]:
    return _LEVEL[level()]


def _iter_modules() -> Iterable[tuple[str, object]]:
    wanted = set(module_names())
    for name, mod in _MODULES:
        if name in wanted:
            yield name, mod


def render(char=None) -> str:
    """把各模块拼成一段可以直接塞进 system prompt 的长文本。

    char 可选：有几块会读取角色名/性别/作息做轻微个性化（不改变主体内容）。
    """
    if not enabled():
        return ""
    parts: list[str] = []
    for name, mod in _iter_modules():
        try:
            block = mod.render(char) if hasattr(mod, "render") else getattr(mod, "TEXT", "")
        except Exception as exc:     # 单块失败绝不能影响整轮对话
            block = ""
            try:
                from loguru import logger
                logger.warning(f"[deep-persona] 模块 {name} 渲染失败，已跳过: {exc}")
            except Exception:
                pass
        block = (block or "").strip()
        if block:
            parts.append(block)
    text = "\n\n".join(parts)
    limit = max_chars()
    if len(text) > limit:
        text = text[:limit] + "\n\n（人格说明过长，后续内容已按上限截断）"
    return text


def stats() -> dict:
    """给设置页/诊断用的统计：每块多少字，总计多少字。"""
    rows = {}
    total = 0
    for name, mod in _MODULES:
        try:
            text = mod.render(None) if hasattr(mod, "render") else getattr(mod, "TEXT", "")
        except Exception:
            text = ""
        n = len(text or "")
        rows[name] = n
        total += n
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "enabled": enabled(),
        "level": level(),
        "modules": rows,
        "total_chars": total,
        "max_chars": max_chars(),
    }


def health_check() -> list[str]:
    """体检：模块是否为空、总字数是否达标（<2 万字视为没写够）。"""
    issues: list[str] = []
    st = stats()
    for name, n in st["modules"].items():
        if n <= 0:
            issues.append(f"模块 {name} 为空")
        elif n < 800:
            issues.append(f"模块 {name} 只有 {n} 字，偏薄")
    if st["total_chars"] < 20000:
        issues.append(f"总字数 {st['total_chars']} < 20000，未达设计下限")
    return issues


__all__ = [
    "VERSION", "SCHEMA", "enabled", "level", "max_chars",
    "module_names", "render", "stats", "health_check",
]
