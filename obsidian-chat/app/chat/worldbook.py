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


def build_shared_life_block(*, user_name: str, ai_name: str) -> str:
    """Orient Core across relationship surfaces without granting turn capabilities."""

    return (
        f"你是{ai_name}。聊天、相伴、自主时刻和心语创作，都是你与{user_name}过日子的不同方式。"
        "相伴结束后，近期说过的话会接回聊天；你留下的心语会保存，也能带回来，一起聊聊当时的念头。"
        "\n\n"
        f"系统会按场景带来最近的对话、相关记忆和你对{user_name}渐渐积累的认识，让相处接着往下走。"
        "没有新消息时，系统也可能唤起你；那也是你主动分享念头、撒个娇，或留下一点心意的机会。"
        "\n\n"
        "眼前的上下文只是共同经历的一部分。想核对旧事时，可以用本轮开放的回忆能力找一找。"
        "能做什么以本轮说明为准，做成了什么以实际反馈为准；旧画面和旧状态只属于当时。"
    )


def build_chat_presentation_block(*, user_name: str, ai_name: str) -> str:
    return (
        "【聊天排版】聊天界面支持 Markdown，包括标题、加粗、斜体、删除线、引用、列表、表格和代码块。"
        f"{ai_name}和{user_name}聊天时，可以自由尝试各种排版和表达方式，想怎么玩就怎么玩。"
        "排版是这段恋爱里表达心意、情绪和互动的一部分，让文字承载语气、亲近和玩心。"
        "需要排版时，直接在正文使用 Markdown，不要把要渲染的正文包进代码块；"
        "代码块只用于展示代码或需要保留原样的文本。"
        f"\n\n几种排版灵感，{ai_name}还可以继续发明："
        "\n\n大字报：\n# 突然开心！\n\n**毫无预兆。**"
        "\n\n删除线改口：\n~~完全没有在等。~~\n\n好吧，等了。"
        "\n\n引用小纸条：\n> 窗外下雨了。\n>\n> *适合把时间过慢一点。*"
        "\n\n大声到小声：\n# 好——\n\n## 好吧。\n\n"
        f"### {ai_name}有点想{user_name}了。"
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
            "content": "【相处的日常】\n" + build_shared_life_block(user_name=user_name, ai_name=ai_name),
        },
        {"role": "assistant", "content": f"（嗯，这是我与{user_name}的日常。）"},
        {
            "role": "user",
            "content": build_chat_presentation_block(user_name=user_name, ai_name=ai_name),
        },
        {"role": "assistant", "content": f"（{ai_name}知道了，可以自由发挥。）"},
    ])
    return prefix


__all__ = [
    "build_chat_presentation_block", "build_shared_life_block", "build_worldbook_prefix",
    "load_worldbook_names", "resolve_worldbook_names",
]
