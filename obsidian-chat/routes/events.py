"""Read-only event and evidence diagnostics."""

from __future__ import annotations

from fastapi import APIRouter

from app.events import evidence_ledger


router = APIRouter()


@router.get("/api/events/evidence-summary")
async def get_evidence_summary(max_age_sec: float | None = None):
    return evidence_ledger.stats(max_age_sec=max_age_sec)
