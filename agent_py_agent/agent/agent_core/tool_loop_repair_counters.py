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
    open_write_session_repairs: int = 0
    bootstrap_materialization_redirects: int = 0
    local_progress_redirects: int = 0
    delivery_repair_redirects: int = 0
    exploration_fuse_redirects: int = 0


# LLM: _inc_reserved returns a new counters bundle after fake-record repair.
# 函数用途: 增加 reserved record 纠偏计数，保持 dataclass 不可变。
def _inc_reserved(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs + 1,
        open_write_session_repairs=counters.open_write_session_repairs,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects,
        local_progress_redirects=counters.local_progress_redirects,
        delivery_repair_redirects=counters.delivery_repair_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
    )


# LLM: _inc_open_session returns a new counters bundle after open-session repair.
# 函数用途: 增加分块写入 session 纠偏计数，保持 dataclass 不可变。
def _inc_open_session(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs + 1,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects,
        local_progress_redirects=counters.local_progress_redirects,
        delivery_repair_redirects=counters.delivery_repair_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
    )


# LLM: _inc_local_progress returns a new immutable counters bundle after one local-progress redirect.
# 函数用途: 累加“回到本地推进”的纠偏次数，避免模型连续忽略后无限继续。
def _inc_local_progress(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects,
        local_progress_redirects=counters.local_progress_redirects + 1,
        delivery_repair_redirects=counters.delivery_repair_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
    )


# LLM: _inc_bootstrap_materialization returns a new immutable counters bundle after one startup-materialization redirect.
# 函数用途: 累加 bootstrap 开工纠偏计数，避免“先物化一个目标”的提醒无限重复。
def _inc_bootstrap_materialization(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects + 1,
        local_progress_redirects=counters.local_progress_redirects,
        delivery_repair_redirects=counters.delivery_repair_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
    )


# LLM: _inc_delivery_repair returns a new immutable counters bundle after one staged-delivery redirect.
# 函数用途: 增加 delivery repair 纠偏计数，避免该类状态无限提醒不收口。
def _inc_delivery_repair(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects,
        local_progress_redirects=counters.local_progress_redirects,
        delivery_repair_redirects=counters.delivery_repair_redirects + 1,
        exploration_fuse_redirects=counters.exploration_fuse_redirects,
    )


# LLM: _inc_exploration_fuse returns a new immutable counters bundle after one exploration redirect.
# 函数用途: 增加探索空转纠偏次数，连续忽略后进入确定性阻断。
def _inc_exploration_fuse(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs,
        bootstrap_materialization_redirects=counters.bootstrap_materialization_redirects,
        local_progress_redirects=counters.local_progress_redirects,
        delivery_repair_redirects=counters.delivery_repair_redirects,
        exploration_fuse_redirects=counters.exploration_fuse_redirects + 1,
    )


__all__ = [
    "ToolLoopRepairCounters",
    "_inc_bootstrap_materialization",
    "_inc_delivery_repair",
    "_inc_exploration_fuse",
    "_inc_local_progress",
    "_inc_open_session",
    "_inc_reserved",
]
