"""Memory embedding provider boundary.

The default remains the historical SiliconFlow ``BAAI/bge-m3`` path so a
deployment that has no ``memory_embedding`` configuration is byte-for-byte
compatible with the old runtime.  Gemini can be selected explicitly for a
controlled full-corpus migration.

Gemini Embedding 2 is asymmetric for retrieval.  Stored memories are embedded
as documents while live recall text is embedded as a query.  Those two call
sites must not be collapsed back into one ambiguous helper.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import math
import os
import random
import struct
import time
import weakref
from typing import Any, Literal

import httpx

from config import SETTINGS, get_key
from provider_status import (
    classify_exception,
    classify_http_status,
    new_request_id,
    record_provider_event,
)


EmbeddingPurpose = Literal["query", "document"]

SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Compatibility exports.  Runtime code should use load_embedding_config() so a
# settings change becomes effective after restart.
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIMS = 1024

DEFAULT_EMBEDDING_CONFIG: dict[str, Any] = {
    "provider": "siliconflow",
    "model": EMBEDDING_MODEL,
    "dimensions": EMBEDDING_DIMS,
    "batch_size": 8,
    "request_interval_sec": 0.0,
    "timeout_sec": 60.0,
    "max_retries": 3,
    "initial_backoff_sec": 1.0,
    "max_backoff_sec": 30.0,
    "jitter_ratio": 0.2,
    "profile": "retrieval-v1",
}

GEMINI_EMBEDDING_2_DEFAULTS: dict[str, Any] = {
    "model": "gemini-embedding-2",
    "dimensions": 768,
    # Paid Tier 2 is much less restrictive than this.  One serial request per
    # second deliberately leaves headroom for token-per-minute quotas.
    "batch_size": 32,
    "request_interval_sec": 1.0,
    "timeout_sec": 90.0,
    "max_retries": 8,
    "initial_backoff_sec": 2.0,
    "max_backoff_sec": 90.0,
    "jitter_ratio": 0.25,
    "profile": "retrieval-v1",
}


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _proxy_value(provider: str, raw: dict) -> str:
    value = str(raw.get("proxy_url") or "").strip()
    if not value:
        if provider == "gemini":
            value = _env("OBSIDIAN_GEMINI_PROXY") or _env("OBSIDIAN_PROVIDER_PROXY")
        else:
            value = _env("OBSIDIAN_OPENAI_PROXY") or _env("OBSIDIAN_PROVIDER_PROXY")
    if value.lower() in {"", "none", "direct", "off", "false", "0"}:
        return ""
    return value


def load_embedding_config(overrides: dict | None = None) -> dict[str, Any]:
    """Return a validated runtime embedding configuration.

    Environment overrides are primarily for the offline migration tool.  The
    chat service normally reads ``settings.json -> memory_embedding``.
    """
    raw = SETTINGS.get("memory_embedding")
    raw = dict(raw) if isinstance(raw, dict) else {}
    if isinstance(overrides, dict):
        raw.update(overrides)

    provider = (_env("OBSIDIAN_MEMORY_EMBEDDING_PROVIDER") or raw.get("provider") or "siliconflow")
    provider = str(provider).strip().lower()
    if provider not in {"siliconflow", "gemini"}:
        provider = "siliconflow"

    defaults = dict(DEFAULT_EMBEDDING_CONFIG)
    if provider == "gemini":
        defaults.update(GEMINI_EMBEDDING_2_DEFAULTS)

    model = _env("OBSIDIAN_MEMORY_EMBEDDING_MODEL") or raw.get("model") or defaults["model"]
    dimensions_raw = _env("OBSIDIAN_MEMORY_EMBEDDING_DIMENSIONS") or raw.get("dimensions")
    dimensions = _as_int(dimensions_raw, int(defaults["dimensions"]), 128, 3072)
    batch_size = _as_int(raw.get("batch_size"), int(defaults["batch_size"]), 1, 100)
    request_interval = _as_float(
        raw.get("request_interval_sec"),
        float(defaults["request_interval_sec"]),
        0.0,
        30.0,
    )
    timeout = _as_float(raw.get("timeout_sec"), float(defaults["timeout_sec"]), 5.0, 300.0)
    max_retries = _as_int(raw.get("max_retries"), int(defaults["max_retries"]), 0, 12)
    initial_backoff = _as_float(
        raw.get("initial_backoff_sec"),
        float(defaults["initial_backoff_sec"]),
        0.25,
        60.0,
    )
    max_backoff = _as_float(
        raw.get("max_backoff_sec"),
        float(defaults["max_backoff_sec"]),
        initial_backoff,
        300.0,
    )
    jitter_ratio = _as_float(
        raw.get("jitter_ratio"),
        float(defaults["jitter_ratio"]),
        0.0,
        1.0,
    )
    profile = str(raw.get("profile") or defaults["profile"]).strip() or "retrieval-v1"

    base_url = str(raw.get("base_url") or (
        GEMINI_BASE if provider == "gemini" else SILICONFLOW_BASE
    )).rstrip("/")
    proxy_url = _proxy_value(provider, raw)
    signature = f"{provider}:{model}:{dimensions}:{profile}"
    return {
        "provider": provider,
        "model": str(model).strip(),
        "dimensions": dimensions,
        "batch_size": batch_size,
        "request_interval_sec": request_interval,
        "timeout_sec": timeout,
        "max_retries": max_retries,
        "initial_backoff_sec": initial_backoff,
        "max_backoff_sec": max_backoff,
        "jitter_ratio": jitter_ratio,
        "profile": profile,
        "base_url": base_url,
        "proxy_url": proxy_url,
        "signature": signature,
    }


def embedding_signature(config: dict | None = None) -> str:
    return str((config or load_embedding_config())["signature"])


def pack_embedding(values: list[float]) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


def unpack_embedding(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def _normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in values))
    if norm <= 0:
        return values
    return [float(value) / norm for value in values]


def _prepared_text(text: str, *, purpose: EmbeddingPurpose, config: dict) -> str:
    value = str(text or "").strip()
    if config["provider"] != "gemini":
        return value
    if config["model"] == "gemini-embedding-2":
        if purpose == "query":
            return f"task: search result | query: {value}"
        return f"title: none | text: {value}"
    return value


@dataclass
class EmbeddingBatchResult:
    vectors: list[list[float] | None]
    prompt_tokens: int = 0
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0


@dataclass
class _LoopRateState:
    lock: asyncio.Lock
    next_request_at: float = 0.0


_RATE_STATES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopRateState]" = (
    weakref.WeakKeyDictionary()
)


def _rate_state() -> _LoopRateState:
    loop = asyncio.get_running_loop()
    state = _RATE_STATES.get(loop)
    if state is None:
        state = _LoopRateState(lock=asyncio.Lock())
        _RATE_STATES[loop] = state
    return state


async def _wait_for_rate_slot(config: dict) -> None:
    interval = float(config.get("request_interval_sec") or 0.0)
    if interval <= 0:
        return
    state = _rate_state()
    async with state.lock:
        now = time.monotonic()
        delay = max(state.next_request_at - now, 0.0)
        if delay > 0:
            await asyncio.sleep(delay)
        state.next_request_at = time.monotonic() + interval


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = str(response.headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            return max(parsed.timestamp() - time.time(), 0.0)
        except (TypeError, ValueError, OverflowError):
            return None


def _backoff_seconds(attempt: int, config: dict, response: httpx.Response | None) -> float:
    retry_after = _retry_after_seconds(response)
    if retry_after is not None:
        base = retry_after
    else:
        base = min(
            float(config["max_backoff_sec"]),
            float(config["initial_backoff_sec"]) * (2 ** max(attempt, 0)),
        )
    jitter = float(config.get("jitter_ratio") or 0.0)
    if jitter > 0:
        base *= random.uniform(max(0.0, 1.0 - jitter), 1.0 + jitter)
    return min(max(base, 0.0), float(config["max_backoff_sec"]))


def _record_provider(
    *,
    config: dict,
    scope: str,
    request_id: str,
    started_at: float,
    ok: bool,
    path: str,
    http_status: int | None = None,
    error_type: str | None = None,
    retryable: bool = False,
    message: str = "",
    meta: dict | None = None,
) -> None:
    if http_status is not None and not error_type:
        error_type, retryable = classify_http_status(http_status)
    provider = str(config["provider"])
    label = "Gemini memory embedding" if provider == "gemini" else "SiliconFlow memory"
    record_provider_event({
        "request_id": request_id,
        "scope": scope,
        "provider_type": provider,
        "provider_label": label,
        "endpoint_name": label,
        "base_url": f"{config['base_url']}{path}",
        "model": config["model"],
        "ok": ok,
        "http_status": http_status,
        "error_type": error_type or ("ok" if ok else "unknown"),
        "retryable": retryable,
        "elapsed_ms": (time.perf_counter() - started_at) * 1000,
        "message": str(message or "")[:300],
        "meta": meta or {},
    })


def _request_parts(
    texts: list[str],
    *,
    purpose: EmbeddingPurpose,
    config: dict,
) -> tuple[str, dict, dict[str, str]]:
    prepared = [_prepared_text(text, purpose=purpose, config=config) for text in texts]
    if config["provider"] == "gemini":
        key = get_key("gemini")
        model = str(config["model"])
        path = f"/models/{model}:batchEmbedContents"
        requests = []
        for text in prepared:
            item: dict[str, Any] = {
                "model": f"models/{model}",
                "content": {"parts": [{"text": text}]},
                "outputDimensionality": int(config["dimensions"]),
            }
            if model == "gemini-embedding-001":
                item["taskType"] = (
                    "RETRIEVAL_QUERY" if purpose == "query" else "RETRIEVAL_DOCUMENT"
                )
            requests.append(item)
        return path, {"requests": requests}, {"x-goog-api-key": key}

    key = get_key("siliconflow")
    path = "/embeddings"
    body: dict[str, Any] = {"model": config["model"], "input": prepared}
    return path, body, {"Authorization": f"Bearer {key}"}


def _provider_key_available(config: dict) -> bool:
    return bool(get_key("gemini" if config["provider"] == "gemini" else "siliconflow"))


def _parse_vectors(payload: dict, *, count: int, config: dict) -> tuple[list[list[float] | None], int]:
    vectors: list[list[float] | None] = [None for _ in range(count)]
    if config["provider"] == "gemini":
        data = payload.get("embeddings") or []
        for index, item in enumerate(data[:count]):
            values = item.get("values") if isinstance(item, dict) else None
            if isinstance(values, list) and len(values) == int(config["dimensions"]):
                vectors[index] = _normalize(values)
    else:
        for index, item in enumerate(payload.get("data") or []):
            try:
                target = int(item.get("index", index))
                values = item.get("embedding")
            except (AttributeError, TypeError, ValueError):
                continue
            if 0 <= target < count and isinstance(values, list):
                vectors[target] = values
    usage = payload.get("usageMetadata") or payload.get("usage_metadata") or {}
    prompt_tokens = int(usage.get("promptTokenCount") or usage.get("prompt_token_count") or 0)
    return vectors, prompt_tokens


async def _request_batch_once(
    texts: list[str],
    *,
    purpose: EmbeddingPurpose,
    config: dict,
) -> EmbeddingBatchResult:
    if not texts:
        return EmbeddingBatchResult(vectors=[])
    if not _provider_key_available(config):
        return EmbeddingBatchResult(vectors=[None for _ in texts])

    path, body, headers = _request_parts(texts, purpose=purpose, config=config)
    url = f"{config['base_url']}{path}"
    scope = f"memory:embedding_{purpose}_batch"
    last_response: httpx.Response | None = None
    retries = 0
    rate_limited = 0

    for attempt in range(int(config["max_retries"]) + 1):
        await _wait_for_rate_slot(config)
        request_id = new_request_id("mem")
        started_at = time.perf_counter()
        last_response = None
        try:
            async with httpx.AsyncClient(
                timeout=float(config["timeout_sec"]),
                proxy=str(config.get("proxy_url") or "") or None,
            ) as client:
                response = await client.post(url, json=body, headers=headers)
            last_response = response
            if response.status_code == 200:
                payload = response.json()
                vectors, prompt_tokens = _parse_vectors(
                    payload,
                    count=len(texts),
                    config=config,
                )
                success_count = sum(1 for vector in vectors if vector)
                if success_count != len(texts):
                    raise ValueError(
                        f"embedding_count_mismatch:{success_count}/{len(texts)}"
                    )
                _record_provider(
                    config=config,
                    scope=scope,
                    request_id=request_id,
                    started_at=started_at,
                    ok=True,
                    path=path,
                    http_status=200,
                    meta={
                        "batch_size": len(texts),
                        "input_chars": sum(len(text) for text in texts),
                        "dimensions": config["dimensions"],
                        "purpose": purpose,
                        "attempt": attempt + 1,
                        "prompt_tokens": prompt_tokens,
                    },
                )
                return EmbeddingBatchResult(
                    vectors=vectors,
                    prompt_tokens=prompt_tokens,
                    requests=attempt + 1,
                    retries=retries,
                    rate_limited=rate_limited,
                )

            error_type, retryable = classify_http_status(response.status_code)
            if response.status_code == 429:
                retryable = True
                rate_limited += 1
            _record_provider(
                config=config,
                scope=scope,
                request_id=request_id,
                started_at=started_at,
                ok=False,
                path=path,
                http_status=response.status_code,
                error_type=error_type,
                retryable=retryable,
                message=f"HTTP {response.status_code}",
                meta={"batch_size": len(texts), "purpose": purpose, "attempt": attempt + 1},
            )
            if not retryable or attempt >= int(config["max_retries"]):
                break
        except Exception as exc:
            error_type, retryable = classify_exception(exc)
            if isinstance(exc, (json.JSONDecodeError, KeyError, IndexError, ValueError, TypeError)):
                error_type, retryable = "parse_error", False
            _record_provider(
                config=config,
                scope=scope,
                request_id=request_id,
                started_at=started_at,
                ok=False,
                path=path,
                error_type=error_type,
                retryable=retryable,
                message=exc.__class__.__name__,
                meta={"batch_size": len(texts), "purpose": purpose, "attempt": attempt + 1},
            )
            if not retryable or attempt >= int(config["max_retries"]):
                break

        retries += 1
        await asyncio.sleep(_backoff_seconds(attempt, config, last_response))

    return EmbeddingBatchResult(
        vectors=[None for _ in texts],
        requests=retries + 1,
        retries=retries,
        rate_limited=rate_limited,
    )


async def embed_texts_detailed(
    texts: list[str],
    *,
    purpose: EmbeddingPurpose,
    config: dict | None = None,
) -> EmbeddingBatchResult:
    config = dict(config or load_embedding_config())
    clean = [str(text or "") for text in texts]
    if not clean:
        return EmbeddingBatchResult(vectors=[])
    batch_size = max(int(config["batch_size"]), 1)
    combined = EmbeddingBatchResult(vectors=[])
    for start in range(0, len(clean), batch_size):
        result = await _request_batch_once(
            clean[start: start + batch_size],
            purpose=purpose,
            config=config,
        )
        combined.vectors.extend(result.vectors)
        combined.prompt_tokens += result.prompt_tokens
        combined.requests += result.requests
        combined.retries += result.retries
        combined.rate_limited += result.rate_limited
    return combined


async def get_query_embedding(text: str) -> list[float] | None:
    result = await embed_texts_detailed([text], purpose="query")
    return result.vectors[0] if result.vectors else None


async def get_document_embedding(text: str) -> list[float] | None:
    result = await embed_texts_detailed([text], purpose="document")
    return result.vectors[0] if result.vectors else None


async def get_document_embeddings_batch(texts: list[str]) -> list[list[float] | None]:
    result = await embed_texts_detailed(texts, purpose="document")
    return result.vectors


# Compatibility names: recall code historically imported get_embedding(), and
# chunk backfill historically imported get_embeddings_batch().
async def get_embedding(text: str) -> list[float] | None:
    return await get_query_embedding(text)


async def get_embeddings_batch(texts: list[str]) -> list[list[float] | None]:
    return await get_document_embeddings_batch(texts)
