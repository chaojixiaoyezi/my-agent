"""末级交接参与权与冻结提交的原子验收，不扩大下级管理权限。"""
from contextlib import contextmanager
from threading import Event, Thread

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import leaf_allowed_tools
from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
    ManageHandoffBoardTool,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.collaboration.handoff_board import HandoffError, IdempotencyConflictError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import (
    ToolPolicyRequest,
    scheduled_child_tools,
)
from agent_py_agent.agent.tooling.models import ToolInvocationContext


@pytest.fixture
def board(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_subagents=True), tmp_path)
    manager = agent.subagents
    root = manager.create_run(goal="root", thought="test", plan=["handoff"], role="coordinator")
    def child(role="worker"):
        return manager.create_run(goal="child", thought="test", plan=["handoff"], role=role,
                                  parent_id=root.id, root_id=root.id, allowed_tools=["read_file", "manage_handoff_board"])
    recipient, other, approver = child(), child(), child()
    approver.attributes["role_template"] = {"id": "host-acceptor", "can_accept": True,
                                             "can_spawn_children": False}
    manager.save(approver)
    tool = ManageHandoffBoardTool(agent)
    service = tool._resolve_service()
    case = service.store.open_case({"task_id": root.id})
    item = service.publish_item(case_id=case.case_id, request_id="request", responsible_actor=recipient.id,
                                frozen_input_version="sha256:frozen", item_id="item")
    return agent, tool, service, root, recipient, other, approver, case, item


@contextmanager
def caller(agent, task):
    before = getattr(agent, "_current_run_params", None)
    agent._current_run_params = RunParams(run_id=task.id, task_id=task.root_id,
                                          task_attributes=task.attributes)
    previous = set_current_subagent_context(agent, run_id=task.id, task_attributes=task.attributes)
    try:
        yield ToolInvocationContext(runtime_snapshot=agent.tools.runtime_snapshot())
    finally:
        restore_current_subagent_context(agent, previous)
        agent._current_run_params = before


def invoke(board, task, action, **extra):
    agent, tool, _, _, _, _, _, case, item = board
    with caller(agent, task) as context:
        return tool.execute_scoped({"action": action, "case_id": case.case_id, "item_id": item.item_id,
                                    **extra}, context)


def submitted(board):
    _, _, _, _, recipient, _, _, _, _ = board
    assert invoke(board, recipient, "record_ack", frozen_input_version="sha256:frozen").ok
    result = invoke(board, recipient, "submit_handoff", frozen_input_version="sha256:frozen", evidence_ids=["sha256:evidence"])
    assert result.ok, result.output
    return result.result_envelope["item"]


def test_leaf_and_grandchild_explicit_handoff_keeps_no_child_controls(board):
    agent, _, _, _, recipient, _, _, _, _ = board
    assert "manage_handoff_board" in recipient.allowed_tools
    assert leaf_allowed_tools(["manage_handoff_board", "create_subagents", "cancel_subagents"]) == ["manage_handoff_board"]
    spec = type("Spec", (), {"allowed_tools": ["manage_handoff_board"], "role": "worker", "attributes": {}})()
    tools = scheduled_child_tools(ToolPolicyRequest(parent_tools=["read_file", "manage_handoff_board", "create_subagents"],
                                                     spec=spec, extra_write_roots=[], role="worker"))
    assert "manage_handoff_board" in tools and "create_subagents" not in tools
    # grant 应用后的列表仍保留交接参与权，但不能捎带创建权限。
    current = agent.subagents.load(recipient.id)
    current.allowed_tools += ["create_subagents"]
    from agent_py_agent.agent.subagents.role_contracts import _allowed_tools_for_role
    from agent_py_agent.agent.subagents.role_templates import template_for_role
    assert _allowed_tools_for_role("worker", current.allowed_tools, template_for_role("worker")) == ["read_file", "manage_handoff_board"]
    inherited = scheduled_child_tools(ToolPolicyRequest(parent_tools=["read_file"], spec=spec,
                                                         extra_write_roots=[], role="worker"))
    assert "manage_handoff_board" not in inherited  # 角色不增加父级没有的工具权限。
    from agent_py_agent.agent.subagents.services.runner_context_service import _runner_allowed_tools
    current.effective_permissions = {"disabled_tools": ["manage_handoff_board"]}
    assert "manage_handoff_board" not in _runner_allowed_tools(current, current.allowed_tools)


@pytest.mark.parametrize("action", ["open_case", "publish_item", "cancel", "sync_external_mailbox", "list_cases"])
def test_leaf_cannot_manage_board_or_enumerate_owner(board, action):
    *_, recipient, other, approver, case, item = board
    outcome = invoke(board, recipient, action)
    assert not outcome.ok and outcome.effect_outcome == "not_started"
    assert "HANDOFF_LEAF_ACTION_FORBIDDEN" in outcome.output
    assert board[2].store.load_handoff_item(case.case_id, item.item_id).state == "published"


def test_exact_recipient_ack_submit_and_authorized_sibling_accept(board):
    _, _, _, _, recipient, other, approver, _, _ = board
    denied = invoke(board, other, "record_ack", frozen_input_version="sha256:frozen")
    assert not denied.ok and "HANDOFF_RECIPIENT_MISMATCH" in denied.output
    row = submitted(board)
    denied = invoke(board, other, "decide_acceptance", verdict="accepted",
                    frozen_input_version=row["frozen_input_version"], submission_digest=row["metadata"]["submission_digest"])
    assert not denied.ok and "HANDOFF_ACCEPTANCE_ROLE_REQUIRED" in denied.output
    accepted = invoke(board, approver, "decide_acceptance", verdict="accepted", reviewer_principal=recipient.id,
                      frozen_input_version=row["frozen_input_version"], submission_digest=row["metadata"]["submission_digest"])
    assert accepted.ok, accepted.output
    vote = accepted.result_envelope["item"]["acceptance_verdict"]
    assert vote["reviewer_principal"] == approver.id
    assert vote["submission_digest"] == row["metadata"]["submission_digest"]
    assert vote["frozen_input_version"] == row["frozen_input_version"]


def test_leaf_read_needs_exact_ref_and_same_root(board):
    agent, _, service, _, recipient, other, _, case, item = board
    assert invoke(board, other, "list_items").ok  # 同任务显式授予的审核读口不丢失。
    denied = invoke(board, other, "list_items", item_id="")
    assert not denied.ok and "HANDOFF_ITEM_REFERENCE_REQUIRED" in denied.output
    foreign = agent.subagents.create_run(goal="foreign", thought="test", plan=["read"], role="worker")
    denied = invoke(board, foreign, "list_items")
    assert not denied.ok and "HANDOFF_TASK_SCOPE_MISMATCH" in denied.output
    assert service.store.load_handoff_item(case.case_id, item.item_id).state == "published"


def test_scope_read_failure_identifies_record_without_exposing_exception_body(board, monkeypatch):
    agent, _, service, _, recipient, other, _, case, item = board
    original = agent.subagents.load
    def load(run_id):
        if run_id == recipient.id:
            raise OSError("private record body must not be exposed")
        return original(run_id)
    monkeypatch.setattr(agent.subagents, "load", load)
    outcome = invoke(board, other, "list_items")
    assert not outcome.ok and outcome.effect_outcome == "not_started"
    assert outcome.result_envelope["details"]["source"] == {"record": "recipient_task", "run_id": recipient.id}
    assert "OSError" in outcome.output and "private record body" not in outcome.output
    assert service.store.load_handoff_item(case.case_id, item.item_id).state == "published"


def test_omitted_bindings_cannot_sign_new_vote_but_can_read_original_vote(board):
    _, _, service, _, _, _, approver, case, item = board
    row = submitted(board)
    denied = invoke(board, approver, "decide_acceptance", verdict="accepted")
    assert not denied.ok and "HANDOFF_REVIEW_BINDING_REQUIRED" in denied.output
    assert service.store.load_handoff_item(case.case_id, item.item_id).state == "submitted"
    accepted = invoke(board, approver, "decide_acceptance", verdict="accepted", frozen_input_version=row["frozen_input_version"],
                      submission_digest=row["metadata"]["submission_digest"])
    assert accepted.ok
    before = service.store._handoff_items_path(case.case_id).read_bytes()
    replay = invoke(board, approver, "decide_acceptance", verdict="accepted")
    assert replay.ok and replay.result_envelope["existing_vote_reused_without_new_signature"]
    assert service.store._handoff_items_path(case.case_id).read_bytes() == before
    explicit = invoke(board, approver, "decide_acceptance", verdict="accepted",
                      frozen_input_version=row["frozen_input_version"], submission_digest=row["metadata"]["submission_digest"])
    assert explicit.ok and service.store._handoff_items_path(case.case_id).read_bytes() == before


def test_rework_between_read_and_vote_cannot_accept_stale_submission(board):
    _, _, service, _, recipient, _, approver, case, item = board
    row = submitted(board)
    read_done, changed = Event(), Event()
    failures = []
    def rework():
        try:
            assert read_done.wait(3)
            service.decide_acceptance(case_id=case.case_id, item_id=item.item_id, verdict="changes_requested",
                reviewer_principal=approver.id, frozen_input_version=row["frozen_input_version"],
                submission_digest=row["metadata"]["submission_digest"])
            service.submit_handoff(case_id=case.case_id, item_id=item.item_id, evidence_ids=["sha256:new-evidence"])
        except BaseException as exc:
            failures.append(exc)
        finally:
            changed.set()
    worker = Thread(target=rework)
    worker.start()
    read_done.set()
    assert changed.wait(3)
    worker.join(3)
    assert not failures and not worker.is_alive()
    with pytest.raises(IdempotencyConflictError):
        service.decide_acceptance(case_id=case.case_id, item_id=item.item_id, verdict="accepted", reviewer_principal=approver.id,
            frozen_input_version=row["frozen_input_version"], submission_digest=row["metadata"]["submission_digest"])
    assert service.store.load_handoff_item(case.case_id, item.item_id).state == "submitted"
    with pytest.raises(HandoffError):
        service.decide_acceptance(case_id=case.case_id, item_id=item.item_id, verdict="accepted", reviewer_principal=recipient.id)
