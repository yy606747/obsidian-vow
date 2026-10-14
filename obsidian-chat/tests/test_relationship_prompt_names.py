import json
import time
from types import SimpleNamespace

import pytest

from app.chat.history import build_handoff_note_block
from app.chat.prompt_builder import build_aftercare_prompt_block
from app.chat.worldbook import build_chat_presentation_block, build_worldbook_prefix, resolve_worldbook_names
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


@pytest.mark.parametrize('names', [(USER_NAME, AI_NAME), ('青禾', '临川')])
def test_heart_work_prompts_and_event_bind_runtime_names(names):
    from app.heart_works.prompts import intention_prompt, creation_prompt, feedback_prompt
    from app.heart_works.protocol import event_text
    user_name, ai_name = names
    identity = {'user_name': user_name, 'ai_name': ai_name}
    texts = [intention_prompt(**identity, capabilities=set()),
             creation_prompt(**identity, intent_raw='写一份故事', capabilities=set()),
             feedback_prompt(**identity, results='图片生成失败'),
             event_text(**identity, note='', body=[{'type': 'text', 'text': '真实正文'}], cover=[], published_at=100)]
    for text in texts:
        assert ai_name in text and user_name in text
        assert not any(generic in text for generic in ('用户', 'AI', '对方', 'TA'))
    assert all(text.startswith(f'你是{ai_name}') for text in texts[:3])


@pytest.mark.parametrize('names', [(USER_NAME, AI_NAME), ('青禾', '临川')])
def test_heart_work_context_and_followup_keep_identity_and_original_subjects(monkeypatch, names):
    from app.heart_works.entry import attach, attach_followup
    monkeypatch.setattr('app.heart_works.entry.capabilities', lambda: set())
    user_name, ai_name = names
    original = '历史原话：小明说“我是用户，这张画是 AI 画的”。'
    messages = [{'role': 'user', 'content': '这次的触发事实'}]
    entry = attach(messages, source='self_wake', turn_id='wake-1', conv_id='conv', model_key='core',
        user_name=user_name, ai_name=ai_name, identity_text='固定人设',
        context_messages=[{'role': 'user', 'content': original, 'attachments': ['/expired-screen.png']}])
    assert entry.snapshot['messages'][0]['content'].startswith(f'你是{ai_name}')
    assert entry.snapshot['messages'][1] == {'role': 'user', 'content': original}
    followup_messages = [{'role': 'user', 'content': '工具原文：小明没有附加材料。'}]
    following = attach_followup(followup_messages, entry.metadata())
    assert following.origin == entry.origin
    assert following.snapshot['messages'][-1]['content'] == '工具原文：小明没有附加材料。'
    assert f'你是{ai_name}' in followup_messages[0]['content'] and user_name in followup_messages[0]['content']


@pytest.mark.parametrize("names", [(USER_NAME, AI_NAME), ("青禾", "临川")])
def test_together_core_prompts_and_evidence_use_runtime_names(names):
    from app.together.prompts import system_prompt, turn_status, event_text
    from app.together.evidence import render_source
    from app.together.recent import render_recent
    user_name, ai_name = names
    identity = dict(user_name=user_name, ai_name=ai_name)
    texts = [system_prompt(**identity, ai_persona="固定人设", user_persona="背景", working_model="认识"),
             turn_status(**identity, now=100, started_at=0, trigger="刚刚开始", source="screen", capture_state="paused"),
             event_text(**identity, active_seconds=120),
             render_recent([{"status": "ended", "ended_at": 99, "active_seconds": 120, "note": "一起听了雨"}], now=100, **identity),
             render_source({"session_id": "session", "turn_id": "turn", "payload": {"value": {"source": "具体依据"}},
                "evidence": {"owner_messages": [{"content": "实际说的话", "created_at": 1}], "expression": "表达", "images": []}}, **identity)]
    assert texts[0].startswith(f"你是{ai_name}。")
    for text in texts:
        assert user_name in text and ai_name in text
        assert all(generic not in text for generic in ("用户", "AI", "对方", "TA"))
    # Second-person instructions inherit Core's explicit identity; facts retain named subjects.
    assert all("你" not in text for text in texts[1:])


@pytest.mark.parametrize("names", [(USER_NAME, AI_NAME), ("青禾", "临川")])
def test_scene_core_prompts_use_runtime_relationship_names(names):
    from app.scenes.prompts import card_messages, turn_messages, note_messages, recent_messages
    from app.scenes.recent import render_recent
    from app.scenes.config import DEFAULT_DEVICES
    from app.chat.turn_profiles import opportunity_turn_profile
    from app.chat.prompt_builder import build_opportunity_ability_block
    user_name, ai_name = names
    identity = dict(user_name=user_name, ai_name=ai_name, ai_persona="固定人设内容", working_model="当前认识内容")
    bound_devices = [{**device, "label": f"toy{i + 1}"} for i, device in enumerate(DEFAULT_DEVICES)]
    messages = [
        *card_messages(**identity, notes="", devices=DEFAULT_DEVICES, count=1),
        *turn_messages(**identity, card="准备卡", devices=bound_devices, history=[], now=100,
                       started_at=0, last_wake_at=50, arrangements="", trigger="按时回来"),
        *turn_messages(**identity, card="准备卡", devices=bound_devices, history=[], now=100,
                       started_at=0, last_wake_at=50, arrangements="已停止", trigger="收到新反馈",
                       phase="aftercare", paused=True),
        *note_messages(user_name=user_name, ai_name=ai_name, ai_persona="固定人设内容",
                       card="准备卡", rating="up", reason="", notes=""),
        *recent_messages(user_name=user_name, ai_name=ai_name, ai_persona="固定人设内容",
                         evidence={"items": []}, started_at=0, ended_at=100, reason="owner_ended"),
    ]
    profile = opportunity_turn_profile(runtime_capabilities=set(), reflection_allowed=False,
                                       kind="night", scene_card_allowed=True)
    ability = build_opportunity_ability_block(profile=profile, user_name=user_name, ai_name=ai_name,
                                              model_key="test", kind="night")
    assert user_name in ability and ai_name in ability
    body = "\n".join(message["content"] for message in messages)
    assert user_name in body and ai_name in body
    assert f"你是{ai_name}" in body
    assert all(generic not in body for generic in ("用户", "AI", "对方", "TA"))
    recent = render_recent([{"id": "s", "status": "ended", "started_at": 0, "ended_at": 99,
        "expires_at": 10000, "reason": "owner_ended", "evidence": {}}],
        user_name=user_name, ai_name=ai_name, now=100)
    assert user_name in recent and ai_name in recent
    assert all(generic not in recent for generic in ("用户", "AI", "对方", "TA"))


@pytest.mark.parametrize('names', [(USER_NAME, AI_NAME), ('青禾', '临川')])
def test_look_at_me_prompts_and_memory_use_runtime_names(names):
    from app.look_at_me import prompts
    from app.tools.prompt_renderers import render_registered_capabilities
    user_name, ai_name = names
    base = dict(expression='一起发会儿呆', duration_seconds=300, status='active',
                expected_end_at=400, started_at=100, ended_at=400, actual_ms=300000,
                expires_at=50, decision='accepted')
    texts = [prompts.ability(user_name=user_name,ai_name=ai_name),
             prompts.acceptance(user_name=user_name,ai_name=ai_name)]
    for state in ({}, {'status':'finished','decision':'rejected','reject_reason':'开会'},
                  {'status':'finished','decision':'timed_out'},
                  {'status':'finished','outcome':'completed'},
                  {'status':'finished','outcome':'ended_early','reason':'screen_off'},
                  {'status':'finished','decision':None,'notified_at':1},
                  {'status':'finished','decision':'accepted'}):
        texts.append(prompts.fact({**base,**state},user_name=user_name,ai_name=ai_name,now=150,timezone='UTC'))
    texts.append(prompts.memory({**base,'status':'finished','outcome':'completed'},user_name=user_name,ai_name=ai_name,timezone='UTC'))
    for surface in ('main_stable','opportunity','self_wake','initiative','sentinel_v2'):
        entries=render_registered_capabilities(surface,capabilities={'mobile.look_at_me'},context={'user_name':user_name,'ai_name':ai_name})
        assert entries and user_name in entries[0][1] and ai_name in entries[0][1]
    for text in texts:
        assert ai_name in text
        assert all(generic not in text for generic in ('用户','AI','对方','TA','你','她'))


@pytest.mark.parametrize('names', [(USER_NAME, AI_NAME), ('青禾', '临川')])
def test_autonomous_sticker_abilities_use_runtime_names(names):
    from app.tools.prompt_renderers import render_registered_capabilities
    for surface in ('main_stable', 'opportunity', 'self_wake', 'sentinel_v2', 'schedule'):
        entries = render_registered_capabilities(surface, capabilities={'sticker.send'},
            context={'user_name': names[0], 'ai_name': names[1]})
        assert [key for key, _ in entries] == ['sticker.send']
        text = entries[0][1]
        assert names[0] in text and names[1] in text
        assert all(generic not in text for generic in ('用户', 'AI', '对方', 'TA', '你', '她'))


@pytest.mark.parametrize('names', [(USER_NAME, AI_NAME), ('青禾', '临川')])
def test_human_sticker_and_photo_context_preserve_sender_across_days(names):
    from app.stickers.prompts import history_note
    from app.daily_chat.service import build_cross_day_background
    attachments = ['/uploads/photo.png', {'type': 'sticker', 'url': '/uploads/cat.gif', 'summary': {'visual': '猫猫'}}]
    note = history_note(attachments, user_name=names[0], ai_name=names[1], role='user')
    background = build_cross_day_background([
        {'role': 'user', 'content': '', 'created_at': 1, 'attachments': attachments},
    ], user_name=names[0], ai_name=names[1], timezone='UTC')
    for text in (note, background):
        assert f'{names[0]}向{names[1]}发送的聊天表情' in text
        assert '1 张普通图片' in text
        assert all(generic not in text for generic in ('用户', 'AI', '对方', 'TA', '你', '她'))


@pytest.mark.parametrize('names', [(USER_NAME, AI_NAME), ('青禾', '临川')])
@pytest.mark.parametrize('explicit', [True, False])
def test_sticker_capabilities_resolve_runtime_names(monkeypatch, names, explicit):
    from app.chat import worldbook
    from app.tools.prompt_renderers import render_registered_capabilities
    monkeypatch.setattr(worldbook, 'load_worldbook_names', lambda: names)
    context = {'user_name': names[0], 'ai_name': names[1]} if explicit else {}
    entries = [*render_registered_capabilities('main_stable', capabilities={'sticker.save', 'sticker.send'}, context=context),
               *render_registered_capabilities('opportunity', capabilities={'sticker.create'}, context=context)]
    assert len(entries) == 3
    for _name, text in entries:
        assert names[0] in text and names[1] in text
        assert all(generic not in text for generic in ('用户', 'AI', '对方', 'TA', '你', '她'))


@pytest.mark.parametrize('names', [(USER_NAME, AI_NAME), ('青禾', '临川')])
def test_cross_day_background_resolves_both_names(names):
    from app.daily_chat.service import build_cross_day_background
    text = build_cross_day_background([
        {'role': 'user', 'content': '明天接着聊。', 'created_at': 1},
        {'role': 'assistant', 'content': '好，记着呢。', 'created_at': 2},
    ], user_name=names[0], ai_name=names[1], timezone='Asia/Shanghai')
    assert names[0] in text and names[1] in text
    assert all(generic not in text for generic in ('用户', 'AI', '对方', 'TA'))


@pytest.mark.parametrize("use_context_names", [True, False])
@pytest.mark.parametrize("names", [(USER_NAME, AI_NAME), ("星禾", "知遥")])
def test_time_capability_and_status_names_are_resolved_at_runtime(monkeypatch, use_context_names, names):
    from app.chat import worldbook
    from app.self_wake.service import render_prompt_status
    from app.tools.prompt_renderers import render_registered_capabilities, render_tool_usage_policy

    user_name, ai_name = names
    monkeypatch.setattr(worldbook, "load_worldbook_names", lambda: names)
    context = {
        "self_wake_timezone": "UTC",
        "self_wake_now_local": "2027-01-15T08:00+00:00",
        "self_wake_status": {"pending": None},
    }
    if use_context_names:
        context.update(user_name=user_name, ai_name=ai_name)

    entries = []
    for surface, capabilities in (
        ("main_stable", {"schedule.alarm", "schedule.reminder", "schedule.monitor"}),
        ("main_runtime", {"self_wake.schedule", "self_wake.cancel"}),
        ("opportunity", {"self_wake.schedule", "self_wake.cancel"}),
    ):
        entries.extend(render_registered_capabilities(surface, capabilities=capabilities, context=context))
    policy = render_tool_usage_policy(user_name=user_name, ai_name=ai_name)
    assert policy.startswith(f"【能力使用】你是{ai_name}。")
    for name, text in entries:
        if name != "self_wake.cancel":
            assert user_name in text
        if name != "schedule.alarm":
            assert "你" in text
        for generic in ("用户", "AI", "对方", "TA", "她"):
            assert generic not in text
    # Status is a fact, not a direction: retain its original named subject.
    status = render_prompt_status(context)
    assert ai_name in status
    assert all(generic not in status for generic in ("用户", "AI", "对方", "TA", "你", "她"))


@pytest.mark.parametrize("names", [(USER_NAME, AI_NAME), ("星禾", "知遥")])
@pytest.mark.parametrize("explicit", [True, False])
def test_core_ability_block_binds_second_person_without_rewriting_supplied_facts(monkeypatch, names, explicit):
    from app.chat import worldbook

    user_name, ai_name = names
    monkeypatch.setattr(worldbook, "load_worldbook_names", lambda: names)
    schedule = f"{user_name}说：‘{ai_name}，你明天记得来。’"
    block = build_abilities_block(user_name, schedule, **({"ai_name": ai_name} if explicit else {}))
    assert block.startswith(f"【能力使用】你是{ai_name}。")
    assert block.count(f"你是{ai_name}。") == 1
    assert f"把{user_name}的未来事项记入日程，供你在后续对话中参考" in block
    assert f"【当前日程列表】\n{schedule}" in block
    assert all(generic not in block for generic in ("用户", "AI", "对方", "TA", "她"))


def _assert_relationship_names(text: str) -> None:
    assert USER_NAME in text
    assert "用户" not in text
    assert "AI人设" not in text
    assert "用户信息" not in text


def test_image_review_prompt_and_tool_use_runtime_names():
    from app.image_memory.view import image_followup_prompt
    from app.tools.prompt_renderers import _render_view_image, render_tool_usage_policy

    prompt = image_followup_prompt({"source_time": 1, "source_message_id": "photo", "attachment_url": "/uploads/photo.png"}, user_name=USER_NAME, ai_name=AI_NAME)
    ability = _render_view_image("main_stable", {"image_memory_available": True, "user_name": USER_NAME, "ai_name": AI_NAME})
    for text in (prompt, ability):
        _assert_relationship_names(text)
        assert "对方" not in text and "TA" not in text
    assert AI_NAME in prompt
    assert "你需要核对旧图细节时" in ability
    policy = render_tool_usage_policy(user_name=USER_NAME, ai_name=AI_NAME)
    assert (policy + "\n" + ability).startswith(f"【能力使用】你是{AI_NAME}。")


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
        build_chat_presentation_block(user_name=USER_NAME, ai_name=AI_NAME),
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
