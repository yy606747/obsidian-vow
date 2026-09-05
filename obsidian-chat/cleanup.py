"""
启动时的磁盘保护：清理过旧的缓存/日志，避免跑久了把盘写满。
只动不影响正确性的"可重建"数据（tts_cache、monitor_logs、activity_logs）。
用户上传 (uploads/) 和截图 (screenshots/) 不动 —— 它们被聊天记录引用。
"""

from __future__ import annotations

import time
from pathlib import Path


def prune_files(directory: Path, *, max_age_days: int | None = None,
                max_total_mb: int | None = None, pattern: str = "*") -> int:
    """按年龄 + 总大小两个维度清理。返回删除的文件数。"""
    if not directory.exists():
        return 0
    now = time.time()
    deleted = 0

    files = [f for f in directory.glob(pattern) if f.is_file()]

    # 先按年龄清
    if max_age_days:
        cutoff = now - max_age_days * 86400
        for f in list(files):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
                    files.remove(f)
            except Exception:
                pass

    # 再按总大小清：oldest first
    if max_total_mb:
        max_bytes = max_total_mb * 1024 * 1024
        sized = []
        total = 0
        for f in files:
            try:
                s = f.stat()
                sized.append((s.st_mtime, s.st_size, f))
                total += s.st_size
            except Exception:
                pass
        sized.sort(key=lambda x: x[0])  # 最老在前
        for mtime, size, f in sized:
            if total <= max_bytes:
                break
            try:
                f.unlink()
                total -= size
                deleted += 1
            except Exception:
                pass

    return deleted


def run_startup_cleanup() -> dict:
    """在 lifespan 启动时跑一次。纯本地 I/O，耗时 <50ms。"""
    from config import TTS_CACHE_DIR, MONITOR_LOGS_DIR, DATA_DIR
    from app.daily_signals.config import (
        activity_raw_max_total_mb,
        activity_raw_retention_days,
    )

    activity_dir = DATA_DIR / "activity_logs"

    results = {
        "tts_cache": prune_files(
            TTS_CACHE_DIR, max_age_days=14, max_total_mb=100, pattern="*.mp3"),
        "monitor_logs": prune_files(
            MONITOR_LOGS_DIR, max_age_days=30, max_total_mb=100, pattern="*.jsonl"),
        "activity_logs": prune_files(
            activity_dir,
            max_age_days=activity_raw_retention_days(),
            max_total_mb=activity_raw_max_total_mb(),
            pattern="*.jsonl",
        ),
    }
    total = sum(results.values())
    if total:
        print(f"[Cleanup] pruned {total} files: {results}")
    return results
