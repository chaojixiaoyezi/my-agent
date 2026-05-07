# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""exposes explicit review operations for run-local memory gate candidates.

给人看的解释：
这里不写长期记忆，也不生成正式 skill。它只让父代理把某个候选标记成
approved/rejected/needs_evidence，后续提升必须走另一条显式流程。
"""

from pathlib import Path

from ..memory_archive.memory_gate import (
    list_memory_gate_candidates,
)
from ..memory_archive.memory_gate_export import (
    MemoryGateExportRequest,
    MemoryGateExportResult,
    export_approved_memory_candidates,
    export_approved_skill_sparks,
)
from ..memory_archive.memory_gate_retention import (
    MemoryGateRetentionRequest,
    MemoryGateRetentionResult,
    run_memory_gate_retention,
)
from ..memory_archive.memory_gate_review import (
    MemoryGateReviewRequest,
    MemoryGateReviewResult,
    record_memory_gate_review,
)
from ..memory_archive.memory_gate_verifier import (
    MemoryGateVerifierResult,
    verify_memory_gate_boundary,
)
from .models import SubAgentTask


# LLM: SubAgentMemoryGateMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent记忆闸门混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentMemoryGateMixin:
    # LLM: list_memory_gate_candidates 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询记忆闸门candidates需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def list_memory_gate_candidates(self, run_id: str) -> list[dict[str, object]]:
        """Return current run-local gate candidates, creating the skeleton if needed."""

        task = self._ensure_memory_gate_workspace(run_id)
        return list_memory_gate_candidates(Path(task.agent_run_workspace_dir))

    # LLM: review_memory_gate_candidate 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理审查记忆闸门candidate相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def review_memory_gate_candidate(
        self,
        run_id: str,
        request: MemoryGateReviewRequest,
    ) -> MemoryGateReviewResult:
        """Record a review decision without exporting to long-term memory or skills."""

        task = self._ensure_memory_gate_workspace(run_id)
        # LLM: review writes only gate files/checkpoint refs; promotion remains a separate command.
        return record_memory_gate_review(
            Path(task.agent_run_workspace_dir),
            request,
        )

    # LLM: run_memory_gate_retention 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 推进记忆闸门retention的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def run_memory_gate_retention(
        self,
        run_id: str,
        request: MemoryGateRetentionRequest,
    ) -> MemoryGateRetentionResult:
        """Plan or apply queue retention without deleting audit facts."""

        task = self._ensure_memory_gate_workspace(run_id)
        # LLM: retention may compact the active queue, but candidate/decision audit files stay put.
        return run_memory_gate_retention(Path(task.agent_run_workspace_dir), request)

    # LLM: export_memory_gate_candidates_to_memory 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理export记忆闸门candidatesto记忆相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def export_memory_gate_candidates_to_memory(
        self,
        run_id: str,
        *,
        memory_path: str | Path | None = None,
        request: MemoryGateExportRequest,
    ) -> MemoryGateExportResult:
        """Export approved memory candidates only after explicit review approval."""

        task = self._ensure_memory_gate_workspace(run_id)
        bundled = _export_request_with_paths(request, memory_path=memory_path)
        return export_approved_memory_candidates(Path(task.agent_run_workspace_dir), request=bundled)

    # LLM: export_memory_gate_candidates_to_skill_drafts 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理export记忆闸门candidatestoskilldrafts相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def export_memory_gate_candidates_to_skill_drafts(
        self,
        run_id: str,
        *,
        output_dir: str | Path | None = None,
        request: MemoryGateExportRequest,
    ) -> MemoryGateExportResult:
        """Export approved skill candidates as local drafts, never as installed skills."""

        task = self._ensure_memory_gate_workspace(run_id)
        bundled = _export_request_with_paths(request, output_dir=output_dir)
        return export_approved_skill_sparks(Path(task.agent_run_workspace_dir), request=bundled)

    # LLM: verify_memory_gate_boundary 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理verify记忆闸门boundary相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def verify_memory_gate_boundary(self, run_id: str) -> MemoryGateVerifierResult:
        """Run deterministic checks for no-auto-promotion gate boundaries."""

        task = self._ensure_memory_gate_workspace(run_id)
        return verify_memory_gate_boundary(Path(task.agent_run_workspace_dir))

    # LLM: _ensure_memory_gate_workspace 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 校验记忆闸门workspace需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    def _ensure_memory_gate_workspace(self, run_id: str) -> SubAgentTask:
        task = self.load(run_id)
        if task.agent_run_workspace_dir and Path(task.agent_run_memory_candidates_jsonl).exists():
            return task
        self.save(task)
        return self.load(run_id)


# LLM: _export_request_with_paths 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理export请求路径相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _export_request_with_paths(
    request: MemoryGateExportRequest,
    *,
    memory_path: str | Path | None = None,
    output_dir: str | Path | None = None,
) -> MemoryGateExportRequest:
    # LLM: 旧管理器字段保持兼容，核心导出接口只接收归一后的参数包。
    return MemoryGateExportRequest(
        candidate_id=request.candidate_id,
        reviewer=request.reviewer,
        now=request.now,
        memory_path=memory_path if memory_path is not None else request.memory_path,
        output_dir=output_dir if output_dir is not None else request.output_dir,
    )
