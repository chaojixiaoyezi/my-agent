"""托管字节管道的隔离进程验证；不连接模型、Gateway 或真实 TUI。"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling import background_process_host as host
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling.background_process_host import _read_launch_spec
from agent_py_agent.agent.tooling.process_registry import (
    _process_instance_terminated,
    capture_process_birth_token,
    terminate_process_tree,
)
from agent_py_agent.agent.tooling.process_scope import (
    ProcessAccessScope,
    ProcessActivationScope,
    ProcessExecutionScope,
)
from agent_py_agent.agent.tooling.process_session_cleanup import stop_process_session
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests._managed_process_harness import managed_request


# LLM: 仅构造临时目录中的真实托管子进程；字节模式不创建日志，测试退出时必须精确清理自己的 session。
# 函数用途: 为管道测试提供完整请求，不借用用户配置或后台任务。
def stdio_request(tmp_path, code="import time; time.sleep(30)", **changes):
    values = dict(io_mode="stdio", log_path=None, max_log_bytes=0, stop_on_launcher_exit=True)
    values.update(changes)
    return managed_request(tmp_path, code, **values)


# LLM: 只清理传入的精确 session，launcher 不是终止对象；原失败不被宽范围清理遮盖。
# 函数用途: 结束测试创建的 host/child 并关闭返回管道，防止失败样本遗留资源。
def close_hosted(hosted):
    store = ProcessSessionStore(hosted.store_root)
    try:
        cleanup = stop_process_session(store, store.load(hosted.record["session_id"]).record,
                                       host_process=hosted.process)
        assert cleanup.confirmed
        hosted.process.wait(timeout=5)
    finally:
        for stream in (hosted.process.stdin, hosted.process.stdout, hosted.process.stderr):
            if stream is not None:
                stream.close()


@pytest.mark.parametrize("changes", [
    {"io_mode": "tty"}, {"io_mode": []}, {"stop_on_launcher_exit": False},
    {"max_log_bytes": 1}, {"max_log_bytes": False}, {"log_path": Path("unused.log")},
    {"io_mode": "log"},
])
def test_invalid_stdio_request_does_not_create_authority(tmp_path, changes):
    with pytest.raises(ValueError):
        stdio_request(tmp_path, **changes)
    assert not (tmp_path / "authority").exists()


@pytest.mark.parametrize("change", [
    {"schema": "background_process_launch.v3"}, {"io_mode": None}, {"io_mode": "pty"},
    {"stop_on_launcher_exit": False}, {"max_log_bytes": 1},
])
def test_host_refuses_old_or_inconsistent_stdio_spec(tmp_path, change):
    store = ProcessSessionStore(tmp_path)
    spec_path = tmp_path / ".launches" / "bg-test.json"
    spec_path.parent.mkdir()
    spec = {"schema": launch.LAUNCH_SPEC_SCHEMA, "session_id": "bg-test", "command_argv": ["unused"],
            "max_log_bytes": 0, "deadline_monotonic": 0, "stop_on_launcher_exit": True,
            "io_mode": "stdio", **change}
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(ValueError):
        _read_launch_spec(store, "bg-test", spec_path)


def test_stdio_preserves_binary_bytes_and_separate_stderr_under_backpressure(tmp_path):
    code = "import os,sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data); sys.stderr.buffer.write(str(os.getpid()).encode()+b'\\n'+data[::-1])"
    hosted = launch.start_background_process(stdio_request(tmp_path, code))
    data = bytes(range(256)) * 4096
    try:
        output, errors = hosted.process.communicate(data, timeout=8)
        assert output == data
        assert errors == str(hosted.record["child_pid"]).encode() + b"\n" + data[::-1]
        assert hosted.record["pid"] == hosted.process.pid != hosted.record["child_pid"]
        current = ProcessSessionStore(hosted.store_root).load(hosted.record["session_id"]).record
        assert current["handoff_confirmed"] and current["status"] == "exited"
        assert current["exit_code"] == 0 and current["termination"]["confirmed"]
        assert current["output_file"] == ""
        assert not (tmp_path / "output.log").exists()
    finally:
        close_hosted(hosted)


def test_stdio_natural_failure_preserves_child_exit_code(tmp_path):
    code = "import sys; print('protocol'); print('diagnostic',file=sys.stderr); raise SystemExit(7)"
    hosted = launch.start_background_process(stdio_request(tmp_path, code))
    try:
        output, errors = hosted.process.communicate(timeout=5)
        assert (output, errors) == (b"protocol\n", b"diagnostic\n")
        current = ProcessSessionStore(hosted.store_root).load(hosted.record["session_id"]).record
        assert hosted.process.returncode == current["exit_code"] == 7
        assert current["status"] == "exited"
    finally:
        close_hosted(hosted)


def test_host_releases_its_output_endpoints_before_child_lifecycle_finishes(tmp_path):
    hosted = launch.start_background_process(stdio_request(
        tmp_path, "import os,time; os.close(1); os.close(2); time.sleep(30)"))
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        reads = [pool.submit(stream.read) for stream in (hosted.process.stdout, hosted.process.stderr)]
        assert [future.result(timeout=2) for future in reads] == [b"", b""]
        assert not _process_instance_terminated(hosted.record["child_pid"], hosted.record["child_pid_birth_token"])
        assert ProcessSessionStore(hosted.store_root).load(hosted.record["session_id"]).record["status"] == "running"
    finally:
        close_hosted(hosted)
        pool.shutdown(wait=True)


def test_failed_handoff_closes_parent_pipes_and_cleans_exact_child(tmp_path, monkeypatch):
    actual_popen = subprocess.Popen
    created, checks = [], []

    def capture(args, *pos, **kwargs):
        process = actual_popen(args, *pos, **kwargs)
        if "agent_py_agent.agent.tooling.background_process_host" in args:
            created.append(process)
        return process

    def authority():
        checks.append(1)
        if len(checks) == 3:
            raise RuntimeError("revoked before handoff")

    monkeypatch.setattr(launch.subprocess, "Popen", capture)
    with pytest.raises(launch.BackgroundLaunchError) as caught:
        launch.start_background_process(stdio_request(tmp_path, authority_check=authority))
    assert caught.value.cleanup_confirmed and not caught.value.record["handoff_confirmed"]
    assert len(created) == 1
    assert all(stream.closed for stream in (created[0].stdin, created[0].stdout, created[0].stderr))
    assert _process_instance_terminated(caught.value.record["child_pid"], caught.value.record["child_pid_birth_token"])


def test_child_is_owned_before_log_close_failure(tmp_path, monkeypatch):
    request = managed_request(tmp_path)
    store = ProcessSessionStore(request.store_root)
    store.write(launch._reservation(request, "bg-close-failure"))
    spec_path = store.root / ".launches" / "bg-close-failure.json"
    spec_path.parent.mkdir()
    spec_path.write_text(json.dumps({"schema": launch.LAUNCH_SPEC_SCHEMA, "session_id": "bg-close-failure",
        "command_argv": request.argv, "max_log_bytes": request.max_log_bytes, "deadline_monotonic": 0,
        "stop_on_launcher_exit": False, "io_mode": "log"}))
    original_open, original_popen = Path.open, subprocess.Popen
    spawned = []

    class FailingClose:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self.stream

        def __exit__(self, *_args):
            self.stream.close()
            raise OSError("log close failed after spawn")

    def open_log(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return FailingClose(stream) if path == request.log_path else stream

    def popen(args, *pos, **kwargs):
        process = original_popen(args, *pos, **kwargs)
        if args == request.argv:
            spawned.append((process, capture_process_birth_token(process.pid)))
        return process

    monkeypatch.setattr(Path, "open", open_log)
    monkeypatch.setattr(host.subprocess, "Popen", popen)
    try:
        assert host.run_background_process_host(store, "bg-close-failure", spec_path) == 1
        assert len(spawned) == 1
        process, birth = spawned[0]
        assert _process_instance_terminated(process.pid, birth)
        current = store.load("bg-close-failure").record
        assert current["termination"]["confirmed"] and current["error_type"] == "OSError"
        assert current["status"] == "unknown" and current["child_launch_started"]
    finally:
        for process, birth in spawned:
            terminate_process_tree(process.pid, process, expected_birth_token=birth, grace_seconds=0.1)


def test_exact_activation_stop_preserves_another_activation_and_business_task(tmp_path):
    base = managed_request(tmp_path)
    scope = ProcessActivationScope("owner-test", str(tmp_path), "workspace-peek", "a" * 64)
    shared = replace(stdio_request(tmp_path), activation_scope=scope,
                     access_scope=ProcessAccessScope(scope.owner_id, "", scope.owner_home),
                     execution_scope=ProcessExecutionScope(owner_home=scope.owner_home))
    hosted = []
    try:
        first = launch.start_background_process(shared)
        hosted.append(first)
        second = launch.start_background_process(replace(shared, activation_scope=replace(scope, activation_id="b" * 64)))
        hosted.append(second)
        business = launch.start_background_process(base)
        hosted.append(business)
        store = ProcessSessionStore(base.store_root)
        selected = store.request_stop_activation(scope)
        assert [row["session_id"] for row in selected.records] == [first.record["session_id"]]
        cleanup = stop_process_session(store, selected.records[0], host_process=first.process)
        assert cleanup.confirmed
        for other in (second, business):
            current = store.load(other.record["session_id"]).record
            assert current["status"] == "running" and not current["stop_requested"]
            assert not _process_instance_terminated(current["child_pid"], current["child_pid_birth_token"])
        task_stop = store.request_stop(base.execution_scope)
        assert [row["session_id"] for row in task_stop.records] == [business.record["session_id"]]
        assert not store.load(second.record["session_id"]).record["stop_requested"]
    finally:
        for item in hosted:
            close_hosted(item)


def test_stdio_host_recovers_launcher_loss_after_handoff(tmp_path):
    code = "\n".join([
        "import os", "from pathlib import Path",
        "from agent_py_agent.agent.tooling.background_process_launch import start_background_process",
        "from agent_py_agent.tests._managed_process_harness import managed_request",
        f"start_background_process(managed_request(Path({str(tmp_path)!r}), io_mode='stdio', log_path=None, max_log_bytes=0, stop_on_launcher_exit=True))",
        "os._exit(28)",
    ])
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[2], timeout=8)
    assert result.returncode == 28
    store = ProcessSessionStore(tmp_path / "authority")
    rows, errors = store.list_records()
    assert not errors and len(rows) == 1
    current = rows[0]
    try:
        deadline = time.monotonic() + 5
        while current["status"] != "killed" and time.monotonic() < deadline:
            time.sleep(0.05)
            current = store.load(current["session_id"]).record
        assert current["handoff_confirmed"] and current["status"] == "killed"
        assert current["reason"] == "launcher_unavailable" and current["termination"]["confirmed"]
    finally:
        assert stop_process_session(store, current).confirmed
