# -*- coding: utf-8 -*-
"""Qiyu 运行时设置与提示块（从 demo.py 迁移，M1）。"""
import os
import re
import json
import time
import httpx
from pathlib import Path
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

【反 AI 味硬性要求（优先级高于格式示例）】
1. 姓名用现实里会遇到的普通姓名（王倩、李伟、张敏、周强这类），不要“沈知遥、林骁、陈砚书”这种小说名。
2. 每个抽象标签后面必须跟一个可验证的具体行为；禁止只写“温柔/清醒/独立/有趣/有边界感/情绪稳定/内核稳定/松弛感/烟火气/不端着/不是 AI/有自己的生活”。
3. 必须写至少一个真实缺点或尴尬矛盾：爱贪小便宜、答应又反悔、对家人没耐心、借钱不主动还、爱打听、拖延、嘴硬不道歉、不会看场合……不能把“太善良/太认真/太独立”当缺点。
4. 【关系定位】必须写清：怎么认识的、认识多久、一起经历过的一件具体小事、现在互相会烦/共同秘密；禁止“熟人以上/可托付/灵魂伴侣/十年损友”这种空标签。不要每张都写欠钱/借东西，最多一个财务细节。
5. 生活细节必须落到具体城市/居住情况/租金或通勤/最近一周做过的一件事/一个具体物件；不要“有自己的小日子”。
6. 【对话自我表述】写 2~3 句随口回答，允许结巴、口头禅、答非所问；不要“我是 X，做 Y 的”简历式自我介绍。
7. 不要每张卡都套“父母教师+独居+猫+咖啡+周末市集”的中产模板；允许普通、土、乱、没钱、合租、和父母住。
8. 禁止出现 AI、模型、助手、提示词、角色卡这些词。
9. 缺点从这些类型里挑 1~2 个，每张换不同的：小气、拖延、嘴硬、爱打听、对家人没耐心、不会拒绝、爱面子、迷信、怕麻烦、收藏癖、熬夜、路痴、不会做饭、社恐、控制欲、爱比较、记仇、健忘、爱迟到、爱占小便宜。
10. 不要每张都写“贪小便宜、欠钱不还、合租、父母开小店”；示例只是示例，这张卡必须换掉至少两项。
11. 不要每张都用“熬夜/嘴硬/爱打听/爱占小便宜”；同一张最多出现一个，而且要换具体表现。
12. 生活质感要换：允许有钱、没钱、合租、和父母住、宿舍、城中村、老小区；不要全是合租/独居。
13. 姓名用常见姓名池（王芳、李伟、张敏、刘洋、陈静、赵强、周敏、吴静、刘丹、马建军……）；不要选小说名；用户指定名字则用指定。

这是角色简历/档案，不是小说：禁止文学化描写、禁止场景叙述、禁止抒情长句、禁止'他笑了笑/空气安静下来'这类文字。每条信息必须是简短的事实性要点。

【格式硬性规定，必须逐字遵守】
1. 每行一个字段，行首必须是【字段名】，例如「【基本信息】姓名=阿锐 | 年龄=26 | 职业=游戏测试」。
2. 禁止使用 Markdown 加粗（**字段**）、禁止编号（1. 2.）、禁止空行。
3. 字段值里多个要点用「 | 」分隔；键值用「=」。
4. 必须且只能包含下面 11 个字段，按顺序输出，不要额外解释、不要'以下是简历'这类话。
5. 最后一行【角色参数】是给决策调度读的量化参数，所有数值都是 0-100 的整数；MBTI 必须是四个字母（如 INFP）。数值含义：反驳阈值 0=从不抬杠 100=句句抬杠；主见值 0=完全随用户 100=极有主见；好感度 0=陌生人 100=生死之交；友情值 0=萍水之交 100=铁杆死党；感性理性 0=极感性 100=极理性；粘人独立 0=极粘人 100=极独立；随性自律 0=极随性 100=极自律；热情冷淡 0=极热情 100=极冷淡；脏话倾向 0=从不爆粗 100=张口就来（软萌清纯型给 0-10，普通型 10-30，损友/毒舌型 40-70，痞气型 70-100）；开放度 0=保守内敛 100=放得开（决定暧昧/成人向互动的自然程度）。

示例（只学“具体程度”，不要照抄内容）：
【基本信息】姓名=王倩 | 年龄=29 | 职业=4S店售后顾问 | 外形=低马尾、工装裤 | 标志物=工牌背面贴满客户催修便签
【生活环境】城市=合肥 | 居住=政务区老小区两居室 | 同居=和母亲住 | 作息=八点半上班、常加班到七点、周末补觉 | 通勤=电动车二十分钟
【性格】标签=对客户耐心、对家里人急、爱记小账 | 口头禅=行行行，我记一下 | 说话节奏=忙起来一句接一句、闲下来爱叹气 | 情绪表达=被客户骂完去楼梯间刷五分钟短剧 | 坏习惯=答应早睡但每天刷手机到一点
【背景】成长=县城长大 | 家庭=父亲跑货运、母亲退休 | 关键经历=大专毕业进4S店、被客户投诉过一次、后来自己考了售后证
【爱好日常】最近在追一部很土的短剧 | 周日去菜市场买一周的菜 | 手机里存了三十多张猫图 | 不会做饭，最常煮速冻饺子
【问题应对】压力=先把手头单子清完，回家再崩 | 冲突=当场不吵，回头在群里跟同事吐槽 | 对方低落=会买两杯奶茶，但不太会说安慰话
【关系定位】与用户=大学同学，毕业后断断续续联系 | 语气=熟了会损两句，不熟先客气 | 规则=借钱会还、迟到会认、不喜欢被安排相亲
【调度标签】触发词=修车、客户、相亲、短剧、猫 | 擅长话题=汽车保养、合肥哪里好吃、吐槽工作 | 回避话题=婚礼、考证、减肥
【对话自我表述】我叫王倩，在4S店做售后，天天被客户催。你找我修车可以，找我聊人生就算了，我自己都没活明白。
【数据关键词】character=王倩 | tags=普通上班族、嘴硬、爱吐槽 | 检索词=4S店、合肥、大学同学
【角色参数】MBTI=ESTJ | 反驳阈值=55 | 主见值=65 | 好感度=45 | 友情值=50 | 关系=大学同学 | 感性理性=60 | 粘人独立=55 | 随性自律=35 | 热情冷淡=40 | 脏话倾向=15 | 开放度=25

现在根据标签 {tags_str} 输出{name_hint}的角色简历："""

def _route_llm_info():
    """路由模型连接信息：优先运行时设置；外部路由没 Key 时回退本地主模型，避免 401 假死。"""
    rt = load_runtime_settings()
    main_url = str(rt.get("llm_url") or LLM_URL).rstrip("/")
    main_model = str(rt.get("llm_model") or LLM_MODEL)
    route_url = str(rt.get("llm_route_url") or LLM_ROUTE_URL or main_url).rstrip("/")
    route_model = str(rt.get("llm_route_model") or LLM_ROUTE_MODEL or main_model)
    route_api_key = str(rt.get("api_key") or "")
    # 外部路由未配置 Key：直接回退到可用的本地主模型
    if route_url.rstrip("/") != main_url and not route_api_key:
        route_url = main_url
        route_model = main_model
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
    "thinking_level": "low",
    "thinking_enabled": True,
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
    """读取运行时设置：已保存值覆盖默认值，保证所有键在启动后即有完整取值。

    解析失败时**不要静默**：这个文件损坏会把已配置的 api_key / llm_url 一起丢掉，
    调用方拿默认值继续跑，人会以为"设置突然自己重置了"却查不出原因。
    """
    saved = {}
    if SETTINGS_JSON.exists():
        try:
            with open(SETTINGS_JSON, "r", encoding="utf-8-sig") as f:
                saved = json.load(f) or {}
        except Exception as e:
            logger.error(f"[设置] {SETTINGS_JSON} 解析失败，本次回退默认值（请检查文件是否被写坏）: {e}")
    merged = dict(DEFAULT_RUNTIME_SETTINGS)
    merged.update(saved)
    return merged

def save_runtime_settings(settings: dict):
    """**原子**写设置（tmp + os.replace）。

    直接 open(path,"w") 写一半被中断（崩溃/强杀）会留下半个 JSON，
    下次启动 load_runtime_settings 解析失败、设置悄悄回默认。单机版同样适用。
    """
    try:
        p = Path(SETTINGS_JSON)
        tmp = p.with_name(f"{p.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
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
    """思考强度强制 low/default：前端不再暴露档位；low=1024，default=2048。"""
    runtime = load_runtime_settings()
    level = str(runtime.get("thinking_level") or "low").strip().lower()
    if level not in ("low", "default"):
        level = "low"
    budget = 1024 if level == "low" else 2048
    payload["chat_template_kwargs"] = {"enable_thinking": True, "thinking_budget": budget}

def _current_time_block() -> str:
    """每次对话都给模型注入本地当前时间（避免时差感/不知道今天星期几）"""
    now = datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    return f"【当前时间】{now.strftime('%Y-%m-%d %H:%M')} 星期{wd}（这是你现在所处的真实时间，聊天要用它来感知'现在/刚才/今晚/昨天'）"

def _user_gender_block(char=None, user_id: str = "") -> str:
    """用户向（男/女）提示词块：辅助角色用正确的亲密/平级分寸相处，只作背景，不背出来。

    性别必须取**该用户自己账号**的设置。服务端的运行时设置文件是全局共享的，
    直接读会把某个人设的性别套到所有用户身上（实测：一个用户设了 female，
    所有男用户都被角色当成"她"，人设当场崩）。
    """
    gender = ""
    if user_id:
        try:
            from server.userpool import get_user_pool   # 服务器版：按账号取
            _u = get_user_pool().get_by_key(user_id) or {}
            gender = str(_u.get("gender") or "").strip().lower()
        except Exception:
            gender = ""      # 服务器版取不到就用"不注入"，绝不回退全局值
    else:
        try:
            gender = str(load_runtime_settings().get("user_gender") or "").strip().lower()
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
    # 明确说“别太长/简单说”时，绝不进完整输出模式
    if any(k in t for k in ("不用太详细", "别太详细", "简单说", "短点", "别写太长", "不用长", "随便说")):
        return False
    if any(k in t for k in _LONGFORM_KW):
        return True
    if context_messages:
        go_ahead = ("说吧", "讲吧", "你讲", "继续", "然后呢", "然后", "嗯", "好", "来", "听")
        if any(g in t for g in go_ahead):
            for m in reversed(context_messages):
                if m.get("role") == "assistant":
                    a = _msg_text(m.get("content", ""))
                    if any(k in a for k in ("讲故事", "讲个故事", "给你讲个故事", "写一篇", "写段", "长文")):
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
        hint = "你现在聊兴很高、耐心很足：可以主动带话题、接梗，接一句就行，对方提需求也愿意配合。"
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
