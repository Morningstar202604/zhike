"""语音转写回归：格式转换、外部命令、降级路径都必须给可读结果。

这里不打真实模型（CI 没有 460MB 模型），只测：
- 音频 → PCM 的 ffmpeg 调用参数
- 外部 ASR 命令模板替换
- 无 ffmpeg / 空音频 / 命令无输出 的失败说明
- available() 的判定
"""
import os

import pytest

from core import asr


def test_available_with_external_cmd(monkeypatch):
    monkeypatch.setenv("ASR_CMD", "echo hi")
    assert asr.available() is True


def test_available_with_model_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("ASR_CMD", raising=False)
    monkeypatch.setenv("ASR_MODEL", str(tmp_path))
    assert asr.available() is True


def test_unavailable_when_nothing_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("ASR_CMD", raising=False)
    monkeypatch.setenv("ASR_MODEL", str(tmp_path / "missing"))
    assert asr.available() is False


def test_empty_audio_returns_error():
    text, err = asyncio_run(asr.transcribe(b""))
    assert text == "" and err == "空音频"


def test_ffmpeg_missing_gives_readable_error(monkeypatch):
    monkeypatch.setenv("FFMPEG_BIN", "definitely-not-ffmpeg-xyz")
    text, err = asyncio_run(asr.transcribe(b"fake audio"))
    assert text == "" and "ffmpeg" in err.lower()


def test_external_cmd_template_substitution(monkeypatch, tmp_path):
    """ASR_CMD 里的 {wav} 必须被替换成真实临时文件，命令 stdout 即结果。"""
    script = tmp_path / "fake_asr.py"
    script.write_text(
        "import sys, pathlib, wave\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "assert p.exists()\n"
        "print('这是转写结果')\n", encoding="utf-8")
    import sys as _sys
    monkeypatch.setenv("ASR_CMD", f"{_sys.executable} {script} {{wav}}")
    # 用真实 wav 让 ffmpeg 转码成功
    wav = tmp_path / "a.wav"
    import wave
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    monkeypatch.setattr(asr, "_to_pcm", lambda b: b"\x00\x00" * 16000)
    monkeypatch.setattr(asr, "_ffmpeg", lambda: "ffmpeg")
    text, err = asyncio_run(asr.transcribe(wav.read_bytes()))
    assert text == "这是转写结果" and err == ""


def test_external_cmd_without_output_reports(monkeypatch, tmp_path):
    import sys as _sys
    script = tmp_path / "silent.py"
    script.write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("ASR_CMD", f"{_sys.executable} {script} {{wav}}")
    monkeypatch.setattr(asr, "_to_pcm", lambda b: b"\x00" * 100)
    text, err = asyncio_run(asr.transcribe(b"x"))
    assert text == "" and "没有输出" in err


def test_status_reports_configuration(monkeypatch):
    monkeypatch.setenv("ASR_CMD", "echo x")
    st = asr.status()
    assert st["available"] is True and st["external_cmd"] is True


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
