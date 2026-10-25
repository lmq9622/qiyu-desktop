# -*- coding: utf-8 -*-
"""Qiyu 关系/日程/主动概率状态（从 demo.py 迁移，M1）。"""
import json
import re
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
    if task.get("kind") == "reminder":
        # 同一件事、同一个时间只留一个：模型回复为空重试时会把 schedules 再执行一遍，
        # 以前不去重 → 到点会发两条提醒。
        _p = task.get("payload") or {}
        for t in tasks:
            _tp = t.get("payload") or {}
            if (t.get("kind") == "reminder" and not t.get("done")
                    and _tp.get("char_id") == _p.get("char_id")
                    and _tp.get("text") == _p.get("text")
                    and abs(float(t.get("due_at") or 0) - float(task.get("due_at") or 0)) < 300):
                return
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
    """白天每次判定的主动概率（调度器约每 10 分钟才掷一次骰子，好感越高越勤：约 34%~62%）"""
    return 0.34 + (affinity / 100) * 0.28

def _proactive_night_prob(affinity: int) -> float:
    """夜间主动概率：3%~10%，好感度越高越可能（凌晨求安慰/分享心事）"""
    return 0.03 + (affinity / 100) * 0.07


# 人设职业/身份 → 不该闲聊的时段（学生上课、上班族上班、医生值班、夜班睡觉……）
# 按顺序匹配，第一个命中就定身份：夜班放最前，否则"夜班护士"会被"护士"先认成白班医护
_PERSONA_OCCUPATION_HINTS = (
    ("nightshift", ("夜班", "上夜", "值夜", "半夜上班", "通宵班")),
    ("student", ("学生", "上学", "在校", "高三", "高二", "高一", "初三", "初二", "初一", "初中",
                 "大学", "大一", "大二", "大三", "大四", "研究生", "上课", "school", "班级", "作业", "备考", "考研")),
    ("teacher", ("老师", "教师", "带班", "班主任", "教授", "讲师")),
    ("medical", ("护士", "医生", "大夫", "护师", "实习医", "住院部", "值班室")),
    ("office", ("上班", "公司", "职场", "社畜", "打工人", "职员", "白领", "工位", "office",
                "程序员", "开发", "码农", "工程师", "设计师", "会计", "销售", "公务员", "银行")),
    ("nightshift", ("夜店", "酒吧")),                 # 在夜店/酒吧上班；排在学生、上班族之后
    ("service", ("店员", "服务员", "导购", "外卖", "骑手", "快递", "司机", "网约车", "美容", "美发")),
    ("performer", ("主播", "演员", "艺人", "模特", "歌手", "自媒体", "开播")),
    ("free", ("自由职业", "在家办公", "全职太太", "家庭主妇", "无业", "待业", "退休")),
)

# 身份 → 工作日"本来就在忙"的时段（小时区间，左闭右开）
_OCCUPATION_BUSY_HOURS = {
    "student": (8, 17),
    "teacher": (7, 17),
    "medical": (8, 18),
    "office": (9, 18),
    "service": (10, 21),
}

_GRADUATED_RE = re.compile(
    r"(?:大学|大专|本科|研究生|硕士|博士|高中|初中|中专)(?:已经|早就|刚)?毕业[了后]?|毕业于\S{0,12}|已毕业|毕业[了后多]"
)


def strip_graduated(text: str) -> str:
    """去掉"大学毕业 / 毕业于XX大学"这类经历描述，免得上班族被"大学"认成学生。"""
    return _GRADUATED_RE.sub(" ", str(text or ""))


def persona_occupation(char=None) -> str:
    """从人设/简历里判断这个角色的身份（student/office/teacher/medical/service/...）。"""
    try:
        if char is None:
            return ""
        parts = [getattr(char, "persona", "") or "", getattr(char, "description", "") or ""]
        rp = getattr(char, "persona_params", None) or {}
        rs = getattr(char, "resume", None) or {}
        try:
            parts.append(str(rp.get("occupation") or ""))
            parts.append(str(rs.get("职业") or "") + str(rs.get("身份") or "") + str(rs.get("年龄") or ""))
        except Exception:
            pass
        text = strip_graduated(" ".join(parts))
        for name, kws in _PERSONA_OCCUPATION_HINTS:
            if any(k in text for k in kws):
                return name
    except Exception:
        pass
    return ""


def is_school_holiday(d=None) -> bool:
    """(粗略) 学生寒暑假：寒假 1/10~2/20、暑假 7/1~8/31 —— 放假期间不受"工作日白天在上课"限制。"""
    try:
        import datetime as _dt
        d = d or _dt.datetime.now()
        m, day = d.month, d.day
        if m == 1 and day >= 10:
            return True
        if m == 2 and day <= 20:
            return True
        if m in (7, 8):
            return True
    except Exception:
        pass
    return False


def persona_busy_now(char=None, now=None) -> bool:
    """现在这个点，按人设身份"本来就在忙"吗（学生工作日上课、上班族工作日上班…）。"""
    try:
        import time as _t, datetime as _dt
        occ = persona_occupation(char)
        if not occ or occ == "free":
            return False
        d = _dt.datetime.fromtimestamp(float(now) if now else _t.time())
        h = d.hour
        if occ == "nightshift":
            return h >= 22 or h < 7                      # 夜班：夜里到清晨在忙
        if occ == "performer":
            return 18 <= h < 23                          # 主播/艺人：晚上开播
        if occ == "student":
            if is_school_holiday(d):                     # 寒暑假 → 不按上课时间限制
                return False
            return d.weekday() < 5 and 8 <= h < 17
        win = _OCCUPATION_BUSY_HOURS.get(occ)
        if not win:
            return False
        if d.weekday() >= 5:
            return False                                 # 周末不算忙
        return win[0] <= h < win[1]
    except Exception:
        pass
    return False


def _proactive_daily_cap(affinity: int) -> int:
    """每天最多主动几次（硬上限）。

    真人不会一天单向发十几条：普通关系 1 条，熟一点 2 条，很近 3 条。
    """
    a = int(affinity or 0)
    if a <= 40:
        return 1
    if a <= 70:
        return 2
    return 3


_COLD_WORDS = (
    "别发", "别找", "别来", "别闹", "别烦", "别说了", "别打扰", "少发", "少找", "少来",
    "烦了", "真烦", "好烦", "很烦", "太烦", "烦人", "嫌烦", "烦死", "能不能别",
    "不用了", "不用回", "不想聊", "不想回", "没意思", "没话找话", "无聊", "腻了",
    "打扰", "闭嘴", "闭麦", "够了", "滚",
)
_WARM_HINTS = ("？", "?", "哈哈", "想你", "想聊", "在干嘛", "干嘛呢", "好呀", "嗯嗯", "来呀")


def reset_no_reply_streak(user_id: str, char_id: str) -> None:
    """用户开口了（不管隔了多久）→ "连续没人回"的找话计数清零。

    以前只有 1 小时内回主动消息才清零，用户隔天才回时计数卡在上限，
    调度器就再也不让角色主动找话了。
    """
    key = f"{user_id}::{char_id}"
    entry = _proactive_store.get(key)
    if entry and int(entry.get("no_reply_streak") or 0):
        entry = dict(entry)
        entry["no_reply_streak"] = 0
        _proactive_store[key] = entry
        _save_proactive()


def note_proactive_reply(user_id: str, char_id: str, text: str) -> str:
    """用户对主动消息的回复反馈：冷淡→即时停手(24h)，连续冷淡→72h 减半频率；热情→清零。
    返回 cold / warm / neutral"""
    key = f"{user_id}::{char_id}"
    entry = dict(_proactive_store.get(key) or {})
    now = int(time.time())
    t = (text or "").strip()
    if not t:
        return "neutral"
    cold = any(w in t for w in _COLD_WORDS)
    warm = (not cold) and ((len(t) >= 10) or any(w in t for w in _WARM_HINTS))
    # 用户有回应 → 连续无回复计数清零（找话次数按"没人回"算，不是按每天算）
    entry["no_reply_streak"] = 0
    if warm:
        entry["cold_streak"] = 0
        entry.pop("penalty_until", None)
        _proactive_store[key] = entry
        _save_proactive()
        return "warm"
    if cold:
        # 只有命中明确"嫌烦"词（别烦/烦死了/别发了/闭嘴/滚…）才记冷淡。
        # 短回复（"谢谢/对/收到/嗯/没事/好的"）不再算冷淡 —— 以前 len<=3 就当冷，
        # 连续两条短回复 → cold_until +24h、penalty 72h，纯属误伤。
        streak = int(entry.get("cold_streak") or 0) + 1
        entry["cold_streak"] = streak
        entry["cold_until"] = max(int(entry.get("cold_until") or 0), now + 86400)
        if streak >= 2:
            entry["penalty_until"] = now + 259200
        _proactive_store[key] = entry
        _save_proactive()
        return "cold"
    # 短回复最多算"中性"：不清零惩罚、也不惩罚（no_reply_streak 已在上面清零不影响）
    return "neutral"


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
    "_proactive_daily_cap",
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
    "is_school_holiday",
    "mem_mgr",
    "note_proactive_reply",
    "persona_busy_now",
    "persona_occupation",
    "reset_no_reply_streak",
    "strip_graduated",
]
