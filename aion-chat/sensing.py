"""
体感 / 体征 / 社交脉搏统一时间线

替代旧摄像头监控：哨兵不再看图像，而是读这个文字化的 timeline。
三类事件合流到同一个 JSONL 文件，按时间戳排序。

- type=sensor       手机传感器快照（动作、环境光、气压、WiFi、电量、屏幕）
- type=biometric    Health Connect 手环数据（心率、睡眠、步数、SpO2）
- type=notification 社交脉搏（微信/QQ 通知到达事件）
- type=unlock       屏幕解锁事件（补偿失去的「轰炸」信号）
"""

from __future__ import annotations

import json, time, threading
from pathlib import Path
from datetime import date, timedelta

from config import DATA_DIR

# ── 路径 ──────────────────────────────────────────
SENSING_LOGS_DIR = DATA_DIR / "sensing_logs"
SENSING_LOGS_DIR.mkdir(exist_ok=True)

# 仅供兼容读取；清理时动态读取可配置的原始数据保留期。
KEEP_DAYS = 14

_file_lock = threading.Lock()
_last_cleanup_ts = 0.0
_CLEANUP_INTERVAL = 600  # 10 分钟清理一次


# ── JSONL 读写 ────────────────────────────────────

def _today_log_path() -> Path:
    return SENSING_LOGS_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"


def append_sensing_entry(entry: dict):
    path = _today_log_path()
    with _file_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_sensing_entries(entries: list[dict]):
    """Append one audit batch under a single lock so readers never see half of it."""
    if not entries:
        return
    path = _today_log_path()
    encoded = "".join(
        json.dumps(entry, ensure_ascii=False) + "\n"
        for entry in entries
    )
    with _file_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(encoded)


def read_recent_sensing(hours: int = 3) -> list:
    """读取最近 N 小时内的条目（跨天也支持），按时间戳升序。"""
    cutoff_ts = time.time() - hours * 3600
    cutoff_date = date.fromtimestamp(cutoff_ts)
    result = []
    for logfile in sorted(SENSING_LOGS_DIR.glob("*.jsonl")):
        try:
            if date.fromisoformat(logfile.stem) < cutoff_date:
                continue
        except ValueError:
            continue
        with open(logfile, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) >= cutoff_ts:
                        result.append(entry)
                except Exception:
                    pass
    result.sort(key=lambda e: e.get("timestamp", 0))
    return result


def cleanup_old_sensing_logs():
    global _last_cleanup_ts
    now = time.time()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL:
        return
    _last_cleanup_ts = now
    from app.daily_signals.config import sensing_raw_retention_days

    cutoff = date.today() - timedelta(days=sensing_raw_retention_days())
    for logfile in list(SENSING_LOGS_DIR.glob("*.jsonl")):
        try:
            if date.fromisoformat(logfile.stem) < cutoff:
                logfile.unlink(missing_ok=True)
        except ValueError:
            pass


# ── 语义化描述 ────────────────────────────────────

_MOTION_ZH = {
    "still": "静止",
    "walking": "走动",
    "running": "跑步",
    "in_vehicle": "在交通工具上",
    "on_bicycle": "骑行",
    "tilting": "轻微晃动（状态不明）",
    "unknown": "未知",
}

def _fmt_sensor(data: dict) -> str:
    parts = []
    motion = data.get("motion")
    if motion:
        parts.append(_MOTION_ZH.get(motion, motion))
    if data.get("screen_on") is not None:
        parts.append("亮屏" if data["screen_on"] else "锁屏")
    bat = data.get("battery_pct")
    if bat is not None:
        charge = "充电中" if data.get("charging") else ""
        parts.append(f"电量{bat}%{charge}")
    return " · ".join(parts)


def _fmt_biometric(data: dict) -> str:
    parts = []
    hr = data.get("heart_rate")
    if hr:
        parts.append(f"心率{hr}")
    spo2 = data.get("spo2")
    if spo2:
        parts.append(f"血氧{spo2}%")
    sleep = data.get("sleep_stage")
    if sleep:
        parts.append(f"睡眠:{sleep}")
    steps = data.get("steps_delta")
    if steps:
        parts.append(f"步数+{steps}")
    stress = data.get("stress")
    if stress is not None:
        parts.append(f"压力{stress}")
    return " · ".join(parts)


def _fmt_notification(data: dict) -> str:
    app = data.get("app", "?")
    return f"收到 {app} 消息"


def _fmt_unlock(data: dict) -> str:
    return "解锁屏幕"


_FORMATTERS = {
    "sensor": _fmt_sensor,
    "biometric": _fmt_biometric,
    "notification": _fmt_notification,
    "unlock": _fmt_unlock,
}


def format_sensing_for_prompt(hours: int = 3, max_entries: int = 60) -> str:
    """
    把最近 N 小时的 timeline 渲染成给哨兵 LLM 的文字。
    通知事件密度大，做 5 分钟桶聚合；传感器/体征事件稀疏，直接列出。
    """
    entries = [
        entry
        for entry in read_recent_sensing(hours)
        if entry.get("backfill") is not True
    ]
    if not entries:
        return ""

    # 按类型分流
    sensor_biometric = [e for e in entries if e.get("type") in ("sensor", "biometric")]
    notif_unlock = [e for e in entries if e.get("type") in ("notification", "unlock")]

    # 通知/解锁按 5 分钟桶聚合
    BUCKET_SECS = 300
    buckets: dict[int, dict] = {}
    for e in notif_unlock:
        b = int(e["timestamp"] // BUCKET_SECS) * BUCKET_SECS
        buckets.setdefault(b, {"wechat": 0, "qq": 0, "other": 0, "unlock": 0, "ts": b})
        if e["type"] == "unlock":
            buckets[b]["unlock"] += 1
        else:
            app = e.get("data", {}).get("app", "").lower()
            if "微信" in app or "wechat" in app:
                buckets[b]["wechat"] += 1
            elif "qq" in app:
                buckets[b]["qq"] += 1
            else:
                buckets[b]["other"] += 1

    # 合并成统一时间线
    lines = []
    for e in sensor_biometric:
        fmt = _FORMATTERS.get(e.get("type"))
        if not fmt:
            continue
        text = fmt(e.get("data", {}))
        if not text:
            continue
        t = time.strftime("%H:%M", time.localtime(e["timestamp"]))
        lines.append((e["timestamp"], f"[{t}] {text}"))

    for b, info in buckets.items():
        if info["wechat"] + info["qq"] + info["other"] + info["unlock"] == 0:
            continue
        parts = []
        if info["wechat"]: parts.append(f"微信×{info['wechat']}")
        if info["qq"]:     parts.append(f"QQ×{info['qq']}")
        if info["other"]:  parts.append(f"其他×{info['other']}")
        if info["unlock"]: parts.append(f"解锁×{info['unlock']}")
        t = time.strftime("%H:%M", time.localtime(b))
        lines.append((b, f"[{t}] {' · '.join(parts)}"))

    lines.sort(key=lambda x: x[0])
    if len(lines) > max_entries:
        lines = lines[-max_entries:]
    return "\n".join(line for _, line in lines)
