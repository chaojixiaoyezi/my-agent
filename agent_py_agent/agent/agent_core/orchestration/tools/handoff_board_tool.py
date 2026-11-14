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
    compute_payload_digest,
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
        description="管理跨代理原生交接板项目与外部助手邮箱。仅用于多代理协作，单代理进度请用 Todo。"
        "末级需显式受权，用 case_id+item_id 读取指定项目、本人 ACK/提交；不创建或发布交接项。"
        "终审需宿主角色 can_accept=true，decide_acceptance 必须带原读取的 frozen_input_version 和 item.metadata.submission_digest。",
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
                "responsible_actor": {"type": "string", "description": "publish/publish_item的接收者：internal_run必须原样使用create_subagents.created_run_ids或原生树中的run_id，不得用姓名/角色/自造别名；external_assistant用原外部助手标识。发表后将回执case_id+item_id交给该接收者"},
                "actor_id": {"type": "string", "description": "发布目标的兼容字段；internal_run仍须真实run_id，与responsible_actor同时传入时必须相同；external_assistant保持原外部助手身份语义"},
                "actor_kind": {
                    "type": "string",
                    "enum": ["internal_run", "external_assistant"],
                    "description": "internal_run表示宿主已证明的原生运行身份；外部助手必须明确external_assistant，不能靠ID前缀推断",
                },
                "frozen_input_version": {"type": "string", "description": "冻结输入版本；签收、提交和验收均须携带原读取版本"},
                "submission_digest": {"type": "string", "description": "验收必填：从 list_items 返回的 item.metadata.submission_digest 原样读取；不可自行猜测"},
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
            use_cases=(
                "多代理协作时创建协作单（open_case）、向真实 run_id 或外部助手发布带独占写集的交接项（publish_item）",
                "接收方签收（record_ack）、提交阶段交付证据（submit_handoff）及有权限主体按 submission_digest 终审（decide_acceptance）",
                "与外部交互式助手同步单写者文件邮箱（sync_external_mailbox）",
            ),
            avoid_when=(
                "单代理独立维护自己的待办或跨轮进度清单时改用 task_progress，不要自建跨代理交接单",
                "向正在运行的直属子代理追加实时引导时改用 send_guidance；跨会话发普通消息时改用 send_session_message",
                "需要绑定需求输入版本、关联验证事件或登记独立评审/需求级验收溯源时改用 manage_requirement_provenance",
            ),
            keywords=("交接板", "handoff", "mailbox", "验收", "ack"),
            examples=(
                '{"action":"open_case","title":"前后端接口联调","summary":"对齐字段契约与独占写集"}',
                '{"action":"publish_item","case_id":"case-01","responsible_actor":"run-child-01","actor_kind":"internal_run","frozen_input_version":"v1","exclusive_write_set":["src/api.py"]}',
                '{"action":"list_items","case_id":"case-01"}',
            ),
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
        action = str(params.get("action") or "").strip().lower()
        principal, _ = authoritative_self_principal(self.agent)
        actor = str(principal.run_id or "").strip()
        access = self._leaf_handoff_access(params, actor)
        if access is not None:
            return access
        scope_warnings: list[str] = []
        if action in {"decide_acceptance", "release", "block", "open_case"}:
            # 协调者也只能代表自己的裁决；不能用模型给出的名字绕过独立审查。
            field = "created_by" if action == "open_case" else "reviewer_principal"
            if params.get(field) and params[field] != actor:
                scope_warnings.append(f"{field} '{params[field]}' overridden by authoritative run_id '{actor}'")
            params = {**params, field: actor}
        if (action in {"claim", "record_ack", "complete", "submit_handoff"}
                and params.get("case_id") and params.get("item_id")):
            service = self._resolve_service()
            if service is not None:
                item = service.store.load_handoff_item(
                    str(params.get("case_id") or "").strip(), str(params.get("item_id") or "").strip())
                if item is not None:
                    if item.actor_kind != "internal_run" or actor != item.responsible_actor:
                        return self._failure(
                            "只有交接项的原生责任人可确认接收或提交；管理者可派发和观察，外部助手须走原邮箱回执。",
                            "TOOL_PERMISSION_DENIED",
                            details={"reason": "HANDOFF_RECIPIENT_MISMATCH", "requester": actor,
                                     "responsible_actor": item.responsible_actor})
                    version = str(params.get("frozen_input_version") or "").strip()
                    if not version or version != item.frozen_input_version:
                        return self._failure(
                            "确认和提交必须显式携带收件人实际读取的原冻结版本，不可自动采用当前版本。",
                            "FROZEN_INPUT_VERSION_MISMATCH", details={"reason": "FROZEN_INPUT_VERSION_MISMATCH"})
                    # 回执身份只来自宿主；模型提供的任意嵌套字段不能代表另一个执行者。
                    params = dict(params)
                    key = "recipient_ack" if action in {"claim", "record_ack"} else "submission_payload"
                    payload = params.get(key)
                    if isinstance(payload, dict) and payload.get("actor_id") and payload["actor_id"] != actor:
                        scope_warnings.append(f"{key}.actor_id overridden by authoritative run_id '{actor}'")
                    raw = payload if isinstance(payload, dict) else ({"status": "claimed"} if key == "recipient_ack" else {})
                    digest_key = "ack_digest" if key == "recipient_ack" else "submission_digest"
                    digest_payload = raw if key == "recipient_ack" else {
                        "evidence_ids": sorted(str(value) for value in params.get("evidence_ids") or ()), "payload": raw}
                    if item.metadata.get(digest_key) == compute_payload_digest(digest_payload):
                        # 旧回执按原载荷去重，不补造历史身份或因新增身份字段重复提交。
                        params[key] = raw
                        scope_warnings.append("existing_receipt_reused_without_historical_identity_claim")
                    else:
                        params[key] = {**raw, "actor_id": actor, "frozen_input_version": version}
        outcome = self.execute(params)
        if scope_warnings:
            payload = json.loads(outcome.output)
            payload["scope_warnings"] = scope_warnings
            outcome.output = json.dumps(payload, ensure_ascii=False, indent=2)
            outcome.result_envelope = {**outcome.result_envelope, "scope_warnings": scope_warnings}
        return outcome

    def _leaf_handoff_access(self, params: dict[str, object], actor: str) -> ToolHandlerOutcome | None:
        """末级只处理同任务明确交给自己的项目；终审另需宿主角色 can_accept。"""
        if not current_subagent_run_id(self.agent):
            return None
        from ....subagents.role_templates import role_template_snapshot_for_task

        source = {"record": "runner_task", "run_id": actor}
        try:
            manager = getattr(self.agent, "subagents", None)
            task = manager.load(actor) if manager is not None else None
            if task is None:
                return self._failure("当前执行者的规范角色记录不存在，未操作；请核实原 run_id，不能用模型角色名补授权。",
                                     "TOOL_PERMISSION_DENIED", {"reason": "HANDOFF_ROLE_UNPROVEN", "run_id": actor})
            role = role_template_snapshot_for_task(task)
            if role.get("can_spawn_children") is True:
                return None
            action = str(params.get("action") or "").strip().lower()
            reads = {"read", "list_items"}
            receipts = {"claim", "record_ack", "complete", "submit_handoff"}
            reviews = {"release", "block", "decide_acceptance"}
            if action not in reads | receipts | reviews:
                return self._failure("末级不能创建、发布、取消交接项或同步外部邮箱，也不能枚举全部协作单；"
                                     "请由原协调者派发，末级仅处理指定 case_id/item_id。",
                                     "TOOL_PERMISSION_DENIED", {"reason": "HANDOFF_LEAF_ACTION_FORBIDDEN", "action": action})
            case_id = str(params.get("case_id") or "").strip()
            item_id = str(params.get("item_id") or "").strip()
            if not case_id or not item_id:
                return self._failure('末级须指定 case_id 和 item_id；请使用发送方给出的引用，例如 '
                                     '{"action":"list_items","case_id":"case-…","item_id":"item-…"}。未读取其它项目。',
                                     "TOOL_INVALID_ARGUMENTS", {"reason": "HANDOFF_ITEM_REFERENCE_REQUIRED",
                                         "required_fields": {"$.case_id": "nonempty string", "$.item_id": "nonempty string"},
                                         "provided": {"case_id": case_id, "item_id": item_id},
                                         "next_action": "read_exact_case_and_item_refs_from_sender"})
            service = self._resolve_service()
            source = {"record": "handoff_item", "case_id": case_id, "item_id": item_id}
            item = service.store.load_handoff_item(case_id, item_id) if service is not None else None
            if item is None:
                return self._failure("指定交接项不存在；请向原协调者核对 case_id/item_id，未操作。",
                                     "RESOURCE_NOT_FOUND")
            source = {"record": "recipient_task", "run_id": item.responsible_actor}
            root_id = str(getattr(task, "root_id", "") or "").strip()
            try:
                recipient = task if item.responsible_actor == actor else manager.load(item.responsible_actor)
                from ....subagents.authorization_gate import OperationRequest, authorize_operation

                owner = str(getattr(getattr(self.agent, 'home_paths', None), 'owner_id', '') or '')
                recipient = authorize_operation(manager, OperationRequest('view', item.responsible_actor,
                    requester_owner=owner, requester_run_id=root_id), target=recipient)
                repo = getattr(manager, 'runtime_db', None)
                if repo is not None:
                    caller_run, recipient_run = repo.agent_run_for_run_id(actor), repo.agent_run_for_run_id(item.responsible_actor)
                    if (caller_run is None or recipient_run is None
                            or caller_run['task_run_id'] != recipient_run['task_run_id']):
                        return self._failure('原执行库未证明接收者与当前执行者属于同一原任务；未读取正文或写入。',
                            'TOOL_PERMISSION_DENIED', {'reason': 'HANDOFF_TASK_SCOPE_MISMATCH'})
                recipient_root = str(getattr(recipient, 'root_id', '') or getattr(recipient, 'id', '') or '').strip()
            except FileNotFoundError:
                owner = str(getattr(getattr(self.agent, 'home_paths', None), 'owner_id', '') or '')
                native_root = self._native_root_recipient(manager, item.responsible_actor, root_id, owner)
                recipient_root = root_id if native_root is not None else ''
            if (not root_id or recipient_root != root_id or item.actor_kind != "internal_run"):
                return self._failure("交接项不属于当前末级的原生任务树；请由其原协调者处理，未读取正文或更改状态。",
                                     "TOOL_PERMISSION_DENIED", {"reason": "HANDOFF_TASK_SCOPE_MISMATCH"})
            if action in reviews and role.get("can_accept") is not True:
                return self._failure("当前宿主角色 can_accept 不是 true，不能签终审票；请交给原协调者指定的受权验收主体，未签票。",
                                     "TOOL_PERMISSION_DENIED", {"reason": "HANDOFF_ACCEPTANCE_ROLE_REQUIRED",
                                         "role_id": role.get("id"), "can_accept": role.get("can_accept"),
                                         "required_can_accept": True, "next_action": "request_authorized_acceptor_from_parent"})
            if action in reads:
                return self._success({"items": [item.to_dict()], "total_count": 1, "next_cursor": None})
        except Exception as exc:
            return self._failure(f"核验当前角色或交接项规范记录失败（{type(exc).__name__}）；未操作。"
                                 "请核实当前 run 与指定交接项可读，不要原样盲目重试。",
                                 "TOOL_PERMISSION_DENIED", {"reason": "HANDOFF_SCOPE_UNAVAILABLE", "error_type": type(exc).__name__,
                                     "source": source, "next_action": "verify_canonical_record_readability"})
        return None

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

        thread_id, task_id = self._native_case_context()
        if self.agent is None:
            thread_id = str(params.get("thread_id") or "").strip()
            task_id = str(params.get("task_id") or "").strip()
        elif any(params.get(key) and params[key] != value for key, value in (
            ("thread_id", thread_id), ("task_id", task_id),
        )):
            return self._failure(
                "CASE_SCOPE_MISMATCH: thread_id/task_id 与原宿主运行来源不一致；省略这两个字段，"
                "由当前运行的 canonical Task/TaskLink 绑定。未创建协作单。",
                "CASE_SCOPE_MISMATCH",
            )
        req_payload = {
            "title": title or summary,
            "summary": summary or title,
            "priority": str(params.get("priority") or "normal").strip(),
            "created_by": created_by or "coordinator",
            "thread_id": thread_id,
            "task_id": task_id,
            "required_capabilities": params.get("required_capabilities") or [],
            "metadata": params.get("metadata") or {},
        }
        case = service.store.open_case(req_payload)
        return self._success({"action": "open_case", "case": case.to_dict(),
                              "scope_resolution": {"case_context": "canonical_task" if task_id else "unbound"}})

    def _native_case_context(self) -> tuple[str, str]:
        """从当前执行的原运行库取得业务根与来源线程；子代执行聊天不能替代根任务线程。"""
        agent = self.agent
        if agent is None:
            return "", ""
        principal, _ = authoritative_self_principal(agent)
        repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
        native = repo.agent_run_for_run_id(principal.run_id) if repo is not None else None
        if native is None:
            return "", ""
        task_run = repo.get_task_run(native["task_run_id"])
        task = repo.get_task(task_run["task_id"]) if task_run is not None else None
        owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
        if task is None or not owner or task["owner_id"] != owner:
            raise HandoffError("CASE_OWNER_MISMATCH: 当前运行来源不属于本 owner，未创建协作单。",
                               error_code="CASE_OWNER_MISMATCH", details={"effect_outcome": "not_started"})
        thread_id = str(task["thread_id"] or "")
        root_id = str(task["conversation_task_id"] or "")
        store = getattr(agent, "conversation_store", None)
        if not thread_id or not root_id or store is None:
            return "", ""
        link, error = store.tasks.load_report(root_id)
        thread = store.threads.load(thread_id)
        if (error is not None or link is None or link.thread_id != thread_id
                or thread is None or thread.owner_id != owner):
            return "", ""
        return thread_id, root_id

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

    def _native_root_recipient(self, manager, recipient_id: str, root_id: str, owner: str):
        """主控无叶记录时只核原canonical root与runtime owner；不把名字/前缀当身份。"""
        if not owner or not root_id or recipient_id != root_id:
            return None
        repo = getattr(manager, 'runtime_db', None)
        native = repo.agent_run_for_run_id(recipient_id) if repo is not None else None
        if native is None:
            return None
        principal, _ = authoritative_self_principal(self.agent)
        caller = repo.agent_run_for_run_id(principal.run_id)
        if caller is None or caller['task_run_id'] != native['task_run_id']:
            return None
        task_run = repo.get_task_run(native['task_run_id'])
        task = repo.get_task(task_run['task_id']) if task_run is not None else None
        return native if task is not None and task['owner_id'] == owner else None

    def _validate_native_publish_recipient(self, recipient_id: str, params: dict[str, object]) -> None:
        """只核新发布的规范身份；工具待授权与可恢复状态不当作永久不能接收。"""
        from ....subagents.authorization_gate import (
            AuthorizationError,
            OperationRequest,
            authorize_operation,
        )
        from ....subagents.models import SUBAGENT_RECOVERY_CLOSED_STATUSES, task_status_in

        def reject(reason: str, message: str, *, error_code: str = 'TOOL_PERMISSION_DENIED',
                   source: str | None = None, recipient_status: str | None = None,
                   execution_status: str | None = None, exception_type: str | None = None) -> None:
            extra = {key: value for key, value in (
                ('source', source), ('recipient_status', recipient_status),
                ('execution_status', execution_status), ('exception_type', exception_type),
            ) if value is not None}
            raise HandoffError(message, error_code=error_code, details={
                'reason': reason, 'field': 'responsible_actor', 'actual': recipient_id,
                'effect_outcome': 'not_started',
                'next_action': 'use_exact_created_run_id_and_deliver_case_item_refs',
                'example': {'action': 'publish_item', 'case_id': '<原case_id>',
                    'responsible_actor': '<create_subagents.created_run_ids中的原run_id>',
                    'actor_kind': 'internal_run', 'frozen_input_version': '<原冻结版本>'}, **extra})

        explicit = str(params.get('responsible_actor') or '').strip()
        alternate = str(params.get('actor_id') or '').strip()
        if explicit and alternate and explicit != alternate:
            reject('HANDOFF_RECIPIENT_REFERENCE_CONFLICT',
                'publish的responsible_actor与actor_id不一致；未发布。仅传一个真实接收者run_id，不能混用名称或角色。',
                error_code='TOOL_INVALID_ARGUMENTS')
        principal, _ = authoritative_self_principal(self.agent)
        sender = str(principal.run_id or '').strip()
        if not sender:
            reject('HANDOFF_RECIPIENT_CONTEXT_UNPROVEN', '缺少原发布者执行身份；未发布。请在原任务有权执行轮内发布。')
        # 根主代理本人没有普通子代理记录；其原执行身份已由scoped入口证明。
        if recipient_id == sender and not current_subagent_run_id(self.agent):
            return
        manager = getattr(self.agent, 'subagents', None)
        owner = str(getattr(getattr(self.agent, 'home_paths', None), 'owner_id', '') or '')
        try:
            scope_root = sender
            if current_subagent_run_id(self.agent):
                caller = authorize_operation(manager, OperationRequest('view', sender, requester_owner=owner,
                    requester_run_id=sender))
                scope_root = str(caller.root_id or caller.id).strip()
            try:
                target = manager.load(recipient_id)
            except FileNotFoundError:
                repo = getattr(manager, 'runtime_db', None)
                native = repo.agent_run_for_run_id(recipient_id) if repo is not None else None
                native_root = self._native_root_recipient(manager, recipient_id, scope_root, owner)
                if native_root is not None:
                    # 合法上级主控只在原runtime有身份，没有叶task.json；不猜名字/前缀。
                    from ....conversation.models import THREAD_TASK_LINK_ACTIVE_STATUS
                    from ....conversation.task_state import conversation_task_link_is_terminal
                    from ....runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES

                    # core一轮done不等于原工作已关闭；父级自然yield后仍可由原任务接交接。
                    tasks = getattr(getattr(manager, 'conversation_store', None), 'tasks', None)
                    read_link = getattr(tasks, 'load_report', None)
                    link, error = read_link(recipient_id) if callable(read_link) else (None, None)
                    if error is not None:
                        reject('HANDOFF_RECIPIENT_STATE_UNAVAILABLE',
                            '原主控任务链接不可读，不能把单轮结束当作任务关闭或继续证明；未发布。请核对原task链接后有界重试。',
                            source='thread_task_link')
                    if link is not None:
                        origin = getattr(link, 'host_user_origin', None)
                        if (link.task_id != recipient_id or not link.thread_id
                                or origin is not None and (not isinstance(origin, dict)
                                    or origin.get('owner_id') != owner or origin.get('task_id') != recipient_id
                                    or origin.get('thread_id') != link.thread_id)):
                            reject('HANDOFF_RECIPIENT_NOT_IN_SCOPE', '原主控任务链接与已证明owner/task/thread不一致；未发布，不能以active或closed标签冒充另一任务。')
                        if conversation_task_link_is_terminal(link.status):
                            reject('HANDOFF_RECIPIENT_CLOSED', '原会话任务已明确关闭，不能新接单；未发布。请沿原任务合法恢复入口处理，不能给旧ID换名。',
                                recipient_status=link.status, execution_status=native_root['status'])
                        if link.status == THREAD_TASK_LINK_ACTIVE_STATUS:
                            return
                        reject('HANDOFF_RECIPIENT_STATE_UNAVAILABLE', '原主控任务链接状态没有明确可接收证明；未发布。请核对原任务状态，不得据单轮done猜测关闭或放行。')
                    if native_root['status'] in AGENT_RUN_TERMINAL_STATUSES:
                        reject('HANDOFF_RECIPIENT_STATE_UNAVAILABLE', '原主控执行轮已结束且缺少活动任务链接，不能证明仍可接收；未发布。请由原任务正常续接后再发布。',
                            execution_status=native_root['status'])
                    return
                if native is not None:
                    sender_run = repo.agent_run_for_run_id(sender)
                    if sender_run is None or sender_run['task_run_id'] != native['task_run_id']:
                        reject('HANDOFF_RECIPIENT_NOT_IN_SCOPE',
                            'responsible_actor不是原任务可见的已证明原生run；未发布。请使用create_subagents.created_run_ids或原生树run_id，不要使用名字/角色。')
                    reject('HANDOFF_RECIPIENT_STATE_UNAVAILABLE',
                        '原运行存在但规范接收记录暂不可读，不能确认资格；未发布。请核对原run状态后有界重试，不要创建替代别名。')
                reject('HANDOFF_RECIPIENT_NOT_IN_SCOPE',
                    'responsible_actor不是原任务可见的已证明原生run；未发布。请使用create_subagents.created_run_ids或原生树run_id，不要使用名字/角色。')
            target_root = str(target.root_id or target.id).strip()
            if target_root != scope_root:
                reject('HANDOFF_RECIPIENT_NOT_IN_SCOPE', '接收者不在原任务有权范围或原权威身份不存在；未发布。请用当前有权树的真实run_id。')
            target = authorize_operation(manager, OperationRequest('view', recipient_id, requester_owner=owner,
                requester_run_id=scope_root), target=target)
            repo = getattr(manager, 'runtime_db', None)
            if repo is not None:
                sender_run, target_run = repo.agent_run_for_run_id(sender), repo.agent_run_for_run_id(recipient_id)
                if sender_run is None or target_run is None or sender_run['task_run_id'] != target_run['task_run_id']:
                    reject('HANDOFF_RECIPIENT_NOT_IN_SCOPE', '原执行库未证明发送者与接收者属于同一原任务；未发布。请核对原生树run_id，不能用root字段声明扩大范围。')
            if task_status_in(target.status, SUBAGENT_RECOVERY_CLOSED_STATUSES):
                reject('HANDOFF_RECIPIENT_CLOSED',
                    '原接收运行已关闭，不能新接单；未发布。请核对原任务合法恢复语义，不要向已结束ID新发交接。',
                    recipient_status=str(getattr(target.status, 'value', target.status)))
        except HandoffError:
            raise
        except AuthorizationError:
            reject('HANDOFF_RECIPIENT_NOT_IN_SCOPE', '接收者不在原任务有权范围或原权威身份不存在；未发布。请用当前有权树的真实run_id。')
        except Exception as error:
            reject('HANDOFF_RECIPIENT_STATE_UNAVAILABLE',
                f'核验原接收身份失败（{type(error).__name__}）；未发布。请核实原run记录与权限后有界重试，不要改名盲试。',
                exception_type=type(error).__name__)

    # LLM: 处理 publish_item 动作。
    # 函数用途: 发布交接项。
    def _handle_publish(self, service: HandoffBoardService, params: dict[str, object]) -> ToolHandlerOutcome:
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return self._failure("publish 缺少 case_id。", "TOOL_INVALID_ARGUMENTS")
        service._require_existing_case(case_id)

        request_id = str(params.get("request_id") or "").strip()
        actor_field = 'responsible_actor' if params.get('responsible_actor') else 'actor_id'
        raw_actor = params.get('responsible_actor') or params.get('actor_id')
        raw_version = params.get('frozen_input_version')
        actor = raw_actor.strip() if isinstance(raw_actor, str) else ''
        actor_kind = str(params.get("actor_kind") or "internal_run").strip()
        ver = raw_version.strip() if isinstance(raw_version, str) else ''
        if not actor or not ver:
            invalid = {}
            for field, raw, valid in ((actor_field if raw_actor is not None else 'responsible_actor', raw_actor, actor),
                                      ('frozen_input_version', raw_version, ver)):
                if valid:
                    continue
                invalid['$.' + field] = {'expected': 'nonempty string', 'actual_type': type(raw).__name__,
                    'actual_value': raw if raw is None or isinstance(raw, (str, bool, int, float)) else '<non-string value omitted>',
                    'reason': 'missing' if raw is None else 'empty' if isinstance(raw, str) else 'type_mismatch'}
            return self._failure(
                'publish参数未满足：' + ', '.join(invalid) + '必须是非空string；未发布。'
                + ('已提供的接收者有效，不能改成新名称；' if actor else '')
                + '请保留有效字段，仅修正列出的字段。冻结版本取自原任务冻结输入/候选版本，不由本工具猜测或自动补齐。',
                'TOOL_INVALID_ARGUMENTS', {'reason': 'HANDOFF_PUBLISH_PARAMETERS_INVALID',
                    'invalid_fields': invalid,
                    'missing_fields': [key for key, value in invalid.items() if value['reason'] in {'missing', 'empty'}],
                    'provided_responsible_actor': actor or None,
                    'provided_frozen_input_version': ver or None,
                    'effect_outcome': 'not_started',
                    'next_action': 'preserve_valid_actor_and_supply_original_frozen_version',
                    'example': {'action': 'publish_item', 'case_id': case_id,
                        'responsible_actor': actor or '<原已证明接收者标识>', 'actor_kind': actor_kind,
                        'frozen_input_version': ver or '<从原冻结输入/候选读取的非空版本string>'}})

        write_set = params.get("exclusive_write_set") or ()
        if isinstance(write_set, (list, tuple)):
            ws_tuple = tuple(str(x) for x in write_set)
        else:
            ws_tuple = ()

        # 身份读取在case锁外完成；去重后只应用结果，不在case锁里取manager层级锁。
        recipient_error = None
        if actor_kind == 'internal_run' and self.agent is not None:
            try:
                self._validate_native_publish_recipient(actor, params)
            except HandoffError as error:
                recipient_error = error

        def apply_recipient_validation():
            if recipient_error is not None:
                raise recipient_error

        item = service.publish_item(
            case_id=case_id,
            request_id=request_id,
            responsible_actor=actor,
            actor_kind=actor_kind,
            frozen_input_version=ver,
            exclusive_write_set=ws_tuple,
            idempotency_key=str(params.get("idempotency_key") or "").strip(),
            item_id=str(params.get("item_id") or "").strip(),
            recipient_validator=apply_recipient_validation if actor_kind == 'internal_run' and self.agent is not None else None,
        )
        return self._success({"item": item.to_dict(),
            "handoff_reference": {"case_id": item.case_id, "item_id": item.item_id,
                "responsible_actor": item.responsible_actor, "actor_kind": item.actor_kind,
                "frozen_input_version": item.frozen_input_version},
            "next_action": "将handoff_reference经原有消息入口交给接收者；同级接收者由共同父级转交，末级须携带精确case_id+item_id读取。"})

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
            frozen_input_version=str(params.get("frozen_input_version") or "").strip(),
            submission_digest=str(params.get("submission_digest") or "").strip(),
        )
        res_data: dict[str, Any] = {"item": item.to_dict()}
        if not params.get("frozen_input_version") and not params.get("submission_digest"):
            res_data["existing_vote_reused_without_new_signature"] = True
            res_data["existing_vote_binding_known"] = bool(
                (item.acceptance_verdict or {}).get("frozen_input_version")
                and (item.acceptance_verdict or {}).get("submission_digest"))
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
