# -*- coding: utf-8 -*-
"""MBTI 口癖块：有类型才注入，且不同型口癖不同。"""
import unittest
from companion.mbti_voice import build_mbti_voice_block, mbti_profile


class _C:
    def __init__(self, mbti):
        self.persona_params = {"mbti": mbti} if mbti else {}


class TestMbtiVoice(unittest.TestCase):
    def test_entp_fillers(self):
        block = build_mbti_voice_block(_C("ENTP"))
        self.assertIn("诶", block)
        self.assertIn("你想想", block)
        self.assertIn("优先级高于通用语气词", block)

    def test_isfp_differs(self):
        entp = build_mbti_voice_block(_C("ENTP"))
        isfp = build_mbti_voice_block(_C("ISFP"))
        self.assertNotEqual(entp, isfp)
        self.assertIn("别多想", isfp)

    def test_missing_mbti_empty(self):
        self.assertEqual(build_mbti_voice_block(_C("")), "")
        self.assertEqual(mbti_profile(_C("")), {})


if __name__ == "__main__":
    unittest.main()
