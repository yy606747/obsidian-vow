"""誓约层 prompt 片段：常驻注入 block、能力纪律片段、must-fire 降级模板。

注入位置由各管道负责（chat_turn.py 等，恒在 control / aftercare 的
ability block 之前，§5.1）；本模块只产出文本。
"""

import time

from app.chat.worldbook import load_worldbook_names


def _resolved_user_name(user_name: str | None) -> str:
    return str(user_name or "").strip() or load_worldbook_names()[0]


def _vow_block_header(user_name: str) -> str:
    return (
        "【你们之间已经说定的事】\n"
        f"以下是你和{user_name}明确说定、目前仍有效的约定。它们不是按话题召回的记忆，\n"
        "理解和回应时应始终保持一致。它们不授予任何设备或 control 权限，\n"
        f"也不覆盖{user_name}此刻明确表达的边界、安全词和安全停止。不要机械复述。"
    )


def _age_label(created_at: float, now: float) -> str:
    days = int(max(0.0, now - created_at) // 86400)
    if days < 1:
        return "今天立下"
    if days < 2:
        return "昨天立下"
    if days < 30:
        return f"{days}天前立下"
    if days < 365:
        return f"{days // 30}个月前立下"
    return f"{days // 365}年前立下"


def build_vow_block(
    active_vows: list[dict],
    *,
    now: float | None = None,
    user_name: str | None = None,
) -> str:
    """全部 active 按 created_at 排列，带年龄。不排序不打分。无 active 时返回空串。"""
    if not active_vows:
        return ""
    user_name = _resolved_user_name(user_name)
    now_ts = now if now is not None else time.time()
    lines = [_vow_block_header(user_name), ""]
    for vow in active_vows:
        lines.append(f"- {vow['content']}（{_age_label(vow['created_at'], now_ts)}）")
    return "\n".join(lines)


def build_vow_ability_block(
    *,
    remaining_today: int,
    user_name: str | None = None,
) -> str:
    """[VOW] 写入能力说明。只提供给 send / regenerate 两条路径（§4.6）。

    每日限额状态放在这里，不放常驻 block（§5.3）。
    """
    user_name = _resolved_user_name(user_name)
    if remaining_today > 0:
        quota_line = f"今天你还可以主动立约 {remaining_today} 次。"
        usage = (
            f"当你和{user_name}明确说定了一件关于你们之间、值得永远为真的事，"
            "你可以用 [VOW:誓约内容|确认语] 把它正式记下。"
            "誓约内容是约定本身（240 字以内）；确认语是你对这次立约说的一句话"
            "（120 字以内），只有系统确认写入成功后才会出现在你的回复里。\n"
            "准入判据：只收一年后你们仍希望它为真的东西；拿不准，不收。\n"
            "不要在正文里声称已经立约——确认语只写在标记内，由系统在写入成功后替你说出。"
        )
    else:
        quota_line = "今天的立约额度已经用完，不要再输出 [VOW] 标记。"
        usage = ""
    parts = [p for p in (usage, quota_line) if p]
    return "\n".join(parts)


# 确认/拒绝说明（§4.3）：都持久化在 assistant 正文里，当场所见 = 刷新所见。
# 确认语带固定前缀，作为"系统识别的正式立约确认"（§1 原则 3）的可见锚。
VOW_CONFIRMATION_PREFIX = "🔏 "


def format_vow_confirmation(affirmation: str) -> str:
    return f"{VOW_CONFIRMATION_PREFIX}{affirmation}"


def format_vow_rejection(reason: str) -> str:
    """中性系统措辞，不以人格口吻解释。"""
    return f"（这次的约定没有正式记下：{reason}。）"


# must-fire 降级模板（§5.2）：誓约读取失败时闹铃照常触发，
# 使用固定模板文本（非模型生成），不以人格开口。
ALARM_FALLBACK_TEMPLATE = "⏰ 到点了：{content}"


def build_alarm_fallback_text(content: str) -> str:
    return ALARM_FALLBACK_TEMPLATE.format(content=(content or "").strip() or "你设过的提醒")
