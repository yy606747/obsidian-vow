import json
import time
from types import SimpleNamespace

from app.chat.history import build_handoff_note_block
from app.chat.prompt_builder import build_aftercare_prompt_block
from app.chat.worldbook import build_worldbook_prefix, resolve_worldbook_names
from app.context_delivery import ContextDeliveryProjection, CurrentContextItem
from app.context_delivery.renderer import render_context_delivery_projection
from app.memory_v2.prompt_block import build_v2_memory_prompt_block
from app.memory_v3.recall_intent import recall_intent_ability_block
from app.memory_v3.timeline import build_timeline_prompt_block
from app.schedule.prompt import build_abilities_block
from app.vows.prompt import build_vow_ability_block, build_vow_block
from app.web_search.prompt import render_ready_results
from app.working_model.writer import (
    build_working_model_writer_messages,
    build_writer_identity_snapshot,
)


USER_NAME = "小栀"
AI_NAME = "阿澈"


def _assert_relationship_names(text: str) -> None:
    assert USER_NAME in text
    assert "用户" not in text
    assert "AI人设" not in text
    assert "用户信息" not in text


def test_image_review_prompt_and_tool_use_runtime_names():
    from app.image_memory.view import image_followup_prompt
    from app.tools.prompt_renderers import _render_view_image

    prompt = image_followup_prompt({"source_time": 1, "source_message_id": "photo", "attachment_url": "/uploads/photo.png"}, user_name=USER_NAME, ai_name=AI_NAME)
    ability = _render_view_image("main_stable", {"image_memory_available": True, "user_name": USER_NAME, "ai_name": AI_NAME})
    for text in (prompt, ability):
        _assert_relationship_names(text)
        assert AI_NAME in text
        assert "对方" not in text and "TA" not in text


def test_core_relationship_blocks_use_configured_names():
    worldbook = {
        "user_name": USER_NAME,
        "ai_name": AI_NAME,
        "user_persona": "最近在准备一次重要考试。",
        "ai_persona": "嘴硬但会认真接住她。",
    }
    prefix_text = "\n".join(message["content"] for message in build_worldbook_prefix(worldbook))
    _assert_relationship_names(prefix_text)
    assert AI_NAME in prefix_text

    blocks = [
        build_vow_block(
            [{"content": "说定的事要算数", "created_at": 1.0}],
            now=2.0,
            user_name=USER_NAME,
        ),
        build_vow_ability_block(remaining_today=1, user_name=USER_NAME),
        recall_intent_ability_block(user_name=USER_NAME),
        build_handoff_note_block("还在等一个决定。", user_name=USER_NAME),
        build_aftercare_prompt_block(
            SimpleNamespace(aftercare_active=True, safety_close_reason="safeword"),
            USER_NAME,
        ),
        build_abilities_block(USER_NAME, "暂无日程"),
    ]
    for block in blocks:
        _assert_relationship_names(block)


def test_memory_timeline_and_web_guards_use_configured_name():
    memory = build_v2_memory_prompt_block(
        {
            "selected": [
                {
                    "id": "memory-1",
                    "content": "她之前提到过这件事。",
                    "kind": "episode",
                    "namespace": "normal",
                    "score": 0.9,
                }
            ]
        },
        user_name=USER_NAME,
    )
    _assert_relationship_names(memory["content"])

    now = time.time()
    timeline = build_timeline_prompt_block(
        {
            "entries_json": json.dumps(
                [
                    {"text": "用户的考试安排已经定下。", "source_message_ids": ["m0"]},
                    {"text": "assistant答应会陪着她。", "source_message_ids": ["m1"]},
                    {"text": "考试安排已经定下。", "source_message_ids": ["m2"]},
                ],
                ensure_ascii=False,
            )
        },
        visible_message_ids=[],
        source_created_at={"m0": now - 180, "m1": now - 120, "m2": now - 60},
        reference_ts=now,
        max_chars=1000,
        user_name=USER_NAME,
        ai_name=AI_NAME,
    )
    _assert_relationship_names(timeline["content"])
    assert "assistant" not in timeline["content"].casefold()
    assert timeline["generic_relationship_label_indices"] == [0, 1]

    web = render_ready_results(
        [
            {
                "intent_text": "查一下今天的资料",
                "ready_at": now,
                "result_json": json.dumps(
                    {"digest": "资料已经整理好。", "searched_at": now},
                    ensure_ascii=False,
                ),
            }
        ],
        user_name=USER_NAME,
    )
    _assert_relationship_names(web)


def test_writer_identity_uses_both_names_and_legacy_placeholders_do_not_leak():
    assert resolve_worldbook_names({"user_name": "用户", "ai_name": "AI"}) == ("她", "我")

    identity = build_writer_identity_snapshot({
        "user_name": USER_NAME,
        "ai_name": AI_NAME,
        "user_persona": "很在意回答是否直接。",
        "ai_persona": "会认真听完。",
    })
    messages = build_working_model_writer_messages(
        identity_snapshot=identity,
        current_working_model="她很在意回答是否直接。",
        current_desire="认真陪着她。",
        statement="她希望问题被正面回答。",
        source="她刚才明确说不要绕开问题。",
        original_user_message="不要绕开这个问题。",
        original_user_message_id="source-message",
    )
    system_prompt = messages[0]["content"]
    _assert_relationship_names(system_prompt)
    assert AI_NAME in system_prompt
    payload = json.loads(messages[-1]["content"])
    assert payload["original_user_message"]["speaker"] == USER_NAME
    assert payload["statement_source"]["speaker"] == AI_NAME


def test_context_delivery_location_boundary_uses_configured_name():
    rendered = render_context_delivery_projection(
        ContextDeliveryProjection(
            generated_at=1010.0,
            observations=(CurrentContextItem(
                key="location.place",
                value="家",
                source="location.v2",
                observed_at=1000.0,
                received_at=1001.0,
                freshness_sec=10.0,
            ),),
        ),
        user_name=USER_NAME,
        ai_name=AI_NAME,
        time_formatter=lambda _value: "15:00",
    )

    _assert_relationship_names(rendered)
    assert "仅凭定位不能判断小栀" in rendered
