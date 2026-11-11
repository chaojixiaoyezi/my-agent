from __future__ import annotations

"""Requirement provenance tool for model orchestration (ORCH-05).

LLM: This tool exposes structured actions for requirement binding, verification
evidence linking, independent reviews, and final acceptance records. It enforces
fail-closed execute_scoped requester identity verification (PRO-MSG-031(2)).
模块用途: 模型侧需求溯源工具，支持需求绑定、范围诚实性校验、独立评审与阶段验收。
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ....runtime_context import current_subagent_run_id
from ....tooling.models import (
    ApprovalPolicy,
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from ....verification.repository import VerificationContext, VerificationEvidenceRepository
from ....verification.requirement_provenance import (
    AcceptanceRecord,
    IndependentReviewRecord,
    ObservationScopeHonestyError,
    RequirementProvenanceRecord,
    SelfReviewForbiddenError,
    StaleInputVersionError,
    provenance_closeout_summary,
)
from ...runtime.owner_roots import runtime_owner_root
from ..management_continuity import authoritative_self_principal

if TYPE_CHECKING:
    from ....core import SimpleAgent


# LLM: 构造需求溯源工具模型规范，默认收起（default_deferred=True）。
# 函数用途: 定义模型可见的 manage_requirement_provenance 模式与参数结构。
def build_manage_requirement_provenance_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="manage_requirement_provenance",
        description="管理与查询需求项、输入版本、单测范围、独立评审及最终验收溯源（ORCH-05）。",
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "bind_requirement",
                        "link_verification",
                        "record_independent_review",
                        "record_acceptance",
                        "query_provenance",
                    ],
                    "description": "要执行的溯源操作动作。",
                },
                "requirement_id": {
                    "type": "string",
                    "description": "需求标识（如 ORCH-05）。",
                },
                "input_version": {
                    "type": "string",
                    "description": "绑定的提交哈希或版本快照标识。",
                },
                "watched_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "该需求涉及并监控的文件或目录路径列表。",
                },
                "verification_event_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "关联的真实验证命令事件 ID 列表。",
                },
                "observation_scope": {
                    "type": "string",
                    "enum": ["module_only", "focused_integration", "full_candidate", "unverified"],
                    "description": "观测覆盖范围（升至 full_candidate 需集成单测与通过的独立评审）。",
                },
                "reviewer_principal_id": {
                    "type": "string",
                    "description": "独立评审人主体标识（禁止与作者相同）。",
                },
                "author_principal_id": {
                    "type": "string",
                    "description": "需求实现作者主体标识。",
                },
                "reviewed_input_version": {
                    "type": "string",
                    "description": "独立评审审查的具体输入版本。",
                },
                "review_verdict": {
                    "type": "string",
                    "enum": ["pass", "changes_requested"],
                    "description": "独立评审结论：pass 或 changes_requested。",
                },
                "review_scope": {
                    "type": "string",
                    "description": "评审范围说明（如架构、代码、回归单测等）。",
                },
                "accepted_by_principal_id": {
                    "type": "string",
                    "description": "签署验收的主体标识（如协调者）。",
                },
                "accepted_input_version": {
                    "type": "string",
                    "description": "验收确认的具体输入版本。",
                },
                "acceptance_verdict": {
                    "type": "string",
                    "description": "验收结论（默认 accepted）。",
                },
                "verdict": {
                    "type": "string",
                    "description": "评审或验收结论（review_verdict 或 acceptance_verdict 的别名）。",
                },
                "scope": {
                    "type": "string",
                    "description": "评审或观测范围（review_scope 的别名）。",
                },
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "引用的证据标识或快照引用列表。",
                },
                "root": {
                    "type": "string",
                    "description": "工程根目录（默认当前工作区）。",
                },
                "task_id": {
                    "type": "string",
                    "description": "所属任务标识（可选）。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="orchestration",
            use_cases=("绑定需求与代码路径及输入版本", "关联真实单测证据并诚实验证范围", "记录独立评审与最终验收", "查询结构化溯源闭环与失效建议"),
            avoid_when=("普通只读文件操作无需调用", "没有需求与版本绑定关系时"),
            keywords=("需求溯源", "输入版本", "独立评审", "验收", "provenance", "review", "acceptance"),
            default_deferred=True,
            deferred_summary="管理需求到版本、单测、独立评审与验收的闭环溯源",
        ),
    )


# LLM: 校验调用上下文是否具备可信请求方执行身份（PRO-MSG-031(2)）。
# 函数用途: 注册表调用边界检查请求方身份证明。
def _scoped_requester_proven(agent: object, context: object) -> bool:
    if not isinstance(context, ToolInvocationContext) or context.runtime_snapshot is None:
        return False

    try:
        if current_subagent_run_id(agent):
            principal, _warning = authoritative_self_principal(agent)
            return bool(str(principal.run_id or "").strip())
        run_params = getattr(agent, "_current_run_params", None)
        if run_params is None:
            return False
        has_exec_id = any(
            isinstance(value, str) and bool(value.strip())
            for value in (
                getattr(run_params, "run_id", None),
                getattr(run_params, "task_id", None),
                getattr(run_params, "request_id", None),
            )
        )
        if not has_exec_id:
            return False
        principal, _warning = authoritative_self_principal(agent)
        return bool(str(principal.run_id or "").strip())
    except Exception:  # noqa: BLE001 - 身份证明异常一律失败关闭
        return False


# LLM: 未证明请求方身份时的统一拒绝结果（TOOL_PERMISSION_DENIED + not_started + REQUESTER_IDENTITY_UNPROVEN）。
# 函数用途: 返回身份缺失失败闭合信封。
def _unproven_requester_provenance_failure() -> ToolHandlerOutcome:
    reason = "REQUESTER_IDENTITY_UNPROVEN"
    message = "当前工具调用缺少已证明的请求方执行身份，未执行需求溯源操作。"
    details: dict[str, object] = {
        "reason": reason,
        "error": {"reason": reason, "message": message},
    }
    payload: dict[str, object] = {
        "ok": False,
        "reason": reason,
        "message": message,
        "details": details,
    }
    return ToolHandlerOutcome(
        "manage_requirement_provenance",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        result_envelope={"details": details},
        error_code="TOOL_PERMISSION_DENIED",
        effect_outcome="not_started",
    )


# LLM: 生成通用的失败 ToolHandlerOutcome 封装。
# 函数用途: 格式化结构化错误返回。
def _provenance_failure(
    reason: str,
    message: str,
    *,
    error_code: str = "TOOL_INVALID_ARGUMENTS",
    effect_outcome: str = "not_started",
    scope_warnings: list[str] | tuple[str, ...] | None = None,
) -> ToolHandlerOutcome:
    details: dict[str, object] = {
        "reason": reason,
        "error": {"reason": reason, "message": message},
    }
    payload: dict[str, object] = {
        "ok": False,
        "reason": reason,
        "message": message,
        "details": details,
    }
    if scope_warnings:
        details["scope_warnings"] = list(scope_warnings)
        payload["scope_warnings"] = list(scope_warnings)
    return ToolHandlerOutcome(
        "manage_requirement_provenance",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        result_envelope={"details": details},
        error_code=error_code,
        effect_outcome=effect_outcome,
    )


# LLM: 需求溯源操作模型工具类（实现 BaseTool 与 execute_scoped）。
# 类用途: 处理来自模型或协调层的结构化需求溯源动作请求。
class RequirementProvenanceTool(BaseTool):
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        approval_policy=ApprovalPolicy("never"),
        idempotency_policy=IdempotencyPolicy("operation"),
        concurrency_policy=ConcurrencyPolicy("serial"),
        resource_scopes=ResourceScopePolicy(
            mode="declared", static_scopes=("verification_provenance",)
        ),
        mutates_workspace=False,
        promotes_task=False,
    )

    # LLM: 初始化并绑定当前 Agent。
    # 函数用途: 实例化需求溯源工具并加载模型规范。
    def __init__(self, agent: SimpleAgent) -> None:
        self.agent = agent
        self.model_spec = build_manage_requirement_provenance_model_spec()

    # LLM: 受权边界首先校验请求方身份证明，未证明时同步失败关闭（PRO-MSG-031(2)）。
    # 函数用途: 执行经过身份门槛拦截的受限调用。
    def execute_scoped(
        self,
        params: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolHandlerOutcome:
        if not _scoped_requester_proven(self.agent, context):
            return _unproven_requester_provenance_failure()
        return self.execute(params)

    # LLM: 分发并执行具体的溯源动作，捕获业务异常并输出精准结构化回执。
    # 函数用途: 处理具体动作分发与执行。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        action = str(params.get("action") or "").strip()
        if not action:
            return _provenance_failure("MISSING_ACTION", "必须指定操作动作 action 参数。")

        scope_warnings: list[str] = []
        try:
            repo = VerificationEvidenceRepository(runtime_owner_root(self.agent))
            context = self._resolve_context(params)

            if action == "bind_requirement":
                return self._handle_bind(params, context, repo, scope_warnings)
            if action == "link_verification":
                return self._handle_link(params, context, repo)
            if action == "record_independent_review":
                return self._handle_review(params, context, repo, scope_warnings)
            if action == "record_acceptance":
                return self._handle_acceptance(params, context, repo, scope_warnings)
            if action == "query_provenance":
                return self._handle_query(params, context, repo)

            return _provenance_failure("UNKNOWN_ACTION", f"不支持的溯源动作: '{action}'")
        except SelfReviewForbiddenError as exc:
            return _provenance_failure(
                "SELF_REVIEW_FORBIDDEN",
                str(exc),
                error_code="TOOL_PERMISSION_DENIED",
                effect_outcome="not_started",
                scope_warnings=scope_warnings,
            )
        except ObservationScopeHonestyError as exc:
            return _provenance_failure(
                "OBSERVATION_SCOPE_HONESTY_VIOLATION",
                str(exc),
                error_code="TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                scope_warnings=scope_warnings,
            )
        except StaleInputVersionError as exc:
            return _provenance_failure(
                "STALE_INPUT_VERSION",
                str(exc),
                error_code="TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                scope_warnings=scope_warnings,
            )
        except KeyError as exc:
            return _provenance_failure(
                "REQUIREMENT_NOT_FOUND",
                str(exc),
                error_code="TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                scope_warnings=scope_warnings,
            )
        except ValueError as exc:
            return _provenance_failure(
                "INVALID_ARGUMENTS",
                str(exc),
                error_code="TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                scope_warnings=scope_warnings,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底异常处理
            return _provenance_failure(
                "INTERNAL_ERROR",
                f"需求溯源执行异常: {exc}",
                error_code="TOOL_RUNTIME_ERROR",
                effect_outcome="failed",
                scope_warnings=scope_warnings,
            )

    # LLM: 从当前 agent 与参数中解析权威验证上下文。
    # 函数用途: 构建用于 SQLite 账本的 VerificationContext。
    def _resolve_context(self, params: dict[str, object]) -> VerificationContext:
        owner_root = str(runtime_owner_root(self.agent))
        home_paths = getattr(self.agent, "home_paths", None)
        owner_ref = str(getattr(home_paths, "owner_id", "") or Path(owner_root).name or "local")

        principal, _ = authoritative_self_principal(self.agent)
        task_id = str(params.get("task_id") or principal.run_id or principal.thread_id or "default-task").strip()
        thread_id = str(getattr(self.agent, "thread_id", "") or "default-thread").strip()

        return VerificationContext(owner_ref=owner_ref, thread_id=thread_id, task_id=task_id)

    # LLM: 处理需求绑定动作（bind_requirement）。
    # 函数用途: 注册或更新需求与监控路径及版本。
    def _handle_bind(
        self,
        params: dict[str, object],
        context: VerificationContext,
        repo: VerificationEvidenceRepository,
        scope_warnings: list[str],
    ) -> ToolHandlerOutcome:
        req_id = str(params.get("requirement_id") or "").strip()
        if not req_id:
            return _provenance_failure(
                "MISSING_REQUIREMENT_ID",
                "bind_requirement 必须指定 requirement_id。",
                scope_warnings=scope_warnings,
            )

        input_ver = str(params.get("input_version") or "").strip()
        if not input_ver:
            return _provenance_failure(
                "MISSING_INPUT_VERSION",
                "bind_requirement 必须指定 input_version。",
                scope_warnings=scope_warnings,
            )

        watched_raw = params.get("watched_paths", ())
        if isinstance(watched_raw, str):
            watched = tuple(part.strip() for part in watched_raw.split(",") if part.strip())
        elif isinstance(watched_raw, (list, tuple)):
            watched = tuple(str(p).strip() for p in watched_raw if str(p).strip())
        else:
            watched = ()

        root = str(params.get("root") or getattr(self.agent, "root", "") or ".").strip()
        author = str(params.get("author_principal_id") or "").strip()

        sub_id = current_subagent_run_id(self.agent)
        if sub_id:
            principal, _ = authoritative_self_principal(self.agent)
            self_id = str(principal.run_id or "").strip()
            if not author:
                author = self_id
            elif author != self_id:
                scope_warnings.append(f"author_principal_id overridden by runner identity '{self_id}'")
                author = self_id

        record = RequirementProvenanceRecord(
            requirement_id=req_id,
            owner_ref=context.owner_ref,
            task_id=context.task_id,
            root=root,
            input_version=input_ver,
            watched_paths=watched,
            author_principal_id=author,
        )
        updated = repo.bind_requirement(context, record)
        payload: dict[str, Any] = {"ok": True, "action": "bind_requirement", "requirement": updated.to_dict()}
        if scope_warnings:
            payload["scope_warnings"] = list(scope_warnings)
        return ToolHandlerOutcome(
            "manage_requirement_provenance",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
            result_envelope={"details": {"scope_warnings": list(scope_warnings)}} if scope_warnings else None,
        )

    # LLM: 处理关联测试命令证据动作（link_verification）。
    # 函数用途: 绑定测试命令事件到需求项。
    def _handle_link(
        self,
        params: dict[str, object],
        context: VerificationContext,
        repo: VerificationEvidenceRepository,
    ) -> ToolHandlerOutcome:
        req_id = str(params.get("requirement_id") or "").strip()
        if not req_id:
            return _provenance_failure("MISSING_REQUIREMENT_ID", "link_verification 必须指定 requirement_id。")

        raw_event_ids = params.get("verification_event_ids")
        if not isinstance(raw_event_ids, (list, tuple)):
            return _provenance_failure("INVALID_EVENT_IDS", "verification_event_ids 必须是整数列表。")

        int_event_ids: list[int] = []
        for eid in raw_event_ids:
            try:
                int_event_ids.append(int(eid))
            except (TypeError, ValueError):
                return _provenance_failure("INVALID_EVENT_ID", f"无效的事件编号: {eid}")

        observation_scope = str(params.get("observation_scope") or "module_only").strip()
        input_ver = str(params.get("input_version") or "").strip() or None

        updated = repo.link_verification(
            context,
            requirement_id=req_id,
            event_ids=int_event_ids,
            observation_scope=observation_scope,
            input_version=input_ver,
        )
        payload = {"ok": True, "action": "link_verification", "requirement": updated.to_dict()}
        return ToolHandlerOutcome(
            "manage_requirement_provenance",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    # LLM: 处理独立评审记录动作（record_independent_review）。
    # 函数用途: 登记独立评审结论并防自评。
    def _handle_review(
        self,
        params: dict[str, object],
        context: VerificationContext,
        repo: VerificationEvidenceRepository,
        scope_warnings: list[str],
    ) -> ToolHandlerOutcome:
        req_id = str(params.get("requirement_id") or "").strip()
        if not req_id:
            return _provenance_failure(
                "MISSING_REQUIREMENT_ID",
                "record_independent_review 必须指定 requirement_id。",
                scope_warnings=scope_warnings,
            )

        reviewer = str(params.get("reviewer_principal_id") or "").strip()
        author = str(params.get("author_principal_id") or "").strip()

        sub_id = current_subagent_run_id(self.agent)
        if sub_id:
            principal, _ = authoritative_self_principal(self.agent)
            self_id = str(principal.run_id or "").strip()
            if reviewer and reviewer != self_id:
                scope_warnings.append(f"reviewer_principal_id overridden by runner identity '{self_id}'")
            reviewer = self_id
        elif not reviewer:
            principal, _ = authoritative_self_principal(self.agent)
            reviewer = str(principal.run_id or "").strip()

        reviewed_ver = str(params.get("reviewed_input_version") or "").strip()
        if not reviewed_ver:
            return _provenance_failure(
                "MISSING_REVIEWED_VERSION",
                "必须指定 reviewed_input_version。",
                scope_warnings=scope_warnings,
            )

        verdict_raw = str(params.get("review_verdict") or params.get("verdict") or "pass").strip()
        if verdict_raw not in ("pass", "changes_requested"):
            return _provenance_failure(
                "INVALID_VERDICT",
                f"无效的评审结论: '{verdict_raw}'，必须为 pass 或 changes_requested。",
                scope_warnings=scope_warnings,
            )

        scope = str(params.get("review_scope") or params.get("scope") or "independent_review").strip()
        evidence_refs_raw = params.get("evidence_refs", ())
        refs = tuple(str(r).strip() for r in evidence_refs_raw if str(r).strip())

        record = IndependentReviewRecord(
            review_id="",
            requirement_id=req_id,
            reviewer_principal_id=reviewer,
            author_principal_id=author,
            reviewed_input_version=reviewed_ver,
            verdict=verdict_raw,  # type: ignore[arg-type]
            scope=scope,
            evidence_refs=refs,
            owner_ref=context.owner_ref,
        )
        saved = repo.record_independent_review(context, record)
        req = repo.get_requirement(context.owner_ref, req_id)
        payload: dict[str, Any] = {
            "ok": True,
            "action": "record_independent_review",
            "review": saved.to_dict(),
            "requirement": req.to_dict() if req is not None else None,
        }
        if scope_warnings:
            payload["scope_warnings"] = list(scope_warnings)
        return ToolHandlerOutcome(
            "manage_requirement_provenance",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
            result_envelope={"details": {"scope_warnings": list(scope_warnings)}} if scope_warnings else None,
        )

    # LLM: 处理阶段与最终验收动作（record_acceptance）。
    # 函数用途: 登记验收确认事实。
    def _handle_acceptance(
        self,
        params: dict[str, object],
        context: VerificationContext,
        repo: VerificationEvidenceRepository,
        scope_warnings: list[str],
    ) -> ToolHandlerOutcome:
        req_id = str(params.get("requirement_id") or "").strip()
        if not req_id:
            return _provenance_failure(
                "MISSING_REQUIREMENT_ID",
                "record_acceptance 必须指定 requirement_id。",
                scope_warnings=scope_warnings,
            )

        accepted_by = str(params.get("accepted_by_principal_id") or "").strip()
        sub_id = current_subagent_run_id(self.agent)
        if sub_id:
            principal, _ = authoritative_self_principal(self.agent)
            self_id = str(principal.run_id or "").strip()
            if accepted_by and accepted_by != self_id:
                scope_warnings.append(f"accepted_by_principal_id overridden by runner identity '{self_id}'")
            accepted_by = self_id
        elif not accepted_by:
            principal, _ = authoritative_self_principal(self.agent)
            accepted_by = str(principal.run_id or "").strip()

        if not accepted_by:
            return _provenance_failure(
                "MISSING_ACCEPTED_BY",
                "必须指定 accepted_by_principal_id。",
                scope_warnings=scope_warnings,
            )

        accepted_ver = str(params.get("accepted_input_version") or "").strip()
        if not accepted_ver:
            return _provenance_failure(
                "MISSING_ACCEPTED_VERSION",
                "必须指定 accepted_input_version。",
                scope_warnings=scope_warnings,
            )

        verdict = str(params.get("acceptance_verdict") or params.get("verdict") or "accepted").strip()
        scope = str(params.get("observation_scope") or "").strip()
        evidence_refs_raw = params.get("evidence_refs", ())
        refs = tuple(str(r).strip() for r in evidence_refs_raw if str(r).strip())

        record = AcceptanceRecord(
            acceptance_id="",
            requirement_id=req_id,
            accepted_by_principal_id=accepted_by,
            accepted_input_version=accepted_ver,
            verdict=verdict,  # type: ignore[arg-type]
            observation_scope=scope,
            evidence_refs=refs,
            owner_ref=context.owner_ref,
        )
        saved = repo.record_acceptance(context, record)
        req = repo.get_requirement(context.owner_ref, req_id)
        payload: dict[str, Any] = {
            "ok": True,
            "action": "record_acceptance",
            "acceptance": saved.to_dict(),
            "requirement": req.to_dict() if req is not None else None,
        }
        if scope_warnings:
            payload["scope_warnings"] = list(scope_warnings)
        return ToolHandlerOutcome(
            "manage_requirement_provenance",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
            result_envelope={"details": {"scope_warnings": list(scope_warnings)}} if scope_warnings else None,
        )

    # LLM: 处理需求溯源查询动作（query_provenance）。
    # 函数用途: 查询特定或全量需求的闭环状态与软性建议。
    def _handle_query(
        self,
        params: dict[str, object],
        context: VerificationContext,
        repo: VerificationEvidenceRepository,
    ) -> ToolHandlerOutcome:
        req_id = str(params.get("requirement_id") or "").strip()
        task_id = str(params.get("task_id") or "").strip() or None
        root = str(params.get("root") or "").strip() or None

        if req_id:
            req = repo.get_requirement(context.owner_ref, req_id)
            records = [req] if req is not None else []
            reviews = repo.list_independent_reviews(context.owner_ref, requirement_id=req_id)
            acceptances = repo.list_acceptance_records(context.owner_ref, requirement_id=req_id)
        else:
            records = repo.list_requirements(context.owner_ref, task_id=task_id, root=root)
            reviews = repo.list_independent_reviews(context.owner_ref)
            acceptances = repo.list_acceptance_records(context.owner_ref)

        summary = provenance_closeout_summary(records)
        payload = {
            "ok": True,
            "action": "query_provenance",
            "records": [r.to_dict() for r in records],
            "reviews": [rev.to_dict() for rev in reviews],
            "acceptances": [acc.to_dict() for acc in acceptances],
            "summary": summary,
        }
        return ToolHandlerOutcome(
            "manage_requirement_provenance",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )


build_requirement_provenance_model_spec = build_manage_requirement_provenance_model_spec

__all__ = [
    "RequirementProvenanceTool",
    "build_manage_requirement_provenance_model_spec",
    "build_requirement_provenance_model_spec",
]
