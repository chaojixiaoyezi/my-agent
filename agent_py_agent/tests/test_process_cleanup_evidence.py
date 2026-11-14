"""原清理事实的持久回读验证，终态业务内容不被资源回收改写。"""

import pytest

from agent_py_agent.agent.tooling import process_session_cleanup as cleanup_module
from agent_py_agent.agent.tooling import process_session_commit as commit_module
from agent_py_agent.agent.tooling.background_process_launch import start_background_process
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.agent.tooling.process_session_cleanup import (
    ProcessSessionCleanupError,
    stop_process_session,
)
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    ProcessSessionTransaction,
)
from agent_py_agent.tests._managed_process_harness import managed_request
from agent_py_agent.tests.test_background_handoff import _bound_record


def test_natural_exit_retains_business_facts_and_persists_complete_cleanup(tmp_path):
    hosted = start_background_process(managed_request(tmp_path, "raise SystemExit(7)"))
    hosted.process.wait(timeout=5)
    store = ProcessSessionStore(hosted.store_root)
    before = store.load(hosted.record["session_id"]).record
    assert before["status"] == "exited" and before["exit_code"] == 7
    result = stop_process_session(store, before, host_process=hosted.process)
    assert result.confirmed
    fresh = ProcessSessionStore(hosted.store_root).load(before["session_id"]).record
    assert fresh["termination"]["cleanup"]["confirmed"]
    assert {key: fresh[key] for key in ("status", "exit_code", "finished_at")} == {
        key: before[key] for key in ("status", "exit_code", "finished_at")}
    assert {key: value for key, value in fresh["termination"].items() if key != "cleanup"} == before["termination"]
    # 模拟同一原 host 晚到的当前 revision 更新；它没有清理事实，不能抹掉已确认退出。
    late = {**before, "revision": fresh["revision"]}
    assert store.write(late)["termination"]["cleanup"] == fresh["termination"]["cleanup"]


@pytest.mark.parametrize("prior", [None, {"confirmed": False}, {"confirmed": True}])
def test_terminal_cleanup_is_separate_from_child_receipt(tmp_path, monkeypatch, prior):
    store, record = _bound_record(tmp_path, "exited")
    if prior is not None:
        # 原始终态夹具在第一次写盘前带入 child 回执，不借普通终态更新改写历史。
        record = {**record, "session_id": "bg-prior", "revision": 0, "termination": prior}
        record = store.write(record)
    receipt = ProcessTerminationReceipt("not_running", True, 0, 0)
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances", lambda *_: (receipt,))
    monkeypatch.setattr(cleanup_module, "_process_instance_terminated", lambda *_: True)
    result = stop_process_session(store, record)
    assert result.confirmed
    after = store.load(record["session_id"]).record
    assert after["termination"]["cleanup"]["confirmed"]
    assert after["status"] == "exited" and after["finished_at"] == record["finished_at"]
    if prior:
        assert after["termination"]["confirmed"] is prior["confirmed"]
    later = store.write({**after, "termination": {**after["termination"], "cleanup": {
        "confirmed": False, "instances": [],
    }}})
    assert later["termination"]["cleanup"] == after["termination"]["cleanup"]


def test_terminal_cleanup_write_failure_retains_unconfirmed_receipt(tmp_path, monkeypatch):
    store, record = _bound_record(tmp_path, "exited")
    receipt = ProcessTerminationReceipt("not_running", True, 0, 0)
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances", lambda *_: (receipt,))
    monkeypatch.setattr(cleanup_module, "_process_instance_terminated", lambda *_: True)
    original = ProcessSessionTransaction.write

    def fail_evidence(self, payload):
        if "cleanup" in payload.get("termination", {}):
            raise OSError("evidence storage unavailable")
        return original(self, payload)

    monkeypatch.setattr(ProcessSessionTransaction, "write", fail_evidence)
    with pytest.raises(ProcessSessionCleanupError) as failure:
        stop_process_session(store, record)
    assert failure.value.report["termination_receipts"][0]["confirmed"]
    after = store.load(record["session_id"]).record
    assert after["status"] == "exited" and after["stop_requested"]
    assert "cleanup" not in after.get("termination", {})


@pytest.mark.parametrize("cleanup", [{"confirmed": "true", "instances": []}, {"confirmed": True},
                                   {"confirmed": True, "instances": [{}]}])
def test_malformed_cleanup_does_not_become_resource_authority(tmp_path, cleanup):
    store, record = _bound_record(tmp_path, "exited")
    with pytest.raises(ValueError, match="cleanup"):
        store.write({**record, "termination": {"cleanup": cleanup}})


def test_unknown_without_persisted_cleanup_cannot_be_confirmed_by_gone_pids(tmp_path, monkeypatch):
    store, record = _bound_record(tmp_path, "unknown")
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances", lambda *_: ())
    monkeypatch.setattr(cleanup_module, "_process_instance_terminated", lambda *_: True)
    result = stop_process_session(store, record)
    assert not result.confirmed and not result.terminations
    after = ProcessSessionStore(store.root).load(record["session_id"]).record
    assert after["status"] == "unknown" and after["stop_requested"]
    assert after["termination"]["cleanup"] == {"confirmed": False, "instances": []}


def test_committed_cleanup_recovers_original_evidence_after_install_failure(tmp_path, monkeypatch):
    store, record = _bound_record(tmp_path, "unknown")
    receipt = ProcessTerminationReceipt("SIGTERM", True, -15, 2)
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances", lambda *_: (receipt,))
    monkeypatch.setattr(cleanup_module, "_process_instance_terminated", lambda *_: True)
    original = commit_module.write_json_file_atomic_unlocked

    def fail_record_install(path, payload):
        if path.name == record["session_id"] + ".json" and "cleanup" in payload.get("termination", {}):
            raise OSError("committed cleanup installation unavailable")
        return original(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(commit_module, "write_json_file_atomic_unlocked", fail_record_install)
        with pytest.raises(ProcessSessionCleanupError) as failure:
            stop_process_session(store, record)
        assert failure.value.report["recovery_required"]
        assert failure.value.record["termination"]["cleanup"]["confirmed"]
    fresh = ProcessSessionStore(store.root).load(record["session_id"]).record
    assert fresh["status"] == "killed" and fresh["termination"]["cleanup"]["confirmed"]
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances", lambda *_: ())
    assert stop_process_session(store, fresh).confirmed
    assert not (store.root / ".process-sessions.redo.json").exists()


def test_stop_allows_original_host_to_publish_before_fallback_signals(tmp_path, monkeypatch):
    """Event交错证明短事务已释放，原host能提交子树凭证后才进入fallback。"""
    import time
    from threading import Event, Thread

    store, record = _bound_record(tmp_path)
    observed, published = Event(), Event()
    original_load = store.load
    def observe(session_id):
        report = original_load(session_id)
        if report.record.get("stop_requested"):
            observed.set()
        return report
    monkeypatch.setattr(store, "load", observe)
    monkeypatch.setattr(cleanup_module, "capture_process_birth_token",
                        lambda pid: "" if published.is_set() else record["pid_birth_token"])
    monkeypatch.setattr(cleanup_module, "_process_instance_terminated", lambda *_: published.is_set())
    def publish():
        assert observed.wait(2)
        with store.transaction() as tx:
            current = tx.load(record["session_id"])
            tx.write({**current, "status": "killed", "finished_at": time.time(), "exit_code": -15,
                      "termination": {"method": "original_host_child_tree", "confirmed": True,
                                      "return_code": -15, "observed_processes": 2, "unresolved_pids": []}})
        published.set()
    def fallback(*_):
        assert published.is_set(), "不得先杀host而让原凭证丢失"
        return ()
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances", fallback)
    worker = Thread(target=publish)
    worker.start()
    try:
        result = stop_process_session(store, record)
    finally:
        observed.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert result.confirmed
    assert result.record["termination"]["method"] == "original_host_child_tree"
    assert result.record["termination"]["cleanup"]["confirmed"]


def test_dead_host_without_original_receipt_stays_unknown(tmp_path, monkeypatch):
    store, record = _bound_record(tmp_path)
    monkeypatch.setattr(cleanup_module, "capture_process_birth_token", lambda _: "")
    monkeypatch.setattr(cleanup_module, "_process_instance_terminated", lambda *_: True)
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances", lambda *_: ())
    result = stop_process_session(store, record)
    assert not result.confirmed and result.record["status"] == "unknown"
    assert result.record["termination"] == {"cleanup": {"confirmed": False, "instances": []}}


def test_real_stop_retains_host_owned_child_tree_exit_proof(tmp_path):
    """真实独立host收束child及其孙进程并落原凭证；不以PID消失或returncode替代。"""
    import json
    import time

    code = ("import subprocess,sys,time,json; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "open('grandchild.json','w').write(json.dumps({'pid':p.pid})); time.sleep(30)")
    hosted = start_background_process(managed_request(tmp_path, code))
    store = ProcessSessionStore(hosted.store_root)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "grandchild.json").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert json.loads((tmp_path / "grandchild.json").read_text())["pid"] > 1
        before = store.load(hosted.record["session_id"]).record
        result = stop_process_session(store, before, host_process=hosted.process)
        assert result.confirmed
        proof = result.record["termination"]
        assert proof["confirmed"] is True
        assert proof["observed_processes"] >= 2
        assert proof["return_code"] is not None and proof["unresolved_pids"] == []
        assert proof["cleanup"]["confirmed"] is True
        hosted.process.wait(timeout=5)
    finally:
        if hosted.process.poll() is None:
            stop_process_session(store, store.load(hosted.record["session_id"]).record, host_process=hosted.process)
            hosted.process.wait(timeout=5)


def test_host_cleanup_observation_fault_retains_precise_authority_report(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionLoadReport

    store, record = _bound_record(tmp_path)
    monkeypatch.setattr(cleanup_module, "capture_process_birth_token", lambda _: record["pid_birth_token"])
    monkeypatch.setattr(cleanup_module, "_process_instance_terminated", lambda *_: False)
    monkeypatch.setattr(store, "load", lambda _: ProcessSessionLoadReport({}, {
        "error_type": "PermissionError", "context": "process_session_store.load", "reason": "synthetic denied"}))
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances",
                        lambda *_: pytest.fail("权威不可读时不得继续发信号"))
    with pytest.raises(ProcessSessionCleanupError) as failure:
        stop_process_session(store, record)
    assert failure.value.report["committed"] is True
    assert failure.value.report["authority_error"]["error_type"] == "PermissionError"
    assert failure.value.report["termination_receipts"] == []
