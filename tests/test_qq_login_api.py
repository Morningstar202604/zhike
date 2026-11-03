"""QQ 扫码登录代理：WebUI 鉴权握手 / 二维码透传 / 凭证过期重登。

这组测试守三件事：
1. token 绝不下发到响应里（它是 NapCat 的管理凭据）
2. 鉴权哈希必须是 SHA256(token + ".napcat")——写错 NapCat 就永远 401
3. Credential 过期（401/403）要自动重登一次并重放，前端不该看到 401 抖动
"""
import hashlib
import json

import pytest

MODULE = "core.api.qq_login"


class FakeNapCat:
    """按 NapCat WebUI 的真实行为模拟：200 包业务错误、401 表示凭证过期。"""

    def __init__(self, token: str):
        self.token = token
        self.auth_calls = 0
        self.business_calls = 0
        self.expire_first_business = False
        self.fail_qr = False

    async def post(self, url, json=None, headers=None):
        import httpx

        class R:
            def __init__(self, status_code, body):
                self.status_code = status_code
                self._body = body

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise httpx.HTTPStatusError(f"HTTP {self.status_code}",
                                                request=None, response=None)

            def json(self):
                return self._body

        if url.endswith("/api/auth/login"):
            self.auth_calls += 1
            expect = hashlib.sha256(f"{self.token}.napcat".encode()).hexdigest()
            if json.get("hash") != expect:
                return R(200, {"code": -1, "message": "hash 不对"})
            return R(200, {"code": 0, "data": {"Credential": f"cred-{self.auth_calls}"}})

        self.business_calls += 1
        # 模拟凭证过期：第一次业务请求 401，客户端应重登后重放
        if self.expire_first_business and self.business_calls == 1:
            return R(401, {})
        if url.endswith("/api/QQLogin/GetQQLoginQrcode"):
            if self.fail_qr:
                return R(200, {"code": -1, "message": "二维码不存在"})
            return R(200, {"code": 0,
                           "data": {"qrcodeurl": "https://ssl.ptlogin2.qq.com/jump?x=1"}})
        if url.endswith("/api/QQLogin/CheckLoginStatus"):
            return R(200, {"code": 0, "data": {"isLogin": False, "isOffline": False,
                                               "coreReady": False, "loginPhase": 0}})
        return R(200, {"code": 0, "data": {}})


@pytest.fixture()
def napcat(monkeypatch):
    """把连接池换成假 NapCat，并注入 token；返回假实例供断言。"""
    fake = FakeNapCat(token="test-webui-token")
    monkeypatch.setenv("NAPCAT_WEBUI_URL", "http://127.0.0.1:6099")
    monkeypatch.setenv("NAPCAT_WEBUI_TOKEN", "test-webui-token")

    def fake_get_client(base_url="", timeout=30.0):
        assert base_url == "http://127.0.0.1:6099"
        return fake

    import core.api.qq_login as qql

    monkeypatch.setattr(qql, "get_client", fake_get_client)
    qql._cred_cache.update(credential="", ts=0.0)
    return fake


def test_qr_unconfigured_reports_honestly(client, monkeypatch):
    """没配 token 时必须明说，而不是返回一个前端没法用的空二维码。"""
    monkeypatch.delenv("NAPCAT_WEBUI_TOKEN", raising=False)
    r = client.get("/api/channel/qq/qr")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert "NAPCAT_WEBUI_TOKEN" in body["error"]


def test_qr_proxies_url_and_never_leaks_token(client, napcat):
    r = client.get("/api/channel/qq/qr")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["qrcode_url"].startswith("https://ssl.ptlogin2.qq.com/")
    # token / credential 都不许出现在响应里
    assert "test-webui-token" not in json.dumps(body)
    assert "cred-" not in json.dumps(body)
    # 鉴权握手只发生一次，且哈希正确（FakeNapCat 里已校验）
    assert napcat.auth_calls == 1


def test_qr_business_error_surfaced(client, napcat):
    """NapCat 用 200 包业务错误时，要把 message 带给前端，不能假装成功。"""
    napcat.fail_qr = True
    r = client.get("/api/channel/qq/qr")
    body = r.json()
    assert body["configured"] is True
    assert "二维码不存在" in body["error"]
    assert "qrcode_url" not in body


def test_credential_expiry_relogs_and_replays(client, napcat):
    """凭证过期（401）→ 重登一次 → 重放成功，整个过程对前端透明。"""
    napcat.expire_first_business = True
    r = client.get("/api/channel/qq/qr")
    assert r.status_code == 200
    assert r.json()["qrcode_url"].startswith("https://")
    assert napcat.auth_calls == 2, "401 后应自动重登一次"


def test_login_status_shape(client, napcat):
    r = client.get("/api/channel/qq/login-status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True and body["reachable"] is True
    assert body["is_login"] is False and body["login_phase"] == 0
