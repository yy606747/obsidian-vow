from app.sentinel import build_attention_snapshot


def test_sentinel_attention_derives_pc_status_and_foreground_tags():
    snapshot = build_attention_snapshot({
        "reference_time": "2026-05-15T20:00:00+08:00",
        "raw_signals": [
            {
                "kind": "activity.app",
                "source": "pc.activity",
                "text": (
                    "PC activity pc_state=active pc_active app=VS Code "
                    "pc_foreground_dev_tool title=ObsidianVow"
                ),
            },
        ],
        "recent_chat": [],
    })

    tags = snapshot["debug_trace"]["feature_tags"]
    assert "pc_active" in tags
    assert "pc_foreground_dev_tool" in tags
    assert snapshot["world_state"]["pc_activity"] == "active"
    assert snapshot["world_state"]["pc_foreground"] == "dev_tool"
    assert snapshot["attention_targets"] == ["pc_activity_context", "screen_check_opportunity"]
    assert "不能单独证明需要打扰" in snapshot["compact_text"]


def test_sentinel_attention_does_not_derive_pc_tags_from_android_activity():
    snapshot = build_attention_snapshot({
        "reference_time": "2026-05-15T20:00:00+08:00",
        "raw_signals": [
            {
                "kind": "activity.app",
                "source": "android.activity",
                "text": "pc_state=active pc_active pc_foreground_dev_tool",
            },
        ],
        "recent_chat": [],
    })

    tags = snapshot["debug_trace"]["feature_tags"]
    assert "pc_active" not in tags
    assert "pc_foreground_dev_tool" not in tags


def test_sentinel_attention_pc_offline_has_no_foreground_tag():
    snapshot = build_attention_snapshot({
        "reference_time": "2026-05-15T20:00:00+08:00",
        "raw_signals": [
            {
                "kind": "activity.app",
                "source": "pc.activity",
                "text": "PC activity remote agent offline; pc_state=offline pc_offline",
            },
        ],
        "recent_chat": [],
    })

    assert snapshot["world_state"] == {"pc_activity": "offline"}
    assert "pc_offline" in snapshot["debug_trace"]["feature_tags"]
    assert "pc_foreground_dev_tool" not in snapshot["debug_trace"]["feature_tags"]
