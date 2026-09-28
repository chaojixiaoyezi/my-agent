"""一次选包的执行权和原策略边界；真实临时账本、零模型、零工具执行。"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.tool_loop.recovery import runtime_run_scope
from agent_py_agent.agent.capability import package_selection_authority as authority_module
from agent_py_agent.agent.capability import package_selection_runtime as selection_runtime
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.capability.package_selection_authority import (
    PackageSelectionAuthority,
    package_entry_policy,
    package_selection_model_digest,
)
from agent_py_agent.agent.capability.skill_snapshot import SkillSnapshot
from agent_py_agent.agent.common.cancellation import CancellationToken, ToolCancelled
from agent_py_agent.agent.conversation.capability_selection_state import TaskCapabilitySelection
from agent_py_agent.agent.conversation.task_resources import close_main_task_authority
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.io import _path_lock
from agent_py_agent.agent.runtime_db.execution_mode import ExecutionMode
from agent_py_agent.agent.runtime_db.managed_operation_store import (
    AuthorityContextMissing,
    ManagedOperationStore,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.executor import ToolExecutor
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.tests.test_capability_package_discovery import package_fixture


# LLM: 运行通过原 create_run 和真实 RuntimeDB 激活，不伪造 require_authority；所有副作用只在临时 owner 内。
# 函数用途: 提供真实冻结 ToolLoopExecuteParams 与独立 outer 参数，供执行 scope、失权和原 TaskStore 锁测试。
@pytest.fixture
def bound_execution(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                    enable_plugins=True, enable_subagents=True, prompt_files=[]), tmp_path / "workspace")
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "authority"})
    link = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "selection-task", "goal": "核对参考材料"},
                            capability_selection=TaskCapabilitySelection.pending())
    attrs = {"conversation_thread_id": thread.thread_id, "conversation_task_id": link.task_id}
    run = agent.subagents.create_run(goal="核对参考材料", thought="测试执行权", plan=["读取入口"], role="main", attributes=attrs)
    repo = agent.subagents.runtime_db
    record = repo.agent_run_for_run_id(run.id)
    attempt = repo.create_attempt(record["agent_run_id"], reuse_pending=True)
    identity = dict(request_id="request-selection", run_id=run.id, task_id=link.task_id, attempt_id=attempt["attempt_id"])
    outer = RunParams(**identity, task_attributes=dict(attrs))
    params = ToolLoopExecuteParams(
        user_prompt="核对参考材料", root_user_prompt="核对参考材料", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=["skill_search"], write_boundary=None, one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        **identity, task_attributes=dict(attrs), conversation_turn_id="turn-selection", cancellation_token=CancellationToken(),
    )
    agent._current_run_params = outer
    authority = PackageSelectionAuthority(agent, params, link.task_id, thread.thread_id,
                                          params.request_id, params.run_id, params.attempt_id)
    return SimpleNamespace(agent=agent, params=params, outer=outer, authority=authority, link=link,
                           repo=repo, record=record, tasks=store.tasks, path=store.storage.task_path(link.task_id))


# LLM: 此测试引用只检验原 pin 入口是否写入，不能作为已安装/已读取包的证据。
# 函数用途: 从既有快照 fixture 取得规范引用，避免构造另一种 refs schema。
def _reference():
    return package_fixture("authority-package").to_ref()


# LLM: 只读取已存在的原操作表，不创建 tool_operations 或查询新的 owner。
# 函数用途: 核对宿主准备没有冒充工具执行。
def _operation_count(fixture):
    with fixture.repo._runtime_connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM tool_operations").fetchone()[0]


# LLM: 测试中的禁止入口只报错，不执行原工具或模型；monkeypatch 在本用例结束自动恢复。
# 函数用途: 证明权限准备不会借 ToolExecutor 或 handler 产生工具历史。
def _unexpected(*_args, **_kwargs):
    raise AssertionError("宿主选择准备不应执行工具、请求模型或回读 task")


def test_current_attempt_allows_real_store_without_fake_tool_operations(bound_execution):
    fixture = bound_execution
    before = fixture.path.read_bytes()
    fixture.authority.check()
    assert isinstance(fixture.agent._operation_store, ManagedOperationStore)
    assert fixture.authority.is_current(fixture.link, TaskCapabilitySelection.pending())
    assert fixture.path.read_bytes() == before
    assert _operation_count(fixture) == 0 and fixture.params.tool_ir_history == []


@pytest.mark.parametrize("change", ["attempt", "done", "failed", "cancelled", "pause", "token"])
def test_stopped_or_replaced_execution_cannot_pin(bound_execution, change):
    fixture = bound_execution
    if change == "attempt":
        fixture.repo.create_attempt(fixture.record["agent_run_id"])
    elif change == "pause":
        with fixture.tasks.transition_guard(fixture.link.task_id):
            close_main_task_authority(fixture.repo, owner_home=str(fixture.agent.home_paths.owner_home_dir),
                                      task_id=fixture.link.task_id, thread_id=fixture.link.thread_id)
        fixture.tasks.update_status({"task_id": fixture.link.task_id, "status": "interrupted"})
    elif change == "token":
        fixture.params.cancellation_token.cancel("user_stop")
    else:
        fixture.repo.settle_agent_run(agent_run_id=fixture.record["agent_run_id"],
                                     attempt_id=fixture.params.attempt_id, status=change)
    before = fixture.path.read_bytes()
    with pytest.raises(ToolCancelled) as caught:
        fixture.authority.atomic(lambda: fixture.tasks.pin_skill_reference(
            task_id=fixture.link.task_id, thread_id=fixture.link.thread_id, reference=_reference(),
            execution_authority_check=fixture.authority.check,
        ))
    if change != "token":
        assert isinstance(caught.value.__cause__, AuthorityContextMissing)
    assert fixture.path.read_bytes() == before
    assert fixture.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()
    assert _operation_count(fixture) == 0


@pytest.mark.parametrize("change", ["attempt", "cancelled"])
def test_authority_revoked_after_atomic_entry_is_rechecked_inside_pin_lock(bound_execution, monkeypatch, change):
    fixture = bound_execution
    original = ManagedOperationStore.require_authority
    checks = []

    def checked(store, request, **kwargs):
        checks.append(_path_lock(fixture.path).locked())
        return original(store, request, **kwargs)

    def late_pin():
        if change == "attempt":
            fixture.repo.create_attempt(fixture.record["agent_run_id"])
        else:
            fixture.repo.settle_agent_run(agent_run_id=fixture.record["agent_run_id"],
                                         attempt_id=fixture.params.attempt_id, status="cancelled")
        return fixture.tasks.pin_skill_reference(
            task_id=fixture.link.task_id, thread_id=fixture.link.thread_id, reference=_reference(),
            execution_authority_check=fixture.authority.check,
        )

    monkeypatch.setattr(ManagedOperationStore, "require_authority", checked)
    before = fixture.path.read_bytes()
    with pytest.raises(ToolCancelled) as caught:
        fixture.authority.atomic(late_pin)
    assert isinstance(caught.value.__cause__, AuthorityContextMissing)
    assert checks == [False, True]
    assert fixture.path.read_bytes() == before
    assert fixture.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()


@pytest.mark.parametrize("surface", ["outer", "loop"])
@pytest.mark.parametrize("field", ["request_id", "run_id", "attempt_id", "task_id", "thread_id", "declared_task_id"])
def test_frozen_identity_rejects_outer_or_loop_param_drift(bound_execution, surface, field):
    fixture = bound_execution
    target = fixture.outer if surface == "outer" else fixture.params
    if field in {"task_id", "thread_id"}:
        target.task_attributes[f"conversation_{field}"] = "other-identity"
    else:
        # 负例刻意突破冻结载体，验证宿主保存的原身份仍能拒绝漂移；正式调用不应修改此对象。
        object.__setattr__(target, "task_id" if field == "declared_task_id" else field, "other-identity")
    with pytest.raises(ToolCancelled, match="CAPABILITY_SELECTION_EXECUTION_CHANGED"):
        fixture.authority.check()
    assert fixture.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()


@pytest.mark.parametrize("field", ["request_id", "run_id", "attempt_id", "task_id"])
def test_frozen_authority_rechecks_runtime_scope_identity(bound_execution, field):
    fixture = bound_execution
    scope = runtime_run_scope(fixture.agent, fixture.params)
    object.__setattr__(fixture.params, "run_scope", replace(scope, **{field: "other-identity"}))
    with pytest.raises(ToolCancelled, match="CAPABILITY_SELECTION_EXECUTION_CHANGED"):
        fixture.authority.check()
    assert fixture.tasks.load(fixture.link.task_id).skill_snapshot_refs == ()
    assert _operation_count(fixture) == 0


def test_successor_link_keeps_original_execution_task_and_only_pins_new_link(bound_execution):
    fixture = bound_execution
    fixture.tasks.update_status({"task_id": fixture.link.task_id, "status": "done"})
    previous = fixture.path.read_bytes()
    successor = fixture.tasks.bind({"thread_id": fixture.link.thread_id, "task_id": "successor-task", "goal": "后续任务"},
                                   capability_selection=TaskCapabilitySelection.pending())
    attrs = {**fixture.params.task_attributes, "conversation_task_id": successor.task_id}
    params = replace(fixture.params, task_attributes=attrs)
    fixture.outer.task_attributes = dict(attrs)
    authority = PackageSelectionAuthority(fixture.agent, params, successor.task_id, successor.thread_id,
                                          params.request_id, params.run_id, params.attempt_id)
    assert runtime_run_scope(fixture.agent, params).task_id == fixture.link.task_id != successor.task_id
    authority.check()
    claim = authority.atomic(lambda: fixture.tasks.claim_capability_selection(
        task_id=successor.task_id, thread_id=successor.thread_id, request_id=params.request_id,
        run_id=params.run_id, attempt_id=params.attempt_id, candidate_digest="a" * 64,
        model_binding_digest=package_selection_model_digest(fixture.agent), execution_is_current=authority.is_current,
    ))
    pinned = authority.atomic(lambda: fixture.tasks.pin_skill_reference(
        task_id=successor.task_id, thread_id=successor.thread_id, reference=_reference(),
        execution_authority_check=authority.check,
    ))
    finished = authority.atomic(lambda: fixture.tasks.finish_capability_selection(
        task_id=successor.task_id, thread_id=successor.thread_id, expected_claim=claim,
        outcome="selected", selected_refs=pinned.skill_snapshot_refs, execution_is_current=authority.is_current,
    ))
    assert finished.status == "finished" and pinned.skill_snapshot_refs == (_reference(),)
    assert fixture.path.read_bytes() == previous
    assert fixture.repo.task_id_for_run_id(params.run_id) == fixture.link.task_id
    assert _operation_count(fixture) == 0 and params.tool_ir_history == []


@pytest.mark.parametrize("mode", [ExecutionMode.LOCAL_UNMANAGED, "local_unmanaged", "unknown", None])
def test_only_explicit_unmanaged_mode_can_skip_managed_authority(bound_execution, mode):
    fixture = bound_execution
    local = SimpleNamespace(subagents=SimpleNamespace(execution_mode=mode), local_store=fixture.agent.local_store,
                            tools=fixture.agent.tools, _current_run_params=fixture.outer)
    authority = replace(fixture.authority, agent=local)
    if mode is ExecutionMode.LOCAL_UNMANAGED:
        authority.check()
        assert local._operation_store is fixture.agent.local_store
    else:
        with pytest.raises(TypeError, match="execution_mode"):
            authority.check()
        assert not hasattr(local, "_operation_store")


def test_atomic_claim_uses_turn_then_task_locks_and_callback_does_not_read_task(bound_execution, monkeypatch):
    fixture = bound_execution
    turn_lock = threading.Lock()
    path_lock = _path_lock(fixture.path)
    task_lock = _path_lock(fixture.agent.conversation_store.storage.tasks_dir / ".selection-task.transition")
    observations = []
    original = ManagedOperationStore.require_authority

    def require(store, request, **kwargs):
        observations.append((turn_lock.locked(), task_lock.locked(), path_lock.locked()))
        return original(store, request, **kwargs)

    def transition(kind, operation):
        assert kind == "capability_selection" and not task_lock.locked() and not path_lock.locked()
        with turn_lock:
            return operation()

    authority = replace(fixture.authority, params=replace(fixture.params, active_turn_transition_callback=transition))
    monkeypatch.setattr(ManagedOperationStore, "require_authority", require)
    monkeypatch.setattr(fixture.tasks, "load", _unexpected)
    monkeypatch.setattr(fixture.tasks, "load_report", _unexpected)
    claim = authority.atomic(lambda: fixture.tasks.claim_capability_selection(
        task_id=fixture.link.task_id, thread_id=fixture.link.thread_id, request_id=fixture.params.request_id,
        run_id=fixture.params.run_id, attempt_id=fixture.params.attempt_id,
        candidate_digest="a" * 64, model_binding_digest=package_selection_model_digest(fixture.agent),
        execution_is_current=authority.is_current,
    ))
    assert claim.status == "claimed"
    assert observations == [(True, False, False), (True, True, True)]
    assert json.loads(fixture.path.read_text())["host_capability_selection.v1"]["claim_id"] == claim.claim_id


def test_actual_preparation_never_downgrades_stale_attempt_to_optional_warning(bound_execution, monkeypatch):
    fixture = bound_execution
    skills = SkillSnapshot((), (), "snapshot", "local/main", str(fixture.agent.root), packages=(package_fixture("authority-package"),))
    scope = SimpleNamespace(skills=skills, tools=fixture.agent.tools.runtime_snapshot(run_id=fixture.params.run_id), config=CapabilityConfig())
    monkeypatch.setattr(selection_runtime, "package_selection_scope", lambda *_: scope)
    monkeypatch.setattr(selection_runtime, "select_capability_packages", _unexpected)
    fixture.repo.create_attempt(fixture.record["agent_run_id"])
    before = fixture.path.read_bytes()
    with pytest.raises(ToolCancelled):
        selection_runtime.prepare_capability_package_selection(fixture.agent, fixture.params)
    assert fixture.path.read_bytes() == before and fixture.params.tool_ir_history == []


@pytest.mark.parametrize("gate,code", [
    ("allow", ""), ("disabled", "CAPABILITY_SELECTION_READ_UNAVAILABLE"),
    ("guardrail", "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED"), ("rate", "TOOL_RATE_LIMIT_EXCEEDED"),
    ("approval", "APPROVAL_REQUIRED"), ("approval_mode", "APPROVAL_MODE_UNAVAILABLE"),
    ("approval_error", "approval unavailable"),
])
def test_entry_policy_reuses_real_gates_without_executor_or_history(bound_execution, monkeypatch, gate, code):
    fixture = bound_execution
    agent, params = fixture.agent, fixture.params
    arguments = {"action": "get", "package_id": "authority-package",
                 "expected_package_sha256": "a" * 64, "expected_activation_id": "b" * 64, "max_chars": 100}
    args_hash = tool_arguments_hash(arguments)
    agent.tools.approval_mode_reader = lambda: "ask"
    if gate == "disabled":
        agent.tools.disabled_tool_names.add("skill_search")
    tools = agent.tools.runtime_snapshot(allowed_tools=["skill_search"], run_id=params.run_id)
    if gate == "guardrail":
        params.task_attributes["readonly_no_progress_threshold"] = 1
        params = replace(params, write_boundary={"tool_guardrail_policy": {"readonly_no_progress_threshold": 1},
                                                "tool_guardrail_records": [{"tool_name": "skill_search", "args_hash": args_hash,
                                                                           "failed": False, "result_hash": "same"}] * 3})
    elif gate == "rate":
        params = replace(params, write_boundary={"tool_rate_limit_policy": {"max_calls": 1, "window_seconds": 60},
                                                "tool_rate_limit_records": [{"tool_name": "skill_search", "args_hash": args_hash,
                                                                            "attempt_timestamps": [time.time()]}]})
    elif gate == "approval":
        runtime = tools.runtime("skill_search")
        policy = replace(runtime.runtime_policy, approval_policy=replace(runtime.runtime_policy.approval_policy, mode="always"))
        tools = replace(tools, runtimes=(replace(runtime, runtime_policy=policy),))
    elif gate == "approval_mode":
        agent.tools.approval_mode_reader = lambda: "unknown"
    elif gate == "approval_error":
        def broken_approval():
            raise OSError("approval unavailable")
        agent.tools.approval_mode_reader = broken_approval
    monkeypatch.setattr(ToolExecutor, "execute", _unexpected)
    monkeypatch.setattr(agent.tools.tools["skill_search"], "execute", _unexpected)
    before = fixture.path.read_bytes()
    if gate in {"disabled", "approval_error"}:
        with pytest.raises((ValueError, OSError), match=code):
            package_entry_policy(agent, params, tools, arguments, claim_id="claim")
    else:
        decision = package_entry_policy(agent, params, tools, arguments, claim_id="claim")
        assert decision.allowed is (gate == "allow")
        assert decision.resolved_effect == "read_only"
        assert not code or code in decision.reason_codes
    assert fixture.path.read_bytes() == before
    assert _operation_count(fixture) == 0 and params.tool_ir_history == []


def test_unknown_authority_failure_is_not_mislabeled_as_cancellation(bound_execution, monkeypatch):
    failure = OSError("runtime database unavailable")

    def unavailable(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(ManagedOperationStore, "require_authority", unavailable)
    with pytest.raises(OSError) as caught:
        bound_execution.authority.check()
    assert caught.value is failure
    assert bound_execution.tasks.load(bound_execution.link.task_id).skill_snapshot_refs == ()


def test_model_digest_uses_current_binding_and_never_profile_or_secret_fields(bound_execution, monkeypatch):
    fixture = bound_execution
    observed = []
    original = authority_module.json.dumps

    def capture(value, **kwargs):
        if isinstance(value, dict) and "backend" in value:
            observed.append(dict(value))
        return original(value, **kwargs)

    monkeypatch.setattr(authority_module.json, "dumps", capture)
    first = package_selection_model_digest(fixture.agent)
    fixture.agent.config.api_key = "private-fixture-secret"
    fixture.agent.backend.api_key = "another-private-fixture-secret"
    fixture.agent.conversation_store.threads.update_atomic(fixture.link.thread_id, lambda thread: replace(
        thread, model_profile_id="later-menu-profile", model_selection_source="explicit",
        model_selection_revision=thread.model_selection_revision + 1,
        model_selection_last_explicit_revision=thread.model_selection_revision + 1,
    ))
    assert package_selection_model_digest(fixture.agent) == first
    assert set(observed[0]) == {"model_backend", "api_base", "temperature", "top_p", "stream_enabled",
                                "model_reasoning_control", "model_structured_output", "backend", "model",
                                "max_output_tokens", "context_window_tokens"}
    assert "private-fixture-secret" not in original(observed)
    fixture.agent.backend.model_name = "different-actual-model"
    assert package_selection_model_digest(fixture.agent) != first


# LLM: 决策对象本身不可直接读回，所以用一个只做记录的包装类替换模块里的 ActionPolicy；包装类把真正的
#   ActionPolicy 类提前抓在闭包里再调用，避免 monkeypatch 之后自己调到自己。断言只看 workspace_roots /
#   owner_scope_root 这两个与权限边界直接相关的结构化字段，不看 message 或自然语言。
# 函数用途: 记录 package_entry_policy 实际用哪组结构化边界去问原 ActionPolicy。
def _capture_policy_request(monkeypatch, authority_module):
    real_policy_cls = authority_module.ActionPolicy
    captured = {}

    class _RecordingPolicy:
        def decide(self, request):
            captured["workspace_roots"] = tuple(request.workspace_roots)
            captured["owner_scope_root"] = request.owner_scope_root
            captured["workspace_root"] = request.workspace_root
            return real_policy_cls().decide(request)

    monkeypatch.setattr(authority_module, "ActionPolicy", _RecordingPolicy)
    return captured


# LLM: 墙外授权根来自本 run 冻结的 write_boundary，不是 registry 自己的值；registry.workspace_roots 里没有这个根，
#   把它改回“忽略冻结值”这条用例就会红（变异验证见 TESTS.md 同日条目）。
# 函数用途: 证明包入口读取判定用的是冻结下来的墙外授权根与冻结 owner 墙。
def test_entry_policy_uses_frozen_boundary_not_registry_scope(bound_execution, tmp_path, monkeypatch):
    fixture = bound_execution
    agent, params = fixture.agent, fixture.params
    frozen_root = tmp_path / "frozen-worktree"
    frozen_root.mkdir(parents=True, exist_ok=True)
    frozen_scope = str(tmp_path / "frozen-owner-home")
    assert str(frozen_root) not in {str(item) for item in agent.tools.workspace_roots}
    agent.tools.approval_mode_reader = lambda: "ask"
    tools = agent.tools.runtime_snapshot(allowed_tools=["skill_search"], run_id=params.run_id)
    monkeypatch.setattr(ToolExecutor, "execute", _unexpected)
    monkeypatch.setattr(agent.tools.tools["skill_search"], "execute", _unexpected)
    monkeypatch.setattr(authority_module, "write_boundary_with_runtime_ledger", lambda *_: {
        "execution_workspace_roots": [str(frozen_root)],
        "effective_owner_scope_root": frozen_scope,
    })
    captured = _capture_policy_request(monkeypatch, authority_module)

    package_entry_policy(agent, params, tools, {"action": "get", "package_id": "authority-package"}, claim_id="claim")

    assert str(frozen_root) in {str(item) for item in captured["workspace_roots"]}
    assert captured["owner_scope_root"] == frozen_scope
    # 冻结的墙外根生效：包入口之外的默认根一个没多。
    assert {str(item) for item in captured["workspace_roots"]} != {str(item) for item in agent.tools.workspace_roots}
