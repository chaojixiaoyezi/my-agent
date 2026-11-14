from __future__ import annotations

import errno
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_RUN_EPOCH_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OPEN_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.ingestion import harvester as harvester_module
from agent_py_agent.agent.ingestion import source_worker as source_worker_module
from agent_py_agent.agent.ingestion import watch_state as watch_state_module
from agent_py_agent.agent.ingestion import watch_tool as watch_tool_module
from agent_py_agent.agent.runtime_errors import DataCorruptionError


def _cleanup_owner_watch(owner_home: Path, watch_id: str) -> None:
    harvester_module.stop_harvester(watch_id, owner_home)
    watch_state_module.registry.drop(watch_id, owner_home)


def test_watch_registry_isolates_identical_watch_ids_across_owners(tmp_path: Path) -> None:
    owner_a = tmp_path / "owner_a"
    owner_b = tmp_path / "owner_b"
    shared_watch_id = "ws-0123456789"
    state_a = watch_state_module.new_state(
        owner_a,
        "https://feed.example.invalid/events",
        {},
        watch_id=shared_watch_id,
    )
    state_a.cursor = 11
    state_b = watch_state_module.new_state(
        owner_b,
        "https://feed.example.invalid/events",
        {},
        watch_id=shared_watch_id,
    )
    state_b.cursor = 29

    local_registry = watch_state_module.WatchRegistry()
    local_registry.put(state_a)
    local_registry.put(state_b)

    assert local_registry.get(shared_watch_id, owner_a) is state_a
    assert local_registry.get(shared_watch_id, owner_b) is state_b
    # Ambiguous owner_home=None refuses cross-owner return or drop while 2 owners hold same id.
    assert local_registry.get(shared_watch_id) is None
    local_registry.drop(shared_watch_id)
    assert local_registry.get(shared_watch_id, owner_a) is state_a
    assert local_registry.get(shared_watch_id, owner_b) is state_b

    local_registry.drop(shared_watch_id, owner_a)
    assert local_registry.get(shared_watch_id, owner_a) is None
    assert local_registry.get(shared_watch_id, owner_b) is state_b
    # Unambiguous single-owner legacy fallback continues working.
    assert local_registry.get(shared_watch_id) is state_b
    local_registry.drop(shared_watch_id)
    assert local_registry.get(shared_watch_id, owner_b) is None

    # Hot-cache entry with mismatched internal owner_home is never returned for another owner.
    foreign_key = (
        watch_state_module._canonical_owner_home_key(owner_a),
        shared_watch_id,
    )
    local_registry._states[foreign_key] = state_b
    assert local_registry.get(shared_watch_id, owner_a) is None
    assert local_registry.get_or_load(owner_a, shared_watch_id) is None


def test_load_state_and_watch_tool_enforce_single_segment_id_and_nofollow(tmp_path: Path) -> None:
    owner_a = tmp_path / "owner_a"
    owner_b = tmp_path / "owner_b"
    wid_a = "ws-aaaa111122"
    wid_b = "ws-bbbb333344"

    state_b = watch_state_module.new_state(
        owner_b,
        "https://feed.example.invalid/b",
        {},
        watch_id=wid_b,
    )
    watch_state_module.persist_state(state_b)

    # Traversal and malformed watch_ids return None cleanly.
    for bad_id in ("", "../escape", "sub/file", "a\\b", ".hidden", "ws-1..2"):
        assert not watch_state_module.is_valid_watch_id(bad_id)
        assert watch_state_module.load_state(owner_a, bad_id) is None
        assert watch_state_module.registry.get_or_load(owner_a, bad_id) is None

    # JSON whose internal watch_id disagrees with the requested watch_id must be rejected.
    watch_state_module.state_dir(owner_a).mkdir(parents=True, exist_ok=True)
    mismatched_path = watch_state_module.state_dir(owner_a) / f"{wid_a}.json"
    mismatched_doc = json.loads(
        (watch_state_module.state_dir(owner_b) / f"{wid_b}.json").read_text(encoding="utf-8")
    )
    mismatched_path.write_text(json.dumps(mismatched_doc), encoding="utf-8")
    assert watch_state_module.load_state(owner_a, wid_a) is None

    # Symlink leaf inside watch_state directory must be rejected by nofollow reader.
    if hasattr(os, "symlink"):
        symlink_wid = "ws-cccc555566"
        symlink_path = watch_state_module.state_dir(owner_a) / f"{symlink_wid}.json"
        try:
            symlink_path.symlink_to(watch_state_module.state_dir(owner_b) / f"{wid_b}.json")
        except OSError:
            pass
        else:
            assert watch_state_module.load_state(owner_a, symlink_wid) is None

    # WatchStreamTool rejects malformed watch_id with concrete effect_outcome and next_action.
    fake_agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_a, owner_id="owner-a"),
        config=SimpleNamespace(path_access_mode="normal", path_dangerous_roots=[]),
    )
    tool = watch_tool_module.WatchStreamTool(fake_agent)
    outcome = tool.execute({"action": "status", "watch_id": "../escape"})
    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"
    parsed = json.loads(outcome.output)
    assert parsed["ok"] is False
    assert parsed["effect_outcome"] == "not_started"
    assert str(parsed.get("next_action") or "").strip()


def test_harvester_registry_and_close_watch_isolate_same_watch_id(tmp_path: Path) -> None:
    owner_a = tmp_path / "owner_a"
    owner_b = tmp_path / "owner_b"
    shared_watch_id = "ws-deadbeef01"

    state_a = watch_state_module.new_state(
        owner_a,
        "https://feed.example.invalid/shared",
        {},
        watch_id=shared_watch_id,
    )
    state_b = watch_state_module.new_state(
        owner_b,
        "https://feed.example.invalid/shared",
        {},
        watch_id=shared_watch_id,
    )
    watch_state_module.persist_state(state_a)
    watch_state_module.persist_state(state_b)
    watch_state_module.registry.put(state_a)
    watch_state_module.registry.put(state_b)

    stop_a = threading.Event()
    stop_b = threading.Event()
    ready_a = threading.Event()
    ready_b = threading.Event()

    def _run_worker(stop_evt: threading.Event, ready_evt: threading.Event) -> None:
        ready_evt.set()
        stop_evt.wait(timeout=5.0)

    thread_a = threading.Thread(target=_run_worker, args=(stop_a, ready_a), daemon=True)
    thread_b = threading.Thread(target=_run_worker, args=(stop_b, ready_b), daemon=True)
    thread_a.start()
    thread_b.start()
    assert ready_a.wait(timeout=2.0)
    assert ready_b.wait(timeout=2.0)

    try:
        harvester_module.harvesters.put(
            harvester_module.HarvesterHandle(
                shared_watch_id,
                "lease-a",
                stop_a,
                thread_a,
                owner_home=owner_a,
            )
        )
        harvester_module.harvesters.put(
            harvester_module.HarvesterHandle(
                shared_watch_id,
                "lease-b",
                stop_b,
                thread_b,
                owner_home=owner_b,
            )
        )

        handle_a = harvester_module.harvesters.get_live(shared_watch_id, owner_a)
        handle_b = harvester_module.harvesters.get_live(shared_watch_id, owner_b)
        assert handle_a is not None and handle_a.lease_id == "lease-a"
        assert handle_b is not None and handle_b.lease_id == "lease-b"
        assert harvester_module.harvesters.get_live(shared_watch_id) is None

        # Ambiguous stop_harvester without owner_home does not halt either owner's harvester.
        harvester_module.stop_harvester(shared_watch_id)
        assert not stop_a.is_set()
        assert not stop_b.is_set()

        # Closing owner_a's watch stops ONLY owner_a and leaves owner_b running and cached.
        watch_state_module.close_watch_state(state_a, reason="watch_tool_close")
        assert stop_a.is_set()
        assert not stop_b.is_set()
        assert harvester_module.harvesters.get_live(shared_watch_id, owner_a) is None
        assert harvester_module.harvesters.get_live(shared_watch_id, owner_b) is handle_b
        assert watch_state_module.registry.get(shared_watch_id, owner_a) is None
        assert watch_state_module.registry.get(shared_watch_id, owner_b) is state_b

        # Now only owner_b remains, so legacy single-owner stop_harvester works.
        harvester_module.stop_harvester(shared_watch_id)
        assert stop_b.is_set()
    finally:
        stop_a.set()
        stop_b.set()
        thread_a.join(timeout=2.0)
        thread_b.join(timeout=2.0)
        _cleanup_owner_watch(owner_a, shared_watch_id)
        _cleanup_owner_watch(owner_b, shared_watch_id)


def test_persist_state_and_binding_revision_serialize_without_losing_fields(tmp_path: Path) -> None:
    owner_home = tmp_path / "owner"
    watch_id = "ws-concurrent01"
    url = "https://feed.example.invalid/stream"

    initial = watch_state_module.new_state(owner_home, url, {}, watch_id=watch_id)
    initial.audit_guarantee = True
    initial.owner_id = "owner-1"
    initial.source_id = "src-1"
    initial.audit_root_task_id = "task-audit-1"
    initial.audit_run_epoch = 1
    initial.source_profile_ref = "docs/p-0.md"
    initial.document_refs = ["docs/d-0.md"]
    initial.source_config_version = "sha256:" + "0" * 64
    initial.harvest_commit_id = "txn-0"
    watch_state_module.persist_state(initial)

    harvester_state = watch_state_module.load_state(owner_home, watch_id)
    worker_state = watch_state_module.load_state(owner_home, watch_id)
    assert harvester_state is not None
    assert worker_state is not None

    errors: list[BaseException] = []
    rounds = 15
    barrier = threading.Barrier(2)

    def _harvester_writer() -> None:
        try:
            for idx in range(1, rounds + 1):
                barrier.wait(timeout=5.0)
                harvester_state.cursor = idx * 10
                harvester_state.spool_seq = idx
                harvester_state.totals["spool_candidates"] = idx * 5
                harvester_state.harvest_commit_id = f"txn-{idx}"
                watch_state_module.persist_state(harvester_state)
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    def _binding_writer() -> None:
        try:
            for idx in range(1, rounds + 1):
                barrier.wait(timeout=5.0)
                worker_state.source_profile_ref = f"docs/p-{idx}.md"
                worker_state.document_refs = [] if idx == rounds else [f"docs/d-{idx}.md"]
                worker_state.source_config_version = f"sha256:{idx:064x}"
                watch_state_module.persist_source_binding_revision(worker_state)
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    t1 = threading.Thread(target=_harvester_writer, daemon=True)
    t2 = threading.Thread(target=_binding_writer, daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)
    assert errors == []

    # One final sync from each writer to confirm neither stale instance rolls back the other.
    watch_state_module.persist_state(harvester_state)
    watch_state_module.persist_source_binding_revision(worker_state)

    reloaded = watch_state_module.load_state(owner_home, watch_id)
    assert reloaded is not None
    assert reloaded.harvest_commit_id == f"txn-{rounds}"
    assert reloaded.cursor == rounds * 10
    assert reloaded.spool_seq == rounds
    assert reloaded.totals["spool_candidates"] == rounds * 5
    assert reloaded.source_profile_ref == f"docs/p-{rounds}.md"
    assert reloaded.document_refs == []
    assert reloaded.source_config_version == f"sha256:{rounds:064x}"

    # Concurrent reopen_on_disk also preserves harvest_commit_id and binding revision.
    reloaded.closed = True
    reloaded.closed_at = time.time()
    reloaded.close_reason = "watch_tool_close"
    watch_state_module.persist_state(reloaded)
    reloaded.opened_at += 1.0
    watch_state_module.reopen_on_disk(reloaded)
    after_reopen = watch_state_module.load_state(owner_home, watch_id)
    assert after_reopen is not None
    assert after_reopen.closed is False
    assert after_reopen.harvest_commit_id == f"txn-{rounds}"
    assert after_reopen.source_config_version == f"sha256:{rounds:064x}"


def _make_audit_agent_and_state(
    tmp_path: Path,
    *,
    loader,
    audit_id: str = "audit-root-1",
    epoch: int = 2,
) -> tuple[object, watch_state_module.WatchState]:
    owner_home = tmp_path / "owner_epoch"
    watch_id = watch_state_module.watch_id_for(owner_home, "https://feed.example.invalid/a", f"{audit_id}:e{epoch}")
    state = watch_state_module.new_state(
        owner_home,
        "https://feed.example.invalid/a",
        {},
        watch_id=watch_id,
    )
    state.audit_guarantee = True
    state.owner_id = "owner-1"
    state.source_id = "source-a"
    state.audit_root_task_id = audit_id
    state.audit_run_epoch = epoch
    state.source_task_goal = "Audit source records completely."
    state.source_config_version = "sha256:" + "a" * 64
    watch_state_module.persist_state(state)

    worker_key = audit_source_worker_key(audit_id, watch_id)
    attrs = {
        CONVERSATION_REQUEST_ID_ATTR: audit_id,
        AUDIT_ATTR: True,
        AUDIT_RUN_EPOCH_ATTR: epoch,
        AUDIT_SOURCE_WORKER_ATTR: True,
        AUDIT_SOURCE_ID_ATTR: "source-a",
        AUDIT_SOURCE_OPEN_ATTR: True,
        AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
        AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
        AUDIT_SOURCE_WORKER_KEY_ATTR: worker_key,
    }
    agent = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=owner_home, owner_id="owner-1"),
        conversation_store=SimpleNamespace(
            tasks=SimpleNamespace(
                load=loader,
                thread_for=lambda _tid: SimpleNamespace(thread_id="thread-1"),
                link_report=lambda _th, _tid: (
                    SimpleNamespace(
                        task_id=audit_id,
                        status="active",
                        work_kind="audit",
                        run_epoch=epoch,
                    ),
                    None,
                ),
            )
        ),
        _current_run_params=SimpleNamespace(
            task_id="worker-task-1",
            task_attributes=attrs,
        ),
    )
    return agent, state


@pytest.mark.parametrize(
    "load_error",
    [
        OSError(errno.EIO, "simulated I/O error"),
        DataCorruptionError("simulated json read error"),
    ],
)
def test_transient_epoch_read_error_defers_without_closing_watch(
    tmp_path: Path,
    load_error: Exception,
) -> None:
    def _failing_loader(_task_id: str) -> object:
        raise load_error

    agent, state = _make_audit_agent_and_state(tmp_path, loader=_failing_loader)
    try:
        assert source_worker_module._source_state_matches_current_audit_epoch(agent, state) == "unavailable"

        auth = source_worker_module.authorize_source_worker_action(agent, state)
        assert auth["ok"] is False
        assert auth["error_code"] == "AUDIT_EPOCH_CHECK_UNAVAILABLE"
        assert auth["effect_outcome"] == "not_started"
        assert str(auth.get("next_action") or "").strip()

        _, tool_err = watch_tool_module._source_worker_authority(agent, state)
        assert tool_err is not None
        assert tool_err.error_code == "TOOL_UNAVAILABLE"
        assert tool_err.reported_error_code == "AUDIT_EPOCH_CHECK_UNAVAILABLE"
        err_doc = json.loads(tool_err.output)
        assert err_doc["effect_outcome"] == "not_started"
        assert str(err_doc.get("next_action") or "").strip()

        spool_err = watch_tool_module._spool_authorization_error(auth)
        assert spool_err.error_code == "TOOL_UNAVAILABLE"
        assert spool_err.reported_error_code == "AUDIT_EPOCH_CHECK_UNAVAILABLE"
        spool_doc = json.loads(spool_err.output)
        assert spool_doc["effect_outcome"] == "not_started"
        assert str(spool_doc.get("next_action") or "").strip()

        _audit_id, _wk, preflight = source_worker_module._source_worker_preflight(agent, state)
        assert preflight is not None
        assert preflight["ok"] is False
        assert preflight["state"] == "degraded"
        assert preflight["error_code"] == "AUDIT_EPOCH_CHECK_UNAVAILABLE"
        assert preflight["effect_outcome"] == "not_started"

        summary = source_worker_module.reconcile_audit_source_workers(agent)
        assert summary["degraded"] == 1
        assert int(summary.get("skipped") or 0) == 0
        assert summary["workers"][0]["error_code"] == "AUDIT_EPOCH_CHECK_UNAVAILABLE"
        assert summary["workers"][0]["effect_outcome"] == "not_started"

        reloaded = watch_state_module.load_state(state.owner_home, state.watch_id)
        assert reloaded is not None
        assert state.closed is False
        assert reloaded.closed is False
        assert reloaded.close_reason == ""
    finally:
        _cleanup_owner_watch(state.owner_home, state.watch_id)


def test_confirmed_epoch_mismatch_closes_superseded_watch(tmp_path: Path) -> None:
    agent, state = _make_audit_agent_and_state(
        tmp_path,
        loader=lambda _tid: SimpleNamespace(run_epoch=3),
        epoch=2,
    )
    try:
        assert source_worker_module._source_state_matches_current_audit_epoch(agent, state) == "mismatch"
        summary = source_worker_module.reconcile_audit_source_workers(agent)
        assert summary["skipped"] == 1
        assert summary["workers"][0]["error_code"] == "AUDIT_RUN_EPOCH_MISMATCH"
        reloaded = watch_state_module.load_state(state.owner_home, state.watch_id)
        assert reloaded is not None
        assert reloaded.closed is True
        assert reloaded.close_reason == "audit_run_superseded"
    finally:
        _cleanup_owner_watch(state.owner_home, state.watch_id)


def test_source_worker_follows_watch_state_registry_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_home = tmp_path / "owner_reg"
    state = watch_state_module.new_state(
        owner_home,
        "https://feed.example.invalid/reg",
        {},
        watch_id="ws-live-reg",
    )
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-live-reg"
    state.closed = True
    watch_state_module.persist_state(state)
    task = SimpleNamespace(
        id="run-live-reg",
        attributes={
            AUDIT_ATTR: True,
            AUDIT_RUN_EPOCH_ATTR: 1,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_OPEN_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-live-reg",
            AUDIT_SOURCE_ID_ATTR: state.source_id,
            AUDIT_SOURCE_OWNER_HOME_ATTR: str(owner_home),
            AUDIT_SOURCE_WATCH_ID_ATTR: state.watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key("audit-live-reg", state.watch_id),
        },
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                watch_state_module,
                "registry",
                SimpleNamespace(get_or_load=lambda *_args, **_kwargs: None),
            )
            assert source_worker_module.source_worker_lifecycle_state(task) == "unavailable"
        with monkeypatch.context() as patch:
            patch.setattr(watch_state_module, "registry", watch_state_module.WatchRegistry())
            assert source_worker_module.source_worker_lifecycle_state(task) == "closed"
    finally:
        _cleanup_owner_watch(owner_home, state.watch_id)


def test_harvester_commit_cas_conflict_preserves_cause_after_inline_drain_rewind(tmp_path) -> None:
    from collections import defaultdict
    from types import SimpleNamespace

    import pytest

    from agent_py_agent.agent.ingestion import harvester as harvester_module
    from agent_py_agent.agent.ingestion.watch_tool import _drain_and_digest

    owner_home = tmp_path / "owner_r1_cas"
    src = tmp_path / "events.ndjson"
    src.write_text('{"seq": 1, "level": "info"}\n{"seq": 2, "level": "warn"}\n', encoding="utf-8")
    state = watch_state_module.new_state(owner_home, src.as_uri(), {})
    watch_state_module.persist_state(state)
    tool = SimpleNamespace(_fetch_json=lambda *_args, **_kwargs: {})
    aggregate: dict[str, object] = defaultdict(int)
    try:
        _drain_and_digest(tool, state, aggregate)
        first_size = src.stat().st_size
        assert state.cursor == first_size
        assert state.harvest_commit_id

        tx = harvester_module._begin_harvest_transaction(state)
        assert tx["start"]["file_identity"] == state.file_identity

        # Simulate atomic rotation to a smaller file committed inline before harvest commits.
        rotated = tmp_path / "events.ndjson.next"
        rotated.write_text('{"seq": 9}\n', encoding="utf-8")
        rotated.replace(src)

        inline_state = watch_state_module.load_state(owner_home, state.watch_id)
        _drain_and_digest(tool, inline_state, aggregate)
        rotated_size = src.stat().st_size
        assert rotated_size < first_size
        assert inline_state.cursor == rotated_size

        state.cursor += 16
        spool_file = harvester_module.spool_path(state)
        spool_file.parent.mkdir(parents=True, exist_ok=True)
        with spool_file.open("ab") as fh:
            fh.write(b'{"uncommitted": true}\n')
        assert spool_file.stat().st_size > int(tx["start"]["spool_bytes"])

        with pytest.raises(harvester_module._HarvestTransactionUnavailable) as exc_info:
            harvester_module._commit_harvest_transaction(state, tx)
        assert isinstance(exc_info.value.__cause__, watch_state_module.WatchSourcePositionConflict)
        assert isinstance(exc_info.value.__cause__, OSError)
        assert "conflict" in str(exc_info.value.__cause__)

        assert not harvester_module._harvest_transaction_path(state).exists()
        assert spool_file.stat().st_size == int(tx["start"]["spool_bytes"])
        assert state.harvest_commit_id == inline_state.harvest_commit_id
        assert state.cursor == rotated_size

        persisted = watch_state_module.load_state(owner_home, state.watch_id)
        assert persisted.harvest_commit_id == inline_state.harvest_commit_id
        assert persisted.file_identity == inline_state.file_identity
        assert persisted.cursor == rotated_size

        next_tx = harvester_module._begin_harvest_transaction(persisted)
        assert next_tx["start"]["cursor"] == rotated_size
        assert next_tx["start"]["file_identity"] == inline_state.file_identity
        harvester_module._commit_harvest_transaction(persisted, next_tx)
        assert not harvester_module._harvest_transaction_path(persisted).exists()
    finally:
        _cleanup_owner_watch(owner_home, state.watch_id)


def test_harvester_recover_pending_detects_file_identity_drift(tmp_path) -> None:
    import pytest

    from agent_py_agent.agent.ingestion import harvester as harvester_module

    owner_home = tmp_path / "owner_r1_identity_drift"
    src = tmp_path / "events.ndjson"
    src.write_text('{"seq": 1}\n', encoding="utf-8")
    state = watch_state_module.new_state(owner_home, src.as_uri(), {})
    state.file_identity = "dev1:ino1"
    state.cursor = 12
    state.line_cursor = 1
    state.harvest_commit_id = "commit-1"
    watch_state_module.persist_state(state)
    try:
        tx = harvester_module._begin_harvest_transaction(state)
        assert tx["start"]["file_identity"] == "dev1:ino1"

        drifted = watch_state_module.load_state(owner_home, state.watch_id)
        prior_pos = (
            drifted.harvest_commit_id,
            drifted.file_identity,
            drifted.cursor,
            drifted.line_cursor,
        )
        drifted.file_identity = "dev1:ino2"
        watch_state_module.persist_state(drifted, expected_source_position=prior_pos)

        with pytest.raises(
            harvester_module._HarvestTransactionUnavailable,
            match="moved outside harvest transaction",
        ):
            harvester_module._recover_pending_harvest(state)

        with pytest.raises(harvester_module._HarvestTransactionUnavailable) as exc_info:
            harvester_module._commit_harvest_transaction(state, tx)
        assert isinstance(exc_info.value.__cause__, OSError)
        assert not harvester_module._harvest_transaction_path(state).exists()
        assert state.file_identity == "dev1:ino2"

        reloaded = watch_state_module.load_state(owner_home, state.watch_id)
        next_tx = harvester_module._begin_harvest_transaction(reloaded)
        assert next_tx["start"]["file_identity"] == "dev1:ino2"
        harvester_module._commit_harvest_transaction(reloaded, next_tx)
        assert not harvester_module._harvest_transaction_path(reloaded).exists()
    finally:
        _cleanup_owner_watch(owner_home, state.watch_id)


def test_drain_and_digest_cas_conflict_and_error_path_preserve_commit_continuity(tmp_path) -> None:
    from collections import defaultdict
    from types import SimpleNamespace

    import pytest

    from agent_py_agent.agent.ingestion.watch_tool import _drain_and_digest

    owner_home = tmp_path / "owner_r1_drain_cas"
    src = tmp_path / "events.ndjson"
    src.write_text('{"seq": 1}\n', encoding="utf-8")
    state = watch_state_module.new_state(owner_home, src.as_uri(), {})
    watch_state_module.persist_state(state)
    tool = SimpleNamespace(_fetch_json=lambda *_args, **_kwargs: {})
    aggregate: dict[str, object] = defaultdict(int)
    try:
        _drain_and_digest(tool, state, aggregate)
        commit_1 = state.harvest_commit_id
        assert commit_1

        stale = watch_state_module.load_state(owner_home, state.watch_id)
        with src.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 2}\n')
        fresh = watch_state_module.load_state(owner_home, state.watch_id)
        _drain_and_digest(tool, fresh, aggregate)
        commit_2 = fresh.harvest_commit_id
        assert commit_2 != commit_1

        with src.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 3}\n')
        with pytest.raises(OSError, match="conflict"):
            _drain_and_digest(tool, stale, aggregate)
        assert stale.harvest_commit_id == commit_2
        assert stale.cursor == fresh.cursor

        persisted = watch_state_module.load_state(owner_home, state.watch_id)
        assert persisted.harvest_commit_id == commit_2
        assert persisted.cursor == fresh.cursor

        # Second inline drain on the same handle must move forward from winner's cursor.
        with src.open("a", encoding="utf-8") as fh:
            fh.write('{"seq": 4}\n')
        _drain_and_digest(tool, stale, aggregate)
        commit_3 = stale.harvest_commit_id
        assert commit_3 != commit_2
        assert stale.cursor == src.stat().st_size > fresh.cursor

        src.unlink()
        error_digest = _drain_and_digest(tool, stale, aggregate)
        assert error_digest is None
        after_err = watch_state_module.load_state(owner_home, state.watch_id)
        assert after_err.harvest_commit_id == commit_3
        assert after_err.totals["http_errors"] >= 1
        assert after_err.last_error
    finally:
        _cleanup_owner_watch(owner_home, state.watch_id)


def test_single_writer_truncation_feedback_rewind_and_stale_writer_isolation(tmp_path) -> None:
    from agent_py_agent.agent.ingestion import watch_feedback
    from agent_py_agent.agent.ingestion.watch_state import state_dir

    owner_home = tmp_path / "owner_r1_rewind"
    src = tmp_path / "events.ndjson"
    src.write_text('{"seq": 1}\n', encoding="utf-8")
    state = watch_state_module.new_state(owner_home, src.as_uri(), {})
    state.harvest_commit_id = "commit-1"
    state.file_identity = "dev1:ino1"
    state.cursor = 64
    state.line_cursor = 4
    watch_state_module.persist_state(state)
    inbox_path = state_dir(owner_home) / f"{state.watch_id}.feedback.ndjson"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text('{"kind": "note", "text": "keep"}\n{"kind": "note", "text": "more"}\n', encoding="utf-8")
    try:
        watch_feedback.consume_feedback_inbox(state, 1000.0)
        initial_fb_offset = state.feedback_offset
        assert initial_fb_offset > 0
        watch_state_module.persist_state(state)

        # Truncate source progress + feedback inbox on a single active writer without CAS args.
        inbox_path.write_text('{"kind": "note"}\n', encoding="utf-8")
        watch_feedback.consume_feedback_inbox(state, 1001.0)
        assert state.feedback_offset == inbox_path.stat().st_size
        assert state.feedback_offset < initial_fb_offset
        state.cursor = 16
        state.line_cursor = 1
        watch_state_module.persist_state(state)

        reloaded = watch_state_module.load_state(owner_home, state.watch_id)
        assert reloaded.cursor == 16
        assert reloaded.line_cursor == 1
        assert reloaded.feedback_offset == inbox_path.stat().st_size

        # Now let a stale snapshot try ordinary persist after another writer rotates.
        stale = watch_state_module.load_state(owner_home, state.watch_id)
        newer = watch_state_module.load_state(owner_home, state.watch_id)
        prior_pos = (newer.harvest_commit_id, newer.file_identity, newer.cursor, newer.line_cursor)
        newer.harvest_commit_id = "commit-2"
        newer.file_identity = "dev1:ino2"
        newer.cursor = 8
        newer.line_cursor = 1
        watch_state_module.persist_state(newer, expected_source_position=prior_pos)

        stale.audit_objective = "updated-objective"
        stale.cursor = 99
        watch_state_module.persist_state(stale)

        final_state = watch_state_module.load_state(owner_home, state.watch_id)
        assert final_state.harvest_commit_id == "commit-2"
        assert final_state.file_identity == "dev1:ino2"
        assert final_state.cursor == 8
        assert final_state.line_cursor == 1
        assert final_state.audit_objective == "updated-objective"
    finally:
        _cleanup_owner_watch(owner_home, state.watch_id)


def test_refresh_scalars_from_disk_and_binding_revision_across_source_rotation(tmp_path) -> None:
    owner_home = tmp_path / "owner_r1_refresh"
    src = tmp_path / "events.ndjson"
    src.write_text('{"seq": 1}\n', encoding="utf-8")
    state = watch_state_module.new_state(owner_home, src.as_uri(), {})
    state.harvest_commit_id = "commit-1"
    state.file_identity = "dev1:ino1"
    state.cursor = 80
    state.line_cursor = 5
    state.feedback_offset = 60
    state.source_profile_ref = "profile-v1"
    watch_state_module.persist_state(state)
    try:
        pull_view = watch_state_module.load_state(owner_home, state.watch_id)
        binding_view = watch_state_module.load_state(owner_home, state.watch_id)
        writer = watch_state_module.load_state(owner_home, state.watch_id)

        prior_pos = (writer.harvest_commit_id, writer.file_identity, writer.cursor, writer.line_cursor)
        writer.harvest_commit_id = "commit-2"
        writer.file_identity = "dev1:ino2"
        writer.cursor = 12
        writer.line_cursor = 1
        writer.feedback_offset = 10
        watch_state_module.persist_state(writer, expected_source_position=prior_pos)

        watch_state_module.refresh_scalars_from_disk(pull_view)
        assert pull_view.harvest_commit_id == "commit-2"
        assert pull_view.file_identity == "dev1:ino2"
        assert pull_view.cursor == 12
        assert pull_view.line_cursor == 1
        assert pull_view.feedback_offset == 10

        watch_state_module.persist_state(pull_view)
        after_pull_persist = watch_state_module.load_state(owner_home, state.watch_id)
        assert after_pull_persist.cursor == 12
        assert after_pull_persist.file_identity == "dev1:ino2"

        # Same-generation feedback rewind must also propagate via refresh_scalars_from_disk.
        writer.feedback_offset = 0
        watch_state_module.persist_state(writer)
        watch_state_module.refresh_scalars_from_disk(pull_view)
        assert pull_view.feedback_offset == 0

        # A stale binding revision must publish new profile metadata without reverting rotated cursors.
        binding_view.source_profile_ref = "profile-v2"
        watch_state_module.persist_source_binding_revision(binding_view)
        final_state = watch_state_module.load_state(owner_home, state.watch_id)
        assert final_state.source_profile_ref == "profile-v2"
        assert final_state.harvest_commit_id == "commit-2"
        assert final_state.file_identity == "dev1:ino2"
        assert final_state.cursor == 12
        assert final_state.line_cursor == 1
        assert final_state.feedback_offset == 0
    finally:
        _cleanup_owner_watch(owner_home, state.watch_id)


def _open_fd_count() -> int:
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(fd_dir):
            return len(os.listdir(fd_dir))
    return -1


@pytest.mark.parametrize("writer", ["metadata", "binding", "source_commit"])
def test_disk_merge_read_error_preserves_canonical_snapshot_for_all_writers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    owner_home = tmp_path / f"owner_r2_merge_read_{writer}"
    state = watch_state_module.new_state(
        owner_home,
        "https://feed.example.invalid/records",
        {},
    )
    watch_state_module.persist_state(state)
    try:
        stale = watch_state_module.load_state(owner_home, state.watch_id)
        assert stale is not None
        prior = (stale.harvest_commit_id, stale.file_identity, stale.cursor, stale.line_cursor)

        state.cursor = 23
        state.harvest_commit_id = "committed-current"
        state.closed = True
        state.closed_at = 123.0
        state.close_reason = "watch_tool_close"
        watch_state_module.persist_state(state)

        path = watch_state_module.state_dir(owner_home) / f"{state.watch_id}.json"
        before = path.read_bytes()
        original_read_text = Path.read_text
        reads_failed = 0

        def fail_once(self: Path, *args, **kwargs) -> str:
            nonlocal reads_failed
            if self == path and reads_failed == 0:
                reads_failed += 1
                raise OSError(errno.EIO, "simulated I/O error")
            return original_read_text(self, *args, **kwargs)

        fd_before = _open_fd_count()
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", fail_once)
            with pytest.raises(OSError) as exc_info:
                if writer == "metadata":
                    stale.audit_objective = "metadata revision"
                    watch_state_module.persist_state(stale)
                elif writer == "binding":
                    stale.source_profile_ref = "profile-revision"
                    watch_state_module.persist_source_binding_revision(stale)
                else:
                    stale.cursor = 99
                    stale.harvest_commit_id = "stale-candidate"
                    watch_state_module.persist_state(stale, expected_source_position=prior)
        fd_after = _open_fd_count()
        if fd_before >= 0 and fd_after >= 0:
            assert fd_after - fd_before == 0

        assert type(exc_info.value) is OSError
        assert reads_failed == 1
        assert path.read_bytes() == before

        after = watch_state_module.load_state(owner_home, state.watch_id)
        assert after is not None
        assert after.closed is True
        assert after.cursor == 23
        assert after.harvest_commit_id == "committed-current"

        # Subsequent healthy calls preserve monotonic closed/progress facts or report CAS conflict.
        if writer == "metadata":
            watch_state_module.persist_state(stale)
            merged = watch_state_module.load_state(owner_home, state.watch_id)
            assert merged is not None
            assert merged.closed is True
            assert merged.cursor == 23
            assert merged.harvest_commit_id == "committed-current"
            assert merged.audit_objective == "metadata revision"
        elif writer == "binding":
            watch_state_module.persist_source_binding_revision(stale)
            merged = watch_state_module.load_state(owner_home, state.watch_id)
            assert merged is not None
            assert merged.closed is True
            assert merged.cursor == 23
            assert merged.harvest_commit_id == "committed-current"
            assert merged.source_profile_ref == "profile-revision"
        else:
            with pytest.raises(watch_state_module.WatchSourcePositionConflict):
                watch_state_module.persist_state(stale, expected_source_position=prior)
            merged = watch_state_module.load_state(owner_home, state.watch_id)
            assert merged is not None
            assert merged.cursor == 23
            assert merged.harvest_commit_id == "committed-current"
    finally:
        _cleanup_owner_watch(owner_home, state.watch_id)


def test_disk_merge_corrupt_json_raises_oserror_and_preserves_bytes(tmp_path: Path) -> None:
    owner_home = tmp_path / "owner_r2_merge_corrupt"
    state = watch_state_module.new_state(
        owner_home,
        "https://feed.example.invalid/corrupt",
        {},
    )
    watch_state_module.persist_state(state)
    path = watch_state_module.state_dir(owner_home) / f"{state.watch_id}.json"
    corrupt_bytes = b"{not-valid-json"
    path.write_bytes(corrupt_bytes)
    try:
        fd_before = _open_fd_count()
        with pytest.raises(OSError) as exc_info:
            state.audit_objective = "should not replace corrupt file"
            watch_state_module.persist_state(state)
        fd_after = _open_fd_count()
        if fd_before >= 0 and fd_after >= 0:
            assert fd_after - fd_before == 0
        assert type(exc_info.value) is OSError
        assert path.read_bytes() == corrupt_bytes
    finally:
        _cleanup_owner_watch(owner_home, state.watch_id)
