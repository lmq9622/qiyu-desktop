# -*- coding: utf-8 -*-
"""主动追问的小闸门（移植自服务器版 companion/active_guards.py 的实质判定）。"""
import re

# 追问里的"空话"：去掉这些填充词后还得剩点实在内容，才算带上了那件没说完的事
_NUDGE_FILLER_RE = re.compile(
    r"行吧|好吧|算了|人呢|在吗|在不在|还在吗|在不|喂+|咋不理我了?|怎么不理我了?|不理我了?|理我|"
    r"睡了吗|睡着了吗?|忙吗|忙啥呢|干嘛呢|去哪了|[行好嗯哦啊呢吧呀啦哈诶欸]")
_NUDGE_PUNCT_RE = re.compile(r"[\s　？?。！!~～，,、…·.\-_*]+")


def _nudge_has_substance(text: str) -> bool:
    """追问要带上那件没说完的事：去掉空格、标点和『人呢/在吗/行吧』这类空话后还得剩点实在内容。

    真人不会隔一小时只发「行吧」「人呢」「？」这种空话；追问没实质内容时宁可不发。
    """
    rest = _NUDGE_FILLER_RE.sub("", _NUDGE_PUNCT_RE.sub("", str(text or "")))
    return len(rest) >= 2


__all__ = ["_nudge_has_substance"]
