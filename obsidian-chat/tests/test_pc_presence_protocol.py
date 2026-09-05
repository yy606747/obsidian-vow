from __future__ import annotations

import ctypes
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
PC_AGENT = ROOT / "pc_agent"
sys.path.insert(0, str(PC_AGENT))

from presence_protocol import (  # noqa: E402
    ANCHORS,
    AckJournal,
    EASINGS,
    LOCAL_IDLE_SCALE_MAX,
    LOCAL_IDLE_Y_AMPLITUDE_DIP,
    LONG_TICK_INTERVAL_MS,
    MAX_DURATION_MS,
    MAX_GEOMETRY_SAMPLES,
    MAX_WINDOW_AXIS_FRACTION,
    MOVING_TICK_INTERVAL_MS,
    ORIGINS,
    PresenceContractError,
    SpriteCache,
    WINDOW_BOUNDS_PADDING_DIP,
    analyze_trajectory_geometry,
    easing_value,
    evaluate_trajectory,
    frame_bounds,
    inspect_sprite_png,
    local_idle_adjustment,
    playback_interruption_reason,
    playback_tick_interval_ms,
    rendered_trajectory_values,
    trajectory_intervals,
    trajectory_sample_times,
    trajectory_window_bounds,
    validate_geometry,
    validate_trajectory,
)
from app.presence.schema import (  # noqa: E402
    ALLOWED_EASINGS as SERVER_EASINGS,
    easing_value as server_easing_value,
    trajectory_intervals as server_trajectory_intervals,
    validate_trajectory as validate_server_trajectory,
)
import activity_worker  # noqa: E402
import agent as pc_agent  # noqa: E402
import presence_worker  # noqa: E402
import screen  # noqa: E402
from scripts.presence_windows_smoke import build_trajectory  # noqa: E402


def _trajectory():
    return {
        "sprite_id": "fog_seed",
        "target_screen": "active",
        "anchor": "bottom_right",
        "transform_origin": "center",
        "duration_ms": 1_000,
        "tracks": [
            {"prop": "x", "keys": [[0, 80], [500, 0]], "ease": "out_cubic"},
            {"prop": "opacity", "keys": [[100, 0], [900, 1]]},
        ],
    }


def _png() -> bytes:
    image = Image.new("RGBA", (40, 30), (0, 0, 0, 0))
    for x in range(10, 30):
        for y in range(5, 25):
            image.putpixel((x, y), (100, 70, 220, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_client_schema_is_fail_closed_and_interpolation_holds_edges():
    trajectory = validate_trajectory(_trajectory())
    assert evaluate_trajectory(trajectory, 0)["opacity"] == 0
    assert evaluate_trajectory(trajectory, 1_000)["x"] == 0
    assert 0 < evaluate_trajectory(trajectory, 250)["x"] < 80
    invalid = _trajectory()
    invalid["unknown"] = True
    with pytest.raises(PresenceContractError, match="trajectory:keys"):
        validate_trajectory(invalid)


def test_client_schema_always_accepts_ten_minutes_and_rejects_more():
    trajectory = _trajectory()
    trajectory["duration_ms"] = MAX_DURATION_MS
    assert validate_trajectory(trajectory)["duration_ms"] == 600_000

    trajectory["duration_ms"] = MAX_DURATION_MS + 1
    with pytest.raises(PresenceContractError, match="duration_ms:invalid"):
        validate_trajectory(trajectory)


def test_server_and_pc_interval_contracts_share_frozen_fixtures():
    fixture_path = Path(__file__).parent / "fixtures" / "presence_trajectory_intervals.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1

    for case in payload["cases"]:
        server = validate_server_trajectory(case["trajectory"])
        client = validate_trajectory(case["trajectory"])
        assert server_trajectory_intervals(server) == case["intervals"], case["name"]
        assert trajectory_intervals(client) == case["intervals"], case["name"]


def test_all_allowed_easings_remain_strictly_monotonic():
    assert EASINGS == SERVER_EASINGS
    positions = [index / 1_000 for index in range(1_001)]
    for name in sorted(EASINGS):
        pc_values = [easing_value(name, position) for position in positions]
        server_values = [server_easing_value(name, position) for position in positions]
        assert pc_values == server_values
        assert pc_values[0] == 0.0
        assert pc_values[-1] == 1.0
        assert all(left < right for left, right in zip(pc_values, pc_values[1:])), name


def _long_presence_trajectory(*, drifting=False, duration_ms=60_000, origin="center"):
    hold_end = duration_ms - 1_000
    return validate_trajectory(
        {
            "sprite_id": "long_sprite",
            "target_screen": "active",
            "anchor": "center",
            "transform_origin": origin,
            "duration_ms": duration_ms,
            "tracks": [
                {
                    "prop": "x",
                    "keys": [
                        [0, -20],
                        [1_000, 0],
                        [hold_end, 5 if drifting else 0],
                        [duration_ms, 20],
                    ],
                    "ease": "linear",
                },
                {
                    "prop": "opacity",
                    "keys": [[0, 0], [1_000, 0.5], [hold_end, 0.5], [duration_ms, 0]],
                    "ease": "linear",
                },
            ],
        }
    )


def test_long_intervals_lower_frame_rate_even_when_slowly_drifting():
    dwell = _long_presence_trajectory()
    dwell_intervals = trajectory_intervals(dwell)
    assert playback_tick_interval_ms(
        dwell, 500, intervals=dwell_intervals
    ) == MOVING_TICK_INTERVAL_MS
    assert playback_tick_interval_ms(
        dwell, 2_000, intervals=dwell_intervals
    ) == LONG_TICK_INTERVAL_MS
    assert playback_tick_interval_ms(
        dwell, 59_500, intervals=dwell_intervals
    ) == MOVING_TICK_INTERVAL_MS

    drift = _long_presence_trajectory(drifting=True, duration_ms=300_000)
    drift_intervals = trajectory_intervals(drift)
    middle = next(interval for interval in drift_intervals if interval["is_long"])
    assert middle["is_dwell"] is False
    assert playback_tick_interval_ms(
        drift, 2_000, intervals=drift_intervals
    ) == LONG_TICK_INTERVAL_MS


def test_local_idle_has_smooth_boundaries_and_fixed_extrema():
    trajectory = _long_presence_trajectory()
    intervals = trajectory_intervals(trajectory)

    at_start = local_idle_adjustment(trajectory, 1_000, intervals=intervals)
    at_quarter = local_idle_adjustment(trajectory, 3_000, intervals=intervals)
    at_half = local_idle_adjustment(trajectory, 5_000, intervals=intervals)
    before_end = local_idle_adjustment(trajectory, 58_999, intervals=intervals)
    at_end = local_idle_adjustment(trajectory, 59_000, intervals=intervals)

    assert at_start == {"y_offset": 0.0, "scale_multiplier": 1.0}
    assert at_quarter["y_offset"] == pytest.approx(LOCAL_IDLE_Y_AMPLITUDE_DIP)
    assert at_half["y_offset"] == pytest.approx(0.0, abs=1e-12)
    assert at_half["scale_multiplier"] == pytest.approx(LOCAL_IDLE_SCALE_MAX)
    assert abs(before_end["y_offset"]) < 0.01
    assert abs(before_end["scale_multiplier"] - 1.0) < 0.001
    assert at_end == {"y_offset": 0.0, "scale_multiplier": 1.0}

    base = evaluate_trajectory(trajectory, 3_000)
    rendered = rendered_trajectory_values(
        trajectory,
        3_000,
        local_idle_enabled=True,
        intervals=intervals,
    )
    assert rendered["y"] == pytest.approx(
        base["y"] + LOCAL_IDLE_Y_AMPLITUDE_DIP
    )
    assert rendered["scale"] > base["scale"]


def _rotation_trajectory(duration_ms):
    return validate_trajectory(
        {
            "sprite_id": "rotating_sprite",
            "target_screen": "active",
            "anchor": "center",
            "transform_origin": "center",
            "duration_ms": duration_ms,
            "tracks": [
                {
                    "prop": "rotation",
                    "keys": [[0, 0], [duration_ms, 180]],
                    "ease": "in_out_cubic",
                },
                {
                    "prop": "opacity",
                    "keys": [[0, 1], [duration_ms, 1]],
                    "ease": "linear",
                },
            ],
        }
    )


def test_geometry_sampling_is_duration_bounded_and_catches_rotation_midpoint():
    minute = _rotation_trajectory(60_000)
    ten_minutes = _rotation_trajectory(600_000)
    assert len(trajectory_sample_times(minute)) == len(
        trajectory_sample_times(ten_minutes)
    )

    analysis = analyze_trajectory_geometry(
        ten_minutes,
        work_width=1_000,
        work_height=800,
        image_width_px=100,
        image_height_px=200,
        base_height_dip=200,
    )
    midpoint_bounds = frame_bounds(
        ten_minutes,
        evaluate_trajectory(ten_minutes, 300_000),
        work_width=1_000,
        work_height=800,
        sprite_width=100,
        sprite_height=200,
    )
    left, top, right, bottom = analysis.bounds
    assert left <= midpoint_bounds[0] - WINDOW_BOUNDS_PADDING_DIP
    assert top <= midpoint_bounds[1] - WINDOW_BOUNDS_PADDING_DIP
    assert right >= midpoint_bounds[2] + WINDOW_BOUNDS_PADDING_DIP
    assert bottom >= midpoint_bounds[3] + WINDOW_BOUNDS_PADDING_DIP


def test_worst_legal_key_count_stays_inside_derived_geometry_budget():
    props_and_values = (
        ("x", 0),
        ("y", 0),
        ("scale", 1),
        ("rotation", 0),
        ("opacity", 0.5),
    )
    tracks = []
    for track_index, (prop, value) in enumerate(props_and_values):
        keys = [
            [1 + (track_index * 24 + key_index) * 4_000, value]
            for key_index in range(24)
        ]
        tracks.append({"prop": prop, "keys": keys, "ease": "linear"})
    trajectory = validate_trajectory(
        {
            "sprite_id": "max_keys",
            "target_screen": "active",
            "anchor": "center",
            "transform_origin": "center",
            "duration_ms": 600_000,
            "tracks": tracks,
        }
    )

    assert len(trajectory_intervals(trajectory)) == 121
    assert len(trajectory_sample_times(trajectory)) <= MAX_GEOMETRY_SAMPLES == 7_745


@pytest.mark.parametrize("origin", sorted(ORIGINS))
@pytest.mark.parametrize(
    ("image_width", "image_height"),
    ((512, 512), (512, 768), (768, 512)),
    ids=("square", "portrait_2_3", "landscape_3_2"),
)
def test_local_idle_extrema_are_included_for_aspect_ratios_and_origins(
    origin,
    image_width,
    image_height,
):
    trajectory = _long_presence_trajectory(origin=origin)
    analysis = analyze_trajectory_geometry(
        trajectory,
        work_width=1_536,
        work_height=864,
        image_width_px=image_width,
        image_height_px=image_height,
        base_height_dip=260,
    )
    assert analysis.local_idle_enabled is True

    sprite_height = 260.0
    sprite_width = sprite_height * image_width / image_height
    base = evaluate_trajectory(trajectory, 2_000)
    left, top, right, bottom = analysis.bounds
    for y_offset in (-LOCAL_IDLE_Y_AMPLITUDE_DIP, 0.0, LOCAL_IDLE_Y_AMPLITUDE_DIP):
        for scale_multiplier in (1.0, LOCAL_IDLE_SCALE_MAX):
            values = dict(base)
            values["y"] += y_offset
            values["scale"] *= scale_multiplier
            frame = frame_bounds(
                trajectory,
                values,
                work_width=1_536,
                work_height=864,
                sprite_width=sprite_width,
                sprite_height=sprite_height,
            )
            assert left <= frame[0] - WINDOW_BOUNDS_PADDING_DIP
            assert top <= frame[1] - WINDOW_BOUNDS_PADDING_DIP
            assert right >= frame[2] + WINDOW_BOUNDS_PADDING_DIP
            assert bottom >= frame[3] + WINDOW_BOUNDS_PADDING_DIP


def test_idle_expansion_over_compact_limit_falls_back_to_pure_trajectory():
    trajectory = validate_trajectory(
        {
            "sprite_id": "threshold_idle",
            "target_screen": "active",
            "anchor": "center",
            "transform_origin": "center",
            "duration_ms": 20_000,
            "tracks": [
                {
                    "prop": "opacity",
                    "keys": [[0, 0.5], [20_000, 0.5]],
                    "ease": "linear",
                }
            ],
        }
    )
    analysis = analyze_trajectory_geometry(
        trajectory,
        work_width=1_000,
        work_height=800,
        image_width_px=484,
        image_height_px=384,
        base_height_dip=384,
    )

    assert analysis.bounds == (250, 200, 750, 600)
    assert analysis.local_idle_enabled is False


def test_client_geometry_accepts_visible_overscan_and_rejects_invisible_path():
    trajectory = validate_trajectory(_trajectory())
    validate_geometry(
        trajectory,
        work_width=1920,
        work_height=1040,
        image_width_px=40,
        image_height_px=30,
        base_height_dip=240,
    )
    invisible = _trajectory()
    invisible["tracks"][0] = {
        "prop": "x",
        "keys": [[0, 8_000], [1_000, 8_000]],
        "ease": "linear",
    }
    with pytest.raises(PresenceContractError, match="geometry:x_overscan"):
        validate_geometry(
            validate_trajectory(invisible),
            work_width=1920,
            work_height=1040,
            image_width_px=40,
            image_height_px=30,
            base_height_dip=240,
        )


@pytest.mark.parametrize("anchor", sorted(ANCHORS))
@pytest.mark.parametrize(
    ("image_width", "image_height"),
    ((512, 512), (512, 768), (768, 512)),
    ids=("square", "portrait_2_3", "landscape_3_2"),
)
def test_presence_smoke_trajectories_fit_compact_window(
    anchor,
    image_width,
    image_height,
):
    sprite_height = 260.0
    sprite_width = sprite_height * image_width / image_height
    trajectory = validate_trajectory(
        build_trajectory(
            sprite_id="synthetic_protocol_sprite",
            target_screen="active",
            anchor=anchor,
        )
    )

    left, top, right, bottom = trajectory_window_bounds(
        trajectory,
        work_width=1536,
        work_height=864,
        sprite_width=sprite_width,
        sprite_height=sprite_height,
    )

    assert right - left <= 1536 * MAX_WINDOW_AXIS_FRACTION
    assert bottom - top <= 864 * MAX_WINDOW_AXIS_FRACTION
    sample_times = set(range(0, trajectory["duration_ms"] + 1, 16))
    sample_times.add(trajectory["duration_ms"])
    for track in trajectory["tracks"]:
        sample_times.update(pair[0] for pair in track["keys"])
    for at_ms in sample_times:
        frame_left, frame_top, frame_right, frame_bottom = frame_bounds(
            trajectory,
            evaluate_trajectory(trajectory, at_ms),
            work_width=1536,
            work_height=864,
            sprite_width=sprite_width,
            sprite_height=sprite_height,
        )
        assert left <= frame_left - WINDOW_BOUNDS_PADDING_DIP
        assert top <= frame_top - WINDOW_BOUNDS_PADDING_DIP
        assert right >= frame_right + WINDOW_BOUNDS_PADDING_DIP
        assert bottom >= frame_bottom + WINDOW_BOUNDS_PADDING_DIP


def test_compact_window_accepts_exact_half_and_rejects_larger_axis():
    trajectory = validate_trajectory(
        {
            "sprite_id": "threshold",
            "target_screen": "active",
            "anchor": "center",
            "transform_origin": "center",
            "duration_ms": 100,
            "tracks": [{"prop": "opacity", "keys": [[0, 1], [100, 1]]}],
        }
    )

    left, top, right, bottom = trajectory_window_bounds(
        trajectory,
        work_width=1000,
        work_height=800,
        sprite_width=484,
        sprite_height=384,
    )
    assert (right - left, bottom - top) == (500, 400)

    with pytest.raises(PresenceContractError, match="trajectory_bounds_too_large"):
        trajectory_window_bounds(
            trajectory,
            work_width=1000,
            work_height=800,
            sprite_width=485,
            sprite_height=384,
        )
    with pytest.raises(PresenceContractError, match="trajectory_bounds_too_large"):
        trajectory_window_bounds(
            trajectory,
            work_width=1000,
            work_height=800,
            sprite_width=484,
            sprite_height=385,
        )


def test_sprite_cache_checks_png_alpha_and_sha256(tmp_path):
    data = _png()
    inspected = inspect_sprite_png(data)
    cache = SpriteCache(tmp_path / "sprites")
    path = cache.store(inspected["sprite_hash"], data)
    assert cache.valid_path(inspected["sprite_hash"]) == path
    path.write_bytes(data + b"corruption")
    assert cache.valid_path(inspected["sprite_hash"]) is None

    opaque = io.BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 255)).save(opaque, format="PNG")
    with pytest.raises(PresenceContractError, match="alpha_content"):
        inspect_sprite_png(opaque.getvalue())


def test_ack_journal_persists_terminal_and_recovers_crashed_accept(tmp_path):
    path = tmp_path / "acks.json"
    journal = AckJournal(path)
    journal.record("event-1", "accepted", now=100)
    restarted = AckJournal(path)
    assert restarted.recover_incomplete(now=101) == 1
    recovered = restarted.get("event-1")
    assert recovered["status"] == "rejected"
    assert recovered["reason"] == "agent_restarted_after_accept"

    restarted.record("event-2", "played", actual_playback_ms=900, now=102)
    restarted.record("event-2", "accepted", now=103)
    assert restarted.get("event-2")["status"] == "played"
    assert json.loads(path.read_text(encoding="utf-8"))["events"]["event-2"]["status"] == "played"

    restarted.record("event-3", "accepted", now=104)
    restarted.record_ack(
        {
            "event_id": "event-3",
            "status": "played",
            "reason": "",
            "actual_playback_ms": 1_000,
        },
        now=105,
    )
    crashed_after_gui_callback = AckJournal(path)
    assert crashed_after_gui_callback.recover_incomplete(now=106) == 0
    assert crashed_after_gui_callback.get("event-3")["status"] == "played"


def test_playback_interruption_detects_lock_sleep_and_normal_ticks():
    assert playback_interruption_reason(
        locked=True,
        last_tick_at=10.0,
        now=10.016,
    ) == "locked_during_playback"
    assert playback_interruption_reason(
        locked=False,
        last_tick_at=10.0,
        now=11.001,
    ) == "playback_timer_gap"
    assert playback_interruption_reason(
        locked=False,
        last_tick_at=10.0,
        now=10.016,
    ) == ""


def _delivery(cache: SpriteCache, *, event_id: str = "event-deadline"):
    data = _png()
    sprite_hash = inspect_sprite_png(data)["sprite_hash"]
    cache.store(sprite_hash, data)
    return {
        "event_id": event_id,
        "remaining_ttl_ms": 1_000,
        "sprite_id": "fog_seed",
        "sprite_hash": sprite_hash,
        "base_height_dip": 240,
        "trajectory": _trajectory(),
    }


def test_delivery_deadline_is_frozen_before_accepted_ack(monkeypatch, tmp_path):
    class Controller:
        def __init__(self):
            self.requests = []

        def submit(self, request):
            self.requests.append(request)

    clock = [100.0]
    controller = Controller()
    journal = AckJournal(tmp_path / "acks.json")
    cache = SpriteCache(tmp_path / "sprites")

    def slow_accepted_ack(*_args, **_kwargs):
        clock[0] += 0.4

    monkeypatch.setattr(presence_worker, "_flush_acks", slow_accepted_ack)
    presence_worker._handle_delivery(
        "https://example.invalid",
        "token",
        _delivery(cache),
        controller,
        cache,
        journal,
        set(),
        lambda: False,
        monotonic=lambda: clock[0],
    )

    assert len(controller.requests) == 1
    assert controller.requests[0]["local_start_deadline"] == 101.0


def test_slow_accepted_ack_expires_instead_of_resetting_deadline(monkeypatch, tmp_path):
    class Controller:
        def __init__(self):
            self.requests = []

        def submit(self, request):
            self.requests.append(request)

    clock = [200.0]
    controller = Controller()
    journal = AckJournal(tmp_path / "acks.json")
    cache = SpriteCache(tmp_path / "sprites")

    def slower_than_ttl(*_args, **_kwargs):
        clock[0] += 1.1

    monkeypatch.setattr(presence_worker, "_flush_acks", slower_than_ttl)
    presence_worker._handle_delivery(
        "https://example.invalid",
        "token",
        _delivery(cache),
        controller,
        cache,
        journal,
        set(),
        lambda: False,
        monotonic=lambda: clock[0],
    )

    assert controller.requests == []
    assert journal.get("event-deadline")["status"] == "expired"


def test_pc_agent_qt_main_thread_and_clickthrough_contract_are_wired():
    agent_source = (PC_AGENT / "agent.py").read_text(encoding="utf-8")
    player_source = (PC_AGENT / "presence_player.py").read_text(encoding="utf-8")
    assert "QApplication" in agent_source
    assert "setQuitOnLastWindowClosed(False)" in agent_source
    assert "application.exec()" in agent_source
    assert "WindowTransparentForInput" in player_source
    assert "WA_TransparentForMouseEvents" in player_source
    assert "time.monotonic()" in player_source
    assert "ctypes.c_ssize_t" in player_source
    assert "analyze_trajectory_geometry(" in player_source
    assert "geometry_analysis.local_idle_enabled" in player_source
    assert "playback_tick_interval_ms(" in player_source
    assert "rendered_trajectory_values(" in player_source
    assert "min(120_000" not in player_source
    assert "self.setGeometry(geometry)" not in player_source
    assert "- self._window_left" in player_source
    assert "- self._window_top" in player_source
    assert "PresenceWindow(request, screen, self.lock_checker)" in player_source
    begin_index = player_source.index("window.begin()")
    begin_except_index = player_source.index("except Exception as exc:", begin_index)
    rejected_index = player_source.index("self._queue_terminal(", begin_except_index)
    active_clear_index = player_source.index("self._active = None", begin_except_index)
    assert begin_except_index < active_clear_index < rejected_index
    assert player_source.index("self.journal.record_ack(payload") < player_source.index(
        "self.ack_queue.put(payload)"
    )
    assert "presence_journal = AckJournal" in agent_source
    assert "PresenceController(ack_queue, _is_locked, presence_journal)" in agent_source
    ca_setup = 'bundled_ca = base_dir / "cacert.pem"'
    assert ca_setup in agent_source
    assert 'os.environ["SSL_CERT_FILE"] = str(bundled_ca)' in agent_source
    assert agent_source.index(ca_setup) < agent_source.index("_start_worker(")


def test_screen_confirmation_countdown_times_out_and_rejects_late_click():
    request = screen.ScreenConfirmRequest("reason", timeout=2, ai_name="Arden")

    first_tick = request.advance_countdown()
    assert first_tick.remaining == 1
    assert not first_tick.done
    assert not request.event.is_set()

    terminal = request.advance_countdown()
    assert terminal.done
    assert terminal.timed_out
    assert not terminal.allowed
    assert request.event.is_set()
    assert not request.finish(True)
    assert not request.snapshot().allowed


def test_screen_confirmation_bridge_preserves_timed_out_function_attribute(
    monkeypatch,
):
    class ImmediateController:
        def __init__(self, *, allowed: bool, timed_out: bool):
            self.allowed = allowed
            self.timed_out = timed_out

        def submit(self, request):
            request.finish(self.allowed, timed_out=self.timed_out)

    monkeypatch.setattr(
        screen,
        "_confirm_controller",
        ImmediateController(allowed=True, timed_out=False),
    )
    assert screen.show_confirm_dialog("reason", timeout=30, ai_name="Arden")
    assert screen.show_confirm_dialog.timed_out is False

    monkeypatch.setattr(
        screen,
        "_confirm_controller",
        ImmediateController(allowed=False, timed_out=True),
    )
    assert not screen.show_confirm_dialog("reason", timeout=30, ai_name="Arden")
    assert screen.show_confirm_dialog.timed_out is True


def test_screen_confirmation_uses_qt_handoff_without_changing_worker_protocol():
    screen_source = (PC_AGENT / "screen.py").read_text(encoding="utf-8")
    agent_source = (PC_AGENT / "agent.py").read_text(encoding="utf-8")
    worker_source = (PC_AGENT / "screen_worker.py").read_text(encoding="utf-8")

    for source_path in PC_AGENT.glob("*.py"):
        assert "tk" + "inter" not in source_path.read_text(encoding="utf-8").lower()
    assert "requested = Signal(object)" in screen_source
    assert "Qt.ConnectionType.QueuedConnection" in screen_source
    assert "request.event.wait(request.timeout + CONFIRM_WAIT_GRACE_SEC)" in screen_source
    assert "ScreenConfirmController()" in agent_source
    assert "install_confirm_controller(screen_confirm_controller)" in agent_source
    assert (
        "screen.show_confirm_dialog(reason, timeout=30, ai_name=ai_name)"
        in worker_source
    )
    assert '"confirm_timeout"' in worker_source
    assert '"denied"' in worker_source


def test_activity_worker_preserves_64_bit_windows_process_handle_signatures(
    monkeypatch,
):
    class FakeFunction:
        def __init__(self, callback):
            self.callback = callback
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.callback(*args)

    handle = 0x1_0000_0123
    closed = []
    open_process = FakeFunction(lambda *_args: handle)

    def query_name(received_handle, _flags, buffer, _size):
        assert received_handle == handle
        buffer.value = "/tmp/example-browser.exe"
        return 1

    query_process_name = FakeFunction(query_name)
    close_handle = FakeFunction(lambda received: closed.append(received) or 1)
    kernel32 = SimpleNamespace(
        OpenProcess=open_process,
        QueryFullProcessImageNameW=query_process_name,
        CloseHandle=close_handle,
    )
    monkeypatch.setattr(activity_worker, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        activity_worker.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )

    assert activity_worker.process_name_from_pid(42) == "example-browser.exe"
    assert open_process.restype is ctypes.c_void_p
    assert close_handle.argtypes == [ctypes.c_void_p]
    assert closed == [handle]


def test_activity_snapshot_failure_retries_and_later_reports(monkeypatch):
    class StopLoop(Exception):
        pass

    snapshots = 0
    reported = []

    def snapshot(_idle_threshold):
        nonlocal snapshots
        snapshots += 1
        if snapshots == 1:
            raise RuntimeError("temporary user32 failure")
        return {"device": "pc"}

    def stop_after_report(_delay, _label):
        if reported:
            raise StopLoop

    monkeypatch.setattr(activity_worker, "snapshot_payload", snapshot)
    monkeypatch.setattr(
        activity_worker,
        "post_json",
        lambda *_args, **_kwargs: reported.append(True),
    )
    monkeypatch.setattr(activity_worker, "retry_delay", lambda *_args: 0)
    monkeypatch.setattr(activity_worker, "sleep_with_gap_log", stop_after_report)

    with pytest.raises(StopLoop):
        activity_worker.run_activity_loop("https://example.invalid", "token", 60, 180)

    assert snapshots == 2
    assert reported == [True]


def test_worker_heartbeat_is_immediate_then_ten_minutes():
    assert activity_worker.heartbeat_due(None, 100.0)
    assert not activity_worker.heartbeat_due(100.0, 699.999)
    assert activity_worker.heartbeat_due(100.0, 700.0)


def test_worker_supervisor_restarts_after_exception_and_return(monkeypatch):
    class StopSupervisor(Exception):
        pass

    calls = 0

    def target():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("worker failed")

    def stop_after_second_run(_delay):
        if calls == 2:
            raise StopSupervisor

    monkeypatch.setattr(pc_agent.time, "sleep", stop_after_second_run)

    with pytest.raises(StopSupervisor):
        pc_agent._supervise_worker("test-worker", target, ())

    assert calls == 2


def test_pc_agent_config_accepts_windows_powershell_utf8_bom(tmp_path):
    (tmp_path / "config.json").write_bytes(
        b'\xef\xbb\xbf{"server_url":"http://localhost","token":"test"}'
    )

    assert pc_agent._load_config(tmp_path) == {
        "server_url": "http://localhost",
        "token": "test",
    }


def test_windows_build_uses_msys2_pyside_for_mingw_python():
    source = (PC_AGENT / "build_windows.bat").read_text(encoding="utf-8")
    launcher = (PC_AGENT / "run_windows.bat").read_text(encoding="utf-8")
    requirements = (PC_AGENT / "requirements-windows.txt").read_text(encoding="utf-8")

    assert "sysconfig.get_platform" in source
    assert "mingw-w64-ucrt-x86_64-pyside6" in source
    assert "mingw-w64-ucrt-x86_64-pyinstaller" in source
    assert "mingw-w64-ucrt-x86_64-python-pillow" in source
    assert "mingw-w64-ucrt-x86_64-python-certifi" in source
    assert "pip install -r requirements-windows.txt" in source
    assert "from PySide6.QtCore import QTimer" in source
    assert "from PySide6.QtGui import QImage" in source
    assert "from PySide6.QtWidgets import QApplication" in source
    assert "GUI archive check:" in source
    assert 'import certifi; print(certifi.where())' in source
    assert "dist\\cacert.pem" in source
    assert "dist\\run_windows.bat" in source
    assert "dist\\start_hidden.vbs" in source
    assert "certifi" in requirements
    assert "ObsidianVowPcAgent.exe" in launcher
    assert launcher.index("ObsidianVowPcAgent.exe") < launcher.index('python agent.py')
