import pathlib

import pytest
from fastapi.testclient import TestClient


ADMIN = "test-admin-token"
CHANNEL = "test-channel-token"

# 仓库内置的只读知识库种子：测试一律复制到临时目录，绝不写入原文件
_REPO_KB = pathlib.Path(__file__).resolve().parent.parent / "core" / "data" / "kb.json"


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """所有用例默认跑在临时库上：防止漏写 fixture 的用例（如 test_pipeline）往仓库根写 cs_agent.db。"""
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "autouse.db"))
    from core import store

    store._SCHEMA_READY.clear()


@pytest.fixture(autouse=True)
def _isolated_kb(monkeypatch, tmp_path):
    """知识库隔离：任何测试（含未来新增的导入/编辑用例）都不会改动仓库里的 kb.json。"""
    from core import retrieval

    kb_copy = tmp_path / "kb.json"
    kb_copy.write_bytes(_REPO_KB.read_bytes())
    monkeypatch.setattr(retrieval, "KB_PATH", kb_copy)
    retrieval._KB_CACHE = None
    yield
    retrieval._KB_CACHE = None


@pytest.fixture(autouse=True)
def _isolated_retrieval_index(monkeypatch, tmp_path):
    """检索索引库隔离到临时目录：否则跑测试会往 core/data/retrieval.db 写，污染工作区。"""
    from core import retrieval

    monkeypatch.setattr(retrieval, "RETRIEVAL_DB", tmp_path / "retrieval.db")
    retrieval._INDEX_STATE.update({"sig": "", "fts5": False, "vec": False})
    yield
    retrieval._INDEX_STATE.update({"sig": "", "fts5": False, "vec": False})


@pytest.fixture()
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("CS_AGENT_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("CHANNEL_TOKEN", CHANNEL)
    monkeypatch.delenv("USER_LLM_API_KEY", raising=False)
    from core.main import create_app
    from core import store

    store.init_db()
    return create_app()


@pytest.fixture()
def client(app):
    """默认带 admin/channel 双 token 的测试 client，覆盖大多数管理/渠道端点。"""
    with TestClient(app) as c:
        c.headers.update({"X-Admin-Token": ADMIN, "X-Channel-Token": CHANNEL})
        yield c


def create_web_session(client, visitor_id="anonymous"):
    """创建 web 访客会话并返回 (sid, secret)；后续访客请求必须带 X-Session-Secret。"""
    r = client.post("/api/sessions", json={"visitor_id": visitor_id})
    assert r.status_code == 200, r.text
    data = r.json()
    sid = data["session_id"]
    secret = data["session_secret"]
    # 后续该 client 的访客请求自动带 secret（同一 client 多会话时需注意隔离，测试一般单会话）
    client.headers.update({"X-Session-Secret": secret})
    return sid, secret


def reset_web_session_auth(client):
    client.headers.pop("X-Session-Secret", None)


# ---------------------------------------------------------------
# 执行器测试共用 fixture（policy / workspace）
# 放在 conftest 里，所有测试文件都能用，避免在各文件重复定义。
# ---------------------------------------------------------------

@pytest.fixture()
def workspace(tmp_path):
    """受控工作目录，同时充当 allow_root。"""
    root = tmp_path / "work"
    root.mkdir()
    (root / "hello.txt").write_text("你好世界", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    return root


@pytest.fixture()
def policy(workspace):
    """以 workspace 为 allow_root 的真实 Policy 实例（非 mock）。"""
    import json as _json
    from executor.policy import Policy, load_policy

    base = _json.loads(_json.dumps(load_policy().data))
    base["fs"]["allow_roots"] = [str(workspace)]
    return Policy(base)
