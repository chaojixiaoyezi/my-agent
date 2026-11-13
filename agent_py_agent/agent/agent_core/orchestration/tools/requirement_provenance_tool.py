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
from ....verification.repository import (
    ProvenanceAuthorizationError,
    VerificationContext,
    VerificationEvidenceRepository,
)
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
                "review_id": {"type": "string", "description": "本需求同输入版本的独立评审引用；省略时冻结最近匹配的通过记录。"},
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
        principal, warning = authoritative_self_principal(self.agent)
        actor = str(principal.run_id or "").strip()
        bound = dict(params)
        warnings = [warning] if warning else []
        action = str(params.get("action") or "").strip()
        # 查询 task_id 是原授权读范围内的筛选条件，不是调用者身份。
        fields = [] if action == "query_provenance" else ["task_id"]
        identity_field = {"bind_requirement": "author_principal_id",
                          "record_independent_review": "reviewer_principal_id",
                          "record_acceptance": "accepted_by_principal_id"}.get(action)
        if identity_field:
            fields.append(identity_field)
        for key in fields:
            if params.get(key) and params[key] != actor:
                warnings.append(f"{key} overridden by authoritative run_id '{actor}'")
            bound[key] = actor
        caller_context = self._resolve_context({"task_id": actor})
        review_requirement = self._leaf_review_scope(bound, caller_context, context, actor)
        if isinstance(review_requirement, ToolHandlerOutcome):
            return review_requirement
        outcome = self.execute(bound, _context=caller_context, _review_requirement=review_requirement)
        if warnings:
            payload = json.loads(outcome.output)
            payload["scope_warnings"] = [*payload.get("scope_warnings", []), *warnings]
            outcome.output = json.dumps(payload, ensure_ascii=False, indent=2)
            outcome.result_envelope = {**outcome.result_envelope, "scope_warnings": warnings}
        return outcome

    def _leaf_review_scope(self, params, owner_context, invocation, actor):
        """叶只登记自己的同工作独立审核；工具名不授予绑定、验收或跨任务权限。"""
        if not current_subagent_run_id(self.agent):
            return None
        from ....runtime_context import current_subagent_attempt_id
        from ....subagents.role_templates import role_template_snapshot_for_task

        def deny(reason, message):
            return _provenance_failure(reason, message, error_code="TOOL_PERMISSION_DENIED")

        try:
            manager = self.agent.subagents
            task = manager.load(actor)
            if task.owner != owner_context.owner_ref:
                return deny("REVIEW_OWNER_MISMATCH", "审核执行者不属于当前owner；未写入。")
            if role_template_snapshot_for_task(task).get("can_spawn_children") is True:
                return None
            if "manage_requirement_provenance" not in (task.allowed_tools or []):
                return deny("REVIEW_TOOL_NOT_GRANTED", "原派工未授予需求溯源工具；请由原父按精确范围重新派工，未写入。")
            if invocation.runtime_snapshot.run_id != actor:
                return deny("REVIEW_RUN_MISMATCH", "工具快照不属于当前审核执行者；未写入。")
            if params.get("action") not in {"record_independent_review", "query_provenance"}:
                return deny("LEAF_PROVENANCE_ACTION_FORBIDDEN", "叶只能查询指定候选或登记自己的独立审核；绑定、关联验证和验收仍由原协调者处理，未写入。")
            requirement_id = str(params.get("requirement_id") or "").strip()
            if not requirement_id:
                return deny("REVIEW_REQUIREMENT_REQUIRED", "叶必须指定已绑定的requirement_id；不能枚举整个owner，未写入。")
            repo = VerificationEvidenceRepository(runtime_owner_root(self.agent))
            requirement = repo.get_requirement(owner_context.owner_ref, requirement_id)
            if requirement is None:
                return deny("REVIEW_REQUIREMENT_NOT_FOUND", "当前owner内没有该已绑定候选；先由原作者绑定，未写入。")
            runtime = manager.runtime_db
            attempt = current_subagent_attempt_id(self.agent)
            if runtime is None or not attempt:
                return deny("REVIEW_EXECUTION_UNPROVEN", "缺少当前审核者的原运行库或attempt；不能据工具名、角色或task_id代签，未写入。")
            own = runtime.parent_execution_ref(owner_id=owner_context.owner_ref, run_id=actor, attempt_id=attempt)
            business = runtime.business_context_for_execution(owner_id=owner_context.owner_ref,
                run_id=actor, attempt_id=attempt)
            own_task = runtime.get_task_run(own.task_run_id)["task_id"]
            related = requirement.task_id in {actor, own_task, business.task_id if business else own_task}
            if not related:
                candidate = runtime.agent_run_for_run_id(requirement.task_id)
                if candidate is not None:
                    target = runtime.parent_execution_ref(owner_id=owner_context.owner_ref,
                        run_id=requirement.task_id)
                    related = target.task_run_id == own.task_run_id
            if not related:
                # 工作流候选的task_id仍是原workflow run；用其原冻结业务DTO核对同一工作，不另建授权映射。
                from ....workflows.run_store import WorkflowRunStore

                try:
                    run = WorkflowRunStore(runtime_owner_root(self.agent)).load_run(requirement.task_id)
                except FileNotFoundError:
                    return deny("REVIEW_TASK_SCOPE_MISMATCH", "候选没有同一原工作关联；不能审核其它root或未授权候选，未写入。")
                related = (run.owner_id == owner_context.owner_ref and business is not None
                    and run.business_context == business.to_dict())
                if related:
                    bindings = [step.escalation_result.get("pending_review") for step in run.steps]
                    related = any(isinstance(binding, dict) and binding.get("requirement_id") == requirement_id
                        and binding.get("input_version") == requirement.input_version for binding in bindings)
                    if params.get("action") == "record_independent_review" and params.get("review_scope", params.get("scope")) != "workflow_effect.v1":
                        return deny("REVIEW_SCOPE_MISMATCH", "原工作流候选仅接受workflow_effect.v1审核；未写入。")
            if not related:
                return deny("REVIEW_TASK_SCOPE_MISMATCH", "候选不属于当前审核者的同一原工作/TaskRun；不能审核其它root，未写入。")
            # 先证明同工作，再比较版本/根；无权调用不能用拒绝差异探测其它root的候选内容。
            if params.get("action") == "record_independent_review" and (
                    params.get("reviewed_input_version") != requirement.input_version):
                return deny("REVIEW_INPUT_VERSION_MISMATCH", "审核输入版本与当前绑定候选不符；请读取准确候选后再审核，未写入。")
            if params.get("root") and str(params["root"]) != requirement.root:
                return deny("REVIEW_ROOT_MISMATCH", "请求的候选根与原绑定不符；未写入。")
            return requirement
        except (AttributeError, OSError, ValueError, TypeError, KeyError) as error:
            return deny("REVIEW_EXECUTION_UNPROVEN",
                f"原审核身份或候选关联无法核实（{type(error).__name__}）；先核对原派工与候选，未写入。")

    # LLM: 分发并执行具体的溯源动作，捕获业务异常并输出精准结构化回执。
    # 函数用途: 处理具体动作分发与执行。
    def execute(self, params: dict[str, object], *, _context: VerificationContext | None = None,
                _review_requirement: RequirementProvenanceRecord | None = None) -> ToolHandlerOutcome:
        action = str(params.get("action") or "").strip()
        if not action:
            return _provenance_failure("MISSING_ACTION", "必须指定操作动作 action 参数。")

        scope_warnings: list[str] = []
        try:
            repo = VerificationEvidenceRepository(runtime_owner_root(self.agent))
            context = _context or self._resolve_context(params)

            if action == "bind_requirement":
                return self._handle_bind(params, context, repo, scope_warnings)
            if action == "link_verification":
                return self._handle_link(params, context, repo)
            if action == "record_independent_review":
                return self._handle_review(params, context, repo, scope_warnings,
                    expected_requirement=_review_requirement)
            if action == "record_acceptance":
                return self._handle_acceptance(params, context, repo, scope_warnings)
            if action == "query_provenance":
                return self._handle_query(params, context, repo, expected_requirement=_review_requirement)

            return _provenance_failure("UNKNOWN_ACTION", f"不支持的溯源动作: '{action}'")
        except ProvenanceAuthorizationError as exc:
            return _provenance_failure(exc.reason, str(exc), error_code="TOOL_PERMISSION_DENIED",
                effect_outcome="not_started", scope_warnings=scope_warnings)
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
            result_envelope={"details": {"scope_warnings": list(scope_warnings)}} if scope_warnings else {},
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
        *, expected_requirement: RequirementProvenanceRecord | None = None,
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
        if expected_requirement is not None:
            saved, req = repo.record_independent_review(context, record,
                expected_requirement=expected_requirement, include_requirement=True)
        else:
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
            result_envelope={"details": {"scope_warnings": list(scope_warnings)}} if scope_warnings else {},
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
            review_id=str(params.get("review_id") or "").strip(),
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
            result_envelope={"details": {"scope_warnings": list(scope_warnings)}} if scope_warnings else {},
        )

    # LLM: 处理需求溯源查询动作（query_provenance）。
    # 函数用途: 查询特定或全量需求的闭环状态与软性建议。
    def _handle_query(
        self,
        params: dict[str, object],
        context: VerificationContext,
        repo: VerificationEvidenceRepository,
        *, expected_requirement: RequirementProvenanceRecord | None = None,
    ) -> ToolHandlerOutcome:
        req_id = str(params.get("requirement_id") or "").strip()
        task_id = str(params.get("task_id") or "").strip() or None
        root = str(params.get("root") or "").strip() or None

        if expected_requirement is not None:
            records, reviews, acceptances = repo.read_authorized_provenance(context, expected_requirement)
        elif req_id:
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
