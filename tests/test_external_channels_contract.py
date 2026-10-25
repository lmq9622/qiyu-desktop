# -*- coding: utf-8 -*-
"""外部通道合同验收：用本地 mock 官方端点验证 token/发送/错误路径。

不依赖真实账号凭证；真实上线仍需在平台后台用真实凭证做一次 live selftest。
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from channels import feishu, qqbot, wecom


RECORDS = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        RECORDS.append({"method": "GET", "path": self.path, "headers": dict(self.headers), "body": b""})
        if self.path.startswith("/cgi-bin/gettoken"):
            self._send({"errcode": 0, "access_token": "wecom-token", "expires_in": 7200})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        body = self._body()
        RECORDS.append({"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body})
        path = self.path.split("?", 1)[0]
        if path == "/cgi-bin/message/send":
            self._send({"errcode": 0, "errmsg": "ok"})
        elif path == "/app/getAppAccessToken":
            self._send({"access_token": "qq-token", "expires_in": 7200})
        elif path.startswith("/v2/users/") or path.startswith("/v2/channels/"):
            self._send({"id": "qq-message-id"})
        elif path == "/open-apis/auth/v3/tenant_access_token/internal":
            self._send({"tenant_access_token": "feishu-token", "expire": 7200})
        elif path == "/open-apis/im/v1/messages":
            self._send({"code": 0, "data": {"message_id": "om_x"}})
        elif path == "/hook":
            self._send({"code": 0, "msg": "ok"})
        else:
            self._send({"error": "not found"}, 404)


class ExternalChannelContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=3)

    def setUp(self):
        RECORDS.clear()

    def test_wecom_token_and_send_payload(self):
        ch = wecom.WeComChannel()
        ch.config = {"corp_id": "corp", "agent_id": "1000002", "secret": "secret",
                     "default_touser": "zhangsan"}
        ch._token = ""
        with patch.object(wecom, "API", self.base):
            self.assertTrue(ch.start())
            self.assertTrue(ch.send("wx_wecom__lisi", "你好"))
        paths = [r["path"] for r in RECORDS]
        self.assertTrue(any(p.startswith("/cgi-bin/gettoken") for p in paths))
        send = next(r for r in RECORDS if r["path"].startswith("/cgi-bin/message/send"))
        payload = json.loads(send["body"])
        self.assertEqual(payload["touser"], "lisi")
        self.assertEqual(payload["msgtype"], "text")
        self.assertEqual(payload["text"]["content"], "你好")
        self.assertEqual(payload["agentid"], 1000002)

    def test_wecom_missing_config_rejected(self):
        ch = wecom.WeComChannel()
        ch.config = {k: "" for k in ch.config}
        self.assertFalse(ch.start())

    def test_qq_token_and_c2c_send(self):
        ch = qqbot.QQBotChannel()
        ch.config = {"app_id": "app", "app_secret": "secret", "default_openid": "openid-1"}
        ch._token = ""
        with patch.object(qqbot, "TOKEN_API", self.base + "/app/getAppAccessToken"), \
             patch.object(qqbot, "API", self.base):
            self.assertTrue(ch.start())
            self.assertTrue(ch.send("", "测试"))
        send = next(r for r in RECORDS if r["path"].startswith("/v2/users/"))
        self.assertIn("openid-1", send["path"])
        self.assertEqual(json.loads(send["body"])["content"], "测试")
        self.assertTrue(send["headers"].get("Authorization", "").startswith("QQBot "))

    def test_feishu_app_send(self):
        ch = feishu.FeishuChannel()
        ch.config = {"app_id": "app", "app_secret": "secret", "default_chat_id": "oc_chat"}
        ch._token = ""
        with patch.object(feishu, "API", self.base):
            self.assertTrue(ch.start())
            self.assertTrue(ch.send("", "飞书测试"))
        send = next(r for r in RECORDS if r["path"].startswith("/open-apis/im/v1/messages"))
        self.assertIn("receive_id_type=chat_id", send["path"])
        payload = json.loads(send["body"])
        self.assertEqual(payload["receive_id"], "oc_chat")
        self.assertEqual(payload["content"], '{"text": "飞书测试"}')

    def test_feishu_webhook_mode(self):
        ch = feishu.FeishuChannel()
        ch.config = {"webhook": self.base + "/hook"}
        with patch.object(feishu, "API", self.base):
            self.assertTrue(ch.start())
            self.assertTrue(ch.send("", "群推测试"))
        send = next(r for r in RECORDS if r["path"] == "/hook")
        self.assertEqual(json.loads(send["body"])["msg_type"], "text")

    def test_http_error_returns_false(self):
        ch = wecom.WeComChannel()
        ch.config = {"corp_id": "corp", "agent_id": "1", "secret": "bad",
                     "default_touser": "u"}
        with patch.object(wecom, "API", self.base + "/missing"):
            # token endpoint is 404 => start must fail, no exception leaks
            self.assertFalse(ch.start())


if __name__ == "__main__":
    unittest.main()
