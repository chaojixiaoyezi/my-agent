"""运行时工作根与子代理谱系回归。"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import path_access_policy
from agent_py_agent.agent.agent_core.orchestration.create_payload import _task_payload
from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.authority import CONVERSATION_TASK_TURN_ACTIVE_ATTR
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import request_context, request_execution
from agent_py_agent.agent.gateway_parts.io import write_gateway_request
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_worker import claim_request
from agent_py_agent.agent.subagents.capability_scope import partition_capability_paths_by_owner
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.tooling._filesystem_read import FileSystemAccessOptions
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call


def _fixture_agent(tmp_path):
    owner = tmp_path / "owner"
    granted = tmp_path / "project"
    owner.mkdir()
    granted.mkdir()
    manager = SubAgentManager(owner)
    agent = SimpleNamespace(
        subagents=manager,
        home_paths=SimpleNamespace(owner_home_dir=owner, owner_id="local/main"),
        tools=SimpleNamespace(owner_scope_root=str(owner)),
        _current_run_params=RunParams(
            source="gateway",
            request_id="new-request",
            run_id="new-run",
            attempt_id="new-attempt",
            write_boundary={"allowed_write_roots": [str(granted)]},
            task_attributes={"conversation_task_id": "old-task"},
        ),
    )
    freeze = getattr(path_access_policy, "freeze_delegable_scope", None)
    if freeze is not None:
        agent._current_run_params.delegable_scope = freeze(
            agent._current_run_params.write_boundary, owner,
            owner_id="local/main", task_id="", run_id="new-run",
        )
    return agent, owner, granted


def test_main_turn_granted_root_reaches_real_child_write(tmp_path):
    agent, owner, granted = _fixture_agent(tmp_path)
    child = agent.subagents.create_run(params=create_run_params(agent, {}, "整理材料", ["write_file"]))
    boundary = agent.subagents.runner_context._build_write_boundary(child)
    target = granted / "sub" / "result.txt"
    target.parent.mkdir()
    tool = WriteFileTool(owner, options=WriteFileToolOptions(
        access_options=FileSystemAccessOptions(owner_scope_root=str(owner)),
    ))
    outcome = execute_canonical_test_call(
        owner, tools={"write_file": tool}, tool_name="write_file",
        arguments={"path": str(target), "content": "delegated"},
        write_boundary=boundary, owner_scope_root=str(owner), operation_owner_id="local/main",
    )
    assert target.exists(), repr(outcome.result)
    assert target.read_text() == "delegated"


def test_create_and_shell_share_one_path_decision(tmp_path):
    agent, owner, granted = _fixture_agent(tmp_path)
    target = granted / "sub" / "result.txt"
    scope = getattr(agent._current_run_params, "delegable_scope", None)
    if scope is None:
        child = create_run_params(agent, {}, "整理材料", ["write_file"])
        old_create = any(target.is_relative_to(root) for root in child.extra_write_roots)
        old_shell = path_access_policy.granted_external_work_roots(agent._current_run_params.write_boundary, owner)
        assert old_create == any(target.resolve().is_relative_to(root) for root in old_shell)
    else:
        checks = [path_access_policy.delegable_path_decision(scope, target, purpose=purpose)
                  for purpose in ("create", "grant", "write", "shell")]
        assert all(item == checks[0] and item.allowed for item in checks)


def test_independent_followup_records_current_request_lineage(tmp_path):
    agent, _, _ = _fixture_agent(tmp_path)
    task = agent.subagents.create_run(params=create_run_params(agent, {}, "整理材料", ["write_file"]))
    assert task.initiating_request_id == "new-request"
    assert task.parent_id == "new-run"
    assert task.root_id == "new-run"
    assert not task.attributes.get("conversation_task_id")


def test_replaced_directory_is_refused_after_admission(tmp_path):
    agent, owner, granted = _fixture_agent(tmp_path)
    scope = getattr(agent._current_run_params, "delegable_scope", None)
    granted.rename(tmp_path / "original")
    granted.mkdir()
    target = granted / "sub" / "result.txt"
    if scope is None:
        allowed = any(target.resolve().is_relative_to(root) for root in
                      path_access_policy.granted_external_work_roots(agent._current_run_params.write_boundary, owner))
    else:
        allowed = path_access_policy.delegable_path_decision(scope, target, purpose="write").allowed
    assert not allowed


def test_real_tool_revalidates_each_root_and_carries_decision_receipt(tmp_path):
    agent, owner, first = _fixture_agent(tmp_path)
    second = tmp_path / "second"
    second.mkdir()
    current = agent._current_run_params
    current.write_boundary["allowed_write_roots"].append(str(second))
    current.delegable_scope = path_access_policy.freeze_delegable_scope(
        current.write_boundary, owner, owner_id="local/main", task_id="task-a", run_id="new-run",
        permission_version="grant-v1",
    )
    child = agent.subagents.create_run(params=create_run_params(agent, {}, "整理材料", ["write_file"]))
    loaded = agent.subagents.load(child.id)
    boundary = agent.subagents.runner_context._build_write_boundary(loaded)

    def write(root, name):
        target = root / name
        tool = WriteFileTool(owner, options=WriteFileToolOptions(
            access_options=FileSystemAccessOptions(owner_scope_root=str(owner)),
        ))
        return execute_canonical_test_call(
            owner, tools={"write_file": tool}, tool_name="write_file",
            arguments={"path": str(target), "content": "ok"}, write_boundary=boundary,
            owner_scope_root=str(owner), operation_owner_id="local/main", run_id=child.id, call_id=name,
        ).result

    assert write(first, "before.txt").status == "succeeded"
    assert write(second, "before.txt").status == "succeeded"
    first.rename(tmp_path / "prior-first")
    first.mkdir()
    denied = write(first, "after.txt")
    assert denied.error_code == "PATH_OWNER_SCOPE_BLOCKED" and denied.handler_executed is False
    assert not (first / "after.txt").exists()
    assert write(second, "after.txt").status == "succeeded"
    assert denied.metadata["error_details"]["expected_relative_location"] == "after.txt"
    assert denied.metadata["error_details"]["staging_location"].endswith("/output")
    assert denied.metadata["error_details"]["parent_apply_tool"] == "assets.versions.accept"
    lineage = denied.metadata["action_decision"]["evidence"]["delegation_lineage"]
    assert lineage == {
        "owner_id": "local/main", "run_id": loaded.id, "root_run_id": "new-run", "parent_run_id": "new-run",
        "attempt_id": "test-attempt", "initiating_request_id": "new-request", "grant_revision": "grant-v1",
    }


@pytest.mark.parametrize("purpose", ("create", "grant", "write", "shell"))
def test_owner_aliases_siblings_and_symlinks_share_physical_boundary(tmp_path, purpose):
    agent, owner, project = _fixture_agent(tmp_path)
    scope = agent._current_run_params.delegable_scope
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)
    expected = path_access_policy.delegable_path_decision(scope, project / "f", purpose=purpose)
    assert expected.allowed
    assert path_access_policy.delegable_path_decision(scope, alias / "f", purpose=purpose) == expected
    if sys.platform == "darwin":
        with TemporaryDirectory(dir="/private/tmp") as scratch:
            physical = Path(scratch).resolve()
            alias_scope = path_access_policy.freeze_delegable_scope(
                {"allowed_write_roots": [str(physical)]}, owner,
                owner_id="local/main", task_id="alias-task", run_id="alias-run",
            )
            through_tmp = Path("/tmp") / physical.relative_to("/private/tmp") / "f"
            direct = path_access_policy.delegable_path_decision(alias_scope, physical / "f", purpose=purpose)
            assert direct.allowed
            assert path_access_policy.delegable_path_decision(alias_scope, through_tmp, purpose=purpose) == direct
    spill = tmp_path / "project-other"
    spill.mkdir()
    for destination in (spill, owner):
        link = project / destination.name
        link.symlink_to(destination, target_is_directory=True)
        assert not path_access_policy.delegable_path_decision(scope, link / "f", purpose=purpose).allowed
        assert not path_access_policy.in_delegation_owner_root(scope, link / "f")
    assert not path_access_policy.delegable_path_decision(scope, spill / "f", purpose=purpose).allowed
    invalid = path_access_policy.freeze_delegable_scope(
        {"allowed_write_roots": list(path_access_policy.UNINHERITABLE_ROOT_DIRS)}, owner,
        owner_id="local/main", task_id="task", run_id="run",
    )
    assert not invalid.approved_roots


def test_child_and_grandchild_keep_declared_subset_and_existing_path_grant(tmp_path):
    agent, owner, project = _fixture_agent(tmp_path)
    agent.conversation_store = ConversationStore(tmp_path / "conversation")
    agent.subagents.conversation_store = agent.conversation_store
    subset, sibling = project / "subset", project / "sibling"
    subset.mkdir()
    sibling.mkdir()
    child = agent.subagents.create_run(params=create_run_params(
        agent, {"extra_write_roots": [str(subset)]}, "整理材料", ["write_file"],
    ))
    loaded = agent.subagents.load(child.id)
    child_boundary = agent.subagents.runner_context._build_write_boundary(loaded)
    child_run = RunParams(write_boundary=child_boundary, task_id=child.id, run_id=child.id,
                          context_scope="task_local", attempt_id="child-attempt")
    assert child_run.delegable_scope.approved_root_ids == agent._current_run_params.delegable_scope.approved_root_ids
    assert child_run.delegable_scope.declared_roots == (subset.resolve(),)
    requested = [str(subset), str(sibling)]
    grant = partition_capability_paths_by_owner(agent.subagents, loaded, requested)
    assert grant.allowed_paths == (str(subset),)
    assert grant.rejected_paths == (str(sibling),)
    child_source = agent._current_run_params
    agent._current_run_params = child_run
    grandchild = agent.subagents.create_run(params=create_run_params(
        agent, {"extra_write_roots": requested, "parent_id": child.id, "root_id": child.root_id},
        "整理第二部分", ["write_file"],
    ))
    assert str(subset) in grandchild.allowed_write_roots and str(sibling) not in grandchild.allowed_write_roots
    assert grandchild.initiating_request_id == child_source.request_id
    refusal = _task_payload(grandchild)["delegation_path_refusals"][0]
    assert refusal["staging_location"] == grandchild.output_dir and refusal["staging_required"] is True
    subset.rename(project / "previous")
    subset.symlink_to(sibling, target_is_directory=True)
    assert not path_access_policy.delegable_path_decision(child_run.delegable_scope, subset / "f", purpose="write").allowed


def test_genuine_followup_keeps_conversation_task(tmp_path):
    agent, _, _ = _fixture_agent(tmp_path)
    agent._current_run_params.task_attributes[CONVERSATION_TASK_TURN_ACTIVE_ATTR] = True
    task = agent.subagents.create_run(params=create_run_params(agent, {}, "整理材料", ["write_file"]))
    assert task.parent_id == task.root_id == "old-task"
    assert task.attributes["conversation_task_id"] == "old-task"
    assert task.initiating_request_id == "new-request"


def test_gateway_admission_scope_is_reused_through_real_compact_retry(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_gateway_conversation_compact import _agent

    agent = _agent(tmp_path, context_tokens=30_000)
    project = tmp_path / "project"
    project.mkdir()
    request = {"id": "gw-scope", "prompt": "整理材料"}
    conversation = request_context.GatewayConversationContext(
        thread_id="thread-scope", cwd=str(project), runtime_workspace_roots=(str(project),),
    )
    context = request_context.GatewayAskRunContext(
        agent, request, tmp_path / "request.json", tmp_path / "response.json", request["id"], None,
    )
    seen = []

    def run(_prompt, *, params):
        seen.append(params)
        if len(seen) == 1:
            return SimpleNamespace(runtime_status="context_overflow")
        return SimpleNamespace(runtime_status="done")

    def compact(_context, _prompt, current, _params, _rows, *, observer=None):
        project.rename(tmp_path / "retired")
        project.mkdir()
        return replace(current, compact_generation=current.compact_generation + 1)

    monkeypatch.setattr(agent, "run", run)
    monkeypatch.setattr(request_execution, "_gateway_compact_overflowing_turn", compact)
    result, updated = request_execution._run_gateway_turn_with_conversation_compact(context, request["prompt"], conversation)
    assert result.runtime_status == "done" and updated.compact_generation == 1
    scope = seen[0].delegable_scope
    assert scope.approved_roots == (project.resolve(),)
    assert seen[1].delegable_scope is scope
    assert replace(seen[1], compact_auto_continue_depth=1).delegable_scope is scope
    assert not path_access_policy.delegable_path_decision(scope, project / "f", purpose="write").allowed


def test_gateway_restart_reads_original_scope_from_existing_authority_record(tmp_path):
    from agent_py_agent.tests.test_gateway_conversation_compact import _agent

    agent = _agent(tmp_path, context_tokens=30_000)
    project = tmp_path / "project"
    project.mkdir()
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.processing.mkdir(parents=True)
    request = {"id": "gw-scope", "goal": "整理材料"}
    path = write_gateway_request(paths, request)
    claim = claim_request(paths, path)
    assert claim is not None
    request.update(execution_attempt_id=claim.execution_attempt_id, lease_epoch=claim.lease_epoch)
    conversation = request_context.GatewayConversationContext(
        thread_id="thread-scope", cwd=str(project), runtime_workspace_roots=(str(project),),
    )
    context = request_context.GatewayAskRunContext(agent, request, claim.path, tmp_path / "result.json", request["id"], None)
    original = request_execution._gateway_run_params(request_execution._GatewayRunParamsRequest(
        request, context, conversation, request["goal"],
    ))
    assert original.conversation_task_binding_callback.bind_runtime_authority({
        "invocation_run_id": request["id"], "task_id": "task-a", "run_id": "run-a",
        "agent_run_id": "agent-run-a", "attempt_id": "attempt-a",
    })
    import json

    loaded = json.loads(claim.path.read_text())
    project.rename(tmp_path / "old-project")
    project.mkdir()
    recovered = request_execution._gateway_run_params(request_execution._GatewayRunParamsRequest(
        loaded, replace(context, request=loaded), conversation, request["goal"],
    ))
    assert recovered.delegable_scope == original.delegable_scope
    assert not path_access_policy.delegable_path_decision(recovered.delegable_scope, project / "f", purpose="write").allowed


def test_dispatch_refuses_root_replaced_after_preflight(tmp_path):
    agent, owner, project = _fixture_agent(tmp_path)
    child = agent.subagents.create_run(params=create_run_params(agent, {}, "整理材料", ["write_file"]))
    boundary = agent.subagents.runner_context._build_write_boundary(child)
    tool = WriteFileTool(owner, options=WriteFileToolOptions(
        access_options=FileSystemAccessOptions(owner_scope_root=str(owner)),
    ))

    def replace_after_policy(_call):
        project.rename(tmp_path / "old")
        project.mkdir()
        return None

    outcome = execute_canonical_test_call(
        owner, tools={"write_file": tool}, tool_name="write_file",
        arguments={"path": str(project / "f"), "content": "data"}, write_boundary=boundary,
        owner_scope_root=str(owner), operation_owner_id="local/main", pre_handler_gate=replace_after_policy,
    )
    assert outcome.decision.allowed and outcome.result.handler_executed is False
    assert outcome.result.error_code == "PATH_OWNER_SCOPE_BLOCKED" and not (project / "f").exists()


def test_shell_working_dir_revalidates_the_same_admitted_root(tmp_path):
    agent, owner, project = _fixture_agent(tmp_path)
    child = agent.subagents.create_run(params=create_run_params(agent, {}, "整理材料", ["run_command"]))
    boundary = agent.subagents.runner_context._build_write_boundary(child)
    shell = ShellTool(owner, options=ShellToolOptions(owner_scope_root=str(owner)))
    project.rename(tmp_path / "retired")
    project.mkdir()
    outcome = execute_canonical_test_call(
        owner, tools={"run_command": shell}, tool_name="run_command",
        arguments={"command": "pwd", "working_dir": str(project)}, write_boundary=boundary,
        owner_scope_root=str(owner), operation_owner_id="local/main",
    ).result
    assert outcome.error_code == "PATH_OWNER_SCOPE_BLOCKED" and outcome.handler_executed is False
    assert outcome.metadata["error_details"]["staging_required"] is True
