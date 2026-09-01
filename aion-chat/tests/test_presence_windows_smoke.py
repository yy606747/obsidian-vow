from app.presence.schema import trajectory_intervals, validate_trajectory
from scripts.presence_windows_smoke import build_trajectory


def test_windows_smoke_trajectory_exercises_all_v1_props():
    trajectory = validate_trajectory(
        build_trajectory(
            sprite_id="seed_fox",
            target_screen="active",
            anchor="bottom_right",
        )
    )

    assert {track["prop"] for track in trajectory["tracks"]} == {
        "x",
        "y",
        "scale",
        "rotation",
        "opacity",
    }


def test_windows_smoke_can_build_two_minute_local_idle_fixture():
    trajectory = validate_trajectory(
        build_trajectory(
            sprite_id="seed_fox",
            target_screen="active",
            anchor="bottom_right",
            duration_ms=120_000,
        )
    )

    assert trajectory["duration_ms"] == 120_000
    assert any(interval["is_dwell"] for interval in trajectory_intervals(trajectory))
