"""
语音唤醒路由：开关控制 + 状态查询 + AI说话通知 + 远程ASR
"""

from fastapi import APIRouter, UploadFile, File
from pydantic import BaseModel
from typing import Optional
import time
import httpx

from voice import voice
from camera import CAMERA_DISABLED_REASON
from config import get_key
from provider_status import (
    classify_exception, classify_http_status, new_request_id, record_provider_event,
)

router = APIRouter()


class VoiceToggle(BaseModel):
    enabled: bool
    wake_word: str = "老公"


class AISpeakingNotify(BaseModel):
    speaking: bool


@router.get("/api/voice/status")
async def voice_status():
    return {
        "enabled": voice.enabled,
        "in_call": voice.in_call,
        "ai_speaking": voice.ai_speaking,
        "wake_word": voice.wake_word,
    }


@router.post("/api/voice/toggle")
async def voice_toggle(body: VoiceToggle):
    if body.enabled:
        voice.start(body.wake_word)
    else:
        voice.stop()
    return {"ok": True, "enabled": voice.enabled}


@router.post("/api/voice/ai-speaking")
async def voice_ai_speaking(body: AISpeakingNotify):
    """前端通知：AI TTS 播放状态"""
    voice.notify_ai_speaking(body.speaking)
    return {"ok": True}


@router.post("/api/voice/cam-check-start")
async def voice_cam_check_start():
    """Legacy CAM_CHECK voice hold endpoint."""
    return {
        "ok": False,
        "error": CAMERA_DISABLED_REASON,
        "message": "[CAM_CHECK] 已禁用，不再保持摄像头查看语音态。",
    }


ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"


def _record_remote_asr(request_id: str, start: float, *, ok: bool,
                       http_status: int = None, error_type: str = None,
                       retryable: bool = False, message: str = "",
                       meta: dict = None):
    if http_status is not None and not error_type:
        error_type, retryable = classify_http_status(http_status)
    record_provider_event({
        "request_id": request_id,
        "scope": "asr:legacy_remote",
        "provider_type": "openai",
        "provider_label": "SiliconFlow ASR legacy",
        "endpoint_name": "SiliconFlow ASR legacy",
        "base_url": ASR_URL,
        "model": ASR_MODEL,
        "ok": ok,
        "http_status": http_status,
        "error_type": error_type or ("ok" if ok else "unknown"),
        "retryable": retryable,
        "elapsed_ms": (time.perf_counter() - start) * 1000,
        "message": message,
        "meta": meta or {},
    })


@router.post("/api/voice/remote-asr")
async def remote_asr(file: UploadFile = File(...)):
    """远程 ASR：接收手机端录音，调硅基流动 ASR 返回文本"""
    key = get_key("siliconflow")
    if not key:
        return {"text": "", "error": "No siliconflow key"}
    content = await file.read()
    print(f"[RemoteASR] Received {len(content)} bytes, filename={file.filename}")
    request_id = new_request_id("asr")
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                ASR_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", content, "audio/wav")},
                data={"model": ASR_MODEL, "language": "zh"},
                timeout=15,
            )
        if resp.status_code != 200:
            error_type, retryable = classify_http_status(resp.status_code)
            _record_remote_asr(
                request_id, start, ok=False,
                http_status=resp.status_code, error_type=error_type,
                retryable=retryable, message=f"HTTP {resp.status_code}",
                meta={"audio_bytes": len(content)},
            )
            return {"text": "", "error": f"HTTP {resp.status_code}"}
        result = resp.json()
        text = result.get("text", "").strip()
        _record_remote_asr(
            request_id, start, ok=True,
            http_status=resp.status_code,
            meta={"audio_bytes": len(content), "text_chars": len(text)},
        )
        print(f"[RemoteASR] Result: '{text}' (raw: {result})")
        return {"text": text}
    except Exception as e:
        if isinstance(e, (KeyError, IndexError, ValueError, TypeError)):
            error_type, retryable = "parse_error", False
        else:
            error_type, retryable = classify_exception(e)
        _record_remote_asr(
            request_id, start, ok=False,
            error_type=error_type, retryable=retryable,
            message=e.__class__.__name__,
            meta={"audio_bytes": len(content)},
        )
        print(f"[RemoteASR] Error: {e}")
        return {"text": "", "error": str(e)}
