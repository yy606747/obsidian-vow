from __future__ import annotations

from app.tools.prompt_renderers import render_registered_capabilities
from app.tools.registry import registered_tools_for_surface, validate_turn_advertisement


class ScheduleAbilityPrompt(str):
    advertised_tools: tuple[str, ...]

    def __new__(cls, content: str, *, available_tools=(), advertised_tools=()):
        value = super().__new__(cls, content)
        value.advertised_tools = validate_turn_advertisement(
            available_tools,
            advertised_tools,
        )
        return value


def build_abilities_block(user_name: str, schedule_text: str) -> str:
    available_tools = tuple(tool for tool in registered_tools_for_surface("schedule") if tool != "schedule.alarm")
    rendered = render_registered_capabilities("schedule", capabilities=available_tools, context={"user_name": user_name})
    abilities = [prose for _tool_name, prose in rendered]
    block = "[系统能力] 你可以在回复中根据对话氛围，善用以下指令：\n"
    block += "\n".join(f"{i+1}. {ability}" for i, ability in enumerate(abilities))
    block += f"\n\n【指令使用建议】大多数回复直接聊天即可，无需每条都用指令。只有在{user_name}明确要求、你刚承诺提醒/记录，或上下文确实需要系统动作时，才使用对应指令。"
    block += f"\n\n【当前日程列表】\n{schedule_text}"
    return ScheduleAbilityPrompt(
        block,
        available_tools=available_tools,
        advertised_tools=available_tools,
    )


def build_alarm_trigger_prompt(item: dict, now_str: str, user_name: str) -> str:
    return (
        f"[日程闹铃触发]\n"
        f"日程内容：{item['trigger_at']} — {item['content']}\n"
        f"现在时间已经到了（当前 {now_str}），请提醒【{user_name}】。"
    )


def build_monitor_trigger_prompt(
    item: dict,
    now_str: str,
    user_name: str,
    evidence_text: str,
    evidence_errors: list[str],
) -> str:
    trigger_prompt = (
        f"[定时查岗触发]\n"
        f"你之前设置了在 {item['trigger_at'].replace('T', ' ')} 查看【{user_name}】的状态。\n"
        f"查岗目的：{item['content']}\n"
        f"当前时间：{now_str}\n"
        f"{evidence_text}"
    )
    if evidence_errors:
        trigger_prompt += (
            f"\n【系统调试提示】部分查岗证据读取失败，不能把缺失数据当成{user_name}没有活动。"
            "请只基于已给出的信号和聊天上下文判断。\n"
        )
    return trigger_prompt + _monitor_guidance(user_name)


def build_merged_trigger_prompt(
    items: list[dict],
    now_str: str,
    user_name: str,
    evidence_text: str,
    evidence_errors: list[str],
) -> str:
    lines = [
        "[日程批量触发]",
        f"以下 {len(items)} 条日程/查岗同时到期，请在一条回复中自然地处理所有事项。",
        f"当前时间：{now_str}",
        "",
    ]
    for index, item in enumerate(items, 1):
        if item["type"] == "monitor":
            lines.extend([
                f"--- 第 {index} 项：定时查岗 ---",
                f"你之前设置了在 {item['trigger_at'].replace('T', ' ')} 查看【{user_name}】的状态。",
                f"查岗目的：{item['content']}",
                "",
            ])
        else:
            lines.extend([
                f"--- 第 {index} 项：闹铃 ---",
                f"日程内容：{item['trigger_at']} — {item['content']}",
                "",
            ])
    if evidence_text:
        lines.append(evidence_text.rstrip())
        lines.append("")
    if evidence_errors:
        lines.append(f"【系统调试提示】部分查岗证据读取失败，不能把缺失数据当成{user_name}没有活动。请只基于已给出的信号和聊天上下文判断。")
        lines.append("")
    if any(item["type"] == "monitor" for item in items):
        lines.append(_monitor_guidance(user_name).lstrip())
        lines.append("")
    lines.append("请一条回复处理完所有事项，不要分开回答。重要的事项优先处理。")
    return "\n".join(lines)


def build_system_message(items: list[dict], ai_name: str) -> str:
    if len(items) > 1:
        return f"⏰ {len(items)} 条日程同时到期"
    item = items[0]
    if item["type"] == "monitor":
        return f"{ai_name}来查岗了"
    return f"⏰ 日程闹铃触发：{item['content']}"


def _monitor_guidance(user_name: str) -> str:
    return (
        f"\n【判断指引——避免误判】\n"
        f"- 「静止+暗处+锁屏」≠ 睡觉。手机可能正面朝下、在包里、或只是没在用。"
        f"只有深夜时段（约 23:00~7:00）且长时间无任何解锁/通知/活动才值得怀疑在睡觉。\n"
        f"- 单次「跑步」读数 ≠ 在跑步。拿起手机、放进口袋、翻身等瞬间动作都会产生高加速度。"
        f"只有连续多条显示「跑步」且伴随步数增长才可判定在运动。\n"
        f"- 宁可用「不太确定你在干嘛」的口吻自然搭话，也不要基于弱信号强行下结论。\n"
        f"\n请结合上述数据和之前的对话上下文，推断{user_name}当前可能的状态，"
        f"然后自然地开口搭话，不要罗列数据。"
    )


__all__ = [
    "ScheduleAbilityPrompt",
    "build_abilities_block",
    "build_alarm_trigger_prompt",
    "build_merged_trigger_prompt",
    "build_monitor_trigger_prompt",
    "build_system_message",
]
