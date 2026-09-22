"""原派工入口的可选模型选择；真实配置和任务存储，供应商调用用协议替身。"""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import hierarchy_tools, orchestration_tools
from agent_py_agent.agent.agent_core.orchestration import decision_subagent as selection
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.backends.typesafe_decision_wire import parse_typesafe_response
from agent_py_agent.agent.conversation import decision_model_call as calls
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at, patch
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_orchestration_create_subagents_items import _agent


# LLM: 使用原配置/manager 持久化，只有后台发布和收费模型边界被替换；guard_depth 证明建议请求未持创建锁。
# 函数用途: 创建一套带两个已保存生成模型的离线派工宿主。
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
    assert [task.attributes["host_model_profile.v1"]["profile_id"] for task in tasks] == [a, b, a]
    assert len(seen) == 1 and set(seen[0][1]["questions"]) == {"0", "1"}
    assert a in seen[0][1]["questions"]["0"]["criteria"]  # 250K 候选可替换 1M 父窗口。
    assert isinstance(seen[0][2]["deadline"], float)
    assert "secret" not in json.dumps(seen[0][1])
    assert agent.config.model_context_window_tokens == 1_000_000


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
    assert [task.attributes["host_model_profile.v1"]["profile_id"] for task in tasks] == ["default", b]
    reason = tasks[0].attributes["host_model_decision.v1"]["reason"]
    assert reason == choice if choice != "foreign-model" else reason != "not_needed"


def test_authorized_candidate_removed_during_request_is_not_applied(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    provider(monkeypatch, prepared, {"0": a}, action=lambda: execute_model_profile_operation(agent, "delete", {"profile_id": a}))
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料"}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}


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


def test_unknown_external_inputs_keep_original_without_request(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    seen = provider(monkeypatch, prepared, {"0": a})
    tasks = created(agent, CreateSubagentsTool(agent).execute({"goal": "检查材料", "input_refs": ["file:unknown.txt"]}))
    assert tasks[0].attributes["host_model_profile.v1"] == {"profile_id": "default"}
    assert tasks[0].attributes["host_model_decision.v1"]["reason"] == "capacity_unknown"
    assert not seen


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
    assert agent.subagents.load(first[0].id).attributes["host_model_profile.v1"] == {"profile_id": a}


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
    assert [task.attributes["host_model_profile.v1"]["profile_id"] for task in tasks] == [a, b]
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
    assert not seen


def test_real_tool_executor_uses_frozen_specs_with_tools_enabled(prepared, monkeypatch, tmp_path):
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
    assert len(tasks) == 1 and tasks[0].attributes["host_model_profile.v1"] == {"profile_id": a}
    assert tasks[0].attributes["direct_parent_tool_authority"]["available_tool_names"] == ["create_subagents", "echo_tool"]
    assert len(seen) == 1 and selection._CURRENT_TOOLS.get() is None
    assert seen[0][1]["questions"]["0"]["criteria"][a]["provider_tool_support"] == "unknown_until_original_runner_probe"


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
