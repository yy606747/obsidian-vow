from __future__ import annotations

import asyncio
import io
import wave

from app.provider_privacy import private_provider_call


def model_available(model_key):
    from config import resolve_core_model
    return resolve_core_model(model_key) is not None


def supports_images(model_key):
    from config import resolve_core_model
    cfg = resolve_core_model(model_key)
    if not cfg:
        return False
    if type(cfg.get("image_input")) is bool:
        return cfg["image_input"]
    from app.pc_screen.service import model_supports_vision
    return model_supports_vision(model_key)


class TogetherModels:
    async def stream(self, model_key, messages, meta):
        from ai_providers import stream_ai
        source = stream_ai(messages, model_key, meta, retry_prohibited=False)
        try:
            while True:
                with private_provider_call("together:turn"):
                    try:
                        chunk = await anext(source)
                    except StopAsyncIteration:
                        break
                if "\x00" in chunk or chunk.startswith("[错误]"):
                    raise ValueError("together_model_failed")
                yield chunk
        finally:
            with private_provider_call("together:turn"):
                await source.aclose()
        if meta.get("error") or meta.get("_prohibited") or str(meta.get("finish_reason", "")).lower() in {"length", "max_tokens"}:
            raise ValueError("together_model_incomplete")

    async def transcribe(self, data, *, max_seconds=60):
        from config import get_slot
        from ai_providers import call_slot_asr
        slot = get_slot("asr")
        if not slot or not slot.get("model"):
            raise ValueError("together_asr_not_configured")
        with wave.open(io.BytesIO(data), "rb") as recording:
            if (recording.getnchannels() != 1 or recording.getsampwidth() != 2 or recording.getframerate() != 16000
                    or not 0 < recording.getnframes() <= 16000*max_seconds):
                raise ValueError("together_invalid_recording")
        with private_provider_call("together:asr"):
            text = await asyncio.wait_for(asyncio.to_thread(call_slot_asr, data, timeout=60), timeout=65)
        if not text:
            raise ValueError("together_asr_failed")
        return text[:8000]
