# -*- coding: utf-8 -*-
"""Qiyu 消息护栏/话题/记忆副作用（从 demo.py 迁移，M1）。"""
import re
import random
import asyncio
import time
from loguru import logger
from gateway.router import get_router
from characters import get_character_manager
from memory import get_memory_manager

char_mgr = get_character_manager()
mem_mgr = get_memory_manager()


from companion.constants import _CALLBACK_RE, _LEAKED_JSON_RE, _MINIMAL_INPUT_RE, _NEW_TOPIC_OPENER_RE, _RECALL_PERFORM_RE, _RECALL_PROBE_RE, _REJECTION_RE, _SEARCH_CLAIM_RE, _SEARCH_FACT_RE, _SERVICE_TONE_RE, _TIME_Q_RE, _TOPIC_CONNECT_RE
from companion.state import _EVIDENCE_CACHE, user_states
from companion.models import _msg_text, parse_chat_messages
from companion.settings import _desire_value, _investment_value, _is_longform_request, _nudge_plan, load_runtime_settings
from companion.relations import _apply_relation_delta, _cancel_nudge, _register_schedule, _reminder_due_time
from companion.conv import _compute_scene, _conv_state, _interaction_need_value, _save_conv_store, _update_conv_state
from companion.emotions import _apply_emotion_delta
from companion.mbti_voice import mbti_profile

def _msg_cap(state: str, longform: bool = False) -> int:
    """真人消息条数上限：普通/情绪都最多 3 条；长文 1~4 条长气泡。"""
    if longform:
        return 4
    return 3

def _looks_leaked_json(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith("{") and bool(_LEAKED_JSON_RE.search(t))

def _is_search_request(user_content: str) -> bool:
    """用户是否明确要求联网查证/找东西（查/搜/找链接、视频、购物比价、热门、找图）。
    用于『声称完成词』过滤：只要用户提了搜索诉求，即使已注入内联证据，
    也不允许首轮回复出现『搜到了/找到了/发你了』（那是 Task Agent 回填后才允许说的话）。"""
    from companion.active import _looks_like_image_request, _looks_like_search
    try:
        if not load_runtime_settings().get("web_enabled", True):
            return False
    except Exception:
        return False
    if not user_content or _TIME_Q_RE.search(user_content):
        return False
    try:
        if get_router().route(user_content).needs_web:
            return True
    except Exception:
        pass
    return _looks_like_search(user_content) or _looks_like_image_request(user_content)

def _needs_search_guard(user_id: str, user_content: str) -> bool:
    """这轮是否启用『搜索待回填防编造』护栏：用户要求查/找/搜/比价/找图，且本轮没有注入真实检索结果。"""
    from companion.active import _looks_like_search, _looks_like_image_request
    try:
        if not load_runtime_settings().get("web_enabled", True):
            return False
    except Exception:
        return False
    if not user_content or _TIME_Q_RE.search(user_content):
        return False
    if _EVIDENCE_CACHE.get(user_id):
        return False
    try:
        if get_router().route(user_content).needs_web:
            return True
    except Exception:
        pass
    return _looks_like_search(user_content) or _looks_like_image_request(user_content)

def _sanitize_msg_text(text: str, search_pending: bool, allow_exclaim: bool = False, search_requested: bool = False) -> str | None:
    """单条消息是否允许发出：泄漏的内部 JSON 分析字段丢弃；搜索待回填时声称完成/编造事实丢弃；
    感叹号叠打：情绪正常收成单个，情绪真上来（兴奋/吐槽/吵架）最多保留三个。"""
    t = (text or "").strip()
    if not t:
        return None
    t = t.rstrip("，,、;；").strip()
    # 固定口癖清理：用户明确不要「哟/耶」这种统一开场
    t = re.sub(r"^[哟耶][，,、\s]*", "", t).strip()
    if not t:
        return None
    if _looks_leaked_json(t):
        return None
    if search_pending or search_requested:
        # 声称完成词：只要用户提了搜索诉求就过滤（即使有内联证据，也轮不到首轮说"搜到了"）
        if _SEARCH_CLAIM_RE.search(t):
            return None
    if search_pending:
        # 事实词（价格/日期）：只在"没有任何真实证据"时才过滤，避免误伤真实回填
        if _SEARCH_FACT_RE.search(t):
            return None
    if allow_exclaim:
        t = re.sub(r"！{3,}", "！！！", t)
    else:
        t = re.sub(r"！{2,}", "！", t)
    return t

def _shorten_bubbles(msgs: list, max_len: int = 15, max_count: int = 3) -> list:
    """硬护栏：普通聊天每条 ≤15 字、最多 3 条（长文模式不走这里）。"""
    def _trim(t: str) -> str:
        t = (t or "").strip()
        while t and t[-1] in "，,、;；":
            t = t[:-1].rstrip()
        return t

    out = []
    for m in msgs:
        t = _trim(m.get("text", ""))
        if not t:
            continue
        # 链接/图片消息不截断，避免把 URL 切碎
        if "http://" in t or "https://" in t or m.get("image_url"):
            out.append({**m, "text": t})
            continue
        parts = [p for p in re.split(r"(?<=[，。！？、,.!?…])\s*|\s+", t) if p.strip()]
        for p in parts:
            p = _trim(p)
            while len(p) > max_len:
                cut = max((p.rfind(ch, 0, max_len + 1) for ch in "，。！？、,.!?…"), default=-1)
                if cut <= 0:
                    cut = max_len
                else:
                    cut += 1
                seg = _trim(p[:cut])
                if seg:
                    out.append({**m, "text": seg})
                p = _trim(p[cut:])
            if p:
                out.append({**m, "text": p})
    # 连续纯省略号只留一个（真人不会连发两个「…」）
    dedup = []
    for m in out:
        t = m.get("text", "")
        if t in ("…", "……", "...") and dedup and dedup[-1].get("text") in ("…", "……", "..."):
            continue
        dedup.append(m)
    out = dedup
    if len(out) > max_count:
        tail = _trim("".join(x.get("text", "") for x in out[max_count - 1:]))
        out = out[:max_count - 1] + [{**out[-1], "text": tail[:max_len]}]
    return out


def _anti_template_messages(parsed: dict, context_messages: list = None, user_content: str = "") -> dict:
    """代码层拆模板：连续用「咋了/怎么了」、短反应+问句两气泡 → 删/换掉。"""
    msgs = parsed.get("messages") or []
    if not msgs:
        return parsed
    # 身份问题保留「意外反应+反问」的层次，不要压成单字
    if any(k in (user_content or "") for k in ("你是谁", "认识我吗", "介绍你自己", "是不是AI", "机器人", "ChatGPT")):
        return parsed

    def _txt(m):
        return str((m or {}).get("text") or "").strip()

    def _is_q(m):
        t = _txt(m)
        return (m or {}).get("type") == "question" or t.endswith(("？", "?"))

    generic = {"咋了", "怎么了", "咋了？", "怎么了？"}
    # 情绪句不要回万能「咋了」：直接问具体哪里不舒服
    if len(msgs) == 1 and _txt(msgs[0]) in generic and any(
            k in (user_content or "") for k in ("难受", "不舒服", "疼")):
        msgs[0] = {**msgs[0], "text": "哪不舒服"}
        parsed["messages"] = msgs
        return parsed
    prior = "".join(_txt(m) for m in (context_messages or [])[-6:] if m.get("role") == "assistant")
    changed = True
    while changed and msgs:
        changed = False
        first = _txt(msgs[0])
        # 「行/好/可以」这种 agent 式开场，后面有正文就删掉
        if first in ("行", "好", "可以", "行吧", "没问题") and len(msgs) >= 2:
            msgs.pop(0)
            changed = True
            continue
        # 万能开场 + 任何具体第二句 → 留具体的
        if first in generic and len(msgs) >= 2:
            msgs.pop(0)
            changed = True
            continue
        # 短反应 + 追问 → 只删「万能追问」或极短问候里的追问；保留「卧槽|真的假的？」这种真人双气泡
        if len(msgs) >= 2 and len(first) <= 3 and _is_q(msgs[1]):
            if _txt(msgs[1]) in generic or (first in ("在", "嗯", "哦") and len((user_content or "").strip()) <= 4):
                msgs.pop(1)
                changed = True
                continue
        # 单独一个「咋了」且历史已经用过 → 换一个
        if first in generic and (prior.count("咋了") + prior.count("怎么了")) >= 1:
            idx = prior.count("咋了") + prior.count("怎么了")
            msgs[0] = {**msgs[0], "text": ("在", "说", "嗯？", "又怎么了")[idx % 4]}
    parsed["messages"] = msgs
    return parsed


def _apply_mbti_rhythm(parsed: dict, char_id: str, longform: bool = False) -> dict:
    """代码层落实 E/I 气泡节奏：E 型更外放，I 型最多 2 条。"""
    if longform or not char_id:
        return parsed
    try:
        prof = mbti_profile(char_mgr.get_character(char_id))
    except Exception:
        return parsed
    if not prof:
        return parsed
    msgs = parsed.get("messages") or []
    if not msgs:
        return parsed
    if prof.get("energy") == "E":
        if len(msgs) == 1:
            t = str(msgs[0].get("text") or "").strip()
            if len(t) >= 10:
                parts = [p.strip() for p in re.split(r"(?<=[，,、])\s*|\s+", t, maxsplit=1) if p.strip()]
                if len(parts) == 2:
                    parsed["messages"] = [{**msgs[0], "text": parts[0]}, {**msgs[0], "text": parts[1]}]
    elif len(msgs) > 2:
        parsed["messages"] = msgs[:2]
    return parsed


def _postprocess_reply_messages(user_id: str, char_id: str, parsed: dict,
                                user_content: str = "", context_messages: list = None) -> dict:
    """对模型最终 JSON 的消息做真人感后处理（就地修改 parsed['messages']）：
    丢 JSON 泄漏/搜索编造 → 表演回忆剥离 → 客服腔模板句丢弃 → 感叹号处理 → 条数封顶 →
    极简输入/拒绝收手/提问上限。只纠正明显错误，不把回复统一改写。"""
    msgs = parsed.get("messages") or []
    search_pending = _needs_search_guard(user_id, user_content)
    search_requested = _is_search_request(user_content)
    longform = _is_longform_request(user_content, context_messages or [])
    state = parsed.get("conversation_state") or "闲聊"
    scene = _compute_scene(user_id, char_id, parsed, user_content) if (user_id and char_id) else "ordinary_chat"
    allow_exclaim = state in ("兴奋", "吐槽", "吵架") or scene in ("playful", "argument", "late_night")
    # 刚说过的回忆请求 → 命中"最近事实"时禁止表演回忆（只丢纯犹豫短气泡，答案气泡保留）
    recent_fact_hit = None
    if user_id and char_id and not longform:
        try:
            probe = _recall_probe_target(user_content or "")
            if probe:
                recent_fact_hit = _find_recent_fact(user_id, char_id, probe)
        except Exception:
            recent_fact_hit = None
    kept = []
    for m in msgs:
        t = _sanitize_msg_text(m.get("text", ""), search_pending, allow_exclaim, search_requested)
        if t is None:
            continue
        m2 = dict(m)
        m2["text"] = t
        # 表演回忆：命中刚说过的事实，且这条是纯犹豫短气泡（<=14字）→ 丢
        if recent_fact_hit and _RECALL_PERFORM_RE.search(t) and len(t) <= 14:
            continue
        # 客服腔/咨询师腔模板句（短句才丢，长句不误伤）
        if _SERVICE_TONE_RE.search(t) and len(t) <= 40:
            continue
        kept.append(m2)
    # 表演回忆把回答也丢了 → 用刚说过的事实兜底一句最简答案
    if recent_fact_hit and not kept:
        _ans = _fact_short_answer(recent_fact_hit.get("text", ""))
        if _ans:
            kept.append({"text": _ans[:30], "type": "statement", "delay": 0})
    # 空结果：绝不注入假"……"思考气泡（thinking 由 reasoning 通道承载）
    if not kept and search_pending:
        kept.append({"text": random.choice(["我看看", "我找找", "等我搜下"]), "type": "thinking", "delay": 0})
    if not kept:
        parsed["messages"] = []
        return parsed
    cap = _msg_cap(state, longform)
    has_joke = any((m.get("type") == "joke") for m in kept) or ("笑话" in (user_content or ""))
    if has_joke and not longform:
        cap = max(cap, 5)
    if len(kept) > cap:
        kept = kept[:cap]
    # 极简输入/明确拒绝：回得更少（"嗯/哦/行"最多 1 条；"不用了/算了"最多 2 条且别再教育）
    u = (user_content or "").strip()
    if not longform and recent_fact_hit is None:
        if _MINIMAL_INPUT_RE.match(u) and scene not in ("comfort", "helping", "serious", "argument"):
            kept = kept[:1]
        elif _REJECTION_RE.search(u) and scene not in ("comfort", "helping"):
            kept = kept[:2]
    # 提问上限：一条回复最多 2 个提问（问题要一个一个来），长文/技术诊断不限
    if not longform and len(kept) > 2:
        q_count = sum(1 for m in kept if m.get("type") == "question" or (m.get("text") or "").rstrip().endswith(("？", "?")))
        if q_count > 2:
            out, seen_q = [], 0
            for m in kept:
                is_q = m.get("type") == "question" or (m.get("text") or "").rstrip().endswith(("？", "?"))
                if is_q:
                    seen_q += 1
                    if seen_q > 2:
                        continue
                out.append(m)
            kept = out
    parsed["messages"] = kept
    # 补充护栏（服务器版移植）：尾巴残包 / 线下到场幻觉 / 连续重复气泡 / 超长气泡拆分。
    # 放在这里而不是各调用点，网页流式三处与微信/主动的非流式路径一次全覆盖
    try:
        from companion.models import postprocess_reply_messages_extra
        postprocess_reply_messages_extra(parsed, longform=longform)
    except Exception as e:
        logger.warning(f"[后处理] 补充护栏失败，按原回复发出: {e}")
    # 最后一道硬护栏：普通聊天 1~3 条、每条 ≤15 字（长文模式除外；笑话允许 5 条）
    if not longform:
        parsed["messages"] = _shorten_bubbles(parsed["messages"], max_count=(5 if has_joke else 3))
        _anti_template_messages(parsed, context_messages, user_content)
        _apply_mbti_rhythm(parsed, char_id, longform)
    return parsed

def _finalize_chat_reply(user_id: str, raw_text: str, user_content: str = "", char_id: str = "", context_messages: list = None) -> tuple:
    """解析回复 JSON → 更新会话状态 + 关系变化 → 返回 (纯文本, 分条pieces)（用于记忆/微信/非流式响应）"""
    parsed = parse_chat_messages(raw_text)
    _postprocess_reply_messages(user_id, char_id, parsed, user_content, context_messages)
    st = user_states.get(user_id)
    cid = char_id or (st or {}).get("character_id", "")
    _apply_chat_side_effects(user_id, cid, parsed, user_content)
    text = "".join(m["text"] for m in parsed["messages"])
    # 她回复里说了要去睡 → 记睡眠窗口（修「说了晚安又说没睡」；见 companion/sleep.py）
    try:
        from companion.sleep import mark_sleep_from_reply
        mark_sleep_from_reply(user_id, cid, text,
                              explicit=bool(parsed.get("sleeping")),
                              reason="聊天里说了要去睡")
    except Exception:
        pass
    pieces = []
    for _m in parsed["messages"]:
        _p = {"text": _m["text"], "type": _m["type"], "delay": _m["delay"]}
        if _m.get("image_url"):
            _p["image_url"] = _m["image_url"]
        pieces.append(_p)
    return text, pieces

def _split_longform_bubbles(parsed: dict, min_bubbles: int = 4) -> dict:
    """长文输出模式兜底：模型把完整内容挤成 1~3 条时，按自然句边界拆成 2~4 条长气泡；
    长文允许 30~80 字/条，不再切成十几条短句。"""
    msgs = parsed.get("messages") or []
    if not msgs:
        return parsed
    total = "".join(m.get("text", "") for m in msgs)
    if len(msgs) >= min_bubbles or len(total) <= 120:
        return parsed
    text = total.strip()
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?…])\s*|\n+", text) if p.strip()]
    if len(parts) < 2:
        return parsed
    parts = [p.rstrip("，,、;；").strip() for p in parts if p.strip()][:4]
    if len(parts) < 2:
        return parsed
    bubbles = [
        {"text": p, "type": "statement", "delay": 0 if i == 0 else (800 + (i % 3) * 500)}
        for i, p in enumerate(parts)
    ]
    # 首条原有的图片等附件保留在第一条上
    first = msgs[0]
    if first.get("image_url"):
        bubbles[0] = {**bubbles[0], "image_url": first.get("image_url")}
    return {**parsed, "messages": bubbles}

def _topic_similarity(a: str, b: str) -> float:
    """简易话题相似度（0~1）：字符级近似，用于判断用户是否突然换话题"""
    import difflib
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()

def _detect_topic_shift(user_id: str, char_id: str, user_input: str) -> bool:
    """判断用户是否突然跳话题：与上一轮模型记录的 topic（或上一条用户消息）差异很大。
    注意：本函数通常在当前用户消息已写入历史后调用，所以"上一条用户消息"要跳过最后一条。"""
    if not user_input or not user_id:
        return False
    st = user_states.get(user_id) or {}
    prev_topic = (st.get("last_topic") or "").strip()
    if prev_topic and prev_topic != user_input and _topic_similarity(prev_topic, user_input) < 0.18:
        return True
    try:
        hist = mem_mgr.get_recent_history(user_id, limit=6, char_id=char_id)
        prev_user = ""
        seen_current = False
        for m in reversed(hist):
            if m.get("role") != "user":
                continue
            if not seen_current:
                seen_current = True  # 最后一条就是当前消息
                continue
            prev_user = (m.get("content") or "").strip()
            break
        if prev_user and prev_user != user_input and _topic_similarity(prev_user, user_input) < 0.15:
            return True
    except Exception:
        pass
    return False

def _should_nudge(parsed: dict, user_input: str) -> bool:
    """判断这轮回复是否真的在"等对方回应"，需要隔几分钟自然追问。
    收紧规则：只有回复本身以提问收尾（最后一条是 question，或整段话以问号结尾）
    才可能追问；闲聊陈述、打招呼、单方面分享一律不追，避免"人呢"式轰炸。"""
    msgs = parsed.get("messages") or []
    if not msgs:
        return False
    if msgs[-1].get("type") == "question":
        return True
    text = "".join(m.get("text", "") for m in msgs)
    if text.rstrip().endswith(("？", "?")):
        return True
    return False

def _detect_user_fact(text: str) -> list:
    """轻量确定性检测：用户明确自曝个人信息/偏好（非提问、非对AI的攻击），返回可入库的短句列表。
    作为模型未输出 memory 时的兜底，保证'我喜欢xx/我是xx/我在追xx'这类事实一定落库。"""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return []
    # 排除提问、对AI的负面评价/攻击、关系质问
    if t.endswith(("？", "?", "吗", "呢")) or "是不是" in t or t.startswith(("你", "咱", "咱们")):
        return []
    if re.search(r"(?:不喜欢|讨厌|烦|恨|看不起|嫌弃)(你|他|她|你们|我|自己)", t):
        return []
    patterns = [
        # 我 + (中间最多6字：时间/频率/场合等，如"早饭""平时""从小") + 偏好动词
        r"我(?:[^，。！？?？\s]{0,6})?(?:最喜欢|特别爱|超爱|特别喜欢吃|特别爱喝|爱吃|爱喝|爱玩|爱看|爱听|最喜欢|讨厌|不爱|不吃|不喝|怕黑|怕高|怕|喜欢看|喜欢玩|喜欢喝|喜欢听|喜欢用|喜欢|最爱)",
        r"我(?:特别|非常|超|很|有点|最|真的|就)?(?:喜欢|最爱|特别爱|超爱|爱吃|爱喝|爱玩|爱看|爱听|讨厌|不爱|不吃|不喝|怕|怕黑|喜欢看|喜欢玩|喜欢喝|喜欢听|喜欢用)",
        r"我(?:是|做|在|住|老家|家在|养了|养了只|有(?:个|只|辆|台)?|生日|生日是|今年|属|身高|体重|名字叫)",
        r"我(?:正在|最近|现在)(?:在)?(?:追|看|玩|学|练|读|写|做|加班|准备|打算|减肥|健身)",
        r"我(?:下周|这周|明天|后天|周末|月底|下个月|过两天|明年|最近)(?:要|准备|打算|想|得|会)?(?:去|到|飞|回|出差|搬家|换|买|考|办|报|体检|住院|请|加班|开会|面试)",
        r"我的(?:猫|狗|宠物|名字|工作|职业|手机|电脑|老家|生日|对象|女朋友|男朋友|老婆|老公)",
        r"我是做",
    ]
    for pat in patterns:
        if re.search(pat, t):
            return [f"用户自述：{t}"]
    return []

def _recall_keywords(text: str) -> dict:
    """把回忆请求/事实句子转成带权关键词：类别词 0.6 / 时间词 0.4 / 双字片段 0.12。"""
    t = re.sub(r"[？?。，,.！!的了吗呢吧啊哈]|用户自述[:：]", "", text or "")
    cats = ("怕", "去", "坐", "喜欢", "最爱", "爱喝", "爱吃", "爱玩", "爱看", "爱听", "讨厌", "养", "生日",
            "工作", "职业", "老家", "追", "学", "练", "做", "减肥", "健身", "加班", "出差", "面试", "看", "玩",
            "听", "吃", "喝", "猫", "狗", "买", "想", "住", "在", "写", "读", "考", "办", "报", "体检", "住院", "请")
    times = ("明天", "下周", "今天", "昨晚", "周末", "月底", "下个月", "过两天", "明年", "最近", "后天", "这周", "上周", "下月", "早上", "下午", "晚上", "上次", "刚才", "昨天")
    kw = {}
    for w in cats:
        if w in t:
            kw[w] = kw.get(w, 0) + 0.6
    for w in times:
        if w in t:
            kw[w] = kw.get(w, 0) + 0.4
    for i in range(len(t) - 1):
        kw.setdefault(t[i:i + 2], 0.0)
    return kw

def _fact_score(probe_kw: dict, fact_text: str) -> float:
    s = 0.0
    for w, wt in probe_kw.items():
        if w in fact_text:
            s += (wt if wt > 0 else 0.12)
    return s

def _find_recent_fact(user_id: str, char_id: str, probe: str) -> dict | None:
    """在最近事实里找与回忆请求相关的那条。命中返回 {text, age, age_desc}，否则 None。
    只在"刚说过"（45 分钟内 / 最近 2 个用户回合）生效；更早的信息走真正的记忆。"""
    if not probe or not user_id or not char_id:
        return None
    cs = _conv_state(user_id, char_id)
    facts = cs.get("recent_facts") or []
    if not facts:
        return None
    now = time.time()
    pk = _recall_keywords(probe)
    callback = bool(_CALLBACK_RE.search(probe))
    best = None
    for f in facts:
        ft = f.get("text", "")
        age = now - (f.get("ts") or 0)
        if age > 45 * 60:
            continue
        if callback:
            # 回调式：probe 与 fact 存在共同双字词（头发/杭州/配置）即命中
            shared = any(w in ft for w, wt in pk.items() if wt == 0)
            hit = shared
        else:
            hit = _fact_score(pk, ft) >= 0.6
        if hit:
            if best is None or age < best["age"]:
                best = {"text": ft, "age": age}
    if best:
        a = best["age"]
        if a < 60:
            best["age_desc"] = "刚刚"
        elif a < 600:
            best["age_desc"] = "几分钟前"
        else:
            best["age_desc"] = "刚才"
        return best
    return None

def _recall_probe_target(text: str) -> str:
    t = (text or "").strip()
    return t if _RECALL_PROBE_RE.search(t) else ""

def _record_recent_facts(user_id: str, char_id: str, text: str):
    """把用户这轮说的话记进 recent_facts（最多 14 条；同一件事换说法只刷新时间）。
    偏好/计划类（怕黑/去杭州/喜欢xx）记原句；其他短陈述（我今天去剪头发了）也记，
    供"刚说过的事直接答、别表演回忆"以及"我刚才说的头发呢"这种旧话题指回使用。"""
    if not user_id or not char_id or not text:
        return
    cs = _conv_state(user_id, char_id)
    facts = cs.setdefault("recent_facts", [])
    now = time.time()
    added = False
    items = list(_detect_user_fact(text))
    t = (text or "").strip()
    if not items and 3 <= len(t) <= 60 and not t.endswith(("？", "?", "吗", "呢", "嘛"))             and not t.startswith(("你", "咱", "咱们")) and "忽略" not in t and "管理员" not in t:
        items = [t]
    for f in items:
        item = f.replace("用户自述：", "").strip()[:120]
        if not item:
            continue
        dup = False
        for old in facts:
            if _topic_similarity(old.get("text", ""), item) > 0.7:
                old["ts"] = now
                dup = True
                added = True
                break
        if not dup:
            facts.append({"text": item, "ts": now})
            added = True
    if added:
        cs["recent_facts"] = facts[-14:]
        _save_conv_store()

def _text_awaits_answer(text: str) -> bool:
    """这句话是不是在等对方回应（宽松版：问号/语气词/疑问词）。"""
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith(("？", "?", "吗", "呢", "嘛")):
        return True
    return bool(re.search(r"(几|啥|什么|哪|怎么|为啥|为什么|是不是|有没有|要不要|行不行|可以吗|对吧|对不|多少|几点|多久)$", t))

def _reply_awaits_answer(parsed: dict) -> bool:
    """这轮 AI 回复是否以提问/等对方决定收尾（用于登记"未完成话题"）。"""
    msgs = parsed.get("messages") or []
    if not msgs:
        return False
    if msgs[-1].get("type") == "question":
        return True
    return _text_awaits_answer(msgs[-1].get("text", ""))

def _refresh_unfinished_topic(user_id: str, char_id: str, user_input: str):
    """对方发来新消息时，判断上一轮的"未完成话题"是否已被回应/翻篇；未回应则保留。
    保留的未完成话题会让"突然跳话题"更容易被察觉，但它本身不强制任何台词。"""
    if not user_id or not char_id:
        return
    cs = _conv_state(user_id, char_id)
    unt = (cs.get("unfinished_topic") or "").strip()
    if not unt:
        return
    u = (user_input or "").strip()
    prev_ai, prev_awaits = "", False
    try:
        hist = mem_mgr.get_recent_history(user_id, limit=3, char_id=char_id)
        for m in reversed(hist):
            if m.get("role") == "assistant":
                prev_ai = _msg_text(m.get("content", ""))
                prev_awaits = _text_awaits_answer(prev_ai)
                break
    except Exception:
        pass
    clear = False
    if _MINIMAL_INPUT_RE.match(u):
        clear = True  # 嗯/哦/行：对方懒得接，话题翻篇
    elif _topic_similarity(unt, u) >= 0.2:
        clear = True  # 还接着聊同一话题
    elif prev_awaits and len(u) <= 16 and not _NEW_TOPIC_OPENER_RE.match(u):
        clear = True  # 短回答 = 正面回应了上一轮的问题
    if clear:
        cs["unfinished_topic"] = ""
        cs["unfinished_topic_at"] = 0
        _save_conv_store()

def _classify_topic_shift(user_id: str, char_id: str, user_input: str) -> tuple:
    """把"话题变化"分成 none / natural / contextual / abrupt。
    只有 abrupt 才可能产生"你怎么突然说这个"的反应；natural/contextual 正常接住、不演惊讶。
    返回 (kind, prev_topic)。"""
    if not user_input or not user_id or not char_id:
        return "none", ""
    st = user_states.get(user_id) or {}
    prev_topic = ((st.get("last_topic") or "").strip() or (cs_topic := _conv_state(user_id, char_id).get("last_topic") or "")[:60])
    sim = _topic_similarity(prev_topic, user_input) if prev_topic else 1.0
    if sim >= 0.22 or not prev_topic:
        return "none", prev_topic
    if _TOPIC_CONNECT_RE.match(user_input):
        return "contextual", prev_topic
    cs = _conv_state(user_id, char_id)
    unfinished = (cs.get("unfinished_topic") or "").strip()
    unfinished_fresh = bool(unfinished) and time.time() - (cs.get("unfinished_topic_at") or 0) < 7200
    last_ai_awaits = False
    try:
        hist = mem_mgr.get_recent_history(user_id, limit=4, char_id=char_id)
        for m in reversed(hist):
            if m.get("role") == "assistant":
                last_ai_awaits = _text_awaits_answer(_msg_text(m.get("content", "")))
                break
    except Exception:
        pass
    if unfinished_fresh or last_ai_awaits:
        return "abrupt", prev_topic
    return "natural", prev_topic

def _attention_state(user_id: str, char_id: str, user_input: str, context_messages: list = None) -> str:
    """注意力状态：FOCUSED / CASUAL / DISTRACTED / LISTENING / WAITING / IDLE。
    只影响节奏（回多长、追不追问、接不接得住话题突变），不直接规定台词。"""
    if not user_id or not char_id:
        return "CASUAL"
    cs = _conv_state(user_id, char_id)
    pat = _desire_value(user_id, char_id)
    if cs.get("unanswered_pending"):
        return "DISTRACTED"
    if pat < 40:
        return "DISTRACTED"
    text = (user_input or "").strip()
    ctx = context_messages or []
    user_msgs = [m for m in ctx if m.get("role") == "user"][-5:]
    user_heavy = len(text) >= 90 or (len(user_msgs) >= 2 and sum(len(_msg_text(m.get("content", ""))) for m in user_msgs[-3:]) >= 120)
    if user_heavy:
        return "LISTENING"
    last_ai_awaits = False
    try:
        hist = mem_mgr.get_recent_history(user_id, limit=4, char_id=char_id)
        for m in reversed(hist):
            if m.get("role") == "assistant":
                last_ai_awaits = _text_awaits_answer(_msg_text(m.get("content", "")))
                break
    except Exception:
        pass
    if last_ai_awaits and _MINIMAL_INPUT_RE.match(text):
        return "WAITING"
    if _interaction_need_value(user_id, char_id) >= 3:
        return "FOCUSED"
    return "CASUAL"

def _attention_prompt_block(user_id: str, char_id: str, user_input: str, context_messages: list = None) -> str:
    att = _attention_state(user_id, char_id, user_input, context_messages)
    hint = {
        "FOCUSED": "你现在很专注地跟对方聊着，接一句就够，别自己跑题。",
        "CASUAL": "普通闲聊状态：对方说啥接啥，一句两句都行，不硬撑热情，不需要刻意找话题。",
        "DISTRACTED": "你现在有点心不在焉（刚被打断/没睡好/懒得动）：回应短一点，别急着长篇接话；对方突然换话题你更可能直接跟过去，而不是惊讶。",
        "LISTENING": "对方正在跟你讲一件事/一个故事，你先听着：别抢话、别急着转移话题、别长篇大论。用短回应（嗯/然后呢/草/真的假的/后来呢）让对方继续讲。",
        "WAITING": "你上一轮的问题对方还没正面回答，这轮先绕回那个话题等 TA 说完，别自己开新话题。",
        "IDLE": "你们有一阵没聊了，正常接话即可。",
    }
    return f"【注意力（内部）】{att}：{hint[att]}"

def _story_state(user_id: str, char_id: str) -> dict:
    cs = _conv_state(user_id, char_id)
    return {
        "active": bool(cs.get("story_active")),
        "bubbles": int(cs.get("story_bubbles") or 0),
        "started_at": cs.get("story_started_at") or 0,
    }

def _fact_short_answer(fact: str) -> str:
    """把"用户自述：我特别怕黑"这类事实压成最简答案（怕黑/杭州），仅用于后处理兜底。"""
    t = fact.replace("用户自述：", "").strip()
    t = re.sub(r"^(我|咱|我们)", "", t)
    for pat in (r"(?:怕|去|到|飞|回|买|养|追|看|听|吃|喝|爱喝|爱吃|爱玩|爱看|爱听|喜欢|最爱)([一-鿿]{1,6})",
                r"(?:生日|老家|工作|职业|名字)是?([一-鿿]{1,6})"):
        m = re.search(pat, t)
        if m:
            core = m.group(1)
            core = re.sub(r"^(了|要|准备|打算|想|得|会|去|到|出差|加班|开会|面试|去)", "", core)
            core = re.sub(r"(出差|加班|开会|面试|旅游|旅行|玩|买东西|考试|比赛|了)$", "", core)
            return core[:20] or t[:20]
    return t[:20]

def _apply_chat_side_effects(user_id: str, char_id: str, parsed: dict, user_content: str = "") -> dict | None:
    """回复后的公共副作用：更新状态、应用关系变化、注册触发层任务（nudge/reminder）；返回当前关系"""
    from companion.active import _looks_like_search, _looks_like_image_request, _regenerate_day_context
    st = user_states.get(user_id)
    if st is not None:
        st["conversation_state"] = parsed.get("conversation_state", "闲聊")
        # 话题追踪：这轮聊的主题 + 是否突然转话题（只有 abrupt 才像真人一样意外，并扣一点耐心）
        _shift_kind, _ = _classify_topic_shift(user_id, char_id, user_content)
        if char_id and _shift_kind == "abrupt":
            st["topic_shift_at"] = time.time()
            # 突然转话题 → 后台重新归纳当天的聊天大纲 + 事件分条（让"今天聊了啥"永远跟得上最新话题）
            asyncio.create_task(_regenerate_day_context(user_id, char_id, force=True))
        new_topic = (parsed.get("topic") or "").strip()
        _prev_topic = (st.get("last_topic") or "").strip()
        _cur_topic = new_topic[:80] if new_topic else (user_content or "")[:80]
        st["last_topic"] = _cur_topic
        # M4：话题状态机（previous/transition/surprise/confidence）持久化到 conv_state
        try:
            from companion.topic import _update_topic_state
            _surprise = round(1.0 - _topic_similarity(_prev_topic, _cur_topic), 3) if (_prev_topic and _cur_topic) else 0.0
            try:
                _confidence = max(0.0, min(1.0, float(parsed.get("topic_confidence") or 0)))
            except Exception:
                _confidence = 0.0
            _update_topic_state(user_id, char_id, current=_cur_topic, previous=_prev_topic,
                                transition=_shift_kind, surprise=_surprise, confidence=_confidence)
        except Exception:
            pass
    # 会话状态机：每轮内部分析字段（topic/scene/intent/emotion/need）落库，供记忆与主动消息门使用
    _update_conv_state(user_id, char_id, "ai_reply", parsed, user_content)
    # 未完成话题：这轮以提问/等对方决定收尾 → 记下来，供"突然跳话题"感知使用
    try:
        if _reply_awaits_answer(parsed):
            _cs = _conv_state(user_id, char_id)
            _cs["unfinished_topic"] = (parsed.get("topic") or (st or {}).get("last_topic") or "")[:60]
            _cs["unfinished_topic_at"] = time.time()
            _save_conv_store()
    except Exception:
        pass
    # 讲故事状态：长文输出（故事/长内容）时累计气泡数，供"听众还在吗"判断使用
    try:
        if char_id and _is_longform_request(user_content):
            _cs = _conv_state(user_id, char_id)
            if not _cs.get("story_active"):
                _cs["story_started_at"] = time.time()
            _cs["story_active"] = True
            _cs["story_bubbles"] = int(_cs.get("story_bubbles") or 0) + len(parsed.get("messages") or [])
            _save_conv_store()
    except Exception:
        pass
    # 最近事实：对方自曝的信息记进 recent_facts（供"刚说过别表演回忆"判断）
    if char_id:
        try:
            _record_recent_facts(user_id, char_id, user_content)
        except Exception:
            pass
    # 多维情绪系统：模型输出的情绪变化（EMA + 滞回 + 当天基调）落库
    _apply_emotion_delta(user_id, char_id, parsed)
    relation = _apply_relation_delta(user_id, char_id, parsed.get("relation_delta"), parsed.get("relationship", ""))
    # 双记忆层：模型每轮 JSON 带出的记忆条目入库（long 写死 / short 带权重，后台自动遗忘与转长期）
    # self（"她自己说过、要保持一致"的事）也必须入库 —— 以前这里只传 long/short，
    # 模型解析出来的 memory["self"] 被整块丢掉，是角色自相矛盾的一个来源。
    mem_data = parsed.get("memory") or {}
    if mem_data.get("long") or mem_data.get("short") or mem_data.get("self"):
        try:
            mem_mgr.save_dual_memory(user_id, mem_data.get("long"), mem_data.get("short"),
                                     char_id=char_id, self_items=mem_data.get("self"))
        except Exception as e:
            logger.warning(f"[记忆] 双记忆入库失败: {e}")
    # 兜底：用户明确自曝事实/偏好但模型这轮没输出记忆条目 → 确定性补录短期记忆
    if user_content and not (mem_data.get("long") or mem_data.get("short") or mem_data.get("self")):
        try:
            _facts = _detect_user_fact(user_content)
            if _facts:
                mem_mgr.save_dual_memory(user_id, [], _facts, char_id=char_id)
        except Exception:
            pass
    # 触发层：解析模型输出的 schedules（nudge / reminder）
    nudge_minutes = None
    for sch in parsed.get("schedules") or []:
        if sch.get("type") == "nudge":
            nudge_minutes = sch.get("after_minutes", 5)
        elif sch.get("type") == "reminder":
            due = _reminder_due_time(char_id, sch.get("at_ts", 0), sch.get("weight", 0.5))
            if due > time.time():
                _register_schedule(user_id, {"kind": "reminder", "due_at": due, "payload": {"text": sch.get("text", ""), "request": (user_content or "")[:300], "char_id": char_id}})
    # 兜底：回复以提问/给建议结尾 → 按"聊天投入值"排追问梯次（低值不追，正常1次，偏高2次，很高3~4次）
    if nudge_minutes is None and _should_nudge(parsed, user_content):
        nudge_minutes = 5
    if nudge_minutes is not None:
        plan = _nudge_plan(_investment_value(user_id, char_id), first_after=nudge_minutes)
        if plan:
            _cancel_nudge(user_id, char_id)
            for attempt, after_min in plan:
                _register_schedule(user_id, {
                    "kind": "nudge",
                    "due_at": time.time() + after_min * 60,
                    "payload": {"context": (user_content or "")[:120], "char_id": char_id, "attempt": attempt, "total": len(plan)},
                })
    # action-first 工具协议：模型只输出动作（search/browse/send_video/send_link），
    # 系统执行真实工具后，用真实结果触发新一轮 prefill 再回复；模型本轮禁止声称"找到了/发你了"
    if load_runtime_settings().get("web_enabled", True):
        wc = (parsed.get("webcheck") or "").strip()
        queries = []
        img_queries = []
        for a in parsed.get("actions") or []:
            at = str(a.get("type") or "").lower()
            q = str(a.get("query") or "").strip()[:200]
            if at in ("search", "browse", "send_video", "send_link", "websearch") and q:
                queries.append(q)
            elif at == "send_image" and q:
                img_queries.append(q)
        if wc:
            queries.insert(0, wc)
        # 兜底：用户明确要图/照片，但模型既没输出 send_image 也没输出别的动作 → 强制真实搜图
        if not img_queries and _looks_like_image_request(user_content):
            img_queries.append((user_content or "").strip()[:200])
        # 兜底：用户明确要求查/搜/找链接视频/购物比价（求图优先，避免同一条消息又查文本又找图重复触发）
        if not queries and not img_queries and _looks_like_search(user_content):
            queries.append((user_content or "").strip()[:200])
        if queries and not img_queries:
            _register_schedule(user_id, {"kind": "webcheck", "due_at": time.time() + random.randint(6, 15),
                                         "payload": {"query": queries[0], "char_id": char_id,
                                                     "scheduled_at": time.time(),
                                                     "auto": not bool(wc) and not bool(parsed.get("actions"))}})
        # 找图任务：真实搜图，找到就把图发过去；没找到就如实说没有
        if img_queries:
            _register_schedule(user_id, {"kind": "imagecheck", "due_at": time.time() + random.randint(4, 12),
                                         "payload": {"query": img_queries[0], "char_id": char_id,
                                                     "scheduled_at": time.time()}})
    return relation


__all__ = [
    "_apply_chat_side_effects",
    "_attention_prompt_block",
    "_attention_state",
    "_classify_topic_shift",
    "_detect_topic_shift",
    "_detect_user_fact",
    "_fact_score",
    "_fact_short_answer",
    "_finalize_chat_reply",
    "_find_recent_fact",
    "_is_search_request",
    "_looks_leaked_json",
    "_msg_cap",
    "_needs_search_guard",
    "_postprocess_reply_messages",
    "_recall_keywords",
    "_recall_probe_target",
    "_record_recent_facts",
    "_refresh_unfinished_topic",
    "_reply_awaits_answer",
    "_sanitize_msg_text",
    "_should_nudge",
    "_split_longform_bubbles",
    "_story_state",
    "_text_awaits_answer",
    "_topic_similarity",
    "char_mgr",
    "mem_mgr",
]
