from app.context_delivery import (
    AvailabilityItem,
    ContextDeliveryProjection,
    CurrentContextItem,
    RecentContextEvent,
    render_context_delivery_projection,
)


def _item(key, value, *, observed_at=1000, since_at=None, confidence=1.0):
    return CurrentContextItem(
        key=key,
        value=value,
        source="android.sensing",
        observed_at=observed_at,
        received_at=observed_at + 1,
        freshness_sec=10,
        since_at=since_at,
        confidence=confidence,
    )


def test_renderer_separates_fact_derived_event_and_availability_sections():
    projection = ContextDeliveryProjection(
        generated_at=1010,
        observations=(_item("phone.screen", "on", since_at=900),),
        device_derived=(_item("phone.motion", "still", confidence=0.18),),
        recent_events=(RecentContextEvent(
            key="phone.screen", event="transition", from_value="off", to_value="on",
            observed_at=900, source="android.sensing",
        ),),
        availability=(AvailabilityItem(
            source="location.v2", status="stale", last_observed_at=100,
            reason="最近没有达到 freshness 要求的数据",
        ),),
    )

    rendered = render_context_delivery_projection(
        projection,
        user_name="阿玖",
        ai_name="阿澈",
        time_formatter=lambda value: f"T{int(value)}",
    )

    assert rendered.startswith("[设备与环境上下文]")
    assert "直接观测：" in rendered
    assert "设备端归纳：" in rendered
    assert "最近变化：" in rendered
    assert "数据可用性：" in rendered
    assert "手机运动分类为 静止（设备端归纳）" in rendered
    assert "置信度" not in rendered
    assert "正在休息" not in rendered
    assert "owner" not in rendered.lower()


def test_renderer_omits_empty_projection_and_never_emits_raw_internal_fields():
    assert render_context_delivery_projection(
        ContextDeliveryProjection(generated_at=1),
        user_name="阿玖",
        ai_name="阿澈",
    ) == ""

    rendered = render_context_delivery_projection(
        ContextDeliveryProjection(
            generated_at=2,
            observations=(_item("location.place", "家"),),
            metrics={"evidence_id": "secret", "lat": 31.2, "lng": 121.4},
        ),
        user_name="阿玖",
        ai_name="阿澈",
        time_formatter=lambda _value: "10:00",
    )

    assert "secret" not in rendered
    assert "31.2" not in rendered
    assert "121.4" not in rendered
    assert "evidence_id" not in rendered


def test_renderer_combines_geofence_and_fresh_address_into_one_cautious_line():
    projection = ContextDeliveryProjection(
        generated_at=1010,
        observations=(
            _item("location.address", "南京大学仙林校区", observed_at=995),
            _item("location.place", "家", observed_at=990),
        ),
    )

    rendered = render_context_delivery_projection(
        projection,
        user_name="阿玖",
        ai_name="阿澈",
        time_formatter=lambda value: {990.0: "10:00", 995.0: "10:05"}[value],
    )
    location_lines = [line for line in rendered.splitlines() if "定位" in line]

    assert location_lines == [
        "- 10:00 定位服务报告设备落在「家」的位置范围内；"
        "10:05 高德地址报告设备大概在「南京大学仙林校区」。"
        "仅凭定位不能判断阿玖在宿舍、教室、是否上课或正在做什么。"
    ]
    assert "用户当前大概在家" not in rendered
    assert "当前区域为「家」" not in rendered


def test_renderer_describes_unmatched_fix_without_inventing_an_outside_place():
    rendered = render_context_delivery_projection(
        ContextDeliveryProjection(
            generated_at=1010,
            observations=(_item("location.place", "unmatched", observed_at=1000),),
        ),
        user_name="阿玖",
        ai_name="阿澈",
        time_formatter=lambda _value: "10:00",
    )

    assert "设备未落入任何已登记地点范围" in rendered
    assert "当前区域为「outside」" not in rendered


def test_renderer_clips_only_at_complete_line_boundaries():
    projection = ContextDeliveryProjection(
        generated_at=1010,
        observations=tuple(
            _item(f"mobile.device_{index}.foreground_app", f"应用{index}")
            for index in range(8)
        ),
    )

    rendered = render_context_delivery_projection(
        projection,
        user_name="阿玖",
        ai_name="阿澈",
        max_chars=105,
        time_formatter=lambda _value: "10:00",
    )

    assert len(rendered) <= 105
    assert rendered.splitlines()[-1].endswith("。")
    assert "..." not in rendered


def test_renderer_enforces_current_item_count_even_with_large_projection():
    projection = ContextDeliveryProjection(
        generated_at=1010,
        observations=tuple(
            _item(f"mobile.device_{index}.foreground_app", f"应用{index}")
            for index in range(12)
        ),
    )

    rendered = render_context_delivery_projection(
        projection,
        user_name="阿玖",
        ai_name="阿澈",
        max_chars=10000,
        time_formatter=lambda _value: "10:00",
    )

    assert rendered.count("移动设备 device_") == 8


def test_renderer_formats_aggregated_notifications_as_one_time_range_line():
    projection = ContextDeliveryProjection(
        generated_at=1000,
        recent_events=(RecentContextEvent(
            key="phone.notification",
            event="occurred",
            to_value="群聊 10",
            observed_at=900,
            source="android.sensing",
            occurrence_count=10,
            first_observed_at=800,
        ),),
    )

    rendered = render_context_delivery_projection(
        projection,
        user_name="阿玖",
        ai_name="阿澈",
        time_formatter=lambda value: {
            800.0: "13:26",
            900.0: "14:00",
        }[value],
    )

    assert "13:26–14:00 手机报告 10 条通知（群聊 10）。" in rendered
    assert rendered.count("通知") == 1


def test_renderer_aggregates_summons_before_line_limit_and_caps_time_list():
    projection = ContextDeliveryProjection(
        generated_at=2000,
        recent_events=tuple(
            RecentContextEvent(
                key="relationship.summon",
                event="occurred",
                to_value="summoned",
                observed_at=1000 + index,
                source="presence.summon",
            )
            for index in range(20)
        ),
    )

    rendered = render_context_delivery_projection(
        projection,
        user_name="小栀",
        ai_name="阿澈",
        max_chars=10000,
        time_formatter=lambda value: f"T{int(value)}",
    )

    assert rendered.count("想过阿澈") == 1
    assert "T1014、T1015、T1016、T1017、T1018、T1019，一共 20 次" in rendered
    assert "最近一天里" in rendered
    assert "不表示仍在等待回应" in rendered
    assert "T1000" not in rendered
    assert "status" not in rendered


def test_renderer_marks_summons_from_the_previous_local_day():
    """The summon window is a rolling 24h, so bare HH:MM must not imply today."""

    from datetime import datetime, timedelta

    reference = datetime(2026, 8, 25, 1, 30).timestamp()
    yesterday = (
        datetime(2026, 8, 25, 1, 30) - timedelta(hours=3)
    ).timestamp()
    projection = ContextDeliveryProjection(
        generated_at=reference,
        recent_events=(
            RecentContextEvent(
                key="relationship.summon",
                event="occurred",
                to_value="summoned",
                observed_at=yesterday,
                source="presence.summon",
            ),
            RecentContextEvent(
                key="relationship.summon",
                event="occurred",
                to_value="summoned",
                observed_at=reference - 600,
                source="presence.summon",
            ),
        ),
    )

    rendered = render_context_delivery_projection(
        projection,
        user_name="小栀",
        ai_name="阿澈",
        max_chars=10000,
    )

    assert "昨天 22:30" in rendered
    assert "昨天 01:20" not in rendered
    assert "01:20" in rendered
