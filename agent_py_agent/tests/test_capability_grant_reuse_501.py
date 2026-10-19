"""能力授权复用只消费同canonical task真实grant；已明确工作目录不被owner墙误拒。"""
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.core import ResolveCapabilityRequestsTool, SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.capability_scope import partition_capability_paths_by_owner
from agent_py_agent.agent.subagents.services.lifecycle import (
    RecordCapabilityGrantParams,
    RecordCapabilityRequestParams,
)
from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation


def test_granted_exact_scope_does_not_need_reapproval_after_wording_changes(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_plugins=False), tmp_path / "project")
    task = agent.subagents.create_run(goal="合成文件任务", thought="离线验证", plan=["写文件"], allowed_tools=["write_file"])
    target = str(Path(task.task_workspace_dir) / "output")
    first = agent.subagents.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
        problem="first description", needed_capability="write initial output", capability_type="filesystem",
        requested_tools=["write_file"], path_scope=[target]))
    agent.subagents.lifecycle.record_capability_grant(task.id, RecordCapabilityGrantParams(
        request_id=first.id, tools=["write_file"], path_scope=[target]))
    second = agent.subagents.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
        problem="new description", needed_capability="continue writing same output", capability_type="filesystem",
        requested_tools=["write_file"], path_scope=[target]))
    assert second.id == first.id and second.status == "GRANTED", (first.id, second.id, second.status)


def test_explicit_full_parent_work_root_can_be_granted_without_false_owner_rejection(tmp_path):
    project = tmp_path / "explicit-project"
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_plugins=False,
        access_mode="full-access", path_access_mode="full"), project)
    execute_approval_mode_operation(agent.home_paths, "set", "full-access")
    task = agent.subagents.create_run(goal="合成项目写任务", thought="离线验证", plan=["申请已声明目录"],
        allowed_tools=["read_file"], extra_write_roots=[str(project)])
    request = agent.subagents.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
        problem="need write permission", needed_capability="write output", capability_type="filesystem",
        requested_tools=["write_file"], path_scope=[str(project)]))
    result = ResolveCapabilityRequestsTool(agent).execute({"run_id": task.id, "request_id": request.id,
        "decision": "grant", "reason": "明确当前项目目录"})
    assert result.ok, result.output
    assert agent.subagents.load(task.id).capability_requests[0].status == "GRANTED"



# LLM: 所有grant均经真实lifecycle事务落盘，只在独立临时owner内构造请求，不运行工具或模型。
# 函数用途: 创建一条有实际授权的canonical请求，返回同范围申请参数供正反覆盖复用。
def grant_case(tmp_path, *, command=False):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), enable_plugins=False), tmp_path / "project")
    task = agent.subagents.create_run(goal="合成授权覆盖", thought="离线回归", plan=["操作获批范围"], allowed_tools=["write_file"])
    root = str(Path(task.task_workspace_dir) / "output")
    params = RecordCapabilityRequestParams(problem="first", needed_capability="first wording", capability_type="shell" if command else "filesystem",
        requested_tools=["run_command"] if command else ["write_file"], path_scope=[root],
        requested_commands=["python -V"] if command else [])
    request = agent.subagents.lifecycle.record_capability_request(task.id, params)
    grant = agent.subagents.lifecycle.record_capability_grant(task.id, RecordCapabilityGrantParams(request_id=request.id,
        grant_type=params.capability_type, tools=params.requested_tools, path_scope=[root],
        command_allowlist=["python"] if command else [],
        request_scope={"requested_commands": params.requested_commands} if command else {}))
    return agent, task, params, request, grant


def test_subpath_and_output_name_change_inside_granted_directory_reuses(tmp_path):
    agent, task, params, request, _ = grant_case(tmp_path)
    new = replace(params, needed_capability="other wording", path_scope=[str(Path(params.path_scope[0]) / "new-output.txt")])
    second = agent.subagents.lifecycle.record_capability_request(task.id, new)
    assert second.id == request.id and second.status == "GRANTED"
    assert len(agent.subagents.load(task.id).capability_requests) == 1


@pytest.mark.parametrize("changes", [
    {"requested_tools": ["edit_file"]}, {"requested_mcp_tools": ["mcp__other__action"]},
    {"requested_skills": ["new-skill"]}, {"requested_commands": ["python -V"]},
    {"network_scope": ["https://new.invalid"]}, {"output_budget": {"max_chars": 200}},
    {"capability_type": "shell"}, {"constraints": {"execution": "different"}},
])
def test_new_structural_capability_is_not_covered_by_old_directory_grant(tmp_path, changes):
    agent, task, params, request, _ = grant_case(tmp_path)
    second = agent.subagents.lifecycle.record_capability_request(task.id, replace(params, **changes))
    assert second.id != request.id and second.status == "OPEN"


@pytest.mark.parametrize("field", ["path_scope", "cwd_scope"])
def test_directory_outside_actual_grant_remains_new_request(tmp_path, field):
    agent, task, params, request, _ = grant_case(tmp_path)
    changes = {field: [str(tmp_path / "outside-grant")]}
    second = agent.subagents.lifecycle.record_capability_request(task.id, replace(params, **changes))
    assert second.id != request.id and second.status == "OPEN"


@pytest.mark.parametrize("text", ["python -m http.server", "python -V && echo extra", "python -c 'print(1)'"])
def test_executable_name_match_does_not_reuse_different_command_grant(tmp_path, text):
    agent, task, params, request, _ = grant_case(tmp_path, command=True)
    second = agent.subagents.lifecycle.record_capability_request(task.id, replace(params, requested_commands=[text]))
    assert second.id != request.id and second.status == "OPEN"


def test_identical_complete_command_and_directory_reuses_actual_grant(tmp_path):
    agent, task, params, request, _ = grant_case(tmp_path, command=True)
    second = agent.subagents.lifecycle.record_capability_request(task.id, replace(params, needed_capability="continue same exact command"))
    assert second.id == request.id and second.status == "GRANTED"


@pytest.mark.parametrize("wording_change", [False, True])
@pytest.mark.parametrize("change", ["expired", "bad_deadline", "wrong_run", "missing_grant", "missing_tools", "missing_scope", "revoked_tools", "terminal"])
def test_missing_expired_or_different_actual_authority_does_not_reuse(tmp_path, change, wording_change):
    agent, task, params, request, _ = grant_case(tmp_path)
    def alter(current):
        if change == "expired":
            current.capability_grants[0].expires_at = time.time()-1
        elif change == "bad_deadline":
            current.capability_grants[0].expires_at = float("nan")
        elif change == "wrong_run":
            current.capability_grants[0].grant_to_run_id = "other-run"
        elif change == "missing_grant":
            current.capability_grants = []
        elif change == "missing_tools":
            current.capability_grants[0].tools = []
        elif change == "missing_scope":
            current.capability_grants[0].path_scope = []
        elif change == "revoked_tools":
            current.allowed_tools = []
        elif change == "terminal":
            current.status = "DONE"
    agent.subagents.mutate(task.id, alter)
    second = agent.subagents.lifecycle.record_capability_request(task.id, replace(params, needed_capability="different wording") if wording_change else params)
    assert second.id != request.id and second.status == "OPEN"


def test_other_task_and_owner_paths_cannot_become_declared_work_roots(tmp_path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    agent, task, _, _, _ = grant_case(tmp_path)
    foreign = resolve_owner_home(agent.home_paths.root, OwnerIdentity.provider_user("feishu", "other")).home_dir
    def alter(current):
        current.allowed_write_roots.extend([str(foreign), str(agent.home_paths.root / "config"), "/"])
    task = agent.subagents.mutate(task.id, alter)
    targets = [str(foreign / "out.txt"), str(agent.home_paths.root / "config" / "fake.yaml"), str(tmp_path / "unrelated")]
    result = partition_capability_paths_by_owner(agent.subagents, task, targets)
    assert result.allowed_paths == () and set(result.rejected_paths) == set(targets)


def test_symlink_outside_granted_directory_is_not_covered(tmp_path):
    agent, task, params, request, _ = grant_case(tmp_path)
    root = Path(params.path_scope[0])
    root.mkdir(exist_ok=True)
    external = tmp_path / "external"
    external.mkdir()
    (root / "escape").symlink_to(external, target_is_directory=True)
    second = agent.subagents.lifecycle.record_capability_request(task.id,
        replace(params, path_scope=[str(root / "escape" / "out.txt")]))
    assert second.id != request.id and second.status == "OPEN"


@pytest.mark.parametrize("field,value", [("network_scope", ["https://offline.invalid"]),
    ("output_budget", {"max_chars": 100}), ("constraints", {"execution": "bounded"})])
@pytest.mark.parametrize("persisted", [False, True])
def test_actual_grant_must_preserve_nonempty_structured_fields(tmp_path, field, value, persisted):
    agent, task, base, _, _ = grant_case(tmp_path)
    params = replace(base, **{field: value})
    request = agent.subagents.lifecycle.record_capability_request(task.id, params)
    grant_fields = {field: value} if persisted else {}
    agent.subagents.lifecycle.record_capability_grant(task.id, RecordCapabilityGrantParams(request_id=request.id,
        tools=params.requested_tools, path_scope=params.path_scope, **grant_fields))
    second = agent.subagents.lifecycle.record_capability_request(task.id, params)
    assert (second.id == request.id) == persisted
    assert second.status == ("GRANTED" if persisted else "OPEN")


@pytest.mark.parametrize("change", ["missing_full_commands", "different_full_commands", "missing_executable"])
def test_executable_only_or_incomplete_command_grant_is_not_reused(tmp_path, change):
    agent, task, params, request, _ = grant_case(tmp_path, command=True)
    def alter(current):
        grant = current.capability_grants[0]
        if change == "missing_full_commands":
            grant.request_scope = {}
        elif change == "different_full_commands":
            grant.request_scope = {"requested_commands": ["python -m http.server"]}
        else:
            grant.command_allowlist = []
    agent.subagents.mutate(task.id, alter)
    second = agent.subagents.lifecycle.record_capability_request(task.id, params)
    assert second.id != request.id and second.status == "OPEN"


@pytest.mark.parametrize("state", ["valid", "expired", "missing"])
def test_structured_result_consumer_uses_same_canonical_grant_rule(tmp_path, state):
    from dataclasses import asdict
    from types import SimpleNamespace

    from agent_py_agent.agent.subagents.result_structured import (
        _create_capability_requests_from_parsed,
    )

    agent, task, params, request, _ = grant_case(tmp_path)
    result = []
    def consume(current):
        if state == "expired":
            current.capability_grants[0].expires_at = time.time()-1
        elif state == "missing":
            current.capability_grants = []
        parsed = SimpleNamespace(capability_requests=[asdict(replace(params, needed_capability="new wording"))])
        result.append(_create_capability_requests_from_parsed(current, parsed, time.time()))
    agent.subagents.mutate(task.id, consume)
    count, ids = result[0]
    assert count == 1 and len(ids) == 1
    assert (ids[0] == request.id) == (state == "valid")


def test_open_same_request_still_reuses_original_pending(tmp_path):
    agent, task, params, _, _ = grant_case(tmp_path, command=True)
    def clear(current):
        current.capability_requests = []
        current.capability_grants = []
    agent.subagents.mutate(task.id, clear)
    first = agent.subagents.lifecycle.record_capability_request(task.id, params)
    second = agent.subagents.lifecycle.record_capability_request(task.id, params)
    assert second.id == first.id and second.status == "OPEN"


@pytest.mark.parametrize("kind", ["user", "group"])
def test_scoped_ordinary_owner_does_not_gain_host_or_foreign_roots(tmp_path, kind):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    from agent_py_agent.tests.test_ingestion_secret_scope import _agent

    agent = _agent(tmp_path, kind)
    task = agent.subagents.create_run(goal="合成普通主体范围", thought="离线边界回归", plan=["检查既有目录"])
    foreign = resolve_owner_home(agent.home_paths.root, OwnerIdentity.provider_user("feishu", "other")).home_dir
    targets = [str(tmp_path / "outside"), str(foreign / "private.txt"), str(agent.home_paths.root / "config")]
    result = partition_capability_paths_by_owner(agent.subagents, task, targets)
    assert not result.allowed_paths and set(result.rejected_paths) == set(targets)


@pytest.mark.parametrize("ancestor", [False, True])
def test_replaced_grant_root_or_ancestor_does_not_rebind_old_grant(tmp_path, ancestor):
    agent, task, params, request, _ = grant_case(tmp_path)
    parent = Path(params.path_scope[0])
    parent.mkdir(exist_ok=True)
    if ancestor:
        target_root = parent / "nested"
        target_root.mkdir()
        params = replace(params, path_scope=[str(target_root)])
        request = agent.subagents.lifecycle.record_capability_request(task.id, params)
        # 原大目录许可先退役，再通过正式写入口登记更窄的canonical根。
        def clear(current):
            current.capability_requests = []
            current.capability_grants = []
        agent.subagents.mutate(task.id, clear)
        request = agent.subagents.lifecycle.record_capability_request(task.id, params)
        agent.subagents.lifecycle.record_capability_grant(task.id, RecordCapabilityGrantParams(
            request_id=request.id, tools=params.requested_tools, path_scope=params.path_scope))
    changed = parent
    changed.rename(changed.with_name("old-output"))
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    if ancestor:
        (replacement / "nested").mkdir()
    changed.symlink_to(replacement, target_is_directory=True)
    second = agent.subagents.lifecycle.record_capability_request(task.id,
        replace(params, path_scope=[str(Path(params.path_scope[0]) / "out.txt")]))
    assert second.id != request.id and second.status == "OPEN"


@pytest.mark.parametrize("target_kind", ["foreign", "config", "secrets", "plain"])
def test_declared_ancestor_root_cannot_hide_host_or_foreign_target(tmp_path, target_kind):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    agent, task, _, _, _ = grant_case(tmp_path)
    # 本task原有明确工作根恰好包含宿主home，目标也必须独立通过同一过滤。
    def declare(current):
        current.allowed_write_roots.append(str(tmp_path))
    current = agent.subagents.mutate(task.id, declare)
    foreign = resolve_owner_home(agent.home_paths.root, OwnerIdentity.provider_user("feishu", "foreign")).home_dir
    target = {"foreign": foreign / "private.txt", "config": agent.home_paths.root / "config" / "fake.yaml",
        "secrets": agent.home_paths.root / "secrets" / "fake.txt", "plain": tmp_path / "plain-work" / "out.txt"}[target_kind]
    decision = partition_capability_paths_by_owner(agent.subagents, current, [str(target)])
    assert bool(decision.allowed_paths) == (target_kind == "plain")
    assert bool(decision.rejected_paths) == (target_kind != "plain")


@pytest.mark.parametrize("mcp", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_grant_reuse_tracks_actual_runner_disabled_tool_projection(tmp_path, mcp, disabled):
    agent, task, params, request, _ = grant_case(tmp_path)
    name = "mcp__offline__act" if mcp else "write_file"
    if mcp:
        params = replace(params, requested_tools=[], requested_mcp_tools=[name])
        request = agent.subagents.lifecycle.record_capability_request(task.id, params)
        agent.subagents.lifecycle.record_capability_grant(task.id, RecordCapabilityGrantParams(
            request_id=request.id, mcp_tools=[name], path_scope=params.path_scope))
    if disabled:
        def revoke(current):
            current.effective_permissions = {**current.effective_permissions, "disabled_tools": [name]}
        agent.subagents.mutate(task.id, revoke)
    current = agent.subagents.load(task.id)
    assert name in current.allowed_tools
    actual = agent.subagents.runner_context.build_execution_context(task.id)
    assert (name in actual.allowed_tools) == (not disabled)
    second = agent.subagents.lifecycle.record_capability_request(task.id, params)
    assert (second.id == request.id) == (not disabled)
    assert second.status == ("OPEN" if disabled else "GRANTED")
