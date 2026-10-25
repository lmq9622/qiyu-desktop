# -*- coding: utf-8 -*-
"""主动消息/关系/睡眠 相关修复的单元测试（标准库 unittest，无网络/无模型）。

运行：
    cd D:/tmp/qiyu-desktop-license && python -X utf8 -m unittest tests.test_proactive -v

约定：先把 QIYU_DATA_DIR 指到临时目录再 import，避免污染真实 data/。
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

# —— 必须在 import companion.* 之前设好数据目录 ——
_TMP = tempfile.mkdtemp(prefix="qiyu_test_")
os.environ["QIYU_DATA_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import companion.relations as R          # noqa: E402
import companion.conv as C               # noqa: E402
import companion.scheduler as S          # noqa: E402
import companion.sleep as SL             # noqa: E402
from companion.state import _proactive_store, _schedules_store, _conv_store  # noqa: E402


def _ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi).timestamp()


# 2026-10-14 是周三 —— 用于"工作日白天"相关的确定性断言
WED = (2026, 10, 14)


class FakeChar:
    """最小可用的人设替身，只带 persona_occupation 会读的字段。"""

    def __init__(self, persona="", description="", params=None, resume=None):
        self.persona = persona
        self.description = description
        self.persona_params = params or {}
        self.resume = resume or {}


class TestReminderDedup(unittest.TestCase):
    """任务 1：同一件事/同一时间的提醒只留一条（空回复重试不再发两条）。"""

    def setUp(self):
        _schedules_store.clear()

    def test_same_reminder_registered_once(self):
        due = time.time() + 3600
        task = {"kind": "reminder", "due_at": due,
                "payload": {"text": "八点叫我吃药", "char_id": "c1"}}
        R._register_schedule("u1", dict(task))
        R._register_schedule("u1", dict(task))          # 模拟空回复重试再跑一遍
        self.assertEqual(len(_schedules_store["u1"]), 1)

    def test_different_text_not_deduped(self):
        due = time.time() + 3600
        R._register_schedule("u1", {"kind": "reminder", "due_at": due,
                                    "payload": {"text": "吃药", "char_id": "c1"}})
        R._register_schedule("u1", {"kind": "reminder", "due_at": due,
                                    "payload": {"text": "开会", "char_id": "c1"}})
        self.assertEqual(len(_schedules_store["u1"]), 2)

    def test_far_apart_time_not_deduped(self):
        R._register_schedule("u1", {"kind": "reminder", "due_at": time.time() + 3600,
                                    "payload": {"text": "吃药", "char_id": "c1"}})
        R._register_schedule("u1", {"kind": "reminder", "due_at": time.time() + 7200,
                                    "payload": {"text": "吃药", "char_id": "c1"}})
        self.assertEqual(len(_schedules_store["u1"]), 2)

    def test_nudge_not_deduped(self):
        R._register_schedule("u1", {"kind": "nudge", "due_at": time.time() + 300,
                                    "payload": {"context": "x", "char_id": "c1"}})
        R._register_schedule("u1", {"kind": "nudge", "due_at": time.time() + 300,
                                    "payload": {"context": "x", "char_id": "c1"}})
        self.assertEqual(len(_schedules_store["u1"]), 2)


class TestInteractionNeed(unittest.TestCase):
    """任务 2：无模型输入时聊天欲不再恒低；连续没回计数能被用户回归清零。"""

    def setUp(self):
        _conv_store.clear()
        _proactive_store.clear()

    def test_no_parsed_not_stuck_low(self):
        # 空闲 8 小时、关系正常、没有模型 parsed → 旧实现 mi=0 会拉到 0~1
        cs = C._conv_state("u1", "c1")
        cs["last_user_at"] = time.time() - 8 * 3600
        need = C._interaction_need_value("u1", "c1", None, "")
        self.assertGreaterEqual(need, 2)

    def test_parsed_still_has_effect(self):
        cs = C._conv_state("u1", "c1")
        cs["last_user_at"] = time.time() - 60
        low = C._interaction_need_value("u1", "c1", {"interaction_need": 0}, "嗯")
        high = C._interaction_need_value("u1", "c1", {"interaction_need": 4}, "想你了")
        self.assertGreaterEqual(high, low)

    def test_reset_no_reply_streak(self):
        _proactive_store["u1::c1"] = {"last_at": 1, "no_reply_streak": 9}
        R.reset_no_reply_streak("u1", "c1")
        self.assertEqual(_proactive_store["u1::c1"]["no_reply_streak"], 0)

    def test_user_message_resets_streak(self):
        # 关键：用户隔天回来开口 → 计数清零（调用点在 conv._update_conv_state）
        _proactive_store["u1::c1"] = {"last_at": 1, "no_reply_streak": 9}
        C._update_conv_state("u1", "c1", "user_message", None, "在吗")
        self.assertEqual(_proactive_store["u1::c1"]["no_reply_streak"], 0)

    def test_cold_reply_sets_cold_until(self):
        cs = C._conv_state("u1", "c1")
        cs["last_proactive_at"] = time.time()
        C._update_conv_state("u1", "c1", "user_message", None, "别发了 很烦")
        self.assertGreater(int(_proactive_store["u1::c1"].get("cold_until") or 0), time.time())

    def test_warm_reply_clears_penalty(self):
        _proactive_store["u1::c1"] = {"last_at": 1, "cold_streak": 3,
                                      "penalty_until": time.time() + 9999}
        cs = C._conv_state("u1", "c1")
        cs["last_proactive_at"] = time.time()
        C._update_conv_state("u1", "c1", "user_message", None, "哈哈我今天超开心的想跟你聊")
        self.assertEqual(int(_proactive_store["u1::c1"].get("penalty_until") or 0), 0)

    # --- 审查后修复：短回复（谢谢/对/收到/嗯/没事/好的）不再被误判冷淡 ---

    def test_short_replies_are_neutral_not_cold(self):
        for w in ("谢谢", "对", "收到", "嗯", "没事", "好的"):
            _proactive_store.clear()
            tag = R.note_proactive_reply("u1", "c1", w)
            self.assertEqual(tag, "neutral", w)
            self.assertFalse(_proactive_store.get("u1::c1", {}).get("cold_until"), w)

    def test_two_short_replies_never_mark_cold(self):
        """旧实现：连续两条短回复就 cold_until +24h、penalty 72h —— 现在不该再发生。"""
        _proactive_store.clear()
        for _ in range(2):
            R.note_proactive_reply("u1", "c1", "谢谢")
        entry = _proactive_store.get("u1::c1") or {}
        self.assertFalse(entry.get("cold_until"))
        self.assertFalse(entry.get("penalty_until"))

    def test_explicit_cold_words_still_mark_cold(self):
        for w in ("别发了 很烦", "烦死了", "闭嘴", "滚", "能不能别发了", "别烦我"):
            _proactive_store.clear()
            self.assertEqual(R.note_proactive_reply("u1", "c1", w), "cold", w)
            self.assertGreater(int(_proactive_store["u1::c1"].get("cold_until") or 0), time.time())


class TestScheduler(unittest.TestCase):
    """任务 3：冷却/掷骰窗口/冷淡信号/每日上限/早安。"""

    def setUp(self):
        _proactive_store.clear()
        _schedules_store.clear()
        _conv_store.clear()
        S.proactive_scheduler._last_failed_roll.clear()
        S.proactive_scheduler._pending_greet.clear()
        self.s = S.proactive_scheduler
        self.u, self.c = "u2", "c2"
        self.key = f"{self.u}::{self.c}"

    def _recent_conv(self, hour=15):
        """把会话状态摆成"可以主动"的样子：非 ACTIVE、久未互动、无冷却。"""
        cs = C._conv_state(self.u, self.c)
        now = _ts(*WED, hour)
        cs.update({"conv_state": "AVAILABLE_FOR_PROACTIVE", "last_user_at": now - 3600,
                   "last_ai_at": now - 3600, "last_proactive_at": 0, "ended_at": 0,
                   "cooldown_until": 0, "unanswered_pending": None})
        return now

    def test_base_cooldown_45min(self):
        now = self._recent_conv(15)
        self.assertEqual(self.s.cooldown_seconds(self.u, self.c, now), 2700.0)

    def test_cold_signal_blocks(self):
        now = self._recent_conv(15)
        _proactive_store[self.key] = {"last_at": now - 3000, "cold_until": int(now) + 3600}
        d = self.s.evaluate(self.u, self.c, now=now)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "cold_signal")

    def test_roll_window_prevents_spam(self):
        now = self._recent_conv(15)
        _proactive_store[self.key] = {"last_at": now - 3000}
        with mock.patch("companion.scheduler.random.random", return_value=0.99):
            first = self.s.evaluate(self.u, self.c, now=now)
            second = self.s.evaluate(self.u, self.c, now=now + 1)
        self.assertEqual(first.reason, "probability")
        self.assertEqual(second.reason, "roll_window")

    def test_daily_cap(self):
        now = self._recent_conv(15)
        today = time.strftime("%Y-%m-%d")
        _proactive_store[self.key] = {"last_at": now - 3000, "day": today, "day_count": 999}
        d = self.s.evaluate(self.u, self.c, now=now)
        self.assertEqual(d.reason, "daily_cap")

    def test_daily_cap_never_exceeds_30(self):
        """审查后修复：倍数 3→2；最高好感(>=91)下单用户每日上限 ≤30。"""
        cap_top = R._proactive_daily_cap(100)
        self.assertLessEqual(max(cap_top * 2, cap_top + 2), 30)
        # 逐档检查也不超过 30
        for a in (0, 25, 45, 70, 85, 95, 100):
            c = R._proactive_daily_cap(a)
            self.assertLessEqual(max(c * 2, c + 2), 30, f"affinity={a}")

    def test_no_reply_streak_cap(self):
        now = self._recent_conv(15)
        _proactive_store[self.key] = {"last_at": now - 3000, "day": time.strftime("%Y-%m-%d"),
                                      "day_count": 1, "no_reply_streak": 7}
        d = self.s.evaluate(self.u, self.c, now=now)
        self.assertEqual(d.reason, "no_reply_streak")

    def test_morning_once_per_day(self):
        now = self._recent_conv(8)
        _proactive_store[self.key] = {"last_at": now - 3000}
        first = self.s.evaluate(self.u, self.c, now=now)
        self.assertTrue(first.allowed)
        self.assertEqual(first.reason, "morning")
        self.s.record_attempt(self.u, self.c)          # 落 morning_sent 标记
        with mock.patch("companion.scheduler.random.random", return_value=0.99):
            second = self.s.evaluate(self.u, self.c, now=now + 1)
        self.assertNotEqual(second.reason, "morning")

    def test_greet_flag_rolls_over_next_day(self):
        now = self._recent_conv(8)
        _proactive_store[self.key] = {"last_at": now - 3000, "morning_sent": True,
                                      "greet_day": "2000-01-01"}
        with mock.patch("companion.scheduler.random.random", return_value=0.99):
            d = self.s.evaluate(self.u, self.c, now=now)
        self.assertNotEqual(d.reason, "roll_window")   # 说明确实重新走到掷骰


class TestPersonaBusy(unittest.TestCase):
    """任务 3：人设作息（学生上课/上班族上班时不主动）。"""

    def test_occupation_detection(self):
        self.assertEqual(R.persona_occupation(FakeChar(persona="我是一名高三学生")), "student")
        self.assertEqual(R.persona_occupation(FakeChar(persona="在互联网公司上班的程序员")), "office")
        self.assertEqual(R.persona_occupation(FakeChar(persona="已经大学毕业，在银行工作")), "office")
        self.assertEqual(R.persona_occupation(FakeChar(persona="自由职业插画师")), "free")

    def test_busy_hours_weekday(self):
        student = FakeChar(persona="高一学生")
        office = FakeChar(persona="公司职员")
        self.assertTrue(R.persona_busy_now(student, now=_ts(*WED, 10)))
        self.assertTrue(R.persona_busy_now(office, now=_ts(*WED, 15)))
        self.assertFalse(R.persona_busy_now(office, now=_ts(*WED, 20)))
        self.assertFalse(R.persona_busy_now(FakeChar(persona="自由职业"), now=_ts(*WED, 15)))

    def test_school_holiday_not_busy(self):
        student = FakeChar(persona="大学生")
        self.assertFalse(R.persona_busy_now(student, now=_ts(2026, 8, 5, 10)))  # 暑假

    def test_evaluate_blocks_when_busy(self):
        _proactive_store.clear()
        _conv_store.clear()
        S.proactive_scheduler._last_failed_roll.clear()
        s = S.proactive_scheduler
        now = _ts(*WED, 15)
        cs = C._conv_state("u3", "c3")
        cs.update({"conv_state": "AVAILABLE_FOR_PROACTIVE", "last_user_at": now - 3600,
                   "last_ai_at": now - 3600, "last_proactive_at": 0, "ended_at": 0,
                   "cooldown_until": 0})
        _proactive_store["u3::c3"] = {"last_at": now - 3000}
        with mock.patch("characters.get_character_manager",
                        return_value=mock.Mock(get_character=lambda _cid: FakeChar(persona="公司职员"))):
            d = s.evaluate("u3", "c3", now=now)
        self.assertFalse(d.allowed)
        self.assertEqual(d.reason, "persona_busy")


class TestSleepWindow(unittest.TestCase):
    """任务 4：说了晚安后落睡眠窗口（修「说了晚安又说没睡」）。"""

    def setUp(self):
        SL._sleep_store.clear()

    def test_detects_self_sleep(self):
        self.assertTrue(SL.says_going_to_sleep("我先睡啦 晚安"))
        self.assertTrue(SL.says_going_to_sleep("困了 我去睡了 明天见"))
        self.assertTrue(SL.says_going_to_sleep("晚安"))
        # 审查后修复：需要第一人称（我…睡/困了），或纯"晚安"且不劝对方睡
        self.assertTrue(SL.says_going_to_sleep("你早点睡，我也睡了"))
        self.assertTrue(SL.says_going_to_sleep("晚安～"))
        self.assertTrue(SL.says_going_to_sleep("我困了 晚安 明天见"))

    def test_does_not_detect_question_or_other(self):
        self.assertFalse(SL.says_going_to_sleep("你睡了吗？"))
        self.assertFalse(SL.says_going_to_sleep("你也早点睡呀"))
        self.assertFalse(SL.says_going_to_sleep("我昨晚睡了三个小时"))
        # 审查后修复：她劝对方睡 + 晚安，不该把"她自己"锁进睡眠窗口
        self.assertFalse(SL.says_going_to_sleep("你也早点睡吧，晚安，明天见"))
        self.assertFalse(SL.says_going_to_sleep("你也早点休息，晚安"))
        self.assertFalse(SL.says_going_to_sleep("你早点睡吧 晚安"))

    def test_mark_and_is_asleep(self):
        now = datetime(2026, 10, 14, 23, 0)
        SL.mark_sleep_from_reply("u4", "c4", "我先睡了 晚安", now=now)
        self.assertTrue(SL.is_asleep("u4", "c4", now=now.timestamp() + 60))
        # 到起床时间之后自动失效
        self.assertFalse(SL.is_asleep("u4", "c4", now=now.timestamp() + 9 * 3600))
        self.assertNotIn("u4::c4", SL._sleep_store)

    def test_explicit_before_regex(self):
        now = datetime(2026, 10, 14, 22, 30)
        SL.mark_sleep_from_reply("u4", "c4", "嗯", explicit=True, now=now)
        self.assertTrue(SL.is_asleep("u4", "c4", now=now.timestamp() + 1))

    def test_nap_only_locks_half_hour(self):
        now = datetime(2026, 10, 14, 13, 0)
        SL.mark_sleep_from_reply("u4", "c4", "我先睡啦", now=now)
        info = SL.sleep_info("u4", "c4", now=now.timestamp())
        self.assertAlmostEqual(info["until"] - info["since"], 30 * 60, delta=5)

    def test_prompt_block_when_asleep(self):
        now = datetime(2026, 10, 14, 23, 0)
        SL.mark_sleep_from_reply("u4", "c4", "我先睡了 晚安", now=now)
        # 用固定时间判定（原来用真实当前时间，过了 10-15 早上 7 点这条就会失败）
        block = SL.sleep_prompt_block("u4", "c4", now=now.timestamp() + 60)
        self.assertIn("睡眠状态", block)
        self.assertIn("绝对不许", block)

    def test_no_prompt_when_awake(self):
        self.assertEqual(SL.sleep_prompt_block("nobody", "nochar"), "")

    def test_clear_sleep(self):
        now = datetime(2026, 10, 14, 23, 0)
        SL.mark_sleep_from_reply("u4", "c4", "我先睡了", now=now)
        SL.clear_sleep("u4", "c4")
        self.assertFalse(SL.is_asleep("u4", "c4", now=now.timestamp() + 1))


class TestNudgeGate(unittest.TestCase):
    """任务 5：追问空话闸门（隔一小时只发「行吧/人呢/？」≠ 真人）。"""

    def test_filler_rejected(self):
        from companion.active_guards import _nudge_has_substance
        for t in ("行吧", "人呢", "在吗？", "？", "喂喂", "好吧…"):
            self.assertFalse(_nudge_has_substance(t), t)

    def test_substance_kept(self):
        from companion.active_guards import _nudge_has_substance
        for t in ("你刚说的那个考试几点来着", "吃药了没 八点那场", "后来呢 面试结果出来没"):
            self.assertTrue(_nudge_has_substance(t), t)


class TestConvStateIdle(unittest.TestCase):
    """任务 5：ACTIVE 8 分钟就松开、闲置才结束；无变更不写盘。"""

    def setUp(self):
        _conv_store.clear()

    def test_active_releases_after_8min(self):
        cs = C._conv_state("u5", "c5")
        cs.update({"conv_state": "ACTIVE", "last_user_at": time.time() - 9 * 60, "last_ai_at": 0})
        C._update_conv_state("u5", "c5", "idle")
        self.assertEqual(C._conv_state("u5", "c5")["conv_state"], "QUIET")

    def test_active_kept_within_8min(self):
        cs = C._conv_state("u5", "c5")
        cs.update({"conv_state": "ACTIVE", "last_user_at": time.time() - 60, "last_ai_at": 0})
        C._update_conv_state("u5", "c5", "idle")
        self.assertEqual(C._conv_state("u5", "c5")["conv_state"], "ACTIVE")

    def test_ended_cooldown_is_15min(self):
        cs = C._conv_state("u5", "c5")
        old = time.time() - 7 * 3600
        cs.update({"conv_state": "QUIET", "last_user_at": old, "last_ai_at": old,
                   "cooldown_until": 0})
        C._update_conv_state("u5", "c5", "idle")
        cu = C._conv_state("u5", "c5")["cooldown_until"]
        self.assertAlmostEqual(cu - time.time(), 15 * 60, delta=30)

    def test_unchanged_idle_does_not_mark_dirty(self):
        cs = C._conv_state("u5", "c5")
        cs.update({"conv_state": "AVAILABLE_FOR_PROACTIVE", "last_user_at": time.time(),
                   "last_ai_at": time.time()})
        C._conv_dirty = False
        C._update_conv_state("u5", "c5", "idle", save=False)
        self.assertFalse(C._conv_dirty)


class TestActiveSendGating(unittest.TestCase):
    """任务 6/7：主动消息真的发出才计数；同步通道下发走线程池，不卡事件循环。"""

    def setUp(self):
        _proactive_store.clear()
        _conv_store.clear()
        import companion.active as A
        self.A = A
        A.user_states.clear()

    def test_send_offloads_blocking_channel_to_executor(self):
        A = self.A
        seen = {}

        def fake_send(uid, txt):
            seen["thread"] = threading.current_thread().name
            seen["args"] = (uid, txt)
            return True

        with mock.patch.object(A.channel_registry, "send", side_effect=fake_send):
            ok = asyncio.run(A._send_active_messages(
                "wx_clawbot_main__u1",
                [{"text": "在吗", "type": "statement", "delay": 0}],
                "闲聊", reason="proactive", char_id="c1"))
        self.assertTrue(ok)
        self.assertEqual(seen["args"], ("wx_clawbot_main__u1", "在吗"))
        # 同步发送被丢到线程池（不是主事件循环所在线程）
        self.assertNotEqual(seen["thread"], threading.current_thread().name)

    def test_send_returns_false_when_channel_rejects(self):
        A = self.A
        with mock.patch.object(A.channel_registry, "send", return_value=False):
            ok = asyncio.run(A._send_active_messages(
                "wx_clawbot_main__u1",
                [{"text": "在吗", "type": "statement", "delay": 0}],
                "闲聊", reason="proactive", char_id="c1"))
        self.assertFalse(ok)

    def test_send_returns_false_when_asleep(self):
        A = self.A
        with mock.patch("companion.sleep.is_asleep", return_value=True):
            ok = asyncio.run(A._send_active_messages(
                "u9", [{"text": "在吗", "type": "statement", "delay": 0}],
                "闲聊", reason="proactive", char_id="c9"))
        self.assertFalse(ok)

    def _fake_llm(self, text="在吗"):
        fake = mock.Mock()
        fake.available = True
        fake.generate_proactive = mock.AsyncMock(
            return_value=[{"text": text, "type": "statement", "delay": 0}])
        return fake

    def test_fire_proactive_counts_only_after_real_send(self):
        A = self.A
        A.user_states["web_user"] = {"character_id": "c1"}
        with mock.patch.object(A, "llm_client", self._fake_llm()), \
                mock.patch.object(A, "_context_gate", return_value=(True, "")), \
                mock.patch.object(A, "_send_active_messages",
                                  mock.AsyncMock(return_value=True)), \
                mock.patch.object(A, "_record_shared_event", lambda *a, **k: None):
            asyncio.run(A._fire_proactive("web_user", char_id="c1"))
        entry = _proactive_store.get("web_user::c1") or {}
        self.assertEqual(entry.get("day_count"), 1)
        self.assertEqual(entry.get("no_reply_streak"), 1)
        self.assertEqual(entry.get("last_text"), "在吗")

    def test_fire_proactive_not_counted_when_send_refused(self):
        A = self.A
        A.user_states["web_user"] = {"character_id": "c1"}
        with mock.patch.object(A, "llm_client", self._fake_llm()), \
                mock.patch.object(A, "_context_gate", return_value=(True, "")), \
                mock.patch.object(A, "_send_active_messages",
                                  mock.AsyncMock(return_value=False)), \
                mock.patch.object(A, "_record_shared_event", lambda *a, **k: None):
            asyncio.run(A._fire_proactive("web_user", char_id="c1"))
        # 被睡眠门/通道拒绝 → 不写 day_count / 冷却
        self.assertNotIn("web_user::c1", _proactive_store)


if __name__ == "__main__":
    unittest.main()
