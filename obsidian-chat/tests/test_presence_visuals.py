import asyncio
import base64
import hashlib
import json

import pytest

from app.presence.db import init_presence_tables
from app.presence.outcomes import PresenceOutcomeInbox
from app.presence.renderer import build_renderer_prompt
from app.presence.visuals import (
    PresenceVisualSummarizer, VISUAL_SCHEMA, parse_visual_summary, visual_disposition,
)
from app.tools.schemas import ToolContext
from test_presence_sprites import _library, _transparent_png, _second_transparent_png


def observation(face="clear"):
    return {"visual": "黑发金眼的小人，脸部清楚", "face": face,
            "pose": "坐着托腮", "palette": "浅灰衣服，明亮光线", "style": "Q版",
            "impression": "嘴角上扬，神情轻松"}


class Summarizer:
    def __init__(self, *, face="clear", fail=False):
        self.face, self.fail = face, fail

    async def preflight(self):
        pass

    async def summarize(self, _png):
        if self.fail:
            raise TimeoutError("test")
        return observation(self.face)


class Provider:
    async def generate(self, _prompt):
        return _second_transparent_png()


@pytest.mark.parametrize("face", ["faceless", "hidden", "not_human", "uncertain"])
def test_human_missing_face_stays_draft_while_nonhuman_is_free(face):
    assert visual_disposition(observation(face), form="human")[0] == "draft"
    assert visual_disposition(observation(face), form="nonhuman") == ("ready", "")


def test_visual_call_sees_finished_png_without_author_story_and_cleans_file(tmp_path):
    async def scenario():
        png = _transparent_png()

        async def caller(slot, messages, **kwargs):
            assert slot == "presence_summary"
            assert kwargs["response_schema"] == VISUAL_SCHEMA
            attachment = messages[1]["attachments"][0]
            path = tmp_path / attachment["url"].split("/")[-1]
            assert path.read_bytes() == png
            assert attachment["expected_sha256"] == hashlib.sha256(png).hexdigest()
            assert "这是我的创作故事" not in str(messages)
            return json.dumps(observation(), ensure_ascii=False)

        summarizer = PresenceVisualSummarizer(caller=caller, uploads=tmp_path)
        assert await summarizer.summarize(png) == observation()
        assert not list(tmp_path.iterdir())

        async def broken(*_args, **_kwargs):
            return '{"face":"clear"}'

        summarizer.caller = broken
        with pytest.raises(ValueError):
            await summarizer.summarize(png)
        assert not list(tmp_path.iterdir())

    asyncio.run(scenario())


def test_gemini_slot_transport_encodes_actual_image_and_verifies_source(tmp_path, monkeypatch):
    import ai_providers

    png = _transparent_png()
    (tmp_path / "finished.png").write_bytes(png)
    monkeypatch.setattr(ai_providers, "UPLOADS_DIR", tmp_path)
    messages = [{"role": "system", "content": "观察成图"}, {"role": "user", "content": "请看图", "attachments": [{
        "url": "/uploads/finished.png", "mime_type": "image/png",
        "expected_sha256": hashlib.sha256(png).hexdigest(),
    }]}]
    payload = ai_providers._gemini_slot_payload(messages, response_schema=VISUAL_SCHEMA)
    parts = payload["contents"][0]["parts"]
    assert len(parts) == 2
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == png
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert payload["generationConfig"]["responseJsonSchema"] == VISUAL_SCHEMA
    (tmp_path / "finished.png").write_bytes(_second_transparent_png())
    with pytest.raises(ValueError, match="图片来源"):
        ai_providers._gemini_slot_payload(messages)
    (tmp_path / "finished.png").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        ai_providers._gemini_slot_payload(messages)


@pytest.mark.parametrize("fail", [False, True])
def test_draft_preserves_png_never_appears_or_becomes_baseline_and_feedback_waits(tmp_path, fail):
    async def scenario():
        library = await _library(tmp_path, provider=Provider(),
                                 summarizer=Summarizer(face="faceless", fail=fail))
        result = await library.draw(form="human", prompt="清楚的脸", description="想安静陪着",
                                    context=ToolContext(conv_id="daily"))
        assert result["status"] == "draft"
        row = await library.get_sprite(result["sprite_id"])
        assert row["active"] == 0 and row["archived"] == 0
        assert row["description"] == "想安静陪着"
        assert (await library.file_for_hash(row["sprite_hash"]))[0] == _second_transparent_png()
        assert await library.human_baseline() is None
        assert not await library.has_non_seed_sprites()  # next night can retry bootstrap
        await library.mark_synced(row["sprite_hash"])
        assert not await library.has_synced_non_seed_sprites()
        assert not await library.available_sprites()
        assert not await library.has_available_sprites()
        async with library._get_db() as db:
            await library._archive_overflow_in_tx(db)
            await db.commit()
        assert (await library.get_sprite(row["sprite_id"]))["active"] == 0
        inbox = PresenceOutcomeInbox(get_db_factory=library._get_db, now=library._now)
        claimed = await inbox.claim_for_turn(conv_id="daily", bound_turn_id="next-chat")
        assert len(claimed["rows"]) == 1
        assert claimed["rows"][0]["presence_status"] == "draw_draft"
        assert "草稿" in claimed["block"]
        if not fail:
            assert "faceless" in claimed["block"]
        await inbox.release_claim(bound_turn_id="next-chat")
        assert (await inbox.claim_for_turn(conv_id="daily", bound_turn_id="retry"))["outcome_ids"] == claimed["outcome_ids"]

    asyncio.run(scenario())


def test_ready_sprite_requires_sync_and_nonhuman_does_not_need_human_face(tmp_path):
    async def scenario():
        library = await _library(tmp_path, provider=Provider(), summarizer=Summarizer(face="not_human"))
        baseline = await library.add_sprite(sprite_id="baseline", png=_transparent_png(), form="human",
                                          provider="owner", description="既有形象", prompt="黑金Q版")
        await library.mark_synced(baseline["sprite_hash"])
        result = await library.draw(form="nonhuman", prompt="石碑", description="想当一块石碑",
                                    context=ToolContext(conv_id="daily"))
        assert result["visual_status"] == "ready"
        assert len(await library.available_sprites()) == 1
        await library.mark_synced(result["sprite_hash"])
        assert len(await library.available_sprites()) == 2
        assert (await library.human_baseline())["sprite_id"] == "baseline"
        inbox = PresenceOutcomeInbox(get_db_factory=library._get_db, now=library._now)
        claim = await inbox.claim_for_turn(conv_id="daily", bound_turn_id="next-chat")
        assert claim["rows"][0]["presence_status"] == "draw_ready"
        assert "不代表已经展示" in claim["block"]

    asyncio.run(scenario())


def test_backfill_preserves_owner_curation_and_author_intent(tmp_path):
    async def scenario():
        library = await _library(tmp_path, summarizer=Summarizer(face="faceless"))
        result = await library.add_sprite(sprite_id="archived", png=_transparent_png(), form="human",
                                          provider="gemini", description="私人的创作自述", prompt="画图指令")
        async with library._get_db() as db:
            await db.execute("UPDATE presence_sprites SET active=0,archived=1 WHERE sprite_id='archived'")
            await db.commit()
        row = await library.summarize_existing("archived")
        assert (row["active"], row["archived"]) == (0, 1)
        assert row["description"] == "私人的创作自述"
        assert json.loads(row["visual_summary_json"])["face"] == "faceless"
        assert row["sprite_hash"] == result["sprite_hash"]

    asyncio.run(scenario())


def test_renderer_prioritizes_observed_appearance_separate_from_story():
    sprite = {"sprite_id": "mask", "base_height_dip": 260, "description": "温柔守护" * 100,
              "prompt": "画一张温柔的脸", "visual_summary_json": json.dumps(observation("faceless"))}
    prompt = build_renderer_prompt(intent_text="来陪着", sprites=[sprite])
    candidate = json.loads(prompt[1]["content"])["available_sprites"][0]
    assert candidate["appearance"]["face"] == "faceless"
    assert "author_intent" in candidate and "description" not in candidate
    assert "以外观为准" in prompt[0]["content"]
    del sprite["visual_summary_json"]
    legacy = json.loads(build_renderer_prompt(intent_text="来陪着", sprites=[sprite])[1]["content"])
    assert legacy["available_sprites"][0]["appearance"] == {"unverified_generation_prompt": "画一张温柔的脸"}


def test_outcome_migration_preserves_consumed_receipts(tmp_path):
    async def scenario():
        library = await _library(tmp_path)
        inbox = PresenceOutcomeInbox(get_db_factory=library._get_db, now=library._now)
        await inbox.record_terminal(event_id="old", conv_id="daily", status="played")
        await inbox.claim_for_turn(conv_id="daily", bound_turn_id="old-turn")
        async with library._get_db() as db:
            await inbox.consume_claimed_in_tx(db, bound_turn_id="old-turn", assistant_message_id="old-message")
            # Recreate the previous CHECK constraint, keeping the exact old data.
            sql = (await (await db.execute("SELECT sql FROM sqlite_master WHERE name='presence_outcomes'")).fetchone())[0]
            await db.execute("ALTER TABLE presence_outcomes RENAME TO saved_outcomes")
            await db.execute("DROP INDEX idx_presence_outcomes_conv_state_time")
            await db.execute(sql.replace(",'draw_ready','draw_draft'", ""))
            await db.execute("INSERT INTO presence_outcomes SELECT * FROM saved_outcomes")
            await db.execute("DROP TABLE saved_outcomes")
            await init_presence_tables(db)
            await init_presence_tables(db)
            await db.commit()
        rows = await inbox.rows_for_event("old")
        assert rows[0]["state"] == "consumed"
        assert rows[0]["consumed_by_message_id"] == "old-message"
        assert await inbox.record_terminal(event_id="new", conv_id="daily", status="draw_ready")

    asyncio.run(scenario())


def test_summary_slot_copies_vision_configuration_once_and_preserves_owner_choices():
    import config

    data = {"endpoints": [], "slots": {"sticker_summary": {"endpoint": "vision", "model": "vision-model"}}}
    config._ensure_endpoints_and_slots(data)
    assert data["slots"]["presence_summary"] == {"endpoint": "vision", "model": "vision-model"}
    data["slots"]["sticker_summary"]["model"] = "changed-sticker-model"
    data["slots"]["presence_summary"]["model"] = ""  # explicitly disabled by owner
    config._ensure_endpoints_and_slots(data)
    assert data["slots"]["presence_summary"] == {"endpoint": "vision", "model": ""}
