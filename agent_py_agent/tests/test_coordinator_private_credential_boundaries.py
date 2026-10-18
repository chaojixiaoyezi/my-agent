"""原私卡C事务的真实锁、配额、来源异常与旧字段边界。"""

import json
import time

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.conversation.input_dependencies import input_dependency_path
from agent_py_agent.agent.gateway_parts.io import locked_file_transition
from agent_py_agent.tests.test_coordinator_private_credential_transactions import (
    original_case,
    post,
)


@pytest.mark.parametrize("held", ["quota", "card", "input", "refs", "wake", "audit"])
def test_original_locks_refuse_immediately_and_replay_only_original_facts(
    tmp_path, monkeypatch, held
):
    operation = "cancel" if held == "audit" else "submit"
    with original_case(tmp_path, monkeypatch, operation=operation) as (
        agent,
        path,
        row,
        body,
        server,
        _runtime,
    ):
        store = agent.conversation_store
        dependency_path = input_dependency_path(agent, row["input_id"])
        task_path = store.tasks.storage.task_path(row["scope"]["root_task_id"])
        anchor, manager = {
            "quota": (agent.home_paths.owner_home_dir / ".owner-quota", locked_json_path),
            "card": (path, locked_json_path),
            "input": (dependency_path, locked_json_path),
            "refs": (task_path, locked_file_transition),
            "wake": (
                store.storage.wake_dedupe_path(
                    row["scope"]["thread_id"], "task-input:" + row["input_id"]
                ),
                locked_file_transition,
            ),
            "audit": (agent.home_paths.owner_audit_log_jsonl, locked_json_path),
        }[held]
        if held == "quota":
            (agent.home_paths.owner_home_dir / "quota.json").write_text(
                json.dumps({"max_disk_mb": 1024})
            )
        before = path.read_bytes()
        with manager(anchor):
            started = time.monotonic()
            status, result = post(server, row, body)
            assert status == 503 and result["error_code"] == "IM_INTERACTION_HOST_UNAVAILABLE"
            assert time.monotonic() - started < 1
        saved = path.read_bytes()
        input_row = json.loads(dependency_path.read_text())
        if held in {"quota", "card", "audit"}:
            assert saved == before and input_row["status"] == "pending"
        else:
            assert json.loads(saved)["status"] == "ready"
            assert input_row["status"] == ("pending" if held == "input" else "ready")
        before_wakes = {wake.wake_signal_id for wake in store.wakes.pending()}
        assert post(server, row, body)[1]["ok"] is True
        if held not in {"quota", "card", "audit"}:
            assert path.read_bytes() == saved
        wakes = {wake.wake_signal_id for wake in store.wakes.pending()}
        assert len(wakes) == (0 if operation == "cancel" else 1)
        assert before_wakes <= wakes
        assert (
            row["input_id"]
            not in store.tasks.load(row["scope"]["root_task_id"]).input_dependency_refs
        )


def test_enabled_quota_runs_original_strict_scan_and_preserves_pending_on_excess(
    tmp_path, monkeypatch
):
    from agent_py_agent.agent.user_space import owner_quota

    calls = []
    original = owner_quota._owner_usage_bytes_for_admission

    def observed(root):
        calls.append(root)
        return original(root)

    with original_case(tmp_path, monkeypatch) as (agent, path, row, body, server, _runtime):
        home = agent.home_paths.owner_home_dir
        (home / "quota.json").write_text(json.dumps({"max_disk_mb": 1}))
        (home / "synthetic-quota-data.bin").write_bytes(b"x" * (2 * 1024 * 1024))
        monkeypatch.setattr(owner_quota, "_owner_usage_bytes_for_admission", observed)
        before = path.read_bytes()
        status, _result = post(server, row, body)
        assert status == 503 and calls == [home]
        assert path.read_bytes() == before
        assert (
            json.loads(input_dependency_path(agent, row["input_id"]).read_text())["status"]
            == "pending"
        )
        assert not agent.conversation_store.wakes.pending()


@pytest.mark.parametrize(
    "kind", ["request_missing", "request_unknown", "bounded_unknown", "tool_missing"]
)
def test_actual_typed_failure_is_same_object_without_new_claim_or_terminal(
    tmp_path, monkeypatch, kind
):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
    from agent_py_agent.agent.gateway_parts import private_interaction_transaction as pi
    from agent_py_agent.agent.user_space import task_credentials

    errors = {
        "request_missing": RequestExecutionContextUnavailable("synthetic"),
        "request_unknown": RequestExecutionOutcomeUncertainError(
            {
                "id": "synthetic-original",
                "execution_attempt_id": "attempt",
                "lease_epoch": 1,
                "lease_owner": "owner",
            },
            "private-row",
        ),
        "bounded_unknown": BoundedCallExitUnconfirmedError(
            "synthetic-original-call", "worker_exit_unconfirmed"
        ),
        "tool_missing": ToolWriteContextUnavailable("synthetic"),
    }
    error = errors[kind]
    observed = []
    original = pi.execute_coordinator_private_interaction

    def observe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except BaseException as caught:
            observed.append(caught)
            raise

    def fail(*_args):
        raise error

    with original_case(tmp_path, monkeypatch) as (agent, path, row, body, server, _runtime):
        before = path.read_bytes()
        gateway_before = {
            str(p.relative_to(server.paths.root)): p.read_bytes()
            for p in server.paths.root.rglob("*.json")
        }
        monkeypatch.setattr(pi, "execute_coordinator_private_interaction", observe)
        monkeypatch.setattr(task_credentials, "_save_credential_row", fail)
        assert post(server, row, body)[0] == 503
        assert observed == [error] and observed[0] is error
        assert path.read_bytes() == before
        assert (
            json.loads(input_dependency_path(agent, row["input_id"]).read_text())["status"]
            == "pending"
        )
        assert not agent.conversation_store.wakes.pending()
        assert {
            str(p.relative_to(server.paths.root)): p.read_bytes()
            for p in server.paths.root.rglob("*.json")
        } == gateway_before


@pytest.mark.parametrize("kind", ["model_configuration", "tool_approval"])
def test_other_valid_card_domains_remain_waiting_without_foreign_agent(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.user_space.interaction_refs import (
        sign_interaction_reference,
        verify_interaction_reference,
    )

    with original_case(tmp_path, monkeypatch) as (agent, path, row, body, server, _runtime):
        payload = verify_interaction_reference(
            agent.home_paths.root, body["reference"], channel="feishu", actor_id=row["actor_id"]
        )
        payload["kind"] = kind
        body["reference"] = sign_interaction_reference(agent.home_paths.owner_home_dir, payload)
        body["decision"] = "cancel"
        body.pop("fields", None)
        before = path.read_bytes()
        status, result = post(server, row, body)
        if kind == "tool_approval":
            assert status == 200 and result == {"ok": False, "error_code": "IM_INTERACTION_ALREADY_HANDLED"}
        else:
            assert status == 503 and result["error_code"] == "COORDINATOR_BUSINESS_NOT_READY"
        assert path.read_bytes() == before and server.agent is None


@pytest.mark.parametrize("operation", ["submit", "cancel"])
def test_original_missing_generation_and_input_fields_keep_ref_default_replay(
    tmp_path, monkeypatch, operation
):
    from agent_py_agent.agent.user_space.interaction_refs import (
        sign_interaction_reference,
        verify_interaction_reference,
    )

    with original_case(tmp_path, monkeypatch, operation=operation) as (
        agent,
        path,
        row,
        body,
        server,
        _runtime,
    ):
        payload = verify_interaction_reference(
            agent.home_paths.root, body["reference"], channel="feishu", actor_id=row["actor_id"]
        )
        payload.pop("generation")
        row.pop("generation")
        previous_id = row.pop("input_id")
        body["reference"] = row["reference"] = sign_interaction_reference(
            agent.home_paths.owner_home_dir, payload
        )
        old_path = input_dependency_path(agent, previous_id)
        dependency = json.loads(old_path.read_text())
        dependency["input_id"] = row["credential_ref"]
        new_path = input_dependency_path(agent, row["credential_ref"])
        new_path.write_text(json.dumps(dependency))
        old_path.unlink()
        link = agent.conversation_store.tasks.load(row["scope"]["root_task_id"])
        from agent_py_agent.agent.conversation.input_dependency_index import update_task_input_refs

        update_task_input_refs(agent, link.task_id, previous_id, remove=True)
        update_task_input_refs(agent, link.task_id, row["credential_ref"])
        path.write_text(json.dumps(row))
        assert post(server, row, body)[1]["ok"] is True
        saved = path.read_bytes()
        assert post(server, row, body)[1]["ok"] is True
        assert path.read_bytes() == saved
        assert json.loads(new_path.read_text())["status"] == (
            "ready" if operation == "submit" else "cancelled"
        )


def test_read_only_personal_lock_exclusive_busy_is_unavailable_not_unlocked(tmp_path, monkeypatch):
    import sqlite3

    from agent_py_agent.agent.session_lock.store import SessionLockStore

    with original_case(tmp_path, monkeypatch) as (agent, path, row, body, server, _runtime):
        lock_path = agent.home_paths.root / "session_lock" / "private_chat_locks.db"
        with SessionLockStore(lock_path, read_only=True)._connect() as readonly:
            assert readonly.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        with SessionLockStore(lock_path)._connect() as ordinary:
            assert ordinary.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        readonly.close()
        ordinary.close()
        import gc

        gc.collect()
        writer = sqlite3.connect(lock_path, timeout=0)
        try:
            assert writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
            writer.execute("BEGIN EXCLUSIVE")
            before = path.read_bytes()
            started = time.monotonic()
            status, result = post(server, row, body)
            assert status == 503 and result["error_code"] == "IM_INTERACTION_HOST_UNAVAILABLE"
            assert time.monotonic() - started < 1
            assert path.read_bytes() == before
            assert (
                json.loads(input_dependency_path(agent, row["input_id"]).read_text())["status"]
                == "pending"
            )
        finally:
            writer.rollback()
            writer.close()
        assert SessionLockStore(lock_path, read_only=True).has_password(row["actor_id"])
        assert post(server, row, body)[1]["ok"] is True


def test_existing_only_does_not_inherit_normal_cached_sql_budget(tmp_path):
    import sqlite3

    from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    path = tmp_path / "owner" / "runtime.db"
    normal = RuntimeRepository(path)
    existing = RuntimeRepository(path, existing_only=True)
    with sqlite_connection_scope(True):
        with normal._runtime_connection() as connection:
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        with existing._runtime_connection() as connection:
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        with normal._runtime_connection() as connection:
            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        writer = sqlite3.connect(path, timeout=0)
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        try:
            with pytest.raises(sqlite3.OperationalError):
                existing.upsert_wake(root_task_id="same-original", next_due_at=1)
            assert time.monotonic() - started < 1
        finally:
            writer.rollback()
            writer.close()


@pytest.mark.parametrize("held", ["signal", "paired_observation"])
def test_nonblocking_paired_install_preserves_prepared_and_replays_original_pair(tmp_path, held):
    from agent_py_agent.agent.conversation import store_wake_publication as publication
    from agent_py_agent.agent.conversation.store_observations import observation_from_request
    from agent_py_agent.agent.conversation.store_wakes import _wake_signal
    from agent_py_agent.tests.test_wake_publication_recovery import _fixture, _signal_ids

    store, observation_request, request = _fixture(tmp_path)
    observation, current = observation_from_request(request["thread_id"], observation_request)
    signal = _wake_signal(request["thread_id"], current, {**request, "observation": observation})
    anchor = (
        store.storage.wake_signal_path(signal)
        if held == "signal"
        else store.storage.observation_path(signal.thread_id)
    )
    with locked_json_path(anchor):
        with pytest.raises(BlockingIOError):
            publication.publish_deduped(
                store.storage, signal, observation=observation, retain_handled=True, blocking=False
            )
    receipt_path = store.storage.wake_dedupe_path(signal.thread_id, signal.dedupe_key)
    frozen = json.loads(receipt_path.read_text())
    assert frozen["phase"] == "prepared"
    installed_ids = _signal_ids(store)
    original, recovered = publication.publish_deduped(
        store.storage, signal, observation=observation, retain_handled=True, blocking=False
    )
    assert original.observation_id == observation.observation_id
    assert recovered.wake_signal_id == signal.wake_signal_id
    assert original.summary == observation.summary
    assert _signal_ids(store) == {signal.wake_signal_id} and installed_ids <= _signal_ids(store)
    assert len(store.observations.recent(signal.thread_id, limit=0)) == 1
    assert (
        publication.publication_receipt(store.storage, signal.thread_id, signal.dedupe_key)
        == "pending"
    )


@pytest.mark.parametrize("revoke", [False, True])
def test_local_main_card_requires_current_original_admin_binding(tmp_path, monkeypatch, revoke):
    from agent_py_agent.agent.user_space.admin_channel_identity import remove_admin_channel_identity

    with original_case(tmp_path, monkeypatch, kind="main") as (
        agent,
        path,
        row,
        body,
        server,
        _runtime,
    ):
        before = path.read_bytes()
        if revoke:
            remove_admin_channel_identity(agent.home_paths.root, "feishu", row["actor_id"])
        status, result = post(server, row, body)
        if revoke:
            assert status == 403 and result["ok"] is False
            assert path.read_bytes() == before
            assert (
                json.loads(input_dependency_path(agent, row["input_id"]).read_text())["status"]
                == "pending"
            )
            assert not agent.conversation_store.wakes.pending()
        else:
            assert status == 200 and result["ok"] is True
            assert json.loads(path.read_text())["status"] == "ready"


def test_existing_c_key_read_does_not_wait_for_original_key_write_lock(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
    from agent_py_agent.agent.user_space import task_credentials as tc

    with original_case(tmp_path, monkeypatch) as (agent, path, row, body, server, _runtime):
        reached = threading.Event()
        results = []
        original = tc._cipher

        def observe(*args, **kwargs):
            reached.set()
            return original(*args, **kwargs)

        monkeypatch.setattr(tc, "_cipher", observe)
        key_path = agent.home_paths.owner_home_dir.joinpath(
            *PRIVATE_INTERACTIONS_OWNER_PARTS, "key.json"
        )
        before = key_path.read_bytes()
        client = threading.Thread(target=lambda: results.append(post(server, row, body)))
        try:
            with locked_json_path(key_path):
                client.start()
                assert reached.wait(2)
                client.join(0.5)
                assert not client.is_alive()
                assert results[0][0] == 200 and results[0][1]["ok"] is True
                assert key_path.read_bytes() == before
        finally:
            client.join(5)
            assert not client.is_alive()
        saved = path.read_bytes()
        assert post(server, row, body)[1]["ok"] is True
        assert path.read_bytes() == saved and key_path.read_bytes() == before
        assert (
            json.loads(input_dependency_path(agent, row["input_id"]).read_text())["status"]
            == "ready"
        )
        assert len(agent.conversation_store.wakes.pending()) == 1


@pytest.mark.parametrize("fault", ["missing", "invalid"])
def test_c_key_missing_after_last_source_check_is_never_rebuilt(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
    from agent_py_agent.agent.user_space import task_credentials as tc

    with original_case(tmp_path, monkeypatch) as (agent, path, row, body, server, _runtime):
        key_path = agent.home_paths.owner_home_dir.joinpath(
            *PRIVATE_INTERACTIONS_OWNER_PARTS, "key.json"
        )
        original = tc._cipher

        def changed(*args, **kwargs):
            if fault == "missing":
                key_path.unlink()
            else:
                key_path.write_text('{"schema_version":"invalid"}')
            return original(*args, **kwargs)

        monkeypatch.setattr(tc, "_cipher", changed)
        before = path.read_bytes()
        assert post(server, row, body)[0] == 403
        if fault == "missing":
            assert not key_path.exists()
        else:
            assert key_path.read_text() == '{"schema_version":"invalid"}'
        assert path.read_bytes() == before
        assert (
            json.loads(input_dependency_path(agent, row["input_id"]).read_text())["status"]
            == "pending"
        )
        assert not agent.conversation_store.wakes.pending()
