"""研判隔离的真实能力边界；当前仅支持无状态、无工具的模型研判。

需要取数时使用前置 tool_call 步骤，经原 native dispatch 和权限门读取，再把输出传入
stateless_skill。受限轻量执行器尚未实现，任意 callable 不构成隔离或限时能力证明。
"""
from __future__ import annotations

from typing import Any


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
        # 不因对象可调用或名字恰好不在黑名单就授予执行、记忆隔离或deadline保证。
        raise WorkflowIsolationUnavailable()
    return {'requested': 'stateless_skill', 'mode': 'stateless_skill', 'note': ''}
