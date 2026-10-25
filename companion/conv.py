# -*- coding: utf-8 -*-
"""Qiyu 会话/场景/上下文状态机（从 demo.py 迁移，M1）。"""
import json
import time
from datetime import datetime
from loguru import logger


from companion.constants import SCENES, SCENE_BEHAVIOR, SCENE_LABELS, SCENE_TRIGGERS, _STORY_STOP_RE
from companion.state import CONV_STATE_JSON, SHARED_EVENTS_JSON, _conv_store, _schedules_store, _shared_events, save_user_store
from companion.settings import _desire_value
from companion.relations import _clamp_int, _init_relation, note_proactive_reply, reset_no_reply_streak
from companion.emotions import _mood_blocks_proactive

_conv_dirty = False


def _mark_conv_dirty():
    """标记录音会话状态需要落盘（用于后台批量扫描时合并写盘）。"""
    global _conv_dirty
    _conv_dirty = True


def _save_conv_store():
    global _conv_dirty
    _conv_dirty = False
    try:
        save_user_store("conv_state", _conv_store)
    except Exception as e:
        logger.error(f"保存会话状态失败: {e}")


def flush_conv_store():
    """后台批量更新结束后一次性落盘；无变更不写。"""
    if _conv_dirty:
        _save_conv_store()

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

def _recent_reply_freq(user_id: str, char_id: str, hours: float = 2.0) -> int:
    """近 hours 小时内用户实际回复条数（主动消息要不要发的依据之一）。"""
    try:
        from memory import get_memory_manager
        hist = get_memory_manager().get_recent_history(user_id, limit=40, char_id=char_id)
    except Exception:
        return 0
    now = time.time()
    n = 0
    for m in hist or []:
        if m.get("role") != "user":
            continue
        try:
            ts = datetime.fromisoformat(str(m.get("timestamp") or ""))
        except Exception:
            continue
        if ts.timestamp() > now - hours * 3600:
            n += 1
    return n

def _interaction_need_value(user_id: str, char_id: str, parsed: dict | None = None, user_input: str = "") -> int:
    """0=不太想聊 1=可以应付 2=正常 3=有兴趣 4=很想聊（当前主动性和聊天欲，与耐心度解耦）。

    修复：主动消息场景（无模型 parsed 输入）时不再因 mi=0 恒低 → 白天永远 low_need。
    改为由 空闲时长 + 近期回复频率 + 关系 + 耐心 + 最近主动次数 + 时段 + 未完成话题 共同决定；
    模型刚解析过聊天时，其 interaction_need 只作 0.3 权重的小修正。
    """
    cs = _conv_state(user_id, char_id)
    rel = _init_relation(user_id, char_id)
    aff = _clamp_int(rel.get("affinity"), 50)
    pat = _desire_value(user_id, char_id)
    now = time.time()
    last_u = float(cs.get("last_user_at") or 0)
    idle = (now - last_u) if last_u else 0.0
    v = 2.0
    # 1) 空闲时长：刚聊过想接住；隔一会儿想找话题；太久没聊更想冒泡（白天不至于归零）
    if last_u:
        if idle < 900:
            v += 0.5
        elif idle < 7200:
            v += 0.5
        elif idle < 21600:
            v += 1.0
        else:
            v += 1.5
    else:
        v += 0.5  # 新关系第一次主动打招呼
    # 2) 近期回复频率：最近 2h 内聊得越勤，此刻主动越自然（证明对方在线/愿意聊）
    freq = _recent_reply_freq(user_id, char_id)
    if freq >= 8:
        v += 1.0
    elif freq >= 4:
        v += 0.5
    # 3) 关系：好感越高越愿意主动
    if aff >= 75:
        v += 1.0
    elif aff >= 60:
        v += 0.5
    elif aff <= 30:
        v -= 0.5
    # 4) 耐心（_desire_value=角色当前耐心/精力）
    if pat >= 70:
        v += 0.5
    elif pat <= 30:
        v -= 1.0
    # 5) 最近主动次数：刚主动过别连环打扰（Context Gate 的 recent_proactive 仍会硬拦 <8 分钟）
    last_p = float(cs.get("last_proactive_at") or 0)
    if last_p and now - last_p < 15 * 60:
        v -= 1.5
    elif last_p and now - last_p < 7200:
        v -= 0.5
    # 6) 时段
    hour = datetime.now().hour
    if hour >= 23 or hour < 6:
        v += 1.0 if aff >= 60 else -0.5
    elif 6 <= hour < 9:
        v -= 0.5   # 早上尽量少打扰
    elif (12 <= hour < 14) or (18 <= hour < 23):
        v += 0.5   # 午休/晚间更自然
    # 7) 上次主动对方没回 → 不连环轰炸
    if cs.get("unanswered_pending"):
        v -= 1.0
    # 8) 正在讲故事 → 别开新话题打断（storycheck 单独负责轻唤）
    if cs.get("story_active"):
        v -= 1.0
    # 9) 最近话题钩子：聊过具体话题 → 主动时有话可接（不生硬开新话题）
    if (cs.get("last_topic") or "").strip() and (cs.get("unfinished_topic") or "").strip():
        v += 0.5
    # 模型信号（刚解析过聊天）只作小修正；没有模型输入时不再恒低
    if parsed:
        try:
            mi = int(float((parsed or {}).get("interaction_need") or 2))
        except Exception:
            mi = 2
        mi = max(0, min(4, mi))
        v = v * 0.7 + mi * 0.3
    return max(0, min(4, int(v + 0.5)))

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

def _update_conv_state(user_id: str, char_id: str, event: str, parsed: dict | None = None,
                       user_input: str = "", save: bool = True):
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
        # 主动消息反馈：用户冷淡→记冷淡信号(即时停手)；热情→清零计数
        _lp_at = float(cs.get("last_proactive_at") or 0)
        # 以前只有 1 小时内回主动消息才清零"连续没回"计数：用户第二天才回，
        # 计数一直卡在上限，scheduler 就再也不让角色主动找话了
        if user_input:
            try:
                reset_no_reply_streak(user_id, char_id)
            except Exception:
                pass
        if user_input and _lp_at and (now - _lp_at) < 3600:
            try:
                _tag = note_proactive_reply(user_id, char_id, user_input)
                if _tag != "neutral":
                    logger.info(f"[主动消息] {user_id}/{char_id} 对主动消息的反馈: {_tag} | {user_input[:30]}")
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
        # 后台扫描每 20s 会对所有历史 (用户,角色) 触发一次 idle；
        # 没有真实状态转移时不必全量序列化 conv_state（高频全量写会拖慢主事件循环）。
        _changed = False
        last_any = max(cs.get("last_user_at") or 0, cs.get("last_ai_at") or 0)
        # 2026-10-07：ACTIVE 原来要静默满 1 小时才松开，找话等于被关在门外。
        # 8 分钟没新消息就当这轮聊完（正在来回发的时候 last_any 一直在刷新，不会误判）。
        if cs["conv_state"] == "ACTIVE" and now - last_any > 8 * 60:
            cs["conv_state"] = "QUIET"
            cs["ended_at"] = now
            _changed = True
        # 半天没说话才算这天的对话结束；冷却只留 15 分钟，不要再额外罚一小时。
        if now - last_any > 6 * 3600:
            cs["conv_state"] = "ENDED"
            if not cs.get("cooldown_until") or cs["cooldown_until"] < now:
                cs["cooldown_until"] = now + 15 * 60
            _changed = True
        if cs["conv_state"] in ("ENDED", "COOLDOWN") and (cs.get("cooldown_until") or 0) <= now:
            cs["conv_state"] = "AVAILABLE_FOR_PROACTIVE"
            _changed = True
        if not _changed:
            return
    cs["updated_at"] = now
    _mark_conv_dirty()
    if save:
        flush_conv_store()

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
    # 刚说完别立刻找话（8 分钟）；她自己刚主动过也先歇 8 分钟。
    # 以前是 90 分钟 / 25 分钟，叠上 ACTIVE 要等 1 小时，线上几乎不来找。
    if now - (cs.get("last_user_at") or 0) < 8 * 60:
        return False, "recent_user"
    if now - (cs.get("last_proactive_at") or 0) < 8 * 60:
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
    "flush_conv_store",
    "_recent_shared_events",
    "_record_shared_event",
    "_save_conv_store",
    "_save_shared_events",
    "_scene_prompt_block",
    "_shared_events_prompt",
    "_update_conv_state",
]
