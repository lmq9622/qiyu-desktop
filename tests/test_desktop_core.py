# -*- coding: utf-8 -*-
"""桌面版主链路护栏（2026-10-14 移植批次，主控部分）

覆盖：
- 忙标志在异常路径也释放（否则「出一次错之后再也不回」）
- 记忆清理能选中按角色隔离的记忆库（原来键前缀写错，清理从未执行）
- 默认只监听本机 + 设置接口不回传明文 api_key
- 空回复不再以「(无回复)」写进历史
- build.py --fast 拒绝指向别处的 spec
- companion.state 与 pathutil 数据目录同一口径

运行：python -X utf8 -m unittest tests.test_desktop_core -v
"""
import asyncio
import contextlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEMO_SRC = (ROOT / "demo.py").read_text(encoding="utf-8")


def _load_chat_gate_cls():
    """只抽出 UserChatGate 类执行，避免 import demo 拉起整套运行时"""
    start = DEMO_SRC.index("class UserChatGate")
    end = DEMO_SRC.index("chat_gate = UserChatGate()")
    ns = {"asyncio": asyncio, "contextlib": contextlib}
    exec(DEMO_SRC[start:end], ns)
    return ns["UserChatGate"]


class TestChatGateRelease(unittest.TestCase):
    def test_busy_released_on_exception(self):
        gate = _load_chat_gate_cls()()

        async def run():
            lock = gate.lock_for("u")
            with self.assertRaises(RuntimeError):
                async with lock, gate.busy_scope("u"):
                    self.assertTrue(gate.is_busy("u"))
                    raise RuntimeError("模拟写历史时抛错")
            self.assertFalse(gate.is_busy("u"))
            self.assertFalse(lock.locked())

        asyncio.run(run())

    def test_early_clear_then_exit_is_safe(self):
        gate = _load_chat_gate_cls()()

        async def run():
            async with gate.busy_scope("u"):
                gate.clear_busy("u")  # 块内提前释放（冲刷 pending 前）
            self.assertFalse(gate.is_busy("u"))

        asyncio.run(run())

    def test_all_chat_paths_use_scope(self):
        # 网页流式 / 非流式两条路径都必须走 busy_scope
        self.assertEqual(DEMO_SRC.count("async with lock, chat_gate.busy_scope(user_id):"), 2)
        self.assertNotIn("async with lock:", DEMO_SRC)

    def test_wechat_path_has_finally(self):
        body = DEMO_SRC[DEMO_SRC.index("async def _process_wechat_msg"):
                        DEMO_SRC.index("async def handle_wechat_msg")]
        self.assertRegex(body, r"finally:\s*\n(\s*#.*\n)*\s*chat_gate\.clear_busy\(user_id\)")


class TestStreamPathMarksSleep(unittest.TestCase):
    def test_stream_path_records_sleep_window(self):
        # 2026-10-16 真模型验收发现：网页流式路径不经过 behavior._finalize_chat_reply，
        # 她说了「晚安」也没记睡眠窗口，之后问「你睡了吗」她回「没刚躺下」
        body = DEMO_SRC[DEMO_SRC.index("relation = _apply_chat_side_effects(user_id, char_id, parsed, user_content)"):]
        self.assertIn("mark_sleep_from_reply", body[:800])


class TestChoicesIndexSafe(unittest.TestCase):
    def test_no_bare_choices_index(self):
        # 2026-10-16：DeepSeek 流式会发 choices=[] 的块（如 usage 尾块），
        # obj.get("choices", [{}])[0] 遇到空列表直接 IndexError → 每轮流式都「生成失败」再走兜底
        for rel in ("demo.py", "companion/llm.py", "companion/active.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn('.get("choices", [{}])[0]', src, rel)
            self.assertNotIn('["choices"][0]', src, rel)


class TestNoFakeEmptyReply(unittest.TestCase):
    def test_no_placeholder_written_to_history(self):
        self.assertNotIn('"(无回复)"', DEMO_SRC)
        self.assertNotIn("'(无回复)'", DEMO_SRC)


class TestMemoryExpireSelectsStores(unittest.TestCase):
    def test_selects_per_char_stores(self):
        import memory
        m = memory.MemoryManager.__new__(memory.MemoryManager)
        m._stores = {"web_user__a": "A", "web_user__b": "B", "web_user": "D",
                     "other__a": "X", "web_user2__a": "Y"}
        hit = []
        m._clean_store = lambda s: hit.append(s) or False
        m.expire_and_promote("web_user")
        self.assertEqual(sorted(hit), ["A", "B", "D"])


class TestNetworkExposure(unittest.TestCase):
    def test_default_host_is_loopback(self):
        m = re.search(r'DEMO_HOST\s*=\s*os\.getenv\("QIYU_HOST",\s*"([^"]+)"\)', DEMO_SRC)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "127.0.0.1")

    def test_settings_get_masks_api_key(self):
        self.assertIn('"api_key": API_KEY_MASK if runtime.get("api_key") else ""', DEMO_SRC)
        self.assertNotIn('"api_key": runtime.get("api_key", "")', DEMO_SRC)

    def test_mask_never_overwrites_saved_key(self):
        # 两个保存入口都要忽略占位符
        self.assertIn("data.api_key != API_KEY_MASK", DEMO_SRC)
        self.assertIn('data.get("api_key") != API_KEY_MASK', DEMO_SRC)


class TestBuildSpecGuard(unittest.TestCase):
    def test_spec_guard(self):
        import build
        here = Path(r"D:\proj\qiyu")
        ok = "a = Analysis(\n    ['D:/proj/qiyu/client.py'],"
        other = "a = Analysis(\n    ['D:/Codex projects/ai-companion-codex/ai-companion/client.py'],"
        sibling = "a = Analysis(['D:/proj/qiyu2/client.py'],"
        self.assertTrue(build.spec_points_here(ok, here))
        self.assertFalse(build.spec_points_here(other, here))
        self.assertFalse(build.spec_points_here(sibling, here))
        self.assertFalse(build.spec_points_here("no analysis here", here))


class TestDataDirUnified(unittest.TestCase):
    def test_state_and_pathutil_agree(self):
        old = os.environ.pop("QIYU_DATA_DIR", None)
        try:
            import importlib
            import pathutil
            import companion.state as st
            importlib.reload(st)
            self.assertEqual(st.get_data_dir().resolve(), pathutil.get_data_dir().resolve())
            self.assertEqual(st.PROJECT_DIR.resolve(), ROOT)
        finally:
            if old is not None:
                os.environ["QIYU_DATA_DIR"] = old

    def test_legacy_migration_copies_missing_only(self):
        import companion.state as st
        orig_file = st.__file__
        with tempfile.TemporaryDirectory() as td:
            fake_pkg = Path(td) / "pkg"          # 伪装成 companion/，其下 data/ 即旧位置
            legacy = fake_pkg / "data"
            target = Path(td) / "data"
            legacy.mkdir(parents=True)
            target.mkdir()
            (legacy / "_runtime_settings.json").write_text('{"a":1}', encoding="utf-8")
            (legacy / "qiyu.db").write_text("old", encoding="utf-8")
            (target / "qiyu.db").write_text("new", encoding="utf-8")
            st.__file__ = str(fake_pkg / "state.py")
            try:
                st._LEGACY_MIGRATED = False
                st._migrate_legacy_companion_data(target)
            finally:
                st.__file__ = orig_file
                st._LEGACY_MIGRATED = True
            self.assertEqual((target / "_runtime_settings.json").read_text(encoding="utf-8"), '{"a":1}')
            self.assertEqual((target / "qiyu.db").read_text(encoding="utf-8"), "new")  # 不覆盖
            self.assertTrue((legacy / "qiyu.db").exists())  # 不删源


if __name__ == "__main__":
    unittest.main()
