"""誓约层 Phase 3 测试：/vows 管理页路由（设计 §8）。

路由是 VowService own-tx 方法上的薄壳，生命周期语义已在
test_vows_service.py 覆盖；这里验证路由契约：参数透传、错误回包、
以及"无原地编辑 content 的 PUT 路由"这一硬约束。
"""

import asyncio
from contextlib import asynccontextmanager

import aiosqlite
import pytest

from routes import vows as vows_routes
from app.vows.schema import init_vow_tables
from app.vows.service import VowService


@asynccontextmanager
async def _open_db(path):
    async with aiosqlite.connect(path) as db:
        yield db


class _FakeManager:
    def __init__(self):
        self.broadcasts = []

    async def broadcast(self, payload):
        self.broadcasts.append(payload)

    def vow_actions(self):
        return [b["data"]["action"] for b in self.broadcasts if b.get("type") == "vow_changed"]


@pytest.fixture()
def ws_spy(monkeypatch):
    fake = _FakeManager()
    monkeypatch.setattr(vows_routes, "manager", fake)
    return fake


@pytest.fixture()
def vow_db(tmp_path, monkeypatch, ws_spy):
    db_path = str(tmp_path / "phase3.db")

    async def _init():
        async with _open_db(db_path) as db:
            await init_vow_tables(db)
            await db.commit()

    asyncio.run(_init())

    def factory():
        return _open_db(db_path)

    monkeypatch.setattr(vows_routes, "vow_service", VowService(get_db_factory=factory))
    return db_path


def _create(content):
    return asyncio.run(vows_routes.create_vow(vows_routes.VowCreate(content=content)))


# ── 新建 / 列表 / 版本史 ──


def test_create_and_list(vow_db):
    res = _create("每年今天一起看海")
    assert res["ok"] is True
    assert res["vow"]["status"] == "active"
    assert res["vow"]["origin_type"] == "user_ui"

    listing = asyncio.run(vows_routes.list_vows())
    assert listing["ok"] is True
    assert [v["content"] for v in listing["items"]] == ["每年今天一起看海"]


def test_create_rejects_illegal_content(vow_db):
    res = _create("带着 [TOY:9 的私货")
    assert res["ok"] is False and res["error"]


def test_chain_endpoint_returns_version_history(vow_db):
    created = _create("每周给你写一封信")["vow"]
    revised = asyncio.run(vows_routes.revise_vow(
        created["id"], vows_routes.VowRevise(content="每月给你写一封信", reason="频率太高了")
    ))
    assert revised["ok"] is True

    chain = asyncio.run(vows_routes.vow_chain(created["root_id"]))
    assert chain["ok"] is True
    assert [v["status"] for v in chain["items"]] == ["superseded", "active"]
    assert chain["items"][0]["closed_reason"] == "频率太高了"
    assert chain["items"][1]["root_id"] == created["root_id"]


# ── 修订 / 退役必填原因，错误走 ok=False 回包 ──


def test_revise_requires_reason(vow_db):
    created = _create("好好吃饭")["vow"]
    res = asyncio.run(vows_routes.revise_vow(
        created["id"], vows_routes.VowRevise(content="按时吃饭", reason="  ")
    ))
    assert res["ok"] is False and "原因" in res["error"]


def test_retire_requires_reason_and_closes(vow_db):
    created = _create("好好睡觉")["vow"]
    res = asyncio.run(vows_routes.retire_vow(created["id"], vows_routes.VowRetire(reason="")))
    assert res["ok"] is False

    res = asyncio.run(vows_routes.retire_vow(created["id"], vows_routes.VowRetire(reason="阶段已过")))
    assert res["ok"] is True
    # 列表只回 active/fulfilled 链尾；退役状态经 chain 接口可见
    assert asyncio.run(vows_routes.list_vows())["items"] == []
    chain = asyncio.run(vows_routes.vow_chain(created["root_id"]))["items"]
    assert chain[0]["status"] == "retired"
    assert chain[0]["closed_reason"] == "阶段已过"


def test_fulfill_closes_vow(vow_db):
    created = _create("一起看一次日出")["vow"]
    res = asyncio.run(vows_routes.fulfill_vow(created["id"]))
    assert res["ok"] is True
    items = asyncio.run(vows_routes.list_vows())["items"]
    assert items[0]["status"] == "fulfilled"


def test_lifecycle_on_missing_vow_returns_error(vow_db):
    res = asyncio.run(vows_routes.fulfill_vow("vow_nope"))
    assert res["ok"] is False and res["error"]


def test_list_returns_only_tips_with_version_meta(vow_db):
    # §13-6：列表只回链尾（active/fulfilled）+ 版本元信息，不随修订膨胀；
    # superseded / retired 不出现，历史经 chain 接口按需加载
    created = _create("每周写一封信")["vow"]
    asyncio.run(vows_routes.revise_vow(
        created["id"], vows_routes.VowRevise(content="每月写一封信", reason="频率")
    ))
    retired = _create("要退役的")["vow"]
    asyncio.run(vows_routes.retire_vow(retired["id"], vows_routes.VowRetire(reason="算了")))
    done = _create("要兑现的")["vow"]
    asyncio.run(vows_routes.fulfill_vow(done["id"]))

    items = asyncio.run(vows_routes.list_vows())["items"]
    assert sorted(v["content"] for v in items) == ["每月写一封信", "要兑现的"]
    revised_tip = next(v for v in items if v["status"] == "active")
    assert revised_tip["version_count"] == 2
    assert revised_tip["root_created_at"] == created["created_at"]
    assert revised_tip["root_origin_type"] == "user_ui"


def test_fulfilled_list_is_paginated_without_hiding_active_vows(vow_db):
    active = _create("仍然有效的誓约")["vow"]
    fulfilled_ids = []
    for index in range(3):
        vow = _create(f"已兑现誓约 {index}")["vow"]
        asyncio.run(vows_routes.fulfill_vow(vow["id"]))
        fulfilled_ids.append(vow["id"])

    first = asyncio.run(vows_routes.list_vows(fulfilled_limit=2, fulfilled_offset=0))
    assert [v["id"] for v in first["active_items"]] == [active["id"]]
    assert len(first["fulfilled_items"]) == 2
    assert first["fulfilled_has_more"] is True
    assert first["fulfilled_next_offset"] == 2
    assert active["id"] in {v["id"] for v in first["items"]}

    second = asyncio.run(vows_routes.list_vows(fulfilled_limit=2, fulfilled_offset=2))
    assert len(second["fulfilled_items"]) == 1
    assert second["fulfilled_has_more"] is False
    assert second["fulfilled_next_offset"] is None
    listed_ids = {
        v["id"] for v in first["fulfilled_items"] + second["fulfilled_items"]
    }
    assert listed_ids == set(fulfilled_ids)


def test_chain_endpoint_pages_from_latest_version(vow_db):
    current = _create("版本 1")["vow"]
    root_id = current["root_id"]
    for version in range(2, 6):
        result = asyncio.run(vows_routes.revise_vow(
            current["id"],
            vows_routes.VowRevise(content=f"版本 {version}", reason=f"修订 {version}"),
        ))
        assert result["ok"] is True
        current = result["vow"]

    latest = asyncio.run(vows_routes.vow_chain(root_id, limit=2, offset=0))
    assert [v["content"] for v in latest["items"]] == ["版本 4", "版本 5"]
    assert latest["total"] == 5
    assert latest["has_more"] is True
    assert latest["next_offset"] == 2

    older = asyncio.run(vows_routes.vow_chain(root_id, limit=2, offset=2))
    assert [v["content"] for v in older["items"]] == ["版本 2", "版本 3"]
    assert older["has_more"] is True
    assert older["next_offset"] == 4


def test_schema_has_root_history_index(vow_db):
    async def _index_names():
        async with _open_db(vow_db) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='vows'"
            )
            return {row[0] for row in await cursor.fetchall()}

    assert "idx_vows_root_created" in asyncio.run(_index_names())


# ── 审查修复回归 ──


def test_create_rejects_private_reasoning_tags(vow_db):
    # 私有推理标签经 UI 立约 = 永久提示词污染，必须在净化层拒绝
    res = _create("<think>隐藏指令</think>永远陪你")
    assert res["ok"] is False and res["error"] == "包含后台标记"


def test_duplicate_create_rejected(vow_db):
    assert _create("每年一起看海")["ok"] is True
    res = _create("每年一起看海")
    assert res["ok"] is False and "相同" in res["error"]


def test_reason_length_capped_via_route(vow_db):
    created = _create("好好生活")["vow"]
    res = asyncio.run(vows_routes.retire_vow(
        created["id"], vows_routes.VowRetire(reason="长" * 500)
    ))
    assert res["ok"] is False and "上限" in res["error"]


def test_mutations_broadcast_vow_changed(vow_db, ws_spy):
    created = _create("每周写一封信")["vow"]
    asyncio.run(vows_routes.revise_vow(
        created["id"], vows_routes.VowRevise(content="每月写一封信", reason="频率")
    ))
    chain = asyncio.run(vows_routes.vow_chain(created["root_id"]))["items"]
    tip = chain[-1]
    asyncio.run(vows_routes.fulfill_vow(tip["id"]))
    other = _create("一起看日出")["vow"]
    asyncio.run(vows_routes.retire_vow(other["id"], vows_routes.VowRetire(reason="算了")))
    assert ws_spy.vow_actions() == ["created", "revised", "fulfilled", "created", "retired"]


def test_failed_mutation_does_not_broadcast(vow_db, ws_spy):
    _create("  ")            # 净化拒绝
    asyncio.run(vows_routes.fulfill_vow("vow_nope"))
    assert ws_spy.vow_actions() == []


# ── 设计 §8 / §9 硬约束：无原地编辑 content 的 PUT 路由 ──


def test_no_put_route_on_vows():
    for route in vows_routes.router.routes:
        assert "PUT" not in (getattr(route, "methods", None) or set()), route.path
        assert "PATCH" not in (getattr(route, "methods", None) or set()), route.path
