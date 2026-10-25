# -*- coding: utf-8 -*-
"""payload schema：版本常量、角色人格原始配置（RoleProfile）、记忆记录视图、校验。

移植来源（逐段对应，勿改数值/文案，改了会让线上包读不回来）：

  `ai-companion/characters/qy3.py:39-57`            格式/版本常量
  `ai-companion/characters/qy3.py:944-978`          QY3Validator.validate_payload
  `ai-companion/characters/persona.py:18-235`       RoleProfile / derive_mbti / 星座
  `ai-companion/memory/records.py:11-121`           MEMORY_TYPES / MemoryRecord / normalize_metadata

`QY3Error` 也放这里：它是全包唯一的异常类型，`code` 字段是集成契约的一部分
（桌面端按 code 分支，不要按 message 文案分支）。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping

# ============ 容器与 schema 版本 ============
FORMAT_VERSION = 1          # v1：桌面开源版 .qy3f（= 老服务器格式）
FORMAT_VERSION_V2 = 2       # v2：服务器版 .qy3
SCHEMA_VERSION = 1
ENGINE_MIN_VERSION = "0.1.0"
COMPRESSION = "zstd"
CIPHER = "AES-256-GCM"
CIPHER_NONE = "none"
PAYLOAD_ENCODING = "msgpack"
VOICE_SCHEMA_VERSION = 1
VOICE_STATE_KEYS = ("main", "quiet", "breath")
PROFILE_VERSION = 1

QY3_SUFFIX = ".qy3"
OPEN_SUFFIX = ".qy3f"


class QY3Error(ValueError):
    """唯一异常类型。`code` 见 docs/FORMAT_SPEC.md「错误码」一节。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ============ 记忆记录（memory/records.py 的等值移植） ============
MEMORY_TYPES = (
    "fact", "preference", "event", "relationship",
    "commitment", "external_reference", "unresolved_reference",
    "self",
    "summary",
)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class MemoryRecord:
    """记忆条目的规范化视图；storage_tier 保留短期/长期实现细节。"""
    id: str
    text: str
    memory_type: str = "event"
    source_message_id: str = ""
    created_at: str = ""
    last_referenced_at: str = ""
    confidence: float = 1.0
    status: str = "active"
    storage_tier: str = "short_term"
    importance: float = 0.5
    content_status: str = ""
    url: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_entry(cls, entry: Any) -> "MemoryRecord":
        meta = dict(getattr(entry, "metadata", None) or {})
        created = str(getattr(entry, "created_at", "") or "")
        if not created:
            created = datetime.now().isoformat()
        inferred = str(meta.get("record_type") or "").strip()
        if inferred not in MEMORY_TYPES:
            inferred = infer_memory_type(str(getattr(entry, "text", "") or ""),
                                         str(getattr(entry, "memory_type", "") or ""))
        last_ref = meta.get("last_referenced_at")
        if last_ref is None:
            last_ref = ""
        content_status = str(meta.get("content_status") or "")
        if inferred == "external_reference" and content_status == "unreadable":
            inferred = "unresolved_reference"
        return cls(
            id=str(getattr(entry, "id", "")),
            text=str(getattr(entry, "text", "") or ""),
            memory_type=inferred,
            source_message_id=str(meta.get("source_message_id") or ""),
            created_at=created,
            last_referenced_at=str(last_ref or ""),
            confidence=max(0.0, min(1.0, _float(meta.get("confidence"), 1.0))),
            status=str(meta.get("status") or "active"),
            storage_tier=str(getattr(entry, "memory_type", "short_term")),
            importance=max(0.0, min(1.0, _float(meta.get("weight"), 0.5))),
            content_status=content_status,
            url=str(meta.get("url") or ""),
            metadata=meta,
        )


def infer_memory_type(text: str, storage_tier: str = "short_term") -> str:
    t = (text or "").strip()
    if not t:
        return "event"
    if any(x in t for x in ("链接", "http://", "https://", "小红书", "网址")):
        return "external_reference"
    if any(x in t for x in ("答应", "承诺", "以后", "到时候", "记得提醒")):
        return "commitment"
    if any(x in t for x in ("喜欢", "讨厌", "偏好", "最爱", "不爱")):
        return "preference"
    if any(x in t for x in ("女朋友", "男朋友", "朋友", "同事", "家人", "关系")):
        return "relationship"
    if storage_tier == "summary":
        return "summary"
    if storage_tier in ("long_term", "user_profile"):
        return "fact"
    return "event"


def normalize_metadata(metadata: Mapping[str, Any] | None,
                       source_message_id: str = "",
                       memory_type: str = "",
                       confidence: float = 1.0,
                       status: str = "active") -> dict:
    meta = dict(metadata or {})
    if source_message_id:
        meta["source_message_id"] = source_message_id
    if memory_type:
        meta["record_type"] = memory_type
    meta.setdefault("confidence", max(0.0, min(1.0, float(confidence))))
    meta.setdefault("status", status)
    meta.setdefault("created_at", datetime.now().isoformat())
    meta.setdefault("last_referenced_at", "")
    return meta


# ============ 角色人格原始配置（characters/persona.py 的等值移植） ============
MBTI_DIMENSIONS = ("energy", "mind", "nature", "tactics", "identity")
MBTI_NAMES = {
    "INTJ": "建筑师", "INTP": "逻辑学家", "ENTJ": "指挥官", "ENTP": "辩论家",
    "INFJ": "提倡者", "INFP": "调停者", "ENFJ": "主人公", "ENFP": "竞选者",
    "ISTJ": "物流师", "ISFJ": "守卫者", "ESTJ": "总经理", "ESFJ": "执政官",
    "ISTP": "鉴赏家", "ISFP": "探险家", "ESTP": "企业家", "ESFP": "表演者",
}

ZODIAC_PROFILES: dict[str, dict[str, Any]] = {
    "白羊座": {"start": (3, 21), "end": (4, 19), "positive": "直接、主动、行动快", "shadow": "容易急躁"},
    "金牛座": {"start": (4, 20), "end": (5, 20), "positive": "稳定、耐心、重舒适", "shadow": "可能固执"},
    "双子座": {"start": (5, 21), "end": (6, 20), "positive": "好奇、灵活、爱交流", "shadow": "注意力容易跳跃"},
    "巨蟹座": {"start": (6, 21), "end": (7, 22), "positive": "重关系、敏感、有保护欲", "shadow": "容易受情绪影响"},
    "狮子座": {"start": (7, 23), "end": (8, 22), "positive": "自信、热情、愿意表达", "shadow": "在意认可"},
    "处女座": {"start": (8, 23), "end": (9, 22), "positive": "细致、务实、重秩序", "shadow": "容易挑剔"},
    "天秤座": {"start": (9, 23), "end": (10, 22), "positive": "重平衡、善于协调", "shadow": "可能犹豫"},
    "天蝎座": {"start": (10, 23), "end": (11, 21), "positive": "专注、深沉、重边界", "shadow": "容易多疑"},
    "射手座": {"start": (11, 22), "end": (12, 21), "positive": "乐观、直接、喜欢新鲜感", "shadow": "可能不够细致"},
    "摩羯座": {"start": (12, 22), "end": (12, 31), "positive": "务实、克制、重长期目标", "shadow": "容易绷得太紧"},
    "水瓶座": {"start": (1, 20), "end": (2, 18), "positive": "独立、跳脱、重视自己的节奏", "shadow": "可能显得疏离"},
    "双鱼座": {"start": (2, 19), "end": (3, 20), "positive": "共情、想象丰富、感受细腻", "shadow": "容易被情绪带走"},
}


def _clamp(value: Any, default: int = 50) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def _first(mapping: Mapping[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return default


def calculate_zodiac(birthday: str) -> str:
    """按传统占星日期区间计算星座；非法生日返回空字符串。"""
    try:
        born = datetime.strptime(str(birthday or ""), "%Y-%m-%d").date()
    except ValueError:
        return ""
    month, day = born.month, born.day
    for sign, profile in ZODIAC_PROFILES.items():
        start_m, start_d = profile["start"]
        end_m, end_d = profile["end"]
        if start_m <= end_m:
            if (start_m, start_d) <= (month, day) <= (end_m, end_d):
                return sign
        elif (month, day) >= (start_m, start_d) or (month, day) <= (end_m, end_d):
            return sign
    return ""


@dataclass(frozen=True)
class RoleProfile:
    """角色工坊保存的连续人格与回复偏好原始配置。"""
    energy: int = 50
    mind: int = 50
    nature: int = 50
    tactics: int = 50
    identity: int = 50
    emotion_threshold: int = 50
    rebuttal_threshold: int = 50
    agency: int = 50
    initiative: int = 50
    question_rate: int = 50
    topic_agency: int = 50
    topic_persistence: int = 50
    initiative_rate: int = 50
    filler_usage: int = 0
    preferred_length: int = 18
    preferred_message_count: int = 1
    reply_brevity: int = 50
    message_split: int = 50
    self_disclosure: int = 50
    emoji_style: str = "偶尔"
    punctuation_style: str = "自然"
    filler_vocabulary: tuple[str, ...] = ()
    forbidden_phrasings: tuple[str, ...] = ()
    response_preferences: str = ""
    affinity: int = 50
    friendship: int = 50
    sensible: int = 50
    clingy: int = 50
    discipline: int = 50
    warmth: int = 50
    speech_habits: tuple[str, ...] = ()
    relationship: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "RoleProfile":
        data = dict(raw or {})
        legacy_mbti = str(_first(data, ("mbti", "mbti_code"), "") or "").upper()
        legacy_variant = str(_first(data, ("mbti_variant", "variant"), "") or "").upper()
        if legacy_variant not in ("A", "T") and legacy_mbti.endswith(("-A", "-T")):
            legacy_variant = legacy_mbti[-1]
            legacy_mbti = legacy_mbti[:-2]
        core = legacy_mbti[:4]
        legacy_map = {
            "energy": (85 if core[:1] == "E" else 15) if core else 50,
            "mind": (85 if core[1:2] == "N" else 15) if len(core) > 1 else 50,
            "nature": (85 if core[2:3] == "F" else 15) if len(core) > 2 else 50,
            "tactics": (85 if core[3:4] == "P" else 15) if len(core) > 3 else 50,
            "identity": (80 if legacy_variant == "A" else 20) if legacy_variant in ("A", "T") else 50,
        }
        dimensions = {k: _clamp(_first(data, (k,), legacy_map[k])) for k in MBTI_DIMENSIONS}
        strings = lambda key, default=(): tuple(
            str(x).strip() for x in (data.get(key) or default)
            if str(x).strip()
        ) if isinstance(data.get(key) or default, (list, tuple)) else (
            tuple(x.strip() for x in str(data.get(key) or "").split("、") if x.strip())
            if key in data else tuple(default)
        )
        return cls(
            **dimensions,
            emotion_threshold=_clamp(_first(data, ("emotion_threshold", "patience"), 50)),
            rebuttal_threshold=_clamp(_first(data, ("rebuttal_threshold", "rebut"), 50)),
            agency=_clamp(_first(data, ("agency", "assertiveness", "assert"), 50)),
            initiative=_clamp(_first(data, ("initiative", "assertiveness", "assert"), 50)),
            question_rate=_clamp(_first(data, ("question_rate",), 50)),
            topic_agency=_clamp(_first(data, ("topic_agency",), 50)),
            topic_persistence=_clamp(_first(data, ("topic_persistence",), 50)),
            initiative_rate=_clamp(_first(data, ("initiative_rate", "initiative"), 50)),
            filler_usage=_clamp(_first(data, ("filler_usage",), 0)),
            preferred_length=max(8, min(80, _clamp(_first(data, ("preferred_length",), 18), 18))),
            preferred_message_count=max(1, min(5, _clamp(_first(data, ("preferred_message_count",), 1), 1))),
            reply_brevity=_clamp(_first(data, ("reply_brevity",), 50)),
            message_split=_clamp(_first(data, ("message_split",), 50)),
            self_disclosure=_clamp(_first(data, ("self_disclosure",), 50)),
            affinity=_clamp(_first(data, ("affinity",), 50)),
            friendship=_clamp(_first(data, ("friendship",), 50)),
            sensible=_clamp(_first(data, ("sensible",), 50)),
            clingy=_clamp(_first(data, ("clingy",), 50)),
            discipline=_clamp(_first(data, ("discipline",), 50)),
            warmth=_clamp(_first(data, ("warmth",), 50)),
            emoji_style=str(data.get("emoji_style") or "偶尔")[:12],
            punctuation_style=str(data.get("punctuation_style") or "自然")[:12],
            filler_vocabulary=strings("filler_vocabulary"),
            forbidden_phrasings=strings("forbidden_phrasings"),
            response_preferences=str(data.get("response_preferences") or "")[:500],
            speech_habits=strings("speech_habits"),
            relationship=str(data.get("relationship") or "")[:80],
        )

    @classmethod
    def from_character(cls, char: Any) -> "RoleProfile":
        return cls.from_mapping(getattr(char, "persona_params", None) or {})

    def to_dict(self) -> dict:
        data = asdict(self)
        data["filler_vocabulary"] = list(self.filler_vocabulary)
        data["forbidden_phrasings"] = list(self.forbidden_phrasings)
        data["speech_habits"] = list(self.speech_habits)
        data.update(derive_mbti(self))
        data["patience"] = self.emotion_threshold
        data["rebut"] = self.rebuttal_threshold
        data["assertiveness"] = self.agency
        data["assert"] = self.agency
        data["initiative"] = self.initiative
        return data


def derive_mbti(profile: RoleProfile) -> dict[str, str]:
    """连续值只负责行为；标签只在不存在中性轴时生成。"""
    neutral = [name for name in MBTI_DIMENSIONS if getattr(profile, name) == 50]
    if any(name in neutral for name in ("energy", "mind", "nature", "tactics")):
        return {"mbti_code": "", "mbti_variant": "", "mbti_name": "中性混合"}
    code = "".join((
        "E" if profile.energy > 50 else "I",
        "N" if profile.mind > 50 else "S",
        "F" if profile.nature > 50 else "T",
        "P" if profile.tactics > 50 else "J",
    ))
    variant = "" if profile.identity == 50 else ("A" if profile.identity > 50 else "T")
    role = MBTI_NAMES.get(code, code)
    if variant:
        vname = "果断（Assertive）" if variant == "A" else "敏锐（Turbulent）"
        name = f"{role}·{variant}（{vname}）"
    else:
        name = f"{role}·Identity中性"
    return {"mbti_code": code, "mbti_variant": variant, "mbti_name": name}


def preference_strength(value: int) -> int:
    return abs(_clamp(value) - 50) * 2


def _strength_word(strength: int) -> str:
    if strength < 10:
        return "非常轻微地"
    if strength < 30:
        return "很轻微地"
    if strength < 55:
        return "轻度地"
    if strength < 80:
        return "明显地"
    return "强烈地"


# ============ payload 校验（对应 qy3.py:QY3Validator.validate_payload） ============
def validate_payload(payload: dict, header: dict) -> None:
    if int(header.get("schema_version") or 0) > SCHEMA_VERSION:
        raise QY3Error("version_too_new", "schema 版本过高，当前客户端无法导入")
    if not isinstance(payload, dict):
        raise QY3Error("schema_error", "schema 错误：payload 根节点必须是对象")
    role = payload.get("role")
    if not isinstance(role, dict):
        raise QY3Error("schema_error", "schema 错误：缺少 role")
    for fld in ("id", "name", "persona_params", "virtual_schedule"):
        if fld not in role:
            raise QY3Error("missing_field", f"内容缺失：role.{fld}")
    for fld in ("memory_records", "rag_documents", "external_references"):
        if fld in payload and not isinstance(payload[fld], list):
            raise QY3Error("schema_error", f"schema 错误：{fld} 必须是数组")
    if "self_notes" in payload and not isinstance(payload["self_notes"], str):
        raise QY3Error("schema_error", "schema 错误：self_notes 必须是字符串")


def _n(value: Any) -> int:
    return len(value) if isinstance(value, (list, dict, str)) else 0


def canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_counts(payload: dict) -> dict:
    """payload 的各项计数（validate 输出用；只统计，不解密内容）。"""
    role = payload.get("role") or {}
    params = payload.get("personality") or role.get("persona_params") or {}
    recs = payload.get("memory_records") or []
    daily = payload.get("daily_memory") or []
    by_day: dict[str, int] = {}
    for ev in daily:
        if isinstance(ev, dict):
            day = str(ev.get("date") or "")
            by_day[day] = by_day.get(day, 0) + 1
    voice = (payload.get("voice") or {}).get("states") or {}
    cr = payload.get("continual_rag") or {}
    bucket = cr.get("bucket") or {}
    tune = payload.get("self_tune") or {}
    return {
        "role_id": role.get("id"),
        "role_name": role.get("name"),
        "persona_params": _n(params),
        "persona_params_sha": _sha(canon(params))[:16],
        "memory_records": len(recs),
        "memory_ids_sha": _sha(canon(sorted(str(r.get("id") or "") for r in recs
                                            if isinstance(r, dict))))[:16],
        "daily_memory_days": len([d for d in by_day if d]),
        "daily_memory_events": len(daily),
        "daily_memory_by_day": dict(sorted(by_day.items())),
        "voice_states": len(voice),
        "voice_keys": sorted(str(k) for k in voice),
        "continual_rag": dict({str(k): _n(v) for k, v in sorted(bucket.items())},
                              buffer=_n(cr.get("buffer") or [])),
        "rag_documents": _n(payload.get("rag_documents") or []),
        "chunks": _n(payload.get("chunks") or []),
        "self_notes_len": len(str(payload.get("self_notes") or "")),
        "self_tune_log": _n(tune.get("log") or []),
        "commitments": _n(payload.get("commitments") or []),
        "events": _n(payload.get("events") or []),
        "world_state_keys": sorted((payload.get("world_state") or {}).keys()),
    }


__all__ = [
    "FORMAT_VERSION", "FORMAT_VERSION_V2", "SCHEMA_VERSION", "ENGINE_MIN_VERSION",
    "COMPRESSION", "CIPHER", "CIPHER_NONE", "PAYLOAD_ENCODING",
    "VOICE_SCHEMA_VERSION", "VOICE_STATE_KEYS", "PROFILE_VERSION",
    "QY3_SUFFIX", "OPEN_SUFFIX", "QY3Error",
    "MEMORY_TYPES", "MemoryRecord", "infer_memory_type", "normalize_metadata",
    "MBTI_DIMENSIONS", "MBTI_NAMES", "ZODIAC_PROFILES", "RoleProfile",
    "derive_mbti", "calculate_zodiac", "preference_strength",
    "validate_payload", "payload_counts",
]
