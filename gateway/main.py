"""
栖语 (Qiyu) - OpenAI 兼容适配网关 v2
支持角色选择、意图路由、Temperature 动态调节
"""

import os
import json
import uuid
import asyncio
import urllib.parse
from typing import AsyncGenerator, Optional, Any
from datetime import datetime

import httpx
from fastapi import FastAPI, HTTPException, Header, Request, Query
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from loguru import logger

# 加载内部模块
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from characters import get_character_manager, Character
from gateway.router import get_router, IntentType, RouteResult
from config import get_config
from rag.tags import get_tag_manager, MemoryTagManager

# ============ 配置 ============
# 默认只听本机：网关承载 OpenAI 兼容接口，默认口令等同于没有口令。
# 要在局域网用，请显式设 GATEWAY_HOST=0.0.0.0 并配置一个真实 GATEWAY_API_KEY。
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8000"))
_DEFAULT_GATEWAY_KEY = "ai-companion-key-change-me"
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", _DEFAULT_GATEWAY_KEY)

LETTA_BASE_URL = os.getenv("LETTA_BASE_URL", "http://127.0.0.1:8283")
LETTA_ADMIN_TOKEN = os.getenv("LETTA_ADMIN_TOKEN", "")

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://192.168.2.6:8081/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.6-35b-a3b-uncensored-heretic")

DEFAULT_CHARACTER = os.getenv("DEFAULT_CHARACTER", "xiaoban")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# ============ 数据模型 ============

class ChatMessage(BaseModel):
    role: str
    content: str
    name: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    model: str = LLM_MODEL
    messages: list[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    top_p: Optional[float] = 1.0
    user: Optional[str] = "default_user"

class CharacterSelectRequest(BaseModel):
    user_id: str = "default_user"
    character_id: str

class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(datetime.now().timestamp()))
    owned_by: str = "ai-companion"


# ============ Letta 客户端 ============

class LettaClient:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=120.0, follow_redirects=True
        )
        self._agents: dict[str, str] = {}
        self._agent_configs: dict[str, dict] = {}
    
    async def close(self):
        await self.client.aclose()
    
    async def list_agents(self) -> list[dict]:
        resp = await self.client.get("/v1/agents")
        resp.raise_for_status()
        return resp.json()
    
    async def get_agent(self, agent_id: str) -> Optional[dict]:
        try:
            resp = await self.client.get(f"/v1/agents/{agent_id}")
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.warning(f"获取 agent 失败: {e}")
        return None
    
    async def create_agent(
        self, name: str, persona: str, human: str = "",
        model: str = LLM_MODEL, context_window: int = 8192,
        temperature: float = 0.7,
    ) -> str:
        """创建 Agent（Letta >=0.16 API：memory_blocks + llm_config）"""
        payload = {
            "name": name,
            "description": f"栖语 角色 Agent: {name}",
            "memory_blocks": [
                {"label": "persona", "value": persona, "limit": 2000, "description": "角色人设"},
                {"label": "human", "value": human or "用户是我的好朋友。", "limit": 1000, "description": "用户信息"},
            ],
            "include_base_tools": True,
            "llm_config": {
                "model": model,
                "model_endpoint_type": "openai",
                "model_endpoint": LLM_BASE_URL,
                "model_wrapper": None,
                "context_window": context_window,
                "temperature": temperature,
            },
        }
        resp = await self.client.post("/v1/agents", json=payload, timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        agent_id = data.get("id") or data.get("agent_id")
        logger.info(f"创建 Agent 成功: {agent_id} ({name}) [temp={temperature}]")
        return agent_id
    
    async def send_message(self, agent_id: str, message: str) -> str:
        payload = {
            "messages": [{"role": "user", "content": message}],
            "streaming": False,
        }
        resp = await self.client.post(
            f"/v1/agents/{agent_id}/messages", json=payload, timeout=180.0
        )
        resp.raise_for_status()
        data = resp.json()
        return self._extract_reply(data)

    @staticmethod
    def _extract_reply(data) -> str:
        """从 LettaResponse 中提取助手回复文本"""
        messages = data.get("messages", []) if isinstance(data, dict) else data
        if not isinstance(messages, list) or not messages:
            return str(data)

        def _text_of(content) -> str:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text" and block.get("text"):
                            parts.append(block["text"])
                        elif block.get("type") == "reasoning" and block.get("reasoning"):
                            parts.append(block["reasoning"])
                return "".join(parts)
            return ""

        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("message_type", "")
            if mtype == "assistant_message" or msg.get("role") == "assistant":
                text = _text_of(msg.get("content", "")).strip()
                if text:
                    return text
        # 兜底：取最后一条含文本的消息
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            text = _text_of(msg.get("content", "")).strip()
            if text:
                return text
        return str(data)
    
    async def send_message_stream(self, agent_id: str, message: str) -> AsyncGenerator[str, None]:
        full_text = await self.send_message(agent_id, message)
        import re
        sentences = re.split(r'([。！？\n]+)', full_text)
        chunks = []
        current = ""
        for s in sentences:
            current += s
            if len(current) >= 8 or s in '。！？\n':
                chunks.append(current)
                current = ""
        if current:
            chunks.append(current)
        for chunk in chunks:
            yield chunk
            await asyncio.sleep(0.05)


# ============ 网关应用 ============

app = FastAPI(
    title="栖语 Gateway",
    description="OpenAI 兼容 API 网关，支持角色选择、意图路由、Temperature 调节",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"http://127.0.0.1:{GATEWAY_PORT}", f"http://localhost:{GATEWAY_PORT}"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件服务（角色选择页面）
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

letta: Optional[LettaClient] = None
char_mgr = get_character_manager()
router = get_router()


def verify_api_key(authorization: Optional[str] = None) -> bool:
    if not GATEWAY_API_KEY or GATEWAY_API_KEY == _DEFAULT_GATEWAY_KEY:
        return True
    if not authorization:
        return False
    key = authorization.replace("Bearer ", "").strip()
    return key == GATEWAY_API_KEY


@app.middleware("http")
async def guard_unconfigured_gateway(request, call_next):
    """未配置真实 Key 时，只允许本机访问。

    默认口令是公开的占位符，等于没有鉴权；此时即使有人把 GATEWAY_HOST 设成
    0.0.0.0，也不该让同网段直接调用。配了真实 Key 的走各端点的 verify_api_key。
    """
    if not GATEWAY_API_KEY or GATEWAY_API_KEY == _DEFAULT_GATEWAY_KEY:
        host = (request.client.host if request.client else "") or ""
        if host not in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
            return JSONResponse(status_code=403,
                                content={"detail": "网关未配置 API Key，只允许本机访问"})
    return await call_next(request)


@app.on_event("startup")
async def startup():
    global letta
    letta = LettaClient(LETTA_BASE_URL, LETTA_ADMIN_TOKEN)
    logger.info("=" * 60)
    logger.info("栖语 Gateway v2.0 启动")
    logger.info("=" * 60)
    logger.info(f"网关地址: http://{GATEWAY_HOST}:{GATEWAY_PORT}")
    logger.info(f"角色选择页: http://{GATEWAY_HOST}:{GATEWAY_PORT}/")
    logger.info(f"Letta Server: {LETTA_BASE_URL}")
    logger.info(f"LLM Backend: {LLM_BASE_URL} | Model: {LLM_MODEL}")
    logger.info(f"已加载角色: {[c['id'] for c in char_mgr.list_characters()]}")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown():
    if letta:
        await letta.close()
    logger.info("网关关闭")


# ============ 根路径：角色选择页面 ============

@app.get("/")
async def root():
    """角色选择首页"""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "栖语 Gateway v2.0", "docs": "/docs"}


# ============ 角色管理 API ============

@app.get("/v1/characters")
async def list_characters():
    return {"object": "list", "data": char_mgr.list_characters()}


@app.get("/v1/characters/{char_id}")
async def get_character_detail(char_id: str):
    char = char_mgr.get_character(char_id)
    if not char:
        raise HTTPException(status_code=404, detail=f"角色 '{char_id}' 不存在")
    return {
        "id": char.id, "name": char.name, "tagline": char.tagline,
        "description": char.description, "temperature": char.temperature,
        "avatar_color": char.avatar_color, "speech_style": char.speech_style,
    }


@app.post("/v1/characters/select")
async def select_character(request: CharacterSelectRequest):
    if not letta:
        raise HTTPException(status_code=503, detail="Letta client not initialized")
    
    char = char_mgr.get_character(request.character_id)
    if not char:
        raise HTTPException(status_code=404, detail=f"角色 '{request.character_id}' 不存在")
    
    user_id = request.user_id
    
    # 清理旧的 agent 映射
    old_agent_id = letta._agents.get(user_id)
    if old_agent_id:
        del letta._agents[user_id]
    
    try:
        agent_id = await letta.create_agent(
            name=f"companion_{user_id}_{char.id}",
            persona=char.to_prompt(),
            human=char.human,
            model=LLM_MODEL,
            temperature=char.temperature,
        )
        letta._agents[user_id] = agent_id
        letta._agent_configs[agent_id] = {
            "character_id": char.id,
            "temperature": char.temperature,
        }
    except Exception as e:
        logger.error(f"创建 Agent 失败: {e}")
        raise HTTPException(status_code=500, detail=f"创建 Agent 失败: {e}")
    
    return {
        "success": True,
        "user_id": user_id,
        "character": {
            "id": char.id, "name": char.name,
            "tagline": char.tagline, "temperature": char.temperature,
        },
        "agent_id": agent_id,
        "message": f"已切换为「{char.name}」— {char.tagline}",
    }


class CharacterCreateRequest(BaseModel):
    id: Optional[str] = None
    name: str
    tagline: str = ""
    description: str = ""
    persona: Optional[str] = None
    human: Optional[str] = None
    temperature: Optional[float] = 0.7
    memory_prompt: str = ""
    speech_style: dict = {}
    avatar_color: Optional[str] = None
    keywords: list[str] = []


@app.post("/v1/characters")
async def create_character(request: CharacterCreateRequest):
    """创建新角色"""
    char = char_mgr.create_character(request.model_dump())
    return {"success": True, "character": char.to_dict()}


@app.delete("/v1/characters/{char_id}")
async def delete_character(char_id: str):
    """删除角色"""
    ok = char_mgr.delete_character(char_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"角色 '{char_id}' 不存在")
    return {"success": True, "deleted": char_id}


@app.post("/v1/llm/verify")
async def verify_llm():
    """验证 LLM 配置是否可用"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{LLM_BASE_URL}/models")
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("id") for m in data.get("data", [])]
                return {"success": True, "base_url": LLM_BASE_URL, "model": LLM_MODEL, "models": models}
            return {"success": False, "base_url": LLM_BASE_URL, "status": resp.status_code, "detail": resp.text[:300]}
    except Exception as e:
        return {"success": False, "base_url": LLM_BASE_URL, "error": str(e)}


# ============ RAG API ============

@app.get("/v1/rag/search")
async def rag_search(q: str = Query(...), top_k: int = Query(5, ge=1, le=20)):
    """RAG 知识库语义搜索"""
    try:
        from rag.indexer import RAGIndexer
        indexer = RAGIndexer()
        results = indexer.search(q, top_k=top_k)
        return {"query": q, "count": len(results), "data": results}
    except Exception as e:
        logger.warning(f"RAG 搜索失败: {e}")
        return {"query": q, "count": 0, "data": [], "error": str(e)}


@app.get("/v1/rag/documents")
async def rag_documents(limit: int = Query(100, ge=1, le=1000)):
    """列出已索引文档"""
    try:
        from rag.indexer import RAGIndexer
        indexer = RAGIndexer()
        docs = indexer.list_documents(limit)
        return {"count": len(docs), "data": docs}
    except Exception as e:
        logger.warning(f"列出文档失败: {e}")
        return {"count": 0, "data": [], "error": str(e)}


class RAGIndexRequest(BaseModel):
    path: str


@app.post("/v1/rag/index")
async def rag_index(request: RAGIndexRequest):
    """手动索引文档/目录"""
    try:
        from rag.indexer import RAGIndexer
        indexer = RAGIndexer()
        if os.path.isdir(request.path):
            indexed = indexer.index_directory(request.path)
            return {"success": True, "path": request.path, "indexed": indexed}
        ok = indexer.index_file(request.path)
        return {"success": ok, "path": request.path}
    except Exception as e:
        logger.error(f"索引失败: {e}")
        raise HTTPException(status_code=500, detail=f"索引失败: {e}")


# ============ 模型 API ============

@app.get("/v1/models")
async def list_models():
    """列出可用模型（角色作为模型展示）"""
    models = [ModelInfo(id=LLM_MODEL)]
    for char_info in char_mgr.list_characters():
        models.append(ModelInfo(
            id=char_info["id"],
            owned_by=f"ai-companion/{char_info['name']}",
        ))
    return {"object": "list", "data": models}


# ============ 核心聊天 API ============

@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    authorization: Optional[str] = Header(None),
    x_character: Optional[str] = Header(None),
    x_temperature: Optional[str] = Header(None),
):
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not letta:
        raise HTTPException(status_code=503, detail="Letta client not initialized")
    
    user_id = request.user or "default_user"
    
    # 1. 角色选择：请求头 > model字段 > 默认值
    character_id = x_character or DEFAULT_CHARACTER
    if request.model and char_mgr.get_character(request.model):
        character_id = request.model
    
    char = char_mgr.get_character(character_id)
    if not char:
        char = char_mgr.get_default()
    if not char:
        raise HTTPException(status_code=404, detail="暂无可用角色，请先在网关创建角色")
    character_id = char.id
    
    # 2. Temperature 决策：请求 > 请求头 > 角色推荐值
    temperature = request.temperature
    if temperature is None:
        try:
            temperature = float(x_temperature) if x_temperature else None
        except ValueError:
            temperature = None
    if temperature is None:
        temperature = char.temperature
    temperature = max(0.0, min(2.0, temperature))
    
    # 3. 解析 messages
    system_prompt = ""
    user_message = ""
    for msg in request.messages:
        if msg.role == "system":
            system_prompt = msg.content
        elif msg.role == "user":
            user_message = msg.content
    if not user_message and request.messages:
        user_message = request.messages[-1].content
    
    # 4. 意图路由分析
    route_result = router.route(user_message)
    logger.info(f"[{user_id}] 角色: {char.id} | 意图: {route_result.intent.value} | 置信度: {route_result.confidence:.2f} | RAG: {route_result.needs_rag} | 记忆: {route_result.needs_memory}")
    
    # 5. 前置处理：RAG / 记忆查询
    context_prefix = ""
    if route_result.needs_rag:
        try:
            from rag.indexer import RAGIndexer
            indexer = RAGIndexer()
            rag_results = indexer.search(user_message, top_k=3)
            if rag_results:
                context_prefix += "\n[相关知识]\n"
                for r in rag_results:
                    context_prefix += f"- {r['text'][:200]}...\n"
        except Exception as e:
            logger.warning(f"RAG 查询失败: {e}")
    
    if route_result.needs_memory:
        context_prefix += "\n[提示] 用户可能在问过去的事情，请搜索你的 archival memory。\n"
    
    final_message = user_message
    if context_prefix:
        final_message = f"{context_prefix}\n\n用户问题: {user_message}"
    
    # 6. 获取或创建 agent
    agent_id = letta._agents.get(user_id)
    if not agent_id:
        try:
            existing = await letta.list_agents()
            for agent in existing:
                if agent.get("name") == f"companion_{user_id}_{char.id}":
                    agent_id = agent.get("id")
                    break
        except Exception as e:
            logger.warning(f"列出 agents 失败: {e}")
    
    if not agent_id:
        persona = char.to_prompt()
        if system_prompt:
            persona = f"{persona}\n\n[额外指令]\n{system_prompt}"
        expected_name = f"companion_{user_id}_{char.id}"

        async def _find_agent_by_name() -> Optional[str]:
            try:
                for agent in await letta.list_agents():
                    if agent.get("name") == expected_name:
                        return agent.get("id")
            except Exception as e2:
                logger.warning(f"按名称查找 Agent 失败: {e2}")
            return None

        try:
            agent_id = await letta.create_agent(
                name=expected_name,
                persona=persona,
                human=char.human,
                model=LLM_MODEL,
                temperature=temperature,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                # 409 通常是同名 Agent 已存在（并发创建或数据库半提交），按名称复用
                logger.warning(f"创建 Agent 返回 409，尝试按名称复用: {e}")
                agent_id = await _find_agent_by_name()
                if not agent_id:
                    logger.error(f"创建 Agent 失败: {e}")
                    raise HTTPException(status_code=500, detail=f"创建 Agent 失败（409 且未找到同名 Agent）: {e}")
            else:
                logger.error(f"创建 Agent 失败: {e}")
                raise HTTPException(status_code=500, detail=f"创建 Agent 失败: {e}")
        except Exception as e:
            logger.error(f"创建 Agent 失败: {e}")
            raise HTTPException(status_code=500, detail=f"创建 Agent 失败: {e}")

        letta._agents[user_id] = agent_id
        letta._agent_configs[agent_id] = {
            "character_id": char.id,
            "temperature": temperature,
        }
    
    # 7. 生成响应
    response_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(datetime.now().timestamp())
    response_headers = {
        "X-Character": char.id,
        "X-Character-Name": urllib.parse.quote(char.name, safe=""),
        "X-Temperature": str(temperature),
    }
    
    if request.stream:
        async def generate_stream() -> AsyncGenerator[str, None]:
            try:
                async for chunk in letta.send_message_stream(agent_id, final_message):
                    data = {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": request.model,
                        "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': request.model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.error(f"流式生成失败: {e}")
                error_data = {"error": {"message": str(e), "type": "internal_error"}}
                yield f"data: {json.dumps(error_data)}\n\n"
        
        return StreamingResponse(
            generate_stream(), media_type="text/event-stream", headers=response_headers
        )
    else:
        try:
            reply = await letta.send_message(agent_id, final_message)
        except Exception as e:
            logger.error(f"Agent 调用失败: {e}")
            raise HTTPException(status_code=502, detail=f"Agent 调用失败: {e}")
        
        return JSONResponse(content={
            "id": response_id,
            "object": "chat.completion",
            "created": created,
            "model": request.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1},
        }, headers=response_headers)


@app.get("/v1/agents")
async def list_agents_api(authorization: Optional[str] = Header(None)):
    if not verify_api_key(authorization):
        raise HTTPException(status_code=401, detail="Invalid API key")
    agents = []
    if letta:
        for uid, aid in letta._agents.items():
            config = letta._agent_configs.get(aid, {})
            agents.append({
                "user_id": uid, "agent_id": aid,
                "character_id": config.get("character_id", "unknown"),
                "temperature": config.get("temperature", 0.7),
            })
    return {"agents": agents}


@app.get("/health")
async def health():
    return {
        "status": "ok", "version": "2.0.0",
        "letta_connected": letta is not None,
        "characters_loaded": len(char_mgr.list_characters()),
        "timestamp": datetime.now().isoformat(),
    }


# ============ 设置管理 API ============

class SettingUpdateRequest(BaseModel):
    key: str
    value: Any


@app.get("/v1/settings")
async def get_settings():
    """获取当前运行配置"""
    cfg = get_config()
    return cfg.settings


@app.post("/v1/settings")
async def update_setting(request: SettingUpdateRequest):
    """更新设置（热重载）"""
    cfg = get_config()
    cfg.set(request.key, request.value, persist=True)
    return {"success": True, "key": request.key, "value": request.value}


@app.post("/v1/settings/reload")
async def reload_settings():
    """重新加载配置文件"""
    cfg = get_config()
    cfg.reload()
    router.reload()  # 同时重载路由规则
    return {"success": True, "message": "配置已热重载"}


# ============ 记忆标签 API ============

class TagUpdateRequest(BaseModel):
    point_id: str
    tags: list[str]


class TagSearchRequest(BaseModel):
    tags: list[str]
    match_all: bool = False
    limit: int = 50


@app.get("/v1/tags")
async def list_all_tags():
    """列出所有标签"""
    mgr = get_tag_manager()
    return {"tags": mgr.list_all_tags()}


@app.post("/v1/tags/add")
async def add_tags(request: TagUpdateRequest):
    """为记忆添加标签"""
    mgr = get_tag_manager()
    success = mgr.add_tags(request.point_id, request.tags)
    return {"success": success, "point_id": request.point_id, "tags": request.tags}


@app.post("/v1/tags/remove")
async def remove_tags(request: TagUpdateRequest):
    """移除记忆标签"""
    mgr = get_tag_manager()
    success = mgr.remove_tags(request.point_id, request.tags)
    return {"success": success, "point_id": request.point_id, "removed": request.tags}


@app.post("/v1/tags/set")
async def set_tags(request: TagUpdateRequest):
    """设置记忆标签（覆盖）"""
    mgr = get_tag_manager()
    success = mgr.set_tags(request.point_id, request.tags)
    return {"success": success, "point_id": request.point_id, "tags": request.tags}


@app.post("/v1/tags/search")
async def search_by_tags(request: TagSearchRequest):
    """按标签搜索记忆"""
    mgr = get_tag_manager()
    results = mgr.search_by_tags(request.tags, request.match_all, request.limit)
    return {
        "count": len(results),
        "data": [
            {"id": r.id, "text": r.text, "file_name": r.file_name, "tags": r.tags}
            for r in results
        ],
    }


@app.get("/v1/tags/{point_id}")
async def get_point_tags(point_id: str):
    """获取单条记忆的标签"""
    mgr = get_tag_manager()
    tags = mgr.get_tags(point_id)
    return {"point_id": point_id, "tags": tags}


# ============ 路由规则 API ============

@app.get("/v1/routes")
async def get_routes():
    """获取当前路由规则"""
    cfg = get_config()
    return cfg.routes


@app.post("/v1/routes/reload")
async def reload_routes():
    """重新加载路由规则"""
    router.reload()
    return {"success": True, "message": "路由规则已热重载"}


# ============ 主入口 ============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=GATEWAY_HOST, port=GATEWAY_PORT, reload=False, log_level="info")
