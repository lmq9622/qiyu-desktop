# -*- coding: utf-8 -*-
"""memory 层移植测试（标准库 unittest，无需 pytest / 网络 / 模型）。

覆盖服务器版搬到桌面版的 8 项修复。所有落盘都指向 QIYU_DATA_DIR 下的临时目录，
在 import memory 之前设置，绝不碰真实 data/。
"""
import ast
import glob
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

# ---- 必须在 import memory 之前把数据目录指到临时目录 ----
_TMP_DATA = tempfile.mkdtemp(prefix="qiyu_test_data_")
os.environ["QIYU_DATA_DIR"] = _TMP_DATA

import memory  # noqa: E402

# discover 按字母序先导入别的测试模块，它们间接 import 了 memory，此时 QIYU_DATA_DIR 还没设，
# memory 已把数据目录绑到真实 data/；tests/__init__.py 在这种 discover 下根本不会被导入
# （test 目录被当成顶层目录，模块按顶层名导入）。所以这里再兜一道：发现绑定到了别处就把
# memory 重新加载，让它按刚设好的临时目录重新绑定，保证本文件的落盘绝不写进真实 data/。
if str(Path(memory._DATA_DIR).resolve()) != str(Path(_TMP_DATA).resolve()):
    import importlib
    importlib.reload(memory)

from memory import MemoryManager, KnowledgeBase, VectorStore, _atomic_write_text, _looks_like_insult  # noqa: E402


TODAY = datetime.now().strftime("%Y-%m-%d")


def _no_tmp_leftover(root) -> bool:
    return not glob.glob(os.path.join(str(root), "**", "*.tmp*"), recursive=True)


class _FakeLLM:
    """记录收到的 prompt，并按 prompt 类型返回预设内容。"""
    def __init__(self, summary="", facts="", items=None):
        self.summary = summary
        self.facts = facts
        self.items = items
        self.prompts = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "记忆整理器" in prompt:
            return self.summary
        if "用户画像提取器" in prompt:
            return self.facts
        return self.items if self.items is not None else ""


class TestAtomicWrite(unittest.TestCase):
    """项1：所有落盘改原子写（临时文件 + os.replace）。"""

    def test_atomic_write_text_content_and_no_tmp(self):
        d = tempfile.mkdtemp(prefix="aw_")
        p = Path(d) / "x.json"
        _atomic_write_text(p, "你好")
        self.assertEqual(p.read_text(encoding="utf-8"), "你好")
        self.assertEqual(glob.glob(os.path.join(d, "*.tmp*")), [])

    def test_vector_store_save_is_atomic(self):
        d = Path(tempfile.mkdtemp(prefix="vs_"))
        vs = VectorStore("user_t_atomic", storage_dir=d)
        vs.add("用户喜欢在周末爬山", memory_type="long_term")
        self.assertTrue((d / "user_t_atomic.json").exists())
        self.assertTrue(_no_tmp_leftover(d))

    def test_all_manager_writes_leave_no_tmp(self):
        mm = MemoryManager()
        uid = "u_atomic"
        mm.add_message(uid, "user", "今天天气不错啊", character_id="c1")
        mm.mark_classified_day(uid, "c1", TODAY)
        mm.save_daily_outline(uid, TODAY, "今天聊了天气", char_id="c1")
        mm.save_daily_events(uid, TODAY, "c1", [{"text": "聊了天气", "weight": 0.3}])
        mm.save_dual_memory(uid, ["用户住在杭州"], char_id="c1")
        self.assertTrue(_no_tmp_leftover(memory.MEMORY_DIR))
        base = Path(_TMP_DATA) / "users" / uid
        self.assertTrue(_no_tmp_leftover(base))


class TestClassifiedDaySkip(unittest.TestCase):
    """项2：只有真入库才 mark_classified_day，否则当天被永久跳过。"""

    def _seed_msgs(self, mm, uid, cid):
        for i in range(4):
            mm.add_message(uid, "user", f"这是第{i}条测试消息内容", character_id=cid)

    def test_empty_items_not_marked(self):
        mm = MemoryManager()
        uid, cid = "u_cls_empty", "c1"
        self._seed_msgs(mm, uid, cid)
        llm = _FakeLLM(items='[{"text":"ab","type":"event"}]')  # 全部 <4 字，入不了库
        import asyncio
        res = asyncio.run(
            mm.classify_day_memories(uid, cid, TODAY, llm))
        self.assertFalse(mm.has_classified_day(uid, cid, TODAY))
        self.assertEqual(res.get("long", 0), 0)
        self.assertEqual(res.get("short", 0), 0)

    def test_real_items_marked(self):
        mm = MemoryManager()
        uid, cid = "u_cls_real", "c1"
        self._seed_msgs(mm, uid, cid)
        llm = _FakeLLM(items='[{"text":"用户喜欢喝不加糖的美式咖啡","type":"event","weight":0.3}]')
        import asyncio
        res = asyncio.run(
            mm.classify_day_memories(uid, cid, TODAY, llm))
        self.assertTrue(mm.has_classified_day(uid, cid, TODAY))
        self.assertGreaterEqual(res.get("short", 0), 1)


class TestInsultFilter(unittest.TestCase):
    """项3：骂人话不当事实；亲属身故双重过滤。"""

    def test_looks_like_insult(self):
        self.assertTrue(_looks_like_insult("你妈死了"))
        self.assertTrue(_looks_like_insult("去死吧你"))
        self.assertFalse(_looks_like_insult("我妈妈今天做了红烧肉"))
        self.assertFalse(_looks_like_insult("这是一句非常长的正常句子" * 5))

    def _run(self, mm, uid, cid):
        # 制造超过保留窗口的历史，让 pending_compress 有内容
        for i in range(25):
            mm.add_message(uid, "user", f"你好呀这是第{i}条普通消息", character_id=cid)
        # 插入脏话 + 正常自曝
        mm.add_message(uid, "user", "你妈死了", character_id=cid)
        mm.add_message(uid, "user", "我平时喜欢养猫", character_id=cid)
        llm = _FakeLLM(summary="用户和AI聊了日常", facts="用户母亲去世\n用户喜欢养猫")
        import asyncio
        asyncio.run(mm.compress_history(uid, llm, char_id=cid))
        return llm

    def test_insult_excluded_and_grief_dropped(self):
        mm = MemoryManager()
        uid, cid = "u_insult", "c1"
        llm = self._run(mm, uid, cid)
        fact_prompt = [p for p in llm.prompts if "用户画像提取器" in p][0]
        # 脏话不进事实提取 prompt
        self.assertNotIn("你妈死了", fact_prompt)
        # 从脏话误推的"母亲去世"被丢弃，正常事实保留
        profile = mm.get_user_profile(uid)
        self.assertNotIn("母亲去世", profile)
        self.assertIn("养猫", profile)


class TestHistoryPreserved(unittest.TestCase):
    """项4：压缩不再删原文；只在 >HARD_KEEP 才硬裁。"""

    def test_compress_keeps_originals(self):
        mm = MemoryManager()
        uid, cid = "u_keep", "c1"
        for i in range(25):
            mm.add_message(uid, "user", f"消息{i}内容测试", character_id=cid)
        before = len(mm.get_recent_history(uid, limit=1000, char_id=cid))
        llm = _FakeLLM(summary="用户聊了日常", facts="无")
        import asyncio
        asyncio.run(mm.compress_history(uid, llm, char_id=cid))
        after = len(mm.get_recent_history(uid, limit=1000, char_id=cid))
        self.assertEqual(before, after)                 # 原文一条不少
        self.assertEqual(mm.pending_compress(uid, cid), [])  # 不重复整理

    def test_trim_refuses_below_hard_keep(self):
        mm = MemoryManager()
        uid, cid = "u_trim", "c1"
        for i in range(30):
            mm.add_message(uid, "user", f"t{i}", character_id=cid)
        mm.trim_history(uid, 10, cid)                   # 会被拒绝
        self.assertEqual(len(mm.get_recent_history(uid, limit=1000, char_id=cid)), 30)

    def test_trim_force_trims(self):
        mm = MemoryManager()
        uid, cid = "u_trim_force", "c1"
        for i in range(30):
            mm.add_message(uid, "user", f"t{i}", character_id=cid)
        mm.trim_history(uid, 10, cid, force=True)
        self.assertEqual(len(mm.get_recent_history(uid, limit=1000, char_id=cid)), 20)


class TestFullDaySummary(unittest.TestCase):
    """项5：全天总结同日只留一条。"""

    def test_only_one_entry(self):
        mm = MemoryManager()
        uid, cid = "u_fds", "c1"
        mm.save_daily_events(uid, TODAY, cid, [{"text": "今天去爬山了", "weight": 0.3}])
        self.assertTrue(mm.save_full_day_summary(uid, cid, TODAY))
        self.assertTrue(mm.save_full_day_summary(uid, cid, TODAY))   # 幂等
        store = mm._get_store(uid, cid)
        hits = [e for e in store.entries if f"【{TODAY} 全天总结】" in (e.text or "")]
        self.assertEqual(len(hits), 1)

    def test_no_data_returns_false(self):
        mm = MemoryManager()
        self.assertFalse(mm.save_full_day_summary("u_fds_none", "c1", TODAY))


class TestSelfMemory(unittest.TestCase):
    """项6：角色自身记忆（memory_write.self）单独存 record_type=self。"""

    def test_self_items_stored_separately(self):
        mm = MemoryManager()
        uid, cid = "u_self", "c1"
        mm.save_dual_memory(uid, ["用户是程序员"], short_items=None,
                            char_id=cid, self_items=["AI 说自己是下午没课"])
        store = mm._get_store(uid, cid)
        longs = [e for e in store.entries if e.memory_type == "long_term"]
        self_entries = [e for e in longs if (e.metadata or {}).get("record_type") == "self"]
        user_entries = [e for e in longs if (e.metadata or {}).get("type") == "long_fact"]
        self.assertEqual(len(self_entries), 1)
        self.assertIn("下午没课", self_entries[0].text)
        self.assertEqual(len(user_entries), 1)


class TestRagCharIsolation(unittest.TestCase):
    """项7：知识库按角色隔离（专属 + 全局），旧条目按文件名回推归属。"""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="rag_"))
        self.kb = KnowledgeBase(storage_dir=self.d)

    def _mk(self, name, text):
        p = self.d / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_char_isolation(self):
        a = self._mk("a.txt", "阿尔法专属资料：栖语角色A的私人设定内容")
        b = self._mk("b.txt", "贝塔专属资料：栖语角色B的私人设定内容")
        g = self._mk("g.txt", "全局资料：所有角色都能用的共同背景内容")
        self.kb.index_document(a, char_id="char_a")
        self.kb.index_document(b, char_id="char_b")
        self.kb.index_document(g)  # 全局
        res = self.kb.search("栖语角色私人设定资料内容", top_k=5, char_id="char_a")
        srcs = {r["metadata"].get("source") for r in res}
        self.assertIn(str(a), srcs)
        self.assertNotIn(str(b), srcs)
        # 不给 char_id 时不过滤（兼容旧调用）
        self.assertTrue(self.kb.search("栖语角色私人设定资料内容", top_k=5))

    def test_old_entry_owner_inferred_from_filename(self):
        src = str(self.d / "resume_char_beta.md")
        self.kb.store.add(text="贝塔的旧版自述：喜欢画画和跑步",
                          metadata={"source": src, "chunk_index": 0}, memory_type="knowledge")
        res_a = self.kb.search("贝塔的旧版自述喜欢画画和跑步", top_k=5, char_id="char_a")
        self.assertNotIn(src, {r["metadata"].get("source") for r in res_a})
        res_b = self.kb.search("贝塔的旧版自述喜欢画画和跑步", top_k=5, char_id="char_beta")
        self.assertIn(src, {r["metadata"].get("source") for r in res_b})


class TestEmbeddingOffline(unittest.TestCase):
    """项8：embedding 加载用 local_files_only=True（离线不卡超时）。"""

    def test_local_files_only_present(self):
        path = Path(__file__).resolve().parent.parent / "embedding" / "service.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn("local_files_only=True", src)
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(
                    kw.arg == "local_files_only" for kw in node.keywords):
                found = True
        self.assertTrue(found, "SentenceTransformer 调用里没有 local_files_only")


class TestAtomicWriteRetry(unittest.TestCase):
    """项5：Windows 上 os.replace 目标被占用（PermissionError）要退避重试，最终失败清 tmp 并抛出。"""

    def test_retries_then_succeeds(self):
        d = tempfile.mkdtemp(prefix="awr_")
        p = Path(d) / "x.json"
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] <= 2:            # 前两次假装被占用
                raise PermissionError("occupied")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=flaky):
            _atomic_write_text(p, "内容")
        self.assertEqual(p.read_text(encoding="utf-8"), "内容")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(glob.glob(os.path.join(d, "*.tmp*")), [])   # 没留临时文件

    def test_gives_up_cleans_tmp_and_raises(self):
        d = tempfile.mkdtemp(prefix="awf_")
        p = Path(d) / "x.json"
        with mock.patch("os.replace", side_effect=PermissionError("occupied")):
            with self.assertRaises(PermissionError):
                _atomic_write_text(p, "内容")
        self.assertEqual(glob.glob(os.path.join(d, "*.tmp*")), [])   # tmp 已清理
        self.assertFalse(p.exists())


class TestSumDonePersistence(unittest.TestCase):
    """项1：压缩水位 _sum_done 落盘，重启后不丢（否则每次重启都重压整段历史）。"""

    def test_watermark_survives_reload(self):
        mm = MemoryManager()
        uid, cid = "u_wm_reload", "c1"
        for i in range(25):
            mm.add_message(uid, "user", f"水位测试消息第{i}条内容", character_id=cid)
        llm = _FakeLLM(summary="用户聊了日常", facts="无")
        import asyncio
        asyncio.run(mm.compress_history(uid, llm, char_id=cid))
        hk = mm._hkey(uid, cid)
        done = mm._sum_done[hk]
        self.assertEqual(done, 5)                             # 25 - 保留窗口(20)
        # 水位文件确实落盘
        sf = mm._sum_file(hk)
        self.assertTrue(sf.exists())
        self.assertEqual(json.loads(sf.read_text(encoding="utf-8")), {"sum_done": 5})
        # 重启：新实例读回水位后不再重复整理，也不会把旧历史重新压缩
        mm2 = MemoryManager()
        self.assertEqual(mm2._sum_done.get(hk), 5)
        self.assertEqual(mm2.pending_compress(uid, cid), [])
        self.assertEqual(len(mm2.get_recent_history(uid, limit=1000, char_id=cid)), 25)

    def test_legacy_history_defaults_to_all_done(self):
        """旧格式历史（无水位文件）首次加载视为已全部整理过，避免升级首启重压几个月历史。"""
        mm = MemoryManager()
        uid, cid = "u_wm_legacy", "c1"
        hk = mm._hkey(uid, cid)
        hp = mm._history_file(hk)                            # 造一个旧格式（纯 list）历史
        legacy = [{"role": "user", "content": f"老消息{i}", "timestamp": "2026-01-01T00:00:00",
                   "character_id": cid} for i in range(40)]
        hp.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(mm._sum_file(hk).exists())
        mm2 = MemoryManager()
        self.assertEqual(mm2._sum_done.get(hk), 40)          # 默认 = len(history)
        self.assertEqual(mm2.pending_compress(uid, cid), [])  # 不重压
        # 新加一条后水位仍指向已整理位置，只有新增那条待整理
        mm2.add_message(uid, "user", "升级后的新消息内容", character_id=cid)
        self.assertEqual(len(mm2.pending_compress(uid, cid)), 0)   # 1 条未超保留窗口

    def test_sum_file_not_treated_as_history(self):
        mm = MemoryManager()
        uid, cid = "u_wm_pair", "c1"
        for i in range(25):
            mm.add_message(uid, "user", f"配对测试第{i}条内容", character_id=cid)
        import asyncio
        asyncio.run(mm.compress_history(uid, _FakeLLM(summary="s", facts="无"), char_id=cid))
        mm2 = MemoryManager()
        self.assertNotIn(f"{mm._hkey(uid, cid)}.sum", mm2._chat_histories)
        self.assertFalse(any(str(c).endswith(".sum") for _, c in mm2.get_user_char_pairs()))


class TestTrimAndClearWatermark(unittest.TestCase):
    """项2/3：硬裁与清空历史时水位同步移动/归零，避免中间段永远不被摘要。"""

    def test_trim_shifts_watermark(self):
        mm = MemoryManager()
        uid, cid = "u_wm_trim", "c1"
        for i in range(30):
            mm.add_message(uid, "user", f"裁剪水位第{i}条", character_id=cid)
        hk = mm._hkey(uid, cid)
        mm._sum_done[hk] = 30
        mm.trim_history(uid, 10, cid, force=True)
        self.assertEqual(mm._sum_done[hk], 20)               # 30 - 10
        self.assertEqual(json.loads(mm._sum_file(hk).read_text(encoding="utf-8")), {"sum_done": 20})
        # 裁剪后新消息能正常进入待整理区间（若水位没跟着减，这里会永远拿不到内容）
        for i in range(25):
            mm.add_message(uid, "user", f"裁剪之后的新消息第{i}条", character_id=cid)
        self.assertEqual(len(mm.pending_compress(uid, cid)), 5)   # 45 条 - 水位 20 - 保留 20

    def test_clear_history_resets_watermark(self):
        mm = MemoryManager()
        uid, cid = "u_wm_clear", "c1"
        for i in range(25):
            mm.add_message(uid, "user", f"清空水位第{i}条", character_id=cid)
        hk = mm._hkey(uid, cid)
        mm._sum_done[hk] = 25
        mm.clear_history(uid, cid)
        self.assertEqual(mm._sum_done[hk], 0)
        self.assertFalse(mm._sum_file(hk).exists())          # 水位文件一并清掉
        self.assertEqual(mm.get_recent_history(uid, limit=100, char_id=cid), [])
        # 重启后也不会残留旧水位
        mm2 = MemoryManager()
        self.assertEqual(mm2._sum_done.get(hk), None)
        # 清空后再聊，超出保留窗口的内容会被正常待整理（而不是要涨到旧水位）
        for i in range(25):
            mm2.add_message(uid, "user", f"清空后的新消息第{i}条", character_id=cid)
        self.assertEqual(len(mm2.pending_compress(uid, cid)), 5)


class TestPersistStripsOldImages(unittest.TestCase):
    """项4：落盘历史不存早期图片 base64（占位），最近若干条仍保留。"""

    def test_only_recent_images_kept_on_disk(self):
        mm = MemoryManager()
        uid, cid = "u_img_strip", "c1"
        img = "data:image/png;base64," + ("A" * 2000)
        for i in range(25):
            mm.add_message(uid, "user", f"带图消息第{i}条", character_id=cid, images=[img])
        hk = mm._hkey(uid, cid)
        on_disk = json.loads(mm._history_file(hk).read_text(encoding="utf-8"))
        kept = [m for m in on_disk if (m.get("images") or [None])[0] != "[图片]"]
        stripped = [m for m in on_disk if (m.get("images") or [None])[0] == "[图片]"]
        self.assertEqual(len(on_disk), 25)
        self.assertEqual(len(kept), mm.HISTORY_KEEP_IMG_MSGS)     # 最近 20 条带原文
        self.assertEqual(len(stripped), 5)                         # 更早 5 条只留占位
        # 内存里的原文不动：前端本次会话翻历史照旧有图
        self.assertTrue(all((m.get("images") or [""])[0].startswith("data:image")
                            for m in mm._chat_histories[hk]))

    def test_plain_messages_untouched(self):
        mm = MemoryManager()
        uid, cid = "u_img_plain", "c1"
        mm.add_message(uid, "user", "没有图的普通消息内容", character_id=cid)
        on_disk = json.loads(mm._history_file(mm._hkey(uid, cid)).read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk), 1)
        self.assertNotIn("images", on_disk[0])


class TestTestDataIsolation(unittest.TestCase):
    """项6：测试必须绑定到 QIYU_DATA_DIR 临时目录，绝不碰真实 data/。"""

    def test_data_dir_is_temp_and_not_project_data(self):
        dd = os.environ.get("QIYU_DATA_DIR", "")
        self.assertTrue(dd, "测试环境应已设好 QIYU_DATA_DIR")
        # memory 绑定的目录必须落在系统临时根下，而不是工程内的 data/
        project_data = Path(__file__).resolve().parent.parent / "data"
        self.assertFalse(str(memory.MEMORY_DIR).startswith(str(project_data)))
        self.assertTrue(str(memory._DATA_DIR).startswith(tempfile.gettempdir()))
        # 实际落盘也确认落在临时目录里
        mm = MemoryManager()
        mm.add_message("u_isolation_probe", "user", "隔离探针消息", character_id="c1")
        hp = mm._history_file(mm._hkey("u_isolation_probe", "c1"))
        self.assertTrue(str(hp).startswith(tempfile.gettempdir()))
        self.assertFalse(str(hp).startswith(str(project_data)))


if __name__ == "__main__":
    unittest.main()
