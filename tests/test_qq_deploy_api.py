"""QQ 协议端部署编排测试：并发互斥 / 失败如实上报 / 启动走后台进程。

部署流程本身要联网拉 release，这里把下载函数替换为本地假实现，
只验证编排逻辑（状态机、互斥、解压保留配置、启动参数）。
"""

import pytest


@pytest.fixture()
def deploy(tmp_path, monkeypatch):
    """隔离的部署模块实例：临时目录 + 假下载。"""
    import core.qq_deploy as d

    monkeypatch.setattr(d, "DEPLOY_DIR", tmp_path / "napcat")
    monkeypatch.setattr(d, "VERSION_FILE", tmp_path / "napcat" / "version.txt")
    monkeypatch.setattr(d, "RUN_LOG", tmp_path / "napcat-deploy.log")
    d.state.update(stage="idle", pct=0, note="", version="")
    yield d


def test_deploy_status_initial(deploy):
    s = deploy.deploy_status()
    assert s["installed"] is False
    assert s["stage"] == "idle"
    assert s["running_3001"] in (True, False)


def test_deploy_success_extracts_and_skips_config(deploy, monkeypatch):
    """部署成功 = 解压全部文件 + 跳过 config/ + 写版本标记。"""
    import io
    import zipfile

    calls = []

    def fake_latest():
        calls.append(1)
        return "vX.Y.Z", "https://example.com/shell.zip", 10

    def fake_download(url, target, out, jobs=6, progress=None, cwd="."):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("NapCatWinBootMain.exe", b"exe")
            z.writestr("config/webui.json", b"{}")  # 用户配置：绝不能覆盖已有内容
        (deploy.DEPLOY_DIR / "_shell.zip").write_bytes(buf.getvalue())

    monkeypatch.setattr(deploy, "latest_release", fake_latest)
    monkeypatch.setattr("core.napcat_fetch.strict_download", fake_download)

    deploy._deploy_worker()  # 直接跑 worker（同步），不测线程
    s = deploy.deploy_status()
    assert s["installed"] is True
    assert s["version"] == "vX.Y.Z"
    assert s["stage"] == "done"
    assert (deploy.DEPLOY_DIR / "NapCatWinBootMain.exe").exists()
    assert not (deploy.DEPLOY_DIR / "config" / "webui.json").exists(), "config/ 必须跳过"


def test_deploy_failure_reports_honestly(deploy, monkeypatch):
    """下载失败要如实写进状态（前端显示原文），不能假装成功。"""
    def fake_latest():
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(deploy, "latest_release", fake_latest)
    deploy._deploy_worker()
    s = deploy.deploy_status()
    assert s["stage"] == "error"
    assert "网络炸了" in s["note"]


def test_deploy_reentrant_locked(deploy, monkeypatch):
    """同一时间只允许一个部署任务。"""
    import io
    import zipfile

    monkeypatch.setattr(deploy, "latest_release", lambda: ("v1", "u", 1))

    def slow_download(*a, **kw):
        import time
        time.sleep(0.3)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("NapCatWinBootMain.exe", b"x")
        (deploy.DEPLOY_DIR / "_shell.zip").write_bytes(buf.getvalue())

    monkeypatch.setattr("core.napcat_fetch.strict_download", slow_download)
    assert deploy.deploy() != "部署已在进行中（看进度条）。"  # 第一次放行
    assert deploy.deploy() == "部署已在进行中（看进度条）。"  # 第二次拒绝


def test_protocol_start_requires_deployed_binary(deploy, monkeypatch):
    monkeypatch.setenv("AUTHORIZED_QQ", "3876137118")
    msg = deploy.protocol_start("3876137118")
    assert msg.startswith("X"), "未部署时必须拒绝启动"


def test_protocol_start_spawns_background_process(deploy, monkeypatch):
    deploy.DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    (deploy.DEPLOY_DIR / "NapCatWinBootMain.exe").write_bytes(b"x")
    captured = {}

    class FakePopen:
        def __init__(self, args, **kw):
            captured["args"] = args
            captured["kw"] = kw

    monkeypatch.setattr(deploy.subprocess, "Popen", FakePopen)
    msg = deploy.protocol_start("3876137118")
    assert msg.startswith("OK")
    assert captured["args"][2:4] == ["start", "--qq"]
    assert captured["args"][-1] == "3876137118"
