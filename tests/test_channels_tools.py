# -*- coding: utf-8 -*-
"""通道层 + 工具超时 回归测试（标准库 unittest，不依赖网络 / 不依赖 ffmpeg）。

覆盖本次从服务器版移植的六项修复：
1. registry.can_send/_bound：绑过微信但长轮询没起时也要能下发。
2. wechat_voice._wav_16k_mono：非 16k / 多声道音频不再静默转不出字。
3. wechat_emoji.convert_emoji_tags + clawbot._normalize_emoji_tags：表情标签转 emoji。
4. clawbot 状态落盘/恢复 + 探测重试 + resolve_remote(allow_fuzzy)。
6. runtime/toolagent 12s wait_for + tools/web 总预算。

跑法：cd D:/tmp/qiyu-desktop-license && python -X utf8 -m unittest tests.test_channels_tools -v
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
import wave
from unittest import mock

# 让 `import channels.* / runtime.* / tools.*` 能直接找到工程根
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from channels.registry import ChannelRegistry  # noqa: E402


# --------------------------------------------------------------------------
# 通用桩件
# --------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, bot_token=""):
        self.bot_token = bot_token


class FakeChannel:
    """registry 用得到的最小通道桩：id / running / config / _client / resolve_remote / send。"""

    def __init__(self, cid, running=False, bot_token="", merge_user_id="",
                 remote_for=None, fuzzy_remote=""):
        self.id = cid
        self.name = cid
        self.running = running
        self.integrated = False
        self.config = {"merge_user_id": merge_user_id}
        self._client = _FakeClient(bot_token)
        self._remote_for = remote_for or {}
        self._fuzzy_remote = fuzzy_remote
        self.sent = []

    def resolve_remote(self, user_id, allow_fuzzy=False):
        if user_id in self._remote_for:
            return self._remote_for[user_id]
        # 只有允许模糊回退时才「猜」唯一联系人（模拟 clawbot 的行为）
        if allow_fuzzy:
            return self._fuzzy_remote
        return ""

    def send(self, user_id, text):
        self.sent.append((user_id, text))
        return True

    def status(self):
        return {"id": self.id}


# --------------------------------------------------------------------------
# 1. registry：绑定态即可下发
# --------------------------------------------------------------------------
class TestRegistryCanSend(unittest.TestCase):
    def test_can_send_running_or_bound(self):
        running = FakeChannel("a", running=True)
        bound = FakeChannel("b", running=False, bot_token="tok")
        neither = FakeChannel("c", running=False)
        self.assertTrue(ChannelRegistry.can_send(running))
        self.assertTrue(ChannelRegistry.can_send(bound))
        self.assertFalse(ChannelRegistry.can_send(neither))

    def test_send_when_bound_but_not_running(self):
        """核心回归：绑过微信（有 token）、长轮询没起（running=False）时，主动消息也要发出去。"""
        reg = ChannelRegistry()
        ch = FakeChannel("clawbot", running=False, bot_token="tok",
                         merge_user_id="web_user",
                         remote_for={"web_user": "wxid_me"})
        reg.register(ch)
        self.assertTrue(reg.send("web_user", "找话"))
        self.assertEqual(ch.sent, [("web_user", "找话")])

    def test_send_false_when_unbound_and_not_running(self):
        reg = ChannelRegistry()
        ch = FakeChannel("clawbot", running=False, bot_token="",
                         merge_user_id="web_user",
                         remote_for={"web_user": "wxid_me"})
        reg.register(ch)
        self.assertFalse(reg.send("web_user", "找话"))
        self.assertEqual(ch.sent, [])

    def test_scan_fuzzy_only_for_owned_channel(self):
        """扫描路由时，只有 merge_user_id 指向该对话的通道才允许模糊回退（防串号）。"""
        reg = ChannelRegistry()
        mine = FakeChannel("clawbot_main", running=True, merge_user_id="web_user",
                           fuzzy_remote="wxid_mine")
        other = FakeChannel("clawbot_other", running=True, merge_user_id="someone_else",
                            fuzzy_remote="wxid_other")
        reg.register(mine)
        reg.register(other)
        self.assertIs(reg.get_channel_for_user("web_user"), mine)


# --------------------------------------------------------------------------
# 2. wechat_voice：非 16k 音频重采样
# --------------------------------------------------------------------------
def _write_wav(path, channels, rate, nframes):
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * (nframes * channels))


class TestVoiceResample(unittest.TestCase):
    def setUp(self):
        from channels.wechat_voice import _wav_16k_mono  # noqa
        self.tmp = tempfile.mkdtemp(prefix="qiyu_test_")

    def tearDown(self):
        for name in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def test_24k_mono_resampled_to_16k(self):
        from channels.wechat_voice import _wav_16k_mono
        p = os.path.join(self.tmp, "a.wav")
        _write_wav(p, 1, 24000, 2400)
        pcm = _wav_16k_mono(p)
        self.assertIsNotNone(pcm)
        # 2400 * 16000/24000 = 1600 帧，每帧 2 字节
        self.assertEqual(len(pcm), 1600 * 2)

    def test_stereo_downmixed_to_mono(self):
        from channels.wechat_voice import _wav_16k_mono
        p = os.path.join(self.tmp, "b.wav")
        _write_wav(p, 2, 16000, 1000)
        pcm = _wav_16k_mono(p)
        self.assertIsNotNone(pcm)
        self.assertEqual(len(pcm), 1000 * 2)

    def test_non_wav_returns_none(self):
        from channels.wechat_voice import _wav_16k_mono
        p = os.path.join(self.tmp, "c.silk")
        with open(p, "wb") as f:
            f.write(b"\x02#!SILK_V3garbage")
        self.assertIsNone(_wav_16k_mono(p))

    def test_ensure_wav_passthrough(self):
        from channels.wechat_voice import _ensure_wav
        p = os.path.join(self.tmp, "d.wav")
        _write_wav(p, 1, 16000, 10)
        self.assertEqual(_ensure_wav(p), p)


# --------------------------------------------------------------------------
# 3. 表情标签转换
# --------------------------------------------------------------------------
class TestEmojiTags(unittest.TestCase):
    def test_convert_known_and_alias(self):
        from channels.wechat_emoji import convert_emoji_tags, WECHAT_EMOJI_CHARS
        self.assertEqual(convert_emoji_tags("我好[委屈]"), "我好🥺")
        self.assertEqual(convert_emoji_tags("[抱抱]"), "🫂")           # 别名
        self.assertEqual(convert_emoji_tags("没有表情"), "没有表情")
        self.assertIn("😭", WECHAT_EMOJI_CHARS["大哭"])

    def test_convert_unknown_keeps_text_drops_brackets(self):
        """认不出的 [名称] 必须去掉方括号保留文字，绝不把句子咬掉一块。"""
        from channels.wechat_emoji import convert_emoji_tags
        self.assertEqual(convert_emoji_tags("今天[奇怪心情]"), "今天奇怪心情")
        self.assertNotIn("[", convert_emoji_tags("[不存在的表情名]"))

    def test_clawbot_normalize_emoji_tags(self):
        from tests.test_channels_tools import make_clawbot
        _STATE.clear()
        _CFG.clear()
        with _patch_store():
            ch = make_clawbot()
            self.assertEqual(ch._normalize_emoji_tags("[大哭]"), "😭")
            self.assertEqual(ch._normalize_emoji_tags("想你了[亲亲]"), "想你了😘")
            # 整条只有认不出的标签 → 退化成名称文字，而不是空串
            self.assertEqual(ch._normalize_emoji_tags("[奇怪标签]"), "奇怪标签")
            self.assertEqual(ch._normalize_emoji_tags("普通文本"), "普通文本")


# --------------------------------------------------------------------------
# 4. clawbot：状态落盘/恢复、探测重试、resolve_remote
# --------------------------------------------------------------------------
_STATE = {}
_CFG = {}


def _patch_store():
    return mock.patch.multiple(
        "channels.store",
        load_channel_state=mock.Mock(side_effect=lambda cid: dict(_STATE.get(cid, {}))),
        save_channel_state=mock.Mock(side_effect=lambda cid, data: _STATE.__setitem__(cid, dict(data))),
        load_config=mock.Mock(side_effect=lambda cid: dict(_CFG.get(cid, {}))),
        save_config=mock.Mock(side_effect=lambda cid, cfg: _CFG.__setitem__(cid, dict(cfg))),
    )


def make_clawbot(account_id="main"):
    from channels.clawbot import ClawBotChannel
    return ClawBotChannel(account_id)


class TestClawBotState(unittest.TestCase):
    def setUp(self):
        _STATE.clear()
        _CFG.clear()
        self._p = _patch_store()
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_save_and_restore_merged_remote_and_sessions(self):
        ch = make_clawbot()
        ch._client.bot_token = "T"
        ch._sessions["u1"] = "ctx1"
        ch._merged_remote["web_user"] = "u1"
        ch._save_state()
        # 新实例（模拟进程重启）应从磁盘恢复
        ch2 = make_clawbot()
        self.assertEqual(ch2._sessions.get("u1"), "ctx1")
        self.assertEqual(ch2._merged_remote.get("web_user"), "u1")

    def test_resolve_remote_exact_then_fuzzy(self):
        ch = make_clawbot()
        ch.config["merge_user_id"] = "web_user"
        ch.config["single_conversation"] = True
        ch._sessions["u1"] = "ctx1"
        # 默认不许猜：合并表为空 → 精确解析返回空
        self.assertEqual(ch.resolve_remote("web_user"), "")
        # 允许模糊回退时才猜唯一联系人，并顺手记住（落盘）
        self.assertEqual(ch.resolve_remote("web_user", allow_fuzzy=True), "u1")
        self.assertEqual(ch._merged_remote.get("web_user"), "u1")
        self.assertEqual(_STATE.get(ch.id, {}).get("merged_remote", {}).get("web_user"), "u1")

    def test_send_without_context_token_still_attempts(self):
        """重启后还没有 context_token 时，不再直接放弃，按无会话态尝试发送（含表情转换）。"""
        ch = make_clawbot()
        ch._client.bot_token = "T"
        ch.config["merge_user_id"] = "web_user"
        ch._merged_remote["web_user"] = "u1"
        calls = []
        ch._client.send_message = lambda remote, text, ctx="": calls.append((remote, text, ctx)) or {"ret": 0}
        ch._client.get_config = mock.Mock(return_value={"typing_ticket": ""})
        self.assertTrue(ch.send("web_user", "在吗[大哭]"))
        self.assertEqual(calls, [("u1", "在吗😭", "")])

    def test_start_probe_retries_then_succeeds(self):
        ch = make_clawbot()
        ch._client.bot_token = "T"
        calls = {"n": 0}

        def fake_get_updates(cursor, timeout=35, probe=False):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("boom")
            return {"ret": 0, "errcode": 0, "msgs": [], "get_updates_buf": "cur2"}

        with mock.patch.object(ch, "_start_updates_loop", lambda: None), \
             mock.patch.object(ch._client, "get_updates", side_effect=fake_get_updates), \
             mock.patch("channels.clawbot.time.sleep", lambda *_a, **_k: None):
            self.assertTrue(ch.start())
        self.assertEqual(calls["n"], 3)
        self.assertTrue(ch.running)
        self.assertEqual(ch.qr_status, "running")
        self.assertEqual(ch._cursor, "cur2")

    def test_start_probe_network_failure_enters_listen_without_qr(self):
        """网络抖动（一直超时）不应把用户踢回二维码流程。"""
        ch = make_clawbot()
        ch._client.bot_token = "T"

        def always_timeout(cursor, timeout=35, probe=False):
            raise TimeoutError("network jitter")

        def must_not_qr(*_a, **_k):
            raise AssertionError("不应回落到二维码流程")

        with mock.patch.object(ch, "_start_updates_loop", lambda: None), \
             mock.patch.object(ch._client, "get_updates", side_effect=always_timeout), \
             mock.patch.object(ch._client, "get_bot_qrcode", side_effect=must_not_qr), \
             mock.patch("channels.clawbot.time.sleep", lambda *_a, **_k: None):
            self.assertTrue(ch.start())
        self.assertTrue(ch.running)
        self.assertEqual(ch.qr_status, "running")


# --------------------------------------------------------------------------
# 6. 工具调用超时
# --------------------------------------------------------------------------
class TestToolTimeouts(unittest.TestCase):
    def test_toolagent_search_timeout(self):
        from runtime.toolagent import ToolAgent
        agent = ToolAgent()

        async def hang(_q):
            await asyncio.sleep(30)

        real_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, timeout=None):
            # 把 12s 缩到 0.05s，只验证"超时会兜底成失败证据"这条逻辑
            return await real_wait_for(coro, timeout=0.05)

        with mock.patch.object(agent, "_search_locked", side_effect=hang), \
             mock.patch("runtime.toolagent.asyncio.wait_for", fast_wait_for):
            ev = asyncio.run(agent.search("测试查询"))
        self.assertFalse(ev.success)
        self.assertEqual(ev.backend, "timeout")
        self.assertEqual(ev.items, [])

    def test_web_search_respects_budget(self):
        import tools.web as web

        async def empty(_q):
            return []

        async def hang(q, top_k=5):
            await asyncio.sleep(30)
            return []

        patches = [
            mock.patch.object(web, "_SEARCH_BUDGET", 1.0),
            mock.patch.object(web, "_weather_now", empty),
        ]
        for name in ("_bing_search", "_sogou_search", "_sogou_wx_search",
                     "_baidu_mobile_search", "_baidu_suggest", "_baidu_search"):
            patches.append(mock.patch.object(web, name, hang))
        for p in patches:
            p.start()
        try:
            t0 = time.time()
            out = asyncio.run(web.web_search("任意查询"))
            elapsed = time.time() - t0
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(out, [])
        self.assertLess(elapsed, 5.0, f"搜索未在预算内返回，耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
