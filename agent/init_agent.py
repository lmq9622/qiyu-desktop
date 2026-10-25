"""
栖语 (Qiyu) - Letta Agent 初始化脚本
用于预创建角色 Agent 或测试 Letta 连接
"""

import os
import sys
import json
import asyncio

import httpx
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

LETTA_BASE_URL = os.getenv("LETTA_BASE_URL", "http://127.0.0.1:8283")
LETTA_ADMIN_TOKEN = os.getenv("LETTA_ADMIN_TOKEN", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "your-model-name")
CHARACTERS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "characters")


def load_character(name: str) -> dict:
    path = os.path.join(CHARACTERS_DIR, f"{name}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    default_path = os.path.join(CHARACTERS_DIR, "default.json")
    with open(default_path, "r", encoding="utf-8") as f:
        return json.load(f)


async def check_letta_server() -> bool:
    """检查 Letta Server 是否运行"""
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            headers = {}
            if LETTA_ADMIN_TOKEN:
                headers["Authorization"] = f"Bearer {LETTA_ADMIN_TOKEN}"
            resp = await client.get(f"{LETTA_BASE_URL}/v1/agents", headers=headers)
            return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Letta Server 未响应: {e}")
        return False


async def init_agent(character_name: str = "default") -> str:
    """初始化角色 Agent"""
    char = load_character(character_name)
    
    headers = {"Content-Type": "application/json"}
    if LETTA_ADMIN_TOKEN:
        headers["Authorization"] = f"Bearer {LETTA_ADMIN_TOKEN}"
    
    async with httpx.AsyncClient(base_url=LETTA_BASE_URL, headers=headers, timeout=60.0, follow_redirects=True) as client:
        # 检查是否已存在同名 agent
        resp = await client.get("/v1/agents")
        resp.raise_for_status()
        agents = resp.json()
        
        target_name = f"companion_{character_name}"
        for agent in agents:
            if agent.get("name") == target_name:
                agent_id = agent.get("id")
                logger.info(f"Agent 已存在: {agent_id} ({target_name})")
                return agent_id
        
        # 创建新 agent
        payload = {
            "name": target_name,
            "description": f"栖语 角色 Agent: {character_name}",
            "memory_blocks": [
                {"label": "persona", "value": char.get("persona", ""), "limit": 2000, "description": "角色人设"},
                {"label": "human", "value": char.get("human", "用户是我的好朋友。"), "limit": 1000, "description": "用户信息"},
            ],
            "include_base_tools": True,
            "llm_config": {
                "model": LLM_MODEL,
                "model_endpoint_type": "openai",
                "model_endpoint": LLM_BASE_URL,
                "model_wrapper": None,
                "context_window": char.get("llm_config", {}).get("context_window", 8192),
                "temperature": char.get("llm_config", {}).get("temperature", 0.7),
            },
        }
        
        resp = await client.post("/v1/agents", json=payload)
        resp.raise_for_status()
        data = resp.json()
        agent_id = data.get("id") or data.get("agent_id")
        logger.success(f"创建 Agent 成功: {agent_id} ({target_name})")
        return agent_id


async def test_chat(agent_id: str, message: str = "你好，你是谁？"):
    """测试对话"""
    headers = {"Content-Type": "application/json"}
    if LETTA_ADMIN_TOKEN:
        headers["Authorization"] = f"Bearer {LETTA_ADMIN_TOKEN}"
    
    async with httpx.AsyncClient(base_url=LETTA_BASE_URL, headers=headers, timeout=120.0, follow_redirects=True) as client:
        payload = {
            "messages": [{"role": "user", "content": message}],
            "streaming": False,
        }
        
        logger.info(f"发送测试消息: {message}")
        resp = await client.post(f"/v1/agents/{agent_id}/messages", json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        # 解析回复
        reply = ""
        messages = data.get("messages", []) if isinstance(data, dict) else data
        if isinstance(messages, list):
            for msg in reversed(messages):
                if isinstance(msg, dict):
                    if msg.get("message_type") == "assistant_message" or msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            reply = content
                        elif isinstance(content, list):
                            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                            reply = "".join(parts)
                        if reply:
                            break
        
        logger.success(f"Agent 回复: {reply}")
        return reply


async def main():
    logger.info("=" * 50)
    logger.info("栖语 - Letta Agent 初始化")
    logger.info("=" * 50)
    
    # 检查 Letta Server
    logger.info(f"检查 Letta Server: {LETTA_BASE_URL}")
    if not await check_letta_server():
        logger.error("Letta Server 未运行！请先启动: letta server")
        print("\n请先在另一个终端运行: letta server")
        sys.exit(1)
    
    logger.success("Letta Server 连接正常")
    
    # 获取角色名
    character = sys.argv[1] if len(sys.argv) > 1 else "default"
    logger.info(f"加载角色: {character}")
    
    # 初始化 Agent
    agent_id = await init_agent(character)
    
    # 测试对话（可选）
    if "--test" in sys.argv:
        await test_chat(agent_id)
    
    logger.info("=" * 50)
    logger.info("初始化完成！")
    logger.info(f"Agent ID: {agent_id}")
    logger.info("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
