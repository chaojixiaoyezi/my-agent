# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Acceptance and patch review report models."""

from dataclasses import dataclass, field


# LLM: AcceptanceReviewFinding 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存验收审查finding字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class AcceptanceReviewFinding:
    """涓€娆￠獙鏀舵鏌ヤ腑鐨勫崟椤圭粨璁恒€?"""

    name: str
    ok: bool
    severity: str
    message: str
    evidence_path: str = ""
    created_at: float = 0.0


# LLM: AcceptanceReviewRecord 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存验收审查记录字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class AcceptanceReviewRecord:
    """鍗曚釜瀛愪唬鐞嗚繍琛岀殑楠屾敹璁板綍銆?"""

    id: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    decision: str
    message: str
    before_status: str
    after_status: str
    before_verification_status: str
    after_verification_status: str
    reviewer: str = ""
    note: str = ""
    evidence_count: int = 0
    test_count: int = 0
    artifact_count: int = 0
    findings: list[AcceptanceReviewFinding] = field(default_factory=list)
    # LLM: layered acceptance separates worker claims, evidence facts, and parent conclusions.
    worker_claims: list[str] = field(default_factory=list)
    evidence_facts: list[str] = field(default_factory=list)
    parent_conclusions: list[str] = field(default_factory=list)
    verifier_checks: list[AcceptanceReviewFinding] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


# LLM: AcceptanceReviewReport 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存验收审查报告字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class AcceptanceReviewReport:
    """鐖朵唬鐞嗛獙鏀舵姤鍛娿€?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[AcceptanceReviewRecord]


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
