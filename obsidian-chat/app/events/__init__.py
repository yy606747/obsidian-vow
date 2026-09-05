"""Evidence and event contracts for backend foundation work."""

from .lifecycle import DEFAULT_EVIDENCE_LIFECYCLE_POLICY, EvidenceLifecyclePolicy
from .schemas import EvidenceRecord, EvidenceSnapshot
from .service import EvidenceLedger, evidence_ledger
from .side_effects import (
    SideEffectGateway,
    SideEffectKind,
    SideEffectPlan,
    SideEffectRequest,
    SideEffectStatus,
    side_effect_gateway,
)


__all__ = [
    "DEFAULT_EVIDENCE_LIFECYCLE_POLICY",
    "EvidenceLifecyclePolicy",
    "EvidenceLedger",
    "EvidenceRecord",
    "EvidenceSnapshot",
    "SideEffectGateway",
    "SideEffectKind",
    "SideEffectPlan",
    "SideEffectRequest",
    "SideEffectStatus",
    "evidence_ledger",
    "side_effect_gateway",
]
