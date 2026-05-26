# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Patch review report models."""

from dataclasses import dataclass, field


# LLM: PatchReviewRecord 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存补丁审查记录字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class PatchReviewRecord:
    """runner patch 杈撳嚭鐨勫鏍歌褰曘€?"""

    id: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    decision: str
    message: str
    patch_count: int
    approved_count: int = 0
    blocked_count: int = 0
    reviewer: str = ""
    note: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    patches: list[dict[str, object]] = field(default_factory=list)
    created_at: float = 0.0


# LLM: PatchReviewReport 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存补丁审查报告字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class PatchReviewReport:
    """鎵归噺 patch 瀹℃牳鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[PatchReviewRecord]


# LLM: PatchApplyRecord 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存补丁应用记录字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class PatchApplyRecord:
    """鐙珛 patch apply 瀹℃牳閾剧殑涓€娆¤褰曘€?"""

    id: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    decision: str
    message: str
    patch_count: int
    applied_count: int = 0
    blocked_count: int = 0
    rollback_performed: bool = False
    applier: str = ""
    note: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    test_results: list[dict[str, object]] = field(default_factory=list)
    patches: list[dict[str, object]] = field(default_factory=list)
    created_at: float = 0.0


# LLM: PatchApplyReport 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存补丁应用报告字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class PatchApplyReport:
    """鎵归噺 patch apply 鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[PatchApplyRecord]
