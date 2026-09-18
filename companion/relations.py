# -*- coding: utf-8 -*-
"""Qiyu 关系/日程/主动概率状态（从 demo.py 迁移，M1）。"""
import json
import time
import random
import uuid
from loguru import logger
from characters import get_character_manager
from memory import get_memory_manager

char_mgr = get_character_manager()
mem_mgr = get_memory_manager()


from companion.state import PROACTIVE_JSON, RELATIONS_JSON, SCHEDULES_JSON, _proactive_store, _relations_store, _schedules_store, save_user_store

def _clamp_int(v, default: int = 50) -> int:
    try:
        return max(0, min(100, int(v)))
    except Exception:
        return default

def _relation_tier(affinity: int, friendship: int) -> str:
    avg = (affinity + friendship) / 2
    if avg < 25:
        return "刚认识"
    if avg < 45:
        return "普通朋友"
    if avg < 65:
        return "熟络朋友"
    if avg < 80:
        return "好朋友"
    return "极亲密"

def _relation_label(affinity: int) -> str:
    if affinity < 25:
        return "生疏"
    if affinity < 50:
        return "一般"
    if affinity < 75:
        return "熟络"
    return "炽热"

def _save_relations():
    try:
        save_user_store("relations", _relations_store)
    except Exception as e:
        logger.error(f"保存关系数据失败: {e}")

def _save_proactive():
    try:
        save_user_store("proactive", _proactive_store)
    except Exception:
        pass

def _save_schedules():
    try:
        save_user_store("schedules", _schedules_store)
    except Exception:
        pass

def _is_clumsy_char(char_id: str) -> bool:
    """判断角色人设是否带"冒失"类特征（用于提醒偏移）"""
    try:
        char = char_mgr.get_character(char_id)
        if not char:
            return False
        blob = " ".join([
            " ".join(char.keywords or []),
            str((char.persona_params or {}).get("relationship") or ""),
            str(char.description or ""),
            json.dumps(char.resume or {}, ensure_ascii=False),
        ])
        return any(k in blob for k in ("冒失", "丢三落四", "迷糊", "马大哈", "健忘", "不靠谱", "拖延"))
    except Exception:
        return False

def _reminder_due_time(char_id: str, at_ts: float, weight: float) -> float:
    """提醒触发时间：用户要求时间提前 1~2 分钟（固定偏移量）；冒失人设 + 低权重 + 提前很久提出 → 再晚 1~2 分钟"""
    due = at_ts - random.randint(60, 120)
    lead = at_ts - time.time()
    if _is_clumsy_char(char_id) and weight < 0.45 and lead > 4 * 3600:
        due += random.randint(60, 120)
    return due

def _register_schedule(user_id: str, task: dict):
    """注册一个定时触发任务（nudge/reminder），持久化"""
    tasks = _schedules_store.setdefault(user_id, [])
    task["id"] = uuid.uuid4().hex[:10]
    task["done"] = False
    task["created_at"] = time.time()
    tasks.append(task)
    # 只保留 48 小时内未完成任务，防止无限堆积
    _schedules_store[user_id] = [t for t in tasks if (not t.get("done") or t.get("created_at", 0) > time.time() - 172800)]
    _save_schedules()
    try:
        from runtime.db import unified_store
        unified_store.record_task(
            user_id, (task.get("payload") or {}).get("char_id") or "",
            str(task.get("kind") or "task"), (task.get("payload") or {}),
            due_at=float(task.get("due_at") or 0))
    except Exception:
        pass
    logger.info(f"[调度] {user_id} 注册 {task['kind']} @ {time.strftime('%m-%d %H:%M', time.localtime(task['due_at']))}")

def _cancel_nudge(user_id: str, char_id: str = ""):
    """用户来消息了：取消未完成的追问任务；传 char_id 时只取消该角色的追问，其它角色互不影响"""
    tasks = _schedules_store.get(user_id)
    if tasks:
        def _keep(t: dict) -> bool:
            if t.get("kind") != "nudge" or t.get("done"):
                return True
            if char_id and (t.get("payload") or {}).get("char_id") and t["payload"]["char_id"] != char_id:
                return True  # 其它角色的追问照常保留
            return False
        _schedules_store[user_id] = [t for t in tasks if _keep(t)]
        _save_schedules()

def _proactive_day_prob(affinity: int) -> float:
    """白天每 30 分钟判定的主动概率：好感度越高越频繁（35%~70%）"""
    return 0.35 + (affinity / 100) * 0.35

def _proactive_night_prob(affinity: int) -> float:
    """夜间主动概率：3%~10%，好感度越高越可能（凌晨求安慰/分享心事）"""
    return 0.03 + (affinity / 100) * 0.07

def _char_share_boost(char_id: str) -> float:
    """角色人设里带"活泼/外向/爱分享"倾向 → 主动分享概率加成"""
    try:
        char = char_mgr.get_character(char_id)
        if not char:
            return 0.0
        p = (char.persona_params or {}) or {}
        tags = " ".join([str(t) for t in (p.get("tags") or [])] + [str(p.get("label") or "")] + [str(p.get("personality") or "")])
        if any(w in tags for w in ("活泼", "外向", "话痨", "分享欲", "爱分享", "开朗", "热情", "e人")):
            return 0.25
    except Exception:
        pass
    return 0.0

def _is_night() -> bool:
    h = time.localtime().tm_hour
    return h >= 23 or h < 7

def _rel_key(user_id: str, char_id: str) -> str:
    return f"{user_id}:{char_id}"

def _get_relation(user_id: str, char_id: str) -> dict:
    """当前用户-角色关系：持久化优先，其次角色初始参数，最后默认 50"""
    k = _rel_key(user_id, char_id)
    st = _relations_store.get(k)
    if st:
        return st
    char = char_mgr.get_character(char_id)
    p = (char.persona_params if char else {}) or {}
    aff = _clamp_int(p.get("affinity"), 50)
    fri = _clamp_int(p.get("friendship"), 50)
    return {
        "affinity": aff,
        "friendship": fri,
        "relationship": (p.get("relationship") or "").strip(),
        "tier": _relation_tier(aff, fri),
    }

def _init_relation(user_id: str, char_id: str) -> dict:
    """首次接触该角色时初始化关系并持久化"""
    if not user_id or not char_id:
        return {}
    k = _rel_key(user_id, char_id)
    if k not in _relations_store:
        _relations_store[k] = _get_relation(user_id, char_id)
        _save_relations()
    return _relations_store[k]

def _apply_relation_delta(user_id: str, char_id: str, delta: dict | None, relationship: str = "") -> dict | None:
    """应用模型输出的关系变化（clamp 0-100）并持久化；层级变化时记入用户档案"""
    if not user_id or not char_id:
        return None
    rel = _init_relation(user_id, char_id)
    old_tier = rel.get("tier") or _relation_tier(_clamp_int(rel.get("affinity")), _clamp_int(rel.get("friendship")))
    changed = False
    today_key = time.strftime("%Y-%m-%d")
    # 每日变化额度：一条消息就把朋友聊成极亲密是很假的，这里做边际递减 + 每日上限
    daily_budget = rel.setdefault("day_budget", {})
    if daily_budget.get("date") != today_key:
        daily_budget = {"date": today_key, "spent": 0}
        rel["day_budget"] = daily_budget
    spent = int(daily_budget.get("spent", 0) or 0)
    for key, val in (("affinity", (delta or {}).get("affinity")), ("friendship", (delta or {}).get("friendship"))):
        if isinstance(val, (int, float)) and val:
            cur = int(rel.get(key, 50))
            raw = int(val)
            raw = max(-4, min(4, raw))  # 每轮单边最多 ±4
            # 边际递减：离 50 越远动得越慢；超过 75（接近极亲密）再减半
            factor = 1.0 - abs(cur - 50) / 100 * 0.65
            if cur >= 75 or cur <= 25:
                factor *= 0.5
            step = int(round(raw * factor))
            if step == 0:
                step = 1 if raw > 0 else -1
            # 每日累计变化上限 ±10
            if spent + abs(step) > 10:
                step = (10 - spent) if raw > 0 else -(10 - spent)
                if step == 0:
                    continue
            spent += abs(step)
            new_v = max(0, min(100, cur + step))
            if new_v != cur:
                rel[key] = new_v
                changed = True
    if spent != int(daily_budget.get("spent", 0)):
        daily_budget["spent"] = spent
        changed = True
    if relationship and relationship != rel.get("relationship"):
        rel["relationship"] = relationship[:120]
        changed = True
    new_tier = _relation_tier(_clamp_int(rel.get("affinity")), _clamp_int(rel.get("friendship")))
    if new_tier != old_tier:
        rel["tier"] = new_tier
        changed = True
        try:
            char = char_mgr.get_character(char_id)
            name = char.name if char else char_id
            mem_mgr.add_user_fact(user_id, f"和「{name}」的关系变成了「{new_tier}」（好感{rel.get('affinity')}/友情{rel.get('friendship')}）")
        except Exception:
            pass
    if changed:
        _save_relations()
    return {
        "affinity": rel.get("affinity", 50),
        "friendship": rel.get("friendship", 50),
        "tier": rel.get("tier") or new_tier,
        "relationship": rel.get("relationship", ""),
    }

def _relation_block(user_id: str, char_id: str) -> str:
    """注入提示词的【当前关系】文本（说话分寸随数值动态变化）"""
    rel = _init_relation(user_id, char_id)
    aff = _clamp_int(rel.get("affinity"), 50)
    fri = _clamp_int(rel.get("friendship"), 50)
    tier = rel.get("tier") or _relation_tier(aff, fri)
    rel_text = (rel.get("relationship") or "").strip()
    if not rel_text:
        rel_text = "还在互相了解的阶段" if tier == "刚认识" else tier
    return f"【当前关系（动态，由系统每轮更新，你说话的分寸要和它匹配，不要向用户复述数值）】好感度={aff}（{_relation_label(aff)}）| 友情值={fri}（{tier}）| 关系定位：{rel_text}"


__all__ = [
    "_apply_relation_delta",
    "_cancel_nudge",
    "_char_share_boost",
    "_clamp_int",
    "_get_relation",
    "_init_relation",
    "_is_clumsy_char",
    "_is_night",
    "_proactive_day_prob",
    "_proactive_night_prob",
    "_register_schedule",
    "_rel_key",
    "_relation_block",
    "_relation_label",
    "_relation_tier",
    "_reminder_due_time",
    "_save_proactive",
    "_save_relations",
    "_save_schedules",
    "char_mgr",
    "mem_mgr",
]
