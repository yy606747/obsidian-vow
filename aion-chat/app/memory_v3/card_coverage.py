"""Pure contract for relational-card semantic coverage checks.

The writer and the coverage checker are intentionally separate.  The writer
only sees the current source chunk; this checker only compares a completed
candidate against earlier accepted v4 cards.  Keeping the contract pure makes
it usable by the offline replay without silently wiring an unvalidated model
judge into production.
"""

from __future__ import annotations

import json
from typing import Any


COVERAGE_PROMPT_VERSION = "relational-card-coverage-v3"
MAX_COMPARISON_CARDS = 8

COVERAGE_INSTRUCTIONS = """你只负责比较关系卡，不负责重写卡片，也不判断卡片是否真实。

输入包含一张刚生成的 v4 候选卡，以及同一会话里来源时间更早、来源 chunk 不同、已经接受的 v4 卡。每张卡都有 kind：shared_moment 或 relational_reading。

“覆盖”的严格定义：
- shared_moment：旧卡已经记住同一件共同经历，候选没有增加独立、值得保留的事实；
- relational_reading：旧卡已经包含同一条由用户自陈的偏好、边界、不满、要求或互动意义，候选没有增加独立条件或例外。
- kind 不同不自动互相覆盖；话题、昵称或语气相似也不算覆盖。

以下不算新增：
- 只换了一种说法或语气；
- 在同一件共同经历内部，只补充了次要经过、行为证据或修辞；发生在另一个时间或场合的经历不算同一件；
- 只是把同一个判断说得更肯定、更动人或更长。

以下应判 novel：
- 候选记住了另一件共同经历；
- 候选提出了旧卡没有的独立偏好、边界、要求或互动意义；
- 候选增加了会改变判断含义的成立条件、例外或矛盾；
- 两张卡只是话题有关，但对用户或关系的核心理解不同。

只有在某一张旧卡明确包含候选的核心判断时才能判 covered；拿不准时判 novel，避免误杀。若多张都覆盖，选择语义最直接的一张。不要因为文字更相似就自动判 covered。

严格只输出以下两种 JSON 对象之一，不要代码块、解释或额外字段：
{"decision":"novel"}
{"decision":"covered","covered_by_card_id":"已有卡 ID"}"""


class CardCoverageContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _card_payload(card: dict[str, Any]) -> dict[str, str]:
    card_id = str(card.get("card_id") or card.get("id") or "").strip()
    source_chunk_id = str(card.get("source_chunk_id") or "").strip()
    content = str(card.get("content") or card.get("note") or "").strip()
    kind = str(card.get("kind") or "").strip().lower()
    if not card_id or not source_chunk_id or not content:
        raise CardCoverageContractError(
            "invalid_card_input",
            "each comparison card requires card_id, source_chunk_id, and content",
        )
    if kind not in {"shared_moment", "relational_reading"}:
        raise CardCoverageContractError(
            "invalid_card_kind",
            "each v4 card requires kind shared_moment or relational_reading",
        )
    return {
        "card_id": card_id,
        "source_chunk_id": source_chunk_id,
        "content": content,
        "kind": kind,
    }


def build_coverage_prompt(
    candidate: dict[str, Any],
    earlier_cards: list[dict[str, Any]],
) -> str:
    """Build a bounded comparison prompt without source conversation text."""

    candidate_payload = _card_payload(candidate)
    comparison_payload = [_card_payload(card) for card in earlier_cards]
    if len(comparison_payload) > MAX_COMPARISON_CARDS:
        raise CardCoverageContractError(
            "too_many_comparison_cards",
            f"at most {MAX_COMPARISON_CARDS} earlier cards may be compared",
        )
    comparison_ids = [card["card_id"] for card in comparison_payload]
    if len(comparison_ids) != len(set(comparison_ids)):
        raise CardCoverageContractError(
            "duplicate_comparison_card",
            "comparison card IDs must be unique",
        )
    if any(
        card["source_chunk_id"] == candidate_payload["source_chunk_id"]
        for card in comparison_payload
    ):
        raise CardCoverageContractError(
            "same_chunk_comparison",
            "a candidate may not be compared with a card from its own source chunk",
        )
    payload = {
        "candidate": candidate_payload,
        "earlier_accepted_v4_cards": comparison_payload,
    }
    return (
        f"{COVERAGE_INSTRUCTIONS}\n\n"
        f"【待判断的卡片】\n{json.dumps(payload, ensure_ascii=False)}"
    )


def validate_coverage_result(
    payload: Any,
    *,
    comparison_card_ids: set[str],
) -> dict[str, str | None]:
    if not isinstance(payload, dict):
        raise CardCoverageContractError(
            "invalid_coverage_payload", "coverage output must be an object"
        )
    decision = str(payload.get("decision") or "").strip().lower()
    if decision == "novel":
        if set(payload) != {"decision"}:
            raise CardCoverageContractError(
                "invalid_novel_shape", "novel output must contain only decision"
            )
        return {"decision": "novel", "covered_by_card_id": None}
    if decision != "covered":
        raise CardCoverageContractError(
            "invalid_coverage_decision", "decision must be novel or covered"
        )
    if set(payload) != {"decision", "covered_by_card_id"}:
        raise CardCoverageContractError(
            "invalid_covered_shape",
            "covered output requires exactly decision and covered_by_card_id",
        )
    covered_by = str(payload.get("covered_by_card_id") or "").strip()
    if not covered_by or covered_by not in comparison_card_ids:
        raise CardCoverageContractError(
            "unknown_covered_by_card",
            "covered_by_card_id must name one supplied comparison card",
        )
    return {"decision": "covered", "covered_by_card_id": covered_by}


__all__ = [
    "COVERAGE_INSTRUCTIONS",
    "COVERAGE_PROMPT_VERSION",
    "MAX_COMPARISON_CARDS",
    "CardCoverageContractError",
    "build_coverage_prompt",
    "validate_coverage_result",
]
