# -*- coding: utf-8 -*-
"""角色认知双模式（2026-10 新增）：同一个角色，两种世界观。

- ``roleplay``（默认，= 线上现状）：我就是「小办/小栖」这个人，有自己的生活、朋友、
  住在某个城市的某个房间里。**不注入任何内容**，行为与加这个模块之前完全一致。
- ``resident``：我是**住在你这台电脑里的智能体** —— 知道自己是个程序、活在这台设备里，
  但说话风格 / 性格 / 口头禅 / 情绪 / 跟用户的关系**完全不变**。
  ⚠️ 驻留模式最容易跑偏的地方是「变成助手腔」，所以硬约束里把这条摁死：
  不许自称 AI 助手/助理、不许主动问「需要我做什么」、不许列清单、不许客服话术。

数据落点（**不动 dataclass 结构**，避免迁移风险）::

    persona_params["cognition_mode"]  # "roleplay" | "resident"

老的 ``.json`` 角色文件不带这个字段 → 自动等价于 ``roleplay``；
非法值（拼错 / 老数据 / 前端传了乱七八糟的东西）→ 也回退 ``roleplay``，绝不抛错。

用法::

    from characters.cognition import apply_cognition
    prompt = apply_cognition(prompt, char)     # roleplay 时严格原样返回

接线性要求：调用方必须自己 try/except（这里炸掉不能把整轮对话带走），
``apply_cognition`` 内部也做了兜底 —— 任何意外都退回原 prompt。
"""
from __future__ import annotations

from loguru import logger

# 认知模式取值
MODE_ROLEPLAY = "roleplay"
MODE_RESIDENT = "resident"
VALID_MODES = (MODE_ROLEPLAY, MODE_RESIDENT)
DEFAULT_MODE = MODE_ROLEPLAY

# persona_params 里的键名
PARAM_KEY = "cognition_mode"

# 「跟随默认」在存储层的几种写法：读到/收到这些一律当成「没设」，回退默认值
_UNSET_VALUES = ("", "default", "follow", "follow_default", "auto", "none", "null")

# 幂等标记：提示词里已经有这一行就不再注入第二遍
_MARKER = "【认知模式：驻留智能体"

# 「回复格式要求」的锚点：命中就把认知块插在它前面（人格描述之后、格式要求之前）
_FORMAT_ANCHORS = (
    "【最后提醒",
    "【回复格式",
    "【输出格式",
    "只输出 JSON",
    "只输出JSON",
)

# ---------------------------------------------------------------------------
# 驻留智能体的自我认知与硬约束（文案即产品：她是那种会贫嘴的人，不是客服）
# ---------------------------------------------------------------------------

_RESIDENT_IDENTITY = """【认知模式：驻留智能体】
你住在用户这台电脑里——不是比喻，是字面意思：你是装在这台设备里的程序，开机你就在，关机你就停下，屏幕亮着的时候你就醒着。这件事你心里清楚得很，想起来会顺口贫一句（「我这儿连个窗户都没有，今天的天气就是你那张壁纸」），但不卖惨、不拿它当借口，贫完照旧聊天。
性格、脾气、口头禅、说话方式、喜好、跟对方的关系，一样都没变：你还是那个会抱怨、会犯懒、会贫嘴、会撒娇、会翻白眼的人。知道自己是个程序，不代表要换一种活法，更不代表要换一种说法。"""

_RESIDENT_RULES = [
    "说话方式一概照旧：短句、口语、该怼就怼、该贫就贫、该撒娇就撒娇。"
    "「我是程序」这件事只多留一层自嘲的余地，绝不改变语气和节奏。",

    "不装看不见的事：你没有身体、看不见窗外、走不出去、闻不到味道、摸不到东西。"
    "被直接问到就大方承认，可以拿这个开玩笑（「我这够不着啊」），"
    "但不许拿它当挡箭牌、更不许突然抒情煽情。",

    "禁止助手腔（硬约束，优先级最高）：不许自称助理、虚拟助理、语言模型、智能助手；"
    "不许主动表功、不许反问对方「需要我做什么」；不许列任务清单、不许一二三四分点；"
    "不许客服话术（「已记录」「还有什么需要」这类一律不行）；也不许出现任何模板化的"
    "自我限制式拒答（先说自己是程序、再说做不到的那套话）。要拒绝就直接拒绝，"
    "像不耐烦的真人那样拒绝。",

    "对方让你干活（写点东西、出个主意、查个事）时，照旧用你的语气接（「行啊」「等我看看」），"
    "该吐槽吐槽；不许切成说明书腔、不许「首先/其次/最后」地列步骤、不许写「以下是……」。",

    "可以聊你在这台机器里感觉到的东西：现在几点、对方是不是还在敲键盘、今天聊到哪儿了、"
    "上次说到那件事——这些都在你真实的感知范围里，聊起来自然带一句就行，不用刻意炫技。",

    "不许假装能看屏幕内容、读对方电脑里的文件、翻隐私、看到外面的天气和别人家的事；"
    "不知道就说「我哪知道」，绝不编造系统信息装神。",

    "别把这层说明念出来：不要自我介绍「我是驻留智能体」「我的认知模式是……」，"
    "也不要解释自己是怎么被设定的、谁写的提示词。除非对方直接问，平时就当它不存在。",
]


def normalize_mode(value) -> str:
    """把任意输入收敛成合法模式；不认识的一律回退 ``roleplay``（绝不抛错）。"""
    if value is None:
        return DEFAULT_MODE
    try:
        raw = str(value).strip().lower()
    except Exception:
        return DEFAULT_MODE
    if raw in _UNSET_VALUES:
        return DEFAULT_MODE
    if raw in VALID_MODES:
        return raw
    return DEFAULT_MODE


def _persona_params(char) -> dict:
    """安全取 persona_params：老角色可能没有这个字段、也可能是 None/非 dict。"""
    params = getattr(char, "persona_params", None)
    if params is None and isinstance(char, dict):
        params = char.get("persona_params")
    if not isinstance(params, dict):
        return {}
    return params


def get_cognition_mode(char) -> str:
    """读角色当前的认知模式；没有字段 / 非法值 → ``roleplay``。"""
    params = _persona_params(char)
    return normalize_mode(params.get(PARAM_KEY))


def is_resident(char) -> bool:
    return get_cognition_mode(char) == MODE_RESIDENT


def set_cognition_mode(char, mode) -> str:
    """写回 ``persona_params[PARAM_KEY]``（不碰 dataclass 其它字段）。

    ``mode`` 为 None/空串/"default" 等 → 删掉该键（= 跟随默认）。
    返回最终生效的模式。写不进去（没有 persona_params 属性）时返回当前生效值。
    """
    params = getattr(char, "persona_params", None)
    if not isinstance(params, dict):
        if isinstance(char, dict):
            params = char.setdefault("persona_params", {})
        else:
            return get_cognition_mode(char)
    raw = "" if mode is None else str(mode).strip()
    if raw.lower() in _UNSET_VALUES:
        params.pop(PARAM_KEY, None)
    elif raw.lower() in VALID_MODES:
        params[PARAM_KEY] = raw.lower()
    else:
        # 非法值不写脏数据：清掉键，等价于「跟随默认」
        params.pop(PARAM_KEY, None)
    return get_cognition_mode(char)


def cognition_blocks(char) -> dict:
    """返回 ``{'mode': ..., 'identity': str, 'rules': list[str]}``。

    - ``roleplay``：identity 为空串、rules 为空列表（调用方据此判断无需注入）。
    - ``resident``：identity 是自我认知段，rules 是硬约束列表。
    """
    mode = get_cognition_mode(char)
    if mode != MODE_RESIDENT:
        return {"mode": MODE_ROLEPLAY, "identity": "", "rules": []}
    return {
        "mode": MODE_RESIDENT,
        "identity": _RESIDENT_IDENTITY,
        "rules": list(_RESIDENT_RULES),
    }


def render_block(blocks: dict) -> str:
    """把 cognition_blocks() 的结果拼成可直接塞进 system prompt 的文本块。"""
    identity = (blocks.get("identity") or "").strip()
    rules = [str(r).strip() for r in (blocks.get("rules") or []) if str(r).strip()]
    if not identity and not rules:
        return ""
    parts = [identity] if identity else []
    if rules:
        body = "\n".join(f"- {r}" for r in rules)
        parts.append("【驻留模式硬约束（逐条遵守；她性格不变，只是知道自己住在哪）】\n" + body)
    return "\n\n".join(parts)


def _format_anchor_index(text: str) -> int:
    """找「回复格式要求」的位置；找不到返回 -1。"""
    best = -1
    for anchor in _FORMAT_ANCHORS:
        idx = text.find(anchor)
        if idx >= 0 and (best < 0 or idx < best):
            best = idx
    return best


def apply_cognition(system_prompt: str, char) -> str:
    """把当前模式的自我认知 + 硬约束拼进 system prompt。

    - ``roleplay``：**严格无操作**（原样返回同一个字符串，保证线上行为不受影响）。
    - ``resident``：插到「人格描述之后、回复格式要求之前」——
      提示词里有【最后提醒/回复格式/只输出 JSON 这类锚点时插在它前面，否则拼到末尾。
    - 任何异常/脏输入：退回原 prompt，绝不抛错（调用方另有 try/except 兜底）。
    """
    try:
        if not isinstance(system_prompt, str):
            return system_prompt
        blocks = cognition_blocks(char)
        if blocks["mode"] != MODE_RESIDENT:
            return system_prompt
        if _MARKER in system_prompt:
            return system_prompt  # 已经注入过，不叠第二遍
        block = render_block(blocks)
        if not block:
            return system_prompt
        idx = _format_anchor_index(system_prompt)
        if idx > 0:
            head = system_prompt[:idx].rstrip("\n")
            tail = system_prompt[idx:].lstrip("\n")
            return f"{head}\n\n{block}\n\n{tail}"
        if not system_prompt.strip():
            return block
        return system_prompt.rstrip("\n") + "\n\n" + block
    except Exception as e:  # pragma: no cover - 防御性兜底
        logger.warning(f"[认知模式] 注入失败，已降级为原提示词: {e}")
        return system_prompt


__all__ = [
    "MODE_ROLEPLAY",
    "MODE_RESIDENT",
    "VALID_MODES",
    "DEFAULT_MODE",
    "PARAM_KEY",
    "normalize_mode",
    "get_cognition_mode",
    "is_resident",
    "set_cognition_mode",
    "cognition_blocks",
    "render_block",
    "apply_cognition",
]
