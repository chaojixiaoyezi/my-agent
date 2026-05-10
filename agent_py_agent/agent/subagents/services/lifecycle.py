# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""lifecycle mutation service for subagent task records.

给人看的解释：
这里承接能力请求、能力授权、能力缺口、验收证据和基础状态更新。
SubAgentManager 继续暴露旧方法名，内部逐步改成服务委托。
"""

import time
from dataclasses import dataclass
from typing import Any

from ...memory_routing import load_routes, match_routes, resolve_required_paths
from ..models import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    SubAgentTask,
    VerificationEvidence,
)
from ..utils import _merge_list
from .lifecycle_capability_records import (
    BuildCapabilityGapInput,
    build_capability_gap,
    build_capability_grant,
    build_capability_request,
)


# LLM: RecordCapabilityGrantParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存记录能力grant参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RecordCapabilityGrantParams:
    """Params bundle for record_capability_grant."""

    request_id: str
    # LLM: scoped grant fields mirror CapabilityGrant so service callers never pass loose kwargs.
    grant_type: str = "generic"
    skills: list[str] | None = None
    tools: list[str] | None = None
    mcp_tools: list[str] | None = None
    command_allowlist: list[str] | None = None
    capability_cards: list[dict[str, str]] | None = None
    reason: str = ""
    constraints: dict[str, str] | None = None
    path_scope: list[str] | None = None
    network_scope: list[str] | None = None
    output_budget: dict[str, object] | None = None
    risk_level: str = ""
    expires_after_task: bool = True
    expires_at: float = 0.0
    reserved: dict[str, object] | None = None


# LLM: RecordCapabilityGapParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存记录能力缺口参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RecordCapabilityGapParams:
    """Params bundle for record_capability_gap."""

    missing_capability: str
    why_failed: str
    # LLM: gap scope keeps enough routing evidence for escalation without reading runner output.
    gap_type: str = "generic"
    attempted_skills: list[str] | None = None
    attempted_tools: list[str] | None = None
    needed_outputs: list[str] | None = None
    suggested_skill: str = ""
    suggested_tool: str = ""
    requested_scope: dict[str, object] | None = None
    escalation_chain: list[str] | None = None
    next_record_refs: list[str] | None = None
    reserved: dict[str, object] | None = None


# LLM: RecordCapabilityRequestParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存记录能力请求参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RecordCapabilityRequestParams:
    """Params bundle for record_capability_request."""

    problem: str
    needed_capability: str
    expected_output: str = ""
    # LLM: request scope fields let child agents ask for constrained tools instead of broad permissions.
    capability_type: str = "generic"
    tried: list[str] | None = None
    evidence: list[str] | None = None
    constraints: dict[str, str] | None = None
    requested_tools: list[str] | None = None
    requested_skills: list[str] | None = None
    requested_mcp_tools: list[str] | None = None
    requested_commands: list[str] | None = None
    cwd_scope: list[str] | None = None
    path_scope: list[str] | None = None
    network_scope: list[str] | None = None
    output_budget: dict[str, object] | None = None
    risk_level: str = ""
    fallback_attempted: list[str] | None = None
    escalation_target: str = ""
    reserved: dict[str, object] | None = None


# LLM: RecordEvidenceParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存记录证据参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RecordEvidenceParams:
    """Params bundle for record_evidence."""

    kind: str
    summary: str
    command: str = ""
    path: str = ""
    url: str = ""
    ok: bool = True


# LLM: SetStatusParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存set状态参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class SetStatusParams:
    """Params bundle for set_status."""

    # LLM: 状态变更使用显式参数包，让证据闸门后续能独立扩展。
    run_id: str
    status: str
    result: str = ""
    failure_type: str = ""
    require_evidence: bool = False


# LLM: SubAgentLifecycleService 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装subagent生命周期服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class SubAgentLifecycleService:
    """Mutate lifecycle fields on subagent tasks through the manager facade."""

    # LLM: __init__ 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: record_capability_request 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入能力请求的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def record_capability_request(
        self,
        run_id: str,
        params: RecordCapabilityRequestParams,
    ) -> CapabilityRequest:
        task = self.manager.load(run_id)
        request = build_capability_request(run_id, params)
        task.capability_requests.append(request)
        task.updated_at = time.time()
        self.manager.save(task)
        return request

    # LLM: record_capability_grant 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入能力grant的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def record_capability_grant(
        self,
        run_id: str,
        *,
        params: RecordCapabilityGrantParams,
    ) -> CapabilityGrant:
        task = self.manager.load(run_id)
        grant = build_capability_grant(run_id, params)
        task.capability_grants.append(grant)
        task.allowed_skills = _merge_list(task.allowed_skills, grant.skills)
        task.allowed_tools = _merge_list(task.allowed_tools, grant.tools)
        task.updated_at = time.time()
        self.manager.save(task)
        return grant

    # LLM: record_capability_gap 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入能力缺口的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def record_capability_gap(
        self,
        run_id: str,
        *,
        params: RecordCapabilityGapParams,
    ) -> CapabilityGap:
        task = self.manager.load(run_id)
        injected_rule_paths, memory_routes = self._match_memory_routes(params.missing_capability, params.why_failed, task)
        gap = build_capability_gap(
            BuildCapabilityGapInput(run_id, task.goal, params, memory_routes, injected_rule_paths)
        )
        task.capability_gaps.append(gap)
        if injected_rule_paths:
            task.context_manifest.required_read_paths = _merge_list(
                task.context_manifest.required_read_paths,
                injected_rule_paths,
            )
        task.updated_at = time.time()
        self.manager.save(task)
        return gap

    # LLM: record_evidence 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入证据的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def record_evidence(
        self,
        run_id: str,
        params: RecordEvidenceParams,
    ) -> VerificationEvidence:
        task = self.manager.load(run_id)
        evidence = VerificationEvidence(
            kind=params.kind,
            summary=params.summary,
            command=params.command,
            path=params.path,
            url=params.url,
            ok=params.ok,
            created_at=time.time(),
        )
        task.evidence.append(evidence)
        task.verification_status = "VERIFIED" if params.ok else "FAILED"
        task.updated_at = time.time()
        self.manager.save(task)
        return evidence

    # LLM: touch_heartbeat 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理touchheartbeat相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def touch_heartbeat(self, run_id: str) -> None:
        task = self.manager.load(run_id)
        task.heartbeat_at = time.time()
        task.updated_at = task.heartbeat_at
        self.manager.save(task)

    # LLM: set_status 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 更新状态对应的任务或运行状态，并保留既有字段语义；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def set_status(
        self,
        params: str | SetStatusParams,
        status: str = "",
        *,
        result: str = "",
        failure_type: str = "",
        require_evidence: bool = False,
    ) -> SubAgentTask:
        if isinstance(params, SetStatusParams):
            status_params = params
        else:
            status_params = SetStatusParams(
                run_id=params,
                status=status,
                result=result,
                failure_type=failure_type,
                require_evidence=require_evidence,
            )

        task = self.manager.load(status_params.run_id)
        normalized = status_params.status.upper()
        if status_params.require_evidence and normalized == "DONE" and not task.evidence:
            raise ValueError("缺少验收证据，不能标记为 DONE。")
        task.status = normalized
        if status_params.result:
            task.result = status_params.result
        if status_params.failure_type:
            task.failure_type = status_params.failure_type
        if normalized in {"DONE", "FAILED", "BLOCKED", "CHANNEL_ERROR", "TIMEOUT"}:
            task.ended_at = time.time()
        task.updated_at = time.time()
        self.manager.save(task)
        return task

    # LLM: _match_memory_routes 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理match记忆routes相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def _match_memory_routes(
        self,
        missing_capability: str,
        why_failed: str,
        task: SubAgentTask,
    ) -> tuple[list[str], list[dict[str, str]]]:
        route_query = " ".join([missing_capability, why_failed, task.goal]).strip()
        if not route_query:
            return [], []
        try:
            index_path = (self.manager.workspace_root / "memory" / "routing" / "INDEX.md").resolve()
            if not index_path.exists():
                return [], []
            routes = load_routes(index_path)
            matches = match_routes(route_query, routes, limit=5)
            resolution = resolve_required_paths(matches, mode="strict", auto_read_limit=3)
            injected_rule_paths = list(dict.fromkeys([*resolution.required_read_paths, *resolution.candidate_paths]))
            memory_routes = [
                {
                    "route_id": match.route.route_id,
                    "source_file": match.route.authority_file(),
                    "inject_mode": match.route.inject_mode,
                }
                for match in matches
            ]
            return injected_rule_paths, memory_routes
        except Exception:
            return [], []
