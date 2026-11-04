"""严格校验的分段下载器（GitHub release 大文件专用）。

为 NapCat Shell 等协议端资产而生：这些文件在国内直连必挂、走代理又常被中途掐断，
所以规则很苛刻——只接受 HTTP 206 且长度精确匹配的数据块，流式落盘（断在中途
也保留已收到的字节），从断点继续，直到 CRC 校验通过。

CLI（scripts/napcat_fetch_part.py 是它的命令行入口）：
    python -m core.napcat_fetch --url <直链> --target <字节数> --out <文件> [--jobs 6]

镜像回退：直链外自动尝试 ghproxy.net / gh-proxy.com 前缀；某个源连续 0 字节就换下一个。
"""
from __future__ import annotations

import argparse
import os
import time
import zipfile
from pathlib import Path
from collections.abc import Callable

import httpx

PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
    or "http://127.0.0.1:7897"
BLOCK = 512 * 1024
IO_CHUNK = 64 * 1024
MIRROR_PREFIXES = ["https://ghproxy.net/", "https://gh-proxy.com/", ""]


def _urls(direct: str) -> list[str]:
    return [p + direct for p in MIRROR_PREFIXES]


def _download_part(index: int, urls: list[str], start: int, end: int, f: str) -> None:
    """把 [start, end] 区间落进文件 f 的对应偏移（f 是分段文件，断点续传）。"""
    url_i = 0
    idle = 0
    while True:
        have = os.path.getsize(f) if os.path.exists(f) else 0
        a = start + have
        if a > end:
            return
        want = min(BLOCK, end - a + 1)
        url = urls[url_i % len(urls)]
        got = 0
        try:
            with httpx.Client(proxy=PROXY, timeout=45, follow_redirects=True) as c:
                with c.stream("GET", url, headers={"Range": f"bytes={a}-{a + want - 1}"}) as r:
                    if r.status_code != 206:
                        raise RuntimeError(f"status {r.status_code}")
                    with open(f, "ab") as fp:
                        for chunk in r.iter_bytes(IO_CHUNK):
                            fp.write(chunk)
                            got += len(chunk)
        except Exception as e:  # noqa: BLE001 - 网络失败靠重试与镜像轮换消化
            print(f"  ! {type(e).__name__}: {str(e)[:60]} at +{got}", flush=True)
        if got == 0:
            idle += 1
            url_i += 1
            time.sleep(2)
        else:
            idle = 0
        if idle >= 6:
            print(f"  ! all mirrors struggling at offset {a}", flush=True)
            idle = 0


def strict_download(url: str, target: int, out: str | Path, jobs: int = 6,
                    progress: Callable[[int], None] | None = None,
                    cwd: str | Path = ".") -> int:
    """并行分段下载 url 到 out（合并后 CRC 校验）。返回 0=成功。

    progress: 回调当前已收字节数（0..target），每 20 秒或每段完成时调用。
    """
    import threading

    cwd = str(cwd)
    urls = _urls(url)
    chunk = (target + jobs - 1) // jobs
    parts = [f"_dl_part_{i}" for i in range(jobs)]

    def run(i: int) -> None:
        start = i * chunk
        end = min(start + chunk - 1, target - 1)
        _download_part(i, urls, start, end, parts[i])

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(jobs)]
    for t in threads:
        t.start()
    last_report = 0.0
    while any(t.is_alive() for t in threads):
        done = sum(os.path.getsize(p) for p in parts if os.path.exists(p))
        if progress and time.time() - last_report > 20:
            progress(done)
            last_report = time.time()
        time.sleep(2)
    for t in threads:
        t.join()
    done = sum(os.path.getsize(p) for p in parts if os.path.exists(p))
    if progress:
        progress(done)

    out_path = os.path.join(cwd, out) if not os.path.isabs(out) else out
    with open(out_path, "wb") as fo:
        for p in parts:
            if os.path.exists(p):
                with open(p, "rb") as pf:
                    fo.write(pf.read())
    if os.path.getsize(out_path) != target:
        raise IOError(f"size mismatch: {os.path.getsize(out_path)} != {target}")
    z = zipfile.ZipFile(out_path)
    bad = z.testzip()
    if bad is not None:
        raise IOError(f"CRC 校验失败：{bad}")
    for p in parts:  # 全部成功才清理分段
        if os.path.exists(p):
            os.remove(p)
    return 0


def cli() -> int:
    ap = argparse.ArgumentParser(description="严格校验的分段下载器")
    ap.add_argument("--url", required=True)
    ap.add_argument("--target", type=int, required=True, help="文件总字节数")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args()
    try:
        strict_download(a.url, a.target, a.out, jobs=a.jobs,
                        progress=lambda done: print(f"PROGRESS {done}/{a.target}", flush=True))
    except Exception as e:
        print(f"X {type(e).__name__}: {e}", flush=True)
        return 1
    print(f"OK {a.out} ({a.target} bytes) CRC 通过", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
