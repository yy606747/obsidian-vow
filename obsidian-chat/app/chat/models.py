"""Pydantic request models for chat routes."""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field

from config import DEFAULT_MODEL

class ConvCreate(BaseModel):
    title: str = "新对话"
    model: str = DEFAULT_MODEL

class ConvUpdate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None

class MsgCreate(BaseModel):
    content: str
    context_limit: int = 30
    attachments: List[Any] = Field(default_factory=list)
    whisper_mode: bool = False
    fast_mode: bool = False
    temperature: Optional[float] = None
    retracted: bool = False
    # 亲密控制（AI Dom）模式：开启后覆盖 whisper_mode，AI 自主下达双通道指令
    ai_dom_mode: bool = False
    safeword: str = ""
    dom_history: List[str] = []
    # CNC 扩展（批次 A）
    cnc_enabled: bool = False
    cnc_weakness: List[str] = []
    # 信号采集
    resist_hits: int = 0
    short_streak: int = 0
    reply_delay_ms: int = 0
    compliance_streak: int = 0
    # 时间锚点（秒）
    session_elapsed: int = 0
    scene_name: Optional[str] = None
    scene_elapsed: int = 0
    since_last_punish: Optional[int] = None
    # 棘轮 / 债务 / 倔强（v2 猎物系统）
    ratchet_valley: int = 0
    debt: float = 0.0
    stubborn_streak: int = 0
    # 自动验收/回放用：真实调用模型，但不触发长期记忆摘要。
    memory_eval_mode: bool = False

class MsgUpdate(BaseModel):
    content: str

class CamCheckTrigger(BaseModel):
    conv_id: str
    model_key: str

class DomInitiativeBody(BaseModel):
    context_limit: int = 15
    safeword: str = ""
    dom_history: List[str] = []
    cnc_enabled: bool = False
    cnc_weakness: List[str] = []
    resist_hits: int = 0
    short_streak: int = 0
    reply_delay_ms: int = 0
    compliance_streak: int = 0
    session_elapsed: int = 0
    scene_name: Optional[str] = None
    scene_elapsed: int = 0
    since_last_punish: Optional[int] = None

class WhisperInitiativeBody(BaseModel):
    context_limit: int = 15
