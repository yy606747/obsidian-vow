"""Pure-Python Presence contracts, interpolation, geometry, and disk state."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image


MAX_DURATION_MS = 600_000
MAX_TRACKS = 5
MAX_KEYS_PER_TRACK = 32
MAX_TOTAL_KEYS = 120
MAX_PNG_BYTES = 20 * 1024 * 1024
MAX_SPRITE_EDGE = 4096
MAX_PLAYBACK_TIMER_GAP_SEC = 1.0
WINDOW_BOUNDS_PADDING_DIP = 8.0
MAX_WINDOW_AXIS_FRACTION = 0.5
LONG_INTERVAL_MIN_MS = 20_000
INTERVAL_VALUE_ABS_TOL = 1e-9
MOVING_TICK_INTERVAL_MS = 16
LONG_TICK_INTERVAL_MS = 100
LOCAL_IDLE_PERIOD_MS = 8_000
LOCAL_IDLE_ENVELOPE_MS = 1_000
LOCAL_IDLE_Y_AMPLITUDE_DIP = 4.0
LOCAL_IDLE_SCALE_MAX = 1.03
MAX_GEOMETRY_SAMPLES_PER_INTERVAL = 64
MAX_GEOMETRY_SAMPLES = 1 + (MAX_TOTAL_KEYS + 1) * MAX_GEOMETRY_SAMPLES_PER_INTERVAL
PROPS = frozenset({"x", "y", "scale", "rotation", "opacity"})
TARGETS = frozenset({"active", "primary"})
ANCHORS = frozenset(
    {
        "top_left", "top_center", "top_right",
        "center_left", "center", "center_right",
        "bottom_left", "bottom_center", "bottom_right",
    }
)
ORIGINS = frozenset({"center", "top_center", "bottom_center"})
EASINGS = frozenset(
    {
        "linear", "in_quad", "out_quad", "in_out_quad",
        "in_cubic", "out_cubic", "in_out_cubic",
    }
)
DEFAULT_VALUES = {"x": 0.0, "y": 0.0, "scale": 1.0, "rotation": 0.0, "opacity": 1.0}
VALUE_LIMITS = {
    "x": (-8192.0, 8192.0),
    "y": (-8192.0, 8192.0),
    "scale": (0.05, 4.0),
    "rotation": (-720.0, 720.0),
    "opacity": (0.0, 1.0),
}
TERMINAL_ACKS = frozenset({"played", "rejected", "expired", "superseded"})
_HASH_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_SPRITE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class PresenceContractError(ValueError):
    pass


@dataclass(frozen=True)
class GeometryAnalysis:
    bounds: tuple[int, int, int, int]
    local_idle_enabled: bool
    sample_count: int


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PresenceContractError(f"{label}:number_required")
    result = float(value)
    if not math.isfinite(result):
        raise PresenceContractError(f"{label}:finite_required")
    return result


def validate_trajectory(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PresenceContractError("trajectory:object_required")
    required = {"sprite_id", "target_screen", "anchor", "transform_origin", "duration_ms", "tracks"}
    if set(value) != required:
        raise PresenceContractError("trajectory:keys")
    sprite_id = str(value.get("sprite_id") or "")
    if not _SPRITE_ID_RE.fullmatch(sprite_id):
        raise PresenceContractError("sprite_id:invalid")
    target = str(value.get("target_screen") or "")
    anchor = str(value.get("anchor") or "")
    origin = str(value.get("transform_origin") or "")
    if target not in TARGETS or anchor not in ANCHORS or origin not in ORIGINS:
        raise PresenceContractError("trajectory:enum")
    duration = value.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= MAX_DURATION_MS:
        raise PresenceContractError("duration_ms:invalid")
    raw_tracks = value.get("tracks")
    if not isinstance(raw_tracks, list) or not 1 <= len(raw_tracks) <= MAX_TRACKS:
        raise PresenceContractError("tracks:count")
    tracks = []
    seen = set()
    total = 0
    for index, raw in enumerate(raw_tracks):
        if not isinstance(raw, dict) or not {"prop", "keys"} <= set(raw) <= {"prop", "keys", "ease"}:
            raise PresenceContractError(f"tracks[{index}]:keys")
        prop = str(raw.get("prop") or "")
        ease = str(raw.get("ease") or "linear")
        keys = raw.get("keys")
        if prop not in PROPS or prop in seen or ease not in EASINGS:
            raise PresenceContractError(f"tracks[{index}]:metadata")
        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS_PER_TRACK:
            raise PresenceContractError(f"tracks[{index}]:key_count")
        seen.add(prop)
        total += len(keys)
        if total > MAX_TOTAL_KEYS:
            raise PresenceContractError("tracks:complexity")
        normalized_keys = []
        previous = -1
        low, high = VALUE_LIMITS[prop]
        for key_index, pair in enumerate(keys):
            if not isinstance(pair, list) or len(pair) != 2:
                raise PresenceContractError(f"tracks[{index}].keys[{key_index}]:pair")
            key_time = pair[0]
            if isinstance(key_time, bool) or not isinstance(key_time, int):
                raise PresenceContractError("key_time:integer_required")
            if key_time <= previous or not 0 <= key_time <= duration:
                raise PresenceContractError("key_time:invalid")
            number = _finite_number(pair[1], "key_value")
            if not low <= number <= high:
                raise PresenceContractError("key_value:range")
            previous = key_time
            normalized_keys.append([key_time, int(number) if number.is_integer() else number])
        tracks.append({"prop": prop, "keys": normalized_keys, "ease": ease})
    return {
        "sprite_id": sprite_id,
        "target_screen": target,
        "anchor": anchor,
        "transform_origin": origin,
        "duration_ms": duration,
        "tracks": tracks,
    }


def easing_value(name: str, value: float) -> float:
    """Evaluate one allowed monotonic easing at a normalized position."""

    value = min(1.0, max(0.0, float(value)))
    if name == "in_quad":
        return value * value
    if name == "out_quad":
        return 1 - (1 - value) ** 2
    if name == "in_out_quad":
        return 2 * value * value if value < 0.5 else 1 - ((-2 * value + 2) ** 2) / 2
    if name == "in_cubic":
        return value**3
    if name == "out_cubic":
        return 1 - (1 - value) ** 3
    if name == "in_out_cubic":
        return 4 * value**3 if value < 0.5 else 1 - ((-2 * value + 2) ** 3) / 2
    return value


_ease = easing_value


def _track_value(track: dict[str, Any], at_ms: float) -> float:
    keys = track["keys"]
    if at_ms <= keys[0][0]:
        return float(keys[0][1])
    if at_ms >= keys[-1][0]:
        return float(keys[-1][1])
    for left, right in zip(keys, keys[1:]):
        if left[0] <= at_ms <= right[0]:
            span = right[0] - left[0]
            progress = easing_value(track["ease"], (at_ms - left[0]) / span)
            return float(left[1]) + (float(right[1]) - float(left[1])) * progress
    return float(keys[-1][1])


def evaluate_trajectory(trajectory: dict[str, Any], at_ms: float) -> dict[str, float]:
    values = dict(DEFAULT_VALUES)
    at_ms = min(float(trajectory["duration_ms"]), max(0.0, float(at_ms)))
    for track in trajectory["tracks"]:
        values[track["prop"]] = _track_value(track, at_ms)
    return values


def trajectory_intervals(
    trajectory: dict[str, Any],
) -> list[dict[str, int | bool]]:
    """Classify union-key intervals for frame-rate and local-idle decisions.

    Equality at both interval endpoints implies constancy only because the
    boundaries are the union of every track's keys and every allowed easing is
    strictly monotonic.  Adding an overshooting easing requires upgrading this
    classifier before adding it to ``EASINGS``.
    """

    duration_ms = int(trajectory["duration_ms"])
    boundaries = {0, duration_ms}
    for track in trajectory["tracks"]:
        boundaries.update(int(pair[0]) for pair in track["keys"])
    ordered = sorted(boundaries)
    resolved = {
        at_ms: evaluate_trajectory(trajectory, at_ms)
        for at_ms in ordered
    }
    intervals: list[dict[str, int | bool]] = []
    for start_ms, end_ms in zip(ordered, ordered[1:]):
        start_values = resolved[start_ms]
        end_values = resolved[end_ms]
        constant = all(
            math.isclose(
                start_values[prop],
                end_values[prop],
                rel_tol=0.0,
                abs_tol=INTERVAL_VALUE_ABS_TOL,
            )
            for prop in DEFAULT_VALUES
        )
        is_long = end_ms - start_ms >= LONG_INTERVAL_MIN_MS
        intervals.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "is_long": is_long,
                "is_dwell": bool(
                    is_long and constant and start_values["opacity"] > 0.0
                ),
            }
        )
    return intervals


def trajectory_interval_at(
    intervals: list[dict[str, int | bool]], at_ms: float
) -> dict[str, int | bool] | None:
    if not intervals:
        return None
    bounded_ms = max(0.0, float(at_ms))
    for interval in intervals:
        if int(interval["start_ms"]) <= bounded_ms < int(interval["end_ms"]):
            return interval
    return None


def playback_tick_interval_ms(
    trajectory: dict[str, Any],
    at_ms: float,
    *,
    intervals: list[dict[str, int | bool]] | None = None,
) -> int:
    classified = intervals if intervals is not None else trajectory_intervals(trajectory)
    interval = trajectory_interval_at(classified, at_ms)
    return (
        LONG_TICK_INTERVAL_MS
        if interval is not None and bool(interval["is_long"])
        else MOVING_TICK_INTERVAL_MS
    )


def _smoothstep(value: float) -> float:
    bounded = min(1.0, max(0.0, float(value)))
    return bounded * bounded * (3.0 - 2.0 * bounded)


def local_idle_adjustment(
    trajectory: dict[str, Any],
    at_ms: float,
    *,
    intervals: list[dict[str, int | bool]] | None = None,
) -> dict[str, float]:
    classified = intervals if intervals is not None else trajectory_intervals(trajectory)
    interval = trajectory_interval_at(classified, at_ms)
    if interval is None or not bool(interval["is_dwell"]):
        return {"y_offset": 0.0, "scale_multiplier": 1.0}

    start_ms = float(interval["start_ms"])
    end_ms = float(interval["end_ms"])
    position_ms = max(0.0, float(at_ms) - start_ms)
    remaining_ms = max(0.0, end_ms - float(at_ms))
    envelope = min(
        _smoothstep(position_ms / LOCAL_IDLE_ENVELOPE_MS),
        _smoothstep(remaining_ms / LOCAL_IDLE_ENVELOPE_MS),
    )
    phase = (position_ms % LOCAL_IDLE_PERIOD_MS) / LOCAL_IDLE_PERIOD_MS
    radians = math.tau * phase
    return {
        "y_offset": LOCAL_IDLE_Y_AMPLITUDE_DIP * math.sin(radians) * envelope,
        "scale_multiplier": 1.0
        + (LOCAL_IDLE_SCALE_MAX - 1.0)
        * ((1.0 - math.cos(radians)) / 2.0)
        * envelope,
    }


def rendered_trajectory_values(
    trajectory: dict[str, Any],
    at_ms: float,
    *,
    local_idle_enabled: bool,
    intervals: list[dict[str, int | bool]] | None = None,
) -> dict[str, float]:
    values = evaluate_trajectory(trajectory, at_ms)
    if not local_idle_enabled:
        return values
    adjustment = local_idle_adjustment(
        trajectory,
        at_ms,
        intervals=intervals,
    )
    values["y"] += adjustment["y_offset"]
    values["scale"] *= adjustment["scale_multiplier"]
    return values


def playback_interruption_reason(
    *,
    locked: bool | None,
    last_tick_at: float,
    now: float,
    max_timer_gap_sec: float = MAX_PLAYBACK_TIMER_GAP_SEC,
) -> str:
    """Return why an in-progress animation can no longer claim ``played``."""
    if locked is True:
        return "locked_during_playback"
    gap = float(now) - float(last_tick_at)
    if not math.isfinite(gap) or gap < 0 or gap > float(max_timer_gap_sec):
        return "playback_timer_gap"
    return ""


def _axis_point(name: str, extent: float) -> float:
    if name in {"left", "top"}:
        return 0.0
    if name in {"right", "bottom"}:
        return extent
    return extent / 2.0


def _anchor_points(anchor: str, width: float, height: float) -> tuple[float, float]:
    if anchor == "center":
        vertical, horizontal = "center", "center"
    else:
        vertical, horizontal = anchor.split("_", 1)
    return _axis_point(horizontal, width), _axis_point(vertical, height)


def _origin_point(origin: str, width: float, height: float) -> tuple[float, float]:
    if origin == "center":
        return width / 2.0, height / 2.0
    return width / 2.0, 0.0 if origin == "top_center" else height


def frame_bounds(
    trajectory: dict[str, Any],
    values: dict[str, float],
    *,
    work_width: float,
    work_height: float,
    sprite_width: float,
    sprite_height: float,
) -> tuple[float, float, float, float]:
    screen_anchor = _anchor_points(trajectory["anchor"], work_width, work_height)
    sprite_anchor = _anchor_points(trajectory["anchor"], sprite_width, sprite_height)
    origin = _origin_point(trajectory["transform_origin"], sprite_width, sprite_height)
    translate_x = screen_anchor[0] + values["x"] - sprite_anchor[0]
    translate_y = screen_anchor[1] + values["y"] - sprite_anchor[1]
    scale = values["scale"]
    radians = math.radians(values["rotation"])
    cosine, sine = math.cos(radians), math.sin(radians)
    points = []
    for x, y in ((0.0, 0.0), (sprite_width, 0.0), (sprite_width, sprite_height), (0.0, sprite_height)):
        local_x = (x - origin[0]) * scale
        local_y = (y - origin[1]) * scale
        rotated_x = local_x * cosine - local_y * sine + origin[0] + translate_x
        rotated_y = local_x * sine + local_y * cosine + origin[1] + translate_y
        points.append((rotated_x, rotated_y))
    xs, ys = [point[0] for point in points], [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


_GEOMETRY_CURVE_TOLERANCES = {
    "x": 0.25,
    "y": 0.25,
    "scale": 0.001,
    "rotation": 0.25,
}


def _rotation_crossing_time(
    trajectory: dict[str, Any],
    start_ms: float,
    end_ms: float,
    target_rotation: float,
) -> float:
    start_rotation = evaluate_trajectory(trajectory, start_ms)["rotation"]
    increasing = evaluate_trajectory(trajectory, end_ms)["rotation"] > start_rotation
    low, high = float(start_ms), float(end_ms)
    for _ in range(32):
        midpoint = (low + high) / 2.0
        value = evaluate_trajectory(trajectory, midpoint)["rotation"]
        if (value < target_rotation) == increasing:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2.0


def _needs_geometry_subdivision(
    trajectory: dict[str, Any], left_ms: float, right_ms: float
) -> bool:
    midpoint = (left_ms + right_ms) / 2.0
    left = evaluate_trajectory(trajectory, left_ms)
    right = evaluate_trajectory(trajectory, right_ms)
    middle = evaluate_trajectory(trajectory, midpoint)
    return any(
        abs(middle[prop] - (left[prop] + right[prop]) / 2.0) > tolerance
        for prop, tolerance in _GEOMETRY_CURVE_TOLERANCES.items()
    )


def _interval_geometry_sample_times(
    trajectory: dict[str, Any], start_ms: int, end_ms: int
) -> list[float]:
    span = float(end_ms - start_ms)
    samples = {
        float(start_ms) + span * index / 8.0
        for index in range(9)
    }
    start_rotation = evaluate_trajectory(trajectory, start_ms)["rotation"]
    end_rotation = evaluate_trajectory(trajectory, end_ms)["rotation"]
    if not math.isclose(start_rotation, end_rotation, rel_tol=0.0, abs_tol=1e-12):
        low_rotation, high_rotation = sorted((start_rotation, end_rotation))
        first_quadrant = math.floor(low_rotation / 90.0) + 1
        last_quadrant = math.ceil(high_rotation / 90.0) - 1
        for quadrant in range(first_quadrant, last_quadrant + 1):
            target = quadrant * 90.0
            samples.add(
                _rotation_crossing_time(
                    trajectory,
                    start_ms,
                    end_ms,
                    target,
                )
            )

    pending = list(zip(sorted(samples), sorted(samples)[1:]))
    while pending and len(samples) < MAX_GEOMETRY_SAMPLES_PER_INTERVAL:
        left_ms, right_ms = pending.pop()
        if right_ms - left_ms <= 1e-6:
            continue
        if not _needs_geometry_subdivision(trajectory, left_ms, right_ms):
            continue
        midpoint = (left_ms + right_ms) / 2.0
        if midpoint in samples:
            continue
        samples.add(midpoint)
        pending.append((left_ms, midpoint))
        pending.append((midpoint, right_ms))
    return sorted(samples)


def trajectory_sample_times(trajectory: dict[str, Any]) -> list[float]:
    sample_times: set[float] = set()
    for interval in trajectory_intervals(trajectory):
        sample_times.update(
            _interval_geometry_sample_times(
                trajectory,
                int(interval["start_ms"]),
                int(interval["end_ms"]),
            )
        )
    sample_times.add(0.0)
    sample_times.add(float(trajectory["duration_ms"]))
    if len(sample_times) > MAX_GEOMETRY_SAMPLES:
        # This is unreachable for a protocol-valid trajectory: at most 121
        # intervals each contribute at most 64 samples.  Keep the check as an
        # internal invariant, not as a normal model-output rejection path.
        raise PresenceContractError("geometry:sample_budget")
    return sorted(sample_times)


_trajectory_sample_times = trajectory_sample_times


def _trajectory_frames(
    trajectory: dict[str, Any],
    sample_times: list[float],
    *,
    work_width: float,
    work_height: float,
    sprite_width: float,
    sprite_height: float,
) -> list[tuple[float, float, float, float]]:
    return [
        frame_bounds(
            trajectory,
            evaluate_trajectory(trajectory, at_ms),
            work_width=work_width,
            work_height=work_height,
            sprite_width=sprite_width,
            sprite_height=sprite_height,
        )
        for at_ms in sample_times
    ]


def _validate_geometry_frames(
    frames: list[tuple[float, float, float, float]],
    *,
    work_width: float,
    work_height: float,
) -> None:
    intersects = False
    for left, top, right, bottom in frames:
        width, height = right - left, bottom - top
        center_x, center_y = (left + right) / 2.0, (top + bottom) / 2.0
        if not (-width <= center_x <= work_width + width):
            raise PresenceContractError("geometry:x_overscan")
        if not (-height <= center_y <= work_height + height):
            raise PresenceContractError("geometry:y_overscan")
        intersects = intersects or (
            right > 0 and bottom > 0 and left < work_width and top < work_height
        )
    if not intersects:
        raise PresenceContractError("geometry:never_visible")


def _padded_window_bounds(
    frames: list[tuple[float, float, float, float]],
    *,
    work_width: float,
    work_height: float,
) -> tuple[int, int, int, int]:
    left = math.floor(min(frame[0] for frame in frames) - WINDOW_BOUNDS_PADDING_DIP)
    top = math.floor(min(frame[1] for frame in frames) - WINDOW_BOUNDS_PADDING_DIP)
    right = math.ceil(max(frame[2] for frame in frames) + WINDOW_BOUNDS_PADDING_DIP)
    bottom = math.ceil(max(frame[3] for frame in frames) + WINDOW_BOUNDS_PADDING_DIP)
    if (
        right - left > work_width * MAX_WINDOW_AXIS_FRACTION
        or bottom - top > work_height * MAX_WINDOW_AXIS_FRACTION
    ):
        raise PresenceContractError("trajectory_bounds_too_large")
    return left, top, right, bottom


def _local_idle_extreme_frames(
    trajectory: dict[str, Any],
    intervals: list[dict[str, int | bool]],
    *,
    work_width: float,
    work_height: float,
    sprite_width: float,
    sprite_height: float,
) -> list[tuple[float, float, float, float]]:
    frames = []
    for interval in intervals:
        if not bool(interval["is_dwell"]):
            continue
        base = evaluate_trajectory(trajectory, float(interval["start_ms"]))
        for y_offset in (-LOCAL_IDLE_Y_AMPLITUDE_DIP, 0.0, LOCAL_IDLE_Y_AMPLITUDE_DIP):
            for scale_multiplier in (1.0, LOCAL_IDLE_SCALE_MAX):
                values = dict(base)
                values["y"] += y_offset
                values["scale"] *= scale_multiplier
                frames.append(
                    frame_bounds(
                        trajectory,
                        values,
                        work_width=work_width,
                        work_height=work_height,
                        sprite_width=sprite_width,
                        sprite_height=sprite_height,
                    )
                )
    return frames


def _analyze_geometry_with_sprite_dimensions(
    trajectory: dict[str, Any],
    *,
    work_width: float,
    work_height: float,
    sprite_width: float,
    sprite_height: float,
    validate_frames: bool,
) -> GeometryAnalysis:
    if min(work_width, work_height, sprite_width, sprite_height) <= 0:
        raise PresenceContractError("geometry:dimensions")
    intervals = trajectory_intervals(trajectory)
    sample_times = trajectory_sample_times(trajectory)
    pure_frames = _trajectory_frames(
        trajectory,
        sample_times,
        work_width=work_width,
        work_height=work_height,
        sprite_width=sprite_width,
        sprite_height=sprite_height,
    )
    if validate_frames:
        _validate_geometry_frames(
            pure_frames,
            work_width=work_width,
            work_height=work_height,
        )
    pure_bounds = _padded_window_bounds(
        pure_frames,
        work_width=work_width,
        work_height=work_height,
    )
    idle_frames = _local_idle_extreme_frames(
        trajectory,
        intervals,
        work_width=work_width,
        work_height=work_height,
        sprite_width=sprite_width,
        sprite_height=sprite_height,
    )
    if not idle_frames:
        return GeometryAnalysis(
            bounds=pure_bounds,
            local_idle_enabled=False,
            sample_count=len(sample_times),
        )
    try:
        augmented_bounds = _padded_window_bounds(
            pure_frames + idle_frames,
            work_width=work_width,
            work_height=work_height,
        )
    except PresenceContractError as exc:
        if str(exc) != "trajectory_bounds_too_large":
            raise
        return GeometryAnalysis(
            bounds=pure_bounds,
            local_idle_enabled=False,
            sample_count=len(sample_times),
        )
    return GeometryAnalysis(
        bounds=augmented_bounds,
        local_idle_enabled=True,
        sample_count=len(sample_times),
    )


def analyze_trajectory_geometry(
    trajectory: dict[str, Any],
    *,
    work_width: float,
    work_height: float,
    image_width_px: int,
    image_height_px: int,
    base_height_dip: float,
) -> GeometryAnalysis:
    if (
        work_width <= 0
        or work_height <= 0
        or image_width_px <= 0
        or image_height_px <= 0
    ):
        raise PresenceContractError("geometry:dimensions")
    sprite_height = float(base_height_dip)
    sprite_width = sprite_height * float(image_width_px) / float(image_height_px)
    if not 32.0 <= sprite_height <= 1200.0:
        raise PresenceContractError("geometry:base_height")
    return _analyze_geometry_with_sprite_dimensions(
        trajectory,
        work_width=work_width,
        work_height=work_height,
        sprite_width=sprite_width,
        sprite_height=sprite_height,
        validate_frames=True,
    )


def trajectory_window_bounds(
    trajectory: dict[str, Any],
    *,
    work_width: float,
    work_height: float,
    sprite_width: float,
    sprite_height: float,
) -> tuple[int, int, int, int]:
    """Return the padded compact host bounds relative to the work area."""

    return _analyze_geometry_with_sprite_dimensions(
        trajectory,
        work_width=work_width,
        work_height=work_height,
        sprite_width=sprite_width,
        sprite_height=sprite_height,
        validate_frames=False,
    ).bounds


def validate_geometry(
    trajectory: dict[str, Any],
    *,
    work_width: float,
    work_height: float,
    image_width_px: int,
    image_height_px: int,
    base_height_dip: float,
) -> None:
    analyze_trajectory_geometry(
        trajectory,
        work_width=work_width,
        work_height=work_height,
        image_width_px=image_width_px,
        image_height_px=image_height_px,
        base_height_dip=base_height_dip,
    )


def inspect_sprite_png(data: bytes, expected_hash: str | None = None) -> dict[str, Any]:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_PNG_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PresenceContractError("sprite:png_invalid")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or "A" not in image.getbands():
                raise PresenceContractError("sprite:alpha_required")
            width, height = image.size
            if width < 1 or height < 1 or max(width, height) > MAX_SPRITE_EDGE:
                raise PresenceContractError("sprite:dimensions")
            alpha = image.getchannel("A")
            extrema = alpha.getextrema()
    except PresenceContractError:
        raise
    except Exception as exc:
        raise PresenceContractError("sprite:decode_failed") from exc
    if not extrema or extrema[0] >= 255 or extrema[1] <= 0:
        raise PresenceContractError("sprite:alpha_content")
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    if expected_hash is not None and digest != normalize_hash(expected_hash):
        raise PresenceContractError("sprite:hash_mismatch")
    return {"sprite_hash": digest, "width_px": width, "height_px": height}


def normalize_hash(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(normalized):
        raise PresenceContractError("sprite:hash_invalid")
    return normalized


class SpriteCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sprite_hash: str) -> Path:
        match = _HASH_RE.fullmatch(normalize_hash(sprite_hash))
        return self.root / f"{match.group(1)}.png"

    def valid_path(self, sprite_hash: str) -> Path | None:
        path = self.path_for(sprite_hash)
        try:
            inspect_sprite_png(path.read_bytes(), sprite_hash)
        except (OSError, PresenceContractError):
            return None
        return path

    def store(self, sprite_hash: str, data: bytes) -> Path:
        inspect_sprite_png(data, sprite_hash)
        path = self.path_for(sprite_hash)
        temporary = self.root / f".{path.name}.{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(data)
        os.replace(temporary, path)
        return path


class AckJournal:
    def __init__(self, path: Path, max_entries: int = 256):
        self.path = Path(path)
        self.max_entries = max(32, int(max_entries))
        self._lock = threading.Lock()
        self._events = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            events = value.get("events") if isinstance(value, dict) else None
            return events if isinstance(events, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(self._events.items(), key=lambda item: float(item[1].get("updated_at", 0)))[-self.max_entries :]
        self._events = dict(ordered)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"events": self._events}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.path)

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._events.get(str(event_id or ""))
            return dict(value) if value else None

    def record(
        self,
        event_id: str,
        status: str,
        *,
        reason: str = "",
        actual_playback_ms: int | None = None,
        now: float,
    ) -> dict[str, Any]:
        event_id = str(event_id or "").strip()
        status = str(status or "").strip().lower()
        if not event_id or status not in {"accepted", "played", "rejected", "expired"}:
            raise PresenceContractError("ack:invalid")
        with self._lock:
            existing = self._events.get(event_id, {})
            if str(existing.get("status")) in TERMINAL_ACKS and status == "accepted":
                return dict(existing)
            value = {
                "event_id": event_id,
                "status": status,
                "reason": " ".join(str(reason or "").split())[:240],
                "actual_playback_ms": actual_playback_ms,
                "sent": False,
                "updated_at": float(now),
            }
            self._events[event_id] = value
            self._save()
            return dict(value)

    def record_ack(self, ack: dict[str, Any], *, now: float) -> dict[str, Any]:
        """Synchronously persist a player ACK payload."""
        return self.record(
            str(ack.get("event_id") or ""),
            str(ack.get("status") or ""),
            reason=str(ack.get("reason") or ""),
            actual_playback_ms=ack.get("actual_playback_ms"),
            now=now,
        )

    def mark_sent(self, event_id: str, response: dict[str, Any] | None, *, now: float) -> None:
        with self._lock:
            value = self._events.get(str(event_id or ""))
            if not value:
                return
            server_status = str((response or {}).get("status") or "")
            if server_status in TERMINAL_ACKS:
                value["status"] = server_status
                value["reason"] = str((response or {}).get("reason") or value.get("reason") or "")
                value["actual_playback_ms"] = (response or {}).get("actual_playback_ms")
            value["sent"] = True
            value["updated_at"] = float(now)
            self._save()

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(value) for value in self._events.values() if not value.get("sent")]

    def recover_incomplete(self, *, now: float) -> int:
        recovered = 0
        with self._lock:
            for value in self._events.values():
                if value.get("status") != "accepted":
                    continue
                value.update(
                    status="rejected",
                    reason="agent_restarted_after_accept",
                    actual_playback_ms=None,
                    sent=False,
                    updated_at=float(now),
                )
                recovered += 1
            if recovered:
                self._save()
        return recovered


__all__ = [
    "ANCHORS", "AckJournal", "EASINGS", "GeometryAnalysis",
    "INTERVAL_VALUE_ABS_TOL", "LOCAL_IDLE_ENVELOPE_MS",
    "LOCAL_IDLE_PERIOD_MS", "LOCAL_IDLE_SCALE_MAX",
    "LOCAL_IDLE_Y_AMPLITUDE_DIP", "LONG_INTERVAL_MIN_MS",
    "LONG_TICK_INTERVAL_MS", "MAX_DURATION_MS", "MAX_GEOMETRY_SAMPLES",
    "MAX_GEOMETRY_SAMPLES_PER_INTERVAL", "MAX_PLAYBACK_TIMER_GAP_SEC",
    "MAX_WINDOW_AXIS_FRACTION", "MOVING_TICK_INTERVAL_MS", "ORIGINS",
    "PresenceContractError", "SpriteCache", "analyze_trajectory_geometry",
    "easing_value", "evaluate_trajectory", "frame_bounds", "inspect_sprite_png",
    "local_idle_adjustment", "normalize_hash", "playback_interruption_reason",
    "playback_tick_interval_ms", "rendered_trajectory_values",
    "trajectory_interval_at", "trajectory_intervals", "trajectory_sample_times",
    "trajectory_window_bounds", "validate_geometry", "validate_trajectory",
    "WINDOW_BOUNDS_PADDING_DIP",
]
