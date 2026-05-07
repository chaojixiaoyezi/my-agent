# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""build capability-gap, blocker, test, artifact, and patch findings.

新手说明:
检查是否还有未关闭的能力请求/缺口、output.json 里的 blocker 和测试失败、
artifact 路径是否真实存在、以及 patch 的状态和审核情况。
"""

from collections.abc import Callable

from ..models import SubAgentTask
from ..reports import AcceptanceReviewFinding
from .artifacts import _build_test_and_artifact_findings
from .capabilities import _build_capability_and_blocker_findings
from .patches import _build_patch_findings


# LLM: _build_output_and_capability_findings 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建output能力findings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_output_and_capability_findings(
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
    artifact_exists_fn: Callable[[str], bool],
) -> list[AcceptanceReviewFinding]:

    findings: list[AcceptanceReviewFinding] = []
    findings.extend(_build_capability_and_blocker_findings(task, output, created_at))
    findings.extend(_build_test_and_artifact_findings(task, output, created_at, artifact_exists_fn))
    findings.extend(_build_patch_findings(task, output, created_at))
    return findings
