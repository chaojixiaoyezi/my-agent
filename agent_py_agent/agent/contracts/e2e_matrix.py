
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class E2EMatrixCase:
    case_id: str
    execution_mode: str
    goal: str
    acceptance: list[str]
    contracts: list[str] = field(default_factory=list)
    notes: str = ""


REAL_E2E_MATRIX: tuple[E2EMatrixCase, ...] = (
    E2EMatrixCase(
        case_id="windows_chinese_path_write",
        execution_mode="deterministic",
        goal="在包含中文和空格的 Windows 风格路径下写入并读取文件。",
        acceptance=["路径被当作结构化字段处理", "写入和读取不靠自然语言猜路径"],
        contracts=["RunScope", "ErrorTaxonomy"],
    ),
    E2EMatrixCase(
        case_id="large_tool_output_artifact",
        execution_mode="deterministic",
        goal="读取大工具输出并外置为 artifact，再通过 read hints 分片恢复。",
        acceptance=["完整输出不进入 prompt", "artifact ref/hash/size 可恢复"],
        contracts=["ArtifactRef", "ToolManifest"],
    ),
    E2EMatrixCase(
        case_id="compact_resume_continue",
        execution_mode="real_model",
        goal="真实模型完成一半任务后 compact，再 resume 接着做。",
        acceptance=["scope matched", "lineage 连续", "resume 后不重做已完成工作"],
        contracts=["ContextBundle", "StateMachine"],
    ),
    E2EMatrixCase(
        case_id="tool_failure_taxonomy",
        execution_mode="deterministic",
        goal="制造路径、权限、工具不可用和模型上游失败，检查错误分类和恢复建议。",
        acceptance=["错误代码稳定", "每类错误有推荐动作", "不会被混成 unknown"],
        contracts=["ErrorTaxonomy"],
    ),
    E2EMatrixCase(
        case_id="acceptance_failed_then_repair",
        execution_mode="real_model",
        goal="先让验收失败，再让 agent 按 findings 修复并重新验收。",
        acceptance=["ACCEPTANCE_FAILED 被结构化记录", "repair 后进入 VERIFIED"],
        contracts=["AcceptanceContract", "StateMachine"],
    ),
    E2EMatrixCase(
        case_id="long_task_interrupted_recovery",
        execution_mode="real_model",
        goal="长任务中断后从 checkpoint/summary/continue packet 恢复。",
        acceptance=["恢复后下一步正确", "不会重读大正文", "不会无限重试"],
        contracts=["StateMachine", "Idempotency"],
    ),
    E2EMatrixCase(
        case_id="single_agent_full_task",
        execution_mode="real_model",
        goal="单主代理完成真实商业页面或文件处理任务，不派子代理。",
        acceptance=["产物完整", "自检记录清楚", "用户可直接查看成果"],
        contracts=["RunScope", "ToolManifest", "AcceptanceContract"],
    ),
    E2EMatrixCase(
        case_id="subagent_reuses_main_kernel",
        execution_mode="real_model",
        goal="子代理复用主代理 kernel，只替换 owner、记忆和任务目录边界。",
        acceptance=["子代理不读主长期 memory", "子代理工具能力合同一致", "父级只读 refs/summary"],
        contracts=["OwnerModel", "RunScope", "ToolManifest"],
    ),
)


def required_matrix_ids() -> set[str]:
    return {
        "windows_chinese_path_write",
        "large_tool_output_artifact",
        "compact_resume_continue",
        "tool_failure_taxonomy",
        "acceptance_failed_then_repair",
        "long_task_interrupted_recovery",
        "single_agent_full_task",
        "subagent_reuses_main_kernel",
    }


__all__ = ["E2EMatrixCase", "REAL_E2E_MATRIX", "required_matrix_ids"]
