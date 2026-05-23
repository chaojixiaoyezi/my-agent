# LLM: Real-task suite is a compatibility facade over the unified main-agent task suite.
# 模块用途: 保留 real_task 的公开名称和 schema，真实计划逻辑统一走 main_agent_task_suite。

from __future__ import annotations

from .main_agent_task_suite import (
    MainAgentTaskArtifact as MainAgentRealTaskArtifact,
)
from .main_agent_task_suite import (
    MainAgentTaskCase as MainAgentRealTaskCase,
)
from .main_agent_task_suite import (
    MainAgentTaskCasePlan as MainAgentRealTaskCasePlan,
)
from .main_agent_task_suite import (
    MainAgentTaskCasePlanRequest as MainAgentRealTaskCasePlanRequest,
)
from .main_agent_task_suite import (
    MainAgentTaskSuiteReport as MainAgentRealTaskSuiteReport,
)
from .main_agent_task_suite import (
    MainAgentTaskSuiteRequest as MainAgentRealTaskSuiteRequest,
)
from .main_agent_task_suite import (
    plan_main_agent_task_suite,
)

SCHEMA_VERSION = "main-agent-real-task-suite.v1"
ACCEPTANCE_SCHEMA_VERSION = "main-agent-real-task-acceptance.v1"
ARTIFACT_SCHEMA_VERSION = "main-agent-real-task-artifacts.v1"


# LLM: plan_main_agent_real_task_suite only selects the real-track schema/root.
# 函数用途: 复用统一 suite writer，同时保持旧 CLI/tests 读取的 real_task 引用和版本。
def plan_main_agent_real_task_suite(
    request: MainAgentRealTaskSuiteRequest,
) -> MainAgentRealTaskSuiteReport:
    from .main_agent_real_task_suite_cases import default_main_agent_real_task_cases

    return plan_main_agent_task_suite(
        request,
        schema_version=SCHEMA_VERSION,
        acceptance_schema_version=ACCEPTANCE_SCHEMA_VERSION,
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        root_name="main_agent_real_task_suite",
        cases=default_main_agent_real_task_cases(),
    )


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ACCEPTANCE_SCHEMA_VERSION",
    "MainAgentRealTaskArtifact",
    "MainAgentRealTaskCase",
    "MainAgentRealTaskCasePlan",
    "MainAgentRealTaskCasePlanRequest",
    "MainAgentRealTaskSuiteReport",
    "MainAgentRealTaskSuiteRequest",
    "SCHEMA_VERSION",
    "plan_main_agent_real_task_suite",
]
