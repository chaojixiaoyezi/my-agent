from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.multiprocess_native_host_client import NativeHostProcess
from agent_py_agent.agent.tooling import process_registry, process_session_cleanup
from agent_py_agent.agent.tooling.process_cleanup_evidence import (
    confirm_cleanup_reference,
    process_cleanup_reference,
    validate_cleanup_reference,
)
from agent_py_agent.agent.tooling.process_registry import (
    ProcessRegistry,
    ProcessTerminationReceipt,
    capture_process_birth_token,
    terminate_process_tree,
)
from agent_py_agent.agent.tooling.process_session_cleanup import stop_process_session
from agent_py_agent.agent.tooling.process_session_records import (
    PROCESS_SESSION_SCHEMA,
    PROCESS_TERMINAL_STATUSES,
    validate_process_record,
)
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore


def _base_record(
    store_root: Path,
    *,
    session_id: str = "bg-w6-test",
    status: str = "running",
    pid: int = 9999901,
    pid_birth_token: str = "dead-host-token",
    child_pid: int = 9999902,
    child_pid_birth_token: str = "dead-child-token",
    termination: dict[str, object] | None = None,
) -> dict[str, object]:
    finished_at = 2.0 if status in PROCESS_TERMINAL_STATUSES else None
    record: dict[str, object] = {
        "schema": PROCESS_SESSION_SCHEMA,
        "session_id": session_id,
        "revision": 0,
        "command": "python3 -c sleep",
        "cwd": str(store_root),
        "reserved_at": 1.0,
        "started_at": 1.0,
        "launcher_pid": os.getpid(),
        "launcher_birth_token": "launcher-test-token",
        "pid": pid,
        "pid_birth_token": pid_birth_token,
        "child_pid": child_pid,
        "child_pid_birth_token": child_pid_birth_token,
        "stop_requested": True,
        "handoff_confirmed": True,
        "child_launch_started": bool(child_pid),
        "status": status,
        "finished_at": finished_at,
        "exit_code": None,
        "access_scope": {
            "owner_id": "owner-w6",
            "conversation_id": "conv-w6",
            "owner_home": str(store_root),
        },
        "execution_scope": {
            "owner_home": str(store_root),
            "thread_id": "conv-w6",
            "root_task_id": "task-w6",
            "run_id": "run-w6",
            "attempt_id": "attempt-w6",
        },
        "completion_target": {},
        "completion_notice_id": "",
        "activation_scope": None,
        "retain_until_consumed": False,
        "output_file": "",
        "host_state_file": "",
    }
    if termination is not None:
        record["termination"] = termination
    return record


def _spawn_sleeper(tmp_path: Path | None = None, *, new_session: bool = True) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=new_session,
        text=True,
    )
    birth = ""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not birth:
        birth = capture_process_birth_token(proc.pid)
        if not birth:
            time.sleep(0.02)
    assert birth, f"无法采集测试子进程 {proc.pid} 的出生标识"
    return proc, birth


def _cleanup_proc(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX 进程出生标识与会话级子树测试")
def test_stop_retry_after_leader_exit_tracks_reparented_descendant_by_birth_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    host, host_birth = _spawn_sleeper()
    child, child_birth = _spawn_sleeper()
    descendant, desc_birth = _spawn_sleeper()
    try:
        saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-reparent",
                status="running",
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
            )
        )

        # 第一次 stop：host 和 child 被终止，但后代 descendant 仍存活并被持久化到 unresolved_instances。
        real_terminate_tree = process_session_cleanup.terminate_process_tree

        def attempt1_terminate(pid: int, proc: subprocess.Popen | None, **kwargs: object) -> ProcessTerminationReceipt:
            if pid == host.pid:
                return ProcessTerminationReceipt(
                    "SIGTERM", True, 0, 1, (), tree_observation_complete=True,
                )
            if pid == child.pid:
                _cleanup_proc(child)
                return ProcessTerminationReceipt(
                    "SIGTERM->SIGKILL",
                    False,
                    None,
                    2,
                    (descendant.pid,),
                    tree_observation_complete=True,
                    unresolved_instances=({"pid": descendant.pid, "birth_token": desc_birth},),
                )
            return real_terminate_tree(pid, proc, **kwargs)

        _cleanup_proc(host)
        monkeypatch.setattr(process_session_cleanup, "terminate_process_tree", attempt1_terminate)
        first = stop_process_session(store, saved)

        assert first.confirmed is False
        assert first.record["status"] == "unknown"
        cleanup1 = first.record["termination"]["cleanup"]
        assert cleanup1["confirmed"] is False
        assert cleanup1["tree_observation_complete"] is True
        assert cleanup1["unresolved_instances"] == [{"pid": descendant.pid, "birth_token": desc_birth}]
        assert descendant.poll() is None

        # 第二次 stop：host/child 已死且后代依然抵抗退出，重试绝不能仅凭祖先退出虚报 killed/confirmed。
        targeted_in_attempt2: list[tuple[int, str | None]] = []

        def attempt2_resists(pid: int, proc: subprocess.Popen | None, **kwargs: object) -> ProcessTerminationReceipt:
            targeted_in_attempt2.append((pid, kwargs.get("expected_birth_token")))  # type: ignore[arg-type]
            return ProcessTerminationReceipt(
                "SIGTERM->SIGKILL",
                False,
                None,
                1,
                (pid,),
                tree_observation_complete=True,
                unresolved_instances=({"pid": pid, "birth_token": desc_birth},),
            )

        monkeypatch.setattr(process_session_cleanup, "terminate_process_tree", attempt2_resists)
        second = stop_process_session(store, first.record)

        assert (descendant.pid, desc_birth) in targeted_in_attempt2
        assert second.confirmed is False
        assert second.record["status"] == "unknown"
        cleanup2 = second.record["termination"]["cleanup"]
        assert cleanup2["confirmed"] is False
        assert cleanup2["tree_observation_complete"] is True
        assert cleanup2["unresolved_instances"] == [{"pid": descendant.pid, "birth_token": desc_birth}]
        assert descendant.poll() is None

        # 第三次 stop：恢复真实 terminate_process_tree，按 (pid, birth_token) 定向清理遗留后代并结案。
        targeted_in_attempt3: list[tuple[int, str | None]] = []

        def spy_real_terminate(pid: int, proc: subprocess.Popen | None, **kwargs: object) -> ProcessTerminationReceipt:
            targeted_in_attempt3.append((pid, kwargs.get("expected_birth_token")))  # type: ignore[arg-type]
            return real_terminate_tree(pid, proc, **kwargs)

        monkeypatch.setattr(process_session_cleanup, "terminate_process_tree", spy_real_terminate)
        third = stop_process_session(store, second.record)

        assert targeted_in_attempt3 == [(descendant.pid, desc_birth)]
        assert third.confirmed is True
        assert third.record["status"] == "killed"
        cleanup3 = third.record["termination"]["cleanup"]
        assert cleanup3["confirmed"] is True
        assert cleanup3["tree_observation_complete"] is True
        assert cleanup3["unresolved_instances"] == []
        assert descendant.wait(timeout=2) is not None

        reference = process_cleanup_reference(third.record)
        validate_cleanup_reference(reference)
        confirm_cleanup_reference(reference, third.record)
        with store.transaction() as tx:
            tx.consume_cleanup((reference,))
        assert not store.record_path("bg-w6-reparent").exists()
    finally:
        _cleanup_proc(host)
        _cleanup_proc(child)
        _cleanup_proc(descendant)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 出生标识测试")
def test_pid_reuse_after_descendant_exit_discharges_without_signalling_unrelated_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    unrelated, unrelated_birth = _spawn_sleeper()
    try:
        stale_birth = f"{unrelated_birth}-prior-deceased-instance"
        prior_cleanup = {
            "confirmed": False,
            "instances": [
                {
                    "method": "SIGTERM->SIGKILL",
                    "confirmed": False,
                    "return_code": None,
                    "observed_processes": 2,
                    "unresolved_pids": [unrelated.pid],
                    "tree_observation_complete": True,
                    "unresolved_instances": [{"pid": unrelated.pid, "birth_token": stale_birth}],
                }
            ],
            "tree_observation_complete": True,
            "unresolved_instances": [{"pid": unrelated.pid, "birth_token": stale_birth}],
        }
        saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-pid-reuse",
                status="unknown",
                termination={"cleanup": prior_cleanup},
            )
        )

        called: list[int] = []
        monkeypatch.setattr(
            process_session_cleanup,
            "terminate_process_tree",
            lambda pid, *args, **kwargs: called.append(pid) or ProcessTerminationReceipt("unexpected", False, None, 0),
        )

        settled = stop_process_session(store, saved)
        assert called == [], "出生标识已变更的复用 PID 绝不能再被 terminate_process_tree 信号触碰"
        assert unrelated.poll() is None, "复用同号 PID 的无关进程必须继续存活"
        assert settled.confirmed is True
        assert settled.record["status"] == "killed"
        cleanup = settled.record["termination"]["cleanup"]
        assert cleanup["confirmed"] is True
        assert cleanup["tree_observation_complete"] is True
        assert cleanup["unresolved_instances"] == []
    finally:
        _cleanup_proc(unrelated)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 进程树快照行为测试")
def test_incomplete_tree_observation_before_leader_exit_stays_sticky_unknown_across_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    host, host_birth = _spawn_sleeper()
    child, child_birth = _spawn_sleeper()
    try:
        saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-incomplete",
                status="running",
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
            )
        )

        # 通过模拟快照接口返回 complete=False，验证真实 terminate_process_tree 会写入 tree_observation_complete=False。
        monkeypatch.setattr(
            process_registry,
            "_fresh_process_tree_snapshot",
            lambda root_pid: ({root_pid: host_birth if root_pid == host.pid else child_birth}, False),
        )
        first = stop_process_session(store, saved, host_process=host)
        assert first.confirmed is False
        assert first.record["status"] == "unknown"
        assert first.record["termination"]["cleanup"]["tree_observation_complete"] is False

        # 确保两位组长均已退出，且未留下 unresolved_instances；后续连续两次重试均不能虚报 confirmed=True。
        _cleanup_proc(host)
        _cleanup_proc(child)
        monkeypatch.undo()

        second = stop_process_session(store, first.record)
        assert second.confirmed is False
        assert second.record["status"] == "unknown"
        assert second.record["termination"]["cleanup"]["confirmed"] is False
        assert second.record["termination"]["cleanup"]["tree_observation_complete"] is False

        third = stop_process_session(store, second.record)
        assert third.confirmed is False
        assert third.record["status"] == "unknown"
        assert third.record["termination"]["cleanup"]["confirmed"] is False
        assert third.record["termination"]["cleanup"]["tree_observation_complete"] is False
    finally:
        _cleanup_proc(host)
        _cleanup_proc(child)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 进程树快照行为测试")
def test_incomplete_observation_recovers_only_while_leaders_remain_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    host, host_birth = _spawn_sleeper()
    child, child_birth = _spawn_sleeper()
    try:
        saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-live-recover",
                status="running",
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
            )
        )

        # 第一次停止因快照 incomplete 失败，但组长仍在；第二次真实完整快照+清理可以合法结案。
        monkeypatch.setattr(
            process_session_cleanup,
            "terminate_process_tree",
            lambda pid, proc, **kwargs: ProcessTerminationReceipt(
                "SIGTERM", False, None, 1, (), tree_observation_complete=False,
            ),
        )
        first = stop_process_session(store, saved, host_process=host)
        assert first.confirmed is False
        assert first.record["termination"]["cleanup"]["tree_observation_complete"] is False

        monkeypatch.undo()
        second = stop_process_session(store, first.record, host_process=host)
        assert second.confirmed is True
        assert second.record["status"] == "killed"
        assert second.record["termination"]["cleanup"]["tree_observation_complete"] is True
        assert second.record["termination"]["cleanup"]["unresolved_instances"] == []
    finally:
        _cleanup_proc(host)
        _cleanup_proc(child)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 出生标识与信号行为测试")
def test_legacy_bare_unresolved_pids_stay_unknown_and_are_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    unrelated, _ = _spawn_sleeper()
    try:
        legacy_cleanup = {
            "confirmed": False,
            "instances": [
                {
                    "method": "SIGTERM->SIGKILL",
                    "confirmed": False,
                    "return_code": None,
                    "observed_processes": 3,
                    "unresolved_pids": [unrelated.pid],
                }
            ],
        }
        saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-legacy-bare-pid",
                status="unknown",
                termination={"cleanup": legacy_cleanup},
            )
        )

        called: list[int] = []
        monkeypatch.setattr(
            process_session_cleanup,
            "terminate_process_tree",
            lambda pid, *args, **kwargs: called.append(pid) or ProcessTerminationReceipt("unexpected", False, None, 0),
        )

        second = stop_process_session(store, saved)
        assert called == []
        assert unrelated.poll() is None
        assert second.confirmed is False
        assert second.record["status"] == "unknown"
        assert second.record["termination"]["cleanup"]["confirmed"] is False
        assert any(unrelated.pid in item["unresolved_pids"] for item in second.record["termination"]["cleanup"]["instances"])

        third = stop_process_session(store, second.record)
        assert called == []
        assert unrelated.poll() is None
        assert third.confirmed is False
        assert third.record["status"] == "unknown"
    finally:
        _cleanup_proc(unrelated)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 宿主退出后未决后代传递测试")
def test_host_top_level_unconfirmed_termination_without_cleanup_propagates_to_stop_retry(
    tmp_path: Path,
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    descendant, desc_birth = _spawn_sleeper()
    try:
        host_receipt = ProcessTerminationReceipt(
            "SIGTERM->SIGKILL",
            False,
            0,
            2,
            (descendant.pid,),
            tree_observation_complete=True,
            unresolved_instances=({"pid": descendant.pid, "birth_token": desc_birth},),
        )
        saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-host-exit-handoff",
                status="unknown",
                termination=asdict(host_receipt),
            )
        )

        result = stop_process_session(store, saved)
        assert result.confirmed is True
        assert result.record["status"] == "killed"
        assert result.record["termination"]["cleanup"]["confirmed"] is True
        assert result.record["termination"]["cleanup"]["unresolved_instances"] == []
        assert descendant.wait(timeout=2) is not None
    finally:
        _cleanup_proc(descendant)


def test_validate_process_record_accepts_legacy_and_extended_cleanup_and_rejects_contradictions(
    tmp_path: Path,
) -> None:
    legacy_ok = _base_record(
        tmp_path,
        session_id="bg-w6-val-legacy",
        status="killed",
        termination={
            "cleanup": {
                "confirmed": True,
                "instances": [
                    {
                        "method": "fixture",
                        "confirmed": True,
                        "return_code": 0,
                        "observed_processes": 1,
                        "unresolved_pids": [],
                    }
                ],
            }
        },
    )
    validate_process_record(legacy_ok)

    extended_ok = _base_record(
        tmp_path,
        session_id="bg-w6-val-ext",
        status="unknown",
        termination={
            "cleanup": {
                "confirmed": False,
                "tree_observation_complete": False,
                "unresolved_instances": [{"pid": 123, "birth_token": "bt-123"}],
                "instances": [
                    {
                        "method": "SIGTERM",
                        "confirmed": False,
                        "return_code": None,
                        "observed_processes": 2,
                        "unresolved_pids": [123],
                        "tree_observation_complete": False,
                        "unresolved_instances": [{"pid": 123, "birth_token": "bt-123"}],
                    }
                ],
            }
        },
    )
    validate_process_record(extended_ok)

    for bad_cleanup in (
        {"confirmed": True, "instances": [], "tree_observation_complete": False},
        {"confirmed": True, "instances": [], "unresolved_instances": [{"pid": 123, "birth_token": "bt"}]},
        {"confirmed": False, "instances": [], "tree_observation_complete": "false"},
        {"confirmed": False, "instances": [], "unresolved_instances": [{"pid": 0, "birth_token": "bt"}]},
        {"confirmed": False, "instances": [], "unresolved_instances": [{"pid": 12, "birth_token": "  "}]},
        {"confirmed": False, "instances": [], "unresolved_instances": [{"pid": 12, "birth_token": "bt", "extra": 1}]},
        {"confirmed": False, "instances": [], "unexpected_flag": True},
    ):
        with pytest.raises(ValueError, match="cleanup"):
            validate_process_record(
                _base_record(tmp_path, session_id="bg-w6-bad", status="unknown", termination={"cleanup": bad_cleanup})
            )


def test_terminate_process_tree_populates_birth_proof_and_incomplete_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc, birth = _spawn_sleeper()
    try:
        child_pid = proc.pid + 100000
        monkeypatch.setattr(
            process_registry,
            "_fresh_process_tree_snapshot",
            lambda pid: ({pid: birth, child_pid: "child-bt"}, True),
        )
        monkeypatch.setattr(process_registry, "_signal_process_snapshot", lambda *_: True)
        monkeypatch.setattr(process_registry, "_wait_process_snapshot_gone", lambda *_: False)
        monkeypatch.setattr(
            process_registry,
            "_process_instance_terminated",
            lambda member, token: member != child_pid,
        )
        monkeypatch.setattr(process_registry, "_same_process", lambda *_: False)

        receipt = terminate_process_tree(proc.pid, None, expected_birth_token=birth, grace_seconds=0.0)
        assert receipt.confirmed is False
        assert receipt.tree_observation_complete is True
        assert receipt.unresolved_pids == (child_pid,)
        assert receipt.unresolved_instances == ({"pid": child_pid, "birth_token": "child-bt"},)
    finally:
        _cleanup_proc(proc)


def test_five_arg_receipt_asdict_preserves_native_host_wire_contract_and_extended_replace() -> None:
    five_arg = ProcessTerminationReceipt("native_singleton_kernel_exit", True, 0, 1, ())
    wire_dict = asdict(five_arg)
    assert set(wire_dict) == {
        "method",
        "confirmed",
        "return_code",
        "observed_processes",
        "unresolved_pids",
    }
    wire_frame = json.loads(json.dumps({"version": 2, "receipt": wire_dict}))

    proxy = NativeHostProcess.__new__(NativeHostProcess)
    proxy._token = "tok-w6"  # noqa: SLF001
    proxy._require_original = lambda: None  # noqa: SLF001
    proxy._client = SimpleNamespace(_rpc=lambda frame, kind: {"result": wire_frame})  # noqa: SLF001
    parsed = proxy.terminate_tree()
    assert parsed == five_arg
    assert proxy.returncode == 0

    extended = ProcessTerminationReceipt(
        "SIGTERM->SIGKILL",
        False,
        None,
        2,
        (7,),
        tree_observation_complete=True,
        unresolved_instances=({"pid": 7, "birth_token": "bt-7"},),
    )
    assert isinstance(extended, ProcessTerminationReceipt)
    assert set(asdict(extended)) == {
        "method",
        "confirmed",
        "return_code",
        "observed_processes",
        "unresolved_pids",
        "tree_observation_complete",
        "unresolved_instances",
    }
    replaced = replace(extended, confirmed=True, unresolved_pids=(), unresolved_instances=())
    assert isinstance(replaced, ProcessTerminationReceipt)
    assert replaced.tree_observation_complete is True and replaced.confirmed is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX 僵尸进程出生标识探测行为测试")
def test_incomplete_observation_retry_with_zombie_leader_stays_sticky_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    host_pid, host_birth = 9999801, "zombie-host-birth"
    child_pid, child_birth = 9999802, "zombie-child-birth"
    prior_cleanup = {
        "confirmed": False,
        "instances": [
            {
                "method": "SIGTERM",
                "confirmed": False,
                "return_code": None,
                "observed_processes": 2,
                "unresolved_pids": [],
                "tree_observation_complete": False,
            }
        ],
        "tree_observation_complete": False,
        "unresolved_instances": [],
    }
    saved = store.write(
        _base_record(
            tmp_path,
            session_id="bg-w6-zombie-leader",
            status="unknown",
            pid=host_pid,
            pid_birth_token=host_birth,
            child_pid=child_pid,
            child_pid_birth_token=child_birth,
            termination={"cleanup": prior_cleanup},
        )
    )

    # 未回收 zombie 的出生标识仍能读出原 birth_token，但 _process_instance_terminated 判为已终止。
    monkeypatch.setattr(
        process_session_cleanup,
        "capture_process_birth_token",
        lambda pid: host_birth if pid == host_pid else child_birth if pid == child_pid else "",
    )
    monkeypatch.setattr(process_session_cleanup, "_process_instance_terminated", lambda *_: True)
    monkeypatch.setattr(process_session_cleanup, "_same_process", lambda *_: False)
    monkeypatch.setattr(
        process_session_cleanup,
        "terminate_process_tree",
        lambda pid, *args, **kwargs: ProcessTerminationReceipt(
            "SIGTERM", True, 0, 1, (), tree_observation_complete=True,
        ),
    )

    retried = stop_process_session(store, saved)
    assert retried.confirmed is False
    assert retried.record["status"] == "unknown"
    assert retried.record["termination"]["cleanup"]["confirmed"] is False
    assert retried.record["termination"]["cleanup"]["tree_observation_complete"] is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX 进程清理缓存副本隔离测试")
def test_process_registry_kill_returns_isolated_cleanup_copy(tmp_path: Path) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    saved = store.write(
        _base_record(
            tmp_path,
            session_id="bg-w6-kill-copy",
            status="killed",
            termination={
                "cleanup": {
                    "confirmed": True,
                    "tree_observation_complete": True,
                    "unresolved_instances": [],
                    "instances": [
                        {
                            "method": "SIGTERM",
                            "confirmed": True,
                            "return_code": 0,
                            "observed_processes": 2,
                            "unresolved_pids": [],
                            "tree_observation_complete": True,
                        }
                    ],
                }
            },
        )
    )
    reg = ProcessRegistry()
    scope = process_registry.ProcessAccessScope(
        owner_id="owner-w6",
        conversation_id="conv-w6",
        owner_home=str(tmp_path),
    )
    summary = reg.kill(saved["session_id"], access_scope=scope, store_root=store.root)
    assert summary is not None
    cached_entry = reg._processes[(str(store.root.resolve()), saved["session_id"])]  # noqa: SLF001
    cached_cleanup = cached_entry.persisted_snapshot["termination"]["cleanup"]
    summary["termination"]["confirmed"] = False
    summary["termination"]["instances"][0]["method"] = "MUTATED"
    summary["termination"]["instances"].append({"method": "EXTRA"})
    assert cached_cleanup["confirmed"] is True
    assert len(cached_cleanup["instances"]) == 1
    assert cached_cleanup["instances"][0]["method"] == "SIGTERM"
    reloaded = reg.get(saved["session_id"], access_scope=scope, store_root=store.root)
    assert reloaded is not None
    reloaded_cleanup = reloaded.persisted_snapshot["termination"]["cleanup"]
    assert reloaded_cleanup["confirmed"] is True
    assert len(reloaded_cleanup["instances"]) == 1
    assert reloaded_cleanup["instances"][0]["method"] == "SIGTERM"


@pytest.mark.skipif(os.name == "nt", reason="POSIX host 退出竞态与树完整性证明测试")
def test_host_self_exit_identity_unavailable_during_snapshot_settles_with_complete_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    host, host_birth = _spawn_sleeper()
    child, child_birth = _spawn_sleeper()
    try:
        saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-host-exit-unavail",
                status="exited",
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
                termination={"confirmed": True, "method": "SIGTERM", "observed_processes": 1, "unresolved_pids": []},
            )
        )
        _cleanup_proc(child)
        real_snapshot = process_registry._fresh_process_tree_snapshot  # noqa: SLF001

        def snapshot_during_host_exit(root_pid: int) -> tuple[dict[int, str], bool]:
            if root_pid == host.pid:
                _cleanup_proc(host)
                return {}, False
            return real_snapshot(root_pid)

        monkeypatch.setattr(process_registry, "_fresh_process_tree_snapshot", snapshot_during_host_exit)
        result = stop_process_session(store, saved)
        assert result.confirmed is True
        assert result.record["status"] == "exited"
        assert result.record["termination"]["confirmed"] is True
        cleanup = result.record["termination"]["cleanup"]
        assert cleanup["confirmed"] is True
        assert cleanup["tree_observation_complete"] is True
        assert cleanup["unresolved_instances"] == []
        assert len(cleanup["instances"]) == 1
        host_entry = cleanup["instances"][0]
        assert host_entry["method"] == "host_exited_during_stop"
        assert host_entry["confirmed"] is True
        assert host_entry["tree_observation_complete"] is True
        assert host_entry["unresolved_pids"] == []

        persisted = store.load("bg-w6-host-exit-unavail").record
        assert persisted["termination"]["cleanup"]["confirmed"] is True
        assert persisted["termination"]["cleanup"]["tree_observation_complete"] is True
        assert persisted["termination"]["cleanup"]["unresolved_instances"] == []

        ref = process_cleanup_reference(persisted)
        validate_cleanup_reference(ref)
        confirm_cleanup_reference(ref, persisted)
    finally:
        _cleanup_proc(host)
        _cleanup_proc(child)


@pytest.mark.skipif(os.name == "nt", reason="POSIX host 与子进程清理区分测试")
def test_host_self_exit_identity_unavailable_alongside_already_gone_child_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    host, host_birth = _spawn_sleeper()
    child, child_birth = _spawn_sleeper()
    try:
        saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-host-unavail-child-gone",
                status="exited",
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
                termination={"confirmed": True, "method": "SIGTERM", "observed_processes": 1, "unresolved_pids": []},
            )
        )
        _cleanup_proc(host)
        _cleanup_proc(child)
        monkeypatch.setattr(process_session_cleanup, "capture_process_birth_token", lambda pid: host_birth if pid == host.pid else child_birth if pid == child.pid else "")
        monkeypatch.setattr(
            process_session_cleanup,
            "terminate_process_tree",
            lambda pid, proc, **kwargs: (
                ProcessTerminationReceipt(
                    "identity_unavailable",
                    False,
                    None,
                    0,
                    (host.pid,),
                    tree_observation_complete=False,
                    unresolved_instances=({"pid": host.pid, "birth_token": host_birth},),
                )
                if pid == host.pid
                else ProcessTerminationReceipt(
                    "already_gone",
                    True,
                    0,
                    1,
                    (),
                    tree_observation_complete=True,
                )
            ),
        )
        result = stop_process_session(store, saved)
        assert result.confirmed is True
        cleanup = result.record["termination"]["cleanup"]
        assert cleanup["confirmed"] is True
        assert cleanup["tree_observation_complete"] is True
        assert cleanup["unresolved_instances"] == []
        assert [inst["method"] for inst in cleanup["instances"]] == ["host_exited_during_stop", "already_gone"]
    finally:
        _cleanup_proc(host)
        _cleanup_proc(child)


@pytest.mark.skipif(os.name == "nt", reason="POSIX child identity_unavailable 严禁自动结清测试")
@pytest.mark.parametrize("with_child_unresolved", [True, False])
def test_child_identity_unavailable_never_auto_settles_and_stays_sticky_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_child_unresolved: bool
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    host, host_birth = _spawn_sleeper()
    child, child_birth = _spawn_sleeper()
    try:
        saved = store.write(
            _base_record(
                tmp_path,
                session_id=f"bg-w6-child-unavail-{int(with_child_unresolved)}",
                status="exited",
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
                termination={"confirmed": True, "method": "SIGTERM", "observed_processes": 1, "unresolved_pids": []},
            )
        )
        _cleanup_proc(host)
        _cleanup_proc(child)
        monkeypatch.setattr(
            process_session_cleanup,
            "capture_process_birth_token",
            lambda pid: host_birth if pid == host.pid else child_birth if pid == child.pid else "",
        )
        monkeypatch.setattr(
            process_session_cleanup,
            "terminate_process_tree",
            lambda pid, proc, **kwargs: (
                ProcessTerminationReceipt("already_gone", True, 0, 1, (), tree_observation_complete=True)
                if pid == host.pid
                else ProcessTerminationReceipt(
                    "identity_unavailable",
                    False,
                    None,
                    0,
                    (child.pid,) if with_child_unresolved else (),
                    tree_observation_complete=False,
                    unresolved_instances=(({"pid": child.pid, "birth_token": child_birth},) if with_child_unresolved else ()),
                )
            ),
        )
        first = stop_process_session(store, saved)
        assert first.confirmed is False
        cleanup1 = first.record["termination"]["cleanup"]
        assert cleanup1["confirmed"] is False
        assert cleanup1["tree_observation_complete"] is False
        assert cleanup1["instances"][1]["method"] == "identity_unavailable"

        monkeypatch.undo()
        second = stop_process_session(store, first.record)
        assert second.confirmed is False
        cleanup2 = second.record["termination"]["cleanup"]
        assert cleanup2["confirmed"] is False
        assert cleanup2["tree_observation_complete"] is False
    finally:
        _cleanup_proc(host)
        _cleanup_proc(child)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 缺 host 终态确认时 identity_unavailable 严禁自动结清测试")
@pytest.mark.parametrize(
    "status,termination,expected_status",
    [
        ("running", {"confirmed": True, "method": "SIGTERM", "observed_processes": 1, "unresolved_pids": []}, "unknown"),
        ("exited", {"confirmed": False, "method": "SIGTERM", "observed_processes": 1, "unresolved_pids": []}, "exited"),
        ("exited", {}, "exited"),
    ],
)
def test_host_identity_unavailable_without_confirmed_terminal_proof_stays_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    termination: dict[str, object],
    expected_status: str,
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    host, host_birth = _spawn_sleeper()
    child, child_birth = _spawn_sleeper()
    try:
        saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-host-no-proof",
                status=status,
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
                termination=termination,
            )
        )
        _cleanup_proc(host)
        _cleanup_proc(child)
        monkeypatch.setattr(process_session_cleanup, "capture_process_birth_token", lambda pid: host_birth if pid == host.pid else "")
        monkeypatch.setattr(
            process_session_cleanup,
            "terminate_process_tree",
            lambda pid, proc, **kwargs: ProcessTerminationReceipt(
                "identity_unavailable",
                False,
                None,
                0,
                (host.pid,),
                tree_observation_complete=False,
                unresolved_instances=({"pid": host.pid, "birth_token": host_birth},),
            ),
        )
        result = stop_process_session(store, saved)
        assert result.confirmed is False
        assert result.record["status"] == expected_status
        cleanup = result.record["termination"]["cleanup"]
        assert cleanup["confirmed"] is False
        assert cleanup["tree_observation_complete"] is False
        assert cleanup["instances"][0]["method"] == "identity_unavailable"
    finally:
        _cleanup_proc(host)
        _cleanup_proc(child)


@pytest.mark.skipif(os.name == "nt", reason="POSIX prior_blocked 或存活后代阻塞 host 自动结清测试")
def test_host_identity_unavailable_fails_closed_on_prior_blocked_and_live_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProcessSessionStore(tmp_path / "sessions")
    host, host_birth = _spawn_sleeper()
    child, child_birth = _spawn_sleeper()
    desc, desc_birth = _spawn_sleeper()
    try:
        _cleanup_proc(host)
        _cleanup_proc(child)

        # 场景 1: prior_blocked=True（存在先前未完整树观察及存活后代）时严禁结算。
        prior_cleanup = {
            "confirmed": False,
            "tree_observation_complete": False,
            "unresolved_instances": [{"pid": desc.pid, "birth_token": desc_birth}],
            "instances": [
                {
                    "method": "SIGTERM",
                    "confirmed": False,
                    "return_code": None,
                    "observed_processes": 2,
                    "unresolved_pids": [desc.pid],
                    "tree_observation_complete": False,
                    "unresolved_instances": [{"pid": desc.pid, "birth_token": desc_birth}],
                }
            ],
        }
        blocked_saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-prior-blocked",
                status="exited",
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
                termination={"confirmed": True, "cleanup": prior_cleanup},
            )
        )
        with monkeypatch.context() as patch:
            patch.setattr(process_session_cleanup, "capture_process_birth_token", lambda pid: host_birth if pid == host.pid else "")
            patch.setattr(
                process_session_cleanup,
                "terminate_process_tree",
                lambda pid, proc, **kwargs: (
                    ProcessTerminationReceipt(
                        "identity_unavailable",
                        False,
                        None,
                        0,
                        (host.pid,),
                        tree_observation_complete=False,
                        unresolved_instances=({"pid": host.pid, "birth_token": host_birth},),
                    )
                    if pid == host.pid
                    else ProcessTerminationReceipt(
                        "SIGTERM->SIGKILL",
                        False,
                        None,
                        1,
                        (desc.pid,),
                        tree_observation_complete=True,
                        unresolved_instances=({"pid": desc.pid, "birth_token": desc_birth},),
                    )
                ),
            )
            blocked_res = stop_process_session(store, blocked_saved)
        assert blocked_res.confirmed is False
        blocked_cleanup = blocked_res.record["termination"]["cleanup"]
        assert blocked_cleanup["confirmed"] is False
        assert blocked_cleanup["tree_observation_complete"] is False
        assert {"pid": desc.pid, "birth_token": desc_birth} in blocked_cleanup["unresolved_instances"]

        # 场景 2: 无 prior_blocked，但 host 回执携带仍在存活的后代实例 -> 严禁结清并保留该后代。
        live_desc_saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-live-desc-receipt",
                status="exited",
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
                termination={"confirmed": True, "method": "SIGTERM", "observed_processes": 1, "unresolved_pids": []},
            )
        )
        with monkeypatch.context() as patch:
            patch.setattr(process_session_cleanup, "capture_process_birth_token", lambda pid: host_birth if pid == host.pid else "")
            patch.setattr(
                process_session_cleanup,
                "terminate_process_tree",
                lambda pid, proc, **kwargs: ProcessTerminationReceipt(
                    "identity_unavailable",
                    False,
                    None,
                    2,
                    (host.pid, desc.pid),
                    tree_observation_complete=False,
                    unresolved_instances=({"pid": desc.pid, "birth_token": desc_birth},),
                ),
            )
            live_res = stop_process_session(store, live_desc_saved)
        assert live_res.confirmed is False
        live_cleanup = live_res.record["termination"]["cleanup"]
        assert live_cleanup["confirmed"] is False
        assert live_cleanup["tree_observation_complete"] is False
        assert live_cleanup["unresolved_instances"] == [{"pid": desc.pid, "birth_token": desc_birth}]

        # 场景 3: host 回执携带未绑定出生身份的其它裸 PID -> 严禁结清。
        bare_pid_saved = store.write(
            _base_record(
                tmp_path,
                session_id="bg-w6-bare-pid-receipt",
                status="exited",
                pid=host.pid,
                pid_birth_token=host_birth,
                child_pid=child.pid,
                child_pid_birth_token=child_birth,
                termination={"confirmed": True, "method": "SIGTERM", "observed_processes": 1, "unresolved_pids": []},
            )
        )
        with monkeypatch.context() as patch:
            patch.setattr(process_session_cleanup, "capture_process_birth_token", lambda pid: host_birth if pid == host.pid else "")
            patch.setattr(
                process_session_cleanup,
                "terminate_process_tree",
                lambda pid, proc, **kwargs: ProcessTerminationReceipt(
                    "identity_unavailable",
                    False,
                    None,
                    2,
                    (desc.pid,),
                    tree_observation_complete=False,
                ),
            )
            bare_res = stop_process_session(store, bare_pid_saved)
        assert bare_res.confirmed is False
        bare_cleanup = bare_res.record["termination"]["cleanup"]
        assert bare_cleanup["confirmed"] is False
        assert bare_cleanup["tree_observation_complete"] is False
    finally:
        _cleanup_proc(host)
        _cleanup_proc(child)
        _cleanup_proc(desc)
