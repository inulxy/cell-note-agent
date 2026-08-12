"""Deterministic validators for remote acquisition packages."""

from __future__ import annotations

from urllib.parse import urlparse

from .models import ArtifactRole, BundleAudit, FileArtifact


ALLOWED_REMOTE_SCHEMES = {"https", "ftp", "s3", "fixture"}


def validate_artifact(artifact: FileArtifact) -> list[str]:
    errors: list[str] = []
    parsed = urlparse(artifact.source_uri)
    if parsed.scheme not in ALLOWED_REMOTE_SCHEMES:
        errors.append(f"unsupported source scheme: {parsed.scheme or '(none)'}")
    if artifact.size_bytes < 0:
        errors.append("size_bytes must be non-negative")
    if not artifact.artifact_id:
        errors.append("artifact_id is required")
    return errors


def audit_bundle(
    dataset_id: str,
    artifacts: list[FileArtifact],
    required_roles: tuple[ArtifactRole, ...],
) -> BundleAudit:
    present = tuple(sorted({item.role for item in artifacts}, key=lambda item: item.value))
    present_set = set(present)
    missing = tuple(role for role in required_roles if role not in present_set)
    return BundleAudit(
        dataset_id=dataset_id,
        required_roles=required_roles,
        present_roles=present,
        missing_roles=missing,
        usable=not missing,
    )

