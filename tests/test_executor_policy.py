"""阶段 2：执行器策略与能力回归 —— 路径逃逸、命令注入、体积限制、签名。

安全相关的用例宁可多写：每一条都对应一个真实的绕过手法。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from executor import capabilities as cap
from executor.policy import Policy, PolicyError, load_policy


# ---------------- 路径逃逸 ----------------

ESCAPE_CASES = [
    "../etc/passwd",
    "../../",
    "..\\..\\windows\\system32",
    "sub/../../outside.txt",
    "./../work/../../etc",
    "/etc/passwd",
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "%~dp0..\\..",
]


@pytest.mark.parametrize("bad", ESCAPE_CASES)
def test_path_escape_rejected(policy, bad):
    with pytest.raises(PolicyError):
        policy.check_path(bad)


def test_outside_absolute_rejected(policy, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(PolicyError):
        policy.check_path(str(outside))


def test_inside_path_allowed(policy, workspace):
    assert policy.check_path(str(workspace / "hello.txt")).endswith("hello.txt")


def test_deny_glob_blocks_sensitive_names(policy, workspace):
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(PolicyError):
        policy.check_path(str(workspace / ".env"))


def test_deny_glob_blocks_pem(policy, workspace):
    (workspace / "server.pem").write_text("KEY", encoding="utf-8")
    with pytest.raises(PolicyError):
        policy.check_path(str(workspace / "server.pem"))


def test_null_byte_rejected(policy, workspace):
    with pytest.raises(PolicyError):
        policy.check_path(str(workspace / "a\x00b.txt"))


def test_control_chars_rejected(policy, workspace):
    with pytest.raises(PolicyError):
        policy.check_path(str(workspace / "a\nb.txt"))


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 有设备名/ADS")
def test_windows_reserved_names_rejected(policy, workspace):
    for name in ("CON", "NUL", "COM1", "PRN"):
        with pytest.raises(PolicyError):
            policy.check_path(str(workspace / name))


def test_empty_roots_denies_everything(tmp_path):
    p = Policy()
    p.data["fs"]["allow_roots"] = []
    with pytest.raises(PolicyError):
        p.check_path(str(tmp_path))


def test_symlink_escape_rejected(policy, workspace, tmp_path):
    """符号链接指向 allow_roots 之外必须拒绝。

    Windows 上普通用户建符号链接需要特权，会抛 OSError。
    这时改用目录 junction（mklink /J，无需特权）验证同样逻辑；
    两者都不可用才跳过，并明确标注原因——不能让测试静默通过。
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("secret", encoding="utf-8")
    link = workspace / "link"

    made = False
    if os.name == "nt":
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                           capture_output=True, text=True)
        made = r.returncode == 0
    if not made:
        try:
            link.symlink_to(outside, target_is_directory=True)
            made = True
        except (OSError, NotImplementedError):
            made = False

    if not made:
        pytest.skip("当前环境无法创建符号链接或 junction（需特权），该路径未被验证")

    with pytest.raises(PolicyError):
        policy.check_path(str(link / "x.txt"))


# ---------------- 命令注入 ----------------

INJECTION_CASES = [
    (["python", "-c", "print(1); rm -rf /"], True),        # 参数里含危险串
    (["git", "status && rm -rf /"], True),
    (["echo", "$(whoami)"], False),                         # echo 在白名单且字面无危险串
    (["sh", "-c", "id"], True),                             # sh 不在白名单
    (["bash", "-c", "id"], True),
    (["powershell", "-Command", "Get-Process"], True),
    (["cmd", "/c", "dir"], True),
    (["rm", "-rf", "/"], True),
    (["python", "-c", "import os; os.system('id')"], False),  # python 白名单，无边模式
]


@pytest.mark.parametrize("argv,should_reject", INJECTION_CASES)
def test_command_whitelist(policy, argv, should_reject):
    if should_reject:
        with pytest.raises(PolicyError):
            policy.check_command(argv, None)
    else:
        # 不真正执行，只验证策略层放行
        checked, _ = policy.check_command(argv, None)
        assert checked == argv


def test_shell_binary_not_allowed(policy):
    for exe in ("sh", "bash", "zsh", "cmd", "powershell", "pwsh"):
        with pytest.raises(PolicyError):
            policy.check_command([exe, "-c", "id"], None)


def test_safe_env_strips_secrets(policy, monkeypatch):
    """两层防护都要验证：
    1) 不在白名单的变量不传
    2) 即使被显式加进白名单，敏感名也要被二次剔除（这是双保险，容易被写漏）
    """
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    monkeypatch.setenv("MY_TOKEN", "leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    env = policy.safe_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "MY_TOKEN" not in env
    assert "PATH" in env

    # 双保险：把敏感变量塞进白名单，仍然必须被剔除
    policy.data["shell"]["env_allowlist"] = ["PATH", "MY_TOKEN", "AWS_SECRET_ACCESS_KEY", "DB_PASSWORD"]
    monkeypatch.setenv("DB_PASSWORD", "leak")
    env2 = policy.safe_env()
    assert "MY_TOKEN" not in env2, "白名单内的敏感变量也必须剔除"
    assert "AWS_SECRET_ACCESS_KEY" not in env2
    assert "DB_PASSWORD" not in env2
    assert "PATH" in env2


# ---------------- 文件能力 ----------------

def test_fs_read_returns_content_and_hash(policy, workspace):
    r = cap.fs_read(policy, str(workspace / "hello.txt"))
    assert r["content"] == "你好世界"
    assert len(r["sha256"]) == 64
    assert r["binary_guess"] is False


def test_fs_read_size_limit(policy, workspace):
    big = workspace / "big.txt"
    big.write_text("x" * 2000, encoding="utf-8")
    policy.data["fs"]["max_read_bytes"] = 100
    with pytest.raises(cap.CapabilityError):
        cap.fs_read(policy, str(big))


def test_fs_write_atomic_and_backup(policy, workspace):
    target = workspace / "out.txt"
    target.write_text("old", encoding="utf-8")
    r = cap.fs_write(policy, str(target), "new content")
    assert target.read_text(encoding="utf-8") == "new content"
    assert r["backup"] and Path(r["backup"]).exists(), "覆盖前应留快照供撤销"
    assert not list(workspace.glob("*.zktmp")), "临时文件必须清理"


def test_fs_write_limit(policy, workspace):
    policy.data["fs"]["max_write_bytes"] = 10
    with pytest.raises(cap.CapabilityError):
        cap.fs_write(policy, str(workspace / "x.txt"), "x" * 100)


def test_fs_list_depth_and_entries(policy, workspace):
    r = cap.fs_list(policy, str(workspace), depth=2)
    names = {e["name"] for e in r["entries"]}
    assert "hello.txt" in names and "sub" in names
    assert "nested.txt" in names, "depth=2 应包含子目录内容"


def test_fs_list_is_bounded(policy, workspace):
    for i in range(30):
        (workspace / f"f{i}.txt").write_text("x", encoding="utf-8")
    policy.data["fs"]["max_list_entries"] = 10
    r = cap.fs_list(policy, str(workspace))
    assert r["truncated"] is True
    assert len(r["entries"]) <= 10


def test_fs_move_within_root(policy, workspace):
    src = workspace / "hello.txt"
    dst = workspace / "renamed.txt"
    cap.fs_move(policy, str(src), str(dst))
    assert dst.exists() and not src.exists()


def test_fs_move_outside_rejected(policy, workspace, tmp_path):
    with pytest.raises(PolicyError):
        cap.fs_move(policy, str(workspace / "hello.txt"), str(tmp_path / "escape.txt"))


def test_fs_delete_defaults_to_recoverable(policy, workspace):
    f = workspace / "temp.txt"
    f.write_text("x", encoding="utf-8")
    r = cap.fs_delete(policy, str(f))
    assert not f.exists()
    assert r.get("recoverable") is True


def test_fs_stat_shape(policy, workspace):
    r = cap.fs_stat(policy, str(workspace / "hello.txt"))
    assert r["exists"] and r["is_dir"] is False and r["size"] > 0


# ---------------- 进程能力 ----------------

def test_proc_list_works(policy):
    r = cap.proc_list(policy)
    assert r["count"] >= 1


def test_proc_kill_disabled_by_default(policy):
    with pytest.raises(PolicyError):
        cap.proc_kill(policy, 12345, "anything")


def test_proc_kill_protects_system(policy):
    policy.data["proc"]["allow_kill"] = True
    with pytest.raises(PolicyError):
        cap.proc_kill(policy, 4, "System")


# ---------------- 策略加载 ----------------

def test_load_policy_merges_defaults(tmp_path):
    f = tmp_path / "policy.json"
    f.write_text(json.dumps({"fs": {"allow_roots": ["/tmp/x"]}}), encoding="utf-8")
    p = load_policy(str(f))
    assert p.data["fs"]["allow_roots"] == ["/tmp/x"]
    assert p.data["fs"]["max_read_bytes"] == 1048576, "未覆盖的字段应保留默认值"
    assert p.data["shell"]["allow_binaries"], "shell 白名单必须保留"


def test_load_policy_missing_file_defaults():
    p = load_policy("/nonexistent/policy.json")
    assert p.data["fs"]["allow_roots"] == []


def test_load_policy_bad_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(str(f))
