"""错配持久停止回执不能凭同owner/终态/PID消失成为确认事实。"""

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space.owner_maintenance import _cleanup_owner_runtime


def receipt():
    return {
        "schema": "stop-operation.v1",
        "failure_codes": ["RUNNER_WORKER_EXIT_UNCONFIRMED"],
        "records": [
            {
                "schema": "runner-stop.v1",
                "run_id": "child",
                "attempt_id": "attempt",
                "authority_closed": True,
                "resources_confirmed": True,
                "runner_confirmed": False,
                "pid": 12345,
                "birth_token": "birth",
                "launch_id": "launch",
                "runner_status": "cooperative_requested",
            }
        ],
    }


def _database(tmp_path, count):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "runtime.db")
    parent = repo.record_run_creation(
        owner_id="owner",
        goal="test",
        conversation_task_id="parent-task",
        thread_id="thread",
        run_id="parent",
        role="assistant",
    )
    child = repo.record_run_creation(
        owner_id="owner",
        goal="test",
        conversation_task_id="child-task",
        thread_id="thread",
        run_id="child",
        role="assistant",
    )
    repo.settle_agent_run(
        agent_run_id=child["agent_run_id"], attempt_id=child["attempt_id"], status="cancelled"
    )
    repo.settle_agent_run(
        agent_run_id=parent["agent_run_id"], attempt_id=parent["attempt_id"], status="failed"
    )
    frozen = receipt()
    frozen["records"][0]["attempt_id"] = child["attempt_id"]
    with repo.transaction() as conn:
        for number in range(count):
            payload = {
                "result": {"result_envelope": {"stop_receipts": frozen}},
                "unknown_reason": "preserve-original",
            }
            conn.execute(
                "INSERT INTO tool_operations(operation_id,agent_run_id,attempt_id,attempt_generation,"
                "tool_operation_generation,operation_type,status,outcome_json,created_at,updated_at) "
                "VALUES(?,?,?,1,7,'cancel_subagents','UNKNOWN',?,1,1)",
                (
                    f"op-{number:04d}",
                    parent["agent_run_id"],
                    parent["attempt_id"],
                    json.dumps(payload),
                ),
            )
    return repo, parent, child


@pytest.mark.parametrize("mismatch", ["target", "birth", "launch", "pid", "canonical_missing"])
def test_missing_source_binding_never_attaches_confirmation(tmp_path, monkeypatch, mismatch):
    repo, parent, child = _database(tmp_path, 1)
    with repo.transaction() as conn:
        row = conn.execute("SELECT outcome_json FROM tool_operations").fetchone()
        payload = json.loads(row[0])
        # 现有schema没有可与此claimed operation对拍的canonical source binding。
        # 同owner的取消终态与任何非空birth/launch都不能补出这份证据。
        payload["args_hash"] = "hash-of-another-authorized-target"
        frozen = payload["result"]["result_envelope"]["stop_receipts"]["records"][0]
        if mismatch in {"birth", "launch", "pid"}:
            key = {"birth": "birth_token", "launch": "launch_id", "pid": "pid"}[mismatch]
            frozen[key] = 2147483647 if key == "pid" else "wrong-but-nonempty"
        original = json.dumps(payload)
        conn.execute("UPDATE tool_operations SET outcome_json=?", (original,))
    monkeypatch.setattr("os.kill", lambda *args: pytest.fail("diagnostics must not observe a PID"))
    report = _cleanup_owner_runtime(
        SimpleNamespace(owner_home_dir=tmp_path), now=1, commit_transition=lambda action: action()
    )
    assert report["executed"] is True
    assert "stop_reconciliation" not in report
    with repo._runtime_connection() as conn:
        row = conn.execute("SELECT status,outcome_json FROM tool_operations").fetchone()
        assert row["status"] == "UNKNOWN"
        assert (
            "stop_reconciliation"
            not in json.loads(row["outcome_json"])["result"]["result_envelope"]
        )
        assert row["outcome_json"] == original
        assert (
            conn.execute(
                "SELECT 1 FROM metadata WHERE key='stop_reconciliation.cursor.v1'"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT status FROM agent_runs WHERE agent_run_id=?", (parent["agent_run_id"],)
            ).fetchone()[0]
            == "failed"
        )
        assert (
            conn.execute(
                "SELECT status FROM agent_runs WHERE agent_run_id=?", (child["agent_run_id"],)
            ).fetchone()[0]
            == "cancelled"
        )


def test_owned_process_exit_does_not_upgrade_unbound_legacy_receipt(tmp_path, monkeypatch):
    import subprocess
    import sys

    # 原Popen句柄确证本测试自己的进程退出，仍不能补出operation→授权目标的持久绑定。
    process = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.buffer.read()'],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        repo, _parent, _child = _database(tmp_path, 1)
        with repo.transaction() as conn:
            payload = json.loads(conn.execute('SELECT outcome_json FROM tool_operations').fetchone()[0])
            payload['result']['result_envelope']['stop_receipts']['records'][0]['pid'] = process.pid
            original = json.dumps(payload)
            conn.execute('UPDATE tool_operations SET outcome_json=?', (original,))
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        monkeypatch.setattr('os.kill', lambda *_args: pytest.fail('legacy diagnostic cannot authorize PID observations'))
        report = _cleanup_owner_runtime(SimpleNamespace(owner_home_dir=tmp_path), now=1,
                                        commit_transition=lambda action: action())
        assert report['executed'] is True and 'stop_reconciliation' not in report
        with repo._runtime_connection() as conn:
            row = conn.execute('SELECT status,outcome_json FROM tool_operations').fetchone()
            assert row['status'] == 'UNKNOWN' and row['outcome_json'] == original
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.wait(timeout=5)
