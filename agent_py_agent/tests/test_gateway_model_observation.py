"""Gateway 原模型冻结与车道后一次观察的组合验收；fake 决策经过原账本，不启动服务或访问供应商。"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import gateway_model_observation as model_observation
from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.backends import gateway_helpers
from agent_py_agent.agent.backends.decision_protocol import DecisionAnswer, DecisionResponse
from agent_py_agent.agent.conversation import decision_policy, decision_service
from agent_py_agent.agent.conversation.history_seed import history_source_provider_messages
from agent_py_agent.agent.gateway_parts import (
    request_binding,
    request_context,
    request_execution,
)
from agent_py_agent.agent.gateway_parts.io import (
    gateway_request_fingerprint,
    read_json_file,
    update_json_file_atomic,
)
from agent_py_agent.agent.llm_scale import hot_path
from agent_py_agent.agent.settings import model_profiles
from agent_py_agent.agent.settings.decision_settings_projection import (
    decision_point_mode_from_read,
    decision_settings_projection,
)
from agent_py_agent.agent.settings.decision_settings_schema import empty_decision_settings
from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_gateway_request_runtime_errors import _make_agent
from agent_py_agent.tests.test_model_profiles import add


@pytest.fixture(autouse=True)
def _optional_admission(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "2")
    monkeypatch.setenv("LLM_ADMISSION_WAIT_SECONDS", "0")
    hot_path.reset_hot_path_admission_for_test()
    with decision_policy._LOCK:
        decision_policy._FAILURES.clear()
    yield
    hot_path.reset_hot_path_admission_for_test()


# LLM: 原 SimpleAgent、owner 目录、线程和队列事务均真实；模型仅用 echo 与 fake Decision，不加载私有配置或网络。
# 函数用途: 创建隔离 Gateway 请求并通过原设置服务开关观察，供身份/排队/账本验收。
def prepared(tmp_path, *, mode="observe"):
    agent, paths = _make_agent(tmp_path)
    agent.config.enable_tools = False
    candidate, _ = add(agent, model_name="candidate-large", model_context_window_tokens=1_000_000)
    profile, _ = decision(agent)
    patch(agent, {"enabled": mode != "disabled", "profile_id": profile,
                  "points.model_selection.mode": "observe" if mode == "disabled" else mode})
    request = {"id": "observe-request", "kind": "ask", "prompt": "请整理当前资料并给出下一步建议",
               "status": "processing", "turn_phase": "open", "execution_attempt_id": "transport-one",
               "conversation": {"canonical_user_id": agent.home_paths.owner_id, "channel": "chat",
                                "channel_conversation_id": "session", "channel_user_id": agent.home_paths.owner_id}}
    path = paths.processing / f"{request['id']}.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    context = request_context.GatewayAskRunContext(agent, request, path, paths.responses / "response.json", request["id"], None)
    preflight = request_context.preflight_gateway_conversation(request_context.GatewayConversationLoadRequest(
        agent, request, request["id"], request["prompt"],
    ))
    return SimpleNamespace(agent=agent, context=context, paths=paths, thread_id=preflight.thread_id,
                           candidate=candidate, decision_profile=profile)


# LLM: fake 实际经过原 bounded worker 与 provider observer，只替换供应商；回执带真实绑定，不能绕过 service 校验。
# 类用途: 记录请求、线程和原 HTTP 尝试，用事件注入故障/停止而不访问收费模型。
class DecisionBackend:
    name = "fake-decision"
    model_name = "jev-test"

    def __init__(self, choice, action=None):
        self.choice, self.action, self.calls = choice, action, []

    def decide(self, request, *, deadline):
        self.calls.append((request, deadline, threading.get_ident()))
        gateway_helpers._emit_provider_attempt({"attempt_id": "test-http", "status": "started", "method": "POST"})
        if self.action:
            self.action(request)
        return DecisionResponse(request.binding, request.input_digest, self.model_name, self.model_name,
                                (DecisionAnswer("model", "choice", self.choice),), b'{"input_tokens":17}', True)


def install_backend(monkeypatch, fixture, *, action=None, choice=None):
    backend = DecisionBackend(choice or fixture.candidate, action)
    monkeypatch.setattr(decision_service, "decision_backend_from_profile", lambda _config: backend)
    return backend


def capture_main(monkeypatch):
    calls = []

    def execute(context, prompt, conversation, *, observer=None):
        calls.append((context.agent.config.model_name, context.agent.backend, prompt, conversation))
        return "original-main"

    monkeypatch.setattr(request_execution, "_execute_gateway_conversation_turn", execute)
    return calls


@pytest.mark.parametrize("mode", ["observe", "apply"])
def test_lane_hook_observes_once_and_preserves_original_model_and_request(tmp_path, monkeypatch, mode):
    fixture = prepared(tmp_path, mode=mode)
    backend = install_backend(monkeypatch, fixture)
    main = capture_main(monkeypatch)
    initial_model = fixture.agent.config.model_name
    fingerprint = gateway_request_fingerprint(fixture.context.request, fixture.context.request_id)
    events = []
    original_repair = request_context.request_history.repair_gateway_conversation_messages

    def repair(*args):
        assert len(backend.calls) == 1
        events.append("repair")
        return original_repair(*args)

    monkeypatch.setattr(request_context.request_history, "repair_gateway_conversation_messages", repair)
    assert request_execution._run_gateway_ask(fixture.context) == "original-main"
    assert len(backend.calls) == 1 and len(main) == 1 and events == ["repair"]
    assert main[0][0] == initial_model
    assert backend.calls[0][2] != threading.get_ident()
    current = fixture.agent.conversation_store.threads.load(fixture.thread_id)
    assert current.model_profile_id == "default" and current.model_selection_source == "default"
    marker = read_json_file(fixture.context.request_path)[request_binding.MODEL_OBSERVATION_KEY]
    assert marker["status"] == "observed" and marker["choice"] == fixture.candidate
    assert marker["adopted"] is False and marker["adoption_eligibility"] == "not_evaluated"
    assert marker["requested_mode"] == mode
    assert gateway_request_fingerprint(fixture.context.request, fixture.context.request_id) == fingerprint
    assert "private-secret" not in json.dumps(marker) and "请整理" not in json.dumps(marker)
    summary = model_call_summary(fixture.agent, request_id=fixture.context.request_id)
    bucket = summary["purpose_breakdown"]["decision"]
    assert bucket["provider_http_attempt_count"] == 1
    assert bucket["usage_breakdown"]["provider"]["input_tokens"] == 17
    replay = replace(fixture.context, request=read_json_file(fixture.context.request_path))
    with model_profiles.capture_selected_model_read() as captured:
        model_profiles.selected_model_config(fixture.agent)
    model_observation.GatewayModelObservation(replay, captured[0],
        fixture.agent.conversation_store.claims.load(fixture.thread_id))(current)
    assert len(backend.calls) == 1


@pytest.mark.parametrize("mode", ["off", "disabled"])
def test_off_hook_needs_no_io_or_candidate_or_service_and_keeps_loaded_input(tmp_path, monkeypatch, mode):
    fixture = prepared(tmp_path, mode=mode)
    thread = fixture.agent.conversation_store.threads.load(fixture.thread_id)
    with model_profiles.capture_selected_model_read() as captured:
        model_profiles.selected_model_config(fixture.agent)
    observer = model_observation.GatewayModelObservation(fixture.context, captured[0], {
        "status": "running", "claim_id": "existing-claim", "thread_id": thread.thread_id,
        "task_id": f"gateway:{fixture.context.request_id}",
    })
    before = fixture.context.request_path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("关闭观察不得读盘、写盘或准备决策")

    monkeypatch.setattr(model_observation, "_candidates", forbidden)
    monkeypatch.setattr(decision_service, "begin_decision_stage", forbidden)
    monkeypatch.setattr(model_profiles, "read_model_profiles", forbidden)
    monkeypatch.setattr(type(fixture.context.request_path), "read_text", forbidden)
    monkeypatch.setattr(type(fixture.context.request_path), "write_text", forbidden)
    observer(thread)
    assert observer.params is None and fixture.context.request_path.read_bytes() == before
    assert request_binding.MODEL_OBSERVATION_KEY not in fixture.context.request


@pytest.mark.parametrize("initial,after,expected", [("off", "observe", 1), ("observe", "off", 0)])
def test_thread_mode_changed_while_queued_is_read_after_lane(tmp_path, monkeypatch, initial, after, expected):
    fixture = prepared(tmp_path, mode="observe")
    patch(fixture.agent, {"points.model_selection.mode": initial}, scope="thread", thread_id=fixture.thread_id)
    backend = install_backend(monkeypatch, fixture)
    main = capture_main(monkeypatch)
    queued, proceed = threading.Event(), threading.Event()
    original_lane = request_binding.gateway_conversation_execution_lane

    @contextmanager
    def lane(context, thread_id):
        queued.set()
        assert proceed.wait(3)
        with original_lane(context, thread_id) as claim:
            yield claim

    monkeypatch.setattr(request_binding, "gateway_conversation_execution_lane", lane)
    errors = []

    def execute():
        try:
            request_execution._run_gateway_ask(fixture.context)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=execute)
    worker.start()
    assert queued.wait(3)
    patch(fixture.agent, {"points.model_selection.mode": after}, scope="thread", thread_id=fixture.thread_id)
    proceed.set()
    worker.join(5)
    assert not worker.is_alive() and not errors
    assert len(backend.calls) == expected and len(main) == 1


@pytest.mark.parametrize("fault", ["provider", "timeout", "invalid_answer"])
def test_optional_failure_keeps_original_main(tmp_path, monkeypatch, fault):
    fixture = prepared(tmp_path)

    def fail(_request):
        if fault == "provider":
            raise OSError("private provider response")
        if fault == "timeout":
            raise TimeoutError("private timeout detail")

    backend = install_backend(monkeypatch, fixture, action=fail, choice="not-in-candidates" if fault == "invalid_answer" else None)
    main = capture_main(monkeypatch)
    assert request_execution._run_gateway_ask(fixture.context) == "original-main"
    assert len(backend.calls) == 1 and len(main) == 1
    marker = fixture.context.request[request_binding.MODEL_OBSERVATION_KEY]
    assert marker["status"] in {"error", "deadline"} and marker["adopted"] is False
    assert "private" not in json.dumps(marker)


@pytest.mark.parametrize("field", ["runtime_authority", "active_turn_recovery", "model_selection_observation", "resume_context", "system_task"])
def test_recovery_or_existing_marker_or_control_never_reobserves(tmp_path, monkeypatch, field):
    fixture = prepared(tmp_path)
    fixture.context.request[field] = ({"schema_version": "gateway_active_turn_recovery.v1", "request_id": fixture.context.request_id}
                                      if field == "active_turn_recovery" else True)
    capture_main(monkeypatch)
    backend = install_backend(monkeypatch, fixture)
    assert request_execution._run_gateway_ask(fixture.context) == "original-main"
    assert not backend.calls


def test_cancelled_exact_attempt_cannot_send_and_never_runs_main(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    main = capture_main(monkeypatch)
    backend = install_backend(monkeypatch, fixture)
    candidates = model_observation._candidates

    def cancel(agent, deadline):
        rows = candidates(agent, deadline)
        update_json_file_atomic(fixture.context.request_path, lambda current: {**current, "cancel_requested": True})
        return rows

    monkeypatch.setattr(model_observation, "_candidates", cancel)
    with pytest.raises(InterruptedError):
        request_execution._run_gateway_ask(fixture.context)
    assert not backend.calls and not main
    assert fixture.context.request[request_binding.MODEL_OBSERVATION_KEY]["status"] == "cancelled"


def test_preparation_failure_settles_original_decision_usage(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    backend = install_backend(monkeypatch, fixture)

    def fail(*args):
        raise OSError("history unavailable")

    monkeypatch.setattr(request_context.request_history, "repair_gateway_conversation_messages", fail)
    with pytest.raises(OSError, match="history unavailable"):
        request_execution._run_gateway_ask(fixture.context)
    events, errors = fixture.agent.conversation_store.model_usage.events_report(fixture.thread_id)
    assert not errors and len(events) == 1 and len(backend.calls) == 1
    assert events[0].model_calls["purpose_breakdown"]["decision"]["usage_breakdown"]["provider"]["input_tokens"] == 17


@pytest.mark.parametrize("owner,thread", [({}, {}), ({"enabled": True, "points.model_selection.mode": "observe"}, {}),
    ({"enabled": True, "points.model_selection.mode": "apply"}, {"enabled": False}),
    ({"enabled": False}, {"enabled": True, "points.model_selection.mode": "observe"})])
def test_pure_mode_matches_original_projection_without_connection_resolution(tmp_path, monkeypatch, owner, thread):
    fixture = prepared(tmp_path)
    data = model_profiles.read_model_profiles(model_profiles.model_profiles_path(fixture.agent.home_paths))
    data["decision_settings"] = {**empty_decision_settings(), "overrides": owner}
    current = replace(fixture.agent.conversation_store.threads.load(fixture.thread_id),
                      decision_settings={**empty_decision_settings(), "overrides": thread})
    expected = decision_settings_projection(fixture.agent, data, current)["effective"]["points"]["model_selection"]["effective_mode"]
    assert decision_point_mode_from_read(fixture.agent.config, data["decision_settings"], current,
                                         point="model_selection") == expected


def test_off_matches_original_gateway_runner_input_bytes_and_io(tmp_path, monkeypatch):
    fixture = prepared(tmp_path, mode="off")
    thread_model_profile_id(fixture.agent, fixture.thread_id)
    observed, file_reads, file_writes = [], [], []
    original_read, original_write = Path.read_text, Path.write_text

    def read(path, *args, **kwargs):
        file_reads.append(str(path))
        return original_read(path, *args, **kwargs)

    def write(path, *args, **kwargs):
        comparable = path.with_name(path.name.rsplit(".", 2)[0] + ".tmp") if path.suffix == ".tmp" else path
        file_writes.append(str(comparable))
        return original_write(path, *args, **kwargs)

    @contextmanager
    def held_lane(context, thread_id):
        yield {"status": "running", "claim_id": "test-held-lane", "thread_id": thread_id,
               "task_id": f"gateway:{context.request_id}"}

    def original_runner_input(context, prompt, conversation, *, observer=None):
        params = request_execution._gateway_run_params(request_execution._GatewayRunParamsRequest(
            context.request, context, conversation, prompt,
        ))
        observed.append(json.dumps({"prompt": prompt, "inject": params.inject, "prompt_files": params.prompt_files,
            "system_prompt_override": params.system_prompt_override, "resume_context": params.resume_context,
            "history": ([] if conversation.history_source is None else list(history_source_provider_messages(conversation.history_source))), "summary": conversation.compact_summary,
            "model_name": context.agent.config.model_name, "backend": context.agent.config.model_backend},
            ensure_ascii=False, sort_keys=True).encode())
        return "ok"

    monkeypatch.setattr(request_binding, "gateway_conversation_execution_lane", held_lane)
    monkeypatch.setattr(request_execution, "_execute_gateway_conversation_turn", original_runner_input)
    request_execution._run_gateway_ask(fixture.context)  # 原后端与读取缓存先按正常路径预热。
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", write)
    request_execution._run_gateway_ask(fixture.context)
    new_reads, new_writes = list(file_reads), list(file_writes)
    file_reads.clear()
    file_writes.clear()
    with monkeypatch.context() as baseline:
        baseline.setattr(model_observation.GatewayModelObservation, "__call__", lambda self, thread: None)
        request_execution._run_gateway_ask(fixture.context)
    assert observed[-1] == observed[-2]
    assert file_reads == new_reads and file_writes == new_writes
    assert request_binding.MODEL_OBSERVATION_KEY not in fixture.context.request


def test_queued_manual_model_change_keeps_prequeue_frozen_model(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    backend = install_backend(monkeypatch, fixture)
    main = capture_main(monkeypatch)
    original_name = fixture.agent.config.model_name
    original_lane = request_binding.gateway_conversation_execution_lane

    @contextmanager
    def lane(context, thread_id):
        thread_model_profile_id(fixture.agent, thread_id, select=fixture.candidate)
        with original_lane(context, thread_id) as claim:
            yield claim

    monkeypatch.setattr(request_binding, "gateway_conversation_execution_lane", lane)
    request_execution._run_gateway_ask(fixture.context)
    assert len(backend.calls) == 1 and main[0][0] == original_name
    marker = fixture.context.request[request_binding.MODEL_OBSERVATION_KEY]
    assert marker["frozen_profile_id"] == "default"
    assert marker["observed_thread_profile_id"] == fixture.candidate
    assert fixture.agent.conversation_store.threads.load(fixture.thread_id).model_profile_id == fixture.candidate


def test_explicit_same_model_during_decision_is_not_overwritten(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    capture_main(monkeypatch)

    def select_same(_request):
        thread_model_profile_id(fixture.agent, fixture.thread_id, select="default")

    install_backend(monkeypatch, fixture, action=select_same)
    request_execution._run_gateway_ask(fixture.context)
    current = fixture.agent.conversation_store.threads.load(fixture.thread_id)
    marker = fixture.context.request[request_binding.MODEL_OBSERVATION_KEY]
    assert current.model_profile_id == "default" and current.model_selection_source == "explicit"
    assert current.model_selection_revision == marker["observed_selection_revision"] + 1
    assert marker["adopted"] is False


def test_compact_reload_does_not_reuse_initial_observation_hook(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    backend = install_backend(monkeypatch, fixture)

    def main(context, prompt, conversation, *, observer=None):
        params = request_execution._gateway_run_params(request_execution._GatewayRunParamsRequest(
            context.request, context, conversation, prompt,
        ))
        from agent_py_agent.agent.gateway_compact_context import build_gateway_compact_load_request

        reload_request = build_gateway_compact_load_request(context, prompt, params, ())
        assert reload_request.on_thread_loaded is None
        request_context.gateway_conversation_context(reload_request)
        return "after-compact-reload"

    monkeypatch.setattr(request_execution, "_execute_gateway_conversation_turn", main)
    assert request_execution._run_gateway_ask(fixture.context) == "after-compact-reload"
    assert len(backend.calls) == 1


def test_started_marker_survives_caller_failure_without_second_send(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    backend = install_backend(monkeypatch, fixture)
    capture_main(monkeypatch)
    monkeypatch.setattr(request_binding.GatewayModelObservationWriter, "finish", lambda *_args: (_ for _ in ()).throw(OSError("disk failure")))
    request_execution._run_gateway_ask(fixture.context)
    saved = read_json_file(fixture.context.request_path)
    assert saved[request_binding.MODEL_OBSERVATION_KEY]["status"] == "started"
    with model_profiles.capture_selected_model_read() as captured:
        model_profiles.selected_model_config(fixture.agent)
    replay = replace(fixture.context, request=saved)
    model_observation.GatewayModelObservation(replay, captured[0], fixture.agent.conversation_store.claims.load(fixture.thread_id))(
        fixture.agent.conversation_store.threads.load(fixture.thread_id))
    assert len(backend.calls) == 1


def test_original_gateway_runner_settles_decision_usage_once(tmp_path, monkeypatch):
    fixture = prepared(tmp_path)
    backend = install_backend(monkeypatch, fixture)
    result = request_execution._handle_gateway_request(fixture.agent, fixture.context.request_path)
    assert result["ok"] is True
    assert len(backend.calls) == 1
    events, errors = fixture.agent.conversation_store.model_usage.events_report(fixture.thread_id)
    assert not errors and len(events) == 1
    assert events[0].model_calls["purpose_breakdown"]["decision"]["usage_breakdown"]["provider"]["input_tokens"] == 17


@pytest.mark.parametrize("mode,after,expected", [("off", "observe", 0), ("observe", "off", 0)])
def test_owner_gate_uses_existing_read_and_service_checks_revocation(tmp_path, monkeypatch, mode, after, expected):
    fixture = prepared(tmp_path, mode=mode)
    backend = install_backend(monkeypatch, fixture)
    main = capture_main(monkeypatch)
    original_lane = request_binding.gateway_conversation_execution_lane

    @contextmanager
    def lane(context, thread_id):
        patch(fixture.agent, {"points.model_selection.mode": after})
        with original_lane(context, thread_id) as claim:
            yield claim

    monkeypatch.setattr(request_binding, "gateway_conversation_execution_lane", lane)
    request_execution._run_gateway_ask(fixture.context)
    assert len(backend.calls) == expected and len(main) == 1


@pytest.mark.parametrize("kind", [InterruptedError, KeyboardInterrupt])
def test_user_cancellation_is_not_optional_failure(tmp_path, monkeypatch, kind):
    fixture = prepared(tmp_path)
    main = capture_main(monkeypatch)

    def cancel(_request):
        raise kind("user stopped")

    install_backend(monkeypatch, fixture, action=cancel)
    with pytest.raises(kind):
        request_execution._run_gateway_ask(fixture.context)
    assert not main


def test_selected_read_capture_is_local_and_cleared_on_exit(tmp_path):
    fixture = prepared(tmp_path)
    with model_profiles.capture_selected_model_read() as outer:
        model_profiles.selected_model_config(fixture.agent)
        with model_profiles.capture_selected_model_read() as inner:
            model_profiles.selected_model_config(fixture.agent, profile_id=fixture.candidate)
        assert outer[0].profile_id == "default" and inner[0].profile_id == fixture.candidate
    model_profiles.selected_model_config(fixture.agent, profile_id=fixture.candidate)
    assert len(outer) == 1 and outer[0].profile_id == "default"


@pytest.mark.parametrize("mode", ["observe", "apply"])
def test_question_explains_usage_tags_and_apply_asks_for_the_best_semantic_match(tmp_path, monkeypatch, mode):
    fixture = prepared(tmp_path, mode=mode)
    backend = install_backend(monkeypatch, fixture)
    capture_main(monkeypatch)
    request_execution._run_gateway_ask(fixture.context)
    instructions = json.loads(backend.calls[0][0]._body)["questions"]["model"]["instructions"]
    assert model_observation._USAGE_TAGS_NOTE in instructions, "两种模式都要解释用途标签"
    if mode == "apply":
        assert "按任务的语义需要挑选最合适的候选" in instructions and "本次只观察" not in instructions
    else:
        assert "本次只观察" in instructions
