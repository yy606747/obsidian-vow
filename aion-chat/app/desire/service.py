"""Desire-layer validation and root initialization."""

from . import repository
from .prompt import DESIRE_MAX_CHARS, normalize_desire_prompt_content

DESIRE_ROOT_ID = "desire_root"
DESIRE_ROOT_ORIGIN_REQUEST_ID = "root"
DESIRE_ROOT_CHANGE_NOTE = "(初始空版本，系统建表时创建)"
ROOT_WRITER_MODEL = "unknown"
ROOT_PROMPT_VERSION = "legacy"


class DesireRootConflictError(RuntimeError):
    """The stable desire root exists but no longer matches its contract."""


def validate_desire_content(content: object) -> str:
    return normalize_desire_prompt_content(content)


async def ensure_desire_root_in_tx(
    db,
    *,
    working_model_id: str,
    created_at: float,
) -> str:
    existing = await repository.get_version(db, DESIRE_ROOT_ID)
    if existing is None:
        if await repository.count_versions(db) != 0:
            raise DesireRootConflictError(
                "desire_versions contains rows but the stable root is missing"
            )
        await repository.insert_version(
            db,
            version_id=DESIRE_ROOT_ID,
            previous_version_id=None,
            content="",
            change_note=DESIRE_ROOT_CHANGE_NOTE,
            origin_request_id=DESIRE_ROOT_ORIGIN_REQUEST_ID,
            working_model_id=working_model_id,
            writer_model=ROOT_WRITER_MODEL,
            prompt_version=ROOT_PROMPT_VERSION,
            created_at=created_at,
        )
        return "created"

    expected = {
        "previous_version_id": None,
        "content": "",
        "change_note": DESIRE_ROOT_CHANGE_NOTE,
        "origin_request_id": DESIRE_ROOT_ORIGIN_REQUEST_ID,
        "working_model_id": working_model_id,
    }
    mismatches = [key for key, value in expected.items() if existing.get(key) != value]
    if mismatches:
        raise DesireRootConflictError(
            "existing desire root violates migration contract: "
            + ", ".join(mismatches)
        )
    return "already_migrated"
