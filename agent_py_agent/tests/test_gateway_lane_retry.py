"""后台失败车道：配置等待、环境暂停、定时退避与用户隔离；不调用真实模型。"""

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import errors
from agent_py_agent.agent.backends.errors import (
    ModelNotConfiguredError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderQuotaExhaustedError,
    ProviderRequestRejectedError,
    ProviderTimeoutError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.settings.model_profiles import ModelProfileError
from agent_py_agent.cli import gateway_lane_retry


@pytest.mark.parametrize("error", [ModelNotConfiguredError(), ModelProfileError("模型引用已删除")])
def test_missing_model_waits_for_configuration_not_elapsed_time(monkeypatch, error):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = [100.0]
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: now[0])
    retries.failed("owner-a", "thread", error, delay=30)
    now[0] += 24 * 3600
    assert not retries.ready("owner-a", "thread", model_ready=lambda: False)
    assert retries.ready("owner-b", "thread", model_ready=lambda: False)
    assert retries.ready("owner-a", "healthy", model_ready=lambda: False)
    assert retries.ready("owner-a", "thread", model_ready=lambda: True)
    retries.succeeded("owner-a", "thread")
    assert retries.ready("owner-a", "thread", model_ready=lambda: False)


@pytest.mark.parametrize("error", [RuntimeError("failure"), ProviderRequestRejectedError("400")])
@pytest.mark.parametrize("delay", [0, 30])
def test_other_errors_keep_monotonic_cooldown(monkeypatch, error, delay):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = [50.0]
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: now[0])
    retries.failed("a", "t", error, delay=delay)
    assert retries.ready("a", "t", model_ready=lambda: False) is (delay == 0)
    now[0] += delay
    assert retries.ready("a", "t", model_ready=lambda: False)


def test_owner_eviction_and_capacity_do_not_accumulate_stale_records():
    retries = gateway_lane_retry.BackgroundLaneRetry()
    for index in range(1100):
        retries.failed("a", str(index), ModelNotConfiguredError(), delay=30)
    assert retries.count("a") == 1024
    retries.failed("b", "t", RuntimeError(), delay=30)
    retries.retain_owners(["b"])
    assert retries.count("a") == 0
    assert retries.count("b") == 1


def test_blocked_configuration_read_does_not_hold_retry_lock():
    from concurrent.futures import ThreadPoolExecutor

    retries = gateway_lane_retry.BackgroundLaneRetry()
    retries.failed("a", "t", ModelNotConfiguredError(), delay=30)

    def check_model():
        with ThreadPoolExecutor(max_workers=1) as pool:
            # 若配置读取仍持锁，这个不同线程的记录操作会超时。
            pool.submit(retries.succeeded, "a", "t").result(timeout=2)
        return True

    assert retries.ready("a", "t", model_ready=check_model)


def test_planner_skips_unconfigured_lane_and_resumes_exact_thread(tmp_path, monkeypatch, capsys):
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
    from agent_py_agent.cli import gateway_loops
    from agent_py_agent.tests.test_model_profiles import add
    from agent_py_agent.tests.test_thread_model_selection import host_with_store

    host = host_with_store(tmp_path)
    host.config.model_backend = ""
    target = execute_local_model_operation(host, "old", "list", {})["thread_id"]
    requests = []
    prepared = []
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = host
    supervisor._inflight = {}
    supervisor._owner_schedulers = {}
    supervisor._lane_retry = gateway_lane_retry.BackgroundLaneRetry()

    def fail(thread_id):
        requests.append(thread_id)
        raise ModelNotConfiguredError()

    scheduler = SimpleNamespace(
        runtime=SimpleNamespace(agent=host), tick_thread=fail, prepare_tick=lambda **kw: None,
        ready_thread_ids=lambda **kw: (target, "healthy")[:kw["limit"]],
    )
    supervisor._base_scheduler = scheduler
    supervisor._submit_thread_candidates = lambda candidates, **kw: prepared.extend(candidates)
    supervisor._safe_thread_tick(scheduler, "base", target)
    for _ in range(3):
        prepared.clear()
        supervisor._submit_ready_thread_ticks()
        assert prepared[0][2] == ["healthy"]
    assert requests == [target]
    assert capsys.readouterr().err.count('"category": "model_not_configured"') == 1
    selected = add(host, model_name="configured-model")[0]
    # 只改用户的新会话默认值不会偷换旧会话；必须明确选择旧会话的模型。
    execute_local_model_operation(host, "new", "set_default", {"profile_id": selected})
    prepared.clear()
    supervisor._submit_ready_thread_ticks()
    assert prepared[0][2] == ["healthy"]
    execute_local_model_operation(host, "old", "select", {"profile_id": selected})
    prepared.clear()
    supervisor._submit_ready_thread_ticks()
    assert prepared[0][2] == [target, "healthy"]


@pytest.mark.parametrize("error,expected", [
    (ModelNotConfiguredError(), "active"), (ModelProfileError("已撤销"), "active"),
    (RuntimeError("bug"), "blocked"), (ProviderRequestRejectedError("400"), "blocked"),
])
def test_goal_configuration_failure_preserves_original_wake(tmp_path, error, expected):
    from agent_py_agent.agent.conversation import FakeDeliveryService
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        _handle_nonquota_wake_error,
    )
    from agent_py_agent.tests.test_conversation_goal_tools import _goal_agent

    agent, thread, goal = _goal_agent(tmp_path)
    store = agent.conversation_store
    signal = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "root_task_id": goal.task_id,
        "reason": "thread_goal_continue", "metadata": {"goal_id": goal.goal_id},
    })
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    released = []
    scheduler.scheduler_service = SimpleNamespace(release=lambda claim, **kw: released.append(claim))
    claim = object() if expected == "active" else None
    _handle_nonquota_wake_error(scheduler, signal, lifecycle_reason="thread_goal_continue", claim=claim, error=error)
    assert store.goals.load(thread.thread_id).status == expected
    pending = {wake.wake_signal_id for wake in store.wakes.pending()}
    assert (signal.wake_signal_id in pending) is (expected == "active")
    if expected == "active":
        assert released == [claim]
        assert store.tasks.load(goal.task_id).status == "active"


@pytest.mark.parametrize("error,expect_release,expected_status", [
    (ProviderTimeoutError("模型接口等待首个流式事件超时", stage="first_event"), False, "blocked"),
    (ProviderUsageLimitError("HTTP 429: usage limit"), True, "usage_limited"),
])
def test_budget_exceeded_errors_keep_steady_state_closeout(tmp_path, error, expect_release, expected_status):
    """到总时长上限后的原异常在后台 claim 结算上与跑满阶梯同口径：

    到上限不再换异常类型（只加 error_code/retry_budget_seconds），分路只看类型——
    ProviderTimeoutError 照旧按 failed 结算 claim（不释放重跑）、Goal 记 blocked；
    ProviderUsageLimitError 照旧按可重跑释放 claim、Goal 记 usage_limited。
    """
    from agent_py_agent.agent.agent_core.provider_transient_auto_resume import (
        PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED,
    )
    from agent_py_agent.agent.conversation import FakeDeliveryService
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        _handle_nonquota_wake_error,
    )
    from agent_py_agent.tests.test_conversation_goal_tools import _goal_agent

    error.error_code = PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED
    error.retry_budget_seconds = 1800.0

    agent, thread, goal = _goal_agent(tmp_path)
    store = agent.conversation_store
    signal = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "root_task_id": goal.task_id,
        "reason": "thread_goal_continue", "metadata": {"goal_id": goal.goal_id},
    })
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    released, finished = [], []
    scheduler.scheduler_service = SimpleNamespace(
        release=lambda claim, **kw: released.append(claim),
        finish=lambda claim, **kw: finished.append(kw) or object(),
    )
    claim = object()
    _handle_nonquota_wake_error(
        scheduler, signal, lifecycle_reason="thread_goal_continue", claim=claim, error=error,
    )

    if expect_release:
        assert released == [claim] and finished == [], "429 的既有口径：按可重跑释放 claim"
    else:
        assert released == [] and finished and finished[0]["status"] == "failed", "超时的既有口径：按 failed 结算"
    assert store.goals.load(thread.thread_id).status == expected_status


ENVIRONMENT_FAULTS = [
    *(ProviderRequestRejectedError(f"HTTP {status}", status_code=status) for status in (401, 402, 403, 404, 407)),
    ProviderConnectionError("DNS 解析失败"),
]
ENVIRONMENT_FAULT_IDS = ["401", "402", "403", "404", "407", "connection"]


def _clock(monkeypatch, start=100.0):
    now = [start]
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: now[0])
    return now


def _lane_events(capsys):
    lines = capsys.readouterr().out.splitlines()
    return [json.loads(line.split(" ", 1)[1]) for line in lines if line.startswith("[gateway-lane-retry] ")]


@pytest.mark.parametrize(("error", "expected"), [
    *((error, True) for error in ENVIRONMENT_FAULTS),
    (ModelNotConfiguredError(), True),
    (ProviderConfigurationError("接口地址无效"), True),
    (SimpleNamespace(status_code=401), True),
    *((ProviderRequestRejectedError(f"HTTP {status}", status_code=status), False) for status in (400, 413, 422)),
    (ProviderRequestRejectedError("未带状态码"), False),
    (ProviderQuotaExhaustedError("额度耗尽", details={"status_code": 429}), False),
    (ProviderTransientError("HTTP 503"), False),
    (RuntimeError("程序错误"), False),
], ids=[*ENVIRONMENT_FAULT_IDS, "model-not-configured", "configuration", "bare-401", "400", "413", "422",
        "rejected-no-status", "quota", "transient", "bug"])
def test_environment_fault_authority(error, expected):
    assert errors.is_provider_environment_fault(error) is expected


def test_wake_poison_and_lane_share_one_environment_fault_authority(monkeypatch):
    from agent_py_agent.agent.conversation import wake_poison

    assert gateway_lane_retry.is_provider_environment_fault is errors.is_provider_environment_fault
    assert not hasattr(wake_poison, "_ENVIRONMENT_HTTP_STATUSES")
    seen = []
    monkeypatch.setattr(errors, "is_provider_environment_fault", lambda exc: seen.append(exc) or True)
    bug = ValueError("程序错误")
    assert wake_poison._is_uncounted(bug)
    assert seen == [bug]


@pytest.mark.parametrize("error", ENVIRONMENT_FAULTS, ids=ENVIRONMENT_FAULT_IDS)
def test_environment_fault_pauses_lane_past_ordinary_cooldown(monkeypatch, capsys, error):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = _clock(monkeypatch)
    retries.failed("a", "t", error, delay=30)
    for elapsed in (0, 30, 59.9):
        now[0] = 100.0 + elapsed
        assert not retries.ready("a", "t", model_ready=lambda: True, fingerprint=lambda: "same")
    assert retries.ready("a", "other", model_ready=lambda: True, fingerprint=lambda: "same")
    assert retries.ready("b", "t", model_ready=lambda: True, fingerprint=lambda: "same")
    assert _lane_events(capsys) == [{
        "event": "lane_environment_paused", "owner": "a", "thread_id": "t", "probe_in_seconds": 60.0,
        "error_type": type(error).__name__, "http_status": getattr(error, "status_code", None),
    }]


def test_model_fingerprint_change_releases_paused_lane_immediately(monkeypatch, capsys):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = _clock(monkeypatch)
    current = ["before"]

    def check():
        return retries.ready("a", "t", model_ready=lambda: True, fingerprint=lambda: current[0])

    error = ProviderRequestRejectedError("HTTP 401", status_code=401)
    retries.failed("a", "t", error, delay=30)
    assert not check()
    now[0] += 1
    assert not check()
    current[0] = "after"
    assert check()
    assert retries.count("a") == 0
    # 换过配置后再失败，从 60 秒重新计，不沿用换配置前的间隔。
    retries.failed("a", "t", error, delay=30)
    now[0] += 59.9
    assert not check()
    now[0] = 161.0
    assert check()
    events = _lane_events(capsys)
    assert [event["event"] for event in events] == [
        "lane_environment_paused", "lane_environment_resumed", "lane_environment_paused",
    ]
    assert events[1]["reason"] == "model_fingerprint_changed"
    assert events[2]["probe_in_seconds"] == 60.0


def test_probe_releases_on_schedule_doubles_after_failure_and_success_clears(monkeypatch, capsys):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = _clock(monkeypatch)

    def check():
        return retries.ready("a", "t", model_ready=lambda: True, fingerprint=lambda: "same")

    error = ProviderConnectionError("代理不可达")
    retries.failed("a", "t", error, delay=30)
    for interval in (60, 120, 240, 480, 900, 900):
        start = now[0]
        now[0] = start + interval - 0.5
        assert not check()
        now[0] = start + interval
        assert check()
        # 到点只是放行一次探测；暂停记录要留到探测有结果。
        assert retries.count("a") == 1
        retries.failed("a", "t", error, delay=30)
    now[0] += 900
    assert check()
    retries.succeeded("a", "t")
    assert retries.count("a") == 0
    assert check()
    events = _lane_events(capsys)
    paused = [event["probe_in_seconds"] for event in events if event["event"] == "lane_environment_paused"]
    assert paused == [60, 120, 240, 480, 900, 900, 900]
    assert events[-1] == {
        "event": "lane_environment_resumed", "owner": "a", "thread_id": "t", "reason": "probe_succeeded",
    }


@pytest.mark.parametrize("status", [400, 413])
def test_request_rejections_keep_thirty_second_cooldown(monkeypatch, capsys, status):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = _clock(monkeypatch)
    reads = []

    def fingerprint():
        reads.append(status)
        return "same"

    retries.failed("a", "t", ProviderRequestRejectedError(f"HTTP {status}", status_code=status), delay=30)
    now[0] = 129.9
    assert not retries.ready("a", "t", model_ready=lambda: True, fingerprint=fingerprint)
    now[0] = 130.0
    assert retries.ready("a", "t", model_ready=lambda: True, fingerprint=fingerprint)
    assert reads == []
    assert _lane_events(capsys) == []


def test_model_not_configured_still_waits_for_model_configuration(monkeypatch, capsys):
    error = ModelNotConfiguredError()
    # 它也命中环境级判定；车道必须先按"等模型配置"处理，配好模型立即恢复，而不是等探测时刻。
    assert errors.is_provider_environment_fault(error)
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = _clock(monkeypatch)
    retries.failed("a", "t", error, delay=30)
    now[0] += 24 * 3600
    fingerprints = iter(["one", "two", "three"])
    assert not retries.ready("a", "t", model_ready=lambda: False, fingerprint=lambda: next(fingerprints))
    assert not retries.ready("a", "t", model_ready=lambda: False, fingerprint=lambda: next(fingerprints))
    assert retries.ready("a", "t", model_ready=lambda: True, fingerprint=lambda: next(fingerprints))
    assert _lane_events(capsys) == []


def test_quota_exhausted_pauses_lane_like_environment_fault(monkeypatch, capsys):
    quota = ProviderQuotaExhaustedError("HTTP 429: weekly quota", details={"status_code": 429})
    # 共享判定不含额度（毒丸也在用）；额度只在车道这层显式并入暂停。
    assert not errors.is_provider_environment_fault(quota)
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = _clock(monkeypatch)
    current = ["same-model"]

    def check():
        return retries.ready("a", "t", model_ready=lambda: True, fingerprint=lambda: current[0])

    retries.failed("a", "t", quota, delay=30)
    for moment in (100.0, 130.0, 159.9):
        now[0] = moment
        assert not check()
    now[0] = 160.0
    assert check()
    assert retries.count("a") == 1
    retries.failed("a", "t", quota, delay=30)
    now[0] = 161.0
    assert not check()
    current[0] = "switched-model"
    assert check()
    assert retries.count("a") == 0
    events = _lane_events(capsys)
    assert [event["event"] for event in events] == [
        "lane_environment_paused", "lane_environment_paused", "lane_environment_resumed",
    ]
    assert [event.get("probe_in_seconds") for event in events[:2]] == [60.0, 120.0]
    assert events[0]["error_type"] == "ProviderQuotaExhaustedError" and events[0]["http_status"] is None
    assert events[2]["reason"] == "model_fingerprint_changed"


def test_success_during_fingerprint_read_is_not_undone(monkeypatch):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    _clock(monkeypatch)
    retries.failed("a", "t", ProviderConnectionError("dns"), delay=30)

    def racing_fingerprint():
        # 读指纹在锁外；这期间别的线程报告了成功，过期的基线不能把暂停写回来。
        retries.succeeded("a", "t")
        return "baseline"

    assert not retries.ready("a", "t", model_ready=lambda: True, fingerprint=racing_fingerprint)
    assert retries.count("a") == 0
    assert retries.ready("a", "t", model_ready=lambda: True, fingerprint=lambda: "baseline")


def _fingerprint_read_errors():
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    return [OSError(5, "Input/output error"), DataCorruptionError("会话文件损坏"), ModelProfileError("当前会话不存在")]


@pytest.mark.parametrize("error", _fingerprint_read_errors(), ids=["os", "corrupt", "profile"])
def test_fingerprint_read_failure_keeps_pause_and_doubling(monkeypatch, capsys, error):
    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = _clock(monkeypatch)
    fault = ProviderConnectionError("dns")

    def broken():
        raise error

    retries.failed("a", "t", fault, delay=30)
    now[0] = 160.0
    assert retries.ready("a", "t", model_ready=lambda: True, fingerprint=lambda: "same")
    retries.failed("a", "t", fault, delay=30)
    now[0] = 161.0
    assert not retries.ready("a", "t", model_ready=lambda: True, fingerprint=lambda: "same")
    # 第二次故障后间隔 120 秒，基线已记下；这期间取指纹出错只当未知：不抛、不当成指纹变化放行、记录仍是环境暂停。
    now[0] = 200.0
    assert not retries.ready("a", "t", model_ready=lambda: True, fingerprint=broken)
    assert retries.count("a") == 1
    now[0] = 280.0
    assert retries.ready("a", "t", model_ready=lambda: True, fingerprint=broken)
    retries.failed("a", "t", fault, delay=30)
    paused = [event["probe_in_seconds"] for event in _lane_events(capsys) if event["event"] == "lane_environment_paused"]
    assert paused == [60.0, 120.0, 240.0]


def test_planner_fingerprint_failure_is_not_recorded_as_new_lane_failure(monkeypatch, capsys):
    from agent_py_agent.cli import gateway_loops

    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._lane_retry = gateway_lane_retry.BackgroundLaneRetry()
    now = _clock(monkeypatch)
    supervisor._lane_retry.failed("base", "t", ProviderConnectionError("dns"), delay=30)

    def unreadable(agent, thread_id):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(gateway_loops, "thread_model_fingerprint", unreadable)
    recorded = []
    monkeypatch.setattr(supervisor, "_record_thread_failure", lambda *args: recorded.append(args))
    scheduler = SimpleNamespace(runtime=SimpleNamespace(agent=object()))
    assert not supervisor._thread_lane_can_retry(scheduler, "base", "t")
    assert recorded == []
    now[0] += 60
    assert supervisor._thread_lane_can_retry(scheduler, "base", "t")
    assert recorded == []


def test_lane_log_failure_does_not_change_pause(monkeypatch):
    from agent_py_agent.agent.gateway_parts.loop_health import loop_health

    retries = gateway_lane_retry.BackgroundLaneRetry()
    now = _clock(monkeypatch)
    noted = []
    monkeypatch.setattr(loop_health, "note_print_failure", lambda context, exc: noted.append((context, type(exc))))

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("builtins.print", full_disk)
    retries.failed("a", "t", ProviderConnectionError("dns"), delay=30)
    now[0] += 30
    assert not retries.ready("a", "t", model_ready=lambda: True, fingerprint=lambda: "same")
    assert noted == [("gateway_lane_retry", OSError)]


def test_planner_pauses_environment_fault_and_resumes_on_model_change(tmp_path, monkeypatch, capsys):
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
    from agent_py_agent.cli import gateway_loops
    from agent_py_agent.tests.test_model_profiles import add
    from agent_py_agent.tests.test_thread_model_selection import host_with_store

    host = host_with_store(tmp_path)
    first = add(host, model_name="first-model")[0]
    target = execute_local_model_operation(host, "old", "select", {"profile_id": first})["thread_id"]
    requests = []
    prepared = []
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = host
    supervisor._inflight = {}
    supervisor._owner_schedulers = {}
    supervisor._lane_retry = gateway_lane_retry.BackgroundLaneRetry()

    def fail(thread_id):
        requests.append(thread_id)
        raise ProviderRequestRejectedError("HTTP 401", status_code=401)

    scheduler = SimpleNamespace(
        runtime=SimpleNamespace(agent=host), tick_thread=fail, prepare_tick=lambda **kw: None,
        ready_thread_ids=lambda **kw: (target, "healthy")[:kw["limit"]],
    )
    supervisor._base_scheduler = scheduler
    supervisor._submit_thread_candidates = lambda candidates, **kw: prepared.extend(candidates)

    def planned():
        prepared.clear()
        supervisor._submit_ready_thread_ticks()
        return prepared[0][2]

    def pause_again():
        supervisor._safe_thread_tick(scheduler, "base", target)
        assert planned() == ["healthy"]

    pause_again()
    # 30 秒普通冷却早已过去也不放行：只有模型指纹变化或探测时刻才放行。
    now = [gateway_lane_retry.time.monotonic() + 45]
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: now[0])
    assert planned() == ["healthy"]
    # 同值再选一次也会前进选择版本，算用户处理过。
    execute_local_model_operation(host, "old", "select", {"profile_id": first})
    assert planned() == [target, "healthy"]
    pause_again()
    # 只换当前模型服务商的密钥，选择不变，也要立即放行。
    execute_model_profile_operation(host, "save_provider", {
        "provider_id": "provider-" + first, "editing": True, "provider": {"api_key": "rotated-secret"},
    })
    assert planned() == [target, "healthy"]
    pause_again()
    # 改 owner 新会话默认值不影响已选模型的旧会话，不能放行。
    second = add(host, model_name="second-model")[0]
    execute_local_model_operation(host, "new", "set_default", {"profile_id": second})
    assert planned() == ["healthy"]
    execute_local_model_operation(host, "old", "select", {"profile_id": second})
    assert planned() == [target, "healthy"]
    assert requests == [target] * 3
    captured = capsys.readouterr()
    assert captured.out.count('"event": "lane_environment_paused"') == 3
    assert captured.out.count('"reason": "model_fingerprint_changed"') == 3
    logs = captured.out + captured.err
    assert "rotated-secret" not in logs and "only-private-secret" not in logs
