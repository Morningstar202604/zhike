"""文档 → 知识库条目 管道：解析（可选依赖，缺失给可读提示）+ 标题/段落分块，纯函数便于回归。

支持：Markdown/TXT（零依赖）、PDF（pypdf）、DOCX（python-docx）。
分块策略：标题优先分节，节内超长按句打包 ≤500 字；无标题文档按段落聚合，标题回落为文件名。
"""
import re

MAX_CHUNK = 500   # 单块上限（与 KB 导入的 answer 语义一致，控制 prompt 占用）
MIN_CHUNK = 60    # 小于该长度的段落并入相邻块


def extract_text(filename: str, data: bytes) -> str:
    """按扩展名解析出纯文本；可选依赖缺失时抛 RuntimeError（端点转 400）。"""
    name = (filename or "").lower()
    if name.endswith((".md", ".markdown", ".txt")):
        return data.decode("utf-8", errors="replace")
    if name.endswith(".pdf"):
        try:
            import io

            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("PDF 解析需要依赖：pip install -r requirements-docs.txt") from e
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [(page.extract_text() or "").strip() for page in reader.pages]
        except Exception as e:  # 伪 PDF/加密/损坏：转可读 400，不让 pypdf 异常裸奔成 500
            raise RuntimeError(f"PDF 解析失败：{type(e).__name__}: {str(e)[:80]}") from e
        return "\n\n".join(p for p in pages if p)
    if name.endswith(".docx"):
        try:
            import io

            import docx
        except ImportError as e:
            raise RuntimeError("DOCX 解析需要依赖：pip install -r requirements-docs.txt") from e
        try:
            d = docx.Document(io.BytesIO(data))
        except Exception as e:  # 伪 DOCX：转可读 400
            raise RuntimeError(f"DOCX 解析失败：{type(e).__name__}: {str(e)[:80]}") from e
        lines = []
        for p in d.paragraphs:
            t = (p.text or "").strip()
            if not t:
                continue
            style = (p.style.name or "").lower() if p.style else ""
            if style.startswith("heading"):
                try:
                    level = int(style.split()[-1])
                except ValueError:
                    level = 1
                # DOCX H1 → Markdown ##（H1 留给文档名语义）
                lines.append("#" * min(level + 1, 6) + " " + t)
            else:
                lines.append(t)
        return "\n\n".join(lines)
    if name.endswith((".xlsx", ".xlsm")):
        try:
            import io

            import openpyxl
        except ImportError as e:
            raise RuntimeError("Excel 解析需要依赖：pip install openpyxl") from e
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            raise RuntimeError(f"Excel 解析失败：{type(e).__name__}: {str(e)[:80]}") from e
        chunks = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                chunks.append(f"## {ws.title}\n\n" + "\n".join(rows))
        try:
            wb.close()
        except Exception:
            pass
        return "\n\n".join(chunks)
    raise RuntimeError("不支持的文档类型（支持 .md / .txt / .pdf / .docx / .xlsx）")


def _pack(long_text: str, title: str) -> list[tuple[str, str]]:
    """节内超长：按句/段落打包成 ≤MAX_CHUNK 的多块，标题沿用小节名。"""
    parts = re.split(r"(?<=[。！？!?])\s*|\n+", long_text.strip())  # 任意换行都是段落断点（flush 已滤空行）
    blocks, buf = [], ""
    for p in (p for p in parts if p and p.strip()):
        p = p.strip()
        while len(p) > MAX_CHUNK:  # 无标点超长段兜底硬切
            blocks.append((title, p[:MAX_CHUNK]))
            p = p[MAX_CHUNK:]
        if buf and len(buf) + len(p) + 1 > MAX_CHUNK:
            blocks.append((title, buf))
            buf = p
        else:
            buf = f"{buf}\n{p}".strip()
    if buf:
        blocks.append((title, buf))
    return blocks


def chunk_document(text: str, fallback_title: str) -> list[tuple[str, str]]:
    """文档文本 → [(标题, 块正文)]。标题分节；无标题时按段落聚合，标题回落为文件名。"""
    text = (text or "").strip()
    if not text:
        return []
    fallback_title = (fallback_title or "文档").strip()[:60]
    blocks: list[tuple[str, str]] = []
    cur_title = ""
    buf: list[str] = []
    has_heading = bool(re.search(r"^#{1,6}\s+\S", text, re.M))

    def flush(title: str, lines: list[str]) -> None:
        body = "\n".join(l for l in lines if l.strip()).strip()
        if not body:
            return
        for t, b in _pack(body, title):
            blocks.append((t[:80], b))

    for line in text.splitlines():
        t = None
        m = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if m:
            t = m.group(2).strip()
        if t is not None:
            flush(cur_title or fallback_title, buf)
            buf = []
            cur_title = t
        else:
            buf.append(line)
    flush(cur_title or fallback_title, buf)

    if not has_heading:
        # 无标题文档：把按标题 flushed 的多块重新聚合标题为「文件名（n）」
        merged: list[tuple[str, str]] = []
        for i, (_, b) in enumerate(blocks, 1):
            merged.append((f"{fallback_title}（{i}）", b))
        blocks = merged
    else:
        blocks = [(t if t != fallback_title else f"{fallback_title}·概览", b) for t, b in blocks]
    return blocks
