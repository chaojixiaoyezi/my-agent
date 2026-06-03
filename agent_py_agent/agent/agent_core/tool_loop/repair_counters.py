
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class ToolLoopRepairCounters:
    __test__: ClassVar[bool] = False

    reserved_record_repairs: int = 0
    local_progress_redirects: int = 0
    exploration_fuse_redirects: int = 0
    unresolved_runtime_issue_redirects: int = 0


def _inc_reserved(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs + 1,
        local_progress_redirects=counters.local_progress_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
    )


def _inc_local_progress(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        local_progress_redirects=counters.local_progress_redirects + 1,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
    )


def _inc_exploration_fuse(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        local_progress_redirects=counters.local_progress_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects + 1,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
    )


def _inc_unresolved_runtime_issue(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        local_progress_redirects=counters.local_progress_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects + 1,
    )


__all__ = [
    "ToolLoopRepairCounters",
    "_inc_exploration_fuse",
    "_inc_local_progress",
    "_inc_reserved",
    "_inc_unresolved_runtime_issue",
]
