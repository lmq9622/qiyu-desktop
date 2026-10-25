# -*- coding: utf-8 -*-
"""Qiyu 数据模型与消息协议解析（从 demo.py 迁移，M1）。"""
import re
import json
import time
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel


from companion.constants import CHAT_MSG_TYPES, CHAT_STATES, EMOTION_KEYS, SCENES

class ChatMessage(BaseModel):
    role: str
    content: str

def _msg_text(content) -> str:
    """把 OpenAI 多模态消息 content（str 或 list 内容块）规整为纯文本"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "image_url":
                    parts.append("[图片]")
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content or "")

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str = "demo"
    temperature: Optional[float] = None
    stream: bool = False
    user: str = "demo_user"
    use_memory: bool = True
    use_rag: bool = True
    images: Optional[list] = None

class CreateCharacterRequest(BaseModel):
    id: Optional[str] = None
    name: str
    tagline: Optional[str] = ""
    description: Optional[str] = ""
    persona: Optional[str] = None
    temperature: float = 0.7
    keywords: Optional[list] = None
    tone: Optional[str] = "自然"
    avatar_color: Optional[str] = None
    avatar: Optional[str] = None
    resume: Optional[dict] = None
    persona_params: Optional[dict] = None
    soul: Optional[str] = ""        # 灵魂：价值观/气质（最稳定层）
    boundaries: Optional[str] = ""  # 红线：硬约束（优先级最高）
    user_profile: Optional[str] = None

def parse_resume(text: str) -> dict:
    """把结构化角色简历文本解析成 dict（按【字段名】行切分），供 RAG / 调度读取"""
    canonical = {
        "基本信息": "基本信息",
        "虚构生活环境": "生活环境",
        "生活环境": "生活环境",
        "性格细节": "性格",
        "性格": "性格",
        "虚构背景": "背景",
        "背景": "背景",
        "爱好与日常": "爱好日常",
        "爱好日常": "爱好日常",
        "遇到问题时的反应": "问题应对",
        "问题应对": "问题应对",
        "与用户的关系": "关系定位",
        "关系定位": "关系定位",
        "调度标签": "调度标签",
        "对话自我表述": "对话自我表述",
        "数据关键词": "数据关键词",
        "角色参数": "角色参数",
    }
    resume = {}
    if not text:
        return resume
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 去掉行首编号，如 "1. " / "1、"
        m = re.match(r"^\d+[\.\)、]\s*", line)
        if m:
            line = line[m.end():]
        field = None
        content = None
        # 格式1：【字段名】内容
        m = re.match(r"^【(.+?)】\s*(.*)$", line)
        if m:
            field, content = m.group(1), m.group(2)
        else:
            # 格式2：**字段名**：内容
            m = re.match(r"^\*\*(.+?)\*\*\s*[:：]\s*(.*)$", line)
            if m:
                field, content = m.group(1), m.group(2)
            else:
                # 格式3：字段名：内容（仅当字段名是已知字段）
                m = re.match(r"^(.+?)[:：]\s*(.+)$", line)
                if m and m.group(1).strip() in canonical:
                    field, content = m.group(1).strip(), m.group(2)
        if not field:
            continue
        canonical_field = canonical.get(field.strip())
        if not canonical_field:
            continue
        content = (content or "").strip().strip("|").strip()
        if canonical_field in resume and content:
            resume[canonical_field] += " | " + content
        elif content:
            resume[canonical_field] = content
    return resume

def resume_to_text(resume: dict) -> str:
    """把简历 dict 还原成结构化文本（用于写入 RAG 知识库）"""
    return "\n".join(f"【{k}】{v}" for k, v in resume.items() if v)

def parse_persona_params(text: str) -> dict:
    """从简历文本中解析【角色参数】为结构化 dict（MBTI / 滑块数值 / 关系）"""
    params = {}
    for line in (text or "").splitlines():
        line = line.strip()
        m = re.match(r"^\d+[\.\)、]\s*", line)
        if m:
            line = line[m.end():]
        m = re.match(r"^【角色参数】\s*(.*)$", line)
        if not m:
            continue
        for part in m.group(1).split("|"):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k.upper().startswith("MBTI"):
                    params["mbti"] = v.upper()[:4]
                elif k == "反驳阈值":
                    params["rebut"] = _to_int(v, 50)
                elif k == "主见值":
                    params["assertiveness"] = _to_int(v, 50)
                elif k == "好感度":
                    params["affinity"] = _to_int(v, 50)
                elif k == "友情值":
                    params["friendship"] = _to_int(v, 50)
                elif k == "脏话倾向":
                    params["crude"] = _to_int(v, 20)
                elif k == "开放度":
                    params["openness"] = _to_int(v, 40)
                elif k == "关系":
                    params["relationship"] = v
                elif k == "感性理性":
                    params["sensible"] = _to_int(v, 50)
                elif k == "粘人独立":
                    params["clingy"] = _to_int(v, 50)
                elif k == "随性自律":
                    params["discipline"] = _to_int(v, 50)
                elif k == "热情冷淡":
                    params["warmth"] = _to_int(v, 50)
    return params

def _to_int(v: str, default: int = 50) -> int:
    try:
        n = int(re.sub(r"\D", "", v))
        return max(0, min(100, n))
    except Exception:
        return default

def _unescape_json_text(s: str) -> str:
    """把 JSON 字符串片段还原成文本（处理 \\uXXXX / \\" / \\n 等转义）"""
    try:
        return json.loads('"' + s + '"')
    except Exception:
        return s

def _extract_json_object(text: str) -> str:
    """从模型输出中稳健提取 JSON 对象字符串（容忍 Markdown 围栏/前后废话）"""
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.M)
    t = re.sub(r"\s*```\s*$", "", t)
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        return ""
    return t[start:end + 1]

# 模型会把上下文里的时间线索（[刚刚]/【3分钟前】/[今天]）抄进正文，统一剥掉。
# 位置不限：标签出现在句子中间时，说明模型是拿它当"两条气泡的分隔符"——
# 这时换成换行，让下面按行拆条把一条拆成两条气泡（不能挤在一个气泡里）。
_TIME_TAG_RE = re.compile(
    r"[\[【(（]\s*(?:刚刚|更早|今天|昨天|前天|刚刚才|\d+\s*(?:分钟|小时|天)前)\s*[\]】)）]"
)
_TIME_TAG_SPACE_RE = re.compile(r"[ \t]{2,}")

def strip_time_tag(text) -> str:
    """剥掉抄进正文的时间标签；夹在句子中间的换成换行（当作分条记号）。"""
    out = _TIME_TAG_RE.sub("\n", str(text or ""))
    out = re.sub(r"[ \t]*\n[ \t]*", "\n", out)
    return _TIME_TAG_SPACE_RE.sub(" ", out).strip()

# 只含 ASCII 结构符号/空白的输出（"{"、"{}"、"[" 之类）当成无效输出，绝不发给用户
_STRUCTURAL_ONLY_RE = re.compile(r'^[\s\[\]{}",:;\'`]+$')
_JSON_FRAGMENT_RE = re.compile(r'"(?:messages|conversation_state|text|type|emotion_delta|relation_delta|memory)"\s*:')

def _raw_has_usable_text(messages) -> bool:
    """这段 messages（模型输出里的字段）里有没有至少一条可发的 text。"""
    if isinstance(messages, dict):
        messages = [messages]
    if isinstance(messages, str):
        return bool(messages.strip())
    if not isinstance(messages, list):
        return False
    for item in messages:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            return True
    return False

def looks_like_broken_model_output(raw_text: str) -> bool:
    """这段模型输出能不能变成至少一条可发的消息；不能就算坏输出。

    弱模型偶尔只回 `{`、`{}`、`{"messages": [`，或回一个 messages 里 text 全空的 JSON。
    老逻辑最后有一段"兜底成单条消息"，会把这些原样发给用户 —— 用户看到的就是一个 `{`。
    """
    t = str(raw_text or "").strip()
    if not t:
        return True
    if _STRUCTURAL_ONLY_RE.match(t):
        return True
    obj = _extract_json_object(t)
    if obj:
        try:
            data = json.loads(obj)
        except Exception:
            data = None
        if isinstance(data, dict):
            return not _raw_has_usable_text(data.get("messages"))
        return True
    if t[0] in "{[" and _JSON_FRAGMENT_RE.search(t):
        return True
    return False

# 模型偶发吐出来的尾巴垃圾：连续逗号/顿号/省略号（"等你下课去找你啊，，，，，"）
_TAIL_JUNK_RE = re.compile(r"[，,、；;．…\s]{2,}$")

def _strip_tail_junk(t: str) -> str:
    """去掉尾巴上的连续标点垃圾；清洗后为空则回退原串。"""
    try:
        out = _TAIL_JUNK_RE.sub("", (t or "").strip())
        return out if out else (t or "").strip()
    except Exception:
        return t

# 线下"到场"幻觉：只拦"角色声称自己人已经在用户身边 / 已经线下见面"，
# 不再误杀日常寒暄与关心（"我在这呢，怎么啦" / "你出来吃饭了吗" / "早点睡别动气" /
# "收到 我这就去写" / "我等你呢" 这些都必须放行）。收窄要点：
#   · 必须带"你"字锚点（你楼下/你门口/你身边…）或明确的物理接触/开门/过来找你；
#   · 单纯"我在/我到/我站/我等你/这就去写"不再命中。
_PRESENCE_RE = re.compile(
    # ① 声称已到/正在「你」所在地（你家楼下、你门口、你身边、你对面…）
    r"我.{0,3}(?:到|在|站|蹲|坐|赶|走)(?:了)?你(?:家|楼下|楼上|门口|门(?:前|口)|身边|身旁|旁边|对面|面前|身后|这边|那边|那儿|这儿)"
    # ② 命令对方开门 / 快点出来（= "我人就在门外"）
    r"|开(?:一下)?门|快点(?:出|下)来"
    # ③ 现在/马上 过来、过去找对方（线下）
    r"|我.{0,4}(?:过来|过去)找"
    # ④ 物理接触（线下动作描写）：抱住/搂住/牵住…你
    r"|(?:抱住|抱着|搂住|搂着|牵住|牵着)你"
    # ⑤ 让对方向"我这边"看 / 确认看到了我本人
    r"|往我(?:这边|那儿|这)看|看到我(?:吗|了没|了没有|没)"
    # ⑥ 独立"我到了"（后面不接地点宾语 = 到用户面前了）
    r"|我到了(?=[，,。！!？?\s]|$|你|楼下|门口|楼底下)"
)

def _looks_like_presence_hallucination(t: str) -> bool:
    """是否在假装"我人已经到现场了"（我到你家楼下了/我就在你门口/你往我这边看…）。

    只抓"声称已在用户身边 / 已线下见面"，不误杀普通寒暄与关心。
    """
    try:
        return bool(_PRESENCE_RE.search(t or ""))
    except Exception:
        return False

# 行首是列表符号/编号 → 这一行不是新气泡边界，要并入上一条（清单/步骤不该被拆散）
_LIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*•·+]\s+|(?:\d{1,2}[.、)）]\s*)|(?:[（(]\d{1,2}[)）]\s*)|(?:[一二三四五六七八九十]+[.、)]\s*))"
)


def _split_bubbles(text: str) -> list:
    """把一段可能含换行的文本拆成若干气泡（真人微信逐条发的节奏）。

    收窄旧实现"无条件按换行拆"导致的误拆：
      · ``` 包裹的代码块**整段不拆**（连同围栏一起保留，含未闭合的）；
      · 行首是列表符号/编号（- * • 1. 1、 (1) 一、 …）的行，或带缩进的行 → 并入上一行；
      · 以上一行冒号（：/:）结尾时，与紧随其后的内容并成一条（「给你写个清单：」+清单）；
      · 其余才按换行拆。
    返回去掉空段后的列表；全空时回退 [strip 后的原文]。
    """
    t = str(text or "")
    if not t.strip():
        return []
    lines = t.split("\n")
    segs: list = []
    cur = ""
    code_buf = None   # None = 不在代码块内；list = 正在累积的代码块行

    def _flush():
        nonlocal cur
        if cur.strip():
            segs.append(cur.strip())
        cur = ""

    for line in lines:
        s = line.strip()
        if code_buf is not None:
            code_buf.append(line)
            if s.startswith("```"):
                segs.append("\n".join(code_buf).strip())
                code_buf = None
            continue
        if s.startswith("```"):
            _flush()
            code_buf = [line]
            continue
        if not s:
            _flush()                     # 空行 = 气泡边界
            continue
        indented = line[:1] in (" ", "\t", "　")
        if _LIST_ITEM_RE.match(s) or (indented and cur):
            # 列表项 / 缩进行 → 并入上一条
            cur = (cur + "\n" + s) if cur else s
            continue
        if cur and cur.rstrip().endswith(("：", ":")):
            # 冒号结尾的行与后续并成一条
            cur = cur + "\n" + s
            continue
        _flush()
        cur = s
    if code_buf is not None:
        segs.append("\n".join(code_buf).strip())   # 未闭合的代码块也整段保留
    _flush()
    return segs or [t.strip()]


def _split_long_bubbles(msgs: list, limit: int = 26) -> list:
    """兜底用：单条太长又**带空格**时，只按空格折成多条短气泡。

    规则收窄成用户要求的样子：
    - **只按空格（含全角空格/换行）拆**，不按标点拆，**绝不按字数硬切**
    - 没有空格就原样保留（长短交给提示词去管，这里不动）
    - 折出来的段数最多 4 条，第 2 条起隔 700ms
    """
    out = []
    for m in msgs or []:
        t = str(m.get("text") or "").strip()
        if len(t) <= limit:
            out.append(m)
            continue
        parts = [x.strip() for x in re.split(r"[\s　]+", t) if x.strip()]
        if len(parts) <= 1:
            out.append(m)          # 没空格 → 不拆（不硬拆、不按标点拆）
            continue
        merged, cur = [], ""
        for seg in parts:
            if cur and len(cur) + 1 + len(seg) > limit:
                merged.append(cur)
                cur = seg
            else:
                cur = (cur + " " + seg) if cur else seg
        if cur:
            merged.append(cur)
        merged = [x for x in merged if x][:4]
        if len(merged) <= 1:
            out.append(m)
            continue
        for k, seg in enumerate(merged):
            out.append({"text": seg, "type": m.get("type", "statement"),
                        "delay": 0 if k == 0 else 700})
    return out

def _dedup_consecutive_bubbles(msgs: list) -> list:
    """去掉连续重复气泡（模型有时把同一句发两遍）。"""
    out, prev = [], ""
    for m in msgs or []:
        t = str(m.get("text") or "").strip()
        if t and t == prev:
            continue
        out.append(m)
        prev = t or prev
    return out

def postprocess_reply_messages_extra(parsed: dict, longform: bool = False) -> dict:
    """回复后处理的补充护栏（服务器版 _postprocess_reply_messages 里桌面缺的那几项）：
    尾巴垃圾清洗 → 到场幻觉拦截 → 连续重复气泡去重 → 过长气泡按空格拆分。
    就地修改 parsed["messages"] 并返回 parsed。

    只纠正明显错误、不改写正常回复。全部被拦空时**返回空 messages**（不再回退首条）——
    上层自有兜底：behavior._postprocess_reply_messages 之后 streaming 路径
    （demo.py）对空 messages 会重拉一次，仍空则回「在呢 / 刚走神了一下」这类自然话。
    """
    msgs = parsed.get("messages") or []
    if not msgs:
        return parsed
    cleaned = []
    for m in msgs:
        t = _strip_tail_junk(m.get("text", ""))
        if not t:
            continue
        # 到场幻觉整条丢弃（角色不能真的出现在用户面前）
        if _looks_like_presence_hallucination(t):
            continue
        cleaned.append({**m, "text": t})
    # 全被拦空 → 留空，交给上层空回复兜底（不回退首条）
    if not cleaned:
        parsed["messages"] = []
        return parsed
    cleaned = _dedup_consecutive_bubbles(cleaned)
    if not longform:
        cleaned = _split_long_bubbles(cleaned)
    parsed["messages"] = cleaned
    return parsed

def parse_chat_messages(raw_text: str) -> dict:
    """解析模型输出的微信消息 JSON → {"conversation_state", "relation_delta", "relationship", "messages":[{text,type,delay}]}
    模型输出漂移时自动兜底为单条消息，不让对话断掉。
    """
    state = "闲聊"
    relation_delta = None
    relationship = ""
    messages = []
    obj_str = _extract_json_object(raw_text or "")
    data = None
    if obj_str:
        try:
            data = json.loads(obj_str)
        except Exception:
            data = None
    if isinstance(data, dict):
        cs = (data.get("conversation_state") or "").strip()
        if cs in CHAT_STATES:
            state = cs
        rd = data.get("relation_delta")
        if isinstance(rd, dict):
            d = {}
            for k in ("affinity", "friendship"):
                try:
                    v = int(float(rd.get(k, 0) or 0))
                except Exception:
                    v = 0
                if v:
                    d[k] = max(-8, min(8, v))
            if d:
                relation_delta = d
        rel = (data.get("relationship") or "").strip()
        if rel:
            relationship = rel[:120]
        raw_msgs = data.get("messages")
        if isinstance(raw_msgs, dict):  # 容忍单条 dict
            raw_msgs = [raw_msgs]
        if isinstance(raw_msgs, list):
            for i, m in enumerate(raw_msgs[:60]):
                if not isinstance(m, dict):
                    continue
                text = str(m.get("text") or "").strip()
                if not text:
                    continue
                text = strip_time_tag(text)              # 时间标签剥掉；夹在中间的当分条记号
                if not text:
                    continue
                mtype = str(m.get("type") or "").strip().lower()
                if mtype not in CHAT_MSG_TYPES:
                    mtype = "statement"
                try:
                    delay = int(float(str(m.get("delay") or 0)))
                except Exception:
                    delay = 0
                # 模型偶尔把两条气泡塞进同一个 text（用换行分隔），微信里就会渲染成
                # 「一条消息中间换行」而不是两条气泡 —— 这里统一按行拆开。
                # 但代码块整段不拆、列表项/冒号引导的清单并入上一条（用 _split_bubbles）。
                chunks = _split_bubbles(text) or [text]
                for ci, chunk in enumerate(chunks):
                    _delay = _normalize_delay(state, i + ci, delay,
                                              total_hint=len(raw_msgs) + len(chunks) - 1)
                    _item = {"text": chunk[:3000], "type": mtype, "delay": _delay}
                    if ci == 0:
                        img = str(m.get("image_url") or "").strip()
                        if img:
                            _item["image_url"] = img[:2000]
                    messages.append(_item)
    if not messages:
        raw = (raw_text or "").strip()
        # 兜底1：模型 JSON 被截断/损坏时，提取里面已输出的 text 片段当消息，绝不把原始 JSON 当消息发出去
        if raw and ("conversation_state" in raw or '"messages"' in raw):
            texts = []
            for mm in re.finditer(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
                t = _unescape_json_text(mm.group(1)).strip()
                if t:
                    texts.append(t)
            if texts:
                # 兜底分支也要按行拆：模型把两条气泡塞进一个 text 时，
                # 这里不拆的话用户看到的还是"一条消息中间换行"。
                flat = []
                for t in texts[:60]:
                    flat.extend(_split_bubbles(strip_time_tag(t)) or [t])
                for i, t in enumerate(flat):
                    messages.append({"text": t[:3000], "type": "statement",
                                     "delay": _normalize_delay("闲聊", i, 0, total_hint=len(flat))})
        if not messages and raw and not looks_like_broken_model_output(raw):
            # 走到这里说明模型没吐 JSON（常见于"把已生成好的整段回复再喂回解析器"）：
            # 以前整段塞进一个气泡 → 用户看到"两条消息挤在一个气泡里、中间换行"。
            # 现在照样按行拆成独立气泡（时间标签一起剥掉）；坏输出（"{"、"{}"）直接丢弃。
            _raw2 = strip_time_tag(str(raw).replace("\\n", "\n"))
            _fb = _split_bubbles(_raw2)
            for _i, _t in enumerate(_fb[:60] or [raw]):
                messages.append({"text": _t[:3000], "type": "statement",
                                 "delay": _normalize_delay("闲聊", _i, 0, total_hint=len(_fb))})
        # 模型确实什么都没输出：返回空列表，绝不注入假"……"思考气泡（thinking 由流式 reasoning 通道承载）
    schedules = []
    if isinstance(data, dict):
        schedules = _parse_schedules(data.get("schedules"))
    # 双记忆层：模型每轮可带出固定记忆条目（long 写死 / short 带权重短期记忆）
    memory_data = {"long": [], "short": []}
    if isinstance(data, dict):
        mem = data.get("memory")
        if isinstance(mem, dict):
            long_items = mem.get("long") or []
            short_items = mem.get("short") or []
            memory_data["long"] = [str(x).strip()[:300] for x in long_items if isinstance(x, str) and len(str(x).strip()) >= 4][:8]
            short_clean = []
            for x in short_items[:8]:
                if isinstance(x, str):
                    short_clean.append({"text": x.strip()[:300], "weight": 0.5})
                elif isinstance(x, dict) and (x.get("text") or "").strip():
                    try:
                        w = max(0.0, min(1.0, float(x.get("weight", 0.5) or 0.5)))
                    except Exception:
                        w = 0.5
                    short_clean.append({"text": str(x["text"]).strip()[:300], "weight": w})
            memory_data["short"] = [x for x in short_clean if len(x["text"]) >= 4]
    webcheck = ""
    if isinstance(data, dict):
        wc = data.get("webcheck")
        if isinstance(wc, str):
            webcheck = wc.strip()[:200]
        elif isinstance(wc, list) and wc:
            webcheck = str(wc[0]).strip()[:200]
    topic = ""
    if isinstance(data, dict):
        t = data.get("topic")
        if isinstance(t, str):
            topic = t.strip()[:80]
    # 场景/意愿/内部分析字段（模型软提示，最终以代码计算为准）
    scene = ""
    if isinstance(data, dict):
        s = (data.get("scene") or "").strip().lower()
        if s in SCENES:
            scene = s
    interaction_need = 0
    try:
        interaction_need = int(float((data or {}).get("interaction_need") or 0))
    except Exception:
        interaction_need = 0
    topic_confidence = 0.0
    try:
        topic_confidence = max(0.0, min(1.0, float((data or {}).get("topic_confidence") or 0)))
    except Exception:
        topic_confidence = 0.0
    topic_shift = bool((data or {}).get("topic_shift"))
    user_intent = str((data or {}).get("user_intent") or "").strip()[:40]
    user_emotion = str((data or {}).get("user_emotion") or "").strip()[:40]
    emotion_delta = {}
    if isinstance(data, dict):
        ed = data.get("emotion_delta")
        if isinstance(ed, dict):
            for k in EMOTION_KEYS:
                try:
                    v = int(float(ed.get(k) or 0))
                except Exception:
                    continue
                if v:
                    emotion_delta[k] = max(-15, min(15, v))
    emotion_reason = str((data or {}).get("emotion_reason") or "").strip()[:120]
    actions = []
    if isinstance(data, dict):
        for a in (data.get("actions") or [])[:4]:
            if isinstance(a, dict) and a.get("type"):
                actions.append({"type": str(a["type"]).strip()[:20], "query": str(a.get("query") or "")[:200]})
    return {"conversation_state": state, "relation_delta": relation_delta, "relationship": relationship,
            "schedules": schedules, "memory": memory_data, "webcheck": webcheck, "topic": topic,
            "scene": scene, "interaction_need": interaction_need, "topic_confidence": topic_confidence,
            "topic_shift": topic_shift, "user_intent": user_intent, "user_emotion": user_emotion,
            "emotion_delta": emotion_delta, "emotion_reason": emotion_reason,
            "actions": actions, "messages": messages}

def _try_stream_parse(buf: str):
    """增量解析流式输出的 JSON：能解析就返回 (state, messages)，还没闭合/不可解析返回 None。
    只尝试最后 1~2 个右花括号，避免 O(n^2)；delay 按 state 用同一套规则校准。"""
    t = (buf or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    start = t.find("{")
    if start < 0:
        return None
    e = t.rfind("}")
    if e <= start:
        return None
    ends = [e]
    e2 = t.rfind("}", 0, e)
    if e2 > start:
        ends.append(e2)
    for end in ends:
        try:
            data = json.loads(t[start:end + 1])
        except Exception:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            continue
        state = (data.get("conversation_state") or "").strip()
        if state not in CHAT_STATES:
            state = "闲聊"
        msgs = []
        for i, m in enumerate(data["messages"][:60]):
            if not isinstance(m, dict):
                continue
            text = str(m.get("text") or "").strip()
            if not text:
                continue
            text = strip_time_tag(text)
            if not text:
                continue
            mtype = str(m.get("type") or "").strip().lower()
            if mtype not in CHAT_MSG_TYPES:
                mtype = "statement"
            try:
                delay = int(float(str(m.get("delay") or 0)))
            except Exception:
                delay = 0
            # 与 parse_chat_messages 保持同一套拆条规则（真换行/句中时间标签；
            # 代码块整段不拆、列表项并入上一条）：
            # 否则流式预览与最终解析的条数不一致，demo 的 safe_pushed 会重复推送气泡。
            chunks = _split_bubbles(text) or [text]
            for ci, chunk in enumerate(chunks):
                _delay = _normalize_delay(state, i + ci, delay,
                                          total_hint=len(data["messages"]) + len(chunks) - 1)
                msgs.append({"text": chunk[:3000], "type": mtype, "delay": _delay})
        return state, msgs
    return None

def _normalize_delay(state: str, index: int, delay: int, total_hint: int = 0) -> int:
    """真人节奏：第一条即时反应；情绪状态连发无延迟；普通状态每条之间 0.3~2 秒。
    长内容（讲故事/长文，消息 10 条以上）中后段加快到 0.3~1.5 秒，避免整段故事拖几分钟才播完。"""
    delay = max(0, min(8000, delay))
    if index == 0:
        return max(0, min(800, delay))
    if state in ("兴奋", "吐槽", "吵架"):
        return max(0, min(800, delay))
    if total_hint >= 10 and index >= 8:
        return max(300, min(1500, delay))
    return max(300, min(2000, delay))

def _parse_schedule_time(at_str: str) -> float | None:
    """解析提醒时间：支持 YYYY-MM-DD HH:MM 或 HH:MM（今天已过则明天），返回 epoch"""
    try:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})", (at_str or "").strip())
        if m:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
        else:
            m2 = re.match(r"^(\d{1,2}):(\d{2})", (at_str or "").strip())
            if not m2:
                return None
            dt = datetime.now().replace(hour=int(m2.group(1)) % 24, minute=int(m2.group(2)), second=0, microsecond=0)
            if dt.timestamp() <= time.time():
                dt = datetime.fromtimestamp(dt.timestamp() + 86400)
        return dt.timestamp()
    except Exception:
        return None

def _parse_schedules(raw) -> list:
    """解析模型输出的 schedules（nudge/reminder），非法项跳过"""
    out = []
    if not isinstance(raw, list):
        return out
    for s in raw[:5]:
        if not isinstance(s, dict):
            continue
        stype = str(s.get("type") or "").strip()
        if stype == "nudge":
            try:
                after = max(1, min(60, int(float(s.get("after_minutes", 5) or 5))))
            except Exception:
                after = 5
            out.append({"type": "nudge", "after_minutes": after})
        elif stype == "reminder":
            text = str(s.get("text") or "").strip()[:200]
            ts = _parse_schedule_time(str(s.get("at") or ""))
            if text and ts:
                try:
                    weight = max(0.0, min(1.0, float(s.get("weight", 0.5) or 0.5)))
                except Exception:
                    weight = 0.5
                out.append({"type": "reminder", "at_ts": ts, "text": text, "weight": weight})
    return out

class SaveRoutesRequest(BaseModel):
    rules: list[dict]

class SaveSettingsRequest(BaseModel):
    llm_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_route_url: Optional[str] = None
    llm_route_model: Optional[str] = None
    api_key: Optional[str] = None
    default_temperature: Optional[float] = None
    memory_tags: Optional[list] = None
    allow_profanity: Optional[bool] = None
    allow_naughty: Optional[bool] = None
    profanity_level: Optional[str] = None   # off/low/mid/high/ultra
    naughty_level: Optional[str] = None     # off/low/mid/high/ultra
    thinking_level: Optional[str] = None    # off/low/mid/high/ultra
    desire_base: Optional[int] = None       # 对话欲望基准 0-100
    web_enabled: Optional[bool] = None
    proactive_enabled: Optional[bool] = None  # 联想开关：角色主动发消息/分享日常
    show_thinking: Optional[bool] = None
    thinking_enabled: Optional[bool] = None
    user_profile: Optional[str] = None
    parallel_requests: Optional[str] = None   # auto/1/2/3/4/0(不限制)
    delayed_reply_enabled: Optional[bool] = None  # 延时回复开关：像真人一样 1~3 秒后才回、连续多条攒成一轮
    day_memory_enabled: Optional[bool] = None     # 当天记忆实时归纳（大纲+事件分条），默认开，增加算力但极大影响记忆
    night_memory_enabled: Optional[bool] = None   # 凌晨4点把前一天聊天归纳入库（短/长期记忆分级），默认开
    user_location: Optional[str] = None           # 用户所在城市/地区（手动填，最高优先级；留空则用公网 IP 解析兜底）
    user_gender: Optional[str] = None

class MemoryAddRequest(BaseModel):
    text: str
    tags: Optional[list] = None


__all__ = [
    "ChatMessage",
    "ChatRequest",
    "CreateCharacterRequest",
    "MemoryAddRequest",
    "SaveRoutesRequest",
    "SaveSettingsRequest",
    "_extract_json_object",
    "_msg_text",
    "_normalize_delay",
    "_parse_schedule_time",
    "_parse_schedules",
    "_to_int",
    "_try_stream_parse",
    "_unescape_json_text",
    "looks_like_broken_model_output",
    "parse_chat_messages",
    "parse_persona_params",
    "parse_resume",
    "postprocess_reply_messages_extra",
    "resume_to_text",
    "strip_time_tag",
]
