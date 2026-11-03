"""QQ 接入自检：判据本身要可靠，否则会把人带错方向。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import qq_doctor  # noqa: E402


def test_ws_ready_reports_unreachable_port(monkeypatch):
    """端口没人监听时，必须明确说"不可达"，不能报成"未登录"。"""
    # 选一个几乎不可能被占用的高位端口
    ok, reason = qq_doctor._ws_ready(1)
    assert ok is False
    assert "不可达" in reason or "握手" in reason


def test_qq_processes_only_parses_numeric_rows(monkeypatch):
    """wmic 输出含表头，解析时必须忽略，否则会把 "ProcessId" 当 pid。"""
    class _R:
        stdout = "ProcessId  WorkingSetSize\n1220  20791296\n"

    monkeypatch.setattr(qq_doctor.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(qq_doctor.os, "name", "nt")
    assert qq_doctor._qq_processes() == [(1220, 19)]


def test_version_hint_reads_napcat_target(monkeypatch, tmp_path):
    """版本比对是判断"该不该换 Docker"的核心依据，必须能读到目标版本。"""
    napcat = tmp_path / "napcat"
    napcat.mkdir()
    (napcat / "qqnt.json").write_text('{"version": "9.9.22-40990"}', encoding="utf-8")
    monkeypatch.setattr(qq_doctor, "NAPCAT_DIR", napcat)
    want, _ = qq_doctor._version_hint()
    assert want == "9.9.22-40990"


def test_docker_state_reports_missing_cli(monkeypatch):
    monkeypatch.setattr(qq_doctor.shutil, "which", lambda _: None)
    has_cli, engine_ok, note = qq_doctor._docker_state()
    assert has_cli is False and engine_ok is False
    assert "未安装" in note


def test_doctor_main_exits_nonzero_without_protocol_end(monkeypatch, capsys):
    """协议端没起来时必须返回非零，否则脚本会骗过 CI 或运维。"""
    monkeypatch.setattr(qq_doctor, "_ws_ready", lambda port=qq_doctor.WS_PORT: (False, "端口不可达"))
    monkeypatch.setattr(qq_doctor, "_port_open", lambda p: False)
    monkeypatch.setattr(qq_doctor, "_qq_processes", lambda: [])
    monkeypatch.setattr(qq_doctor, "_version_hint", lambda: ("9.9.22-40990", "9.9.36-53644"))
    monkeypatch.setattr(qq_doctor, "_docker_state", lambda: (True, False, "引擎未运行"))
    monkeypatch.setattr(qq_doctor, "_wsl_state", lambda: "Enabled")
    rc = qq_doctor.main()
    assert rc == 1
    out = capsys.readouterr().out
    assert "不匹配" in out
    assert "Docker" in out


def test_doctor_main_exits_zero_when_protocol_ready(monkeypatch, capsys):
    monkeypatch.setattr(qq_doctor, "_ws_ready", lambda port=qq_doctor.WS_PORT: (True, ""))
    monkeypatch.setattr(qq_doctor, "_port_open", lambda p: True)
    monkeypatch.setattr(qq_doctor, "_qq_processes", lambda: [(1, 400)])
    monkeypatch.setattr(qq_doctor, "_version_hint", lambda: ("9.9.36-53644", "9.9.36-53644"))
    monkeypatch.setattr(qq_doctor, "_docker_state", lambda: (True, True, "引擎正常"))
    monkeypatch.setattr(qq_doctor, "_wsl_state", lambda: "Enabled")
    assert qq_doctor.main() == 0
    assert "已就绪" in capsys.readouterr().out
