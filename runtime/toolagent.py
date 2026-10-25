# -*- coding: utf-8 -*-
"""Qiyu Tool Agent（工具代理，M4）。

规格（二十一、五十四）：工具/搜索必须真实执行，禁止假装工具调用成功、假装搜索成功。
ToolAgent：子代理规划关键词 → 真实执行（tools.web）→ 返回诚实 ToolEvidence。
只有 success=True 且 items 非空时，主模型才允许声称「查到了/发你了」；否则必须如实说没查到。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from loguru import logger

from runtime.providers import AIProvider, ProviderKind, ProviderStatus
from tools import web as web_tools


@dataclass
class ToolEvidence:
    """工具执行的诚实证据：success=False 时 items 必须为空，不允许半真半假。"""
    success: bool
    items: list
    query: str = ""
    backend: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "items": self.items[:6] if isinstance(self.items, list) else [],
            "query": self.query,
            "backend": self.backend,
            "error": self.error,
        }


class ToolAgent(AIProvider):
    """工具代理：规划 → 真实执行 → 诚实证据。

    planner 由业务层注入（依赖 Main Brain / 路由模型的搜索子代理），
    本模块不直接耦合任何具体 LLM。
    """

    kind = ProviderKind.TOOL
    id = "task-agent"
    name = "Tool Agent（真实联网/搜图，禁止假装成功）"

    def __init__(self) -> None:
        super().__init__()
        self.planner: Optional[Callable[[str], Awaitable[list]]] = None

    def probe(self) -> ProviderStatus:
        from companion.settings import load_runtime_settings
        enabled = bool(load_runtime_settings().get("web_enabled", True))
        return ProviderStatus(
            available=bool(enabled and self.planner is not None),
            backend="api" if enabled else "off",
            device="tools.web",
            reason="" if enabled and self.planner is not None else ("联网已关闭" if not enabled else "规划器未注入"),
        )

    def status(self) -> ProviderStatus:
        # 可用性依赖运行时开关，实时探测
        return self.probe()

    def _record(self, ev: ToolEvidence, tool: str) -> None:
        """工具执行落账：分层日志(tool.log) + 统一 DB(tool_calls)。"""
        try:
            from runtime.logging_setup import logger_tool
            logger_tool.info(
                f"[ToolAgent] {tool} | query={ev.query[:120]!r} | "
                f"success={ev.success} items={len(ev.items)} backend={ev.backend}"
            )
        except Exception:
            pass
        try:
            from runtime.db import unified_store
            unified_store.record_tool_call(
                "web_user", "", tool, ev.query, success=ev.success,
                latency_ms=0.0,
                evidence={"items": [it.get("url") or it.get("image_url") or "" for it in ev.items[:6]]},
            )
        except Exception:
            pass

    async def _plan(self, query: str) -> list:
        if self.planner is None:
            return [query[:80]]
        try:
            return await self.planner(query)
        except Exception as e:
            logger.warning(f"[ToolAgent] 搜索规划失败: {e}")
            return [query[:80]]

    async def search(self, query: str) -> ToolEvidence:
        """真实联网搜索：成功/失败都如实上报，绝不编造结果。受全局并发总闸(tool 分闸)约束。"""
        query = (query or "").strip()
        if not query:
            return ToolEvidence(False, [], query, backend="empty-query")
        from runtime.concurrency import concurrency_limiter
        async with concurrency_limiter.slot("tool"):
            # 离线机器连不出去时必须快速失败，否则工具协程挂死、占满 tool 分闸，
            # 把整条聊天链路堵住。
            try:
                return await asyncio.wait_for(self._search_locked(query), timeout=12)
            except Exception as e:
                logger.warning(f"[ToolAgent] 搜索超时/失败: {e}")
                return ToolEvidence(False, [], query, backend="timeout")

    async def _search_locked(self, query: str) -> ToolEvidence:
        keywords = await self._plan(query)
        if not keywords:
            return ToolEvidence(False, [], query, backend="plan-none")
        results = []
        seen_urls = set()
        for kw in keywords:
            try:
                for it in await web_tools.web_search(kw, top_k=4):
                    u = it.get("url") or ""
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        results.append(it)
            except Exception as e:
                logger.warning(f"[ToolAgent] 搜索失败 {kw}: {e}")
            if len(results) >= 6:
                break
        if not results and any(k in query for k in ("视频", "链接", "热门", "新闻")):
            try:
                hot = await web_tools.fetch_hotlist()
                results = hot[:4] or []
            except Exception as e:
                logger.warning(f"[ToolAgent] 热门兜底失败: {e}")
        ev = ToolEvidence(bool(results), results[:6], query, backend="web")
        self._record(ev, "search")
        return ev

    async def search_images(self, query: str) -> ToolEvidence:
        """真实搜图：返回 [{title, url, image_url}]；没找到就如实说没有，绝不假装拍图。受全局并发总闸约束。"""
        query = (query or "").strip()
        if not query:
            return ToolEvidence(False, [], query, backend="empty-query")
        from runtime.concurrency import concurrency_limiter
        async with concurrency_limiter.slot("tool"):
            try:
                return await asyncio.wait_for(self._search_images_locked(query), timeout=12)
            except Exception as e:
                logger.warning(f"[ToolAgent] 搜图超时/失败: {e}")
                return ToolEvidence(False, [], query, backend="timeout")

    async def _search_images_locked(self, query: str) -> ToolEvidence:
        keywords = await self._plan(query)
        if not keywords:
            return ToolEvidence(False, [], query, backend="plan-none")
        seen = set()
        out = []
        for kw in keywords[:2]:
            try:
                for it in await web_tools.image_search(kw, top_k=3):
                    u = it.get("image_url") or ""
                    if u and u not in seen:
                        seen.add(u)
                        out.append(it)
            except Exception as e:
                logger.warning(f"[ToolAgent] 搜图失败 {kw}: {e}")
            if len(out) >= 3:
                break
        ev = ToolEvidence(bool(out), out[:3], query, backend="image")
        self._record(ev, "image_search")
        return ev


tool_agent = ToolAgent()


__all__ = [
    "ToolAgent",
    "ToolEvidence",
    "tool_agent",
]
