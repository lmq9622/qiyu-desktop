# -*- coding: utf-8 -*-
"""Qiyu 主动消息调度器（Proactive Scheduler，M4）。

规格（十八/十九/二十/三十五/三十六）：
禁止简单 Timer → Generate；必须由
Scheduler + Context + Memory + Recent conversation + Last topic + Relationship
+ User activity + Duplicate detection + Cooldown 共同决定是否打扰用户。

主动消息先判断「现在适不适合打扰」：冷淡信号 / 冷却未到 / 正在聊天 / 人设在忙 /
耐心不足 / 意愿低 / 有未完成任务 / 心情差 / 每日上限 / 概率未命中 → 一律取消本次主动消息。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass

from companion.state import _proactive_store
from companion.relations import (_char_share_boost, _init_relation, _is_night,
                                 _proactive_daily_cap, _proactive_day_prob,
                                 _proactive_night_prob, _save_proactive,
                                 persona_busy_now)
from companion.conv import _context_gate, _conv_state
from companion.emotions import _mood_is_great
from companion.behavior import _topic_similarity


@dataclass
class ProactiveDecision:
    """调度器的单次决策结果。"""
    allowed: bool
    reason: str = ""            # 拦截原因（cooldown / context_gate / probability / ok …）
    night: bool = False
    probability: float = 0.0    # 本次是否命中的概率（仅诊断用）


class ProactiveScheduler:
    """主动消息调度器：综合冷淡信号 / 冷却 / 上下文门 / 关系 / 耐心 / 心情 / 概率决定是否打扰。"""

    BASE_COOLDOWN = 2700     # 45 分钟基础冷却：真人不会每 7 分钟就找你一次
    MIN_COOLDOWN = 1800      # 关系很好时最短 30 分钟
    CONV_COOLDOWN = 3600     # 刚结束对话后至少隔 1 小时
    ROLL_WINDOW = 900        # 主动概率每 15 分钟最多掷一次骰子

    def __init__(self) -> None:
        self._last_failed_roll: dict = {}   # key -> 上次没掷中的时间
        self._pending_greet: dict = {}       # key -> (标记名, 日期)：放行了早安/晚安，等 record_attempt 落标记

    @staticmethod
    def key(user_id: str, char_id: str) -> str:
        return f"{user_id}::{char_id}"

    def cooldown_seconds(self, user_id: str, char_id: str, now: float | None = None) -> float:
        """动态冷却：基础 7 分钟；刚结束对话不加长；关系好（好感≥70）→ 最短 3 分钟。"""
        now = now or time.time()
        cooldown = float(self.BASE_COOLDOWN)
        cs = _conv_state(user_id, char_id)
        ended_at = float(cs.get("ended_at") or 0)
        if ended_at and now - ended_at < 1800:
            cooldown = max(cooldown, float(self.CONV_COOLDOWN))
        rel = _init_relation(user_id, char_id)
        affinity = int(rel.get("affinity", 50) or 50)
        if affinity >= 70:
            cooldown = min(cooldown, float(self.MIN_COOLDOWN))
        return cooldown

    def duplicate_detected(self, user_id: str, char_id: str, text: str) -> bool:
        """§19：与上次主动消息语义过相似（换说法也认），禁止重复发送。"""
        if not text:
            return False
        prev_text = ((_proactive_store.get(self.key(user_id, char_id)) or {}).get("last_text") or "").strip()
        return bool(prev_text and _topic_similarity(prev_text, text) > 0.6)

    def _in_goodnight_window(self, char, d) -> bool:
        """晚安时间窗跟着她的作息走：[睡觉前 1.5 小时, 睡觉后 0.5 小时]；取不到作息就 22~24 点。"""
        try:
            if char is not None:
                from companion.sleep import char_sleep_window
                (sh, sm), _w = char_sleep_window(char)
                st = sh * 60 + sm
                cur = d.hour * 60 + d.minute
                # 跨零点也要算对：看 cur 距睡觉点的偏移（-90 ~ +30 分钟）
                off = (cur - st + 12 * 60) % (24 * 60) - 12 * 60
                return -90 <= off <= 30
        except Exception:
            pass
        return 22 <= d.hour <= 23

    def evaluate(self, user_id: str, char_id: str, now: float | None = None) -> ProactiveDecision:
        """完整判断现在适不适合主动打扰用户。"""
        if not user_id or not char_id:
            return ProactiveDecision(False, reason="no_user_or_char")
        now = now or time.time()
        # 0) 冷淡信号：用户上次嫌烦 → 即时停手 24h，不再消耗概率
        _pst0 = _proactive_store.get(self.key(user_id, char_id)) or {}
        if int(_pst0.get("cold_until") or 0) > int(now):
            return ProactiveDecision(False, reason="cold_signal")
        # 1) 冷却：未到冷却时间一律不主动
        last = (_proactive_store.get(self.key(user_id, char_id)) or {}).get("last_at") or 0
        if now - last < self.cooldown_seconds(user_id, char_id, now):
            return ProactiveDecision(False, reason="cooldown")
        # 2) Context Gate：正在聊天 / 冷却 / 耐心不足 / 意愿低 / 有未完成任务
        allowed, reason = _context_gate(user_id, char_id, now)
        if not allowed:
            return ProactiveDecision(False, reason=reason or "context_gate")
        rel = _init_relation(user_id, char_id)
        affinity = int(rel.get("affinity", 50) or 50)
        # 2.3) 人设作息：学生工作日白天在上课、上班族工作日白天在上班 —— 这时候不该"闲得找话"。
        # 放在早安/晚安前面：不然学生工作日 8~10 点在上课也会发一句"早安"。
        _ch_obj = None
        try:
            from characters import get_character_manager as _gcm
            _ch_obj = _gcm().get_character(char_id)
            if persona_busy_now(_ch_obj, now):
                return ProactiveDecision(False, reason="persona_busy")
        except Exception:
            pass
        # 2.4) 先看硬上限：当天已经发够 / 连续没回 → 连早安晚安也不发
        _pst_e = _proactive_store.get(self.key(user_id, char_id)) or {}
        _cap_e = _proactive_daily_cap(affinity)
        if int(_pst_e.get("no_reply_streak") or 0) >= _cap_e:
            return ProactiveDecision(False, reason="no_reply_streak")
        if _pst_e.get("day") == time.strftime("%Y-%m-%d") and int(_pst_e.get("day_count") or 0) >= _cap_e:
            return ProactiveDecision(False, reason="daily_cap")
        _cs_e = _conv_state(user_id, char_id)
        _lp_e = float(_cs_e.get("last_proactive_at") or 0)
        _lu_e = float(_cs_e.get("last_user_at") or 0)
        if _lp_e and _lp_e > _lu_e and now - _lp_e < 2.5 * 3600:
            return ProactiveDecision(False, reason="unanswered_wait")
        # 2.5) 早安 / 晚安：每天各一次（亲密值 ≥50 才做），时间窗内优先放行
        try:
            import datetime as _dt
            _d = _dt.datetime.fromtimestamp(now)
            _h = _d.hour
            _today0 = _d.strftime("%Y-%m-%d")
            _k = self.key(user_id, char_id)
            _pst0 = _proactive_store.get(_k) or {}
            # 两个标记都是"某一天"的：记录日期不是今天 → 昨天的早安/晚安作废，
            # 否则昨天发过早安，今天早安就被跳过。早安和晚安各看各的标记，
            # 发过早安不能挡住当天的晚安。
            if str(_pst0.get("greet_day") or "") != _today0 and (
                    _pst0.get("morning_sent") or _pst0.get("night_sent")):
                _pst0 = dict(_pst0)
                _pst0.pop("morning_sent", None)
                _pst0.pop("night_sent", None)
                _proactive_store[_k] = _pst0
                try:
                    _save_proactive()
                except Exception:
                    pass
            if int(affinity) >= 50:
                if 7 <= _h < 10 and not _pst0.get("morning_sent"):
                    self._pending_greet[_k] = ("morning_sent", _today0)
                    return ProactiveDecision(True, reason="morning", night=False, probability=1.0)
                if self._in_goodnight_window(_ch_obj, _d) and not _pst0.get("night_sent"):
                    self._pending_greet[_k] = ("night_sent", _today0)
                    return ProactiveDecision(True, reason="goodnight", night=True, probability=1.0)
        except Exception:
            pass
        # 深夜 0~7 点不主动找人（早安/晚安已在上面单独处理，深夜不随机打扰）
        try:
            _hour = time.localtime(now).tm_hour
            if 0 <= _hour < 7:
                return ProactiveDecision(False, reason="quiet_hours")
        except Exception:
            pass
        # 还没有任何用户消息的空会话，不主动开第一句
        _cs0 = _conv_state(user_id, char_id)
        if not float(_cs0.get("last_user_at") or 0):
            return ProactiveDecision(False, reason="no_history")
        # 3) 概率：好感度驱动 + 人设加成 + 心情加成（心情差已被 Context Gate 拦截）
        # 每日上限：真正的停手线是"连续没被回复"（用户有回应就清零）；
        # 日上限只做兜底（放宽到 2 倍），避免极端情况下刷屏。
        # 最高好感 cap=15 → 上限 30 条/天；单用户级桌面上限别超过 30。
        _today = time.strftime("%Y-%m-%d")
        _pst = _proactive_store.get(self.key(user_id, char_id)) or {}
        # 直接看时间戳：上一条主动之后没有用户消息 → 至少隔 2.5 小时，避免 streak 被误清零时连发
        _cs = _conv_state(user_id, char_id)
        _last_pro = float(_cs.get("last_proactive_at") or 0)
        _last_user = float(_cs.get("last_user_at") or 0)
        if _last_pro and _last_pro > _last_user and now - _last_pro < 2.5 * 3600:
            return ProactiveDecision(False, reason="unanswered_wait")
        _cap = _proactive_daily_cap(affinity)
        _streak = int(_pst.get("no_reply_streak") or 0)
        if _streak >= _cap:
            return ProactiveDecision(False, reason="no_reply_streak")
        # 上一条主动还没人回：至少隔 2.5 小时再试第二句；真人不会连发
        if _streak >= 1 and now - float(_pst.get("last_at") or 0) < 2.5 * 3600:
            return ProactiveDecision(False, reason="unanswered_wait")
        if int(_pst.get("penalty_until") or 0) > int(now):
            _cap = max(1, _cap // 2)
        if _pst.get("day") == _today and int(_pst.get("day_count") or 0) >= _cap:
            return ProactiveDecision(False, reason="daily_cap")
        night = _is_night()
        prob = _proactive_night_prob(affinity) if night else _proactive_day_prob(affinity)
        prob = min(0.9, prob + _char_share_boost(char_id) + (0.2 if _mood_is_great(user_id, char_id) else 0.0))
        # 概率按"每 10 分钟一次判定"设计的，但主循环 20 秒就调一次：没中也不记冷却的话，
        # 这 10 分钟里会掷几十次骰子，几乎必中，人设的主动性高低就没有区别了。
        # 所以没中 → 这 10 分钟内不再掷（只在内存里记，重启丢了也无妨）。
        _k = self.key(user_id, char_id)
        if now - float(self._last_failed_roll.get(_k) or 0) < self.ROLL_WINDOW:
            return ProactiveDecision(False, reason="roll_window", night=night, probability=prob)
        if random.random() > prob:
            self._last_failed_roll[_k] = now
            return ProactiveDecision(False, reason="probability", night=night, probability=prob)
        return ProactiveDecision(True, reason="ok", night=night, probability=prob)

    def record_attempt(self, user_id: str, char_id: str) -> None:
        """触发主动生成前登记时间（无论生成是否成功都进入冷却，避免失败后立刻重试）。"""
        _k = self.key(user_id, char_id)
        _entry = dict(_proactive_store.get(_k) or {})
        _entry["last_at"] = time.time()
        # 刚放行的是早安/晚安 → 这里就把当天标记记上。发送侧只在 7~10 点 / 22 点后才记，
        # 晚安窗口按作息提前到 21 点多时它记不上，会每 7 分钟重复一句晚安。
        _g = self._pending_greet.pop(_k, None)
        if _g:
            _flag, _day = _g
            if str(_entry.get("greet_day") or "") != _day:
                _entry.pop("morning_sent", None)
                _entry.pop("night_sent", None)
            _entry[_flag] = True
            _entry["greet_day"] = _day
        _proactive_store[_k] = _entry
        _save_proactive()

    def record_sent(self, user_id: str, char_id: str, text: str) -> None:
        """真正发送后登记文本，供下次重复检测使用。"""
        entry = dict(_proactive_store.get(self.key(user_id, char_id)) or {})
        entry["last_at"] = time.time()
        entry["last_text"] = (text or "")[:200]
        _proactive_store[self.key(user_id, char_id)] = entry
        _save_proactive()


proactive_scheduler = ProactiveScheduler()


__all__ = [
    "ProactiveDecision",
    "ProactiveScheduler",
    "proactive_scheduler",
]
