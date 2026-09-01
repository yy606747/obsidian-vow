"""Image-generation provider pipeline for transparent Presence sprites."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image

from config import DATA_DIR, get_slot

from .schema import inspect_transparent_png


DEFAULT_OPENAI_IMAGE_SIZE = "1024x1024"
DEFAULT_TIMEOUT_SEC = 180.0
MAX_REQUEST_ATTEMPTS = 3
SUPPORTED_ENDPOINT_TYPES = frozenset({"gemini", "openai"})
_NEGATIVE_PROMPT = (
    "background, room, landscape, scenery, solid backdrop, checkerboard, frame, "
    "floor, cast shadow, readable text, logo, watermark"
)
_SESSION: Any | None = None
_SESSION_LOCK = asyncio.Lock()


class PresenceImageProviderError(RuntimeError):
    pass


class _RetryableImageProviderError(PresenceImageProviderError):
    pass


@dataclass(frozen=True)
class PresenceImageGeneration:
    png: bytes
    provider_type: str
    degraded: bool = False
    degradation_reason: str = ""


def provider_configured() -> bool:
    try:
        _request_config()
    except PresenceImageProviderError:
        return False
    return True


def _request_config() -> tuple[dict[str, Any], str, float]:
    slot = get_slot("presence_image")
    if not slot:
        raise PresenceImageProviderError("presence_image_provider_unconfigured")
    endpoint = slot.get("endpoint") or {}
    if not str(endpoint.get("base_url") or "").strip() or not str(
        endpoint.get("api_key") or ""
    ).strip():
        raise PresenceImageProviderError("presence_image_provider_unconfigured")
    model = str(slot.get("model") or "").strip()
    if not model:
        raise PresenceImageProviderError("presence_image_model_unconfigured")
    endpoint_type = str(endpoint.get("type") or "openai").strip().lower()
    if endpoint_type not in SUPPORTED_ENDPOINT_TYPES:
        raise PresenceImageProviderError(
            f"presence_image_provider_type_unsupported:{endpoint_type}"
        )
    extras = slot.get("extras") or {}
    raw_timeout = extras.get("timeout_sec")
    if raw_timeout in (None, ""):
        raw_timeout = endpoint.get("timeout_sec")
    if raw_timeout in (None, ""):
        raw_timeout = DEFAULT_TIMEOUT_SEC
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise PresenceImageProviderError("presence_image_timeout_invalid") from exc
    if not math.isfinite(timeout):
        raise PresenceImageProviderError("presence_image_timeout_invalid")
    return endpoint, model, max(30.0, min(timeout, 300.0))


def _decode_image_base64(value: Any, error_code: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise PresenceImageProviderError(error_code)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PresenceImageProviderError(error_code) from exc
    if not decoded:
        raise PresenceImageProviderError(error_code)
    return decoded


def _extract_gemini_image(body: Any) -> bytes:
    if not isinstance(body, dict):
        raise PresenceImageProviderError("image_generation_missing_inline_data")
    candidates = body.get("candidates")
    if not isinstance(candidates, list):
        raise PresenceImageProviderError("image_generation_missing_inline_data")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if not isinstance(inline, dict):
                continue
            mime_type = str(
                inline.get("mimeType") or inline.get("mime_type") or ""
            ).lower()
            if not mime_type.startswith("image/"):
                raise PresenceImageProviderError(
                    "image_generation_invalid_inline_mime"
                )
            return _decode_image_base64(
                inline.get("data"), "image_generation_invalid_inline_data"
            )
    raise PresenceImageProviderError("image_generation_missing_inline_data")


async def _rembg_session():
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    async with _SESSION_LOCK:
        if _SESSION is not None:
            return _SESSION

        def build():
            import os

            rembg_home = DATA_DIR / "presence" / "rembg"
            rembg_home.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("REMBG_HOME", str(rembg_home))
            from rembg import new_session

            return new_session("u2netp")

        _SESSION = await asyncio.to_thread(build)
        return _SESSION


async def _remove_background(data: bytes) -> bytes:
    session = await _rembg_session()

    def run() -> bytes:
        from rembg import remove

        result = remove(data, session=session)
        with Image.open(io.BytesIO(result)) as image:
            rgba = image.convert("RGBA")
            alpha = rgba.getchannel("A")
            bbox = alpha.getbbox()
            if bbox is None:
                raise PresenceImageProviderError("background_removal_empty")
            left, top, right, bottom = bbox
            pad = max(8, round(max(right - left, bottom - top) * 0.035))
            crop = (
                max(0, left - pad),
                max(0, top - pad),
                min(rgba.width, right + pad),
                min(rgba.height, bottom + pad),
            )
            rgba = rgba.crop(crop)
            output = io.BytesIO()
            rgba.save(output, format="PNG", optimize=True)
            return output.getvalue()

    return await asyncio.to_thread(run)


class PresenceImageProvider:
    """Configurable endpoint plus local u2netp extraction and alpha validation."""

    async def preflight(self) -> None:
        self._request_config()
        # Load the local post-processor (and its model) before a paid image
        # request or quota reservation. Missing configuration and runtime
        # dependencies therefore fail without consuming the daily allowance.
        await _rembg_session()

    async def generate(
        self,
        prompt: str,
        reference_png: bytes | None = None,
        reference_prompt: str = "",
    ) -> bytes:
        return (
            await self.generate_with_metadata(
                prompt,
                reference_png=reference_png,
                reference_prompt=reference_prompt,
            )
        ).png

    async def generate_with_metadata(
        self,
        prompt: str,
        *,
        reference_png: bytes | None = None,
        reference_prompt: str = "",
    ) -> PresenceImageGeneration:
        await self.preflight()
        endpoint, model, timeout = self._request_config()
        endpoint_type = str(endpoint.get("type") or "openai").lower()
        reference = bytes(reference_png) if reference_png is not None else None
        if reference is not None:
            try:
                inspect_transparent_png(reference)
            except Exception as exc:
                raise PresenceImageProviderError(
                    "presence_reference_png_invalid"
                ) from exc
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                proxy=endpoint.get("proxy_url") or None,
                trust_env=False,
                follow_redirects=True,
            ) as client:
                raw = await self._generate_with_retries(
                    client,
                    endpoint=endpoint,
                    endpoint_type=endpoint_type,
                    model=model,
                    prompt=prompt,
                    reference_png=reference,
                    reference_prompt=reference_prompt,
                )
        except PresenceImageProviderError:
            raise
        except Exception as exc:
            raise PresenceImageProviderError(
                f"image_provider_{type(exc).__name__}"
            ) from exc
        cutout = await _remove_background(raw)
        inspect_transparent_png(cutout)
        degraded = bool(reference is not None and endpoint_type != "gemini")
        return PresenceImageGeneration(
            png=cutout,
            provider_type=endpoint_type,
            degraded=degraded,
            degradation_reason=(
                "reference_image_unsupported:openai"
                if degraded
                else ""
            ),
        )

    async def _generate_with_retries(
        self,
        client: httpx.AsyncClient,
        *,
        endpoint: dict[str, Any],
        endpoint_type: str,
        model: str,
        prompt: str,
        reference_png: bytes | None,
        reference_prompt: str,
    ) -> bytes:
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            try:
                if endpoint_type == "gemini":
                    return await self._generate_gemini(
                        client,
                        endpoint,
                        model,
                        prompt,
                        reference_png=reference_png,
                        reference_prompt=reference_prompt,
                    )
                return await self._generate_openai_compatible(
                    client,
                    endpoint,
                    model,
                    prompt,
                    reference_prompt=(
                        reference_prompt if reference_png is not None else ""
                    ),
                )
            except (
                httpx.ConnectError,
                httpx.TimeoutException,
                _RetryableImageProviderError,
            ) as exc:
                if attempt + 1 >= MAX_REQUEST_ATTEMPTS:
                    if isinstance(exc, _RetryableImageProviderError):
                        raise PresenceImageProviderError(str(exc)) from exc
                    raise PresenceImageProviderError(
                        f"image_provider_{type(exc).__name__}"
                    ) from exc
                await asyncio.sleep(float(attempt + 1))
        raise AssertionError("unreachable")

    @staticmethod
    def _request_config() -> tuple[dict[str, Any], str, float]:
        return _request_config()

    @classmethod
    async def _generate_gemini(
        cls,
        client: httpx.AsyncClient,
        endpoint: dict[str, Any],
        model: str,
        prompt: str,
        *,
        reference_png: bytes | None = None,
        reference_prompt: str = "",
    ) -> bytes:
        base_url = str(endpoint.get("base_url") or "").rstrip("/")
        model_id = model.removeprefix("models/")
        url = (
            f"{base_url}/models/{quote(model_id, safe='-._~')}:generateContent"
        )
        parts: list[dict[str, Any]] = [
            {"text": cls._render_prompt(prompt, reference_prompt=reference_prompt)}
        ]
        if reference_png is not None:
            parts.append({
                "inlineData": {
                    "mimeType": "image/png",
                    "data": base64.b64encode(reference_png).decode("ascii"),
                }
            })
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
            },
        }
        response = await client.post(
            url,
            headers={
                "x-goog-api-key": str(endpoint.get("api_key") or ""),
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code >= 500:
            raise _RetryableImageProviderError(
                f"image_generation_http_{response.status_code}"
            )
        if response.status_code != 200:
            raise PresenceImageProviderError(
                f"image_generation_http_{response.status_code}"
            )
        return _extract_gemini_image(response.json())

    @classmethod
    async def _generate_openai_compatible(
        cls,
        client: httpx.AsyncClient,
        endpoint: dict[str, Any],
        model: str,
        prompt: str,
        *,
        reference_prompt: str = "",
    ) -> bytes:
        base_url = str(endpoint.get("base_url") or "").rstrip("/")
        payload = {
            "model": model,
            "prompt": cls._render_prompt(
                prompt,
                reference_prompt=reference_prompt,
            ),
            "negative_prompt": _NEGATIVE_PROMPT,
            "image_size": DEFAULT_OPENAI_IMAGE_SIZE,
            "num_inference_steps": 8,
        }
        response = await client.post(
            f"{base_url}/images/generations",
            headers={
                "Authorization": f"Bearer {endpoint.get('api_key', '')}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code >= 500:
            raise _RetryableImageProviderError(
                f"image_generation_http_{response.status_code}"
            )
        if response.status_code != 200:
            raise PresenceImageProviderError(
                f"image_generation_http_{response.status_code}"
            )
        body = response.json()
        images = None
        if isinstance(body, dict):
            images = body.get("images") or body.get("data")
        first = images[0] if isinstance(images, list) and images else {}
        if not isinstance(first, dict):
            first = {}
        encoded = first.get("b64_json") or first.get("b64")
        if encoded:
            return _decode_image_base64(
                encoded, "image_generation_invalid_base64"
            )
        image_url = str(first.get("url") or "")
        if not image_url:
            raise PresenceImageProviderError("image_generation_missing_url")
        download = await client.get(image_url)
        if download.status_code >= 500:
            raise _RetryableImageProviderError(
                f"image_download_http_{download.status_code}"
            )
        if download.status_code != 200:
            raise PresenceImageProviderError(
                f"image_download_http_{download.status_code}"
            )
        return bytes(download.content)

    @staticmethod
    def _render_prompt(prompt: str, *, reference_prompt: str = "") -> str:
        subject = " ".join(str(prompt or "").split())[:800]
        baseline = " ".join(str(reference_prompt or "").split())[:800]
        if baseline:
            subject = (
                "Keep the same person and recognizable identity as the supplied "
                f"baseline ({baseline}); apply this new variation: {subject}"
            )
        return (
            f"{subject}. A single desktop spirit sprite, full subject visible, centered, "
            "clean silhouette, generous empty margins, isolated on a perfectly uniform "
            "pure white studio background for later cutout, no floor and no cast shadow. "
            "No scenery, frame, readable text, logo, or watermark."
        )


# Compatibility import for the first Presence implementation.  Existing code
# may still name the old provider, but endpoint dispatch is now slot-driven.
SiliconFlowPresenceImageProvider = PresenceImageProvider


__all__ = [
    "MAX_REQUEST_ATTEMPTS",
    "PresenceImageGeneration",
    "PresenceImageProvider",
    "PresenceImageProviderError",
    "SiliconFlowPresenceImageProvider",
    "provider_configured",
]
