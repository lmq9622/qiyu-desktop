# -*- coding: utf-8 -*-
"""Qiyu 睡眠窗口（移植自服务器版 companion/dispatch.py 的睡眠部分）。

解决「说了晚安又说没睡」的左右脑互搏：她回复里说了要去睡之后，把"睡眠窗口"
记下来（到她人设起床时间为止）。窗口内：
  - 提示词侧注入【睡眠状态】硬约束（不许再说"在呢 / 还没睡"）；
  - 主动消息侧不再开口（道别/提醒除外）。

桌面版是单用户、本机 Windows，这里只保留状态与判定逻辑，不搬服务器的夜间排队补发。
"""
from __future__ import annotations

import re
import time
from loguru import logger

from companion.state import save_user_store, _load_user_store

# key = "user_id::char_id" -> {"since": ts, "until": ts, "reason": str}
_sleep_store: dict = _load_user_store("sleep")

# 默认作息：23:00 睡、07:00 起（人设没写作息时用）
_DEFAULT_SLEEP = (23, 0)
_DEFAULT_WAKE = (7, 0)


def _save_sleep():
    try:
        save_user_store("sleep", _sleep_store)
    except Exception as e:
        logger.warning(f"[睡眠] 落盘失败: {e}")


def _parse_hm(t: str, default: tuple) -> tuple:
    try:
        h, m = str(t).split(":")
        return int(h) % 24, int(m) % 60
    except Exception:
        return default


def char_sleep_window(char) -> tuple:
    """取角色的作息（睡觉, 起床），没设置就给 23:00 / 07:00。"""
    try:
        pp = getattr(char, "persona_params", None) or {}
        sa = getattr(char, "sleep_at", "") or str(pp.get("sleep_at") or "") or "23:00"
        wa = getattr(char, "wake_at", "") or str(pp.get("wake_at") or "") or "07:00"
        return _parse_hm(sa, _DEFAULT_SLEEP), _parse_hm(wa, _DEFAULT_WAKE)
    except Exception:
        return _DEFAULT_SLEEP, _DEFAULT_WAKE


def _char_obj(char_id: str):
    """按 id 取角色对象（取不到就 None，调用方自己回退默认值）。"""
    try:
        from characters import get_character_manager as _gcm
        return _gcm().get_character(char_id)
    except Exception:
        return None


_SLEEP_SELF_RE = None
_SLEEP_BYE_RE = None
_SLEEP_OTHER_RE = None   # 第二人称"劝对方睡"（你也早点睡/早点休息…）


def says_going_to_sleep(text: str) -> bool:
    """回复文本里是不是"她自己要去睡了"（正则兜底，只在模型没给 sleeping 标记时用）。

    以前 "睡了/晚安" 不分主语地命中：她问一句「你睡了吗？」「你怎么还不睡了」
    都会把她自己锁进睡眠窗口到第二天早上，用户消息全被压住不回。现在按分句判断：
      · 问句（？/? 结尾，或以 吗/呢/么/没 收尾）不算——那是在问对方；
      · 带「你」又没有「我…睡」的分句不算——那是在叫对方睡；
      · 「睡了/睡啦」必须落在分句末尾（「我昨晚睡了三个小时」只是在讲事）。
    光一句「晚安」：只有它是整段回复的**最后一句**、且整段里没有在问问题、
    也没有在劝对方睡（「你也早点休息，晚安」是给对方的，不算她自己收尾）时才算她收尾。
    """
    global _SLEEP_SELF_RE, _SLEEP_BYE_RE, _SLEEP_OTHER_RE
    t = str(text or "").strip()
    if not t:
        return False
    if _SLEEP_SELF_RE is None:
        _tail = r"[呀啊哦噢哈嘛喔~～]*$"
        _SLEEP_SELF_RE = re.compile(
            r"(我(也|先|就|要|得|这就|准备|真的)?(先|要|去|回去)?睡(觉)?(了|啦|咯|喽|去了)"
            r"|先睡(了|啦|咯|喽)?|(^|那|先|也|就|要|该)去睡(觉)?(了|啦|咯|喽)|^睡(了|啦|咯|喽)"
            r"|困.{0,6}睡(了|啦|咯)|我困得不行.{0,6}睡.{0,2}"
            # 第一人称"困了/困得"（"我不困了"用否定前瞻挡掉，别反着判）
            r"|我[^，,。！!？?\n]{0,4}(?<!不)困(?:了|得))"
            r"[呀啊哦噢哈嘛喔~～]*(晚安)*" + _tail)
        _SLEEP_BYE_RE = re.compile(r"^(晚安)+" + _tail)
        # 第二人称劝睡：你(也/早点/快点…)…睡/休息/躺
        _SLEEP_OTHER_RE = re.compile(r"你[^，,。！!？?\n]{0,6}(?:早点|早点儿|快|赶紧|也)?(?:睡|休息|躺|眯)")
    parts = re.findall(r"[^，,。.！!？?\n…~～ ]+[？?]?", t)
    clauses = [p.strip() for p in parts if p.strip()]
    if not clauses:
        return False

    def _is_q(c: str) -> bool:
        return c.endswith(("？", "?")) or bool(re.search(r"(吗|呢|么|没)[呀啊哦]*$", c))

    has_q = any(_is_q(c) for c in clauses)
    # 整段里有没有在"劝对方睡觉"——有的话末尾的"晚安"是客套，不是她自己要睡
    has_other_sleep = bool(_SLEEP_OTHER_RE.search(t))
    for c in clauses:
        if _is_q(c):
            continue
        if "你" in c and not re.search(r"我.{0,6}睡", c):
            continue
        if _SLEEP_SELF_RE.search(c):
            return True
    last = clauses[-1]
    return (not has_q) and (not has_other_sleep) and bool(_SLEEP_BYE_RE.search(last))


def mark_sleep_from_reply(user_id: str, char_id: str, reply_text: str,
                          explicit: bool = False, reason: str = "",
                          now=None, char=None) -> None:
    """记录"她要去睡了"的窗口（到她人设里的起床时间，取不到才用早 7 点）。

    explicit=True 表示模型在 JSON 里显式声明了这轮就是睡前收尾（比正则可靠，直接生效）；
    否则退回 says_going_to_sleep() 从回复文本里找"我先睡了 / 晚安"这类说法兜底。
    """
    try:
        if not user_id or not char_id:
            return
        if not explicit and not says_going_to_sleep(reply_text):
            return
        import datetime as _dt
        now = now or _dt.datetime.now()
        if 7 <= now.hour < 21:
            # 白天说的"我去睡了"多半是午睡/小憩：只锁半小时，
            # 别把整个下午的消息都排队到第二天早上。
            wake = now + _dt.timedelta(minutes=30)
        else:
            # 醒来时间跟她自己的作息走，别一刀切 7 点。
            wh, wm = _DEFAULT_WAKE
            try:
                ch = char if char is not None else _char_obj(char_id)
                if ch is not None:
                    (_s, (wh, wm)) = char_sleep_window(ch)
            except Exception:
                wh, wm = _DEFAULT_WAKE
            wake = now.replace(hour=wh, minute=wm, second=0, microsecond=0)
            if wake <= now:
                wake += _dt.timedelta(days=1)
        _sleep_store[f"{user_id}::{char_id}"] = {
            "since": now.timestamp(),
            "until": wake.timestamp(),
            "reason": (reason or ("模型判定这轮是睡前收尾" if explicit else "回复里说了要去睡"))[:160],
        }
        _save_sleep()
        logger.info(f"[睡眠] {user_id}/{char_id} 进入睡眠窗口，到 {wake:%m-%d %H:%M} 前不回消息"
                    f"（explicit={explicit}）")
    except Exception as e:
        logger.warning(f"[睡眠] 记录失败: {e}")


def clear_sleep(user_id: str, char_id: str) -> None:
    """清掉睡眠窗口（用户把角色叫起来 / 手动重置时用）。"""
    if _sleep_store.pop(f"{user_id}::{char_id}", None) is not None:
        _save_sleep()


def is_asleep(user_id: str, char_id: str, now=None) -> bool:
    """现在是否处于她的睡眠窗口（真正困了在睡 → 不回复）。"""
    try:
        ts = float(now) if now is not None else time.time()
        k = f"{user_id}::{char_id}"
        rec = _sleep_store.get(k) or {}
        try:
            until = float(rec.get("until") or 0)
        except Exception:
            until = 0.0
        if not until:
            return False
        if ts >= until:
            _sleep_store.pop(k, None)
            _save_sleep()
            return False
        return True
    except Exception:
        return False


def sleep_info(user_id: str, char_id: str, now=None) -> dict:
    """给提示词用的睡眠状态：她什么时候睡的、自己怎么说的、什么时候醒。"""
    try:
        ts = float(now) if now is not None else time.time()
        rec = _sleep_store.get(f"{user_id}::{char_id}") or {}
        try:
            until = float(rec.get("until") or 0)
        except Exception:
            until = 0.0
        if not until or ts >= until:
            return {"asleep": False}
        since = float(rec.get("since") or 0)
        return {
            "asleep": True,
            "since": since,
            "until": until,
            "since_text": time.strftime("%H:%M", time.localtime(since)) if since else "",
            "until_text": time.strftime("%H:%M", time.localtime(until)),
            "reason": str(rec.get("reason") or ""),
        }
    except Exception:
        return {"asleep": False}


def sleep_prompt_block(user_id: str, char_id: str, char=None, now=None) -> str:
    """把"你已经睡了"这件事明确写进提示词——否则模型只能靠上下文猜，就会出现
    "前面说了晚安、后面又说我没睡"的左右脑互搏。"""
    try:
        info = sleep_info(user_id, char_id, now=now)
        if info.get("asleep"):
            return (
                f"【睡眠状态（内部，最高优先级的一致性约束）】你已经在 {info.get('since_text') or '刚才'} "
                f"说了要去睡（{info.get('reason') or '自己说的晚安'}），**现在是你的睡眠时间，你已经睡着了**（预计 "
                f"{info.get('until_text')} 醒）。规则：\n"
                "- 睡着之后对方发来的消息你收不到，系统会替你压住、留到你醒来；这一轮如果你被要求开口，"
                "只能是早上醒来后「刚拿到手机看到」的说法（『昨晚睡着了』『刚看到』）。\n"
                "- **绝对不许**再出现「没睡」「在呢」「还没」「你咋还没睡」这类『我还醒着』的回复——那等于跟自己刚说的晚安打架。\n"
                "- 也不要解释这套机制，就是很自然地当一个刚睡醒的人。"
            )
    except Exception:
        pass
    return ""


__all__ = [
    "char_sleep_window",
    "clear_sleep",
    "is_asleep",
    "mark_sleep_from_reply",
    "says_going_to_sleep",
    "sleep_info",
    "sleep_prompt_block",
]
