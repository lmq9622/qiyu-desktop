# -*- coding: utf-8 -*-
"""Qiyu Runtime · RuntimeManager（规格§12）。

负责：启动 / 停止 / 检测 / backend 选择 / 模型加载卸载 / 进程管理 / 崩溃恢复 / 日志。
目标：Qiyu.exe → RuntimeManager → RealtimeBrain / MainBrain / TTS / STT / Tool / Memory
所有服务生命周期统一管理。

backend 选择（规格§4/§13）：HardwareDetector + BackendCapability + MicroBenchmark 共同决定，
不是「检测到 GPU 就用 GPU」。
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from runtime.benchmark import BenchmarkResult, micro_benchmark
from runtime.hardware import BackendCapability, HardwareDetector, HardwareProfile
from runtime.providers import ProviderKind, ProviderRegistry, RealtimeBrainProvider
from runtime.realtime import (UnavailableRealtimeBackend, build_realtime_backend,
                              discover_models)

# backend 优先级：NVIDIA+CUDA → Vulkan → CPU（仅作初始候选排序，最终由 MicroBenchmark 定夺）
BACKEND_PRIORITY = ("cuda", "vulkan", "cpu")


class RuntimeManager:
    """Qiyu 运行时管理器：统一管理 Provider 生命周期与 backend 选择。"""

    def __init__(self) -> None:
        self.registry = ProviderRegistry()
        self.hardware = HardwareDetector()
        self.profile: Optional[HardwareProfile] = None
        self.backends: list[BackendCapability] = []
        self._started_at: float = 0.0
        self._realtime: Optional[RealtimeBrainProvider] = None
        self._realtime_backend: Optional[BackendCapability] = None
        self._crashes: dict[str, list[float]] = {}  # provider_id -> 崩溃时间戳
        self._bench_results: list[BenchmarkResult] = []

    # ---------- 启动/停止 ----------
    def start(self) -> None:
        self.profile = self.hardware.detect()
        self.backends = self.hardware.probe_backends()
        self._started_at = time.time()
        # Realtime Brain：按硬件能力 + MiniMind-O 模型存在性选择真实后端（诚实降级）
        self._realtime = build_realtime_backend(self.profile, self.backends)
        self.registry.register(self._realtime, fallback_ids=[])
        self._realtime_backend = self.select_backend("realtime")
        logger.info(
            f"[Runtime] 启动完成，Realtime Brain={self._realtime.id} "
            f"backend={self._realtime_backend.backend if self._realtime_backend else '无'}"
        )

    def stop(self) -> None:
        self._started_at = 0.0
        logger.info("[Runtime] 已停止")

    @property
    def running(self) -> bool:
        return self._started_at > 0

    # ---------- 微基准 + backend 选择（规格§4/§13） ----------
    async def bench(self, bench_fn=None) -> list[BenchmarkResult]:
        """对可用候选 backend 跑 MicroBenchmark（真实 bench_fn 或硬件启发式）。

        默认 bench_fn：对当前 Realtime 后端真实测 TTFT/tok/s（仅测它能跑的 backend，
        其它候选交给启发式估算，绝不把测不到的 backend 当实测）。"""
        candidates = [c for c in self.backends if c.available]
        if bench_fn is None:
            bench_fn = self._default_bench_fn()
        self._bench_results = await micro_benchmark.benchmark(candidates, self.profile, bench_fn)
        return self._bench_results

    def _default_bench_fn(self):
        """把当前 Realtime 后端的 bench_inference 包成 MicroBenchmark 需要的回调。"""
        backend_name = getattr(self._realtime, "backend_name", "") if self._realtime else ""
        bench_inf = getattr(self._realtime, "bench_inference", None) if self._realtime else None
        if not backend_name or bench_inf is None:
            return None  # 无真实后端 → 纯硬件启发式（和之前行为一致）

        async def _fn(cap: BackendCapability):
            if cap.backend != backend_name:
                return None  # 该 backend 跑不了当前模型 → 启发式兜底，不假装实测
            return await bench_inf()
        return _fn

    def select_backend(self, role: str) -> Optional[BackendCapability]:
        """先看基准结果，没有则按能力矩阵 + 优先级选择（CPU 永远是底线）。"""
        candidates = [c for c in self.backends if c.available]
        if role == "realtime":
            candidates = [c for c in candidates if c.text]
            # 只保留当前 Realtime 后端真实能跑的 backend（如 MiniMind-O 只有 cpu/cuda）
            supported = getattr(self._realtime, "supported_backends", None)
            if callable(supported):
                sup = set(supported())
                candidates = [c for c in candidates if c.backend in sup]
        if self._bench_results:
            top = self._bench_results[0]
            hit = next((c for c in candidates if c.backend == top.backend), None)
            if hit is not None:
                return hit
        for prio in BACKEND_PRIORITY:
            for c in candidates:
                if c.backend == prio:
                    return c
        return candidates[0] if candidates else None

    # ---------- 模型加载/卸载（规格§12/§11） ----------
    def model_status(self) -> dict:
        """MiniMind-O 模型清单 + 组件运行计划（每组件独立 backend，规格§6）。"""
        model = discover_models()
        plan = []
        if self._realtime is not None and hasattr(self._realtime, "model_plan"):
            try:
                plan = self._realtime.model_plan(self.backends)
            except Exception:
                plan = []
        omni = getattr(self._realtime, "_runtime", None)
        if not hasattr(omni, "model_dir"):
            # llama.cpp 后端的 _runtime 是字符串标签（"llama_cpp"/"none"），不是 omni 加载器；
            # 直接取 .model_dir 会 AttributeError，导致 /v1/runtime/status 整页 500。
            omni = None
        _rt = getattr(self._realtime, "_runtime", "") if self._realtime else ""
        if not isinstance(_rt, str):
            # omni 后端的 _runtime 是加载器对象：只上报字符串标签，绝不把对象塞进状态
            _rt = str(getattr(self._realtime, "runtime_label", "")) or type(_rt).__name__
        return {
            "models_dir": str(model.root),
            "thinker_present": model.has_thinker(),
            "components": model.present_components(),
            "plan": [p.to_dict() for p in plan],
            "runtime": _rt,
            "realtime_available": self._realtime.status().available if self._realtime else False,
            "omni": {
                "model_dir": str(omni.model_dir) if omni else "",
                "complete": omni.available() if omni else False,
                "device": omni.backend_name if omni else "",
                "loaded": omni.loaded if omni else False,
                "load_error": omni.load_error or "" if omni else "",
            } if omni else None,
        }

    def load_model(self, path: str = "") -> dict:
        """加载 MiniMind-O Thinker 权重（安装器下载后调用）；成功返回 status。"""
        try:
            if hasattr(self._realtime, "_runtime") and getattr(self._realtime, "_runtime", None) is not None:
                rt = self._realtime._runtime
                if not rt.available():
                    return {"ok": False, "reason": self._realtime._unavailable_reason()}
                ok = rt.ready()
                return {"ok": ok, "path": str(rt.model_dir), "status": self.model_status()}
            from runtime.realtime import _load_model
            if not path:
                model = discover_models()
                fname = model.files.get("thinker", "")
                if not fname:
                    return {"ok": False, "reason": "models/realtime/ 缺少 Thinker 权重"}
                path = str(model.root / fname)
            gen = _load_model(getattr(self._realtime, "backend_name", "cpu") if self._realtime else "cpu", path)
            if gen is None:
                return {"ok": False, "reason": "模型加载失败（推理运行时缺失或文件损坏）"}
            return {"ok": True, "path": path, "status": self.model_status()}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def unload_model(self) -> dict:
        """卸载 Realtime 模型（释放内存；下次 judge 重新加载）。"""
        if self._realtime is not None:
            try:
                unloader = getattr(self._realtime, "unload", None)
                if callable(unloader):
                    unloader()
                elif hasattr(self._realtime, "_gen"):
                    self._realtime._gen = None
                    self._realtime._gen_checked = False
            except Exception as e:
                logger.warning(f"[Runtime] 卸载 Realtime 模型失败: {e}")
        return {"ok": True}

    # ---------- Realtime Brain ----------
    @property
    def realtime(self) -> RealtimeBrainProvider:
        if self._realtime is None:
            self._realtime = UnavailableRealtimeBackend()
        return self._realtime

    async def realtime_judge(self, user_text: str, context: Optional[dict] = None):
        return await self.realtime.judge(user_text, context)

    # ---------- 崩溃恢复（轻量） ----------
    def record_crash(self, provider_id: str) -> None:
        now = time.time()
        self._crashes.setdefault(provider_id, []).append(now)
        self._crashes[provider_id] = [t for t in self._crashes[provider_id] if now - t < 600]
        logger.warning(f"[Runtime] {provider_id} 崩溃，10 分钟内 {len(self._crashes[provider_id])} 次")

    def crash_backoff_ok(self, provider_id: str, max_in_window: int = 5) -> bool:
        return len(self._crashes.get(provider_id, [])) < max_in_window

    # ---------- 状态 ----------
    def status(self) -> dict:
        from runtime.perf import perf_monitor
        return {
            "running": self.running,
            "started_at": round(self._started_at, 3),
            "hardware": self.profile.to_dict() if self.profile else {},
            "backends": [c.to_dict() for c in self.backends],
            "benchmark": micro_benchmark.summary(),
            "realtime": {
                "backend": self._realtime_backend.backend if self._realtime_backend else "",
                "provider": self.realtime.to_dict(),
            },
            "models": self.model_status(),
            "providers": self.registry.list(),
            "crashes": {k: len(v) for k, v in self._crashes.items()},
            "perf": perf_monitor.snapshot(),
        }
