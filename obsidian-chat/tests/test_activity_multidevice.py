"""Phase 2: activity summary must group by device_id, not the hardcoded
['pc','phone'] list, and label by device_name so a phone and a tablet never
get merged into one 'phone' bucket."""

from activity import (
    _device_key,
    _device_label,
    _ordered_device_keys,
    _summarize_window,
)


def test_device_key_prefers_device_id_over_legacy_device():
    assert _device_key({"device_id": "android_tab1", "device": "phone"}) == "android_tab1"
    assert _device_key({"device": "phone"}) == "phone"
    assert _device_key({"device": "pc"}) == "pc"
    assert _device_key({}) == "unknown"


def test_device_label_priority():
    # explicit user name wins
    assert _device_label({"device_name": "华为平板", "device_type": "tablet"}) == "华为平板"
    # fall back to coarse device_type
    assert _device_label({"device_type": "tablet"}) == "平板"
    # fall back to legacy device field
    assert _device_label({"device": "phone"}) == "手机"
    assert _device_label({"device": "pc"}) == "PC"
    # unknown coarse class echoes back rather than crashing
    assert _device_label({"device": "watch"}) == "watch"


def test_ordered_keys_put_pc_first_then_stable():
    keys = {"phone": [], "pc": [], "android_tab1": []}
    assert _ordered_device_keys(keys) == ["pc", "android_tab1", "phone"]


def test_summarize_window_keeps_phone_and_tablet_separate():
    ws, we = 1000.0, 1600.0  # far in the past → last segment ends at window end
    entries = [
        {"device": "pc", "app": "VSCode", "title": "", "timestamp": 1000.0},
        {"device": "phone", "app": "微信", "title": "", "timestamp": 1000.0},
        {
            "device": "phone",  # legacy coarse still phone during migration
            "device_id": "android_tab1",
            "device_name": "华为平板",
            "device_type": "tablet",
            "app": "网易云音乐",
            "title": "",
            "timestamp": 1000.0,
        },
    ]

    summary = _summarize_window(entries, ws, we)
    parts = summary.split(" | ")

    # three distinct device buckets, none merged
    assert len(parts) == 3
    labels = [p.split(":")[0] for p in parts]
    assert labels == ["PC", "华为平板", "手机"]  # pc first, then by stable key
    # tablet's app stays under the tablet, not folded into the phone bucket
    tablet_part = next(p for p in parts if p.startswith("华为平板"))
    phone_part = next(p for p in parts if p.startswith("手机"))
    assert "网易云音乐" in tablet_part
    assert "网易云音乐" not in phone_part
    assert "微信" in phone_part


def test_summarize_window_carry_forward_does_not_bleed_across_devices():
    ws, we = 1000.0, 1600.0
    # tablet went screen_off before the window; phone is active inside it.
    carry_forward = {
        "android_tab1": {
            "device_id": "android_tab1", "device_name": "华为平板",
            "device_type": "tablet", "app": "screen_off", "title": "", "timestamp": 900.0,
        },
    }
    entries = [
        {"device": "phone", "device_id": "android_ph1", "device_name": "我的手机",
         "device_type": "phone", "app": "微信", "title": "", "timestamp": 1000.0},
    ]

    summary = _summarize_window(entries, ws, we, carry_forward)

    # phone's active state is not overwritten by the tablet's carried screen_off
    phone_part = next(p for p in summary.split(" | ") if p.startswith("我的手机"))
    assert "微信" in phone_part
    assert "我的手机: screen_off" not in summary
