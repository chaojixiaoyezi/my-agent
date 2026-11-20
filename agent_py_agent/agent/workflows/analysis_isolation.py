"""沿原 Agent 节点能力解析工作流的只读执行档。"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Any

READ_ONLY_AGENT_TASK_PROFILE = 'read_only'
AGENT_TASK_PROFILE_KEY = 'workflow_execution_profile'


@dataclass(frozen=True)
class ReadOnlyAgentTaskProfile:
    owner_id: str
    space_id: str
    workflow_run_id: str
    step_id: str
    attempt_id: str
    deadline_at: float
    token_budget: int | None = None
    effect_policy: str = READ_ONLY_AGENT_TASK_PROFILE

    def __post_init__(self):
        if (self.effect_policy != READ_ONLY_AGENT_TASK_PROFILE
                or not all(isinstance(value, str) and value for value in
                           (self.owner_id, self.space_id, self.workflow_run_id, self.step_id, self.attempt_id))
                or type(self.deadline_at) not in (float, int) or not math.isfinite(self.deadline_at)
                or self.deadline_at <= 0
                or (self.token_budget is not None and (type(self.token_budget) is not int or self.token_budget <= 0))):
            raise ValueError('WORKFLOW_AGENT_CONFIG_INVALID')

    def to_record(self):
        return asdict(self)

    def remaining_seconds(self):
        return max(0.0, self.deadline_at - time.time())


class WorkflowIsolationUnavailable(RuntimeError):
    """在任何模型/工具执行前拒绝不可兑现的隔离配置。"""
    error_code = 'WORKFLOW_ISOLATION_UNAVAILABLE'
    effect_outcome = 'not_started'

    def __init__(self):
        self.details = {
            'requested': 'lightweight_subagent', 'available_mode': 'stateless_skill',
            'reason': 'bounded_readonly_executor_unavailable', 'effect_outcome': 'not_started',
            'next_action': 'use_native_tool_call_steps_then_explicit_stateless_skill',
            'retry_same_configuration': False,
        }
        super().__init__('WORKFLOW_ISOLATION_UNAVAILABLE：尚无具备只读效果核验与真实期限的轻量执行器，'
            '本步骤未执行模型或工具；请先用原生 tool_call 步骤取数，再显式选择 stateless_skill 研判。')


def resolve_analysis_isolation(run_params: Any, *, step_config: dict[str, Any] | None = None,
                               isolation_executor: Any = None) -> dict[str, Any]:
    config = step_config if isinstance(step_config, dict) else {}
    value = (run_params.get('analysis_isolation') if isinstance(run_params, dict)
             else getattr(run_params, 'analysis_isolation', None))
    value = value if value is not None else config.get('analysis_isolation')
    if value == 'lightweight_subagent':
        from .agent_steps import WorkflowAgentSteps

        ephemeral = (run_params.get('ephemeral_debug') if isinstance(run_params, dict)
                     else getattr(run_params, 'ephemeral_debug', False))
        if (not ephemeral and isinstance(isolation_executor, WorkflowAgentSteps)
                and isolation_executor.supports_read_only_profile()):
            return {'requested': value, 'mode': 'agent_task', 'profile': READ_ONLY_AGENT_TASK_PROFILE, 'note': ''}
        raise WorkflowIsolationUnavailable()
    return {'requested': 'stateless_skill', 'mode': 'stateless_skill', 'note': ''}
