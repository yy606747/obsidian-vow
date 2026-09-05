from pathlib import Path

from app.context_delivery import ContextDeliveryProjection, CurrentContextItem
from app.context_delivery.renderer import render_context_delivery_projection


ROOT = Path(__file__).resolve().parents[1]


def _item(key, value, *, confidence=1.0):
    return CurrentContextItem(
        key=key,
        value=value,
        source="android.sensing",
        observed_at=1000.0,
        received_at=1001.0,
        freshness_sec=10.0,
        confidence=confidence,
    )


def test_cp3b_final_renderer_filters_diagnostic_only_fields_before_provider_use():
    projection = ContextDeliveryProjection(
        generated_at=1010.0,
        observations=(
            _item("phone.screen", "on"),
            _item("phone.light_lux", 1200.0),
            _item("phone.wifi", "NJU-WLAN"),
            _item("location.place", "家"),
        ),
        device_derived=(
            _item("phone.motion", "still", confidence=0.42),
        ),
    )

    rendered = render_context_delivery_projection(
        projection,
        user_name="阿玖",
        ai_name="阿澈",
        time_formatter=lambda _value: "15:00",
    )

    assert "手机报告屏幕亮起" in rendered
    assert "手机运动分类为 静止（设备端归纳）" in rendered
    assert "仅凭定位不能判断阿玖" in rendered
    assert "NJU-WLAN" not in rendered
    assert "lux" not in rendered.lower()
    assert "1200" not in rendered
    assert "0.42" not in rendered
    assert "motion_confidence" not in rendered


def test_cp3b_provider_call_sites_use_renderers_not_projection_dicts():
    call_sites = {
        "normal_chat": ROOT / "app/chat/prompt_builder.py",
        "opportunity_self_wake": ROOT / "app/chat/autonomous_capabilities.py",
        "sentinel_legacy": ROOT / "sentinel_runtime.py",
    }
    text_by_surface = {
        surface: path.read_text(encoding="utf-8")
        for surface, path in call_sites.items()
    }

    assert "render_current_context_delivery" in text_by_surface["normal_chat"]
    assert "render_current_context_delivery" in text_by_surface["opportunity_self_wake"]
    assert "render_autonomous_context_delivery" in text_by_surface["sentinel_legacy"]
    for text in text_by_surface.values():
        assert "read_context_delivery_projection" not in text
        assert ".to_dict()" not in text
        assert "context_delivery_projection.v1\"" not in text
