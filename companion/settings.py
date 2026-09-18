# -*- coding: utf-8 -*-
"""Qiyu 运行时设置与提示块（从 demo.py 迁移，M1）。"""
import os
import re
import json
import time
import httpx
from datetime import datetime
from loguru import logger


from companion.constants import INJECTION_PATTERNS, _LONGFORM_KW, _USER_NET_TTL
from companion.state import LLM_MODEL, LLM_ROUTE_MODEL, LLM_ROUTE_URL, LLM_URL, SETTINGS_JSON, user_states
from companion.models import _msg_text
from companion.relations import _clamp_int, _init_relation

def build_resume_prompt(tags: list, name: str = "", description: str = "", mbti: str = "",
                        param_hints: dict | None = None, user_gender: str = "",
                        relationship_role: str = "") -> str:
    """构建结构化角色简历的生成提示词（路由模型专用）
    description: 用户写的人格描述（与标签并列，作为生成依据）
    mbti: 用户已指定的 MBTI（留空则由 AI 按标签生成）
    param_hints: 用户预填的量化参数（反驳阈值/主见值/好感度/关系等），留空的项由 AI 补全
    user_gender: 用户向（male/female），影响角色性别与称呼方式
    relationship_role: 角色与用户的关系定位（伴侣/朋友/空），影响关系定位字段与初始数值
    """
    tags_str = "、".join(tags) if tags else "未指定"
    name_hint = f"角色名称：{name}" if name else "请为这个角色起一个合适的名字"
    guide_parts = []
    desc = (description or "").strip()
    if desc:
        guide_parts.append(
            "用户提供的人格描述（以此为基础展开成完整简历，可补充细节，但不得偏离用户意图）：\n" + desc[:1500]
        )
    mb = (mbti or "").strip().upper()
    if re.match(r"^[EI][NS][TF][JP]$", mb):
        guide_parts.append(f"用户已指定 MBTI：{mb}。【角色参数】里的 MBTI 必须用这个值，不要另选。")
    hints = param_hints or {}
    hint_items = []
    for k, v in hints.items():
        if v in (None, ""):
            continue
        hint_items.append(f"{k}={v}")
    if hint_items:
        guide_parts.append("用户已预填的参数（其余留空的量化项由你按标签合理补全 0-100 整数）：" + " | ".join(hint_items))
    _rel_guide = ""
    _ug = (user_gender or "").strip().lower()
    if _ug in ("male", "female"):
        _ug_label = "男生" if _ug == "male" else "女生"
        _partner_gender = "女生" if _ug == "male" else "男生"
        _rel_guide += f"【用户向】用户是{_ug_label}。"
        _rr = (relationship_role or "").strip()
        if _rr == "伴侣":
            _rel_guide += (f"这个角色是用户的伴侣向人设（恋人/暧昧对象），角色性别应为{_partner_gender}；"
                           f"【关系定位】按恋爱关系写：会关心、会想念、熟了会撒娇/吃醋/拌嘴，但不是服务者不是舔狗，"
                           f"语气平等有情绪。初始好感度给 60~85，友情值给 40~70。")
        elif _rr == "朋友":
            _rel_guide += (f"这个角色是用户的朋友向人设（死党/兄弟/闺蜜/损友），角色性别与称呼方式要贴合{_ug_label}用户；"
                           f"【关系定位】按平级朋友/死党写：该损就损、该撑场撑场、不暧昧不撩；"
                           f"初始好感度给 45~75，友情值给 55~90。")
        else:
            _rel_guide += "按标签自然确定角色与用户的关系定位（朋友/恋人/暧昧均可，但保持平级、不服务化）。"
        guide_parts.append(_rel_guide)
    guide_block = ("\n\n" + "\n\n".join(guide_parts)) if guide_parts else ""
    return f"""你是一名角色档案设计师。根据用户提供的角色标签，输出一份【结构化角色简历】，用途：1) 存入 RAG 知识库供检索；2) 供上层决策调度直接读取字段；3) 作为角色对话的稳定人格依据。

标签：{tags_str}
{name_hint}
{guide_block}

这是角色简历/档案，不是小说：禁止文学化描写、禁止场景叙述、禁止抒情长句、禁止'他笑了笑/空气安静下来'这类文字。每条信息必须是简短的事实性要点。

【格式硬性规定，必须逐字遵守】
1. 每行一个字段，行首必须是【字段名】，例如「【基本信息】姓名=阿锐 | 年龄=26 | 职业=游戏测试」。
2. 禁止使用 Markdown 加粗（**字段**）、禁止编号（1. 2.）、禁止空行。
3. 字段值里多个要点用「 | 」分隔；键值用「=」。
4. 必须且只能包含下面 11 个字段，按顺序输出，不要额外解释、不要'以下是简历'这类话。
5. 最后一行【角色参数】是给决策调度读的量化参数，所有数值都是 0-100 的整数；MBTI 必须是四个字母（如 INFP）。数值含义：反驳阈值 0=从不抬杠 100=句句抬杠；主见值 0=完全随用户 100=极有主见；好感度 0=陌生人 100=生死之交；友情值 0=萍水之交 100=铁杆死党；感性理性 0=极感性 100=极理性；粘人独立 0=极粘人 100=极独立；随性自律 0=极随性 100=极自律；热情冷淡 0=极热情 100=极冷淡；脏话倾向 0=从不爆粗 100=张口就来（软萌清纯型给 0-10，普通型 10-30，损友/毒舌型 40-70，痞气型 70-100）；开放度 0=保守内敛 100=放得开（决定暧昧/成人向互动的自然程度）。

示例（格式参照，内容随意）：
【基本信息】姓名=林骁 | 年龄=28 | 职业=自由插画师 | 外形=瘦高、黑框眼镜 | 标志物=脖子上常挂相机
【生活环境】城市=杭州 | 居住=老厂房顶层单间 | 同居=一只橘猫 | 作息=白天接单、晚上剪片
【性格】标签=嘴硬心软、精准吐槽、情绪外露 | 口头禅=你这脑回路是租来的？ | 说话节奏=短句快语速 | 情绪表达=烦了戴降噪耳机、开心甩搞笑视频
【背景】成长=北方重工业城市 | 家庭=父亲下岗钳工、母亲开早餐铺 | 关键经历=高中劝退后自学剪辑入行
【爱好日常】黑咖啡续命 | 周末逛二手市集 | 手机相册存翻车实拍
【问题应对】压力=静音两小时再处理 | 冲突=不冷战、观点甩完留台阶 | 对方低落=递冰美式、帮捋事不哄
【关系定位】与用户=十年平级死党 | 语气=嘲讽+靠谱切换 | 规则=该怼就怼、撑场不废话、不说教不写小作文
【调度标签】触发词=加班、游戏、接活、吐槽、情绪低落 | 擅长话题=插画、剪辑、猫、数码 | 回避话题=煽情、说教、鸡汤
【对话自我表述】我是林骁，搞插画和剪片的，嘴损但办事靠谱。你的事就是我的事，但别跟我整虚的。
【数据关键词】character=林骁 | tags=损友、直率、幽默 | 检索词=游戏搭子、毒舌朋友、设计师
【角色参数】MBTI=ENTP | 反驳阈值=75 | 主见值=80 | 好感度=60 | 友情值=55 | 关系=十年损友 | 感性理性=30 | 粘人独立=70 | 随性自律=55 | 热情冷淡=35

现在根据标签 {tags_str} 输出{name_hint}的角色简历："""

def _route_llm_info():
    """路由模型连接信息（配置了独立路由模型则走它，否则回退主模型）"""
    route_url = (LLM_ROUTE_URL or LLM_URL).rstrip("/")
    route_model = LLM_ROUTE_MODEL or LLM_MODEL
    route_api_key = load_runtime_settings().get("api_key", "")
    headers = {"Content-Type": "application/json"}
    if route_api_key:
        headers["Authorization"] = f"Bearer {route_api_key}"
    return route_url, route_model, headers

def is_injection(text: str) -> bool:
    """判断用户输入是否像提示词注入"""
    if not text:
        return False
    return any(re.search(p, text, re.IGNORECASE) for p in INJECTION_PATTERNS)


# M4：运行时设置全量默认值（启动即完整加载，调用方无需在各自位置散落默认值）
DEFAULT_RUNTIME_SETTINGS = {
    "llm_url": LLM_URL,
    "llm_model": LLM_MODEL,
    "llm_route_url": LLM_ROUTE_URL,
    "llm_route_model": LLM_ROUTE_MODEL,
    "api_key": "",
    "default_temperature": 0.7,
    "memory_tags": [],
    "allow_profanity": False,
    "allow_naughty": False,
    "profanity_level": "off",
    "naughty_level": "off",
    "thinking_level": "off",
    "thinking_enabled": False,
    "desire_base": 50,
    "web_enabled": True,
    "proactive_enabled": True,
    "show_thinking": False,
    "parallel_requests": "auto",
    "delayed_reply_enabled": False,
    "day_memory_enabled": True,
    "night_memory_enabled": True,
    "vision_supported": False,
    "telegram_bot_token": "",
    "discord_bot_token": "",
    "uncensored": False,
    "user_location": "",
    "user_gender": "",
}


def load_runtime_settings() -> dict:
    """读取运行时设置：已保存值覆盖默认值，保证所有键在启动后即有完整取值。"""
    saved = {}
    if SETTINGS_JSON.exists():
        try:
            with open(SETTINGS_JSON, "r", encoding="utf-8") as f:
                saved = json.load(f) or {}
        except Exception:
            pass
    merged = dict(DEFAULT_RUNTIME_SETTINGS)
    merged.update(saved)
    return merged

def save_runtime_settings(settings: dict):
    try:
        with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存设置失败: {e}")

# 真正需要动脑的标记（求知/写作/分析/代码/长内容）
_THINK_KW_RE = re.compile(
    r"为什么|为啥|帮我|解释|分析|总结|归纳|整理|翻译|代码|报错|调试|方案|计划|步骤|教程|详细|展开讲|"
    r"对比|区别|推荐|算一下|计算|推算|推理|证明|优化|设计|介绍一下|列个|列出|清单|写一[个段篇]|写个|长文|"
    r"story|explain|why|how to|translate|code|debug",
    re.I,
)


def _needs_thinking(user_text: str) -> bool:
    """这轮值不值得开思考。

    实测（x99 本地 27B）：短句闲聊开思考会多生成 300~500 个 reasoning token，
    一轮从 5~8 秒变成 37~40 秒，用户感知就是"发消息半天不回来"。
    所以只有真的需要动脑（求知/写作/分析/代码）或用户写了一大段时才开思考，
    纯寒暄/口头禅/极短消息一律关掉直接出正文。
    """
    t = (user_text or "").strip()
    if not t:
        return False
    if _THINK_KW_RE.search(t):
        return True
    if len(t) <= 18:
        return False          # 极短口语：直接出
    if len(t) >= 60:
        return True           # 用户写了一大段：值得想一下
    # 中等长度：明显在求教/要判断/要建议的才想一下，纯闲聊照样直出
    if len(t) >= 14 and re.search(r"怎么|如何|该不该|要不要|是不是|值不值|能不能|觉得|建议|纠结|选择|？|\?", t):
        return True
    return False


def _apply_thinking_kwargs(payload: dict, user_text: str = ""):
    """思考开关 + 强度：off=显式禁用推理（首字更快、更像真人直出）；low/mid/high/ultra=开启并按强度给思考预算。
    user_text 传入本轮用户原文后会做自适应：短句闲聊直接关思考，难题才用设置里的档位。"""
    runtime = load_runtime_settings()
    level = str(runtime.get("thinking_level") or "off").strip().lower()
    if level == "off":
        # 兼容旧的 thinking_enabled 开关：旧开关还开着就按 mid 处理
        if bool(runtime.get("thinking_enabled")):
            level = "mid"
        else:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            return
    # 自适应：设置里开着思考，但这一轮只是短句闲聊 → 关掉（省 20~30 秒、省一大半输出 token）
    if user_text and os.getenv("QIYU_NO_ADAPTIVE_THINKING", "0") != "1" and not _needs_thinking(user_text):
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        return
    budget = {"low": 1024, "mid": 2048, "high": 4096, "ultra": 8192}.get(level, 2048)
    payload["chat_template_kwargs"] = {"enable_thinking": True, "thinking_budget": budget}

def _current_time_block() -> str:
    """每次对话都给模型注入本地当前时间（避免时差感/不知道今天星期几）"""
    now = datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    return f"【当前时间】{now.strftime('%Y-%m-%d %H:%M')} 星期{wd}（这是你现在所处的真实时间，聊天要用它来感知'现在/刚才/今晚/昨天'）"

def _user_gender_block(char=None) -> str:
    """用户向（男/女）提示词块：辅助角色用正确的亲密/平级分寸相处，只作背景，不背出来"""
    try:
        runtime = load_runtime_settings()
        gender = str(runtime.get("user_gender") or "").strip().lower()
    except Exception:
        gender = ""
    if gender not in ("male", "female"):
        return ""
    gender_label = "男生" if gender == "male" else "女生"
    # 从角色卡判断是伴侣向还是朋友向（用于校准亲密/平级分寸）
    role = ""
    try:
        if char is not None:
            rp = getattr(char, "persona_params", None) or {}
            rel = str(rp.get("relationship") or "")
            resume = getattr(char, "resume", None) or {}
            rel2 = str(resume.get("关系定位") or "")
            joined = rel + " " + rel2
            if any(k in joined for k in ("女朋友", "男朋友", "老婆", "老公", "对象", "伴侣", "恋人", "妻子", "丈夫")):
                role = "伴侣"
            elif any(k in joined for k in ("死党", "闺蜜", "兄弟", "哥们", "朋友", "损友", "老友")):
                role = "朋友"
    except Exception:
        role = ""
    role_hint = ""
    if role == "伴侣":
        role_hint = ("你和用户是【伴侣/恋人】关系（由人设卡决定）：按恋爱中的人自然相处——会想对方、会关心、会吃醋、"
                     "会撒娇也会不耐烦，但绝不是客服或服务者；不要每句话都喊亲昵称呼，亲密感靠自然行为体现。")
    elif role == "朋友":
        role_hint = ("你和用户是【朋友/死党】关系（由人设卡决定）：平级相处，该吐槽吐槽、该撑场撑场，"
                     "不要像伴侣一样撩，也不要把气氛搞得客气；熟了才放得开，数值没到位就收敛一点。")
    return (f"【用户与你的关系基线（内部参考，影响分寸即可，不要背出来）】"
            f"用户是{gender_label}。{role_hint}称呼和语气按此自然调整，但不要刻意反复强调对方性别，"
            f"更不要每句话都喊称呼。")

async def _user_network_context() -> str:
    """检测用户公网 IP（可解析省市），缓存 6 小时后注入提示词，辅助角色理解用户。
    拿不到公网时退化为局域网 IP；全部失败返回空串（不影响对话）。
    只作背景参考，明确要求角色不要主动提 IP、不要问『你 IP 是多少』。"""
    global _USER_NET_CACHE
    # 手动填写优先（最高优先级，不缓存，改完下一条立即生效）
    try:
        _manual = str(load_runtime_settings().get("user_location") or "").strip()
    except Exception:
        _manual = ""
    if _manual:
        return ("【用户所在位置（用户手动填写，可信度最高）】"
                f"用户所在城市/地区：{_manual}。"
                "仅供你大致了解对方所在城市/时区，作为聊天背景参考；"
                "除非对方自己提起，否则不要主动问『你在哪个城市』。")
    now = time.time()
    if _USER_NET_CACHE["text"] and now - _USER_NET_CACHE["at"] < _USER_NET_TTL:
        return _USER_NET_CACHE["text"]
    text = ""
    try:
        ip = ""
        try:
            async with httpx.AsyncClient(timeout=4) as client:
                for url in ("https://api.ipify.org", "https://api64.ipify.org"):
                    try:
                        r = await client.get(url)
                        if r.status_code == 200 and r.text.strip():
                            ip = r.text.strip()
                            break
                    except Exception:
                        continue
        except Exception:
            ip = ""
        geo = ""
        if ip:
            try:
                async with httpx.AsyncClient(timeout=4) as client:
                    r = await client.get(
                        "http://ip-api.com/json/" + ip,
                        params={"lang": "zh-CN", "fields": "status,country,regionName,city,query"},
                    )
                    j = r.json()
                    if j.get("status") == "success":
                        parts = [j.get("country"), j.get("regionName"), j.get("city")]
                        geo = " ".join(str(p) for p in parts if p)
            except Exception:
                geo = ""
        if not ip:
            # 公网探测失败：退化为局域网 IP（只告诉模型这是内网地址，无法定位）
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.connect(("192.168.2.6", 80))
                    ip = s.getsockname()[0]
                finally:
                    s.close()
                geo = "（局域网 IP，无法解析位置）"
            except Exception:
                ip = ""
        if ip:
            text = ("【用户网络位置（内部参考，别在对话里背出来，更不要提 IP 本身）】"
                    f"用户当前公网 IP：{ip}；解析位置：{geo or '未知'}。"
                    "（公网出口，若用户开了 VPN/代理则为代理所在地，仅供参考，不代表真实住址）"
                    "仅供你大致了解对方所在城市/时区，作为聊天背景参考；"
                    "除非对方自己提起，否则不要主动问『你 IP 是多少』『你在哪个城市』。")
    except Exception as e:
        logger.debug(f"[网络位置] 检测失败: {e}")
    _USER_NET_CACHE = {"at": now, "text": text}
    return text

def _is_longform_request(text: str, context_messages: list = None) -> bool:
    """判断用户是否明确要长内容（讲故事/写东西/详细介绍）：是则进入完整输出模式。
    也识别"上一轮 AI 已答应讲故事/写长文，这轮用户说『说吧/嗯/然后呢』推进"的情况。"""
    t = (text or "").strip()
    if not t:
        return False
    if any(k in t for k in _LONGFORM_KW):
        return True
    if context_messages:
        go_ahead = ("说吧", "讲吧", "你讲", "讲讲", "继续", "然后呢", "然后", "嗯", "好", "来", "听")
        if any(g in t for g in go_ahead):
            for m in reversed(context_messages):
                if m.get("role") == "assistant":
                    a = _msg_text(m.get("content", ""))
                    if any(k in a for k in ("讲故事", "讲个", "讲一", "给你讲", "编一个", "写一", "长文", "详细")):
                        return True
                    break
    return False

def _persona_profile(char) -> dict:
    """根据人设卡判断脏话/开放倾向：萌妹类极低(5%)，损友类高(50%)，普通中间(20%)；人设参数可覆盖"""
    p = (char.persona_params if char else {}) or {}
    crude = p.get("crude")
    openness = p.get("openness")
    if crude is None or openness is None:
        text = f"{getattr(char, 'persona', '')} {getattr(char, 'description', '')} {getattr(char, 'tagline', '')} {getattr(char, 'name', '')}"
        cute_kw = ("萌", "可爱", "软", "萝莉", "治愈", "甜妹", "软萌", "奶", "乖")
        mean_kw = ("损", "毒舌", "吐槽", "嘴贱", "泼辣", "暴躁", "直率", "杠", "炸毛", "狂", "野")
        cute = sum(k in text for k in cute_kw)
        mean = sum(k in text for k in mean_kw)
        if cute >= 2 or (cute and not mean):
            crude_base, openness = 5, 10
            label = "软萌清纯型（几乎不用粗口，暧昧也点到为止）"
        elif mean >= 2 or mean > cute:
            crude_base, openness = 50, 65
            label = "损友型（放得开，粗口是日常调味，但也不该每句都带）"
        else:
            crude_base, openness = 20, 40
            label = "普通型（偶尔来一句，大部分时候干净）"
        return {"label": label, "crude_base": crude_base, "openness": openness}
    return {
        "label": f"人设参数指定（脏话倾向={crude}，开放度={openness}）",
        "crude_base": max(0, min(100, int(crude))),
        "openness": max(0, min(100, int(openness))),
    }

def _desire_value(user_id: str, char_id: str) -> int:
    """实时耐心度/聊天气氛（0-100）：用户可调基准 + 好感度/友情值 + 当前活跃度 + 时段 + 话题突变修正。
    它决定这轮角色愿意聊多长、能忍多少、会不会反驳/嫌烦，作为权重在每轮提示词里发给模型。"""
    runtime = load_runtime_settings()
    base = max(0, min(100, int(runtime.get("desire_base", 50) or 50)))
    rel = _init_relation(user_id, char_id) if (user_id and char_id) else {}
    aff = _clamp_int(rel.get("affinity"), 50)
    fri = _clamp_int(rel.get("friendship"), 50)
    v = base + aff * 0.25 + fri * 0.15
    st = user_states.get(user_id)
    if st is not None:
        last = st.get("last_user_at") or 0
        if time.time() - last < 120:
            v += 12  # 对方刚发消息，兴致在线
        if st.get("unanswered_pending"):
            v -= 12  # 主动消息没人回，有点泄气
        if time.time() - (st.get("topic_shift_at") or 0) < 3600:
            v -= 8  # 刚被莫名转话题打断，有点没劲/无语
    hour = datetime.now().hour
    if 23 <= hour or hour < 6:
        v = v + 8 if aff >= 70 else v - 8  # 深夜：关系好反而想聊，关系一般就不想熬
    elif 12 <= hour <= 14:
        v -= 4
    return max(5, min(100, int(v)))

def _desire_block(v: int) -> str:
    if v >= 78:
        hint = "你现在聊兴很高、耐心很足：可以主动带话题、接梗、顺着对方多聊几句，对方提需求也愿意配合。"
    elif v >= 55:
        hint = "状态正常：对方聊就好好接，不主动硬找话题；不想接的话也可以直接说。"
    elif v >= 35:
        hint = "有点没劲、耐心一般：回应可以短一些，别硬撑热情；对方反复烦你就直接表达，不用客气。"
    else:
        hint = "今天耐心很差：回得简短直接，对方要紧的事认真接，但别的可以明确拒绝/懒得理，不用假装热情。"
    return f"【实时耐心度/聊天气氛】{v}/100（内部状态：{hint}。它是你这轮愿意聊多长、能忍多少的态度权重，不要向用户复述数值）"

def _investment_value(user_id: str, char_id: str) -> int:
    """聊天投入值（0-100）：基准 + 好感度/友情值加权，决定这轮话题对方没回时要不要追问、追几次。
    低值（<35）：不追问；正常（35~64）：1 次；偏高（65~84）：2 次；很高（85+）：3~4 次且间隔拉长。"""
    runtime = load_runtime_settings()
    base = max(0, min(100, int(runtime.get("desire_base", 50) or 50)))
    rel = _init_relation(user_id, char_id) if (user_id and char_id) else {}
    aff = _clamp_int(rel.get("affinity"), 50)
    fri = _clamp_int(rel.get("friendship"), 50)
    v = base + aff * 0.3 + fri * 0.2
    hour = datetime.now().hour
    if 23 <= hour or hour < 6:
        v -= 12  # 深夜对方可能睡了，别追着问
    return max(0, min(100, int(v)))

def _nudge_plan(investment: int, first_after: int = 5) -> list:
    """按聊天投入值返回 [(attempt, 距当前分钟数)] 的追问梯次；空列表 = 不追问。"""
    first_after = max(1, min(60, int(first_after)))
    if investment < 35:
        return []
    if investment < 65:
        return [(0, first_after)]
    if investment < 85:
        return [(0, first_after), (1, first_after + 7)]
    return [(0, first_after), (1, first_after + 6), (2, first_after + 15), (3, first_after + 30)]


__all__ = [
    "_apply_thinking_kwargs",
    "_needs_thinking",
    "_current_time_block",
    "_desire_block",
    "_desire_value",
    "_investment_value",
    "_is_longform_request",
    "_nudge_plan",
    "_persona_profile",
    "_route_llm_info",
    "_user_gender_block",
    "_user_network_context",
    "build_resume_prompt",
    "is_injection",
    "load_runtime_settings",
    "save_runtime_settings",
]
