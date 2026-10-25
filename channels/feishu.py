# -*- coding: utf-8 -*-
"""飞书自建应用通道（Feishu / Lark）。

按 OpenClaw channel 抽象实现。

能力边界
--------
- **发送（应用身份）**：已接通。`app_id` + `app_secret` → `tenant_access_token`
  → `POST /open-apis/im/v1/messages?receive_id_type=open_id|chat_id`。
- **发送（自定义机器人 Webhook）**：也接通。填 webhook 就能直接往群里推文本，
  不需要应用凭证 —— 适合"只想把消息发到某个群"的场景。
- **接收**：飞书用事件订阅（HTTP 回调）。桌面版没有公网地址时收不到；
  回调参数（Verification Token / Encrypt Key）会收在配置里，endpoint 由 demo.py 挂。

user_id 约定：`wx_feishu__<open_id>`
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from .base import BaseChannel
from . import store

API = "https://open.feishu.cn"
PREFIX = "wx_feishu__"


class FeishuChannel(BaseChannel):
    id = "feishu"
    kind = "feishu"
    name = "飞书"
    mode = "app"
    icon = "feishu"
    available = True
    integrated = True

    receive_path = "/v1/channels/feishu/callback"

    def __init__(self):
        self.running = False
        self.qr_status = "idle"
        self.qr_message = "填写 App ID / App Secret（或用 Webhook）后即可发送"
        self._token = ""
        self._token_exp = 0.0
        self._client = None
        self.config_schema = [
            {"key": "app_id", "label": "App ID", "type": "text",
             "placeholder": "cli_xxxxxxxx", "hint": "飞书开放平台 → 凭证与基础信息"},
            {"key": "app_secret", "label": "App Secret", "type": "password",
             "placeholder": "应用 App Secret", "secret": True},
            {"key": "webhook", "label": "自定义机器人 Webhook（可选）", "type": "text",
             "placeholder": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
             "hint": "填了就能直接往群推消息，不需要应用凭证"},
            {"key": "verification_token", "label": "事件订阅 Verification Token（可选）", "type": "password",
             "placeholder": "接收事件回调时校验用", "secret": True},
            {"key": "encrypt_key", "label": "事件订阅 Encrypt Key（可选）", "type": "password",
             "placeholder": "开启加密时填", "secret": True},
            {"key": "default_chat_id", "label": "默认会话 chat_id（可选）", "type": "text",
             "placeholder": "oc_xxxxxxxx；留空则用会话里的 open_id"},
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
        return bool(c.get("webhook") or (c.get("app_id") and c.get("app_secret")))

    # ---------------------------------------------------------------- token
    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp:
            return self._token
        import httpx
        c = self.config or {}
        r = httpx.post(f"{API}/open-apis/auth/v3/tenant_access_token/internal",
                       json={"app_id": c.get("app_id"), "app_secret": c.get("app_secret")},
                       timeout=15)
        d = r.json() if r.status_code == 200 else {}
        token = d.get("tenant_access_token") or ""
        if not token:
            raise RuntimeError(f"飞书取 token 失败: {str(d)[:140]}")
        self._token = token
        self._token_exp = time.time() + max(60, int(d.get("expire") or 7200) - 200)
        return token

    # ---------------------------------------------------------------- 生命周期
    def start(self, **kwargs) -> bool:
        if not self._ready():
            self.qr_status = "error"
            self.qr_message = "缺少 App ID / App Secret，也没有 Webhook"
            return False
        c = self.config or {}
        if c.get("webhook"):
            self.running = True
            self.qr_status = "running"
            self.qr_message = "已接通（Webhook 群推模式；接收需事件订阅回调）"
            return True
        try:
            self._get_token()
        except Exception as exc:
            self.qr_status = "error"
            self.qr_message = f"凭证校验失败：{exc}"
            logger.warning(f"[飞书] {exc}")
            return False
        self.running = True
        self.qr_status = "running"
        self.qr_message = "已接通（应用身份发送；接收需事件订阅回调）"
        return True

    def stop(self):
        self.running = False
        self.qr_status = "idle"
        self.qr_message = "已停止"

    # ---------------------------------------------------------------- 发送
    @staticmethod
    def _extract_receive(text: str) -> tuple:
        """飞书要求文本消息体是 JSON：{"text": "..."}"""
        return ("text", {"text": str(text or "")[:4000]})

    def _target(self, user_id: str) -> dict:
        c = self.config or {}
        uid = str(user_id or "")
        if uid.startswith(PREFIX):
            return {"id_type": "open_id", "id": uid[len(PREFIX):]}
        if c.get("default_chat_id"):
            return {"id_type": "chat_id", "id": c["default_chat_id"]}
        return {}

    def send(self, user_id: str, text: str) -> bool:
        if not self._ready():
            return False
        c = self.config or {}
        try:
            import httpx
            # ① Webhook 模式：直接推群，最简单
            if c.get("webhook"):
                r = httpx.post(str(c["webhook"]), json={
                    "msg_type": "text", "content": {"text": str(text or "")[:4000]}}, timeout=15)
                d = r.json() if r.status_code == 200 else {}
                if d.get("code") not in (0, None):
                    logger.warning(f"[飞书] webhook 发送失败: {d.get('msg')}")
                    return False
                return True
            # ② 应用身份
            t = self._target(user_id)
            if not t.get("id"):
                logger.warning("[飞书] 没有接收目标（既无 wx_feishu__ 会话，也没配默认 chat_id）")
                return False
            msg_type, content = self._extract_receive(text)
            r = httpx.post(
                f"{API}/open-apis/im/v1/messages",
                params={"receive_id_type": t["id_type"]},
                headers={"Authorization": f"Bearer {self._get_token()}",
                         "Content-Type": "application/json"},
                json={"receive_id": t["id"], "msg_type": msg_type,
                      "content": __import__("json").dumps(content, ensure_ascii=False)},
                timeout=15)
            d = r.json() if r.status_code == 200 else {}
            if d.get("code") not in (0, None):
                logger.warning(f"[飞书] 发送失败: {d.get('msg')}")
                return False
            return True
        except Exception as exc:
            logger.warning(f"[飞书] 发送异常: {exc}")
            return False

    def resolve_remote(self, user_id: str, allow_fuzzy: bool = False) -> str:
        if str(user_id or "").startswith(PREFIX):
            return str(user_id)[len(PREFIX):]
        return str((self.config or {}).get("default_chat_id") or "")

    # ---------------------------------------------------------------- 状态
    def status(self) -> dict:
        base = super().status()
        c = self.config or {}
        base["receive_ready"] = bool(c.get("verification_token"))
        base["receive_path"] = self.receive_path
        base["ready"] = self._ready()
        base["mode_detail"] = "webhook" if c.get("webhook") else "app"
        return base
