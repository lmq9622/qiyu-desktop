# -*- coding: utf-8 -*-
"""wb-* 中枢会丢弃 system 角色：验证我们把设定改塞进第一条 user。"""
import unittest
from companion.llm import _with_system


class TestSystemInjection(unittest.TestCase):
    def test_wb_uses_user_message(self):
        msgs = [{"role": "user", "content": "在吗"}]
        out = _with_system("【人设】你是小办", msgs, "wb-deepseekv4.1")
        self.assertEqual(out[0]["role"], "user")
        self.assertIn("系统设定", out[0]["content"])
        self.assertIn("你是小办", out[0]["content"])
        self.assertEqual(out[1]["content"], "在吗")
        self.assertNotIn("system", [m["role"] for m in out])

    def test_non_wb_keeps_system_role(self):
        msgs = [{"role": "user", "content": "在吗"}]
        out = _with_system("你是小办", msgs, "deepseek-flash")
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[1]["content"], "在吗")


if __name__ == "__main__":
    unittest.main()
