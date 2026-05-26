# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""quality contract dataclasses shared by subagent task and workflow state.

给人看的解释：
这里只放父会话传给子代理的质量标准和上下文清单，避免核心 models.py 继续膨胀。
"""

from dataclasses import dataclass, field


# LLM: QualityContract 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存qualitycontract字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class QualityContract:
    """Structured quality bar passed from the parent session to a worker."""

    user_visible_goal: str = ""
    benchmark_sample: str = ""
    quality_bar: str = ""
    failure_conditions: list[str] = field(default_factory=list)
    forbidden_delivery: list[str] = field(default_factory=list)
    must_check: list[str] = field(default_factory=list)
    sampling_plan: list[str] = field(default_factory=list)
    evidence_required: list[str] = field(default_factory=list)
    risk_report_required: str = ""
    allowed_degradation: list[str] = field(default_factory=list)
    final_judge: str = "parent_final_gate"
    cannot_self_accept: bool = True
    parent_final_gate: bool = True


# LLM: ContextManifest 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存上下文manifest字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass
class ContextManifest:
    """Manifest describing the focused context packs given to a worker."""

    core_pack_version: str = "subagent-quality-contract-v1"
    task_pack_refs: list[str] = field(default_factory=list)
    role_pack: str = ""
    required_read_paths: list[str] = field(default_factory=list)
    # LLM: hint_read_paths are advisory refs for the runner prompt, not required inputs.
    hint_read_paths: list[str] = field(default_factory=list)
    quality_contract_ref: str = ""
    omitted_context: list[str] = field(default_factory=list)
    token_budget: int = 0
