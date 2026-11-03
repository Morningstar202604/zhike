"""四个此前完全没有测试的工具：退款、列目录、好友列表、群列表。

为什么单独补：
- `initiate_refund` 是**写操作**（真的提交退款）。它出错不是"答错话"，是
  真金白银的后果，边界必须锁死：未接入时要老实说没接入，不能假装成功。
- `list_files` 是**特权工具**（能看主机文件系统），必须先过权限闸门。
- `qq_friends` / `qq_groups` 是模型判断"该发到哪"的依据，返回结构不稳会让
  模型发错目标。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


from core import tools  # noqa: E402
from core.tool_context import ToolContext  # noqa: E402


# ---------------- initiate_refund ----------------

def test_initiate_refund_reports_not_connected_when_unconfigured(monkeypatch):
    """没配退款端点时必须明确说"没接入"，绝不能返回成功。

    这是最容易骗过用户的一类 bug：模型看到 found=False 会如实说没接入；
    但如果这里错误地返回 found=True，用户会以为钱真退了。
    """
    monkeypatch.setattr(tools, "_refund_endpoint", lambda: ("", ""))
    r = tools.tool_initiate_refund("ORD1", "不想要了")
    assert r["connected"] is False
    assert r["found"] is False
    assert "未接入" in r["note"] or "暂未接入" in r["note"]


def test_initiate_refund_success_shape(monkeypatch):
    """成功路径要带回 refund_id 与状态，供模型转述给用户。"""
    monkeypatch.setattr(tools, "_refund_endpoint", lambda: ("http://fake/refund", "tok"))

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"refund_id": "RF123", "status": "submitted"}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            assert json["order_id"] == "ORD1"
            return _Resp()

    monkeypatch.setattr(tools.httpx, "Client", lambda **k: _Client())
    r = tools.tool_initiate_refund("ORD1", "质量问题")
    assert r["connected"] is True and r["found"] is True
    assert r["refund_id"] == "RF123"
    assert "提交" in r["note"]


def test_initiate_refund_network_error_is_not_fake_success(monkeypatch):
    """退款接口挂了，必须返回失败，不能吞异常当成功。"""
    monkeypatch.setattr(tools, "_refund_endpoint", lambda: ("http://fake/refund", "tok"))

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            raise tools.httpx.ConnectError("boom")

    monkeypatch.setattr(tools.httpx, "Client", lambda **k: _Client())
    r = tools.tool_initiate_refund("ORD1")
    assert r["found"] is False
    assert "不可用" in r["note"]


def test_initiate_refund_default_reason_when_blank(monkeypatch):
    """空理由要填一个默认值，不能让下游收到空字符串。"""
    monkeypatch.setattr(tools, "_refund_endpoint", lambda: ("http://fake/refund", ""))
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"refund_id": "X", "status": "ok"}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            seen.update(json)
            return _Resp()

    monkeypatch.setattr(tools.httpx, "Client", lambda **k: _Client())
    tools.tool_initiate_refund("ORD1", "")
    assert seen["reason"] == "未说明"


# ---------------- list_files（特权工具） ----------------

def test_list_files_requires_trusted_subject(monkeypatch):
    """list_files 能读主机目录，普通群成员绝不能直接调用。

    权限闸门在 _authorize_tool 里；这里确认工具层的判定确实把这些
    "能碰主机文件系统"的工具列进特权集合，避免以后误删。
    """
    assert "list_files" in tools.PRIVILEGED_TOOLS
    assert "write_file" in tools.PRIVILEGED_TOOLS
    assert "read_file" in tools.PRIVILEGED_TOOLS


def test_list_files_denied_for_untrusted_context():
    """非可信主体调用 list_files 应被拒绝并给出理由，而不是静默执行。

    判据是 ctx.principal（qq:123 这种主体串），不是布尔 trusted 字段——
    权限要能追溯"谁"发起的，而不只是"可不可信"。
    """
    denied = tools._authorize_tool(
        "list_files",
        ToolContext(channel_type="qq_group", channel_user_id="u1", group_id="g1",
                    principal="qq:99999999"),
    )
    assert denied is not None
    assert denied.get("found") is False


def test_list_files_allowed_for_internal_context():
    """内部直调（无主体）保持原行为，避免破坏工具本职。"""
    assert tools._authorize_tool("list_files", ToolContext()) is None


# ---------------- qq_friends / qq_groups ----------------

def test_qq_friends_without_gateway_is_not_connected(monkeypatch):
    """网关没连上时，好友列表必须如实报失败，不能返回空列表冒充"没有好友"。"""
    import asyncio

    async def boom(*a, **k):
        raise RuntimeError("no gateway")

    from core.actions import client as action_client

    monkeypatch.setattr(action_client, "call_action", boom)
    r = asyncio.run(tools.tool_qq_friends(ToolContext()))
    assert r["found"] is False
    assert "失败" in r["note"]


def test_qq_groups_without_gateway_is_not_connected(monkeypatch):
    import asyncio

    async def boom(*a, **k):
        raise RuntimeError("no gateway")

    from core.actions import client as action_client

    monkeypatch.setattr(action_client, "call_action", boom)
    r = asyncio.run(tools.tool_qq_groups(ToolContext()))
    assert r["found"] is False
    assert "失败" in r["note"]


def test_qq_query_wraps_gateway_payload(monkeypatch):
    """_qq_query 要把网关返回的 data 序列化进 note，模型才能读群号/群名。"""
    import asyncio

    from core.actions import client as action_client

    async def fake_call(action, params, timeout_s=15):
        assert action == "get_group_list"
        return {"data": [{"group_id": 123, "group_name": "测试群"}]}

    monkeypatch.setattr(action_client, "call_action", fake_call)
    r = asyncio.run(tools._qq_query("get_group_list", {}, "qq_groups"))
    assert r["found"] is True
    assert "123" in r["note"] and "测试群" in r["note"]


def test_qq_query_reports_failure_not_empty(monkeypatch):
    """网关报错时返回 found=False，不能伪装成"没有数据"。"""
    import asyncio

    from core.actions import client as action_client

    async def boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(action_client, "call_action", boom)
    r = asyncio.run(tools._qq_query("get_friend_list", {}, "qq_friends"))
    assert r["found"] is False
    assert "失败" in r["note"]
