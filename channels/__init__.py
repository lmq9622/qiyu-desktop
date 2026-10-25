"""栖语 外部通讯通道层（参考 OpenClaw channel 抽象）。

在用的通道
----------
微信：
- ClawBot：官方 iLink 协议，前端扫码绑定，多账号并行（推荐，置顶）
- Wechatauto 本机接管已下线（封号风险，不再注册）

其它平台（2026-10 接入，发送链路已接通，接收需用户配回调/事件订阅）：
- 企业微信（WeCom）：自建应用 + access_token 发消息
- QQ Bot：开放平台 v2 API，AppID/AppSecret 换 token 发私聊/频道
- 飞书（Feishu）：应用身份发消息，或自定义机器人 Webhook 群推

已下线
------
- Wechaty（Node 网关单账号接管）：**从注册表移除，文件保留**。
  后续若要单独部署一个带 Wechaty 的版本，把 `wechaty_channel.py` 重新 register 即可，
  不需要改这里以外的任何代码。
"""

from .base import BaseChannel
from .registry import get_channel_registry, ChannelRegistry
from .clawbot import ClawBotChannel, ILinkClient
from .wecom import WeComChannel
from .qqbot import QQBotChannel
from .feishu import FeishuChannel
from .placeholders import PlaceholderChannel, build_placeholder_channels
from . import store

# Wechaty 保持可 import（文件还在、类还在），但**不再进入默认注册表**。
try:
    from .wechaty_channel import WechatyChannel
except Exception:  # 依赖缺失时不要让整个包 import 失败
    WechatyChannel = None  # type: ignore

__all__ = [
    "BaseChannel",
    "ChannelRegistry",
    "get_channel_registry",
    "ClawBotChannel",
    "ILinkClient",
    "WeComChannel",
    "QQBotChannel",
    "FeishuChannel",
    "PlaceholderChannel",
    "build_placeholder_channels",
    "WechatyChannel",   # 保留导出，默认不注册
    "store",
]
