"""原派工的待验证模型建议：真实配置与 canonical thread，假 Jev；本片不会采用建议模型。"""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import hierarchy_tools, orchestration_tools
from agent_py_agent.agent.agent_core.orchestration import decision_subagent as selection
from agent_py_agent.agent.agent_core.orchestration.decision_subagent import (
    _input_budget as unprojected_input_budget,
)
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.backends.typesafe_decision_wire import parse_typesafe_response
from agent_py_agent.agent.conversation import decision_model_call as calls
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.settings.thread_model_selection import SUBAGENT_MODEL_ADVICE_KEY
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at, patch
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_orchestration_create_subagents_items import _agent


# LLM: 配置、manager/thread 持久化真实；只替换模型网络/发布，guard_depth 证明 Jev 锁外，完整请求容量保持生产未知。
# 函数用途: 验证权限、身份、pending 绑定和显式优先，不能据此宣称自动改选已闭合。
@pytest.fixture
def prepared(tmp_path, monkeypatch):
    host = host_at(tmp_path)
    a, _ = add(host, model_name="small", model_context_window_tokens=250_000)
    b, _ = add(host, model_name="large", model_context_window_tokens=1_000_000)
    key, _ = decision(host)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    patch(host, {"enabled": True, "profile_id": key, "points.subagent_model.mode": "apply"})
    agent = _agent()
    agent.config = host.config
    agent.config.enable_subagents = True
    agent.config.max_subagents = 10
    agent.config.enable_tools = False
    agent.config.max_tokens = 4096
    agent.config.model_context_window_tokens = 1_000_000
    agent.home_paths = host.home_paths
    agent.backend = host.backend
    agent.conversation_store = host.conversation_store
    agent.capability_config = host.capability_config
    agent._model_profile_backends = {}
    agent._current_run_params = RunParams(request_id="request", run_id="", task_id="",
        task_attributes={"conversation_thread_id": thread.thread_id})
    agent.subagents = SubAgentManager(tmp_path / "subagents", workspace_root=tmp_path)
    agent.subagents.conversation_store = host.conversation_store
    manager_guard = agent.subagents.creation_guard
    depth = [0]

    @contextmanager
    def guarded():
        with manager_guard():
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1

    agent.subagents.creation_guard = guarded
    published = SimpleNamespace(auto_start={"status": "not_attempted"}, conversation_bind_errors=[])
    monkeypatch.setattr(orchestration_tools, "publish_created_subagents", lambda _: published)
    monkeypatch.setattr(hierarchy_tools, "publish_created_subagents", lambda _: published)
    return agent, (a, b), depth


# LLM: 只读 canonical child thread，不从 task attrs 或测试内存推断建议；读取本身不采用。
# 函数用途: 取得真实落盘的 pending/retained 元数据，用来验证幂等、恢复和显式选择。
def advice(agent, task):
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.model_profile_id == task.attributes["host_model_profile.v1"]["profile_id"]
    assert SUBAGENT_MODEL_ADVICE_KEY not in task.attributes
    assert "host_model_decision.v1" not in task.attributes
    return thread.metadata.get(SUBAGENT_MODEL_ADVICE_KEY)


# LLM: 响应走真实 wire 校验，未授权值和缺题仍按产品协议变成逐题错误；不跳过 service 的绑定复核。
# 函数用途: 安装一次假供应商，记录整批请求及 deadline，并验证网络边界未持创建锁。
def provider(monkeypatch, prepared, picks, *, action=None):
    agent, _models, depth = prepared
    seen = []

    def invoke(_agent, _params, request, backend, **kwargs):
        assert depth[0] == 0
        body = request.payload(backend.model_name)
        seen.append((request, body, kwargs))
        if action:
            action()
        choices = picks(body) if callable(picks) else picks
        return parse_typesafe_response(request, backend.model_name, {"model": backend.model_name,
            "answers": {str(key): {"type": "choice", "choice": value, "confidence": 1.0,
                "probabilities": {candidate: float(candidate == value) for candidate in body["questions"][str(key)]["criteria"]}}
                for key, value in choices.items()}})

    monkeypatch.setattr(calls, "invoke_decision_model_call", invoke)
    return seen


# LLM: 只读已创建的 canonical 记录；回执中的建议、内存对象或 mocked create_run 不能充当落盘证据。
# 函数用途: 从工具回执取得真实创建的子代理。
def created(agent, result):
    assert result.ok, result.output
    payload = json.loads(result.output)
    return [agent.subagents.load(run_id) for run_id in payload["created_run_ids"]]


def test_batch_selects_each_child_once_outside_guard_and_keeps_explicit(prepared, monkeypatch):
    agent, (a, b), _ = prepared
    seen = provider(monkeypatch, prepared, {"0": a, "1": b})
    tasks = created(agent, CreateSubagentsTool(agent).execute({"items": [
        {"goal": "检查甲"}, {"goal": "检查乙"}, {"goal": "检查丙", "model": a},
    ]}))
    assert [task.attributes["host_model_profile.v1"]["profile_id"] for task in tasks] == ["default", "default", a]
    assert [advice(agent, task)["profile_id"] for task in tasks[:2]] == [a, b]
    assert advice(agent, tasks[2]) is None
    assert len(seen) == 1 and set(seen[0][1]["questions"]) == {"0", "1"}
    assert a in seen[0][1]["questions"]["0"]["criteria"]  # 250K 候选可替换 1M 父窗口。
    assert isinstance(seen[0][2]["deadline"], float)
    assert "secret" not in json.dumps(seen[0][1])
    assert agent.config.model_context_window_tokens == 1_000_000


@pytest.mark.parametrize("explicit_model", [False, True])
def test_unknown_first_request_allows_only_pending_and_explicit_model_skips_jev(prepared, monkeypatch, explicit_model):
    agent, (a, _), _ = prepared
    monkeypatch.setattr(selection, "_input_budget", unprojected_input_budget)
    seen = provider(monkeypatch, prepared, {"0": a})
    params = {"goal": "检查材料", **({"model": a} if explicit_model else {})}
    tasks = created(agent, CreateSubagentsTool(agent).execute(params))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": a if explicit_model else "default"}
    if explicit_model:
        assert advice(agent, tasks[0]) is None
    else:
        assert advice(agent, tasks[0])["status"] == "pending"
        assert seen[0][1]["state"]["children"]["0"]["estimated_input_tokens"] is None
    assert len(seen) == (not explicit_model)


@pytest.mark.parametrize("creation_path", ["root", "child", "grandchild"])
def test_batch_commits_prepared_objects_after_parent_revision_updates(prepared, monkeypatch, creation_path):
    agent, (a, b), _ = prepared
    parent = agent.subagents.create_run(goal="父任务", allowed_tools=["create_subagents"])
    if creation_path == "grandchild":
        parent = agent.subagents.create_run(goal="递归父任务", allowed_tools=["create_subagents"],
            parent_id=parent.id, root_id=parent.root_id, depth=1)
    if creation_path != "root":
        agent._current_subagent_run_id = parent.id
        agent._current_run_params.run_id = parent.id
    service = agent.subagents.base_service
    original_prepare, original_commit = service.prepare_run, service.create_run
    runs, committed = [], []

    def prepare_once(**kwargs):
        run = original_prepare(**kwargs)
        runs.append(run)
        return run

    def commit_same(**kwargs):
        assert kwargs["prepared"] is not None
        task = original_commit(**kwargs)
        committed.append(task)
        return task

    monkeypatch.setattr(service, "prepare_run", prepare_once)
    monkeypatch.setattr(service, "create_run", commit_same)
    seen = provider(monkeypatch, prepared, {"0": a, "1": b}, action=lambda: (
        len(runs) == 2 or pytest.fail("网络前必须准备整批")))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"parent_id": parent.id, "root_id": parent.root_id,
        "items": [{"goal": "检查甲"}, {"goal": "检查乙"}]}))
    assert len(runs) == len(committed) == len(tasks) == 2 and len(seen) == 1
    assert all(task is run.task for task, run in zip(committed, runs, strict=True))
    assert [task.id for task in tasks] == [run.run_id for run in runs]
    assert [task.created_at for task in tasks] == [run.created_at for run in runs]
    assert all(task.parent_id == parent.id for task in tasks)
    assert all(task.root_id == parent.root_id for task in tasks)
    assert all(task.agent_thread_id == run.task.agent_thread_id for task, run in zip(tasks, runs, strict=True))
    assert set(agent.subagents.load(parent.id).child_ids) == {task.id for task in tasks}
    assert [task.attributes["host_model_profile.v1"]["profile_id"] for task in tasks] == ["default", "default"]
    assert [advice(agent, task)["profile_id"] for task in tasks] == [a, b]


@pytest.mark.parametrize("recursive", [False, True])
def test_reuse_discovered_after_jev_keeps_existing_task_and_discards_unpublished_preparation(prepared, monkeypatch, recursive):
    from agent_py_agent.tests.test_orchestration_create_subagents_idempotency import (
        _idempotency_pack,
    )

    agent, (a, _), _ = prepared
    if recursive:
        parent = agent.subagents.create_run(goal="父任务", allowed_tools=["create_subagents"])
        agent._current_subagent_run_id = parent.id
        agent._current_run_params.run_id = parent.id
    service = agent.subagents.base_service
    original = service.prepare_run
    runs, elsewhere = [], []

    def capture(**kwargs):
        run = original(**kwargs)
        runs.append(run)
        return run

    monkeypatch.setattr(service, "prepare_run", capture)
    seen = provider(monkeypatch, prepared, {"0": a}, action=lambda: elsewhere.append(
        agent.subagents.create_run(params=runs[0].params)))
    params = {"goal": "检查材料", "context_packs": [_idempotency_pack("inflight-reuse", "report")]}
    result = CreateSubagentsTool(agent).execute(params)
    assert result.ok, result.output
    assert json.loads(result.output)["reused_run_ids"] == [elsewhere[0].id]
    assert elsewhere[0].id != runs[0].run_id
    with pytest.raises(FileNotFoundError):
        agent.subagents.load(runs[0].run_id)
    assert agent.subagents.load(elsewhere[0].id).attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert "host_model_decision.v1" not in runs[0].task.attributes
    monkeypatch.setattr(service, "prepare_run", lambda **_: pytest.fail("复用任务不应分配准备身份"))
    assert CreateSubagentsTool(agent).execute(params).ok
    assert len(seen) == 1


@pytest.mark.parametrize("recursive", [False, True])
def test_owner_permission_change_invalidates_prepared_model_suggestion(prepared, monkeypatch, recursive):
    agent, (a, _), _ = prepared
    if recursive:
        parent = agent.subagents.create_run(goal="父任务", allowed_tools=["create_subagents", "read_file"])
        agent._current_subagent_run_id = parent.id
        agent._current_run_params.run_id = parent.id
    provider(monkeypatch, prepared, {"0": a}, action=lambda: setattr(
        agent.subagents, "owner_policy_snapshot", {"tools": {"disabled_tools": ["read_file"]}}))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料", "allowed_tools": ["read_file"]}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert "read_file" in tasks[0].effective_permissions["disabled_tools"]
    assert advice(agent, tasks[0]) is None


@pytest.mark.parametrize("recursive", [False, True])
def test_cancellation_between_prepared_children_leaves_only_committed_prefix(prepared, monkeypatch, recursive):
    from agent_py_agent.agent.common.cancellation import ToolCancelled

    agent, (a, _), _ = prepared
    if recursive:
        parent = agent.subagents.create_run(goal="父任务", allowed_tools=["create_subagents"])
        agent._current_subagent_run_id = parent.id
        agent._current_run_params.run_id = parent.id
    service = agent.subagents.base_service
    original = service.create_run
    committed = []

    def check():
        if committed:
            raise ToolCancelled("停止后续准备项")

    def commit_one(**kwargs):
        task = original(**kwargs)
        committed.append(task)
        return task

    monkeypatch.setattr(service, "create_run", commit_one)
    monkeypatch.setattr(orchestration_tools, "raise_if_cancelled", check)
    monkeypatch.setattr(hierarchy_tools, "raise_if_cancelled", check)
    seen = provider(monkeypatch, prepared, {"0": a, "1": a})
    result = CreateSubagentsTool(agent).execute({"items": [{"goal": "检查甲"}, {"goal": "检查乙"}]})
    assert not result.ok and result.error_code == "CANCELLED"
    assert len(committed) == len(seen) == 1
    children = [task for task in agent.subagents.list_runs() if not recursive or task.parent_id == parent.id]
    assert [task.id for task in children] == [committed[0].id]


def test_structured_candidate_scope_only_exposes_selected_profiles(prepared, monkeypatch):
    agent, (a, b), _ = prepared
    patch(agent, {"points.subagent_model.candidate_profile_ids": [a]})
    seen = provider(monkeypatch, prepared, {"0": a})
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert advice(agent, tasks[0])["profile_id"] == a
    criteria = seen[0][1]["questions"]["0"]["criteria"]
    assert a in criteria and b not in criteria


def test_candidate_scope_change_during_request_keeps_original(prepared, monkeypatch):
    agent, (a, b), _ = prepared
    patch(agent, {"points.subagent_model.candidate_profile_ids": [a]})
    provider(monkeypatch, prepared, {"0": a}, action=lambda: patch(
        agent, {"points.subagent_model.candidate_profile_ids": [b]}))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert advice(agent, tasks[0]) is None


def test_candidate_scope_change_before_request_skips_jev(prepared, monkeypatch):
    agent, (a, b), _ = prepared
    patch(agent, {"points.subagent_model.candidate_profile_ids": [a]})
    seen = provider(monkeypatch, prepared, {"0": a})
    original = selection.decide_subagent_models

    def changed_before_send(host, decision):
        patch(host, {"points.subagent_model.candidate_profile_ids": [b]})
        return original(host, decision)

    monkeypatch.setattr(orchestration_tools, "decide_subagent_models", changed_before_send)
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert not seen


def test_candidate_scope_check_propagates_user_cancel(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    seen = provider(monkeypatch, prepared, {"0": a})
    original = selection.execute_decision_settings_operation
    reads = [0]

    def interrupted_read(*args, **kwargs):
        reads[0] += 1
        if reads[0] == 2:
            raise InterruptedError("user stop")
        return original(*args, **kwargs)

    monkeypatch.setattr(selection, "execute_decision_settings_operation", interrupted_read)
    with pytest.raises(InterruptedError):
        CreateSubagentsTool(agent).execute({"goal": "检查材料"})
    assert reads[0] == 2 and not seen and not agent.subagents.list_runs()


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_off_and_observe_keep_original(prepared, monkeypatch, mode):
    agent, (a, _), _ = prepared
    patch(agent, {"points.subagent_model.mode": mode})
    seen = provider(monkeypatch, prepared, {"0": a})
    if mode == "off":
        monkeypatch.setattr(selection, "_candidates", lambda *_: pytest.fail("off cannot prepare candidates"))
        monkeypatch.setattr(selection, "operation_id", lambda *_: pytest.fail("off cannot create selection identity"))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert len(seen) == (mode == "observe")
    assert advice(agent, tasks[0]) is None


def test_explicit_invalid_model_fails_before_optional_network(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    seen = provider(monkeypatch, prepared, {"0": a})
    result = CreateSubagentsTool(agent).execute({"items": [{"goal": "甲"}, {"goal": "乙", "model": "missing"}]})
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert not agent.subagents.list_runs() and not seen


@pytest.mark.parametrize("choice", ["need_data", "not_needed", "no_match", "abstain", "retain_original", "foreign-model"])
def test_nonselection_and_bad_child_answer_do_not_discard_sibling(prepared, monkeypatch, choice):
    agent, (a, b), _ = prepared
    provider(monkeypatch, prepared, {"0": choice, "1": b})
    tasks = created(agent, CreateSubagentsTool(agent).execute({"items": [{"goal": "甲"}, {"goal": "乙"}]}))
    assert [task.attributes["host_model_profile.v1"]["profile_id"] for task in tasks] == ["default", "default"]
    assert advice(agent, tasks[0]) is None
    assert advice(agent, tasks[1])["profile_id"] == b


def test_authorized_candidate_removed_during_request_is_not_applied(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    provider(monkeypatch, prepared, {"0": a}, action=lambda: execute_model_profile_operation(agent, "delete_model", {"profile_id": a}))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert advice(agent, tasks[0]) is None


def test_provider_error_preserves_original_and_user_cancel_propagates(prepared, monkeypatch):
    agent, (a, _), _ = prepared

    def fail():
        raise ProviderTransientError("offline")

    provider(monkeypatch, prepared, {"0": a}, action=fail)
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    from agent_py_agent.agent.conversation import decision_policy
    with decision_policy._LOCK:
        decision_policy._FAILURES.clear()

    def cancel():
        raise InterruptedError("user stop")

    provider(monkeypatch, prepared, {"0": a}, action=cancel)
    with pytest.raises(InterruptedError):
        CreateSubagentsTool(agent).execute({"goal": "另一个任务"})
    assert len(agent.subagents.list_runs()) == 1


def test_unknown_external_inputs_remain_unverified_advice(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    seen = provider(monkeypatch, prepared, {"0": a})
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料", "input_refs": ["file:unknown.txt"]}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert advice(agent, tasks[0])["status"] == "pending"
    assert len(seen) == 1


def test_persisted_root_idempotency_replays_without_new_selection(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    from agent_py_agent.tests.test_orchestration_create_subagents_idempotency import (
        _idempotency_pack,
    )

    seen = provider(monkeypatch, prepared, {"0": a})
    params = {"goal": "检查材料", "context_packs": [_idempotency_pack("selection", "result")], "defer_start": True}
    first = created(agent, CreateSubagentsTool(agent).execute(params))
    replay = CreateSubagentsTool(agent).execute(params)
    assert replay.ok, replay.output
    assert json.loads(replay.output)["reused_run_ids"] == [first[0].id]
    assert len(seen) == 1 and len(agent.subagents.list_runs()) == 1
    assert agent.subagents.load(first[0].id).attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert advice(agent, first[0])["profile_id"] == a


@pytest.mark.parametrize("explicit_names", [True, False])
def test_nested_models_and_persisted_replay_keep_parent_and_existing_children(prepared, monkeypatch, explicit_names):
    agent, (a, b), _ = prepared
    from agent_py_agent.tests.test_orchestration_create_subagents_idempotency import (
        _idempotency_pack,
    )

    parent = agent.subagents.create_run(goal="负责检查", thought="分工", plan=["检查"],
        allowed_tools=["create_subagents"], attributes={"host_model_profile.v1": {"profile_id": b}})
    agent._current_subagent_run_id = parent.id
    agent._current_run_params.run_id = parent.id
    agent.config.config_sources = {"model_name": {"source": "owner_model_profile", "profile_id": b}}
    seen = provider(monkeypatch, prepared, {"0": a})
    params = {"items": [
        {"goal": "检查甲", "agent_name": "review-one", "context_packs": [_idempotency_pack("nested-a", "a")]},
        {"goal": "检查乙", "agent_name": "review-two", "model": b, "context_packs": [_idempotency_pack("nested-b", "b")]},
    ]}
    if not explicit_names:
        for item in params["items"]:
            item.pop("agent_name")
    tasks = created(agent, CreateSubagentsTool(agent).execute(params))
    assert [task.attributes["host_model_profile.v1"]["profile_id"] for task in tasks] == [b, b]
    assert advice(agent, tasks[0])["profile_id"] == a
    assert advice(agent, tasks[1]) is None
    result = CreateSubagentsTool(agent).execute(params)
    assert result.ok, result.output
    assert set(json.loads(result.output)["reused_run_ids"]) == {task.id for task in tasks}
    assert len(seen) == 1
    assert agent.subagents.load(parent.id).attributes["host_model_profile.v1"] == {"profile_id": b}


def test_capacity_rechecked_after_network_and_no_partial_materialization(prepared, monkeypatch):
    agent, (a, b), _ = prepared
    provider(monkeypatch, prepared, {"0": a, "1": b}, action=lambda: setattr(agent.config, "max_subagents", 1))
    result = CreateSubagentsTool(agent).execute({"items": [{"goal": "甲"}, {"goal": "乙"}]})
    assert not result.ok
    assert not agent.subagents.list_runs()


def test_unscoped_internal_creation_without_tools_snapshot_retains_original(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    agent.config.enable_tools = True
    seen = provider(monkeypatch, prepared, {"0": a})
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查代码", "allowed_tools": ["read_file"]}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert len(seen) == 1 and advice(agent, tasks[0])["status"] == "pending"


def test_real_tool_executor_parent_snapshot_does_not_prove_candidate_tool_support(prepared, monkeypatch, tmp_path):
    from agent_py_agent.agent.backends.http import HttpBackend
    from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
    from agent_py_agent.tests.test_tool_runtime_unification import _CountingTool

    agent, (a, _), _ = prepared
    agent.config.enable_tools = True
    assert isinstance(agent._current_run_params, RunParams)
    assert not hasattr(agent._current_run_params, "tool_protocol_snapshot")
    assert not hasattr(agent._current_run_params, "tool_runtime_snapshot")
    monkeypatch.setattr(HttpBackend, "probe_tool_capability", lambda *_: pytest.fail("selection must not probe"))
    seen = provider(monkeypatch, prepared, {"0": a})
    tools = {"create_subagents": CreateSubagentsTool(agent), "echo_tool": _CountingTool()}
    execution = execute_canonical_test_call(tmp_path, tools=tools, tool_name="create_subagents",
        arguments={"goal": "检查材料", "allowed_tools": ["echo_tool"]})
    assert execution.result.status == "succeeded", execution.result
    tasks = agent.subagents.list_runs()
    assert len(tasks) == 1 and tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert tasks[0].attributes["direct_parent_tool_authority"]["available_tool_names"] == ["create_subagents", "echo_tool"]
    assert len(seen) == 1 and selection._CURRENT_TOOLS.get() is None
    assert advice(agent, tasks[0])["status"] == "pending"
    facts = seen[0][1]["questions"]["0"]["criteria"][a]
    assert facts["output_cap_status"] == "known"
    assert facts["provider_tool_support"] == "unknown_until_original_runner_probe"


@pytest.mark.parametrize("input_tokens,small_fits", [(170_000, True), (187_500, False)])
def test_candidate_window_includes_its_own_output_cap(prepared, monkeypatch, input_tokens, small_fits):
    agent, (a, b), _ = prepared
    agent.config.max_tokens = 100_000
    monkeypatch.setattr(selection, "_input_budget", lambda *_: input_tokens)
    seen = provider(monkeypatch, prepared, {"0": a if small_fits else b})
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "核对材料"}))
    criteria = seen[0][1]["questions"]["0"]["criteria"]
    assert a in criteria  # 语义建议不是容量采用，已知数字也不能跳过首轮验证。
    assert criteria[b]["output_cap_tokens"] == 100_000
    if small_fits:
        assert criteria[a]["output_cap_tokens"] == 62_500
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert advice(agent, tasks[0])["profile_id"] == (a if small_fits else b)
    assert agent.config.max_tokens == 100_000


@pytest.mark.parametrize("changed_field", ["max_tokens", "system_prompt"])
def test_candidate_final_config_change_during_jev_invalidates_advice(prepared, monkeypatch, changed_field):
    agent, (a, _), _ = prepared
    provider(monkeypatch, prepared, {"0": a}, action=lambda: setattr(
        agent.config, changed_field, 2048 if changed_field == "max_tokens" else "修改后的系统提示"))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "核对材料"}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert advice(agent, tasks[0]) is None


def test_task_overlay_changed_during_jev_is_reloaded_without_applying_logs(prepared, monkeypatch, tmp_path):
    from agent_py_agent.agent.settings.services import runtime_config_task

    agent, (a, _), _ = prepared
    overlay = tmp_path / "candidate-runtime.yaml"
    overlay.write_text("runner_timeout_seconds: 31\n", encoding="utf-8")
    monkeypatch.setattr(runtime_config_task, "apply_log_level", lambda *_: pytest.fail("候选预览不能应用全局日志"))
    provider(monkeypatch, prepared, {"0": a}, action=lambda: overlay.write_text(
        "runner_timeout_seconds: 47\n", encoding="utf-8"))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "核对材料",
        "attributes": {"config_overlay_ref": str(overlay), "config_scope": "run"}}))
    assert tasks[0].runtime_identity.config_overlay_ref == str(overlay)
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert advice(agent, tasks[0]) is None


@pytest.mark.parametrize("name,protocol,window", [
    ("MiniMax-M2.7", "anthropic_compatible", 200_000),
    ("MiniMax-M3", "anthropic_compatible", 1_000_000),
    ("DeepSeek-V4-Flash", "openai_compatible", 1_000_000),
])
def test_candidate_cap_matches_original_adapter_payload_without_network(prepared, monkeypatch, name, protocol, window):
    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.backends.http import HttpBackend
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent, _, _ = prepared
    agent.config.max_tokens, agent.config.stream_enabled = 100_000, False
    profile_id, _ = add(agent, model_name=name, model_backend=protocol, model_context_window_tokens=window)
    run = agent.subagents.base_service.prepare_run(params=CreateRunParams(goal="核对材料", thought="", plan=[]))
    child = selection.SubagentModelInput({}, run.params, canonical=run.task)
    backends, payloads = [], []

    def factory(backend_name, config):
        backend = get_backend(backend_name, config)
        backends.append(backend)
        return backend

    def capture_request(_backend, _path, payload, _headers):
        payloads.append(payload)
        raise RuntimeError("captured-request-without-transport")

    monkeypatch.setattr(selection, "get_backend", factory)
    monkeypatch.setattr(HttpBackend, "request_json", capture_request)
    monkeypatch.setattr(HttpBackend, "probe_tool_capability", lambda *_: pytest.fail("候选不能发协议探针"))
    facts = selection._candidate_request_limits(agent, child, {profile_id: {}}, deadline=float("inf"))[profile_id]
    with pytest.raises(RuntimeError, match="captured-request-without-transport"):
        backends[0].generate("核对原请求字段", messages=[])
    assert facts["context_window_tokens"] == window
    assert facts["output_cap_tokens"] == payloads[0]["max_tokens"] == min(100_000, window // 4)
    assert not agent.subagents.list_runs()
    assert "secret" not in json.dumps(facts)


def test_oauth_responses_unknown_output_cap_cannot_be_zero_reserve(prepared, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.settings import model_oauth
    from agent_py_agent.agent.settings.model_profiles import selected_model_config

    agent, (a, _), _ = prepared
    config = replace(selected_model_config(agent, profile_id=a), model_backend="openai_responses", api_key="",
        model_auth_ref={"path": "unused-owner-auth.json", "provider_id": "test-provider", "generation": "test-generation",
                        "binding": "test-binding", "mode": "chatgpt"})
    patch(agent, {"points.subagent_model.candidate_profile_ids": [a]})
    monkeypatch.setattr(selection, "selected_model_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(model_oauth, "request_credentials", lambda *_: pytest.fail("预览不能读取或刷新认证"))
    seen = provider(monkeypatch, prepared, {"0": a})
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "核对材料"}))
    facts = seen[0][1]["questions"]["0"]["criteria"][a]
    assert facts["output_cap_tokens"] is None and facts["output_cap_status"] == "unknown"
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert len(seen) == 1 and advice(agent, tasks[0])["status"] == "pending"


def test_ten_children_share_one_stage_and_one_request(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    stages = []
    begin = selection.decision_service.begin_decision_stage

    def begin_once(*args, **kwargs):
        result = begin(*args, **kwargs)
        stages.append(result)
        return result

    monkeypatch.setattr(selection.decision_service, "begin_decision_stage", begin_once)
    seen = provider(monkeypatch, prepared, lambda body: dict.fromkeys(body["questions"], a))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"items": [{"goal": f"检查第 {index} 项"} for index in range(10)]}))
    assert len(tasks) == 10 and len(stages) == len(seen) == 1
    assert seen[0][2]["deadline"] <= stages[0].deadline


def test_late_consumption_and_changed_settings_cannot_apply(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    provider(monkeypatch, prepared, {"0": a},
        action=lambda: monkeypatch.setattr(selection, "time", SimpleNamespace(monotonic=lambda: float("inf"))))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查迟到结果"}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert advice(agent, tasks[0]) is None


def test_owner_scope_and_decision_purpose_never_become_child_candidates(prepared, monkeypatch, tmp_path):
    agent, (a, b), _ = prepared
    stranger = host_at(tmp_path, owner="bob")
    other, _ = add(stranger, model_name="other-owner")
    seen = provider(monkeypatch, prepared, {"0": a})
    created(agent, CreateSubagentsTool(agent).execute({"goal": "检查候选"}))
    candidates = set(seen[0][1]["questions"]["0"]["criteria"]) - set(selection._RETAIN_CHOICES)
    assert candidates == {a, b} and other not in candidates


@pytest.mark.parametrize("boundary", ["explicit_rename", "no_contract", "legacy_origin"])
def test_recursive_default_name_fix_does_not_widen_reuse(prepared, monkeypatch, boundary):
    from agent_py_agent.tests.test_orchestration_create_subagents_idempotency import (
        _idempotency_pack,
    )

    agent, (a, _), _ = prepared
    parent = agent.subagents.create_run(goal="负责检查", thought="分工", plan=["检查"], allowed_tools=["create_subagents"])
    agent._current_subagent_run_id = parent.id
    agent._current_run_params.run_id = parent.id
    params = {"goal": "检查材料"}
    if boundary != "no_contract":
        params["context_packs"] = [_idempotency_pack("one-contract", "result")]
    if boundary == "explicit_rename":
        params["agent_name"] = "agent-d1-worker-1"
    seen = provider(monkeypatch, prepared, {"0": a})
    first = created(agent, CreateSubagentsTool(agent).execute(params))[0]
    if boundary == "explicit_rename":
        params["agent_name"] = "agent-d1-worker-2"
    elif boundary == "legacy_origin":
        first.attributes.pop("host_agent_name_origin.v1")
        agent.subagents.save(first)
    second = created(agent, CreateSubagentsTool(agent).execute(params))
    assert len(second) == 1 and second[0].id != first.id
    assert len(seen) == 2


@pytest.mark.parametrize("same_contract", [True, False])
def test_default_name_reuse_requires_same_idempotency_even_with_same_repair(same_contract):
    from agent_py_agent.agent.subagents.services.base import CreateRunParams
    from agent_py_agent.agent.subagents.services.hierarchy.schedule_idempotency import (
        _same_schedule_contract,
    )
    from agent_py_agent.tests.test_orchestration_create_subagents_idempotency import (
        _idempotency_pack,
    )

    repair = {"contract": {"schema": "subagent_repair_contract.v1", "kind": "repair",
                           "failed_run_ids": ["failed-run"], "target_artifact_refs": ["artifact"]}}
    attrs = {"host_agent_name_origin.v1": {"explicit": False}}
    task = SimpleNamespace(status="DONE", parent_id="parent", root_id="root", role="worker",
        agent_name="auto-1", attributes=attrs, allowed_write_roots=[], task_dir="",
        context_packs=[repair, _idempotency_pack("first", "artifact")])
    params = CreateRunParams(goal="检查", thought="", plan=[], parent_id="parent", root_id="root", role="worker",
        agent_name="auto-2", attributes=attrs,
        context_packs=[repair, _idempotency_pack("first" if same_contract else "second", "artifact")])
    assert _same_schedule_contract(task, params) is same_contract


def test_pending_persists_only_stable_origin_and_reading_never_adopts(prepared, monkeypatch):
    from agent_py_agent.agent.conversation.agent_thread import ensure_subagent_thread
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.settings.model_profiles import inherited_model_config

    agent, (a, _), _ = prepared
    provider(monkeypatch, prepared, {"0": a})
    task = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))[0]
    pending = advice(agent, task)
    assert set(pending) == {"schema", "status", "profile_id", "operation_id", "source_owner_ref",
        "source_thread_id", "source_run_id", "source_task_id", "source_owner_revision", "source_thread_revision",
        "child_run_id", "child_thread_id", "source_model_generation"}
    assert pending["source_model_generation"]["schema"] == "model_profile_generation.v1"
    assert pending["source_model_generation"]["profile_id"] == a
    assert pending["child_run_id"] == task.id and pending["child_thread_id"] == task.agent_thread_id
    assert pending["source_thread_id"] == agent._current_run_params.task_attributes["conversation_thread_id"]
    assert pending["status"] == "pending" and pending["schema"] == SUBAGENT_MODEL_ADVICE_KEY
    agent.conversation_store = ConversationStore(agent.conversation_store.storage.root)
    agent.subagents.conversation_store = agent.conversation_store
    before = inherited_model_config(agent, task)
    patch(agent, {"enabled": False})
    execute_model_profile_operation(agent, "delete_model", {"profile_id": a})
    assert ensure_subagent_thread(agent.subagents, task).model_profile_id == "default"
    assert inherited_model_config(agent, task) is before
    assert advice(agent, task) == pending  # 读取不采用；启动时还要重新核对目录版本、设置和真实请求。


@pytest.mark.parametrize("same_model", [True, False])
def test_explicit_selection_atomically_retires_pending_even_if_same_profile(prepared, monkeypatch, same_model):
    from dataclasses import replace

    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id

    agent, (a, b), _ = prepared
    agent.config.config_sources = {"model_name": {"source": "owner_model_profile", "profile_id": b}}
    provider(monkeypatch, prepared, {"0": a})
    task = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))[0]
    store = agent.conversation_store.threads
    store.update_atomic(task.agent_thread_id, lambda row: replace(row, metadata={**row.metadata, "other": "keep"},
        provider_context_observation={"count": 10}, model_context_usage={"count": 11}))
    calls_seen = []
    original = store.update_atomic

    def capture(thread_id, update):
        result = original(thread_id, update)
        calls_seen.append(result)
        return result

    monkeypatch.setattr(store, "update_atomic", capture)
    chosen = b if same_model else a
    assert thread_model_profile_id(agent, task.agent_thread_id, select=chosen) == chosen
    assert len(calls_seen) == 1
    thread = calls_seen[0]
    assert thread.model_profile_id == chosen and thread.metadata["other"] == "keep"
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "retained"
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["reason"] == "explicit_model_selection"
    assert thread.provider_context_observation == ({"count": 10} if same_model else {})
    assert thread.model_context_usage == ({"count": 11} if same_model else {})


@pytest.mark.parametrize("recursive", [False, True])
def test_task_attributes_cannot_forge_thread_advice(prepared, monkeypatch, recursive):
    agent, (a, _), _ = prepared
    patch(agent, {"enabled": False})
    if recursive:
        parent = agent.subagents.create_run(goal="父任务", allowed_tools=["create_subagents"])
        agent._current_subagent_run_id = parent.id
        agent._current_run_params.run_id = parent.id
    seen = provider(monkeypatch, prepared, {"0": a})
    task = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料", "attributes": {
        SUBAGENT_MODEL_ADVICE_KEY: {"status": "pending", "profile_id": a},
        "host_model_decision.v1": {"status": "applied"}, "host_model_profile.v1": {"profile_id": a},
    }}))[0]
    assert not seen and advice(agent, task) is None
    assert task.attributes["host_model_profile.v1"] == {"profile_id": "default"}


def test_crash_after_thread_creation_retry_cannot_replant_cancelled_pending(prepared, monkeypatch):
    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id

    agent, (a, _), _ = prepared
    provider(monkeypatch, prepared, {"0": a})
    service = agent.subagents.base_service
    original_create, original_authority = service.create_run, service._write_authority_records
    runs = []

    def capture(**kwargs):
        runs.append(kwargs["prepared"])
        return original_create(**kwargs)

    def crash(*_):
        raise OSError("simulated crash between thread and authority publication")

    monkeypatch.setattr(service, "create_run", capture)
    monkeypatch.setattr(service, "_write_authority_records", crash)
    result = CreateSubagentsTool(agent).execute({"goal": "检查材料"})
    assert not result.ok and not agent.subagents.list_runs()
    run = runs[0]
    pending = advice(agent, run.task)
    assert pending["status"] == "pending"
    thread_model_profile_id(agent, run.task.agent_thread_id, select="default")
    monkeypatch.setattr(service, "_write_authority_records", original_authority)
    task = original_create(params=run.params, prepared=run)
    assert task is run.task and len(agent.subagents.list_runs()) == 1
    assert advice(agent, task)["status"] == "retained"


@pytest.mark.parametrize("change", ["settings", "candidate_connection", "decision_connection"])
def test_settings_and_connections_changed_during_jev_cannot_create_pending(prepared, monkeypatch, change):
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )

    agent, (a, _), _ = prepared
    changed = []

    def modify():
        if change == "settings":
            patch(agent, {"points.subagent_model.mode": "observe"})
            changed.append(change)
            return
        data = read_model_profiles(model_profiles_path(agent.home_paths))
        profile = a if change == "candidate_connection" else next(
            key for key, value in data["profiles"].items() if value["capability"] == "decision")
        provider_id = data["profiles"][profile]["provider_id"]
        execute_model_profile_operation(agent, "save_provider", {"provider_id": provider_id, "editing": True,
            "provider": {**data["providers"][provider_id], "api_key": "changed-private-secret"}})
        changed.append(change)

    seen = provider(monkeypatch, prepared, {"0": a}, action=modify)
    task = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))[0]
    assert changed == [change] and len(seen) == 1 and advice(agent, task) is None


@pytest.mark.parametrize("change", ["unchanged", "params", "permissions", "identity"])
def test_refreeze_binds_advice_to_same_unpublished_task_and_current_facts(prepared, monkeypatch, change):
    from dataclasses import replace

    from agent_py_agent.agent.settings.thread_model_selection import PendingSubagentModelAdvice
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent, (a, _), _ = prepared
    service = agent.subagents.base_service
    run = service.prepare_run(params=CreateRunParams(goal="检查材料", thought="", plan=[], allowed_tools=["read_file"]))
    proposal = PendingSubagentModelAdvice(a, "operation", "owner", "source-thread", "", "", 1, 0,
        run.task.id, run.task.agent_thread_id)
    run = replace(run, model_advice=proposal)
    params = replace(run.params, goal="检查修改后的材料") if change == "params" else run.params
    if change == "permissions":
        agent.subagents.owner_policy_snapshot = {"tools": {"disabled_tools": ["read_file"]}}
    if change == "identity":
        run = replace(run, model_advice=replace(proposal, child_run_id="another-run"))
    updated = service.refreeze_run(params=params, prepared=run)
    assert updated.task is run.task and (updated.run_id, updated.created_at) == (run.run_id, run.created_at)
    if change == "identity":
        with pytest.raises(ValueError, match="其他运行"):
            service.create_run(params=params, prepared=updated)
        assert agent.conversation_store.threads.load(run.task.agent_thread_id) is None
        return
    assert (updated.model_advice is proposal) is (change == "unchanged")
    task = service.create_run(params=params, prepared=updated)
    row = agent.conversation_store.threads.require(task.agent_thread_id)
    assert row.model_profile_id == "default"
    assert (SUBAGENT_MODEL_ADVICE_KEY in row.metadata) is (change == "unchanged")
