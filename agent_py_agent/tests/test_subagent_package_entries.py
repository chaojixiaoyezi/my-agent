# LLM: 子入口测试走原安装、创建、线程和 runner；只替换供应商发送，不访问真实模型或业务目录。
# 模块用途: 验证正文真正进入孩子请求，首请求资格与权限引用保持唯一权威。
from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.conversation.agent_thread import ensure_subagent_thread
from agent_py_agent.agent.conversation.agent_thread_store import SUBAGENT_FIRST_REQUEST_KEY
from agent_py_agent.tests.test_capability_package_task_refs import _agent


# LLM: 开关写 pytest 私有配置并由原加载器消费；不替造包引用或 runner 状态。
# 函数用途: 创建原工具可用的合成包环境，保留关闭与显式授权对照。
def _fixture(tmp_path, *, enabled=True):
    agent, store, installed = _agent(tmp_path)
    path = tmp_path / "capability.yaml"
    path.write_text(f"enable_capability_package_selection: {str(enabled).lower()}\n", encoding="utf-8")
    agent.capability_config_path = path
    agent._capability_config_runtime_snapshot = None
    return agent, store, installed


# LLM: 原 create_subagents 负责授权、快照和身份；defer_start 只推迟真实 runner 到被测步骤。
# 函数用途: 创建一位明确包范围的孩子，不手改 canonical 权限或执行状态。
def _child(agent, *, allowed=None):
    result = CreateSubagentsTool(agent).execute({
        "goal": "读取已授权故事资料后说明观察", "allowed_skills": ["capability:story-a"] if allowed is None else allowed,
        "defer_start": True,
    })
    assert result.ok, result.output
    return agent.subagents.list_runs()[-1]


@pytest.mark.parametrize("enabled,allowed,expected", [(True, None, True), (False, None, False), (True, [], False)])
def test_original_creation_initializes_only_eligible_package_child(tmp_path, enabled, allowed, expected):
    agent, _, _ = _fixture(tmp_path, enabled=enabled)
    child = _child(agent, allowed=allowed)
    thread = agent.conversation_store.threads.require(child.agent_thread_id)
    assert (SUBAGENT_FIRST_REQUEST_KEY in thread.metadata) is expected
    assert "host_subagent_model_advice.v1" not in thread.metadata
    if expected:
        assert thread.metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "unsubmitted"


def test_existing_thread_never_acquires_package_first_eligibility(tmp_path):
    from agent_py_agent.agent.capability.subagent_entry_authority import SubagentEntryInitialization
    agent, _, _ = _fixture(tmp_path)
    child = _child(agent)
    store = agent.conversation_store.threads
    store.update_atomic(child.agent_thread_id, lambda row: replace(row, metadata={
        key: value for key, value in row.metadata.items() if key != SUBAGENT_FIRST_REQUEST_KEY
    }))
    ensure_subagent_thread(agent.subagents, child,
                           package_entry_initialization=SubagentEntryInitialization(child.id, child.agent_thread_id))
    assert SUBAGENT_FIRST_REQUEST_KEY not in store.require(child.agent_thread_id).metadata


# LLM: 真正 provider 序列化抵达本地替身；断言在 runner 返回后做，避免被业务异常收口吞掉断言。
# 函数用途: 为真实离线 runner 收集业务载荷和首次发送状态，不执行网络。
def _provider(agent, child, monkeypatch, *, respond=None):
    from agent_py_agent.agent.backends import http
    agent.config.model_backend = "anthropic_compatible"
    agent.config.model_name = "MiniMax-M2.7"
    agent.config.api_base = "https://offline.invalid/anthropic"
    agent.config.api_key = "test-only"
    agent.config.stream_enabled = False
    from agent_py_agent.agent.backends import get_backend
    agent.backend = get_backend(agent.config.model_backend, agent.config)
    seen = []
    markers = []

    def send(request):
        wire = json.dumps(request.payload, ensure_ascii=False)
        if "my_agent_capability_probe" in wire:
            import re
            nonce = re.search(r"nonce ([0-9a-f]+)", wire).group(1)
            return {"content": [{"type": "tool_use", "id": "probe", "name": "my_agent_capability_probe", "input": {"nonce": nonce}}], "stop_reason": "tool_use"}
        seen.append(request.payload)
        marker = agent.conversation_store.threads.require(child.agent_thread_id).metadata.get(SUBAGENT_FIRST_REQUEST_KEY)
        markers.append(marker)
        if respond is not None:
            return respond(len(seen))
        return {"content": [{"type": "text", "text": "资料已读取。"}], "stop_reason": "end_turn"}

    monkeypatch.setattr(http, "post_json", send)
    return seen, markers


def test_package_entry_is_in_real_serialized_child_request_without_selection_model(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.capability import package_selection_runtime

    agent, _, installed = _fixture(tmp_path)
    child = _child(agent)
    before = json.dumps(child.attributes["skill_snapshot_refs"], sort_keys=True)
    seen, markers = _provider(agent, child, monkeypatch)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("selection")
        raise AssertionError("child must not select packages or models")

    monkeypatch.setattr(package_selection_runtime, "select_capability_packages", forbidden)
    monkeypatch.setattr(model_selection, "_prepare_candidate", forbidden)
    agent.run_subagent(child.id, dry_run=False, probe=False)
    assert len(seen) == 1
    wire = json.dumps(seen[0], ensure_ascii=False)
    assert "# story-a" in wire and "# story-b" not in wire
    assert installed[0].activation_id in wire
    assert markers[0]["status"] == "submitted"
    assert calls == []
    assert json.dumps(agent.subagents.load(child.id).attributes["skill_snapshot_refs"], sort_keys=True) == before


@pytest.mark.parametrize("condition", ["off", "empty", "old"])
def test_no_body_reads_without_original_package_eligibility(tmp_path, monkeypatch, condition):
    from agent_py_agent.agent.capability import package_selection_context

    agent, _, _ = _fixture(tmp_path, enabled=condition != "off")
    child = _child(agent, allowed=[] if condition == "empty" else None)
    if condition == "old":
        agent.conversation_store.threads.update_atomic(child.agent_thread_id, lambda row: replace(row, metadata={
            key: value for key, value in row.metadata.items() if key != SUBAGENT_FIRST_REQUEST_KEY
        }))
    seen, _ = _provider(agent, child, monkeypatch)
    reads = []
    original = package_selection_context.read_package_page

    def observe(*args, **kwargs):
        reads.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(package_selection_context, "read_package_page", observe)
    agent.run_subagent(child.id, dry_run=False, probe=False)
    assert len(seen) == 1
    assert reads == []
    assert "# story-a" not in json.dumps(seen[0], ensure_ascii=False)
    assert SUBAGENT_FIRST_REQUEST_KEY not in agent.conversation_store.threads.require(child.agent_thread_id).metadata


def test_plain_metadata_and_dict_cannot_initialize_host_package_eligibility(tmp_path):
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent, _, _ = _fixture(tmp_path)
    refs = [agent.current_skill_snapshot().resolve_package("story-a").to_ref()]
    params = CreateRunParams(goal="只读", thought="", plan=[], allowed_skills=["capability:story-a"],
                             attributes={"skill_snapshot_refs": refs, SUBAGENT_FIRST_REQUEST_KEY: {"status": "unsubmitted"},
                                         "package_entry_initialization": True})
    child = agent.subagents.create_run(params=params)
    assert SUBAGENT_FIRST_REQUEST_KEY not in agent.conversation_store.threads.require(child.agent_thread_id).metadata
    prepared = agent.subagents.base_service.prepare_run(params=params)
    with pytest.raises(ValueError, match="exact host preparation"):
        agent.subagents.create_run(params=params, prepared=replace(prepared, package_entry_initialization={
            "run_id": prepared.task.id, "thread_id": prepared.task.agent_thread_id,
        }))


@pytest.mark.parametrize("damage", ["missing", "incomplete", "wrong_digest", "wrong_activation", "not_allowed"])
def test_creation_requires_complete_same_generation_reference(tmp_path, damage):
    from agent_py_agent.agent.capability.subagent_package_entries import (
        prepare_subagent_entry_initialization,
    )
    from agent_py_agent.agent.subagents.services.base import CreateRunParams

    agent, _, _ = _fixture(tmp_path)
    ref = agent.current_skill_snapshot().resolve_package("story-a").to_ref()
    refs = [ref]
    if damage == "missing":
        refs = []
    elif damage == "incomplete":
        ref.pop("activation_id")
    elif damage == "wrong_digest":
        ref["content_sha256"] = "0" * 64
    elif damage == "wrong_activation":
        ref["activation_id"] = "0" * 64
    params = CreateRunParams(goal="只读", thought="", plan=[], allowed_tools=["skill_search"],
                             allowed_skills=[] if damage == "not_allowed" else ["capability:story-a"],
                             attributes={"skill_snapshot_refs": refs})
    prepared = agent.subagents.base_service.prepare_run(params=params)
    prepared = prepare_subagent_entry_initialization(agent, prepared)
    assert prepared.package_entry_initialization is None


@pytest.mark.parametrize("decision", ["ask", "deny"])
def test_child_host_read_does_not_auto_approve_original_policy(tmp_path, monkeypatch, decision):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability import package_selection_context
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy

    agent, _, _ = _fixture(tmp_path)
    child = _child(agent)
    seen, _ = _provider(agent, child, monkeypatch)
    original = ActionPolicy.decide
    decisions, reads = [], []

    def decide(self, request):
        if request.call.call_id.startswith("host-read-policy:"):
            decisions.append(decision)
            return SimpleNamespace(allowed=False, resolved_effect="read_only")
        return original(self, request)

    def forbidden(*args, **kwargs):
        reads.append(True)
        raise AssertionError("denied reads cannot run")

    monkeypatch.setattr(ActionPolicy, "decide", decide)
    monkeypatch.setattr(package_selection_context, "read_package_page", forbidden)
    agent.run_subagent(child.id, dry_run=False, probe=False)
    assert decisions == [decision] and reads == [] and len(seen) == 1
    assert "# story-a" not in json.dumps(seen[0], ensure_ascii=False)
    assert "CAPABILITY_SELECTION_ENTRY_NOT_AUTHORIZED" in json.dumps(seen[0])


def test_cancel_during_shared_read_never_delivers_child_entry(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import package_selection_context
    from agent_py_agent.agent.subagents.cancellation import (
        CancelSubagentTaskRequest,
        prepare_subagent_stops,
    )

    agent, _, _ = _fixture(tmp_path)
    child = _child(agent)
    before = child.attributes["skill_snapshot_refs"]
    seen, _ = _provider(agent, child, monkeypatch)
    original = package_selection_context._fit_entry_page
    stopped = []

    def stop(*args):
        result = original(*args)
        current = agent.subagents.load(child.id)
        stopped.append(prepare_subagent_stops(agent, [CancelSubagentTaskRequest(
            current, "conversation_user_stop", kill_process=False, source="test",
        )], include_descendants=False))
        return result

    monkeypatch.setattr(package_selection_context, "_fit_entry_page", stop)
    try:
        agent.run_subagent(child.id, dry_run=False, probe=False)
    except InterruptedError:
        pass
    assert len(stopped) == 1 and not stopped[0].failed
    assert seen == []
    assert agent.subagents.load(child.id).attributes["skill_snapshot_refs"] == before
    assert agent.conversation_store.threads.require(child.agent_thread_id).metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "preparing"


def test_reentry_and_following_tool_request_share_one_entry_read(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import subagent_package_entries

    agent, _, _ = _fixture(tmp_path)
    child = _child(agent)
    material = tmp_path / "repo" / "material.txt"
    material.write_text("合成资料", encoding="utf-8")
    calls = []
    original = subagent_package_entries._prepare_entries

    def prepare(agent, params, task, authority):
        calls.append(task.id)
        subagent_package_entries.prepare_subagent_package_entries(agent, params)
        return original(agent, params, task, authority)

    def respond(count):
        if count == 1:
            return {"content": [{"type": "tool_use", "id": "read-once", "name": "read_file",
                                 "input": {"path": str(material)}}], "stop_reason": "tool_use"}
        return {"content": [{"type": "text", "text": "完成。"}], "stop_reason": "end_turn"}

    seen, _ = _provider(agent, child, monkeypatch, respond=respond)
    monkeypatch.setattr(subagent_package_entries, "_prepare_entries", prepare)
    agent.run_subagent(child.id, dry_run=False, probe=False)
    assert calls == [child.id] and len(seen) == 2
    assert all(json.dumps(wire, ensure_ascii=False).count("# story-a") == 1 for wire in seen)


def test_revocation_during_real_child_read_keeps_old_pin_without_body(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_package
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest

    agent, store, installed = _fixture(tmp_path)
    child = _child(agent)
    before = child.attributes["skill_snapshot_refs"]
    seen, _ = _provider(agent, child, monkeypatch)
    original = plugin_package.read_plugin_member
    revoked = []

    def revoke(*args, **kwargs):
        data = original(*args, **kwargs)
        if not revoked:
            current = installed[0]
            revoked.append(store.change_activation(PluginActivationRequest(
                "stop-test-entry", current.revision, replace(current.activation, phase="revoked"),
            )))
        return data

    monkeypatch.setattr(plugin_package, "read_plugin_member", revoke)
    agent.run_subagent(child.id, dry_run=False, probe=False)
    assert len(revoked) == 1
    assert all("# story-a" not in json.dumps(wire, ensure_ascii=False) for wire in seen)
    assert agent.subagents.load(child.id).attributes["skill_snapshot_refs"] == before


def test_child_budget_delivers_full_reference_and_exact_continuation(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.plugin_package import inspect_plugin_package
    from agent_py_agent.tests.test_capability_package import content_bundle

    agent, store, _ = _fixture(tmp_path)
    agent.capability_config_path.write_text("enable_capability_package_selection: true\ncapability_bundle_max_tokens: 650\n", encoding="utf-8")
    body = "第一条方法。" * 3000
    package = inspect_plugin_package(content_bundle(files={"CAPABILITY.md": body.encode()},
                                                    change=lambda row: row.update(plugin_id="long-entry")))
    row = store.install(PluginInstallRequest(package, "long-install", 0)).installation
    activation = PluginContentActivation("long-enable", "long-entry", row.package_sha256, row.revision, row.settings_revision)
    installed = store.change_activation(PluginActivationRequest("long-enable", row.revision, activation)).installation
    child = _child(agent, allowed=["capability:long-entry"])
    seen, _ = _provider(agent, child, monkeypatch)
    agent.run_subagent(child.id, dry_run=False, probe=False)
    assert len(seen) == 1
    texts = [part["text"] for message in seen[0]["messages"] for part in message["content"]
             if isinstance(part, dict) and part.get("type") == "text"]
    text, = [text for text in texts if text.startswith("[能力包入口参考]")]
    page, = json.loads(text.split("\n", 2)[-1])["entries"]
    assert 0 < len(page["body"]) < len(body)
    assert page["body"] == body[:len(page["body"])]
    assert page["continuation"]["offset"] == len(page["body"])
    assert page["source_ref"]["activation_id"] == installed.activation_id
    assert page["has_more"] is True


def test_causal_mutation_missing_package_claim_is_detected(tmp_path, monkeypatch):
    import inspect

    from agent_py_agent.agent.agent_core.subagent import model_selection

    source = inspect.getsource(model_selection._claim_first_request_preparation)
    needle = 'marker.get("purpose") == PACKAGE_ENTRY_PURPOSE'
    assert source.count(needle) == 1
    namespace = dict(vars(model_selection))
    exec(compile(source.replace(needle, "False"), "<causal-package-claim>", "exec"), namespace)
    monkeypatch.setattr(model_selection, "_claim_first_request_preparation", namespace["_claim_first_request_preparation"])
    with pytest.raises(AssertionError, match="# story-a"):
        test_package_entry_is_in_real_serialized_child_request_without_selection_model(tmp_path, monkeypatch)


def test_causal_mutation_existing_thread_reinitialization_is_detected(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.subagent_entry_authority import PACKAGE_ENTRY_PURPOSE
    from agent_py_agent.agent.conversation import agent_thread_store

    original = agent_thread_store._ensure_existing_agent_thread

    def wrongly_initialize(thread, *, request, expected_metadata, current):
        row = original(thread, request=request, expected_metadata=expected_metadata, current=current)
        return replace(row, metadata={**row.metadata, SUBAGENT_FIRST_REQUEST_KEY: {
            "schema": SUBAGENT_FIRST_REQUEST_KEY, "status": "unsubmitted", "purpose": PACKAGE_ENTRY_PURPOSE,
            "child_run_id": request["agent_run_id"], "child_thread_id": row.thread_id,
        }})

    monkeypatch.setattr(agent_thread_store, "_ensure_existing_agent_thread", wrongly_initialize)
    with pytest.raises(AssertionError, match="host_subagent_first_request"):
        test_existing_thread_never_acquires_package_first_eligibility(tmp_path)


# LLM: 合成包、建议与首次标记都由原宿主入口产生；返回未运行 child，显式选模测试不得另造 marker 或包引用。
# 函数用途: 共用 advice 与包入口同时创建的真实离线夹具，并保持无包/关闭对照。
def _advised_package_child(tmp_path, *, allowed=True, enabled=True):
    from agent_py_agent.agent.capability.subagent_package_entries import (
        prepare_subagent_entry_initialization,
    )
    from agent_py_agent.agent.conversation.decision_policy import decision_owner_ref
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.plugin_package import inspect_plugin_package
    from agent_py_agent.agent.settings.decision_settings import execute_decision_settings_operation
    from agent_py_agent.agent.settings.model_profiles import model_profile_generation
    from agent_py_agent.agent.settings.thread_model_selection import (
        PendingSubagentModelAdvice,
    )
    from agent_py_agent.agent.subagents.services.base import CreateRunParams
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    from agent_py_agent.tests.test_capability_package import content_bundle
    from agent_py_agent.tests.test_subagent_first_request_selection import _automatic_child

    agent, source, key = _automatic_child(tmp_path, persist_child=False)
    agent.config.enable_plugins = True
    path = tmp_path / "capability.yaml"
    path.write_text(f"enable_capability_package_selection: {str(enabled).lower()}\n", encoding="utf-8")
    agent.capability_config_path = path
    agent._capability_config_runtime_snapshot = None
    store = PluginInstallStore(resolve_owner_home(agent.home_paths.root))
    package = inspect_plugin_package(content_bundle(files={"CAPABILITY.md": b"ENTRY-WITH-JEV"}))
    row = store.install(PluginInstallRequest(package, "advice-package", 0)).installation
    activation = PluginContentActivation("advice-enable", package.manifest.plugin_id, row.package_sha256, row.revision, row.settings_revision)
    store.change_activation(PluginActivationRequest("advice-enable", row.revision, activation))
    snapshot = agent.current_skill_snapshot()
    ref = snapshot.packages[0].to_ref()
    params = CreateRunParams(goal="检查材料", thought="", plan=[], allowed_tools=["read_file", "skill_search"],
                             allowed_skills=[ref["stable_id"]] if allowed else [],
                             attributes={"conversation_thread_id": source.thread_id, "skill_snapshot_refs": [ref] if allowed else []})
    prepared = agent.subagents.base_service.prepare_run(params=params)
    settings = execute_decision_settings_operation(agent, "read", {}, thread_id=source.thread_id)
    advice = PendingSubagentModelAdvice(key, "create-operation", decision_owner_ref(agent), source.thread_id,
        "", "", settings["revision"]["owner"], settings["revision"]["thread"], prepared.task.id, prepared.task.agent_thread_id,
        source_model_generation=model_profile_generation(agent, key))
    prepared = prepare_subagent_entry_initialization(agent, replace(prepared, model_advice=advice))
    child = agent.subagents.create_run(params=params, prepared=prepared)
    marker = agent.conversation_store.threads.require(child.agent_thread_id).metadata[SUBAGENT_FIRST_REQUEST_KEY]
    assert marker == {"schema": SUBAGENT_FIRST_REQUEST_KEY, "status": "unsubmitted", "operation_id": "create-operation",
                      "child_run_id": child.id, "child_thread_id": child.agent_thread_id}
    return agent, child, key


def test_existing_advice_marker_and_adoption_keep_entry_in_captured_payload(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings.thread_model_selection import SUBAGENT_MODEL_ADVICE_KEY
    from agent_py_agent.tests.test_subagent_first_request_selection import (
        _install_automatic_provider,
    )

    agent, child, key = _advised_package_child(tmp_path)
    material = tmp_path / "material.txt"
    material.write_text("合成材料", encoding="utf-8")
    calls, _ = _install_automatic_provider(monkeypatch, agent, child, material)
    result = agent.run_subagent(child.id, dry_run=False, probe=False)
    assert len(calls) == 2, result
    assert calls[0][1].metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "adopted"
    assert calls[0][1].model_profile_id == key
    assert all("ENTRY-WITH-JEV" in json.dumps(wire) for wire, _ in calls)


@pytest.mark.parametrize("selected_profile", ["advice", "inherited"])
@pytest.mark.parametrize("selection_time", ["before_scope", "during_claim"])
def test_explicit_model_selection_keeps_package_first_payload(tmp_path, monkeypatch, selected_profile, selection_time):
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.capability import package_selection_context
    from agent_py_agent.agent.settings.thread_model_selection import (
        SUBAGENT_MODEL_ADVICE_KEY,
        thread_model_profile_id,
    )

    agent, child, key = _advised_package_child(tmp_path)
    selected = key if selected_profile == "advice" else "default"
    threads = agent.conversation_store.threads
    original_marker = dict(threads.require(child.agent_thread_id).metadata[SUBAGENT_FIRST_REQUEST_KEY])
    pins = json.dumps(child.attributes["skill_snapshot_refs"], sort_keys=True)
    if selection_time == "before_scope":
        thread_model_profile_id(agent, child.agent_thread_id, select=selected)
        assert threads.require(child.agent_thread_id).metadata[SUBAGENT_FIRST_REQUEST_KEY] == original_marker
    else:
        original_update = threads.update_atomic

        def select_before_claim(thread_id, update):
            if thread_id == child.agent_thread_id and update.__name__ == "claim":
                thread_model_profile_id(agent, child.agent_thread_id, select=selected)
            return original_update(thread_id, update)

        monkeypatch.setattr(threads, "update_atomic", select_before_claim)
    seen, markers = _provider(agent, child, monkeypatch)
    reads, candidates = [], []
    original_read = package_selection_context.read_package_page

    def read(*args, **kwargs):
        reads.append(True)
        return original_read(*args, **kwargs)

    def forbidden(*args, **kwargs):
        candidates.append(True)
        raise AssertionError("explicit selection cannot adopt advice")

    monkeypatch.setattr(package_selection_context, "read_package_page", read)
    monkeypatch.setattr(model_selection, "_prepare_candidate", forbidden)
    result = agent.run_subagent(child.id, dry_run=False, probe=False)
    assert result.ok and len(seen) == 1
    assert "ENTRY-WITH-JEV" in json.dumps(seen[0]), "first wire must retain package entry after explicit selection"
    assert reads == [True] and candidates == []
    assert seen[0]["model"] == ("MiniMax-M3" if selected_profile == "advice" else "MiniMax-M2.7")
    thread = threads.require(child.agent_thread_id)
    assert thread.model_profile_id == selected
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["status"] == "retained"
    assert thread.metadata[SUBAGENT_MODEL_ADVICE_KEY]["reason"] == "explicit_model_selection"
    assert thread.metadata[SUBAGENT_FIRST_REQUEST_KEY] == markers[0]
    assert markers[0]["status"] == "submitted" and "purpose" not in markers[0]
    assert json.dumps(agent.subagents.load(child.id).attributes["skill_snapshot_refs"], sort_keys=True) == pins


@pytest.mark.parametrize("condition", ["off", "empty", "old"])
def test_retained_advice_cannot_grant_package_access(tmp_path, monkeypatch, condition):
    from agent_py_agent.agent.capability import package_selection_context
    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id

    agent, child, key = _advised_package_child(tmp_path, allowed=condition != "empty", enabled=condition != "off")
    threads = agent.conversation_store.threads
    if condition == "old":
        threads.update_atomic(child.agent_thread_id, lambda row: replace(row, metadata={
            key: value for key, value in row.metadata.items() if key != SUBAGENT_FIRST_REQUEST_KEY
        }))
    thread_model_profile_id(agent, child.agent_thread_id, select=key)
    seen, _ = _provider(agent, child, monkeypatch)
    reads = []
    original_read = package_selection_context.read_package_page

    def read(*args, **kwargs):
        reads.append(True)
        return original_read(*args, **kwargs)

    monkeypatch.setattr(package_selection_context, "read_package_page", read)
    result = agent.run_subagent(child.id, dry_run=False, probe=False)
    assert result.ok and len(seen) == 1
    assert reads == [] and "ENTRY-WITH-JEV" not in json.dumps(seen[0])
    if condition == "old":
        assert SUBAGENT_FIRST_REQUEST_KEY not in threads.require(child.agent_thread_id).metadata


@pytest.mark.parametrize("failure", ["already_claimed", "write_error"])
def test_retained_advice_still_requires_winning_original_claim(tmp_path, monkeypatch, failure):
    from agent_py_agent.agent.capability import package_selection_context
    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id

    agent, child, key = _advised_package_child(tmp_path)
    thread_model_profile_id(agent, child.agent_thread_id, select=key)
    threads = agent.conversation_store.threads
    original_update = threads.update_atomic
    seen, _ = _provider(agent, child, monkeypatch)
    reads = []
    original_read = package_selection_context.read_package_page

    def read(*args, **kwargs):
        reads.append(True)
        return original_read(*args, **kwargs)

    def conflict(thread_id, update):
        if thread_id == child.agent_thread_id and update.__name__ == "claim":
            if failure == "write_error":
                raise OSError("offline claim write failed")
            original_update(thread_id, lambda row: replace(row, metadata={
                **row.metadata, SUBAGENT_FIRST_REQUEST_KEY: {
                    **row.metadata[SUBAGENT_FIRST_REQUEST_KEY], "status": "preparing",
                    "attempt_id": agent.subagents.load(child.id).runner_active_attempt_id,
                },
            }))
        return original_update(thread_id, update)

    monkeypatch.setattr(package_selection_context, "read_package_page", read)
    monkeypatch.setattr(threads, "update_atomic", conflict)
    if failure == "write_error":
        with pytest.raises(OSError, match="offline claim write failed"):
            agent.run_subagent(child.id, dry_run=False, probe=False)
        assert seen == []
        assert threads.require(child.agent_thread_id).metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "unsubmitted"
    else:
        result = agent.run_subagent(child.id, dry_run=False, probe=False)
        assert result.ok and len(seen) == 1
        assert "ENTRY-WITH-JEV" not in json.dumps(seen[0])
    assert reads == []


def test_child_compact_recovery_does_not_repeat_entry_preparation(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import subagent_package_entries
    from agent_py_agent.tests.test_subagent_runtime_compact import _OverflowThenCompleteChildBackend

    agent, _, _ = _fixture(tmp_path)
    child = _child(agent)
    for role, text in (("user", "此前提供的合成事实"), ("assistant", "此前记录")):
        agent.conversation_store.messages.append({"thread_id": child.agent_thread_id, "role": role, "content": text,
                                                  "metadata": {"conversation_request_id": "prior-fixture"}})
    backend = _OverflowThenCompleteChildBackend()
    agent.backend = backend
    calls = []
    original = subagent_package_entries._prepare_entries

    def prepare(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(subagent_package_entries, "_prepare_entries", prepare)
    result = agent.run_subagent(child.id, dry_run=False, probe=False)
    assert result.ok
    assert len(backend.model_prompts) == 2 and len(backend.summary_prompts) == 1
    assert calls == [True]
    assert "# story-a" in json.dumps(backend.model_messages[0], ensure_ascii=False)


def test_recursive_creation_keeps_same_pin_and_initializes_original_child_marker(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    agent, _, _ = _fixture(tmp_path)
    parent = _child(agent)
    previous = set_current_subagent_context(agent, run_id=parent.id, task_attributes=parent.attributes)
    try:
        result = CreateSubagentsTool(agent).execute({
            "items": [{"goal": "复核原材料", "allowed_skills": ["capability:story-a"]}], "defer_start": True,
        })
    finally:
        restore_current_subagent_context(agent, previous)
    assert result.ok, result.output
    children = [row for row in agent.subagents.list_runs() if row.parent_id == parent.id]
    child, = children
    assert child.attributes["skill_snapshot_refs"] == parent.attributes["skill_snapshot_refs"]
    thread = agent.conversation_store.threads.require(child.agent_thread_id)
    assert thread.metadata[SUBAGENT_FIRST_REQUEST_KEY]["status"] == "unsubmitted"
    seen, _ = _provider(agent, child, monkeypatch)
    agent.run_subagent(child.id, dry_run=False, probe=False)
    assert len(seen) == 1 and "# story-a" in json.dumps(seen[0], ensure_ascii=False)


def test_failed_first_send_does_not_restore_entry_preparation(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.subagent.model_selection import (
        subagent_first_request_scope,
    )
    from agent_py_agent.agent.capability import subagent_package_entries

    agent, _, _ = _fixture(tmp_path)
    child = _child(agent)
    reads = []
    original = subagent_package_entries._prepare_entries

    def prepare(*args):
        reads.append(True)
        return original(*args)

    def fail(_count):
        raise ValueError("offline failed provider send")

    seen, markers = _provider(agent, child, monkeypatch, respond=fail)
    monkeypatch.setattr(subagent_package_entries, "_prepare_entries", prepare)
    agent.run_subagent(child.id, dry_run=False, probe=False)
    assert seen and reads == [True]
    marker = agent.conversation_store.threads.require(child.agent_thread_id).metadata[SUBAGENT_FIRST_REQUEST_KEY]
    assert marker == markers[0] and marker["status"] == "submitted"
    with subagent_first_request_scope(agent, child.id, marker["attempt_id"]) as preparation:
        assert preparation is None


# LLM: 核查用例（不改产品）：能力配置在子请求读入口时读不了（路径成了目录 / 没有读权限）时，统一入口返回 None，
#   prepare_subagent_package_entries 先经 subagent_entries_enabled 落到默认 CapabilityConfig()（选择开关默认关）直接返回，
#   _prepare_entries 里的 config.capability_bundle_max_tokens 根本不会拿到 None；即便 TOCTOU 让它拿到 None，
#   AttributeError 也被 prepare_subagent_package_entries 的 except Exception 接住记成 CAPABILITY_ENTRY_PREPARATION_UNAVAILABLE。
# 函数用途: 锁定"能力配置读不了时子请求照常发出、不崩"的事实。
@pytest.mark.parametrize("damage", ["directory", "unreadable", "toctou"])
def test_child_request_survives_unreadable_capability_config(tmp_path, monkeypatch, damage):
    from agent_py_agent.agent.capability import subagent_package_entries as entries_module
    from agent_py_agent.agent.capability.runtime_config_reload import capability_config_for_agent
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest
    from agent_py_agent.agent.plugin_package import inspect_plugin_package
    from agent_py_agent.tests.test_capability_package import content_bundle

    agent, store, _ = _fixture(tmp_path)
    body = "第一条方法。" * 300
    package = inspect_plugin_package(content_bundle(files={"CAPABILITY.md": body.encode()},
                                                    change=lambda row: row.update(plugin_id="short-entry")))
    row = store.install(PluginInstallRequest(package, "short-install", 0)).installation
    activation = PluginContentActivation("short-enable", "short-entry", row.package_sha256, row.revision, row.settings_revision)
    store.change_activation(PluginActivationRequest("short-enable", row.revision, activation))
    child = _child(agent, allowed=["capability:short-entry"])
    path = agent.capability_config_path
    reached = {"prepare_entries": 0}
    if damage == "directory":
        path.unlink()
        path.mkdir()
    elif damage == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root 不受读权限限制，读不了的情形无法构造")
        path.chmod(0)
    else:
        # TOCTOU：开关检查那次读到了，只有 _prepare_entries 里那次读不到（返回 None）
        import sys as _sys

        real = entries_module.capability_config_for_agent

        def flaky(target):
            return None if _sys._getframe(1).f_code.co_name == "_prepare_entries" else real(target)

        monkeypatch.setattr(entries_module, "capability_config_for_agent", flaky)
    if damage != "toctou":
        agent._capability_config_runtime_snapshot = None  # 丢掉创建时缓存的快照，逼子请求真的去读文件
        assert capability_config_for_agent(agent) is None  # 前提：统一入口在这两种情况下返回 None
    original_prepare = entries_module._prepare_entries

    def counting_prepare(*args, **kwargs):
        reached["prepare_entries"] += 1
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(entries_module, "_prepare_entries", counting_prepare)

    seen, _ = _provider(agent, child, monkeypatch)
    agent.run_subagent(child.id, dry_run=False, probe=False)  # 任何情况下都不能炸出 AttributeError

    assert len(seen) == 1, "子请求照常发出"
    texts = [part["text"] for message in seen[0]["messages"] for part in message["content"]
             if isinstance(part, dict) and part.get("type") == "text"]
    assert not [text for text in texts if text.startswith("[能力包入口参考]")], "读不了配置时不带入口正文"
    if damage == "toctou":
        assert reached["prepare_entries"] == 1
        assert any("CAPABILITY_ENTRY_PREPARATION_UNAVAILABLE" in text for text in texts), "AttributeError 被接住并记成结构化提示"
    else:
        assert reached["prepare_entries"] == 0, "开关检查已按默认值（关）返回，根本没走到 _prepare_entries"
    if damage == "unreadable":
        path.chmod(0o600)
