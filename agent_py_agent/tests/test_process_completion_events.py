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
    ProcessSessionStore,
    ProcessSessionTransaction,
    process_session_store_root,
)


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
    probe = legacy_birth_probe
    _, _, authority = _fixture(tmp_path)
    payload = authority.load("bg-test-notice").record
    payload.update(pid=probe.pid, pid_birth_token=probe.legacy, status="running", exit_code=None, finished_at=None)
    registry = probe.module.ProcessRegistry()
    cached = registry._cache_payload(payload, authority.root)
    cached.pid_birth_token = cached_token
    handle = cached.process = object()
    refreshed = registry._cache_payload(payload, authority.root)
    assert refreshed.process is (handle if same_instance else None)
    assert refreshed.pid_birth_token == payload["pid_birth_token"] == probe.legacy


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
