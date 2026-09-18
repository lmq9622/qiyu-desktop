# -*- coding: utf-8 -*-
"""Qiyu 会话/场景/上下文状态机（从 demo.py 迁移，M1）。"""
import json
import time
from datetime import datetime
from loguru import logger


from companion.constants import SCENES, SCENE_BEHAVIOR, SCENE_LABELS, SCENE_TRIGGERS, _STORY_STOP_RE
from companion.state import CONV_STATE_JSON, SHARED_EVENTS_JSON, _conv_store, _schedules_store, _shared_events, save_user_store
from companion.settings import _desire_value
from companion.relations import _clamp_int, _init_relation
from companion.emotions import _mood_blocks_proactive

def _save_conv_store():
    try:
        save_user_store("conv_state", _conv_store)
    except Exception as e:
        logger.error(f"保存会话状态失败: {e}")

def _save_shared_events():
    try:
        save_user_store("shared_events", _shared_events)
    except Exception as e:
        logger.error(f"保存共享事件失败: {e}")

def _conv_key(user_id: str, char_id: str) -> str:
    return f"{user_id}:{char_id}"

def _conv_state(user_id: str, char_id: str) -> dict:
    """每个(用户,角色)独立的会话状态（持久化），互不串上下文"""
    if not user_id or not char_id:
        return {}
    k = _conv_key(user_id, char_id)
    cs = _conv_store.get(k)
    if not cs:
        cs = {
            "conv_state": "AVAILABLE_FOR_PROACTIVE", "scene": "ordinary_chat", "interaction_need": 2,
            "patience": 50, "affinity": 50, "friendship": 50,
            "last_topic": "", "last_topic_confidence": 0, "last_topic_shift": False,
            "previous_topic": "", "topic_transition": "none", "topic_surprise": 0,
            "last_user_intent": "", "last_user_emotion": "",
            "last_user_at": 0, "last_ai_at": 0, "last_proactive_at": 0, "last_proactive_text": "",
            "ended_at": 0, "cooldown_until": 0, "unanswered_pending": None, "updated_at": 0,
            "recent_facts": [],          # 最近事实：对方刚说过的话（供"刚说过别表演回忆"判断）
            "unfinished_topic": "",      # 对方还没说完/还没正面回应的话题
            "unfinished_topic_at": 0,
            "story_active": False,       # 正在讲故事（长文输出模式）
            "story_bubbles": 0,
            "story_started_at": 0,
            "story_check_sent": False,   # 讲故事后是否已轻唤过对方（每段故事最多一次）
            "story_check_at": 0,
        }
        _conv_store[k] = cs
    return cs

def _interaction_need_value(user_id: str, char_id: str, parsed: dict | None = None, user_input: str = "") -> int:
    """0=不太想聊 1=可以应付 2=正常 3=有兴趣 4=很想聊（当前主动性和聊天欲，与耐心度解耦）"""
    cs = _conv_state(user_id, char_id)
    rel = _init_relation(user_id, char_id)
    aff = _clamp_int(rel.get("affinity"), 50)
    v = 2.0
    now = time.time()
    last_u = cs.get("last_user_at") or 0
    if last_u and now - last_u < 120:
        v += 1.0
    elif last_u and now - last_u > 3600:
        v -= 1.0
    if aff >= 75:
        v += 1.0
    elif aff <= 30:
        v -= 1.0
    hour = datetime.now().hour
    if 23 <= hour or hour < 6:
        v = v + 1.0 if aff >= 70 else v - 1.0
    if cs.get("unanswered_pending"):
        v -= 1.0
    mi = 0
    try:
        mi = int(float((parsed or {}).get("interaction_need") or 0))
    except Exception:
        mi = 0
    if mi in (0, 1, 2, 3, 4):
        v = v * 0.6 + mi * 0.4
    return max(0, min(4, int(round(v))))

def _compute_scene(user_id: str, char_id: str, parsed: dict | None = None, user_input: str = "") -> str:
    """综合关系/耐心/意愿/意图/情绪/话题/活跃/时段/未完成任务/最近记忆，算出当前场景。
    场景是生成条件（行为范围），不是强制话术。"""
    if not user_id or not char_id:
        return "ordinary_chat"
    cs = _conv_state(user_id, char_id)
    rel = _init_relation(user_id, char_id)
    aff = _clamp_int(rel.get("affinity"), 50)
    need = _interaction_need_value(user_id, char_id, parsed, user_input)
    hour = datetime.now().hour
    text = (user_input or "").strip()
    if text and any(w in text for w in SCENE_TRIGGERS["farewell"]):
        return "farewell"
    if hour >= 23 or hour < 6:
        if any(w in text for w in SCENE_TRIGGERS["late_night"]) or (aff >= 70 and need >= 3):
            return "late_night"
    # 尴尬：突然的关系/感情类提问 → 不自动进暧昧
    if text and any(w in text for w in SCENE_TRIGGERS["awkward"]):
        return "awkward"
    intent = (parsed or {}).get("user_intent") or ""
    emotion = (parsed or {}).get("user_emotion") or ""
    if any(w in emotion for w in ("难过", "烦", "崩溃", "哭", "低落", "累")) or any(w in text for w in SCENE_TRIGGERS["comfort"]):
        return "comfort"
    if any(w in intent for w in ("玩", "调侃", "玩笑")) or any(w in text for w in SCENE_TRIGGERS["playful"]):
        return "playful"
    if any(w in intent for w in ("争执", "反驳", "吵架")) or any(w in text for w in SCENE_TRIGGERS["argument"]):
        return "argument"
    if any(w in intent for w in ("求助", "帮忙", "解决")) or any(w in text for w in SCENE_TRIGGERS["helping"]):
        return "helping"
    if any(w in intent for w in ("认真", "讨论", "分析")) or any(w in text for w in SCENE_TRIGGERS["serious"]):
        return "serious"
    # 亲密：达到阈值只是允许进入，不强制；还需要近 30 分钟有互动
    if aff >= 60 and need >= 2 and time.time() - (cs.get("last_user_at") or 0) < 1800:
        if any(w in intent for w in ("暧昧", "亲密", "撩")) or any(w in text for w in ("想你", "抱抱", "亲亲", "想你了")):
            return "intimate"
    prev = cs.get("scene")
    return prev if prev in SCENES else "ordinary_chat"

def _update_conv_state(user_id: str, char_id: str, event: str, parsed: dict | None = None, user_input: str = ""):
    """事件驱动的会话状态机：user_message / ai_reply / proactive_sent / idle"""
    from companion.behavior import _refresh_unfinished_topic
    if not user_id or not char_id:
        return
    cs = _conv_state(user_id, char_id)
    now = time.time()
    if event == "user_message":
        cs["last_user_at"] = now
        cs["unanswered_pending"] = None
        cs["conv_state"] = "ACTIVE"
        cs["ended_at"] = 0
        cs["cooldown_until"] = 0
        # 用户回来了：下段故事/长内容可再次触发"对方没回"轻唤
        cs["story_check_sent"] = False
        cs["story_check_at"] = 0
        # 上一轮的"未完成话题"：被正面回应/翻篇就解除；对方突然跳走就保留（供 abrupt 感知）
        if user_input:
            try:
                _refresh_unfinished_topic(user_id, char_id, user_input)
            except Exception:
                pass
        # 听众回来了 / 故事被叫停 → 结束讲故事状态
        if cs.get("story_active"):
            if user_input and _STORY_STOP_RE.search(user_input):
                cs["story_active"] = False
            elif not user_input:
                cs["story_active"] = False
        if parsed:
            t = (parsed.get("topic") or user_input or "").strip()
            if t:
                cs["last_topic"] = t[:80]
            try:
                cs["last_topic_confidence"] = max(0.0, min(1.0, float(parsed.get("topic_confidence") or 0)))
            except Exception:
                pass
            cs["last_topic_shift"] = bool(parsed.get("topic_shift"))
            cs["last_user_intent"] = str(parsed.get("user_intent") or "")[:40]
            cs["last_user_emotion"] = str(parsed.get("user_emotion") or "")[:40]
    elif event == "ai_reply":
        cs["last_ai_at"] = now
        cs["conv_state"] = "ACTIVE"
        rel = _init_relation(user_id, char_id)
        cs["patience"] = _desire_value(user_id, char_id)
        cs["affinity"] = _clamp_int(rel.get("affinity"), 50)
        cs["friendship"] = _clamp_int(rel.get("friendship"), 50)
        cs["interaction_need"] = _interaction_need_value(user_id, char_id, parsed, user_input)
        cs["scene"] = _compute_scene(user_id, char_id, parsed, user_input)
    elif event == "proactive_sent":
        cs["last_proactive_at"] = now
        cs["conv_state"] = "QUIET"
    elif event == "idle":
        last_any = max(cs.get("last_user_at") or 0, cs.get("last_ai_at") or 0)
        if cs["conv_state"] == "ACTIVE" and now - last_any > 3600:
            cs["conv_state"] = "QUIET"
            cs["ended_at"] = now
        if now - last_any > 7200:
            cs["conv_state"] = "ENDED"
            if not cs.get("cooldown_until") or cs["cooldown_until"] < now:
                cs["cooldown_until"] = now + 3600
        if cs["conv_state"] in ("ENDED", "COOLDOWN") and (cs.get("cooldown_until") or 0) <= now:
            cs["conv_state"] = "AVAILABLE_FOR_PROACTIVE"
    cs["updated_at"] = now
    _save_conv_store()

def _record_shared_event(user_id: str, char_id: str, event_id: str, content: str, topic: str = ""):
    """主动分享事件登记（event_id 幂等；换说法仍认同一事件，避免重复主动发送）"""
    if not user_id or not char_id or not content:
        return
    key = _conv_key(user_id, char_id)
    events = _shared_events.setdefault(key, [])
    events.append({
        "event_id": event_id or f"share_{int(time.time())}",
        "created_at": time.time(), "shared_at": time.time(),
        "content": (content or "")[:200], "topic": (topic or "")[:80],
    })
    _shared_events[key] = events[-100:]
    _save_shared_events()

def _recent_shared_events(user_id: str, char_id: str, hours: int = 48, limit: int = 20) -> list:
    key = _conv_key(user_id, char_id)
    events = _shared_events.get(key) or []
    cutoff = time.time() - hours * 3600
    return [e for e in events if (e.get("shared_at") or 0) >= cutoff][-limit:]

def _shared_events_prompt(user_id: str, char_id: str) -> str:
    evs = _recent_shared_events(user_id, char_id, hours=24, limit=8)
    if not evs:
        return ""
    lines = [f"- {e.get('content', '')[:80]}（{time.strftime('%m-%d %H:%M', time.localtime(e.get('shared_at') or 0))}）" for e in evs]
    return "【最近你主动发给对方的内容（同一件事别换个说法再发一遍）】\n" + "\n".join(lines)

def _event_similar_to_shared(user_id: str, char_id: str, text: str, threshold: float = 0.6) -> bool:
    """与最近主动分享过的事件文本相似 → 判定为重复，不发"""
    from companion.behavior import _topic_similarity
    if not text:
        return False
    for e in _recent_shared_events(user_id, char_id, hours=48, limit=20):
        if _topic_similarity((e.get("content") or ""), text) > threshold:
            return True
    return False

def _context_gate(user_id: str, char_id: str, now: float | None = None) -> tuple:
    """主动消息 Context Gate：逐项检查（正在聊天/最近互动/冷却/耐心/意愿/未完成任务/去重）。
    返回 (允许: bool, 原因: str)。"""
    now = now or time.time()
    if not user_id or not char_id:
        return False, "no_ctx"
    cs = _conv_state(user_id, char_id)
    if cs.get("conv_state") == "ACTIVE":
        return False, "active"
    if now - (cs.get("last_user_at") or 0) < 5400:
        return False, "recent_user"
    if now - (cs.get("last_proactive_at") or 0) < 1500:
        return False, "recent_proactive"
    if (cs.get("cooldown_until") or 0) > now:
        return False, "cooldown"
    if _desire_value(user_id, char_id) < 35:
        return False, "low_patience"
    if _interaction_need_value(user_id, char_id) <= 1:
        return False, "low_need"
    if _mood_blocks_proactive(user_id, char_id):
        return False, "mood_down"
    tasks = _schedules_store.get(user_id) or []
    pending = [t for t in tasks if not t.get("done") and t.get("due_at", 0) > now]
    if any(t.get("kind") in ("reminder", "webcheck", "imagecheck", "nudge") for t in pending):
        return False, "pending_task"
    return True, "ok"

def _scene_prompt_block(user_id: str, char_id: str, parsed: dict | None = None, user_input: str = "") -> str:
    """注入提示词的场景行为范围 + 内部状态权重（不展示给用户）"""
    scene = _compute_scene(user_id, char_id, parsed, user_input)
    need = _interaction_need_value(user_id, char_id, parsed, user_input)
    pat = _desire_value(user_id, char_id)
    rel = _init_relation(user_id, char_id)
    aff = _clamp_int(rel.get("affinity"), 50)
    out = [f"【内部状态（只用来把握节奏，不要向用户复述）】场景={SCENE_LABELS.get(scene, '普通闲聊')} | 聊天意愿={need}/4 | 耐心={pat}/100 | 好感={aff}/100"]
    out.append(f"【当前场景行为范围】{SCENE_BEHAVIOR.get(scene, SCENE_BEHAVIOR['ordinary_chat'])}")
    if scene == "awkward":
        out.append("【尴尬场景】对方突然聊到关系/感情。你可以短暂沉默、反问、装没听懂、认真回答或回避，由你结合上下文决定；不要因为好感度高就自动往暧昧走，对方如果只是随口说，接一句就翻篇。")
    if scene == "intimate":
        out.append("【暧昧场景】氛围允许，但只是允许进入，不是必须进入；顺着对方自然来，对方后退就退回普通聊天，不硬撩、不服务式讨好。")
    return "\n".join(out)

def _check_unanswered(user_id: str, char_id: str, now: float):
    """主动消息发出 2 小时没被回 → 记一笔（按角色独立），下次聊天让角色自然带出（失落/在意）"""
    if not char_id:
        return
    cs = _conv_state(user_id, char_id)
    last_p = cs.get("last_proactive_at") or 0
    last_u = cs.get("last_user_at") or 0
    if last_p > last_u and now - last_p > 7200 and not cs.get("unanswered_pending"):
        cs["unanswered_pending"] = {"text": (cs.get("last_proactive_text") or "")[:100], "sent_at": last_p}
        _save_conv_store()


__all__ = [
    "_check_unanswered",
    "_compute_scene",
    "_context_gate",
    "_conv_key",
    "_conv_state",
    "_event_similar_to_shared",
    "_interaction_need_value",
    "_recent_shared_events",
    "_record_shared_event",
    "_save_conv_store",
    "_save_shared_events",
    "_scene_prompt_block",
    "_shared_events_prompt",
    "_update_conv_state",
]
