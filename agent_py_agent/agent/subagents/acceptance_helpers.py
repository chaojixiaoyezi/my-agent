# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""helper functions for building acceptance review findings.

This module is a backward-compatibility stub. Actual implementations have been
moved to the acceptance_helpers/ subdirectory.

给人看的解释：
这些函数各自负责构建一组验收检查项，避免单个方法过长。
主 mixin 只需要调用这些函数并拼接结果。
"""

# 这里保持按子模块分组导出，兼容入口只负责转发，不再承载真实实现。
from agent_py_agent.agent.subagents.acceptance_helpers.artifacts import (
    _build_test_and_artifact_findings,
    _check_artifact_exists,
)
from agent_py_agent.agent.subagents.acceptance_helpers.capabilities import (
    _build_capability_and_blocker_findings,
)
from agent_py_agent.agent.subagents.acceptance_helpers.evidence import _build_evidence_findings
from agent_py_agent.agent.subagents.acceptance_helpers.output import (
    _build_output_and_capability_findings,
)
from agent_py_agent.agent.subagents.acceptance_helpers.patches import _build_patch_findings
from agent_py_agent.agent.subagents.acceptance_helpers.readiness import _build_readiness_findings

__all__ = [
    "_build_readiness_findings",
    "_build_evidence_findings",
    "_build_capability_and_blocker_findings",
    "_build_test_and_artifact_findings",
    "_build_patch_findings",
    "_build_output_and_capability_findings",
    "_check_artifact_exists",
]
