"""通道注册表：统一路由 栖语 user_id → 通道，供主动消息下发与状态查询。"""

from typing import Callable, Optional

from loguru import logger

from .base import BaseChannel


class ChannelRegistry:
    """所有外部通讯通道的注册中心"""

    def __init__(self):
        self._channels: dict[str, BaseChannel] = {}

    def register(self, channel: BaseChannel) -> BaseChannel:
        self._channels[channel.id] = channel
        logger.info(f"[通道] 注册: {channel.id}（{channel.name}）")
        return channel

    def get(self, channel_id: str) -> Optional[BaseChannel]:
        return self._channels.get(channel_id)

    def list_channels(self) -> list:
        return [c.status() for c in self._channels.values()]

    def set_message_handler(self, handler: Callable):
        for c in self._channels.values():
            if c.integrated:
                try:
                    c.set_message_handler(handler)
                except Exception as e:
                    logger.warning(f"[通道] 设置 {c.id} 消息处理器失败: {e}")

    @staticmethod
    def _bound(channel: BaseChannel) -> bool:
        """有登录态就能尝试下发，不必等长轮询线程把 running 置真。"""
        client = getattr(channel, "_client", None)
        return bool(getattr(client, "bot_token", "") or "")

    @staticmethod
    def _resolve(channel: BaseChannel, user_id: str, allow_fuzzy: bool) -> str:
        """resolve_remote 统一入口。allow_fuzzy=True 时优先传 flag（支持该参数的
        通道才走「唯一联系人」回退）；不支持 kwarg 的老通道退回无参调用。"""
        if allow_fuzzy:
            try:
                return channel.resolve_remote(user_id, allow_fuzzy=True)
            except TypeError:
                pass
        return channel.resolve_remote(user_id)

    def _can_deliver(self, channel: BaseChannel, user_id: str,
                     allow_fuzzy: bool = False) -> bool:
        if not getattr(channel, "resolve_remote", None):
            return False
        try:
            if not self._resolve(channel, user_id, allow_fuzzy):
                return False
        except Exception:
            return False
        return bool(getattr(channel, "running", False) or self._bound(channel))

    @staticmethod
    def _meant_for(channel: BaseChannel, user_id: str) -> bool:
        """这条通道本来就是为这个 user 建的（merge_user_id 指向他）。
        只有这种归属明确的通道才允许「唯一联系人」模糊回退 —— 否则扫描会把
        别人唯一绑定的微信借给另一个对话，找话发到不相干的联系人手机上。"""
        merge_id = str((getattr(channel, "config", None) or {}).get("merge_user_id") or "").strip()
        return bool(merge_id) and merge_id == user_id

    def get_channel_for_user(self, user_id: str) -> Optional[BaseChannel]:
        """按栖语内部 user_id 路由到通道：
        wx_<通道id>__<对方id> → 对应通道；
        单一对话合并后的 user_id（如 web_user） → 绑定了该对话的通道（resolve_remote 命中）。
        找不到返回 None（前端普通对话）。"""
        if not user_id:
            return None
        if user_id.startswith("wx_"):
            body = user_id[3:]
            if "__" in body:
                acc = body.split("__", 1)[0]
                ch = self.get(acc)
                if ch:
                    return ch
            return self.get("clawbot_main")
        # 合并对话：找绑定了该对话的通道（长轮询在跑，或持有登录态即可）。
        # 归属不明（merge_user_id 不是这个对话）→ 只许精确解析，不许 fuzzy 猜人，避免串号。
        for ch in self._channels.values():
            if self._can_deliver(ch, user_id, allow_fuzzy=self._meant_for(ch, user_id)):
                return ch
        return None

    def send(self, user_id: str, text: str) -> bool:
        """主动消息下发：按 user_id 路由到对应通道。
        绑过微信（有 token）即可发，不要求长轮询此刻 running——
        以前 running=False 就直接放弃，找话只进网页 SSE、发不到微信。"""
        ch = self.get_channel_for_user(user_id)
        if ch and self.can_send(ch):
            try:
                return ch.send(user_id, text)
            except Exception as e:
                logger.warning(f"[通道] {ch.id} 发送失败: {e}")
                return False
        return False

    @staticmethod
    def can_send(channel: BaseChannel) -> bool:
        """这条通道此刻能不能发：长轮询在跑，或持有登录态（bot_token）。"""
        return bool(getattr(channel, "running", False) or ChannelRegistry._bound(channel))

    def running_channels(self) -> list[BaseChannel]:
        return [c for c in self._channels.values() if c.running]


_registry: Optional[ChannelRegistry] = None


def get_channel_registry() -> ChannelRegistry:
    global _registry
    if _registry is None:
        _registry = ChannelRegistry()
    return _registry
