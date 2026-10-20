"""执行器能力实现：文件、命令、进程。

所有入口先过策略校验，再动手；错误统一抛 CapabilityError，由 server 转成结构化响应。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from executor.policy import Policy, PolicyError


class CapabilityError(Exception):
    pass


# ---------------- 文件 ----------------

def fs_stat(policy: Policy, path: str) -> dict:
    p = policy.check_path(path)
    if not os.path.exists(p):
        raise CapabilityError("路径不存在")
    st = os.stat(p)
    return {
        "path": p,
        "exists": True,
        "is_dir": os.path.isdir(p),
        "is_link": os.path.islink(p),
        "size": st.st_size,
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
        "mode": oct(st.st_mode & 0o777),
    }


def fs_list(policy: Policy, path: str, depth: int = 1) -> dict:
    p = policy.check_path(path)
    if not os.path.isdir(p):
        raise CapabilityError("不是目录")
    depth = max(1, min(int(depth or 1), 3))
    limit = policy.max_list()
    entries: list[dict] = []

    def walk(root: str, level: int) -> None:
        try:
            names = sorted(os.listdir(root))
        except OSError as e:
            entries.append({"path": root, "error": type(e).__name__})
            return
        for name in names:
            if len(entries) >= limit:
                return
            full = os.path.join(root, name)
            try:
                st = os.lstat(full)
                entries.append({
                    "name": name,
                    "path": full,
                    "is_dir": os.path.isdir(full),
                    "size": st.st_size,
                    "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                })
            except OSError as e:
                entries.append({"name": name, "path": full, "error": type(e).__name__})
            if level < depth and os.path.isdir(full) and not os.path.islink(full):
                walk(full, level + 1)

    walk(p, 1)
    return {"path": p, "count": len(entries), "truncated": len(entries) >= limit, "entries": entries}


def fs_read(policy: Policy, path: str, encoding: str = "utf-8") -> dict:
    p = policy.check_path(path)
    if not os.path.isfile(p):
        raise CapabilityError("不是文件")
    size = os.path.getsize(p)
    limit = policy.max_read()
    if size > limit:
        raise CapabilityError(f"文件过大（{size} > {limit} 字节上限）")
    with open(p, "rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise CapabilityError(f"文件超过读取上限 {limit} 字节")
    try:
        text = raw.decode(encoding)
        binary = False
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        binary = True
    return {
        "path": p, "size": size, "binary_guess": binary,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content": text,
    }


def fs_write(policy: Policy, path: str, content: str = "", encoding: str = "utf-8") -> dict:
    p = policy.check_path(path, for_write=True)
    data = (content or "").encode(encoding)
    limit = policy.max_write()
    if len(data) > limit:
        raise CapabilityError(f"内容过大（{len(data)} > {limit} 字节上限）")
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    backup = None
    if os.path.exists(p):
        # 覆盖前留快照，供撤销使用（超过 1MB 不复制，只记录）
        if os.path.getsize(p) <= 1048576:
            backup = p + ".zksave"
            shutil.copy2(p, backup)
    tmp = p + ".zktmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return {
        "path": p, "written": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "backup": backup,
    }


def fs_mkdir(policy: Policy, path: str) -> dict:
    p = policy.check_path(path, for_write=True)
    os.makedirs(p, exist_ok=True)
    return {"path": p, "created": True}


def fs_move(policy: Policy, src: str, dst: str) -> dict:
    s = policy.check_path(src)
    d = policy.check_path(dst, for_write=True)
    if not os.path.exists(s):
        raise CapabilityError("源路径不存在")
    os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
    shutil.move(s, d)
    return {"src": s, "dst": d, "moved": True}


def fs_delete(policy: Policy, path: str, mode: str = "trash") -> dict:
    """删除。默认进回收站（可恢复），permanent 需要策略显式允许。"""
    p = policy.check_path(path)
    if not os.path.exists(p):
        raise CapabilityError("路径不存在")
    if mode == "permanent":
        if os.path.isdir(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
        return {"path": p, "deleted": "permanent"}
    # 回收站：优先 send2trash，缺失时退化为移动到同目录 .zhtrash
    try:
        from send2trash import send2trash

        send2trash(p)
        return {"path": p, "deleted": "trash", "recoverable": True}
    except ImportError:
        trash_dir = os.path.join(os.path.dirname(p) or ".", ".zhtrash")
        os.makedirs(trash_dir, exist_ok=True)
        target = os.path.join(trash_dir, f"{int(time.time())}_{os.path.basename(p)}")
        shutil.move(p, target)
        return {"path": p, "deleted": "moved-to-trash-dir", "trash_path": target, "recoverable": True}


# ---------------- 命令 ----------------

def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n...(已截断 {len(text) - limit} 字符)...\n" + text[-half:]


def shell_exec(policy: Policy, argv: list[str], cwd: str | None = None,
               timeout_s: int | None = None) -> dict:
    checked_argv, workdir = policy.check_command(argv, cwd)
    timeout = int(timeout_s or policy.command_timeout())
    limit = policy.max_output()
    started = time.time()
    try:
        proc = subprocess.run(
            checked_argv,
            cwd=workdir or None,
            env=policy.safe_env(),
            capture_output=True,
            timeout=timeout,
            shell=False,          # 关键：永不使用 shell，杜绝注入
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise CapabilityError(f"命令超时（{timeout}s），已终止") from e
    except FileNotFoundError as e:
        raise CapabilityError("命令不存在或不可执行") from e
    except OSError as e:
        raise CapabilityError(f"命令执行失败：{type(e).__name__}") from e
    return {
        "argv": checked_argv,
        "cwd": workdir,
        "returncode": proc.returncode,
        "stdout": _truncate(proc.stdout or "", limit),
        "stderr": _truncate(proc.stderr or "", limit),
        "duration_ms": int((time.time() - started) * 1000),
    }


# ---------------- 进程 ----------------

def proc_list(policy: Policy, filter_text: str = "") -> dict:
    """列出进程。不依赖 psutil：Windows 用 tasklist，POSIX 用 ps。"""
    items: list[dict] = []
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                                 text=True, timeout=20, errors="replace").stdout
            for line in out.splitlines():
                parts = [x.strip('"') for x in line.split('","')]
                if len(parts) >= 2:
                    items.append({"name": parts[0], "pid": parts[1]})
        else:
            out = subprocess.run(["ps", "-eo", "pid,comm"], capture_output=True,
                                 text=True, timeout=20, errors="replace").stdout
            for line in out.splitlines()[1:]:
                bits = line.split(None, 1)
                if len(bits) == 2:
                    items.append({"pid": bits[0], "name": bits[1]})
    except (subprocess.SubprocessError, OSError) as e:
        raise CapabilityError(f"无法枚举进程：{type(e).__name__}") from e
    if filter_text:
        low = filter_text.lower()
        items = [i for i in items if low in str(i.get("name", "")).lower()]
    return {"count": len(items), "entries": items[:500]}


def proc_kill(policy: Policy, pid: int, name: str = "") -> dict:
    policy.check_kill(int(pid), name)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        else:
            os.kill(int(pid), 15)
    except (subprocess.SubprocessError, OSError) as e:
        raise CapabilityError(f"结束进程失败：{type(e).__name__}") from e
    return {"pid": pid, "killed": True}
