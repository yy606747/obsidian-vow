"""Frozen wire contracts for inverse-query generation and core reflection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .harness import render_labeled_items


INVERSE_QUERY_PROMPT_VERSION = "wm_reflection_inverse_query.v1"
REFLECTION_PROMPT_VERSION = "wm_reflection_core.v1"

INVERSE_QUERY_SYSTEM_PROMPT = """你是反例检索 query 改写器。

输入是一句认识层里的粗认识。请写出一条用于检索历史对话的反向 query，让检索更容易找到能推翻、修正或显著削弱这句认识的具体证据。

硬要求：
1. 保持同一人物、同一主题、同一情境和同一判断维度，不得换话题或偷换标准。
2. 语义方向必须真正相反；原认识和反向 query 描述的倾向不能同时成立。
3. 写成自然、具体、可由言行观察到的反向假设，不要只机械增加或删除“不、没有、并非”等否定词。若原句是否定式自我陈述，优先描述相反的具体表达或行为；例如原句“她不喜欢被打断”时，禁止输出“她喜欢被打断”，可写“她被插话时仍自然接话，并把这种节奏当成顺畅互动”。
4. 不得虚构输入中没有的人物、事件、动机或原因。
5. 不要改写成疑问句，不要写“寻找/检索/是否有证据”等元指令。
6. query 要简洁、独立可读，只输出 JSON：{"query":"一条反向 query"}。
"""

REFLECTION_TASK_PROMPT = """[认识层反思]
你正在私下检查自己对用户的一句认识，不是在回复聊天。材料里的来源标签必须保留其含义：ai_note 是你过去自己的想法，digest/关系卡是模型或系统整理，conversation_excerpt 同时含双方说话，unknown 来源不明；不要把这些悄悄改写成用户事实。

把线索放回完整认识层中理解，再根据给出的历史材料选择一个结论：
- holds：材料与这句认识对得上，仍然成立。
- unclear：材料不足以说清楚。
- conflicts：材料让这句认识不再准确，需要提出更合适的替代表述。

不追求一定修改；holds 与 unclear 都是完整有效的结果。只输出 JSON 对象，必须恰好包含 verdict、reason、proposed_statement 三个字段：
{"verdict":"holds|unclear|conflicts","reason":"一句具体理由","proposed_statement":"仅 conflicts 时填写，否则为空字符串"}
不要输出代码块、解释、最近聊天或任何额外字段。"""

_FENCE_RE = re.compile(r"\A```(?:json)?\s*(.*?)\s*```\Z", re.IGNORECASE | re.DOTALL)


class ReflectionParseError(ValueError):
    pass


def _json_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw.strip():
        raise ReflectionParseError("empty output")
    text = raw.strip()
    match = _FENCE_RE.fullmatch(text)
    if match:
        text = match.group(1).strip()

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReflectionParseError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise ReflectionParseError("output is not JSON") from exc
    if not isinstance(value, dict):
        raise ReflectionParseError("output is not an object")
    return value


def build_inverse_query_messages(clue: str) -> list[dict[str, str]]:
    normalized = " ".join(str(clue or "").split())
    if not normalized:
        raise ValueError("clue must not be empty")
    return [
        {"role": "system", "content": INVERSE_QUERY_SYSTEM_PROMPT},
        {"role": "user", "content": f"原粗认识：{normalized}"},
    ]


def parse_inverse_query(raw: object) -> str:
    payload = _json_object(raw)
    if set(payload) != {"query"}:
        raise ReflectionParseError("inverse query must contain only query")
    if not isinstance(payload.get("query"), str):
        raise ReflectionParseError("inverse query must be text")
    query = " ".join(payload["query"].split())
    if not query:
        raise ReflectionParseError("inverse query is empty")
    return query


def reflection_prompt_version(identity_snapshot: Mapping[str, Any]) -> str:
    digest = str(identity_snapshot.get("sha256") or "").strip()
    if not digest:
        digest = hashlib.sha256(
            str(identity_snapshot.get("text") or "").encode("utf-8")
        ).hexdigest()
    return f"{REFLECTION_PROMPT_VERSION}.identity-{digest[:16]}"


def build_reflection_messages(
    *,
    identity_snapshot: Mapping[str, Any],
    clue: str,
    retrieved_items: Sequence[dict[str, Any]],
    working_model: str,
) -> list[dict[str, str]]:
    identity_text = str(identity_snapshot.get("text") or "").strip()
    if not identity_text:
        raise ValueError("identity snapshot is empty")
    payload = {
        "clue": str(clue or "").strip(),
        "retrieved_evidence": render_labeled_items(retrieved_items),
        "full_working_model": str(working_model or "").strip(),
    }
    if not all(payload.values()):
        raise ValueError("reflection inputs must not be empty")
    return [
        {
            "role": "system",
            "content": f"{identity_text}\n\n{REFLECTION_TASK_PROMPT}",
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def parse_reflection_output(raw: object) -> dict[str, str]:
    payload = _json_object(raw)
    if set(payload) != {"verdict", "reason", "proposed_statement"}:
        raise ReflectionParseError("reflection output fields are invalid")
    verdict = payload.get("verdict")
    reason = payload.get("reason")
    proposed = payload.get("proposed_statement")
    if verdict not in {"holds", "unclear", "conflicts"}:
        raise ReflectionParseError("reflection verdict is invalid")
    if not isinstance(reason, str) or not reason.strip():
        raise ReflectionParseError("reflection reason is empty")
    if not isinstance(proposed, str):
        raise ReflectionParseError("proposed_statement must be text")
    proposed = proposed.strip()
    if verdict == "conflicts" and not proposed:
        raise ReflectionParseError("conflicts requires proposed_statement")
    if verdict != "conflicts" and proposed:
        raise ReflectionParseError("non-conflicts proposed_statement must be empty")
    return {
        "verdict": str(verdict),
        "reason": reason.strip(),
        "proposed_statement": proposed,
    }


def render_reflection_source(log_row: Mapping[str, Any]) -> str:
    try:
        items = json.loads(str(log_row.get("retrieved_items_json") or "[]"))
    except json.JSONDecodeError as exc:
        raise ReflectionParseError("stored evidence JSON is invalid") from exc
    if not isinstance(items, list) or not items:
        raise ReflectionParseError("stored evidence is empty")
    clue = str(log_row.get("clue") or "").strip()
    inverse_query = str(log_row.get("inverse_query") or "").strip()
    if not clue or not inverse_query:
        raise ReflectionParseError("stored reflection context is incomplete")
    return (
        f"[反思线索]\n{clue}\n\n"
        f"[带来源标注的历史材料]\n{render_labeled_items(items)}"
    )


__all__ = [
    "INVERSE_QUERY_PROMPT_VERSION",
    "INVERSE_QUERY_SYSTEM_PROMPT",
    "REFLECTION_PROMPT_VERSION",
    "ReflectionParseError",
    "build_inverse_query_messages",
    "build_reflection_messages",
    "parse_inverse_query",
    "parse_reflection_output",
    "reflection_prompt_version",
    "render_reflection_source",
]
