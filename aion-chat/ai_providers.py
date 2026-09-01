"""
AI 模型调用：
- 硬编码预设（MODELS）继续走 call_siliconflow / call_gemini / call_aipro
- 用户自定义端点走 call_openai_compat / call_gemini_compat（接受 endpoint 字典）
- 哨兵/语音 ASR 通过 slots 配置选端点
"""

from __future__ import annotations

import json, base64, mimetypes, traceback, asyncio, re, io, os, time
from pathlib import Path

import httpx
from prompt_cache import (
    CACHE_BOUNDARY_KEY,
    CACHE_SESSION_KEY,
    cache_request_policy,
    provider_event_usage_meta,
    update_cache_policy_meta,
    update_usage_meta,
)
from provider_status import (
    classify_exception,
    classify_http_status,
    new_request_id,
    record_provider_event,
)
from app.chat.audio_input import (
    AUDIO_INPUT_UNAVAILABLE_MESSAGE,
    attachment_mime_type,
    attachment_url,
    audio_format,
    contains_audio_attachment,
    is_audio_attachment,
    parse_attachments,
)


# ── 日志脱敏 ──────────────────────────────────────
# Gemini URL 会把 key 拼在 query string，httpx 异常/traceback 会带上整条 URL
# 凡是向外打印的字符串，过一下 _redact 再输出，避免 key 进日志。
_REDACT_PATTERNS = [
    (re.compile(r"(key=)[A-Za-z0-9_\-]{6,}", re.I), r"\1***"),
    (re.compile(r"(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9_\-]{6,}", re.I), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{6,}", re.I), r"\1***"),
    (re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{4,}"), r"\1***"),
]

def _redact(text: str) -> str:
    s = str(text)
    for pat, repl in _REDACT_PATTERNS:
        s = pat.sub(repl, s)
    return s


def _print_redacted_traceback():
    buf = io.StringIO()
    traceback.print_exc(file=buf)
    print(_redact(buf.getvalue()))


def _env(*names) -> str:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _resolve_timeout(endpoint: dict | None, default: float = 120.0) -> float:
    raw = (endpoint or {}).get("timeout_sec")
    if raw in (None, ""):
        raw = _env("AION_PROVIDER_TIMEOUT_SEC")
    try:
        value = float(raw)
        return max(5.0, min(value, 300.0))
    except Exception:
        return default


def _normalize_proxy_value(value: str | None) -> str | None:
    if not value:
        return None
    proxy = str(value).strip()
    if not proxy:
        return None
    if proxy.lower() in ("none", "direct", "off", "false", "0"):
        return None
    return proxy


def _resolve_proxy(provider_type: str, endpoint: dict | None,
                   *, preset_gemini: bool = False) -> str | None:
    raw = (endpoint or {}).get("proxy_url")
    if raw:
        return _normalize_proxy_value(raw)

    if provider_type in ("gemini", "vertex"):
        env_raw = _env("AION_GEMINI_PROXY", "AION_PROVIDER_PROXY")
        if env_raw:
            return _normalize_proxy_value(env_raw)
        return None
    env_raw = _env("AION_OPENAI_PROXY", "AION_PROVIDER_PROXY")
    if env_raw:
        return _normalize_proxy_value(env_raw)
    return None


def _provider_context(*, scope: str, provider_type: str, label: str, model: str,
                      base_url: str, endpoint: dict | None = None,
                      preset_gemini: bool = False) -> dict:
    proxy_url = _resolve_proxy(provider_type, endpoint, preset_gemini=preset_gemini)
    return {
        "request_id": new_request_id(),
        "scope": scope,
        "provider_type": provider_type,
        "provider_label": label,
        "endpoint_id": (endpoint or {}).get("id") or "",
        "endpoint_name": (endpoint or {}).get("name") or label,
        "base_url": base_url,
        "model": model,
        "proxy_enabled": bool(proxy_url),
        "proxy_url": proxy_url or "",
        "timeout_sec": _resolve_timeout(endpoint),
    }


def _provider_error_text(label: str, status: int | None, error_type: str,
                         elapsed_ms: int, message: str,
                         request_id: str = "") -> str:
    status_text = f"HTTP {status}" if status else error_type
    detail = _redact(message).strip()[:300] or "无响应"
    rid = f" / {request_id}" if request_id else ""
    return f"[错误] {label} 调用失败（{status_text} / {error_type} / {elapsed_ms}ms{rid}）：{detail}"


def _finish_provider_call(meta: dict | None, ctx: dict, *, ok: bool,
                          start: float, http_status=None,
                          error_type: str = "", retryable: bool = False,
                          message: str = "") -> dict:
    event = dict(ctx)
    event.update({
        "ok": ok,
        "http_status": http_status,
        "error_type": error_type or ("ok" if ok else "unknown"),
        "retryable": retryable,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
        "message": _redact(message),
    })
    usage_event_meta = provider_event_usage_meta(meta)
    if usage_event_meta:
        event["meta"] = usage_event_meta
    saved = record_provider_event(event)
    if meta is not None:
        meta.setdefault("provider_calls", []).append(saved)
        meta["provider_last"] = saved
    return saved


def _event_test_payload(event: dict, message: str = "") -> dict:
    return {
        "ok": bool(event.get("ok")),
        "message": message or event.get("message") or ("OK" if event.get("ok") else ""),
        "status_code": event.get("http_status"),
        "error_type": event.get("error_type"),
        "latency_ms": event.get("elapsed_ms"),
        "request_id": event.get("request_id"),
        "endpoint_id": event.get("endpoint_id"),
        "endpoint_name": event.get("endpoint_name"),
        "provider_type": event.get("provider_type"),
        "model": event.get("model"),
        "proxy_enabled": event.get("proxy_enabled"),
    }

from config import (
    get_key, MODELS, UPLOADS_DIR,
    get_slot, resolve_core_model,
)


# ── 多模态消息构建 ────────────────────────────────
def _openai_text_part(text: str, marker_style: str = "") -> dict:
    part = {"type": "text", "text": text}
    if marker_style == "prompt_cache_breakpoint":
        part["prompt_cache_breakpoint"] = {"mode": "explicit"}
    elif marker_style == "cache_control":
        part["cache_control"] = {"type": "ephemeral"}
    return part


def build_multimodal_messages(
    history: list,
    *,
    marker_style: str = "",
    include_audio: bool = False,
):
    """将带附件的历史记录转换为 OpenAI 兼容多模态格式"""
    result = []
    for m in history:
        boundary_style = marker_style if m.get(CACHE_BOUNDARY_KEY) else ""
        attachments = parse_attachments(m.get("attachments", []))
        if attachments and m["role"] == "user":
            parts = []
            if m["content"]:
                parts.append(_openai_text_part(m["content"], boundary_style))
            for att in attachments:
                url = attachment_url(att)
                fpath = UPLOADS_DIR / Path(url).name
                if is_audio_attachment(att) and include_audio and not fpath.is_file():
                    raise FileNotFoundError(f"Audio attachment is unavailable: {Path(url).name}")
                if fpath.exists():
                    mime = attachment_mime_type(att) or mimetypes.guess_type(str(fpath))[0] or "image/jpeg"
                    b64 = base64.b64encode(fpath.read_bytes()).decode()
                    if is_audio_attachment(att):
                        if not include_audio:
                            continue
                        fmt = audio_format(att)
                        if fmt not in {"wav", "mp3"}:
                            raise ValueError("Direct audio input only supports WAV or MP3")
                        parts.append({
                            "type": "input_audio",
                            "input_audio": {"data": b64, "format": fmt},
                        })
                    elif mime.startswith("image/"):
                        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                    elif mime.startswith("video/"):
                        parts.append({"type": "video_url", "video_url": {"url": f"data:{mime};base64,{b64}"}})
            result.append({"role": m["role"], "content": parts if parts else m["content"]})
        elif boundary_style and m.get("content"):
            result.append({
                "role": m["role"],
                "content": [_openai_text_part(str(m["content"]), boundary_style)],
            })
        else:
            result.append({"role": m["role"], "content": m["content"]})
    return result


def build_gemini_contents(history: list, *, include_audio: bool = False):
    """将带附件的历史记录转换为 Gemini 格式"""
    contents = []
    for m in history:
        role = "user" if m["role"] == "user" else "model"
        attachments = parse_attachments(m.get("attachments", []))
        parts = []
        if m["content"]:
            parts.append({"text": m["content"]})
        if attachments and m["role"] == "user":
            for att in attachments:
                if is_audio_attachment(att) and not include_audio:
                    continue
                url = attachment_url(att)
                fpath = UPLOADS_DIR / Path(url).name
                if is_audio_attachment(att) and not fpath.is_file():
                    raise FileNotFoundError(f"Audio attachment is unavailable: {Path(url).name}")
                if fpath.exists():
                    mime = attachment_mime_type(att) or mimetypes.guess_type(str(fpath))[0] or "image/jpeg"
                    b64 = base64.b64encode(fpath.read_bytes()).decode()
                    parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        contents.append({"role": role, "parts": parts if parts else [{"text": m["content"]}]})
    return contents


# ── 预设：硅基流动 / Gemini / AiPro（保持原 URL + get_key 兼容）──
async def call_siliconflow(messages, model, meta=None, temperature=None, *, include_audio=False):
    endpoint = {
        "id": "preset_siliconflow",
        "name": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "type": "openai",
    }
    async for c in _stream_openai(
        base_url=endpoint["base_url"],
        api_key=get_key("siliconflow"),
        messages=messages, model=model, meta=meta, temperature=temperature,
        label="硅基流动", endpoint=endpoint, include_audio=include_audio,
    ):
        yield c


async def call_gemini(messages, model, meta=None, temperature=None, *, include_audio=False):
    endpoint = {
        "id": "preset_gemini",
        "name": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "type": "gemini",
    }
    async for c in _stream_gemini(
        base_url=endpoint["base_url"],
        api_key=get_key("gemini"),
        messages=messages, model=model, meta=meta, temperature=temperature,
        label="Gemini", endpoint=endpoint, preset_gemini=True,
        include_audio=include_audio,
    ):
        yield c


async def call_aipro(messages, model, meta=None, temperature=None, *, include_audio=False):
    endpoint = {
        "id": "preset_aipro",
        "name": "中转站",
        "base_url": "https://vip.aipro.love/v1",
        "type": "openai",
    }
    async for c in _stream_openai(
        base_url=endpoint["base_url"],
        api_key=get_key("aipro"),
        messages=messages, model=model, meta=meta, temperature=temperature,
        label="中转站", endpoint=endpoint, include_audio=include_audio,
    ):
        yield c


# ── 底层通用流：OpenAI 兼容 ───────────────────────
async def _stream_openai(*, base_url, api_key, messages, model,
                         meta=None, temperature=None, label="OpenAI",
                         endpoint: dict | None = None, scope: str = "core",
                         include_audio: bool = False):
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    policy = cache_request_policy(
        base_url=base_url,
        model=model,
        endpoint_type=(endpoint or {}).get("type", "openai"),
        messages=messages,
    )
    update_cache_policy_meta(meta, policy)
    api_messages = build_multimodal_messages(
        messages,
        marker_style=policy["marker_style"],
        include_audio=include_audio,
    )
    payload = {"model": model, "messages": api_messages, "stream": True,
               "stream_options": {"include_usage": True}}
    payload.update(policy["request_fields"])
    if temperature is not None:
        payload["temperature"] = temperature
    ctx = _provider_context(
        scope=scope, provider_type="openai", label=label, model=model,
        base_url=base_url, endpoint=endpoint,
    )
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=ctx["timeout_sec"], proxy=ctx["proxy_url"] or None
        ) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    try:
                        err = json.loads(body).get("error", {}).get("message", body.decode())
                    except Exception:
                        err = body.decode(errors="replace")[:500]
                    error_type, retryable = classify_http_status(resp.status_code)
                    event = _finish_provider_call(
                        meta, ctx, ok=False, start=start,
                        http_status=resp.status_code, error_type=error_type,
                        retryable=retryable, message=err,
                    )
                    await _broadcast_endpoint_error(
                        scope, ctx["endpoint_id"], event["message"],
                        status=resp.status_code, error_type=error_type,
                        elapsed_ms=event["elapsed_ms"],
                        request_id=event["request_id"],
                    )
                    yield _provider_error_text(label, resp.status_code, error_type,
                                               event["elapsed_ms"], err,
                                               event["request_id"])
                    return
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            _finish_provider_call(meta, ctx, ok=True, start=start)
                            return
                        try:
                            chunk = json.loads(data)
                            if meta is not None and "usage" in chunk and chunk["usage"]:
                                u = chunk["usage"]
                                update_usage_meta(meta, u, provider_type="openai")
                            delta = chunk["choices"][0].get("delta", {}) if chunk.get("choices") else {}
                            if "content" in delta and delta["content"]:
                                yield delta["content"]
                        except Exception:
                            pass
        _finish_provider_call(meta, ctx, ok=True, start=start)
    except Exception as e:
        error_type, retryable = classify_exception(e)
        event = _finish_provider_call(
            meta, ctx, ok=False, start=start,
            error_type=error_type, retryable=retryable, message=str(e),
        )
        await _broadcast_endpoint_error(
            scope, ctx["endpoint_id"], event["message"],
            error_type=error_type, elapsed_ms=event["elapsed_ms"],
            request_id=event["request_id"],
        )
        yield _provider_error_text(label, None, error_type, event["elapsed_ms"], str(e), event["request_id"])


GEMINI_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
]


def _vertex_model_url(base_url: str, model: str, method: str, *, stream: bool = False) -> str:
    suffix = "?alt=sse" if stream else ""
    return f"{base_url.rstrip('/')}/models/{model}:{method}{suffix}"


def _vertex_headers(api_key: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-goog-api-key"] = api_key
    return headers


def _gemini_stream_payload(
    messages: list,
    temperature: float | None = None,
    *,
    include_audio: bool = False,
) -> dict:
    payload = {
        "contents": build_gemini_contents(messages, include_audio=include_audio),
        "safetySettings": GEMINI_SAFETY_SETTINGS,
    }
    if temperature is not None:
        payload["generationConfig"] = {"temperature": temperature}
    return payload


def _gemini_slot_payload(
    messages: list,
    temperature: float | None = None,
    max_tokens: int | None = None,
    *,
    expect_json: bool = False,
    response_schema: dict | None = None,
    thinking_budget: int | None = None,
) -> dict:
    contents = []
    system_text = ""
    for m in messages:
        if m["role"] == "system":
            system_text += m.get("content", "") + "\n"
        else:
            role = "user" if m["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    payload = {"contents": contents}
    if system_text.strip():
        payload["systemInstruction"] = {"parts": [{"text": system_text.strip()}]}
    generation_config = {}
    if temperature is not None:
        generation_config["temperature"] = temperature
    if max_tokens is not None:
        generation_config["maxOutputTokens"] = max_tokens
    if response_schema is not None:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseJsonSchema"] = response_schema
    elif expect_json:
        generation_config["responseMimeType"] = "application/json"
    if thinking_budget is not None:
        generation_config["thinkingConfig"] = {
            "thinkingBudget": int(thinking_budget),
        }
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


def _gemini_response_text(data: dict) -> str:
    return (data.get("candidates", [{}])[0]
                .get("content", {}).get("parts", [{}])[0].get("text", ""))


# ── 底层通用流：Gemini ─────────────────────────────
async def _stream_gemini(*, base_url, api_key, messages, model,
                         meta=None, temperature=None, label="Gemini",
                         endpoint: dict | None = None, scope: str = "core",
                         preset_gemini: bool = False,
                         include_audio: bool = False):
    url = f"{base_url.rstrip('/')}/models/{model}:streamGenerateContent?alt=sse&key={api_key}"
    policy = cache_request_policy(
        base_url=base_url,
        model=model,
        endpoint_type="gemini",
        messages=messages,
    )
    update_cache_policy_meta(meta, policy)
    payload = _gemini_stream_payload(
        messages,
        temperature,
        include_audio=include_audio,
    )
    ctx = _provider_context(
        scope=scope, provider_type="gemini", label=label, model=model,
        base_url=base_url, endpoint=endpoint, preset_gemini=preset_gemini,
    )
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=ctx["timeout_sec"], proxy=ctx["proxy_url"] or None
        ) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    try:
                        err = json.loads(body).get("error", {}).get("message", body.decode())
                    except Exception:
                        err = body.decode(errors="replace")[:500]
                    error_type, retryable = classify_http_status(resp.status_code)
                    event = _finish_provider_call(
                        meta, ctx, ok=False, start=start,
                        http_status=resp.status_code, error_type=error_type,
                        retryable=retryable, message=err,
                    )
                    await _broadcast_endpoint_error(
                        scope, ctx["endpoint_id"], event["message"],
                        status=resp.status_code, error_type=error_type,
                        elapsed_ms=event["elapsed_ms"],
                        request_id=event["request_id"],
                    )
                    yield _provider_error_text(label, resp.status_code, error_type,
                                               event["elapsed_ms"], err,
                                               event["request_id"])
                    return
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            chunk = json.loads(line[6:])
                            if meta is not None and "usageMetadata" in chunk:
                                u = chunk["usageMetadata"]
                                update_usage_meta(meta, u, provider_type="gemini")
                            cand = chunk.get("candidates", [{}])[0]
                            finish = cand.get("finishReason", "")
                            if finish and finish not in ("", "STOP"):
                                print(f"[{label}] finishReason={finish}, safetyRatings={cand.get('safetyRatings','')}")
                                if finish == "PROHIBITED_CONTENT" and meta is not None:
                                    meta["_prohibited"] = True
                            text = cand.get("content", {}).get("parts", [{}])[0].get("text", "")
                            if text:
                                yield text
                        except Exception:
                            pass
        _finish_provider_call(meta, ctx, ok=True, start=start)
    except Exception as e:
        error_type, retryable = classify_exception(e)
        event = _finish_provider_call(
            meta, ctx, ok=False, start=start,
            error_type=error_type, retryable=retryable, message=str(e),
        )
        await _broadcast_endpoint_error(
            scope, ctx["endpoint_id"], event["message"],
            error_type=error_type, elapsed_ms=event["elapsed_ms"],
            request_id=event["request_id"],
        )
        yield _provider_error_text(label, None, error_type, event["elapsed_ms"], str(e), event["request_id"])


# ── 底层通用流：Vertex AI Gemini ───────────────────
async def _stream_vertex(*, base_url, api_key, messages, model,
                         meta=None, temperature=None, label="Vertex Gemini",
                         endpoint: dict | None = None, scope: str = "core",
                         include_audio: bool = False):
    url = _vertex_model_url(base_url, model, "streamGenerateContent", stream=True)
    policy = cache_request_policy(
        base_url=base_url,
        model=model,
        endpoint_type="vertex",
        messages=messages,
    )
    update_cache_policy_meta(meta, policy)
    payload = _gemini_stream_payload(
        messages,
        temperature,
        include_audio=include_audio,
    )
    headers = _vertex_headers(api_key)
    ctx = _provider_context(
        scope=scope, provider_type="vertex", label=label, model=model,
        base_url=base_url, endpoint=endpoint,
    )
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=ctx["timeout_sec"], proxy=ctx["proxy_url"] or None
        ) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    try:
                        err = json.loads(body).get("error", {}).get("message", body.decode())
                    except Exception:
                        err = body.decode(errors="replace")[:500]
                    error_type, retryable = classify_http_status(resp.status_code)
                    event = _finish_provider_call(
                        meta, ctx, ok=False, start=start,
                        http_status=resp.status_code, error_type=error_type,
                        retryable=retryable, message=err,
                    )
                    await _broadcast_endpoint_error(
                        scope, ctx["endpoint_id"], event["message"],
                        status=resp.status_code, error_type=error_type,
                        elapsed_ms=event["elapsed_ms"],
                        request_id=event["request_id"],
                    )
                    yield _provider_error_text(label, resp.status_code, error_type,
                                               event["elapsed_ms"], err,
                                               event["request_id"])
                    return
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            chunk = json.loads(line[6:])
                            if meta is not None and "usageMetadata" in chunk:
                                u = chunk["usageMetadata"]
                                update_usage_meta(meta, u, provider_type="vertex")
                            cand = chunk.get("candidates", [{}])[0]
                            finish = cand.get("finishReason", "")
                            if finish and finish not in ("", "STOP"):
                                print(f"[{label}] finishReason={finish}, safetyRatings={cand.get('safetyRatings','')}")
                                if finish == "PROHIBITED_CONTENT" and meta is not None:
                                    meta["_prohibited"] = True
                            text = cand.get("content", {}).get("parts", [{}])[0].get("text", "")
                            if text:
                                yield text
                        except Exception:
                            pass
        _finish_provider_call(meta, ctx, ok=True, start=start)
    except Exception as e:
        error_type, retryable = classify_exception(e)
        event = _finish_provider_call(
            meta, ctx, ok=False, start=start,
            error_type=error_type, retryable=retryable, message=str(e),
        )
        await _broadcast_endpoint_error(
            scope, ctx["endpoint_id"], event["message"],
            error_type=error_type, elapsed_ms=event["elapsed_ms"],
            request_id=event["request_id"],
        )
        yield _provider_error_text(label, None, error_type, event["elapsed_ms"], str(e), event["request_id"])


# ── 自定义端点入口 ────────────────────────────────
async def call_openai_compat(endpoint: dict, model: str, messages: list,
                             meta=None, temperature=None, *, include_audio=False):
    async for c in _stream_openai(
        base_url=endpoint.get("base_url", ""),
        api_key=endpoint.get("api_key", ""),
        messages=messages, model=model, meta=meta, temperature=temperature,
        label=endpoint.get("name", "自定义"), endpoint=endpoint,
        include_audio=include_audio,
    ):
        yield c


async def call_gemini_compat(endpoint: dict, model: str, messages: list,
                             meta=None, temperature=None, *, include_audio=False):
    async for c in _stream_gemini(
        base_url=endpoint.get("base_url", ""),
        api_key=endpoint.get("api_key", ""),
        messages=messages, model=model, meta=meta, temperature=temperature,
        label=endpoint.get("name", "自定义"), endpoint=endpoint,
        include_audio=include_audio,
    ):
        yield c


async def call_vertex_compat(endpoint: dict, model: str, messages: list,
                             meta=None, temperature=None, *, include_audio=False):
    async for c in _stream_vertex(
        base_url=endpoint.get("base_url", ""),
        api_key=endpoint.get("api_key", ""),
        messages=messages, model=model, meta=meta, temperature=temperature,
        label=endpoint.get("name", "Vertex Gemini"), endpoint=endpoint,
        include_audio=include_audio,
    ):
        yield c


# ── 统一调度（主脑入口）────────────────────────────
def _normalize_for_claude_like(messages: list) -> list:
    """
    Claude-on-Vertex 等中转站对消息交替校验严格：
    - 不允许空 content 消息
    - 连续同角色会判定为 prefill / 格式错误
    - 末尾必须是 user（否则报 "must end with a user message"）
    这里先做角色映射（cam_* → user/assistant），再合并连续同角色、剔除空消息，
    并丢弃尾部 assistant，确保所有后端都拿到一份规整的 messages。
    """
    def _att_list(v):
        if isinstance(v, str):
            try: return json.loads(v) if v else []
            except: return []
        return list(v or [])

    cleaned = []
    for m in messages:
        role = m.get("role")
        if role in ("cam_user", "cam_trigger"):
            role = "user"
        elif role == "cam_log":
            role = "assistant"
        content = m.get("content") or ""
        att = _att_list(m.get("attachments"))
        if not content and not att:
            continue
        nm = dict(m)
        nm["role"] = role
        nm["content"] = content
        if att:
            nm["attachments"] = att
        cleaned.append(nm)

    merged = []
    for m in cleaned:
        if merged and merged[-1]["role"] == m["role"]:
            prev = merged[-1]
            pc, nc = prev.get("content") or "", m.get("content") or ""
            prev["content"] = f"{pc}\n\n{nc}" if pc and nc else (pc or nc)
            pa, na = _att_list(prev.get("attachments")), _att_list(m.get("attachments"))
            if pa or na:
                prev["attachments"] = pa + na
            if m.get(CACHE_BOUNDARY_KEY):
                prev[CACHE_BOUNDARY_KEY] = True
                if m.get(CACHE_SESSION_KEY):
                    prev[CACHE_SESSION_KEY] = m[CACHE_SESSION_KEY]
        else:
            merged.append(m)

    while merged and merged[-1]["role"] == "assistant":
        merged.pop()

    return merged


async def stream_ai(messages: list, model_key: str, meta: dict | None = None,
                    temperature: float | None = None):
    normalized = _normalize_for_claude_like(messages)

    cfg = resolve_core_model(model_key)
    if not cfg:
        yield f"[错误] 未知模型: {model_key}"
        return

    include_audio = cfg.get("audio_input") is True
    history_has_audio = any(
        contains_audio_attachment(message.get("attachments", []))
        for message in normalized
    )
    if history_has_audio and not include_audio:
        yield f"[错误] {AUDIO_INPUT_UNAVAILABLE_MESSAGE}"
        return
    audio_kwargs = {"include_audio": True} if include_audio else {}

    if cfg.get("_kind") == "custom":
        ep = cfg["endpoint"]
        ep_type = ep.get("type", "openai")
        if ep_type in ("gemini", "vertex"):
            provider_call = call_vertex_compat if ep_type == "vertex" else call_gemini_compat
            async for chunk in provider_call(
                ep, cfg["model"], normalized, meta, temperature, **audio_kwargs
            ):
                yield chunk
            if meta and meta.pop("_prohibited", False):
                print(f"[Auto-retry] Gemini PROHIBITED_CONTENT, retrying...")
                yield "\x00RETRY\x00"
                async for chunk in provider_call(
                    ep, cfg["model"], normalized, meta, temperature, **audio_kwargs
                ):
                    yield chunk
        else:
            async for chunk in call_openai_compat(
                ep, cfg["model"], normalized, meta, temperature, **audio_kwargs
            ):
                yield chunk
        return

    # 预设：维持原有逻辑
    provider = cfg["provider"]
    model = cfg["model"]
    if provider == "siliconflow":
        async for chunk in call_siliconflow(
            normalized, model, meta, temperature, **audio_kwargs
        ):
            yield chunk
    elif provider == "gemini":
        async for chunk in call_gemini(
            normalized, model, meta, temperature, **audio_kwargs
        ):
            yield chunk
        if meta and meta.pop("_prohibited", False):
            print(f"[Auto-retry] Gemini PROHIBITED_CONTENT, retrying...")
            yield "\x00RETRY\x00"
            async for chunk in call_gemini(
                normalized, model, meta, temperature, **audio_kwargs
            ):
                yield chunk
    elif provider == "aipro":
        async for chunk in call_aipro(
            normalized, model, meta, temperature, **audio_kwargs
        ):
            yield chunk


# ── Slot 辅助（哨兵 / 语音 ASR 用）────────────────
async def _broadcast_endpoint_error(slot_name: str, endpoint_id: str, error: str, **extra):
    """失败时往前端推一条，用户能第一时间看到是哪个槽位挂了。"""
    try:
        from ws import manager
        data = {"slot": slot_name, "endpoint": endpoint_id,
                "error": _redact(error)[:300]}
        data.update({k: v for k, v in extra.items() if v not in (None, "")})
        await manager.broadcast({
            "type": "endpoint_error",
            "data": data,
        })
    except Exception:
        pass


async def _call_endpoint_chat_once(
    *,
    endpoint: dict,
    model: str,
    messages: list,
    error_channel: str,
    expect_json: bool = False,
    timeout: float = 60.0,
    temperature: float | None = None,
    scope: str,
    usage_meta: dict | None = None,
    max_tokens: int | None = None,
    preset_gemini: bool = False,
    response_schema: dict | None = None,
    thinking_budget: int | None = None,
    enable_thinking: bool | None = None,
) -> str:
    """One non-streaming transport call. No retry or endpoint failover."""

    ep_type = endpoint.get("type", "openai")
    ctx = _provider_context(
        scope=scope,
        provider_type=ep_type,
        label=endpoint.get("name", "自定义"),
        model=model,
        base_url=endpoint.get("base_url", ""),
        endpoint=endpoint,
        preset_gemini=preset_gemini,
    )
    ctx["timeout_sec"] = _resolve_timeout(endpoint, timeout)
    start = time.monotonic()
    try:
        if ep_type == "openai":
            url = endpoint["base_url"].rstrip("/") + "/chat/completions"
            headers = {
                "Authorization": f"Bearer {endpoint.get('api_key', '')}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "messages": build_multimodal_messages(messages),
                "stream": False,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            if enable_thinking is not None:
                payload["enable_thinking"] = enable_thinking
            if response_schema is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "structured_response",
                        "strict": True,
                        "schema": response_schema,
                    },
                }
            elif expect_json:
                payload["response_format"] = {"type": "json_object"}
            async with httpx.AsyncClient(
                timeout=ctx["timeout_sec"],
                proxy=ctx["proxy_url"] or None,
                trust_env=False,
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
        elif ep_type in ("gemini", "vertex"):
            if ep_type == "vertex":
                url = _vertex_model_url(endpoint["base_url"], model, "generateContent")
                headers = _vertex_headers(endpoint.get("api_key", ""))
            else:
                url = (
                    f"{endpoint['base_url'].rstrip('/')}/models/{model}:generateContent"
                    f"?key={endpoint.get('api_key', '')}"
                )
                headers = None
            payload = _gemini_slot_payload(
                messages,
                temperature,
                max_tokens,
                expect_json=expect_json,
                response_schema=response_schema,
                thinking_budget=thinking_budget,
            )
            payload["safetySettings"] = GEMINI_SAFETY_SETTINGS
            async with httpx.AsyncClient(
                timeout=ctx["timeout_sec"],
                proxy=ctx["proxy_url"] or None,
                trust_env=False,
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
        else:
            await _broadcast_endpoint_error(
                error_channel,
                endpoint.get("id", ""),
                f"不支持的端点类型: {ep_type}",
            )
            return ""

        if resp.status_code != 200:
            error_type, retryable = classify_http_status(resp.status_code)
            event = _finish_provider_call(
                usage_meta,
                ctx,
                ok=False,
                start=start,
                http_status=resp.status_code,
                error_type=error_type,
                retryable=retryable,
                message=resp.text[:200],
            )
            await _broadcast_endpoint_error(
                error_channel,
                endpoint.get("id", ""),
                f"HTTP {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
                error_type=error_type,
                elapsed_ms=event["elapsed_ms"],
                request_id=event["request_id"],
            )
            return ""

        data = resp.json()
        if ep_type == "openai":
            choice = data["choices"][0]
            if usage_meta is not None and data.get("usage"):
                update_usage_meta(usage_meta, data["usage"], provider_type="openai")
            if usage_meta is not None:
                usage_meta["finish_reason"] = str(choice.get("finish_reason") or "")
            text = choice["message"]["content"]
        else:
            if usage_meta is not None and data.get("usageMetadata"):
                update_usage_meta(usage_meta, data["usageMetadata"], provider_type=ep_type)
            if usage_meta is not None:
                candidates = data.get("candidates") or []
                first_candidate = candidates[0] if candidates else {}
                usage_meta["finish_reason"] = str(
                    first_candidate.get("finishReason") or ""
                )
            text = _gemini_response_text(data)
        _finish_provider_call(
            usage_meta,
            ctx,
            ok=True,
            start=start,
            http_status=resp.status_code,
        )
        return str(text or "")
    except Exception as exc:
        print(f"[single_chat:{error_channel}] {_redact(exc)}")
        _print_redacted_traceback()
        error_type, retryable = classify_exception(exc)
        event = _finish_provider_call(
            usage_meta,
            ctx,
            ok=False,
            start=start,
            error_type=error_type,
            retryable=retryable,
            message=str(exc),
        )
        await _broadcast_endpoint_error(
            error_channel,
            endpoint.get("id", ""),
            _redact(str(exc)),
            error_type=error_type,
            elapsed_ms=event["elapsed_ms"],
            request_id=event["request_id"],
        )
        return ""


async def call_core_chat_once(
    model_key: str,
    messages: list,
    *,
    expect_json: bool = False,
    timeout: float = 120.0,
    temperature: float | None = None,
    scope: str = "core:single",
    usage_meta: dict | None = None,
    max_tokens: int | None = None,
) -> str:
    """Resolve the captured core model and perform exactly one provider call."""

    cfg = resolve_core_model(model_key)
    if not cfg:
        await _broadcast_endpoint_error(scope, "", f"未知核心模型: {model_key}")
        return ""
    normalized = _normalize_for_claude_like(messages)
    model = str(cfg.get("model") or "")
    preset_gemini = False
    if cfg.get("_kind") == "custom":
        endpoint = dict(cfg["endpoint"])
    else:
        provider = cfg.get("provider")
        if provider == "siliconflow":
            endpoint = {
                "id": "preset_siliconflow",
                "name": "硅基流动",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": get_key("siliconflow"),
                "type": "openai",
            }
        elif provider == "gemini":
            endpoint = {
                "id": "preset_gemini",
                "name": "Gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": get_key("gemini"),
                "type": "gemini",
            }
            preset_gemini = True
        elif provider == "aipro":
            endpoint = {
                "id": "preset_aipro",
                "name": "中转站",
                "base_url": "https://vip.aipro.love/v1",
                "api_key": get_key("aipro"),
                "type": "openai",
            }
        else:
            await _broadcast_endpoint_error(scope, "", f"不支持的核心 provider: {provider}")
            return ""
    return await _call_endpoint_chat_once(
        endpoint=endpoint,
        model=model,
        messages=normalized,
        error_channel=scope,
        expect_json=expect_json,
        timeout=timeout,
        temperature=temperature,
        scope=scope,
        usage_meta=usage_meta,
        max_tokens=max_tokens,
        preset_gemini=preset_gemini,
    )


async def call_slot_chat(slot_name: str, messages: list,
                         expect_json: bool = False,
                         timeout: float = 60.0,
                         temperature: float | None = None,
                         scope: str | None = None,
                         usage_meta: dict | None = None,
                         max_tokens: int | None = None,
                         model_override: str | None = None,
                         response_schema: dict | None = None,
                         thinking_budget: int | None = None) -> str:
    """非流式一次性调用（后台槽位用）。
    失败返回空字符串并在前端推 endpoint_error。

    ``model_override`` freezes an already-audited model identity while still
    resolving the slot endpoint at execution time.  Existing callers keep the
    live slot model when it is omitted.
    """
    slot = get_slot(slot_name)
    if not slot:
        await _broadcast_endpoint_error(slot_name, "", "槽位未配置或端点丢失")
        return ""
    model = str(model_override if model_override is not None else slot["model"]).strip()
    if not model:
        await _broadcast_endpoint_error(slot_name, "", "槽位模型未配置")
        return ""
    enable_thinking = slot["extras"].get("enable_thinking")
    if not isinstance(enable_thinking, bool):
        enable_thinking = None
    return await _call_endpoint_chat_once(
        endpoint=slot["endpoint"],
        model=model,
        messages=messages,
        error_channel=slot_name,
        expect_json=expect_json,
        timeout=timeout,
        temperature=temperature,
        scope=scope or f"slot:{slot_name}",
        usage_meta=usage_meta,
        max_tokens=max_tokens,
        response_schema=response_schema,
        thinking_budget=thinking_budget,
        enable_thinking=enable_thinking,
    )


def call_slot_asr(wav_bytes: bytes, language: str = "zh",
                  timeout: float = 15.0) -> str:
    """同步 ASR。voice 在后台线程里调，不能 await。
    失败返回空字符串并把错误广播到前端（用 anyio/event loop 能找到时）。"""
    slot = get_slot("asr")
    if not slot:
        _sync_broadcast_error("asr", "", "槽位未配置")
        return ""
    ep = slot["endpoint"]
    model = slot["model"]
    if ep.get("type", "openai") != "openai":
        _sync_broadcast_error("asr", ep.get("id", ""), "ASR 仅支持 openai 类型端点")
        return ""
    path = slot["extras"].get("path", "/audio/transcriptions")
    url = ep["base_url"].rstrip("/") + path
    ctx = _provider_context(
        scope="slot:asr", provider_type="openai",
        label=ep.get("name", "自定义"), model=model,
        base_url=ep.get("base_url", ""), endpoint=ep,
    )
    ctx["timeout_sec"] = _resolve_timeout(ep, timeout)
    start = time.monotonic()
    try:
        with httpx.Client(timeout=ctx["timeout_sec"], proxy=ctx["proxy_url"] or None) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {ep.get('api_key','')}"},
                files={"file": ("s.wav", wav_bytes, "audio/wav")},
                data={"model": model, "language": language},
        )
        if resp.status_code != 200:
            error_type, retryable = classify_http_status(resp.status_code)
            event = _finish_provider_call(
                None, ctx, ok=False, start=start, http_status=resp.status_code,
                error_type=error_type, retryable=retryable, message=resp.text[:200],
            )
            _sync_broadcast_error("asr", ep["id"], f"HTTP {resp.status_code}: {resp.text[:200]}",
                                  status=resp.status_code, error_type=error_type,
                                  elapsed_ms=event["elapsed_ms"],
                                  request_id=event["request_id"])
            return ""
        _finish_provider_call(None, ctx, ok=True, start=start,
                              http_status=resp.status_code)
        return resp.json().get("text", "").strip()
    except Exception as e:
        print(f"[call_slot_asr] {_redact(e)}")
        error_type, retryable = classify_exception(e)
        event = _finish_provider_call(
            None, ctx, ok=False, start=start,
            error_type=error_type, retryable=retryable, message=str(e),
        )
        _sync_broadcast_error("asr", ep.get("id", ""), _redact(str(e)),
                              error_type=error_type, elapsed_ms=event["elapsed_ms"],
                              request_id=event["request_id"])
        return ""


def _sync_broadcast_error(slot_name: str, endpoint_id: str, error: str, **extra):
    """从同步代码里尝试把错误发到前端；拿不到 loop 就只打印。"""
    try:
        from ws import manager
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            data = {"slot": slot_name, "endpoint": endpoint_id,
                    "error": _redact(error)[:300]}
            data.update({k: v for k, v in extra.items() if v not in (None, "")})
            asyncio.run_coroutine_threadsafe(
                manager.broadcast({
                    "type": "endpoint_error",
                    "data": data,
                }),
                loop,
            )
    except Exception:
        pass


# ── 端点测试（给 UI 用的「保存前体检」）──────────
async def test_endpoint(endpoint: dict, model: str | None = None,
                        timeout: float = 8.0) -> dict:
    """对端点发一个极小请求，确认可达 + 鉴权 OK + 模型存在。
    返回结构化诊断结果，兼容旧 UI 的 message 字段。"""
    ep_type = endpoint.get("type", "openai")
    if ep_type == "openai":
        base = endpoint["base_url"].rstrip("/")
        headers = {"Authorization": f"Bearer {endpoint.get('api_key', '')}"}
        ctx = _provider_context(
            scope="endpoint_test", provider_type="openai",
            label=endpoint.get("name", "自定义"),
            model=model or "", base_url=base, endpoint=endpoint,
        )
        ctx["timeout_sec"] = _resolve_timeout(endpoint, timeout)
        start = time.monotonic()
        # 未指定模型时，用 /models 接口验证可达性和鉴权，成功即返回
        if not model:
            try:
                async with httpx.AsyncClient(
                    timeout=ctx["timeout_sec"], proxy=ctx["proxy_url"] or None
                ) as client:
                    r = await client.get(f"{base}/models", headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    models_list = data.get("data") or data.get("models") or []
                    count = len(models_list)
                    event = _finish_provider_call(None, ctx, ok=True, start=start,
                                                  http_status=r.status_code)
                    return _event_test_payload(event, f"连通成功（{count} 个可用模型）")
                elif r.status_code in (401, 403):
                    error_type, retryable = classify_http_status(r.status_code)
                    event = _finish_provider_call(
                        None, ctx, ok=False, start=start, http_status=r.status_code,
                        error_type=error_type, retryable=retryable, message=r.text[:200],
                    )
                    return _event_test_payload(event, f"鉴权失败 HTTP {r.status_code}: {r.text[:200]}")
                else:
                    # /models 不支持时降级到 chat 测试，用通用小模型
                    model = "gpt-3.5-turbo"
            except Exception as e:
                error_type, retryable = classify_exception(e)
                event = _finish_provider_call(
                    None, ctx, ok=False, start=start,
                    error_type=error_type, retryable=retryable, message=str(e),
                )
                return _event_test_payload(event, f"连接失败: {_redact(e)}")
        mdl = model
        ctx["model"] = mdl
        url = f"{base}/chat/completions"
        payload = {"model": mdl,
                   "messages": [{"role": "user", "content": "ping"}],
                   "max_tokens": 1, "stream": False}
        try:
            async with httpx.AsyncClient(
                timeout=ctx["timeout_sec"], proxy=ctx["proxy_url"] or None
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                event = _finish_provider_call(None, ctx, ok=True, start=start,
                                              http_status=resp.status_code)
                return _event_test_payload(event, f"OK（模型：{mdl}）")
            error_type, retryable = classify_http_status(resp.status_code)
            event = _finish_provider_call(
                None, ctx, ok=False, start=start, http_status=resp.status_code,
                error_type=error_type, retryable=retryable, message=resp.text[:200],
            )
            return _event_test_payload(event, f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            error_type, retryable = classify_exception(e)
            event = _finish_provider_call(
                None, ctx, ok=False, start=start,
                error_type=error_type, retryable=retryable, message=str(e),
            )
            return _event_test_payload(event, f"连接失败: {_redact(e)}")
    elif ep_type == "gemini":
        mdl = model or "gemini-3-flash-preview"
        ctx = _provider_context(
            scope="endpoint_test", provider_type="gemini",
            label=endpoint.get("name", "自定义"),
            model=mdl, base_url=endpoint.get("base_url", ""), endpoint=endpoint,
        )
        ctx["timeout_sec"] = _resolve_timeout(endpoint, timeout)
        start = time.monotonic()
        url = (f"{endpoint['base_url'].rstrip('/')}/models/{mdl}:generateContent"
               f"?key={endpoint.get('api_key','')}")
        payload = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}
        try:
            async with httpx.AsyncClient(
                timeout=ctx["timeout_sec"], proxy=ctx["proxy_url"] or None
            ) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                event = _finish_provider_call(None, ctx, ok=True, start=start,
                                              http_status=resp.status_code)
                return _event_test_payload(event, "OK")
            error_type, retryable = classify_http_status(resp.status_code)
            event = _finish_provider_call(
                None, ctx, ok=False, start=start, http_status=resp.status_code,
                error_type=error_type, retryable=retryable, message=resp.text[:200],
            )
            return _event_test_payload(event, f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            error_type, retryable = classify_exception(e)
            event = _finish_provider_call(
                None, ctx, ok=False, start=start,
                error_type=error_type, retryable=retryable, message=str(e),
            )
            return _event_test_payload(event, f"连接失败: {_redact(e)}")
    elif ep_type == "vertex":
        mdl = model or "gemini-2.5-flash"
        base = endpoint["base_url"].rstrip("/")
        ctx = _provider_context(
            scope="endpoint_test", provider_type="vertex",
            label=endpoint.get("name", "Vertex Gemini"),
            model=mdl, base_url=base, endpoint=endpoint,
        )
        ctx["timeout_sec"] = _resolve_timeout(endpoint, timeout)
        start = time.monotonic()
        url = _vertex_model_url(base, mdl, "generateContent")
        payload = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}
        try:
            async with httpx.AsyncClient(
                timeout=ctx["timeout_sec"], proxy=ctx["proxy_url"] or None
            ) as client:
                resp = await client.post(url, json=payload, headers=_vertex_headers(endpoint.get("api_key", "")))
            if resp.status_code == 200:
                event = _finish_provider_call(None, ctx, ok=True, start=start,
                                              http_status=resp.status_code)
                return _event_test_payload(event, f"OK（模型：{mdl}）")
            error_type, retryable = classify_http_status(resp.status_code)
            event = _finish_provider_call(
                None, ctx, ok=False, start=start, http_status=resp.status_code,
                error_type=error_type, retryable=retryable, message=resp.text[:200],
            )
            return _event_test_payload(event, f"HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            error_type, retryable = classify_exception(e)
            event = _finish_provider_call(
                None, ctx, ok=False, start=start,
                error_type=error_type, retryable=retryable, message=str(e),
            )
            return _event_test_payload(event, f"连接失败: {_redact(e)}")
    return {
        "ok": False,
        "message": f"不支持的端点类型: {ep_type}",
        "status_code": None,
        "error_type": "unsupported_endpoint_type",
        "latency_ms": 0,
        "request_id": new_request_id("cfg"),
        "endpoint_id": endpoint.get("id", ""),
        "endpoint_name": endpoint.get("name", ""),
        "provider_type": ep_type,
        "model": model or "",
        "proxy_enabled": False,
    }
