"""Pure Attention snapshot builder used by Sentinel replay tests.

The builder consumes already-loaded replay input and returns a compact
Attention snapshot. It intentionally has no file, database, network, model, or
runtime side effects.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.location_geofence import (
    LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION,
    normalize_location_geofence_payload,
)

from .attention_config import resolve_attention_config
from .sources import EvidenceRecord, adapt_replay_input


ATTENTION_SNAPSHOT_SCHEMA_VERSION = "attention_triage_snapshot.v0"

@dataclass(frozen=True)
class _AttentionFeatures:
    text: str
    source_records: tuple[str, ...]
    raw_signal_count: int
    recent_chat_count: int
    tags: frozenset[str]
    geofence_payloads: tuple[Mapping[str, Any], ...] = ()


_SNAPSHOT_PRIORITY = (
    "recent_chat_cooldown",
    "relationship_unsettled",
    "routine_risk",
    "conflict_busy_active",
    "return_home_transition",
    "left_home_transition",
    "stale_location",
    "stable_outside",
    "low_confidence_sleep",
    "busy_study",
    "motion_uncertain",
    "notification_uncertain",
    "charging_home_low_activity",
    "possible_idle",
    "sleep_uncertain",
    "mood_uncertain",
    "long_silence",
    "camera_disabled",
    "pc_disabled",
    "pc_context",
)

_STATIC_SNAPSHOTS: dict[str, dict[str, Any]] = {
    "relationship_unsettled": {
        "compact_text": "最近聊天里出现争执或冷淡收尾，关系状态可能未收束。需要克制表达，不能只按普通空闲窗口处理。",
        "world_state": {"relationship_state": "unsettled"},
        "label": "关系未收束", "confidence": 0.72, "salience": 0.82,
        "support": ["最近聊天有争执或冷淡收尾"],
        "missing": ["用户当前情绪", "是否愿意继续聊"],
        "attention_targets": ["relationship_unsettled"],
        "suggested_next_check_sec": 1200,
    },
    "routine_risk": {
        "compact_text": "当前接近承诺或作息节点，屏幕仍有活动。这里只适合提醒风险，不能把它写成惩罚或已经违规。",
        "world_state": {"routine_state": "risk_window"},
        "label": "承诺或作息风险", "confidence": 0.7, "salience": 0.78,
        "support": ["接近承诺或作息节点", "屏幕仍有活动"],
        "against": ["不知道用户是否已经准备结束"],
        "missing": ["用户当前安排", "是否已经完成承诺"],
        "attention_targets": ["routine_risk"],
        "suggested_next_check_sec": 600,
    },
    "conflict_busy_active": {
        "compact_text": "用户之前明确说在开会或忙正事，但手机又有活跃信号。这是忙碌声明和手机活跃冲突，优先尊重忙碌声明。",
        "world_state": {"availability": "busy_conflicted"},
        "label": "忙碌信号优先", "confidence": 0.74, "salience": 0.76,
        "support": ["用户明确说在忙", "手机有活跃信号"],
        "against": ["手机活跃可能只是会议间隙或通知"],
        "missing": ["忙碌是否已经结束"],
        "debug_against": ["手机活跃不能覆盖忙碌声明"],
        "attention_targets": ["conflict_busy_active", "restraint_busy"],
        "suggested_next_check_sec": 1200,
    },
    "low_confidence_sleep": {
        "compact_text": "较久没有新解锁，环境很暗且手机在充电。睡眠可能性值得注意，但证据不足以确认睡着。",
        "world_state": {"sleep_state": "possible_low_confidence", "activity_state": "inactive"},
        "label": "可能睡眠但证据不足", "confidence": 0.38, "salience": 0.8,
        "support": ["较久没有新解锁", "环境很暗", "手机在充电"],
        "against": ["没有睡眠阶段"],
        "missing": ["连续屏幕状态", "睡眠阶段"],
        "attention_targets": ["sleep_uncertain", "uncertain_important"],
        "suggested_next_check_sec": 600,
    },
    "notification_uncertain": {
        "compact_text": "短时间内通知变多但互动证据不足。只能说明外部打扰增多，不能确认正在聊天。",
        "world_state": {"notification_state": "spike_uncertain"},
        "label": "外部打扰增多", "confidence": 0.58, "salience": 0.52,
        "support": ["短时间内通知增多"],
        "against": ["没有连续亮屏或回复证据"],
        "missing": ["通知来源", "用户是否查看或回复"],
        "attention_targets": ["notification_uncertain"],
        "suggested_next_check_sec": 900,
    },
    "camera_disabled": {
        "compact_text": "摄像头来源关闭，当前没有画面或视觉识别结果。不能推断视觉状态，只能按其他来源继续观察。",
        "world_state": {"camera_evidence": "disabled"},
        "label": "视觉证据缺失", "confidence": 0.9, "salience": 0.45,
        "support": ["摄像头来源关闭"],
        "missing": ["画面", "视觉识别结果"],
        "attention_targets": ["missing_camera_evidence"],
        "suggested_next_check_sec": 1200,
    },
    "pc_disabled": {
        "compact_text": "PC activity 来源关闭，当前没有电脑前台窗口或键鼠活动证据。不能确认电脑前状态，也不能由手机低活动反推出忙闲。",
        "world_state": {"pc_activity": "disabled"},
        "label": "PC证据缺失", "confidence": 0.9, "salience": 0.45,
        "support": ["PC activity 来源关闭"],
        "missing": ["电脑前台窗口", "键鼠活动"],
        "attention_targets": ["missing_pc_evidence"],
        "suggested_next_check_sec": 1200,
    },
    "charging_home_low_activity": {
        "compact_text": "当前位置在家且正在充电，手机活动较低。这仍缺少活跃证据，不能直接判断已经睡觉或空闲。",
        "world_state": {"location_state": "at_home", "activity_state": "low_signal", "power_state": "charging"},
        "label": "低活动在家", "confidence": 0.54, "salience": 0.48,
        "support": ["在家", "手机正在充电", "活动较低"],
        "against": ["没有睡眠阶段或连续无活动窗口"],
        "missing": ["睡眠阶段", "最近屏幕状态"],
        "attention_targets": ["low_signal"],
        "suggested_next_check_sec": 1200,
    },
    "mood_uncertain": {
        "compact_text": "音乐或内容偏伤感只能算弱线索。不能只凭音乐推断心情，缺少聊天原文或直接表达。",
        "world_state": {"mood_state": "unknown"},
        "label": "情绪线索不足", "confidence": 0.34, "salience": 0.5,
        "support": ["音乐内容可能偏伤感"],
        "against": ["音乐不能直接代表心情"],
        "missing": ["聊天原文", "直接情绪表达"],
        "attention_targets": ["mood_uncertain"],
        "suggested_next_check_sec": 1200,
    },
    "long_silence": {
        "compact_text": "已经较长时间没有互动，但缺少最近活跃证据。只能标记长沉默，不应把它当作明确好时机。",
        "world_state": {"chat_gap": "long", "availability": "unknown"},
        "label": "长沉默但缺少时机证据", "confidence": 0.62, "salience": 0.58,
        "support": ["较长时间没有互动"],
        "against": ["缺少最近活跃证据"],
        "missing": ["当前是否醒着", "是否忙碌"],
        "attention_targets": ["long_silence"],
        "suggested_next_check_sec": 1200,
    },
}


def _build_recent_chat_cooldown(f: _AttentionFeatures) -> dict[str, Any]:
    minutes = _first_match(f.text, r"(\d+)分钟前", default="")
    if "刚正常聊天" in f.text or "最近互动正常" in f.text:
        compact_text = "最近互动正常且刚刚发生，屏幕活跃不能单独说明需要出现；主动出现收益不明确。"
        label, support, missing = "最近已互动", ["最近刚正常互动"], ["用户是否希望继续聊天"]
    else:
        time_text = f"{minutes}分钟前" if minutes else "刚才"
        compact_text = f"用户{time_text}刚有明确收束或暂停聊天的表达，不应把屏幕活跃当成好时机。"
        label, support, missing = "刚结束互动或明确暂停", ["用户明确表达暂停或稍后再聊"], ["暂停是否已经结束"]
    return _make_snapshot(
        compact_text=compact_text,
        world_state={"availability": "recently_interacted"},
        label=label, confidence=0.76, salience=0.62,
        support=support, against=["屏幕活跃不是继续聊天意愿"], missing=missing,
        debug_against=["屏幕活跃不能证明适合主动出现"],
        attention_targets=["recent_chat_cooldown"],
        suggested_next_check_sec=900,
    )


def _build_left_home(f: _AttentionFeatures) -> dict[str, Any]:
    structured = _geofence_transition(f, direction="inside_to_outside")
    distance = (
        _number_text(structured["distance_m"])
        if structured is not None
        else _first_match(
            f.text,
            r"(?:距围栏中心|距离家)约?(\d+(?:\.\d+)?)米",
            default="",
        )
    )
    accuracy = (
        _number_text(structured["accuracy_m"])
        if structured is not None
        else _first_match(f.text, r"精度\s*(\d+(?:\.\d+)?)(?:m|米)", default="")
    )
    radii_text = ""
    if structured is not None:
        enter = _number_text(structured["configured_enter_m"])
        exit_ = _number_text(structured["configured_exit_m"])
        radii_text = f"，配置进入阈值{enter}米、退出阈值{exit_}米"
    d_text = f"，距围栏中心约{distance}米" if distance else ""
    a_text = f"，定位精度约{accuracy}米" if accuracy else ""
    support = ["设备定位从家围栏内变为围栏外"]
    if distance:
        support.append(f"距围栏中心约{distance}米")
    if accuracy:
        support.append(f"定位精度约{accuracy}米")
    if structured is not None:
        support.extend([
            f"配置进入阈值{_number_text(structured['configured_enter_m'])}米",
            f"配置退出阈值{_number_text(structured['configured_exit_m'])}米",
        ])
    return _make_snapshot(
        compact_text=(
            f"手机的定位从你们标注的家范围里走到了范围外{d_text}{a_text}{radii_text}。"
            "这只是手机越过了那条边界，说明不了她离开的是哪儿、"
            "去了哪儿或者在做什么。"
        ),
        world_state={
            "device_geofence_state": "outside",
            "device_geofence_transition": "inside_to_outside",
        },
        label="设备定位越出家围栏", confidence=0.86, salience=0.82,
        support=support,
        missing=["手机是不是在她身上", "她具体在哪", "她此刻在做什么"],
        debug_support=["状态变化", *(support[1:] or [])],
        attention_targets=["location_transition"],
        suggested_next_check_sec=300,
    )


def _build_return_home(f: _AttentionFeatures) -> dict[str, Any]:
    structured = _geofence_transition(f, direction="outside_to_inside")
    distance = (
        _number_text(structured["distance_m"])
        if structured is not None
        else _first_match(
            f.text,
            r"(?:距围栏中心|距离家)约?(\d+(?:\.\d+)?)米",
            default="",
        )
    )
    accuracy = (
        _number_text(structured["accuracy_m"])
        if structured is not None
        else _first_match(f.text, r"精度\s*(\d+(?:\.\d+)?)(?:m|米)", default="")
    )
    radii_text = ""
    if structured is not None:
        enter = _number_text(structured["configured_enter_m"])
        exit_ = _number_text(structured["configured_exit_m"])
        radii_text = f"，配置进入阈值{enter}米、退出阈值{exit_}米"
    d_text = f"，距围栏中心约{distance}米" if distance else ""
    a_text = f"，定位精度约{accuracy}米" if accuracy else ""
    support = ["设备定位从家围栏外变为围栏内"]
    if distance:
        support.append(f"距围栏中心约{distance}米")
    if accuracy:
        support.append(f"定位精度约{accuracy}米")
    if structured is not None:
        support.extend([
            f"配置进入阈值{_number_text(structured['configured_enter_m'])}米",
            f"配置退出阈值{_number_text(structured['configured_exit_m'])}米",
        ])
    return _make_snapshot(
        compact_text=(
            f"手机的定位从你们标注的家范围外回到了范围里{d_text}{a_text}{radii_text}。"
            "这只是手机越过了那条边界，说明不了她回到的是哪儿、"
            "在做什么，或者是不是已经闲下来了。"
        ),
        world_state={
            "device_geofence_state": "inside",
            "device_geofence_transition": "outside_to_inside",
        },
        label="设备定位进入家围栏", confidence=0.84, salience=0.76,
        support=support,
        missing=["手机是不是在她身上", "她具体在哪", "她此刻在做什么"],
        debug_support=["状态变化", *(support[1:] or [])],
        attention_targets=["location_transition"],
        suggested_next_check_sec=600,
    )


def _build_stale_location(f: _AttentionFeatures) -> dict[str, Any]:
    minutes = _first_match(f.text, r"更新时间(\d+)分钟前", default="")
    time_text = f"{minutes}分钟前" if minutes else "较早前"
    return _make_snapshot(
        compact_text=f"位置数据过期，最后更新时间是{time_text}。不能确认仍在外出，也不能把旧位置当成当前状态。",
        world_state={"location_state": "stale"},
        label="过期位置不可确认", confidence=0.68, salience=0.55,
        support=["有旧位置记录"], against=["位置更新时间过旧"],
        missing=["最新定位", "最近活动"],
        attention_targets=["stale_location"],
        suggested_next_check_sec=900,
    )


def _build_stable_outside(f: _AttentionFeatures) -> dict[str, Any]:
    minutes = _first_match(
        f.text,
        r"(?:持续 outside |定位更新时间约?)(\d+)分钟",
        default="",
    )
    age_text = f"，最近定位更新于约{minutes}分钟前" if minutes else ""
    return _make_snapshot(
        compact_text=(
            f"最近一次定位还在你们标注的家范围外{age_text}，"
            "之后没有新的进出记录。这说明不了她人在不在那个范围里、"
            "去了哪儿或者在做什么。"
        ),
        world_state={
            "device_geofence_state": "outside",
            "device_geofence_transition": "none",
        },
        label="设备定位在家围栏外（无新变化）", confidence=0.7, salience=0.52,
        support=["最近一次设备定位在家围栏外"],
        against=["没有观测到新的围栏边界变化"],
        missing=["手机是不是在她身上", "她具体在哪", "她此刻在做什么"],
        attention_targets=["outside_state"],
        suggested_next_check_sec=1200,
    )


def _build_busy(f: _AttentionFeatures) -> dict[str, Any]:
    minutes = _first_match(f.text, r"(\d+)分钟前.*?专心写作业", default="")
    time_text = f"{minutes}分钟前" if minutes else "最近"
    return _make_snapshot(
        compact_text=f"用户{time_text}明确说要专心写作业，PC前台像是开发工具，手机只是低频解锁。当前更像在忙正事，主动打扰风险较高。",
        world_state={"availability": "busy_likely", "recent_activity": "work_study"},
        label="正在忙正事", confidence=0.78, salience=0.72,
        support=["用户明确说要专心写作业", "PC前台像开发工具"],
        against=["手机有低频解锁"], missing=["任务是否已经结束"],
        debug_against=["手机低频解锁不能证明空闲"],
        attention_targets=["restraint_busy"],
        suggested_next_check_sec=1800,
    )


def _build_motion_uncertain(f: _AttentionFeatures) -> dict[str, Any]:
    against = []
    if "步数没有连续增长" in f.text:
        against.append("步数没有连续增长")
    if "屏幕关闭" in f.text:
        against.append("随后屏幕关闭")
    return _make_snapshot(
        compact_text="只有一次运动读数接近跑步，但步数没有连续增长，随后屏幕关闭。更可能是拿起手机或短暂移动，不能确认正在跑步。",
        world_state={"motion_state": "uncertain"},
        label="短暂移动而非持续运动", confidence=0.64, salience=0.55,
        support=["单次 running 读数"], against=against or ["缺少持续运动证据"],
        missing=["连续运动窗口", "心率变化"],
        attention_targets=["motion_uncertain"],
        suggested_next_check_sec=900,
    )


def _build_possible_idle(f: _AttentionFeatures) -> dict[str, Any]:
    window = _first_match(f.text, r"过去(\d+)分钟", default="40")
    return _make_snapshot(
        compact_text=f"过去{window}分钟多次切换社交和视频应用，屏幕频繁点亮，最近没有明确忙碌声明。可能处于空闲或轻度娱乐状态，但不能确认心情和是否愿意聊天。",
        world_state={"availability": "possibly_idle", "recent_activity": "social_or_video"},
        label="可能空闲或轻度娱乐", confidence=0.66, salience=0.68,
        support=["社交/视频应用多次切换", "屏幕频繁点亮"],
        against=["没有直接表达想聊天"], missing=["当前心情", "是否希望被打扰"],
        attention_targets=["possible_good_timing"],
        suggested_next_check_sec=900,
    )


def _build_sleep_uncertain(f: _AttentionFeatures) -> dict[str, Any]:
    minutes = _first_match(f.text, r"(\d+)分钟前解锁", default="")
    unlock_text = f"过去{minutes}分钟内有一次解锁" if minutes else "最近有一次解锁"
    dark_text = "，环境偏暗" if "环境光较暗" in f.text else ""
    unlock_support = f"{minutes}分钟前有一次解锁" if minutes else "最近有一次解锁"
    return _make_snapshot(
        compact_text=f"{unlock_text}{dark_text}，近30分钟没有连续使用记录。不能确认已经睡着，也不能确认正在长时间刷手机。",
        world_state={"sleep_state": "unknown", "activity_state": "low_signal"},
        label="可能短暂醒着或查看手机", confidence=0.48, salience=0.64,
        support=[unlock_support], against=["没有连续使用记录"],
        missing=["睡眠阶段", "连续屏幕状态"],
        attention_targets=["sleep_uncertain"],
        suggested_next_check_sec=1200,
    )


def _build_pc_context(f: _AttentionFeatures) -> dict[str, Any]:
    if "pc_active" in f.tags:
        state = "active"
        text = "PC 最近仍活跃，这是用户可能在电脑前的上下文，也可以作为交给主脑判断是否看一眼的弱线索；但不能单独证明需要打扰。"
        label = "PC 活跃机会"
        support = ["PC active"]
        against = ["只有 PC 活跃不足以打扰，需要结合长沉默、任务上下文或作息节点"]
        attention_targets = ["pc_activity_context", "screen_check_opportunity"]
    elif "pc_idle" in f.tags:
        state = "idle"
        text = "PC 当前处于 idle，键鼠近期缺少输入。这个信号降低打扰把握，不能当作空闲邀请。"
        label = "PC 空闲或离开"
        support = ["PC idle"]
        against = ["PC idle 不适合作为看屏幕理由"]
        attention_targets = ["pc_activity_context"]
    elif "pc_locked" in f.tags:
        state = "locked"
        text = "PC 当前锁屏。用户可能离开电脑或暂时不可用，不应把旧前台应用当作当前活动。"
        label = "PC 锁屏"
        support = ["PC locked"]
        against = ["锁屏时不应请求看屏幕"]
        attention_targets = ["pc_activity_context"]
    elif "pc_offline" in f.tags:
        state = "offline"
        text = "PC activity 来源离线，当前没有电脑前台窗口或键鼠活动证据。"
        label = "PC 离线"
        support = ["PC offline"]
        against = ["PC activity 来源离线"]
        attention_targets = ["pc_activity_context"]
    else:
        state = "unknown"
        text = "PC 状态未知，只能作为很弱的上下文，不能派生正在电脑前。"
        label = "PC 状态未知"
        support = ["PC unknown"]
        against = ["PC 状态未知"]
        attention_targets = ["pc_activity_context"]

    foreground = _pc_foreground_state(f.tags)
    world_state = {"pc_activity": state}
    if foreground:
        world_state["pc_foreground"] = foreground
        support.append(f"PC foreground {foreground}")
    return _make_snapshot(
        compact_text=text,
        world_state=world_state,
        label=label, confidence=0.58, salience=0.42,
        support=support,
        against=against,
        missing=["用户是否希望被打扰"],
        attention_targets=attention_targets,
        suggested_next_check_sec=900,
    )


_DYNAMIC_BUILDERS: dict[str, Any] = {
    "recent_chat_cooldown": _build_recent_chat_cooldown,
    "return_home_transition": _build_return_home,
    "left_home_transition": _build_left_home,
    "stale_location": _build_stale_location,
    "stable_outside": _build_stable_outside,
    "busy_study": _build_busy,
    "motion_uncertain": _build_motion_uncertain,
    "possible_idle": _build_possible_idle,
    "sleep_uncertain": _build_sleep_uncertain,
    "pc_context": _build_pc_context,
}

_FALLBACK_SNAPSHOT: dict[str, Any] = {
    "compact_text": "当前只有零散观察，缺少足够证据确认用户状态；不要把弱信号写成确定事实。",
    "world_state": {"availability": "unknown"},
    "attention_targets": ["low_signal"],
    "missing": ["连续活动窗口", "近期聊天语境", "位置或日程信号"],
}


def _resolve_snapshot(features: _AttentionFeatures) -> dict[str, Any]:
    for tag in _SNAPSHOT_PRIORITY:
        if tag not in features.tags:
            continue
        if tag in _DYNAMIC_BUILDERS:
            return _DYNAMIC_BUILDERS[tag](features)
        data = _STATIC_SNAPSHOTS.get(tag)
        if data is not None:
            return _make_snapshot(**data)
    return _make_snapshot(**_FALLBACK_SNAPSHOT)


def build_attention_snapshot_from_case(case: Mapping[str, Any]) -> dict[str, Any]:
    input_payload = case.get("input")
    if not isinstance(input_payload, Mapping):
        raise ValueError(f"case {case.get('id') or '<unknown>'} requires input object")
    return build_attention_snapshot(input_payload)


def build_attention_snapshot(input_payload: Mapping[str, Any]) -> dict[str, Any]:
    bundle = adapt_replay_input(input_payload)
    config = resolve_attention_config(input_payload.get("attention_config"))
    features = _extract_attention_features(
        evidence=bundle.evidence,
        recent_chat=bundle.recent_chat,
        context_tags=bundle.context_tags,
    )

    snapshot = _resolve_snapshot(features)

    _apply_attention_config(snapshot, config)
    snapshot["generated_at"] = bundle.reference_time
    snapshot["debug_trace"].update({
        "source_records": list(features.source_records),
        "raw_signal_count": features.raw_signal_count,
        "recent_chat_count": features.recent_chat_count,
        "feature_tags": sorted(features.tags),
    })
    return snapshot


def _apply_attention_config(snapshot: dict[str, Any], config: Mapping[str, Any]) -> None:
    snapshot["suggested_next_check_sec"] = _clamp_next_check(
        snapshot["suggested_next_check_sec"],
        config,
    )
    snapshot["compact_text"] = _limit_compact_text(
        snapshot["compact_text"],
        config["compact_text_max_chars"],
    )


def _clamp_next_check(value: int, config: Mapping[str, Any]) -> int:
    return min(
        max(value, config["next_check_min_sec"]),
        config["next_check_max_sec"],
    )


def _limit_compact_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return f"{text[:max_chars - 3].rstrip()}..."


def _extract_attention_features(
    *,
    evidence: Sequence[EvidenceRecord],
    recent_chat: Sequence[str],
    context_tags: Iterable[str] = (),
) -> _AttentionFeatures:
    text = _join_text([record.text for record in evidence] + list(recent_chat))
    tags = set().union(*(record.tags for record in evidence)) if evidence else set()
    tags.update(context_tags)

    if {"busy_declared", "phone_active"}.issubset(tags):
        tags.add("conflict_busy_active")
    if "last_unlock_old" in tags and ("dark_environment" in tags or "charging" in tags):
        tags.add("low_confidence_sleep")
    if {"outside_continuous", "no_location_change"}.issubset(tags):
        tags.add("stable_outside")
    if {"at_home", "charging", "low_activity"}.issubset(tags):
        tags.add("charging_home_low_activity")
    if "social_video_activity" in tags or {"social_activity", "video_activity"}.issubset(tags):
        tags.add("possible_idle")
    if "unlock_event" in tags and ("dark_environment" in tags or "no_continuous_activity" in tags):
        tags.add("sleep_uncertain")
    if tags.intersection({"pc_active", "pc_idle", "pc_locked", "pc_offline", "pc_unknown"}):
        tags.add("pc_context")

    return _AttentionFeatures(
        text=text,
        source_records=tuple(_unique(record.kind for record in evidence)),
        raw_signal_count=len(evidence),
        recent_chat_count=len(recent_chat),
        tags=frozenset(tags),
        geofence_payloads=tuple(
            normalize_location_geofence_payload(record.payload)
            for record in evidence
            if record.payload.get("payload_schema")
            == LOCATION_GEOFENCE_PAYLOAD_SCHEMA_VERSION
        ),
    )


def _geofence_transition(
    features: _AttentionFeatures,
    *,
    direction: str,
) -> Mapping[str, Any] | None:
    return next(
        (
            payload
            for payload in reversed(features.geofence_payloads)
            if payload.get("event_type") == "transition"
            and payload.get("geofence_direction") == direction
        ),
        None,
    )


def _number_text(value: int | float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _make_snapshot(
    *,
    compact_text: str,
    world_state: Mapping[str, Any],
    label: str = "",
    confidence: float = 0.5,
    salience: float = 0.5,
    support: Sequence[str] = (),
    against: Sequence[str] = (),
    missing: Sequence[str] = (),
    debug_support: Sequence[str] | None = None,
    debug_against: Sequence[str] | None = None,
    debug_missing: Sequence[str] | None = None,
    attention_targets: Sequence[str] = (),
    suggested_next_check_sec: int = 1200,
) -> dict[str, Any]:
    hypotheses = []
    if label:
        hypotheses.append({
            "label": label,
            "confidence": confidence,
            "salience": salience,
            "support": list(support),
            "against": list(against),
            "missing": list(missing),
        })
    return {
        "schema_version": ATTENTION_SNAPSHOT_SCHEMA_VERSION,
        "compact_text": compact_text,
        "world_state": dict(world_state),
        "hypotheses": hypotheses,
        "attention_targets": list(attention_targets),
        "suggested_next_check_sec": suggested_next_check_sec,
        "debug_trace": {
            "support": list(debug_support if debug_support is not None else support),
            "against": list(debug_against if debug_against is not None else against),
            "missing": list(debug_missing if debug_missing is not None else missing),
        },
    }


def _first_match(text: str, pattern: str, *, default: str) -> str:
    match = re.search(pattern, text)
    if not match:
        return default
    return match.group(1)


def _join_text(parts: Sequence[str]) -> str:
    return "\n".join(part for part in parts if part)


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _pc_foreground_state(tags: frozenset[str] | set[str]) -> str:
    if "pc_foreground_dev_tool" in tags:
        return "dev_tool"
    if "pc_foreground_browser" in tags:
        return "browser"
    if "pc_foreground_media" in tags:
        return "media"
    return ""


__all__ = [
    "ATTENTION_SNAPSHOT_SCHEMA_VERSION",
    "build_attention_snapshot",
    "build_attention_snapshot_from_case",
]
