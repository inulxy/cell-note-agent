"""CellNote deterministic domain core.

Pi coding agent is the conversational harness.  This package owns the
scientific state, evidence, policy, validation, and reproducible artifacts.
"""

from .models import (
    ArtifactRole,
    ClaimRule,
    ClaimStatus,
    DatasetState,
    EvidenceStrength,
)

__all__ = [
    "ArtifactRole",
    "ClaimRule",
    "ClaimStatus",
    "DatasetState",
    "EvidenceStrength",
]

__version__ = "0.2.0"

