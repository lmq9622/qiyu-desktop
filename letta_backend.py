"""
栖语 (Qiyu) - Letta 后端集成
=================================
把记忆流水线产出的记忆同步进本地 Letta Agent：
  产出: 对话摘要 / 用户事实（由 memory.compress_history 生成）
  存放: Letta Agent 的 human 记忆块 + 归档记忆（archival memory）
  调用: 检索归档记忆 + 核心记忆，注入下一次对话的系统提示词

Letta 不可用时自动降级，不影响主链路（MemoryManager）。
"""

import os
import re
import httpx
from loguru import logger


class LettaBackend:
    def __init__(self, base_url: str = "", token: str = "", llm_url: str = "", llm_model: str = ""):
        self.base_url = (base_url or os.getenv("LETTA_BASE_URL", "http://127.0.0.1:8283")).rstrip("/")
        self.token = token or os.getenv("LETTA_ADMIN_TOKEN", "")
        self.llm_url = (llm_url or os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1")).rstrip("/")
        self.llm_model = llm_model or os.getenv("LLM_MODEL", "your-model-name")
        self.available = False
        self._agent_cache: dict[str, str] = {}  # character_id -> agent_id
        self._http_client = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _http(self):
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self.base_url, headers=self._headers(), timeout=60.0, follow_redirects=True
            )
        return self._http_client

    async def close(self):
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def check(self) -> bool:
        """探测 Letta Server 是否可用"""
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as c:
                resp = await c.get(f"{self.base_url}/v1/agents", headers=self._headers())
                self.available = resp.status_code == 200
        except Exception as e:
            self.available = False
            logger.warning(f"Letta 未连接: {e}")
        if self.available:
            logger.success(f"Letta Server 已连接: {self.base_url}")
        return self.available

    async def list_agents(self) -> list[dict]:
        resp = await self._http().get("/v1/agents")
        resp.raise_for_status()
        return resp.json()

    async def get_agent(self, agent_id: str) -> dict | None:
        try:
            resp = await self._http().get(f"/v1/agents/{agent_id}")
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            return None
        return None

    async def ensure_agent(self, character_id: str, character_name: str, persona: str,
                           human: str = "") -> str | None:
        """为角色创建/复用 Letta Agent，返回 agent_id；失败返回 None"""
        if not self.available:
            return None
        if character_id in self._agent_cache:
            return self._agent_cache[character_id]
        target_name = f"qiyu_{character_id}"
        try:
            agents = await self.list_agents()
            for a in agents:
                if a.get("name") == target_name:
                    self._agent_cache[character_id] = a["id"]
                    logger.info(f"[Letta] 复用 Agent: {a['id']} ({target_name})")
                    return a["id"]
        except Exception as e:
            logger.warning(f"[Letta] 查询 Agent 失败: {e}")
            return None
        payload = {
            "name": target_name,
            "description": f"栖语 角色 Agent: {character_name}",
            "memory_blocks": [
                {"label": "persona", "value": (persona or "")[:2000], "limit": 2000, "description": "角色人设"},
                {"label": "human", "value": (human or "用户是我的好朋友。")[:1000], "limit": 1000, "description": "用户信息"},
            ],
            "include_base_tools": False,
            "llm_config": {
                "model": self.llm_model,
                "model_endpoint_type": "openai",
                "model_endpoint": self.llm_url,
                "model_wrapper": None,
                "context_window": 8192,
                "temperature": 0.7,
            },
        }
        try:
            resp = await self._http().post("/v1/agents", json=payload, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            agent_id = data.get("id") or data.get("agent_id")
            self._agent_cache[character_id] = agent_id
            logger.info(f"[Letta] 创建 Agent: {agent_id} ({target_name})")
            return agent_id
        except Exception as e:
            logger.warning(f"[Letta] 创建 Agent 失败: {e}")
            return None

    async def update_human_block(self, agent_id: str, human: str) -> bool:
        """更新 human 记忆块（用户档案）"""
        if not self.available or not human:
            return False
        try:
            resp = await self._http().patch(
                f"/v1/agents/{agent_id}/core-memory/blocks/human",
                json={"value": human[:1000]},
            )
            ok = resp.status_code in (200, 204)
            if ok:
                logger.info(f"[Letta] 已更新 human 记忆块: {human[:40]}...")
            return ok
        except Exception as e:
            logger.warning(f"[Letta] 更新 human 块失败: {e}")
            return False

    async def insert_archival(self, agent_id: str, text: str, tags: list[str] | None = None) -> bool:
        """插入归档记忆（对话摘要 / 用户事实）"""
        if not self.available or not text:
            return False
        try:
            payload = {"text": text[:2000]}
            if tags:
                payload["tags"] = tags
            resp = await self._http().post(
                f"/v1/agents/{agent_id}/archival-memory", json=payload
            )
            ok = resp.status_code in (200, 201, 204)
            if ok:
                logger.info(f"[Letta] 归档记忆已写入: {text[:40]}...")
            return ok
        except Exception as e:
            logger.warning(f"[Letta] 写入归档记忆失败: {e}")
            return False

    # 常见中文停用词/语气词，用于关键词提取
    _STOP_CHARS = set("的了吗呢吧啊哦嗯哈啥什么怎么我你他她它我们是这那有没有在不就也还都着过一下呀嘛哟诶喔噢")

    @staticmethod
    def _extract_search_items(data) -> list:
        if isinstance(data, dict):
            return data.get("results") or data.get("data") or []
        if isinstance(data, list):
            return data
        return []

    def _extract_keywords(self, query: str, limit: int = 6) -> list[str]:
        """从自然语句里拆出中文关键词（2/3 字组合），用于子串检索"""
        import re
        clean = re.sub(r"[\W_]+", "", query)
        chars = [c for c in clean if c not in self._STOP_CHARS]
        if len(chars) < 2:
            return [clean] if clean else []
        grams: list[str] = []
        for i in range(len(chars) - 2):
            grams.append("".join(chars[i:i + 3]))
        for i in range(len(chars) - 1):
            grams.append("".join(chars[i:i + 2]))
        # 长词优先（3字 > 2字），去重
        out: list[str] = []
        for g in grams:
            if g not in out:
                out.append(g)
            if len(out) >= limit:
                break
        return out

    async def _search_archival(self, agent_id: str, query: str, limit: int) -> list:
        """归档记忆检索：全文检索 -> 关键词分片检索 -> 最近归档兜底"""
        items: list = []
        if query:
            resp = await self._http().get(
                f"/v1/agents/{agent_id}/archival-memory/search",
                params={"query": query, "top_k": limit},
            )
            if resp.status_code == 200:
                items = self._extract_search_items(resp.json())
        # 中文无 embedding 时全文检索基本失效，退化为关键词分片检索
        if not items and query:
            for kw in self._extract_keywords(query):
                resp = await self._http().get(
                    f"/v1/agents/{agent_id}/archival-memory/search",
                    params={"query": kw, "top_k": 2},
                )
                if resp.status_code == 200:
                    items.extend(self._extract_search_items(resp.json()))
                if len(items) >= limit:
                    break
        # 兜底：拉最近归档，保证新记忆总能被引用
        if not items:
            resp = await self._http().get(
                f"/v1/agents/{agent_id}/archival-memory",
                params={"limit": limit},
            )
            if resp.status_code == 200:
                items = self._extract_search_items(resp.json())
        return items

    async def pull_context(self, character_id: str, query: str, limit: int = 4) -> str:
        """从 Letta 拉取记忆上下文（核心记忆 + 归档记忆检索）"""
        agent_id = self._agent_cache.get(character_id)
        if not self.available or not agent_id:
            return ""
        parts = []
        try:
            resp = await self._http().get(f"/v1/agents/{agent_id}/core-memory")
            if resp.status_code == 200:
                data = resp.json()
                blocks = {}
                if isinstance(data, dict):
                    if isinstance(data.get("blocks"), list):  # letta 0.16.x: {"blocks":[...]}
                        blocks = {b.get("label", ""): b for b in data["blocks"] if isinstance(b, dict)}
                    else:  # 旧版: {"memory":{"block_map":{...}}}
                        blocks = data.get("memory", {}).get("block_map", {}) or {}
                human = ""
                persona = ""
                for label in ("human", "persona"):
                    blk = blocks.get(label)
                    val = blk.get("value", "") if isinstance(blk, dict) else (str(blk) if blk else "")
                    if label == "human":
                        human = val
                    elif label == "persona":
                        persona = val
                if persona:
                    parts.append(f"【角色核心人设】{persona[:600]}")
                if human and human != "用户是我的好朋友。":
                    parts.append(f"【Letta 用户档案】{human[:600]}")
        except Exception as e:
            logger.warning(f"[Letta] 读取核心记忆失败: {e}")
        try:
            items = await self._search_archival(agent_id, query, limit)
            seen: set = set()
            for item in items:
                pid = item.get("id") if isinstance(item, dict) else None
                text = (item.get("content") or item.get("text") or "") if isinstance(item, dict) else str(item)
                if pid:
                    if pid in seen:
                        continue
                    seen.add(pid)
                if text:
                    parts.append(f"- {text[:300]}")
        except Exception as e:
            logger.warning(f"[Letta] 检索归档记忆失败: {e}")
        if not parts:
            return ""
        return "【Letta 记忆】\n" + "\n".join(parts)


_letta: LettaBackend | None = None


def get_letta_backend() -> LettaBackend:
    global _letta
    if _letta is None:
        _letta = LettaBackend()
    return _letta






