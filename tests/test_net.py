import asyncio
import socket

from core import net as netmod
from core.net import apply, parse_llm_hosts


def test_parse_llm_hosts_pairs():
    assert parse_llm_hosts("a.com=1.2.3.4, b.com=5.6.7.8") == {"a.com": "1.2.3.4", "b.com": "5.6.7.8"}


def test_parse_llm_hosts_empty_and_bad():
    assert parse_llm_hosts("") == {}
    assert parse_llm_hosts("no-equal-sign, =ip-only, x=") == {}


def test_apply_patches_sync_getaddrinfo(monkeypatch):
    import socket

    calls = []

    def fake_getaddrinfo(host, *args, **kwargs):
        calls.append(host)
        return [(2, 1, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(netmod, "_applied", False)
    assert apply({"api.example.com": "9.9.9.9"}) == 1
    import socket as s

    s.getaddrinfo("api.example.com", 443)
    s.getaddrinfo("other.example.com", 443)
    assert calls == ["9.9.9.9", "other.example.com"]


def test_apply_maps_bytes_host(monkeypatch):
    import socket

    resolved = []

    def fake(host, *a, **k):
        resolved.append(host)
        return [(2, 1, 6, "", ("1.2.3.4", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setattr(netmod, "_applied", False)
    apply({"api.example.com": "1.2.3.4"})
    socket.getaddrinfo(b"api.example.com", 443)
    assert resolved == ["1.2.3.4"]


def test_apply_maps_asyncio_loop(monkeypatch):
    monkeypatch.setattr(netmod, "_applied", False)
    assert apply({"api.example.com": "127.0.0.1"}) == 1

    async def run():
        loop = asyncio.get_running_loop()
        return await loop.getaddrinfo("api.example.com", 443, type=socket.SOCK_STREAM)

    infos = asyncio.run(run())
    assert infos
