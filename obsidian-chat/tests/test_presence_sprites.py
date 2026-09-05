import asyncio
import base64
import io
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from PIL import Image

from app.presence.db import init_presence_tables
from app.presence import image_provider as image_provider_module
from app.presence.image_provider import (
    PresenceImageGeneration,
    PresenceImageProvider,
    PresenceImageProviderError,
)
from app.presence.schema import inspect_transparent_png
from app.presence.sprites import DrawQuotaExceeded, SpriteLibrary, SpriteLibraryError


def _transparent_png() -> bytes:
    image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    for x in range(8, 24):
        for y in range(6, 26):
            image.putpixel((x, y), (100, 60, 210, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _second_transparent_png() -> bytes:
    image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    for x in range(6, 26):
        for y in range(8, 24):
            image.putpixel((x, y), (40, 180, 120, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _variant_transparent_png(index: int) -> bytes:
    image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    color = ((index * 37) % 255, (index * 71) % 255, (index * 113) % 255, 255)
    for x in range(7, 25):
        for y in range(5, 27):
            image.putpixel((x, y), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


async def _library(tmp_path, *, provider=None):
    db_path = tmp_path / "presence.db"

    @asynccontextmanager
    async def db_factory():
        async with aiosqlite.connect(db_path, timeout=2.0) as db:
            yield db

    async with db_factory() as db:
        await init_presence_tables(db)
        await db.commit()
    return SpriteLibrary(
        get_db_factory=db_factory,
        storage_dir=tmp_path / "sprites",
        provider=provider,
        now=lambda: 1_776_600_000.0,
        timezone_name="UTC",
        daily_limit=2,
    )


async def _add_test_seeds(library, *, count=2):
    rows = []
    for index in range(count):
        rows.append(await library.add_sprite(
            sprite_id=f"test_seed_{index}",
            png=_variant_transparent_png(200 + index),
            form="unknown",
            description=f"test-only fallback seed {index}",
            provider="seed",
        ))
    return rows


def test_daily_draw_quota_is_atomic_and_persistent(tmp_path):
    async def scenario():
        library = await _library(tmp_path)

        async def reserve():
            try:
                return await library.reserve_daily_draw()
            except DrawQuotaExceeded:
                return None

        results = await asyncio.gather(*(reserve() for _ in range(8)))
        assert len([item for item in results if item]) == 2
        assert (await library.quota_snapshot())["used"] == 2

        restarted = await _library(tmp_path)
        assert (await restarted.quota_snapshot())["remaining"] == 0

    asyncio.run(scenario())


def test_quota_is_spent_before_provider_and_failure_does_not_refund(tmp_path):
    class FailingProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, _prompt):
            self.calls += 1
            raise RuntimeError("provider_failed")

    async def scenario():
        provider = FailingProvider()
        library = await _library(tmp_path, provider=provider)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="provider_failed"):
                await library.draw(
                    form="human",
                    prompt="a small violet human spirit",
                    description="wanted a quiet violet shape",
                )
        with pytest.raises(DrawQuotaExceeded):
            await library.draw(
                form="human",
                prompt="must not reach provider",
                description="quota exhausted",
            )
        assert provider.calls == 2

    asyncio.run(scenario())


def test_local_provider_preflight_fails_before_quota_or_paid_generation(tmp_path):
    class MissingLocalDependencyProvider:
        def __init__(self):
            self.generate_calls = 0

        async def preflight(self):
            raise ImportError("rembg missing")

        async def generate(self, _prompt):
            self.generate_calls += 1
            return _transparent_png()

    async def scenario():
        provider = MissingLocalDependencyProvider()
        library = await _library(tmp_path, provider=provider)
        with pytest.raises(ImportError, match="rembg missing"):
            await library.draw(
                form="human",
                prompt="must fail locally",
                description="preflight failure",
            )
        assert provider.generate_calls == 0
        assert (await library.quota_snapshot())["used"] == 0

    asyncio.run(scenario())


def test_unconfigured_image_provider_fails_before_quota_and_rembg(tmp_path, monkeypatch):
    rembg_calls = []

    monkeypatch.setattr(
        image_provider_module,
        "get_slot",
        lambda _slot_name: None,
    )

    async def fake_rembg_session():
        rembg_calls.append(1)
        return object()

    monkeypatch.setattr(image_provider_module, "_rembg_session", fake_rembg_session)

    async def scenario():
        library = await _library(
            tmp_path,
            provider=PresenceImageProvider(),
        )
        with pytest.raises(
            PresenceImageProviderError,
            match="presence_image_provider_unconfigured",
        ):
            await library.draw(
                form="human",
                prompt="must fail before quota",
                description="provider is not configured",
            )
        assert rembg_calls == []
        assert (await library.quota_snapshot())["used"] == 0

    asyncio.run(scenario())


def test_blank_image_model_fails_in_preflight(monkeypatch):
    monkeypatch.setattr(
        image_provider_module,
        "get_slot",
        lambda _slot_name: {
            "endpoint": {
                "api_key": "test",
                "base_url": "https://example.invalid",
                "type": "gemini",
            },
            "model": "   ",
            "extras": {},
        },
    )

    async def scenario():
        with pytest.raises(
            PresenceImageProviderError,
            match="presence_image_model_unconfigured",
        ):
            await PresenceImageProvider().preflight()

    asyncio.run(scenario())


def test_unsupported_image_endpoint_fails_before_rembg(monkeypatch):
    rembg_calls = []
    monkeypatch.setattr(
        image_provider_module,
        "get_slot",
        lambda _slot_name: {
            "endpoint": {
                "api_key": "test",
                "base_url": "https://aiplatform.googleapis.com/v1/project",
                "type": "vertex",
            },
            "model": "gemini-3.1-flash-image",
            "extras": {},
        },
    )

    async def fake_rembg_session():
        rembg_calls.append(1)
        return object()

    monkeypatch.setattr(image_provider_module, "_rembg_session", fake_rembg_session)

    async def scenario():
        with pytest.raises(
            PresenceImageProviderError,
            match="presence_image_provider_type_unsupported:vertex",
        ):
            await PresenceImageProvider().preflight()

    asyncio.run(scenario())
    assert rembg_calls == []


def test_gemini_image_provider_uses_native_inline_image_api(monkeypatch):
    cutout = _transparent_png()
    seen = {}

    monkeypatch.setattr(
        image_provider_module,
        "get_slot",
        lambda _slot_name: {
            "endpoint": {
                "id": "gem",
                "api_key": "test-secret",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "type": "gemini",
                "timeout_sec": 75,
            },
            "model": "gemini-3.1-flash-image",
            "extras": {},
        },
    )

    async def fake_rembg_session():
        return object()

    async def fake_remove_background(raw):
        seen["raw"] = raw
        return cutout

    monkeypatch.setattr(image_provider_module, "_rembg_session", fake_rembg_session)
    monkeypatch.setattr(
        image_provider_module, "_remove_background", fake_remove_background
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(
                                            b"generated-image"
                                        ).decode("ascii"),
                                    }
                                }
                            ]
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            seen["url"] = url
            seen["headers"] = headers
            seen["payload"] = json
            return FakeResponse()

        async def get(self, _url):
            raise AssertionError("Gemini inline image must not be downloaded")

    monkeypatch.setattr(image_provider_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        result = await PresenceImageProvider().generate("a tiny violet fog cat")
        assert result == cutout

    asyncio.run(scenario())
    assert seen["raw"] == b"generated-image"
    assert seen["client_kwargs"]["timeout"] == 75.0
    assert seen["url"].endswith(
        "/models/gemini-3.1-flash-image:generateContent"
    )
    assert "test-secret" not in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "test-secret"
    generation = seen["payload"]["generationConfig"]
    assert generation == {"responseModalities": ["IMAGE"]}
    assert "tiny violet fog cat" in seen["payload"]["contents"][0]["parts"][0]["text"]


def test_gemini_human_reference_uses_inline_data_png(monkeypatch):
    reference = _transparent_png()
    cutout = _second_transparent_png()
    seen = {}
    monkeypatch.setattr(
        image_provider_module,
        "get_slot",
        lambda _slot_name: {
            "endpoint": {
                "api_key": "key",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "type": "gemini",
            },
            "model": "gemini-image",
            "extras": {},
        },
    )

    async def fake_rembg_session():
        return object()

    async def fake_remove_background(_raw):
        return cutout

    monkeypatch.setattr(image_provider_module, "_rembg_session", fake_rembg_session)
    monkeypatch.setattr(image_provider_module, "_remove_background", fake_remove_background)

    class Response:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"inlineData": {
                "mimeType": "image/png",
                "data": base64.b64encode(b"generated").decode("ascii"),
            }}]}}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, headers, json):
            seen["payload"] = json
            return Response()

    monkeypatch.setattr(image_provider_module.httpx, "AsyncClient", Client)

    async def scenario():
        generated = await PresenceImageProvider().generate_with_metadata(
            "new coat and pose",
            reference_png=reference,
            reference_prompt="black hair and violet eyes",
        )
        assert generated.png == cutout
        assert generated.degraded is False

    asyncio.run(scenario())
    parts = seen["payload"]["contents"][0]["parts"]
    assert parts[1] == {
        "inlineData": {
            "mimeType": "image/png",
            "data": base64.b64encode(reference).decode("ascii"),
        }
    }
    assert "black hair and violet eyes" in parts[0]["text"]


def test_openai_compatible_image_provider_path_is_preserved(monkeypatch):
    cutout = _transparent_png()
    seen = {}

    monkeypatch.setattr(
        image_provider_module,
        "get_slot",
        lambda _slot_name: {
            "endpoint": {
                "api_key": "sf-key",
                "base_url": "https://api.siliconflow.cn/v1",
                "type": "openai",
            },
            "model": "Tongyi-MAI/Z-Image-Turbo",
            "extras": {},
        },
    )

    async def fake_rembg_session():
        return object()

    async def fake_remove_background(raw):
        seen["raw"] = raw
        return cutout

    monkeypatch.setattr(image_provider_module, "_rembg_session", fake_rembg_session)
    monkeypatch.setattr(
        image_provider_module, "_remove_background", fake_remove_background
    )

    class FakeResponse:
        status_code = 200

        def __init__(self, *, body=None, content=b""):
            self._body = body
            self.content = content

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, *, headers, json):
            seen["url"] = url
            seen["headers"] = headers
            seen["payload"] = json
            return FakeResponse(body={"images": [{"url": "https://cdn.invalid/a.png"}]})

        async def get(self, url):
            seen["download_url"] = url
            return FakeResponse(content=b"legacy-image")

    monkeypatch.setattr(image_provider_module.httpx, "AsyncClient", FakeClient)

    async def scenario():
        result = await PresenceImageProvider().generate("a moon rabbit")
        assert result == cutout

    asyncio.run(scenario())
    assert seen["raw"] == b"legacy-image"
    assert seen["url"] == "https://api.siliconflow.cn/v1/images/generations"
    assert seen["download_url"] == "https://cdn.invalid/a.png"
    assert seen["payload"]["model"] == "Tongyi-MAI/Z-Image-Turbo"
    assert seen["payload"]["image_size"] == "1024x1024"


def test_openai_reference_degrades_to_text_and_reports_it(monkeypatch):
    cutout = _transparent_png()
    seen = {}
    monkeypatch.setattr(
        image_provider_module,
        "get_slot",
        lambda _slot_name: {
            "endpoint": {
                "api_key": "key",
                "base_url": "https://images.invalid/v1",
                "type": "openai",
            },
            "model": "image-model",
            "extras": {},
        },
    )

    async def fake_rembg_session():
        return object()

    async def fake_remove_background(_raw):
        return cutout

    monkeypatch.setattr(image_provider_module, "_rembg_session", fake_rembg_session)
    monkeypatch.setattr(image_provider_module, "_remove_background", fake_remove_background)

    class Response:
        status_code = 200
        content = b"raw"

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(b"raw").decode("ascii")}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, headers, json):
            seen["payload"] = json
            return Response()

    monkeypatch.setattr(image_provider_module.httpx, "AsyncClient", Client)

    async def scenario():
        result = await PresenceImageProvider().generate_with_metadata(
            "new pose",
            reference_png=_second_transparent_png(),
            reference_prompt="same black-haired person",
        )
        assert result.degraded is True
        assert result.degradation_reason == "reference_image_unsupported:openai"

    asyncio.run(scenario())
    assert "same black-haired person" in seen["payload"]["prompt"]


def test_retryable_image_failures_retry_in_same_call(monkeypatch):
    cutout = _transparent_png()
    calls = []
    sleeps = []
    monkeypatch.setattr(
        image_provider_module,
        "get_slot",
        lambda _slot_name: {
            "endpoint": {
                "api_key": "key",
                "base_url": "https://images.invalid/v1",
                "type": "openai",
            },
            "model": "image-model",
            "extras": {},
        },
    )
    async def fake_rembg_session():
        return object()

    monkeypatch.setattr(image_provider_module, "_rembg_session", fake_rembg_session)

    async def fake_remove_background(_raw):
        return cutout

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(image_provider_module, "_remove_background", fake_remove_background)
    monkeypatch.setattr(image_provider_module.asyncio, "sleep", fake_sleep)

    class Response:
        content = b"raw"

        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(b"raw").decode("ascii")}]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, *, headers, json):
            calls.append(1)
            return Response(503 if len(calls) < 3 else 200)

    monkeypatch.setattr(image_provider_module.httpx, "AsyncClient", Client)

    async def scenario():
        assert await PresenceImageProvider().generate("retry me") == cutout

    asyncio.run(scenario())
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize("failure_type", ["connect", "timeout"])
def test_connect_and_timeout_retry_in_same_call(monkeypatch, failure_type):
    provider = PresenceImageProvider()
    calls = []
    sleeps = []

    async def fake_generate(*_args, **_kwargs):
        calls.append(1)
        if len(calls) < 3:
            request = image_provider_module.httpx.Request(
                "POST",
                "https://images.invalid/v1/images/generations",
            )
            if failure_type == "connect":
                raise image_provider_module.httpx.ConnectError(
                    "connect failed",
                    request=request,
                )
            raise image_provider_module.httpx.ReadTimeout(
                "timed out",
                request=request,
            )
        return b"raw"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(provider, "_generate_openai_compatible", fake_generate)
    monkeypatch.setattr(image_provider_module.asyncio, "sleep", fake_sleep)

    async def scenario():
        raw = await provider._generate_with_retries(
            object(),
            endpoint={},
            endpoint_type="openai",
            model="model",
            prompt="prompt",
            reference_png=None,
            reference_prompt="",
        )
        assert raw == b"raw"

    asyncio.run(scenario())
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]


def test_non_retryable_4xx_is_not_retried(monkeypatch):
    provider = PresenceImageProvider()
    calls = []

    async def fake_generate(*_args, **_kwargs):
        calls.append(1)
        raise PresenceImageProviderError("image_generation_http_400")

    monkeypatch.setattr(provider, "_generate_openai_compatible", fake_generate)

    async def scenario():
        with pytest.raises(
            PresenceImageProviderError,
            match="image_generation_http_400",
        ):
            await provider._generate_with_retries(
                object(),
                endpoint={},
                endpoint_type="openai",
                model="model",
                prompt="prompt",
                reference_png=None,
                reference_prompt="",
            )

    asyncio.run(scenario())
    assert calls == [1]


def test_sprite_storage_hashes_png_and_starts_unsynced(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        result = await library.add_sprite(
            sprite_id="seed_fog",
            png=_transparent_png(),
            description="soft violet fog",
            base_height_dip=240,
        )
        assert result["sprite_hash"].startswith("sha256:")
        assert result["available_on_device"] is False
        assert (library.storage_dir / f"{result['sprite_hash'][7:]}.png").is_file()

    asyncio.run(scenario())


def test_sprite_id_collision_raises_without_replacing_or_storing_new_asset(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        original = await library.add_sprite(
            sprite_id="seed_collision",
            png=_transparent_png(),
            description="original sprite",
        )
        replacement_hash = inspect_transparent_png(_second_transparent_png()).sha256

        with pytest.raises(
            SpriteLibraryError, match="presence_sprite_id_conflict"
        ):
            await library.add_sprite(
                sprite_id="seed_collision",
                png=_second_transparent_png(),
                description="must not replace original",
            )

        stored = await library.get_sprite("seed_collision")
        assert stored["sprite_hash"] == original["sprite_hash"]
        assert not (
            library.storage_dir / f"{replacement_hash.removeprefix('sha256:')}.png"
        ).exists()

    asyncio.run(scenario())


def test_failed_sprite_transaction_removes_new_asset_file(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        png = _second_transparent_png()
        sprite_hash = inspect_transparent_png(png).sha256

        async def fail_after_file_write(_db):
            raise RuntimeError("forced_database_failure")

        library._archive_overflow_in_tx = fail_after_file_write
        with pytest.raises(RuntimeError, match="forced_database_failure"):
            await library.add_sprite(
                sprite_id="failed_asset",
                png=png,
                description="must roll back",
            )

        assert await library.get_sprite("failed_asset") is None
        assert not (
            library.storage_dir / f"{sprite_hash.removeprefix('sha256:')}.png"
        ).exists()

    asyncio.run(scenario())


def test_sprite_manifest_file_integrity_and_sync_state(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        result = await library.add_sprite(
            sprite_id="seed_sync",
            png=_transparent_png(),
            description="sync test",
            base_height_dip=220,
        )
        data, row = await library.file_for_hash(result["sprite_hash"])
        assert data == _transparent_png()
        assert row["sprite_id"] == "seed_sync"
        assert (await library.manifest())[0]["sync_status"] == "pending"
        await library.mark_synced(result["sprite_hash"])
        assert (await library.manifest())[0]["available_on_device"] is True
        await library.mark_missing(result["sprite_hash"])
        assert (await library.manifest())[0]["available_on_device"] is False

    asyncio.run(scenario())


def test_production_seed_manifest_is_intentionally_empty(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        assert await library.ensure_seed_sprites() == 0
        assert await library.ensure_seed_sprites() == 0
        async with library._get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT sprite_hash) FROM presence_sprites"
            )
            count, distinct_hashes = await cursor.fetchone()
        assert count == distinct_hashes == 0

    asyncio.run(scenario())


def test_first_non_seed_sprite_must_be_human_before_quota_or_provider(tmp_path):
    class Provider:
        def __init__(self):
            self.calls = 0

        async def generate(self, _prompt):
            self.calls += 1
            return _transparent_png()

    async def scenario():
        provider = Provider()
        library = await _library(tmp_path, provider=provider)
        with pytest.raises(
            SpriteLibraryError,
            match="presence_first_sprite_must_be_human",
        ):
            await library.draw(
                form="nonhuman",
                prompt="a violet cat",
                description="for sleepy evenings",
            )
        assert provider.calls == 0
        assert (await library.quota_snapshot())["used"] == 0

    asyncio.run(scenario())


def test_synced_non_seed_state_changes_only_after_pc_ack(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        custom = await library.add_sprite(
            sprite_id="first_formal_sprite",
            png=_variant_transparent_png(77),
            form="human",
            prompt="front-facing human baseline",
            description="the first formal shape",
            provider="gemini",
        )
        assert await library.has_non_seed_sprites() is True
        assert await library.has_synced_non_seed_sprites() is False
        await library.mark_synced(custom["sprite_hash"])
        assert await library.has_synced_non_seed_sprites() is True

    asyncio.run(scenario())


def test_second_human_draw_receives_earliest_human_png_reference(tmp_path):
    class Provider:
        def __init__(self):
            self.seen = None

        async def generate_with_metadata(
            self,
            prompt,
            *,
            reference_png=None,
            reference_prompt="",
        ):
            self.seen = (prompt, reference_png, reference_prompt)
            return PresenceImageGeneration(
                png=_second_transparent_png(),
                provider_type="gemini",
            )

    async def scenario():
        provider = Provider()
        library = await _library(tmp_path, provider=provider)
        await library.add_sprite(
            sprite_id="human_baseline",
            png=_transparent_png(),
            form="human",
            prompt="black hair and violet eyes",
            description="this felt like me",
            provider="manual",
            created_at=100,
        )
        result = await library.draw(
            form="human",
            prompt="same person in a long coat",
            description="wanted to look more composed",
        )
        assert provider.seen == (
            "same person in a long coat",
            _transparent_png(),
            "black hair and violet eyes",
        )
        assert result["form"] == "human"
        assert result["provider_degraded"] is False

    asyncio.run(scenario())


def test_seed_archives_only_after_non_seed_sync_and_restores_on_missing(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        seeds = await _add_test_seeds(library)
        await library.mark_synced(seeds[0]["sprite_hash"])
        custom = await library.add_sprite(
            sprite_id="first_self_human",
            png=_variant_transparent_png(99),
            form="human",
            prompt="black hair, violet coat, front view",
            description="the first human shape I chose",
            provider="gemini",
        )
        async with library._get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM presence_sprites "
                "WHERE provider='seed' AND archived=1"
            )
            assert int((await cursor.fetchone())[0]) == 0

        await library.mark_synced(custom["sprite_hash"])
        async with library._get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM presence_sprites "
                "WHERE provider='seed' AND archived=1"
            )
            assert int((await cursor.fetchone())[0]) == len(seeds)

        await library.mark_missing(custom["sprite_hash"])
        restored = await library.manifest()
        assert any(row["provider"] == "seed" for row in restored)
        assert await library.has_available_sprites() is True

        await library.mark_synced(custom["sprite_hash"])
        assert all(row["provider"] != "seed" for row in await library.manifest())

    asyncio.run(scenario())


def test_pending_seed_fallback_reenters_manifest_but_readiness_stays_false(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        seeds = await _add_test_seeds(library)
        custom = await library.add_sprite(
            sprite_id="only_non_seed",
            png=_variant_transparent_png(101),
            form="human",
            prompt="human baseline",
            description="baseline",
            provider="gemini",
        )
        await library.mark_synced(custom["sprite_hash"])
        await library.mark_missing(custom["sprite_hash"])
        manifest = await library.manifest()
        assert len([row for row in manifest if row["provider"] == "seed"]) == len(seeds)
        assert not any(row["available_on_device"] for row in manifest)
        assert await library.has_available_sprites() is False

    asyncio.run(scenario())


def test_overflow_never_archives_earliest_human_baseline(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        for index in range(31):
            await library.add_sprite(
                sprite_id=f"custom_{index:02d}",
                png=_variant_transparent_png(index + 1),
                form="human" if index == 0 else "nonhuman",
                prompt=f"visual {index}",
                description=f"description {index}",
                provider="gemini",
                created_at=float(index + 1),
            )
        baseline = await library.get_sprite("custom_00")
        assert baseline["active"] == 1
        assert baseline["archived"] == 0
        async with library._get_db() as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM presence_sprites "
                "WHERE active=1 AND archived=0"
            )
            assert int((await cursor.fetchone())[0]) == 28

    asyncio.run(scenario())
