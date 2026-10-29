"""知识库文件存储：人可读 JSON 为真相源 + 跨进程锁 + 原子写。

为什么不用数据库存 KB：知识库是运营同学要直接编辑/导出/进 Git 的内容，
JSON + Markdown 导出比 SQL 表更合适；检索索引（FTS5/vec）是它的派生缓存。

并发安全三层（每层都对应一次真实事故）：
1. 进程内 threading.Lock —— 同进程多线程互斥；
2. 操作系统文件锁 —— 多 worker / 多实例互斥（实测 5 进程各写 10 条只剩 12 条）；
3. 临时文件 + os.replace —— 崩溃/断电不会留下半个 JSON（读侧永远看到完整文件）。

从 core/main.py 抽出：路由文件里不该出现文件锁与原子写，这层可以独立单测。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import uuid

from core import retrieval


class KbStore:
    def __init__(self, lock: threading.Lock | None = None) -> None:
        self._lock = lock or threading.Lock()

    # ---------- 跨进程文件锁 ----------

    @contextlib.contextmanager
    def file_lock(self):
        """操作系统级独占锁：Linux/macOS 走 fcntl，Windows 走 msvcrt。"""
        path = retrieval.KB_PATH
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()

    # ---------- 读改写 ----------

    def mutate(self, fn):
        """在【进程内锁 + 跨进程文件锁】内完成「读 → 改 → 写」。

        fn(items) -> (result, new_items)；写入后清空 KB 缓存，下一次检索立即可见。
        """
        with self._lock, self.file_lock():
            retrieval._KB_CACHE = None
            items = list(retrieval.load_kb())
            result, new_items = fn(items)
            self.save(new_items)
            retrieval._KB_CACHE = None
            return result, new_items

    def save(self, items: list[dict]) -> None:
        """原子写：临时文件（带 pid + uuid）→ os.replace，失败重试 3 次。"""
        p = retrieval.KB_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"kb": items}, f, ensure_ascii=False, indent=2)
            f.write("\n")
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                os.replace(tmp, p)
                return
            except OSError as e:  # 杀毒/索引器会短暂占用句柄
                last_err = e
                time.sleep(0.05 * (attempt + 1))
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise RuntimeError(f"知识库写入失败：{type(last_err).__name__}") from last_err


store = KbStore()


# ---------- 纯函数（无 IO，单测直接调） ----------

def parse_blocks(raw: str) -> list[tuple[str, str]]:
    """批量导入文本 → [(标题, 答案)]，自动识别两种格式：

    - 标题模式：以 ## / ### 开头的行为条目标题，其后所有内容（可含空行）为答案
    - 分块模式：空行分隔的块，块内首行为标题，其余行为答案
    """
    raw = (raw or "").replace("\r\n", "\n").strip()
    if not raw:
        return []
    lines = raw.split("\n")
    blocks: list[tuple[str, str]] = []
    if any(ln.lstrip().startswith(("## ", "### ")) for ln in lines):
        title, body = None, []
        for ln in lines:
            ls = ln.strip()
            if ls.startswith(("## ", "### ")):
                if title is not None:
                    blocks.append((title, "\n".join(body).strip()))
                title, body = ls.lstrip("#").strip(), []
            elif title is not None:
                body.append(ln)
        if title is not None:
            blocks.append((title, "\n".join(body).strip()))
    else:
        title, body = None, []
        for ln in lines:
            if not ln.strip():
                if title is not None:
                    blocks.append((title, "\n".join(body).strip()))
                    title, body = None, []
            elif title is None:
                title = ln.strip()
            else:
                body.append(ln)
        if title is not None:
            blocks.append((title, "\n".join(body).strip()))
    return [(t, a) for t, a in blocks if t]


def next_base(items: list[dict]) -> int:
    """下一个可用 KB 编号基数（KB101 起）。"""
    base = 100
    for e in items:
        m = re.match(r"^KB(\d+)$", e.get("id") or "")
        if m:
            base = max(base, int(m.group(1)))
    return base


def export_markdown(items: list[dict]) -> str:
    """导出为可再次导入的 Markdown（## 标题 + 正文），形成备份闭环。"""
    lines: list[str] = []
    for e in items:
        lines.append(f"## {e['title']}")
        lines.append(e.get("answer") or e.get("text") or "")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
