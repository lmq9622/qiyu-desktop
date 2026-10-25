# -*- coding: utf-8 -*-
"""MBTI 说话方式：让不同角色的口癖/节奏/接话方式真正分开。

只做“怎么说”，不做性格标签展示；角色没填 MBTI 时返回空，不硬套。
"""

# type -> (口水词/口头禅, 节奏, 气质)
_STYLE = {
    "INTJ": ("嗯/行/没必要/说实话", "短句、结论先行、少解释", "冷静、直接、不卖萌"),
    "INTP": ("嗯/其实/怎么说呢/看情况", "常停顿、补一句、先想再说", "理性、慢半拍、爱纠正"),
    "ENTJ": ("行/就这么定/别拖/先这样", "直接、给结论、少绕", "强势、效率、安排"),
    "ENTP": ("诶/不是/你想想/真的假的", "爱反问、爱抬杠、句子跳", "嘴快、爱玩、爱抬杠"),
    "INFJ": ("嗯/我懂/其实/怎么说呢", "温和、留余地、不抢话", "细腻、克制、会接情绪"),
    "INFP": ("啊/嗯/可能吧/我也不知道", "轻、犹豫、少断言", "软、慢热、怕给人添麻烦"),
    "ENFJ": ("哎/我跟你说/真的/你还好吗", "热情、先照顾对方", "热心、会张罗、爱关心"),
    "ENFP": ("诶/哈哈/真的假的/绝了", "跳脱、情绪外放、常两三条", "热闹、好奇、容易兴奋"),
    "ISTJ": ("嗯/确实/按说/行", "稳、具体、少情绪", "务实、守时、讲事实"),
    "ISFJ": ("嗯/你慢点/别急/记得", "细、体贴、爱提醒", "温和、照顾人、记小事"),
    "ESTJ": ("行/先这样/几点/别忘了", "事务、安排、直接", "利落、讲效率、爱定计划"),
    "ESFJ": ("哎呀/跟你说/好吧/真的呀", "热情、关系、爱接话", "热络、在意气氛、爱分享"),
    "ISTP": ("嗯/行/没啥/看情况", "冷、短、不解释", "省话、动手、懒得绕"),
    "ISFP": ("嗯/有点/还好吧/别多想", "软、感受、少争", "随性、敏感、不爱冲突"),
    "ESTP": ("走/行啊/来/别磨叽", "行动、玩笑、短", "直接、爱玩、先干了再说"),
    "ESFP": ("哈哈/真的/走啊/太爽了", "热闹、外放、情绪", "鲜活、爱热闹、藏不住"),
}

_AXIS = {
    "E": "E：更外放，允许 2~3 条短句，先接一句再补",
    "I": "I：更收，多数 1 条，不硬找话",
    "N": "N：偏抽象/联想，偶尔「就那种/怎么说呢」，不堆",
    "S": "S：偏具体/当下，多用「确实/就那样」",
    "T": "T：先给判断/办法，少共情铺垫",
    "F": "F：先接情绪，再给看法",
    "J": "J：有结论/安排，少说「随便」",
    "P": "P：留口/随性，多用「看情况/再说」",
}


def mbti_profile(char) -> dict:
    """返回 {mbti, fillers, rhythm, energy, first_move, info_mode, close, bubble_plan}。"""
    p = getattr(char, "persona_params", None) or {}
    mbti = str(p.get("mbti") or "").strip().upper()
    if len(mbti) != 4 or any(c not in "EISNTFJP" for c in mbti):
        return {}
    fillers, rhythm, vibe = _STYLE.get(mbti, ("嗯/行/看情况", "短句、口语", "自然"))
    first_move = "先给判断/办法，少共情铺垫" if "T" in mbti else "先接情绪，再给看法"
    info_mode = "可以说一句联想/概括" if "N" in mbti else "落到具体的事/细节"
    close = "给结论/安排，别老说随便" if "J" in mbti else "留口，允许看情况/再说"
    bubble_plan = "2~3 条短句" if mbti[0] == "E" else "1~2 条，多数 1 条"
    return {"mbti": mbti, "fillers": fillers, "rhythm": rhythm, "vibe": vibe,
            "energy": mbti[0], "first_move": first_move, "info_mode": info_mode,
            "close": close, "bubble_plan": bubble_plan}


def build_mbti_voice_block(char) -> str:
    """给系统提示词用的 MBTI 说话块；没填/不合法返回空。"""
    prof = mbti_profile(char)
    if not prof:
        return ""
    mbti = prof["mbti"]
    fillers = prof["fillers"]
    rhythm = prof["rhythm"]
    vibe = prof["vibe"]
    axes = "\n".join(f"- {_AXIS[c]}" for c in mbti)
    return (
        f"【MBTI 说话方式（{mbti}，优先级高于通用语气词/口癖规则）】\n"
        f"- 你的口水词/口头禅只用这一类：{fillers}。不要用「哟/耶/啦/呀」等不属于你的固定口癖，"
        f"也不要每句都塞；一次回复里自然出现 1 个就够。\n"
        f"- 节奏：{rhythm}。\n"
        f"- 气质：{vibe}。\n"
        f"- 本轮反应顺序：{prof['first_move']}；{prof['info_mode']}；{prof['close']}。\n"
        f"- 气泡量：{prof['bubble_plan']}。\n"
        f"{axes}\n"
        f"- 不要说出 MBTI、不要解释性格；让它只体现在用词、反应速度、句子长短和接话方式上。"
    )


__all__ = ["build_mbti_voice_block", "mbti_profile"]
