"""
定位 API 路由：心跳上报、状态查询、配置管理、POI搜索
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from app.legacy_adapters.location_runtime import location_runtime
from app.location.use_case import (
    location_freshness,
    significant_location_move,
)

router = APIRouter()


# ── 心跳上报 ──────────────────────────────────────
class HeartbeatBody(BaseModel):
    lng: float
    lat: float
    accuracy: float = 0.0
    is_gcj02: bool = False   # 默认 WGS84，Android GPS 原始数据
    force: bool = False      # 强制处理（即使未启用，如浏览器设家）
    provider: Optional[str] = None
    location_age_ms: Optional[float] = None
    is_mock: Optional[bool] = None


class LocationDiagnosticBody(BaseModel):
    event: str
    ok: bool = False
    provider: Optional[str] = None
    message: Optional[str] = None
    elapsed_ms: Optional[float] = None
    retryable: Optional[bool] = None
    location_age_ms: Optional[float] = None
    meta: Optional[dict] = None


@router.post("/api/location/heartbeat")
async def location_heartbeat(body: HeartbeatBody):
    """接收手机端定位心跳"""
    return await location_runtime.heartbeat_response(body.model_dump())


@router.post("/api/location/diagnostic")
async def location_diagnostic(body: LocationDiagnosticBody):
    """记录 Android 定位链路诊断，不写入坐标、不改变定位状态。"""
    return location_runtime.record_diagnostic(body.model_dump())


async def process_heartbeat(
    lng: float,
    lat: float,
    accuracy: float = 0.0,
    is_gcj02: bool = False,
    *,
    provider: str | None = None,
    location_age_ms: float | None = None,
    is_mock: bool | None = None,
) -> dict:
    return await location_runtime.process_heartbeat(
        lng=lng,
        lat=lat,
        accuracy=accuracy,
        is_gcj02=is_gcj02,
        provider=provider,
        location_age_ms=location_age_ms,
        is_mock=is_mock,
    )


# ── 状态查询 ──────────────────────────────────────
@router.get("/api/location/status")
async def get_location_status():
    """查看当前位置状态"""
    return location_runtime.status_payload()


# ── POI 查询 ─────────────────────────────────────
class PoiSearchBody(BaseModel):
    category: str = "餐饮美食"      # 类型名称
    radius: Optional[int] = None    # 覆盖默认半径

@router.post("/api/location/poi-search")
async def poi_search(body: PoiSearchBody):
    """手动触发 POI 搜索（刷新某个类型）"""
    return await location_runtime.poi_search_response(category=body.category, radius=body.radius)


# ── 获取缓存的 POI（供 Core 读取）────────────────
@router.get("/api/location/pois")
async def get_cached_pois():
    """获取缓存的周边 POI 数据"""
    return location_runtime.cached_pois_payload()


# ── 配置管理 ──────────────────────────────────────
class LocationConfigUpdate(BaseModel):
    amap_key: Optional[str] = None
    home_lng: Optional[float] = None
    home_lat: Optional[float] = None
    home_threshold: Optional[int] = None
    heartbeat_outdoor_min: Optional[int] = None
    heartbeat_home_min: Optional[int] = None
    poi_radius: Optional[int] = None
    enabled: Optional[bool] = None
    quiet_hours_enabled: Optional[bool] = None
    quiet_hours_start: Optional[str] = None
    quiet_hours_end: Optional[str] = None

@router.get("/api/location/config")
async def get_location_config():
    return location_runtime.config_payload()

@router.put("/api/location/config")
async def update_location_config(body: LocationConfigUpdate):
    return location_runtime.update_config(body.model_dump())


# ── 设置家的位置（快捷接口：用当前位置设为家）─────
@router.post("/api/location/set-home")
async def set_home_location():
    """将当前位置设为家的位置"""
    return location_runtime.set_home_response()


def _significant_location_move(cfg: dict, result: dict) -> bool:
    return significant_location_move(cfg, result)


def _location_freshness(status: dict, *, now: float | None = None) -> dict:
    return location_freshness(status, now=now)
