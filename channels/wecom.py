# -*- coding: utf-8 -*-
"""企业微信自建应用通道（WeCom）。

按 OpenClaw channel 抽象实现，父类接口见 `channels/base.py`。

能力边界（诚实说明）
--------------------
- **发送**：已接通。拿 corp secret 换 access_token（缓存 7000s），调
  `message/send` 发给指定成员。这是外部主动找话、以及"微信侧收到消息后回话"的主链路。
- **接收**：需要在企业微信后台配置「接收消息服务器」并暴露公网回调（Token + EncodingAESKey +
  URL）。桌面版跑在本机、默认没有公网地址，所以这里只把回调所需的参数收好并把
  `receive_ready` 标出来，真正的回调 endpoint 由 `demo.py` 挂（见 `receive_path`）。
  没有公网时：**能发不能收**，UI 会明确显示这一点，不假装通了。

user_id 约定：`wx_wecom__<企业成员 UserID>`
"""
from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from .base import BaseChannel
from . import store

API = "https://qyapi.weixin.qq.com"
PREFIX = "wx_wecom__"


class WeComChannel(BaseChannel):
    id = "wecom"
    kind = "wecom"
    name = "企业微信"
    mode = "app"
    icon = "wecom"
    available = True
    integrated = True      # 发送链路真的接通了

    receive_path = "/v1/channels/wecom/callback"

    def __init__(self):
        self.running = False
        self.qr_status = "idle"
        self.qr_message = "填写企业 ID / AgentId / Secret 后即可发送（接收需公网回调）"
        self._token: str = ""
        self._token_exp: float = 0.0
        self._client = None            # 兼容 registry._bound 的探测
        self.config_schema = [
            {"key": "corp_id", "label": "企业 ID（CorpID）", "type": "text",
             "placeholder": "ww1234567890abcdef", "hint": "企业微信管理后台 → 我的企业 → 企业ID"},
            {"key": "agent_id", "label": "应用 AgentId", "type": "text",
             "placeholder": "1000002", "hint": "自建应用 → 应用详情"},
            {"key": "secret", "label": "应用 Secret", "type": "password",
             "placeholder": "应用详情页的 Secret", "secret": True},
            {"key": "token", "label": "回调 Token（可选）", "type": "text",
             "placeholder": "接收消息服务器配置里的 Token"},
            {"key": "aes_key", "label": "回调 EncodingAESKey（可选）", "type": "password",
             "placeholder": "接收消息服务器配置里的 AESKey", "secret": True},
            {"key": "default_touser", "label": "默认接收人（可选）", "type": "text",
             "placeholder": "UserID，多个用 | 分隔；留空则用会话里的 UserID"},
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
        return bool(c.get("corp_id") and c.get("secret") and c.get("agent_id"))

    # ---------------------------------------------------------------- token
    def _get_token(self) -> str:
        c = self.config or {}
        if self._token and time.time() < self._token_exp:
            return self._token
        import httpx
        r = httpx.get(f"{API}/cgi-bin/gettoken",
                      params={"corpid": c.get("corp_id"), "corpsecret": c.get("secret")},
                      timeout=15)
        d = r.json() if r.status_code == 200 else {}
        if d.get("errcode") not in (0, None):
            raise RuntimeError(f"企业微信 token 失败: {d.get('errmsg')}（{d.get('errcode')}）")
        token = d.get("access_token") or ""
        if not token:
            raise RuntimeError("企业微信没返回 access_token")
        self._token = token
        self._token_exp = time.time() + max(60, int(d.get("expires_in") or 7200) - 200)
        return token

    # ---------------------------------------------------------------- 生命周期
    def start(self, **kwargs) -> bool:
        if not self._ready():
            self.qr_status = "error"
            self.qr_message = "缺少企业 ID / AgentId / Secret"
            return False
        try:
            self._get_token()
        except Exception as exc:
            self.qr_status = "error"
            self.qr_message = f"凭证校验失败：{exc}"
            logger.warning(f"[企业微信] {exc}")
            return False
        self.running = True
        self.qr_status = "running"
        self.qr_message = "已接通（发送可用；接收需在企微后台配置回调到本机公网地址）"
        return True

    def stop(self):
        self.running = False
        self.qr_status = "idle"
        self.qr_message = "已停止"

    # ---------------------------------------------------------------- 发送
    def _targets(self, user_id: str) -> str:
        uid = str(user_id or "")
        if uid.startswith(PREFIX):
            uid = uid[len(PREFIX):]
        if uid and not uid.startswith("web_"):
            return uid
        return str((self.config or {}).get("default_touser") or "")

    def send(self, user_id: str, text: str) -> bool:
        touser = self._targets(user_id)
        if not touser:
            logger.warning("[企业微信] 没有接收人（既没有 wx_wecom__ 前缀的会话，也没配默认接收人）")
            return False
        if not self._ready():
            return False
        try:
            import httpx
            token = self._get_token()
            payload = {
                "touser": touser,
                "msgtype": "text",
                "agentid": int(str((self.config or {}).get("agent_id") or 0) or 0),
                "text": {"content": str(text or "")[:2000]},
                "safe": 0,
            }
            r = httpx.post(f"{API}/cgi-bin/message/send",
                           params={"access_token": token}, json=payload, timeout=15)
            d = r.json() if r.status_code == 200 else {}
            if d.get("errcode") not in (0, None):
                logger.warning(f"[企业微信] 发送失败: {d.get('errmsg')}")
                return False
            return True
        except Exception as exc:
            logger.warning(f"[企业微信] 发送异常: {exc}")
            return False

    def resolve_remote(self, user_id: str, allow_fuzzy: bool = False) -> str:
        return self._targets(user_id)

    # ---------------------------------------------------------------- 状态
    def status(self) -> dict:
        base = super().status()
        base["receive_ready"] = bool((self.config or {}).get("token")
                                     and (self.config or {}).get("aes_key"))
        base["receive_path"] = self.receive_path
        base["ready"] = self._ready()
        return base
