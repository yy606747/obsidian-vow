"""The sole natural-language renderer for context delivery projections."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Mapping

from .contracts import ContextDeliveryProjection, CurrentContextItem, RecentContextEvent
from .policy import (
    MAX_AVAILABILITY,
    MAX_BASELINE_DEVIATIONS,
    MAX_DEVICE_DERIVED,
    MAX_OBSERVATIONS,
    MAX_RECENT_EVENTS,
    MAX_RENDERED_CHARS,
)


_DIAGNOSTIC_ONLY_CURRENT_KEYS = frozenset({"phone.light_lux", "phone.wifi"})


def render_context_delivery_projection(
    projection: ContextDeliveryProjection | Mapping,
    *,
    user_name: str,
    ai_name: str,
    max_chars: int = MAX_RENDERED_CHARS,
    time_formatter: Callable[[float], str] | None = None,
) -> str:
    owner_name = str(user_name or "").strip()
    if not owner_name:
        raise ValueError("user_name is required for model-facing context delivery")
    companion_name = str(ai_name or "").strip()
    if not companion_name:
        raise ValueError("ai_name is required for model-facing context delivery")
    value = (
        projection
        if isinstance(projection, ContextDeliveryProjection)
        else ContextDeliveryProjection.from_dict(projection)
    )
    if value.is_empty or max_chars <= 0:
        return ""
    format_time = time_formatter or _local_time
    sections: list[tuple[str, list[str]]] = [
        ("直接观测：", _render_observations(
            value.observations,
            format_time,
            reference_time=value.generated_at,
            user_name=owner_name,
        )),
        ("设备端归纳：", [
            _render_derived(item, format_time)
            for item in value.device_derived[:MAX_DEVICE_DERIVED]
        ]),
        ("最近变化：", _render_recent_events(
            value.recent_events,
            format_time,
            reference_time=value.generated_at,
            user_name=owner_name,
            ai_name=companion_name,
        )),
        ("个人基线偏离：", [
            f"- {item.current_summary}；个人基线为{item.baseline_summary}（{item.sample_window}，覆盖率 {item.coverage:.0%}）。"
            for item in value.baseline_deviations[:MAX_BASELINE_DEVIATIONS]
        ]),
        ("数据可用性：", [
            f"- {_source_label(item.source)}{('自 ' + format_time(item.last_observed_at) + ' 后') if item.last_observed_at is not None else ''}{item.reason}。"
            for item in value.availability[:MAX_AVAILABILITY]
        ]),
    ]
    return _clip_complete_lines("[设备与环境上下文]", sections, max_chars=max_chars)


def _render_observations(
    items: tuple[CurrentContextItem, ...],
    format_time: Callable[[float], str],
    *,
    reference_time: float,
    user_name: str,
) -> list[str]:
    selected: list[CurrentContextItem] = []
    occupied_slots: set[str] = set()
    for item in items:
        if item.key in _DIAGNOSTIC_ONLY_CURRENT_KEYS:
            continue
        slot = (
            "location.current"
            if item.key in {"location.address", "location.place"}
            else item.key
        )
        if slot not in occupied_slots and len(occupied_slots) >= MAX_OBSERVATIONS:
            continue
        selected.append(item)
        occupied_slots.add(slot)

    place = next((item for item in selected if item.key == "location.place"), None)
    address = next((item for item in selected if item.key == "location.address"), None)
    lines: list[str] = []
    location_rendered = False
    for item in selected:
        if item.key in {"location.address", "location.place"}:
            if not location_rendered:
                lines.append(_render_location(
                    place,
                    address,
                    format_time,
                    user_name=user_name,
                ))
                location_rendered = True
            continue
        lines.append(_render_current(
            item,
            format_time,
            reference_time=reference_time,
            user_name=user_name,
        ))
    return lines


def _render_location(
    place: CurrentContextItem | None,
    address: CurrentContextItem | None,
    format_time: Callable[[float], str],
    *,
    user_name: str,
) -> str:
    facts: list[str] = []
    if place is not None:
        at = format_time(place.observed_at)
        if place.payload is not None:
            payload = place.payload
            side = "配置围栏内" if payload["boundary_side"] == "inside" else "配置围栏外"
            facts.append(
                f"{at} 手机定位在{side}"
                f"（距围栏中心约 {_number_text(payload['distance_m'])}m，"
                f"进入阈值 {_number_text(payload['configured_enter_m'])}m，"
                f"退出阈值 {_number_text(payload['configured_exit_m'])}m，"
                f"定位精度约 {_number_text(payload['accuracy_m'])}m）"
            )
        elif place.value == "unmatched":
            facts.append(f"{at} 定位服务报告设备未落入任何已登记地点范围")
        else:
            facts.append(
                f"{at} 定位服务报告设备落在「{place.value}」的位置范围内"
            )
    if address is not None:
        facts.append(
            f"{format_time(address.observed_at)} 高德地址报告设备大概在「{address.value}」"
        )
    return (
        f"- {'；'.join(facts)}。"
        f"仅凭定位不能判断{user_name}在宿舍、教室、是否上课或正在做什么。"
    )


def _render_current(
    item: CurrentContextItem,
    format_time: Callable[[float], str],
    *,
    reference_time: float,
    user_name: str,
) -> str:
    at = format_time(item.observed_at)
    key = item.key
    value = item.value
    if key in _DIAGNOSTIC_ONLY_CURRENT_KEYS:
        return ""
    if key == "phone.screen":
        state = "亮起" if value == "on" else "关闭"
        duration = _duration_text(item, reference_time=reference_time)
        return f"- {at} 手机报告屏幕{state}；{duration}。"
    if key == "phone.battery":
        return f"- {at} 手机报告电量 {value}%。"
    if key == "phone.charging":
        return f"- {at} 手机报告{'正在充电' if value else '未在充电'}。"
    if key.startswith("mobile.") and key.endswith(".foreground_app"):
        device_id = key.split(".", 2)[1]
        return f"- {at} 移动设备 {device_id} 报告前台应用为「{value}」。"
    if key == "pc.state":
        return f"- {at} PC 报告状态为 {value}；{_duration_text(item, reference_time=reference_time)}。"
    if key == "pc.foreground_app":
        return f"- {at} PC 报告前台应用为「{value}」。"
    if key in {"location.address", "location.place"}:
        return _render_location(
            item if key == "location.place" else None,
            item if key == "location.address" else None,
            format_time,
            user_name=user_name,
        )
    return f"- {at} {key} 报告值为 {value}。"


def _render_derived(item: CurrentContextItem, format_time: Callable[[float], str]) -> str:
    at = format_time(item.observed_at)
    if item.key == "phone.motion":
        return f"- {at} 手机运动分类为 {_motion_label(item.value)}（设备端归纳）。"
    return f"- {at} {item.key} 为 {item.value}（设备端归纳）。"


def _render_event(item: RecentContextEvent, format_time: Callable[[float], str]) -> str:
    at = format_time(item.observed_at)
    if item.event == "occurred":
        if item.key == "phone.unlock":
            return f"- {at} 手机报告一次解锁事件。"
        if item.key == "phone.notification":
            if item.occurrence_count > 1:
                start = format_time(
                    item.first_observed_at
                    if item.first_observed_at is not None
                    else item.observed_at
                )
                time_range = start if start == at else f"{start}–{at}"
                return (
                    f"- {time_range} 手机报告 {item.occurrence_count} 条通知"
                    f"（{item.to_value}）。"
                )
            return f"- {at} 手机报告「{item.to_value}」有通知到达。"
        return f"- {at} {item.key} 发生一次事件。"
    if item.key == "phone.screen":
        return f"- {at} 手机屏幕从{_screen_label(item.from_value)}变为{_screen_label(item.to_value)}。"
    if item.key == "location.place":
        if item.payload is not None:
            payload = item.payload
            direction = payload["geofence_direction"]
            direction_text = (
                "从配置围栏内越到围栏外"
                if direction == "inside_to_outside"
                else "从配置围栏外进入围栏内"
            )
            crossed = (
                payload["configured_exit_m"]
                if direction == "inside_to_outside"
                else payload["configured_enter_m"]
            )
            return (
                f"- {at} 手机定位{direction_text}"
                f"（距围栏中心约 {_number_text(payload['distance_m'])}m，"
                f"本次使用的边界阈值 {_number_text(crossed)}m，"
                f"定位精度约 {_number_text(payload['accuracy_m'])}m）。"
                "这只说明手机越过了配置边界。"
            )
        return f"- {at} 定位区域从「{item.from_value}」变为「{item.to_value}」。"
    if item.key.startswith("mobile.") and item.key.endswith(".foreground_app"):
        return f"- {at} 移动设备前台应用从「{item.from_value}」切换为「{item.to_value}」。"
    if item.key == "pc.foreground_app":
        return f"- {at} PC 前台应用从「{item.from_value}」切换为「{item.to_value}」。"
    return f"- {at} {item.key} 从 {item.from_value} 变为 {item.to_value}。"


def _render_recent_events(
    events: tuple[RecentContextEvent, ...],
    format_time: Callable[[float], str],
    *,
    reference_time: float,
    user_name: str,
    ai_name: str,
) -> list[str]:
    summons = sorted(
        (item for item in events if item.key == "relationship.summon"),
        key=lambda item: item.observed_at,
    )
    rendered: list[tuple[float, str]] = [
        (item.observed_at, _render_event(item, format_time))
        for item in events
        if item.key != "relationship.summon"
    ]
    if summons:
        visible = summons[-6:]
        # The summon window is a rolling 24h, not a calendar day: anything on
        # an earlier local date has to say so, or bare HH:MM reads as today.
        times = "、".join(
            _render_summon_time(item.observed_at, reference_time, format_time)
            for item in visible
        )
        count_suffix = (
            f"，一共 {len(summons)} 次" if len(summons) > len(visible) else ""
        )
        rendered.append((
            summons[-1].observed_at,
            f"- 最近一天里，{user_name}在 {times}{count_suffix}想过{ai_name}。"
            "这些是已经发生的时刻，不表示仍在等待回应。",
        ))
    rendered.sort(key=lambda item: item[0])
    return [line for _timestamp, line in rendered[-MAX_RECENT_EVENTS:]]


def _clip_complete_lines(
    header: str,
    sections: list[tuple[str, list[str]]],
    *,
    max_chars: int,
) -> str:
    lines = [header]
    for title, entries in sections:
        entries = [entry for entry in entries if entry]
        if not entries:
            continue
        candidate_title = ("\n" if len(lines) > 1 else "") + title
        trial = "\n".join([*lines, candidate_title, entries[0]])
        if len(trial) > max_chars:
            continue
        lines.append(candidate_title)
        for entry in entries:
            if len("\n".join([*lines, entry])) > max_chars:
                break
            lines.append(entry)
    rendered = "\n".join(lines).strip()
    return rendered if rendered != header else ""


def _duration_text(item: CurrentContextItem, *, reference_time: float) -> str:
    if item.since_at is None:
        return "该状态持续时间未知"
    seconds = max(0.0, reference_time - item.since_at)
    if seconds < 60:
        return "该状态刚发生变化"
    if seconds < 3600:
        return f"该状态至少持续约 {int(seconds // 60)} 分钟"
    hours = seconds / 3600
    return f"该状态至少持续约 {hours:.1f} 小时"


def _motion_label(value) -> str:
    return {
        "still": "静止",
        "walking": "步行",
        "running": "跑动",
        "in_vehicle": "乘车",
        "on_bicycle": "骑行",
        "tilting": "设备姿态变化",
        "unknown": "未知",
    }.get(str(value), str(value))


def _number_text(value) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _screen_label(value) -> str:
    return {"on": "亮屏", "off": "锁屏"}.get(str(value), str(value))


def _source_label(source: str) -> str:
    return {
        "android.sensing": "手机 sensing ",
        "pc.context": "PC agent ",
        "location.v2": "位置服务",
    }.get(source, f"{source} ")


def _local_time(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp)).strftime("%H:%M")


def _render_summon_time(
    observed_at: float,
    reference_time: float,
    format_time: Callable[[float], str],
) -> str:
    rendered = format_time(observed_at)
    try:
        observed_day = datetime.fromtimestamp(float(observed_at)).date()
        reference_day = datetime.fromtimestamp(float(reference_time)).date()
    except (OSError, OverflowError, ValueError):
        return rendered
    return rendered if observed_day == reference_day else f"昨天 {rendered}"


__all__ = ["render_context_delivery_projection"]
