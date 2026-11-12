# LLM: 原生交接板（ManageHandoffBoardTool）模型工具：
#   提供跨代理任务交接板生命周期管理与外部助手单写者安全邮箱同步：
#   1. execute_scoped 边界执行严格的已证明请求方身份校验（REQUESTER_IDENTITY_UNPROVEN，对齐 031(2)）；
#   2. 严格遵循 Submission != receipt != acceptance 单向状态机；
#   3. 支持 publish_item / record_ack / submit_handoff / decide_acceptance / sync_external_mailbox / list_items；
#   4. 稳定 ID 冲突与写入集重叠统一返回确定性错误码及 not_started 副作用判定。
# 模块用途: 供模型调用的原生交接板管理工具。
"""Model tool for managing collaboration handoff board and external mailboxes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ....collaboration.external_mailbox_adapter import (
    ExternalMailboxAdapter,
    ExternalMailboxSecurityError,
    create_external_mailbox_layout,
)
from ....collaboration.handoff_board import (
    FrozenInputVersionMismatchError,
    HandoffBoardService,
    HandoffError,
    HandoffWriteSetOverlapError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
)
from ....collaboration.store import CollaborationStore
from ....runtime_context import current_subagent_run_id
from ....tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from ...runtime.owner_roots import runtime_owner_root
from ..management_continuity import authoritative_self_principal

if TYPE_CHECKING:
    from ....core import SimpleAgent

logger = logging.getLogger(__name__)


# LLM: 构造 manage_handoff_board 的模型规范，声明入参 Schema 与延迟加载提示。
# 函数用途: 返回交接板工具的 ToolModelSpec 描述。
def build_manage_handoff_board_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="manage_handoff_board",
        description="管理跨代理原生交接板（Handoff Board）项目生命周期与外部助手邮箱同步。本工具严格用于多代理/子代理协作任务交接（multi-agent / subagent collaboration），单代理独立执行时严禁调用本工具（单代理步骤进度记录请使用 Todo 工具）。",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "publish_item",
                        "record_ack",
                        "submit_handoff",
                        "decide_acceptance",
                        "sync_external_mailbox",
                        "list_items",
                        "open_case",
                        "list_cases",
                        "publish",
                        "claim",
                        "complete",
                        "release",
                        "block",
                        "cancel",
                        "read",
                    ],
                    "description": "交接板管理动作",
                },
                "title": {"type": "string", "description": "协作单标题"},
                "summary": {"type": "string", "description": "协作单摘要描述"},
                "priority": {"type": "string", "description": "优先级 (normal, high, low)"},
                "case_id": {"type": "string", "description": "所属协作 case 标识"},
                "item_id": {"type": "string", "description": "交接项标识"},
                "request_id": {"type": "string", "description": "关联协作请求标识"},
                "responsible_actor": {"type": "string", "description": "负责代理标识"},
                "actor_id": {"type": "string", "description": "外部助手标识（同 responsible_actor）"},
                "actor_kind": {
                    "type": "string",
                    "enum": ["internal_run", "external_assistant"],
                    "description": "代理类型",
                },
                "frozen_input_version": {"type": "string", "description": "冻结输入版本"},
                "exclusive_write_set": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "排他独占写入文件/目录路径列表",
                },
                "recipient_ack": {"type": "object", "description": "接收方 ACK 回执字典"},
                "ack_id": {"type": "string", "description": "ACK 回执去重标识"},
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "交付证据标识列表",
                },
                "submission_id": {"type": "string", "description": "交付提交去重标识"},
                "verdict": {
                    "type": "string",
                    "enum": ["accepted", "changes_requested"],
                    "description": "验收判定结论",
                },
                "reviewer_principal": {"type": "string", "description": "验收评审主体标识"},
                "reason": {"type": "string", "description": "判定说明或修改原因"},
                "root_dir": {"type": "string", "description": "外部邮箱协调根目录路径"},
                "state": {"type": "string", "description": "按交接项状态过滤"},
                "idempotency_key": {"type": "string", "description": "幂等性去重键"},
                "limit": {"type": "integer", "description": "分页返回上限"},
                "cursor": {"type": "string", "description": "分页游标"},
            },
            "required": ["action"],
        },
        hints=ToolModelHints(
            category="orchestration",
            use_cases=("跨代理交付与交接项板管理", "外部交互式助手安全文件邮箱同步"),
            avoid_when=("内部同一会话简单发消息用 send_session_message",),
            keywords=("交接板", "handoff", "mailbox", "验收", "ack"),
            default_deferred=True,
            deferred_summary="管理跨代理交接板项目状态与外部助手邮箱",
        ),
    )


# LLM: 校验 ToolInvocationContext 是否携带权威已证明请求方身份，对齐 031(2)。
# 函数用途: 判断当前调用请求方是否已通过宿主身份证明。
def _scoped_requester_proven(agent: object, context: object) -> bool:
    if not isinstance(context, ToolInvocationContext) or context.runtime_snapshot is None:
        return False
    from ....runtime_context import current_subagent_run_id

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


# LLM: 未证明身份在任何交接板读取或写入前同步返回 TOOL_PERMISSION_DENIED + not_started。
# 函数用途: 生成身份未证明时的标准化失败结果。
def _unproven_requester_failure() -> ToolHandlerOutcome:
    reason = "REQUESTER_IDENTITY_UNPROVEN"
    message = "当前工具调用缺少已证明的请求方执行身份，未操作任何交接项。"
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
        "manage_handoff_board",
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        result_envelope={"details": details},
        error_code="TOOL_PERMISSION_DENIED",
        effect_outcome="not_started",
    )


# LLM: 原生交接板管理工具实体，处理发布、ACK、证据提交、终审验收与外部邮箱同步。
# 类用途: 暴露交接板管理能力的 BaseTool 子类。
class ManageHandoffBoardTool(BaseTool):
    model_spec = build_manage_handoff_board_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        approval_policy=ApprovalPolicy("never"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(
            parameter_names=("case_id", "item_id", "request_id"),
            parameter_kinds={"case_id": "logical", "item_id": "logical", "request_id": "logical"},
        ),
    )
    default_deferred = True

    # LLM: 初始化交接板工具，可注入 agent 引用或显式 handoff_service。
    # 函数用途: 构造 ManageHandoffBoardTool 实例。
    def __init__(
        self,
        agent: SimpleAgent | None = None,
        service: HandoffBoardService | None = None,
    ) -> None:
        self.agent = agent
        self._service = service

    # LLM: 统一注册表受权入口；在执行任何交接板动作前先校验请求方身份已证明。
    # 函数用途: 执行已证明请求方作用域保护后委托给 execute()。
    def execute_scoped(
        self,
        params: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolHandlerOutcome:
        if not _scoped_requester_proven(self.agent, context):
            return _unproven_requester_failure()
        return self.execute(params)

    # LLM: 解析操作并分发至各具体动作，统一包装结构化结果或失败原因。
    # 函数用途: 执行交接板具体动作指令。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        action = str(params.get("action") or "").strip().lower()
        if not action:
            return self._failure("缺少必需参数 'action'。", "TOOL_INVALID_ARGUMENTS")

        service = self._resolve_service()
        if service is None:
            return self._failure("无法解析交接板底层存储服务。", "SERVICE_UNAVAILABLE")

        try:
            if action == "open_case":
                return self._handle_open_case(service, params)
            if action == "list_cases":
                return self._handle_list_cases(service, params)
            if action in {"publish", "publish_item"}:
                return self._handle_publish(service, params)
            if action in {"claim", "record_ack"}:
                return self._handle_ack(service, params)
            if action in {"complete", "submit_handoff"}:
                return self._handle_submit(service, params)
            if action in {"release", "block", "decide_acceptance"}:
                return self._handle_decide(service, params)
            if action in {"cancel", "close_item"}:
                return self._handle_close(service, params)
            if action in {"read", "list_items"}:
                return self._handle_list(service, params)
            if action == "sync_external_mailbox":
                return self._handle_sync_mailbox(service, params)
            return self._failure(f"未知交接板动作: {action!r}", "TOOL_INVALID_ARGUMENTS")
        except KeyError as exc:
            return self._failure(
                str(exc),
                "RESOURCE_NOT_FOUND",
                details={
                    "error": str(exc),
                    "hint": "请先通过 action='list_cases' 查询已有协作单，或通过 action='open_case' 创建新协作单。",
                },
            )
        except (ValueError, TypeError) as exc:
            return self._failure(str(exc), "TOOL_INVALID_ARGUMENTS")
        except HandoffError as exc:
            return self._failure(str(exc), exc.error_code, details=exc.details)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in ManageHandoffBoardTool: %s", exc)
            return self._failure(f"执行失败: {exc}", "INTERNAL_ERROR")

    # LLM: 尝试从内部属性或 agent 解析 HandoffBoardService 实例。
    # 函数用途: 解析交接板服务对象。
    def _resolve_service(self) -> HandoffBoardService | None:
        if self._service is not None:
            return self._service
        if self.agent is not None:
            store = getattr(self.agent, "collaboration_store", None)
            conv_store = getattr(self.agent, "conversation_store", None)
            ws_root = (
                getattr(self.agent, "workspace_root", None)
                or getattr(self.agent, "root", None)
                or getattr(getattr(self.agent, "subagents", None), "workspace_root", None)
            )
            if store is None and ws_root is not None:
                store = CollaborationStore(Path(ws_root) / "collaboration")
            if store is not None:
                self._service = HandoffBoardService(
                    store=store,
                    conversation_store=conv_store,
                    workspace_root=ws_root,
                )
                return self._service
        return None

    def _handle_open_case(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        title = str(params.get("title") or params.get("summary") or "").strip()
        summary = str(params.get("summary") or params.get("title") or "").strip()
        if not title and not summary:
            return self._failure("open_case 必须指定 title 或 summary。", "TOOL_INVALID_ARGUMENTS")

        agent = self.agent or getattr(self, "_agent", None)
        if agent is not None and getattr(agent, "subagents", None) is not None:
            parent_run_id = current_subagent_run_id(agent)
            is_background_agent = False
            thread_id = str(params.get("thread_id") or getattr(agent, "current_thread_id", None) or getattr(getattr(agent, "_current_thread", None), "thread_id", "") or "").strip()
            conv_store = getattr(agent, "conversation_store", None) or getattr(service, "conversation_store", None)
            if conv_store is not None and thread_id:
                try:
                    thread_obj = conv_store.threads.get(thread_id)
                    if thread_obj is not None:
                        from ....conversation.background_team_lifecycle import background_identity
                        if background_identity(thread_obj) is not None or getattr(thread_obj, "thread_kind", "") == "background_agent":
                            is_background_agent = True
                except Exception:
                    pass

            subagents_mgr = getattr(agent, "subagents", None)
            subagent_runs_count = 0
            root_id = str(getattr(getattr(agent, "_current_run_params", None), "run_id", "") or getattr(getattr(agent, "_current_run_params", None), "task_id", "") or "").strip()
            if root_id and hasattr(subagents_mgr, "list_runs_for_root_report"):
                try:
                    report = subagents_mgr.list_runs_for_root_report(root_id)
                    subagent_runs_count = len(getattr(report, "runs", ()))
                except Exception:
                    pass
            if subagent_runs_count == 0 and thread_id and hasattr(subagents_mgr, "list_current_runs_report"):
                try:
                    report = subagents_mgr.list_current_runs_report(thread_id=thread_id)
                    subagent_runs_count = len(getattr(report, "runs", ()))
                except Exception:
                    pass
            if subagent_runs_count == 0 and not root_id and not thread_id and hasattr(subagents_mgr, "list_runs"):
                try:
                    subagent_runs_count = len(subagents_mgr.list_runs())
                except Exception:
                    pass

            if not parent_run_id and not is_background_agent and subagent_runs_count == 0:
                message = "当前处于单代理前台独立执行环境，无需创建跨代理交接单；单代理步骤与进度管理请改用 Todo 工具推进，请勿重试 manage_handoff_board。"
                details = {
                    "error_code": "SINGLE_AGENT_HANDOFF_NOT_NEEDED",
                    "effect_outcome": "not_started",
                    "retryable": False,
                    "next_action": "use_todo_tool",
                    "message": message,
                }
                outcome = ToolHandlerOutcome(
                    "manage_handoff_board",
                    False,
                    json.dumps({"ok": False, **details}, ensure_ascii=False, indent=2),
                    result_envelope={"retryable": False, "effect_outcome": "not_started", "next_action": "use_todo_tool", "details": details},
                    error_code="SINGLE_AGENT_HANDOFF_NOT_NEEDED",
                    effect_outcome="not_started",
                )
                object.__setattr__(outcome, "error_code", "SINGLE_AGENT_HANDOFF_NOT_NEEDED")
                return outcome

        created_by = str(params.get("created_by") or params.get("responsible_actor") or params.get("actor_id") or "").strip()
        if self.agent is not None and current_subagent_run_id(self.agent):
            principal, _ = authoritative_self_principal(self.agent)
            self_id = str(principal.run_id or "").strip()
            if self_id:
                created_by = self_id
        elif not created_by and self.agent is not None:
            try:
                principal, _ = authoritative_self_principal(self.agent)
                created_by = str(getattr(principal, "run_id", "") or "").strip()
            except Exception:
                pass

        req_payload = {
            "title": title or summary,
            "summary": summary or title,
            "priority": str(params.get("priority") or "normal").strip(),
            "created_by": created_by or "coordinator",
            "thread_id": str(params.get("thread_id") or "").strip(),
            "task_id": str(params.get("task_id") or "").strip(),
            "required_capabilities": params.get("required_capabilities") or [],
            "metadata": params.get("metadata") or {},
        }
        case = service.store.open_case(req_payload)
        return self._success({"action": "open_case", "case": case.to_dict()})

    def _handle_list_cases(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        status = str(params.get("status") or "").strip() or None
        raw_limit = params.get("limit")
        limit = self._parse_limit(raw_limit, default=10, max_val=20)
        cases = service.store.list_cases(status=status)
        cases_data = [
            {
                "case_id": c.case_id,
                "status": c.status,
                "title": c.title,
                "updated_at": c.updated_at,
            }
            for c in cases[:limit]
        ]
        return self._success({"action": "list_cases", "cases": cases_data, "total_count": len(cases)})

    # LLM: 处理 publish_item 动作。
    # 函数用途: 发布交接项。
    def _handle_publish(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return self._failure("publish 缺少 case_id。", "TOOL_INVALID_ARGUMENTS")
        service._require_existing_case(case_id)

        request_id = str(params.get("request_id") or "").strip()
        actor = str(params.get("responsible_actor") or params.get("actor_id") or "").strip()
        actor_kind = str(params.get("actor_kind") or "internal_run").strip()
        ver = str(params.get("frozen_input_version") or "").strip()
        if not actor or not ver:
            return self._failure("publish 缺少 responsible_actor 或 frozen_input_version。", "TOOL_INVALID_ARGUMENTS")

        write_set = params.get("exclusive_write_set") or ()
        if isinstance(write_set, (list, tuple)):
            ws_tuple = tuple(str(x) for x in write_set)
        else:
            ws_tuple = ()

        item = service.publish_item(
            case_id=case_id,
            request_id=request_id,
            responsible_actor=actor,
            actor_kind=actor_kind,
            frozen_input_version=ver,
            exclusive_write_set=ws_tuple,
            idempotency_key=str(params.get("idempotency_key") or "").strip(),
            item_id=str(params.get("item_id") or "").strip(),
        )
        return self._success({"item": item.to_dict()})

    # LLM: 处理 record_ack 动作。
    # 函数用途: 登记接收方 ACK 回执。
    def _handle_ack(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        case_id = str(params.get("case_id") or "").strip()
        item_id = str(params.get("item_id") or "").strip()
        if not case_id or not item_id:
            return self._failure("claim / record_ack 缺少 case_id 或 item_id。", "TOOL_INVALID_ARGUMENTS")
        service._require_existing_case(case_id)
        recipient_ack = params.get("recipient_ack") or {"status": "claimed"}
        frozen_ver = str(params.get("frozen_input_version") or "").strip()
        if not frozen_ver:
            try:
                existing = service.store.load_handoff_item(case_id, item_id)
                if existing is not None:
                    frozen_ver = existing.frozen_input_version
            except Exception:
                pass

        item = service.record_ack(
            case_id=case_id,
            item_id=item_id,
            recipient_ack=dict(recipient_ack) if isinstance(recipient_ack, dict) else {"ack": recipient_ack},
            frozen_input_version=frozen_ver or None,
            ack_id=str(params.get("idempotency_key") or params.get("ack_id") or "").strip(),
        )
        return self._success({"item": item.to_dict()})

    # LLM: 处理 submit_handoff 动作。
    # 函数用途: 登记交付物证据提交。
    def _handle_submit(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        case_id = str(params.get("case_id") or "").strip()
        item_id = str(params.get("item_id") or "").strip()
        if not case_id or not item_id:
            return self._failure("complete / submit_handoff 缺少 case_id 或 item_id。", "TOOL_INVALID_ARGUMENTS")
        service._require_existing_case(case_id)

        ev_ids = params.get("evidence_ids") or ()
        if isinstance(ev_ids, (list, tuple)):
            ev_tuple = tuple(str(x) for x in ev_ids)
        elif params.get("evidence_id"):
            ev_tuple = (str(params.get("evidence_id")).strip(),)
        else:
            ev_tuple = ()

        item = service.submit_handoff(
            case_id=case_id,
            item_id=item_id,
            evidence_ids=ev_tuple,
            submission_payload=params.get("submission_payload") if isinstance(params.get("submission_payload"), dict) else None,
            submission_id=str(params.get("idempotency_key") or params.get("submission_id") or "").strip(),
        )
        return self._success({"item": item.to_dict()})

    # LLM: 处理 decide_acceptance 动作。
    # 函数用途: 执行终审验收裁决。
    def _handle_decide(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        action = str(params.get("action") or "").strip().lower()
        case_id = str(params.get("case_id") or "").strip()
        item_id = str(params.get("item_id") or "").strip()
        if not case_id or not item_id:
            return self._failure("decide_acceptance 缺少 case_id 或 item_id。", "TOOL_INVALID_ARGUMENTS")
        service._require_existing_case(case_id)

        verdict = str(params.get("verdict") or "").strip()
        if not verdict:
            if action == "release":
                verdict = "accepted"
            elif action == "block":
                verdict = "changes_requested"
            else:
                return self._failure("decide_acceptance 缺少 verdict。", "TOOL_INVALID_ARGUMENTS")

        reason = str(params.get("reason") or "").strip()
        supplied_reviewer = str(params.get("reviewer_principal") or "").strip()
        reviewer = supplied_reviewer or "coordinator"

        scope_warnings: list[str] = []
        if self.agent is not None and current_subagent_run_id(self.agent):
            principal, _ = authoritative_self_principal(self.agent)
            bound_run_id = str(principal.run_id or "").strip()
            if bound_run_id:
                if supplied_reviewer and supplied_reviewer != bound_run_id:
                    scope_warnings.append(
                        f"reviewer_principal '{supplied_reviewer}' overridden by authoritative run_id '{bound_run_id}'"
                    )
                reviewer = bound_run_id

        item = service.decide_acceptance(
            case_id=case_id,
            item_id=item_id,
            verdict=verdict,
            reviewer_principal=reviewer,
            reason=reason,
        )
        res_data: dict[str, Any] = {"item": item.to_dict()}
        if scope_warnings:
            res_data["scope_warnings"] = scope_warnings
        return self._success(res_data)

    def _handle_close(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        case_id = str(params.get("case_id") or "").strip()
        item_id = str(params.get("item_id") or "").strip()
        if not case_id or not item_id:
            return self._failure("cancel / close_item 缺少 case_id 或 item_id。", "TOOL_INVALID_ARGUMENTS")
        service._require_existing_case(case_id)
        reason = str(params.get("reason") or "cancelled").strip()
        item = service.close_item(case_id=case_id, item_id=item_id, reason=reason)
        return self._success({"item": item.to_dict()})

    # LLM: 处理 sync_external_mailbox 动作。
    # 函数用途: 触发外部文件邮箱双向同步。
    def _handle_sync_mailbox(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        root_dir = str(params.get("root_dir") or "").strip()
        actor_id = str(params.get("responsible_actor") or params.get("actor_id") or "").strip()
        if not root_dir or not actor_id:
            return self._failure("sync_external_mailbox 缺少 root_dir 或 actor_id。", "TOOL_INVALID_ARGUMENTS")

        authorized_roots: list[Path] = []
        if getattr(service, "workspace_root", None):
            authorized_roots.append(Path(service.workspace_root).resolve())
        if getattr(service, "store", None) and getattr(service.store, "root", None):
            authorized_roots.append(Path(service.store.root).resolve())
        if self.agent is not None:
            agent_root = getattr(self.agent, "root", None)
            if agent_root:
                try:
                    authorized_roots.append(Path(agent_root).resolve())
                except Exception:
                    pass
            for wr in getattr(getattr(self.agent, "tools", None), "workspace_roots", ()) or ():
                if wr:
                    try:
                        authorized_roots.append(Path(wr).resolve())
                    except Exception:
                        pass
            try:
                owner_root = runtime_owner_root(self.agent)
                if owner_root:
                    authorized_roots.append(Path(owner_root).resolve())
            except Exception:
                pass

        try:
            adapter = ExternalMailboxAdapter(allowed_roots=authorized_roots)
            adapter.validate_confined_path(Path(root_dir))
            layout = create_external_mailbox_layout(root_dir, actor_id)
        except ExternalMailboxSecurityError as exc:
            return self._failure(str(exc), exc.error_code)

        report = adapter.sync_external_mailbox(service, layout)
        return self._success(report)

    def _parse_limit(self, raw_limit: object, default: int = 50, max_val: int = 200) -> int:
        if raw_limit is None:
            return default
        try:
            val = int(raw_limit)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"limit must be a positive integer, got: {raw_limit!r}") from exc
        if val <= 0:
            raise ValueError(f"limit must be a positive integer, got: {val}")
        return min(val, max_val)

    # LLM: 处理 list_items 动作。
    # 函数用途: 检索交接项列表与游标。
    def _handle_list(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        case_id = str(params.get("case_id") or "").strip() or None
        state = str(params.get("state") or "").strip() or None
        actor = str(params.get("responsible_actor") or params.get("actor_id") or "").strip() or None
        raw_limit = params.get("limit")
        limit = self._parse_limit(raw_limit, default=50, max_val=200)
        cursor = str(params.get("cursor") or "").strip() or None

        if case_id:
            service._require_existing_case(case_id)

        res = service.list_items(
            case_id=case_id,
            state=state,
            responsible_actor=actor,
            limit=limit,
            cursor=cursor,
        )
        return self._success(res)

    # LLM: 构造标准化失败 ToolHandlerOutcome。
    # 函数用途: 返回错误结果封装。
    def _failure(
        self,
        message: str,
        error_code: str,
        details: dict[str, Any] | None = None,
        effect_outcome: str = "not_started",
    ) -> ToolHandlerOutcome:
        det = details or {}
        payload = {
            "ok": False,
            "error_code": error_code,
            "message": message,
            "details": det,
        }
        outcome = ToolHandlerOutcome(
            "manage_handoff_board",
            False,
            json.dumps(payload, ensure_ascii=False, indent=2),
            result_envelope={"details": det, "error_code": error_code},
            error_code=error_code,
            effect_outcome=effect_outcome,
        )
        object.__setattr__(outcome, "error_code", error_code)
        return outcome

    # LLM: 构造标准化成功 ToolHandlerOutcome。
    # 函数用途: 返回成功结果封装。
    def _success(self, data: dict[str, Any]) -> ToolHandlerOutcome:
        payload = {
            "ok": True,
            "result": data,
            **data,
        }
        return ToolHandlerOutcome(
            "manage_handoff_board",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
            result_envelope=data,
        )


HandoffBoardTool = ManageHandoffBoardTool

__all__ = [
    "HandoffBoardTool",
    "ManageHandoffBoardTool",
    "build_manage_handoff_board_model_spec",
]
