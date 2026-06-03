
"""Public facade for subagent orchestration services."""

from __future__ import annotations

from .kernel import SubagentKernelMixin
from .manager_base import SubAgentBaseMixin, SubAgentManagerInitParams
from .services.actions import SubAgentActionService
from .services.board.facade import SubAgentBoardFacade
from .services.budget import SubAgentBudgetService
from .services.capabilities import SubAgentCapabilityService
from .services.channel_probe import SubAgentChannelProbeService
from .services.dispatch import SubAgentDispatchService, SubAgentParentPlannerService
from .services.hierarchy import SubAgentHierarchyService
from .services.indexing import SubAgentIndexingService
from .services.indexing.params import LocalRecordParams
from .services.learning import SubAgentLearningService
from .services.memory_gate import SubAgentMemoryGateService
from .services.patch_apply.facade import SubAgentPatchService
from .services.runner_context import SubAgentRunnerContextService
from .services.runner_result import SubAgentRunnerResultService
from .services.workflow import SubAgentWorkflowService

_SERVICE_FACADE_METHODS = {
    "_to_board_item": ("board", "to_board_item"),
    "_risk_flags": ("board", "risk_flags"),
    "build_board": ("board", "build_board"),
    "write_board": ("board", "write_board"),
    "due_check": ("board", "due_check"),
    "write_due_check": ("board", "write_due_check"),
    "plan_actions": ("board", "plan_actions"),
    "write_action_plan": ("board", "write_action_plan"),
    "_filter_action_plan_items": ("board", "filter_action_plan_items"),
    "_apply_action_item": ("actions", "_apply_action_item"),
    "_record_after_task_action": ("actions", "_record_after_task_action"),
    "_append_action_apply_log": ("actions", "_append_action_apply_log"),
    "_append_task_work_log": ("actions", "_append_task_work_log"),
    "apply_actions": ("actions", "apply_actions"),
    "write_action_apply_report": ("actions", "write_action_apply_report"),
    "record_capability_request": ("lifecycle", "record_capability_request"),
    "record_capability_grant": ("lifecycle", "record_capability_grant"),
    "record_capability_gap": ("lifecycle", "record_capability_gap"),
    "record_evidence": ("lifecycle", "record_evidence"),
    "touch_heartbeat": ("lifecycle", "touch_heartbeat"),
    "set_status": ("lifecycle", "set_status"),
    "prepare_runner_attempt": ("lifecycle", "prepare_runner_attempt"),
    "abandon_runner_attempt": ("lifecycle", "abandon_runner_attempt"),
    "review_patches": ("patch", "review_patches"),
    "write_patch_review_report": ("patch", "write_patch_review_report"),
    "apply_patches": ("patch", "apply_patches"),
    "write_patch_apply_report": ("patch", "write_patch_apply_report"),
    "resolve_patch_target": ("patch", "resolve_patch_target"),
    "_resolve_patch_target": ("patch", "_resolve_patch_target"),
    "_build_unified_diff": ("patch", "_build_unified_diff"),
    "_validate_patch_test_command": ("patch", "_validate_patch_test_command"),
    "_review_patch_task": ("patch", "_review_patch_task"),
    "_apply_patch_task": ("patch", "_apply_patch_task"),
    "_normalize_patch_apply_spec": ("patch", "_normalize_patch_apply_spec"),
    "_rollback_patch_apply": ("patch", "_rollback_patch_apply"),
    "route_capability_requests": ("capability", "route_capability_requests"),
    "write_capability_route_report": ("capability", "write_capability_route_report"),
    "_route_capability_apply": ("capability", "_route_capability_apply"),
    "_route_capability_request": ("capability", "_route_capability_request"),
    "_extract_granted_caps": ("runner_context", "_extract_granted_caps"),
    "_build_write_boundary": ("runner_context", "_build_write_boundary"),
    "build_execution_context": ("runner_context", "build_execution_context"),
    "_make_execution_context": ("runner_context", "_make_execution_context"),
    "write_execution_context": ("runner_context", "write_execution_context"),
    "record_runner_result": ("runner_result", "record_runner_result"),
    "_build_and_persist_result": ("runner_result", "_build_and_persist_result"),
    "_extract_parsed_output": ("runner_result", "_extract_parsed_output"),
    "_apply_status_and_build_payload": ("runner_result", "_apply_status_and_build_payload"),
    "_post_result_side_effects": ("runner_result", "_post_result_side_effects"),
    "_runner_result_build_context": ("runner_result", "_runner_result_build_context"),
    "_check_stale_runner_result": ("runner_result", "_check_stale_runner_result"),
    "_make_quick_result": ("runner_result", "_make_quick_result"),
    "make_dispatch_record": ("dispatch", "make_dispatch_record"),
    "build_dispatch_report": ("dispatch", "build_dispatch_report"),
    "write_dispatch_report": ("dispatch", "write_dispatch_report"),
    "_append_dispatch_log": ("dispatch", "_append_dispatch_log"),
    "make_dispatch_watch_record": ("dispatch", "make_dispatch_watch_record"),
    "build_dispatch_watch_report": ("dispatch", "build_dispatch_watch_report"),
    "write_dispatch_watch_report": ("dispatch", "write_dispatch_watch_report"),
    "write_dispatch_watch_heartbeat": ("dispatch", "write_dispatch_watch_heartbeat"),
    "write_parent_planner_exchange": ("parent_planner", "write_parent_planner_exchange"),
    "make_parent_planner_record": ("parent_planner", "make_parent_planner_record"),
    "build_parent_planner_report": ("parent_planner", "build_parent_planner_report"),
    "write_parent_planner_report": ("parent_planner", "write_parent_planner_report"),
    "append_dispatch_watch_log": ("dispatch", "append_dispatch_watch_log"),
    "append_parent_planner_log": ("parent_planner", "append_parent_planner_log"),
    "write_run_budget_report": ("budget", "write_run_budget_report"),
    "probe_channel": ("channel_probe", "probe_channel"),
    "probe_channels": ("channel_probe", "probe_channels"),
    "write_channel_probe_report": ("channel_probe", "write_channel_probe_report"),
    "_probe_work_order_check": ("channel_probe", "probe_work_order_check"),
    "_probe_json_files": ("channel_probe", "probe_json_files"),
    "_update_task_from_probe": ("channel_probe", "update_task_from_probe"),
    "_write_channel_probe_files": ("channel_probe", "write_channel_probe_files"),
    "_index_task": ("indexing", "index_task"),
    "_index_report": ("indexing", "index_report"),
    "_index_action_apply": ("indexing", "index_action_apply"),
    "_index_capability_route": ("indexing", "index_capability_route"),
    "_index_patch_review": ("indexing", "index_patch_review"),
    "_index_dispatch_record": ("indexing", "index_dispatch_record"),
    "_index_dispatch_watch_record": ("indexing", "index_dispatch_watch_record"),
    "_index_parent_planner_record": ("indexing", "index_parent_planner_record"),
    "_index_execution_context": ("indexing", "index_execution_context"),
    "_index_runner_result": ("indexing", "index_runner_result"),
    "_index_channel_probe": ("indexing", "index_channel_probe"),
    "_index_dataclass_record": ("indexing", "_index_dataclass_record"),
    "_select_runs": ("indexing", "select_runs"),
    "select_runs": ("indexing", "select_runs"),
    "index_task": ("indexing", "index_task"),
    "index_dispatch_record": ("indexing", "index_dispatch_record"),
    "index_dispatch_watch_record": ("indexing", "index_dispatch_watch_record"),
    "index_parent_planner_record": ("indexing", "index_parent_planner_record"),
    "index_execution_context": ("indexing", "index_execution_context"),
    "index_report": ("indexing", "index_report"),
    "learning_drafts_dir": ("learning", "learning_drafts_dir"),
    "learning_enabled": ("learning", "learning_enabled"),
    "list_learning_candidates": ("learning", "list_learning_candidates"),
    "load_learning_candidate": ("learning", "load_learning_candidate"),
    "save_learning_candidate": ("learning", "save_learning_candidate"),
    "record_learning_candidates": ("learning", "record_learning_candidates"),
    "set_learning_candidate_status": ("learning", "set_learning_candidate_status"),
    "learning_stats": ("learning", "learning_stats"),
    "list_memory_gate_candidates": ("memory_gate", "list_memory_gate_candidates"),
    "review_memory_gate_candidate": ("memory_gate", "review_memory_gate_candidate"),
    "run_memory_gate_retention": ("memory_gate", "run_memory_gate_retention"),
    "export_memory_gate_candidates_to_memory": ("memory_gate", "export_memory_gate_candidates_to_memory"),
    "export_memory_gate_candidates_to_skill_drafts": ("memory_gate", "export_memory_gate_candidates_to_skill_drafts"),
    "verify_memory_gate_boundary": ("memory_gate", "verify_memory_gate_boundary"),
    "schedule_child_runs": ("hierarchy", "schedule_child_runs"),
    "build_hierarchy_recovery_packet": ("hierarchy", "build_hierarchy_recovery_packet"),
    "orchestrate_recovery": ("hierarchy", "orchestrate_recovery"),
    "plan_leadership_recovery": ("hierarchy", "plan_leadership_recovery"),
    "write_leadership_recovery_plan": ("hierarchy", "write_leadership_recovery_plan"),
    "apply_leadership_recovery": ("hierarchy", "apply_leadership_recovery"),
    "write_leadership_recovery_apply": ("hierarchy", "write_leadership_recovery_apply"),
    "ensure_workflow_plan": ("workflow", "plan_workflow"),
    "realize_workflow_plan": ("workflow", "realize_workflow_plan"),
}


class SubAgentManager(
    SubAgentBaseMixin,
    SubagentKernelMixin,
):
    """Compatibility facade over focused subagent services."""

    def __init__(
        self,
        workspace,
        *,
        params: SubAgentManagerInitParams | None = None,
        local_store=None,
        collaboration_store=None,
        conversation_store=None,
        workspace_root=None,
        workspace_roots=None,
        role_template_dirs=None,
        enable_self_learning=False,
        debug_trace_level=0,
        takeover_chain_max_depth=0,
        closeout_for_all_task_nodes=False,
        owner_id="",
        owner_home_dir="",
        owner_policy_snapshot=None,
    ):
        # collaboration_store 只用于给 runner 注入点名 request refs；为空时保持普通子代理行为。
        params = params or _legacy_init_params(locals())
        super().__init__(
            workspace,
            params=params,
        )
        _attach_services(self)

    @property
    def _indexing_service(self):
        return self.indexing

    def __getattr__(self, name: str):
        route = _SERVICE_FACADE_METHODS.get(name)
        if route is not None:
            service_name, method_name = route
            return getattr(getattr(self, service_name), method_name)
        raise AttributeError(f"{type(self).__name__!s} object has no attribute {name!r}")

    def _log_local_record(
        self,
        *,
        params: LocalRecordParams | None = None,
        source_type: str = "",
        source_id: str = "",
        title: str = "",
        content: str = "",
        event_type: str = "",
        metadata: dict[str, object] | None = None,
    ):
        params = params or LocalRecordParams(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            event_type=event_type,
            metadata=metadata,
        )
        return self.indexing.log_local_record(params=params)

    def log_local_record(
        self,
        *,
        params: LocalRecordParams | None = None,
        source_type: str = "",
        source_id: str = "",
        title: str = "",
        content: str = "",
        event_type: str = "",
        metadata: dict[str, object] | None = None,
    ):
        params = params or LocalRecordParams(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            event_type=event_type,
            metadata=metadata,
        )
        return self.indexing.log_local_record(params=params)


def _legacy_init_params(values: dict[str, object]) -> SubAgentManagerInitParams:
    return SubAgentManagerInitParams(
        local_store=values.get("local_store"),
        collaboration_store=values.get("collaboration_store"),
        conversation_store=values.get("conversation_store"),
        workspace_root=values.get("workspace_root"),
        workspace_roots=values.get("workspace_roots"),
        role_template_dirs=values.get("role_template_dirs"),
        enable_self_learning=bool(values.get("enable_self_learning")),
        debug_trace_level=int(values.get("debug_trace_level") or 0),
        takeover_chain_max_depth=int(values.get("takeover_chain_max_depth") or 0),
        closeout_for_all_task_nodes=bool(values.get("closeout_for_all_task_nodes")),
        owner_id=str(values.get("owner_id") or ""),
        owner_home_dir=str(values.get("owner_home_dir") or ""),
        owner_policy_snapshot=values.get("owner_policy_snapshot"),
    )


def _attach_services(manager: SubAgentManager) -> None:
    manager.actions = SubAgentActionService(manager)
    manager.board = SubAgentBoardFacade(manager)
    manager.budget = SubAgentBudgetService(manager)
    manager.capability = SubAgentCapabilityService(manager)
    manager.channel_probe = SubAgentChannelProbeService(manager)
    manager.dispatch = SubAgentDispatchService(manager)
    manager.hierarchy = SubAgentHierarchyService(manager)
    manager.indexing = SubAgentIndexingService(manager)
    manager.learning = SubAgentLearningService(manager)
    manager.memory_gate = SubAgentMemoryGateService(manager)
    manager.patch = SubAgentPatchService(manager)
    manager.parent_planner = SubAgentParentPlannerService(manager)
    manager.runner_context = SubAgentRunnerContextService(manager)
    manager.runner_result = SubAgentRunnerResultService(manager)
    manager.workflow = SubAgentWorkflowService(manager)
