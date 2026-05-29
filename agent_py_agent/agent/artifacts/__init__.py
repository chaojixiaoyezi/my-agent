# LLM: Artifact package exports registry and shell-protection primitives.
# 模块用途: 给工具、closeout 和 tree 统一导出产物登记与 shell 保护入口。

"""Unified artifact registry for user-visible deliverables."""

from .registry import (
    ArtifactGroupRegistration,
    ArtifactRegistration,
    ArtifactRegistryRecord,
    latest_artifact_records,
    register_artifact,
    register_artifact_group,
    registry_path,
    resolve_artifact_record,
)
from .shell_protection import (
    ShellArtifactSnapshot,
    reconcile_shell_artifacts,
    shell_artifact_protection_note,
    snapshot_ready_artifacts,
)

__all__ = [
    "ArtifactRegistration",
    "ArtifactGroupRegistration",
    "ArtifactRegistryRecord",
    "ShellArtifactSnapshot",
    "latest_artifact_records",
    "reconcile_shell_artifacts",
    "register_artifact",
    "register_artifact_group",
    "registry_path",
    "resolve_artifact_record",
    "shell_artifact_protection_note",
    "snapshot_ready_artifacts",
]
