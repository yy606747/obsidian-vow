"""Thin real-provider adapters; tests inject the same interface without network."""

from app.provider_privacy import private_provider_call


class SceneModels:
    async def once(self, model_key, messages, *, scope):
        from ai_providers import call_core_chat_once
        meta = {}
        with private_provider_call("scene:" + scope):
            text = await call_core_chat_once(model_key, messages, scope="scene:" + scope,
                                            usage_meta=meta, max_tokens=5000)
        if not text or str(meta.get("finish_reason", "")).lower() in {"length", "max_tokens"}:
            raise ValueError("scene_model_incomplete")
        return text

    async def stream(self, model_key, messages):
        from ai_providers import stream_ai
        meta = {}
        source = stream_ai(messages, model_key, meta, retry_prohibited=False)
        try:
            while True:
                with private_provider_call("scene:turn"):
                    try:
                        chunk = await anext(source)
                    except StopAsyncIteration:
                        break
                if "\x00" in chunk or chunk.startswith("[错误]"):
                    raise ValueError("scene_model_failed")
                yield chunk
        finally:
            with private_provider_call("scene:turn"):
                await source.aclose()
        if meta.get("_prohibited") or meta.get("error") or str(meta.get("finish_reason", "")).lower() in {"length", "max_tokens"}:
            raise ValueError("scene_model_incomplete")

    async def compile(self, messages):
        from config import get_slot
        from ai_providers import call_slot_chat
        slot = get_slot("scene_compiler")
        if not slot or not str(slot.get("model") or "").strip():
            raise ValueError("scene_compiler_not_configured")
        meta = {}
        with private_provider_call("scene:compiler"):
            result = await call_slot_chat("scene_compiler", messages, expect_json=True,
                                          timeout=60, scope="scene:compiler", usage_meta=meta,
                                          max_tokens=4000, slot_snapshot=slot)
        if not result or str(meta.get("finish_reason", "")).lower() in {"length", "max_tokens"}:
            raise ValueError("scene_compile_failed")
        return result
