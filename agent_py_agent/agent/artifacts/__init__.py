
"""Unified artifact registry for user-visible deliverables."""

from .registry import (
    ArtifactGroupRegistration,
    ArtifactRegistration,
    ArtifactRegistryLookupReport,
    ArtifactRegistryReadReport,
    ArtifactRegistryRecord,
    latest_artifact_records,
    latest_artifact_records_report,
    register_artifact,
    register_artifact_group,
    registry_path,
    resolve_artifact_record,
    resolve_artifact_record_report,
)
from .shell_protection import (
    ShellArtifactSnapshot,
    reconcile_shell_artifacts,
    resolve_shell_artifact_backup,
    settle_shell_artifact_operation,
    shell_artifact_protection_note,
    snapshot_ready_artifacts,
)

__all__ = [
    "ArtifactRegistration",
    "ArtifactGroupRegistration",
    "ArtifactRegistryLookupReport",
    "ArtifactRegistryRecord",
    "ArtifactRegistryReadReport",
    "ShellArtifactSnapshot",
    "latest_artifact_records",
    "latest_artifact_records_report",
    "reconcile_shell_artifacts",
    "resolve_shell_artifact_backup",
    "settle_shell_artifact_operation",
    "register_artifact",
    "register_artifact_group",
    "registry_path",
    "resolve_artifact_record",
    "resolve_artifact_record_report",
    "shell_artifact_protection_note",
    "snapshot_ready_artifacts",
]
