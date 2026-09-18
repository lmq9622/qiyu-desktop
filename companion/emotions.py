# -*- coding: utf-8 -*-
"""Qiyu 情绪状态与记忆流水线（从 demo.py 迁移，M1）。"""
import json
import time
from loguru import logger
from characters import get_character_manager
from memory import get_memory_manager
from letta_backend import get_letta_backend

char_mgr = get_character_manager()
mem_mgr = get_memory_manager()
letta_backend = get_letta_backend()
llm_client = None


from companion.constants import EMOTION_BASE, EMOTION_KEYS, EMOTION_LABELS
from companion.state import EMOTIONS_JSON, _emotions_store, save_user_store

def _save_emotions():
    try:
        # 按用户分文件：/data/users/<用户>/emotions.json（不再全站一个大文件）
        save_user_store("emotions", _emotions_store)
    except Exception as e:
        logger.error(f"保存情绪失败: {e}")

def _emotion_state(user_id: str, char_id: str) -> dict:
    """每个(用户,角色)独立的情绪向量（持久化）。"""
    if not user_id or not char_id:
        return {}
    k = f"{user_id}:{char_id}"
    es = _emotions_store.get(k)
    if not es:
        es = dict(EMOTION_BASE)
        es.update({"reason": "", "mood_event": None, "mood_date": "", "updated_at": 0})
        _emotions_store[k] = es
    return es

def _emotion_composite(es: dict) -> float:
    """总情绪（-100~100）：开心/兴奋为正，悲伤/焦虑/害怕为负。"""
    if not es:
        return 0.0
    return (float(es.get("joy", 20)) * 0.28 + float(es.get("excitement", 15)) * 0.22
            - float(es.get("sadness", 5)) * 0.26 - float(es.get("anxiety", 8)) * 0.14
            - float(es.get("fear", 5)) * 0.10)

def _emotion_tone(comp: float) -> str:
    if comp >= 40:
        return "很开心"
    if comp >= 15:
        return "心情不错"
    if comp > -15:
        return "一般"
    if comp > -40:
        return "有点低落"
    return "很差"

def _emotion_active_mood_event(es: dict) -> dict | None:
    """当天情绪基调事件：只当天有效，过了午夜自动失效。"""
    me = es.get("mood_event")
    if me and es.get("mood_date") == time.strftime("%Y-%m-%d"):
        return me
    return None

def _apply_emotion_delta(user_id: str, char_id: str, parsed: dict):
    """每轮应用模型输出的情绪变化：EMA 平滑 + 滞回（难哄，不是 0/1）+ 当天基调事件记录。
    情绪向基线缓慢回落；冲高到位的负面情绪会定调一整天，正面互动也要反复哄才慢慢回升。"""
    if not user_id or not char_id:
        return
    delta = (parsed or {}).get("emotion_delta") or {}
    if not isinstance(delta, dict) or not any(k in EMOTION_KEYS for k in delta):
        return
    es = _emotion_state(user_id, char_id)
    if not es:
        return
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    reason = str((parsed or {}).get("emotion_reason") or "").strip()[:120]
    # 跨天：情绪向基线回落，基调事件失效
    if es.get("mood_date") != today:
        for k in EMOTION_KEYS:
            base = EMOTION_BASE.get(k, 10)
            es[k] = max(0, min(100, float(es.get(k, base)) + (base - float(es.get(k, base))) * 0.5))
        es["mood_event"] = None
        es["mood_date"] = today
    for k in EMOTION_KEYS:
        try:
            v = int(float(delta.get(k) or 0))
        except Exception:
            continue
        if not v:
            continue
        v = max(-15, min(15, v))
        cur = float(es.get(k, EMOTION_BASE.get(k, 10)))
        # 难哄：正向推到高位后边际减半；负面情绪上来后，单次正面很难压回去
        if v > 0 and cur >= 60:
            v = max(1, int(v * 0.5))
        if v > 0 and k in ("sadness", "fear", "anxiety") and cur >= 55:
            v = max(1, int(v * 0.7))
        es[k] = max(0, min(100, cur + v))
    if reason:
        es["reason"] = reason
    # 当天基调：负面情绪冲到高位 → 定调一整天
    for k in ("sadness", "fear", "anxiety"):
        if float(es.get(k, 0)) >= 62:
            es["mood_event"] = {"emotion": k, "reason": reason or f"（{EMOTION_LABELS[k]}压着）", "strength": int(es[k])}
            es["mood_date"] = today
            break
    es["updated_at"] = now
    _save_emotions()

def _mood_blocks_proactive(user_id: str, char_id: str) -> bool:
    """当天情绪基调为负面（或总情绪很低）→ 不适合主动找话题/分享日常。"""
    es = _emotion_state(user_id, char_id)
    if not es:
        return False
    if _emotion_active_mood_event(es):
        return True
    return _emotion_composite(es) < -20

def _mood_is_great(user_id: str, char_id: str) -> bool:
    es = _emotion_state(user_id, char_id)
    if not es:
        return False
    return _emotion_composite(es) >= 25 and not _emotion_active_mood_event(es)

def _emotion_prompt_block(user_id: str, char_id: str) -> str:
    """注入提示词的当前情绪状态（语气要匹配；数值与原因只作内部参考，不要复述）。"""
    es = _emotion_state(user_id, char_id)
    if not es:
        return ""
    me = _emotion_active_mood_event(es)
    comp = _emotion_composite(es)
    tone = _emotion_tone(comp)
    parts = [f"【当前情绪状态（内部，语气要和它匹配，不要向用户复述数值）】总情绪：{tone} | "
             f"开心{int(es.get('joy', 20))} 害怕{int(es.get('fear', 5))} 悲伤{int(es.get('sadness', 5))} "
             f"焦虑{int(es.get('anxiety', 8))} 兴奋{int(es.get('excitement', 15))}"]
    if me:
        neg = EMOTION_LABELS.get(me.get("emotion"), "低落")
        parts.append(
            f"【今天的情绪基调】因为：{me.get('reason') or '（说不上来，就是提不起劲）'}。"
            f"你今天一整天都被{neg}压着：分享欲很低、不太容易开心，更不会兴高采烈地找话题；"
            f"最多简短回应。对方安慰你时情绪回升要有个真实的过程，不会一句就哄好。"
        )
    elif comp >= 40:
        parts.append("【当前情绪】你今天心情很好、分享欲高：可以自然主动找话题、接梗、多说两句。")
    elif comp < -20:
        parts.append("【当前情绪】你今天整体不太在状态：回话偏短偏淡，不硬撑热情，对方烦你也可以直接说。")
    if es.get("reason"):
        parts.append(f"【情绪原因（对方问起或你自然提及时再带出，别主动背台词）】{es['reason']}")
    return "\n".join(parts)

async def _run_memory_pipeline(user_id: str, char_id: str = ""):
    """回复完成后触发的记忆流水线：压缩旧对话 + 摘取用户事实入库（后台执行，不影响前端）"""
    try:
        if not llm_client or not llm_client.available:
            return
        result = await mem_mgr.compress_history(user_id, llm_client.summarize_text, char_id=char_id)
        if result:
            await _sync_memory_to_letta(user_id, result)
    except Exception as e:
        logger.error(f"[记忆流水线] 触发失败: {e}")

async def _sync_memory_to_letta(user_id: str, result: dict):
    """把压缩产物同步到 Letta Agent（归档记忆 + 用户档案），失败自动降级不影响主链路"""
    try:
        if not letta_backend or not letta_backend.available:
            return
        char_id = result.get("character_id") or ""
        char = char_mgr.get_character(char_id) if char_id else None
        if not char:
            char = char_mgr.get_default()
        if not char:
            return
        profile = mem_mgr.get_user_profile(user_id)
        agent_id = await letta_backend.ensure_agent(
            char.id, char.name, char.to_prompt(), human=profile,
        )
        if not agent_id:
            return
        summary = (result.get("summary") or "").strip()
        if summary:
            await letta_backend.insert_archival(agent_id, summary, tags=["auto_summary"])
        for fact in result.get("facts") or []:
            await letta_backend.insert_archival(agent_id, fact, tags=["user_fact"])
        if profile:
            await letta_backend.update_human_block(agent_id, profile)
    except Exception as e:
        logger.warning(f"[Letta] 记忆同步失败（已忽略）: {e}")

async def _sync_character_to_letta(char):
    """角色创建/切换时在 Letta 侧建 Agent（含完整角色提示词），失败不影响主链路"""
    try:
        if letta_backend and letta_backend.available:
            await letta_backend.ensure_agent(char.id, char.name, char.to_prompt())
    except Exception as e:
        logger.warning(f"[Letta] 角色同步失败（已忽略）: {e}")


__all__ = [
    "_apply_emotion_delta",
    "_emotion_active_mood_event",
    "_emotion_composite",
    "_emotion_prompt_block",
    "_emotion_state",
    "_emotion_tone",
    "_mood_blocks_proactive",
    "_mood_is_great",
    "_run_memory_pipeline",
    "_save_emotions",
    "_sync_character_to_letta",
    "_sync_memory_to_letta",
    "char_mgr",
    "letta_backend",
    "llm_client",
    "mem_mgr",
]
