"""微信 ClawBot（iLink Bot API）通道 —— 官方协议，扫码绑定，多账号并行。

端点（Base: https://ilinkai.weixin.qq.com）：
- GET  /ilink/bot/get_bot_qrcode?bot_type=3    拉取登录二维码
- GET  /ilink/bot/get_qrcode_status?qrcode=xx  轮询扫码状态（长轮询，confirmed 后返回 bot_token）
- POST /ilink/bot/getupdates                    长轮询收消息（服务器 hold 最长约 35s）
- POST /ilink/bot/sendmessage                   发送消息（需携带上下文 context_token）

鉴权头：
- AuthorizationType: ilink_bot_token
- X-WECHAT-UIN: base64(str(random uint32))，每次请求随机
- Authorization: Bearer <bot_token>（登录后才有）
"""

import asyncio
import base64
import io
import random
import threading
import time
import uuid
from typing import Callable, Optional

import requests
from loguru import logger

from .base import BaseChannel
from . import store

ILINK_BASE = "https://ilinkai.weixin.qq.com"
QR_POLL_INTERVAL = 1.0          # 二维码状态轮询间隔（秒，仅超时重试时生效）
QR_EXPIRE_SECONDS = 150         # 二维码有效期（服务端实测约 146 秒过期，本地兜底 150 秒）
UPDATES_TIMEOUT = 35            # getupdates 长轮询 hold 时长
SEND_DELAY_CAP = 3.0            # 微信多消息间隔上限（秒）

# ---------- iLink 发送结果码（协议实测 + 社区实现） ----------
SEND_RET_OK = (0, None, "0")
ERR_RATE_LIMIT = -2        # 频率限制（ret=-2 且 errmsg=unknown error 也代表 context_token 已失效）
ERR_SESSION_EXPIRED = -14  # 会话过期：去掉 context_token 重试一次（iLink 支持无 token 降级推送）
TYPING_START = 1
TYPING_STOP = 2
TYPING_TICKET_TTL = 600    # typing_ticket 有效期（秒）
TYPING_REFRESH_SECONDS = 4  # 生成期间每 4 秒重发一次「正在输入」，避免微信端指示器消失


def _looks_like_base64(s: str) -> bool:
    """粗略判断字符串是否为 base64 图片数据（排除 URL / 普通文本）"""
    s = (s or "").strip()
    if not s or len(s) < 40:
        return False
    if s.startswith(("http://", "https://", "wx", "//")):
        return False
    if any(ch in s for ch in " ?&=%"):
        return False
    return all(c.isalnum() or c in "+/=" for c in s)


def _to_bool(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _ret_ok(resp: dict) -> bool:
    """sendmessage / sendtyping 返回是否成功"""
    if not isinstance(resp, dict):
        return False
    return (resp.get("ret") in SEND_RET_OK) and (resp.get("errcode") in SEND_RET_OK)


def _is_stale_token_ret(resp: dict) -> bool:
    """ret=-2 + errmsg='unknown error' 表示 context_token 已失效（并非真正限流）"""
    ret = resp.get("ret")
    errcode = resp.get("errcode")
    if ret != ERR_RATE_LIMIT and errcode != ERR_RATE_LIMIT:
        return False
    return str(resp.get("errmsg") or "").strip().lower() == "unknown error"


def _is_rate_limited(resp: dict) -> bool:
    return resp.get("ret") == ERR_RATE_LIMIT or resp.get("errcode") == ERR_RATE_LIMIT


def _is_session_expired(resp: dict) -> bool:
    return resp.get("ret") == ERR_SESSION_EXPIRED or resp.get("errcode") == ERR_SESSION_EXPIRED


class ILinkClient:
    """iLink Bot API 客户端"""

    def __init__(self, bot_token: str = "", baseurl: str = ""):
        self.bot_token = bot_token
        self.baseurl = (baseurl or ILINK_BASE).rstrip("/")

    def _headers(self) -> dict:
        uin = base64.b64encode(str(random.randrange(2 ** 32)).encode("ascii")).decode("ascii")
        h = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": uin,
        }
        if self.bot_token:
            h["Authorization"] = f"Bearer {self.bot_token}"
        return h

    def get_bot_qrcode(self, bot_type: int = 3) -> dict:
        r = requests.get(
            f"{self.baseurl}/ilink/bot/get_bot_qrcode",
            params={"bot_type": bot_type},
            headers=self._headers(),
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def get_qrcode_status(self, qrcode: str, timeout: int = 40) -> dict:
        # 服务端对状态请求做长轮询（约 30s 一次返回 wait），超时要给足
        r = requests.get(
            f"{self.baseurl}/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode},
            headers=self._headers(),
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()

    def get_updates(self, cursor: str, timeout: int = UPDATES_TIMEOUT, probe: bool = False) -> dict:
        body = {
            "get_updates_buf": cursor or "",
            "base_info": {"channel_version": "1.0.2"},
        }
        r = requests.post(
            f"{self.baseurl}/ilink/bot/getupdates",
            json=body,
            headers=self._headers(),
            timeout=(6 if probe else timeout + 10),
        )
        r.raise_for_status()
        return r.json()

    def _post_sendmessage(self, msg: dict, context_token: str = "", retries: int = 3) -> dict:
        """POST /ilink/bot/sendmessage，带结果码校验与重试：
        - ret=-14 / errcode=-14：会话过期 → 去掉 context_token 降级重试一次
        - ret=-2 + errmsg=unknown error：context_token 已失效 → 去掉 token 重试一次
        - ret=-2：真频率限制 → 指数退避重试（最多 retries 次）
        任何失败都会抛异常（含错误码），调用方可见（日志/前端异常），不再静默吞掉。"""
        if context_token:
            msg["context_token"] = context_token
        token = context_token
        stripped = False
        for attempt in range(retries + 1):
            if token:
                msg["context_token"] = token
            body = {"msg": dict(msg), "base_info": {"channel_version": "1.0.2"}}
            r = requests.post(
                f"{self.baseurl}/ilink/bot/sendmessage",
                json=body,
                headers=self._headers(),
                timeout=20,
            )
            r.raise_for_status()
            resp = r.json()
            if _ret_ok(resp):
                return resp
            ret = resp.get("ret")
            errcode = resp.get("errcode")
            errmsg = resp.get("errmsg") or resp.get("msg") or "unknown error"
            # 会话过期 / 失效 token：剥掉 context_token 重试一次
            if token and (_is_session_expired(resp) or _is_stale_token_ret(resp)) and not stripped:
                stripped = True
                token = ""
                msg.pop("context_token", None)
                logger.warning(f"[iLink] sendmessage 会话过期/失效（ret={ret} errcode={errcode} {errmsg}），去 token 重试")
                continue
            # 频率限制：退避重试
            if _is_rate_limited(resp):
                if attempt >= retries:
                    raise RuntimeError(f"iLink sendmessage 频率限制：ret={ret} errcode={errcode} {errmsg}")
                wait = 3.0 * (attempt + 1)
                logger.warning(f"[iLink] sendmessage 频率限制（ret={ret}），{wait:.0f}s 后重试")
                time.sleep(wait)
                continue
            raise RuntimeError(f"iLink sendmessage 失败：ret={ret} errcode={errcode} {errmsg}")
        return {}

    def send_message(self, to_user_id: str, text: str, context_token: str = "") -> dict:
        msg = {
            "to_user_id": to_user_id,
            "message_type": 2,
            "message_state": 2,
            "client_id": f"qiyu-{uuid.uuid4().hex}",
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        return self._post_sendmessage(msg, context_token)

    def send_image(self, to_user_id: str, image: str, context_token: str = "") -> dict:
        """发送图片：image 可为 http(s) URL 或 base64 / data URL（best effort）"""
        image = (image or "").strip()
        item = {"type": 2, "image_item": {}}
        if image.startswith("http://") or image.startswith("https://"):
            item["image_item"]["url"] = image
        elif image.startswith("data:image"):
            import re as _re
            m = _re.match(r"data:image/[\w.+-]+;base64,([\s\S]+)", image)
            if m:
                item["image_item"]["base64"] = m.group(1)
            else:
                item["image_item"]["url"] = image
        else:
            item["image_item"]["base64"] = image
        msg = {
            "to_user_id": to_user_id,
            "message_type": 2,
            "message_state": 2,
            "client_id": f"qiyu-{uuid.uuid4().hex}",
            "item_list": [item],
        }
        return self._post_sendmessage(msg, context_token)

    def get_config(self, user_id: str, context_token: str = "") -> dict:
        """GET ilink/bot/getconfig：取 typing_ticket（对方正在输入需要）"""
        body = {
            "ilink_user_id": user_id,
            "context_token": context_token,
            "base_info": {"channel_version": "1.0.2"},
        }
        r = requests.post(
            f"{self.baseurl}/ilink/bot/getconfig",
            json=body,
            headers=self._headers(),
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def send_typing(self, user_id: str, typing_ticket: str, status: int = TYPING_START) -> dict:
        """POST ilink/bot/sendtyping：1=显示正在输入，2=取消"""
        body = {
            "ilink_user_id": user_id,
            "typing_ticket": typing_ticket,
            "status": status,
            "base_info": {"channel_version": "1.0.2"},
        }
        r = requests.post(
            f"{self.baseurl}/ilink/bot/sendtyping",
            json=body,
            headers=self._headers(),
            timeout=10,
        )
        r.raise_for_status()
        return r.json()


class ClawBotChannel(BaseChannel):
    """微信 ClawBot 通道：前端扫码绑定 → 长轮询收消息 → 进入栖语对话链路"""

    kind = "wechat"
    mode = "clawbot"
    icon = "wechat"

    def __init__(self, account_id: str = "main"):
        self.account_id = account_id or "main"
        self.id = f"clawbot_{self.account_id}"
        self.name = f"微信 ClawBot（{self.account_id}）"
        self.available = True
        self.integrated = True
        self.running = False
        self.bot_name = "栖语"
        self.qr_status = "idle"
        self.qr_data = ""
        self.qr_message = ""
        self._handler: Optional[Callable] = None
        self._client = ILinkClient()
        self._threads: list = []
        self._stop_events: dict = {}
        self._lifecycle_lock = threading.Lock()   # start/stop/reset 串行化，避免并发启动重复开线程/卡死
        self._sessions: dict = {}      # from_user_id -> context_token（发送必需）
        self._merged_remote: dict = {}  # 合并后的栖语 user_id -> 最近发消息的微信联系人（主动消息回发用）
        self._typing_tickets: dict = {}  # from_user_id -> (typing_ticket, fetched_at)
        self._typing_stop_events: dict = {}  # from_user_id -> threading.Event（生成期间保持「正在输入」）
        self._typing_threads: dict = {}  # from_user_id -> threading.Thread
        self._pending_qrcode = ""
        self._cursor = ""
        self.config_schema = [
            {"key": "account_id", "label": "账号标识", "type": "text",
             "placeholder": "main（多账号可填任意英文标识）",
             "hint": "多个微信号共用一个栖语后端时，每个微信号一个独立标识"},
            {"key": "character_id", "label": "绑定角色（透传）", "type": "select",
             "options_source": "characters",
             "hint": "该微信号发来的消息走这个角色；留空使用默认角色"},
            {"key": "single_conversation", "label": "单一对话（并入 App 主对话）", "type": "checkbox",
             "hint": "默认开启：微信只是另一个前端，消息并入 App 同一个对话线程，不再新建一堆微信会话；记忆/历史/关系完全合并。关闭后每个微信联系人独立会话。"},
            {"key": "merge_user_id", "label": "并入的对话 ID", "type": "text",
             "placeholder": "web_user",
             "hint": "单一对话模式并入哪个栖语对话；默认 web_user（App 主对话）。"},
            {"key": "emoji_beta", "label": "微信表情（beta）", "type": "checkbox",
             "hint": "开启后：回复会用微信表情（[表情名] 格式，微信端直接渲染成表情图）；用户发来的 [表情] 也按语义理解。仅微信来源的消息生效。"},
        ]
        self.config = {"account_id": self.account_id, "character_id": "",
                       "single_conversation": True, "merge_user_id": "web_user",
                       "emoji_beta": False}
        self._load_state()
        saved_cfg = store.load_config(self.id)
        if saved_cfg.get("account_id"):
            new_id = str(saved_cfg["account_id"]).strip() or "main"
            if new_id != self.account_id:
                self.account_id = new_id
                self.id = f"clawbot_{new_id}"
                self.name = f"微信 ClawBot（{new_id}）"
                self.config["account_id"] = new_id
                self._load_state()

    # ---------- 持久化 ----------
    def _load_state(self):
        st = store.load_channel_state(self.id)
        token = st.get("bot_token") or ""
        baseurl = st.get("baseurl") or ""
        if token:
            self._client.bot_token = token
        if baseurl:
            self._client.baseurl = baseurl.rstrip("/")
        self._cursor = st.get("cursor") or ""
        # 恢复合并对话联系人 + context_token：重启后主动消息（找话/早上补回）才能直接发到微信
        _mr = st.get("merged_remote") or {}
        if isinstance(_mr, dict) and _mr:
            try:
                self._merged_remote.update({str(k): str(v) for k, v in _mr.items()})
                logger.info(f"[{self.id}] 恢复合并对话联系人 {len(_mr)} 条（主动消息可回发）")
            except Exception:
                pass
        _ss = st.get("sessions") or {}
        if isinstance(_ss, dict) and _ss:
            try:
                self._sessions.update({str(k): str(v) for k, v in _ss.items() if v})
                logger.info(f"[{self.id}] 恢复 context_token {len(self._sessions)} 条（重启后主动消息可直接下发）")
            except Exception:
                pass
        saved_cfg = store.load_config(self.id)
        if saved_cfg:
            for k, v in saved_cfg.items():
                if k in ("single_conversation", "emoji_beta"):
                    self.config[k] = _to_bool(v)
                elif k in self.config:
                    self.config[k] = v

    def save_config(self, cfg: dict):
        """保存通道配置（账号标识 / 绑定角色 / 单一对话），落盘持久化"""
        if not isinstance(cfg, dict):
            return
        for k, v in cfg.items():
            if k in ("single_conversation", "emoji_beta"):
                self.config[k] = _to_bool(v)
            elif k in self.config:
                self.config[k] = v
        # 账号标识变更：另起独立通道（多账号并行），原通道保持
        new_id = str(cfg.get("account_id") or "").strip()
        if new_id and new_id != self.account_id:
            store.save_config("clawbot_" + new_id, {
                "account_id": new_id,
                "character_id": self.config.get("character_id", ""),
            })
            return
        store.save_config(self.id, self.config)

    def _save_state(self):
        store.save_channel_state(self.id, {
            "account_id": self.account_id,
            "bot_token": self._client.bot_token,
            "baseurl": self._client.baseurl,
            "cursor": self._cursor,
            # 最近发过消息的微信联系人（合并对话 → 主动消息回发用），重启后也保留
            "merged_remote": dict(self._merged_remote or {}),
            # context_token 也落盘：不发主动消息时它是唯一凭证，
            # 原来只存内存 → 一重启「主动消息/早上补回」就全发不出去
            "sessions": dict(self._sessions or {}),
            "updated_at": time.time(),
        })

    # ---------- 生命周期 ----------
    def start(self, force: bool = False, **kwargs) -> bool:
        with self._lifecycle_lock:
            return self._start_locked(force, **kwargs)

    def _start_locked(self, force: bool = False, **kwargs) -> bool:
        """启动微信通道：
        1) 若已有保存的 bot_token（上次扫码登录态），先用短超时 getupdates 探测，
           会话有效则直接恢复长轮询（不用重新扫码）；探测失败才走二维码流程。
        2) 否则拉取登录二维码（扫码确认后自动开长轮询）。

        force=True：无论当前是否已有二维码在等待，都作废旧码重新拉取
        （对应前端「重新扫码」按钮）。
        """
        # 清理已退出的线程，避免 _threads 无限累积（旧线程不参与逻辑，但会让状态难排查）
        self._threads = [t for t in self._threads if t.is_alive()]
        if self.running:
            return True
        if not force and self._client.bot_token:
            # 探测重试 3 次：刚启动 / 网络抖动时，单次 5s 超时很容易被误判成"登录态失效"，
            # 结果每次重启都退回二维码（用户要重新扫码）。超时/网络错误重试，只有明确返回失效才转扫码。
            for _try in range(3):
                try:
                    data = self._client.get_updates(self._cursor, timeout=5 + 4 * _try, probe=True)
                    ret = data.get("ret")
                    errcode = data.get("errcode")
                    if ret in SEND_RET_OK and errcode in SEND_RET_OK:
                        buf = data.get("get_updates_buf") or ""
                        if buf:
                            self._cursor = buf
                            self._save_state()
                        for msg in data.get("msgs") or []:
                            self._handle_inbound(msg)
                        self.running = True
                        self.qr_status = "running"
                        self.qr_message = "已恢复上次登录，正在监听消息"
                        self._start_updates_loop()
                        logger.success(f"[ClawBot:{self.id}] 复用上次登录态，直接开始监听")
                        return True
                    logger.info(f"[ClawBot:{self.id}] 上次登录态已失效（ret={ret} errcode={errcode}），需重新扫码")
                    break
                except Exception as e:
                    if _try < 2:
                        logger.warning(f"[ClawBot:{self.id}] 登录态探测超时/失败，2 秒后重试（第 {_try + 2}/3 次）: {e}")
                        time.sleep(2)
                        continue
                    # 网络类失败（读超时/连不上）≠ token 失效：iLink 的 getupdates 本身就是长轮询，
                    # 超时很可能只是网络抖动。这里直接进监听，真失效会在轮询里报错、那时再走二维码。
                    logger.warning(f"[ClawBot:{self.id}] 登录态探测超时（疑似网络抖动），先直接进入监听: {e}")
                    self.running = True
                    self.qr_status = "running"
                    self.qr_message = "已恢复上次登录（探测超时，直接进入监听）"
                    self._start_updates_loop()
                    return True
        old = self._find_thread("qr")
        if old and not force:
            return True  # 二维码已生成，等待扫码
        if old:
            ev = self._stop_events.get("qr_poll")
            if ev:
                ev.set()
            old.join(timeout=3)
        # 清掉旧状态，防止旧轮询线程残留覆盖新二维码状态
        self._pending_qrcode = ""
        self.qr_data = ""
        try:
            data = self._client.get_bot_qrcode()
        except Exception as e:
            self.qr_status = "error"
            self.qr_message = f"获取二维码失败：{e}"
            logger.error(f"[ClawBot:{self.id}] 获取二维码失败: {e}")
            return False
        qrcode = (data or {}).get("qrcode") or ""
        img = (data or {}).get("qrcode_img_content") or ""
        if not qrcode:
            self.qr_status = "error"
            self.qr_message = "返回数据缺少 qrcode，可能该登录方式暂不可用"
            return False
        self._pending_qrcode = qrcode
        self.qr_status = "waiting"
        self.qr_data = self._normalize_qr(img, qrcode)
        self.qr_message = "请用微信扫码并确认登录（约 2 分半内有效，过期后点「重新扫码」）"
        logger.info(f"[ClawBot:{self.id}] 二维码已生成，等待扫码")
        t = threading.Thread(target=self._qr_poll_loop, args=(qrcode,), daemon=True,
                             name=f"clawbot_qr_{self.id}")
        self._threads.append(t)
        t.start()
        return True

    def _normalize_qr(self, img_content, qrcode: str) -> str:
        """生成可显示的二维码 data URL：
        - qrcode_img_content 若是图片数据（data URL / base64）直接返回
        - 否则用 qrcode_img_content 的 liteapp URL（扫码跳转用）本地生成二维码
        - 都没有时用 qrcode 令牌本地生成"""
        payload = ""
        if img_content:
            try:
                s = img_content
                if isinstance(s, bytes):
                    s = s.decode("utf-8", "ignore")
                s = str(s).strip()
                if s.startswith("data:image"):
                    return s
                # 纯 base64 或带前缀的 base64（排除 URL 等普通文本）
                if s and "," in s and not s.startswith("data:"):
                    s = s.split(",", 1)[1]
                if _looks_like_base64(s):
                    return "data:image/png;base64," + s
                # 是 URL：扫码内容用完整 URL
                if s.startswith(("http://", "https://")):
                    payload = s
            except Exception:
                pass
        if not payload:
            payload = qrcode
        # 本地生成二维码
        try:
            import qrcode as qr_lib
            qr = qr_lib.QRCode(box_size=8, border=2)
            qr.add_data(payload)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            logger.warning(f"[ClawBot:{self.id}] 本地生成二维码失败: {e}")
            return ""

    def _qr_poll_loop(self, qrcode: str):
        ev = threading.Event()
        self._stop_events["qr_poll"] = ev
        deadline = time.time() + QR_EXPIRE_SECONDS
        try:
            while not ev.is_set():
                if time.time() > deadline:
                    if qrcode == self._pending_qrcode:
                        self.qr_status = "idle"
                        self.qr_message = "二维码已过期，请点击「重新扫码」获取新二维码"
                        self.qr_data = ""
                    return
                try:
                    data = self._client.get_qrcode_status(qrcode, timeout=40)
                except Exception as e:
                    if ev.is_set():
                        return
                    logger.debug(f"[ClawBot:{self.id}] 轮询二维码状态超时重试: {e}")
                    time.sleep(QR_POLL_INTERVAL)
                    continue
                ret = (data or {}).get("ret")
                status = str((data or {}).get("status") or "")
                if ret not in (0, None, "0") and not status:
                    if qrcode == self._pending_qrcode:
                        self.qr_status = "error"
                        self.qr_message = f"二维码状态查询失败（ret={ret}），请点击「重新扫码」"
                        self.qr_data = ""
                    return
                if status in ("confirmed", "success", "ok"):
                    if qrcode != self._pending_qrcode:
                        return  # 旧二维码线程，作废不处理
                    self._client.bot_token = (data or {}).get("bot_token") or ""
                    self._client.baseurl = str((data or {}).get("baseurl") or ILINK_BASE).rstrip("/")
                    if not self._client.bot_token:
                        self.qr_status = "error"
                        self.qr_message = "扫码确认但未返回 bot_token，请重新尝试"
                        return
                    self._save_state()
                    self.qr_status = "running"
                    self.qr_message = "登录成功，正在监听消息"
                    self.running = True
                    logger.success(f"[ClawBot:{self.id}] 扫码确认，bot_token 已保存")
                    self._stop_events.pop("qr_poll", None)
                    self._start_updates_loop()
                    return
                elif status in ("expired", "canceled", "cancel", "timeout"):
                    if qrcode == self._pending_qrcode:
                        self.qr_status = "idle"
                        self.qr_message = "二维码已失效，请点击「重新扫码」获取新二维码"
                        self.qr_data = ""
                    return
                # 等待中 / 已扫码 继续轮询
                time.sleep(QR_POLL_INTERVAL)
        finally:
            if self._stop_events.get("qr_poll") is ev:
                self._stop_events.pop("qr_poll", None)

    def _start_updates_loop(self):
        if self._find_thread("updates"):
            return
        t = threading.Thread(target=self._updates_loop, daemon=True,
                             name=f"clawbot_updates_{self.id}")
        self._threads.append(t)
        t.start()

    def _updates_loop(self):
        ev = threading.Event()
        self._stop_events["updates"] = ev
        logger.info(f"[ClawBot:{self.id}] 开始长轮询接收消息")
        try:
            while not ev.is_set():
                try:
                    data = self._client.get_updates(self._cursor, timeout=UPDATES_TIMEOUT)
                except Exception as e:
                    if ev.is_set():
                        break
                    logger.warning(f"[ClawBot:{self.id}] getupdates 失败: {e}")
                    time.sleep(3)
                    continue
                if not isinstance(data, dict):
                    continue
                ret = data.get("ret")
                if ret not in (0, None, "0"):
                    logger.warning(f"[ClawBot:{self.id}] getupdates ret={ret}")
                    time.sleep(3)
                    continue
                buf = data.get("get_updates_buf") or ""
                if buf:
                    self._cursor = buf
                    self._save_state()
                for msg in data.get("msgs") or []:
                    self._handle_inbound(msg)
        finally:
            self._stop_events.pop("updates", None)
            self.running = False
            logger.info(f"[ClawBot:{self.id}] 消息监听已停止")

    def _handle_inbound(self, msg: dict):
        """收到用户消息 → 进入栖语对话链路。
        单一对话模式（默认）：user_id 并入 App 主对话（merge_user_id），微信只是另一个前端，
        消息/记忆/历史全部合并到同一个线程，不再新建一堆 wx_ 会话。
        关闭时：每个微信联系人独立会话（user_id = wx_<通道id>__<对方id>）。
        文本/图片/表情透传；生成期间同步「对方正在输入」到微信，结束前取消。"""
        try:
            from_user = (msg or {}).get("from_user_id") or ""
            msg_type = (msg or {}).get("message_type")
            ctx = (msg or {}).get("context_token") or ""
            if not from_user or from_user.endswith("@im.bot"):
                return  # 机器人自己的消息
            # 不按 message_type 硬过滤（不同版本协议枚举不一致，漏过滤会导致消息丢失）：
            # 只要对方 id 合法、item_list 有内容就进入对话链路
            # context_token 变了就顺手落盘（重启后主动消息/早上补回还得靠它）
            if ctx and self._sessions.get(from_user) != ctx:
                self._sessions[from_user] = ctx
                try:
                    self._save_state()
                except Exception:
                    pass
            else:
                self._sessions[from_user] = ctx
            items = (msg or {}).get("item_list") or []
            texts = []
            images = []
            for it in items:
                it_type = it.get("type")
                if it_type == 1:
                    texts.append((it.get("text_item") or {}).get("text") or "")
                elif it_type == 2:
                    # 图片：优先取 url，其次 base64
                    img_item = it.get("image_item") or {}
                    url = str(img_item.get("url") or "").strip()
                    b64 = str(img_item.get("base64") or img_item.get("image_base64") or img_item.get("image_content") or "").strip()
                    if url:
                        images.append(url)
                        texts.append("[图片]")
                    elif b64:
                        images.append(b64 if b64.startswith("data:image") else "data:image/png;base64," + b64)
                        texts.append("[图片]")
                    else:
                        texts.append("[图片]")
                elif it_type == 6:
                    # 表情：微信 2021+ 只存名称（text/emoji/name 字段），统一成 [名称] 让模型按语义理解
                    em = (it.get("emoji_item") or {})
                    emoji_name = str(em.get("text") or em.get("emoji") or em.get("name") or "").strip()
                    texts.append(f"[{emoji_name}]" if emoji_name else "[表情]")
                elif it_type == 3:
                    # 语音：iLink 会带微信自带的 ASR 转写（voice_item.text），直接用它让模型"听懂"；
                    # 没有转写文本时，若本机装了 sherpa-onnx 本地 ASR + 模型，才尝试下载识别；否则退化 [语音]
                    voice = (it.get("voice_item") or {})
                    vtext = str(voice.get("text") or "").strip()
                    if not vtext:
                        try:
                            from .wechat_voice import local_asr_available, transcribe_voice_item
                            if local_asr_available():
                                vtext = transcribe_voice_item(voice) or ""
                        except Exception:
                            vtext = ""
                    if vtext:
                        texts.append(f"[语音] {vtext}")
                    else:
                        texts.append("[语音]")
                elif it_type == 4:
                    texts.append("[文件]")
                elif it_type == 5:
                    texts.append("[视频]")
            content = "\n".join(x for x in texts if x).strip()
            if not content:
                return
            merge = bool(self.config.get("single_conversation", True))
            if merge:
                user_id = str(self.config.get("merge_user_id") or "web_user").strip() or "web_user"
                self._merged_remote[user_id] = from_user  # 记住该对话最近来自微信的联系人（主动消息回发）
                try:
                    self._save_state()   # 落盘：重启后主动消息仍能直接发到微信
                except Exception:
                    pass
            else:
                user_id = f"wx_{self.id}__{from_user}"
            logger.info(f"[ClawBot:{self.id}] 收到消息 [{from_user}]: {content[:50]} 图片数={len(images)}")
            if not self._handler:
                return
            self._start_typing(from_user, ctx)
            try:
                if asyncio.iscoroutinefunction(self._handler):
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        reply = loop.run_until_complete(self._handler(user_id, content, self, images))
                    finally:
                        loop.close()
                else:
                    reply = self._handler(user_id, content, self, images)
            except Exception as e:
                logger.error(f"[ClawBot:{self.id}] 处理消息失败: {e}")
                self._stop_typing(from_user)
                return
            self._stop_typing(from_user)
            self._send_reply(from_user, reply)
        except Exception as e:
            logger.error(f"[ClawBot:{self.id}] 解析消息失败: {e}")

    def _normalize_emoji_tags(self, txt: str) -> str:
        """发送前把 [表情名] 换成标准 emoji：微信通道发不出表情图片、也不认 [名称] 格式。
        统一走 channels.wechat_emoji.convert_emoji_tags（含别名表；认不出的去掉方括号保留文字），
        原来这里自己查表、认不出替换成空串，会把句子咬掉一块（"我好[抱抱]" → "我好"）。"""
        if not txt or "[" not in txt:
            return txt
        from channels.wechat_emoji import convert_emoji_tags
        return convert_emoji_tags(txt)

    def _send_reply(self, from_user: str, reply):
        """把回复（str 或 (text, pieces)）按真人节奏逐条发送；每条都校验发送结果，失败即记日志并停止后续。"""
        ctx = self._sessions.get(from_user) or ""
        if isinstance(reply, tuple):
            reply_text, pieces = reply
            items = pieces or []
        else:
            reply_text, pieces = reply, None
            items = [{"text": reply_text, "delay": 0}] if reply_text else []
        for i, p in enumerate(items):
            txt = self._normalize_emoji_tags((p.get("text") or "").strip())
            img = str(p.get("image") or p.get("image_url") or "").strip()
            if not txt and not img:
                continue
            try:
                if img and img.startswith(("http://", "https://", "data:image", "iVBOR", "/9j/")):
                    self._client.send_image(from_user, img, ctx)
                    logger.info(f"[ClawBot:{self.id}] 已发送图片给 {from_user}（{i+1}/{len(items)}）")
                if txt:
                    self._client.send_message(from_user, txt, ctx)
                    logger.info(f"[ClawBot:{self.id}] 已发送 {from_user}: {txt[:30]}（{i+1}/{len(items)}）")
            except Exception as e:
                logger.error(f"[ClawBot:{self.id}] 发送失败（第 {i+1} 条，共 {len(items)} 条）: {e}")
                self.qr_message = f"微信发送失败（第 {i+1}/{len(items)} 条）: {e}"
                return
            delay = (p.get("delay") or 0) / 1000.0
            if i < len(items) - 1 and delay > 0:
                time.sleep(min(delay, SEND_DELAY_CAP))

    # ---------- 发送（主动消息） ----------
    def send(self, user_id: str, text: str) -> bool:
        if not self._client.bot_token:
            return False
        # send 只会在「这条通道已被路由给自己」时被调用，允许唯一联系人回退
        remote = self.resolve_remote(user_id, allow_fuzzy=True)
        if not remote:
            logger.warning(f"[ClawBot:{self.id}] 无法定位微信联系人（user_id={user_id}），主动消息不下发")
            return False
        text = self._normalize_emoji_tags(text)
        ctx = self._sessions.get(remote) or ""
        if not ctx:
            # 重启后还没收到过对方消息时就是没有 token。底层 _post_sendmessage 本来就支持
            # 不带 token 发送（失败还会去 token 重试一次），所以这里照样尝试，不再直接放弃。
            logger.info(f"[ClawBot:{self.id}] 暂无 {remote} 的 context_token，按无会话态尝试发送")
        try:
            self._start_typing(remote, ctx)
            try:
                self._client.send_message(remote, text, ctx)
            finally:
                self._stop_typing(remote)
            logger.info(f"[ClawBot:{self.id}] 主动消息已发送 {remote}: {text[:30]}")
            return True
        except Exception as e:
            logger.error(f"[ClawBot:{self.id}] 发送失败: {e}")
            return False

    def resolve_remote(self, user_id: str, allow_fuzzy: bool = False) -> str:
        """把栖语内部 user_id 解析成微信联系人 id（供主动消息回发）：
        - wx_<通道id>__<对方id> → 对方 id
        - 单一对话合并后的 user_id（如 web_user） → 合并表里记过的微信联系人

        allow_fuzzy=True（仅限「这条通道就是该对话自己绑的」时）才允许
        「会话表/合并表里只剩一个联系人就猜是他」。默认 False，避免扫描别人的通道时猜错串号。
        """
        if not user_id:
            return ""
        if user_id.startswith("wx_"):
            body = user_id[3:]
            _, _, remote = body.partition("__")
            if remote:
                return remote
        hit = str(self._merged_remote.get(user_id) or "")
        if hit:
            return hit
        merge_id = str((self.config or {}).get("merge_user_id") or "").strip()
        if merge_id and user_id == merge_id:
            hit = str(self._merged_remote.get(merge_id) or "")
            if hit:
                return hit
        # 单一对话：会话表里往往只剩这一个微信联系人。
        # merged_remote 没写上（只在网页聊过、或重启前那次没落盘）时，用它把找话发回微信。
        if allow_fuzzy and bool((self.config or {}).get("single_conversation", True)):
            keys = [str(k) for k in (self._sessions or {}) if k]
            if len(keys) == 1:
                self._remember_remote(user_id or merge_id, keys[0])
                return keys[0]
            if len(self._merged_remote) == 1:
                only = next(iter(self._merged_remote.values()))
                self._remember_remote(user_id or merge_id, only)
                return str(only)
        return ""

    def _remember_remote(self, user_id: str, remote: str) -> None:
        if not user_id or not remote:
            return
        if str(self._merged_remote.get(user_id) or "") == remote:
            return
        self._merged_remote[user_id] = remote
        try:
            self._save_state()
        except Exception:
            pass

    # ---------- 对方正在输入（iLink sendtyping） ----------
    def _get_typing_ticket(self, from_user: str, ctx: str) -> str:
        cached = self._typing_tickets.get(from_user) or (None, 0)
        ticket, fetched_at = cached
        if ticket and (time.time() - fetched_at) < TYPING_TICKET_TTL - 30:
            return ticket
        try:
            resp = self._client.get_config(from_user, ctx)
            ticket = str(resp.get("typing_ticket") or "")
            if ticket:
                self._typing_tickets[from_user] = (ticket, time.time())
            return ticket
        except Exception as e:
            logger.debug(f"[ClawBot:{self.id}] 获取 typing_ticket 失败: {e}")
            return ""

    def _typing_refresh_loop(self, from_user: str):
        ev = self._typing_stop_events.get(from_user)
        ctx = self._sessions.get(from_user) or ""
        ticket = self._get_typing_ticket(from_user, ctx)
        if not ticket:
            return
        while ev and not ev.is_set():
            try:
                self._client.send_typing(from_user, ticket, TYPING_START)
            except Exception as e:
                logger.debug(f"[ClawBot:{self.id}] sendtyping 失败: {e}")
            ev.wait(TYPING_REFRESH_SECONDS)
        try:
            self._client.send_typing(from_user, ticket, TYPING_STOP)
        except Exception:
            pass

    def _start_typing(self, from_user: str, ctx: str = ""):
        """生成阶段开始：微信端显示「对方正在输入…」并周期性续显"""
        if not from_user or not self._client.bot_token:
            return
        self._stop_typing(from_user)
        ev = threading.Event()
        self._typing_stop_events[from_user] = ev
        t = threading.Thread(target=self._typing_refresh_loop, args=(from_user,), daemon=True,
                             name=f"clawbot_typing_{self.id}")
        self._typing_threads[from_user] = t
        t.start()

    def _stop_typing(self, from_user: str):
        """生成结束/发送前：取消微信端「正在输入…」"""
        ev = self._typing_stop_events.pop(from_user, None)
        if ev:
            ev.set()
        t = self._typing_threads.pop(from_user, None)
        if t and t.is_alive():
            t.join(timeout=2)

    def stop(self):
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self):
        for ev in list(self._stop_events.values()):
            ev.set()
        for from_user in list(self._typing_stop_events.keys()):
            self._stop_typing(from_user)
        # 等待仍在运行的轮询线程退出并清理，避免残留线程在下次启动时互相覆盖状态
        for t in list(self._threads):
            if t.is_alive():
                t.join(timeout=2)
        self._threads = [t for t in self._threads if t.is_alive()]
        self.running = False
        self.qr_status = "idle"
        self.qr_data = ""
        self.qr_message = ""
        self._pending_qrcode = ""
        logger.info(f"[ClawBot:{self.id}] 已停止")

    def reset(self):
        """重置连接（切换微信号）：清掉登录态/游标/会话/输入指示/二维码，
        保留用户配置（绑定角色、单一对话等），下次启动直接进入全新扫码流程。"""
        self.stop()
        with self._lifecycle_lock:
            self._client.bot_token = ""
            self._client.baseurl = ILINK_BASE
            self._cursor = ""
            self._sessions.clear()
            self._merged_remote.clear()
            self._typing_tickets.clear()
            self._pending_qrcode = ""
            # 清掉持久化登录态（保留 config 里的用户配置）
            try:
                store.save_channel_state(self.id, {
                    "account_id": self.account_id,
                    "updated_at": time.time(),
                })
            except Exception as e:
                logger.warning(f"[ClawBot:{self.id}] 清空登录态失败: {e}")
        logger.success(f"[ClawBot:{self.id}] 已重置连接，下次启动将重新扫码")

    def _find_thread(self, kind: str):
        prefix = f"clawbot_{kind}_{self.id}"
        for t in self._threads:
            if t.name and t.name.startswith(prefix) and t.is_alive():
                return t
        return None
