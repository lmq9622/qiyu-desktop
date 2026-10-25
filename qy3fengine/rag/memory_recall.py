# -*- coding: utf-8 -*-
# ============================================================================
# 本文件从 ai-companion 现网代码**逐字移植**（只改 import 与日志库）。
# 出处见 docs/EDITION_MATRIX.md，改动前请对照原文件行号，
# 算法/阈值/话术任何一处变动都会让新引擎与线上行为不一致。
# ============================================================================
#   来源: companion/memory_recall.py:1-315（整文件）
#   改动: `from memory.records import MemoryRecord` → `from ..schema import ...`
#   源文件行数: 316
# ============================================================================
"""记忆召回策略：只让当前语义真正需要的记忆进入运行时上下文。"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Iterable

from ..schema import MemoryRecord


RECALL_MARKERS = (
    "还记得", "记不记得", "记得吗", "之前", "上次", "以前", "你说过",
    "我告诉过你", "那个链接", "前几天", "上周", "昨天提到",
)
PRIVATE_TYPES = {"relationship", "external_reference", "unresolved_reference", "commitment"}


def is_explicit_recall(query: str) -> bool:
    t = (query or "").lower()
    return any(marker in t for marker in RECALL_MARKERS) or "http" in t


def _tokens(text: str) -> set[str]:
    """中文实词滑窗。2~4 字是中文实词的自然长度；单字噪声大（"一"、"的"到处都是），不取。"""
    t = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", (text or "").lower())
    if not t or len(t) < 2:
        return set()
    out = set()
    for n in (2, 3, 4):
        for i in range(len(t) - n + 1):
            out.add(t[i:i + n])
    return out


# 这些词几乎每句都有，拿来当「关键词命中」会把不相关的旧事也召回来。
# "天气/穿搭"也停：它们是话题标签不是记忆内容 —— 只有 query 里的词才能落进正文。
_KW_STOP = frozenset({
    "今天", "昨天", "明天", "刚才", "现在", "早上", "晚上", "中午", "下午",
    "什么", "怎么", "怎么样", "为什么", "这个", "那个", "我们", "你们", "他们",
    "一下", "知道", "觉得", "可以", "没有", "不是", "就是", "然后", "因为",
    "所以", "但是", "如果", "还是", "已经", "自己", "一个", "一起", "时候",
    "你好", "在吗", "嗯嗯", "哈哈",
})


def _keyword_hit(query: str, text: str) -> float:
    """query 里的实词如果整段出现在记忆正文里，就算召回。

    只查 query 自己的词 —— 把 topic 也拼进去会让 topic 撞上它自己（query=「天气」
    topic=「天气」= 满分），旧实现就是这么把任何话题都判成「正好提过」的。
    """
    raw = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", (query or "").lower())
    body = (text or "").lower()
    if len(raw) < 2 or not body:
        return 0.0
    best = 0
    for n in (4, 3, 2):
        for i in range(0, len(raw) - n + 1):
            w = raw[i:i + n]
            if w in _KW_STOP or w.isdigit():
                continue
            if not any("\u4e00" <= ch <= "\u9fff" or ch.isalpha() for ch in w):
                continue
            if w in body:
                best = max(best, n)
                break
        if best >= 4:
            break
    if best < 2:
        return 0.0
    return min(0.72, 0.22 + 0.14 * best)


def semantic_relevance(query: str, text: str, topic: str = "") -> float:
    q, r, topic_set = _tokens(query), _tokens(text), _tokens(topic)
    direct = 0.0
    if q and r:
        direct = len(q & r) / max(1, min(len(q), len(r)))
    topic_score = 0.0
    if topic_set and r:
        topic_score = len(topic_set & r) / max(1, min(len(topic_set), len(r)))
    keyword = _keyword_hit(query, text or "")
    return max(direct, topic_score * 0.85, keyword)


def _recent_short_ok(record: MemoryRecord, query: str, topic: str) -> bool:
    """短期记忆单独放宽：这几天的、话题真碰到了（query 和 topic 各算一遍），就用得上。
    只用 raw relevance 会被 topic 串场 —— topic 是「正在聊什么」，常和 query 撞词。"""
    if str(getattr(record, "storage_tier", "") or "") != "short_term":
        return False
    age = _age_seconds(record.created_at or record.metadata.get("last_seen"), time.time())
    if age > 3 * 86400:
        return False
    return max(_keyword_hit(query, record.text),
               _keyword_hit(topic, record.text)) >= 0.20


def _age_seconds(value: str | float | None, now: float) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return max(0.0, now - float(value))
    try:
        ts = datetime.fromisoformat(str(value)[:19]).timestamp()
        return max(0.0, now - ts)
    except Exception:
        return 0.0


def score_memory(record: MemoryRecord, query: str, topic: str = "",
                 affinity: int = 50, explicit_recall: bool = False,
                 recent_topic: bool = False, now: float | None = None) -> dict:
    now = now or time.time()
    relevance = semantic_relevance(query, record.text, topic)
    age = _age_seconds(record.created_at or record.metadata.get("last_seen"), now)
    recency = math.exp(-age / (14 * 86400))
    last_ref_age = _age_seconds(record.last_referenced_at or record.metadata.get("last_referenced_at"), now)
    last_ref_factor = 1.0 if not last_ref_age else math.exp(-last_ref_age / (7 * 86400))
    relationship_relevance = 1.0
    if record.memory_type in PRIVATE_TYPES and affinity < 60:
        relationship_relevance = 0.25
    unfinished = record.memory_type == "commitment" and record.status == "pending"
    importance = record.importance or float(record.metadata.get("weight", 0.5) or 0.5)
    tier = str(getattr(record, "storage_tier", "") or "")
    age_hours = age / 3600.0
    # 这几天还热着的短期记忆，比隔了很久的长期事实更容易被顺口提起。
    short_boost = 0.08 if tier == "short_term" and age_hours <= 72 else 0.0
    score = (
        relevance * 0.55
        + recency * 0.15
        + importance * 0.15
        + relationship_relevance * 0.10
        + (0.05 if unfinished else 0.0)
        + short_boost
    ) * last_ref_factor
    if explicit_recall and relevance >= 0.08:
        score = max(score, 0.38)
    if recent_topic and relevance >= 0.20:
        score = max(score, 0.34)
    if record.status in ("cancelled", "expired"):
        score = 0.0
    if record.memory_type == "unresolved_reference" and not explicit_recall and relevance < 0.30:
        score = 0.0
    if record.memory_type == "external_reference" and record.content_status == "unreadable" and not explicit_recall:
        score *= 0.2
    return {
        "semantic_relevance": round(relevance, 4),
        "recency": round(recency, 4),
        "importance": round(importance, 4),
        "relationship_relevance": round(relationship_relevance, 4),
        "unfinished_task_state": unfinished,
        "last_referenced_at": record.last_referenced_at,
        "score": round(max(0.0, min(1.0, score)), 4),
        "storage_tier": tier,
        "age_hours": round(age_hours, 2),
        "kw_hit": _keyword_hit(query, record.text),
    }


@dataclass(frozen=True)
class MemoryGateDecision:
    """区分“有这条记忆”“当前可用”“允许主动提起”三个不同层次。"""
    exists: bool
    usable: bool
    should_mention: bool
    reason: str

    def to_dict(self) -> dict:
        return {
            "exists": self.exists,
            "usable": self.usable,
            "should_mention": self.should_mention,
            "reason": self.reason,
        }


def memory_gate(record: MemoryRecord, query: str = "", topic: str = "",
                explicit_recall: bool = False, recent_topic: bool = False,
                unfinished_commitment: bool = False,
                behavior_needs_memory: bool = False) -> MemoryGateDecision:
    """Memory Gate：旧记忆只有当前确实需要时才可用，主动提起还要更严格。"""
    scores = score_memory(
        record, query, topic, explicit_recall=explicit_recall,
        recent_topic=recent_topic,
    )
    relevance = float(scores.get("semantic_relevance") or 0.0)
    score = float(scores.get("score") or 0.0)
    if record.status in ("cancelled", "expired"):
        return MemoryGateDecision(True, False, False, "inactive_record")
    if record.memory_type in ("external_reference", "unresolved_reference") and not explicit_recall:
        return MemoryGateDecision(True, False, False, "external_reference_requires_explicit_recall")
    # 短期放宽不能越过「承诺 / 显式问」的严格分支，先看那两条。
    if unfinished_commitment or scores.get("unfinished_task_state"):
        return MemoryGateDecision(True, True, True, "unfinished_commitment")
    if explicit_recall and relevance >= 0.08:
        return MemoryGateDecision(True, True, True, "explicit_user_recall")
    if recent_topic and relevance >= 0.20:
        return MemoryGateDecision(True, True, relevance >= 0.30, "current_topic_continuation")
    if behavior_needs_memory and relevance >= 0.25:
        return MemoryGateDecision(True, True, relevance >= 0.34, "behavior_requires_context")
    tier = str(getattr(record, "storage_tier", "") or "")
    # 「话题碰到了」必须真的有实词落在记忆正文：光靠综合分会被「今天/什么」这类词顶起来。
    kw = _keyword_hit(query, record.text)
    if _recent_short_ok(record, query, topic):
        return MemoryGateDecision(True, True, relevance >= 0.20, "recent_short_term")
    if relevance >= 0.18 and score >= 0.20 and kw >= 0.30:
        return MemoryGateDecision(True, True, relevance >= 0.28, "keyword_overlap")
    return MemoryGateDecision(True, False, False, "no_keyword_overlap")

def should_recall(scores: dict, explicit_recall: bool, recent_topic: bool) -> bool:
    return bool(recall_reason(scores, explicit_recall, recent_topic))


def recall_reason(scores: dict, explicit_recall: bool, recent_topic: bool) -> str:
    """返回本次召回的唯一主因；空字符串表示不应进入上下文。"""
    if scores["score"] <= 0:
        return ""
    if scores["unfinished_task_state"]:
        return "unfinished_commitment_due_or_relevant"
    if explicit_recall and scores["semantic_relevance"] >= 0.08:
        return "explicit_user_recall"
    if recent_topic and scores["semantic_relevance"] >= 0.20:
        return "current_topic_continuation"
    tier = str(scores.get("storage_tier") or "")
    age_hours = float(scores.get("age_hours") or 9999)
    # 短期在这几天的"新鲜窗口"里才算数；过了三天就走长期那条判定。
    if tier == "short_term" and age_hours <= 72 and scores["semantic_relevance"] >= 0.20 \
            and scores.get("short_ok"):
        return "recent_short_term"
    # 「关键词撞上了」不能只看综合分：query 里的高频词会把它顶到 0.18+。
    # 这条路必须真有一个实词落进记忆正文（_kw_hit），否则归到 high_semantic / 不召回。
    if scores["semantic_relevance"] >= 0.18 and scores["score"] >= 0.20 and (scores.get("kw_hit") or 0) >= 0.30:
        return "keyword_overlap"
    # 「光语义相关」但 query 里没一个实词落进正文 —— 这是闲聊里误召回最多的一条，
    # 只能当"这条记忆存在"，不能当"这轮该提"。
    if scores["semantic_relevance"] >= 0.34 and scores["score"] >= 0.30 and (scores.get("kw_hit") or 0) >= 0.30:
        return "high_semantic_relevance"
    return ""


def select_relevant_memories(manager: Any, user_id: str, char_id: str,
                             query: str, topic: str = "", affinity: int = 50,
                             recent_topic: bool = False, limit: int = 4,
                             behavior_needs_memory: bool = False,
                             mark_referenced: bool = True) -> list[dict]:
    if not user_id:
        return []
    store = manager._get_store(user_id, char_id)
    explicit = is_explicit_recall(query)
    now = time.time()
    candidates: list[tuple[dict, MemoryRecord, Any, MemoryGateDecision]] = []
    for entry in list(store.entries):
        if entry.memory_type in ("knowledge", "user_profile"):
            continue
        record = MemoryRecord.from_entry(entry)
        scores = score_memory(record, query, topic, affinity, explicit, recent_topic, now)
        scores["short_ok"] = _recent_short_ok(record, query, topic)
        gate = memory_gate(record, query, topic, explicit, recent_topic, behavior_needs_memory=behavior_needs_memory)
        if (should_recall(scores, explicit, recent_topic) or (behavior_needs_memory and gate.usable)) and gate.usable:
            candidates.append((scores, record, entry, gate))
    candidates.sort(key=lambda item: item[0]["score"], reverse=True)
    selected = candidates[:max(1, limit)]
    if mark_referenced and selected:
        stamp = datetime.now().isoformat(timespec="seconds")
        changed = False
        selected_ids = {record.id for _, record, _, _ in selected}
        for entry in store.entries:
            if entry.id in selected_ids:
                meta = dict(entry.metadata or {})
                meta["last_referenced_at"] = stamp
                entry.metadata = meta
                changed = True
        if changed:
            store._save()
    return [
        {"record": record.to_dict(), "scores": scores, "memory_gate": gate.to_dict()}
        for scores, record, _, gate in selected
    ]


def format_relevant_memories(items: Iterable[dict], limit: int = 4) -> str:
    lines = []
    for item in list(items)[:limit]:
        record = item.get("record") or {}
        text = str(record.get("text") or "").strip()
        if not text:
            continue
        kind = str(record.get("memory_type") or "event")
        tier = str(record.get("storage_tier") or "")
        tier_label = {"short_term": "短期", "long_term": "长期", "summary": "摘要"}.get(tier, "")
        tag = f"{tier_label}/{kind}" if tier_label else kind
        confidence = float(record.get("confidence") or 1.0)
        uncertainty = "（这条不完全确定）" if confidence < 0.7 else ""
        if record.get("content_status") == "unreadable":
            lines.append(f"- [未读取成功]{text}{uncertainty}")
        else:
            lines.append(f"- [{tag}]{text}{uncertainty}")
    if not lines:
        return ""
    return (
        "【记得的事】短期是这几天还热着的，长期是更早的。"
        "对方这句话碰到了才自然提起，别为了证明自己记得而翻出来；"
        "手上没有的事就说没印象，不许编。\n" + "\n".join(lines)
    )


__all__ = [
    "MemoryGateDecision", "memory_gate", "is_explicit_recall", "semantic_relevance", "score_memory",
    "should_recall", "recall_reason", "select_relevant_memories",
    "format_relevant_memories",
]
