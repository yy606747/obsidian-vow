"""
誓约管理页 API（设计 §8）：列表 / 版本史 / 新建 / 修订 / 退役 / 兑现。

全部生命周期操作走 VowService 的 own-tx 方法（BEGIN IMMEDIATE，§3）；
**无原地编辑 content 的 PUT 路由**——修订是唯一改内容的途径，且必产生新版本。
"""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ws import manager

from app.vows.service import VowConflictError, vow_service

router = APIRouter()


async def _broadcast_vow_changed(action: str) -> None:
    # 管理页/其他设备靠这个事件刷新（不带载荷，前端整表重拉）
    await manager.broadcast({"type": "vow_changed", "data": {"action": action}})


class VowCreate(BaseModel):
    content: str


class VowRevise(BaseModel):
    content: str
    reason: str


class VowRetire(BaseModel):
    reason: str


@router.get("/api/vows")
async def list_vows(
    fulfilled_limit: Annotated[int, Query(ge=1, le=100)] = 50,
    fulfilled_offset: Annotated[int, Query(ge=0)] = 0,
):
    # 只返回链尾（active / fulfilled）+ 版本数：响应不随修订次数膨胀，
    # 历史版本由 chain 接口按需加载
    page = await vow_service.list_tips(
        fulfilled_limit=fulfilled_limit,
        fulfilled_offset=fulfilled_offset,
    )
    return {"ok": True, **page}


@router.get("/api/vows/{root_id}/chain")
async def vow_chain(
    root_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    page = await vow_service.list_chain_page(root_id, limit=limit, offset=offset)
    return {"ok": True, **page}


@router.post("/api/vows")
async def create_vow(body: VowCreate):
    vow, err = await vow_service.create_ui_vow(body.content)
    if err:
        return {"ok": False, "error": err}
    await _broadcast_vow_changed("created")
    return {"ok": True, "vow": vow}


@router.post("/api/vows/{vow_id}/revise")
async def revise_vow(vow_id: str, body: VowRevise):
    try:
        vow, err = await vow_service.revise_vow(vow_id, body.content, body.reason)
    except VowConflictError:
        return {"ok": False, "error": "状态冲突，请刷新后重试"}
    if err:
        return {"ok": False, "error": err}
    await _broadcast_vow_changed("revised")
    return {"ok": True, "vow": vow}


@router.post("/api/vows/{vow_id}/retire")
async def retire_vow(vow_id: str, body: VowRetire):
    try:
        ok, err = await vow_service.retire_vow(vow_id, body.reason)
    except VowConflictError:
        return {"ok": False, "error": "状态冲突，请刷新后重试"}
    if not ok:
        return {"ok": False, "error": err}
    await _broadcast_vow_changed("retired")
    return {"ok": True}


@router.post("/api/vows/{vow_id}/fulfill")
async def fulfill_vow(vow_id: str):
    try:
        ok, err = await vow_service.fulfill_vow(vow_id)
    except VowConflictError:
        return {"ok": False, "error": "状态冲突，请刷新后重试"}
    if not ok:
        return {"ok": False, "error": err}
    await _broadcast_vow_changed("fulfilled")
    return {"ok": True}
