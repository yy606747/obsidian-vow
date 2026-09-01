"""Shared epistemic boundaries for device-proxy context."""

from __future__ import annotations


DEFAULT_ADDRESSEE = "她"


def device_proxy_hard_limits(user_name: str = DEFAULT_ADDRESSEE) -> tuple[str, ...]:
    """Boundary lines for reading device signals, addressed in-persona."""

    who = str(user_name or DEFAULT_ADDRESSEE).strip() or DEFAULT_ADDRESSEE
    return (
        f"{who}最近亲口说的情况，永远压过设备信号。"
        f"定位、WiFi、光线、运动状态、电脑和手机状态，都不能推翻{who}自己说的事。",
        f"设备信号和{who}说的对不上时，先当作设备分不清、设备不在{who}身上，"
        "或者这个读数本身就说明不了任何事。",
        f"绝不能因为这种对不上，就说{who}骗你、撒谎、编故事，或者被你抓到了。"
        "拿不准就先放着、先不动，或者自然地问一句。",
    )


def trigger_context_hard_limit(user_name: str = DEFAULT_ADDRESSEE) -> str:
    """One line separating past intent from present fact."""

    who = str(user_name or DEFAULT_ADDRESSEE).strip() or DEFAULT_ADDRESSEE
    return (
        f"你之前为什么想找{who}、原本打算说什么，都只是当时的念头，"
        f"不是{who}此刻的状态。这些不能盖过最近的聊天和眼前的事实。"
    )


DEVICE_PROXY_HARD_LIMITS = device_proxy_hard_limits()
TRIGGER_CONTEXT_HARD_LIMIT = trigger_context_hard_limit()


def render_device_proxy_hard_limits(
    user_name: str = DEFAULT_ADDRESSEE,
    *,
    include_trigger_context: bool = True,
) -> str:
    """Render one shared provider-facing boundary block."""

    limits = list(device_proxy_hard_limits(user_name))
    if include_trigger_context:
        limits.append(trigger_context_hard_limit(user_name))
    return "【设备信号的边界】\n" + "\n".join(f"- {item}" for item in limits)


__all__ = [
    "DEFAULT_ADDRESSEE",
    "DEVICE_PROXY_HARD_LIMITS",
    "TRIGGER_CONTEXT_HARD_LIMIT",
    "device_proxy_hard_limits",
    "render_device_proxy_hard_limits",
    "trigger_context_hard_limit",
]
