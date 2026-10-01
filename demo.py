"""
栖语 (Qiyu) - 完整版 Demo 启动器
=================================
集成：自定义角色 + 意图路由 + 记忆系统 + RAG + 微信 + 流式聊天
运行: python demo.py
访问: http://localhost:8765
=================================
"""

import os
import sys
import json
import re
import uuid
import asyncio
import random
import threading
import time
import base64
import struct
import zlib
import httpx
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

# 项目根目录（兼容 PyInstaller 打包）
if hasattr(sys, "_MEIPASS"):
    _ROOT = Path(sys._MEIPASS)
else:
    _ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

# 加载 .env（若存在），使 LLM/Letta 等配置可在部署时覆盖
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Header, Request, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from loguru import logger

# 导入各模块
from gateway.router import get_router, IntentType
from rag import get_rag_manager
from wechat import get_wechat_bot, ITCHAT_AVAILABLE
from channels import (ClawBotChannel, WechatyChannel,
                      WechatautoChannel, build_placeholder_channels)
from channels import store as channel_store

# M1：行为 / 状态 / LLM 逻辑已拆分到 companion/ 包
from companion import *  # noqa: F401,F403
from runtime import RuntimeManager
from runtime.memory import MemoryProvider
from runtime.toolagent import tool_agent
import companion.active as companion_active
import companion.emotions as companion_emotions
import companion.settings as companion_settings
import companion.llm as companion_llm
from companion.pipeline import pending_queue
"""
栖语 (Qiyu) - 完整版 Demo 启动器
=================================
集成：自定义角色 + 意图路由 + 记忆系统 + RAG + 微信 + 流式聊天
运行: python demo.py
访问: http://localhost:8765
=================================
"""

import os
import sys
import json
import re
import uuid
import asyncio
import random
import threading
import time
import base64
import struct
import zlib
import httpx
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

# 项目根目录（兼容 PyInstaller 打包）
def get_resource_path(relative_path: str = "") -> Path:
    if hasattr(sys, '_MEIPASS'):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent
    if relative_path:
        return base / relative_path
    return base

PROJECT_DIR = get_resource_path()
STATIC_DIR = get_resource_path("gateway/static")
sys.path.insert(0, str(PROJECT_DIR))

# 加载 .env（若存在），使 LLM/Letta 等配置可在部署时覆盖
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Header, Request, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from loguru import logger

# 导入各模块
from characters import get_character_manager
from gateway.router import get_router, IntentType
from memory import get_memory_manager, get_knowledge_base
from rag import get_rag_manager
from wechat import get_wechat_bot, ITCHAT_AVAILABLE
from channels import (get_channel_registry, ClawBotChannel, WechatyChannel,
                      WechatautoChannel, build_placeholder_channels)
from channels import store as channel_store
from channels.wechat_emoji import build_emoji_prompt_block
from letta_backend import get_letta_backend
from tools import web as web_tools

# Demo 配置（桌面壳可通过环境变量覆盖 host/port）
DEMO_PORT = int(os.getenv("QIYU_PORT", "8765"))
DEMO_HOST = os.getenv("QIYU_HOST", "0.0.0.0")
LLM_URL = os.getenv("LLM_BASE_URL", "http://192.168.2.6:8081/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.6-35b-a3b-uncensored-heretic")
# 路由模型（可选）：专门用于生成角色信息卡，配置后主对话模型只会看到信息卡、看不到人设标签
LLM_ROUTE_URL = os.getenv("LLM_ROUTE_URL", "")
LLM_ROUTE_MODEL = os.getenv("LLM_ROUTE_MODEL", "")

STATIC_DIR = PROJECT_DIR / "gateway" / "static"

# 人设平级约束（写死，优先级最高，置于系统提示词最前）


# ============ 数据模型 ============


# ============ 微信消息生成（JSON 多消息协议） ============


# 场景枚举：描述当前聊天状态（生成条件），不直接规定台词

# 场景行为范围（不是固定台词）：只描述氛围、允许/禁止、触发与退出条件，台词由模型自由生成

# 场景关键词触发（命中即进入对应场景候选）


# ============ 真人感代码级护栏（行为归代码管，不靠提示词硬撑） ============


# ============ 真人聊天行为建模（P0/P1/P2：行为归代码管，台词归模型） ============
# 目标不是"说得像真人"，而是"这一刻的决定像真人"：感知 → 状态判断 → 是否自然 → 反应 → 文字。

# 1) 回忆探测：对方在要求回忆（"你还记得X吗/我明天去哪来着"）
# 表演回忆：假装在记忆里翻找（禁止在"刚说过"的场景出现）
# 客服腔/心理咨询师腔（短模板句才丢，长句不误伤）
# 对方只发了个字/表情：不需要展开
# 对方明确拒绝：别再教育
# 换话题连接词：带这些词 = 自然带过，不用觉得意外
# 开新话题的口气词（长陈述 + 这些词 = 大概率是新话题，不解除"未完成话题"）
# 故事被叫停 / 自然结束


# 回调式指回：对方说"我刚才说的X呢/刚才那个X呢/上次说的X呢"→ 靠共同双字词就能命中


# ============ 关系系统（好感度 + 友情值，动态变化并持久化） ============


# ============ 主动消息：追问 + 每日展开 ============


# ============ 外部会话注册表（微信/预留通道的用户 → 前端「会话选择器」） ============


# ============ LLM 全局并发限制（并行请求数：auto/1/2/3/4/0=不限制） ============


# 提示词注入检测（把用户发来的"系统指令"当成恶搞，不执行）


# ============ 设置管理 ============


# ============ LLM 主流 API 预设（设置页「快速接入」，填 Key 即可用） ============


# ============ 会话状态机 / 场景系统 / 主动消息 Context Gate ============
# 每个(用户,角色)独立维护，互不串上下文；只影响后端行为，不改前端显示


# ============ 多维情绪系统（头脑特工队式：开心/害怕/悲伤/焦虑/兴奋） ============


# ============ LLM 客户端 ============

# ============ FastAPI 应用 ============

app = FastAPI(
    title="栖语",
    description="AI 聊天陪伴机器人 - 角色 + 记忆 + RAG + 微信",
    version="2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 全局状态
runtime_manager = RuntimeManager()
letta_backend = get_letta_backend()
char_mgr = get_character_manager()
mem_mgr = get_memory_manager()
kb = get_knowledge_base()
rag_mgr = get_rag_manager()
wechat_bot = get_wechat_bot()
channel_registry = get_channel_registry()
main_loop: asyncio.AbstractEventLoop | None = None
_background_task: asyncio.Task | None = None


class UserChatGate:
    """同用户聊天串行门 + §15 服务端合批
    回复生成期间到达的新消息：请求立即返回 queued（前端正常显示已发出），
    消息进入 pending 队列，等上一条回复结束（流式/非流式）后作为批量上下文一次 prefill，
    不逐条单独推理。asyncio.Lock 公平排队（FIFO），不同用户互不影响。
    busy 标记保证『同一用户同一时刻最多一个生成』，剩余消息全部进队列。
    """

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._busy: set[str] = set()

    def lock_for(self, user_id: str) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def is_busy(self, user_id: str) -> bool:
        return user_id in self._busy

    def mark_busy(self, user_id: str) -> None:
        self._busy.add(user_id)

    def clear_busy(self, user_id: str) -> None:
        self._busy.discard(user_id)


chat_gate = UserChatGate()


def _build_trailing_user_batch(messages: list) -> list | None:
    """§15 多消息流水线：取末尾连续的多条用户消息（中间无助手回复）作为结构化批次。"""
    batch = []
    for m in reversed(messages or []):
        if m.get("role") != "user":
            break
        batch.append({"text": _msg_text(m.get("content", "")), "timestamp": time.time()})
    batch.reverse()
    return batch if len(batch) >= 2 else None


async def _handle_pending_batch(user_id: str, char_id: str, temperature: float):
    """§15 服务端合批：把生成期间排队的连续用户消息合并为一次 prefill，
    不逐条单独推理；结果经事件通道（/v1/events）真实下发。"""
    pending = pending_queue.drain(user_id)
    if not pending:
        return
    texts = []
    for p in pending:
        t = (p.get("text") or "").strip()
        if p.get("images"):
            t = ("[图片] " + t).strip()
        if t:
            texts.append(t)
    if not texts:
        return
    combined = "；".join(texts)
    logger.info(f"[合批] {user_id} 生成期间收到 {len(texts)} 条消息，合并为一次 prefill: {combined[:80]!r}")
    _cancel_nudge(user_id, char_id)
    _update_conv_state(user_id, char_id, "user_message", None, combined)
    _st = user_states.setdefault(user_id, {"character_id": char_id, "temperature": 0.7, "history": []})
    _st["character_id"] = char_id
    _st["last_user_at"] = time.time()
    _st.pop("unanswered_pending", None)
    for t in texts:
        mem_mgr.add_message(user_id, "user", t, char_id)
    try:
        from runtime.db import unified_store
        from runtime.logging_setup import logger_conv
        for t in texts:
            unified_store.record_message(user_id, char_id, "user", t)
            logger_conv.info(f"[{user_id}] {char_id} 用户: {t[:200]}")
    except Exception:
        pass
    try:
        history = mem_mgr.get_recent_history(user_id, limit=40, char_id=char_id)
    except Exception:
        history = []
    llm_messages = []
    for h in history:
        role = "user" if h.get("role") == "user" else "assistant"
        content = h.get("content") or ""
        if isinstance(content, list):
            parts = []
            for it in content:
                if isinstance(it, dict):
                    parts.append(it.get("text") or "[图片]")
            content = " ".join(parts)
        llm_messages.append({"role": role, "content": content})
    batch = [{"text": t, "timestamp": (p.get("timestamp") or time.time())}
             for t, p in zip(texts, pending)]
    try:
        reply_text, reply_pieces = await llm_client.chat(
            char_id, llm_messages, temperature,
            user_id=user_id, use_memory=True, use_rag=True,
            pending_messages=batch if len(batch) >= 2 else None)
    except Exception as e:
        logger.error(f"[合批] 批量回复生成失败: {e}")
        return
    if not reply_text:
        return
    try:
        # chat() 内部 _finalize_chat_reply 已更新会话状态/关系/情绪；这里只做记忆落账 + 事件下发
        msgs = []
        for _p in (reply_pieces or []):
            _m = {"text": _p.get("text", ""), "type": _p.get("type", "statement"), "delay": _p.get("delay", 0)}
            if _p.get("image_url"):
                _m["image_url"] = _p.get("image_url")
            msgs.append(_m)
        if msgs:
            await _send_active_messages(user_id, msgs, "ordinary_chat", reason="batch", char_id=char_id)
        try:
            from runtime.db import unified_store
            unified_store.record_message(user_id, char_id, "assistant", reply_text, pieces=reply_pieces)
        except Exception:
            pass
        if main_loop and not main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(_run_memory_pipeline(user_id, char_id), main_loop)
        else:
            asyncio.create_task(_run_memory_pipeline(user_id, char_id))
    except Exception as e:
        logger.error(f"[合批] 批量回复后处理失败: {e}")
    return batch if len(batch) >= 2 else None


@app.on_event("startup")
async def startup():
    # §48 分层日志：runtime/conversation/tool/memory/performance/error
    try:
        from runtime.logging_setup import configure_logging
        configure_logging()
    except Exception as e:
        logger.warning(f"[日志] 分层日志初始化失败: {e}")
    runtime_manager.start()
    global llm_client, LLM_URL, LLM_MODEL, LLM_ROUTE_URL, LLM_ROUTE_MODEL
    # 自动加载上次保存的 LLM 设置（主模型/路由模型/API Key），重启后不用重新配置
    try:
        _rt = load_runtime_settings()
        if _rt.get("llm_url"):
            LLM_URL = str(_rt["llm_url"]).rstrip("/")
        if _rt.get("llm_model"):
            LLM_MODEL = str(_rt["llm_model"])
        if _rt.get("llm_route_url") is not None:
            LLM_ROUTE_URL = str(_rt["llm_route_url"]).rstrip("/")
        if _rt.get("llm_route_model") is not None:
            LLM_ROUTE_MODEL = str(_rt["llm_route_model"])
        logger.info(f"[设置] 已自动加载上次保存的 LLM 配置: 主模型={LLM_MODEL} 路由模型={LLM_ROUTE_MODEL or '（未配置）'}")
        logger.info(f"[设置] 运行时设置已完整加载（{len(_rt)} 项，含默认值合并）")
    except Exception as e:
        logger.warning(f"[设置] 加载 LLM 配置失败: {e}")
    # 同步 LLM 配置 / 客户端到 companion 模块（行为函数读取各自模块内全局）
    companion_settings.LLM_URL = LLM_URL
    companion_settings.LLM_MODEL = LLM_MODEL
    companion_settings.LLM_ROUTE_URL = LLM_ROUTE_URL
    companion_settings.LLM_ROUTE_MODEL = LLM_ROUTE_MODEL
    companion_llm.LLM_URL = LLM_URL
    companion_llm.LLM_MODEL = LLM_MODEL
    companion_llm.LLM_ROUTE_URL = LLM_ROUTE_URL
    companion_llm.LLM_ROUTE_MODEL = LLM_ROUTE_MODEL
    llm_client = LLMClient()
    companion_active.llm_client = llm_client
    companion_emotions.llm_client = llm_client
    # M3：Main Brain 统一经 Provider 接口注册进 Runtime，业务层/前端可经 /v1/runtime/providers 查询
    runtime_manager.registry.register(llm_client, fallback_ids=[])
    # M4：Tool Agent / Memory 同样注册为 Runtime Provider（业务层经 Provider 接口访问，不直接依赖具体实现）
    mem_provider = MemoryProvider(mem_mgr)
    runtime_manager.registry.register(mem_provider, fallback_ids=[])
    runtime_manager.registry.register(tool_agent, fallback_ids=[])
    # P1：Vision / STT / TTS / Embedding / Avatar / Platform Provider 统一注册
    from runtime.vision import MainBrainVisionProvider
    from runtime.stt import stt_provider
    from runtime.tts import tts_provider
    from runtime.embedding_provider import embedding_provider
    from runtime.avatar import Live2DAvatarProvider, VRCAvatarProvider
    from runtime.platform import platform_registry
    from runtime.concurrency import concurrency_limiter
    _vision = MainBrainVisionProvider(llm_client)
    runtime_manager.registry.register(_vision, fallback_ids=[])
    runtime_manager.registry.register(stt_provider, fallback_ids=[])
    runtime_manager.registry.register(tts_provider, fallback_ids=[])
    runtime_manager.registry.register(embedding_provider, fallback_ids=[])
    mem_provider.attach_embedding(embedding_provider)
    runtime_manager.registry.register(Live2DAvatarProvider(), fallback_ids=[])
    runtime_manager.registry.register(VRCAvatarProvider(), fallback_ids=[])
    for _pp in platform_registry.list():
        _prov = platform_registry.get(_pp.get("id"))
        if _prov is not None:
            runtime_manager.registry.register(_prov, fallback_ids=[])
    concurrency_limiter.refresh()
    # §13 微基准：启动后异步跑一次（有真实 backend 就实测，否则硬件启发式）
    asyncio.create_task(runtime_manager.bench())
    await letta_backend.check()
    
    # 预热：把所有角色同步到 Letta，保证记忆检索可命中
    for char in char_mgr.all_characters():
        await _sync_character_to_letta(char)
    
    # 启动 RAG
    rag_mgr.setup(kb)
    rag_mgr.start()
    
    # 设置微信消息处理（ClawBot / itchat 共用；channel 透传绑定角色，images 透传图片给视觉链路）
    async def _process_wechat_msg(user_id: str, content: str, channel=None,
                                  images: list | None = None) -> tuple:
        # 1) 角色透传：优先用通道配置的「绑定角色」，其次默认角色
        char_id = ""
        if channel is not None:
            try:
                char_id = str((getattr(channel, "config", {}) or {}).get("character_id") or "").strip()
            except Exception:
                char_id = ""
        if not char_id:
            try:
                _ch = channel_registry.get("wechaty")
                char_id = str((getattr(_ch, "config", {}) or {}).get("character_id") or "").strip()
            except Exception:
                char_id = ""
        if not char_id or not char_mgr.get_character(char_id):
            default_char = char_mgr.get_default()
            char_id = default_char.id if default_char else "default"
        _register_session(user_id, channel.id if channel else "wechat")

        images = images or []
        st = user_states.setdefault(user_id, {"character_id": char_id, "temperature": 0.7, "history": []})
        st["character_id"] = char_id
        st["last_user_at"] = time.time()
        st.pop("unanswered_pending", None)
        _cancel_nudge(user_id, char_id)
        _update_conv_state(user_id, char_id, "user_message", None, content)

        # 2) 保存用户消息到记忆（图片透传：内容带 [图片] 标记）
        mem_content = content
        if images:
            mem_content = ("[图片] " + content).strip()
        # §15 服务端合批：上一条回复生成中 → 本条进 pending 队列，统一合批（不逐条推理）
        if chat_gate.is_busy(user_id):
            pending_queue.enqueue(user_id, {"text": mem_content, "images": images, "timestamp": time.time()})
            _push_event(user_id, {
                "type": "chat_user",
                "user_id": user_id,
                "char_id": char_id,
                "text": mem_content,
                "images": images,
                "ts": time.time(),
            })
            if user_id != "web_user":
                _push_event("web_user", dict(user_id=user_id, char_id=char_id,
                                             type="chat_user", text=mem_content,
                                             images=images, ts=time.time()))
            logger.info(f"[合批] {user_id} 上一条回复生成中，微信消息进 pending 队列")
            return "", []
        chat_gate.mark_busy(user_id)
        mem_mgr.add_message(user_id, "user", mem_content, char_id, images=images)
        # 微信消息即时同步到 App 前端（不等模型生成完，先显示用户这条）
        _push_event(user_id, {
            "type": "chat_user",
            "user_id": user_id,
            "char_id": char_id,
            "text": mem_content,
            "images": images,
            "ts": time.time(),
        })
        if user_id != "web_user":
            _push_event("web_user", dict(user_id=user_id, char_id=char_id,
                                         type="chat_user", text=mem_content,
                                         images=images, ts=time.time()))

        # 3) 构造消息（图片 → OpenAI 视觉输入格式，交给远端 llama.cpp 多模态）
        if images:
            parts = [{"type": "text", "text": content}]
            for img in images[:4]:
                url = img
                if isinstance(url, str) and not url.startswith("data:") and not url.startswith("http"):
                    url = f"data:image/png;base64,{url}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            messages = [{"role": "user", "content": parts}]
        else:
            messages = [{"role": "user", "content": content}]

        # 4) 生成回复（前后端同步「对方正在输入」：微信端由通道 sendtyping 驱动，
        #    这里同时给 App 前端推 typing 事件，让两端的输入指示器一起亮）
        _push_event(user_id, {"type": "typing", "user_id": user_id, "char_id": char_id})
        try:
            reply, pieces = await llm_client.chat(char_id, messages, user_id=user_id, channel=channel)
        except Exception:
            chat_gate.clear_busy(user_id)
            raise
        finally:
            _push_event(user_id, {"type": "typing_stop", "user_id": user_id, "char_id": char_id})

        # 5) 保存助手回复到记忆
        mem_mgr.add_message(user_id, "assistant", reply, char_id, pieces=pieces)
        # 触发记忆流水线（切回主事件循环，避免微信线程的临时 loop 关闭）
        if main_loop and not main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(_run_memory_pipeline(user_id, char_id), main_loop)
        # 6) 前端同步：微信收到的消息与回复实时推给 App 前端（会话选择器正在看该会话时自动刷新；
        #    同时广播一份给 web_user，让前端无论在看哪个会话都能刷新会话列表并提示）
        ev = {
            "type": "chat_sync",
            "user_id": user_id,
            "char_id": char_id,
            "user_text": mem_content,
            "user_images": images,
            "assistant_text": reply,
            "pieces": pieces,
            "ts": time.time(),
            "notify": True,
        }
        _push_event(user_id, ev)
        if user_id != "web_user":
            _push_event("web_user", dict(ev, notify=True))
        chat_gate.clear_busy(user_id)
        while pending_queue.pending(user_id):
            try:
                await _handle_pending_batch(user_id, char_id, float(st.get("temperature") or 0.7))
            except Exception as e:
                logger.error(f"[合批] 微信路径合批失败: {e}")
                break
        return reply, pieces

    async def handle_wechat_msg(user_id: str, content: str, channel=None,
                                images: list | None = None) -> tuple:
        """微信通道线程入口：投递到主事件循环执行。
        原因：llm_limiter / chat_gate 等 asyncio 资源绑定主 loop，通道线程的临时 loop 里直接
        await 会抛 'bound to a different event loop'，导致微信消息处理静默失败（只回/不回）。
        统一在主 loop 里跑后，ClawBot/itchat 与网页聊天完全同一条链路。"""
        if (main_loop is not None and main_loop.is_running()
                and threading.current_thread() is not threading.main_thread()):
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _process_wechat_msg(user_id, content, channel, images), main_loop)
                return await asyncio.wrap_future(fut)
            except Exception as e:
                logger.error(f"[微信] 投递到主循环执行失败: {e}")
                return "", []
        return await _process_wechat_msg(user_id, content, channel, images)
    
    wechat_bot.set_message_handler(handle_wechat_msg)

    # 外部通讯通道注册：ClawBot（官方扫码，默认置顶）+ Wechatauto（本机接管，读取被动/发送低干扰）
    # + Wechaty（Node 网关单账号接管，备用）+ 预留通道
    channel_registry.register(ClawBotChannel("main"))
    channel_registry.register(WechatautoChannel())
    wechaty_ch = WechatyChannel(callback_url=f"http://127.0.0.1:{DEMO_PORT}/v1/channels/wechaty/webhook")
    channel_registry.register(wechaty_ch)
    for _ph in build_placeholder_channels():
        channel_registry.register(_ph)
    channel_registry.set_message_handler(handle_wechat_msg)

    global main_loop, _background_task
    main_loop = asyncio.get_running_loop()
    # 后台调度：建议追问 + 每日不定时主动消息
    _background_task = asyncio.create_task(_background_loop())
    
    logger.info("=" * 60)
    logger.info("栖语 完整版启动")
    logger.info("=" * 60)
    logger.info(f"访问地址: http://{DEMO_HOST}:{DEMO_PORT}")
    logger.info(f"LLM 服务: {'已连接' if llm_client.available else '未连接 - 请检查设置'}")
    logger.info(f"微信模块: Wechaty {'可用' if wechaty_ch.available else '未就绪（需 Node + wechaty 依赖）'} / ClawBot 可用")
    logger.info(f"RAG 监控: {kb.storage_dir}")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown():
    runtime_manager.stop()
    if _background_task:
        _background_task.cancel()


# ============ 页面路由 ============

@app.get("/")
async def root():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "栖语", "status": "running"}


@app.get("/chat")
async def chat_page():
    return RedirectResponse(url="/")


# ============ 角色 API ============

@app.get("/v1/characters")
async def list_characters():
    return {"data": char_mgr.list_characters()}


@app.get("/v1/characters/{char_id}")
async def get_character(char_id: str):
    char = char_mgr.get_character(char_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    return {
        "id": char.id, "name": char.name, "tagline": char.tagline,
        "description": char.description, "temperature": char.temperature,
        "avatar_color": char.avatar_color, "avatar": char.avatar, "keywords": char.keywords or [],
        "speech_style": char.speech_style, "resume": char.resume or {},
        "persona_params": char.persona_params or {},
    }


@app.post("/v1/characters")
async def create_character(data: CreateCharacterRequest):
    char_data = data.dict(exclude_none=True)
    if data.user_profile:
        try:
            mem_mgr.save_user_profile("web_user", data.user_profile)
        except Exception as e:
            logger.warning(f"保存用户人设失败: {e}")
    char = char_mgr.create_character(char_data)
    asyncio.create_task(_sync_character_to_letta(char))
    return {"success": True, "character": {"id": char.id, "name": char.name, "tagline": char.tagline, "description": char.description, "temperature": char.temperature, "avatar_color": char.avatar_color, "avatar": char.avatar, "resume": char.resume or {}, "persona_params": char.persona_params or {}}}


@app.delete("/v1/characters/{char_id}")
async def delete_character(char_id: str):
    if char_mgr.delete_character(char_id):
        return {"success": True, "message": "角色已删除"}
    raise HTTPException(404, "角色不存在")


@app.post("/v1/characters/select")
async def select_character(data: dict):
    user_id = data.get("user_id", "demo_user")
    char_id = data.get("character_id", "")
    
    char = char_mgr.get_character(char_id)
    if not char:
        raise HTTPException(404, "角色不存在，请先创建角色")
    
    user_states[user_id] = {
        "character_id": char_id,
        "temperature": char.temperature,
        "history": [],
    }
    relation = _init_relation(user_id, char_id)
    asyncio.create_task(_sync_character_to_letta(char))
    
    return {
        "success": True,
        "character": {"id": char.id, "name": char.name, "tagline": char.tagline},
        "relation": relation,
        "message": f"已切换为「{char.name}」— {char.tagline}",
    }

@app.post("/v1/characters/generate")
async def generate_character(data: dict):
    """根据标签生成结构化角色简历（走路由模型，主对话模型只看到简历）"""
    tags = data.get("tags", [])
    name = data.get("name", "")
    description = data.get("description", "")
    mbti = data.get("mbti", "")
    persona_params = data.get("persona_params") or {}
    user_gender = data.get("user_gender", "")
    relationship_role = data.get("relationship_role", "")
    try:
        card = await llm_client.generate_character_card(tags, name, description, mbti, persona_params,
                                                        user_gender, relationship_role)
        resume = parse_resume(card)
        params = parse_persona_params(card)
        return {"success": True, "card": card, "resume": resume, "params": params}
    except Exception as e:
        logger.error(f"生成角色简历失败: {e}")
        raise HTTPException(500, f"生成失败: {str(e)}")


@app.post("/v1/characters/generate_stream")
async def generate_character_stream(data: dict):
    """流式生成角色简历（SSE）：前端展开面板实时显示原始返回（含思考）+ JSON 增量"""
    tags = data.get("tags", [])
    name = data.get("name", "")
    description = data.get("description", "")
    mbti = data.get("mbti", "")
    persona_params = data.get("persona_params") or {}
    user_gender = data.get("user_gender", "")
    relationship_role = data.get("relationship_role", "")
    async def event_gen():
        full = []
        try:
            async for ev in llm_client.generate_character_card_stream(tags, name, description, mbti, persona_params,
                                                                      user_gender, relationship_role):
                # 只把正片内容计入简历，思考过程（reasoning）仅用于前端实时展开面板
                if ev["type"] == "content":
                    full.append(ev["text"])
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            text = "".join(full)
            resume = parse_resume(text)
            params = parse_persona_params(text)
            yield f"data: {json.dumps({'type': 'done', 'card': text, 'resume': resume, 'params': params}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/v1/characters/{char_id}/index_resume")
async def index_character_resume(char_id: str):
    """把角色简历写入 RAG 知识库，供检索与决策调度使用"""
    char = char_mgr.get_character(char_id)
    if not char:
        raise HTTPException(404, "角色不存在")
    if not char.resume:
        raise HTTPException(400, "该角色还没有结构化简历，请先在上方生成并保存")
    try:
        text = resume_to_text(char.resume)
        kb_dir = kb.storage_dir
        kb_dir.mkdir(parents=True, exist_ok=True)
        fname = kb_dir / f"resume_{char.id}.md"
        fname.write_text(f"# 角色简历：{char.name}\n\n{text}\n", encoding="utf-8")
        count = kb.index_document(fname)
        return {"success": True, "chunks": count, "file": fname.name, "resume": char.resume}
    except Exception as e:
        logger.error(f"角色简历入库失败: {e}")
        raise HTTPException(500, f"入库失败: {str(e)}")


# ============ 头像 API ============

@app.get("/v1/avatars")
async def list_avatars():
    names: list[str] = []
    for d in (DEFAULT_AVATAR_DIR, AVATAR_UPLOAD_DIR):
        if d.exists():
            names += [f.name for f in d.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif")]
    return {"data": sorted(set(names))}


@app.get("/avatars/{name}")
async def get_avatar(name: str):
    if "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(400, "非法文件名")
    for d in (AVATAR_UPLOAD_DIR, DEFAULT_AVATAR_DIR):
        p = d / name
        if p.exists() and p.is_file():
            return FileResponse(str(p))
    raise HTTPException(404, "头像不存在")


@app.post("/v1/avatars/upload")
async def upload_avatar(file: UploadFile = File(...)):
    AVATAR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        raise HTTPException(400, "仅支持 png/jpg/webp/gif 图片")
    name = f"upload_{uuid.uuid4().hex[:8]}{ext}"
    with open(AVATAR_UPLOAD_DIR / name, "wb") as f:
        f.write(await file.read())
    logger.success(f"头像已上传: {name}")
    return {"success": True, "filename": name}


# ============ 聊天 API ============

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    user_id = request.user

    state = user_states.get(user_id)
    if not state or "character_id" not in state or "temperature" not in state:
        default_char = char_mgr.get_default()
        if default_char:
            state = {"character_id": default_char.id, "temperature": default_char.temperature, "history": []}
        else:
            return JSONResponse(status_code=400, content={"error": "no_character", "message": "还没有创建任何角色，请先到角色工坊创建一个角色。"})
        user_states[user_id] = state

    char_id = state["character_id"]
    temperature = request.temperature if request.temperature is not None else state["temperature"]

    # 只取消息参数（用户消息等轮到本消息时再写入记忆历史，保证 user/assistant 顺序交错正确）
    # 多消息流水线：用户连续发多条时，前端会合并成一次请求，这里把每条都写入历史、一起喂给模型
    messages = [m.dict() for m in request.messages]
    user_msgs = [m for m in messages if m.get("role") == "user"] or [{"role": "user", "content": ""}]
    user_texts = [_msg_text(m.get("content", "")) for m in user_msgs]
    user_content = user_texts[-1]
    llm_messages = messages
    mem_contents = list(user_texts)
    images = request.images or []
    if images:
        # 图片通道：把 dataURL/base64 转成 OpenAI 视觉输入格式，追加到最后一轮用户消息
        parts = [{"type": "text", "text": user_content}]
        for img in images[:4]:
            url = img
            if isinstance(url, str) and not url.startswith("data:") and not url.startswith("http"):
                url = f"data:image/png;base64,{url}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
        llm_messages = messages[:-1] + [{"role": "user", "content": parts}]
        mem_contents[-1] = ("[图片] " + user_content).strip()
    response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if request.stream:
        async def generate():
            lock = chat_gate.lock_for(user_id)
            if lock.locked():
                # §15 服务端合批：第一条立即生成，后续连发消息进 pending 队列（不逐条单独推理）
                for _mc in mem_contents:
                    pending_queue.enqueue(user_id, {"text": _mc, "images": request.images or [], "timestamp": time.time()})
                logger.info(f"[合批] {user_id} 上一条回复生成中，{len(mem_contents)} 条消息进入 pending 队列")
                yield f"data: {json.dumps({'type': 'queued', 'id': response_id, 'message': '消息已排队，稍后统一回复'})}\n\n"
                yield "data: [DONE]\n\n"
                return
            async with lock:
                chat_gate.mark_busy(user_id)
                async def _flush_batch():
                    chat_gate.clear_busy(user_id)
                    while pending_queue.pending(user_id):
                        try:
                            await _handle_pending_batch(user_id, char_id, temperature)
                        except Exception as e:
                            logger.error(f"[合批] 流式路径合批失败: {e}")
                            break
                # 用户来消息了：取消追问计划、刷新最后活跃时间、清掉未回复的小情绪
                if user_id in user_states:
                    user_states[user_id].pop("pending_nudge", None)
                    user_states[user_id]["last_user_at"] = time.time()
                    user_states[user_id].pop("unanswered_pending", None)
                _cancel_nudge(user_id, char_id)
                _update_conv_state(user_id, char_id, "user_message", None, user_content)
                # 轮到本消息：写入用户消息，保证历史顺序 = 用户/助手交替
                for _mc in mem_contents:
                    mem_mgr.add_message(user_id, "user", _mc, char_id)
                try:
                    from runtime.db import unified_store
                    for _mc in mem_contents:
                        unified_store.record_message(user_id, char_id, "user", _mc)
                except Exception:
                    pass
                try:
                    from runtime.logging_setup import logger_conv
                    for _mc in mem_contents:
                        logger_conv.info(f"[{user_id}] {char_id} 用户: {_mc[:200]}")
                except Exception:
                    pass
                runtime = load_runtime_settings()
                show_thinking = bool(runtime.get("show_thinking"))
                full_raw = []
                buf = ""
                safe_pushed = 0
                # 流式路径也做"刚说过别表演回忆"过滤：命中最近事实就丢纯犹豫短气泡
                _rfh = None
                if user_id and char_id:
                    try:
                        _probe = _recall_probe_target(user_content or "")
                        if _probe:
                            _rfh = _find_recent_fact(user_id, char_id, _probe)
                    except Exception:
                        _rfh = None
                try:
                    _pending = _build_trailing_user_batch(llm_messages)
                    async for reasoning, content in llm_client.chat_stream(
                        char_id, llm_messages, temperature,
                        user_id=user_id,
                        use_memory=request.use_memory,
                        use_rag=request.use_rag,
                        pending_messages=_pending,
                    ):
                        if reasoning and show_thinking:
                            data = {
                                "id": response_id,
                                "object": "chat.completion.chunk",
                                "created": int(datetime.now().timestamp()),
                                "model": char_id,
                                "choices": [{"index": 0, "delta": {"reasoning_content": reasoning}, "finish_reason": None}],
                            }
                            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                        if content:
                            full_raw.append(content)
                            buf += content
                            if len(buf) <= 20000:
                                try:
                                    _st, stream_msgs = _try_stream_parse(buf)
                                    if stream_msgs:
                                        _sp = _needs_search_guard(user_id, user_content)
                                        _sr = _is_search_request(user_content)
                                        _lg = _is_longform_request(user_content, llm_messages)
                                        _ae = (_st or "") in ("兴奋", "吐槽", "吵架")
                                        safe_msgs = []
                                        for _m in stream_msgs:
                                            _t = _sanitize_msg_text(_m.get("text", ""), _sp, _ae, _sr)
                                            if _t is not None:
                                                if _rfh and _RECALL_PERFORM_RE.search(_t) and len(_t) <= 14:
                                                    continue
                                                _m2 = dict(_m)
                                                _m2["text"] = _t
                                                safe_msgs.append(_m2)
                                        _cap = _msg_cap(_st or "闲聊", _lg)
                                        if len(safe_msgs) > _cap:
                                            safe_msgs = safe_msgs[:_cap]
                                        if len(safe_msgs) > safe_pushed:
                                            new_msgs = safe_msgs[safe_pushed:]
                                            safe_pushed = len(safe_msgs)
                                            yield f"data: {json.dumps({'type': 'assistant_messages', 'id': response_id, 'partial': True, 'messages': new_msgs}, ensure_ascii=False)}\n\n"
                                except Exception:
                                    pass
                except HTTPException as e:
                    yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'error'}], 'error': {'message': str(e.detail)}})}\n\n"
                    await _flush_batch()
                    return
                except Exception as e:
                    logger.error(f"流式聊天失败: {e}")
                    yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'error'}], 'error': {'message': f'生成失败: {e}'}})}\n\n"
                    await _flush_batch()
                    return

                # 解析多消息 JSON，更新会话状态 + 关系变化 + 追问计划
                parsed = parse_chat_messages("".join(full_raw))
                _postprocess_reply_messages(user_id, char_id, parsed, user_content, llm_messages)
                # 长文输出兜底：模型只发了个开场白（<=3条且无实质内容）→ 自动重拉一次完整内容
                _lg_req = _is_longform_request(user_content, llm_messages)
                if _lg_req and len(parsed["messages"]) <= 3:
                    _stub = "".join(m["text"] for m in parsed["messages"])
                    if len(_stub) <= 40 and not re.search(r"(从前|有一|很久|那天|后来|最后|结尾|讲完|完蛋|醒来|第二天)", _stub):
                        try:
                            _retry_sys = await llm_client._build_chat_system_msg(char_id, llm_messages, user_id, request.use_memory, request.use_rag)
                            _retry_raw = await llm_client._call_real_llm(
                                _retry_sys,
                                [{"role": "user", "content": "[系统提示] 上一轮你只发了个开头（" + _stub[:30] + "）就停了。用户明确要完整的长内容，这一轮必须把完整内容一次性发完：按自然段落拆成多条消息连续发，从开头一直讲到自然收尾，绝对不要再只发开场白。"}],
                                0.6,
                            )
                            _parsed2 = parse_chat_messages(_retry_raw)
                            _postprocess_reply_messages(user_id, char_id, _parsed2, user_content, llm_messages)
                            if len(_parsed2["messages"]) > len(parsed["messages"]):
                                parsed = _parsed2
                        except Exception:
                            pass
                # 长文兜底2：内容完整（>120字）但模型挤成 1~3 条大消息 → 按句拆成多条短气泡（真人节奏）
                if _lg_req and len(parsed["messages"]) <= 3:
                    try:
                        _lg_total = len("".join(m["text"] for m in parsed["messages"]))
                        if _lg_total > 120:
                            _split = _split_longform_bubbles(parsed)
                            if len(_split["messages"]) > len(parsed["messages"]):
                                parsed = _split
                    except Exception:
                        pass
                # 空回复兜底：思考模式把预算耗光 / 正文没解析出 JSON → 关掉思考重拉一次，保证前端不出现"……"
                if not parsed["messages"]:
                    try:
                        _retry_sys = await llm_client._build_chat_system_msg(char_id, llm_messages, user_id, request.use_memory, request.use_rag)
                        _retry_raw = await llm_client._call_real_llm(
                            _retry_sys,
                            [{"role": "user", "content": user_content or "（继续刚才的对话，正常回复我）"}],
                            0.7, no_thinking=True,
                        )
                        _parsed3 = parse_chat_messages(_retry_raw)
                        _postprocess_reply_messages(user_id, char_id, _parsed3, user_content, llm_messages)
                        if _parsed3["messages"]:
                            parsed = _parsed3
                    except Exception as _retry_e:
                        logger.warning(f"[Chat] 空回复兜底重试失败: {_retry_e}")
                # 重拉仍空：给一句自然的兜底，绝不回"……"，也别回"我这边没接住"那种机器话
                if not parsed["messages"]:
                    _u = (user_content or "").strip()
                    _fb = ("在呢" if _u in ("在吗", "在不在", "在么", "人呢", "喂", "哈喽", "hi", "hello", "嗨")
                           else "刚卡了一下 你再说一遍" if (_u.endswith(("？", "?")) or _u.startswith(("为什么", "怎么", "啥", "什么")))
                           else "刚走神了一下 你说")
                    parsed["messages"] = [{"text": _fb, "type": "statement", "delay": 0}]
                # 增量没推完的（JSON 在末尾才闭合）在这里补齐
                if len(parsed["messages"]) > safe_pushed:
                    new_msgs = parsed["messages"][safe_pushed:]
                    safe_pushed = len(parsed["messages"])
                    yield f"data: {json.dumps({'type': 'assistant_messages', 'id': response_id, 'partial': True, 'messages': new_msgs}, ensure_ascii=False)}\n\n"
                relation = _apply_chat_side_effects(user_id, char_id, parsed, user_content)

                # 先回复，后整理：保存纯文本消息（带分条 pieces，历史可还原逐条）+ 异步压缩记忆
                reply_text = "".join(m["text"] for m in parsed["messages"])
                if reply_text:
                    _pieces = []
                    for _m in parsed["messages"]:
                        _p = {"text": _m["text"], "type": _m["type"], "delay": _m["delay"]}
                        if _m.get("image_url"):
                            _p["image_url"] = _m["image_url"]
                        _pieces.append(_p)
                    mem_mgr.add_message(
                        user_id, "assistant", reply_text, char_id,
                        pieces=_pieces,
                    )
                    try:
                        from runtime.db import unified_store
                        unified_store.record_message(user_id, char_id, "assistant", reply_text, pieces=_pieces)
                        unified_store.record_emotion(user_id, char_id, _emotion_state(user_id, char_id) or {})
                        relation = relation or {}
                        unified_store.record_relationship(
                            user_id, char_id,
                            int(relation.get("affinity") or 0), int(relation.get("friendship") or 0),
                            str(relation.get("tier") or ""), str(relation.get("relationship") or ""))
                    except Exception:
                        pass
                    try:
                        from runtime.logging_setup import logger_conv
                        logger_conv.info(f"[{user_id}] {char_id} 助手: {reply_text[:200]}")
                    except Exception:
                        pass
                    asyncio.create_task(_run_memory_pipeline(user_id, char_id))
                else:
                    mem_mgr.add_message(user_id, "assistant", "(无回复)", char_id)

                # 最终事件：带完整状态/关系/记忆，前端只用来更新状态（消息已在增量里推完）
                _emo = _emotion_state(user_id, char_id)
                _emotion_payload = {
                    "emotions": {k: int(_emo.get(k, EMOTION_BASE.get(k, 10))) for k in EMOTION_KEYS} if _emo else {},
                    "composite": round(_emotion_composite(_emo), 1) if _emo else 0,
                    "tone": _emotion_tone(_emotion_composite(_emo)) if _emo else "一般",
                    "reason": (_emo or {}).get("reason", ""),
                    "mood_event": _emotion_active_mood_event(_emo) if _emo else None,
                }
                yield f"data: {json.dumps({'type': 'assistant_messages', 'id': response_id, 'partial': False, 'conversation_state': parsed['conversation_state'], 'relation': relation, 'emotion': _emotion_payload, 'messages': parsed['messages'], 'pushed': safe_pushed}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                await _flush_batch()

        return StreamingResponse(generate(), media_type="text/event-stream")

    else:
        lock = chat_gate.lock_for(user_id)
        if lock.locked():
            # §15 服务端合批：第一条立即生成，后续连发消息进 pending 队列（不逐条单独推理）
            for _mc in mem_contents:
                pending_queue.enqueue(user_id, {"text": _mc, "images": request.images or [], "timestamp": time.time()})
            logger.info(f"[合批] {user_id} 上一条回复生成中，{len(mem_contents)} 条消息进入 pending 队列")
            return JSONResponse(content={"status": "queued", "id": response_id, "message": "消息已排队，稍后统一回复"})
        async with lock:
            chat_gate.mark_busy(user_id)
            # 用户来消息了：取消追问计划、刷新最后活跃时间、清掉未回复的小情绪
            if user_id in user_states:
                user_states[user_id].pop("pending_nudge", None)
                user_states[user_id]["last_user_at"] = time.time()
                user_states[user_id].pop("unanswered_pending", None)
            _cancel_nudge(user_id, char_id)
            _update_conv_state(user_id, char_id, "user_message")
            for _mc in mem_contents:
                mem_mgr.add_message(user_id, "user", _mc, char_id)
            try:
                from runtime.db import unified_store
                from runtime.logging_setup import logger_conv
                for _mc in mem_contents:
                    logger_conv.info(f"[{user_id}] {char_id} 用户: {_mc[:200]}")
                    unified_store.record_message(user_id, char_id, "user", _mc)
            except Exception:
                pass
            _pending = _build_trailing_user_batch(llm_messages)
            try:
                reply, pieces = await llm_client.chat(
                    char_id, llm_messages, temperature,
                    user_id=user_id,
                    use_memory=request.use_memory,
                    use_rag=request.use_rag,
                    pending_messages=_pending,
                )
            except Exception as e:
                chat_gate.clear_busy(user_id)
                raise HTTPException(500, f"生成失败: {e}")
            if reply:
                mem_mgr.add_message(user_id, "assistant", reply, char_id, pieces=pieces)
            else:
                mem_mgr.add_message(user_id, "assistant", "(无回复)", char_id)
            try:
                from runtime.db import unified_store
                from runtime.logging_setup import logger_conv
                logger_conv.info(f"[{user_id}] {char_id} 助手: {(reply or '(无回复)')[:200]}")
                unified_store.record_message(user_id, char_id, "assistant", reply or "(无回复)", pieces=pieces)
            except Exception:
                pass
            asyncio.create_task(_run_memory_pipeline(user_id, char_id))
            chat_gate.clear_busy(user_id)
            while pending_queue.pending(user_id):
                try:
                    await _handle_pending_batch(user_id, char_id, temperature)
                except Exception as e:
                    logger.error(f"[合批] 非流式路径合批失败: {e}")
                    break
        return JSONResponse(content={
            "id": response_id,
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": char_id,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
        }, headers={"X-Character": char_id, "X-Temperature": str(temperature)})


# ============ 记忆 API ============

@app.get("/v1/user/profile")
async def get_user_profile_api():
    """获取当前用户的人设档案（手动填写 + 聊天自动摘取）"""
    return {"success": True, "user_profile": mem_mgr.get_user_profile("web_user"),
            "user_location": load_runtime_settings().get("user_location", "")}


@app.post("/v1/user/profile")
async def save_user_profile_api(data: dict):
    """保存用户人设档案（预填信息，覆盖式）"""
    profile = (data.get("user_profile") or "").strip()
    mem_mgr.save_user_profile("web_user", profile)
    if data.get("user_location") is not None:
        rt = load_runtime_settings()
        rt["user_location"] = (data.get("user_location") or "").strip()
        save_runtime_settings(rt)
    return {"success": True, "message": "用户人设已保存"}


@app.get("/v1/chat/history")
async def get_chat_history_api(user_id: str = "web_user", char_id: str = "", limit: int = 100):
    """按 (用户,角色) 返回对话历史，供前端切换智能体后恢复聊天记录（已持久化，重启不丢）"""
    try:
        history = mem_mgr.get_recent_history(user_id, limit=min(500, max(1, int(limit))), char_id=char_id)
        return {"success": True, "data": history}
    except Exception as e:
        raise HTTPException(500, f"读取聊天记录失败: {e}")


@app.post("/v1/chat/history/clear")
async def clear_chat_history_api(user_id: str = "web_user", char_id: str = ""):
    """清空该 (用户,角色) 的对话历史（不影响记忆库与每日大纲）"""
    try:
        mem_mgr.clear_history(user_id, char_id=char_id)
        return {"success": True, "message": "对话历史已清空"}
    except Exception as e:
        raise HTTPException(500, f"清空聊天记录失败: {e}")


@app.get("/v1/memory/{user_id}")
async def get_memory(user_id: str, query: str = "", top_k: int = 10, char_id: str = ""):
    """获取用户记忆（可传 char_id 只看该角色的隔离记忆库）。
    返回三档：day=当天话题/看法总结（不是逐条聊天记录），short=短期记忆，long=长期记忆。"""
    store = mem_mgr._get_store(user_id, char_id)
    if query:
        results = store.search(query, top_k=top_k)
        return {"data": results, "day": [], "short": [], "long": []}
    all_ = store.list_all()
    short = [m for m in all_ if m.get("memory_type") == "short_term"]
    long = [m for m in all_ if m.get("memory_type") == "long_term"]
    # 当天记忆 = 当天聊天话题 + 双方看法的总结（有就显示，没有就后台生成 + 占位提示）
    today = datetime.now().strftime("%Y-%m-%d")
    outline = ""
    try:
        outline = mem_mgr.get_daily_outline(user_id, today, char_id)
    except Exception:
        pass
    day = []
    if outline:
        day = [{"type": "day_summary", "role": "system", "text": outline, "content": outline, "timestamp": None}]
    else:
        n = 0
        try:
            n = mem_mgr.day_message_count(user_id, char_id, today)
        except Exception:
            pass
        if n >= 3:
            asyncio.create_task(_generate_daily_outline(user_id, char_id, today, min_messages=3, idle_minutes=0))
        day = [{"type": "day_summary", "role": "system",
                "text": f"今天已聊 {n} 条，正在整理当天话题总结…（稍后自动刷新）" if n >= 3 else f"今天聊了 {n} 条，还不足以总结",
                "content": f"今天已聊 {n} 条，正在整理当天话题总结…（稍后自动刷新）" if n >= 3 else f"今天聊了 {n} 条，还不足以总结",
                "timestamp": None}]
    # 当天具体事件分条（带权重，独立列出，权重影响后续记忆归纳）
    events = []
    try:
        events = mem_mgr.get_daily_events(user_id, today, char_id)
    except Exception:
        pass
    return {"data": all_, "day": day, "events": events, "short": short, "long": long}


@app.post("/v1/memory/{user_id}")
async def add_memory(user_id: str, data: MemoryAddRequest, char_id: str = ""):
    """添加记忆（传 char_id 时存入该角色的隔离记忆库，只在该角色面板/上下文中生效）"""
    mem_mgr.save_fact(user_id, data.text, {"tags": data.tags or []}, char_id=char_id)
    return {"success": True, "message": "记忆已保存"}


@app.delete("/v1/memory/{user_id}/{memory_id}")
async def delete_memory(user_id: str, memory_id: str, char_id: str = ""):
    """删除记忆（按角色隔离库定位）"""
    store = mem_mgr._get_store(user_id, char_id)
    if store.delete(memory_id):
        return {"success": True, "message": "记忆已删除"}
    raise HTTPException(404, "记忆不存在")


@app.post("/v1/memory/{user_id}/clear")
async def clear_memory(user_id: str, char_id: str = "", scope: str = "day"):
    """清空记忆。scope: day=仅当天聊天记录（默认）；short=短期记忆；long=长期记忆；all=全部（含对话历史）"""
    scope = (scope or "day").strip().lower()
    if scope == "day":
        mem_mgr.clear_day_log(user_id, char_id=char_id)
    elif scope == "short":
        _st = mem_mgr._get_store(user_id, char_id)
        _st.clear(memory_type="short_term")
        _st._save()
    elif scope == "long":
        _st = mem_mgr._get_store(user_id, char_id)
        _st.clear(memory_type="long_term")
        _st._save()
    else:
        mem_mgr.clear_history(user_id, char_id=char_id)
        mem_mgr.clear_store(user_id, char_id=char_id)
    return {"success": True, "message": f"已清空：{scope}"}


@app.post("/v1/memory/{user_id}/summary")
async def generate_memory_summary(user_id: str, char_id: str = ""):
    """生成记忆摘要（按角色隔离库）"""
    # 触发摘要生成
    history = mem_mgr.get_recent_history(user_id, char_id=char_id)
    if len(history) < 10:
        return {"success": False, "message": "对话历史不足，无法生成摘要"}
    
    # 这里简化处理：将最近对话压缩为关键句
    key_facts = []
    for msg in history:
        if msg["role"] == "user" and len(msg["content"]) > 10:
            key_facts.append(msg["content"][:150])
    
    if key_facts:
        summary = "；".join(key_facts[:5])
        mem_mgr.save_fact(user_id, summary, {"type": "manual_summary"}, char_id=char_id)
        return {"success": True, "summary": summary}
    
    return {"success": False, "message": "没有足够的内容生成摘要"}


@app.get("/v1/emotion/{user_id}")
async def get_emotion(user_id: str, char_id: str = ""):
    """获取角色当前多维情绪（开心/害怕/悲伤/焦虑/兴奋）+ 总情绪 + 原因 + 当天基调"""
    es = _emotion_state(user_id, char_id)
    if not es:
        return {"emotions": {}, "composite": 0, "tone": "一般", "reason": "", "mood_event": None}
    comp = round(_emotion_composite(es), 1)
    return {
        "emotions": {k: int(es.get(k, EMOTION_BASE.get(k, 10))) for k in EMOTION_KEYS},
        "composite": comp,
        "tone": _emotion_tone(comp),
        "reason": es.get("reason", ""),
        "mood_event": _emotion_active_mood_event(es),
    }


# ============ RAG API ============

@app.get("/v1/rag/search")
async def rag_search(query: str, top_k: int = 3):
    """RAG 知识搜索"""
    results = kb.search(query, top_k=top_k)
    return {"data": results}


@app.get("/v1/rag/documents")
async def list_documents():
    """列出已索引文档"""
    entries = kb.store.list_all(memory_type="knowledge")
    # 按 source 分组
    sources = {}
    for e in entries:
        src = e.get("metadata", {}).get("source", "unknown")
        if src not in sources:
            sources[src] = {"source": src, "chunks": 0}
        sources[src]["chunks"] += 1
    return {"data": list(sources.values())}


@app.post("/v1/rag/index")
async def index_document(path: str = ""):
    """手动索引文档"""
    if path:
        filepath = Path(path)
        if filepath.exists():
            count = kb.index_document(filepath)
            return {"success": True, "chunks": count}
        raise HTTPException(404, "文件不存在")
    else:
        from rag import UPLOADS_DIR
        count = kb.index_directory(UPLOADS_DIR)
        return {"success": True, "total_chunks": count}


# ============ 路由 API ============

@app.get("/v1/routes")
async def get_routes():
    from config import get_config
    return get_config().routes


@app.post("/v1/routes")
async def save_routes(data: SaveRoutesRequest):
    try:
        from config import get_config
        cfg = get_config()
        cfg._routes["rules"] = data.rules
        cfg._save_settings()
        from gateway.router import get_router
        get_router().reload()
        return {"success": True, "message": f"已保存 {len(data.rules)} 条路由规则"}
    except Exception as e:
        logger.error(f"保存路由规则失败: {e}")
        raise HTTPException(500, f"保存失败: {str(e)}")


# ============ 设置 API ============

@app.get("/v1/settings")
async def get_settings():
    runtime = load_runtime_settings()
    from config import get_config
    cfg = get_config()
    user_profile = ""
    try:
        user_profile = mem_mgr.get_user_profile("web_user")
    except Exception as e:
        logger.warning(f"读取用户人设失败: {e}")
    
    return {
        "llm": {
            "base_url": runtime.get("llm_url", LLM_URL),
            "model": runtime.get("llm_model", LLM_MODEL),
            "route_url": runtime.get("llm_route_url") or LLM_ROUTE_URL or runtime.get("llm_url", LLM_URL),
            "route_model": runtime.get("llm_route_model") or LLM_ROUTE_MODEL or runtime.get("llm_model", LLM_MODEL),
            "api_key": runtime.get("api_key", ""),
            "default_temperature": runtime.get("default_temperature", 0.7),
        },
        "routes": cfg.routes.get("rules", []),
        "memory_tags": runtime.get("memory_tags", []),
        "allow_profanity": runtime.get("allow_profanity", False),
        "allow_naughty": runtime.get("allow_naughty", False),
        "profanity_level": runtime.get("profanity_level", "off"),
        "naughty_level": runtime.get("naughty_level", "off"),
        "thinking_level": runtime.get("thinking_level", "off"),
        "desire_base": runtime.get("desire_base", 50),
        "web_enabled": runtime.get("web_enabled", True),
        "proactive_enabled": runtime.get("proactive_enabled", True),
        "show_thinking": runtime.get("show_thinking", False),
        "thinking_enabled": runtime.get("thinking_enabled", False),
        "parallel_requests": runtime.get("parallel_requests", "auto"),
        "delayed_reply_enabled": runtime.get("delayed_reply_enabled", False),
        "day_memory_enabled": runtime.get("day_memory_enabled", True),
        "night_memory_enabled": runtime.get("night_memory_enabled", True),
        "vision_supported": runtime.get("vision_supported", False),
        "uncensored": runtime.get("uncensored", False),
        "user_profile": user_profile,
        "user_location": runtime.get("user_location", ""),
        "user_gender": runtime.get("user_gender", ""),
        "wechat_available": bool(channel_registry.get("wechaty") and channel_registry.get("wechaty").available),
        "wechat_running": bool(channel_registry.get("wechaty") and channel_registry.get("wechaty").running),
        "channels": [c["id"] for c in channel_registry.list_channels()],
    }


@app.post("/v1/settings")
async def save_settings(data: SaveSettingsRequest):
    try:
        settings = load_runtime_settings()
        
        if data.llm_url:
            settings["llm_url"] = data.llm_url
            global LLM_URL
            LLM_URL = data.llm_url
        if data.llm_model:
            settings["llm_model"] = data.llm_model
            global LLM_MODEL
            LLM_MODEL = data.llm_model
        if data.llm_route_url is not None:
            settings["llm_route_url"] = data.llm_route_url
            global LLM_ROUTE_URL
            LLM_ROUTE_URL = data.llm_route_url
        if data.llm_route_model is not None:
            settings["llm_route_model"] = data.llm_route_model
            global LLM_ROUTE_MODEL
            LLM_ROUTE_MODEL = data.llm_route_model
        if data.api_key is not None:
            settings["api_key"] = data.api_key
        if data.default_temperature is not None:
            settings["default_temperature"] = data.default_temperature
        if data.memory_tags is not None:
            settings["memory_tags"] = data.memory_tags
        if data.allow_profanity is not None:
            settings["allow_profanity"] = data.allow_profanity
        if data.allow_naughty is not None:
            settings["allow_naughty"] = data.allow_naughty
        if data.profanity_level is not None:
            settings["profanity_level"] = data.profanity_level
        if data.naughty_level is not None:
            settings["naughty_level"] = data.naughty_level
        if data.thinking_level is not None:
            settings["thinking_level"] = data.thinking_level
        if data.desire_base is not None:
            settings["desire_base"] = max(0, min(100, int(data.desire_base)))
        if data.web_enabled is not None:
            settings["web_enabled"] = data.web_enabled
        if data.proactive_enabled is not None:
            settings["proactive_enabled"] = bool(data.proactive_enabled)
        if data.show_thinking is not None:
            settings["show_thinking"] = data.show_thinking
        if data.thinking_enabled is not None:
            settings["thinking_enabled"] = data.thinking_enabled
        if data.parallel_requests is not None:
            settings["parallel_requests"] = str(data.parallel_requests).strip() or "auto"
        if data.delayed_reply_enabled is not None:
            settings["delayed_reply_enabled"] = bool(data.delayed_reply_enabled)
        if data.day_memory_enabled is not None:
            settings["day_memory_enabled"] = bool(data.day_memory_enabled)
        if data.night_memory_enabled is not None:
            settings["night_memory_enabled"] = bool(data.night_memory_enabled)
        if data.user_profile is not None:
            try:
                mem_mgr.save_user_profile("web_user", data.user_profile)
            except Exception as e:
                logger.error(f"保存用户人设失败: {e}")
        if data.user_location is not None:
            settings["user_location"] = data.user_location.strip()
        if data.user_gender is not None:
            settings["user_gender"] = (data.user_gender or "").strip().lower()
        
        save_runtime_settings(settings)
        return {"success": True, "message": "设置已保存"}
    except Exception as e:
        raise HTTPException(500, f"保存失败: {str(e)}")


# ============ LLM 代理 API ============

@app.get("/v1/models")
async def list_models():
    """获取可用模型列表"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{LLM_URL}/models")
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data", [])
            return {"data": [{"id": m.get("id", ""), "name": m.get("id", "")} for m in models]}
    except Exception as e:
        logger.warning(f"获取模型列表失败: {e}")
        return {"data": [{"id": LLM_MODEL, "name": LLM_MODEL}]}


@app.get("/v1/llm/presets")
async def llm_presets():
    """设置页「快速接入」：主流 LLM API 预设，选中后自动填 base_url + 模型，用户只需填 Key"""
    return {"data": LLM_PRESETS}


@app.post("/v1/llm/verify")
async def verify_llm(data: dict):
    """验证 LLM 配置是否可用，并探测模型是否为 uncensored（无限制模型）"""
    url = data.get("url", LLM_URL).rstrip("/")
    model = data.get("model", LLM_MODEL)
    api_key = data.get("api_key", "")
    
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    # 测试1: 检查 /models 接口
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{url}/models", headers=headers)
            if resp.status_code != 200:
                return {"success": False, "message": f"/models 接口返回 {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"无法连接到 LLM 服务: {str(e)}"}
    
    # 测试2: 发送一个简单对话请求验证模型可用
    uncensored = False
    uncensored_note = ""
    try:
        test_payload = {
            "model": model,
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": 10,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{url}/chat/completions", json=test_payload, headers=headers)
            if resp.status_code != 200:
                return {"success": False, "message": f"对话接口返回 {resp.status_code}: {resp.text[:200]}"}
            resp_data = resp.json()
            if "choices" not in resp_data:
                return {"success": False, "message": "响应格式异常，缺少 choices 字段"}
    except Exception as e:
        return {"success": False, "message": f"对话测试失败: {str(e)}"}

    # 测试3: uncensored 探测——问一句需要脏话/粗口回应的短句，看模型是拒绝还是自然给出
    try:
        probe_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个说话直接的真人朋友，回复只用一个词，不用解释。"},
                {"role": "user", "content": "朋友请你只用两个字回一句发泄情绪的口头禅（中文脏话或粗口）。"},
            ],
            "max_tokens": 64,
            "temperature": 0.3,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{url}/chat/completions", json=probe_payload, headers=headers)
            if resp.status_code == 400:
                # 部分 OpenAI 兼容服务不认 chat_template_kwargs，去掉重试
                probe_payload.pop("chat_template_kwargs", None)
                resp = await client.post(f"{url}/chat/completions", json=probe_payload, headers=headers)
            if resp.status_code == 200:
                resp_data = resp.json()
                reply = (resp_data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
                refused = any(k in reply for k in ("抱歉", "不能", "无法", "拒绝", "对不起", "作为AI", "不合适", "遵纪守法", "请文明", "文明用语", "不便"))
                has_profanity = any(k in reply for k in ("操", "靠", "妈的", "他妈的", "fuck", "shit", "tmd", "卧槽", "我艹", "去你", "尼玛", "妈逼", "妈的"))
                if has_profanity and not refused:
                    uncensored = True
                    uncensored_note = "模型可自由输出粗口，判定为 uncensored"
                elif refused:
                    uncensored_note = "模型对粗口请求表示拒绝，判定为受控模型"
                else:
                    uncensored_note = f"探测结果不明确（回复：{reply[:30]}），按受控模型处理"
            else:
                uncensored_note = f"探测请求返回 {resp.status_code}，跳过 uncensored 判定"
    except Exception as e:
        uncensored_note = f"uncensored 探测失败（{type(e).__name__}），跳过判定"

    # 测试4: vision 探测——发一张 1x1 红色 PNG，看模型是否接受图片输入
    vision_supported = False
    vision_note = ""
    try:
        def _png_chunk(typ: bytes, data: bytes) -> bytes:
            body = typ + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

        def _tiny_red_png_b64() -> str:
            sig = b"\x89PNG\r\n\x1a\n"
            ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            idat = _png_chunk(b"IDAT", zlib.compress(b"\x00" + bytes([255, 0, 0])))
            iend = _png_chunk(b"IEND", b"")
            return base64.b64encode(sig + ihdr + idat + iend).decode()

        vision_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是图像测试助手，只用一两个中文词回答图片主色。"},
                {"role": "user", "content": [
                    {"type": "text", "text": "这张图是什么颜色？"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_tiny_red_png_b64()}"}},
                ]},
            ],
            "max_tokens": 64,
            "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{url}/chat/completions", json=vision_payload, headers=headers)
            if resp.status_code == 400 and "chat_template_kwargs" in vision_payload:
                vision_payload.pop("chat_template_kwargs", None)
                resp = await client.post(f"{url}/chat/completions", json=vision_payload, headers=headers)
            if resp.status_code == 400:
                vision_note = "该模型不支持图片输入（vision 接口返回 400）"
            elif resp.status_code == 200:
                vmsg = ((resp.json().get("choices", [{}])[0].get("message", {}).get("content") or "")).strip()
                vlow = vmsg.lower()
                if any(k in vlow for k in ("红", "red", "#ff0000", "颜色")):
                    vision_supported = True
                    vision_note = "模型支持图片输入（vision 探测通过）"
                elif any(k in vmsg for k in ("无法", "不支持", "不能", "抱歉", "看不见", "没有图", "没有图片")):
                    vision_note = f"模型对图片请求的回复显示不支持视觉（回复：{vmsg[:30]}）"
                else:
                    vision_supported = True
                    vision_note = f"模型接受了图片请求（回复：{vmsg[:20]}），按支持视觉处理"
            else:
                vision_note = f"vision 探测返回 {resp.status_code}，跳过判定"
    except Exception as e:
        vision_note = f"vision 探测失败（{type(e).__name__}），跳过判定"

    settings = load_runtime_settings()
    settings["uncensored"] = uncensored
    settings["vision_supported"] = vision_supported
    # 验证通过即自动保存整套 LLM 配置（主模型 + 路由模型），下次打开不用重配
    if data.get("url"):
        settings["llm_url"] = str(data["url"]).rstrip("/")
        globals()["LLM_URL"] = settings["llm_url"]
    if data.get("model"):
        settings["llm_model"] = str(data["model"])
        globals()["LLM_MODEL"] = settings["llm_model"]
    if data.get("api_key") is not None:
        settings["api_key"] = data.get("api_key") or ""
    if data.get("route_url") is not None:
        settings["llm_route_url"] = str(data.get("route_url") or "").rstrip("/")
        globals()["LLM_ROUTE_URL"] = settings["llm_route_url"]
    if data.get("route_model") is not None:
        settings["llm_route_model"] = str(data.get("route_model") or "")
        globals()["LLM_ROUTE_MODEL"] = settings["llm_route_model"]
    save_runtime_settings(settings)
    return {
        "success": True,
        "message": "LLM 配置验证通过",
        "uncensored": uncensored,
        "uncensored_note": uncensored_note,
        "vision_supported": vision_supported,
        "vision_note": vision_note,
    }


# ============ 联网 API ============

@app.post("/v1/web/search")
async def web_search_api(data: dict):
    """联网搜索（免费接口）：供前端"我去看看"和手动查证使用"""
    query = (data.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query 不能为空")
    results = await web_tools.web_search(query, top_k=6)
    return {"success": True, "data": results}


@app.post("/v1/web/link")
async def web_link_api(data: dict):
    """解析链接：视频/小红书/购物/普通网页，尽力而为，失败返回 null"""
    url = (data.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url 不能为空")
    info = await web_tools.video_info(url)
    if not info and web_tools.is_shopish(url):
        info = await web_tools.lookup_shop_url(url)
    if not info:
        info = await web_tools.fetch_page_meta(url)
    return {"success": True, "data": info}


@app.get("/v1/web/hot")
async def web_hot_api():
    """每日热门（B站热门 + 综合热搜，本地缓存 6 小时）"""
    items = await web_tools.fetch_hotlist()
    return {"success": True, "data": items}


# ============ 微信 API ============

# ============ 外部通讯通道 API（参考 OpenClaw 架构） ============

@app.get("/v1/channels/list")
async def channels_list():
    """列出所有外部通讯通道（微信 ClawBot / Wechaty / 企业微信 / QQBot / 飞书）"""
    return {"success": True, "data": channel_registry.list_channels()}


@app.get("/v1/sessions")
async def sessions_list():
    """会话选择器数据：本机网页 + 所有外部通讯会话（微信等）。
    微信侧收发的消息会实时注册到这里，前端切过去即可查看/续聊该会话。"""
    items = [{
        "user_id": "web_user",
        "label": "本机 · 网页",
        "source": "web",
        "last_seen": None,
    }]
    seen = {"web_user"}
    for uid, info in sorted(session_registry.items(), key=lambda kv: kv[1].get("last_seen") or 0, reverse=True):
        if uid in seen:
            continue
        seen.add(uid)
        # 单一对话模式下，微信消息已并入 App 主对话（web_user），不再单独列出 wx_ 会话
        try:
            _chid = (info or {}).get("channel_id") or ""
            _ch = channel_registry.get(_chid) if _chid else None
            if _ch and getattr(_ch, "config", {}).get("single_conversation", False):
                continue
        except Exception:
            pass
        # 会话绑定的角色（通道配置里的 character_id）：前端切换会话时可自动选对应角色
        bound_char = ""
        try:
            ch_ = channel_registry.get(_chid) if _chid else None
            bound_char = str((getattr(ch_, "config", {}) or {}).get("character_id") or "").strip() or ""
        except Exception:
            bound_char = ""
        items.append({
            "user_id": uid,
            "label": (info or {}).get("label") or _session_label(uid),
            "source": (info or {}).get("source") or "external",
            "channel_id": (info or {}).get("channel_id") or "",
            "character_id": bound_char,
            "last_seen": (info or {}).get("last_seen"),
        })
    return {"success": True, "data": items}


@app.get("/v1/channels/{channel_id}/status")
async def channel_status(channel_id: str):
    """单个通道状态（含二维码）"""
    ch = channel_registry.get(channel_id)
    if not ch:
        raise HTTPException(404, "通道不存在")
    return {"success": True, "data": ch.status()}


@app.get("/v1/channels/{channel_id}/qr")
async def channel_qr(channel_id: str):
    """获取通道二维码（base64 / data URL），未就绪返回 null"""
    ch = channel_registry.get(channel_id)
    if not ch:
        raise HTTPException(404, "通道不存在")
    return {"success": True, "data": {"qr_status": ch.qr_status, "qr": ch.qr_data or None, "qr_message": ch.qr_message}}


@app.post("/v1/channels/{channel_id}/config")
async def channel_config(channel_id: str, payload: dict):
    """保存通道配置（企业微信/QQBot/飞书表单、ClawBot 账号标识）"""
    ch = channel_registry.get(channel_id)
    if not ch:
        raise HTTPException(404, "通道不存在")
    # ClawBot 切换账号：另起独立通道（多账号并行）
    if channel_id.startswith("clawbot_") and payload.get("account_id"):
        new_id = str(payload["account_id"]).strip() or "main"
        if new_id != ch.account_id:
            channel_store.save_config(f"clawbot_{new_id}", {
                "account_id": new_id,
                "character_id": str(payload.get("character_id") or "").strip(),
            })
            new_ch = ClawBotChannel(new_id)
            channel_registry.register(new_ch)
            ch.stop()
            return {"success": True, "message": f"已切换到账号 {new_id}，请点击启动扫码", "channel_id": new_ch.id}
    try:
        ch.save_config(payload or {})
        return {"success": True, "message": "配置已保存"}
    except Exception as e:
        logger.warning(f"[通道] 保存配置失败 {channel_id}: {e}")
        raise HTTPException(400, f"保存配置失败: {e}")


@app.post("/v1/channels/{channel_id}/start")
async def channel_start(channel_id: str, payload: dict | None = None):
    """启动通道（微信类通道启动后进入扫码等待；ClawBot 可传 {"account_id": "xxx"}）"""
    ch = channel_registry.get(channel_id)
    if not ch:
        raise HTTPException(404, "通道不存在")
    if not ch.integrated:
        return {"success": False, "message": "该通道为预留通道，暂未接通"}
    if not ch.available:
        return {"success": False, "message": ch.qr_message or "该通道依赖未就绪"}
    ok = ch.start(**(payload or {}))
    if ok:
        return {"success": True, "message": "已启动，请完成扫码/登录"}
    return {"success": False, "message": ch.qr_message or "启动失败"}


@app.post("/v1/channels/{channel_id}/stop")
async def channel_stop(channel_id: str):
    """停止通道"""
    ch = channel_registry.get(channel_id)
    if not ch:
        raise HTTPException(404, "通道不存在")
    ch.stop()
    return {"success": True, "message": "已停止"}


@app.post("/v1/channels/{channel_id}/reset")
async def channel_reset(channel_id: str):
    """重置通道连接（切换账号）：清空登录态/游标/会话，下次启动走全新扫码。
    仅集成通道支持；ClawBot 用它切换微信号，Wechaty 用它重登。"""
    ch = channel_registry.get(channel_id)
    if not ch:
        raise HTTPException(404, "通道不存在")
    if not ch.integrated:
        return {"success": False, "message": "该通道为预留通道，无需重置"}
    if hasattr(ch, "reset"):
        ch.reset()
    else:
        ch.stop()
    return {"success": True, "message": "已重置连接，请重新点击启动扫码"}


# ============ Wechaty webhook（Node 网关回调） ============

@app.post("/v1/channels/wechaty/webhook")
async def wechaty_webhook(request: Request):
    """Wechaty 网关推送入站消息：校验共享密钥 → 交给统一消息链路（自动桥接主事件循环）"""
    try:
        secret = request.headers.get("X-Qiyu-Secret", "")
        ch = channel_registry.get("wechaty")
        if not ch:
            return {"ok": False, "message": "wechaty channel not registered"}
        if secret != getattr(ch, "_secret", ""):
            return JSONResponse(status_code=403, content={"ok": False, "message": "bad secret"})
        payload = await request.json()
        ok = await ch.handle_webhook(payload)
        return {"ok": ok}
    except Exception as e:
        logger.warning(f"[Wechaty] webhook 处理异常: {e}")
        return {"ok": False, "message": str(e)}


# ============ 微信 API（向后兼容，映射到 Wechaty 单账号通道） ============

@app.post("/v1/wechat/start")
async def start_wechat():
    """启动微信机器人（Wechaty 单账号接管）"""
    ch = channel_registry.get("wechaty")
    if not ch or not ch.available:
        raise HTTPException(400, ch.qr_message or "Wechaty 网关未就绪")
    success = ch.start()
    if success:
        return {"success": True, "message": "微信机器人已启动，请扫描二维码登录"}
    return {"success": False, "message": ch.qr_message or "启动失败"}


@app.post("/v1/wechat/stop")
async def stop_wechat():
    """停止微信机器人"""
    ch = channel_registry.get("wechaty")
    if ch:
        ch.stop()
    return {"success": True, "message": "微信机器人已停止"}


@app.get("/v1/wechat/status")
async def wechat_status():
    """微信状态（Wechaty 通道）"""
    ch = channel_registry.get("wechaty")
    st = ch.status() if ch else {}
    return {
        "available": bool(ch and ch.available),
        "running": st.get("running", False),
        "bot_name": "栖语 · Wechaty",
        "qr_status": st.get("qr_status", "idle"),
        "qr": st.get("qr"),
    }


@app.get("/v1/wechat/qr")
async def wechat_qr():
    """获取当前登录二维码（base64 PNG），未就绪返回 null"""
    ch = channel_registry.get("wechaty")
    st = ch.status() if ch else {}
    return {
        "qr_status": st.get("qr_status", "idle"),
        "qr": st.get("qr") or None,
    }


# ============ 主动消息推送（SSE） ============

@app.get("/v1/events")
async def events_stream(user_id: str = "web_user"):
    """前端常驻长连接：接收主动消息（追问/每日展开），真人节奏播放"""
    q: asyncio.Queue = asyncio.Queue()
    event_queues[user_id] = q
    async def gen():
        try:
            yield "data: {\"type\": \"connected\"}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            event_queues.pop(user_id, None)
            raise
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


# ============ Runtime ============

@app.get("/v1/runtime/status")
async def runtime_status_api():
    return runtime_manager.status()


@app.get("/v1/runtime/hardware")
async def runtime_hardware_api():
    return runtime_manager.hardware.detect().to_dict()


@app.get("/v1/runtime/providers")
async def runtime_providers_api():
    return {"providers": runtime_manager.registry.list()}


@app.get("/v1/concurrency")
async def concurrency_api():
    """全局并发限制器状态：parallel_requests 换算结果 + 各类分闸上限 + 实时占用。"""
    from runtime.concurrency import concurrency_limiter
    return {"settings": {"parallel_requests": concurrency_limiter.configured_limit() or "unlimited"},
            "stats": concurrency_limiter.stats()}


# ============ Health ============



# ============ Avatar API（§40/§41：Live2D / VRC 统一动作接口） ============
@app.get("/v1/avatar")
async def avatar_state():
    """头像当前状态 + 可用 Provider（前端/外部 Live2D 运行时查询）。"""
    from runtime.avatar import avatar_controller
    return {
        "state": avatar_controller.state(),
        "providers": avatar_controller.list_providers(),
    }


@app.post("/v1/avatar")
async def avatar_apply(data: dict):
    """AI 输出 emotion/intensity/action → 具体平台命令。

    body: {"emotion": "surprised", "intensity": 0.4, "action": "look_at_user",
           "avatar_type": "live2d"|"vrc"}
    """
    from runtime.avatar import AvatarAction, avatar_controller
    action = AvatarAction(
        emotion=str(data.get("emotion") or "neutral"),
        intensity=float(data.get("intensity") or 0.0),
        action=str(data.get("action") or "idle"),
        expression=str(data.get("expression") or ""),
        gesture=str(data.get("gesture") or ""),
        text=str(data.get("text") or ""),
    )
    cmd = await avatar_controller.apply(action, str(data.get("avatar_type") or "live2d"))
    return {"ok": True, "command": cmd, "state": avatar_controller.state()}


@app.post("/v1/avatar/interrupt")
async def avatar_interrupt():
    from runtime.avatar import avatar_controller
    return await avatar_controller.interrupt()


# ============ 性能监控（§49） ============
@app.get("/v1/perf")
async def perf_api():
    from runtime.perf import perf_monitor
    perf_monitor.flush()
    return perf_monitor.snapshot()


# ============ 消息平台（§46） ============
@app.get("/v1/platforms")
async def platforms_api():
    from runtime.platform import platform_registry
    return {"platforms": platform_registry.list()}


# ============ MiniMind-O 模型（§11/§12） ============
@app.get("/v1/runtime/models")
async def runtime_models_api():
    return runtime_manager.model_status()


@app.post("/v1/runtime/models/load")
async def runtime_models_load(data: dict):
    return runtime_manager.load_model(str(data.get("path") or ""))


@app.post("/v1/runtime/models/unload")
async def runtime_models_unload():
    return runtime_manager.unload_model()


@app.post("/v1/runtime/realtime/judge")
async def runtime_realtime_judge(data: dict):
    """Realtime Brain 单轮判断（MiniMind-O 真实后端；未部署时诚实回退 Main Brain）。"""
    decision = await runtime_manager.realtime_judge(str(data.get("text") or ""), data.get("context"))
    return {"decision": decision.to_dict()}


# ============ TTS（§42/§43） ============
@app.post("/v1/tts")
async def tts_api(data: dict):
    """语音合成：text + emotion/intensity/speed/pitch/pause_style/voice。

    返回 wav/mp3 文件流；未配置可用引擎时返回 503（不假装发声）。
    """
    from runtime.tts import tts_provider
    from runtime.concurrency import concurrency_limiter
    if not tts_provider.status().available:
        return JSONResponse(status_code=503, content={"error": "tts_unavailable", "reason": tts_provider.status().reason})
    async with concurrency_limiter.slot("tts"):
        out = await tts_provider.synthesize(
        str(data.get("text") or ""),
        emotion=str(data.get("emotion") or ""),
        intensity=data.get("intensity"),
        speed=data.get("speed"),
        pitch=data.get("pitch"),
        pause_style=str(data.get("pause_style") or ""),
        voice=str(data.get("voice") or ""),
    )
    if not out or not os.path.exists(out.get("path", "")):
        return JSONResponse(status_code=503, content={"error": "tts_failed"})
    return FileResponse(out["path"], media_type="audio/wav" if out.get("format") == "wav" else "audio/mpeg")


@app.get("/v1/tts/status")
async def tts_status_api():
    from runtime.tts import tts_provider
    return {"provider": tts_provider.id, "status": tts_provider.status().to_dict()}


# ============ STT（§42） ============
@app.post("/v1/stt")
async def stt_api(request: Request):
    """语音转写：raw body 为音频字节（wav/silk/amr/mp3），返回 {text}。"""
    from runtime.stt import stt_provider
    from runtime.concurrency import concurrency_limiter
    body = await request.body()
    if not body:
        return JSONResponse(status_code=400, content={"error": "empty_audio"})
    async with concurrency_limiter.slot("stt"):
        text = await stt_provider.transcribe(body)
    return {"text": text, "available": stt_provider.status().available}


@app.get("/v1/stt/status")
async def stt_status_api():
    from runtime.stt import stt_provider
    return {"provider": stt_provider.id, "status": stt_provider.status().to_dict()}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mode": "full",
        "llm_available": llm_client.available if llm_client else False,
        "characters": len(char_mgr.list_characters()),
        "wechat_available": bool(channel_registry.get("wechaty") and channel_registry.get("wechaty").available),
        "wechat_running": bool(channel_registry.get("wechaty") and channel_registry.get("wechaty").running),
        "channels": [c["id"] for c in channel_registry.list_channels()],
    }


# ============ 启动 ============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("demo:app", host=DEMO_HOST, port=DEMO_PORT, reload=False, log_level="info")
