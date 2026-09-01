import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import pytest

import ai_providers
from app.desire import repository as desire_repository
from app.desire.schema import init_desire_tables
from app.desire.service import ensure_desire_root_in_tx
from app.working_model import gate as gate_module
from app.working_model import repository as wm_repository
from app.working_model import runtime
from app.working_model.request_tag import extract_working_model_request
from app.working_model.schema import init_working_model_tables
from app.working_model.service import WORKING_MODEL_ROOT_ID
from app.working_model.writer import (
    WORKING_MODEL_WRITER_PROMPT_VERSION,
    WorkingModelWriterParseError,
    build_working_model_writer_messages,
    build_writer_identity_snapshot,
    parse_working_model_writer_output,
    run_working_model_writer,
    writer_prompt_version,
)
from app.chat.postprocess import PostProcessor
from app.chat import prompt_builder
from app.chat.streaming import _WorkingModelRequestStreamFilter
from config import DEFAULT_AI_BEHAVIOR


async def _with_heartbeat(awaitable):
    async def heartbeat():
        while True:
            await asyncio.sleep(0.001)

    task = asyncio.create_task(heartbeat())
    try:
        return await awaitable
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _run(awaitable):
    return asyncio.run(_with_heartbeat(awaitable))


async def _init_db(path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "CREATE TABLE messages ("
            "id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, role TEXT NOT NULL, "
            "content TEXT NOT NULL, created_at REAL NOT NULL, attachments TEXT DEFAULT '')"
        )
        await db.execute(
            "CREATE TABLE memories ("
            "id TEXT PRIMARY KEY, content TEXT NOT NULL, type TEXT, created_at REAL, "
            "source_conv TEXT, embedding BLOB, keywords TEXT DEFAULT '', importance REAL, "
            "source_start_ts REAL, source_end_ts REAL, unresolved INTEGER DEFAULT 0)"
        )
        await db.execute(
            "CREATE TABLE memory_items ("
            "id TEXT PRIMARY KEY, legacy_memory_id TEXT UNIQUE, "
            "origin_type TEXT NOT NULL DEFAULT 'legacy', "
            "kind TEXT NOT NULL DEFAULT 'episode', "
            "namespace TEXT NOT NULL DEFAULT 'normal', "
            "content TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', "
            "entities_json TEXT NOT NULL DEFAULT '[]', "
            "emotion TEXT NOT NULL DEFAULT '', importance REAL NOT NULL DEFAULT 0.5, "
            "confidence REAL NOT NULL DEFAULT 0.7, "
            "status TEXT NOT NULL DEFAULT 'active', "
            "visibility TEXT NOT NULL DEFAULT 'prompt', embedding BLOB, "
            "keywords_json TEXT NOT NULL DEFAULT '[]', source_conv TEXT, "
            "source_start_ts REAL, source_end_ts REAL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, "
            "last_seen_at REAL, last_used_at REAL, expires_at REAL, "
            "metadata_json TEXT NOT NULL DEFAULT '{}')"
        )
        await db.execute(
            "CREATE TABLE memory_links ("
            "memory_id TEXT, target_id TEXT, target_type TEXT, relation TEXT, created_at REAL, "
            "PRIMARY KEY (memory_id,target_id,target_type,relation))"
        )
        await init_working_model_tables(db)
        await init_desire_tables(db)
        await wm_repository.insert_version(
            db,
            version_id=WORKING_MODEL_ROOT_ID,
            previous_version_id=None,
            content="旧认识",
            created_at=1.0,
            origin_conv_id=None,
            origin_message_id=None,
            origin_request_id=None,
            reason="",
            writer_model="unknown",
            prompt_version="legacy",
            diff_ratio=None,
            flagged=1,
        )
        await ensure_desire_root_in_tx(
            db,
            working_model_id=WORKING_MODEL_ROOT_ID,
            created_at=1.0,
        )
        await db.execute(
            "INSERT INTO messages (id,conv_id,role,content,created_at) VALUES (?,?,?,?,?)",
            ("user-frozen", "conv", "user", "我其实很在意自己做决定。", 2.0),
        )
        await db.execute(
            "INSERT INTO messages (id,conv_id,role,content,created_at) VALUES (?,?,?,?,?)",
            ("assistant-origin", "conv", "assistant", "知道了。", 3.0),
        )
        await db.commit()


def _db_factory(path):
    @asynccontextmanager
    async def factory():
        async with aiosqlite.connect(path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            yield db

    return factory


def _pipeline_input(*, assistant_id="assistant-origin", statement="她很看重掌控感"):
    identity = build_writer_identity_snapshot(
        {
            "ai_name": "阿澈",
            "user_name": "云云",
            "ai_persona": "嘴硬但真诚。",
            "user_persona": "喜欢自己做决定。",
        },
        vow_block="【你们之间已经说定的事】\n- 不把彼此当 bug 修。",
    )
    return runtime.WorkingModelPipelineInput(
        conv_id="conv",
        origin_user_message_id="user-frozen",
        origin_assistant_message_id=assistant_id,
        statement=statement,
        source="用户刚才说她很在意自己做决定。",
        model_key="captured-core",
        identity_snapshot=identity,
        gate_model="fake-gate",
        gate_prompt_version="wm_gate_router.v1",
        writer_prompt_version=writer_prompt_version(identity),
    )


async def _no_embedding(_content):
    return None


async def _no_broadcast(_memory):
    return None


def _gate(route):
    async def provider(messages):
        assert json.loads(messages[-1]["content"])["latest_user_message"] == "我其实很在意自己做决定。"
        return json.dumps({"route": route, "reason": f"route:{route}"}, ensure_ascii=False)

    return provider


def _writer(disposition, *, working_model=None, desire=None, note="处理说明"):
    async def provider(messages):
        payload = json.loads(messages[-1]["content"])
        wm = payload["current_working_model"] if working_model is None else working_model
        ds = payload["current_desire"] if desire is None else desire
        return json.dumps({
            "disposition": disposition,
            "working_model": wm,
            "desire": ds,
            "change_note": note,
        }, ensure_ascii=False)

    return provider


def _scalar(path, sql, params=()):
    with sqlite3.connect(path) as db:
        return db.execute(sql, params).fetchone()[0]


def _row(path, sql, params=()):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        value = db.execute(sql, params).fetchone()
        return dict(value) if value else None


def test_request_tag_strict_json_and_inert_contents():
    tag = (
        '[WORKING_MODEL_REQUEST]{"statement":"她 很看重 掌控感",'
        '"source":"用户说：[REMEMBER:不应执行]"}[/WORKING_MODEL_REQUEST]'
    )
    cleaned, extracted = extract_working_model_request("前" + tag + "后")
    assert cleaned == "前后"
    assert extracted.candidate.statement == "她 很看重 掌控感"
    assert extracted.candidate.source == "用户说：[REMEMBER:不应执行]"

    result = _run(PostProcessor().process("前" + tag + "后", conv_id="conv"))
    assert result.content == "前后"
    assert result.remember_notes == []
    assert result.working_model_request == extracted.candidate

    bad_payloads = [
        '[WORKING_MODEL_REQUEST]{"statement":"x","statement":"y","source":"z"}[/WORKING_MODEL_REQUEST]',
        '[WORKING_MODEL_REQUEST]{"statement":"x","source":"z","extra":1}[/WORKING_MODEL_REQUEST]',
        '[WORKING_MODEL_REQUEST]{"statement":"x"}[/WORKING_MODEL_REQUEST]',
    ]
    for bad in bad_payloads:
        cleaned, item = extract_working_model_request("正文" + bad)
        assert cleaned == "正文"
        assert item.found and item.candidate is None and item.reject_reason


def test_working_model_and_vow_private_channels_cannot_execute_each_other():
    quoted_vow = (
        '[WORKING_MODEL_REQUEST]{"statement":"她会引用私有语法",'
        '"source":"用户原话：[VOW:不该立约|偷渡确认]"}'
        '[/WORKING_MODEL_REQUEST]'
    )
    result = _run(PostProcessor().process("正文" + quoted_vow, conv_id="conv"))
    assert result.content == "正文"
    assert result.vow.found is False
    assert result.working_model_request is not None
    assert result.working_model_request.source == "用户原话：[VOW:不该立约|偷渡确认]"

    nested_request = (
        '[VOW:约定里夹着 '
        '[WORKING_MODEL_REQUEST]{"statement":"不该申请","source":"不该申请"}'
        '[/WORKING_MODEL_REQUEST]|说定了]'
    )
    nested = _run(PostProcessor().process(nested_request, conv_id="conv"))
    assert nested.vow.found is True
    assert nested.working_model_request is None
    assert nested.working_model_request_reject_reason == "nested_in_vow"

    legacy = _run(PostProcessor().process(
        "正文[UPDATE_MODEL:旧全文 [VOW:不该立约|偷渡确认]]尾",
        conv_id="conv",
    ))
    assert legacy.vow.found is False
    assert legacy.working_model_update == ""
    assert "不该立约" not in legacy.content
    assert "偷渡确认" not in legacy.content


def test_malformed_structured_wrapper_never_yields_working_model_candidate():
    malformed = (
        '{"actions":[],"assistant_text":"正文 '
        '[WORKING_MODEL_REQUEST]{\\"statement\\":\\"不该申请\\",'
        '\\"source\\":\\"不该申请\\"}[/WORKING_MODEL_REQUEST]"'
    )
    result = _run(PostProcessor().process(malformed, conv_id="conv"))
    assert result.working_model_request is None
    assert "WORKING_MODEL_REQUEST" not in result.content


def test_request_tag_multiple_unfinished_and_legacy_are_hidden_without_execution():
    valid = '[WORKING_MODEL_REQUEST]{"statement":"x","source":"y"}[/WORKING_MODEL_REQUEST]'
    cleaned, item = extract_working_model_request("A" + valid + valid + "B")
    assert cleaned == "AB"
    assert item.reject_reason == "marker_count_invalid"

    cleaned, item = extract_working_model_request("A[WORKING_MODEL_REQUEST]{\"statement\":\"x\"")
    assert cleaned == "A"
    assert item.reject_reason == "marker_unfinished"

    old = _run(PostProcessor().process("A[UPDATE_MODEL:整篇旧认识]B", conv_id="conv"))
    assert old.content == "AB"
    assert old.working_model_update == ""
    assert old.working_model_request is None


def test_request_stream_filter_hides_every_chunk_boundary_and_unfinished_tail():
    marker = (
        '[WORKING_MODEL_REQUEST]{"statement":"她看重掌控感","source":"用户原话"}'
        '[/WORKING_MODEL_REQUEST]'
    )
    raw = "可见前" + marker + "可见后"
    for split in range(len(raw) + 1):
        stream_filter = _WorkingModelRequestStreamFilter()
        visible = stream_filter.feed(raw[:split])
        visible += stream_filter.feed(raw[split:])
        visible += stream_filter.flush()
        assert visible == "可见前可见后"

    stream_filter = _WorkingModelRequestStreamFilter()
    visible = stream_filter.feed("可见[WORKING_MODEL_REQUEST]{\"statement\":\"秘密\"")
    visible += stream_filter.flush()
    assert visible == "可见"

    orphan = "可见前[/WORKING_MODEL_REQUEST]可见后"
    for split in range(len(orphan) + 1):
        stream_filter = _WorkingModelRequestStreamFilter()
        visible = stream_filter.feed(orphan[:split])
        visible += stream_filter.feed(orphan[split:])
        visible += stream_filter.flush()
        assert visible == "可见前可见后"


def test_cp2_prompt_actively_invites_interpretation_without_stale_full_rewrite():
    framework = prompt_builder.build_thinking_framework_block()
    assert "[WORKING_MODEL_REQUEST]" in framework
    assert '"statement"' in framework and '"source"' in framework
    assert "她是什么样的人" in framework
    assert "整篇认识层" in framework
    assert "[UPDATE_MODEL:" not in framework

    stale = prompt_builder.build_working_model_block({
        "content": "旧认识",
        "updated_at": 0,
    })
    assert stale == "[你对她的当前认识]\n旧认识"
    assert "有一段时间没更新" not in stale


def test_writer_messages_contain_identity_but_no_chat_history_or_journal():
    identity = build_writer_identity_snapshot(
        {"ai_name": "阿澈", "user_name": "云云", "ai_persona": "有自己的主见。"},
        vow_block="【你们之间已经说定的事】\n- 允许彼此犯错。",
    )
    messages = build_working_model_writer_messages(
        identity_snapshot=identity,
        current_working_model="旧认识",
        current_desire="想真诚对她",
        statement="她看重掌控感",
        source="她说想自己拍板",
    )
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "有自己的主见" in serialized
    assert "允许彼此犯错" in serialized
    assert "recent_messages" not in serialized
    assert "conversation" not in serialized.lower()
    assert "journal" not in serialized.lower()
    assert "日记" not in serialized
    assert WORKING_MODEL_WRITER_PROMPT_VERSION == "wm_core_writer.v6"
    # v6：默认保持；只有越过落点判据之后才做整体重估。
    assert "默认落点是不改" in serialized
    assert "判定为 integrated 之后" in serialized
    assert "不要等到字数不够才压缩" in serialized
    assert "粒度判据" in serialized
    assert "8月5日她因为看牙情绪波动" in serialized
    assert "收成模式再写" in serialized
    assert "这是上限不是目标" in serialized
    # 欲望层有独立段落，不再寄生在认识层写作原则下面
    assert "[欲望层维护]" in serialized
    assert "欲望层只能写姿态，不能写行为守则" in serialized
    # v4 给出正反例，并把 v3 实际跑出来的违规文本直接钉成反面样本
    assert "我想成为她能放心说真话的那种人" in serialized
    assert "少纠正，多顺着她当下的方向走" in serialized
    assert "少提醒风险" in serialized
    assert "默认逐字保留" in serialized
    assert "确定要改之后" in serialized
    assert "可以保留、改写或删除旧内容" in serialized
    assert "不单独构成你改变自身欲望的充分理由" in serialized
    assert "这次的强度比上次高" in serialized
    # v3 的“通常保持不变”与“每次整体重估”自相矛盾，v4 已删除
    assert "通常保持不变" not in serialized
    assert [message["role"] for message in messages] == ["system", "user"]


def test_writer_accepts_only_an_exact_json_fence_as_a_compatible_wrapper():
    raw = json.dumps({
        "disposition": "integrated",
        "working_model": "新认识",
        "desire": "真诚靠近她",
        "change_note": "完整重评",
    }, ensure_ascii=False)
    assert parse_working_model_writer_output(f"```json\n{raw}\n```")["desire"] == "真诚靠近她"
    with pytest.raises(WorkingModelWriterParseError, match="invalid_json"):
        parse_working_model_writer_output(f"这是结果：\n{raw}")


def test_writer_shares_one_correction_retry_between_length_and_json_failures():
    identity = build_writer_identity_snapshot({"ai_name": "阿澈"})
    calls = []

    async def over_then_ok(messages):
        calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        if len(calls) == 1:
            return json.dumps({
                "disposition": "integrated",
                "working_model": "长" * 1201,
                "desire": "",
                "change_note": "太长",
            }, ensure_ascii=False)
        assert "validation_feedback" in payload
        return json.dumps({
            "disposition": "integrated",
            "working_model": "压缩后的新认识",
            "desire": "",
            "change_note": "已压缩",
        }, ensure_ascii=False)

    result = _run(run_working_model_writer(
        model_key="fake",
        identity_snapshot=identity,
        current_working_model="旧认识",
        current_desire="",
        statement="新判断",
        source="用户原话",
        provider=over_then_ok,
    ))
    assert result.ok and result.provider_calls == 2

    parse_calls = []

    async def parse_then_ok(messages):
        parse_calls.append(messages)
        if len(parse_calls) == 1:
            return "not json"
        payload = json.loads(messages[-1]["content"])
        assert "严格 JSON 合同" in payload["validation_feedback"]
        return json.dumps({
            "disposition": "integrated",
            "working_model": "纠正后的新认识",
            "desire": "",
            "change_note": "格式已纠正",
        }, ensure_ascii=False)

    corrected = _run(run_working_model_writer(
        model_key="fake",
        identity_snapshot=identity,
        current_working_model="旧认识",
        current_desire="",
        statement="新判断",
        source="用户原话",
        provider=parse_then_ok,
    ))
    assert corrected.ok and corrected.provider_calls == 2
    assert len(parse_calls) == 2

    always_bad_calls = []

    async def always_bad(_messages):
        always_bad_calls.append(1)
        return "not json"

    failed = _run(run_working_model_writer(
        model_key="fake",
        identity_snapshot=identity,
        current_working_model="旧认识",
        current_desire="",
        statement="新判断",
        source="用户原话",
        provider=always_bad,
    ))
    assert failed.failure_code == "parse_failed"
    assert failed.parse_error_code == "invalid_json"
    assert failed.provider_calls == 2
    assert always_bad_calls == [1, 1]


def test_core_writer_transport_resolves_captured_model_and_calls_once(monkeypatch):
    calls = []
    endpoint = {
        "id": "custom-core",
        "name": "Custom Core",
        "type": "openai",
        "base_url": "https://example.invalid/v1",
        "api_key": "secret",
    }
    monkeypatch.setattr(
        ai_providers,
        "resolve_core_model",
        lambda model_key: {
            "_kind": "custom",
            "endpoint": endpoint,
            "model": "resolved-model" if model_key == "captured-core" else "wrong",
        },
    )

    async def fake_single_call(**kwargs):
        calls.append(kwargs)
        return '{"ok":true}'

    monkeypatch.setattr(ai_providers, "_call_endpoint_chat_once", fake_single_call)
    result = _run(ai_providers.call_core_chat_once(
        "captured-core",
        [{"role": "system", "content": "身份"}, {"role": "user", "content": "申请"}],
        expect_json=True,
        temperature=0.2,
        max_tokens=2400,
    ))
    assert result == '{"ok":true}'
    assert len(calls) == 1
    assert calls[0]["endpoint"] == endpoint
    assert calls[0]["model"] == "resolved-model"
    assert calls[0]["expect_json"] is True


def test_cp2_schema_upgrade_adds_writer_audit_columns_to_cp0_request_table(tmp_path):
    db_path = tmp_path / "cp0-upgrade.db"

    async def upgrade():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE working_model_requests (
                    id TEXT PRIMARY KEY,
                    conv_id TEXT,
                    origin_user_message_id TEXT,
                    origin_assistant_message_id TEXT,
                    statement TEXT NOT NULL,
                    source TEXT NOT NULL,
                    route TEXT,
                    gate_reason TEXT,
                    gate_model TEXT,
                    gate_prompt_version TEXT,
                    disposition TEXT,
                    writer_model TEXT,
                    writer_prompt_version TEXT,
                    resulting_memory_id TEXT,
                    status TEXT NOT NULL,
                    failure_code TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            await init_working_model_tables(db)
            await init_working_model_tables(db)
            columns = {
                row[1]
                for row in await (await db.execute(
                    "PRAGMA table_info(working_model_requests)"
                )).fetchall()
            }
            await db.commit()
            return columns

    columns = _run(upgrade())
    assert "writer_change_note" in columns
    assert "parse_error_code" in columns


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "routed", "route": "reject", "disposition": None, "resulting_memory_id": None, "failure_code": None},
        {"status": "routed", "route": "memory", "disposition": None, "resulting_memory_id": "mem", "failure_code": None},
        {"status": "routed", "route": "working_model", "disposition": "memory", "resulting_memory_id": "mem", "failure_code": None},
        {"status": "applied", "route": "working_model", "disposition": "integrated", "resulting_memory_id": None, "failure_code": None},
        {"status": "writer_noop", "route": "working_model", "disposition": "noop", "resulting_memory_id": None, "failure_code": None},
        {"status": "failed", "route": None, "disposition": None, "resulting_memory_id": None, "failure_code": "provider_failed"},
    ],
)
def test_terminal_request_truth_table_accepts_only_declared_outcomes(kwargs):
    runtime.validate_terminal_request_outcome(**kwargs)


def test_terminal_request_truth_table_separates_semantic_noop_from_failure():
    with pytest.raises(ValueError):
        runtime.validate_terminal_request_outcome(
            status="writer_noop",
            route="working_model",
            disposition="noop",
            resulting_memory_id=None,
            failure_code="provider_failed",
        )
    with pytest.raises(ValueError):
        runtime.validate_terminal_request_outcome(
            status="failed",
            route="working_model",
            disposition="noop",
            resulting_memory_id=None,
            failure_code="provider_failed",
        )


def test_terminal_request_truth_table_scopes_parse_detail_to_parse_failure():
    runtime.validate_terminal_request_outcome(
        status="failed",
        route="working_model",
        disposition=None,
        resulting_memory_id=None,
        failure_code="parse_failed",
        parse_error_code="wrong_fields",
    )
    with pytest.raises(ValueError, match="parse error detail"):
        runtime.validate_terminal_request_outcome(
            status="failed",
            route="working_model",
            disposition=None,
            resulting_memory_id=None,
            failure_code="provider_failed",
            parse_error_code="wrong_fields",
        )


def test_gate_reject_and_failures_are_audited_without_side_effects(tmp_path):
    db_path = tmp_path / "reject.db"
    _run(_init_db(db_path))
    factory = _db_factory(db_path)
    writer_calls = []

    async def writer(_messages):
        writer_calls.append(1)
        return "{}"

    result = _run(runtime.run_working_model_pipeline(
        _pipeline_input(),
        db_factory=factory,
        gate_provider=_gate("reject"),
        writer_provider=writer,
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    assert result["request"]["status"] == "routed"
    assert result["request"]["route"] == "reject"
    assert result["request"]["writer_model"] is None
    assert writer_calls == []
    assert _scalar(db_path, "SELECT COUNT(*) FROM memories") == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM working_model_versions") == 1

    db_path2 = tmp_path / "gate-failed.db"
    _run(_init_db(db_path2))
    failed = _run(runtime.run_working_model_pipeline(
        _pipeline_input(),
        db_factory=_db_factory(db_path2),
        gate_provider=lambda _messages: "not json",
        writer_provider=writer,
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    assert failed["request"]["status"] == "failed"
    assert failed["request"]["failure_code"] == "parse_failed"
    assert _scalar(db_path2, "SELECT COUNT(*) FROM memories") == 0
    assert _scalar(db_path2, "SELECT COUNT(*) FROM working_model_versions") == 1


def test_captured_gate_model_is_both_called_and_persisted_after_slot_switch(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "gate-slot-switch.db"
    _run(_init_db(db_path))
    identity = build_writer_identity_snapshot({"ai_name": "阿澈", "user_name": "云云"})
    monkeypatch.setattr(runtime, "get_slot", lambda _name: {
        "model": "captured-gate-model",
        "endpoint": {"id": "old"},
        "extras": {},
    })
    value = runtime.capture_working_model_pipeline_input(
        conv_id="conv",
        origin_user_message_id="user-frozen",
        origin_assistant_message_id="assistant-origin",
        statement="她很看重掌控感",
        source="用户刚才说她很在意自己做决定。",
        model_key="captured-core",
        identity_snapshot=identity,
    )
    assert value.gate_model == "captured-gate-model"

    monkeypatch.setattr(gate_module, "get_slot", lambda _name: {
        "model": "new-live-gate-model",
        "endpoint": {"id": "new"},
        "extras": {},
    })
    called = []

    async def fake_call_slot_chat(_slot_name, *, model_override=None, **_kwargs):
        called.append(model_override)
        return json.dumps(
            {"route": "working_model", "reason": "是跨情境解读。"},
            ensure_ascii=False,
        )

    monkeypatch.setattr(gate_module, "call_slot_chat", fake_call_slot_chat)
    result = _run(runtime.run_working_model_pipeline(
        value,
        db_factory=_db_factory(db_path),
        writer_provider=_writer("integrated", working_model="新认识"),
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))

    assert result["request"]["status"] == "applied"
    assert called == ["captured-gate-model"]
    assert result["request"]["gate_model"] == "captured-gate-model"
    assert _scalar(
        db_path,
        "SELECT COUNT(*) FROM working_model_requests WHERE gate_model=?",
        ("captured-gate-model",),
    ) == 1


@pytest.mark.parametrize(
    "mode,expected_failure,expected_calls",
    [
        ("provider", "provider_failed", 1),
        ("parse", "parse_failed", 2),
        ("over_budget", "validation_failed", 2),
    ],
)
def test_writer_technical_failures_are_audited_without_partial_writes(
    tmp_path,
    mode,
    expected_failure,
    expected_calls,
):
    db_path = tmp_path / f"writer-{mode}.db"
    _run(_init_db(db_path))
    calls = []

    async def failing_writer(_messages):
        calls.append(1)
        if mode == "provider":
            raise RuntimeError("provider down")
        if mode == "parse":
            return "not json"
        return json.dumps({
            "disposition": "integrated",
            "working_model": "长" * 1201,
            "desire": "",
            "change_note": "仍然太长",
        }, ensure_ascii=False)

    result = _run(runtime.run_working_model_pipeline(
        _pipeline_input(),
        db_factory=_db_factory(db_path),
        gate_provider=_gate("working_model"),
        writer_provider=failing_writer,
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    assert calls == [1] * expected_calls
    assert result["request"]["status"] == "failed"
    assert result["request"]["failure_code"] == expected_failure
    assert result["request"]["parse_error_code"] == (
        "invalid_json" if mode == "parse" else None
    )
    assert result["request"]["disposition"] is None
    assert _scalar(db_path, "SELECT COUNT(*) FROM working_model_versions") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM desire_versions") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM memories") == 0


def test_gate_memory_route_is_atomic_bidirectional_and_ai_authored(tmp_path, monkeypatch):
    import importlib

    hybrid_recall = importlib.import_module("app.memory_v2.hybrid_recall")

    invalidations = []
    monkeypatch.setattr(
        hybrid_recall,
        "invalidate_full_corpus_cache",
        lambda **kwargs: invalidations.append(kwargs),
    )
    db_path = tmp_path / "memory.db"
    _run(_init_db(db_path))
    value = _pipeline_input()
    result = _run(runtime.run_working_model_pipeline(
        value,
        db_factory=_db_factory(db_path),
        gate_provider=_gate("memory"),
        writer_provider=_writer("noop"),
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    request = result["request"]
    assert request["status"] == "routed"
    assert request["route"] == "memory"
    assert request["disposition"] is None
    assert request["origin_user_message_id"] == "user-frozen"
    assert request["origin_assistant_message_id"] == "assistant-origin"
    assert request["resulting_memory_id"]
    memory = _row(db_path, "SELECT * FROM memories WHERE id=?", (request["resulting_memory_id"],))
    assert memory["type"] == "ai_note"
    mirror = _row(
        db_path,
        "SELECT origin_type,subject,entities_json,metadata_json "
        "FROM memory_items WHERE legacy_memory_id=?",
        (memory["id"],),
    )
    metadata = json.loads(mirror["metadata_json"])
    assert mirror["origin_type"] == "ai_note"
    assert mirror["subject"] == ""
    assert mirror["entities_json"] == "[]"
    assert metadata["origin_request_id"] == request["id"]
    assert metadata["authored_by"] == "assistant"
    assert metadata["source_identity"] == "assistant_interpretation"
    assert invalidations == [{"notes": True}]

    replay = _run(runtime.run_working_model_pipeline(
        value,
        db_factory=_db_factory(db_path),
        gate_provider=lambda _messages: pytest.fail("gate must not replay"),
        writer_provider=lambda _messages: pytest.fail("writer must not replay"),
        memory_prepare=lambda _content: pytest.fail("embedding must not replay"),
        memory_broadcast=_no_broadcast,
    ))
    assert replay["deduplicated"] is True
    assert invalidations == [{"notes": True}]
    assert _scalar(db_path, "SELECT COUNT(*) FROM memories") == 1


def test_interrupted_processing_request_is_not_auto_replayed(tmp_path):
    db_path = tmp_path / "interrupted.db"
    _run(_init_db(db_path))
    value = _pipeline_input()
    request_id = runtime.stable_working_model_request_id(value)

    async def insert_interrupted():
        async with aiosqlite.connect(db_path) as db:
            await wm_repository.insert_request(
                db,
                request_id=request_id,
                conv_id=value.conv_id,
                origin_user_message_id=value.origin_user_message_id,
                origin_assistant_message_id=value.origin_assistant_message_id,
                statement=value.statement,
                source=value.source,
                status=runtime.REQUEST_STATUS_PROCESSING,
                created_at=4.0,
                gate_model=value.gate_model,
                gate_prompt_version=value.gate_prompt_version,
            )
            await db.commit()

    _run(insert_interrupted())
    result = _run(runtime.run_working_model_pipeline(
        value,
        db_factory=_db_factory(db_path),
        gate_provider=lambda _messages: pytest.fail("gate must not replay"),
        writer_provider=lambda _messages: pytest.fail("writer must not replay"),
        memory_prepare=lambda _content: pytest.fail("embedding must not replay"),
        memory_broadcast=_no_broadcast,
    ))
    assert result["deduplicated"] is True
    assert result["request"]["status"] == runtime.REQUEST_STATUS_PROCESSING
    assert _scalar(db_path, "SELECT COUNT(*) FROM working_model_versions") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM memories") == 0


@pytest.mark.parametrize("desire_text,expected_desire_versions", [("", 1), ("想更尊重她的决定", 2)])
def test_integrated_writer_persists_one_chain_diff_and_optional_desire(
    tmp_path, desire_text, expected_desire_versions,
):
    db_path = tmp_path / f"integrated-{expected_desire_versions}.db"
    _run(_init_db(db_path))
    result = _run(runtime.run_working_model_pipeline(
        _pipeline_input(),
        db_factory=_db_factory(db_path),
        gate_provider=_gate("working_model"),
        writer_provider=_writer(
            "integrated",
            working_model="她在选择工具和安排事情时很看重掌控感。",
            desire=desire_text,
            note="把具体偏好收成掌控感模式",
        ),
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    request = result["request"]
    assert request["status"] == "applied"
    assert request["disposition"] == "integrated"
    assert request["writer_model"] == "captured-core"
    assert request["writer_change_note"] == "把具体偏好收成掌控感模式"
    assert _scalar(db_path, "SELECT COUNT(*) FROM working_model_versions") == 2
    assert _scalar(db_path, "SELECT COUNT(*) FROM desire_versions") == expected_desire_versions
    version = _row(
        db_path,
        "SELECT * FROM working_model_versions WHERE origin_request_id=?",
        (request["id"],),
    )
    assert version["origin_message_id"] == "assistant-origin"
    assert version["diff_ratio"] == runtime.working_model_diff_ratio(
        "旧认识", "她在选择工具和安排事情时很看重掌控感。"
    )
    assert version["flagged"] == 0
    assert _row(db_path, "SELECT diff_ratio FROM working_model_versions WHERE id=?", (WORKING_MODEL_ROOT_ID,))["diff_ratio"] is None

    replay = _run(runtime.run_working_model_pipeline(
        _pipeline_input(),
        db_factory=_db_factory(db_path),
        gate_provider=lambda _messages: pytest.fail("gate must not replay"),
        writer_provider=lambda _messages: pytest.fail("writer must not replay"),
        memory_prepare=lambda _content: pytest.fail("embedding must not replay"),
        memory_broadcast=_no_broadcast,
    ))
    assert replay["deduplicated"] is True
    assert _scalar(db_path, "SELECT COUNT(*) FROM working_model_versions") == 2
    assert _scalar(db_path, "SELECT COUNT(*) FROM desire_versions") == expected_desire_versions


@pytest.mark.parametrize("disposition,status", [("noop", "writer_noop"), ("memory", "routed")])
def test_writer_nonintegrated_dispositions_never_change_desire(tmp_path, disposition, status):
    db_path = tmp_path / f"{disposition}.db"
    _run(_init_db(db_path))
    result = _run(runtime.run_working_model_pipeline(
        _pipeline_input(),
        db_factory=_db_factory(db_path),
        gate_provider=_gate("working_model"),
        writer_provider=_writer(disposition, note=f"writer:{disposition}"),
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    assert result["request"]["status"] == status
    assert result["request"]["writer_change_note"] == f"writer:{disposition}"
    assert _scalar(db_path, "SELECT COUNT(*) FROM working_model_versions") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM desire_versions") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM memories") == (1 if disposition == "memory" else 0)
    if disposition == "memory":
        request = result["request"]
        mirror = _row(
            db_path,
            "SELECT metadata_json FROM memory_items WHERE legacy_memory_id=?",
            (request["resulting_memory_id"],),
        )
        metadata = json.loads(mirror["metadata_json"])
        assert metadata["origin_request_id"] == request["id"]
        assert metadata["authored_by"] == "assistant"
        assert metadata["source_identity"] == "assistant_interpretation"


def test_missing_frozen_user_message_fails_before_gate(tmp_path):
    db_path = tmp_path / "missing.db"
    _run(_init_db(db_path))
    value = _pipeline_input()
    value = runtime.WorkingModelPipelineInput(
        **{**value.to_dict(), "origin_user_message_id": "missing-user"}
    )
    calls = []
    result = _run(runtime.run_working_model_pipeline(
        value,
        db_factory=_db_factory(db_path),
        gate_provider=lambda _messages: calls.append(1),
        writer_provider=_writer("noop"),
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    assert result["request"]["status"] == "failed"
    assert result["request"]["failure_code"] == "origin_user_message_missing"
    assert calls == []


def test_missing_frozen_assistant_message_fails_before_gate(tmp_path):
    db_path = tmp_path / "missing-assistant.db"
    _run(_init_db(db_path))
    value = _pipeline_input(assistant_id="missing-assistant")
    calls = []
    result = _run(runtime.run_working_model_pipeline(
        value,
        db_factory=_db_factory(db_path),
        gate_provider=lambda _messages: calls.append(1),
        writer_provider=_writer("noop"),
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    assert result["request"]["status"] == "failed"
    assert result["request"]["failure_code"] == "origin_assistant_message_missing"
    assert calls == []


def test_memory_failure_rolls_back_memory_and_marks_request_failed(tmp_path):
    db_path = tmp_path / "memory-rollback.db"
    _run(_init_db(db_path))

    async def broken_insert(db, **kwargs):
        await db.execute(
            "INSERT INTO memories (id,content,type,created_at) VALUES (?,?,?,?)",
            (kwargs["memory_id"], kwargs["content"], "ai_note", kwargs["created_at"]),
        )
        raise RuntimeError("fault after memory insert")

    result = _run(runtime.run_working_model_pipeline(
        _pipeline_input(),
        db_factory=_db_factory(db_path),
        gate_provider=_gate("memory"),
        memory_prepare=_no_embedding,
        memory_insert_in_tx=broken_insert,
        memory_broadcast=_no_broadcast,
    ))
    assert result["request"]["status"] == "failed"
    assert result["request"]["failure_code"] == "memory_write_failed"
    assert _scalar(db_path, "SELECT COUNT(*) FROM memories") == 0


def test_desire_failure_rolls_back_working_model_and_marks_failed(tmp_path, monkeypatch):
    db_path = tmp_path / "desire-rollback.db"
    _run(_init_db(db_path))

    async def broken_desire(*_args, **_kwargs):
        raise RuntimeError("desire fault")

    monkeypatch.setattr(runtime.desire_repository, "insert_version", broken_desire)
    result = _run(runtime.run_working_model_pipeline(
        _pipeline_input(),
        db_factory=_db_factory(db_path),
        gate_provider=_gate("working_model"),
        writer_provider=_writer(
            "integrated",
            working_model="新认识",
            desire="新欲望",
        ),
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    assert result["request"]["status"] == "failed"
    assert result["request"]["failure_code"] == "version_write_failed"
    assert _scalar(db_path, "SELECT COUNT(*) FROM working_model_versions") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM desire_versions") == 1


def test_two_requests_reading_same_head_rerun_writer_without_fork(tmp_path):
    db_path = tmp_path / "concurrent.db"
    _run(_init_db(db_path))
    async def add_second_assistant():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO messages (id,conv_id,role,content,created_at) VALUES (?,?,?,?,?)",
                ("assistant-2", "conv", "assistant", "第二条", 4.0),
            )
            await db.commit()
    _run(add_second_assistant())

    first_round_ready = asyncio.Event()
    first_round_count = 0
    lock = asyncio.Lock()
    writer_calls = []

    async def concurrent_writer(messages):
        nonlocal first_round_count
        payload = json.loads(messages[-1]["content"])
        statement = payload["statement"]
        writer_calls.append((statement, payload["current_working_model"]))
        async with lock:
            if first_round_count < 2:
                first_round_count += 1
                if first_round_count == 2:
                    first_round_ready.set()
                wait_for_peer = True
            else:
                wait_for_peer = False
        if wait_for_peer:
            await first_round_ready.wait()
        return json.dumps({
            "disposition": "integrated",
            "working_model": payload["current_working_model"] + "|" + statement,
            "desire": payload["current_desire"],
            "change_note": "并发写入",
        }, ensure_ascii=False)

    async def exercise():
        one = _pipeline_input(statement="判断一")
        two = _pipeline_input(assistant_id="assistant-2", statement="判断二")
        return await asyncio.gather(
            runtime.run_working_model_pipeline(
                one,
                db_factory=_db_factory(db_path),
                gate_provider=_gate("working_model"),
                writer_provider=concurrent_writer,
                memory_prepare=_no_embedding,
                memory_broadcast=_no_broadcast,
            ),
            runtime.run_working_model_pipeline(
                two,
                db_factory=_db_factory(db_path),
                gate_provider=_gate("working_model"),
                writer_provider=concurrent_writer,
                memory_prepare=_no_embedding,
                memory_broadcast=_no_broadcast,
            ),
        )

    results = _run(exercise())
    assert [item["request"]["status"] for item in results] == ["applied", "applied"]
    assert len(writer_calls) == 3
    assert _scalar(db_path, "SELECT COUNT(*) FROM working_model_versions") == 3
    with sqlite3.connect(db_path) as db:
        forks = db.execute(
            "SELECT previous_version_id,COUNT(*) FROM working_model_versions "
            "WHERE previous_version_id IS NOT NULL GROUP BY previous_version_id HAVING COUNT(*)>1"
        ).fetchall()
    assert forks == []
    head = _row(
        db_path,
        "SELECT content FROM working_model_versions current WHERE NOT EXISTS ("
        "SELECT 1 FROM working_model_versions child WHERE child.previous_version_id=current.id)",
    )["content"]
    assert "判断一" in head and "判断二" in head


def test_writer_uses_captured_core_model_key_after_later_switch(tmp_path, monkeypatch):
    db_path = tmp_path / "captured-model.db"
    _run(_init_db(db_path))
    value = _pipeline_input()
    observed_model_keys = []
    original_writer = runtime.run_working_model_writer

    async def spy_writer(**kwargs):
        observed_model_keys.append(kwargs["model_key"])
        return await original_writer(**kwargs)

    monkeypatch.setattr(runtime, "run_working_model_writer", spy_writer)
    # This represents the conversation switching models after scheduling.  The
    # background input remains immutable and the runtime has no late model read.
    current_conversation_model = "different-core"
    assert current_conversation_model != value.model_key
    result = _run(runtime.run_working_model_pipeline(
        value,
        db_factory=_db_factory(db_path),
        gate_provider=_gate("working_model"),
        writer_provider=_writer("noop"),
        memory_prepare=_no_embedding,
        memory_broadcast=_no_broadcast,
    ))
    assert observed_model_keys == ["captured-core"]
    assert result["request"]["writer_model"] == "captured-core"


def test_flags_are_independent_and_default_closed(monkeypatch):
    assert DEFAULT_AI_BEHAVIOR["working_model_v2_write_enabled"] is False
    assert DEFAULT_AI_BEHAVIOR["working_model_v2_injection_enabled"] is False
    assert DEFAULT_AI_BEHAVIOR["opportunity_enabled"] is False
    assert DEFAULT_AI_BEHAVIOR["working_model_reflection_enabled"] is False
    assert DEFAULT_AI_BEHAVIOR["opportunity_intervals_min"] == [21, 34, 55, 89]
    monkeypatch.setattr(runtime, "load_ai_behavior", lambda: {})
    assert runtime.working_model_v2_write_enabled() is False
    assert runtime.working_model_v2_injection_enabled() is False
    monkeypatch.setattr(runtime, "load_ai_behavior", lambda: {
        "working_model_v2_write_enabled": True,
        "working_model_v2_injection_enabled": False,
    })
    assert runtime.working_model_v2_write_enabled() is True
    assert runtime.working_model_v2_injection_enabled() is False
