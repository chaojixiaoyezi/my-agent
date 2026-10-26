import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.process_events import (
    owner_has_pending_process_completions,
    process_completion_delivery_state,
    process_completion_target,
    reconcile_process_completions,
    task_has_pending_process_completion,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.tooling.process_registry import (
    BackgroundProcess,
    capture_process_birth_token,
    process_registry,
)
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope
from agent_py_agent.agent.tooling.process_session_records import (
    LEGACY_PROCESS_SESSION_SCHEMA,
    PROCESS_SESSION_SCHEMA,
)
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionListReport,
    ProcessSessionStore,
    ProcessSessionTransaction,
    process_session_store_root,
)


@pytest.mark.parametrize("entry", ["reconcile", "owner", "task"])
def test_completion_discovery_uses_fixed_query_and_strict_second_pass(tmp_path, monkeypatch, entry):
    from agent_py_agent.agent.conversation import process_events as module

    agent, params, authority = _fixture(tmp_path)
    calls = []
    record = authority.load("bg-test-notice").record

    class Authority:
        def list_pending_completion_records_report(self, **options):
            calls.append(options)
            rows = [record] if entry != "reconcile" and not options.get("cheap_checks") else []
            return ProcessSessionListReport(rows, [])

        def list_records(self, **kwargs):
            pytest.fail("新读口存在时不得全量deepcopy")

    monkeypatch.setattr(module, "ProcessSessionStore", lambda root: Authority())
    if entry == "reconcile":
        assert reconcile_process_completions(agent) == 0
    elif entry == "owner":
        assert owner_has_pending_process_completions(agent.home_paths.owner_home_dir)
    else:
        assert task_has_pending_process_completion(agent, params.task_id)
    assert calls
    if entry != "reconcile":
        assert [item["cheap_checks"] for item in calls] == [True, False]


def test_completion_query_internal_typeerror_is_not_hidden_by_compatibility(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import process_events as module

    agent, _params, _authority = _fixture(tmp_path)

    class Authority:
        def list_pending_completion_records_report(self, **options):
            raise TypeError("real query failure")

        def list_records(self, **options):
            pytest.fail("不能把内部TypeError误判为旧接口")

    monkeypatch.setattr(module, "ProcessSessionStore", lambda root: Authority())
    with pytest.raises(TypeError, match="real query failure"):
        reconcile_process_completions(agent)


def test_completion_ttl_runs_without_candidates_and_is_throttled_per_root(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import process_events as module

    calls, clock = [], [1000.0]
    owner = tmp_path / "owner"
    wakes = {"pending": SimpleNamespace(status="pending"), "done": SimpleNamespace(status="consumed")}
    store = SimpleNamespace(storage=SimpleNamespace(root=owner / "conversations"), wakes=SimpleNamespace(get=wakes.get))
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_home_dir=owner), config=SimpleNamespace())

    class Authority:
        def list_pending_completion_records_report(self, **options):
            return ProcessSessionListReport([], [])

        def list_records(self, **kwargs):
            return [], []

        def prune_finished(self, **options):
            calls.append(options)
            consumed = options["is_notice_consumed"]
            assert not consumed("pending")
            assert consumed("done") and consumed("missing")
            return []

    monkeypatch.setattr(module, "ProcessSessionStore", lambda root: Authority())
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]), raising=False)
    for delta in (0, 1, 299, 300):
        clock[0] = 1000.0 + delta
        reconcile_process_completions(agent)
    assert len(calls) == 2 and all(item["max_age_seconds"] == 86400.0 for item in calls)


# LLM: 测试使用真实持久存储与伪终态进程记录，不启动或杀宿主进程。
# 函数用途: 组装隔离 owner/thread 的旧 v1 或 v4 普通任务记录，验证通知重入与权限边界。
def _fixture(tmp_path, status="exited", *, managed=False, stopped=False):
    owner = tmp_path / "owner"
    store = ConversationStore(owner / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "a", "channel": "tui", "channel_conversation_id": "a"})
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_home_dir=owner),
                            config=SimpleNamespace(background_process_notifications=True))
    params = SimpleNamespace(context_scope="conversation", run_id="run-a", task_id="task-a", request_id="req-a",
                             task_attributes={"conversation_thread_id": thread.thread_id, "conversation_task_id": "task-a"})
    target = process_completion_target(agent, params)
    root = process_session_store_root(owner, owner)
    authority = ProcessSessionStore(root)
    record = BackgroundProcess("bg-test-notice", "test", 99999999, 1.0,
        access_scope=ProcessAccessScope("a", thread.thread_id, str(owner)), pid_birth_token="test",
        status=status, exit_code=0, completion_target=target, store_root=str(root))
    record.persisted_snapshot = {"schema": LEGACY_PROCESS_SESSION_SCHEMA}
    payload = record.to_record()
    if managed:
        child_known = status in {"running", "unknown", "exited", "killed"}
        payload.update(schema=PROCESS_SESSION_SCHEMA, revision=0, activation_scope=None, retain_until_consumed=False,
                       execution_scope={"owner_home": str(owner), "thread_id": thread.thread_id,
                                        "root_task_id": "task-a", "run_id": "run-a", "attempt_id": "attempt-a"},
                       launcher_pid=os.getpid(), launcher_birth_token=capture_process_birth_token(os.getpid()),
                       pid=99999999 if child_known else 0, pid_birth_token="test" if child_known else "",
                       child_pid=99999998 if child_known else 0, child_pid_birth_token="test-child" if child_known else "",
                       reserved_at=1.0, started_at=1.0 if child_known else 0, child_launch_started=child_known,
                       handoff_confirmed=child_known, stop_requested=stopped,
                       finished_at=2.0 if status in {"exited", "killed", "not_started"} else None,
                       exit_code=0 if status in {"exited", "killed"} else None)
    authority.write(payload)
    process_registry.clear()
    return agent, params, authority


# LLM: 仅模拟proc不可用和ps响应，不发信号；同时守住两个POSIX探针的locale及其余环境继承。
# 函数用途: 跨平台验证出生指纹和僵尸状态探针都为ps单独固定C locale，不修改父环境。
@pytest.mark.parametrize("probe", ["birth", "zombie"])
@pytest.mark.parametrize("locale_name", ["LC_ALL", "LANG", "LC_TIME"])
def test_posix_process_probes_force_c_locale(monkeypatch, probe, locale_name):
    from agent_py_agent.agent.tooling import process_registry as registry_module

    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv(locale_name, "zh_CN.UTF-8")
    monkeypatch.setenv("PROCESS_LOCALE_TEST_MARKER", "inherited")
    monkeypatch.setattr(registry_module, "_IS_WINDOWS", False)
    original_read_text, original_is_dir = Path.read_text, Path.is_dir

    def without_proc_read(path, *args, **kwargs):
        if path == Path(f"/proc/{os.getpid()}/stat"):
            raise FileNotFoundError("test has no proc")
        return original_read_text(path, *args, **kwargs)

    def without_proc_dir(path):
        return False if path == Path("/proc") else original_is_dir(path)

    def run_ps(argv, **kwargs):
        assert argv == ["ps", "-o", "lstart=" if probe == "birth" else "stat=", "-p", str(os.getpid())]
        child_env = kwargs.get("env") or {}
        assert child_env.get("LC_ALL") == "C"
        assert child_env.get("PROCESS_LOCALE_TEST_MARKER") == "inherited"
        assert kwargs["stdin"] == registry_module.subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout="Sun Oct 25 05:00:00 2026\n" if probe == "birth" else "Z+\n")

    monkeypatch.setattr(Path, "read_text", without_proc_read)
    monkeypatch.setattr(Path, "is_dir", without_proc_dir)
    monkeypatch.setattr(registry_module.subprocess, "run", run_ps)
    if probe == "birth":
        assert capture_process_birth_token(os.getpid()) == "host:Sun Oct 25 05:00:00 2026"
    else:
        assert registry_module._process_is_zombie(os.getpid()) is True
    assert os.environ[locale_name] == "zh_CN.UTF-8"


# LLM: Darwin上保留真实ps与libproc观察，不用模拟出生时间；旧Store投影只有秒精度，不改原生微秒身份。
# 函数用途: 在非英文环境变量下核验本进程ps指纹的整数秒及原内核身份的Store投影完全一致。
@pytest.mark.skipif(sys.platform != "darwin", reason="真实ps/libproc出生时间对齐仅适用于macOS")
@pytest.mark.parametrize("lc_all", ["zh_CN.UTF-8", None])
def test_macos_process_birth_matches_kernel_under_non_english_locale(monkeypatch, lc_all):
    from datetime import datetime

    from agent_py_agent.agent.gateway_parts.multiprocess_executor_identity import process_birth
    from agent_py_agent.agent.gateway_parts.multiprocess_native_host_observation import store_birth
    from agent_py_agent.agent.tooling import process_registry as registry_module

    if lc_all is None:
        monkeypatch.delenv("LC_ALL", raising=False)
    else:
        monkeypatch.setenv("LC_ALL", lc_all)
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    monkeypatch.setenv("LC_TIME", "zh_CN.UTF-8")
    original_run = registry_module.subprocess.run
    seen = []

    def observe_ps(argv, **kwargs):
        assert argv == ["ps", "-o", "lstart=", "-p", str(os.getpid())]
        child_env = kwargs.get("env") or {}
        assert child_env.get("LC_ALL") == "C"
        assert child_env.get("LANG") == "zh_CN.UTF-8"
        seen.append(argv)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(registry_module.subprocess, "run", observe_ps)
    token = capture_process_birth_token(os.getpid())
    assert len(seen) == 1
    birth = process_birth(os.getpid())
    assert birth is not None
    seconds = int(datetime.strptime(token.removeprefix("host:"), "%a %b %d %H:%M:%S %Y").timestamp())
    assert seconds == int(birth.split(":", 1)[0])
    assert token == store_birth(birth)
    assert os.environ.get("LC_ALL") == lc_all


# LLM: 仅控制出生观察，不启动ps或发送信号；当前C指纹与磁盘旧本地化指纹指向同一虚拟实例。
# 函数用途: 为升级边界及未知观察测试提供原模块和可审计的原生locale探针调用。
@pytest.fixture
def legacy_birth_probe(monkeypatch):
    from agent_py_agent.agent.tooling import process_registry as registry_module

    pid = os.getpid()
    legacy = "host:六 10/25 04:12:00 2026"
    current = "host:Sat Oct 25 04:12:00 2026"
    calls = []

    def native_ps(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv == ["ps", "-p", str(pid), "-o", "lstart="]
        assert "env" not in kwargs
        assert kwargs["stdin"] == registry_module.subprocess.DEVNULL
        assert kwargs["timeout"] == 2 and kwargs["check"] is False
        return SimpleNamespace(returncode=0, stdout="  六  10/25 04:12:00 2026\n")

    monkeypatch.setattr(registry_module, "_IS_WINDOWS", False)
    # 每个旧locale样本独立复核，避免其它用例的成功等价缓存掩盖拒读/复用反例。
    monkeypatch.setattr(registry_module, "_LEGACY_BIRTH_EQUIVALENTS", {}, raising=False)
    monkeypatch.setattr(registry_module, "capture_process_birth_token", lambda observed: current)
    monkeypatch.setattr(registry_module, "_process_is_zombie", lambda observed: False)
    monkeypatch.setattr(registry_module.os, "kill", lambda observed, signum: None)
    monkeypatch.setattr(registry_module.subprocess, "run", native_ps)
    return SimpleNamespace(module=registry_module, pid=pid, legacy=legacy, current=current, calls=calls)


@pytest.mark.parametrize("entry,expected", [("_same_process", True), ("_process_instance_terminated", False)])
def test_legacy_localized_birth_still_identifies_live_process(legacy_birth_probe, entry, expected):
    probe = legacy_birth_probe
    assert getattr(probe.module, entry)(probe.pid, probe.legacy) is expected
    assert len(probe.calls) == 1


@pytest.mark.parametrize("failure", ["permission", "timeout", "exit_code", "empty"])
def test_legacy_birth_probe_failure_does_not_prove_termination(monkeypatch, legacy_birth_probe, failure):
    probe = legacy_birth_probe

    def failed_native(argv, **kwargs):
        if failure == "permission":
            raise PermissionError("native ps denied")
        if failure == "timeout":
            raise probe.module.subprocess.TimeoutExpired(argv, 2)
        return SimpleNamespace(returncode=1 if failure == "exit_code" else 0, stdout="")

    monkeypatch.setattr(probe.module.subprocess, "run", failed_native)
    assert probe.module._process_instance_terminated(probe.pid, probe.legacy) is False
    assert probe.module._same_process(probe.pid, probe.legacy) is False


@pytest.mark.parametrize("expected,current,windows,result", [
    ("host:Sat Oct 25 04:12:00 2026", "host:Sat Oct 25 04:12:00 2026", False, True),
    ("host:Fri Oct 24 04:12:00 2026", "host:Sat Oct 25 04:12:00 2026", False, False),
    ("proc:100", "proc:101", False, False),
    ("proc:100", "proc:100", False, True),
    ("host:638970000000000000", "host:638970000000000001", True, False),
    ("host:638970000000000000", "host:638970000000000000", True, True),
    ("host:六 10/25 04:12:00 2026", "", False, False),
    ("", "", False, False),
])
def test_birth_match_fast_paths_do_not_probe_native(monkeypatch, legacy_birth_probe, expected, current, windows, result):
    probe = legacy_birth_probe
    matcher = getattr(probe.module, "_birth_token_matches", None)
    assert callable(matcher), "需要统一出生指纹比较入口"
    monkeypatch.setattr(probe.module, "_IS_WINDOWS", windows)
    assert matcher(probe.pid, expected, current_token=current) is result
    assert probe.calls == []


def test_different_native_birth_still_proves_pid_reuse(monkeypatch, legacy_birth_probe):
    probe = legacy_birth_probe
    monkeypatch.setattr(probe.module.subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(returncode=0, stdout="日 10/26 04:12:00 2026"))
    assert probe.module._same_process(probe.pid, probe.legacy) is False
    assert probe.module._process_instance_terminated(probe.pid, probe.legacy) is True


@pytest.mark.parametrize("cached_token,same_instance", [
    ("host:Sat Oct 25 04:12:00 2026", True),
    ("host:Fri Oct 24 04:12:00 2026", False),
])
def test_legacy_birth_keeps_cached_handle_without_rewriting_stored_token(tmp_path, legacy_birth_probe, cached_token, same_instance):
    from unittest.mock import Mock

    probe = legacy_birth_probe
    _, _, authority = _fixture(tmp_path)
    payload = authority.load("bg-test-notice").record
    payload.update(pid=probe.pid, pid_birth_token=probe.legacy, status="running", exit_code=None, finished_at=None)
    registry = probe.module.ProcessRegistry()
    cached = registry._cache_payload(payload, authority.root)
    cached.pid_birth_token = cached_token
    handle = cached.process = Mock()
    handle.poll.return_value = None
    refreshed = registry._cache_payload(payload, authority.root)
    assert refreshed.process is (handle if same_instance else None)
    assert refreshed.pid_birth_token == payload["pid_birth_token"] == probe.legacy
    assert registry._pending_reap == ([] if same_instance else [handle])
    assert handle.poll.called is not same_instance
    handle.wait.assert_not_called()
    handle.terminate.assert_not_called()
    handle.kill.assert_not_called()


def test_legacy_birth_tree_stop_guard_uses_compatible_identity(monkeypatch, legacy_birth_probe):
    probe = legacy_birth_probe
    signals = []
    monkeypatch.setattr(probe.module, "_process_tree_snapshot", lambda pid: ({pid: probe.current}, True))
    monkeypatch.setattr(probe.module, "_signal_process_snapshot", lambda snapshot, signum: signals.append(signum) or False)
    monkeypatch.setattr(probe.module, "_wait_process_snapshot_gone", lambda *args: False)
    receipt = probe.module.terminate_process_tree(probe.pid, None, expected_birth_token=probe.legacy)
    assert signals == [probe.module.signal.SIGTERM]
    assert receipt.confirmed is False


def test_legacy_birth_snapshot_merge_does_not_invent_pid_reuse(legacy_birth_probe):
    probe = legacy_birth_probe
    snapshot, terminated = {probe.pid: probe.legacy}, {probe.pid}
    probe.module._merge_tree_snapshot(snapshot, {probe.pid: probe.current}, terminated)
    assert terminated == {probe.pid}
    assert snapshot[probe.pid] == probe.current


@pytest.mark.parametrize("legacy,current", [
    ("host:dim. oct.  4 04:12:00 2026", "host:Sun Oct  4 04:12:00 2026"),
    ("host:六  10/25 04:12:00 2026", "host:Sat Oct 25 04:12:00 2026"),
])
def test_legacy_birth_preserves_internal_date_whitespace(monkeypatch, legacy_birth_probe, legacy, current):
    probe = legacy_birth_probe
    monkeypatch.setattr(probe.module, "capture_process_birth_token", lambda pid: current)
    monkeypatch.setattr(probe.module.subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(returncode=0, stdout=legacy[5:] + "\n"))
    assert probe.module._same_process(probe.pid, legacy) is True
    assert probe.module._process_instance_terminated(probe.pid, legacy) is False


# LLM: 只模拟strptime的已知locale依赖，不修改全局locale或要求系统安装外语locale。
# 函数用途: 原生复核资格必须由固定C词法决定，不能被Python当前LC_TIME翻转。
@pytest.mark.parametrize("localized", [True, False])
def test_birth_c_format_detection_ignores_python_locale(monkeypatch, legacy_birth_probe, localized):
    probe = legacy_birth_probe
    expected = "host:dim. oct. 25 04:12:00 2026" if localized else "host:Fri Oct 24 04:12:00 2026"
    original_strptime = probe.module.time.strptime

    def foreign_locale_parse(value, fmt):
        if fmt == "%a %b %d %H:%M:%S %Y":
            if value.startswith("dim."):
                return object()
            raise ValueError("English names are not the Python LC_TIME names")
        return original_strptime(value, fmt)

    def native_ps(argv, **kwargs):
        probe.calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=expected[5:] + "\n")

    monkeypatch.setattr(probe.module.time, "strptime", foreign_locale_parse)
    monkeypatch.setattr(probe.module.subprocess, "run", native_ps)
    assert probe.module._same_process(probe.pid, expected) is localized
    assert len(probe.calls) == int(localized)


# LLM: 只替换该用例的原生观察结果；失败仍保持异常或空/坏退出码，不构造肯定身份。
# 函数用途: 为取消观察、完成快路径和缓存重试提供相同的拒读反例。
def _failed_legacy_ps(probe, failure):
    # LLM: 保存真实调用参数供次数断言，不启动ps或发送任何信号。
    # 函数用途: 返回一种可重复的未知出生观察。
    def fail(argv, **kwargs):
        probe.calls.append((argv, kwargs))
        if failure == "permission":
            raise PermissionError("native ps denied")
        if failure == "timeout":
            raise probe.module.subprocess.TimeoutExpired(argv, 2)
        if failure == "value":
            raise ValueError("native observation unavailable")
        return SimpleNamespace(returncode=1 if failure == "exit_code" else 0, stdout="")
    return fail


# LLM: PID/内核出生只做可控观察，既有句柄锁与比较函数仍是真实现，不执行真实清理。
# 函数用途: 为完成通知的旧本地化身份快路径准备一个存活实例。
def _legacy_completion_probe(monkeypatch, probe):
    from agent_py_agent.agent.conversation import process_events

    monkeypatch.setattr(probe.module, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(process_events, "kernel_terminated", lambda pid: False)
    monkeypatch.setattr(process_events, "process_birth", lambda pid: "kernel-observation")
    monkeypatch.setattr(process_events, "store_birth", lambda birth: probe.current)
    return process_events


# LLM: runner使用冻结的原PID与旧token，仍走真实cleanup与统一比较，不授整树杀进程权限。
# 函数用途: 创建只读宿主取消观察夹具。
@pytest.fixture
def legacy_runner_stop(monkeypatch, legacy_birth_probe):
    from agent_py_agent.agent.subagents import cancellation_hosts

    probe = legacy_birth_probe
    monkeypatch.setattr(cancellation_hosts, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(cancellation_hosts, "capture_process_birth_token", lambda pid: probe.current)
    stop = cancellation_hosts.FrozenRunnerStop({"status": "cooperative_requested", "pid": probe.pid}, probe.pid, probe.legacy)
    return cancellation_hosts, stop


# LLM: 同PID原生旧locale复核成功不等于宿主退出，冻结报告不可被原地修改。
# 函数用途: 锁住取消宿主因语言不同而误报instance_replaced的修复。
def test_runner_cleanup_legacy_birth_is_same_live_host(legacy_birth_probe, legacy_runner_stop):
    hosts, stop = legacy_runner_stop
    before = dict(stop.report)
    result = hosts.cleanup_runner_stop(stop)
    assert result == {**before, "host_exited": False, "identity_confirmed": True}
    assert stop.report == before
    assert len(legacy_birth_probe.calls) == 1


# LLM: 原生观察的权限/超时/坏形状/空结果均不得作为肯定退出或肯定身份。
# 函数用途: 验证取消观察遇未知时保留未确认状态。
@pytest.mark.parametrize("failure", ["permission", "timeout", "value", "exit_code", "empty"])
def test_runner_cleanup_failed_birth_observation_is_not_exit(monkeypatch, legacy_birth_probe, legacy_runner_stop, failure):
    probe = legacy_birth_probe
    hosts, stop = legacy_runner_stop
    monkeypatch.setattr(probe.module.subprocess, "run", _failed_legacy_ps(probe, failure))
    assert hosts.cleanup_runner_stop(stop) == {**stop.report, "host_exited": False, "identity_confirmed": False}


# LLM: 旧locale复核若确为不同出生时间，仍必须认定原实例已被替换。
# 函数用途: 防兼容逻辑把真实PID复用误当同一宿主。
def test_runner_cleanup_confirms_real_birth_change(monkeypatch, legacy_birth_probe, legacy_runner_stop):
    hosts, stop = legacy_runner_stop
    monkeypatch.setattr(legacy_birth_probe.module.subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(returncode=0, stdout="日 10/26 04:12:00 2026"))
    result = hosts.cleanup_runner_stop(stop)
    assert result["status"] == "instance_replaced" and result["host_exited"] is True


# LLM: 当前出生观察为空时不得拿旧记录或PID存活补猜身份，也不发起原生复核。
# 函数用途: 守住取消入口空观察的保守分支。
def test_runner_cleanup_empty_current_birth_keeps_identity_unknown(monkeypatch, legacy_birth_probe, legacy_runner_stop):
    hosts, stop = legacy_runner_stop
    monkeypatch.setattr(hosts, "capture_process_birth_token", lambda pid: "")
    assert hosts.cleanup_runner_stop(stop) == {**stop.report, "host_exited": False, "identity_confirmed": False}
    assert legacy_birth_probe.calls == []


# LLM: 同一三元组连续调用只允许一次原生复核，之后不再拉起子进程。
# 函数用途: 复现并防止旧locale身份的重复ps开销。
def test_legacy_birth_equivalence_reuses_one_native_observation(legacy_birth_probe):
    probe = legacy_birth_probe
    for _ in range(8):
        assert probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current)
    assert len(probe.calls) == 1


# LLM: 三个真实入口共享同一等价缓存，不互相改写冻结token或会话负载。
# 函数用途: 验证任意入口首次复核后，后续完成tick和取消观察共用结果。
@pytest.mark.parametrize("first", ["matcher", "completion", "cleanup"])
def test_legacy_birth_equivalence_is_shared_by_completion_ticks_and_cleanup(
    tmp_path, monkeypatch, legacy_birth_probe, legacy_runner_stop, first,
):
    probe = legacy_birth_probe
    events = _legacy_completion_probe(monkeypatch, probe)
    hosts, stop = legacy_runner_stop
    payload = {"pid": probe.pid, "pid_birth_token": probe.legacy}
    before = dict(payload)
    operations = {
        "matcher": lambda: probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current),
        "completion": lambda: events._managed_instance_still_live("legacy-cache", tmp_path, payload),
        "cleanup": lambda: hosts.cleanup_runner_stop(stop).get("identity_confirmed"),
    }
    assert operations[first]() is True
    for _ in range(8):
        assert all(operation() is True for operation in operations.values())
    assert payload == before and stop.birth_token == probe.legacy
    assert len(probe.calls) == 1


# LLM: 完成快路径只确认同一存活实例，观察异常必须回退而不能向调用方传播为完成。
# 函数用途: 验证旧locale复核失败时快路径返回False。
@pytest.mark.parametrize("failure", ["permission", "timeout", "value", "exit_code", "empty"])
def test_completion_legacy_birth_probe_failure_is_not_live(tmp_path, monkeypatch, legacy_birth_probe, failure):
    probe = legacy_birth_probe
    events = _legacy_completion_probe(monkeypatch, probe)
    monkeypatch.setattr(probe.module.subprocess, "run", _failed_legacy_ps(probe, failure))
    assert events._managed_instance_still_live("legacy-failure", tmp_path,
        {"pid": probe.pid, "pid_birth_token": probe.legacy}) is False
    assert len(probe.calls) == 1


# LLM: 缓存的PID/旧token/当前token任一改变都须重新原生观察；否定结果不能记成功等价。
# 函数用途: 防跨PID、旧记录或当前出生时间误命中缓存。
@pytest.mark.parametrize("part", ["pid", "expected", "current"])
def test_legacy_birth_equivalence_cache_binds_entire_instance_triple(monkeypatch, legacy_birth_probe, part):
    probe = legacy_birth_probe
    assert probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current)
    monkeypatch.setattr(probe.module.subprocess, "run", lambda argv, **kwargs:
        probe.calls.append((argv, kwargs)) or SimpleNamespace(returncode=0, stdout="日 10/26 04:12:00 2026"))
    pid, expected, current = probe.pid, probe.legacy, probe.current
    if part == "pid":
        pid += 1
    elif part == "expected":
        expected = "host:五 10/24 04:12:00 2026"
    else:
        current = "host:Sun Oct 26 04:12:00 2026"
    assert not probe.module._birth_token_matches(pid, expected, current)
    assert not probe.module._birth_token_matches(pid, expected, current)
    assert len(probe.calls) == 3, "不同三元组必须重查，否定结果不得缓存"


# LLM: 257个虚拟身份只保留最新256条成功证明；命中不增探针，淘汰后必须重查。
# 函数用途: 验证FIFO上限及淘汰后的正确复核。
def test_legacy_birth_equivalence_cache_is_bounded_and_evicts_oldest(monkeypatch, legacy_birth_probe):
    probe = legacy_birth_probe
    monkeypatch.setattr(probe.module.subprocess, "run", lambda argv, **kwargs:
        probe.calls.append((argv, kwargs)) or SimpleNamespace(returncode=0, stdout=probe.legacy[5:]))
    for offset in range(257):
        assert probe.module._birth_token_matches(probe.pid + offset, probe.legacy, probe.current)
    assert len(probe.module._LEGACY_BIRTH_EQUIVALENTS) == 256
    assert probe.module._birth_token_matches(probe.pid + 256, probe.legacy, probe.current)
    assert len(probe.calls) == 257
    assert probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current)
    assert len(probe.calls) == 258 and len(probe.module._LEGACY_BIRTH_EQUIVALENTS) == 256


# LLM: 并发调用复用同一次原生观察；线程全部收束后才断言，不制造后台遗留。
# 函数用途: 验证缓存检查/填充的锁不会重复发起ps。
def test_legacy_birth_equivalence_cache_coalesces_concurrent_checks(legacy_birth_probe):
    from concurrent.futures import ThreadPoolExecutor

    probe = legacy_birth_probe
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current), range(32)))
    assert len(probe.calls) == 1


# LLM: 未知观察每次均需重试，只有后续真正成功后才能缓存，不能复用异常或否定。
# 函数用途: 验证探针失败不会污染等价缓存或阻止恢复。
@pytest.mark.parametrize("failure", ["permission", "timeout", "value", "exit_code", "empty"])
def test_legacy_birth_equivalence_cache_does_not_store_unknown_observation(monkeypatch, legacy_birth_probe, failure):
    probe = legacy_birth_probe
    native = probe.module.subprocess.run
    monkeypatch.setattr(probe.module.subprocess, "run", _failed_legacy_ps(probe, failure))
    for _ in range(2):
        with pytest.raises((OSError, probe.module.subprocess.TimeoutExpired, ValueError)):
            probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current)
    monkeypatch.setattr(probe.module.subprocess, "run", native)
    assert probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current)
    assert probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current)
    assert len(probe.calls) == 3


# LLM: 已缓存的等价不能掩盖当前PID消失、僵尸或出生观察为空，仍由每拍原观察否决。
# 函数用途: 证明缓存不授予存活结论。
@pytest.mark.parametrize("missing", ["dead", "zombie", "birth"])
def test_cached_legacy_equivalence_never_replaces_current_liveness_check(tmp_path, monkeypatch, legacy_birth_probe, missing):
    probe = legacy_birth_probe
    events = _legacy_completion_probe(monkeypatch, probe)
    assert probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current)
    if missing == "dead":
        monkeypatch.setattr(probe.module, "_pid_alive", lambda pid: False)
    elif missing == "zombie":
        monkeypatch.setattr(events, "kernel_terminated", lambda pid: True)
    else:
        monkeypatch.setattr(events, "store_birth", lambda birth: "")
    assert not events._managed_instance_still_live("legacy-gone", tmp_path,
        {"pid": probe.pid, "pid_birth_token": probe.legacy})
    assert len(probe.calls) == 1


# LLM: 使用真实ProcessSessionStore持久旧token，复用比较/完成快路径后逐字节核对，不能伪装迁移写盘。
# 函数用途: 证明成功等价仅留内存，磁盘会话记录和旧出生指纹完全不变。
def test_legacy_birth_equivalence_never_rewrites_persisted_process_token(tmp_path, monkeypatch, legacy_birth_probe):
    probe = legacy_birth_probe
    events = _legacy_completion_probe(monkeypatch, probe)
    _, _, authority = _fixture(tmp_path)
    payload = dict(authority.load("bg-test-notice").record)
    payload.update(session_id="bg-legacy-cache", pid=probe.pid, pid_birth_token=probe.legacy,
        status="running", exit_code=None, finished_at=None)
    payload = authority.write(payload)
    path = authority.root / "bg-legacy-cache.json"
    before = path.read_bytes()
    for _ in range(8):
        assert probe.module._birth_token_matches(probe.pid, probe.legacy, probe.current)
        assert events._managed_instance_still_live("bg-legacy-cache", authority.root, payload)
    assert path.read_bytes() == before
    assert authority.load("bg-legacy-cache").record["pid_birth_token"] == probe.legacy
    assert len(probe.calls) == 1


def test_completion_survives_restart_and_notifies_once(tmp_path):
    agent, params, authority = _fixture(tmp_path)
    assert owner_has_pending_process_completions(agent.home_paths.owner_home_dir)
    assert reconcile_process_completions(agent) == 1
    signals = agent.conversation_store.wakes.pending(limit=0)
    assert len(signals) == 1
    assert signals[0].root_task_id == params.task_id
    assert signals[0].metadata["process_completion"]["exit_code"] == 0
    assert authority.load("bg-test-notice").record["completion_notice_id"] == signals[0].wake_signal_id
    process_registry.clear()
    assert reconcile_process_completions(agent) == 0
    assert not owner_has_pending_process_completions(agent.home_paths.owner_home_dir)


def test_publish_crash_retry_dedupes_and_keeps_receipt(tmp_path, monkeypatch):
    agent, _, authority = _fixture(tmp_path)
    original = ProcessSessionTransaction.write

    def fail_receipt(self, payload):
        if payload.get("completion_notice_id"):
            raise OSError("simulated receipt write failure")
        return original(self, payload)

    monkeypatch.setattr(ProcessSessionTransaction, "write", fail_receipt)
    reconcile_process_completions(agent)
    assert not authority.load("bg-test-notice").record["completion_notice_id"]
    monkeypatch.setattr(ProcessSessionTransaction, "write", original)
    assert reconcile_process_completions(agent) == 1
    assert len(agent.conversation_store.wakes.pending(limit=0)) == 1


def test_explicit_stop_is_not_auto_restarted(tmp_path):
    agent, _, authority = _fixture(tmp_path, "killed")
    assert reconcile_process_completions(agent) == 0
    assert authority.load("bg-test-notice").record["completion_notice_id"] == "explicit_stop"
    assert not agent.conversation_store.wakes.pending(limit=0)


def test_late_completion_is_deliverable_but_stopped_task_is_not(tmp_path):
    from dataclasses import replace

    agent, params, _authority = _fixture(tmp_path)
    store = agent.conversation_store
    store.tasks.bind({"thread_id": params.task_attributes["conversation_thread_id"],
                     "task_id": params.task_id, "status": "completed", "goal": "test"})
    reconcile_process_completions(agent)
    signal = store.wakes.pending(limit=0)[0]
    assert process_completion_delivery_state(agent, signal) == "ready"
    assert not process_completion_delivery_state(agent, replace(signal, source_agent_id="another-run"))
    stopped, stopped_params, _ = _fixture(tmp_path / "stopped")
    stopped.conversation_store.tasks.bind({"thread_id": stopped_params.task_attributes["conversation_thread_id"],
        "task_id": stopped_params.task_id, "status": "interrupted", "goal": "test"})
    reconcile_process_completions(stopped)
    stopped_signal = stopped.conversation_store.wakes.pending(limit=0)[0]
    assert not process_completion_delivery_state(stopped, stopped_signal)


@pytest.mark.parametrize("status,expected", [("completed", 1), ("interrupted", 0), ("cancelled", 0)])
def test_late_completion_passes_scheduler_and_claim_admission(tmp_path, monkeypatch, status, expected):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    prepared, params, authority = _fixture(tmp_path)
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=False), prepared.home_paths.owner_home_dir)
    agent.conversation_store = store = prepared.conversation_store
    root = process_session_store_root(agent.effective_workspace_root, agent.home_paths.owner_home_dir)
    record = authority.load("bg-test-notice").record
    record["access_scope"]["owner_home"] = str(agent.home_paths.owner_home_dir)
    ProcessSessionStore(root).write(record)
    calls = []

    class Backend:
        def generate(self, prompt, **kwargs):
            calls.append(prompt)
            return ModelResponse(text="采样进程已退出，退出码 0。", backend="test")

    agent.backend = Backend()
    store.tasks.bind({"thread_id": params.task_attributes["conversation_thread_id"],
        "task_id": params.task_id, "status": status, "goal": "采样后告知结果"})
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    original_write = ProcessSessionTransaction.write

    def fail_receipt(self, payload):
        if payload.get("completion_notice_id"):
            raise OSError("receipt temporarily unavailable")
        return original_write(self, payload)

    if expected:
        monkeypatch.setattr(ProcessSessionTransaction, "write", fail_receipt)
        reconcile_process_completions(agent)
        assert scheduler.tick() == []
        assert len(store.wakes.pending(limit=0)) == 1
        assert not calls
        monkeypatch.setattr(ProcessSessionTransaction, "write", original_write)
    assert reconcile_process_completions(agent) == 1
    reports = scheduler.tick()
    assert len(calls) == expected
    assert len(reports) == expected
    if expected:
        assert "退出码 0" in reports[0].response
        assert any("退出码 0" in row.content for row in store.messages.recent(params.task_attributes["conversation_thread_id"]))
    assert not store.wakes.pending(limit=0)
    assert scheduler.tick() == []
    assert len(calls) == expected


def test_disabled_notifications_and_child_scope_do_not_launch_shadow_main(tmp_path):
    agent, params, _ = _fixture(tmp_path)
    params.context_scope = "task_local"
    assert process_completion_target(agent, params) == {}
    agent.config.background_process_notifications = False
    assert reconcile_process_completions(agent) == 0
    assert not agent.conversation_store.wakes.pending(limit=0)


def test_completion_target_is_immutable_and_thread_scoped(tmp_path):
    _, _, authority = _fixture(tmp_path)
    record = authority.load("bg-test-notice").record
    record["completion_target"]["thread_id"] = "another-thread"
    with pytest.raises(ValueError):
        authority.write(record)


def test_notified_receipt_survives_stale_registry_write(tmp_path):
    agent, _, authority = _fixture(tmp_path)
    stale = authority.load("bg-test-notice").record
    reconcile_process_completions(agent)
    authority.write(stale)
    assert authority.load("bg-test-notice").record["completion_notice_id"]
    assert reconcile_process_completions(agent) == 0


@pytest.mark.parametrize("status", ["starting", "running", "unknown", "not_started", "killed"])
def test_managed_only_confirmed_natural_exit_can_publish_completion(tmp_path, status):
    agent, _, authority = _fixture(tmp_path, status, managed=True)
    assert reconcile_process_completions(agent) == 0
    assert not agent.conversation_store.wakes.pending(limit=0)
    record = authority.load("bg-test-notice").record
    assert bool(record["completion_notice_id"]) is (status in {"not_started", "killed"})


def test_managed_natural_completion_survives_cas_and_stopped_exit_never_wakes(tmp_path):
    agent, _, authority = _fixture(tmp_path / "natural", managed=True)
    before = authority.load("bg-test-notice").record
    assert reconcile_process_completions(agent) == 1
    assert authority.load("bg-test-notice").record["revision"] > before["revision"]
    assert reconcile_process_completions(agent) == 0
    stopped, _, stopped_authority = _fixture(tmp_path / "stopped", managed=True, stopped=True)
    assert reconcile_process_completions(stopped) == 0
    assert not stopped.conversation_store.wakes.pending(limit=0)
    assert stopped_authority.load("bg-test-notice").record["completion_notice_id"] == "explicit_stop"


def test_store_read_failure_keeps_pending_completion_for_retry(tmp_path):
    agent, params, authority = _fixture(tmp_path, managed=True)
    agent.conversation_store.tasks.bind({"thread_id": params.task_attributes["conversation_thread_id"],
                                        "task_id": params.task_id, "status": "completed", "goal": "test"})
    assert reconcile_process_completions(agent) == 1
    signal = agent.conversation_store.wakes.pending(limit=0)[0]
    (authority.root / ".process-sessions.redo.json").write_text("broken")
    assert owner_has_pending_process_completions(agent.home_paths.owner_home_dir)
    assert process_completion_delivery_state(agent, signal) == "receipt_pending"
    assert reconcile_process_completions(agent) == 0


def test_live_managed_process_skips_registry_probe_and_unknown_not_pending(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from agent_py_agent.agent.tooling import process_registry as registry_module

    agent, _params, authority = _fixture(tmp_path / "live", status="running", managed=True)
    record = dict(authority.load("bg-test-notice").record)
    (authority.root / "bg-test-notice.json").unlink()
    record["session_id"] = "bg-live-notice"
    record["revision"] = 0
    record["pid"] = os.getpid()
    record["pid_birth_token"] = capture_process_birth_token(os.getpid())
    authority.write(record)
    probe_calls: list[int] = []
    monkeypatch.setattr(
        registry_module,
        "_process_instance_terminated",
        lambda pid, birth_token="": probe_calls.append(pid) or False,
    )
    assert reconcile_process_completions(agent) == 0
    assert probe_calls == []

    # PID reuse (mismatched birth token) falls through to process_registry.get and transitions to unknown.
    reused = dict(authority.load("bg-live-notice").record)
    (authority.root / "bg-live-notice.json").unlink()
    reused["revision"] = 0
    reused["pid_birth_token"] = "host:reused-different-instance"
    authority.write(reused)
    monkeypatch.setattr(
        registry_module,
        "_process_instance_terminated",
        lambda pid, birth_token="": probe_calls.append(pid) or True,
    )
    assert reconcile_process_completions(agent) == 0
    assert probe_calls == [os.getpid()]
    assert authority.load("bg-live-notice").record["status"] == "unknown"

    # Terminal record refreshes local registry cache and polls attached Popen handle.
    term_agent, _term_params, term_auth = _fixture(tmp_path / "term", status="running", managed=True)
    running_rec = dict(term_auth.load("bg-test-notice").record)
    (term_auth.root / "bg-test-notice.json").unlink()
    running_rec["revision"] = 0
    running_rec["pid"] = os.getpid()
    running_rec["pid_birth_token"] = capture_process_birth_token(os.getpid())
    running_rec = term_auth.write(running_rec)
    proc_handle = Mock(pid=os.getpid())
    proc_handle.poll.return_value = 0
    process_registry.attach(running_rec, proc_handle, term_auth.root)
    term_auth.write({**running_rec, "status": "exited", "exit_code": 0, "finished_at": 2.0})
    assert reconcile_process_completions(term_agent) == 1
    assert proc_handle.poll.called
    cached = process_registry._processes.get((str(term_auth.root.resolve(strict=False)), "bg-test-notice"))
    assert cached is not None and cached.is_terminal()

    unknown_agent, unknown_params, _ = _fixture(tmp_path / "unknown", status="unknown", managed=True)
    assert not owner_has_pending_process_completions(unknown_agent.home_paths.owner_home_dir)
    assert not task_has_pending_process_completion(unknown_agent, unknown_params.task_id)


# LLM: 同时替换run身份哨兵才模拟原生路径；普通monkeypatch仍必须走原ps合同。
# 函数用途: 隔离无proc的内核观察、ps次数和内存缓存，不启动真实进程。
@pytest.fixture
def native_process_probe(monkeypatch):
    from collections import OrderedDict

    from agent_py_agent.agent.gateway_parts import multiprocess_native_host_observation as native
    from agent_py_agent.agent.tooling import process_registry as module

    probe = SimpleNamespace(module=module, native=native, calls=[], birth="1792929600:1",
                            state=False, output="Sun Oct 25 05:00:00 2026", code=0, failure=None,
                            original_kernel_terminated=native.kernel_terminated)
    original_read, original_dir = Path.read_text, Path.is_dir

    # LLM: 仅遮蔽proc探针，其它文本读取沿原实现。
    # 函数用途: 模拟无proc平台。
    def read(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            raise FileNotFoundError("no proc in this test")
        return original_read(path, *args, **kwargs)

    # LLM: 所有模拟ps仍须关闭stdin，保留原argv与env供断言。
    # 函数用途: 统计原生探针替身并注入结果。
    def run(argv, **kwargs):
        probe.calls.append((argv, kwargs))
        assert kwargs["stdin"] == module.subprocess.DEVNULL
        if probe.failure:
            raise probe.failure
        return SimpleNamespace(returncode=probe.code, stdout=probe.output)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "is_dir", lambda p: False if p == Path("/proc") else original_dir(p))
    monkeypatch.setattr(module, "_IS_WINDOWS", False)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "_DEFAULT_SUBPROCESS_RUN", run, raising=False)
    monkeypatch.setattr(module, "_PID_BIRTH_TOKEN_CACHE", OrderedDict(), raising=False)
    monkeypatch.setattr(module, "_PS_RELATION_CACHE", {}, raising=False)
    monkeypatch.setattr(native, "process_birth", lambda pid: probe.birth)
    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(native, "_darwin_info", lambda pid: (SimpleNamespace(prefix=[0, 2]), 0))
    # Store投影故意丢微秒：测试必须能识别同秒内的PID复用。
    monkeypatch.setattr(native, "store_birth", lambda birth: "host:Sun Oct 25 05:00:00 2026" if birth else "")
    monkeypatch.setattr(native, "kernel_terminated", lambda pid: probe.state)
    return probe


# LLM: 预热旧关系后模拟子PID转属无关进程，真实信号函数只接替身OS出口，禁止触达真实进程。
# 函数用途: 验证终止入口不能用TTL缓存扩大信号范围。
def test_native_relation_cache_never_authorizes_recycled_child_signals(native_process_probe, monkeypatch):
    probe, current, signals = native_process_probe, [False], []
    module = probe.module

    # LLM: 父/组表独立反映切换前后关系，birth为当前PID持有者。
    # 函数用途: 构造旧关系与新身份反例。
    def ps(argv, **kwargs):
        if "-axo" in argv:
            root_relation = 1 if "ppid" in argv[-1] else 321
            child_relation = 999 if current[0] else 321
            output = f"321 {root_relation}\n654 {child_relation}\n"
        else:
            output = "root birth" if argv[-1] == "321" else "unrelated birth"
        return SimpleNamespace(returncode=0, stdout=output)

    # LLM: 仅模拟信号后的确认以免真实等待，不参与目标成员选取。
    # 函数用途: 完成测试进程快照的退出回执。
    def waited(snapshot, proc, seconds, terminated):
        terminated.update(snapshot)
        return True

    monkeypatch.setattr(module.subprocess, "run", ps)
    monkeypatch.setattr(module, "_DEFAULT_SUBPROCESS_RUN", ps)
    monkeypatch.setattr(module.time, "monotonic", lambda: 10.0)
    assert module._process_parent_map()[0][654] == 321
    assert module._process_group_map()[0][654] == 321
    current[0] = True
    monkeypatch.setattr(module.os, "getpgrp", lambda: 9999)
    monkeypatch.setattr(module.os, "getpgid", lambda pid: 321 if pid == 321 else 999)
    monkeypatch.setattr(module.os, "killpg", lambda pgid, sig: signals.append(("group", pgid)))
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: signals.append(("pid", pid)) if sig else None)
    monkeypatch.setattr(module, "_wait_process_snapshot_gone", waited)
    module.terminate_process_tree(321, None, grace_seconds=0)
    assert ("group", 321) in signals
    assert ("group", 999) not in signals and ("pid", 654) not in signals


# LLM: 保留真正kernel helper，在底层注入持续EPERM；不能把观察错误当活进程跳过旧ps。
# 函数用途: 验证原生故障回退仍能识别僵尸。
def test_native_kernel_real_error_does_not_hide_ps_zombie(native_process_probe, monkeypatch):
    import errno

    probe = native_process_probe
    monkeypatch.setattr(probe.native.sys, "platform", "darwin")
    monkeypatch.setattr(probe.native, "kernel_terminated", probe.original_kernel_terminated)
    monkeypatch.setattr(probe.native, "_darwin_info", lambda pid: (None, errno.EPERM))
    probe.output = "Z"
    assert probe.module._process_is_zombie(321) is True
    assert len(probe.calls) == 1


@pytest.mark.parametrize("first_error", [1, 5])
@pytest.mark.parametrize("status", [2, 5])
# LLM: 真kernel先遇EPERM/EIO，再从同一底层读取恢复；复核结果必须覆盖被折叠的旧False，不能多跑ps。
# 函数用途: 验证瞬时故障后按新的活进程或僵尸状态返回。
def test_native_kernel_recovered_read_uses_its_actual_state(native_process_probe, monkeypatch, first_error, status):
    probe = native_process_probe
    reads = iter([(None, first_error), (SimpleNamespace(prefix=[0, status]), 0)])
    monkeypatch.setattr(probe.native, "kernel_terminated", probe.original_kernel_terminated)
    monkeypatch.setattr(probe.native, "_darwin_info", lambda pid: next(reads))
    assert probe.module._process_is_zombie(321) is (status == 5)
    assert not probe.calls


@pytest.mark.parametrize("state", [True, False])
# LLM: 原生确定状态必须避开ps，False复核使用一致的完整内核观察。
# 函数用途: 验证内核活进程和僵尸快路径。
def test_native_zombie_bool_avoids_ps(native_process_probe, state):
    probe = native_process_probe
    probe.state = state
    probe.output = "Z"
    assert probe.module._process_is_zombie(321) is state
    assert probe.calls == []


@pytest.mark.parametrize("state", [None, "unknown"])
# LLM: 非bool是未知，不允许按真值推断状态，回退必须保持C locale。
# 函数用途: 验证未知内核结果走原ps。
def test_native_zombie_unknown_falls_back_to_c_locale_ps(native_process_probe, state):
    probe = native_process_probe
    probe.state, probe.output = state, "Z+\n"
    assert probe.module._process_is_zombie(321)
    assert len(probe.calls) == 1
    assert probe.calls[0][1]["env"]["LC_ALL"] == "C"


# LLM: 同秒复用须贯穿缓存、返回token和最终实例检查；信号出口替身不得触达真实PID。
# 函数用途: 验证相同ps秒级文本不能覆盖不同微秒出生身份。
def test_native_birth_cache_rechecks_kernel_and_rejects_same_second_pid_reuse(native_process_probe, monkeypatch):
    probe = native_process_probe
    monkeypatch.setattr(probe.module.os, "kill", lambda pid, sig: None)
    token = probe.module.capture_process_birth_token(321)
    assert token == "host:" + probe.output + "#kb=1792929600:1"
    assert probe.module.capture_process_birth_token(321) == token
    assert len(probe.calls) == 1
    probe.birth = "1792929600:2"
    current = probe.module.capture_process_birth_token(321)
    assert current == "host:" + probe.output + "#kb=1792929600:2"
    assert current != token
    assert probe.module._birth_tokens_match(token, current) is False
    assert probe.module._birth_token_matches(321, token, current) is False
    assert probe.module._same_process(321, token) is False
    assert probe.module._process_instance_terminated(321, token) is True
    assert len(probe.calls) == 2
    assert set(probe.module._PID_BIRTH_TOKEN_CACHE) == {(321, "1792929600:1"), (321, "1792929600:2")}
    assert probe.module.capture_process_birth_token(322) == current
    assert len(probe.calls) == 3


# LLM: 新旧格式只在缺少一方微秒身份时兼容；双微秒不等必须短路，不能借旧locale探针放宽。
# 函数用途: 覆盖真实信号资格入口的精确匹配、旧记录过渡和空身份拒绝。
@pytest.mark.parametrize("expected,current,match", [
    ("host:Sun Oct 25 05:00:00 2026", "host:Sun Oct 25 05:00:00 2026#kb=1792929600:1", True),
    ("host:Sun Oct 25 05:00:00 2026#kb=1792929600:1", "host:Sun Oct 25 05:00:00 2026", True),
    (" host:date#kb=1:1 ", "host:date#kb=1:1", True),
    ("host:date#kb=1:1", "host:date#kb=1:2", False),
    ("host:date#kb=1:1", "host:other#kb=1:1", False),
    ("host:date", "host:other#kb=1:1", False),
    ("", "host:date#kb=1:1", False),
    ("host:date#kb=1:1", "", False),
])
def test_birth_identity_comparison_preserves_microseconds_and_legacy_tokens(native_process_probe, expected, current, match):
    probe = native_process_probe
    assert probe.module._birth_token_matches(321, expected, current) is match
    if "#kb=" in expected and "#kb=" in current:
        assert probe.calls == []


# LLM: ps抛OSError时必须保守返回空串且不污染出生缓存；待ps恢复后同一进程仍能采到一致格式并与落盘token匹配。
# 函数用途: 验证ps故障期间不制造第二种缓存格式、不将存活进程误判为已退出。
@pytest.mark.parametrize("failure", [PermissionError("ps denied"), OSError("ps unavailable")])
def test_native_birth_oserror_uses_kernel_identity(native_process_probe, monkeypatch, failure):
    probe = native_process_probe
    monkeypatch.setattr(probe.module.os, "kill", lambda pid, sig: None)
    expected_token = probe.module.capture_process_birth_token(321)
    assert expected_token == f"host:{probe.output}#kb=1792929600:1"
    probe.module._PID_BIRTH_TOKEN_CACHE.clear()
    probe.failure = failure
    assert probe.module.capture_process_birth_token(321) == ""
    assert (321, "1792929600:1") not in probe.module._PID_BIRTH_TOKEN_CACHE
    assert probe.module._process_instance_terminated(321, expected_token) is False
    probe.failure = None
    recovered_token = probe.module.capture_process_birth_token(321)
    assert recovered_token == expected_token
    assert probe.module._same_process(321, expected_token) is True
    assert probe.module._process_instance_terminated(321, expected_token) is False
    probe.birth = "1792929600:2"
    assert probe.module._same_process(321, expected_token) is False
    assert probe.module._process_instance_terminated(321, expected_token) is True


# LLM: 原通知活性入口仍传入store_birth秒级投影；基础文本不同时只能重采完整身份，ps不可用时抛观察错误而非判死。
# 函数用途: 验证微秒token兼容旧投影调用方，同时拒绝该投影掩盖的同秒复用。
@pytest.mark.parametrize("ps_denied", [False, True])
def test_native_microsecond_identity_rechecks_legacy_kernel_projection(native_process_probe, ps_denied):
    probe = native_process_probe
    token = probe.module.capture_process_birth_token(321)
    legacy = "host:" + probe.output
    assert probe.module._birth_tokens_match(token, legacy) is True
    probe.module._PID_BIRTH_TOKEN_CACHE.clear()
    probe.failure = PermissionError("ps denied") if ps_denied else None
    if ps_denied:
        with pytest.raises(OSError, match="native process birth observation unavailable"):
            probe.module._birth_token_matches(321, token, "host:1792929600")
        return
    assert probe.module._birth_token_matches(321, token, "host:1792929600") is True
    probe.birth = "1792929600:2"
    assert probe.module._birth_token_matches(321, token, "host:1792929600") is False


# LLM: 已记录微秒身份后内核拒读是观察缺口；不得发送信号，也不得把不确定匹配取反成死亡。
# 函数用途: 保留PID存活且ps正常时，微秒读取丢失的两条安全边界。
@pytest.mark.parametrize("failure", ["unknown", "error"])
def test_native_microsecond_observation_loss_does_not_prove_exit(native_process_probe, monkeypatch, failure):
    probe = native_process_probe
    token = probe.module.capture_process_birth_token(321)
    monkeypatch.setattr(probe.module.os, "kill", lambda pid, sig: None)
    if failure == "unknown":
        probe.birth = None
    else:
        # LLM: 只让出生探针抛观察错误，保留ps成功与内核非僵尸事实。
        # 函数用途: 注入微秒身份查询失败。
        def unavailable(pid):
            raise OSError("kernel birth unavailable")

        monkeypatch.setattr(probe.native, "process_birth", unavailable)
    assert probe.module._same_process(321, token) is False
    assert probe.module._process_instance_terminated(321, token) is False
    with pytest.raises(OSError):
        probe.module._birth_token_matches(321, token, "host:" + probe.output)


# LLM: 没有完整内核出生身份或run已被替换时，ps失败仍是未知，不能伪造权威身份。
# 函数用途: 守住原生回退启用条件和monkeypatch边界。
@pytest.mark.parametrize("unavailable", ["kernel", "patched_run"])
def test_native_birth_oserror_without_authority_stays_unknown(native_process_probe, monkeypatch, unavailable):
    probe = native_process_probe
    probe.failure = PermissionError("ps denied")
    if unavailable == "kernel":
        probe.birth = None
    else:
        monkeypatch.setattr(probe.module, "_DEFAULT_SUBPROCESS_RUN", object())
    assert probe.module.capture_process_birth_token(321) == ""
    assert probe.module._PID_BIRTH_TOKEN_CACHE == {}
    assert len(probe.calls) == 1


# LLM: 覆盖容量外新实例与被淘汰实例重查，不用空缓存数量假定淘汰正确。
# 函数用途: 验证缓存上限和FIFO淘汰。
def test_native_birth_cache_capacity_is_2048(native_process_probe):
    probe = native_process_probe
    for pid in range(1, 2050):
        assert probe.module.capture_process_birth_token(pid).startswith("host:")
    assert len(probe.module._PID_BIRTH_TOKEN_CACHE) == 2048
    assert (1, probe.birth) not in probe.module._PID_BIRTH_TOKEN_CACHE
    assert probe.module.capture_process_birth_token(2049)
    assert len(probe.calls) == 2049
    assert probe.module.capture_process_birth_token(1)
    assert len(probe.calls) == 2050


# LLM: 内核未知不缓存；ps超时/空响应/非零退出仍保留未知，与有权威kernel的OSError回退分开验证。
# 函数用途: 核对未知结果不入birth缓存及恢复后重试。
@pytest.mark.parametrize("failure", ["kernel_unknown", "kernel_error", "timeout", "empty", "exit"])
def test_native_birth_unknown_not_cached_and_recovery_retries(native_process_probe, monkeypatch, failure):
    probe = native_process_probe
    if failure == "kernel_unknown":
        probe.birth = None
    elif failure == "kernel_error":
        # LLM: 故障发生在内核出生查询，不能误写空出生缓存。
        # 函数用途: 注入内核探针异常。
        def unavailable(pid):
            raise OSError("kernel unavailable")
        monkeypatch.setattr(probe.native, "process_birth", unavailable)
    elif failure == "timeout":
        probe.failure = probe.module.subprocess.TimeoutExpired("ps", 1)
    elif failure == "empty":
        probe.output = ""
    else:
        probe.code = 1
    for _ in range(2):
        result = probe.module.capture_process_birth_token(321)
        assert bool(result) is (failure in {"kernel_unknown", "kernel_error"})
    assert len(probe.calls) == 2
    assert not probe.module._PID_BIRTH_TOKEN_CACHE
    probe.birth, probe.failure, probe.output, probe.code = "1792929600:1", None, "recovered", 0
    monkeypatch.setattr(probe.native, "process_birth", lambda pid: probe.birth)
    assert probe.module.capture_process_birth_token(321) == "host:recovered#kb=1792929600:1"
    assert probe.module.capture_process_birth_token(321) == "host:recovered#kb=1792929600:1"
    assert len(probe.calls) == 3


# LLM: 八个真实线程共享事件屏障，观察相同miss的采集次数而非顺序调用假并发。
# 函数用途: 验证并发探针合并一次ps。
@pytest.mark.parametrize("entry,output,expected", [
    ("capture_process_birth_token", "birth", "host:birth#kb=1792929600:1"),
    ("_process_parent_map", "321 1\n", ({321: 1}, True)),
    ("_process_group_map", "321 321\n", ({321: 321}, True)),
])
def test_native_probe_concurrent_misses_share_one_ps(native_process_probe, monkeypatch, entry, output, expected):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    probe = native_process_probe
    probe.output = output
    run = probe.module.subprocess.run
    entered, release = threading.Event(), threading.Event()

    # LLM: 保留ps替身结果，在可控制的采集窗口阻塞并发请求。
    # 函数用途: 打开并发miss竞争窗口。
    def slow_run(argv, **kwargs):
        entered.set()
        assert release.wait(3)
        return run(argv, **kwargs)

    monkeypatch.setattr(probe.module.subprocess, "run", slow_run)
    monkeypatch.setattr(probe.module, "_DEFAULT_SUBPROCESS_RUN", slow_run)
    operation = getattr(probe.module, entry)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(operation, 321) if entry == "capture_process_birth_token" else pool.submit(operation)
                   for _ in range(8)]
        try:
            assert entered.wait(3)
        finally:
            release.set()
        assert [f.result(timeout=3) for f in futures] == [expected] * 8
    assert len(probe.calls) == 1


@pytest.mark.parametrize("entry", ["_process_parent_map", "_process_group_map"])
# LLM: 精确观察249ms命中和250ms过期；调用者改副本不能改共享缓存，False完整性不得升级。
# 函数用途: 验证关系缓存期限、隔离与缺口保留。
def test_native_relation_cache_ttl_copy_and_incomplete(native_process_probe, monkeypatch, entry):
    probe = native_process_probe
    now = [10.0]
    monkeypatch.setattr(probe.module.time, "monotonic", lambda: now[0])
    probe.output = "321 1\nbad row\n"
    operation = getattr(probe.module, entry)
    first, complete = operation()
    assert first == {321: 1} and complete is False
    first[999] = 123
    now[0] = 10.249
    assert operation() == ({321: 1}, False)
    assert len(probe.calls) == 1
    probe.output = "321 2\n"
    now[0] = 10.25
    assert operation() == ({321: 2}, True)
    assert len(probe.calls) == 2


@pytest.mark.parametrize("entry", ["_process_parent_map", "_process_group_map"])
# LLM: 失败观察可在短窗口合并，但到期必须重试且不能伪造完整性。
# 函数用途: 验证失败关系表到期后可恢复。
def test_native_relation_cache_failure_expires(native_process_probe, monkeypatch, entry):
    probe = native_process_probe
    now = [10.0]
    monkeypatch.setattr(probe.module.time, "monotonic", lambda: now[0])
    operation = getattr(probe.module, entry)
    probe.failure = PermissionError("ps denied")
    assert operation() == ({}, False)
    assert operation() == ({}, False)
    assert len(probe.calls) == 1
    probe.failure, probe.output, now[0] = None, "321 2\n", 10.25
    assert operation() == ({321: 2}, True)


# LLM: 即使原生缓存已预热，替换run后仍需看到所有旧ps调用与结果。
# 函数用途: 保持现有monkeypatch观察合同。
def test_monkeypatched_run_bypasses_warm_native_caches(native_process_probe, monkeypatch):
    probe = native_process_probe
    assert probe.module.capture_process_birth_token(321)
    probe.output = "321 1\n"
    assert probe.module._process_parent_map() == ({321: 1}, True)
    assert probe.module._process_group_map() == ({321: 1}, True)
    calls = []

    # LLM: 不同步更改身份哨兵，确保优化门真实识别替身。
    # 函数用途: 记录应绕过缓存的旧探针调用。
    def patched(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="Z" if "stat=" in argv else
                               "321 2\n" if "-axo" in argv else "fresh birth")

    monkeypatch.setattr(probe.module.subprocess, "run", patched)
    assert probe.module.capture_process_birth_token(321) == "host:fresh birth"
    assert probe.module._process_is_zombie(321) is True
    assert probe.module._process_parent_map() == ({321: 2}, True)
    assert probe.module._process_group_map() == ({321: 2}, True)
    assert len(calls) == 4


# LLM: 内核与ps均失败只能得到未知，不能因此发布退出或清理成功。
# 函数用途: 验证观察失败保守返回未终止。
def test_native_zombie_kernel_error_falls_back_and_unknown_does_not_prove_exit(native_process_probe, monkeypatch):
    probe = native_process_probe

    # LLM: 直接抛出原生观察错误，测试必须继续进入ps回退。
    # 函数用途: 注入终止探针故障。
    def unavailable(pid):
        raise OSError("native observation unavailable")

    monkeypatch.setattr(probe.native, "kernel_terminated", unavailable)
    monkeypatch.setattr(probe.module.os, "kill", lambda pid, signum: None)
    # 终止状态和出生身份均未知；有效birth的ps拒绝回退由独立正反例覆盖。
    probe.birth = None
    probe.failure = PermissionError("ps denied")
    assert probe.module._process_instance_terminated(321, "host:original") is False
    assert not probe.module._PID_BIRTH_TOKEN_CACHE
    assert len(probe.calls) == 2


@pytest.mark.parametrize("platform", ["proc", "windows"])
# LLM: Linux和Windows不得调用新原生观察或使用新缓存，替身失败能暴露入口越界。
# 函数用途: 保护原有平台分支。
def test_native_optimizations_do_not_expand_platform_scope(native_process_probe, monkeypatch, platform):
    probe = native_process_probe

    # LLM: 此入口在被测平台必须完全不可达。
    # 函数用途: 将平台分支越界转为明确失败。
    def forbidden(pid):
        pytest.fail("Linux/Windows must not use the new native observer")

    monkeypatch.setattr(probe.native, "process_birth", forbidden)
    monkeypatch.setattr(probe.native, "kernel_terminated", forbidden)
    if platform == "proc":
        old_dir = Path.is_dir
        monkeypatch.setattr(Path, "is_dir", lambda p: True if p == Path("/proc") else old_dir(p))
    else:
        monkeypatch.setattr(probe.module, "_IS_WINDOWS", True)
    assert probe.module._native_posix_probe_enabled() is False
    assert probe.module.capture_process_birth_token(321)
    assert probe.module.capture_process_birth_token(321)
    assert len(probe.calls) == 2
    assert not probe.module._PID_BIRTH_TOKEN_CACHE
    assert probe.module._process_is_zombie(321) is False
    if platform == "windows":
        probe.output = "321 1\n"
        for operation in (probe.module._process_parent_map, probe.module._process_group_map):
            assert operation() == ({321: 1}, True)
            assert operation() == ({321: 1}, True)
        assert not probe.module._PS_RELATION_CACHE
        assert len(probe.calls) == 6


# LLM: 只复用birth文本，内核状态每轮重新观察，转为终止后必须立即可见。
# 函数用途: 防止把身份缓存变成存活缓存。
def test_native_instance_completion_path_reuses_identity_without_caching_liveness(native_process_probe, monkeypatch):
    probe = native_process_probe
    monkeypatch.setattr(probe.module.os, "kill", lambda pid, signum: None)
    token = probe.module.capture_process_birth_token(321)
    for _ in range(10):
        assert probe.module._process_instance_terminated(321, token) is False
    assert len(probe.calls) == 1
    probe.state = True
    assert probe.module._process_instance_terminated(321, token) is True
    assert len(probe.calls) == 1


# LLM: 父表和组表不共享错误的键或内容，相邻读取不得相互覆盖。
# 函数用途: 验证两种关系表独立缓存。
def test_native_relation_parent_group_caches_are_independent(native_process_probe):
    probe = native_process_probe
    probe.output = "321 1\n"
    assert probe.module._process_parent_map() == ({321: 1}, True)
    probe.output = "321 321\n"
    assert probe.module._process_group_map() == ({321: 321}, True)
    assert probe.module._process_parent_map() == ({321: 1}, True)
    assert len(probe.calls) == 2


@pytest.mark.parametrize("entry", ["_process_parent_map", "_process_group_map"])
# LLM: 慢采集完成后才计250ms，否则并发等待者会立即再次fork。
# 函数用途: 验证缓存期限从采集结束起算。
def test_native_relation_cache_window_starts_when_ps_finishes(native_process_probe, monkeypatch, entry):
    probe = native_process_probe
    now = [10.0]
    monkeypatch.setattr(probe.module.time, "monotonic", lambda: now[0])
    run = probe.module.subprocess.run
    probe.output = "321 1\n"

    # LLM: 仅推进受控时钟，不做真实sleep或改变采集结果。
    # 函数用途: 模拟耗时超过缓存窗口的ps。
    def slow_run(argv, **kwargs):
        now[0] += 1.0
        return run(argv, **kwargs)

    monkeypatch.setattr(probe.module.subprocess, "run", slow_run)
    monkeypatch.setattr(probe.module, "_DEFAULT_SUBPROCESS_RUN", slow_run)
    operation = getattr(probe.module, entry)
    assert operation() == ({321: 1}, True)
    assert operation() == ({321: 1}, True)
    assert len(probe.calls) == 1
