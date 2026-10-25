# -*- coding: utf-8 -*-
"""角色认知双模式护栏（2026-10）

覆盖：
- roleplay（默认）下 apply_cognition 严格无操作：返回值 == 原 prompt（逐字符相等）
- resident 下注入「住在设备里」的自我认知，但**绝不出现**助手腔模板
  （作为 AI 助手 / 我能为您 / 作为AI我无法 …）
- 注入不覆盖原人格内容，且落在回复格式要求之前
- 老角色文件（没有 cognition_mode 字段）→ roleplay，不报错
- 非法值（"xxx" 之类）→ 回退 roleplay
- 接线：system prompt 拼装处确实调用了 apply_cognition，且用 try/except 兜底

运行：python -X utf8 -m unittest tests.test_cognition -v
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from characters import Character  # noqa: E402
from characters.cognition import (  # noqa: E402
    DEFAULT_MODE,
    MODE_RESIDENT,
    MODE_ROLEPLAY,
    VALID_MODES,
    apply_cognition,
    cognition_blocks,
    get_cognition_mode,
    normalize_mode,
    set_cognition_mode,
)

LLM_SRC = (ROOT / "companion" / "llm.py").read_text(encoding="utf-8")
DEMO_SRC = (ROOT / "demo.py").read_text(encoding="utf-8")

# 驻留模式一律不许出现的助手腔/客服腔模板（任务书点名的那几条 + 同类变体）
FORBIDDEN_STRINGS = (
    "作为 AI 助手",
    "作为AI助手",
    "作为 AI 助理",
    "我能为您",
    "作为AI我无法",
    "作为AI",
    "AI助手",
    "为您",
)

BASE_PERSONA = (
    "【身份与背景（事实层，可被修改/淘汰）】\n"
    "你叫小栖，25 岁，住在杭州，做设计，养了只叫团子的猫。\n\n"
    "【说话风格】\n说话风格: 自然, 偶尔使用emoji\n"
)

FULL_PROMPT = BASE_PERSONA + (
    "\n【当前时间】2026-10-20 23:41 星期二（这是你现在所处的真实时间）"
    "\n\n【最后提醒（必须遵守）】只输出 JSON（包含 conversation_state 和 messages），不要输出任何其他内容。"
)


class FakeChar:
    """最朴素的假角色：只有 persona_params（模拟老数据/半结构化数据）"""

    def __init__(self, persona_params=None, **kw):
        self.persona_params = persona_params
        for k, v in kw.items():
            setattr(self, k, v)


def legacy_character_json(**overrides) -> dict:
    """模拟磁盘上的老角色 .json（**没有** cognition_mode 字段）"""
    data = {
        "id": "xiaoban",
        "name": "小办",
        "tagline": "会贫嘴的那种",
        "description": "住在这个世界的普通女生",
        "version": "1.0",
        "temperature": 0.7,
        "persona": "你叫小办，27 岁，在南京做运营。",
        "human": "用户是朋友。",
        "memory_prompt": "",
        "speech_style": {"tone": "自然"},
        "avatar_color": "#6366f1",
        "persona_params": {"mbti": "ENFP", "rebut": 60},
    }
    data.update(overrides)
    return data


class TestRoleplayIsNoop(unittest.TestCase):
    """roleplay = 线上现状，一个字符都不能变"""

    def test_missing_field_returns_identical_string(self):
        char = FakeChar(persona_params={"mbti": "ENFP"})
        out = apply_cognition(FULL_PROMPT, char)
        self.assertEqual(out, FULL_PROMPT)
        self.assertIs(out, FULL_PROMPT)

    def test_explicit_roleplay_returns_identical_string(self):
        char = FakeChar(persona_params={"cognition_mode": "roleplay"})
        self.assertEqual(apply_cognition(FULL_PROMPT, char), FULL_PROMPT)

    def test_none_persona_params_and_empty_prompt(self):
        self.assertEqual(apply_cognition(FULL_PROMPT, FakeChar(None)), FULL_PROMPT)
        self.assertEqual(apply_cognition("", FakeChar(None)), "")
        char = FakeChar(None)
        self.assertEqual(get_cognition_mode(char), MODE_ROLEPLAY)
        self.assertEqual(cognition_blocks(char)["identity"], "")
        self.assertEqual(cognition_blocks(char)["rules"], [])

    def test_non_dict_persona_params_does_not_raise(self):
        char = FakeChar(persona_params="oops-not-a-dict")
        self.assertEqual(get_cognition_mode(char), MODE_ROLEPLAY)
        self.assertEqual(apply_cognition(FULL_PROMPT, char), FULL_PROMPT)

    def test_non_string_prompt_passthrough(self):
        char = FakeChar(persona_params={"cognition_mode": "resident"})
        self.assertIsNone(apply_cognition(None, char))


class TestResidentInjection(unittest.TestCase):
    def setUp(self):
        self.char = FakeChar(persona_params={"cognition_mode": "resident"})
        self.out = apply_cognition(FULL_PROMPT, self.char)

    def test_mentions_living_in_the_computer(self):
        self.assertIn("住在", self.out)
        self.assertIn("程序", self.out)
        self.assertIn("驻留智能体", self.out)

    def test_never_leaks_assistant_speaking_style(self):
        for bad in FORBIDDEN_STRINGS:
            self.assertNotIn(bad, self.out, f"驻留模式提示词里出现了助手腔模板：{bad}")

    def test_original_persona_still_there(self):
        # 原人格不能被覆盖/替换
        self.assertIn("你叫小栖，25 岁，住在杭州", self.out)
        self.assertIn("【说话风格】", self.out)
        self.assertTrue(self.out.startswith("【身份与背景"))

    def test_inserted_before_format_requirement(self):
        # 「人格描述之后、回复格式要求之前」
        self.assertLess(self.out.index("认知模式：驻留智能体"), self.out.index("【最后提醒"))
        self.assertGreater(self.out.index("认知模式：驻留智能体"), self.out.index("【身份与背景"))

    def test_tail_append_when_no_format_anchor(self):
        out = apply_cognition("只有人设的一段话", self.char)
        self.assertTrue(out.startswith("只有人设的一段话"))
        self.assertIn("住在", out)

    def test_idempotent(self):
        once = apply_cognition(FULL_PROMPT, self.char)
        twice = apply_cognition(once, self.char)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("认知模式：驻留智能体"), 1)

    def test_blocks_contract(self):
        blocks = cognition_blocks(self.char)
        self.assertEqual(blocks["mode"], MODE_RESIDENT)
        self.assertIsInstance(blocks["identity"], str)
        self.assertIsInstance(blocks["rules"], list)
        self.assertGreaterEqual(len(blocks["rules"]), 3)
        joined = blocks["identity"] + "".join(blocks["rules"])
        for bad in FORBIDDEN_STRINGS:
            self.assertNotIn(bad, joined)


class TestModeFallback(unittest.TestCase):
    def test_default_is_roleplay(self):
        self.assertEqual(DEFAULT_MODE, MODE_ROLEPLAY)
        self.assertEqual(set(VALID_MODES), {"roleplay", "resident"})

    def test_illegal_value_falls_back_to_roleplay(self):
        for bad in ("xxx", "驻留", "agent", "roleplay2", "RESIDENT_MODE", 123, [], {}):
            self.assertEqual(normalize_mode(bad), MODE_ROLEPLAY, bad)
            char = FakeChar(persona_params={"cognition_mode": bad})
            self.assertEqual(get_cognition_mode(char), MODE_ROLEPLAY)
            # 回退后同样是严格无操作
            self.assertEqual(apply_cognition(FULL_PROMPT, char), FULL_PROMPT)

    def test_unset_sentinels_follow_default(self):
        for sentinel in (None, "", "default", "follow", "auto", "none"):
            self.assertEqual(normalize_mode(sentinel), MODE_ROLEPLAY, repr(sentinel))
            self.assertEqual(get_cognition_mode(FakeChar({"cognition_mode": sentinel})), MODE_ROLEPLAY)

    def test_case_insensitive(self):
        self.assertEqual(normalize_mode("RESIDENT"), MODE_RESIDENT)
        self.assertEqual(normalize_mode("  Resident "), MODE_RESIDENT)


class TestLegacyCharacterFile(unittest.TestCase):
    """老 .json（无 cognition_mode）必须自动等价 roleplay，且不许报错"""

    def test_from_dict_without_field(self):
        char = Character.from_dict(legacy_character_json())
        self.assertNotIn("cognition_mode", char.persona_params)
        self.assertEqual(get_cognition_mode(char), MODE_ROLEPLAY)
        self.assertEqual(cognition_blocks(char)["mode"], MODE_ROLEPLAY)

    def test_roundtrip_keeps_schema(self):
        # 不加新 dataclass 字段：老数据的键集合必须原样稳定
        char = Character.from_dict(legacy_character_json())
        d = char.to_dict()
        self.assertNotIn("cognition_mode", d)
        self.assertEqual(sorted(d.keys()), sorted(Character.__dataclass_fields__.keys()))

    def test_to_prompt_unaffected_for_legacy(self):
        char = Character.from_dict(legacy_character_json())
        prompt = char.to_prompt()
        self.assertEqual(apply_cognition(prompt, char), prompt)

    def test_real_character_object_resident(self):
        data = legacy_character_json(persona_params={"mbti": "ENFP", "cognition_mode": "resident"})
        char = Character.from_dict(data)
        out = apply_cognition(char.to_prompt(), char)
        self.assertIn("住在", out)
        self.assertIn("小办", out)
        for bad in FORBIDDEN_STRINGS:
            self.assertNotIn(bad, out)

    def test_json_file_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "legacy.json"
            p.write_text(json.dumps(legacy_character_json(), ensure_ascii=False), encoding="utf-8")
            char = Character.from_file(str(p))
            self.assertEqual(get_cognition_mode(char), MODE_ROLEPLAY)


class TestSetMode(unittest.TestCase):
    def test_set_and_clear(self):
        char = Character.from_dict(legacy_character_json())
        self.assertEqual(set_cognition_mode(char, "resident"), MODE_RESIDENT)
        self.assertEqual(char.persona_params.get("cognition_mode"), "resident")
        self.assertEqual(char.persona_params.get("mbti"), "ENFP")  # 不冲掉其它参数
        # 跟随默认 = 删键
        self.assertEqual(set_cognition_mode(char, None), MODE_ROLEPLAY)
        self.assertNotIn("cognition_mode", char.persona_params)
        # 非法值不写脏数据
        self.assertEqual(set_cognition_mode(char, "xxx"), MODE_ROLEPLAY)
        self.assertNotIn("cognition_mode", char.persona_params)

    def test_dict_character(self):
        d = {"persona_params": {}}
        self.assertEqual(set_cognition_mode(d, "resident"), MODE_RESIDENT)
        self.assertEqual(d["persona_params"]["cognition_mode"], "resident")


class TestWiring(unittest.TestCase):
    """接线护栏：提示词拼装处真的调了，而且炸了不会带走整轮对话"""

    def test_chat_and_active_prompt_call_apply(self):
        self.assertEqual(LLM_SRC.count("from characters.cognition import apply_cognition"), 2)
        self.assertEqual(LLM_SRC.count("apply_cognition(_persona_text, char)"), 1)
        self.assertEqual(LLM_SRC.count("apply_cognition(persona, char)"), 1)

    def test_each_call_is_guarded(self):
        # 两处都必须 try/except + warning 降级
        self.assertEqual(LLM_SRC.count("except Exception as _cog_err:"), 2)
        self.assertEqual(LLM_SRC.count("[认知模式] 注入失败"), 1)
        self.assertEqual(LLM_SRC.count("[认知模式] 主动消息注入失败"), 1)

    def test_demo_exposes_mode_on_get(self):
        self.assertIn('"cognition_mode": get_cognition_mode(char),', DEMO_SRC)

    def test_demo_has_patch_endpoint(self):
        self.assertIn('@app.patch("/v1/characters/{char_id}")', DEMO_SRC)
        # persona_params 必须浅合并（不能整体覆盖掉 MBTI / 好感度等既有参数）
        start = DEMO_SRC.index('# ============ 角色 API')
        body = DEMO_SRC[start:DEMO_SRC.index('@app.post("/v1/characters/select")', start)]
        self.assertIn('merged = dict(getattr(char, "persona_params", None) or {})', body)


if __name__ == "__main__":
    unittest.main()
