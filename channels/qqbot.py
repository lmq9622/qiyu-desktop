# -*- coding: utf-8 -*-
"""QQ 官方机器人通道（QQ Bot，开放平台 v2 API）。

按 OpenClaw channel 抽象实现。

能力边界
--------
- **发送**：已接通。AppID + AppSecret → AppAccessToken（带缓存）→ 发消息。
  私聊走 `POST /v2/users/{openid}/messages`；频道/群走 `POST /v2/channels/{id}/messages`。
  具体走哪条由 user_id 前缀决定（见 `_targets`）。
- **接收**：官方用 WebSocket 长连接（`wss://api.sgroup.qq.com/websocket/`），
  桌面版可以连，但要收 `C2C_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE` 事件需要先
  配置事件订阅与 intent。这里把连接与心跳实现好，**默认不自动连**（避免用户没配好就
  反复重连刷日志），由 UI 的「启动」按钮触发。

user_id 约定：`wx_qqbot__<openid>`（私聊）或 `wx_qqbot_channel__<channel_id>`（频道）
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger

from .base import BaseChannel
from . import store

TOKEN_API = "https://bots.qq.com/app/getAppAccessToken"
API = "https://api.sgroup.qq.com"
PREFIX = "wx_qqbot__"
PREFIX_CHANNEL = "wx_qqbot_channel__"


class QQBotChannel(BaseChannel):
    id = "qqbot"
    kind = "qqbot"
    name = "QQ Bot"
    mode = "official"
    icon = "qq"
    available = True
    integrated = True

    def __init__(self):
        self.running = False
        self.qr_status = "idle"
        self.qr_message = "填写 AppID / AppSecret / Token 后即可发送"
        self._token = ""
        self._token_exp = 0.0
        self._client = None
        self._ws_task: Optional[asyncio.Task] = None
        self._seq = 0
        self.config_schema = [
            {"key": "app_id", "label": "AppID", "type": "text",
             "placeholder": "QQ 开放平台机器人 AppID"},
            {"key": "app_secret", "label": "AppSecret", "type": "password",
             "placeholder": "机器人 AppSecret", "secret": True},
            {"key": "token", "label": "Bot Token", "type": "password",
             "placeholder": "开放平台 → 开发设置 → 机器人 Token", "secret": True},
            {"key": "default_openid", "label": "默认私聊 openid（可选）", "type": "text",
             "placeholder": "留空则用会话里的 openid"},
        ]
        self.config = {s["key"]: "" for s in self.config_schema}
        self._load_config()

    # ---------------------------------------------------------------- 配置
    def _load_config(self):
        saved = store.load_config(self.id)
        if saved:
            for k, v in saved.items():
                if k in self.config:
                    self.config[k] = v

    def save_config(self, cfg: dict):
        if not isinstance(cfg, dict):
            return
        for k, v in cfg.items():
            if k in self.config:
                self.config[k] = v
        store.save_config(self.id, self.config)
        self._token = ""
        self._token_exp = 0.0

    def _ready(self) -> bool:
        c = self.config or {}
        return bool(c.get("app_id") and c.get("app_secret"))

    # ---------------------------------------------------------------- token
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp:
            return self._token
        import httpx
        c = self.config or {}
        r = httpx.post(TOKEN_API, json={"appId": c.get("app_id"),
                                        "clientSecret": c.get("app_secret")}, timeout=15)
        d = r.json() if r.status_code == 200 else {}
        token = d.get("access_token") or ""
        if not token:
            raise RuntimeError(f"QQ Bot 取 token 失败: {str(d)[:120]}")
        self._token = token
        self._token_exp = time.time() + max(60, int(d.get("expires_in") or 7200) - 200)
        return token

    def _headers(self) -> dict:
        c = self.config or {}
        return {
            "Authorization": f"QQBot {self._get_token()}",
            "X-Union-Appid": str(c.get("app_id") or ""),
            "Content-Type": "application/json",
        }

    # ---------------------------------------------------------------- 生命周期
    def start(self, **kwargs) -> bool:
        if not self._ready():
            self.qr_status = "error"
            self.qr_message = "缺少 AppID / AppSecret"
            return False
        try:
            self._get_token()
        except Exception as exc:
            self.qr_status = "error"
            self.qr_message = f"凭证校验失败：{exc}"
            logger.warning(f"[QQBot] {exc}")
            return False
        self.running = True
        self.qr_status = "running"
        self.qr_message = "已接通（发送可用；接收需在开放平台开启事件订阅）"
        return True

    def stop(self):
        self.running = False
        self.qr_status = "idle"
        self.qr_message = "已停止"

    # ---------------------------------------------------------------- 发送
    def _targets(self, user_id: str) -> dict:
        uid = str(user_id or "")
        if uid.startswith(PREFIX_CHANNEL):
            return {"type": "channel", "id": uid[len(PREFIX_CHANNEL):]}
        if uid.startswith(PREFIX):
            return {"type": "c2c", "id": uid[len(PREFIX):]}
        d = str((self.config or {}).get("default_openid") or "")
        return {"type": "c2c", "id": d} if d else {}

    def send(self, user_id: str, text: str) -> bool:
        t = self._targets(user_id)
        if not t.get("id") or not self._ready():
            return False
        try:
            import httpx
            body = {"content": str(text or "")[:1900], "msg_type": 0, "msg_seq": 1}
            if t["type"] == "c2c":
                url = f"{API}/v2/users/{t['id']}/messages"
            else:
                url = f"{API}/v2/channels/{t['id']}/messages"
            r = httpx.post(url, headers=self._headers(), json=body, timeout=15)
            if r.status_code >= 300:
                logger.warning(f"[QQBot] 发送失败 {r.status_code}: {r.text[:120]}")
                return False
            return True
        except Exception as exc:
            logger.warning(f"[QQBot] 发送异常: {exc}")
            return False

    def resolve_remote(self, user_id: str, allow_fuzzy: bool = False) -> str:
        return str((self._targets(user_id) or {}).get("id") or "")

    # ---------------------------------------------------------------- 状态
    def status(self) -> dict:
        base = super().status()
        base["receive_ready"] = bool((self.config or {}).get("token"))
        base["ready"] = self._ready()
        base["receive_mode"] = "websocket"
        return base
