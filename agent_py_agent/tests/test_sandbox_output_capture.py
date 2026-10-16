"""E13：真实同步入口的大输出、独立流限额与回收；只替换平台隔离包装，不假冒真实 OS 沙箱。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tracemalloc
from contextlib import nullcontext

import pytest

from agent_py_agent.agent.attempt import process_run
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.tests.test_pack_verification_cancellation import _plain_sandbox


def _writer(fd, count=200 * 1024 * 1024):
    return f"import os\nchunk=b'x'*65536\nfor i in range({count // 65536}): os.write({fd},chunk)\n"


@pytest.mark.parametrize("stream,fd", [("stdout", 1), ("stderr", 2)])
def test_200m_output_is_bounded_and_fully_drained(tmp_path, monkeypatch, stream, fd):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    tracemalloc.start()
    try:
        # 此处验证空间上界和200MiB确实排空，不把并行车道的调度耗时当性能验收。
        result = sandbox.run([sys.executable, "-c", _writer(fd)], timeout=60, grace_seconds=0.01)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(json.dumps({"stream": stream, "input_bytes": 200 * 1024 * 1024, "peak_bytes": peak}))
    assert peak < 32 * 1024 * 1024, f"无上限捕获峰值：{peak} bytes"
    assert result.returncode == 0, "超过保留额度仍须排空，不能堵满管道后超时"
    assert len(getattr(result, stream).encode()) == 4 * 1024 * 1024
    other = "stderr" if stream == "stdout" else "stdout"
    assert getattr(result, other) == ""
    facts = result.capture
    assert facts["bytes_seen"] == {stream: 200 * 1024 * 1024, other: 0}
    assert facts["bytes_discarded"] == {stream: 196 * 1024 * 1024, other: 0}
    assert facts["streams_truncated"] == {stream: True, other: False}
    assert facts["truncated"] and not facts["complete"] and facts["errors"] == {}


def test_two_streams_have_separate_caps(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    script = "import os\nfor i in range(80):\n os.write(1,b'a'*65536); os.write(2,b'b'*65536)"
    result = sandbox.run([sys.executable, "-c", script], timeout=5)
    assert result.returncode == 0
    assert result.stdout == "a" * (4 * 1024 * 1024) and result.stderr == "b" * (4 * 1024 * 1024)
    assert result.capture["bytes_discarded"] == {"stdout": 1024 * 1024, "stderr": 1024 * 1024}


@pytest.mark.parametrize("stream_case", [("stdout", 1), ("stderr", 2)])
@pytest.mark.parametrize("encoding_case", [(b"w\xff1", True), (b"\xe4\xb8", True),
                                        ("中文".encode(), False), ("\ufffd".encode(), False)])
def test_retained_utf8_validity_is_reported_without_changing_shell_text(tmp_path, monkeypatch, stream_case, encoding_case):
    stream, fd = stream_case
    data, invalid = encoding_case
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    result = sandbox.run([sys.executable, "-c", f"import os; os.write({fd},{data!r})"], timeout=5)
    other = "stderr" if stream == "stdout" else "stdout"
    assert result.returncode == 0
    assert getattr(result, stream) == data.decode("utf-8", errors="replace")
    assert result.capture.get("streams_invalid_utf8") == {stream: invalid, other: False}
    assert result.capture["bytes_seen"] == {stream: len(data), other: 0}
    assert result.capture["complete"] and result.capture["errors"] == {}


def test_small_output_preserves_text_completed_process_contract(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    script = "import os; os.write(1,b'one\\r\\ntwo\\r'); os.write(2,'中文'.encode()); raise SystemExit(7)"
    result = sandbox.run([sys.executable, "-c", script], timeout=3)
    assert isinstance(result, subprocess.CompletedProcess) and result.returncode == 7
    assert result.stdout == "one\ntwo\n" and result.stderr == "中文"
    assert result.capture["complete"] and not result.capture["truncated"]
    assert result.capture["bytes_discarded"] == {"stdout": 0, "stderr": 0}


@pytest.mark.parametrize("cancel", [False, True])
def test_loud_timeout_or_cancel_closes_readers_and_reaps(tmp_path, monkeypatch, cancel):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    token, children = CancellationToken(), []
    original = process_run.subprocess.Popen

    def spawn(*args, **kwargs):
        proc = original(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(process_run.subprocess, "Popen", spawn)
    before = set(threading.enumerate())
    ready = tmp_path / "ready"
    script = f"import pathlib\npathlib.Path({str(ready)!r}).touch()\n" + _writer(1).replace("range(3200)", "iter(int,1)")

    def stop_after_output():
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        token.cancel()

    stopper = threading.Thread(target=stop_after_output) if cancel else None
    if stopper:
        stopper.start()
    expectation = pytest.raises(ToolCancelled) if cancel else nullcontext()
    try:
        with bind_cancellation_token(token), expectation:
            result = sandbox.run([sys.executable, "-c", script], timeout=3 if cancel else 0.3, grace_seconds=0.01)
        if not cancel:
            assert result.returncode == 143 and "TERM->grace" in result.stderr
            assert len(result.stdout.encode()) <= 4 * 1024 * 1024
    finally:
        if stopper:
            stopper.join(5)
    assert children and all(proc.poll() is not None for proc in children)
    assert all(pipe.closed for proc in children for pipe in (proc.stdout, proc.stderr))
    assert token._callbacks == {} and set(threading.enumerate()) <= before


def test_wait_exception_reclaims_process_and_pipes(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    children, original = [], process_run.subprocess.Popen

    def spawn(*args, **kwargs):
        proc = original(*args, **kwargs)
        children.append(proc)
        return proc

    def fail_wait(*args):
        raise ValueError("injected wait failure")

    monkeypatch.setattr(process_run.subprocess, "Popen", spawn)
    monkeypatch.setattr(process_run, "_wait_for_process", fail_wait)
    try:
        with pytest.raises(ValueError, match="injected"):
            sandbox.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=2, grace_seconds=0.01)
        assert children[0].poll() is not None, "异常路径不能留下自己启动的子进程"
        assert children[0].stdout.closed and children[0].stderr.closed
    finally:
        if children and children[0].poll() is None:
            children[0].kill()
            children[0].wait(timeout=3)


def test_capture_output_false_preserves_none_and_real_exit_code(tmp_path, monkeypatch):
    sandbox = _plain_sandbox(tmp_path, monkeypatch)
    result = sandbox.run([sys.executable, '-c', 'raise SystemExit(9)'], timeout=3, capture_output=False)
    assert result.returncode == 9
    assert result.stdout is None and result.stderr is None and result.capture == {}


def test_reader_failure_exposes_stream_error_and_missing_eof(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling import process_output_capture as capture_module

    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    stdout, stderr = os.fdopen(out_r, 'rb'), os.fdopen(err_r, 'rb')
    os.close(out_w)
    os.close(err_w)
    original = capture_module.os.read

    def read(fd, count):
        if fd == out_r:
            raise OSError('synthetic output read failure')
        return original(fd, count)

    monkeypatch.setattr(capture_module.os, 'read', read)
    capture = capture_module.ProcessOutputCapture(SimpleNamespace(stdout=stdout, stderr=stderr))
    capture.finish(1)
    out, err, facts = capture.result()

    assert out == '' and err == ''
    assert facts['errors'] == {'stdout': 'OSError'}
    assert facts['streams_eof'] == {'stdout': False, 'stderr': True}
    assert not facts['complete'] and not facts['truncated']
    assert stdout.closed and stderr.closed


@pytest.mark.skipif(os.name != 'posix', reason='POSIX pipe descriptor boundary')
def test_pipe_descriptor_above_select_limit_is_fully_read():
    import fcntl
    import resource
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.process_output_capture import ProcessOutputCapture

    if resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 1100:
        pytest.skip('host descriptor soft limit does not permit high-fd fixture')
    original, writer = os.pipe()
    try:
        descriptor = fcntl.fcntl(original, fcntl.F_DUPFD, 1100)
    finally:
        os.close(original)
    stdout = os.fdopen(descriptor, 'rb')
    try:
        capture = ProcessOutputCapture(SimpleNamespace(stdout=stdout, stderr=None))
        # 先让reader观察空管道，再写入，覆盖可读等待而非仅有同步read的路径。
        time.sleep(0.02)
        os.write(writer, b'high-fd')
    finally:
        os.close(writer)
    assert capture.finish(1)
    out, err, facts = capture.result()
    assert out == 'high-fd' and err == '' and facts['complete'] and facts['errors'] == {}
