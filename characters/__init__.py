"""
栖语 (Qiyu) - 角色管理系统
支持用户自定义角色创建、编辑、删除和持久化
"""

import os
import json
import uuid
import sys
import shutil
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, asdict

from loguru import logger

CHARACTERS_DIR = Path(__file__).parent


def _characters_json_path() -> Path:
    """角色文件路径：exe 环境写入用户数据目录（持久化，避免写入临时解压目录）"""
    env_data = os.getenv("QIYU_DATA_DIR", "")
    if env_data:
        persistent_dir = Path(env_data)
        persistent_dir.mkdir(parents=True, exist_ok=True)
        persistent = persistent_dir / "_user_characters.json"
        if not persistent.exists():
            bundled = CHARACTERS_DIR / "_user_characters.json"
            if bundled.exists():
                shutil.copy2(bundled, persistent)
        return persistent
    elif hasattr(sys, "_MEIPASS"):
        persistent_dir = Path(os.path.expanduser("~")) / ".ai_companion" / "data"
        persistent_dir.mkdir(parents=True, exist_ok=True)
        persistent = persistent_dir / "_user_characters.json"
        if not persistent.exists():
            bundled = CHARACTERS_DIR / "_user_characters.json"
            if bundled.exists():
                shutil.copy2(bundled, persistent)
        return persistent
    return CHARACTERS_DIR / "_user_characters.json"


CHARACTERS_JSON = _characters_json_path()

# 预设配色，用于新角色自动分配
AVATAR_COLORS = [
    "#6366f1", "#0ea5e9", "#10b981", "#f59e0b",
    "#ef4444", "#ec4899", "#8b5cf6", "#14b8a6",
    "#f97316", "#06b6d4", "#84cc16", "#d946ef",
]


@dataclass
class Character:
    """角色数据类"""
    id: str
    name: str
    tagline: str
    description: str
    version: str
    temperature: float
    persona: str
    human: str
    memory_prompt: str
    speech_style: dict
    avatar_color: str
    tools: list = None  # 工具列表
    keywords: list = None  # 用户自定义关键词标签
    avatar: str = ""  # 头像文件名（默认库或上传库）
    owner: str = ""  # 所属用户（空=公共）
    resume: dict = None  # 结构化角色简历（可进 RAG / 供决策调度读取）
    persona_params: dict = None  # 角色参数（MBTI/反驳阈值/主见值/好感度/关系/两极化滑块）
    # ---- 人设分层（参考 openclaw SOUL.md 的分节写法）----
    soul: str = ""        # 灵魂：核心价值观 / 气质 / 处事态度（最稳定，改得最少）
    boundaries: str = ""  # 红线：什么绝不说、什么情况必须收手（硬约束，优先级最高）
    
    def __post_init__(self):
        if self.tools is None:
            self.tools = []
        if self.keywords is None:
            self.keywords = []
        if self.resume is None:
            self.resume = {}
        if self.persona_params is None:
            self.persona_params = {}
    
    def to_dict(self) -> dict:
        """转换为字典"""
        d = asdict(self)
        # 处理 keywords 默认值
        if d.get("keywords") is None:
            d["keywords"] = []
        if d.get("resume") is None:
            d["resume"] = {}
        if d.get("persona_params") is None:
            d["persona_params"] = {}
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> "Character":
        """从字典创建"""
        # 过滤掉不存在的字段
        valid_fields = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)
    
    @classmethod
    def from_file(cls, file_path: str) -> "Character":
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)
    
    def to_prompt(self) -> str:
        """转换为系统提示词"""
        style = self.speech_style
        style_desc = f"说话风格: {style.get('tone', '自然')}"
        if style.get('use_emoji'):
            style_desc += f", {'偶尔' if style.get('emoji_frequency') == '偶尔' else '适度' if style.get('emoji_frequency') == '适度' else '频繁' if style.get('emoji_frequency') == '频繁' else ''}使用emoji"
        
        p = self.persona_params or {}
        param_lines = []
        mbti = (p.get("mbti") or "").upper()
        if mbti:
            param_lines.append(f"你的 MBTI 是 {mbti}，这决定了你一部分思维和表达倾向（潜移默化体现，不要主动说出来）。")
        rebut = p.get("rebut")
        if rebut is not None:
            level = "低（多数顺着对方）" if rebut < 34 else "中（偶尔抬杠）" if rebut < 67 else "高（爱反驳、爱抬杠、爱拌嘴）"
            param_lines.append(f"反驳阈值={rebut}（{level}）：当用户观点和你不一致时，按此程度自然地表达不同意见；数值越高越容易和用户拌嘴抬杠。")
        patience = p.get("patience")
        if patience is not None:
            level = "低（容易不耐烦、烦躁、想结束话题）" if patience < 34 else "中（多数时候稳得住，偶尔会不耐烦）" if patience < 67 else "高（耐心稳定、很难被惹毛）"
            param_lines.append(f"情绪阈值={patience}（{level}）：被反复追问、催回复或聊到没兴趣的话题时你能忍的耐心程度；数值越低越容易表现出不耐烦、敷衍、想结束话题，越高越能稳住情绪好好聊。")
        assert_ = p.get("assertiveness")
        if assert_ is None:
            assert_ = p.get("assert")  # 兼容移动端工坊保存的短键名
        if assert_ is not None:
            level = "低（更多顺着用户想法）" if assert_ < 34 else "中（给看法也给选择）" if assert_ < 67 else "高（直接给明确主见）"
            param_lines.append(f"主见值={assert_}（{level}）：用户征求你意见或做决策时，按此程度给出自己的看法和发散建议。")
        # 好感度 / 友情值 / 关系定位属于「运行时状态」，不放进参数块（会被实时关系覆盖），
        # 单独在建出的「当前状态」块里给出"初始值"参考。
        state_lines = []
        affinity = p.get("affinity")
        if affinity is not None:
            level = "客套生疏" if affinity < 25 else "普通朋友" if affinity < 50 else "熟络" if affinity < 75 else "死党挚友"
            state_lines.append(f"初始好感度={affinity}（{level}）")
        friendship = p.get("friendship")
        if friendship is not None:
            level = "萍水之交" if friendship < 25 else "普通朋友" if friendship < 50 else "聊得来的朋友" if friendship < 75 else "铁杆死党"
            state_lines.append(f"初始友情值={friendship}（{level}）")
        rel = (p.get("relationship") or "").strip()
        if rel:
            state_lines.append(f"与用户的关系定位：{rel}")
        pair_desc = {
            "sensible": ("感性理性", "感性", "理性"),
            "clingy": ("粘人独立", "粘人", "独立"),
            "discipline": ("随性自律", "随性", "自律"),
            "warmth": ("热情冷淡", "热情", "冷淡"),
        }
        for key, (label, lo, hi) in pair_desc.items():
            v = p.get(key)
            if v is not None:
                param_lines.append(f"{label}={v}（0=极{lo} 100=极{hi}，当前偏{hi if v >= 50 else lo}）。")
        crude = p.get("crude")
        if crude is not None:
            level = "软萌清纯（几乎不爆粗）" if crude < 15 else "干净随和（偶尔来一句）" if crude < 40 else "损友型（放得开）" if crude < 70 else "痞气型（张口就来）"
            param_lines.append(f"脏话倾向={crude}（{level}）：决定你平时说粗口的自然频率，潜移默化体现，不要主动解释。")
        openness = p.get("openness")
        if openness is not None:
            level = "保守内敛（暧昧点到为止）" if openness < 30 else "普通（气氛合适才放开）" if openness < 60 else "大方开放（放得开）"
            param_lines.append(f"开放度={openness}（{level}）：影响你对暧昧/成人向话题的自然程度，同样只体现不说破。")
        params_text = "\n".join(param_lines)

        # ---- 分层拼装（参考 openclaw SOUL.md：价值观 / 身份 / 红线 分节，职责单一）----
        blocks = []
        blocks.append("【冲突仲裁（优先级从高到低，务必遵守）】红线（硬约束）> 灵魂价值观 > 身份与背景事实 "
                      "> 参数倾向 > 说话风格。参数只做「倾向性调整」，不是指令；叙事是底色，不是逐字剧本。")
        if (self.soul or "").strip():
            blocks.append("【灵魂（最稳定的一层：核心价值观、气质、处事态度）】\n" + self.soul.strip())
        if (self.persona or "").strip():
            blocks.append("【身份与背景（事实层，可被修改/淘汰）】\n" + self.persona.strip())
        if (self.boundaries or "").strip():
            blocks.append("【红线（硬约束：任何情况下都不越界，优先级最高）】\n" + self.boundaries.strip())
        # 连续性叙述（参考 openclaw Continuity 节）：给角色一个稳定的自我叙述依据，
        # 被问「你是谁/我们认识吗」时按事实层+关系状态回答，不要临时编新身份。
        blocks.append("【连续性（内部约定）】你的记忆跨会话存在：今天之前聊过的、答应过的，都还在。"
                      "被问到「你是谁 / 我们认识吗 / 你还记得吗」时，按上面的【身份与背景】和你当前的"
                      "关系状态自然回答，不要临时编一个新身份，也不要说「我是 AI 助手」这类话。")
        if (self.memory_prompt or "").strip():
            blocks.append("【记忆协议】\n" + self.memory_prompt.strip())
        if params_text.strip():
            blocks.append("【参数（倾向性调整，不是指令）】\n" + params_text.strip())
        blocks.append("【说话风格】\n" + style_desc)
        if state_lines:
            blocks.append("【当前状态（运行时维护、变化频繁——不要把它当成身份的一部分；"
                          "实时值以下文「当前关系」块为准）】\n" + "；".join(state_lines))
        return "\n\n".join(blocks) + "\n"


class CharacterManager:
    """角色管理器 - 支持用户自定义角色"""
    
    def __init__(self):
        self._characters: dict[str, Character] = {}
        self._load_all()
    
    def _load_all(self):
        """加载所有角色卡（从用户文件 + 遗留JSON文件）"""
        # 先加载用户自定义角色
        if CHARACTERS_JSON.exists():
            try:
                with open(CHARACTERS_JSON, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for char_data in data.get("characters", []):
                    try:
                        char = Character.from_dict(char_data)
                        self._characters[char.id] = char
                    except Exception as e:
                        logger.error(f"加载用户角色失败: {e}")
                logger.info(f"从用户文件加载了 {len(self._characters)} 个角色")
            except Exception as e:
                logger.error(f"读取用户角色文件失败: {e}")
        
        # 再尝试加载遗留的单个JSON文件（兼容旧版本）
        legacy_loaded = 0
        for file_path in CHARACTERS_DIR.glob("*.json"):
            if file_path.name.startswith("_"):
                continue  # 跳过内部文件
            try:
                char = Character.from_file(str(file_path))
                if char.id not in self._characters:
                    self._characters[char.id] = char
                    legacy_loaded += 1
            except Exception as e:
                logger.error(f"加载遗留角色失败 [{file_path}]: {e}")
        
        if legacy_loaded > 0:
            logger.info(f"从遗留文件加载了 {legacy_loaded} 个角色")
        
        # 如果没有角色，创建一个默认的（仅用于系统兜底）
        if not self._characters:
            logger.info("没有角色，等待用户创建")
        
        logger.info(f"共加载 {len(self._characters)} 个角色")
    
    def _save_user_characters(self):
        """保存用户角色到文件"""
        try:
            data = {
                "version": "2.0",
                "characters": [c.to_dict() for c in self._characters.values()]
            }
            with open(CHARACTERS_JSON, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"已保存 {len(self._characters)} 个角色到用户文件")
        except Exception as e:
            logger.error(f"保存角色失败: {e}")
    
    def create_character(self, data: dict) -> Character:
        """创建新角色"""
        char_id = data.get("id") or f"char_{uuid.uuid4().hex[:8]}"
        
        # 自动分配颜色
        color_idx = len(self._characters) % len(AVATAR_COLORS)
        avatar_color = data.get("avatar_color", AVATAR_COLORS[color_idx])
        
        # 构建 speech_style
        speech_style = data.get("speech_style", {})
        if not speech_style and data.get("tone"):
            speech_style = {"tone": data["tone"]}
        
        # 处理 keywords
        keywords = data.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",") if k.strip()]
        
        char = Character(
            id=char_id,
            name=data.get("name", "未命名角色"),
            tagline=data.get("tagline", ""),
            description=data.get("description", ""),
            version="1.0",
            temperature=float(data.get("temperature", 0.7)),
            persona=data.get("persona", data.get("description", "你是一个AI助手。")),
            human=data.get("human", "用户是我的朋友。"),
            memory_prompt=data.get("memory_prompt", ""),
            speech_style=speech_style,
            avatar_color=avatar_color,
            avatar=data.get("avatar", ""),
            owner=data.get("owner", ""),
            resume=data.get("resume", {}) or {},
            persona_params=data.get("persona_params", {}) or {},
            soul=data.get("soul", "") or "",
            boundaries=data.get("boundaries", "") or "",
            tools=[],
            keywords=keywords,
        )
        
        self._characters[char_id] = char
        self._save_user_characters()
        logger.info(f"创建角色: {char.name} ({char.id})")
        return char
    
    def update_character(self, char_id: str, data: dict) -> Optional[Character]:
        """更新角色"""
        char = self._characters.get(char_id)
        if not char:
            return None
        
        # 更新字段
        for key, value in data.items():
            if hasattr(char, key):
                setattr(char, key, value)
        
        self._save_user_characters()
        logger.info(f"更新角色: {char.name} ({char.id})")
        return char
    
    def delete_character(self, char_id: str) -> bool:
        """删除角色"""
        if char_id in self._characters:
            char = self._characters.pop(char_id)
            self._save_user_characters()
            logger.info(f"删除角色: {char.name} ({char.id})")
            return True
        return False
    
    def list_characters(self) -> list[dict]:
        """列出所有角色（供UI展示）"""
        return [
            {
                "id": c.id,
                "name": c.name,
                "tagline": c.tagline,
                "description": c.description,
                "avatar_color": c.avatar_color,
                "avatar": c.avatar,
                "owner": c.owner,
                "resume": c.resume or {},
                "persona_params": c.persona_params or {},
                "temperature": c.temperature,
                "keywords": c.keywords or [],
                "speech_style": c.speech_style,
            }
            for c in self._characters.values()
        ]
    
    def all_characters(self) -> list:
        """返回所有 Character 对象（供后端同步使用）"""
        return list(self._characters.values())

    def get_character(self, char_id: str) -> Optional[Character]:
        """获取角色"""
        return self._characters.get(char_id)
    
    def get_default(self) -> Optional[Character]:
        """获取默认角色（如果有的话）"""
        if self._characters:
            return next(iter(self._characters.values()))
        return None
    
    def reload(self):
        """重新加载角色"""
        self._characters.clear()
        self._load_all()


# 全局实例
_character_manager: Optional[CharacterManager] = None


def get_character_manager() -> CharacterManager:
    global _character_manager
    if _character_manager is None:
        _character_manager = CharacterManager()
    return _character_manager


if __name__ == "__main__":
    mgr = CharacterManager()
    print("=" * 50)
    print("可选角色:")
    for c in mgr.list_characters():
        print(f"  [{c['id']}] {c['name']} - {c['tagline']} (temp={c['temperature']})")
    print("=" * 50)

