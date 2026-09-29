"""observe 不挡主链路：原设置→决策服务→后台执行器→本地 HTTP→结果日志/用量账的组合验收，不调用真实供应商。"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.common.cancellation import CancellationToken, bind_cancellation_token
from agent_py_agent.agent.conversation import decision_point_limits as limits
from agent_py_agent.agent.conversation import decision_policy, decision_service
from agent_py_agent.agent.conversation import decision_reach_counts as reach_counts
from agent_py_agent.agent.conversation.decision_observe_nonblocking import (
    USAGE_SOURCE,
    wait_nonblocking_idle,
)
from agent_py_agent.agent.gateway_parts import request_binding, request_execution
from agent_py_agent.agent.gateway_parts.io import read_json_file, update_json_file_atomic
from agent_py_agent.agent.llm_scale import hot_path
from agent_py_agent.agent.memory_store import MemoryRecord, decision_recall
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
from agent_py_agent.agent.runtime_context import (
    current_subagent_run_id,
    current_task_attributes,
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.decision_settings_schema import (
    BOOLEAN_FIELDS,
    GENERAL_FIELDS,
    decision_field_scopes,
)
from agent_py_agent.tests.test_decision_protocol import questions
from agent_py_agent.tests.test_decision_service_http import configured, decide, server  # noqa: F401
from agent_py_agent.tests.test_decision_settings import execute as settings
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_gateway_model_observation import capture_main, install_backend
from agent_py_agent.tests.test_gateway_model_observation import prepared as gateway_prepared


# LLM: 执行器与关闭标记都是进程级；每个测试从“宿主未关闭、执行器空闲”开始，收尾确认后台已全部落账，不把在途调用留给下一个测试。
# 函数用途: 固定可选准入额度并隔离进程级决策状态。
@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "4")
    monkeypatch.setenv("LLM_ADMISSION_WAIT_SECONDS", "0")
    hot_path.reset_hot_path_admission_for_test()
    monkeypatch.setattr(decision_policy, "_HOST_SHUTDOWN", False)
    assert wait_nonblocking_idle(5)
    yield
    assert wait_nonblocking_idle(5), "后台执行器必须在测试结束前空闲"
    hot_path.reset_hot_path_admission_for_test()


# LLM: 只在原 configured 宿主上补结果日志/到达计数的规范路径，并经原设置服务写开关、前台单次上限与后台预算；
#   changes 里的键是原决策设置字段，覆盖默认的“开关开、前台 2 秒、后台 2 秒”。不改任何运行代码。
# 函数用途: 建一个 observe（或指定模式）点位、默认打开“观察不挡回复”的真实本地 HTTP 宿主。
def nonblocking(tmp_path, server, mode="observe", changes=None):  # noqa: F811
    host, params, thread, stage = configured(tmp_path, server, mode=mode)
    host.home_paths.owner_decision_outcomes_jsonl = tmp_path / "decision" / "outcomes.jsonl"
    host.home_paths.owner_decision_reach_counts_json = tmp_path / "decision" / "reach_counts.json"
    patch(host, {"observe_nonblocking_enabled": True, "timeout_seconds": 2.0, "background_timeout_seconds": 2.0,
                 **(changes or {})}, thread_id=thread.thread_id)
    return host, params, thread, stage


# 函数用途: 读出本宿主结果日志里的全部行。
def rows(host):
    path = host.home_paths.owner_decision_outcomes_jsonl
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# 函数用途: 读出本会话 model_usage 里后台 observe 的用量事件。
def observe_usage(host, thread):
    events, errors = host.conversation_store.model_usage.events_report(thread.thread_id)
    assert not errors
    return [event for event in events if event.source == USAGE_SOURCE]


# 函数用途: 在限定时间内等某个条件成立，用于等本地服务收到第 N 个请求。
def wait_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def test_observe_returns_before_the_provider_answers_then_logs_and_bills_once(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, thread, stage = nonblocking(tmp_path, server)
    started = time.monotonic()
    outcome = decide(host, params, stage)
    # 同步路径会等到 2 秒前台上限才返回；这里在本地服务仍卡住时就拿到了占位结果。
    assert time.monotonic() - started < 1.0, "主链路不能等决策模型"
    assert (outcome.status, outcome.mode, outcome.may_apply, outcome.blocking) == ("deferred", "observe", False, False)
    assert outcome.response is None and rows(host) == [], "占位结果不写结果行"
    assert server.entered.wait(1)
    server.release.set()
    assert wait_nonblocking_idle(5)
    [row] = rows(host)
    assert (row["point"], row["status"], row["mode"], row["blocking"]) == ("subagent_model", "success", "observe", False)
    assert row["run_id"] == params.run_id and row["thread_id"] == thread.thread_id
    [event] = observe_usage(host, thread)
    assert event.request_id.startswith("decision-observe:") and event.run_id == params.run_id
    provider = event.model_calls["purpose_breakdown"]["decision"]["usage_breakdown"]["provider"]
    assert (provider["input_tokens"], provider["output_tokens"], provider["call_count"]) == (120, 30, 1)
    # 独立用量范围：不进发起 run 的请求/run 累计容器，run 收口时不会再算一遍。
    assert model_call_summary(host, request_id=params.request_id)["physical_model_attempt_count"] == 0
    assert model_call_summary(host, run_id=params.run_id)["physical_model_attempt_count"] == 0
    summary = host.conversation_store.model_usage.summary(thread.thread_id)
    assert summary["purpose_breakdown"]["decision"]["usage_breakdown"]["provider"]["input_tokens"] == 120
    assert not [row for row in decision_policy._ACTIVE.values() if row.context is host], "在途登记必须注销"
    # 不写发起回合的 live 状态和输出流。
    assert params.live_archive_state == {}
    assert "decision_call_count" not in params.tui_runtime.store.snapshot().status.model_metrics


def test_switch_off_keeps_observe_synchronous_and_billed_in_the_run(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, _thread, stage = nonblocking(tmp_path, server, changes={"observe_nonblocking_enabled": False})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(decide, host, params, stage)
        assert server.entered.wait(1)
        time.sleep(0.2)
        assert not future.done(), "开关关闭时 observe 仍同步等待"
        server.release.set()
        outcome = future.result(timeout=2)
    assert (outcome.status, outcome.blocking) == ("success", True)
    assert [(row["status"], row["blocking"]) for row in rows(host)] == [("success", True)]
    assert model_call_summary(host, request_id=params.request_id)["purpose_breakdown"]["decision"]["physical_model_attempt_count"] == 1


def test_apply_stays_synchronous_with_the_switch_on(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, _thread, stage = nonblocking(tmp_path, server, mode="apply")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(decide, host, params, stage)
        assert server.entered.wait(1)
        time.sleep(0.2)
        assert not future.done(), "apply 的语义与时序不变：必须等到回答"
        server.release.set()
        outcome = future.result(timeout=2)
    assert (outcome.status, outcome.may_apply, outcome.blocking) == ("success", True, True)
    assert [(row["mode"], row["blocking"]) for row in rows(host)] == [("apply", True)]


@pytest.mark.parametrize("enabled,expected", [(True, "success"), (False, "deadline")])
def test_background_budget_replaces_the_foreground_limit(tmp_path, server, enabled, expected):  # noqa: F811
    server.block = True
    host, params, _thread, stage = nonblocking(tmp_path, server, changes={
        "observe_nonblocking_enabled": enabled, "timeout_seconds": 0.2, "background_timeout_seconds": 3.0})
    timer = threading.Timer(0.6, server.release.set)
    timer.start()
    try:
        outcome = decide(host, params, stage)
        assert wait_nonblocking_idle(5)
    finally:
        timer.cancel()
        server.release.set()
    # 前台单次 0.2 秒挡不住后台；后台按 3 秒预算等到 0.6 秒的回答。开关关闭时照旧 0.2 秒到期。
    assert outcome.status == ("deferred" if enabled else "deadline")
    assert [row["status"] for row in rows(host)] == [expected]


@pytest.mark.parametrize("enabled,expected", [(True, ("deferred", "")), (False, ("deadline", "budget_exhausted"))])
def test_exhausted_foreground_stage_does_not_stop_a_background_call(tmp_path, server, enabled, expected):  # noqa: F811
    host, params, _thread, stage = nonblocking(tmp_path, server, changes={"observe_nonblocking_enabled": enabled})
    exhausted = replace(stage, deadline=time.monotonic() - 1)
    outcome = decide(host, params, exhausted)
    assert (outcome.status, outcome.reason) == expected
    assert wait_nonblocking_idle(5)
    assert len(server.requests) == (1 if enabled else 0), "没人在等的调用不占前台阶段预算"


def test_background_timeout_is_billed_like_a_foreground_timeout(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, thread, stage = nonblocking(tmp_path, server, changes={"background_timeout_seconds": 0.3})
    assert decide(host, params, stage).status == "deferred"
    assert wait_nonblocking_idle(5)
    server.release.set()
    assert [(row["status"], row["reason"], row["blocking"]) for row in rows(host)] == [("deadline", "provider_failed", False)]
    [event] = observe_usage(host, thread)
    bucket = event.model_calls["purpose_breakdown"]["decision"]
    assert bucket["physical_model_attempt_count"] == 1 and bucket["status_counts"]["timed_out"] == 1


def test_one_worker_and_a_bounded_queue_refuse_with_a_structured_reason(tmp_path, server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(limits, "OBSERVE_NONBLOCKING_MAX_PENDING", 2)
    server.block = True
    host, params, thread, stage = nonblocking(tmp_path, server)
    first = decide(host, params, stage)
    assert server.entered.wait(1)
    queued = [decide(host, params, stage) for _ in range(2)]
    refused = decide(host, params, stage)
    time.sleep(0.2)
    assert len(server.requests) == 1, "同一时刻只有一个后台调用"
    assert [item.status for item in (first, *queued)] == ["deferred"] * 3
    assert (refused.status, refused.reason, refused.blocking, refused.may_apply) == (
        "skipped", "observe_nonblocking_busy", False, False)
    assert [(row["status"], row["reason"]) for row in rows(host)] == [("skipped", "observe_nonblocking_busy")]
    reach_counts.flush_decision_reach_counts()
    hours = json.loads(host.home_paths.owner_decision_reach_counts_json.read_text(encoding="utf-8"))["hours"]
    assert sum(point.get("subagent_model", {}).get(reach_counts.NONBLOCKING_BUSY, 0) for point in hours.values()) == 1
    server.release.set()
    assert wait_nonblocking_idle(5)
    assert [row["status"] for row in rows(host)][1:] == ["success"] * 3
    assert len(server.requests) == 3 and len(observe_usage(host, thread)) == 3


def test_queue_limit_is_the_frozen_eight():
    assert limits.OBSERVE_NONBLOCKING_MAX_PENDING == 8


def test_user_stop_before_send_skips_the_request_without_usage(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, thread, stage = nonblocking(tmp_path, server)
    decide(host, params, stage)
    assert server.entered.wait(1)
    token = CancellationToken()
    with bind_cancellation_token(token):
        assert decide(host, params, stage).status == "deferred"
    token.cancel("user_stop")
    server.release.set()
    assert wait_nonblocking_idle(5)
    assert [(row["status"], row["reason"]) for row in rows(host)] == [("success", ""), ("stale", "turn_cancelled")]
    assert len(server.requests) == 1 and len(observe_usage(host, thread)) == 1


def test_user_stop_after_send_lets_the_request_finish_and_bill(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, thread, stage = nonblocking(tmp_path, server)
    token = CancellationToken()
    with bind_cancellation_token(token):
        assert decide(host, params, stage).status == "deferred"
    assert server.entered.wait(1)
    token.cancel("user_stop")
    server.release.set()
    assert wait_nonblocking_idle(5)
    assert [(row["status"], row["blocking"]) for row in rows(host)] == [("success", False)]
    assert len(observe_usage(host, thread)) == 1


def test_settings_change_revokes_queued_calls_without_sending(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, thread, stage = nonblocking(tmp_path, server)
    decide(host, params, stage)
    assert server.entered.wait(1)
    decide(host, params, stage)
    patch(host, {"points.subagent_model.mode": "off"}, thread_id=thread.thread_id)
    server.release.set()
    assert wait_nonblocking_idle(5)
    assert [(row["status"], row["reason"]) for row in rows(host)] == [("stale", "settings_changed")] * 2
    assert len(server.requests) == 1


def test_host_shutdown_drops_queued_calls_without_sending(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, thread, stage = nonblocking(tmp_path, server)
    decide(host, params, stage)
    assert server.entered.wait(1)
    decide(host, params, stage)
    assert decision_policy.cancel_active_decisions_for_shutdown() == 2, "排队中的调用同样在在途登记里"
    server.release.set()
    assert wait_nonblocking_idle(5)
    assert [(row["status"], row["reason"]) for row in rows(host)] == [("stale", "host_shutdown")] * 2
    assert len(server.requests) == 1
    assert len(observe_usage(host, thread)) == 1, "排队中被丢弃的调用不建调用记录，只有已发出的那条记账"


def test_worker_checks_identity_with_the_captured_runner_context(tmp_path, server):  # noqa: F811
    host, params, _thread, _stage = nonblocking(tmp_path, server)
    params.run_id = ""
    previous = set_current_subagent_context(host, run_id="run-from-runner")
    try:
        stage = decision_service.begin_decision_stage(host, params, operation_id="batch-runner")
        assert stage.run_id == "run-from-runner"
        assert decide(host, params, stage).status == "deferred"
    finally:
        restore_current_subagent_context(host, previous)
    assert wait_nonblocking_idle(5)
    [row] = rows(host)
    assert (row["status"], row["run_id"]) == ("success", "run-from-runner"), "后台线程没有 runner 上下文，身份只能来自发起时捕获"


def test_background_call_does_not_block_a_synchronous_apply_on_the_same_thread(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, thread, stage = nonblocking(tmp_path, server)
    patch(host, {"points.planning.mode": "apply"}, thread_id=thread.thread_id)
    decide(host, params, stage)
    assert server.entered.wait(1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(decision_service.decide, host, params, stage, point="planning", state={"需求": "排序"},
                             questions=questions(), candidates_revision="candidates-2")
        assert wait_until(lambda: len(server.requests) == 2), "同步 apply 调用必须照常发出，不能被后台 observe 占住资源"
        server.release.set()
        outcome = future.result(timeout=2)
    assert (outcome.status, outcome.may_apply, outcome.blocking) == ("success", True, True)


def test_recall_main_chain_keeps_the_original_order_without_waiting(tmp_path, server):  # noqa: F811
    server.block = True
    host, _params, thread, _stage = nonblocking(tmp_path, server)
    patch(host, {"points.recall.mode": "observe"}, thread_id=thread.thread_id)
    records = [MemoryRecord("user", f"记忆正文{index}", entry_id=f"m{index}", kind="fact", version=1,
                            attributes={"scope_type": "personal", "scope_key": "personal", "origin": "user_explicit"})
               for index in range(3)]
    request = RuntimeContextRequest("当前问题", None, False, request_id="request-1", run_id="run-1", task_id="task-1",
                                    task_attributes={"conversation_thread_id": thread.thread_id})
    scope = MemoryRecallScope.from_runtime(task_id=request.task_id, task_attributes=request.task_attributes)
    started = time.monotonic()
    kept, finding = decision_recall.rerank_recalled_memories(host, request, records, recall_scope=scope,
                                                            refresh=lambda: pytest.fail("observe 不会采用建议"))
    assert time.monotonic() - started < 1.0 and kept is records
    assert finding == "memory_recall_decision:observe:deferred"
    server.release.set()
    assert wait_nonblocking_idle(5)
    assert [(row["point"], row["blocking"]) for row in rows(host)] == [("recall", False)]


def test_switch_is_one_general_boolean_field_with_matching_defaults(tmp_path):
    assert "observe_nonblocking_enabled" in GENERAL_FIELDS and "observe_nonblocking_enabled" in BOOLEAN_FIELDS
    assert decision_field_scopes()["observe_nonblocking_enabled"] == ["owner", "thread"]
    assert AgentConfig().decision_observe_nonblocking_enabled is False
    from agent_py_agent.tests.test_decision_settings import host_at

    assert settings(host_at(tmp_path), "read", {})["effective"]["observe_nonblocking_enabled"] is False


@pytest.mark.parametrize("case", [("observe", True, False), ("observe", False, True), ("apply", True, True)])
def test_settings_view_reports_the_real_wait(tmp_path, server, case):  # noqa: F811
    mode, enabled, blocking = case
    host, _params, thread, _stage = nonblocking(tmp_path, server, mode=mode, changes={
        "observe_nonblocking_enabled": enabled, "timeout_seconds": 1.5, "background_timeout_seconds": 9.0})
    view = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    row = view["effective"]["points"]["subagent_model"]
    assert row["blocking"] is blocking
    assert (row["max_request_seconds"], row["limiting_field"]) == (
        (9.0, "background_timeout_seconds") if not blocking else (1.5, "points.subagent_model.timeout_seconds"))
    assert view["effective"]["points"]["curator"]["blocking"] is True, "用户后台点位不受这个开关影响"


def test_busy_code_stays_out_of_reached_and_not_called():
    row = reach_counts._point_row({reach_counts.CALLED: 3, reach_counts.NONBLOCKING_BUSY: 2, "point_off": 1})
    assert (row["reached"], row["called"]) == (4, 3)
    assert [item["reason"] for item in row["not_called"]] == ["point_off"]


def test_deferred_placeholder_is_a_distinct_non_adoptable_outcome():
    placeholder = decision_service.DecisionOutcome("observe", decision_service.DEFERRED_STATUS, blocking=False)
    assert not placeholder.may_apply and placeholder.response is None
    assert replace(placeholder, status="success") != placeholder


# 函数用途: 读出 Gateway 请求记录里的模型观察标记。
def marker(fixture):
    return read_json_file(fixture.context.request_path)[request_binding.MODEL_OBSERVATION_KEY]


def test_model_selection_starts_the_main_model_first_and_backfills_while_the_turn_is_open(tmp_path, monkeypatch):
    fixture = gateway_prepared(tmp_path)
    patch(fixture.agent, {"observe_nonblocking_enabled": True})
    gate = threading.Event()
    install_backend(monkeypatch, fixture, action=lambda _request: gate.wait(3))
    initial_model = fixture.agent.config.model_name
    seen = []

    # 函数用途: 替身主模型：记录开始时的观察状态，再放行决策并等后台落账，证明主模型先开始、回合内补记。
    def execute(context, prompt, conversation, *, observer=None):
        seen.append(marker(fixture)["status"])
        gate.set()
        assert wait_nonblocking_idle(3)
        seen.append(marker(fixture)["status"])
        return "original-main"

    monkeypatch.setattr(request_execution, "_execute_gateway_conversation_turn", execute)
    assert request_execution._run_gateway_ask(fixture.context) == "original-main"
    assert seen == ["deferred", "observed"]
    final = marker(fixture)
    assert final["choice"] == fixture.candidate and final["adopted"] is False and final["requested_mode"] == "observe"
    assert final["candidates_revision"] and fixture.agent.config.model_name == initial_model
    outcomes = [json.loads(line) for line in fixture.agent.home_paths.owner_decision_outcomes_jsonl.read_text(
        encoding="utf-8").splitlines()]
    assert [(row["point"], row["status"], row["blocking"]) for row in outcomes] == [("model_selection", "success", False)]


def test_model_selection_answer_after_the_turn_closed_is_not_written_back(tmp_path, monkeypatch):
    fixture = gateway_prepared(tmp_path)
    patch(fixture.agent, {"observe_nonblocking_enabled": True})
    gate = threading.Event()
    install_backend(monkeypatch, fixture, action=lambda _request: gate.wait(3))
    capture_main(monkeypatch)
    assert request_execution._run_gateway_ask(fixture.context) == "original-main"
    # 回合收口：请求不再处于 processing，原 active-turn 事务从此拒绝任何补记。
    update_json_file_atomic(fixture.context.request_path, lambda current: {**current, "status": "done"},
                            require_existing=True)
    gate.set()
    assert wait_nonblocking_idle(3)
    assert marker(fixture)["status"] == "deferred", "已结束的回合不被后台结果改写"
    outcomes = fixture.agent.home_paths.owner_decision_outcomes_jsonl.read_text(encoding="utf-8")
    assert json.loads(outcomes.splitlines()[-1])["status"] == "success", "结果仍写进决策结果日志"


@pytest.mark.parametrize("case", [
    ("started", "op-1", "claim-1", "observed"), ("deferred", "op-1", "claim-1", "observed"),
    ("error", "op-1", "claim-1", "error"), ("deferred", "op-other", "claim-1", "deferred"),
    ("deferred", "op-1", "claim-other", "deferred"),
])
def test_backfill_only_replaces_started_or_deferred_marker_of_the_same_observation(tmp_path, case):
    initial, operation, claim, expected = case
    fixture = gateway_prepared(tmp_path)
    seeded = {"operation_id": operation, "claim_id": claim, "status": initial, "adopted": False}
    update_json_file_atomic(fixture.context.request_path,
                            lambda current: {**current, request_binding.MODEL_OBSERVATION_KEY: seeded}, require_existing=True)
    writer = request_binding.GatewayModelObservationWriter(fixture.context, fixture.thread_id, "claim-1", "op-1")
    writer.complete_deferred({"status": "observed", "choice": "candidate-x", "adopted": True})
    assert marker(fixture)["status"] == expected
    assert marker(fixture)["adopted"] is False, "补记永远不授予采用"


def test_completion_callback_runs_after_the_runner_identity_is_restored(tmp_path, server):  # noqa: F811
    host, params, _thread, stage = nonblocking(tmp_path, server)
    seen = []

    # 函数用途: 在后台 worker 里读当时的 runner 上下文；执行器应已按快照恢复，回调看不到装入的阶段身份。
    def on_result(outcome):
        seen.append((outcome.status, current_subagent_run_id(host), current_task_attributes(host),
                     threading.current_thread().name))

    decision_service.decide(host, params, stage, point="subagent_model", state={"需求": "整理资料"},
                            questions=questions(), candidates_revision="candidates-1", on_background_result=on_result)
    assert wait_nonblocking_idle(5)
    assert seen == [("success", "", None, "decision-observe")], "写行、结算与回调都要在恢复身份之后运行"


def test_identity_snapshot_is_a_copy_of_the_caller_task_attributes(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, thread, _stage = nonblocking(tmp_path, server)
    attributes = {"conversation_thread_id": thread.thread_id}
    params.run_id = ""          # run 身份由 runner 上下文给，避免与参数里的 run 冲突
    previous = set_current_subagent_context(host, run_id="run-copy", task_attributes=attributes)
    try:
        stage = decision_service.begin_decision_stage(host, params, operation_id="batch-copy")
        assert decide(host, params, stage).status == "deferred"
    finally:
        restore_current_subagent_context(host, previous)
    assert server.entered.wait(1)
    attributes["conversation_thread_id"] = "thread-changed-by-the-caller"
    server.release.set()
    assert wait_nonblocking_idle(5)
    assert [(row["status"], row["reason"]) for row in rows(host)] == [("success", "")], "发起回合之后改字典不影响后台复核"


def test_gateway_shutdown_waits_briefly_for_background_rows(tmp_path, server, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.conversation import decision_observe_nonblocking as executor
    from agent_py_agent.cli import gateway_process

    original = executor.append_decision_outcome

    # 函数用途: 把写结果行放慢一点，证明停机收尾真的等了后台执行器，而不是碰巧赶上。
    def slow_append(agent, row):
        time.sleep(0.3)
        original(agent, row)

    monkeypatch.setattr(executor, "append_decision_outcome", slow_append)
    server.block = True
    host, params, _thread, stage = nonblocking(tmp_path, server)
    decide(host, params, stage)
    assert server.entered.wait(1)
    started = time.monotonic()
    gateway_process._cancel_active_decisions(SimpleNamespace(context=SimpleNamespace(agent=host)))
    assert time.monotonic() - started < gateway_process._NONBLOCKING_DRAIN_SECONDS + 0.5
    assert [(row["status"], row["reason"]) for row in rows(host)] == [("stale", "host_shutdown")], "返回前结果行已落盘"
    server.release.set()
