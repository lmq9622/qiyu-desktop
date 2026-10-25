# -*- coding: utf-8 -*-
"""真人短聊硬护栏：≤3 条、每条 ≤15 字；长文否定词不触发。"""
import unittest
from companion.behavior import _shorten_bubbles
from companion.settings import _is_longform_request


class TestHumanGuard(unittest.TestCase):
    def test_shorten_bubbles_caps_count_and_length(self):
        msgs = [
            {"text": "这是一条非常非常非常长而且超过十五个字的消息", "type": "statement", "delay": 0},
            {"text": "第二条", "type": "statement", "delay": 300},
            {"text": "第三条", "type": "statement", "delay": 300},
            {"text": "第四条", "type": "statement", "delay": 300},
        ]
        out = _shorten_bubbles(msgs)
        self.assertLessEqual(len(out), 3)
        self.assertTrue(all(len(m["text"]) <= 15 for m in out))

    def test_consecutive_ellipsis_collapsed(self):
        out = _shorten_bubbles([
            {"text": "…", "type": "thinking", "delay": 0},
            {"text": "…", "type": "thinking", "delay": 100},
            {"text": "行", "type": "statement", "delay": 300},
        ])
        self.assertEqual([m["text"] for m in out], ["…", "行"])

    def test_longform_negation(self):
        self.assertFalse(_is_longform_request("不用太详细，简单说"))
        self.assertTrue(_is_longform_request("详细讲讲怎么学英语"))
        self.assertFalse(_is_longform_request("讲个笑话"))


if __name__ == "__main__":
    unittest.main()
