from __future__ import annotations

import logging

log = logging.getLogger("schedule")


def load_evidence(user_name: str) -> tuple[str, list[str]]:
    activity_summary_text = ""
    sensing_text = ""
    evidence_errors: list[str] = []

    try:
        from activity import get_activity_summary_for_prompt
        activity_summary_text = get_activity_summary_for_prompt(12)
    except Exception as exc:
        msg = f"activity_summary_failed: {type(exc).__name__}: {exc}"
        evidence_errors.append(msg)
        log.warning("monitor activity evidence failed", exc_info=True)

    try:
        from sensing import format_sensing_for_prompt
        sensing_text = format_sensing_for_prompt(hours=3, max_entries=40)
    except Exception as exc:
        msg = f"sensing_timeline_failed: {type(exc).__name__}: {exc}"
        evidence_errors.append(msg)
        log.warning("monitor sensing evidence failed", exc_info=True)

    return _format_evidence(user_name, activity_summary_text, sensing_text), evidence_errors


def _format_evidence(user_name: str, activity_summary_text: str, sensing_text: str) -> str:
    text = ""
    if sensing_text:
        text += (
            f"\n以下是{user_name}最近 3 小时的手机传感器 / 通知时间线"
            f"（光线、运动、心率、微信/QQ/解锁等事件按时间顺序）：\n"
            f"{sensing_text}\n"
        )
    if activity_summary_text:
        text += (
            f"\n以下是{user_name}过去两小时的设备使用动态（手机/电脑应用使用情况，每10分钟一条摘要）：\n"
            f"{activity_summary_text}\n"
        )
    if not sensing_text and not activity_summary_text:
        text += "\n（暂无可用的传感器/设备活动数据，请基于之前的对话上下文自然搭话。）\n"
    return text
