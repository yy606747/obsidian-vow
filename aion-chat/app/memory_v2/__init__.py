"""
Memory V2 工程化入口。

Batch 2.0 只建立服务边界，底层算法仍委托旧 memory.py，确保行为稳定。
"""

from .service import MemoryService, memory_service
from .prompt_block import build_v2_memory_prompt_block
from .v2_repository import MemoryRepository
from .hybrid_recall import hybrid_recall

__all__ = [
    "MemoryService",
    "MemoryRepository",
    "build_v2_memory_prompt_block",
    "hybrid_recall",
    "memory_service",
]
