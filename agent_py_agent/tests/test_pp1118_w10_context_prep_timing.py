"""W10 (Track IV): 会话上下文准备步骤计时、覆盖率不报降级、task-link 记忆化与 IM 路由计时。"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    request_audit_records,
    request_context,
    request_execution,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayAskRunContext,
    GatewayConversationContext,
    GatewayConversationLoadRequest,
    _gateway_task_links,
    _GatewayLoadErrors,
    gateway_conversation_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.audit_lifecycle_topic import metadata_only
from agent_py_agent.tests.test_consolidated_c1_g1 import _r8_gateway_history, _read_counter
from agent_py_agent.tests.test_res_w5_gateway_context_degrade import _events
from agent_py_agent.tests.test_res_w5_gateway_context_degrade import (
    case as case,  # noqa: F401 - pytest fixture
)

EXPECTED_PREP_STEPS = (
    "repair",
    "index",
    "compact_load",
    "history_source",
    "artifacts",
    "goal",
    "named_work",
    "workspace_task",
    "recent_task_workspaces",
    "subagent_completions",
)


def _agent_with_r8_history(tmp_path, count: int):
    store, thread, workspace = _r8_gateway_history(tmp_path, count)
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), auto_save_memory=False),
        tmp_path,
    )
    agent.conversation_store = store
    request = {"working_dir": str(tmp_path), "conversation": {"thread_id": thread.thread_id}}
    return agent, store, thread, workspace, request


def test_task_context_window_is_in_warnings_but_not_degraded_log(tmp_path, case, caplog):
    agent, _store, thread, _workspace, request = _agent_with_r8_history(tmp_path, 140)
    with caplog.at_level(logging.WARNING):
        context = gateway_conversation_context(
            GatewayConversationLoadRequest(agent, request, "", "continue", defer_compact=True)
        )
    assert any(row.get("code") == "task_context_window" for row in context.load_warnings)
    assert _events(caplog, "GATEWAY_CONTEXT_DEGRADED") == []

    # 真实损坏（如 goal JSON 损坏）仍必须记录 GATEWAY_CONTEXT_DEGRADED。
    caplog.clear()
    case.store.storage.goal_path(case.thread.thread_id).write_text("{corrupt", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        degraded = gateway_conversation_context(
            GatewayConversationLoadRequest(case.agent, case.request, "", "continue", defer_compact=True)
        )
    events = _events(caplog, "GATEWAY_CONTEXT_DEGRADED")
    assert len(events) == 1
    assert "conversation.goal.read" in events[0]
    assert degraded.load_errors == ()


def test_prep_steps_ms_contract_keys_and_slow_log(case, monkeypatch, caplog):
    class FakeClock:
        def __init__(self):
            self.now = 100.0

        def monotonic(self):
            return self.now

        def advance(self, seconds: float):
            self.now += seconds

    clock = FakeClock()
    monkeypatch.setattr(request_context, "time", clock)

    original_goal = request_context._gateway_thread_goal

    def slow_goal(*args, **kwargs):
        clock.advance(1.25)
        return original_goal(*args, **kwargs)

    monkeypatch.setattr(request_context, "_gateway_thread_goal", slow_goal)

    with caplog.at_level(logging.WARNING):
        context = gateway_conversation_context(
            GatewayConversationLoadRequest(case.agent, case.request, "", "continue", defer_compact=True)
        )
    assert tuple(context.prep_steps_ms.keys()) == EXPECTED_PREP_STEPS
    assert all(isinstance(value, float) and value >= 0.0 for value in context.prep_steps_ms.values())
    assert context.prep_steps_ms["goal"] == 1250.0

    slow_logs = _events(caplog, "GATEWAY_CONTEXT_PREP_SLOW")
    assert len(slow_logs) == 1
    assert f"thread={case.thread.thread_id}" in slow_logs[0]
    assert "total_ms=1250" in slow_logs[0]
    assert "goal:1250" in slow_logs[0]

    # 早退路径保持默认空字典。
    no_spec = gateway_conversation_context(
        GatewayConversationLoadRequest(case.agent, {}, "", "continue", defer_compact=True)
    )
    assert no_spec.prep_steps_ms == {}
    assert GatewayConversationContext().prep_steps_ms == {}


def test_task_links_memo_bounds_140_task_thread_reads(tmp_path, monkeypatch):
    agent, store, _thread, _workspace, request = _agent_with_r8_history(tmp_path, 140)
    reads = _read_counter(monkeypatch)
    context = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "req-140", "continue", defer_compact=True)
    )
    assert context.workspace_task is not None
    assert len(reads) <= 135, f"expected <= 135 reads with task-link memo, got {len(reads)}"


def test_task_links_memo_copies_and_plain_list_unmemoized():
    calls = []
    sample_link = SimpleNamespace(task_id="t1")
    sample_error = {"context": "err"}
    sample_warning = {"code": "w1"}

    def fake_context_report(thread_id, *, required_task_ids=()):
        calls.append((thread_id, tuple(required_task_ids)))
        return [sample_link], [sample_error], [sample_warning], {"recent_from": 1}

    store = SimpleNamespace(tasks=SimpleNamespace(context_report=fake_context_report))
    load_errors = _GatewayLoadErrors()

    links_1, errors_1 = _gateway_task_links(store, "th-1", load_errors)
    links_2, errors_2 = _gateway_task_links(store, "th-1", load_errors)
    assert len(calls) == 1
    assert len(load_errors.warnings) == 2
    assert links_1 == [sample_link] and links_2 == [sample_link]
    assert links_1 is not links_2 and errors_1 is not errors_2
    links_1.clear()
    errors_1.clear()
    links_3, errors_3 = _gateway_task_links(store, "th-1", load_errors, required_task_ids=("t1",))
    assert len(calls) == 1
    assert links_3 == [sample_link] and errors_3 == [sample_error]

    plain_errors: list[dict] = []
    _gateway_task_links(store, "th-1", plain_errors)
    _gateway_task_links(store, "th-1", plain_errors)
    assert len(calls) == 3


def test_fake_turn_records_prep_steps_ms_and_im_route_ms_in_stages(tmp_path, case, monkeypatch):
    def fake_run(prompt, *, params=None):
        return SimpleNamespace(
            response="done",
            backend="echo",
            used_memories=0,
            tool_rounds=0,
            archive_tool_calls=(),
            prompt=prompt,
            prompt_token_estimate=10,
            runtime_injection_token_estimate=0,
            turn_token_estimate=10,
            cumulative_token_estimate=10,
            runtime_status="ok",
            runtime_reason="",
            runtime_source="",
            turn_end_reason="end_turn",
            channel_delivery={},
            assistant_commentary_messages=(),
            canonical_native_messages=[{"role": "assistant", "content": "done"}],
            conversation_persist_degraded=False,
            conversation_persist_error="",
            memory_resume_context_injected=False,
            memory_resume_context_query="",
            memory_resume_context_matches=0,
            memory_resume_context_token_estimate=0,
            memory_resume_context_error="",
        )

    monkeypatch.setattr(case.agent, "run", fake_run)

    paths = gateway_paths(case.agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    stages: dict[str, object] = {}
    direct_req_path = paths.processing / "req-direct.json"
    direct_req = {
        **case.request,
        "id": "req-direct",
        "kind": "ask",
        "status": "processing",
        "execution_attempt_id": "req-direct",
        "prompt": "hello",
    }
    write_json_file(direct_req_path, direct_req)
    ask_ctx = GatewayAskRunContext(
        agent=case.agent,
        request=direct_req,
        request_path=direct_req_path,
        response_path=paths.responses / "req-direct.json",
        request_id="req-direct",
        on_chunk=None,
        stages=stages,
    )
    request_execution._run_gateway_ask(ask_ctx)
    assert "conversation_prep_ms" in stages
    assert "conversation_prep_steps_ms" in stages
    assert "im_route_ms" in stages
    assert isinstance(stages["im_route_ms"], float) and stages["im_route_ms"] >= 0.0
    prep_steps = stages["conversation_prep_steps_ms"]
    assert isinstance(prep_steps, dict)
    assert tuple(prep_steps.keys()) == EXPECTED_PREP_STEPS
    assert all(isinstance(v, float) and v >= 0.0 for v in prep_steps.values())

    # 端到端通过 _handle_gateway_request 确认 stage_timings 落入 response['stages_ms'] 且不破坏只取数字的消费者。
    e2e_thread = case.store.threads.get_or_create({
        "canonical_user_id": "w10-e2e",
        "channel": "chat",
        "channel_conversation_id": "w10-e2e",
        "cwd": str(tmp_path),
        "owner_id": case.agent.home_paths.owner_id,
        "owner_home": str(case.agent.home_paths.owner_home_dir),
    })
    req_path = paths.processing / "req-e2e.json"
    write_json_file(
        req_path,
        {
            "id": "req-e2e",
            "kind": "ask",
            "status": "processing",
            "execution_attempt_id": "req-e2e",
            "prompt": "hello",
            "working_dir": str(tmp_path),
            "conversation": {"thread_id": e2e_thread.thread_id},
        },
    )
    response = request_execution._handle_gateway_request(case.agent, req_path)
    assert response["ok"] is True
    assert "stages_ms" in response
    assert set(response["stages_ms"]["conversation_prep_steps_ms"]) == set(EXPECTED_PREP_STEPS)
    assert isinstance(response["stages_ms"]["im_route_ms"], float)
    assert json.loads(json.dumps(response["stages_ms"]))["im_route_ms"] == response["stages_ms"]["im_route_ms"]
    assert isinstance(request_audit_records._numeric_map(response["stages_ms"]), dict)
    assert isinstance(metadata_only({"stages_ms": response["stages_ms"]}).get("stages_ms"), dict)
