"""死W的原P6收尾；控制端点组件与真实waitid子进程分开，不调用模型/网络服务。"""

import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import daemon_metadata
from agent_py_agent.agent.gateway_parts import multiprocess_drain_process as exits
from agent_py_agent.agent.gateway_parts import multiprocess_drain_runtime as drain
from agent_py_agent.agent.gateway_parts import multiprocess_generation_metadata as history
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_sources import require_role_coverage
from agent_py_agent.cli.gateway_worker_runtime import worker_process_identity
from agent_py_agent.tests.test_gateway_worker_runtime import context, physical

_FIXTURES = (context, physical)


# LLM: 原C/Controller/P4/issuer与OriginalWorkerExitObservation不替换，只有原既有Popen/native物理fixture受控。
# 函数用途: 模拟W在发表终止心跳前非零退出，原observer消费受控waitid；不签真实OS完整角色证明。
def _dead_runtime(workers, *, code=3, ready=True):
    workers.start()
    runtime = drain.GatewayDrainRuntime(workers.control)
    runtime.retain_workers(workers)
    root = workers.control.paths.root
    for index, (lane, process, _observer) in enumerate(workers._created_origins):
        lane.executor_lane = SimpleNamespace(
            session=SimpleNamespace(_execution_domains=(), _hosts={})
        )
        process.exit_status = code if index == 0 else 0
    workers.close(defer_native=True)
    registry = history.deserialize_record((root / "workers/registry.json").read_bytes())
    write_json_file_atomic(
        root / "gateway_state.json",
        {"status": "stopped", "pid": os.getpid(), "c_run_id": workers.control.c_run_id},
    )
    for row in registry["workers"]:
        failed = row["worker"] == 0
        value = {
            "schema_version": "multiprocess_worker_heartbeat.v1",
            "worker": row["worker"],
            "of": registry["worker_count"],
            "pid": row["process_identity"]["pid"],
            "process_identity": row["process_identity"],
            "c_run_id": registry["c_run_id"],
            "instance_id": row["instance_id"],
            "ready": ready if failed else False,
            "loop_health": "healthy" if failed else "stopped",
            "inflight": 0,
            "updated_at": 1.0,
            "readiness": {"ready": ready if failed else False},
        }
        write_json_file_atomic(root / "workers" / f"w{row['worker']}.json", value)
    return runtime


@pytest.mark.parametrize("code", [1, 3])
@pytest.mark.parametrize("ready", [False, True])
def test_original_failed_worker_seals_full_generation_without_rewriting_heartbeat(
    physical, monkeypatch, code, ready
):
    runtime = _dead_runtime(physical, code=code, ready=ready)
    root = runtime.control.paths.root
    marker = root / "workers/w0.json"
    before = marker.read_bytes()
    runtime.stop_producers()
    reference = runtime.seal_generation()
    record, _raw = history.read_generation_metadata(root, runtime.scope.c_run_id)
    assert reference and record["closure"] is not None
    assert marker.read_bytes() == before
    assert (
        record["closure"]["role_markers"]["workers/w0.json"] == hashlib.sha256(before).hexdigest()
    )
    assert json.loads(before)["loop_health"] == "healthy"
    monkeypatch.setattr(daemon_metadata, "process_identity_is_live", lambda _identity: False)
    captured = require_role_coverage(runtime.control.paths, "next-c", count=runtime.scope.count)
    assert captured and captured[0][0] == runtime.scope.c_run_id


@pytest.mark.parametrize(
    "fault",
    [
        "normal_zero_stale",
        "cached_returncode",
        "cached_bool_returncode",
        "wrong_instance",
        "wrong_p4",
        "unknown_resource",
        "observer_copy",
        "unconfirmed",
    ],
)
def test_dead_worker_never_hides_unknown_or_changed_original_sources(physical, fault):
    runtime = _dead_runtime(
        physical,
        code=0 if fault == "normal_zero_stale" else (1 if fault == "cached_bool_returncode" else 3),
    )
    root = runtime.control.paths.root
    original = physical._created_origins
    observer = original[0][2]
    old_exit = observer._exit
    if fault == "cached_returncode":
        physical.processes[0].returncode = 0
    elif fault == "cached_bool_returncode":
        physical.processes[0].returncode = True
        assert observer._returncode() == 1 and observer.confirmed
    elif fault == "wrong_instance":
        path = root / "workers/w0.json"
        value = json.loads(path.read_text())
        value["instance_id"] = "other-instance"
        write_json_file_atomic(path, value)
    elif fault == "wrong_p4":
        path = root / "workers/registry.json"
        value = json.loads(path.read_text())
        value["workers"][0]["instance_id"] = "other-instance"
        write_json_file_atomic(path, value)
    elif fault == "unknown_resource":
        runtime.prepare_pool("foreground", "unknown-external").retain_fault(
            RuntimeError("synthetic unknown")
        )
    elif fault == "observer_copy":
        physical._created_origins = [
            (original[0][0], original[0][1], copy.copy(observer)),
            *original[1:],
        ]
    elif fault == "unconfirmed":
        observer._exit = None
    runtime.stop_producers()
    before = history.read_generation_metadata(root, runtime.scope.c_run_id)
    try:
        with pytest.raises((ValueError, RuntimeError)):
            runtime.seal_generation()
        assert history.read_generation_metadata(root, runtime.scope.c_run_id) == before
        assert before[0]["closure"] is None
    finally:
        physical._created_origins = original
        observer._exit = old_exit


@pytest.mark.parametrize("outcome", [0, 3, "SIGKILL"])
def test_abnormal_classification_consumes_real_child_waitstatus(tmp_path, outcome):
    if not exits._waitstatus_supported():
        # 原平台不支持准确waitid时只验拒绝，不能把本分支当真实内核验证。
        assert not drain._original_abnormal_worker_exit(SimpleNamespace(confirmed=True))
        return
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", "import sys; raise SystemExit(int(sys.stdin.readline()))"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    observer = exits.OriginalWorkerExitObservation(
        child, worker_process_identity(child.pid)
    ).install()
    try:
        assert not observer.confirmed
        if outcome == "SIGKILL":
            child.send_signal(signal.SIGKILL)
            child.stdin.close()
        else:
            child.stdin.write((str(outcome) + "\n").encode())
            child.stdin.flush()
            child.stdin.close()
        assert observer.wait_until(time.monotonic() + 3)
        assert observer.confirmed
        assert drain._original_abnormal_worker_exit(observer) is (outcome != 0)
        assert child.returncode == (-signal.SIGKILL if outcome == "SIGKILL" else outcome)
        with pytest.raises(ValueError):
            drain._original_abnormal_worker_exit(copy.copy(observer))
        assert not drain._original_abnormal_worker_exit(
            SimpleNamespace(confirmed=True, returncode=-9)
        )
    finally:
        if not observer.confirmed:
            try:
                os.kill(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(child.pid, 0)
            except ChildProcessError:
                pass
