# LLM: Tool-loop repair counters stay isolated from response-decision control flow.
# 模块用途: 保存工具循环纠偏计数和不可变递增函数，避免主决策文件继续膨胀。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


# LLM: ToolLoopRepairCounters keeps repair bookkeeping out of ToolLoopService positional params.
# 类用途: 保存工具循环里的纠偏计数，避免每新增一种修复都扩散方法签名。
@dataclass(frozen=True)
class ToolLoopRepairCounters:
    __test__: ClassVar[bool] = False

    reserved_record_repairs: int = 0
    local_progress_redirects: int = 0
    exploration_fuse_redirects: int = 0
    unresolved_runtime_issue_redirects: int = 0


# LLM: _inc_reserved returns a new counters bundle after fake-record repair.
# 函数用途: 增加 reserved record 纠偏计数，保持 dataclass 不可变。
def _inc_reserved(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs + 1,
        local_progress_redirects=counters.local_progress_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
    )


# LLM: _inc_local_progress returns a new immutable counters bundle after one local-progress redirect.
# 函数用途: 累加“回到本地推进”的纠偏次数，避免模型连续忽略后无限继续。
def _inc_local_progress(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        local_progress_redirects=counters.local_progress_redirects + 1,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
        unresolved_runtime_issue_redirects=counters.unresolved_runtime_issue_redirects,
    )


# LLM: _inc_exploration_fuse returns a new immutable counters bundle after one exploration redirect.
# 函数用途: 增加探索空转纠偏次数，连续忽略后进入确定性阻断。
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
