"""Regression checks for direct JSON object reading and watch binding adoption."""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import time
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.conversation.workspace_paths import audit_workspace_path
from agent_py_agent.agent.ingestion import harvester as harvester_module
from agent_py_agent.agent.ingestion import watch_state as ws
from agent_py_agent.agent.ingestion.source_adapter import SourceAdapterError, _resolved_script
from agent_py_agent.agent.ingestion.source_binding import audit_source_config_version
from agent_py_agent.agent.ingestion.source_worker import _apply_source_runtime_projection
from agent_py_agent.agent.ingestion.watch_tool import _drain_and_digest


def _open_fd_count() -> int:
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(fd_dir):
            return len(os.listdir(fd_dir))
    return -1


def _report_fields(report: json_io.JsonObjectReadReport) -> tuple[dict[str, object], dict[str, object] | None]:
    first, second = (getattr(report, name) for name in report.__dataclass_fields__)
    return first, second


def _cleanup_watch(owner_home: Path, watch_id: str) -> None:
    harvester_module.stop_harvester(watch_id, owner_home)
    ws.registry.drop(watch_id, owner_home)


@dataclass(frozen=True)
class _SnapshotAdapter:
    """Minimal read-only byte snapshot matching compact_reachability._JsonSnapshot."""

    path: Path
    raw: bytes

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.raw.decode(encoding)

    def __str__(self) -> str:
        return str(self.path)


def _live_polling_collector_worker(connection, owner_home_str: str, watch_id: str) -> None:
    """Separate native collector process using real WatchRegistry, lease, and transaction commit."""
    owner_home = Path(owner_home_str)
    state = ws.registry.get_or_load(owner_home, watch_id)
    assert state is not None
    claimed, lease = harvester_module._claim_harvester_lease(state)
    assert claimed
    clock_start = time.time()
    tick_index = 0
    connection.send({"ready": True, "lease_claimed": claimed})
    try:
        while True:
            command = connection.recv()
            if command == "stop":
                break
            tick_index += 1
            observed_requests: list[str] = []

            def _fetch(request):
                revision = str(request.headers.get("X-Revision", ""))
                observed_requests.append(revision)
                return True, {"revision": revision}, ""

            with patch("time.time", return_value=clock_start + tick_index * 125):
                outcome = harvester_module._locked_harvest_step(
                    state,
                    _fetch,
                    lease_id=lease["lease_id"],
                )
            connection.send(
                {
                    "outcome": outcome,
                    "error_code": state.last_error_code,
                    "binding_revision": state.source_binding_revision_seq,
                    "poll_query_seconds": state.tuning.poll_query_seconds,
                    "window_seconds": state.tuning.window_seconds,
                    "max_candidates_per_pull": state.tuning.max_candidates_per_pull,
                    "commit_id_present": bool(state.harvest_commit_id),
                    "request_revisions": observed_requests,
                }
            )
    finally:
        harvester_module._clear_lease(state, lease["lease_id"])
        ws.registry.drop(watch_id, owner_home)


def test_read_json_object_report_status_eio_and_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "canonical.json"
    target.write_text('{"closed": true, "cursor": 42}', encoding="utf-8")

    # Python 3.14 Path.exists() returns False on status-query OSError; direct read skips exists().
    original_exists = Path.exists
    original_stat = Path.stat
    counts = {"exists_calls": 0, "status_errors": 0}

    def _exists_py314(path: Path, *, follow_symlinks: bool = True) -> bool:
        if path != target:
            return original_exists(path) if follow_symlinks else original_exists(path, follow_symlinks=follow_symlinks)
        counts["exists_calls"] += 1
        try:
            path.stat(follow_symlinks=follow_symlinks)
        except OSError:
            return False
        return True

    def _stat_eio_once(path: Path, *, follow_symlinks: bool = True):
        if path == target and counts["status_errors"] == 0:
            counts["status_errors"] += 1
            raise OSError(errno.EIO, "simulated I/O error")
        return original_stat(path, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "exists", _exists_py314)
        patcher.setattr(Path, "stat", _stat_eio_once)
        report = json_io.read_json_object_report(target, context="watch_state.merge")
    data, error = _report_fields(report)
    assert counts["exists_calls"] == 0
    assert counts["status_errors"] == 0
    assert error is None
    assert data == {"closed": True, "cursor": 42}

    # Confirm the modeled Python 3.14 helper itself suppresses exactly one status EIO if called.
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "exists", _exists_py314)
        patcher.setattr(Path, "stat", _stat_eio_once)
        assert target.exists() is False
    assert counts["exists_calls"] == 1
    assert counts["status_errors"] == 1

    # Direct byte-read EIO and EACCES remain reported with context, path, and fd_delta == 0.
    original_read_text = Path.read_text
    for err_no in (errno.EIO, errno.EACCES):
        read_errors = 0

        def _read_fail(path: Path, *args, **kwargs) -> str:
            nonlocal read_errors
            if path == target:
                read_errors += 1
                raise OSError(err_no, "simulated I/O error")
            return original_read_text(path, *args, **kwargs)

        fd_before = _open_fd_count()
        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "read_text", _read_fail)
            err_report = json_io.read_json_object_report(target, context="watch_state.merge")
        fd_after = _open_fd_count()
        if fd_before >= 0 and fd_after >= 0:
            assert fd_after - fd_before == 0
        err_data, err_info = _report_fields(err_report)
        assert read_errors == 1
        assert err_data == {}
        assert err_info is not None
        assert err_info["context"] == "watch_state.merge"
        assert err_info["path"] == str(target)

    # Genuine missing file (with absent parent directory) stays empty success with no mkdir.
    missing = tmp_path / "absent-parent" / "missing.json"
    missing_data, missing_err = _report_fields(
        json_io.read_json_object_report(missing, context="watch_state.merge")
    )
    assert missing_data == {}
    assert missing_err is None
    assert not missing.parent.exists()

    # Nested JSON string parsing remains preserved.
    nested = tmp_path / "nested.json"
    nested.write_text(json.dumps('{"run_id": "run-1", "cursor": 5}'), encoding="utf-8")
    nested_data, nested_err = _report_fields(
        json_io.read_json_object_report(nested, parse_nested_string=True, context="nested.read")
    )
    assert nested_err is None
    assert nested_data == {"run_id": "run-1", "cursor": 5}

    # Malformed JSON and non-object JSON root preserve structured error reports with ValueError details.
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{broken", encoding="utf-8")
    mal_data, mal_err = _report_fields(
        json_io.read_json_object_report(malformed, context="watch_state.merge")
    )
    assert mal_data == {}
    assert mal_err is not None
    assert mal_err["context"] == "watch_state.merge"
    assert mal_err["path"] == str(malformed)

    non_object = tmp_path / "array.json"
    non_object.write_text("[1, 2, 3]", encoding="utf-8")
    arr_data, arr_err = _report_fields(
        json_io.read_json_object_report(non_object, context="watch_state.merge")
    )
    assert arr_data == {}
    assert arr_err is not None
    assert arr_err["context"] == "watch_state.merge"
    assert arr_err["path"] == str(non_object)
    assert arr_err.get("error_type") == "ValueError" or "list" in str(arr_err)

    # Read-only snapshot adapter without exists() continues working cleanly.
    snapshot = _SnapshotAdapter(target, b'{"thread_id": "th-1"}')
    snap_data, snap_err = _report_fields(json_io.read_json_object_report(snapshot, context="snapshot.read"))
    assert snap_err is None
    assert snap_data == {"thread_id": "th-1"}


def test_persist_state_preserves_durable_facts_under_status_eio_and_rejects_read_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_home = tmp_path / "owner_status_eio"
    state = ws.new_state(
        owner_home,
        "https://records.example.invalid/feed",
        {},
        watch_id="ws-112233aabb",
    )
    ws.persist_state(state)
    try:
        stale = ws.load_state(owner_home, state.watch_id)
        assert stale is not None

        state.closed = True
        state.closed_at = 1000.0
        state.close_reason = "watch_tool_close"
        state.cursor = 200
        state.harvest_commit_id = "committed-position"
        ws.persist_state(state)

        canonical = ws.state_dir(owner_home) / f"{state.watch_id}.json"
        original_stat = Path.stat
        original_exists = Path.exists
        original_read_text = Path.read_text
        counts = {"exists_calls": 0, "status_errors": 0, "read_errors": 0}

        def _exists_py314(path: Path, *, follow_symlinks: bool = True) -> bool:
            if path != canonical:
                return original_exists(path) if follow_symlinks else original_exists(path, follow_symlinks=follow_symlinks)
            counts["exists_calls"] += 1
            try:
                if counts["status_errors"] == 0:
                    counts["status_errors"] += 1
                    raise OSError(errno.EIO, "simulated I/O error")
                original_stat(path, follow_symlinks=follow_symlinks)
                return True
            except (OSError, ValueError):
                return False

        # One status-query EIO under Python 3.14 exists semantics must not reset durable facts.
        stale.audit_objective = "safe metadata update"
        fd_before = _open_fd_count()
        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "exists", _exists_py314)
            ws.persist_state(stale)
        fd_after = _open_fd_count()
        if fd_before >= 0 and fd_after >= 0:
            assert fd_after - fd_before == 0

        assert counts["exists_calls"] == 0
        assert counts["status_errors"] == 0

        after_status = ws.load_state(owner_home, state.watch_id)
        assert after_status is not None
        assert after_status.closed is True
        assert after_status.close_reason == "watch_tool_close"
        assert after_status.cursor == 200
        assert after_status.harvest_commit_id == "committed-position"
        assert after_status.audit_objective == "safe metadata update"

        # Refresh and reopen_on_disk also read directly without calling exists().
        refresh_target = ws.load_state(owner_home, state.watch_id)
        assert refresh_target is not None
        refresh_target.closed = False
        refresh_target.cursor = 0
        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "exists", _exists_py314)
            ws.refresh_scalars_from_disk(refresh_target)
        assert counts["exists_calls"] == 0
        assert refresh_target.closed is True
        assert refresh_target.cursor == 200
        assert refresh_target.harvest_commit_id == "committed-position"

        # Separate single byte-read EIO control raises ordinary OSError and leaves bytes identical.
        stale_again = ws.new_state(
            owner_home,
            "https://records.example.invalid/feed",
            {},
            watch_id="ws-112233aabb",
        )
        before_bytes = canonical.read_bytes()

        def _read_eio_once(path: Path, *args, **kwargs) -> str:
            if path == canonical and counts["read_errors"] == 0:
                counts["read_errors"] += 1
                raise OSError(errno.EIO, "simulated I/O error")
            return original_read_text(path, *args, **kwargs)

        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "exists", _exists_py314)
            patcher.setattr(Path, "read_text", _read_eio_once)
            with pytest.raises(OSError) as exc_info:
                ws.persist_state(stale_again)

        assert type(exc_info.value) is OSError
        assert counts["read_errors"] == 1
        assert counts["exists_calls"] == 0
        assert canonical.read_bytes() == before_bytes
        after_read_err = ws.load_state(owner_home, state.watch_id)
        assert after_read_err is not None
        assert after_read_err.closed is True
        assert after_read_err.close_reason == "watch_tool_close"
        assert after_read_err.cursor == 200
        assert after_read_err.harvest_commit_id == "committed-position"

        # Genuine missing-file initialization works without error.
        fresh = ws.new_state(tmp_path / "owner_missing_init", "https://records.example.invalid/new", {})
        ws.persist_state(fresh)
        assert ws.load_state(fresh.owner_home, fresh.watch_id) is not None
    finally:
        _cleanup_watch(owner_home, state.watch_id)


def test_harvester_commit_read_eio_retains_transaction_intent_and_conflict_stays_cas_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_home = tmp_path / "owner_tx_read_eio"
    src = tmp_path / "events.ndjson"
    src.write_text('{"seq": 1}\n', encoding="utf-8")
    state = ws.new_state(owner_home, src.as_uri(), {})
    ws.persist_state(state)
    tool = type("DummyTool", (), {"_fetch_json": staticmethod(lambda *_a, **_kw: {})})()
    aggregate: dict[str, object] = defaultdict(int)
    try:
        _drain_and_digest(tool, state, aggregate)
        tx = harvester_module._begin_harvest_transaction(state)
        state.cursor += 10
        spool_file = harvester_module.spool_path(state)
        spool_file.parent.mkdir(parents=True, exist_ok=True)
        with spool_file.open("ab") as fh:
            fh.write(b'{"seq": 2}\n')

        canonical = ws.state_dir(owner_home) / f"{state.watch_id}.json"
        before_bytes = canonical.read_bytes()
        marker = harvester_module._harvest_transaction_path(state)
        assert marker.exists()

        original_read_text = Path.read_text
        reads_failed = 0

        def _read_eio_once(path: Path, *args, **kwargs) -> str:
            nonlocal reads_failed
            if path == canonical and reads_failed == 0:
                reads_failed += 1
                raise OSError(errno.EIO, "simulated I/O error")
            return original_read_text(path, *args, **kwargs)

        fd_before = _open_fd_count()
        with monkeypatch.context() as patcher:
            patcher.setattr(Path, "read_text", _read_eio_once)
            with pytest.raises(OSError) as exc_info:
                harvester_module._commit_harvest_transaction(state, tx)
        fd_after = _open_fd_count()
        if fd_before >= 0 and fd_after >= 0:
            assert fd_after - fd_before == 0

        assert type(exc_info.value) is OSError
        assert not isinstance(exc_info.value, ws.WatchSourcePositionConflict)
        assert reads_failed == 1
        assert canonical.read_bytes() == before_bytes
        # Ordinary OSError retains transaction intent so crash recovery can settle cleanly.
        assert marker.exists()

        # Contrast: actual source-position drift raises WatchSourcePositionConflict -> rolled back.
        other = ws.load_state(owner_home, state.watch_id)
        assert other is not None
        prior_pos = (other.harvest_commit_id, other.file_identity, other.cursor, other.line_cursor)
        other.cursor += 5
        other.harvest_commit_id = "other-writer-commit"
        ws.persist_state(other, expected_source_position=prior_pos)

        with pytest.raises(harvester_module._HarvestTransactionUnavailable) as conflict_exc:
            harvester_module._commit_harvest_transaction(state, tx)
        assert isinstance(conflict_exc.value.__cause__, ws.WatchSourcePositionConflict)
        assert not marker.exists()
        assert state.harvest_commit_id == "other-writer-commit"
    finally:
        _cleanup_watch(owner_home, state.watch_id)


@pytest.mark.parametrize("refresh_before_save", [False, True])
def test_source_binding_merge_and_refresh_adopt_envelope_and_poll_interval(
    tmp_path: Path,
    refresh_before_save: bool,
) -> None:
    owner_home = tmp_path / f"owner_binding_{refresh_before_save}"
    state = ws.new_state(
        owner_home,
        "https://records.example.invalid/feed",
        {
            "window_seconds": 900,
            "max_candidates_per_pull": 13,
            "poll_interval_seconds": 2.5,
        },
        watch_id="ws-aabbccddee",
    )
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-test"
    state.owner_id = "owner-test"
    state.source_mode = "adapter"
    adapter_file = audit_workspace_path(state.owner_home, state.audit_root_task_id) / "work/adapter.py"
    adapter_file.parent.mkdir(parents=True)
    adapter_a = (
        b'"""Revision A."""\n'
        b"import json, sys\n"
        b"r = json.load(sys.stdin)\n"
        b'print(json.dumps({"request": r.get("base_request"), "context": {}}))\n'
    )
    adapter_b = adapter_a.replace(b"Revision A", b"Revision B")
    adapter_file.write_bytes(adapter_a)
    state.source_envelope = {
        "mode": "adapter",
        "record_boundary": "adapter_records",
        "request": {"method": "GET"},
        "adapter": {"path": "work/adapter.py", "sha256": sha256(adapter_a).hexdigest()},
        "valid": True,
        "continuation_verified": True,
        "observed_record_count": 7,
    }
    state.source_config_version = audit_source_config_version(
        source_url=state.source_url,
        source_mode=state.source_mode,
        source_envelope=state.source_envelope,
        poll_query_seconds=state.tuning.poll_query_seconds,
    )
    ws.persist_state(state)

    try:
        stale = ws.load_state(state.owner_home, state.watch_id)
        publisher = ws.load_state(state.owner_home, state.watch_id)
        assert stale is not None and publisher is not None
        # Simulate runtime page_limit tuning adjustment on stale collector handle.
        stale.tuning = replace(stale.tuning, page_limit=125)

        new_envelope = deepcopy(state.source_envelope)
        adapter_file.write_bytes(adapter_b)
        new_envelope["adapter"]["sha256"] = sha256(adapter_b).hexdigest()
        projection = {
            "source_id": state.source_id,
            "source_url": state.source_url,
            "source_mode": state.source_mode,
            "source_envelope": new_envelope,
            "source_profile_ref": "work/current.md",
            "document_refs": ["work/current-fields.md"],
            "poll_query_seconds": 120,
        }

        def _publish() -> bool:
            return _apply_source_runtime_projection(
                publisher,
                projection,
                objective="",
                run_prompt="",
                config_version_factory=audit_source_config_version,
            )

        assert _publish() is True
        after_update = ws.load_state(state.owner_home, state.watch_id)
        assert after_update is not None
        assert after_update.source_envelope == new_envelope
        assert after_update.tuning.poll_query_seconds == 120
        _resolved_script(state.owner_home, state.audit_root_task_id, after_update.source_envelope["adapter"])

        if refresh_before_save:
            ws.refresh_scalars_from_disk(stale)
            ws.persist_state(stale)
        else:
            prior = (stale.harvest_commit_id, stale.file_identity, stale.cursor, stale.line_cursor)
            stale.harvest_commit_id = "next-cycle"
            stale.cursor = 1
            ws.persist_state(stale, expected_source_position=prior)

        assert _publish() is False
        observed = ws.load_state(state.owner_home, state.watch_id)
        assert observed is not None
        assert observed.source_config_version == publisher.source_config_version
        assert observed.source_envelope == new_envelope
        assert observed.source_profile_ref == "work/current.md"
        assert observed.document_refs == ["work/current-fields.md"]
        assert observed.source_binding_revision_seq == publisher.source_binding_revision_seq
        assert observed.tuning.poll_query_seconds == 120
        # Unrelated tuning fields must remain preserved on the collector handle.
        assert stale.tuning.window_seconds == 900
        assert stale.tuning.max_candidates_per_pull == 13
        assert stale.tuning.page_limit == 125
        assert stale.tuning.poll_interval_seconds == 2.5
        assert observed.cursor == (0 if refresh_before_save else 1)

        try:
            _resolved_script(state.owner_home, state.audit_root_task_id, observed.source_envelope["adapter"])
        except SourceAdapterError as error:
            pytest.fail(f"Unexpected adapter validation error: {error.code}")

        # Explicit publisher prefer_disk_commit=True path continues replacing with intended C binding.
        adapter_c = adapter_a.replace(b"Revision A", b"Revision C")
        adapter_file.write_bytes(adapter_c)
        envelope_c = deepcopy(new_envelope)
        envelope_c["adapter"]["sha256"] = sha256(adapter_c).hexdigest()
        projection_c = {
            **projection,
            "source_envelope": envelope_c,
            "source_profile_ref": "work/rev-c.md",
            "document_refs": ["work/rev-c-fields.md"],
            "poll_query_seconds": 180,
        }
        assert _apply_source_runtime_projection(
            publisher,
            projection_c,
            objective="",
            run_prompt="",
            config_version_factory=audit_source_config_version,
        )
        after_c = ws.load_state(state.owner_home, state.watch_id)
        assert after_c is not None
        assert after_c.source_envelope == envelope_c
        assert after_c.tuning.poll_query_seconds == 180
        assert after_c.source_profile_ref == "work/rev-c.md"
        assert after_c.document_refs == ["work/rev-c-fields.md"]
        assert after_c.source_binding_revision_seq > observed.source_binding_revision_seq

        # Closed monotonicity remains intact across subsequent stale persists.
        closer = ws.load_state(state.owner_home, state.watch_id)
        assert closer is not None
        closer.closed = True
        closer.closed_at = 2000.0
        closer.close_reason = "watch_tool_close"
        ws.persist_state(closer)
        ws.persist_state(stale)
        final_state = ws.load_state(state.owner_home, state.watch_id)
        assert final_state is not None
        assert final_state.closed is True
        assert final_state.close_reason == "watch_tool_close"
        assert final_state.source_envelope == envelope_c
        assert final_state.tuning.poll_query_seconds == 180
    finally:
        _cleanup_watch(owner_home, state.watch_id)


def test_two_process_native_polling_collector_adopts_republished_binding(tmp_path: Path) -> None:
    owner_home = tmp_path / "owner_live_poll"
    state = ws.new_state(
        owner_home,
        "https://records.example.invalid/feed",
        {"window_seconds": 900, "max_candidates_per_pull": 12},
        watch_id="ws-10aabbccdd",
    )
    state.audit_guarantee = True
    state.audit_root_task_id = "audit-test"
    state.owner_id = "owner-test"
    state.source_mode = "poll"
    state.source_envelope = {
        "mode": "poll",
        "record_boundary": "snapshot",
        "request": {"method": "GET", "headers": {"X-Revision": "A"}},
        "valid": True,
    }
    state.source_config_version = audit_source_config_version(
        source_url=state.source_url,
        source_mode=state.source_mode,
        source_envelope=state.source_envelope,
        poll_query_seconds=state.tuning.poll_query_seconds,
    )
    ws.persist_state(state)

    publisher = ws.registry.get_or_load(state.owner_home, state.watch_id)
    assert publisher is not None
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_live_polling_collector_worker,
        args=(child, str(state.owner_home), state.watch_id),
    )
    process.start()
    child.close()

    try:
        assert parent.poll(15), "collector startup exceeded deadline"
        initial = parent.recv()
        assert initial["ready"] is True
        assert initial["lease_claimed"] is True

        envelope_b = deepcopy(state.source_envelope)
        envelope_b["request"]["headers"]["X-Revision"] = "B"
        projection = {
            "source_id": state.source_id,
            "source_url": state.source_url,
            "source_mode": state.source_mode,
            "source_envelope": envelope_b,
            "source_profile_ref": "work/current.md",
            "document_refs": ["work/current-fields.md"],
            "poll_query_seconds": 120,
        }

        def _publish() -> bool:
            return _apply_source_runtime_projection(
                publisher,
                projection,
                objective="",
                run_prompt="",
                config_version_factory=audit_source_config_version,
            )

        assert _publish() is True

        rows: list[dict[str, object]] = []
        for tick in range(3):
            parent.send("tick")
            assert parent.poll(25), f"collector tick {tick + 1} exceeded deadline"
            row = parent.recv()
            after_tick = ws.load_state(state.owner_home, state.watch_id)
            assert after_tick is not None
            row["disk_envelope_current_after_tick"] = after_tick.source_envelope == envelope_b
            row["disk_poll_seconds_after_tick"] = after_tick.tuning.poll_query_seconds
            row["publisher_reconcile_changed"] = _publish()
            rows.append(row)

        assert [row["request_revisions"] for row in rows] == [["A"], ["B"], ["B"]]
        for row in rows:
            assert row["outcome"] == "ok"
            assert row["error_code"] == ""
            assert row["poll_query_seconds"] == 120
            assert row["window_seconds"] == 900
            assert row["max_candidates_per_pull"] == 12
            assert row["commit_id_present"] is True
            assert row["disk_envelope_current_after_tick"] is True
            assert row["disk_poll_seconds_after_tick"] == 120
            assert row["publisher_reconcile_changed"] is False
    finally:
        try:
            parent.send("stop")
        except OSError:
            pass
        parent.close()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        _cleanup_watch(owner_home, state.watch_id)

    assert process.exitcode == 0
