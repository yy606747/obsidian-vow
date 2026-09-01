import pytest

from app.memory_v3.card_coverage import (
    COVERAGE_INSTRUCTIONS,
    COVERAGE_PROMPT_VERSION,
    CardCoverageContractError,
    build_coverage_prompt,
    validate_coverage_result,
)


def _card(
    card_id: str,
    chunk_id: str,
    content: str,
    kind: str = "relational_reading",
) -> dict:
    return {
        "card_id": card_id,
        "source_chunk_id": chunk_id,
        "content": content,
        "kind": kind,
    }


def test_coverage_prompt_uses_strict_non_verbose_definition():
    prompt = build_coverage_prompt(
        _card("candidate", "chunk-2", "她可能需要有件事在等着她。"),
        [_card("old", "chunk-1", "那次我读到，她需要一个明确的下一步。")],
    )

    assert "shared_moment" in COVERAGE_INSTRUCTIONS
    assert "relational_reading" in COVERAGE_INSTRUCTIONS
    assert "成立条件、例外或矛盾" in COVERAGE_INSTRUCTIONS
    assert "kind 不同不自动互相覆盖" in COVERAGE_INSTRUCTIONS
    assert "发生在另一个时间或场合的经历不算同一件" in COVERAGE_INSTRUCTIONS
    assert "拿不准时判 novel" in COVERAGE_INSTRUCTIONS
    assert COVERAGE_PROMPT_VERSION == "relational-card-coverage-v3"
    assert "earlier_accepted_v4_cards" in prompt
    assert "earlier_accepted_v3_cards" not in prompt
    assert "candidate" in prompt
    assert "old" in prompt


def test_coverage_prompt_rejects_current_chunk_predecessor():
    with pytest.raises(CardCoverageContractError) as caught:
        build_coverage_prompt(
            _card("candidate", "same-chunk", "新的写法"),
            [_card("old-v1", "same-chunk", "旧的写法")],
        )

    assert caught.value.code == "same_chunk_comparison"


def test_coverage_prompt_requires_v4_kind():
    with pytest.raises(CardCoverageContractError) as caught:
        build_coverage_prompt(
            {
                "card_id": "candidate",
                "source_chunk_id": "chunk-2",
                "content": "候选",
            },
            [],
        )
    assert caught.value.code == "invalid_card_kind"


def test_coverage_result_must_reference_a_supplied_card():
    assert validate_coverage_result(
        {"decision": "novel"}, comparison_card_ids={"old"}
    ) == {"decision": "novel", "covered_by_card_id": None}
    assert validate_coverage_result(
        {"decision": "covered", "covered_by_card_id": "old"},
        comparison_card_ids={"old"},
    ) == {"decision": "covered", "covered_by_card_id": "old"}

    with pytest.raises(CardCoverageContractError) as caught:
        validate_coverage_result(
            {"decision": "covered", "covered_by_card_id": "not-supplied"},
            comparison_card_ids={"old"},
        )
    assert caught.value.code == "unknown_covered_by_card"


def test_already_covered_is_not_a_writer_abstain_reason():
    from app.memory_v3.relational_cards import ABSTAIN_REASONS

    assert "already_covered" not in ABSTAIN_REASONS
