import asyncio
import io
import json
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from PIL import Image

from app.presence.db import init_presence_tables
from app.presence.renderer import (
    PresenceRenderer,
    build_renderer_prompt,
    duration_policy_reason,
    normalize_round_kind,
    parse_renderer_output,
    presence_renderer_configured,
)
from app.presence.schema import PRESENCE_RENDERER_RESPONSE_SCHEMA
from app.presence.service import PresenceDeliveryService
from app.presence.sprites import SpriteLibrary
from app.tools.schemas import ToolContext


class FakeLedger:
    def __init__(self):
        self.calls = []
        self._invocation_count = 0

    def new_invocation_id(self, prefix):
        self._invocation_count += 1
        return f"{prefix}_test_{self._invocation_count}"

    async def record_model_request(self, *args, **kwargs):
        self.calls.append(("request", args, kwargs))
        return 1

    async def record_model_output(self, *args, **kwargs):
        self.calls.append(("output", args, kwargs))
        return 1

    async def record_renderer_frame(self, *args, **kwargs):
        self.calls.append(("frame", args, kwargs))
        return 1

    async def record_turn(self, *args, **kwargs):
        self.calls.append(("turn", args, kwargs))
        return 1

    async def record_terminal_outcome(self, **kwargs):
        self.calls.append(("terminal", (), kwargs))
        return 1


class UnitDelivery:
    def __init__(self):
        self.enqueued = []
        self.rejected = []

    async def agent_online(self, **_kwargs):
        return True

    async def reserve_intent(self, **kwargs):
        return {
            "intent_id": "presence_intent_unit",
            "intent_version": 7,
            "created_at": 1_000.0,
            **kwargs,
        }

    async def enqueue_trajectory(self, **kwargs):
        self.enqueued.append(kwargs)
        ttl = kwargs.get("start_ttl_sec")
        ttl = 30.0 if ttl is None else float(ttl)
        return {
            "ok": True,
            "status": "queued",
            "event_id": f"event-{len(self.enqueued)}",
            "start_before": 1_000.0 + ttl,
        }

    async def reject_intent(self, intent_id, reason):
        self.rejected.append((intent_id, reason))


class UnitSprites:
    candidate = {
        "sprite_id": "fog_seed",
        "description": "violet fog",
        "base_height_dip": 240,
    }

    async def has_available_sprites(self, **_kwargs):
        return True

    async def available_sprites(self):
        return [dict(self.candidate)]


def _png():
    image = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    for x in range(8, 24):
        for y in range(6, 26):
            image.putpixel((x, y), (120, 70, 220, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _trajectory(sprite_id="fog_seed"):
    return {
        "sprite_id": sprite_id,
        "target_screen": "active",
        "anchor": "bottom_right",
        "transform_origin": "center",
        "duration_ms": 2_000,
        "tracks": [
            {"prop": "x", "keys": [[0, 100], [500, 0], [2_000, 100]], "ease": "out_cubic"},
            {"prop": "opacity", "keys": [[0, 0], [300, 1], [2_000, 0]]},
        ],
    }


def _duration_trajectory(duration_ms, *, sprite_id="fog_seed"):
    return {
        "sprite_id": sprite_id,
        "target_screen": "active",
        "anchor": "bottom_right",
        "transform_origin": "center",
        "duration_ms": int(duration_ms),
        "tracks": [
            {
                "prop": "opacity",
                "keys": [[0, 1], [int(duration_ms), 1]],
                "ease": "linear",
            }
        ],
    }


async def _stack(tmp_path):
    db_path = tmp_path / "renderer.db"

    @asynccontextmanager
    async def db_factory():
        async with aiosqlite.connect(db_path, timeout=2) as db:
            yield db

    async with db_factory() as db:
        await init_presence_tables(db)
        await db.commit()
    ledger = FakeLedger()
    sprites = SpriteLibrary(
        get_db_factory=db_factory,
        storage_dir=tmp_path / "sprites",
        now=lambda: 1_000.0,
        timezone_name="UTC",
    )
    added = await sprites.add_sprite(
        sprite_id="fog_seed",
        png=_png(),
        description="violet fog that peeks from corners",
        base_height_dip=240,
    )
    await sprites.mark_synced(added["sprite_hash"])
    delivery = PresenceDeliveryService(
        get_db_factory=db_factory,
        sprites=sprites,
        now=lambda: 1_000.0,
        monotonic=lambda: 1_000.0,
        terminal_recorder=ledger,
    )
    await delivery.touch_agent()
    return delivery, sprites, ledger


def test_presence_renderer_strictly_renders_and_queues(tmp_path):
    async def scenario():
        delivery, sprites, ledger = await _stack(tmp_path)
        seen_prompts = []

        async def model_call(prompt):
            seen_prompts.append(prompt)
            import json

            return json.dumps(_trajectory())

        renderer = PresenceRenderer(
            delivery=delivery,
            sprites=sprites,
            ledger=ledger,
            slot_checker=lambda: True,
            model_call=model_call,
        )
        result = await renderer.render_and_enqueue(
            intent_text="从右下角探出来，晃一下再缩回去",
            context=ToolContext(
                conv_id="conv-render",
                msg_id="opp-msg",
                request_id="opp-msg",
                metadata={"source": "opportunity"},
            ),
        )
        assert result["status"] == "queued"
        assert len(seen_prompts) == 1
        assert {item[0] for item in ledger.calls} >= {"request", "output", "frame", "turn"}
        assert (await delivery.get_event(result["event_id"]))["status"] == "queued"

    asyncio.run(scenario())


def test_presence_renderer_has_no_fallback_for_invalid_or_unavailable_output(tmp_path):
    async def scenario():
        delivery, sprites, ledger = await _stack(tmp_path)

        async def invalid_model(_prompt):
            return "not json"

        renderer = PresenceRenderer(
            delivery=delivery,
            sprites=sprites,
            ledger=ledger,
            slot_checker=lambda: True,
            model_call=invalid_model,
        )
        invalid = await renderer.render_and_enqueue(
            intent_text="peek",
            context=ToolContext(conv_id="conv-invalid", request_id="opp-invalid"),
        )
        assert invalid["ok"] is False
        assert "json_invalid" in invalid["reason"]

        async def wrong_sprite(_prompt):
            import json

            return json.dumps(_trajectory("not_in_library"))

        renderer.model_call = wrong_sprite
        unavailable = await renderer.render_and_enqueue(
            intent_text="peek again",
            context=ToolContext(conv_id="conv-invalid", request_id="opp-invalid-2"),
        )
        assert unavailable["ok"] is False
        assert unavailable["reason"] == "presence_renderer_sprite_unavailable"

    asyncio.run(scenario())


def test_show_readiness_uses_renderer_agent_and_synced_sprite_in_one_gate():
    class Delivery:
        online = False

        async def agent_online(self, **_kwargs):
            return self.online

    class Sprites:
        available = False

        async def has_available_sprites(self, **_kwargs):
            return self.available

    delivery = Delivery()
    sprites = Sprites()
    configured = [False]
    renderer = PresenceRenderer(
        delivery=delivery,
        sprites=sprites,
        slot_checker=lambda: configured[0],
    )

    assert asyncio.run(renderer.readiness()) == {
        "ready": False,
        "reason": "presence_renderer_unconfigured",
    }
    configured[0] = True
    assert asyncio.run(renderer.readiness()) == {
        "ready": False,
        "reason": "presence_agent_offline",
    }
    delivery.online = True
    assert asyncio.run(renderer.readiness()) == {
        "ready": False,
        "reason": "presence_no_synced_sprites",
    }
    sprites.available = True
    assert asyncio.run(renderer.readiness()) == {"ready": True, "reason": ""}


def test_renderer_output_parser_rejects_extra_prose():
    with pytest.raises(Exception):
        parse_renderer_output("Here you go: " + json.dumps(_trajectory()))


def test_renderer_prompt_contains_production_spike_repairs():
    prompt = build_renderer_prompt(
        intent_text="从左侧探头",
        sprites=[
            {
                "sprite_id": "fog_seed",
                "description": "violet fog",
                "base_height_dip": 240,
            }
        ],
    )
    system = prompt[0]["content"]
    assert "[time_ms,value]" in system
    assert "[0,-300]" in system
    assert "scale 是倍率" in system
    assert "不要用屏幕宽高当越界偏移" in system
    assert "完全没指定位置就用 center_right" in system
    assert "只指定上沿或下沿时分别用 top_right 或 bottom_right" in system
    assert "明确要求左侧或屏幕正中央" in system


def test_renderer_prompt_long_duration_switch_and_round_kind_are_explicit():
    sprites = [UnitSprites.candidate]
    legacy = build_renderer_prompt(intent_text="探出来", sprites=sprites)
    assert "1..15000" in legacy[0]["content"]
    assert "1..600000" not in legacy[0]["content"]

    long_prompt = build_renderer_prompt(
        intent_text="探出来",
        sprites=sprites,
        round_kind="summon",
        long_duration_enabled=True,
    )
    # The server has already classified the round, so the prompt states one
    # range.  Showing the whole table would invite the model to classify the
    # same intent again and disagree, costing a correction round-trip.
    assert "60000..600000" in long_prompt[0]["content"]
    assert "没有别的指定时就取 120000..180000" in long_prompt[0]["content"]
    assert "3000..8000" not in long_prompt[0]["content"]
    assert "180000..480000" not in long_prompt[0]["content"]
    assert json.loads(long_prompt[1]["content"])["round_kind"] == "summon"
    assert normalize_round_kind("unexpected") == "chat"

    brief = build_renderer_prompt(
        intent_text="贴着屏幕下沿快步横穿过去，像只是路过",
        sprites=sprites,
        round_kind="idle",
        long_duration_enabled=True,
    )
    assert "3000..8000" in brief[0]["content"]
    assert "60000..600000" not in brief[0]["content"]
    # A seconds-long flyby must not be told to write a three-phase dwell.
    assert "保持段" not in brief[0]["content"]


def test_renderer_prompt_shows_a_worked_minute_scale_trajectory():
    """Prose alone does not carry; the only worked example must be long-form."""

    prompt = build_renderer_prompt(
        intent_text="陪着我待一会儿",
        sprites=[UnitSprites.candidate],
        round_kind="idle",
        long_duration_enabled=True,
    )[0]["content"]

    assert "180000..480000" in prompt
    assert '"keys":[[0,0],[600,1],[118800,1],[120000,0]]' in prompt
    # Nothing in the schema forces keyframes to span duration_ms, so a
    # trajectory that stops early freezes and then vanishes without an exit.
    assert "最后一帧的时间必须正好等于 duration_ms" in prompt


def test_renderer_correction_states_the_range_in_words_not_a_policy_token():
    prompt = build_renderer_prompt(
        intent_text="陪着我待一会儿",
        sprites=[UnitSprites.candidate],
        round_kind="idle",
        long_duration_enabled=True,
        correction={
            "reason": "presence_duration_policy:stay:expected_180000_480000",
            "previous_trajectory": {"duration_ms": 8_000},
        },
    )
    correction = json.loads(prompt[1]["content"])["correction"]

    assert "上一版写的是 8000" in correction["instruction"]
    assert "180000..480000" in correction["instruction"]
    assert "presence_duration_policy" not in correction["instruction"]
    # The machine token still reaches the ledger unchanged.
    assert correction["reason"] == (
        "presence_duration_policy:stay:expected_180000_480000"
    )


@pytest.mark.parametrize(
    ("intent_text", "round_kind", "duration_ms", "profile", "valid"),
    [
        ("路过闪一下", "idle", 5_000, "brief", True),
        ("路过闪一下", "idle", 2_000, "brief", False),
        ("留下来陪一会", "chat", 240_000, "stay", True),
        ("探出来看看", "chat", 60_000, "default", True),
        ("路过闪一下", "summon", 59_999, "summon", False),
        ("路过闪一下", "summon", 60_000, "summon", True),
    ],
)
def test_renderer_duration_policy_profiles(
    intent_text,
    round_kind,
    duration_ms,
    profile,
    valid,
):
    actual_profile, reason = duration_policy_reason(
        intent_text=intent_text,
        round_kind=round_kind,
        duration_ms=duration_ms,
    )
    assert actual_profile == profile
    assert bool(reason) is not valid


def test_summon_retry_uses_caller_round_kind_distinct_ledger_ids_and_60s_ttl():
    async def scenario():
        delivery = UnitDelivery()
        ledger = FakeLedger()
        outputs = iter(
            [
                json.dumps(_duration_trajectory(59_999)),
                json.dumps(_duration_trajectory(60_000)),
            ]
        )
        prompts = []

        async def model_call(prompt):
            prompts.append(prompt)
            return next(outputs)

        renderer = PresenceRenderer(
            delivery=delivery,
            sprites=UnitSprites(),
            ledger=ledger,
            slot_checker=lambda: True,
            model_call=model_call,
            behavior_loader=lambda: {"presence_long_duration_enabled": True},
        )
        result = await renderer.render_and_enqueue(
            intent_text="路过闪一下",
            context=ToolContext(
                conv_id="conv-summon-duration",
                request_id="summon-duration",
                metadata={"round_kind": "summon"},
            ),
        )

        assert result["status"] == "queued"
        assert result["start_before"] == 1_060.0
        assert delivery.enqueued[0]["start_ttl_sec"] == 60.0
        assert len(prompts) == 2
        correction = json.loads(prompts[1][1]["content"])["correction"]
        assert correction["previous_trajectory"]["duration_ms"] == 59_999

        requests = [call for call in ledger.calls if call[0] == "request"]
        outputs_seen = [call for call in ledger.calls if call[0] == "output"]
        frames = [call for call in ledger.calls if call[0] == "frame"]
        turns = [call for call in ledger.calls if call[0] == "turn"]
        assert (len(requests), len(outputs_seen), len(frames), len(turns)) == (2, 2, 1, 1)
        invocation_ids = [call[2]["invocation_id"] for call in requests]
        assert len(set(invocation_ids)) == 2
        assert all(
            call[1][0].metadata["round_kind"] == "summon"
            for call in requests
        )
        assert outputs_seen[0][2]["error"] == "presence_duration_policy_retry"
        assert frames[0][2]["metadata"]["duration_policy_degraded"] is False

    asyncio.run(scenario())


def test_second_policy_violation_dispatches_degraded_but_invalid_retry_rejects():
    async def run_case(second_output):
        delivery = UnitDelivery()
        ledger = FakeLedger()
        outputs = iter(
            [json.dumps(_duration_trajectory(59_999)), second_output]
        )

        async def model_call(_prompt):
            return next(outputs)

        renderer = PresenceRenderer(
            delivery=delivery,
            sprites=UnitSprites(),
            ledger=ledger,
            slot_checker=lambda: True,
            model_call=model_call,
            behavior_loader=lambda: {"presence_long_duration_enabled": True},
        )
        result = await renderer.render_and_enqueue(
            intent_text="出现一下",
            context=ToolContext(
                conv_id="conv-retry-result",
                request_id=f"retry-{len(str(second_output))}",
                metadata={"round_kind": "summon"},
            ),
        )
        return result, delivery, ledger

    degraded, degraded_delivery, degraded_ledger = asyncio.run(
        run_case(json.dumps(_duration_trajectory(59_999)))
    )
    assert degraded["status"] == "queued"
    assert degraded_delivery.enqueued[0]["start_ttl_sec"] == 60.0
    degraded_frame = next(
        call for call in degraded_ledger.calls if call[0] == "frame"
    )
    assert degraded_frame[2]["metadata"]["duration_policy_degraded"] is True

    rejected, rejected_delivery, rejected_ledger = asyncio.run(run_case("not json"))
    assert rejected["status"] == "rejected"
    assert "json_invalid" in rejected["reason"]
    assert rejected_delivery.enqueued == []
    assert len(rejected_delivery.rejected) == 1
    assert len([call for call in rejected_ledger.calls if call[0] == "frame"]) == 1


@pytest.mark.parametrize(
    ("enabled", "intent_text", "round_kind", "duration_ms"),
    [
        (False, "出现一下", "summon", 2_000),
        (True, "路过闪一下", "idle", 5_000),
    ],
)
def test_legacy_switch_and_explicit_brief_path_do_not_retry(
    enabled,
    intent_text,
    round_kind,
    duration_ms,
):
    async def scenario():
        delivery = UnitDelivery()
        ledger = FakeLedger()
        calls = []

        async def model_call(prompt):
            calls.append(prompt)
            return json.dumps(_duration_trajectory(duration_ms))

        renderer = PresenceRenderer(
            delivery=delivery,
            sprites=UnitSprites(),
            ledger=ledger,
            slot_checker=lambda: True,
            model_call=model_call,
            behavior_loader=lambda: {"presence_long_duration_enabled": enabled},
        )
        result = await renderer.render_and_enqueue(
            intent_text=intent_text,
            context=ToolContext(
                conv_id="conv-no-retry",
                request_id=f"no-retry-{enabled}",
                metadata={"round_kind": round_kind},
            ),
        )
        assert result["status"] == "queued"
        assert result["start_before"] == 1_030.0
        assert delivery.enqueued[0]["start_ttl_sec"] is None
        assert len(calls) == 1

    asyncio.run(scenario())


def test_renderer_passes_verified_structured_controls_to_slot(monkeypatch):
    import ai_providers

    seen = {}

    async def fake_call_slot_chat(slot_name, messages, **kwargs):
        seen.update(slot_name=slot_name, messages=messages, **kwargs)
        return "{}"

    monkeypatch.setattr(ai_providers, "call_slot_chat", fake_call_slot_chat)
    renderer = PresenceRenderer()
    result = asyncio.run(
        renderer._call_model([{"role": "user", "content": "render"}])
    )

    assert result == "{}"
    assert seen["slot_name"] == "presence_renderer"
    assert seen["expect_json"] is True
    assert seen["temperature"] == 0.0
    assert seen["thinking_budget"] == 0
    assert seen["response_schema"] is PRESENCE_RENDERER_RESPONSE_SCHEMA


def test_gemini_slot_payload_honors_json_schema_and_zero_thinking():
    import ai_providers

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    payload = ai_providers._gemini_slot_payload(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        temperature=0.0,
        max_tokens=1100,
        expect_json=True,
        response_schema=schema,
        thinking_budget=0,
    )

    generation = payload["generationConfig"]
    assert generation == {
        "temperature": 0.0,
        "maxOutputTokens": 1100,
        "responseMimeType": "application/json",
        "responseJsonSchema": schema,
        "thinkingConfig": {"thinkingBudget": 0},
    }


def test_renderer_configuration_gate_requires_model_and_resolved_key(monkeypatch):
    import app.presence.renderer as renderer_module

    monkeypatch.setattr(
        renderer_module,
        "get_slot",
        lambda _name: {
            "model": "gemini-2.5-flash",
            "endpoint": {"type": "gemini", "api_key": ""},
        },
    )
    assert presence_renderer_configured() is False

    monkeypatch.setattr(
        renderer_module,
        "get_slot",
        lambda _name: {
            "model": "gemini-2.5-flash",
            "endpoint": {"type": "gemini", "api_key": "resolved-key"},
        },
    )
    assert presence_renderer_configured() is True


def test_settings_migration_adds_explicit_but_fail_closed_renderer_slot(monkeypatch):
    import config

    monkeypatch.setattr(config, "_env", lambda *_names: "")
    data = {
        "endpoints": [
            {
                "id": "sf",
                "name": "SiliconFlow",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": "test",
                "type": "openai",
            }
        ],
        "slots": {},
    }
    assert config._ensure_endpoints_and_slots(data) is True
    assert data["slots"]["presence_renderer"] == {
        "endpoint": "sf",
        "model": "",
    }


def test_settings_migration_adds_and_binds_verified_direct_gemini_once():
    import config

    data = {
        "gemini_key": "test-gemini-key",
        "endpoints": [
            {
                "id": "sf",
                "name": "SiliconFlow",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": "test-sf-key",
                "type": "openai",
            }
        ],
        "slots": {"presence_renderer": {"endpoint": "sf", "model": ""}},
    }
    assert config._ensure_endpoints_and_slots(data) is True
    endpoint = next(item for item in data["endpoints"] if item["type"] == "gemini")
    assert endpoint["base_url"] == "https://generativelanguage.googleapis.com/v1beta"
    assert data["slots"]["presence_renderer"] == {
        "endpoint": endpoint["id"],
        "model": "gemini-2.5-flash",
    }
    assert data["presence_renderer_gemini_migration_v1"] is True
    assert data["slots"]["presence_image"] == {
        "endpoint": endpoint["id"],
        "model": "gemini-3.1-flash-image",
    }
    assert data["presence_image_slot_migration_v1"] is True

    # Once migrated, clearing either slot is an owner-controlled off switch
    # and must not be undone even if the endpoint is later removed.
    data["slots"]["presence_renderer"]["model"] = ""
    data["slots"]["presence_renderer"]["endpoint"] = "sf"
    data["slots"]["presence_image"]["model"] = ""
    data["slots"]["presence_image"]["endpoint"] = "sf"
    data["endpoints"] = [
        item for item in data["endpoints"] if item["type"] != "gemini"
    ]
    assert config._ensure_endpoints_and_slots(data) is False
    assert data["slots"]["presence_renderer"]["model"] == ""
    assert data["slots"]["presence_image"]["model"] == ""
    assert not any(item["type"] == "gemini" for item in data["endpoints"])


def test_gemini_endpoint_reuses_top_level_key(monkeypatch):
    import config

    monkeypatch.setattr(config, "_env", lambda *_names: "")
    monkeypatch.setattr(
        config,
        "SETTINGS",
        {
            "gemini_key": "shared-key",
            "endpoints": [
                {
                    "id": "gem",
                    "name": "Gemini",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta",
                    "api_key": "",
                    "type": "gemini",
                }
            ],
        },
    )
    assert config.get_endpoint("gem")["api_key"] == "shared-key"


def test_presence_image_migration_preserves_owner_configured_slot(monkeypatch):
    import config

    monkeypatch.setattr(config, "_env", lambda *_names: "")
    data = {
        "endpoints": [
            {
                "id": "sf",
                "name": "SiliconFlow",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": "test-sf-key",
                "type": "openai",
            },
            {
                "id": "gem",
                "name": "Gemini",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "test-gemini-key",
                "type": "gemini",
            },
        ],
        "slots": {
            "presence_image": {
                "endpoint": "sf",
                "model": "owner/image-model",
            }
        },
    }

    assert config._ensure_endpoints_and_slots(data) is True
    assert data["slots"]["presence_image"] == {
        "endpoint": "sf",
        "model": "owner/image-model",
    }
    assert data["presence_image_slot_migration_v1"] is True
