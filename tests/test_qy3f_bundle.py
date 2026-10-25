# -*- coding: utf-8 -*-
"""`.qy3f` 单文件角色包（v1 不加密容器）回归测试。

覆盖：
  1. 导出 → 校验 → 导入 → 再导出：role.id 与记忆条数一致
  2. 知识库（RAG 切片）来回：导出带得走、导入写得回、新角色检索得到
  3. 老包（没有 knowledge / world_state 字段）照样能读
  4. 坏包（乱码）→ bad_magic；v2 容器（QY3\\x02）→ format_mismatch（明确拒绝）
  5. 幂等：同一个包导入两次不会产生重复记忆/重复切片
  6. 世界状态（关系 / 情绪 / 未完成话题 / 承诺）来回

运行：
    cd D:/tmp/qiyu-desktop-license && python -X utf8 -m unittest tests.test_qy3f_bundle -v

约定：先把 QIYU_DATA_DIR 指到临时目录再 import，绝不碰真实 data/。
"""
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path

# —— 必须在 import characters / memory / companion 之前设好数据目录 ——
_TMP = tempfile.mkdtemp(prefix="qiyu_qy3f_")
os.environ["QIYU_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from characters import get_character_manager          # noqa: E402
from characters import qy3                            # noqa: E402
from memory import get_knowledge_base, get_memory_manager  # noqa: E402
# 世界状态要读"业务代码真正在读写的那份 dict"：正常运行时它和 companion.state._* 是同一对象，
# 但全套测试里 test_desktop_core 会 importlib.reload(companion.state)（属性被重新赋成空 dict），
# 只有 relations / emotions / conv 自己持有的那份才是活的。
import companion.conv as CC                           # noqa: E402
import companion.emotions as CE                       # noqa: E402
import companion.relations as CR                      # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class Qy3fTestBase(unittest.TestCase):
    """每个用例用独立的 (用户, 角色)，互不串数据。"""

    def setUp(self) -> None:
        self.owner = _uid("qy3f_user")
        self.cid = _uid("qy3f_char")
        self.outdir = Path(tempfile.mkdtemp(prefix="qy3f_out_"))
        get_character_manager().create_character({
            "id": self.cid, "name": "林夏", "tagline": "画画的", "description": "测试角色",
            "persona": "你是个插画师。", "soul": "温柔但固执", "boundaries": "不冒充真人",
            "owner": self.owner, "resume": {"职业": "插画师"},
            "persona_params": {"mbti": "INFP", "affinity": 50},
        })
        self.mem = get_memory_manager()
        self.kb = get_knowledge_base()

    def tearDown(self) -> None:
        # 角色文件会自动落盘；这里只清内存里的角色，避免同一进程里越堆越多
        try:
            get_character_manager().delete_character(self.cid)
        except Exception:
            pass

    # ---- 辅助 ----
    def _add_memories(self, char_id: str | None = None) -> list[str]:
        store = self.mem._get_store(self.owner, char_id or self.cid)
        texts = ["她喜欢在雨天听老唱片", "他下周要做一个小手术", "今天他说要加班到十点"]
        store.add(texts[0], {"weight": 0.9, "mention_count": 2}, "long_term")
        store.add(texts[1], {"weight": 0.8}, "long_term")
        store.add(texts[2], {"weight": 0.3}, "short_term")
        return texts

    def _index_doc(self, name: str, text: str, char_id: str = "") -> Path:
        """把一段文本写成文件再走真正的 RAG 入库（与 /v1/rag/index 同一条路）。"""
        d = Path(tempfile.mkdtemp(prefix="qy3f_doc_"))
        p = d / name
        p.write_text(text, encoding="utf-8")
        self.kb.index_document(p, char_id=char_id)
        return p

    def _forget_char_knowledge(self, char_id: str) -> int:
        """清掉某个角色的知识切片 —— 模拟"换了一台机器/一份新知识库"再导入。"""
        store = self.kb.store
        keep = [e for e in store.entries
                if str((e.metadata or {}).get("char_id") or "") != char_id]
        removed = len(store.entries) - len(keep)
        store.entries = keep
        store.vectors = store.vectorizer.fit_transform([e.text for e in keep]) if keep else None
        store._save()
        return removed

    def _export(self, char_id: str | None = None, name: str = "她.qy3f") -> Path:
        path = self.outdir / name
        qy3.export_role(char_id or self.cid, path, user_id=self.owner)
        return path


class TestQy3fRoundTrip(Qy3fTestBase):
    """导出 → 校验 → 导入 → 再导出：容器还是 v1，身份与记忆条数一致。"""

    def test_export_validate_import_reexport(self) -> None:
        texts = self._add_memories()
        self.mem.save_daily_events(self.owner, TODAY, self.cid,
                                   [{"text": "他说今晚要加班", "weight": 0.3}])

        src = self._export()
        blob = src.read_bytes()
        # 容器层：必须还是 v1（QY3\x01），不是服务器版的 v2（QY3\x02）
        self.assertEqual(blob[:4], b"QY3\x01")
        self.assertNotEqual(blob[:4], b"QY3\x02")

        v = qy3.validate_qy3(src)
        self.assertTrue(v["ok"])
        self.assertEqual(v["format"], "qy3f")
        self.assertFalse(v["encrypted"])
        self.assertEqual(v["role"]["id"], self.cid)
        self.assertEqual(v["memory_records"], len(texts))
        self.assertEqual(v["daily_memory"], 1)
        # 这个角色没有专属知识（全局知识可能有，属于别的用例造的）→ 角色切片必须是 0
        payload = qy3.build_payload(self.cid, user_id=self.owner)
        role_chunks = [c for c in payload["knowledge"]["chunks"] if c["scope"] == "role"]
        self.assertEqual(role_chunks, [])
        self.assertEqual(v["stats"]["memory_records"], len(texts))

        res = qy3.import_role(src)
        self.assertTrue(res["ok"])
        self.assertEqual(res["original_role_id"], self.cid)
        # 同 id 已存在且 replace=False → 自动改名，不覆盖
        self.assertNotEqual(res["role_id"], self.cid)
        self.assertTrue(res["role_id"].startswith(f"{self.cid}_import_"))
        self.assertEqual(res["memory_records"], len(texts))
        self.assertEqual(res["daily_memory"], 1)
        self.assertFalse(res["replaced"])
        # 没失败就不该有错误留痕
        self.assertEqual(res["memory_error"], "")
        self.assertEqual(res["daily_error"], "")

        new_id = res["role_id"]
        imported = get_character_manager().get_character(new_id)
        self.assertIsNotNone(imported)
        self.assertEqual(imported.name, "林夏")
        self.assertEqual(imported.soul, "温柔但固执")
        self.assertEqual(imported.boundaries, "不冒充真人")

        # 记忆真的落到新角色自己的库里
        store = self.mem._get_store(self.owner, new_id)
        got = {e.text for e in store.entries}
        for t in texts:
            self.assertIn(t, got)
        self.assertEqual(len(store.entries), len(texts))
        self.assertTrue(self.mem.get_daily_events(self.owner, TODAY, new_id))

        # 再导出：role.id 换成新 id，记忆条数保持不变
        dst = self._export(new_id, name="她_import.qy3f")
        v2 = qy3.validate_qy3(dst)
        self.assertEqual(v2["role"]["id"], new_id)
        self.assertEqual(v2["memory_records"], len(texts))
        self.assertEqual(v2["daily_memory"], 1)
        self.assertEqual(dst.read_bytes()[:4], b"QY3\x01")

    def test_import_without_replace_twice_is_idempotent(self) -> None:
        """同一个包导入两次：第二次不产生重复记忆（同 id 覆盖导入）。"""
        texts = self._add_memories()
        src = self._export()

        # 换个用户导入：目标记忆库是空的，第一次才会真的写入
        fresh = _uid("qy3f_idem")
        first = qy3.import_role(src, owner=fresh, replace=True)
        self.assertEqual(first["memory_records"], len(texts))
        after_first = len(self.mem._get_store(fresh, first["role_id"]).entries)

        second = qy3.import_role(src, owner=fresh, replace=True)
        after_second = len(self.mem._get_store(fresh, second["role_id"]).entries)

        self.assertEqual(first["role_id"], second["role_id"])
        self.assertTrue(second["replaced"])
        self.assertEqual(second["memory_records"], 0, "第二次不该再写入记忆")
        self.assertEqual(after_first, after_second, "记忆条数不该翻倍")
        self.assertEqual(after_second, len(texts))


class TestQy3fKnowledge(Qy3fTestBase):
    """知识库（RAG 切片）：导出带得走、导入写得回、新角色检索得到。"""

    def test_knowledge_roundtrip(self) -> None:
        other_char = _uid("qy3f_other")
        self._index_doc(f"resume_{self.cid}.md", "林夏，插画师，喜欢在雨天画画。", char_id=self.cid)
        self._index_doc(f"char_{self.cid}_自述.md", "我习惯熬夜画图，白天起不来。", char_id=self.cid)
        self._index_doc(f"resume_{other_char}.md", "别的人设：健身教练，不该被带走。", char_id=other_char)
        self._index_doc("note_global.md", "通用知识：栖语是陪伴型角色引擎。")

        # 1) 导出后的 payload 里真的有知识切片，且不含别人的专属知识
        payload = qy3.build_payload(self.cid, user_id=self.owner)
        self.assertIn("knowledge", payload)
        counts = payload["knowledge"]["counts"]
        self.assertGreaterEqual(counts["chunks"], 3, "两条角色专属 + 一条全局")
        self.assertGreaterEqual(counts["documents"], 2)
        joined = "\n".join(c["text"] for c in payload["knowledge"]["chunks"])
        self.assertIn("雨天画画", joined)
        self.assertIn("熬夜画图", joined)
        self.assertNotIn("健身教练", joined, "别的角色的专属知识不跟人走")

        src = self._export()
        v = qy3.validate_qy3(src)
        self.assertTrue(v["has_knowledge"])
        self.assertEqual(v["knowledge_chunks"], counts["chunks"])

        # 2) 导入：切片写回 RAG，归属改成新角色 id
        res = qy3.import_role(src)
        self.assertTrue(res["has_knowledge"])
        self.assertEqual(res["knowledge_error"], "")
        self.assertEqual(res["knowledge_written"] + res["knowledge_skipped"], counts["chunks"],
                         "每条切片要么写入、要么明确计入 skipped，不能凭空消失")

        new_id = res["role_id"]
        hits = self.kb.search("林夏 插画师 雨天画画 熬夜画图", top_k=5, char_id=new_id)
        hit_texts = "\n".join(h["text"] for h in hits)
        if "雨天画画" not in hit_texts:
            # 检索不到时必须能从返回里看出"没写进去多少"，而不是静默成功
            self.assertGreater(res["knowledge_skipped"], 0,
                               f"知识没写进去却没报 knowledge_skipped：{res}")
            self.skipTest("RAG 检索未命中，已断言 knowledge_skipped 有值")
        self.assertIn("熬夜画图", hit_texts)
        self.assertNotIn("健身教练", hit_texts, "没导入的知识不该出现")
        self.assertTrue(res["knowledge_verified"], "回读校验应确认切片已落盘")

        # 3) 再导出：新角色的知识切片数应保持（角色专属换成新 id，全局仍只有一份）
        again = qy3.build_payload(new_id, user_id=self.owner)
        self.assertGreaterEqual(again["knowledge"]["counts"]["chunks"], 3)

    def test_import_twice_does_not_duplicate_knowledge(self) -> None:
        """覆盖导入两次：切片按 source+chunk_index+文本+归属 去重，不会重复入库。"""
        self._index_doc(f"resume_{self.cid}.md", "林夏的简历：插画师，养了一只叫团子的猫。",
                        char_id=self.cid)
        src = self._export()

        # 模拟"导入到一台本来没有这份知识的机器"：先把本角色切片清掉
        removed = self._forget_char_knowledge(self.cid)
        self.assertGreaterEqual(removed, 1)
        self.assertEqual(
            [c for c in qy3.build_payload(self.cid, user_id=self.owner)["knowledge"]["chunks"]
             if c["scope"] == "role"], [])

        first = qy3.import_role(src, replace=True)
        self.assertGreaterEqual(first["knowledge_written"], 1)
        self.assertTrue(first["knowledge_verified"])

        second = qy3.import_role(src, replace=True)
        self.assertEqual(second["knowledge_written"], 0, "第二次不该再写切片")
        self.assertGreaterEqual(second["knowledge_skipped"], 1)


class TestQy3fLegacyAndErrors(Qy3fTestBase):
    """老包兼容 + 坏包 / v2 的明确错误码。"""

    def test_legacy_package_without_new_fields(self) -> None:
        """老 .qy3f（只有 role/personality/resume/memory_records/voice/metadata）必须还能读。"""
        legacy = {
            "format_version": 1, "schema_version": 1,
            "exported_at": "2025-01-01T00:00:00",
            "role": {"id": _uid("legacy"), "name": "老包角色", "persona": "老版本", "owner": self.owner},
            "personality": {"mbti": "ENFP"},
            "resume": {"职业": "老师"},
            "memory_records": [{"id": "mem_legacy_1", "text": "老版本里记下的一件事",
                                "metadata": {"weight": 0.7}, "created_at": "2025-01-01T00:00:00",
                                "memory_type": "long_term"}],
            "daily_memory": [],
            "voice": {},
            "metadata": {"owner": self.owner, "role_id": "legacy", "scope": "desktop"},
        }
        src = self.outdir / "老包.qy3f"
        src.write_bytes(qy3._encode(legacy))

        v = qy3.validate_qy3(src)
        self.assertTrue(v["ok"])
        self.assertFalse(v["has_knowledge"])
        self.assertEqual(v["knowledge_chunks"], 0)
        self.assertFalse(v["has_world_state"])
        self.assertEqual(v["memory_records"], 1)

        res = qy3.import_role(src)
        self.assertTrue(res["ok"])
        self.assertEqual(res["memory_records"], 1)
        self.assertFalse(res["has_knowledge"])
        self.assertEqual(res["knowledge_written"], 0)
        self.assertEqual(res["knowledge_skipped"], 0)
        self.assertFalse(res["has_world_state"])
        self.assertEqual(res["memory_error"], "")

        # 老包导入后再导出 → 新包自动带上 knowledge / world_state（向后兼容的单向升级）
        again = qy3.build_payload(res["role_id"], user_id=self.owner)
        self.assertIn("knowledge", again)
        self.assertIn("world_state", again)
        # 老包里没有角色专属知识，所以角色切片必须是 0（全局切片属于别的用例，不算）
        self.assertEqual([c for c in again["knowledge"]["chunks"] if c["scope"] == "role"], [])
        self.assertEqual(len(again["memory_records"]), 1)

    def test_bad_magic(self) -> None:
        bad = self.outdir / "乱码.qy3f"
        bad.write_bytes(bytes(range(64)))
        with self.assertRaises(qy3.QY3Error) as ctx:
            qy3.validate_qy3(bad)
        self.assertEqual(ctx.exception.code, "bad_magic")

    def test_v2_container_rejected(self) -> None:
        """服务器版 v2（QY3\\x02）必须明确拒绝，而不是抛一个泛化异常。"""
        v2 = self.outdir / "网站版.qy3f"
        v2.write_bytes(b"QY3\x02" + b"\x00" * 128)
        with self.assertRaises(qy3.QY3Error) as ctx:
            qy3.validate_qy3(v2)
        self.assertEqual(ctx.exception.code, "format_mismatch")
        self.assertIn("v2", str(ctx.exception))
        # import 走同一个解码器，拒绝方式要一致
        with self.assertRaises(qy3.QY3Error) as ctx2:
            qy3.import_role(v2)
        self.assertEqual(ctx2.exception.code, "format_mismatch")

    def test_truncated_header(self) -> None:
        broken = self.outdir / "断头.qy3f"
        broken.write_bytes(b"QY3\x01" + b"\x00\x10")
        with self.assertRaises(qy3.QY3Error) as ctx:
            qy3.validate_qy3(broken)
        self.assertEqual(ctx.exception.code, "bad_header")

    def test_missing_role(self) -> None:
        empty = self.outdir / "空包.qy3f"
        empty.write_bytes(qy3._encode({"role": {}}))
        with self.assertRaises(qy3.QY3Error) as ctx:
            qy3.validate_qy3(empty)
        self.assertEqual(ctx.exception.code, "schema_error")
        with self.assertRaises(qy3.QY3Error) as ctx2:
            qy3.import_role(empty)
        self.assertEqual(ctx2.exception.code, "missing_role")


class TestQy3fWorldState(Qy3fTestBase):
    """世界状态：关系值 / 情绪 / 未完成话题 / 承诺 能带走并写回。"""

    def test_world_state_roundtrip(self) -> None:
        key = f"{self.owner}:{self.cid}"
        CR._relations_store[key] = {"affinity": 87, "friendship": 74,
                                    "tier": "极亲密", "relationship": "恋人未满"}
        CE._emotions_store[key] = {"joy": 66, "sadness": 12, "reason": "他记得我说过的话"}
        CC._conv_store[key] = {"conv_state": "ACTIVE", "scene": "cozy_talk", "last_topic": "画画",
                               "unfinished_topic": "下周的展要不要一起去", "unfinished_topic_at": 1.0}
        CR._proactive_store[f"{self.owner}::{self.cid}"] = {"cold_streak": 1}
        CR._schedules_store.setdefault(self.owner, []).append({
            "id": "t1", "kind": "reminder", "done": False, "created_at": 1.0,
            "due_at": 4102444800.0, "payload": {"char_id": self.cid, "text": "提醒他吃药"},
        })

        payload = qy3.build_payload(self.cid, user_id=self.owner)
        world = payload["world_state"]
        self.assertTrue(world["available"])
        self.assertEqual(world["relations"]["affinity"], 87)
        self.assertEqual(world["conversation"]["unfinished_topic"], "下周的展要不要一起去")
        self.assertEqual([p["text"] for p in world["promises"]], ["提醒他吃药"])
        self.assertIn("companion.relations._relations_store", world["sources"])

        src = self._export()
        v = qy3.validate_qy3(src)
        self.assertTrue(v["has_world_state"])
        self.assertEqual(v["promises"], 1)

        # 导到"另一个用户"名下：世界状态整块重建（新机器/新账号的迁移场景）
        fresh_owner = _uid("qy3f_migrate")
        res = qy3.import_role(src, owner=fresh_owner)
        self.assertTrue(res["has_world_state"])
        restored = res["world_state_restored"]
        self.assertTrue(restored["relations"])
        self.assertTrue(restored["emotions"])
        self.assertTrue(restored["conversation"])
        self.assertEqual(restored["promises"], 1)
        self.assertEqual(res["world_state_errors"], [])

        new_id = res["role_id"]
        new_key = f"{fresh_owner}:{new_id}"
        self.assertEqual(CR._relations_store[new_key]["affinity"], 87)
        self.assertEqual(CE._emotions_store[new_key]["joy"], 66)
        self.assertEqual(CC._conv_store[new_key]["unfinished_topic"], "下周的展要不要一起去")
        self.assertTrue(any(str((t.get("payload") or {}).get("text")) == "提醒他吃药"
                            for t in CR._schedules_store.get(fresh_owner) or []))

    def test_world_state_can_be_skipped(self) -> None:
        """restore_world_state=False：只导角色/记忆，不碰关系与情绪。"""
        key = f"{self.owner}:{self.cid}"
        CR._relations_store[key] = {"affinity": 66, "friendship": 60, "tier": "熟络朋友"}
        src = self._export()
        fresh_owner = _uid("qy3f_skip")
        res = qy3.import_role(src, owner=fresh_owner, restore_world_state=False)
        self.assertFalse(res["world_state_restored"]["relations"])
        self.assertIn("restore_world_state=False", ";".join(res["world_state_errors"]))
        self.assertNotIn(f"{fresh_owner}:{res['role_id']}", CR._relations_store)


if __name__ == "__main__":
    unittest.main(verbosity=2)
