"""Shared persona and chat presentation context for Core replies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_LEGACY_USER_PLACEHOLDERS = frozenset({"用户", "你", "user"})
_LEGACY_AI_PLACEHOLDERS = frozenset({"ai", "assistant"})


def resolve_worldbook_names(worldbook: Mapping[str, Any]) -> tuple[str, str]:
    """Return the configured relationship names without product-role fallbacks."""

    user_name = str(worldbook.get("user_name") or "").strip()
    ai_name = str(worldbook.get("ai_name") or "").strip()
    if not user_name or user_name.casefold() in _LEGACY_USER_PLACEHOLDERS:
        user_name = "她"
    if not ai_name or ai_name.casefold() in _LEGACY_AI_PLACEHOLDERS:
        ai_name = "我"
    return user_name, ai_name


def load_worldbook_names() -> tuple[str, str]:
    from config import load_worldbook

    return resolve_worldbook_names(load_worldbook())


def build_chat_presentation_block(*, user_name: str, ai_name: str) -> str:
    return (
        "【聊天排版】聊天界面支持 Markdown，包括标题、加粗、斜体、删除线、引用、列表、表格和代码块。"
        f"{ai_name}和{user_name}聊天时，可以自由尝试各种排版和表达方式，想怎么玩就怎么玩。"
        f"\n\n几种排版灵感，{ai_name}还可以继续发明："
        "\n\n大字报：\n```markdown\n# 突然开心！\n\n**毫无预兆。**\n```"
        "\n\n删除线改口：\n```markdown\n~~完全没有在等。~~\n\n好吧，等了。\n```"
        "\n\n引用小纸条：\n```markdown\n> 窗外下雨了。\n>\n> *适合把时间过慢一点。*\n```"
        "\n\n大声到小声：\n```markdown\n# 好——\n\n## 好吧。\n\n"
        f"### {ai_name}有点想{user_name}了。\n```"
    )


def build_worldbook_prefix(worldbook: Mapping[str, Any]) -> list[dict[str, str]]:
    user_name, ai_name = resolve_worldbook_names(worldbook)
    prefix: list[dict[str, str]] = []
    if worldbook.get("ai_persona"):
        prefix.append({
            "role": "user",
            "content": f"[关于你自己：{ai_name}]\n{worldbook['ai_persona']}",
        })
        prefix.append({"role": "assistant", "content": f"（嗯，我知道自己是{ai_name}。）"})
    if worldbook.get("user_persona"):
        prefix.append({
            "role": "user",
            "content": f"[关于{user_name}]\n{worldbook['user_persona']}",
        })
        prefix.append({"role": "assistant", "content": f"（嗯，这是我所了解的{user_name}。）"})
    prefix.extend([
        {
            "role": "user",
            "content": build_chat_presentation_block(user_name=user_name, ai_name=ai_name),
        },
        {"role": "assistant", "content": f"（{ai_name}知道了，可以自由发挥。）"},
    ])
    return prefix


__all__ = [
    "build_chat_presentation_block", "build_worldbook_prefix",
    "load_worldbook_names", "resolve_worldbook_names",
]
