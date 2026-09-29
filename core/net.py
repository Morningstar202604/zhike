"""域名解析覆盖：校园/内网 DNS 解析失败时，用 LLM_HOSTS=域=IP,域=IP 指定解析。

同时覆盖同步（socket.getaddrinfo）与异步（BaseEventLoop.getaddrinfo）两条解析路径。
"""
import os
import socket

_applied = False
_hosts: list[str] = []


def status() -> dict:
    return {"applied": _applied, "hosts": list(_hosts)}


def parse_llm_hosts(raw: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in (raw or "").split(","):
        if "=" in item:
            host, ip = item.split("=", 1)
            host, ip = host.strip(), ip.strip()
            if host and ip:
                mapping[host] = ip
    return mapping


def _host_key(host) -> str:
    if isinstance(host, bytes):
        return host.decode("utf-8", errors="replace")
    return host


def apply(mapping: dict[str, str] | None = None) -> int:
    global _applied
    if _applied:
        return -1
    if mapping is None:
        mapping = parse_llm_hosts(os.environ.get("LLM_HOSTS", ""))
    if not mapping:
        return 0

    orig = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        return orig(mapping.get(_host_key(host), host), *args, **kwargs)

    socket.getaddrinfo = patched

    try:
        import asyncio.base_events as _abe

        orig_loop_gai = _abe.BaseEventLoop.getaddrinfo

        def loop_gai(self, host, port, *, family=0, type=0, proto=0, flags=0):
            return orig_loop_gai(self, mapping.get(_host_key(host), host), port, family=family, type=type, proto=proto, flags=flags)

        _abe.BaseEventLoop.getaddrinfo = loop_gai
    except Exception:  # aqg: top-level boundary
        pass

    _applied = True
    _hosts.extend(mapping.keys())
    return len(mapping)
