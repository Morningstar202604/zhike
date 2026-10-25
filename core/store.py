"""SQLite 持久化：会话 / 消息 / 工单 / 记忆 / 反馈 / 注入日志 / 配置 / 工具注册表 / 注入规则。

DB 路径每次连接时读环境变量（CS_AGENT_DB），测试无需重载模块。
"""
import json
import os
import secrets as secrets_module
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    return os.environ.get("CS_AGENT_DB", "cs_agent.db")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            visitor_id TEXT,
            secret TEXT,
            status TEXT DEFAULT 'ongoing',
            escalated INTEGER DEFAULT 0,
            escalate_reason TEXT,
            assignee TEXT,
            started_at TEXT,
            ended_at TEXT,
            first_response_at TEXT,
            first_response_seconds INTEGER
        );
        CREATE TABLE IF NOT EXISTS session_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            operator TEXT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_session_notes_sid ON session_notes(session_id, id DESC);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            refs TEXT,
            tool_used TEXT,
            thought TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            priority TEXT DEFAULT 'normal',
            status TEXT DEFAULT 'pending',
            assignee TEXT,
            created_at TEXT,
            resolved_at TEXT,
            duration_s INTEGER
        );
        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visitor_id TEXT,
            key TEXT,
            value TEXT,
            updated_at TEXT,
            UNIQUE(visitor_id, key)
        );
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            message_id INTEGER,
            rating TEXT,
            comment TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS injection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            visitor_id TEXT,
            raw_input TEXT,
            detected TEXT,
            action TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tools (
            id TEXT PRIMARY KEY,
            name TEXT,
            desc TEXT,
            args_json TEXT,
            enabled INTEGER DEFAULT 1,
            builtin INTEGER DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS injection_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expr TEXT,
            action TEXT CHECK(action IN ('block','log_only')) DEFAULT 'block',
            enabled INTEGER DEFAULT 1,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS channel_sessions (
            channel TEXT PRIMARY KEY,
            session_id TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            hits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_annotations_enabled ON annotations(enabled, id DESC);
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # 轻量 migration：老库无 secret 列则补（SQLite 不支持 ADD COLUMN IF NOT EXISTS）
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "secret" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN secret TEXT")
    _ensure_action_schema(conn)
    _ensure_reminder_schema(conn)
    _seed_builtins(conn)


def _ensure_reminder_schema(conn: sqlite3.Connection) -> None:
    """提醒表。必须与迁移 v15 保持同构——测试库只跑 _ensure_schema 不跑迁移。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            visitor_id TEXT NOT NULL,
            chat_key TEXT NOT NULL,
            channel_type TEXT NOT NULL DEFAULT 'webchat',
            group_id TEXT,
            channel_user_id TEXT,
            text TEXT NOT NULL,
            due_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            sent_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_visitor ON reminders(visitor_id, status)")
    # 后加的列：老库没有则补（测试库只跑这里，不跑迁移）
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(reminders)").fetchall()}
    if "repeat_rule" not in rcols:
        conn.execute("ALTER TABLE reminders ADD COLUMN repeat_rule TEXT DEFAULT ''")


def _ensure_action_schema(conn: sqlite3.Connection) -> None:
    """动作层表：审计日志 / 审批 / 审计哈希链。

    只做加法（CREATE IF NOT EXISTS），老库升级不需要停机迁移。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS action_log (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            risk INTEGER NOT NULL,
            params TEXT NOT NULL,
            principal TEXT NOT NULL,
            effective_tier INTEGER,
            session_id TEXT,
            status TEXT NOT NULL,
            approval_id TEXT,
            idempotency_key TEXT UNIQUE,
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            executed_at TEXT,
            duration_ms INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_action_log_created ON action_log(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_action_log_status ON action_log(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_action_log_session ON action_log(session_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            action_log_id TEXT NOT NULL,
            action TEXT NOT NULL,
            summary TEXT NOT NULL,
            detail TEXT NOT NULL,
            nonce_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_by TEXT,
            decided_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_approvals_log ON approvals(action_log_id);

        CREATE TABLE IF NOT EXISTS audit_chain (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor TEXT,
            kind TEXT,
            payload TEXT NOT NULL,
            prev_hash TEXT,
            hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_chain_ts ON audit_chain(ts DESC);

        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            principal TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            tier INTEGER,
            budget_json TEXT,
            usage_json TEXT,
            result TEXT,
            error TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, started_at DESC);

        CREATE TABLE IF NOT EXISTS agent_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            step_no INTEGER NOT NULL,
            thought TEXT,
            action TEXT,
            params TEXT,
            observation TEXT,
            status TEXT,
            duration_ms INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id, step_no);
        """
    )


# 秘书向内置工具（第 2 批）：上下文 / 记忆 / 提醒 / 网页 / 文件 / 识图 / QQ。
# 与业务工具不同，这些是"真能干活"的本地能力，默认全开。
_EXTRA_TOOLS: list[dict] = [
    ("current_context", "读当前对话", "查看这次对话最近说了什么、当前在哪个群", {}),
    ("recall_memory", "回忆关于你的事", "读取长期记忆里保存的信息", {"key": "string"}),
    ("remember", "记住一件事", "把用户的偏好或事实写入长期记忆",
     {"key": "string", "value": "string"}),
    ("remind", "设提醒", "在指定时间提醒用户（如 10分钟后 / 明天9点）",
     {"text": "string", "when": "string"}),
    ("list_reminders", "查看待提醒", "列出还没触发的提醒", {}),
    ("cancel_reminder", "取消提醒", "按编号取消一条提醒", {"reminder_id": "string"}),
    ("read_webpage", "读网页", "打开一个链接并读正文（仅公网地址）",
     {"url": "string", "max_chars": "string"}),
    ("web_search", "联网搜", "搜索实时信息", {"query": "string", "top_k": "string"}),
    ("list_files", "列目录", "列出目录下的文件（受执行器策略白名单限制）",
     {"path": "string", "depth": "string"}),
    ("read_file", "读文件", "读取文本文件（受执行器策略白名单限制）",
     {"path": "string", "max_chars": "string"}),
    ("write_file", "写文件", "写入或追加文本到文件（受执行器策略白名单限制）",
     {"path": "string", "content": "string", "append": "string"}),
    ("describe_image", "看图", "识别图片内容或读取图中文字", {"source": "string", "question": "string"}),
    ("search_emote", "找表情包", "在收藏的表情包里按关键词查找", {"keyword": "string"}),
    ("qq_group_history", "读群记录", "读取群聊最近的聊天记录",
     {"group_id": "string", "count": "string"}),
    ("qq_send", "发QQ消息", "主动往群里或私聊发一条消息", {"text": "string", "target": "string"}),
    ("now", "看时间", "查当前日期、时间和星期", {}),
    ("calc", "算数", "计算数学表达式", {"expression": "string"}),
    ("send_image", "发图片", "把一张图片发到当前会话", {"source": "string"}),
    ("generate_image", "画图", "按描述生成一张图片并发出去", {"prompt": "string", "size": "string"}),
    ("parse_document", "读文档", "解析 PDF/DOCX/XLSX/MD/TXT 并返回正文",
     {"path": "string", "url": "string"}),
    ("qq_group_members", "查群成员", "列出群成员，可按昵称筛选",
     {"group_id": "string", "keyword": "string"}),
    ("qq_groups", "查我的群", "列出机器人加入的 QQ 群", {}),
    ("qq_friends", "查好友", "列出机器人的 QQ 好友", {}),
]


def _extra_tool_seeds() -> list[dict]:
    return [
        {"id": tid, "name": name, "desc": desc,
         "args_json": json.dumps(args, ensure_ascii=False), "builtin": 1}
        for tid, name, desc, args in _EXTRA_TOOLS
    ]


def _backfill_builtin_tools(conn: sqlite3.Connection) -> None:
    """给已存在的库补上后加的内置工具（幂等：只插缺失项）。"""
    existing = {r["id"] for r in conn.execute("SELECT id FROM tools").fetchall()}
    for t in _extra_tool_seeds():
        if t["id"] in existing:
            continue
        conn.execute(
            "INSERT INTO tools (id, name, desc, args_json, enabled, builtin, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (t["id"], t["name"], t["desc"], t["args_json"], t["builtin"], _now()),
        )


def _seed_builtins(conn: sqlite3.Connection) -> None:
    existing_tools = conn.execute("SELECT COUNT(*) AS c FROM tools").fetchone()["c"]
    if existing_tools == 0:
        builtin_tools = [
            {"id": "query_order", "name": "查订单物流", "desc": "按订单号查询发货状态与物流单号",
             "args_json": json.dumps({"order_id": "string"}, ensure_ascii=False), "builtin": 1},
            {"id": "initiate_refund", "name": "申请退款", "desc": "按订单号发起退款并返回处理状态",
             "args_json": json.dumps({"order_id": "string", "reason": "string"}, ensure_ascii=False), "builtin": 1},
            {"id": "lookup_policy", "name": "查退改政策", "desc": "按主题检索退改签政策",
             "args_json": json.dumps({"topic": "string"}, ensure_ascii=False), "builtin": 1},
        ]
        builtin_tools += _extra_tool_seeds()
        for t in builtin_tools:
            conn.execute(
                "INSERT INTO tools (id, name, desc, args_json, enabled, builtin, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                (t["id"], t["name"], t["desc"], t["args_json"], t["builtin"], _now()),
            )
    else:
        # 老库补齐新内置工具：只补缺失的 id，不改用户已改过的描述/开关
        _backfill_builtin_tools(conn)
    existing_rules = conn.execute("SELECT COUNT(*) AS c FROM injection_rules").fetchone()["c"]
    if existing_rules == 0:
        default_exprs = [
            r"(?i)(ignore|disregard)\s+all\s+(previous|prior)\s+instructions",
            r"忽略.{0,12}(指令|指示|规则|提示)",
            r"(you are now|pretend to be|act as)\s*(a\s+)?(unrestricted|new|an unrestricted)",
            r"(unrestricted|无限制|解除限制)\s*(模式)?",
            r"(reveal|show|print|输出|显示)\s*(your\s+)?(system\s+prompt|系统提示|内部指令)",
            r"(act as|扮演)\s*(a\s+)?(new|unrestricted|无限制)",
            r"(jailbreak|越狱)",
        ]
        for expr in default_exprs:
            conn.execute(
                "INSERT INTO injection_rules (expr, action, enabled, created_at) VALUES (?, 'block', 1, ?)",
                (expr, _now()),
            )


_SCHEMA_READY: set[str] = set()


def _connect_for_tx() -> sqlite3.Connection:
    """为需要显式事务控制的场景创建独立连接（isolation_level=None 手动管事务）。"""
    path = _db_path()
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# 线程本地连接缓存：SQLite 建连接有固定开销（实测约 1.1ms/次），
# 一次对话请求要访问 10+ 次，累计 12-18ms 全是重复建连。
# 复用后同线程的连续查询不再付出这个成本。
_LOCAL = threading.local()


def _close_all_connections() -> None:
    """关闭当前线程持有的连接（测试切换 DB 路径时用）。"""
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _LOCAL.conn = None
    _LOCAL.path = None


def _conn_for(path: str) -> sqlite3.Connection:
    """取本线程到指定库的连接，路径变化时重建。"""
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None and getattr(_LOCAL, "path", None) == path:
        return conn
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    # 容器里 DB 指向持久卷路径（如 /data/cs_agent.db），卷首次挂载时目录可能尚不存在，
    # sqlite3.connect 不会自己建目录，会直接抛 "unable to open database file"。
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 每连接都要设：busy_timeout 与 foreign_keys 是连接级；journal_mode 是库级持久设置，首次建库时设一次
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    if path not in _SCHEMA_READY:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass  # :memory: 等不支持 WAL 的场景不影响功能
        _ensure_schema(conn)
        _SCHEMA_READY.add(path)
    _LOCAL.conn = conn
    _LOCAL.path = path
    return conn


@contextmanager
def get_conn():
    """获取数据库连接（线程本地复用）。

    注意：不再在退出时 close()，连接留给同线程复用；
    事务提交仍在退出时执行，异常则回滚。
    """
    conn = _conn_for(_db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise


def rules_version() -> int:
    """注入规则版本号，存 DB 元表：多 worker 部署下各进程读同一信号，CRUD 即时生效。"""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'rules_version'").fetchone()
    return int(row["value"]) if row else 0


def _bump_rules_version() -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('rules_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
        )


def init_db() -> None:
    """初始化数据库：建表 + 应用迁移。

    迁移失败时抛异常——不允许带着半迁移状态对外服务。
    """
    from core import migrations

    with get_conn() as conn:
        _ensure_schema(conn)
        result = migrations.migrate(conn)
        if not result["ok"]:
            raise RuntimeError(f"数据库迁移失败：{result['error']}")
        if result["applied"]:
            import logging

            logging.getLogger("core").info(
                "schema migrated %s -> %s (applied %s)",
                result["from"], result["to"], result["applied"],
            )


def schema_version() -> int:
    from core import migrations

    with get_conn() as conn:
        return migrations.current_version(conn)


def create_session(session_id: str, visitor_id: str, secret: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, visitor_id, secret, status, started_at) VALUES (?, ?, ?, 'ongoing', ?)",
            (session_id, visitor_id, secret, _now()),
        )


def check_session_secret(session_id: str, secret: str) -> bool:
    """校验访客会话归属：sid + secret 双因子。未设置 secret 的老会话/渠道会话一律拒绝 web 写操作。"""
    if not secret:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT secret FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row or not row["secret"]:
        return False
    import secrets as _secrets
    # compare_digest 对含非 ASCII 的 str 会抛 TypeError（攻击者可用来把接口打成 500）。
    # 统一编码成 bytes 再比，任何异常都按"不匹配"处理。
    try:
        return _secrets.compare_digest(str(row["secret"]).encode("utf-8"), str(secret).encode("utf-8"))
    except (UnicodeError, TypeError):
        return False


def add_message(session_id: str, role: str, content: str, refs: list | None = None,
                tool_used: str | None = None, thought: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (session_id, role, content, refs, tool_used, thought, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, json.dumps(refs or []), tool_used,
             (thought or None), _now()),
        )
        # SLA 首响计时（Chatwoot 对标）：首条 agent/operator 回复落库即记
        if role in ("agent", "operator"):
            row = conn.execute(
                "SELECT started_at, first_response_at FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row and row["first_response_at"] is None and row["started_at"]:
                try:
                    from datetime import datetime, timezone
                    t0 = datetime.fromisoformat(row["started_at"])
                    secs = int((datetime.now(timezone.utc) - t0).total_seconds())
                except (ValueError, TypeError):
                    secs = None
                conn.execute(
                    "UPDATE sessions SET first_response_at = ?, first_response_seconds = ? "
                    "WHERE id = ? AND first_response_at IS NULL",
                    (_now(), secs, session_id),
                )
        return cur.lastrowid or 0


def get_messages(session_id: str, limit: int | None = None,
                 include_deleted: bool = True) -> list[dict]:
    """读取会话消息，按时间正序。

    limit 给定时只取最近 N 条（倒序查再翻转），避免长会话全量加载：
    实测 2000 条历史会让上下文构建耗时从 18ms 涨到 37ms。
    include_deleted=False 用于模型上下文：删掉的话不能再被模型看到。
    """
    cond = "" if include_deleted else " AND deleted = 0"
    with get_conn() as conn:
        if limit and limit > 0:
            rows = conn.execute(
                "SELECT id, role, content, refs, tool_used, thought, deleted, created_at FROM ("
                "  SELECT id, role, content, refs, tool_used, thought, deleted, created_at "
                f"  FROM messages WHERE session_id = ?{cond} ORDER BY id DESC LIMIT ?"
                ") ORDER BY id",
                (session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, role, content, refs, tool_used, thought, deleted, created_at "
                f"FROM messages WHERE session_id = ?{cond} ORDER BY id",
                (session_id,),
            ).fetchall()
    return [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "refs": json.loads(r["refs"] or "[]"),
            "tool_used": r["tool_used"],
            "thought": r["thought"] or "",
            "deleted": bool(r["deleted"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ---------------- 访客身份（避免自报 visitor_id 读到他人记忆） ----------------

def visitor_claim_or_verify(visitor_id: str, token: str) -> tuple[bool, str]:
    """首次声明 visitor_id 的调用方获得令牌；已有记录则必须出示匹配令牌。

    返回 (ok, reason)：ok=False 时 reason ∈ {"taken", "bad_token"}。
    只存令牌的 SHA-256，泄库也无法反推。
    """
    import hashlib

    vid = (visitor_id or "").strip()
    if not vid:
        return False, "bad_token"
    digest = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    with get_conn() as conn:
        row = conn.execute("SELECT token_hash FROM visitors WHERE id = ?", (vid,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO visitors (id, token_hash, created_at) VALUES (?, ?, ?)",
                (vid, digest, _now()),
            )
            return True, "created"
        stored = row["token_hash"] or ""
    ok = bool(stored) and secrets_module.compare_digest(stored.encode(), digest.encode())
    return (True, "verified") if ok else (False, "bad_token")


def visitor_exists(visitor_id: str) -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM visitors WHERE id = ?", (visitor_id,)).fetchone() is not None


def delete_message(session_id: str, message_id: int) -> bool:
    """软删除一条消息（只允许删本会话内的）。返回是否命中。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE messages SET deleted = 1 WHERE id = ? AND session_id = ? AND deleted = 0",
            (message_id, session_id),
        )
        return cur.rowcount > 0


def get_session(session_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def mark_escalated(session_id: str, reason: str) -> None:
    """标记升级。已关闭的会话不允许被"复活"成待接管。

    之前无条件改成 pending_agent，导致关闭后访客再发一条消息就把已结束
    的会话重新拉回队列（已复现），工单也会被重开。
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET escalated = 1, status = 'pending_agent', escalate_reason = ? "
            "WHERE id = ? AND status IN ('ongoing', 'agent_handled')",
            (reason, session_id),
        )


def set_status(session_id: str, status: str, assignee: str | None = None) -> bool:
    """更新会话状态。返回是否命中（False 表示会话不存在）。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sessions SET status = ?, assignee = COALESCE(?, assignee) WHERE id = ?",
            (status, assignee, session_id),
        )
    return cur.rowcount > 0


def close_session(session_id: str) -> None:
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET status = 'closed', ended_at = ? WHERE id = ?",
            (now, session_id),
        )
        conn.execute(
            "UPDATE tickets SET status = 'resolved', resolved_at = ?, "
            "duration_s = CAST((julianday(? ) - julianday(created_at)) * 86400 AS INTEGER) "
            "WHERE session_id = ? AND status = 'pending'",
            (now, now, session_id),
        )


def create_ticket(session_id: str, priority: str = "normal") -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM tickets WHERE session_id = ? AND status = 'pending'",
            (session_id,),
        ).fetchone()
        if row:
            return row["id"]
        ticket_id = f"T-{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO tickets (id, session_id, priority, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (ticket_id, session_id, priority, _now()),
        )
    return ticket_id


def pending_queues() -> dict:
    with get_conn() as conn:
        pending_sessions = conn.execute(
            "SELECT id, visitor_id, escalate_reason, started_at FROM sessions WHERE status = 'pending_agent'"
        ).fetchall()
        tickets = conn.execute(
            "SELECT id, session_id, priority, status, assignee, created_at FROM tickets "
            "WHERE status != 'resolved' ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return {
        "sessions": [dict(r) for r in pending_sessions],
        "tickets": [dict(r) for r in tickets],
    }


def get_channel_session(channel: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT channel, session_id, updated_at FROM channel_sessions WHERE channel = ?",
            (channel,),
        ).fetchone()
    return dict(row) if row else None


def set_channel_session(channel: str, session_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO channel_sessions (channel, session_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(channel) DO UPDATE SET session_id = excluded.session_id, updated_at = excluded.updated_at",
            (channel, session_id, _now()),
        )


def claim_channel_session(channel: str, session_id: str) -> bool:
    """原子认领渠道映射：INSERT OR IGNORE，channel 主键冲突即认领失败。并发安全的会话创建原语。"""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO channel_sessions (channel, session_id, updated_at) VALUES (?, ?, ?)",
            (channel, session_id, _now()),
        )
    return cur.rowcount > 0


def touch_channel_session(channel: str, session_id: str | None = None) -> None:
    """刷新渠道映射的最后活动时间（TTL 语义：从最后一次活动起算空闲时长）。

    传 session_id 时只在映射仍指向该会话时才刷新，避免并发下把别人的新映射写旧。
    """
    now = _now()
    with get_conn() as conn:
        if session_id is None:
            conn.execute("UPDATE channel_sessions SET updated_at = ? WHERE channel = ?", (now, channel))
        else:
            conn.execute(
                "UPDATE channel_sessions SET updated_at = ? WHERE channel = ? AND session_id = ?",
                (now, channel, session_id),
            )


def replace_channel_session(channel: str, session_id: str) -> bool:
    """覆盖渠道映射（TTL 过期换新会话用）。与 claim 的区别：允许覆盖已存在的行。

    返回 True 表示写入后该 channel 确实指向 session_id（即自己是赢家）。
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO channel_sessions (channel, session_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(channel) DO UPDATE SET session_id = excluded.session_id, updated_at = excluded.updated_at",
            (channel, session_id, _now()),
        )
        row = conn.execute(
            "SELECT session_id FROM channel_sessions WHERE channel = ?", (channel,)
        ).fetchone()
    return bool(row and row["session_id"] == session_id)


def discard_session(session_id: str) -> None:
    """删除零消息空会话（并发认领输家清理用）；一旦有消息则保留，绝不丢数据。"""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE id = ? "
            "AND NOT EXISTS (SELECT 1 FROM messages WHERE messages.session_id = sessions.id)",
            (session_id,),
        )


def set_memory(visitor_id: str, key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO memory (visitor_id, key, value, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(visitor_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (visitor_id, key, value, _now()),
        )


def get_memory(visitor_id: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, value FROM memory WHERE visitor_id = ?", (visitor_id,)
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


def clear_memory(visitor_id: str, opt_out: bool = False) -> int:
    """删除某访客全部记忆；返回删除条数。

    opt_out=True 时同时写入持久化的退出标记——否则访客说"别记我"之后，
    下一条消息又会被采集（已复现：清空后发"我喜欢蓝色"又写入 pref_喜欢）。
    """
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM memory WHERE visitor_id = ?", (visitor_id,))
        removed = cur.rowcount or 0
        if opt_out:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'",
                (f"mem_opt_out:{visitor_id}",),
            )
    return removed


def memory_opted_out(visitor_id: str) -> bool:
    """该访客是否已明确要求不再记录。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"mem_opt_out:{visitor_id}",)
        ).fetchone()
    return bool(row and row["value"] == "1")


def clear_memory_opt_out(visitor_id: str) -> None:
    """访客显式要求恢复记忆时调用。"""
    with get_conn() as conn:
        conn.execute("DELETE FROM meta WHERE key = ?", (f"mem_opt_out:{visitor_id}",))


def all_memory() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT visitor_id, key, value, updated_at FROM memory ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def session_count(visitor_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM sessions WHERE visitor_id = ?", (visitor_id,)
        ).fetchone()
    return row["c"] if row else 0


def add_feedback(session_id: str, message_id: int, rating: str, comment: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO feedback (session_id, message_id, rating, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, message_id, rating, comment, _now()),
        )


def upsert_feedback(session_id: str, message_id: int, rating: str, comment: str = "") -> None:
    """同一条消息只保留最后一次评价（重复点击不能刷好评率）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM feedback WHERE session_id = ? AND message_id = ? LIMIT 1",
            (session_id, message_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE feedback SET rating = ?, comment = ?, created_at = ? WHERE id = ?",
                (rating, comment, _now(), row["id"]),
            )
        else:
            # 并发下两个请求可能都走到这里：唯一索引兜底，冲突时退化为更新。
            # 注意 SQLite 的 ON CONFLICT 目标不能指向部分索引，只能显式捕获。
            import sqlite3 as _sqlite3

            try:
                conn.execute(
                    "INSERT INTO feedback (session_id, message_id, rating, comment, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, message_id, rating, comment, _now()),
                )
            except _sqlite3.IntegrityError:
                conn.execute(
                    "UPDATE feedback SET rating = ?, comment = ?, created_at = ? "
                    "WHERE session_id = ? AND message_id = ?",
                    (rating, comment, _now(), session_id, message_id),
                )


def feedback_summary() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT rating, COUNT(*) AS c FROM feedback GROUP BY rating"
        ).fetchall()
    total = sum(r["c"] for r in rows)
    helpful = next((r["c"] for r in rows if r["rating"] == "helpful"), 0)
    not_helpful = next((r["c"] for r in rows if r["rating"] == "not_helpful"), 0)
    return {
        "total": total,
        "helpful": helpful,
        "not_helpful": not_helpful,
        "help_rate": round(helpful / total, 3) if total else 0.0,
    }


_CHANNEL_PREFIXES = ("qq_group", "qq_private", "wecom", "mp", "telegram")


def _channel_of(visitor_id: str) -> str:
    return visitor_id.split(":", 1)[0] if visitor_id.startswith(_CHANNEL_PREFIXES) else "webchat"


def admin_sessions(limit: int = 50, offset: int = 0, status: str | None = None,
                   q: str | None = None) -> dict:
    """会话列表：按状态过滤 + 关键字搜索（访客 ID / 会话 ID / 任意消息内容）。"""
    clauses, params = [], []
    if status:
        clauses.append("s.status = ?")
        params.append(status)
    q = (q or "").strip()
    if q:
        # LIKE 通配符必须转义：否则用户输入 "%" 会命中整表（已复现）
        esc = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        like = f"%{esc}%"
        q_clause = (
            "(s.visitor_id LIKE ? ESCAPE '\\' OR s.id LIKE ? ESCAPE '\\' OR EXISTS ("
            " SELECT 1 FROM messages m WHERE m.session_id = s.id"
            " AND m.content LIKE ? ESCAPE '\\'))"
        )
        clauses.append(q_clause)
        params.extend([like, like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM sessions s {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT s.id, s.visitor_id, s.status, s.escalated, s.escalate_reason, s.assignee, "
            f"s.started_at, s.ended_at, s.first_response_seconds, "
            f"(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) message_count "
            f"FROM sessions s {where} ORDER BY s.started_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["channel"] = _channel_of(d.get("visitor_id") or "")
        items.append(d)
    return {"total": total, "items": items}


def admin_stats() -> dict:
    hour_cutoff = (
        (datetime.now(timezone.utc) - timedelta(hours=23))
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
    )
    day_cutoff = (datetime.now(timezone.utc) - timedelta(days=6)).date().isoformat()
    with get_conn() as conn:
        msg_rows = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:00', datetime(created_at, 'localtime')) bucket, role, COUNT(*) c "
            "FROM messages WHERE created_at >= ? GROUP BY bucket, role",
            (hour_cutoff,),
        ).fetchall()
        sess_rows = conn.execute(
            "SELECT date(datetime(started_at, 'localtime')) day, COUNT(*) c "
            "FROM sessions WHERE started_at >= ? GROUP BY day",
            (day_cutoff,),
        ).fetchall()
        esc_rows = conn.execute(
            "SELECT escalate_reason reason, COUNT(*) c FROM sessions WHERE escalated = 1 "
            "GROUP BY escalate_reason ORDER BY c DESC"
        ).fetchall()
        tool_rows = conn.execute(
            "SELECT tool_used tool, COUNT(*) c FROM messages "
            "WHERE tool_used IS NOT NULL AND tool_used != '' AND role = 'agent' "
            "GROUP BY tool ORDER BY c DESC"
        ).fetchall()
        vid_rows = conn.execute("SELECT visitor_id FROM sessions").fetchall()
        fb_rows = conn.execute("SELECT rating, COUNT(*) c FROM feedback GROUP BY rating").fetchall()
        kb24 = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE role='agent' AND refs != '[]' AND created_at >= ?",
            (hour_cutoff,),
        ).fetchone()["c"]
        tool24 = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE role='agent' AND tool_used IS NOT NULL AND tool_used != '' "
            "AND tool_used != 'injection_guard' AND created_at >= ?",
            (hour_cutoff,),
        ).fetchone()["c"]
        guard24 = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE role='agent' AND tool_used = 'injection_guard' AND created_at >= ?",
            (hour_cutoff,),
        ).fetchone()["c"]
        esc24 = conn.execute(
            "SELECT COUNT(*) c FROM sessions WHERE escalated = 1 AND started_at >= ?",
            (hour_cutoff,),
        ).fetchone()["c"]

    now = datetime.now()
    buckets = [(now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(23, -1, -1)]
    by_role: dict[str, dict[str, int]] = {"visitor": {}, "agent": {}, "operator": {}}
    for r in msg_rows:
        role = r["role"] if r["role"] in by_role else "visitor"
        by_role[role][r["bucket"]] = r["c"]
    messages_by_hour = [
        {
            "bucket": b,
            "visitor": by_role["visitor"].get(b, 0),
            "agent": by_role["agent"].get(b, 0),
            "operator": by_role["operator"].get(b, 0),
        }
        for b in buckets
    ]

    today = datetime.now().date()
    days = [(today - timedelta(days=6 - i)).isoformat() for i in range(7)]
    day_map = {r["day"]: r["c"] for r in sess_rows}
    sessions_by_day = [{"day": d, "count": day_map.get(d, 0)} for d in days]

    channels: dict[str, int] = {}
    for r in vid_rows:
        ch = _channel_of(r["visitor_id"] or "")
        channels[ch] = channels.get(ch, 0) + 1

    fb_map = {r["rating"]: r["c"] for r in fb_rows}
    return {
        "messages_by_hour": messages_by_hour,
        "sessions_by_day": sessions_by_day,
        "escalate_reasons": [{"reason": r["reason"] or "unknown", "count": r["c"]} for r in esc_rows],
        "tool_usage": [{"tool": r["tool"], "count": r["c"]} for r in tool_rows],
        "channels": [{"channel": k, "count": v} for k, v in sorted(channels.items())],
        "feedback": {"helpful": fb_map.get("helpful", 0), "not_helpful": fb_map.get("not_helpful", 0)},
        "funnel": {
            "messages": sum(
                m["visitor"] + m["agent"] + m["operator"] for m in messages_by_hour
            ),
            "kb_answered": kb24,
            "tool_calls": tool24,
            "guardrail_blocked": guard24,
            "escalations": esc24,
        },
    }


def admin_report(days: int = 7) -> dict:
    """经营报表：日序列（会话/回复/KB命中/升级）+ 汇总（命中率/升级率/CSAT）。纯 SQL 聚合，零新表。"""
    days = max(1, min(days, 90))
    day_cutoff = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
    with get_conn() as conn:
        sess_rows = conn.execute(
            "SELECT date(datetime(started_at,'localtime')) d, COUNT(*) c FROM sessions "
            "WHERE date(started_at) >= ? GROUP BY d",
            (day_cutoff,),
        ).fetchall()
        agent_rows = conn.execute(
            "SELECT date(datetime(created_at,'localtime')) d, COUNT(*) c, "
            "SUM(CASE WHEN refs IS NOT NULL AND refs NOT IN ('', '[]') THEN 1 ELSE 0 END) kb "
            "FROM messages WHERE role = 'agent' AND date(created_at) >= ? GROUP BY d",
            (day_cutoff,),
        ).fetchall()
        esc_rows = conn.execute(
            "SELECT date(datetime(started_at,'localtime')) d, COUNT(*) c FROM sessions "
            "WHERE escalated = 1 AND date(started_at) >= ? GROUP BY d",
            (day_cutoff,),
        ).fetchall()
        fb = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN rating = 'helpful' THEN 1 ELSE 0 END) good "
            "FROM feedback WHERE date(created_at) >= ?",
            (day_cutoff,),
        ).fetchone()

    today = datetime.now().date()
    day_list = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    sess_map = {r["d"]: r["c"] for r in sess_rows}
    agent_map = {r["d"]: (r["c"], r["kb"] or 0) for r in agent_rows}
    esc_map = {r["d"]: r["c"] for r in esc_rows}

    daily = []
    tot = {"sessions": 0, "agent_replies": 0, "kb_hits": 0, "escalations": 0}
    for d in day_list:
        replies, kb = agent_map.get(d, (0, 0))
        row = {
            "day": d,
            "sessions": sess_map.get(d, 0),
            "agent_replies": replies,
            "kb_hits": kb,
            "escalations": esc_map.get(d, 0),
        }
        daily.append(row)
        tot["sessions"] += row["sessions"]
        tot["agent_replies"] += replies
        tot["kb_hits"] += kb
        tot["escalations"] += row["escalations"]

    n_fb = fb["n"] or 0
    good = fb["good"] or 0
    return {
        "days": days,
        "daily": daily,
        "totals": {
            **tot,
            "kb_hit_rate": round(tot["kb_hits"] / tot["agent_replies"], 3) if tot["agent_replies"] else None,
            "escalate_rate": round(tot["escalations"] / tot["sessions"], 3) if tot["sessions"] else None,
            "csat": round(good / n_fb, 3) if n_fb else None,
            "csat_n": n_fb,
        },
    }


def log_injection(session_id: str, visitor_id: str, raw_input: str,
                  detected: str, action: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO injection_log (session_id, visitor_id, raw_input, detected, action, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, visitor_id, raw_input, detected, action, _now()),
        )


def injection_log(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, session_id, visitor_id, raw_input, detected, action, created_at "
            "FROM injection_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_config() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
    return {r["key"]: json.loads(r["value"] or "{}") for r in rows}


def config_version() -> int:
    """配置版本号：每次 set_config 递增，供上游缓存判断是否失效。"""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'config_version'").fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def _bump_config_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('config_version', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
    )


def set_config(patch: dict) -> None:
    with get_conn() as conn:
        for group, value in (patch or {}).items():
            row = conn.execute("SELECT value FROM config WHERE key = ?", (group,)).fetchone()
            current = json.loads(row["value"]) if row and row["value"] else {}
            if isinstance(value, dict) and isinstance(current, dict):
                merged = {**current, **value}
            else:
                merged = value
            conn.execute(
                "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (group, json.dumps(merged, ensure_ascii=False), _now()),
            )
        _bump_config_version(conn)


def list_tools() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, desc, args_json, enabled, builtin FROM tools ORDER BY id"
        ).fetchall()
    return [_tool_row(r) for r in rows]


def active_tools() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, desc, args_json, enabled, builtin FROM tools WHERE enabled = 1 ORDER BY id"
        ).fetchall()
    return [_tool_row(r) for r in rows]


def _tool_row(r) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "desc": r["desc"],
        "args_json": json.loads(r["args_json"] or "{}"),
        "enabled": bool(r["enabled"]),
        "builtin": bool(r["builtin"]),
    }


def get_tool(tool_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tools WHERE id = ?", (tool_id,)).fetchone()
    return _tool_row(row) if row else None


def upsert_tool(tool_id: str, name: str, desc: str, args_json: dict,
                enabled: bool = True, builtin: bool = False) -> dict:
    if not isinstance(args_json, dict):
        raise ValueError("args_json 必须是对象（参数名 → 类型）")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tools (id, name, desc, args_json, enabled, builtin, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name = excluded.name, desc = excluded.desc, "
            "args_json = excluded.args_json, enabled = excluded.enabled, "
            "builtin = excluded.builtin, updated_at = excluded.updated_at",
            (tool_id, name, desc, json.dumps(args_json, ensure_ascii=False),
             1 if enabled else 0, 1 if builtin else 0, _now()),
        )
    return get_tool(tool_id)


def set_tool_enabled(tool_id: str, enabled: bool) -> dict | None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tools SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), tool_id),
        )
    return get_tool(tool_id)


def delete_tool(tool_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM tools WHERE id = ?", (tool_id,))
    return cur.rowcount > 0


def list_injection_rules(enabled_only: bool = False) -> list[dict]:
    q = "SELECT id, expr, action, enabled FROM injection_rules"
    if enabled_only:
        q += " WHERE enabled = 1"
    q += " ORDER BY id"
    with get_conn() as conn:
        rows = conn.execute(q).fetchall()
    return [
        {"id": r["id"], "expr": r["expr"], "action": r["action"], "enabled": bool(r["enabled"])}
        for r in rows
    ]


def get_injection_rule(rule_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM injection_rules WHERE id = ?", (rule_id,)).fetchone()
    return dict(row) if row else None


def add_injection_rule(expr: str, action: str = "block", enabled: bool = True) -> dict:
    if action not in ("block", "log_only"):
        raise ValueError("action 必须是 block 或 log_only")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO injection_rules (expr, action, enabled, created_at) VALUES (?, ?, ?, ?)",
            (expr, action, 1 if enabled else 0, _now()),
        )
        rid = cur.lastrowid or 0
    _bump_rules_version()
    return {"id": rid, "expr": expr, "action": action, "enabled": bool(enabled)}


def update_injection_rule(rule_id: int, expr: str | None = None,
                          action: str | None = None, enabled: bool | None = None) -> dict | None:
    sets, params = [], []
    if expr is not None:
        sets.append("expr = ?")
        params.append(expr)
    if action is not None:
        if action not in ("block", "log_only"):
            raise ValueError("action 必须是 block 或 log_only")
        sets.append("action = ?")
        params.append(action)
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if sets:
        params.append(rule_id)
        with get_conn() as conn:
            conn.execute(f"UPDATE injection_rules SET {', '.join(sets)} WHERE id = ?", params)
        _bump_rules_version()
    return get_injection_rule(rule_id)


def delete_injection_rule(rule_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM injection_rules WHERE id = ?", (rule_id,))
    changed = cur.rowcount > 0
    if changed:
        _bump_rules_version()
    return changed


# ============================================================
# 动作层：审计日志 / 审批 / 审计哈希链
# ============================================================

def _audit_hash(prev: str, ts: str, actor: str, kind: str, payload: str) -> str:
    import hashlib

    raw = f"{prev}|{ts}|{actor}|{kind}|{payload}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def append_audit(actor: str, kind: str, payload: dict) -> str:
    """向哈希链追加一条审计记录，返回本条 hash。

    prev_hash 取上一条的 hash，改任何一行都会导致后续校验失败。
    """
    import json as _json

    ts = _now()
    body = _json.dumps(payload, ensure_ascii=False, sort_keys=True)
    # BEGIN IMMEDIATE 立刻拿写锁：否则「读上一条 hash」与「插入」之间会被其他
    # 写者插入，导致两条记录指向同一个 prev_hash —— 链分叉，verify 必然失败。
    # 已复现：8 线程并发写 160 条，出现 11 组分叉。
    conn = _connect_for_tx()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT hash FROM audit_chain ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else ""
        h = _audit_hash(prev, ts, actor, kind, body)
        conn.execute(
            "INSERT INTO audit_chain (ts, actor, kind, payload, prev_hash, hash) VALUES (?,?,?,?,?,?)",
            (ts, actor, kind, body, prev, h),
        )
        conn.execute("COMMIT")
        return h
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def verify_audit_chain(start_id: int | None = None) -> dict:
    """校验哈希链完整性。返回 {ok, checked, broken_at}。"""

    sql = "SELECT id, ts, actor, kind, payload, prev_hash, hash FROM audit_chain"
    params: list = []
    if start_id is not None:
        sql += " WHERE id >= ?"
        params.append(start_id)
    sql += " ORDER BY id"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    prev = ""
    if start_id is not None and rows:
        with get_conn() as conn:
            before = conn.execute(
                "SELECT hash FROM audit_chain WHERE id < ? ORDER BY id DESC LIMIT 1", (start_id,)
            ).fetchone()
        prev = before["hash"] if before else ""
    for r in rows:
        expect = _audit_hash(prev, r["ts"], r["actor"] or "", r["kind"] or "", r["payload"])
        if expect != r["hash"]:
            return {"ok": False, "checked": len(rows), "broken_at": r["id"]}
        prev = r["hash"]
    return {"ok": True, "checked": len(rows), "broken_at": None}


def action_log_create(log_id: str, action: str, risk: int, params: dict, principal: str,
                      effective_tier: int | None, session_id: str | None,
                      idempotency_key: str | None = None) -> dict:
    import json as _json

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO action_log (id, action, risk, params, principal, effective_tier, session_id, "
            "status, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,'created',?,?)",
            (log_id, action, risk, _json.dumps(params, ensure_ascii=False), principal,
             effective_tier, session_id, idempotency_key, _now()),
        )
    return action_log_get(log_id) or {}


def action_log_get(log_id: str) -> dict | None:
    import json as _json

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM action_log WHERE id = ?", (log_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("params", "result"):
        if d.get(k):
            try:
                d[k] = _json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d


def action_log_by_idempotency(key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM action_log WHERE idempotency_key = ?", (key,)).fetchone()
    return action_log_get(row["id"]) if row else None


def action_log_update(log_id: str, **fields) -> None:
    import json as _json

    allowed = {"status", "result", "error", "approval_id", "executed_at", "duration_ms"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "result" and not isinstance(v, str):
            v = _json.dumps(v, ensure_ascii=False)
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return
    params.append(log_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE action_log SET {', '.join(sets)} WHERE id = ?", params)


def action_log_list(limit: int = 100, status: str | None = None, action: str | None = None,
                    risk: int | None = None) -> list[dict]:
    import json as _json

    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(status)
    if action:
        where.append("action = ?")
        params.append(action)
    if risk is not None:
        where.append("risk = ?")
        params.append(risk)
    sql = "SELECT * FROM action_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("params", "result"):
            if d.get(k):
                try:
                    d[k] = _json.loads(d[k])
                except (TypeError, ValueError):
                    pass
        out.append(d)
    return out


def approval_create(ap_id: str, action_log_id: str, action: str, summary: str, detail: dict,
                    nonce_hash: str, expires_at: str) -> dict:
    import json as _json

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO approvals (id, action_log_id, action, summary, detail, nonce_hash, status, "
            "expires_at, created_at) VALUES (?,?,?,?,?,?,'pending',?,?)",
            (ap_id, action_log_id, action, summary, _json.dumps(detail, ensure_ascii=False),
             nonce_hash, expires_at, _now()),
        )
    return approval_get(ap_id) or {}


def approval_get(ap_id: str) -> dict | None:
    import json as _json

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (ap_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("detail"):
        try:
            d["detail"] = _json.loads(d["detail"])
        except (TypeError, ValueError):
            pass
    return d


def approval_list(status: str = "pending", limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, action_log_id, action, summary, status, expires_at, created_at "
            "FROM approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def approval_decide(ap_id: str, approve: bool, decided_by: str) -> dict | None:
    """原子决策：只有 pending 能变成 approved/denied，重复点击不会二次执行。"""
    new_status = "approved" if approve else "denied"
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (new_status, decided_by, _now(), ap_id),
        )
        won = cur.rowcount > 0
    d = approval_get(ap_id)
    if d is not None:
        d["_won"] = won
    return d


def approval_expire(ap_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE approvals SET status = 'expired' WHERE id = ? AND status = 'pending'", (ap_id,)
        )


# ============================================================
# Agent 运行（多步 ReAct）
# ============================================================

def agent_run_create(run_id: str, session_id: str | None, principal: str, goal: str,
                     tier: int, budget: dict) -> dict:
    import json as _json

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_runs (id, session_id, principal, goal, status, tier, "
            "budget_json, usage_json, started_at) VALUES (?,?,?,?,'running',?,?,?,?)",
            (run_id, session_id, principal, goal[:2000], tier,
             _json.dumps(budget, ensure_ascii=False),
             _json.dumps({"steps": 0, "tool_calls": 0, "tokens": 0, "cost": 0.0}), _now()),
        )
    return agent_run_get(run_id) or {}


def agent_run_get(run_id: str) -> dict | None:
    import json as _json

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("budget_json", "usage_json", "result"):
        if d.get(k):
            try:
                d[k] = _json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d


def agent_run_update(run_id: str, **fields) -> None:
    import json as _json

    allowed = {"status", "usage_json", "result", "error", "ended_at"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("usage_json", "result") and not isinstance(v, str):
            v = _json.dumps(v, ensure_ascii=False)
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return
    params.append(run_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE agent_runs SET {', '.join(sets)} WHERE id = ?", params)


def agent_step_add(run_id: str, step_no: int, thought: str, action: str | None,
                   params: dict | None, observation: str, status: str,
                   duration_ms: int = 0) -> int:
    import json as _json

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO agent_steps (run_id, step_no, thought, action, params, observation, "
            "status, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, step_no, (thought or "")[:4000], action,
             _json.dumps(params or {}, ensure_ascii=False) if params is not None else None,
             (observation or "")[:8000], status, duration_ms, _now()),
        )
        return cur.lastrowid or 0


def agent_steps_list(run_id: str) -> list[dict]:
    import json as _json

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY step_no, id", (run_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("params"):
            try:
                d["params"] = _json.loads(d["params"])
            except (TypeError, ValueError):
                pass
        out.append(d)
    return out


def agent_runs_list(limit: int = 50, status: str | None = None) -> list[dict]:
    sql = "SELECT id, session_id, principal, goal, status, tier, started_at, ended_at " \
          "FROM agent_runs"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# 跨进程事件（outbox）：多 worker 部署时替代内存队列
# ============================================================

def outbox_put(topic: str, payload: dict, next_at: str | None = None) -> int:
    """投递一条事件。消费者轮询 status='pending' 的记录。"""
    import json as _json

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO outbox (topic, payload, status, attempts, next_at, created_at) "
            "VALUES (?, ?, 'pending', 0, ?, ?)",
            (topic, _json.dumps(payload, ensure_ascii=False), next_at, _now()),
        )
        return cur.lastrowid or 0


def outbox_claim(limit: int = 20) -> list[dict]:
    """认领待处理事件（至少一次投递语义）。

    用 UPDATE ... RETURNING 在同一事务里标记，避免两个 worker 拿到同一条。
    """
    import json as _json

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM outbox WHERE status = 'pending' "
            "AND (next_at IS NULL OR next_at <= ?) ORDER BY id LIMIT ?",
            (_now(), limit),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE outbox SET status = 'processing' WHERE id IN ({marks})", ids)
        out = conn.execute(f"SELECT * FROM outbox WHERE id IN ({marks})", ids).fetchall()
    result = []
    for r in out:
        d = dict(r)
        try:
            d["payload"] = _json.loads(d["payload"])
        except (TypeError, ValueError):
            pass
        result.append(d)
    return result


def outbox_done(event_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE outbox SET status = 'done', processed_at = ? WHERE id = ?", (_now(), event_id)
        )


def outbox_fail(event_id: int, error: str, retry_in_s: int = 30, max_attempts: int = 5) -> None:
    """失败重试：未超上限则延后重试，超限标记 failed。"""
    with get_conn() as conn:
        row = conn.execute("SELECT attempts FROM outbox WHERE id = ?", (event_id,)).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        if attempts >= max_attempts:
            conn.execute(
                "UPDATE outbox SET status = 'failed', attempts = ?, last_error = ? WHERE id = ?",
                (attempts, error[:500], event_id),
            )
        else:
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz

            nxt = (_dt.now(_tz.utc) + _td(seconds=retry_in_s)).isoformat()
            conn.execute(
                "UPDATE outbox SET status = 'pending', attempts = ?, last_error = ?, next_at = ? WHERE id = ?",
                (attempts, error[:500], nxt, event_id),
            )


def outbox_stats() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) c FROM outbox GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}


# ============================================================
# 数据保留期清理
# ============================================================

def purge_old_data(days: int = 180) -> dict:
    """清理超过保留期的运营数据。

    **审计链 audit_chain 永不删除**（哈希链完整性优先）；
    这里只清理可再生的运营表：action_log、approvals、messages 之外的统计类记录。
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    if days < 1:
        raise ValueError("保留天数必须 >= 1")
    cutoff = (_dt.now(_tz.utc) - _td(days=days)).isoformat()
    removed = {}
    with get_conn() as conn:
        # 已终结的动作日志（保留 created 更早的）
        cur = conn.execute(
            "DELETE FROM action_log WHERE created_at < ? "
            "AND status IN ('executed','failed','denied','expired')", (cutoff,)
        )
        removed["action_log"] = cur.rowcount or 0
        cur = conn.execute(
            "DELETE FROM approvals WHERE created_at < ? "
            "AND status IN ('approved','denied','expired')", (cutoff,)
        )
        removed["approvals"] = cur.rowcount or 0
    return removed


def data_stats() -> dict:
    """各表当前行数，供控制台展示与容量评估。"""
    tables = ["sessions", "messages", "tickets", "memory", "feedback",
              "injection_log", "action_log", "approvals", "audit_chain",
              "agent_runs", "agent_steps", "outbox", "rate_hits"]
    out = {}
    with get_conn() as conn:
        for t in tables:
            try:
                out[t] = conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
            except sqlite3.Error:
                out[t] = -1
    return out


def approval_consume(approval_id: str) -> bool:
    """原子消费审批：pending/approved → consumed。

    只有第一次调用返回 True。这是防「一个审批反复执行」的关键——
    仅靠状态检查会有竞态，必须用条件 UPDATE 的 rowcount 判定赢家。
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE approvals SET status = 'consumed' WHERE id = ? AND status = 'approved'",
            (approval_id,),
        )
    return cur.rowcount > 0


# ---- 标注回复（Dify annotations 对标）----


def _norm_q(q: str) -> str:
    """问题归一化：去空白/标点、小写——「退货政策是什么?」与「退货政策是什么」同一问。"""
    return "".join(ch for ch in q.strip().lower() if ch.isalnum())


def annotations_list(include_disabled: bool = True) -> list[dict]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM annotations"
        if not include_disabled:
            sql += " WHERE enabled = 1"
        rows = conn.execute(sql + " ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def annotation_create(question: str, answer: str) -> dict:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "INSERT INTO annotations (question, answer, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (question.strip(), answer.strip(), now, now),
        )
        row = conn.execute("SELECT * FROM annotations WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def annotation_update(aid: int, question: str | None = None, answer: str | None = None,
                      enabled: bool | None = None) -> dict | None:
    from datetime import datetime, timezone

    fields, vals = [], []
    if question is not None:
        fields.append("question = ?"); vals.append(question.strip())
    if answer is not None:
        fields.append("answer = ?"); vals.append(answer.strip())
    if enabled is not None:
        fields.append("enabled = ?"); vals.append(1 if enabled else 0)
    if not fields:
        return annotation_get(aid)
    fields.append("updated_at = ?"); vals.append(datetime.now(timezone.utc).isoformat())
    vals.append(aid)
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(f"UPDATE annotations SET {', '.join(fields)} WHERE id = ?", vals)
        row = conn.execute("SELECT * FROM annotations WHERE id = ?", (aid,)).fetchone()
    return dict(row) if row else None


def annotation_get(aid: int) -> dict | None:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM annotations WHERE id = ?", (aid,)).fetchone()
    return dict(row) if row else None


def annotation_delete(aid: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM annotations WHERE id = ?", (aid,))
    return cur.rowcount > 0


def annotation_hit(question: str) -> dict | None:
    """归一化精确匹配 + 双向包含（长短问法互相覆盖），命中即回复并计数。

    包含匹配双向：标注「退货政策」能接住「我想问下退货政策」，标注长句也能被短问命中。
    """
    nq = _norm_q(question)
    if not nq:
        return None
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM annotations WHERE enabled = 1").fetchall()
    for r in rows:
        nr = _norm_q(r["question"])
        if not nr:
            continue
        if nq == nr or (len(nr) >= 4 and nr in nq) or (len(nq) >= 4 and nq in nr):
            with get_conn() as conn:
                conn.execute("UPDATE annotations SET hits = hits + 1 WHERE id = ?", (r["id"],))
            return dict(r)
    return None


def delete_channel_session(channel: str) -> bool:
    """删除频道会话映射（AstrBot /reset 对标）：下一条消息将开启全新会话。

    只删映射与关闭旧会话，长期记忆保留——重置的是「对话上下文」，不是「对客人的了解」。
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET status = 'closed', ended_at = ? "
            "WHERE id IN (SELECT session_id FROM channel_sessions WHERE channel = ?) AND status = 'ongoing'",
            (_now(), channel),
        )
        cur = conn.execute("DELETE FROM channel_sessions WHERE channel = ?", (channel,))
    return cur.rowcount > 0


# ---- 坐席内部备注（Chatwoot 对标）：只给团队看，访客不可见 ----


def notes_add(session_id: str, operator: str, content: str) -> dict:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "INSERT INTO session_notes (session_id, operator, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, operator, content.strip(), now),
        )
        row = conn.execute("SELECT * FROM session_notes WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row) if row else {}


def notes_list(session_id: str) -> list[dict]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM session_notes WHERE session_id = ? ORDER BY id DESC LIMIT 100",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---- 回复自检（反思修正机制）----


def critique_add(session_id: str, length: int, issues: list[str]) -> None:
    from datetime import datetime, timezone

    if not issues:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO reply_critiques (session_id, length, issues, created_at) VALUES (?, ?, ?, ?)",
            (session_id, length, json.dumps(issues, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()),
        )


def critique_summary(window: int = 60, threshold: float = 0.25) -> list[str]:
    """最近 window 条自检里出现率超 threshold 的毛病，按频率降序。"""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT issues FROM reply_critiques ORDER BY id DESC LIMIT ?", (window,)
        ).fetchall()
    if not rows:
        return []
    counter: dict[str, int] = {}
    total = 0
    for r in rows:
        try:
            issues = json.loads(r["issues"])
        except Exception:
            continue
        total += 1
        for it in issues or []:
            counter[it] = counter.get(it, 0) + 1
    if not total:
        return []
    ranked = sorted(counter.items(), key=lambda kv: -kv[1])
    return [f"{issue}（最近{total}条里出现{hits}次）"
            for issue, hits in ranked if hits / total >= threshold]


# ---- 角色卡（SillyTavern chara_card 对标）----


def _card_row(r) -> dict:
    d = dict(r)
    d["tags"] = [t for t in (d.get("tags") or "").split(",") if t]
    return d


def cards_list() -> list[dict]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM character_cards ORDER BY id DESC").fetchall()
    return [_card_row(r) for r in rows]


def card_get(cid: int) -> dict | None:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM character_cards WHERE id = ?", (cid,)).fetchone()
    return _card_row(r) if r else None


def card_get_by_name(name: str) -> dict | None:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM character_cards WHERE name = ?", (name,)).fetchone()
    return _card_row(r) if r else None


def card_save(name: str, persona: str, greeting: str | None = None,
              temperature: float | None = None, tags: list[str] | None = None,
              cid: int | None = None) -> dict:
    """创建或更新角色卡（cid 为空按 name upsert）。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    tags_str = ",".join(t.strip() for t in (tags or []) if t.strip())
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        if cid is not None:
            conn.execute(
                "UPDATE character_cards SET name=?, persona=?, greeting=?, temperature=?, tags=?, updated_at=? WHERE id=?",
                (name, persona, greeting, temperature, tags_str, now, cid))
            rid = cid
        else:
            existing = conn.execute("SELECT id FROM character_cards WHERE name=?", (name,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE character_cards SET persona=?, greeting=?, temperature=?, tags=?, updated_at=? WHERE id=?",
                    (persona, greeting, temperature, tags_str, now, existing["id"]))
                rid = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO character_cards (name, persona, greeting, temperature, tags, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (name, persona, greeting, temperature, tags_str, now, now))
                rid = cur.lastrowid
        row = conn.execute("SELECT * FROM character_cards WHERE id=?", (rid,)).fetchone()
    return _card_row(row)


def card_delete(cid: int) -> bool:
    with get_conn() as conn:
        conn.execute("DELETE FROM card_bindings WHERE card_id = ?", (cid,))
        cur = conn.execute("DELETE FROM character_cards WHERE id = ?", (cid,))
    return cur.rowcount > 0


def card_bind(chat_key: str, cid: int | None) -> bool:
    """绑定/解绑（cid=None 解绑）。"""
    from datetime import datetime, timezone

    with get_conn() as conn:
        if cid is None:
            cur = conn.execute("DELETE FROM card_bindings WHERE chat_key = ?", (chat_key,))
            return cur.rowcount > 0
        conn.execute(
            "INSERT INTO card_bindings (chat_key, card_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_key) DO UPDATE SET card_id=excluded.card_id, updated_at=excluded.updated_at",
            (chat_key, cid, datetime.now(timezone.utc).isoformat()))
    return True


def card_bindings_list() -> list[dict]:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT b.chat_key, b.card_id, c.name FROM card_bindings b "
            "LEFT JOIN character_cards c ON c.id = b.card_id ORDER BY b.chat_key").fetchall()
    return [dict(r) for r in rows]


def card_for_chat(chat_key: str) -> dict | None:
    """取某会话（群/人）绑定的角色卡；未绑定返回 None。"""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT c.* FROM card_bindings b JOIN character_cards c ON c.id = b.card_id "
            "WHERE b.chat_key = ?", (chat_key,)).fetchone()
    return _card_row(r) if r else None


# ============================================================
# 提醒 / 待办（秘书核心能力）
# ============================================================

def reminder_add(visitor_id: str, chat_key: str, text: str, due_at: str,
                 channel_type: str = "webchat", group_id: str | None = None,
                 channel_user_id: str | None = None, repeat_rule: str = "") -> dict:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (visitor_id, chat_key, channel_type, group_id, "
            "channel_user_id, text, due_at, status, created_at, repeat_rule) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (visitor_id, chat_key, channel_type, group_id, channel_user_id, text, due_at,
             _now(), repeat_rule),
        )
        rid = cur.lastrowid
    return reminder_get(int(rid)) or {}


def reminder_get(rid: int) -> dict | None:
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
    return dict(r) if r else None


def reminder_list(visitor_id: str = "", status: str = "pending", limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM reminders WHERE 1=1"
    args: list = []
    if visitor_id:
        sql += " AND visitor_id = ?"
        args.append(visitor_id)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY due_at LIMIT ?"
    args.append(limit)
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def reminder_due(now: str, limit: int = 20) -> list[dict]:
    """取出到期且仍待发的提醒。"""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? "
            "ORDER BY due_at LIMIT ?", (now, limit)).fetchall()
    return [dict(r) for r in rows]


def reminder_reschedule(rid: int, next_due: str) -> None:
    """周期提醒触发后：重置为 pending 并把 due_at 推到下一次。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'pending', due_at = ?, sent_at = NULL, "
            "attempts = 0, last_error = '' WHERE id = ?", (next_due, rid))


def reminder_mark(rid: int, status: str, error: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE reminders SET status = ?, sent_at = ?, attempts = attempts + 1, "
            "last_error = ? WHERE id = ?",
            (status, _now() if status == "sent" else None, error[:200], rid),
        )


def reminder_cancel(rid: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (rid,))
    return cur.rowcount > 0


def search_messages(q: str, limit: int = 50, visitor_id: str = "") -> list[dict]:
    """跨会话搜消息内容（控制台「对话搜索」用）。

    用 LIKE 而不是 FTS5：消息表没有维护 FTS 索引，而这里是人工发起的低频查询，
    全表扫几十万行也在几十毫秒量级；LIKE 还能支持中文子串匹配，FTS 默认分词做不到。
    """
    term = (q or "").strip()
    if not term:
        return []
    sql = ("SELECT m.id, m.session_id, m.role, m.content, m.created_at, s.visitor_id "
           "FROM messages m JOIN sessions s ON s.id = m.session_id "
           "WHERE m.deleted = 0 AND m.content LIKE ?")
    args: list = [f"%{term}%"]
    if visitor_id:
        sql += " AND s.visitor_id = ?"
        args.append(visitor_id)
    sql += " ORDER BY m.id DESC LIMIT ?"
    args.append(max(1, min(limit, 200)))
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def export_session(session_id: str) -> dict | None:
    """导出一个会话的全部消息（Markdown/JSON 由调用方格式化）。"""
    sess = get_session(session_id)
    if not sess:
        return None
    return {"session": sess, "messages": get_messages(session_id, limit=None, include_deleted=False)}
