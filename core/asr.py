"""语音转写（ASR）：把用户发来的 QQ 语音转成文字，进管线当普通消息。

为什么放 core 而不是 gateway：转写失败要给用户一句人话（而不是静默），
而「说什么」属于对话决策；网关只负责把音频字节搞到手。

三档降级（全都不抛异常）：
1. faster-whisper 本地模型（默认，离线可用，small 中文模型约 460MB）
2. 环境变量 ASR_CMD 指定的外部命令（模板里用 {wav} 占位，stdout 即文本）
3. 都没有 → `available()` 返回 False，网关回一句「语音听不了，打字吧」

模型路径来自 ASR_MODEL，默认本机缓存目录；首次使用需自行下载（见 DEPLOY）。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_MODEL = "C:/Users/YOU/models/whisper-small-zh/models--Systran--faster-whisper-small/snapshots/536b0662742c02347bc0e980a01041f333bce120"

_MODEL = None
_MODEL_LOCK = asyncio.Lock()
_LAST_ERROR = ""


def available() -> bool:
    """能不能转写：有本地模型目录或配了外部命令。"""
    if os.environ.get("ASR_CMD", "").strip():
        return True
    model = os.environ.get("ASR_MODEL", DEFAULT_MODEL)
    return Path(model).exists()


def status() -> dict:
    return {"available": available(),
            "model": os.environ.get("ASR_MODEL", DEFAULT_MODEL),
            "external_cmd": bool(os.environ.get("ASR_CMD", "").strip()),
            "last_error": _LAST_ERROR}


def _ffmpeg() -> str:
    return os.environ.get("FFMPEG_BIN", "ffmpeg")


def _to_pcm(audio: bytes, suffix: str = ".amr") -> bytes:
    """任意音频字节 → 16k 单声道 s16le PCM（whisper 要的格式）。

    用 ffmpeg 走管道，不落临时文件：QQ 语音是 AMR/AMR-WB，whisper 自己解不了。
    没有 ffmpeg 时抛 FileNotFoundError，由调用方转成可读失败。
    """
    proc = subprocess.run(
        [_ffmpeg(), "-v", "quiet", "-i", "pipe:0", "-f", "s16le",
         "-ac", "1", "-ar", "16000", "pipe:1"],
        input=audio, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg 转码失败（{proc.returncode}）")
    return proc.stdout


async def _load_model():
    global _MODEL, _LAST_ERROR
    async with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL
        from faster_whisper import WhisperModel

        path = os.environ.get("ASR_MODEL", DEFAULT_MODEL)
        _MODEL = await asyncio.to_thread(
            WhisperModel, path, device="cpu", compute_type="int8",
            cpu_threads=int(os.environ.get("ASR_THREADS", "4")))
        return _MODEL


async def transcribe(audio: bytes) -> tuple[str, str]:
    """音频字节 → (文本, 错误说明)。成功时错误为空串。绝不抛异常。"""
    global _LAST_ERROR
    if not audio:
        return "", "空音频"
    cmd = os.environ.get("ASR_CMD", "").strip()
    try:
        pcm = await asyncio.to_thread(_to_pcm, audio)
    except FileNotFoundError:
        _LAST_ERROR = "ffmpeg 不可用"
        return "", _LAST_ERROR
    except Exception as e:
        _LAST_ERROR = f"音频转码失败：{type(e).__name__}"
        return "", _LAST_ERROR

    if cmd:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(pcm)
                wav = f.name
            try:
                argv = cmd.replace("{wav}", wav).split()
                proc = await asyncio.to_thread(
                    subprocess.run, argv, capture_output=True, timeout=120)
                text = (proc.stdout or b"").decode("utf-8", "ignore").strip()
            finally:
                Path(wav).unlink(missing_ok=True)
            if text:
                return text, ""
            _LAST_ERROR = "外部 ASR 命令没有输出"
            return "", _LAST_ERROR
        except Exception as e:
            _LAST_ERROR = f"外部 ASR 失败：{type(e).__name__}"
            return "", _LAST_ERROR

    try:
        model = await _load_model()
        import numpy as np

        audio_arr = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
        # 整段 8 秒中文在 CPU 上约 3s（small/int8/4 线程）；长语音按 15 秒切块更慢，
        # 这里给 180s 上限，超时如实说听不清，不让整轮对话卡死。
        segments, _info = await asyncio.wait_for(
            asyncio.to_thread(lambda: model.transcribe(
                audio_arr, language="zh", beam_size=1, vad_filter=True)),
            timeout=180)
        text = "".join(s.text for s in segments).strip()
        if not text:
            _LAST_ERROR = "没有识别出内容"
            return "", _LAST_ERROR
        return text, ""
    except FileNotFoundError:
        _LAST_ERROR = "语音模型未下载"
        return "", _LAST_ERROR
    except asyncio.TimeoutError:
        _LAST_ERROR = "转写超时"
        return "", _LAST_ERROR
    except Exception as e:
        _LAST_ERROR = f"转写失败：{type(e).__name__}"
        return "", _LAST_ERROR


def ffmpeg_ok() -> bool:
    return shutil.which(_ffmpeg()) is not None
