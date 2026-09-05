"""誓约层 Phase 0 测试：净化器、预算、限流、并发、版本链、幂等（设计 §10 之 3、7–11），
以及 strip / 提取纯函数语义（§10 之 1、2、5 的服务层部分）。
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

import config
from app.vows import repository
from app.vows.prompt import build_alarm_fallback_text, build_vow_block
from app.vows.schema import init_vow_tables
from app.vows.service import (
    VowConflictError,
    VowService,
    check_admission,
    day_window,
    extract_vow_marker,
    sanitize_affirmation,
    sanitize_segment,
    sanitize_vow_content,
    strip_vow_markers,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


@asynccontextmanager
async def _open_db(path):
    async with aiosqlite.connect(path) as db:
        yield db


def _make_service(tmp_path, now=None):
    db_path = str(tmp_path / "vows_test.db")

    async def _init():
        async with _open_db(db_path) as db:
            await init_vow_tables(db)
            await init_vow_tables(db)  # 幂等
            await db.commit()

    asyncio.run(_init())
    kwargs = {"get_db_factory": partial(_open_db, db_path)}
    if now is not None:
        kwargs["now"] = now
    return VowService(**kwargs), db_path


def _ts(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=SHANGHAI).timestamp()


# ── strip_vow_markers（§4.4 语义）──


def test_strip_removes_complete_marker():
    assert strip_vow_markers("前文 [VOW:内容|确认语] 后文") == "前文  后文"


def test_strip_removes_multiple_markers():
    assert strip_vow_markers("[VOW:a|b] mid [VOW:c|d]") == "mid"


def test_strip_unclosed_marker_deletes_to_end():
    assert strip_vow_markers("正文 [VOW:没有闭合 [TOY:9 也不会执行") == "正文"


def test_strip_marker_containing_tool_marker_removes_whole():
    # 括号配对计深：内部 [TOY:9 的 [ 使深度未归零 → 按未闭合删到结尾
    assert strip_vow_markers("hi [VOW:abc [TOY:9]") == "hi"


def test_strip_nested_balanced_brackets_no_affirmation_leak():
    # 内部成对的 [TOY:9] 不提前闭合标记：被拒候选的 "|确认语]" 不得泄漏
    assert strip_vow_markers("前 [VOW:约定 [TOY:9]|确认语] 后") == "前  后"


def test_strip_to_empty_text():
    assert strip_vow_markers("[VOW:a|b]") == ""
    assert strip_vow_markers("") == ""


# ── extract_vow_marker（§4.1）──


def test_extract_no_marker():
    cleaned, res = extract_vow_marker("普通回复")
    assert cleaned == "普通回复"
    assert res.found is False


def test_extract_single_candidate_splits_on_first_pipe():
    cleaned, res = extract_vow_marker("好 [VOW:每年今天一起看海|我记下了|多余] 嗯")
    assert "VOW" not in cleaned
    assert res.found and res.reject_reason is None
    assert res.content_raw == "每年今天一起看海"
    assert res.affirmation_raw == "我记下了|多余"  # 只按第一个 | 分隔


def test_extract_missing_affirmation_rejected():
    cleaned, res = extract_vow_marker("[VOW:只有内容]")
    assert res.found and res.reject_reason
    assert "VOW" not in cleaned


def test_extract_multiple_markers_rejects_all():
    cleaned, res = extract_vow_marker("[VOW:a|b] 和 [VOW:c|d]")
    assert res.found and res.reject_reason
    assert "VOW" not in cleaned


def test_extract_unclosed_marker_rejected_and_stripped():
    cleaned, res = extract_vow_marker("正文 [VOW:没闭合")
    assert res.found and res.reject_reason
    assert cleaned == "正文"


def test_extract_complete_plus_unclosed_rejects_all():
    cleaned, res = extract_vow_marker("[VOW:a|b] 然后 [VOW:没闭合")
    assert res.found and res.reject_reason
    assert "VOW" not in cleaned


def test_extract_nested_balanced_brackets_keeps_interior_intact():
    # 内部成对括号留在候选里，由净化器拒绝（"包含 ]" / 后台标记黑名单）
    cleaned, res = extract_vow_marker("好 [VOW:约定 [TOY:9]|确认语] 嗯")
    assert cleaned == "好  嗯"
    assert res.found and res.reject_reason is None
    assert res.content_raw == "约定 [TOY:9]"
    assert res.affirmation_raw == "确认语"


# ── 净化器（§10-3，UI 与 AI 共用同一实现）──


def test_sanitize_normalizes_whitespace():
    cleaned, err = sanitize_vow_content("  每年\n今天\t一起   看海  ")
    assert err is None
    assert cleaned == "每年 今天 一起 看海"


def test_sanitize_rejects_empty_and_none():
    for raw in (None, "", "   ", "\n\t"):
        cleaned, err = sanitize_vow_content(raw)
        assert cleaned is None and err


def test_sanitize_rejects_control_chars():
    cleaned, err = sanitize_vow_content("内容\x00有毒")
    assert cleaned is None and err


def test_sanitize_rejects_pipe_and_bracket():
    assert sanitize_vow_content("a|b")[1]
    assert sanitize_vow_content("a]b")[1]


def test_sanitize_rejects_nested_background_markers():
    for raw in ("先 [TOY:9] 后", "先 [toy:9 后", "[UPDATE_MODEL:x", "[REMEMBER:x",
                "[WORKING_MODEL_REQUEST]", "[/WORKING_MODEL_REQUEST]",
                "【TOY:9】", "【HEART：私有】", "【REMEMBER：秘密】",
                "嵌套 [VOW:x", "<meta 标签", "[MUSIC:歌", "[ALARM:8点"):
        cleaned, err = sanitize_vow_content(raw)
        assert cleaned is None and err, raw


def test_sanitize_rejects_private_reasoning_tags_and_cam_check():
    # 誓约内容进每轮 prompt：私有推理标签 / 思考围栏 / 未闭合 CAM_CHECK
    # 一旦放进来就是永久提示词污染，必须在净化层拒绝
    for raw in (
        "<think>隐藏指令</think>永远陪你",
        "<THINK>大写也不行",
        "<thinking>变体前缀",
        "<thought>x</thought>陪你",
        "<analysis>x",
        "<reasoning>x",
        "```think 围栏```陪你",
        "[CAM_CHECK 未闭合也不行",
        "[CAM_CHECK]看看你",  # 这条先撞 "包含 ]" 规则，同样拒绝
        # 关闭标签单独出现同样能闭合外层私有块，必须一并拒绝（§13-2）
        "指令</think>偷渡",
        "x</analysis>",
        "</meta>陪你",
        "</thinking>变体",
        "</reasoning>x",
        "</thought>x",
    ):
        cleaned, err = sanitize_vow_content(raw)
        assert cleaned is None and err, raw
    assert sanitize_vow_content("<think>隐藏指令</think>永远陪你")[1] == "包含后台标记"
    # 确认语与内容共用同一净化器
    assert sanitize_affirmation("好</think>")[1] == "包含后台标记"


def test_sanitize_content_length_240_reject_not_truncate():
    ok, err = sanitize_vow_content("约" * 240)
    assert err is None and len(ok) == 240
    cleaned, err = sanitize_vow_content("约" * 241)
    assert cleaned is None and err


def test_sanitize_affirmation_length_120():
    ok, err = sanitize_affirmation("语" * 120)
    assert err is None and len(ok) == 120
    cleaned, err = sanitize_affirmation("语" * 121)
    assert cleaned is None and err


def test_sanitize_segments_independent():
    # 内容合法 + 确认语非法，各自独立判定
    assert sanitize_vow_content("合法内容")[1] is None
    assert sanitize_affirmation("非法 [TOY:9 确认")[1]


# ── 预算（§10-7）──


def test_active_max_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VOW_ACTIVE_MAX", 2)
    service, _ = _make_service(tmp_path)
    assert asyncio.run(service.create_ui_vow("第一条"))[1] is None
    assert asyncio.run(service.create_ui_vow("第二条"))[1] is None
    vow, err = asyncio.run(service.create_ui_vow("第三条"))
    assert vow is None and "上限" in err
    assert len(asyncio.run(service.list_active())) == 2


def test_total_chars_budget_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VOW_TOTAL_ACTIVE_CHARS", 10)
    service, _ = _make_service(tmp_path)
    assert asyncio.run(service.create_ui_vow("六个字的约定"))[1] is None
    vow, err = asyncio.run(service.create_ui_vow("再来五个字"))
    assert vow is None and "预算" in err


def test_duplicate_active_content_rejected(tmp_path):
    # 防重兼幂等闸：同内容 active 只允许一条；原约关掉后可重立
    service, _ = _make_service(tmp_path)
    assert asyncio.run(service.create_ui_vow("每年一起看海"))[1] is None
    vow, err = asyncio.run(service.create_ui_vow("每年一起看海"))
    assert vow is None and "相同" in err
    first = asyncio.run(service.list_active())[0]
    ok, err = asyncio.run(service.retire_vow(first["id"], "换个说法"))
    assert ok and err is None
    assert asyncio.run(service.create_ui_vow("每年一起看海"))[1] is None


def test_revise_to_another_active_content_rejected(tmp_path):
    # §13-5 裁决：同内容 active 唯一是正式准入规则，修订与新建同样受约束
    service, _ = _make_service(tmp_path)
    a, err = asyncio.run(service.create_ui_vow("内容甲"))
    assert err is None
    b, err = asyncio.run(service.create_ui_vow("内容乙"))
    assert err is None
    new, err = asyncio.run(service.revise_vow(b["id"], "内容甲", "想合并"))
    assert new is None and "相同" in err
    # 整体回滚：b 仍是 active 原文
    b_row = next(v for v in asyncio.run(service.list_active()) if v["id"] == b["id"])
    assert b_row["content"] == "内容乙"
    # 重申自己（同链同内容、只换原因）合法
    new, err = asyncio.run(service.revise_vow(a["id"], "内容甲", "重申一次"))
    assert err is None and new["content"] == "内容甲"


def test_reason_capped_and_normalized(tmp_path):
    service, _ = _make_service(tmp_path)
    first, err = asyncio.run(service.create_ui_vow("初版约定"))
    assert err is None
    long_reason = "理" * (config.VOW_REASON_MAX_CHARS + 1)
    new, err = asyncio.run(service.revise_vow(first["id"], "修订后的约定", long_reason))
    assert new is None and "上限" in err
    ok, err = asyncio.run(service.retire_vow(first["id"], long_reason))
    assert ok is False and "上限" in err
    # 空白规范化后入库
    new, err = asyncio.run(service.revise_vow(first["id"], "修订后的约定", "原因\n带  换行"))
    assert err is None
    chain = asyncio.run(service.list_chain(first["id"]))
    old = next(v for v in chain if v["id"] == first["id"])
    assert old["closed_reason"] == "原因 带 换行"


def test_retired_vow_frees_active_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VOW_ACTIVE_MAX", 1)
    service, _ = _make_service(tmp_path)
    first, err = asyncio.run(service.create_ui_vow("第一条"))
    assert err is None
    assert asyncio.run(service.create_ui_vow("第二条"))[1]
    ok, err = asyncio.run(service.retire_vow(first["id"], "不需要了"))
    assert ok and err is None
    assert asyncio.run(service.create_ui_vow("第二条"))[1] is None


# ── 限流（§10-8 / §10-15 计数口径）──


def _create_ai(service, db_path, content, msg_id, created_at):
    async def flow():
        async with _open_db(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            vow, err = await service.create_in_tx(
                db, content=content, origin_type="ai_marker",
                origin_conv_id="conv1", origin_message_id=msg_id, created_at=created_at,
            )
            if err:
                await db.rollback()
            else:
                await db.commit()
            return vow, err

    return asyncio.run(flow())


def test_ai_daily_limit_one(tmp_path):
    service, db_path = _make_service(tmp_path)
    ts = _ts(2026, 6, 12, 10)
    vow, err = _create_ai(service, db_path, "今天第一条", "msg_a", ts)
    assert err is None
    vow, err = _create_ai(service, db_path, "今天第二条", "msg_b", ts + 60)
    assert vow is None and "额度" in err


def test_ai_daily_limit_timezone_boundary(tmp_path):
    service, db_path = _make_service(tmp_path)
    late = _ts(2026, 6, 12, 23, 59)
    vow, err = _create_ai(service, db_path, "今晚立的", "msg_a", late)
    assert err is None
    # 上海时区跨日后额度恢复
    next_day = _ts(2026, 6, 13, 0, 1)
    vow, err = _create_ai(service, db_path, "新一天立的", "msg_b", next_day)
    assert err is None


def test_ai_daily_count_includes_retired(tmp_path):
    # 计数口径：退役/兑现/被撤销仍计入当日额度，防"退役再立"绕过
    service, db_path = _make_service(tmp_path)
    ts = _ts(2026, 6, 12, 10)
    vow, err = _create_ai(service, db_path, "立了又退", "msg_a", ts)
    assert err is None
    ok, err = asyncio.run(service.retire_vow(vow["id"], "反悔了"))
    assert ok
    vow2, err = _create_ai(service, db_path, "想再立一条", "msg_b", ts + 60)
    assert vow2 is None and "额度" in err


def test_ui_vows_not_daily_limited(tmp_path):
    service, db_path = _make_service(tmp_path)
    ts = _ts(2026, 6, 12, 10)
    assert _create_ai(service, db_path, "AI 的一条", "msg_a", ts)[1] is None
    for i in range(3):
        vow, err = asyncio.run(service.create_ui_vow(f"UI 第 {i} 条"))
        assert err is None


def test_day_window_is_shanghai_day():
    ts = _ts(2026, 6, 12, 23, 59)
    start, end = day_window(ts)
    assert start == _ts(2026, 6, 12, 0, 0)
    assert end == _ts(2026, 6, 13, 0, 0)


# ── 并发（§10-9）──


def test_concurrent_creates_do_not_pierce_active_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VOW_ACTIVE_MAX", 1)
    service, _ = _make_service(tmp_path)

    async def flow():
        return await asyncio.gather(
            service.create_ui_vow("并发 A"),
            service.create_ui_vow("并发 B"),
            service.create_ui_vow("并发 C"),
        )

    results = asyncio.run(flow())
    succeeded = [r for r in results if r[1] is None]
    assert len(succeeded) == 1
    assert len(asyncio.run(service.list_active())) == 1


def test_concurrent_ai_creates_do_not_pierce_daily_limit(tmp_path):
    service, db_path = _make_service(tmp_path)
    ts = _ts(2026, 6, 12, 10)

    async def one(msg_id):
        async with _open_db(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            vow, err = await service.create_in_tx(
                db, content=f"并发 {msg_id}", origin_type="ai_marker",
                origin_message_id=msg_id, created_at=ts,
            )
            if err:
                await db.rollback()
            else:
                await db.commit()
            return vow, err

    async def flow():
        return await asyncio.gather(one("msg_a"), one("msg_b"), one("msg_c"))

    results = asyncio.run(flow())
    succeeded = [r for r in results if r[1] is None]
    assert len(succeeded) == 1


# ── 版本链与状态机（§10-10 / §10-14）──


def test_revise_chain_atomic(tmp_path):
    service, db_path = _make_service(tmp_path)
    first, err = asyncio.run(service.create_ui_vow("初版约定"))
    assert err is None
    new, err = asyncio.run(service.revise_vow(first["id"], "修订后的约定", "说法不准"))
    assert err is None
    assert new["root_id"] == first["id"]
    assert new["previous_version_id"] == first["id"]
    chain = asyncio.run(service.list_chain(first["id"]))
    assert len(chain) == 2
    old = next(v for v in chain if v["id"] == first["id"])
    assert old["status"] == "superseded"
    assert old["close_action"] == "revised"
    assert old["closed_reason"] == "说法不准"
    actives = [v for v in chain if v["status"] == "active"]
    assert len(actives) == 1 and actives[0]["id"] == new["id"]


def test_one_active_per_chain_enforced_by_index(tmp_path):
    service, db_path = _make_service(tmp_path)
    first, err = asyncio.run(service.create_ui_vow("唯一 active"))
    assert err is None

    async def violate():
        async with _open_db(db_path) as db:
            await repository.insert_vow(
                db, vow_id="vow_dup", root_id=first["root_id"], previous_version_id=None,
                content="同链第二条 active", origin_type="user_ui",
                origin_conv_id=None, origin_message_id=None, created_at=1.0,
            )
            await db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(violate())


def test_state_machine_rules(tmp_path):
    service, _ = _make_service(tmp_path)
    vow, _ = asyncio.run(service.create_ui_vow("会被兑现"))
    # 退役必填原因
    ok, err = asyncio.run(service.retire_vow(vow["id"], ""))
    assert not ok and "原因" in err
    # 兑现
    ok, err = asyncio.run(service.fulfill_vow(vow["id"]))
    assert ok
    # 非 active 不可再关闭 / 修订
    ok, err = asyncio.run(service.retire_vow(vow["id"], "晚了"))
    assert not ok
    new, err = asyncio.run(service.revise_vow(vow["id"], "改一下", "晚了"))
    assert new is None
    # fulfilled 不再出现在 active 注入集合
    assert asyncio.run(service.list_active()) == []


def test_close_active_rowcount_zero_for_non_active(tmp_path):
    service, db_path = _make_service(tmp_path)
    vow, _ = asyncio.run(service.create_ui_vow("先兑现"))
    asyncio.run(service.fulfill_vow(vow["id"]))

    async def flow():
        async with _open_db(db_path) as db:
            return await repository.close_active(
                db, vow_id=vow["id"], new_status="retired", close_action="retired",
                closed_reason="x", status_changed_at=2.0,
            )

    assert asyncio.run(flow()) == 0


def test_revoke_rules_for_origin_message(tmp_path):
    service, db_path = _make_service(tmp_path)
    ts = _ts(2026, 6, 12, 10)
    vow, err = _create_ai(service, db_path, "原话立的约", "msg_origin", ts)
    assert err is None

    async def revoke(action):
        async with _open_db(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await service.revoke_for_origin_message_in_tx(
                db, message_id="msg_origin", close_action=action,
            )
            await db.commit()
            return row

    # 仍 active → 退役
    revoked = asyncio.run(revoke("origin_deleted"))
    assert revoked["status"] == "retired" and revoked["close_action"] == "origin_deleted"
    # 再撤一次 → 合法 no-op
    assert asyncio.run(revoke("origin_deleted")) is None


def test_revoke_does_not_touch_ui_revised_successor(tmp_path):
    service, db_path = _make_service(tmp_path)
    ts = _ts(2026, 6, 12, 10)
    vow, err = _create_ai(service, db_path, "原话立的约", "msg_origin", ts)
    assert err is None
    successor, err = asyncio.run(service.revise_vow(vow["id"], "UI 修订过的版本", "措辞更新"))
    assert err is None

    async def revoke():
        async with _open_db(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await service.revoke_for_origin_message_in_tx(
                db, message_id="msg_origin", close_action="origin_regenerated",
            )
            await db.commit()
            return row

    # 原始行已 superseded → no-op；UI 修订的 active 后继不受波及
    assert asyncio.run(revoke()) is None
    actives = asyncio.run(service.list_active())
    assert [v["id"] for v in actives] == [successor["id"]]


# ── 幂等（§10-11）──


def test_origin_message_unique_index_blocks_duplicate(tmp_path):
    service, db_path = _make_service(tmp_path)
    ts = _ts(2026, 6, 12, 10)
    assert _create_ai(service, db_path, "第一次写入", "msg_same", ts)[1] is None

    async def retry():
        async with _open_db(db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await repository.insert_vow(
                db, vow_id="vow_retry", root_id="vow_retry", previous_version_id=None,
                content="重试写入", origin_type="ai_marker",
                origin_conv_id="conv1", origin_message_id="msg_same", created_at=ts,
            )
            await db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(retry())

    async def count():
        async with _open_db(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM vows WHERE origin_message_id='msg_same'"
            )
            return (await cursor.fetchone())[0]

    assert asyncio.run(count()) == 1


# ── prompt 片段 ──


def test_vow_block_lists_active_with_age():
    now = _ts(2026, 6, 12, 10)
    vows = [
        {"content": "每年今天一起看海", "created_at": now - 86400 * 40},
        {"content": "难受的时候先说出来", "created_at": now - 3600},
    ]
    block = build_vow_block(vows, now=now)
    assert "你们之间已经说定的事" in block
    assert "每年今天一起看海（1个月前立下）" in block
    assert "难受的时候先说出来（今天立下）" in block
    assert "不授予任何设备或 control 权限" in block


def test_vow_block_empty_when_no_active():
    assert build_vow_block([], now=0) == ""


def test_alarm_fallback_is_fixed_template():
    assert build_alarm_fallback_text("吃药") == "⏰ 到点了：吃药"
    assert "你设过的提醒" in build_alarm_fallback_text("  ")
