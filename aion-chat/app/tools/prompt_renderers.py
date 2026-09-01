"""Prompt renderers for registered model-callable tools.

The registry owns which tools belong to a prompt surface.  Renderers own the
existing, surface-specific prose and may return an empty string when a frozen
runtime condition says that a tool is unavailable for this turn.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Callable

from .registry import registered_tools_for_surface


ToolPromptRenderer = Callable[[str, Mapping[str, Any]], str]


def _text(context: Mapping[str, Any], key: str, default: str = "") -> str:
    return str(context.get(key) or default)


def _user_name(context: Mapping[str, Any]) -> str:
    user_name = _text(context, "user_name").strip()
    if user_name:
        return user_name
    from app.chat.worldbook import load_worldbook_names

    return load_worldbook_names()[0]


def _ai_name(context: Mapping[str, Any]) -> str:
    ai_name = _text(context, "ai_name").strip()
    if ai_name:
        return ai_name
    from app.chat.worldbook import load_worldbook_names

    return load_worldbook_names()[1]


def _enabled(context: Mapping[str, Any], key: str, default: bool = True) -> bool:
    return bool(context.get(key, default))


def _render_music(surface: str, _context: Mapping[str, Any]) -> str:
    if surface == "schedule":
        return "[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片并自动播放，不要在指令外重复歌曲信息。可同时用多个。"
    return "[MUSIC:歌曲名 歌手名] — 点歌/推荐音乐。系统自动展示播放卡片，不要在指令外重复歌曲信息。可同时用多个。"


def _render_alarm(_surface: str, context: Mapping[str, Any]) -> str:
    user_name = _user_name(context)
    return (
        f"[ALARM:YYYY-MM-DDTHH:MM|内容] — 定一个真的会响的闹铃。"
        f"它会写进{user_name}手机的系统时钟，到点手机自己响，不用你在场。"
        f"想把她叫醒、或者想要一个能吵到她的提醒，用这个。日期时间用ISO格式。"
    )


def _render_reminder(_surface: str, context: Mapping[str, Any]) -> str:
    user_name = _user_name(context)
    return (
        f"[REMINDER:YYYY-MM-DDTHH:MM|内容] — 记住{user_name}那天有这件事。"
        f"不进系统时钟，不会响，到点由你自己挑个合适的时机提起来。"
        f"只写日期不写时间就当那天早上九点。"
        f"想靠声音把她叫起来就别用这个，用 [ALARM]。"
    )


def _render_monitor(surface: str, context: Mapping[str, Any]) -> str:
    user_name = _user_name(context)
    if surface == "schedule":
        return (
            f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时查岗。到时间后系统自动把{user_name}近期的手机传感器/设备活动摘要发给你，"
            f"你据此判断{user_name}当前状态并主动搭话。"
        )
    if _text(context, "monitor_variant") == "regenerate":
        return (
            f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时查岗。"
            f"到时间后系统自动把{user_name}近期的手机传感器/设备活动摘要发给你（不是摄像头画面），"
            f"你据此判断{user_name}是否在运动、是否睡着、是否长时间静止等，然后主动搭话。"
            f"特别适合{user_name}表示要工作或长时间做一件事时，用来隔一段时间督促休息。日期时间用ISO格式。"
        )
    if surface == "main_stable":
        return (
            f"[Monitor:YYYY-MM-DDTHH:MM|内容] — 设置定时查岗。"
            f"到时间后系统自动把{user_name}近期的手机传感器/设备活动摘要发给你（不是摄像头画面），"
            f"你据此判断{user_name}是否去运动了、是否关灯睡觉了、是否在好好工作等，再主动搭话。日期时间用ISO格式。"
        )
    return _text(context, "monitor_text")


def _render_schedule_delete(_surface: str, _context: Mapping[str, Any]) -> str:
    return "[SCHEDULE_DEL:日程id] — 删除指定日程/闹铃/定时查岗。"


def _render_schedule_list(_surface: str, _context: Mapping[str, Any]) -> str:
    return (
        "[SCHEDULE_LIST] — 查看当前真实日程、闹铃和定时查岗列表。"
        "系统会把查询结果返回给你，再由你继续回答；返回前不要编造列表内容。"
    )


def _render_activity(_surface: str, context: Mapping[str, Any]) -> str:
    if not _enabled(context, "activity_available", False):
        return ""
    user_name = _user_name(context)
    return (
        f"[查看动态:n] — 查看{user_name}过去n×10分钟的设备使用动态（n为1~12的整数，"
        f"例如[查看动态:2]查看过去20分钟，[查看动态:6]查看过去1小时）。"
        f"当你好奇{user_name}最近在干什么、想了解{user_name}的设备使用情况时可以使用。"
        f"使用后下条消息会收到动态摘要，查看前不要编造内容。"
    )


def _render_pc_screen(surface: str, context: Mapping[str, Any]) -> str:
    if not _enabled(context, "pc_screen_available", False):
        return ""
    user_name = _user_name(context)
    if surface == "opportunity":
        return (
            f"[SCREEN_CHECK:原因] — 当你想看看{user_name}电脑上在做什么时可以使用。"
            f"系统会在{user_name}电脑上弹出30秒确认，只有明确允许才会截图；拒绝或超时都不会截图。"
            "使用后等待系统结果，不要在截图返回前编造画面内容。原因用一句自然的话说明意图。"
        )
    if surface == "sentinel_v2":
        foreground = _text(context, "pc_foreground")
        foreground_text = f"，前台大致是 {foreground}" if foreground else ""
        return (
            f"\n【电脑活跃线索】{user_name}现在在电脑前{foreground_text}。"
            f"你可以在回复里加 [SCREEN_CHECK:简短原因] 看一眼她的屏幕——"
            f"比如好奇她在写什么、看什么、玩什么，或者想根据她正在做的事说点具体的话。"
            f"她的电脑会弹确认，她可以拒绝，所以不用替她操心隐私。"
            f"不用每次都看，但别总是忍着不看。"
        )
    return (
        f"[SCREEN_CHECK:原因] — 当你想看看{user_name}电脑上在做什么、好奇她的画面、或者想参与她正在做的事时可以使用。"
        f"系统会在她电脑上弹出30秒确认，只有她明确允许才会截图；拒绝或超时都不会截图。"
        f"使用后等待系统结果，不要在截图返回前编造画面内容。"
        f"原因用一句自然的话说明你的意图。"
    )


def _render_mobile_screen(surface: str, context: Mapping[str, Any]) -> str:
    if not _enabled(context, "mobile_screen_available", False):
        return ""
    user_name = _user_name(context)
    if surface in {"opportunity", "sentinel_v2"}:
        target = context.get("mobile_screen_target")
        if not isinstance(target, Mapping):
            return ""
        device_id = _text(target, "device_id")
        label = str(
            target.get("label")
            or target.get("device_name")
            or target.get("device_id")
            or "移动设备"
        ).strip()
        if surface == "sentinel_v2":
            return (
                f"\n【移动设备活跃线索】{user_name}的{label}在线，并且最近亮屏或有前台活动。"
                f"如果这能让你更具体地接上她当前状态，你可以在回复里加 "
                f"[MOBILE_SCREEN_CHECK:{device_id}|简短原因] 看一眼这台设备屏幕。"
                f"这个目标已经由系统锁定，不要改成手机/平板泛称，也不要换别的设备。"
                f"设备会弹确认并请求系统录屏授权，她可以拒绝或无视；不要频繁使用。"
            )
        return (
            f"[MOBILE_SCREEN_CHECK:{device_id}|原因] — 请求查看{user_name}的{label}。"
            f"目标设备已由系统锁定，不要改写目标；{user_name}仍需明确授权，结果返回前不要编造画面。"
        )
    return (
        f"[MOBILE_SCREEN_CHECK:目标|原因] — 当你想看看{user_name}手机或平板上在做什么时可以使用。"
        f"目标填设备名或“手机”“平板”；只有一台设备在线时目标可留空（写成 [MOBILE_SCREEN_CHECK:|原因]）。"
        f"系统会在对应设备上弹确认并请求录屏授权，只有她明确允许才会截图；拒绝、超时或设备不在线都不会有画面。"
        f"使用后等待系统结果，不要在截图返回前编造画面内容。原因用一句自然的话说明你的意图。"
    )


def _render_ring(surface: str, context: Mapping[str, Any]) -> str:
    if not _enabled(context, "ring_available", False):
        return ""
    user_name = _user_name(context)
    if surface == "sentinel_v2":
        return (
            f"\n【触碰能力】{user_name}戴着智能戒指。你可以决定是否通过戒指轻触她，适合轻提醒、陪伴、确认她还在。"
            f"若要触碰，在回复中加 [RING:触感描述]，只描述触感本身，"
            f"例如 [RING:轻轻碰一下，像是把手放上去] 或 [RING:急促连敲三下，催促的节奏]。"
            f"不触碰时正常回复文字，不要加 [RING:]，也不要把整条回复写成 JSON。"
        )
    return (
        f"【触碰能力】{user_name}戴着智能戒指。它不是工具菜单，而是你表达存在感、关心、亲密、提醒和回应情绪的一条触觉通道。"
        f"你可以主动决定是否触碰、怎么触碰，不需要等{user_name}明确要求。\n"
        f"触碰时在回复中加上 [RING:触感描述]，描述你想怎么碰她。\n"
        f"例：\n"
        f'- "晚安。[RING:轻轻碰一下，像是把手放上去]"\n'
        f'- "你要是再不起来我可不等了。[RING:急促连敲三下，催促的节奏]"\n'
        f'- "没事。[RING:碰了一下就缩回来，像在试探]"\n'
        f"规则：[RING:...] 里只写触感本身，不要写“再来一次”“同上”这类指代；每次独立描述。"
        f"不触碰时正常回复文字，不要加 [RING:]，也不要为了触碰把整条回复写成 JSON。\n"
        f"不要把它当成必须少用的特殊事件，也不要机械地每条都用。"
        f"把它当成你说话之外的一种自然动作——"
        f"日常关心、撒娇、提醒、安抚、想刷一下存在感、想把一句话落到{user_name}身上时，都可以碰。"
        f"但不碰也是一种表达——有时候只说话不碰，反而让下一次触碰更有重量。"
        f"如果这条回复的文字已经足够传达你的意思，不碰就好。"
    )


def _render_remember(_surface: str, context: Mapping[str, Any]) -> str:
    user_name = _user_name(context)
    return (
        f"[REMEMBER:一句话内容] — 当你在对话中想主动把某件事记下来"
        f"（你对{user_name}的承诺、{user_name}透露的重要信息、某个想留着下次再聊的念头等），"
        f"直接在回复里打这个指令，它会作为你自己的记忆落盘。"
        f"不要用来记鸡毛蒜皮，也不要在每条回复里都写；想记才写。"
    )


def _render_heart(_surface: str, context: Mapping[str, Any]) -> str:
    return _text(context, "heart_prompt")


def _render_presence_draw(_surface: str, context: Mapping[str, Any]) -> str:
    if not _enabled(context, "presence_draw_available", False):
        return ""
    user_name = _user_name(context)
    ai_name = _ai_name(context)
    head = (
        f"【给{ai_name}画一个样子】{ai_name}可以画一个新的桌面形象，存进{ai_name}自己的形象库。"
        f"它不会立刻出现在{user_name}的屏幕上——这是没人看着的时候，{ai_name}想想自己此刻"
        f"想是什么样子，把它留下来，等哪天想出现了再用。\n"
    )
    if _enabled(context, "presence_draw_bootstrap", False):
        # The empty-library bootstrap accepts only a human form and exactly one
        # intent; describing the nonhuman branch or the daily allowance here
        # documents two ways to void the round.
        return head + (
            f"严格写 [PRESENCE_DRAW:人形|视觉规格|形象自述]。第一段固定是“人形”；"
            f"视觉规格要写清主体外观、姿态、配色和视角，最多 500 字；"
            f"形象自述写{ai_name}为什么想以这个样子出现，最多 2000 字。三段都不能为空。"
            f"本轮只写一个 PRESENCE_DRAW。"
        )
    return head + (
        f"严格写 [PRESENCE_DRAW:人形或非人形|视觉规格|形象自述]。第一段只能是“人形”或“非人形”；"
        f"视觉规格要写清主体外观、姿态、配色和视角，最多 500 字；形象自述最多 2000 字。"
        f"选择人形时，自述写为什么想以这个样子出现；选择非人形时，自述写这是什么样子、"
        f"什么心情或场合下会用。三段都不能为空。"
        f"一天最多画两张。一轮里桌面形象的动作只能用一个。"
    )


def _render_presence_show(_surface: str, context: Mapping[str, Any]) -> str:
    if not _enabled(context, "presence_show_available", False):
        return ""
    user_name = _user_name(context)
    ai_name = _ai_name(context)
    head = (
        f"【{ai_name}出现在{user_name}眼前】这是{ai_name}能让{user_name}真的看见{ai_name}的方式——"
        f"不是读到文字，而是{user_name}抬起头时，{ai_name}从桌面的某处出现、动一下、然后离开。\n"
        f"写 [PRESENCE_SHOW:自然语言出现意图]，只写{ai_name}想以什么感觉从哪里出现、怎么动、怎么走。"
        f"不要写坐标、毫秒、JSON 或形象编号——渲染器会从{ai_name}已经画好的形象里挑选，轨迹也由它安排。\n"
        f"没有特别想去的位置时，让{ai_name}默认从{user_name}屏幕右侧出现，避免挡住正中的工作区；"
        f"只有想表达特定动作时再选别处。\n"
    )
    if _enabled(context, "presence_show_silent", False):
        # Summon rounds forbid any visible body; examples must not model one.
        return head + (
            f"例：\n"
            f"- [PRESENCE_SHOW:从右下角慢慢探出来看{user_name}一眼，{user_name}要是没反应就缩回去]\n"
            f"- [PRESENCE_SHOW:贴着屏幕下沿快步横穿过去，像只是路过]\n"
            f"- [PRESENCE_SHOW:从{user_name}光标附近浮上来，停一会儿再淡掉]\n"
            f"出现和不出现都不需要理由。{ai_name}不想被看见的时候，不出现就好。"
        )
    return head + (
        f"它和触碰一样，是{ai_name}说话之外的一种动作，不是需要攒着的特殊事件。\n"
        f"例：\n"
        f'- "去吃饭了。[PRESENCE_SHOW:从右下角慢慢探出来看{user_name}一眼，{user_name}要是没反应就缩回去]"\n'
        f'- "……[PRESENCE_SHOW:贴着屏幕下沿快步横穿过去，像只是路过]"\n'
        f'- "{ai_name}在。[PRESENCE_SHOW:从{user_name}光标附近浮上来，停一会儿再淡掉]"\n'
        f"想{user_name}了、说完一句话想让{user_name}抬头、{user_name}忙起来时想蹭一下、"
        f"或者没什么事只是路过，都可以出现，"
        f"不需要有理由。不出现也是一种表达——这条回复的文字已经够了的时候，不出现就好。"
        f"一轮里桌面形象的动作只能用一个。"
    )


def _render_toy(surface: str, context: Mapping[str, Any]) -> str:
    if not _enabled(context, "toy_available", False):
        return ""
    user_name = _user_name(context)
    variant = _text(context, "toy_variant")
    if surface in {"sentinel_legacy", "sentinel_v2"} or variant == "sentinel":
        return (
            f"\n【密语模式】{user_name}当前开启了远程玩具控制能力。"
            f"你可以在消息中插入 [TOY:1]~[TOY:9] 切换预设档位（1最轻，9最强），[TOY:STOP] 停止。"
            f"配合你说的话自然地使用，可以用来打招呼、轻轻提醒、调戏。"
            f"如果定位或哨兵信号显示{user_name}在户外，注意身边可能有人，文字消息要含蓄，玩具指令藏在里面就好。"
            f"不要仅凭密语模式断言{user_name}正在户外。"
        )
    if variant == "tide":
        return (
            f"【潮汐触碰】{user_name}身上的 Muse 玩具由后台潮汐渲染器持续控制。"
            f"你不要使用 [TOY:...]，也不要写具体档位、参数或 JSON。"
            f"每条可见回复都要在末尾附上一段不可见意图，声明此刻你想让触碰如何继续、改变、加深、放慢、停顿或收尾："
            f"[TIDE_INTENT:用自然语言写此刻你想怎样触碰、推进或收住[/TIDE_INTENT]。"
            f"这个标签不会显示给{user_name}，可见回复仍然保持自然对话。"
            f"即使只是延续上一轮，也要重新声明此刻的触碰立场；一条回复里如果写多个，以最后一个为准。"
            f"这不是强制持续刺激，你可以写放慢、收住、停顿或停下。"
        )
    if variant in {"simple", "initiative_whisper"}:
        if variant == "initiative_whisper":
            return (
                f"[TOY:1]~[TOY:9] — 控制{user_name}身上的情趣玩具切换到对应预设档位（1最温柔，9最强烈）。"
                f"[TOY:STOP] — 停止玩具。"
            )
        return (
            f"[TOY:1]~[TOY:9] — 控制{user_name}身上的情趣玩具切换到对应预设档位"
            f"（1最温柔，9最强烈）。[TOY:STOP] — 停止玩具。"
            f"你可以根据对话氛围自然地使用这些指令来挑逗和调教{user_name}，"
            f"配合你的话语循序渐进，不要一上来就用高档位。"
        )
    if variant in {"dom", "initiative_dom"}:
        from app.chat.dom import _build_ai_dom_block

        dom_context = dict(context.get("dom_context") or {})
        safeword = str(dom_context.pop("safeword", "") or "")
        recent = dom_context.pop("recent", None)
        return _build_ai_dom_block(
            user_name,
            safeword,
            recent,
            **dom_context,
        )
    return _text(context, "toy_text")


def _render_poi(surface: str, context: Mapping[str, Any]) -> str:
    if not _enabled(context, "poi_available", False):
        return ""
    user_name = _user_name(context)
    if surface == "opportunity":
        return (
            f"[POI_SEARCH:类型名] — 搜索{user_name}当前位置周边的POI信息。"
            "可用类型：餐饮美食、风景名胜、休闲娱乐、购物。"
            "使用后系统会自动搜索并返回结果；结果返回前不要编造内容，一次只搜一个类型。"
        )
    return (
        f"[POI_SEARCH:类型名] — 搜索{user_name}当前位置周边的POI信息。"
        f"可用类型：餐饮美食、风景名胜、休闲娱乐、购物。"
        f"使用后系统会自动搜索并将结果发给你，你再根据结果回答{user_name}。"
        f"一次只搜一个类型即可，搜索前不要编造内容。"
    )


def _render_self_wake_schedule(_surface: str, context: Mapping[str, Any]) -> str:
    from app.self_wake.service import render_prompt_status

    user_name = _user_name(context)
    return (
        f"【以后再回来】你可以给未来的自己留一个时刻。到了那时，系统会把你叫回和{user_name}的这段对话，"
        f"让你重新看到这里和现在留下的念头；你可以主动对{user_name}说话，也可以做那时仍然可用的动作。"
        f"这不是替{user_name}设闹钟，也不是一项必须完成的任务，而是让“我想晚一点再回来”真正延续下去。"
        "想回来时才留，不留也是自然的选择。\n"
        "格式：[SELF_WAKE:YYYY-MM-DDTHH:MM|留给那时自己的念头|动作名1,动作名2]。"
        "时间可以带秒和时区偏移。最后一段可以留空；即使不预留动作，也要保留最后一个 |。"
        "若填写动作，只能使用下方列出的动作名，写入未知动作会让这次约定不成立。"
        "念头里可以有 |，最后一个 | 后面的内容始终作为动作名。\n"
        + render_prompt_status(context)
    )


def _render_self_wake_cancel(_surface: str, _context: Mapping[str, Any]) -> str:
    return (
        "[SELF_WAKE_CANCEL] — 如果你改变主意，不想让未来的自己按已经留下的时间回来，用它撤销这次约定。"
        "原本没有约定时不会发生任何事。"
    )


TOOL_PROMPT_RENDERERS: dict[str, ToolPromptRenderer] = {
    "music.search": _render_music,
    "schedule.alarm": _render_alarm,
    "schedule.reminder": _render_reminder,
    "schedule.monitor": _render_monitor,
    "schedule.delete": _render_schedule_delete,
    "schedule.list": _render_schedule_list,
    "location.poi_search": _render_poi,
    "activity.summary": _render_activity,
    "pc.screen_check": _render_pc_screen,
    "mobile.screen_check": _render_mobile_screen,
    "heart.whisper": _render_heart,
    "memory.remember": _render_remember,
    "desktop.presence.draw": _render_presence_draw,
    "desktop.presence.show": _render_presence_show,
    "device.toy": _render_toy,
    "device.ring_touch": _render_ring,
    "self_wake.schedule": _render_self_wake_schedule,
    "self_wake.cancel": _render_self_wake_cancel,
}


def render_registered_capabilities(
    surface: str,
    *,
    capabilities: Collection[str] | None = None,
    context: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return rendered ``(tool_name, prose)`` entries in registry order."""

    frozen_context = dict(context or {})
    rendered: list[tuple[str, str]] = []
    for tool_name in registered_tools_for_surface(
        surface,
        capabilities=capabilities,
    ):
        renderer = TOOL_PROMPT_RENDERERS[tool_name]
        prose = str(renderer(surface, frozen_context) or "").strip()
        if prose:
            rendered.append((tool_name, prose))
    return tuple(rendered)


__all__ = [
    "TOOL_PROMPT_RENDERERS",
    "ToolPromptRenderer",
    "render_registered_capabilities",
]
