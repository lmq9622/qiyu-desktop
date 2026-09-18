# -*- coding: utf-8 -*-
"""Qiyu 主动消息/调度/后台循环（从 demo.py 迁移，M1）。"""
import re
import random
import asyncio
import time
import uuid
import httpx
from datetime import datetime, timedelta
from loguru import logger
from characters import get_character_manager
from memory import get_memory_manager
from channels import get_channel_registry
from runtime.toolagent import tool_agent

char_mgr = get_character_manager()
mem_mgr = get_memory_manager()
channel_registry = get_channel_registry()
llm_client = None


from companion.state import _proactive_store, _schedules_store, user_states
from companion.settings import _apply_thinking_kwargs, _route_llm_info, load_runtime_settings
from companion.relations import _init_relation, _register_schedule, _save_proactive, _save_schedules
from companion.conv import _check_unanswered, _context_gate, _conv_state, _event_similar_to_shared, _record_shared_event, _save_conv_store, _update_conv_state
from companion.behavior import _topic_similarity
from companion.scheduler import proactive_scheduler

def _push_event(user_id: str, event: dict):
    q = event_queues.get(user_id)
    if q:
        try:
            q.put_nowait(event)
        except Exception:
            pass

def _session_label(user_id: str) -> str:
    """把栖语内部 user_id 转成前端可读的会话名（微信 → 微信 · xxx）"""
    if not user_id or user_id == "web_user":
        return "本机 · 网页"
    if user_id.startswith("wx_"):
        body = user_id[3:]
        remote = body.split("__", 1)[-1] if "__" in body else body
        return f"微信 · {remote[:24]}"
    return user_id[:28]

def _register_session(user_id: str, channel_id: str = ""):
    if not user_id or user_id == "web_user":
        return
    try:
        session_registry[user_id] = {
            "user_id": user_id,
            "label": _session_label(user_id),
            "source": "wechat" if user_id.startswith("wx_") else "external",
            "channel_id": channel_id,
            "last_seen": time.time(),
        }
    except Exception:
        pass

class _LLMLimiter:
    """LLM 并发闸（规格§25）：委托统一 GlobalConcurrencyLimiter。

    parallel_requests 选项：auto/1/2/3/4/0(不限制)；auto 按硬件并发能力自动决定。
    """

    def limit(self) -> int:
        from runtime.concurrency import concurrency_limiter
        return concurrency_limiter.configured_limit()

    async def acquire(self):
        from runtime.concurrency import concurrency_limiter
        await concurrency_limiter.acquire("llm")

    def release(self):
        from runtime.concurrency import concurrency_limiter
        concurrency_limiter.release("llm")

llm_limiter = _LLMLimiter()

event_queues: dict[str, asyncio.Queue] = {}

session_registry: dict[str, dict] = {}

_outline_inflight: set = set()

_day_ctx_last = {}

async def _send_active_messages(user_id: str, messages: list, conversation_state: str = "闲聊", reason: str = "", char_id: str = ""):
    """主动消息：写入记忆 + 推送给前端（真人节奏播放）；无内容则不发送"""
    st = user_states.get(user_id) or {}
    if not char_id:
        char_id = st.get("character_id", "")
    reply_text = "".join(m.get("text", "") for m in messages or [])
    if not reply_text:
        return
    _pieces = []
    for m in (messages or []):
        _p = {"text": m.get("text", ""), "type": m.get("type", "statement"), "delay": m.get("delay", 0)}
        if m.get("image_url"):
            _p["image_url"] = m.get("image_url")
        _pieces.append(_p)
    mem_mgr.add_message(
        user_id, "assistant", reply_text, char_id,
        pieces=_pieces,
    )
    if reason in ("proactive", "night", "reminder"):
        if st.get("character_id") == char_id:
            st["last_proactive_at"] = time.time()
            st["last_proactive_text"] = reply_text[:200]
        # 会话状态机：主动开场后进入 QUIET，避免定时器自己打断自己
        cs = _conv_state(user_id, char_id)
        cs["last_proactive_at"] = time.time()
        cs["last_proactive_text"] = reply_text[:200]
        _update_conv_state(user_id, char_id, "proactive_sent")
    # 外部通讯用户：通过通道注册表真实下发（ClawBot / itchat / 预留通道）
    if user_id.startswith("wx_"):
        try:
            for i, m in enumerate(messages or []):
                txt = (m.get("text") or "").strip()
                if not txt:
                    continue
                if not channel_registry.send(user_id, txt):
                    logger.warning(f"[通道] 主动消息下发失败: {user_id}")
                delay = (m.get("delay") or 0) / 1000.0
                if i < len(messages or []) - 1 and delay > 0:
                    time.sleep(min(delay, 3.0))
        except Exception as e:
            logger.warning(f"[通道] 主动消息下发失败: {e}")
    _push_event(user_id, {
        "type": "assistant_messages",
        "conversation_state": conversation_state,
        "messages": messages or [],
        "active": True,
        "reason": reason,
        "char_id": char_id,
    })

async def _fire_nudge(user_id: str, context: str, char_id: str = "", attempt: int = 0, total: int = 1):
    st = user_states.get(user_id)
    if st is None:
        logger.warning(f"[调度] {user_id} 无状态，跳过追问")
        return
    if not char_id:
        char_id = st.get("character_id", "")
    if not char_id:
        default_char = char_mgr.get_default()
        char_id = default_char.id if default_char else ""
        st["character_id"] = char_id
    if not char_id:
        logger.warning(f"[调度] {user_id} 无可用角色，跳过追问")
        return
    if not llm_client or not llm_client.available:
        logger.warning(f"[调度] {user_id} LLM 不可用，跳过追问")
        return
    try:
        messages = await llm_client.generate_nudge(char_id, user_id, context, attempt=attempt, total=total)
        await _send_active_messages(user_id, messages, "闲聊", reason="nudge", char_id=char_id)
        logger.info(f"[追问] 用户 {user_id} 第{attempt + 1}/{total}次追问已发送")
    except Exception as e:
        logger.warning(f"[追问] 生成失败: {e}")

async def _fire_reminder(user_id: str, payload: dict):
    st = user_states.get(user_id)
    char_id = (payload or {}).get("char_id") or ""
    if st is None and not char_id:
        logger.warning(f"[调度] {user_id} 无状态，跳过提醒")
        return
    if not char_id:
        if st is None:
            return
        char_id = st.get("character_id", "")
    if not char_id:
        default_char = char_mgr.get_default()
        char_id = default_char.id if default_char else ""
        if st is not None:
            st["character_id"] = char_id
    if not char_id:
        logger.warning(f"[调度] {user_id} 无可用角色，跳过提醒")
        return
    if not llm_client or not llm_client.available:
        logger.warning(f"[调度] {user_id} LLM 不可用，跳过提醒")
        return
    try:
        messages = await llm_client.generate_reminder(char_id, user_id, payload)
        await _send_active_messages(user_id, messages, "闲聊", reason="reminder", char_id=char_id)
        logger.info(f"[提醒] 用户 {user_id} 提醒已发送")
    except Exception as e:
        logger.warning(f"[提醒] 生成失败: {e}")

async def _fire_proactive(user_id: str, night: bool = False, char_id: str = ""):
    st = user_states.get(user_id)
    if not char_id:
        if st is None:
            return
        char_id = st.get("character_id", "")
    if not char_id:
        default_char = char_mgr.get_default()
        char_id = default_char.id if default_char else ""
        if st is not None:
            st["character_id"] = char_id
    if not char_id or not llm_client or not llm_client.available:
        return
    try:
        allowed, reason = _context_gate(user_id, char_id)
        if not allowed:
            logger.info(f"[主动消息] {user_id}/{char_id} Context Gate 拦截: {reason}")
            return
        messages = await llm_client.generate_proactive(char_id, user_id, night=night)
        if not messages:
            return  # 模型判断此刻没话可说
        reply_text = "".join(m.get("text", "") for m in messages).strip()
        key = f"{user_id}::{char_id}"
        prev = _proactive_store.get(key) or {}
        prev_text = (prev.get("last_text") or "").strip()
        # 防重复开场白：和上次主动消息太像（比如隔一小时发两条差不多的）就跳过
        if prev_text and reply_text and _topic_similarity(prev_text, reply_text) > 0.6:
            logger.info(f"[主动消息] {user_id}/{char_id} 与上次开场白太像，跳过")
            return
        # 与最近主动分享过的事件去重（换说法也认同一事件）
        if _event_similar_to_shared(user_id, char_id, reply_text):
            logger.info(f"[主动消息] {user_id}/{char_id} 与近期主动内容重复，跳过")
            return
        _proactive_store[key] = {"last_at": time.time(), "last_text": reply_text[:200]}
        _save_proactive()
        try:
            from runtime.db import unified_store
            unified_store.record_proactive(
                user_id, char_id, "proactive", reply_text[:200],
                topic=(_conv_state(user_id, char_id).get("last_topic") or "")[:80],
                decision="sent")
        except Exception:
            pass
        await _send_active_messages(user_id, messages, "闲聊", reason="night" if night else "proactive", char_id=char_id)
        cs = _conv_state(user_id, char_id)
        _record_shared_event(user_id, char_id, f"proactive_{int(time.time())}", reply_text, cs.get("last_topic", ""))
        logger.info(f"[主动消息] 用户 {user_id} 主动消息已发送")
    except Exception as e:
        logger.warning(f"[主动消息] 生成失败: {e}")

def _looks_like_search(user_input: str) -> bool:
    """用户是否明确要求联网查证/找东西（查/搜/找链接、视频、购物比价、热门等）。
    作为模型忘输出 webcheck 时的兜底触发，避免"嘴上说查、后台没请求"。"""
    if not user_input or not load_runtime_settings().get("web_enabled", True):
        return False
    text = user_input.strip()
    if len(text) < 2:
        return False
    hits = ("帮我查", "帮我搜", "给我搜", "给我查", "帮我找", "给我找", "帮我看看", "搜一下",
            "查一下", "找一下", "查查", "搜搜", "查一查", "搜一搜",
            "发个链接", "发链接", "发个视频", "发视频", "视频链接", "发我链接", "链接发我",
            "多少钱", "哪个好", "比价", "对比一下", "官网", "最新消息", "今天有什么热门", "热门视频")
    return any(w in text for w in hits)

def _looks_like_image_request(user_input: str) -> bool:
    """用户是否明确要图/照片（发张图、看xxx长什么样、找张图、壁纸、表情包等）。
    作为模型忘输出 send_image 动作时的兜底触发：强制真实搜图，禁止"我拍个照"式空话。"""
    if not user_input or not load_runtime_settings().get("web_enabled", True):
        return False
    text = user_input.strip()
    if len(text) < 2:
        return False
    hits = ("发张图", "发图", "发张照片", "发照片", "发我张", "图片", "照片", "的图", "长什么样",
            "给我看", "搜张", "找张", "来张", "表情包", "壁纸", "配图", "看图", "发过来", "给我发")
    return any(w in text for w in hits)

async def _subagent_plan_search(query: str) -> list:
    """带思考的搜索子代理：让路由模型判断是否需要真实联网搜索，并产出 1~2 个搜索关键词。
    返回关键词列表；空列表 = 判定无需搜索。"""
    if not query:
        return []
    route_url, route_model, headers = _route_llm_info()
    prompt = (
        "你是联网搜索子代理。角色（或用户）说要查的内容：\n"
        f"{query[:300]}\n"
        "判断：1) 若只是闲聊、让对方自己看、或不需要外部信息，只输出：无需搜索\n"
        "2) 若需要，输出 1~2 个简洁的中文搜索关键词，用 | 分隔，不要解释。"
    )
    payload = {
        "model": route_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 300,
    }
    # 搜索规划是极短的结构化输出，不需要思考链（开着会白烧 token 还抢推理槽）
    payload["chat_template_kwargs"] = {"enable_thinking": False}
    await llm_limiter.acquire()
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{route_url}/chat/completions", json=payload, headers=headers)
            if resp.status_code == 400 and "chat_template_kwargs" in payload:
                payload.pop("chat_template_kwargs", None)
                resp = await client.post(f"{route_url}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            msg = data.get("choices", [{}])[0].get("message", {}) or {}
            content = (msg.get("content") or "").strip()
            if not content:
                content = (msg.get("reasoning_content") or "").strip()
    except Exception as e:
        logger.warning(f"[子代理] 搜索规划失败: {e}")
        return [query[:80]]
    finally:
        llm_limiter.release()
    if any(w in content for w in ("无需搜索", "不需要", "不用搜", "无需")):
        return []
    parts = [p.strip() for p in re.split(r"[|\n，,]", content) if p.strip()][:3]
    return parts or [query[:80]]


# M4：把搜索子代理注入 runtime.ToolAgent（规划与执行解耦，业务层只面对 Provider 接口）
tool_agent.planner = _subagent_plan_search

async def _fire_webcheck(user_id: str, payload: dict):
    """联网查证：带思考的子代理规划关键词 → 真实联网检索 → 让模型基于真实结果补一条回复"""
    st = user_states.get(user_id)
    char_id = (payload or {}).get("char_id") or ""
    if st is None and not char_id:
        return
    if not char_id:
        if st is None:
            return
        char_id = st.get("character_id", "")
    if not char_id:
        default_char = char_mgr.get_default()
        char_id = default_char.id if default_char else ""
        if st is not None:
            st["character_id"] = char_id
    if not char_id or not llm_client or not llm_client.available:
        return
    query = ((payload or {}).get("query") or "").strip()
    if not query:
        return
    try:
        # 过期结果判定：查的过程中用户已经聊到别的事去了 → 不再强行把旧结果塞回来
        _cs = _conv_state(user_id, char_id)
        _sched_at = float((payload or {}).get("scheduled_at") or 0)
        _last_u = float(_cs.get("last_user_at") or 0)
        _cur_topic = (_cs.get("last_topic") or "").strip()
        _stale = bool(_sched_at and _last_u and _last_u > _sched_at + 10 and _cur_topic)
        evidence = await _task_agent_search(user_id, char_id, query)
        msgs = await llm_client.generate_webcheck_reply(char_id, user_id, query, evidence.get("items") or [],
                                                        auto=bool((payload or {}).get("auto")),
                                                        success=bool(evidence.get("success")),
                                                        stale=_stale, current_topic=_cur_topic)
        if not msgs:
            return
        reply_text = "".join(m.get("text", "") for m in msgs).strip()
        await _send_active_messages(user_id, msgs, "闲聊", reason="webcheck", char_id=char_id)
        # 真实发出去的链接/视频登记 event_id，避免后续定时分享重复发同一内容
        if re.search(r"https?://", reply_text):
            cs = _conv_state(user_id, char_id)
            _record_shared_event(user_id, char_id, f"webcheck_{int(time.time())}", reply_text[:200], cs.get("last_topic", ""))
        logger.info(f"[联网] {user_id} 查证回复已发送: {query[:40]} success={bool(evidence.get('success'))}")
    except Exception as e:
        logger.warning(f"[联网] 查证回复失败: {e}")

async def _fire_imagecheck(user_id: str, payload: dict):
    """找图任务：Task Agent 真实搜图（免费源）→ 找到就把真实图片+一句自然的话发给用户；
    没找到就按人设如实说没有/敷衍掉，绝不假装"拍个照"却什么也没发。"""
    st = user_states.get(user_id)
    char_id = (payload or {}).get("char_id") or ""
    if st is None and not char_id:
        return
    if not char_id:
        if st is None:
            return
        char_id = st.get("character_id", "")
    if not char_id:
        default_char = char_mgr.get_default()
        char_id = default_char.id if default_char else ""
        if st is not None:
            st["character_id"] = char_id
    if not char_id or not llm_client or not llm_client.available:
        return
    query = ((payload or {}).get("query") or "").strip()
    if not query:
        return
    try:
        _cs = _conv_state(user_id, char_id)
        _sched_at = float((payload or {}).get("scheduled_at") or 0)
        _last_u = float(_cs.get("last_user_at") or 0)
        _cur_topic = (_cs.get("last_topic") or "").strip()
        _stale = bool(_sched_at and _last_u and _last_u > _sched_at + 10 and _cur_topic)
        images = await _task_agent_search_images(user_id, char_id, query)
        msgs = await llm_client.generate_webcheck_reply(char_id, user_id, query, images,
                                                        auto=False, success=bool(images), images=images,
                                                        stale=_stale, current_topic=_cur_topic)
        if not msgs:
            return
        reply_text = "".join(m.get("text", "") for m in msgs).strip()
        await _send_active_messages(user_id, msgs, "闲聊", reason="imagecheck", char_id=char_id)
        logger.info(f"[找图] {user_id} 图片回复已发送: {query[:40]} found={len(images)}")
    except Exception as e:
        logger.warning(f"[找图] 图片回复失败: {e}")

async def _task_agent_search_images(user_id: str, char_id: str, query: str) -> list:
    """Task Agent 搜图（M4 委托 runtime.ToolAgent）：真实图片搜索，返回 [{title, url, image_url}]。"""
    evidence = await tool_agent.search_images(query)
    return evidence.items

async def _task_agent_search(user_id: str, char_id: str, query: str) -> dict:
    """Task Agent（M4 委托 runtime.ToolAgent）：真实搜索并返回 Evidence {success, items, query}。
    只有 success=True 且 items 非空时，主模型才允许声称"查到了/发你了"；否则必须如实说没查到。"""
    evidence = await tool_agent.search(query)
    return evidence.to_dict()

def _maybe_schedule_story_check(user_id: str, char_id: str, now: float):
    """讲故事/长内容后听众长时间没回：按关系与空闲时长决定是否轻唤一次（喂？/睡着了？）。
    每段故事最多一次（story_check_sent 标记），避免打扰；关系太生疏或话题已翻篇则不触发。"""
    try:
        if not user_id or not char_id:
            return
        cs = _conv_state(user_id, char_id)
        if not cs.get("story_active") or cs.get("story_check_sent"):
            return
        last_user = float(cs.get("last_user_at") or 0)
        idle = now - last_user if last_user else 0.0
        if not (150 <= idle <= 1800):
            return
        rel = _init_relation(user_id, char_id)
        aff = int(rel.get("affinity", 50) or 50)
        if aff < 25:
            return
        # 已有未执行的同类任务则不重复排队
        for _t in list((_schedules_store.get(user_id) or [])):
            if _t.get("kind") == "storycheck" and not _t.get("done"):
                return
        cs["story_check_sent"] = True
        cs["story_check_at"] = now
        _save_conv_store()
        _register_schedule(user_id, {"kind": "storycheck", "due_at": now + random.uniform(4, 10),
                                     "payload": {"char_id": char_id,
                                                 "context": (cs.get("last_topic") or "")[:60],
                                                 "scheduled_at": now}})
        logger.info(f"[故事轻唤] {user_id}/{char_id} 讲故事后听众 {int(idle // 60)} 分钟没回，已排轻唤")
    except Exception as e:
        logger.warning(f"[故事轻唤] 调度失败: {e}")


async def _fire_story_check(user_id: str, payload: dict):
    """讲故事后对方半天没回 → 最多一句轻唤；用户已回/已冷场/没话说就跳过。"""
    st = user_states.get(user_id)
    char_id = (payload or {}).get("char_id") or ""
    if st is None and not char_id:
        return
    if not char_id:
        if st is None:
            return
        char_id = st.get("character_id", "")
    if not char_id:
        default_char = char_mgr.get_default()
        char_id = default_char.id if default_char else ""
        if st is not None:
            st["character_id"] = char_id
    if not char_id or not llm_client or not llm_client.available:
        return
    try:
        cs = _conv_state(user_id, char_id)
        sched_at = float((payload or {}).get("scheduled_at") or 0)
        last_user = float(cs.get("last_user_at") or 0)
        # 用户在排队期间已经回消息 / 故事状态已结束 → 不发
        if last_user > sched_at or not cs.get("story_active"):
            return
        messages = await llm_client.generate_story_check(char_id, user_id, (payload or {}).get("context", ""))
        if not messages:
            return
        await _send_active_messages(user_id, messages, "闲聊", reason="story_check", char_id=char_id)
        logger.info(f"[故事轻唤] {user_id}/{char_id} 轻唤已发送")
    except Exception as e:
        logger.warning(f"[故事轻唤] 生成失败: {e}")


async def _maybe_daily_proactive(user_id: str, today: str):
    """联想开关（proactive_enabled）控制：好感度驱动的主动频率；
    按 (用户,角色) 独立调度——不管前台切到哪个角色、不管应用是否最小化，只要程序在跑就会按各自冷却触发"""
    if not load_runtime_settings().get("proactive_enabled", True):
        return
    for uid, cid in mem_mgr.get_user_char_pairs():
        if uid != user_id:
            continue
        await _maybe_daily_proactive_for_char(uid, cid)

async def _maybe_daily_proactive_for_char(user_id: str, char_id: str):
    if not char_id:
        return
    # M4：决策全部交给 ProactiveScheduler（冷却/上下文门/关系/耐心/心情/概率）
    decision = proactive_scheduler.evaluate(user_id, char_id)
    if not decision.allowed:
        if decision.reason == "context_gate":
            _update_conv_state(user_id, char_id, "idle")
        return
    # 生成前先登记时间（进入冷却），避免失败后立刻重试
    proactive_scheduler.record_attempt(user_id, char_id)
    await _fire_proactive(user_id, night=decision.night, char_id=char_id)

async def _maybe_daily_outline_pass(today: str):
    """每天 04:00 后为前一天生成聊天大纲（幂等：已存在则跳过）；
    后一天打开应用发现前一天缺失会自动补生成；白天还会懒生成"今天的聊天大纲"供上下文注入。"""
    try:
        now_dt = datetime.now()
        yesterday = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        for uid, cid in mem_mgr.get_user_char_pairs():
            try:
                # 前一天的大纲：每天 04:00 后补生成（次日打开也补）
                if now_dt.hour >= 4 and not mem_mgr.has_daily_outline(uid, yesterday, cid):
                    await _generate_daily_outline(uid, cid, yesterday)
                # 当天的大纲：聊够 6 条且最近 10 分钟没在聊时懒生成一次，供"今天聊了什么"注入
                if not mem_mgr.has_daily_outline(uid, today, cid):
                    await _generate_daily_outline(uid, cid, today, min_messages=6, idle_minutes=10)
                # 前一天的记忆分级：凌晨4点（或后一天空闲时）把当天聊天总结成 短期/长期 记忆
                if load_runtime_settings().get("night_memory_enabled", True) and now_dt.hour >= 4 and llm_client and llm_client.available:
                    try:
                        res = await mem_mgr.classify_day_memories(uid, cid, yesterday, llm_client.summarize_text)
                        if res.get("long") or res.get("short"):
                            logger.info(f"[记忆分级] {uid}/{cid}/{yesterday} 长{res.get('long')} 短{res.get('short')}")
                    except Exception as e:
                        logger.warning(f"[记忆分级] 处理失败 {uid}/{cid}: {e}")
            except Exception as e:
                logger.warning(f"[每日大纲] 处理失败 {uid}/{cid}: {e}")
    except Exception as e:
        logger.error(f"[每日大纲] 扫描失败: {e}")

async def _generate_daily_outline(user_id: str, char_id: str, date_str: str, min_messages: int = 4, idle_minutes: int = 0):
    """生成某一天的聊天大纲并存入 RAG（幂等由调用方保证）。"""
    key = (user_id, char_id, date_str)
    if key in _outline_inflight:
        return
    if mem_mgr.has_daily_outline(user_id, date_str, char_id):
        return
    _outline_inflight.add(key)
    try:
        msgs = mem_mgr.get_messages_for_date(user_id, date_str, char_id)
        msgs = [m for m in msgs if (m.get("content") or "").strip() and m.get("content") != "(无回复)"]
        if len(msgs) < min_messages or not llm_client or not llm_client.available:
            return
        if idle_minutes:
            try:
                last_ts = datetime.fromisoformat(msgs[-1]["timestamp"])
                if (datetime.now() - last_ts).total_seconds() < idle_minutes * 60:
                    return  # 还在聊，先不生成
            except Exception:
                pass
        convo = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content'][:300]}" for m in msgs[-80:]
        )
        prompt = (
            "你是后台记忆整理器。下面是某人与一个角色的当天微信聊天记录。"
            "请生成一份当天的聊天大纲，用第一人称（我是那个角色）口语化写 2~4 句话，"
            "大致包含：今天我和用户聊了什么话题、各自/共同的倾向与观点、气氛或关系有没有变化。"
            "不要列条、不要客套、不要复述每条消息。\n\n"
            f"日期：{date_str}\n聊天记录：\n{convo}"
        )
        outline = (await llm_client.summarize_text(prompt, max_tokens=600)).strip()
        if outline and outline not in ("无", "无内容"):
            mem_mgr.save_daily_outline(user_id, date_str, outline, char_id)
            logger.info(f"[每日大纲] {user_id} / {char_id} / {date_str} 已生成")
    finally:
        _outline_inflight.discard(key)

async def _regenerate_day_context(user_id: str, char_id: str, date_str: str = "", force: bool = False):
    """当天记忆实时归纳：把当天聊天按话题分波，生成【当天大纲】+【具体事件（带权重）】。
    触发时机：用户突然转话题 / 话题聊完闲置一段时间 / 主动消息前。开关 day_memory_enabled 控制（默认开）。"""
    if not load_runtime_settings().get("day_memory_enabled", True):
        return
    if not user_id or not char_id or not llm_client or not llm_client.available:
        return
    date_str = date_str or time.strftime("%Y-%m-%d")
    now = time.time()
    key = (user_id, char_id, date_str)
    try:
        msgs = mem_mgr.get_messages_for_date(user_id, date_str, char_id)
        msgs = [m for m in msgs if (m.get("content") or "").strip() and m.get("content") != "(无回复)"]
        if len(msgs) < 4:
            return
        last = _day_ctx_last.get(key) or 0
        if not force:
            last_msg_ts = 0
            for m in msgs:
                try:
                    ts = datetime.fromisoformat(str(m.get("timestamp") or "")[:19]).timestamp()
                    last_msg_ts = max(last_msg_ts, ts)
                except Exception:
                    pass
            if last >= last_msg_ts:
                return  # 上次归纳已覆盖到最新消息
            if now - last_msg_ts < 600:
                return  # 最近 10 分钟还在聊，等话题结束再归纳
            if now - last < 1200:
                return  # 20 分钟冷却
        elif now - last < 60:
            return  # 转话题强制重生成也要留 1 分钟最小间隔，防止话题连跳时疯狂调模型
        _day_ctx_last[key] = now
        # 分波：时间间隔 > 20 分钟算新一波话题
        waves, cur, last_ts = [], [], None
        for m in msgs:
            try:
                ts = datetime.fromisoformat(str(m.get("timestamp") or "")[:19])
            except Exception:
                ts = None
            if last_ts and ts and (ts - last_ts).total_seconds() > 1200:
                waves.append(cur)
                cur = []
            cur.append(m)
            if ts:
                last_ts = ts
        if cur:
            waves.append(cur)
        waves = [w for w in waves if len(w) >= 2]
        if not waves:
            return
        convo_parts = []
        for i, w in enumerate(waves):
            convo_parts.append(f"【第{i + 1}波话题】")
            for m in w[-24:]:
                convo_parts.append(f"{'用户' if m['role'] == 'user' else 'AI'}: {(m.get('content') or '')[:200]}")
        convo = "\n".join(convo_parts)
        prompt = (
            "你是后台记忆整理器。下面是一个人（用户）和一个角色（你）当天的微信聊天记录，已按话题分成几波。\n"
            "请严格按下面格式输出两部分：\n"
            "【大纲】\n"
            "用第一人称（我是那个角色）口语化写 2~4 句话，概括今天聊了什么话题、双方各自的倾向与观点、气氛或关系有没有变化。\n"
            "【事件】\n"
            "每一波话题里值得记住的具体事情，一条一行，格式：[权重0~1] 事件内容。\n"
            "权重规则：随口闲聊 0.15~0.25；用户明确表态、约定了什么、偏好/个人信息、值得记住的事 0.30~0.45。"
            "单条权重最高 0.45——初始权重本来就该低，用户重复提及会让它自然上涨；"
            "每波最多 5 条，把这一波值得记住的事都列出来，别把对话流水账搬进来。\n"
            "只输出这两个部分，不要解释、不要客套。\n\n"
            f"日期：{date_str}\n聊天记录：\n{convo}"
        )
        raw = (await llm_client.summarize_text(prompt, max_tokens=1000)).strip()
        if not raw:
            return
        # 解析大纲
        outline = ""
        outline_m = re.search(r"【大纲】\s*(.*?)(?:\s*【事件】|$)", raw, re.S)
        if outline_m:
            outline = outline_m.group(1).strip()[:600]
        if not outline:
            # 退路：整段第一段当大纲
            seg = raw.split("【事件】")[0].replace("【大纲】", "").strip()
            if len(seg) >= 8:
                outline = seg[:600]
        # 解析事件行 [x.xx] 内容
        events = []
        for line in raw.splitlines():
            line = line.strip()
            m = re.match(r"^[\[（(]\s*(0(?:\.\d+)?|1(?:\.0+)?)\s*[\]）)]\s*(.+)$", line)
            if not m:
                continue
            try:
                w = max(0.0, min(1.0, float(m.group(1))))
            except Exception:
                w = 0.5
            text = m.group(2).strip()
            if len(text) >= 4:
                events.append({"text": text[:200], "weight": round(w, 2), "ts": now})
        if outline:
            mem_mgr.save_daily_outline(user_id, date_str, outline, char_id)
        if events:
            mem_mgr.save_daily_events(user_id, date_str, char_id, events)
        logger.info(f"[当天记忆] {user_id}/{char_id}/{date_str} 大纲={bool(outline)} 事件={len(events)}条")
    except Exception as e:
        logger.warning(f"[当天记忆] 归纳失败: {e}")

async def _background_loop():
    """触发层后台调度：nudge/reminder 定时任务 + 主动消息频率（好感度驱动）+ 未回复失落追踪"""
    _tool_task_refs = set()

    async def _run_limited(coro):
        # 联网/找图任务耗时较长（子代理规划 + 真实搜索 + 补回复），
        # 不能阻塞背景循环逐条串行执行，否则多用户排队时回复延迟会被拉爆。
        # 联网类走 GlobalConcurrencyLimiter(tool) 子闸，避免真实搜索风暴；
        # LLM 调用由 llm_limiter（同总闸）兜底。
        from runtime.concurrency import concurrency_limiter
        async with concurrency_limiter.slot("tool"):
            try:
                await coro
            except Exception:
                pass

    def _spawn_tool_task(coro, limited=False):
        if limited:
            coro = _run_limited(coro)
        t = asyncio.create_task(coro)
        _tool_task_refs.add(t)
        t.add_done_callback(_tool_task_refs.discard)

    while True:
        try:
            await asyncio.sleep(20)
            now = time.time()
            today = time.strftime("%Y-%m-%d")
            # 遍历用户状态 + 所有已注册调度任务的用户 + 所有历史上聊过天的用户
            # （即使本次会话还没聊过天、前台没开这个对话，定时任务/主动消息也要照常触发）
            pair_users = {u for u, _ in mem_mgr.get_user_char_pairs()}
            all_uids = set(user_states.keys()) | set(_schedules_store.keys()) | pair_users
            for uid in all_uids:
                st = user_states.get(uid)
                if st is None:
                    st = user_states.setdefault(uid, {})
                # 双记忆层维护：过期短记忆遗忘、高频/高权重转长期
                try:
                    mem_mgr.expire_and_promote(uid)
                except Exception as e:
                    logger.warning(f"[记忆] 清理失败 {uid}: {e}")
                # 触发层：到期任务直接拉起
                tasks = _schedules_store.get(uid) or []
                for task in list(tasks):
                    if task.get("done") or task.get("due_at", 0) > now:
                        continue
                    task["done"] = True
                    _save_schedules()
                    if task.get("kind") == "nudge":
                        _pl = task.get("payload") or {}
                        _spawn_tool_task(_fire_nudge(uid, _pl.get("context", ""), char_id=_pl.get("char_id", ""),
                                                     attempt=int(_pl.get("attempt", 0)), total=int(_pl.get("total", 1))))
                    elif task.get("kind") == "reminder":
                        _spawn_tool_task(_fire_reminder(uid, task.get("payload") or {}))
                    elif task.get("kind") == "webcheck":
                        _spawn_tool_task(_fire_webcheck(uid, task.get("payload") or {}), limited=True)
                    elif task.get("kind") == "imagecheck":
                        _spawn_tool_task(_fire_imagecheck(uid, task.get("payload") or {}), limited=True)
                    elif task.get("kind") == "storycheck":
                        _spawn_tool_task(_fire_story_check(uid, task.get("payload") or {}), limited=True)
                # 会话状态机 idle 扫描 + 主动消息没被回 → 记失落（按角色独立）
                for _cid in [c for u, c in mem_mgr.get_user_char_pairs() if u == uid]:
                    _update_conv_state(uid, _cid, "idle")
                    _check_unanswered(uid, _cid, now)
                    # 讲故事后听众长时间没回 → 排一次轻唤（喂？/睡着了？）
                    try:
                        _maybe_schedule_story_check(uid, _cid, now)
                    except Exception as e:
                        logger.warning(f"[故事轻唤] 后台扫描失败 {uid}/{_cid}: {e}")
                    # 当天记忆实时归纳：话题聊完闲置一段时间且有新消息 → 重新生成大纲 + 事件分条
                    try:
                        await _regenerate_day_context(uid, _cid, today)
                    except Exception as e:
                        logger.warning(f"[当天记忆] 后台补归纳失败 {uid}/{_cid}: {e}")
                # 好感度驱动的主动频率
                await _maybe_daily_proactive(uid, today)
            # 每日聊天大纲：凌晨4点后补生成前一天（含次日打开补生成）；每天执行一次扫描即可
            await _maybe_daily_outline_pass(today)
        except Exception as e:
            logger.error(f"[后台调度] 出错: {e}")


__all__ = [
    "_LLMLimiter",
    "_background_loop",
    "_day_ctx_last",
    "_fire_imagecheck",
    "_fire_nudge",
    "_fire_proactive",
    "_fire_reminder",
    "_fire_webcheck",
    "_generate_daily_outline",
    "_looks_like_image_request",
    "_looks_like_search",
    "_maybe_daily_outline_pass",
    "_maybe_daily_proactive",
    "_maybe_daily_proactive_for_char",
    "_outline_inflight",
    "_push_event",
    "_regenerate_day_context",
    "_register_session",
    "_send_active_messages",
    "_session_label",
    "_subagent_plan_search",
    "_task_agent_search",
    "_task_agent_search_images",
    "channel_registry",
    "char_mgr",
    "event_queues",
    "llm_client",
    "llm_limiter",
    "mem_mgr",
    "session_registry",
]
