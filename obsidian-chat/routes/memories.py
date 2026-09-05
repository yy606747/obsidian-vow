"""
记忆库 CRUD API + 手动总结 + 原文追溯
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import List, Optional

from ws import manager
from app.memory_v2 import memory_service

router = APIRouter()

class MemoryCreate(BaseModel):
    content: str
    type: str = "event"

class MemoryUpdate(BaseModel):
    content: str
    type: Optional[str] = None
    keywords: Optional[str] = None
    importance: Optional[float] = None
    unresolved: Optional[int] = None

class RecallTraceRequest(BaseModel):
    query: str = Field(min_length=1)
    keywords: List[str] = Field(default_factory=list)
    mode: str = "normal"
    namespace: Optional[str] = None
    top_k: int = Field(default=8, ge=1, le=20)
    candidate_limit: int = Field(default=500, ge=1, le=2000)

class RecallConfigUpdate(BaseModel):
    mode: Optional[str] = None
    top_k: Optional[int] = None
    candidate_limit: Optional[int] = None
    include_trace: Optional[bool] = None
    canary_ratio: Optional[float] = None
    prompt_min_score: Optional[float] = None


class MemoryV3ConfigUpdate(BaseModel):
    relational_card_generation_enabled: Optional[bool] = None
    relational_card_v2_generation_enabled: Optional[bool] = None
    relational_card_generation_cutoff_ts: Optional[float] = None
    relational_cards_enabled: Optional[bool] = None
    replace_auto_digest: Optional[bool] = None
    card_readout_mode: Optional[str] = None
    relational_card_stability_delay_sec: Optional[float] = None
    relational_card_generation_batch_size: Optional[int] = None
    relational_card_generation_attempts: Optional[int] = None
    relational_card_failure_retry_delay_sec: Optional[float] = None
    relational_card_relationship_register: Optional[str] = None
    ai_note_lane_enabled: Optional[bool] = None
    ai_note_top_k: Optional[int] = None
    ai_note_max_items: Optional[int] = None
    pending_recall_enabled: Optional[bool] = None
    pending_full_corpus_enabled: Optional[bool] = None
    pending_candidate_k: Optional[int] = None
    pending_candidate_pool_limit: Optional[int] = None
    pending_select_max: Optional[int] = None
    pending_retrieval_timeout_sec: Optional[float] = None
    pending_join_max_wait_sec: Optional[float] = None
    pending_selector_timeout_sec: Optional[float] = None
    pending_selector_attempts: Optional[int] = None
    timeline_enabled: Optional[bool] = None
    timeline_hours: Optional[int] = None
    timeline_max_chars: Optional[int] = None
    timeline_generation_min_interval_sec: Optional[float] = None
    timeline_generation_timeout_sec: Optional[float] = None
    timeline_generation_attempts: Optional[int] = None


class RelationalCardInvalidateRequest(BaseModel):
    reason: str = Field(
        default="owner_marked_incorrect",
        min_length=1,
        max_length=500,
    )

@router.get("/api/memories")
async def list_memories(
    limit: Optional[int] = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str = Query("", max_length=200),
    type: str = Query("", max_length=40),
    unresolved: Optional[int] = Query(None, ge=0, le=1),
):
    """记忆列表。

    不带查询参数时保留旧版全量 list 返回，避免破坏现有 smoke/API 客户端。
    带 limit/q/type/unresolved 时返回分页对象，供前端记忆页使用。
    """
    if limit is None and offset == 0 and not q and not type and unresolved is None:
        return await memory_service.list_memories()
    return await memory_service.list_memories_page(
        limit=limit or 100,
        offset=offset,
        query=q,
        memory_type=type,
        unresolved=unresolved,
    )

@router.post("/api/memories")
async def create_memory(body: MemoryCreate):
    """手动添加记忆（无原文追溯，不影响总结锚点）"""
    mem = await memory_service.create_memory(body.content, body.type)
    await manager.broadcast({"type": "memory_added", "data": mem})
    return mem

@router.put("/api/memories/{mem_id}")
async def update_memory(mem_id: str, body: MemoryUpdate):
    return await memory_service.update_memory(
        mem_id,
        body.content,
        memory_type=body.type,
        keywords=body.keywords,
        importance=body.importance,
        unresolved=body.unresolved,
    )

@router.delete("/api/memories/{mem_id}")
async def delete_memory(mem_id: str):
    return await memory_service.delete_memory(mem_id)

@router.patch("/api/memories/{mem_id}/unresolved")
async def toggle_unresolved(mem_id: str):
    """切换记忆的 unresolved 状态"""
    return await memory_service.toggle_unresolved(mem_id)

@router.post("/api/memories/digest")
async def trigger_digest():
    """手动触发记忆总结"""
    result = await memory_service.manual_digest()
    return result

@router.post("/api/memories/v2/trace-recall")
async def trace_v2_recall(body: RecallTraceRequest):
    """只读 V2 recall 调试，不写 memory_usage，不影响聊天 prompt。"""
    return await memory_service.trace_v2_recall(
        body.query,
        keywords=body.keywords,
        mode=body.mode,
        namespace=body.namespace,
        top_k=body.top_k,
        candidate_limit=body.candidate_limit,
    )

@router.get("/api/memories/v2/recall-config")
async def get_v2_recall_config():
    """获取 V2 recall rollout 配置。默认 full 接管记忆 prompt，可显式回滚 legacy。"""
    return memory_service.get_v2_recall_config()

@router.put("/api/memories/v2/recall-config")
async def update_v2_recall_config(body: RecallConfigUpdate):
    updates = {
        key: value
        for key, value in body.dict().items()
        if value is not None
    }
    return memory_service.update_v2_recall_config(updates)


@router.get("/api/memories/v3/config")
async def get_memory_v3_config():
    """Return the normalized, default-off Memory V3 rollout contract."""
    return memory_service.get_memory_v3_config()


@router.put("/api/memories/v3/config")
async def update_memory_v3_config(body: MemoryV3ConfigUpdate):
    updates = {
        key: value
        for key, value in body.dict().items()
        if value is not None
    }
    return memory_service.update_memory_v3_config(updates)


@router.post("/api/memories/v3/cards/{card_id}/invalidate")
async def invalidate_relational_card(
    card_id: str,
    body: RelationalCardInvalidateRequest,
):
    """Owner correction path: retire one mistaken card without deleting history."""
    try:
        return await memory_service.invalidate_relational_card(
            card_id,
            reason=body.reason,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="relational card not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.get("/api/memories/digest/anchor")
async def get_anchor():
    """获取当前总结锚点时间戳"""
    from datetime import datetime
    ts = memory_service.load_digest_anchor()
    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts > 0 else "从未总结"
    return {"ok": True, "anchor_ts": ts, "anchor_date": date_str}

class AnchorReset(BaseModel):
    date: str  # 格式: YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS

@router.post("/api/memories/digest/anchor")
async def reset_anchor(body: AnchorReset):
    """重置总结锚点到指定日期"""
    from datetime import datetime
    try:
        if len(body.date) <= 10:
            dt = datetime.strptime(body.date, "%Y-%m-%d")
        else:
            dt = datetime.strptime(body.date, "%Y-%m-%d %H:%M:%S")
        ts = dt.timestamp()
        memory_service.save_digest_anchor(ts)
        return {"ok": True, "anchor_ts": ts, "anchor_date": dt.strftime("%Y-%m-%d %H:%M:%S")}
    except ValueError:
        return {"ok": False, "message": "日期格式不正确，请使用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"}

@router.get("/api/memories/{mem_id}/source")
async def get_memory_source(mem_id: str):
    """追溯记忆对应的原始聊天记录"""
    return await memory_service.get_memory_source(mem_id)
