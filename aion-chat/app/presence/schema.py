"""Strict, fail-closed contracts for Desktop Presence trajectories and sprites."""

from __future__ import annotations

import hashlib
import io
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from PIL import Image


MAX_DURATION_MS = 600_000
MAX_TRACKS = 5
MAX_KEYS_PER_TRACK = 32
MAX_TOTAL_KEYS = 120
MAX_PNG_BYTES = 20 * 1024 * 1024
MAX_SPRITE_EDGE_PX = 4096
LONG_INTERVAL_MIN_MS = 20_000
INTERVAL_VALUE_ABS_TOL = 1e-9

ALLOWED_TARGET_SCREENS = frozenset({"active", "primary"})
ALLOWED_ANCHORS = frozenset(
    {
        "top_left",
        "top_center",
        "top_right",
        "center_left",
        "center",
        "center_right",
        "bottom_left",
        "bottom_center",
        "bottom_right",
    }
)
ALLOWED_TRANSFORM_ORIGINS = frozenset(
    {"center", "top_center", "bottom_center"}
)
ALLOWED_PROPS = frozenset({"x", "y", "scale", "rotation", "opacity"})
ALLOWED_EASINGS = frozenset(
    {
        "linear",
        "in_quad",
        "out_quad",
        "in_out_quad",
        "in_cubic",
        "out_cubic",
        "in_out_cubic",
    }
)

_SPRITE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOP_LEVEL_KEYS = frozenset(
    {
        "sprite_id",
        "target_screen",
        "anchor",
        "transform_origin",
        "duration_ms",
        "tracks",
    }
)
_TRACK_KEYS = frozenset({"prop", "keys", "ease"})
_VALUE_LIMITS = {
    "x": (-8192.0, 8192.0),
    "y": (-8192.0, 8192.0),
    "scale": (0.05, 4.0),
    "rotation": (-720.0, 720.0),
    "opacity": (0.0, 1.0),
}
_DEFAULT_VALUES = {
    "x": 0.0,
    "y": 0.0,
    "scale": 1.0,
    "rotation": 0.0,
    "opacity": 1.0,
}

# Provider-facing schema intentionally stays simpler than the authoritative
# validator below.  Gemini rejected a property-specific union as having too
# many states; the local validator still enforces per-prop ranges, unique
# tracks, timing, and total complexity before anything can be queued.
PRESENCE_RENDERER_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sprite_id": {"type": "string"},
        "target_screen": {
            "type": "string",
            "enum": sorted(ALLOWED_TARGET_SCREENS),
        },
        "anchor": {"type": "string", "enum": sorted(ALLOWED_ANCHORS)},
        "transform_origin": {
            "type": "string",
            "enum": sorted(ALLOWED_TRANSFORM_ORIGINS),
        },
        "duration_ms": {"type": "integer"},
        "tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "prop": {"type": "string", "enum": sorted(ALLOWED_PROPS)},
                    "keys": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "number"},
                        },
                    },
                    "ease": {
                        "type": "string",
                        "enum": sorted(ALLOWED_EASINGS),
                    },
                },
                "required": ["prop", "keys", "ease"],
            },
        },
    },
    "required": [
        "sprite_id",
        "target_screen",
        "anchor",
        "transform_origin",
        "duration_ms",
        "tracks",
    ],
}


class TrajectoryValidationError(ValueError):
    """Raised when a trajectory must be rejected instead of repaired."""


class SpriteValidationError(ValueError):
    """Raised when bytes are not a safe, visible transparent PNG."""


@dataclass(frozen=True)
class SpriteInspection:
    width_px: int
    height_px: int
    alpha_min: int
    alpha_max: int
    transparent_pixels: int
    visible_pixels: int
    sha256: str


def _number(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrajectoryValidationError(f"{label}:number_required")
    number = float(value)
    if not math.isfinite(number):
        raise TrajectoryValidationError(f"{label}:finite_required")
    return number


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], *, label: str) -> None:
    actual = frozenset(str(key) for key in value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise TrajectoryValidationError(
            f"{label}:keys:missing={missing!r}:extra={extra!r}"
        )


def validate_trajectory(value: Any) -> dict[str, Any]:
    """Return a normalized copy or raise; never partially repair model output."""

    if not isinstance(value, Mapping):
        raise TrajectoryValidationError("trajectory:object_required")
    _exact_keys(value, _TOP_LEVEL_KEYS, label="trajectory")

    sprite_id = str(value.get("sprite_id") or "")
    if not _SPRITE_ID_RE.fullmatch(sprite_id):
        raise TrajectoryValidationError("sprite_id:invalid")
    target_screen = str(value.get("target_screen") or "")
    if target_screen not in ALLOWED_TARGET_SCREENS:
        raise TrajectoryValidationError("target_screen:invalid")
    anchor = str(value.get("anchor") or "")
    if anchor not in ALLOWED_ANCHORS:
        raise TrajectoryValidationError("anchor:invalid")
    transform_origin = str(value.get("transform_origin") or "")
    if transform_origin not in ALLOWED_TRANSFORM_ORIGINS:
        raise TrajectoryValidationError("transform_origin:invalid")

    duration_raw = value.get("duration_ms")
    if isinstance(duration_raw, bool) or not isinstance(duration_raw, int):
        raise TrajectoryValidationError("duration_ms:integer_required")
    duration_ms = int(duration_raw)
    if not 1 <= duration_ms <= MAX_DURATION_MS:
        raise TrajectoryValidationError("duration_ms:out_of_range")

    raw_tracks = value.get("tracks")
    if not isinstance(raw_tracks, list) or not 1 <= len(raw_tracks) <= MAX_TRACKS:
        raise TrajectoryValidationError("tracks:count")

    tracks: list[dict[str, Any]] = []
    seen_props: set[str] = set()
    total_keys = 0
    for track_index, raw_track in enumerate(raw_tracks):
        label = f"tracks[{track_index}]"
        if not isinstance(raw_track, Mapping):
            raise TrajectoryValidationError(f"{label}:object_required")
        actual_keys = frozenset(str(key) for key in raw_track)
        if not actual_keys <= _TRACK_KEYS or not {"prop", "keys"} <= actual_keys:
            raise TrajectoryValidationError(f"{label}:keys")
        prop = str(raw_track.get("prop") or "")
        if prop not in ALLOWED_PROPS or prop in seen_props:
            raise TrajectoryValidationError(f"{label}:prop")
        seen_props.add(prop)
        ease = str(raw_track.get("ease") or "linear")
        if ease not in ALLOWED_EASINGS:
            raise TrajectoryValidationError(f"{label}:ease")
        raw_keys = raw_track.get("keys")
        if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= MAX_KEYS_PER_TRACK:
            raise TrajectoryValidationError(f"{label}:key_count")
        total_keys += len(raw_keys)
        if total_keys > MAX_TOTAL_KEYS:
            raise TrajectoryValidationError("tracks:complexity")

        keys: list[list[int | float]] = []
        previous_time = -1
        low, high = _VALUE_LIMITS[prop]
        for key_index, raw_key in enumerate(raw_keys):
            key_label = f"{label}.keys[{key_index}]"
            if not isinstance(raw_key, list) or len(raw_key) != 2:
                raise TrajectoryValidationError(f"{key_label}:pair_required")
            time_raw = raw_key[0]
            if isinstance(time_raw, bool) or not isinstance(time_raw, int):
                raise TrajectoryValidationError(f"{key_label}:integer_time_required")
            key_time = int(time_raw)
            if key_time <= previous_time or not 0 <= key_time <= duration_ms:
                raise TrajectoryValidationError(f"{key_label}:time")
            previous_time = key_time
            number = _number(raw_key[1], label=f"{key_label}.value")
            if not low <= number <= high:
                raise TrajectoryValidationError(f"{key_label}:value_range")
            normalized_number: int | float = int(number) if number.is_integer() else number
            keys.append([key_time, normalized_number])
        tracks.append({"prop": prop, "keys": keys, "ease": ease})

    return {
        "sprite_id": sprite_id,
        "target_screen": target_screen,
        "anchor": anchor,
        "transform_origin": transform_origin,
        "duration_ms": duration_ms,
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
        return (
            2 * value * value
            if value < 0.5
            else 1 - ((-2 * value + 2) ** 2) / 2
        )
    if name == "in_cubic":
        return value**3
    if name == "out_cubic":
        return 1 - (1 - value) ** 3
    if name == "in_out_cubic":
        return (
            4 * value**3
            if value < 0.5
            else 1 - ((-2 * value + 2) ** 3) / 2
        )
    return value


def _track_value(track: Mapping[str, Any], at_ms: float) -> float:
    keys = track["keys"]
    if at_ms <= keys[0][0]:
        return float(keys[0][1])
    if at_ms >= keys[-1][0]:
        return float(keys[-1][1])
    for left, right in zip(keys, keys[1:]):
        if left[0] <= at_ms <= right[0]:
            span = right[0] - left[0]
            progress = easing_value(
                str(track.get("ease") or "linear"),
                (at_ms - left[0]) / span,
            )
            return float(left[1]) + (float(right[1]) - float(left[1])) * progress
    return float(keys[-1][1])


def evaluate_trajectory(
    trajectory: Mapping[str, Any], at_ms: float
) -> dict[str, float]:
    """Resolve all five properties for an already validated trajectory."""

    values = dict(_DEFAULT_VALUES)
    bounded_ms = min(
        float(trajectory["duration_ms"]),
        max(0.0, float(at_ms)),
    )
    for track in trajectory["tracks"]:
        values[str(track["prop"])] = _track_value(track, bounded_ms)
    return values


def trajectory_intervals(
    trajectory: Mapping[str, Any],
) -> list[dict[str, int | bool]]:
    """Classify union-key intervals for duration policy and PC parity.

    Equality at both interval endpoints implies constancy only because the
    boundaries are the union of every track's keys and every allowed easing is
    strictly monotonic.  Adding an overshooting easing requires upgrading this
    classifier before adding it to ``ALLOWED_EASINGS``.
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
            for prop in _DEFAULT_VALUES
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


def inspect_transparent_png(data: bytes) -> SpriteInspection:
    if not isinstance(data, bytes) or not data or len(data) > MAX_PNG_BYTES:
        raise SpriteValidationError("png:size")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SpriteValidationError("png:signature")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.format != "PNG":
                raise SpriteValidationError("png:format")
            if image.width < 2 or image.height < 2:
                raise SpriteValidationError("png:dimensions")
            if image.width > MAX_SPRITE_EDGE_PX or image.height > MAX_SPRITE_EDGE_PX:
                raise SpriteValidationError("png:dimensions")
            if "A" not in image.getbands():
                raise SpriteValidationError("png:alpha_missing")
            alpha = image.getchannel("A")
            alpha_min, alpha_max = alpha.getextrema()
            histogram = alpha.histogram()
            transparent_pixels = sum(histogram[:255])
            visible_pixels = sum(histogram[1:])
    except SpriteValidationError:
        raise
    except Exception as exc:
        raise SpriteValidationError("png:decode") from exc
    if alpha_min == 255 or transparent_pixels <= 0:
        raise SpriteValidationError("png:alpha_opaque")
    if alpha_max == 0 or visible_pixels <= 0:
        raise SpriteValidationError("png:alpha_empty")
    return SpriteInspection(
        width_px=image.width,
        height_px=image.height,
        alpha_min=int(alpha_min),
        alpha_max=int(alpha_max),
        transparent_pixels=int(transparent_pixels),
        visible_pixels=int(visible_pixels),
        sha256="sha256:" + hashlib.sha256(data).hexdigest(),
    )


__all__ = [
    "ALLOWED_ANCHORS",
    "ALLOWED_EASINGS",
    "ALLOWED_PROPS",
    "ALLOWED_TARGET_SCREENS",
    "ALLOWED_TRANSFORM_ORIGINS",
    "INTERVAL_VALUE_ABS_TOL",
    "LONG_INTERVAL_MIN_MS",
    "MAX_DURATION_MS",
    "PRESENCE_RENDERER_RESPONSE_SCHEMA",
    "SpriteInspection",
    "SpriteValidationError",
    "TrajectoryValidationError",
    "easing_value",
    "evaluate_trajectory",
    "inspect_transparent_png",
    "trajectory_intervals",
    "validate_trajectory",
]
