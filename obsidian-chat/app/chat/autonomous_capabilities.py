"""Runtime capabilities and facts shared by autonomous core turns."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from app.context_delivery.safety import render_device_proxy_hard_limits
from app.modes import mode_service
from config import is_smart_ring_quiet_hours, load_ai_behavior


log = logging.getLogger(__name__)


async def resolve_autonomous_capabilities(
    *,
    model_key: str,
    mobile_screen_target: Mapping[str, str] | None,
    now: float | None = None,
) -> frozenset[str]:
    """Freeze the autonomous runtime capability set for one provider turn."""

    current = time.time() if now is None else float(now)
    capabilities = {"heart.whisper", "memory.remember"}
    normal = set(mode_service.snapshot("normal", source="autonomous").capabilities)

    if "device.ring_touch" in normal and not is_smart_ring_quiet_hours():
        capabilities.add("device.ring_touch")
    try:
        from app.pc_screen.service import (
            is_screen_capture_enabled,
            model_supports_vision,
        )

        vision = model_supports_vision(model_key)
        if vision and is_screen_capture_enabled():
            capabilities.add("pc.screen_check")
        if vision and mobile_screen_target:
            capabilities.add("mobile.screen_check")
    except Exception:
        pass
    try:
        from app.location import state_from_payload
        from location import load_location_config, load_location_status

        loc_cfg = load_location_config()
        loc_status = load_location_status()
        loc_state = state_from_payload(loc_status.get("v2_state"))
        if (
            loc_cfg.get("enabled")
            and loc_status.get("state") == "outside"
            and loc_state is not None
            and current - loc_state.last_fix_at <= 20 * 60
        ):
            capabilities.add("location.poi_search")
    except Exception:
        pass
    try:
        from app.presence import presence_service, sprite_library
        from app.presence.renderer import presence_renderer_configured

        if await sprite_library.can_draw():
            capabilities.add("desktop.presence.draw")
        if presence_renderer_configured() and await presence_service.ready_for_show():
            capabilities.add("desktop.presence.show")
    except Exception:
        pass
    return frozenset(capabilities)


def build_autonomous_runtime_context(
    *,
    now: float,
    capabilities: frozenset[str],
    user_name: str,
    ai_name: str | None = None,
    last_user_ts: float | None = None,
    heading: str = "空闲窗口实时状态",
    context_delivery_text: str | None = None,
) -> str:
    parts = [
        f"[{heading}]",
        f"当前时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}",
    ]
    if last_user_ts is not None:
        elapsed = max(0.0, now - float(last_user_ts))
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        last_chat_ago = f"{hours}小时{minutes}分钟" if hours else f"{minutes}分钟"
        parts.append(f"距{user_name}上次说话：{last_chat_ago}")
    parts.append("本轮已挂载工具：" + ("、".join(sorted(capabilities)) or "无"))
    if autonomous_context_delivery_enabled():
        try:
            context_text = (
                render_autonomous_context_delivery(
                    user_name=user_name,
                    ai_name=ai_name,
                    reference_time=now,
                )
                if context_delivery_text is None
                else str(context_delivery_text)
            )
            if context_text:
                parts.append(context_text)
        except Exception as exc:
            # Shared context is optional enrichment.  When its flag is on, a
            # read failure must fail closed instead of reviving legacy facts.
            log.warning("Autonomous context delivery skipped: %s", exc)
    else:
        try:
            from sensing import format_sensing_for_prompt

            sensing_text = format_sensing_for_prompt(hours=1) or ""
            if sensing_text:
                parts.append(f"体感信号：{sensing_text}")
        except Exception:
            pass
        try:
            from location import format_location_for_prompt

            location_text = format_location_for_prompt() or ""
            if location_text:
                parts.append(f"位置：{location_text}")
        except Exception:
            pass
    parts.append(render_device_proxy_hard_limits(user_name))
    return "\n".join(parts)


def autonomous_context_delivery_enabled(
    behavior: Mapping[str, object] | None = None,
) -> bool:
    current = behavior if behavior is not None else load_ai_behavior()
    return bool(current.get("context_delivery_autonomous_enabled", False))


def render_autonomous_context_delivery(
    *,
    user_name: str,
    ai_name: str | None = None,
    reference_time: float,
) -> str:
    """Use the sole projection renderer at an autonomous provider boundary."""

    from app.chat.worldbook import resolve_worldbook_names
    from config import load_worldbook
    from context_delivery_runtime_readers import render_current_context_delivery

    if not str(ai_name or "").strip():
        _configured_user_name, ai_name = resolve_worldbook_names(load_worldbook())
    return render_current_context_delivery(
        user_name=user_name,
        ai_name=str(ai_name),
        reference_time=reference_time,
    )


async def load_autonomous_context_delivery(
    *,
    user_name: str,
    ai_name: str,
    conv_id: str | None,
    reference_time: float,
    exclude_summon_id: str | None = None,
) -> str:
    """Fetch durable relationship events at an async provider boundary."""

    from context_delivery_runtime_readers import render_current_context_delivery_async

    return await render_current_context_delivery_async(
        user_name=user_name,
        ai_name=ai_name,
        conv_id=conv_id,
        reference_time=reference_time,
        exclude_summon_id=exclude_summon_id,
    )


__all__ = [
    "autonomous_context_delivery_enabled",
    "build_autonomous_runtime_context",
    "load_autonomous_context_delivery",
    "render_autonomous_context_delivery",
    "resolve_autonomous_capabilities",
]
