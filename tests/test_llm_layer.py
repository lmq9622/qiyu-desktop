# -*- coding: utf-8 -*-
"""LLM 层移植修复的单元测试（2026-10-14 移植批次）

覆盖服务器版 → 桌面版移植的几项修复（全部用 mock，不依赖网络/真实模型）：
- 流式调用「数据间隔超时」（QIYU_MAIN_LLM_TIMEOUT，默认 60s；不再整体 420s）
- _check_llm 探活带 Authorization（有 api_key 时）
- resolve_chat_temperature：请求温度 > 角色卡温度 > 设置页 default_temperature > 0.7
- 自适应思考：短句关思考（既有实现回归）+ 云端 API 剥离 chat_template_kwargs 等本地字段
- settings.json 解析失败不再静默（写日志）+ 保存原子写
- 回复解析：真换行拆多气泡 + 坏输出守卫 + 尾巴垃圾/到场幻觉/去重/超长拆分

运行：python -X utf8 -m unittest tests.test_llm_layer -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from companion import llm as llm_mod  # noqa: E402
from companion import models as models_mod  # noqa: E402
from companion import settings as settings_mod  # noqa: E402


def _make_client():
    """造一个不联网的 LLMClient（跳过构造期探活）。"""
    with mock.patch.object(llm_mod.LLMClient, "_check_llm", lambda self: None):
        return llm_mod.LLMClient()


# ---------------------------------------------------------------------------
# 1. 流式「数据间隔超时」
# ---------------------------------------------------------------------------

class _FakeStreamResp:
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *a):
        return False


class _CapturingStreamClient:
    """替身 httpx.AsyncClient：记录 timeout / 构造参数，stream() 立刻抛连接错误。"""
    captured = {}

    def __init__(self, timeout=None, **kw):
        _CapturingStreamClient.captured["timeout"] = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, *a, **k):
        return _FakeStreamResp(httpx.ConnectError("模拟连不上"))


class TestStreamGapTimeout(unittest.TestCase):
    def test_gap_timeout_helper_defaults(self):
        os.environ.pop("QIYU_MAIN_LLM_TIMEOUT", None)
        os.environ.pop("QIYU_LLM_TIMEOUT", None)
        # 不给 url（= 主模型）默认 60s
        self.assertEqual(llm_mod._stream_gap_timeout(), 60.0)
        # 明确的非主端点默认 420s
        self.assertEqual(llm_mod._stream_gap_timeout("http://other.example.com/v1"), 420.0)

    def test_gap_timeout_env_override(self):
        with mock.patch.dict(os.environ, {"QIYU_MAIN_LLM_TIMEOUT": "25"}):
            self.assertEqual(llm_mod._stream_gap_timeout(), 25.0)
        with mock.patch.dict(os.environ, {"QIYU_LLM_TIMEOUT": "900"}):
            self.assertEqual(llm_mod._stream_gap_timeout("http://other.example.com"), 900.0)

    def test_stream_client_uses_gap_timeout_not_420(self):
        os.environ.pop("QIYU_MAIN_LLM_TIMEOUT", None)
        client = _make_client()
        _CapturingStreamClient.captured.clear()

        async def run():
            agen = client._call_real_llm_stream_inner(
                "sys", [{"role": "user", "content": "在吗"}], 0.7)
            with self.assertRaises(httpx.ConnectError):
                async for _ in agen:
                    pass

        with mock.patch("companion.llm.httpx.AsyncClient", _CapturingStreamClient):
            asyncio.run(run())
        t = _CapturingStreamClient.captured["timeout"]
        self.assertIsInstance(t, httpx.Timeout)
        # 数据间隔超时 = 60s（不是旧的整体 420s）
        self.assertEqual(t.read, 60.0)
        self.assertEqual(t.connect, 10.0)


# ---------------------------------------------------------------------------
# 2. _check_llm 探活带 Authorization
# ---------------------------------------------------------------------------

class _FakeGetResp:
    def __init__(self, status_code):
        self.status_code = status_code


class TestCheckLlmAuth(unittest.TestCase):
    def test_auth_header_sent_when_api_key_configured(self):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeGetResp(200)

        with mock.patch.object(llm_mod, "load_runtime_settings",
                               lambda: {"api_key": "sk-secret"}), \
                mock.patch("companion.llm.httpx.get", fake_get):
            client = llm_mod.LLMClient.__new__(llm_mod.LLMClient)
            client.available = False
            llm_mod.LLMClient._check_llm(client)
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer sk-secret")
        self.assertTrue(client.available)

    def test_no_auth_header_without_key(self):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["headers"] = headers
            return _FakeGetResp(200)

        with mock.patch.object(llm_mod, "load_runtime_settings", lambda: {"api_key": ""}), \
                mock.patch("companion.llm.httpx.get", fake_get):
            client = llm_mod.LLMClient.__new__(llm_mod.LLMClient)
            client.available = False
            llm_mod.LLMClient._check_llm(client)
        self.assertNotIn("Authorization", captured["headers"] or {})

    def test_401_is_not_marked_unavailable_hard(self):
        with mock.patch.object(llm_mod, "load_runtime_settings", lambda: {"api_key": "k"}), \
                mock.patch("companion.llm.httpx.get", lambda *a, **k: _FakeGetResp(401)):
            client = llm_mod.LLMClient.__new__(llm_mod.LLMClient)
            client.available = False
            llm_mod.LLMClient._check_llm(client)   # 不抛异常
        self.assertFalse(client.available)


# ---------------------------------------------------------------------------
# 3. 采样温度优先级
# ---------------------------------------------------------------------------

class _FakeChar:
    def __init__(self, temperature):
        self.temperature = temperature


class TestResolveTemperature(unittest.TestCase):
    def test_explicit_wins(self):
        with mock.patch.object(llm_mod.char_mgr, "get_character", lambda cid: _FakeChar(0.9)):
            self.assertEqual(llm_mod.resolve_chat_temperature("c", 0.3), 0.3)

    def test_char_card_when_no_explicit(self):
        with mock.patch.object(llm_mod.char_mgr, "get_character", lambda cid: _FakeChar(0.4)):
            self.assertEqual(llm_mod.resolve_chat_temperature("c", 0.0), 0.4)

    def test_settings_default_when_char_zero(self):
        with mock.patch.object(llm_mod.char_mgr, "get_character", lambda cid: _FakeChar(0)), \
                mock.patch.object(llm_mod, "load_runtime_settings",
                                  lambda: {"default_temperature": 0.55}):
            self.assertEqual(llm_mod.resolve_chat_temperature("c", 0.0), 0.55)

    def test_fallback_07(self):
        with mock.patch.object(llm_mod.char_mgr, "get_character", lambda cid: _FakeChar(0)), \
                mock.patch.object(llm_mod, "load_runtime_settings", lambda: {}):
            self.assertEqual(llm_mod.resolve_chat_temperature("c", 0.0), 0.7)

    def test_chat_uses_resolver(self):
        """chat()/chat_stream() 默认温度不再是写死的 0.7。"""
        client = _make_client()
        client.available = True
        m = mock.MagicMock(return_value=0.42)
        with mock.patch.object(llm_mod, "resolve_chat_temperature", m), \
                mock.patch.object(client, "_build_chat_system_msg",
                                  mock.AsyncMock(return_value="sys")), \
                mock.patch.object(client, "_call_real_llm",
                                  mock.AsyncMock(return_value='{"messages":[{"text":"hi"}]}')):
            asyncio.run(client.chat("c", [{"role": "user", "content": "hi"}]))
        self.assertEqual(m.call_args[0][1], 0.0)   # 默认值已改为 0.0，交给解析器


# ---------------------------------------------------------------------------
# 4. 自适应思考 + 云端剥离本地扩展字段
# ---------------------------------------------------------------------------

class TestLocalOnlyKwargs(unittest.TestCase):
    def test_self_hosted_detection(self):
        self.assertTrue(llm_mod._is_self_hosted("http://192.168.2.6:8080/v1"))
        self.assertTrue(llm_mod._is_self_hosted("http://localhost:11434/v1"))
        self.assertTrue(llm_mod._is_self_hosted("http://host.internal/v1"))
        self.assertFalse(llm_mod._is_self_hosted("https://api.deepseek.com/v1"))
        self.assertFalse(llm_mod._is_self_hosted("https://api.openai.com"))

    def test_strip_for_cloud(self):
        p = {"model": "x", "chat_template_kwargs": {"enable_thinking": True},
             "thinking_budget": 2048, "cache_reuse": 1}
        llm_mod._strip_local_only_kwargs(p, "https://api.deepseek.com")
        self.assertNotIn("chat_template_kwargs", p)
        self.assertNotIn("thinking_budget", p)
        self.assertNotIn("cache_reuse", p)

    def test_keep_for_self_hosted(self):
        p = {"model": "x", "chat_template_kwargs": {"enable_thinking": True}}
        llm_mod._strip_local_only_kwargs(p, "http://127.0.0.1:8080")
        self.assertIn("chat_template_kwargs", p)

    def test_forced_low_thinking(self):
        """前端不再暴露档位：任何旧档位都归一到 low；default 允许 2048。"""
        with mock.patch.object(settings_mod, "load_runtime_settings",
                               lambda: {"thinking_level": "mid"}):
            p1 = {}
            settings_mod._apply_thinking_kwargs(p1, "在吗")
            self.assertTrue(p1["chat_template_kwargs"]["enable_thinking"])
            self.assertEqual(p1["chat_template_kwargs"]["thinking_budget"], 1024)
            p2 = {}
            settings_mod._apply_thinking_kwargs(p2, "帮我解释一下这个代码为什么会报错")
            self.assertTrue(p2["chat_template_kwargs"]["enable_thinking"])
            self.assertEqual(p2["chat_template_kwargs"]["thinking_budget"], 1024)
        with mock.patch.object(settings_mod, "load_runtime_settings",
                               lambda: {"thinking_level": "default"}):
            p3 = {}
            settings_mod._apply_thinking_kwargs(p3, "在吗")
            self.assertEqual(p3["chat_template_kwargs"]["thinking_budget"], 2048)


# ---------------------------------------------------------------------------
# 5. settings.json 解析失败不再静默 + 原子写
# ---------------------------------------------------------------------------

class TestSettingsRobustness(unittest.TestCase):
    def test_bad_json_logs_and_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            p.write_text("{ this is not json ", encoding="utf-8")
            with mock.patch.object(settings_mod, "SETTINGS_JSON", p), \
                    mock.patch.object(settings_mod.logger, "error") as err:
                out = settings_mod.load_runtime_settings()
            self.assertTrue(err.called)                     # 不再静默
            self.assertEqual(out, settings_mod.DEFAULT_RUNTIME_SETTINGS)

    def test_valid_json_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            p.write_text(json.dumps({"llm_model": "my-model"}), encoding="utf-8")
            with mock.patch.object(settings_mod, "SETTINGS_JSON", p):
                out = settings_mod.load_runtime_settings()
            self.assertEqual(out["llm_model"], "my-model")
            self.assertEqual(out["default_temperature"], 0.7)

    def test_save_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.json"
            with mock.patch.object(settings_mod, "SETTINGS_JSON", p):
                settings_mod.save_runtime_settings({"llm_model": "abc"})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["llm_model"], "abc")
            # 没有残留的临时文件
            leftover = [x.name for x in Path(d).iterdir() if ".tmp" in x.name]
            self.assertEqual(leftover, [])


# ---------------------------------------------------------------------------
# 6. 回复解析与后处理
# ---------------------------------------------------------------------------

class TestBrokenOutputGuard(unittest.TestCase):
    def test_broken(self):
        for bad in ("", "{", "{}", "[", '{"messages": [', '{"messages":[{"text":""}]}',
                    '{"conversation_state": "闲聊", "messages": []}'):
            self.assertTrue(models_mod.looks_like_broken_model_output(bad), bad)

    def test_ok(self):
        self.assertFalse(models_mod.looks_like_broken_model_output('{"messages":[{"text":"hi"}]}'))
        self.assertFalse(models_mod.looks_like_broken_model_output("你好啊"))

    def test_parse_does_not_emit_broken_output(self):
        self.assertEqual(models_mod.parse_chat_messages("{")["messages"], [])
        self.assertEqual(models_mod.parse_chat_messages("{}")["messages"], [])


class TestNewlineSplit(unittest.TestCase):
    def test_json_text_split_into_bubbles(self):
        raw = json.dumps({"conversation_state": "闲聊", "messages": [
            {"text": "第一句\n第二句", "type": "statement", "delay": 0}]}, ensure_ascii=False)
        msgs = models_mod.parse_chat_messages(raw)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["第一句", "第二句"])

    def test_image_stays_on_first_chunk(self):
        raw = json.dumps({"messages": [
            {"text": "看图\n好看吗", "type": "statement", "image_url": "http://x/1.png"}]},
            ensure_ascii=False)
        msgs = models_mod.parse_chat_messages(raw)["messages"]
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].get("image_url"), "http://x/1.png")
        self.assertNotIn("image_url", msgs[1])

    def test_fallback_non_json_split(self):
        msgs = models_mod.parse_chat_messages("你好\n在吗")["messages"]
        self.assertEqual([m["text"] for m in msgs], ["你好", "在吗"])

    def test_time_tag_becomes_separator(self):
        raw = json.dumps({"messages": [
            {"text": "哪里冷淡了 [刚刚] 明明是你", "type": "statement"}]}, ensure_ascii=False)
        msgs = models_mod.parse_chat_messages(raw)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["哪里冷淡了", "明明是你"])

    def test_strip_time_tag(self):
        self.assertEqual(models_mod.strip_time_tag("[今天] 早啊"), "早啊")
        self.assertEqual(models_mod.strip_time_tag("我刚刚差点洒了水"), "我刚刚差点洒了水")

    def test_stream_parse_matches_final_split(self):
        """流式增量解析与最终解析拆条一致（否则 demo 的 safe_pushed 会重复推送）。"""
        raw = json.dumps({"conversation_state": "闲聊", "messages": [
            {"text": "第一句\n第二句", "type": "statement", "delay": 0}]}, ensure_ascii=False)
        parsed = models_mod._try_stream_parse(raw)
        self.assertIsNotNone(parsed)
        st, msgs = parsed
        self.assertEqual([m["text"] for m in msgs], ["第一句", "第二句"])

    # --- 审查后修复：清单/代码块/缩进不再被误拆（原先无条件按换行拆） ---

    def test_list_not_split(self):
        """「给你写个清单：\\n1.牛奶\\n2.鸡蛋」整条保留，不再拆成三条。"""
        raw = json.dumps({"messages": [
            {"text": "给你写个清单：\n1.牛奶\n2.鸡蛋", "type": "statement"}]}, ensure_ascii=False)
        msgs = models_mod.parse_chat_messages(raw)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["给你写个清单：\n1.牛奶\n2.鸡蛋"])

    def test_bullet_and_indent_merge(self):
        raw = json.dumps({"messages": [
            {"text": "看这个\n- 第一点\n- 第二点", "type": "statement"}]}, ensure_ascii=False)
        msgs = models_mod.parse_chat_messages(raw)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["看这个\n- 第一点\n- 第二点"])

    def test_code_block_kept_intact(self):
        code = "```python\nprint(1)\nprint(2)\n```"
        raw = json.dumps({"messages": [{"text": code, "type": "statement"}]}, ensure_ascii=False)
        msgs = models_mod.parse_chat_messages(raw)["messages"]
        self.assertEqual([m["text"] for m in msgs], [code])

    def test_plain_newline_still_splits(self):
        raw = json.dumps({"messages": [
            {"text": "第一句\n第二句", "type": "statement"}]}, ensure_ascii=False)
        msgs = models_mod.parse_chat_messages(raw)["messages"]
        self.assertEqual([m["text"] for m in msgs], ["第一句", "第二句"])

    def test_split_bubbles_helper(self):
        self.assertEqual(models_mod._split_bubbles("甲\n乙"), ["甲", "乙"])
        self.assertEqual(models_mod._split_bubbles("清单：\n1、牛奶\n2、鸡蛋"),
                         ["清单：\n1、牛奶\n2、鸡蛋"])
        self.assertEqual(models_mod._split_bubbles(""), [])
        # 缩进行并入上一条
        self.assertEqual(models_mod._split_bubbles("前一句\n     缩进"),
                         ["前一句\n缩进"])

    def test_stream_parse_list_not_split(self):
        raw = json.dumps({"conversation_state": "闲聊", "messages": [
            {"text": "步骤：\n1. 先烧水\n2. 再下面", "type": "statement"}]}, ensure_ascii=False)
        st, msgs = models_mod._try_stream_parse(raw)
        self.assertEqual([m["text"] for m in msgs], ["步骤：\n1. 先烧水\n2. 再下面"])


class TestReplyPostprocessExtra(unittest.TestCase):
    def test_strip_tail_junk(self):
        self.assertEqual(models_mod._strip_tail_junk("在啊，，，，，"), "在啊")
        self.assertEqual(models_mod._strip_tail_junk("哦……"), "哦")

    def test_presence_hallucination_positive(self):
        """只拦"角色声称已在用户身边/已线下见面"（审查后收窄，正例）。"""
        positives = [
            "我到你家楼下了",
            "我就在你门口",
            "开门 我到了",
            "我现在过来找你",
            "我抱住你了",
            "我就站在你楼下等你呢",
            "你往我这边看",
            "我马上过去找你",
            "我到你身边了",
            "快点出来",
            "我坐你旁边了",
            "我牵着你手呢",
        ]
        for t in positives:
            self.assertTrue(models_mod._looks_like_presence_hallucination(t), t)

    def test_presence_hallucination_negative(self):
        """日常寒暄/关心/线上动作绝不能被误杀（审查后收窄，反例 ≥12）。"""
        negatives = [
            "我在这呢，怎么啦",
            "你出来吃饭了吗",
            "早点睡别动气",
            "收到 我这就去写",
            "我等你呢",
            "今天楼下新开了家店",
            "我在家呢，刚吃完饭",
            "我马上回你消息",
            "我看到你的消息了",
            "门开着呢",
            "我坐在书桌前写作业",
            "你先睡吧 我还要写作业",
            "我这就发你链接",
            "你楼下那家奶茶店不错",
        ]
        for t in negatives:
            self.assertFalse(models_mod._looks_like_presence_hallucination(t), t)

    def test_dedup_consecutive(self):
        msgs = [{"text": "哈哈"}, {"text": "哈哈"}, {"text": "然后呢"}]
        out = models_mod._dedup_consecutive_bubbles(msgs)
        self.assertEqual([m["text"] for m in out], ["哈哈", "然后呢"])

    def test_split_long_bubbles_by_space(self):
        long_text = " ".join(["这是一个词"] * 12)   # > 26 字且带空格
        out = models_mod._split_long_bubbles([{"text": long_text, "type": "statement"}])
        self.assertGreater(len(out), 1)
        self.assertLessEqual(len(out), 4)
        # 第一条 delay 0，之后 700
        self.assertEqual(out[0]["delay"], 0)
        for m in out[1:]:
            self.assertEqual(m["delay"], 700)

    def test_split_long_bubbles_no_space_kept(self):
        t = "这是一段很长很长很长很长很长很长没有空格的文字内容不该被硬拆"
        out = models_mod._split_long_bubbles([{"text": t, "type": "statement"}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], t)

    def test_postprocess_combined(self):
        parsed = {"messages": [
            {"text": "我就站在你楼下等你呢", "type": "statement", "delay": 0},   # 到场幻觉 → 丢
            {"text": "在啊，，，，，", "type": "statement", "delay": 0},          # 去尾巴垃圾
            {"text": "在啊，，，，，", "type": "statement", "delay": 0},          # 连续重复 → 去重
        ]}
        out = models_mod.postprocess_reply_messages_extra(parsed)
        self.assertEqual([m["text"] for m in out["messages"]], ["在啊"])

    def test_postprocess_empties_when_all_blocked(self):
        """全部被拦（到场幻觉）时返回空 messages，交给上层空回复兜底。"""
        parsed = {"messages": [{"text": "我就站在你门口", "type": "statement", "delay": 0}]}
        out = models_mod.postprocess_reply_messages_extra(parsed)
        self.assertEqual(out["messages"], [])


if __name__ == "__main__":
    unittest.main()
