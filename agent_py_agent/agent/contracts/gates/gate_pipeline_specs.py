# LLM: Gate pipeline specs define mandatory runtime gate orders without running validators.
# 模块用途: 保存 phase/action 到 gate 序列的机器合同，供 GatePipeline 执行时按结构化顺序查表。

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_HIGH_RISK_PHASES = frozenset(
    {
        "tool_execution",
        "delivery_closeout",
        "final_closeout",
        "recovery",
        "recovery_replay",
        "runtime_audit",
        "state_transition",
    }
)


# LLM: GatePipelineStep is one required gate in a phase/action pipeline.
# 类用途: 记录 gate 名、前置依赖和是否可能触发副作用，供 pipeline 做顺序和短路判断。
@dataclass(frozen=True)
class GatePipelineStep:
    gate: str
    depends_on: tuple[str, ...] = ()
    side_effect: bool = False

    # LLM: __post_init__ validates one step using machine fields only.
    # 函数用途: 规范 gate 和 depends_on 字段，避免空 gate 或脏依赖进入运行时门流水线。
    def __post_init__(self) -> None:
        object.__setattr__(self, "gate", required_key(self.gate, field_name="gate"))
        object.__setattr__(self, "depends_on", tuple(key(item) for item in self.depends_on if key(item)))


# LLM: GatePipelineSpec maps one runtime phase/action pair to a deterministic gate sequence.
# 类用途: 让调用方用机器字段声明 gate 顺序、依赖和短路策略，而不是依赖提示词解释。
@dataclass(frozen=True)
class GatePipelineSpec:
    phase: str
    action: str
    steps: tuple[GatePipelineStep | str, ...] = ()
    short_circuit: bool = True
    high_risk: bool = True

    # LLM: __post_init__ freezes a deterministic gate sequence for one phase/action.
    # 函数用途: 把字符串 gate 转成 GatePipelineStep，并校验依赖必须出现在更早步骤。
    def __post_init__(self) -> None:
        steps = tuple(pipeline_step(item) for item in self.steps)
        validate_steps(steps)
        object.__setattr__(self, "phase", required_key(self.phase, field_name="phase"))
        object.__setattr__(self, "action", required_key(self.action, field_name="action"))
        object.__setattr__(self, "steps", steps)


# LLM: pipeline_step keeps default specs compact while preserving typed execution steps.
# 函数用途: 把字符串 gate 规范为 GatePipelineStep，已经是 step 的对象原样返回。
def pipeline_step(item: GatePipelineStep | str) -> GatePipelineStep:
    return item if isinstance(item, GatePipelineStep) else GatePipelineStep(str(item))


# LLM: validate_steps rejects duplicate or unsatisfied dependencies before runtime.
# 函数用途: 在注册 spec 时发现配置错误，避免执行中靠自然语言或后置异常补救。
def validate_steps(steps: Iterable[GatePipelineStep]) -> None:
    seen: set[str] = set()
    for step in steps:
        if step.gate in seen:
            raise ValueError(f"duplicate gate in pipeline spec: {step.gate}")
        missing = [gate for gate in step.depends_on if gate not in seen]
        if missing:
            raise ValueError(f"gate {step.gate} depends on gates that must appear earlier: {', '.join(missing)}")
        seen.add(step.gate)


# LLM: required_key rejects empty pipeline identity fields.
# 函数用途: 规范 phase/action/gate 这类机器键，空值直接失败。
def required_key(value: object, *, field_name: str) -> str:
    item = key(value)
    if not item:
        raise ValueError(f"gate pipeline {field_name} is required")
    return item


# LLM: key normalizes stable pipeline identity values.
# 函数用途: 将 phase/action/gate 等结构字段转成去空白字符串。
def key(value: object) -> str:
    return str(value or "").strip()


DEFAULT_GATE_PIPELINE_SPECS = (
    GatePipelineSpec(
        phase="tool_execution",
        action="read_only",
        steps=("tool_call", "tool_manifest", "path_url_command", "tool_rate_limit"),
    ),
    GatePipelineSpec(
        phase="tool_execution",
        action="mutating",
        steps=(
            GatePipelineStep("tool_call"),
            GatePipelineStep("tool_manifest", depends_on=("tool_call",)),
            GatePipelineStep("path_url_command", depends_on=("tool_call", "tool_manifest")),
            GatePipelineStep("tool_rate_limit", depends_on=("path_url_command",)),
            GatePipelineStep("tool_effect", depends_on=("tool_rate_limit",), side_effect=True),
        ),
    ),
    GatePipelineSpec(
        phase="tool_execution",
        action="dangerous",
        steps=(
            GatePipelineStep("tool_call"),
            GatePipelineStep("tool_manifest", depends_on=("tool_call",)),
            GatePipelineStep("path_url_command", depends_on=("tool_call", "tool_manifest")),
            GatePipelineStep("tool_rate_limit", depends_on=("path_url_command",)),
            GatePipelineStep("tool_effect", depends_on=("tool_rate_limit",), side_effect=True),
        ),
    ),
    GatePipelineSpec(
        phase="delivery_closeout",
        action="submit",
        steps=(
            GatePipelineStep("delivery_closeout"),
            GatePipelineStep("delivery_quality", depends_on=("delivery_closeout",)),
            GatePipelineStep("acceptance_closeout", depends_on=("delivery_quality",)),
            GatePipelineStep("final_closeout", depends_on=("acceptance_closeout",)),
        ),
    ),
    GatePipelineSpec(
        phase="recovery",
        action="replay",
        steps=(
            GatePipelineStep("recovery_replay"),
            GatePipelineStep("runtime_audit", depends_on=("recovery_replay",)),
        ),
    ),
)


__all__ = [
    "DEFAULT_GATE_PIPELINE_SPECS",
    "DEFAULT_HIGH_RISK_PHASES",
    "GatePipelineSpec",
    "GatePipelineStep",
    "key",
    "required_key",
]
