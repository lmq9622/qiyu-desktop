# -*- coding: utf-8 -*-
"""Qiyu 数据模型与消息协议解析（从 demo.py 迁移，M1）。"""
import re
import json
import time
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel


from companion.constants import CHAT_MSG_TYPES, CHAT_STATES, EMOTION_KEYS, SCENES

class ChatMessage(BaseModel):
    role: str
    content: str

def _msg_text(content) -> str:
    """把 OpenAI 多模态消息 content（str 或 list 内容块）规整为纯文本"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image_url":
                    parts.append("[图片]")
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str = "demo"
    temperature: Optional[float] = None
    stream: bool = False
    user: str = "demo_user"
    use_memory: bool = True
    use_rag: bool = True
    images: Optional[list] = None

class CreateCharacterRequest(BaseModel):
    id: Optional[str] = None
    name: str
    tagline: Optional[str] = ""
    description: Optional[str] = ""
    persona: Optional[str] = None
    temperature: float = 0.7
    keywords: Optional[list] = None
    tone: Optional[str] = "自然"
    avatar_color: Optional[str] = None
    avatar: Optional[str] = None
    resume: Optional[dict] = None
    persona_params: Optional[dict] = None
    soul: Optional[str] = ""        # 灵魂：价值观/气质（最稳定层）
    boundaries: Optional[str] = ""  # 红线：硬约束（优先级最高）
    user_profile: Optional[str] = None

def parse_resume(text: str) -> dict:
    """把结构化角色简历文本解析成 dict（按【字段名】行切分），供 RAG / 调度读取"""
    canonical = {
        "基本信息": "基本信息",
        "虚构生活环境": "生活环境",
        "生活环境": "生活环境",
        "性格细节": "性格",
        "性格": "性格",
        "虚构背景": "背景",
        "背景": "背景",
        "爱好与日常": "爱好日常",
        "爱好日常": "爱好日常",
        "遇到问题时的反应": "问题应对",
        "问题应对": "问题应对",
        "与用户的关系": "关系定位",
        "关系定位": "关系定位",
        "调度标签": "调度标签",
        "对话自我表述": "对话自我表述",
        "数据关键词": "数据关键词",
        "角色参数": "角色参数",
    }
    resume = {}
    if not text:
        return resume
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 去掉行首编号，如 "1. " / "1、"
        m = re.match(r"^\d+[\.\)、]\s*", line)
        if m:
            line = line[m.end():]
        field = None
        content = None
        # 格式1：【字段名】内容
        m = re.match(r"^【(.+?)】\s*(.*)$", line)
        if m:
            field, content = m.group(1), m.group(2)
        else:
            # 格式2：**字段名**：内容
            m = re.match(r"^\*\*(.+?)\*\*\s*[:：]\s*(.*)$", line)
            if m:
                field, content = m.group(1), m.group(2)
            else:
                # 格式3：字段名：内容（仅当字段名是已知字段）
                m = re.match(r"^(.+?)[:：]\s*(.+)$", line)
                if m and m.group(1).strip() in canonical:
                    field, content = m.group(1).strip(), m.group(2)
        if not field:
            continue
        canonical_field = canonical.get(field.strip())
        if not canonical_field:
            continue
        content = (content or "").strip().strip("|").strip()
        if canonical_field in resume and content:
            resume[canonical_field] += " | " + content
        elif content:
            resume[canonical_field] = content
    return resume

def resume_to_text(resume: dict) -> str:
    """把简历 dict 还原成结构化文本（用于写入 RAG 知识库）"""
    return "\n".join(f"【{k}】{v}" for k, v in resume.items() if v)

def parse_persona_params(text: str) -> dict:
    """从简历文本中解析【角色参数】为结构化 dict（MBTI / 滑块数值 / 关系）"""
    params = {}
    for line in (text or "").splitlines():
        line = line.strip()
        m = re.match(r"^\d+[\.\)、]\s*", line)
        if m:
            line = line[m.end():]
        m = re.match(r"^【角色参数】\s*(.*)$", line)
        if not m:
            continue
        for part in m.group(1).split("|"):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k.upper().startswith("MBTI"):
                    params["mbti"] = v.upper()[:4]
                elif k == "反驳阈值":
                    params["rebut"] = _to_int(v, 50)
                elif k == "主见值":
                    params["assertiveness"] = _to_int(v, 50)
                elif k == "好感度":
                    params["affinity"] = _to_int(v, 50)
                elif k == "友情值":
                    params["friendship"] = _to_int(v, 50)
                elif k == "脏话倾向":
                    params["crude"] = _to_int(v, 20)
                elif k == "开放度":
                    params["openness"] = _to_int(v, 40)
                elif k == "关系":
                    params["relationship"] = v
                elif k == "感性理性":
                    params["sensible"] = _to_int(v, 50)
                elif k == "粘人独立":
                    params["clingy"] = _to_int(v, 50)
                elif k == "随性自律":
                    params["discipline"] = _to_int(v, 50)
                elif k == "热情冷淡":
                    params["warmth"] = _to_int(v, 50)
    return params

def _to_int(v: str, default: int = 50) -> int:
    try:
        n = int(re.sub(r"\D", "", v))
        return max(0, min(100, n))
    except Exception:
        return default

def _unescape_json_text(s: str) -> str:
    """把 JSON 字符串片段还原成文本（处理 \\uXXXX / \\" / \\n 等转义）"""
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s

def _extract_json_object(text: str) -> str:
    """从模型输出中稳健提取 JSON 对象字符串（容忍 Markdown 围栏/前后废话）"""
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.M)
    t = re.sub(r"\s*```\s*$", "", t)
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        return ""
    return t[start:end + 1]

def parse_chat_messages(raw_text: str) -> dict:
    """解析模型输出的微信消息 JSON → {"conversation_state", "relation_delta", "relationship", "messages":[{text,type,delay}]}
    模型输出漂移时自动兜底为单条消息，不让对话断掉。
    """
    state = "闲聊"
    relation_delta = None
    relationship = ""
    messages = []
    obj_str = _extract_json_object(raw_text or "")
    data = None
    if obj_str:
        try:
            data = json.loads(obj_str)
        except Exception:
            data = None
    if isinstance(data, dict):
        cs = (data.get("conversation_state") or "").strip()
        if cs in CHAT_STATES:
            state = cs
        rd = data.get("relation_delta")
        if isinstance(rd, dict):
            d = {}
            for k in ("affinity", "friendship"):
                try:
                    v = int(float(rd.get(k, 0) or 0))
                except Exception:
                    v = 0
                if v:
                    d[k] = max(-8, min(8, v))
            if d:
                relation_delta = d
        rel = (data.get("relationship") or "").strip()
        if rel:
            relationship = rel[:120]
        raw_msgs = data.get("messages")
        if isinstance(raw_msgs, dict):  # 容忍单条 dict
            raw_msgs = [raw_msgs]
        if isinstance(raw_msgs, list):
            for i, m in enumerate(raw_msgs[:60]):
                if not isinstance(m, dict):
                    continue
                text = str(m.get("text") or "").strip()
                if not text:
                    continue
                mtype = str(m.get("type") or "").strip().lower()
                if mtype not in CHAT_MSG_TYPES:
                    mtype = "statement"
                try:
                    delay = int(float(str(m.get("delay") or 0)))
                except Exception:
                    delay = 0
                delay = _normalize_delay(state, i, delay, total_hint=len(raw_msgs))
                _item = {"text": text[:3000], "type": mtype, "delay": delay}
                img = str(m.get("image_url") or "").strip()
                if img:
                    _item["image_url"] = img[:2000]
                messages.append(_item)
    if not messages:
        raw = (raw_text or "").strip()
        # 兜底1：模型 JSON 被截断/损坏时，提取里面已输出的 text 片段当消息，绝不把原始 JSON 当消息发出去
        if raw and ("conversation_state" in raw or '"messages"' in raw):
            texts = []
            for mm in re.finditer(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
                t = _unescape_json_text(mm.group(1)).strip()
                if t:
                    texts.append(t)
            if texts:
                for i, t in enumerate(texts[:60]):
                    messages.append({"text": t[:3000], "type": "statement",
                                     "delay": _normalize_delay("闲聊", i, 0, total_hint=len(texts))})
        if not messages and raw:
            messages.append({"text": raw[:3000], "type": "statement", "delay": 0})
        # 模型确实什么都没输出：返回空列表，绝不注入假"……"思考气泡（thinking 由流式 reasoning 通道承载）
    schedules = []
    if isinstance(data, dict):
        schedules = _parse_schedules(data.get("schedules"))
    # 双记忆层：模型每轮可带出固定记忆条目（long 写死 / short 带权重短期记忆）
    memory_data = {"long": [], "short": []}
    if isinstance(data, dict):
        mem = data.get("memory")
        if isinstance(mem, dict):
            long_items = mem.get("long") or []
            short_items = mem.get("short") or []
            memory_data["long"] = [str(x).strip()[:300] for x in long_items if isinstance(x, str) and len(str(x).strip()) >= 4][:8]
            short_clean = []
            for x in short_items[:8]:
                if isinstance(x, str):
                    short_clean.append({"text": x.strip()[:300], "weight": 0.5})
                elif isinstance(x, dict) and (x.get("text") or "").strip():
                    try:
                        w = max(0.0, min(1.0, float(x.get("weight", 0.5) or 0.5)))
                    except Exception:
                        w = 0.5
                    short_clean.append({"text": str(x["text"]).strip()[:300], "weight": w})
            memory_data["short"] = [x for x in short_clean if len(x["text"]) >= 4]
    webcheck = ""
    if isinstance(data, dict):
        wc = data.get("webcheck")
        if isinstance(wc, str):
            webcheck = wc.strip()[:200]
        elif isinstance(wc, list) and wc:
            webcheck = str(wc[0]).strip()[:200]
    topic = ""
    if isinstance(data, dict):
        t = data.get("topic")
        if isinstance(t, str):
            topic = t.strip()[:80]
    # 场景/意愿/内部分析字段（模型软提示，最终以代码计算为准）
    scene = ""
    if isinstance(data, dict):
        s = (data.get("scene") or "").strip().lower()
        if s in SCENES:
            scene = s
    interaction_need = 0
    try:
        interaction_need = int(float((data or {}).get("interaction_need") or 0))
    except Exception:
        interaction_need = 0
    topic_confidence = 0.0
    try:
        topic_confidence = max(0.0, min(1.0, float((data or {}).get("topic_confidence") or 0)))
    except Exception:
        topic_confidence = 0.0
    topic_shift = bool((data or {}).get("topic_shift"))
    user_intent = str((data or {}).get("user_intent") or "").strip()[:40]
    user_emotion = str((data or {}).get("user_emotion") or "").strip()[:40]
    emotion_delta = {}
    if isinstance(data, dict):
        ed = data.get("emotion_delta")
        if isinstance(ed, dict):
            for k in EMOTION_KEYS:
                try:
                    v = int(float(ed.get(k) or 0))
                except Exception:
                    continue
                if v:
                    emotion_delta[k] = max(-15, min(15, v))
    emotion_reason = str((data or {}).get("emotion_reason") or "").strip()[:120]
    actions = []
    if isinstance(data, dict):
        for a in (data.get("actions") or [])[:4]:
            if isinstance(a, dict) and a.get("type"):
                actions.append({"type": str(a["type"]).strip()[:20], "query": str(a.get("query") or "")[:200]})
    return {"conversation_state": state, "relation_delta": relation_delta, "relationship": relationship,
            "schedules": schedules, "memory": memory_data, "webcheck": webcheck, "topic": topic,
            "scene": scene, "interaction_need": interaction_need, "topic_confidence": topic_confidence,
            "topic_shift": topic_shift, "user_intent": user_intent, "user_emotion": user_emotion,
            "emotion_delta": emotion_delta, "emotion_reason": emotion_reason,
            "actions": actions, "messages": messages}

def _try_stream_parse(buf: str):
    """增量解析流式输出的 JSON：能解析就返回 (state, messages)，还没闭合/不可解析返回 None。
    只尝试最后 1~2 个右花括号，避免 O(n^2)；delay 按 state 用同一套规则校准。"""
    t = (buf or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    start = t.find("{")
    if start < 0:
        return None
    e = t.rfind("}")
    if e <= start:
        return None
    ends = [e]
    e2 = t.rfind("}", 0, e)
    if e2 > start:
        ends.append(e2)
    for end in ends:
        try:
            data = json.loads(t[start:end + 1])
        except Exception:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            continue
        state = (data.get("conversation_state") or "").strip()
        if state not in CHAT_STATES:
            state = "闲聊"
        msgs = []
        for i, m in enumerate(data["messages"][:60]):
            if not isinstance(m, dict):
                continue
            text = str(m.get("text") or "").strip()
            if not text:
                continue
            mtype = str(m.get("type") or "").strip().lower()
            if mtype not in CHAT_MSG_TYPES:
                mtype = "statement"
            try:
                delay = int(float(str(m.get("delay") or 0)))
            except Exception:
                delay = 0
            delay = _normalize_delay(state, i, delay, total_hint=len(data["messages"]))
            msgs.append({"text": text[:3000], "type": mtype, "delay": delay})
        return state, msgs
    return None

def _normalize_delay(state: str, index: int, delay: int, total_hint: int = 0) -> int:
    """真人节奏：第一条即时反应；情绪状态连发无延迟；普通状态每条之间默认 3~5 秒。
    长内容（讲故事/长文，消息 10 条以上）中后段加快到 1~1.5 秒，避免整段故事拖几分钟才播完。"""
    delay = max(0, min(8000, delay))
    if index == 0:
        return max(0, min(800, delay))
    if state in ("兴奋", "吐槽", "吵架"):
        return max(0, min(800, delay))
    if total_hint >= 10 and index >= 8:
        return max(300, min(1500, delay))
    return max(3000, delay)

def _parse_schedule_time(at_str: str) -> float | None:
    """解析提醒时间：支持 YYYY-MM-DD HH:MM 或 HH:MM（今天已过则明天），返回 epoch"""
    try:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", (at_str or "").strip())
        if m:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
        else:
            m2 = re.match(r"^(\d{1,2}):(\d{2})", (at_str or "").strip())
            if not m2:
                return None
            dt = datetime.now().replace(hour=int(m2.group(1)) % 24, minute=int(m2.group(2)), second=0, microsecond=0)
            if dt.timestamp() <= time.time():
                dt = datetime.fromtimestamp(dt.timestamp() + 86400)
        return dt.timestamp()
    except Exception:
        return None

def _parse_schedules(raw) -> list:
    """解析模型输出的 schedules（nudge/reminder），非法项跳过"""
    out = []
    if not isinstance(raw, list):
        return out
    for s in raw[:5]:
        if not isinstance(s, dict):
            continue
        stype = str(s.get("type") or "").strip()
        if stype == "nudge":
            try:
                after = max(1, min(60, int(float(s.get("after_minutes", 5) or 5))))
            except Exception:
                after = 5
            out.append({"type": "nudge", "after_minutes": after})
        elif stype == "reminder":
            text = str(s.get("text") or "").strip()[:200]
            ts = _parse_schedule_time(str(s.get("at") or ""))
            if text and ts:
                try:
                    weight = max(0.0, min(1.0, float(s.get("weight", 0.5) or 0.5)))
                except Exception:
                    weight = 0.5
                out.append({"type": "reminder", "at_ts": ts, "text": text, "weight": weight})
    return out

class SaveRoutesRequest(BaseModel):
    rules: list[dict]

class SaveSettingsRequest(BaseModel):
    llm_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_route_url: Optional[str] = None
    llm_route_model: Optional[str] = None
    api_key: Optional[str] = None
    default_temperature: Optional[float] = None
    memory_tags: Optional[list] = None
    allow_profanity: Optional[bool] = None
    allow_naughty: Optional[bool] = None
    profanity_level: Optional[str] = None   # off/low/mid/high/ultra
    naughty_level: Optional[str] = None     # off/low/mid/high/ultra
    thinking_level: Optional[str] = None    # off/low/mid/high/ultra
    desire_base: Optional[int] = None       # 对话欲望基准 0-100
    web_enabled: Optional[bool] = None
    proactive_enabled: Optional[bool] = None  # 联想开关：角色主动发消息/分享日常
    show_thinking: Optional[bool] = None
    thinking_enabled: Optional[bool] = None
    user_profile: Optional[str] = None
    parallel_requests: Optional[str] = None   # auto/1/2/3/4/0(不限制)
    delayed_reply_enabled: Optional[bool] = None  # 延时回复开关：像真人一样 1~3 秒后才回、连续多条攒成一轮
    day_memory_enabled: Optional[bool] = None     # 当天记忆实时归纳（大纲+事件分条），默认开，增加算力但极大影响记忆
    night_memory_enabled: Optional[bool] = None   # 凌晨4点把前一天聊天归纳入库（短/长期记忆分级），默认开
    user_location: Optional[str] = None           # 用户所在城市/地区（手动填，最高优先级；留空则用公网 IP 解析兜底）
    user_gender: Optional[str] = None

class MemoryAddRequest(BaseModel):
    text: str
    tags: Optional[list] = None


__all__ = [
    "ChatMessage",
    "ChatRequest",
    "CreateCharacterRequest",
    "MemoryAddRequest",
    "SaveRoutesRequest",
    "SaveSettingsRequest",
    "_extract_json_object",
    "_msg_text",
    "_normalize_delay",
    "_parse_schedule_time",
    "_parse_schedules",
    "_to_int",
    "_try_stream_parse",
    "_unescape_json_text",
    "parse_chat_messages",
    "parse_persona_params",
    "parse_resume",
    "resume_to_text",
]
