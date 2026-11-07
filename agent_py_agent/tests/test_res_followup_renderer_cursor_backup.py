"""缺失终态、显式审计缺口与备份清单的故障边界；仅操作临时目录。"""
from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import response_renderer as renderer
from agent_py_agent.agent.memory_store import curator_audit_cursor as audit
from agent_py_agent.agent.memory_store.curator_inputs import _audit_input, _collect_audit
from agent_py_agent.agent.user_space.home_backup import (
    create_home_backup_snapshot,
    restore_home_backup_snapshot_checked,
    verify_home_backup_snapshot,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.tests.support.curator_scan_cases import make_audit


def _terminal(path: Path) -> dict:
    response = {"id": path.stem, "ok": True, "status": "done", "response": "canonical result"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "gateway_terminal_request.v1", "id": path.stem,
                                "terminal_response": response}), encoding="utf-8")
    return response


def _required_terminal(path: Path) -> dict:
    reader = renderer.read_gateway_terminal_response_file
    assert "require_exists" in inspect.signature(reader).parameters, "required-terminal diagnostic is missing"
    return reader(path, request_id=path.stem, require_exists=True)


@pytest.mark.parametrize("deleted", [False, True])
def test_required_missing_terminal_does_not_trust_done_projection(tmp_path, deleted, capsys):
    path = tmp_path / "requests/terminal/request.json"
    if deleted:
        expected = _terminal(path)
        assert renderer.read_gateway_terminal_response_file(path) == expected
        path.unlink()
    projection = tmp_path / "requests/done/request.json"
    projection.parent.mkdir(parents=True)
    projection.write_text(json.dumps({"id": "request", "status": "done", "response": "not authoritative"}))
    before = projection.read_bytes()
    result = _required_terminal(path)
    assert result["id"] == "request" and result["ok"] is False
    assert result["status"] == "state_lost"
    assert result["error_code"] == "GATEWAY_TERMINAL_STATE_LOST"
    assert result["user_error"] and not result.get("response")
    assert renderer.print_gateway_response(result) == 2
    assert result["user_error"] in capsys.readouterr().out
    assert not path.exists() and projection.read_bytes() == before


def test_absent_terminal_remains_pending_for_default_reads_and_polls(tmp_path):
    path = tmp_path / "terminal/request.json"
    report = renderer.read_gateway_terminal_envelope_report(path, request_id="request")
    assert report.payload == {} and report.load_error is None
    assert renderer.read_gateway_terminal_response_file(path, request_id="request") == {}
    assert renderer.read_gateway_terminal_response_file_when_ready(
        path, request_id="request", state=renderer.GatewayResponsePollState(),
    ) == {}
    assert not path.parent.exists()


def test_required_existing_terminal_keeps_canonical_validation(tmp_path):
    path = tmp_path / "request.json"
    expected = _terminal(path)
    assert _required_terminal(path) == expected


@pytest.mark.parametrize("fault", ["bad_json", "schema", "outer_id", "inner_id"])
def test_required_corrupt_terminal_is_not_reclassified_as_missing(tmp_path, fault, monkeypatch):
    path = tmp_path / "request.json"
    _terminal(path)
    payload = json.loads(path.read_text())
    if fault == "bad_json":
        path.write_text("{")
    else:
        if fault == "schema":
            payload["schema_version"] = "foreign.v1"
        elif fault == "outer_id":
            payload["id"] = "other"
        else:
            payload["terminal_response"]["id"] = "other"
        path.write_text(json.dumps(payload))
    # 两次真实读取各自生成结束时间；固定时钟以逐字段比较，不放宽错误合同。
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.request_errors.time.time", lambda: 1234.0)
    original = renderer.read_gateway_terminal_response_file(path, request_id="request")
    result = _required_terminal(path)
    assert result == original and result["ok"] is False
    assert result["error_code"] != "GATEWAY_TERMINAL_STATE_LOST"


def _accept_gap(request, *, expected_root, expected_cursor):
    accept = getattr(audit, "accept_missing_audit_cursor_gap", None)
    assert callable(accept), "explicit owner-bound audit gap acceptance is missing"
    return accept(request, _audit_input, expected_root=expected_root, expected_cursor=expected_cursor)


@pytest.mark.parametrize("limit,max_chars", [(2, 20_000), (20, 1)])
def test_explicit_gap_reads_surviving_earliest_shard_without_automatic_rebase(tmp_path, caplog, limit, max_chars):
    root, other = tmp_path / "owner-a/audit", tmp_path / "owner-b/audit"
    paths, rows = make_audit(root)
    make_audit(other)
    cursor = rows[3]["event_id"]
    request = audit.AuditRead(root, cursor, limit, max_chars)
    _collect_audit(root, after_event_id=cursor, limit=limit, max_chars=max_chars)
    paths[0].unlink()
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*.jsonl")}
    strict = _collect_audit(root, after_event_id=cursor, limit=limit, max_chars=max_chars)
    assert strict[0] == [] and strict[1][0]["error_code"] == "AUDIT_CURSOR_MISSING"
    with caplog.at_level(logging.WARNING):
        selected, errors = _accept_gap(request, expected_root=root, expected_cursor=cursor)
    expected_count = 1 if max_chars == 1 else limit
    assert [item.event_id for item in selected] == [row["event_id"] for row in rows[4:4 + expected_count]]
    assert errors == []
    assert "AUDIT_CURSOR_GAP_ACCEPTED" in caplog.text and str(root) in caplog.text
    assert _collect_audit(root, after_event_id=cursor, limit=limit, max_chars=max_chars) == strict
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*.jsonl")} == before
    normal, normal_errors = _collect_audit(root, after_event_id=selected[-1].event_id, limit=2, max_chars=20_000)
    assert normal and normal_errors == []
    other_rows, other_errors = _collect_audit(other, after_event_id=cursor, limit=2, max_chars=20_000)
    assert [item.event_id for item in other_rows] == [row["event_id"] for row in rows[4:6]]
    assert other_errors == []


@pytest.mark.parametrize("mismatch", ["root", "cursor", "empty_cursor"])
def test_operator_gap_confirmation_rejects_owner_or_cursor_mismatch(tmp_path, caplog, mismatch):
    root = tmp_path / "owner-a/audit"
    make_audit(root)
    request = audit.AuditRead(root, "missing", 2, 20_000)
    expected_root = tmp_path / "owner-b/audit" if mismatch == "root" else root
    expected_cursor = "other" if mismatch == "cursor" else "" if mismatch == "empty_cursor" else "missing"
    with pytest.raises(ValueError, match="owner or cursor"):
        _accept_gap(request, expected_root=expected_root, expected_cursor=expected_cursor)
    assert "AUDIT_CURSOR_GAP_ACCEPTED" not in caplog.text


def test_gap_acceptance_does_not_replay_when_cursor_is_still_present(tmp_path, caplog):
    root = tmp_path / "audit"
    _, rows = make_audit(root)
    request = audit.AuditRead(root, rows[3]["event_id"], 2, 20_000)
    expected = audit.collect_audit_window(request, _audit_input)
    assert _accept_gap(request, expected_root=root, expected_cursor=request.target) == expected
    assert "AUDIT_CURSOR_GAP_ACCEPTED" not in caplog.text


def test_operator_acceptance_cannot_hide_surviving_audit_corruption(tmp_path, caplog):
    root = tmp_path / "audit"
    paths, _ = make_audit(root)
    with paths[0].open("ab") as handle:
        handle.write(b"not json\n")
    request = audit.AuditRead(root, "missing", 2, 20_000)
    expected = audit.collect_audit_window(request, _audit_input)
    assert any(error["error_code"] == "AUDIT_READ_FAILED" for error in expected[1])
    assert _accept_gap(request, expected_root=root, expected_cursor="missing") == expected
    assert "AUDIT_CURSOR_GAP_ACCEPTED" not in caplog.text


def test_empty_cursor_is_not_an_operator_gap(tmp_path, caplog):
    root = tmp_path / "audit"
    make_audit(root)
    request = audit.AuditRead(root, "", 2, 20_000)
    with pytest.raises(ValueError, match="owner or cursor"):
        _accept_gap(request, expected_root=root, expected_cursor="")
    assert "AUDIT_CURSOR_GAP_ACCEPTED" not in caplog.text


def _backup_case(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    live = home.owner_memory_dir / "note.md"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("original backup content", encoding="utf-8")
    snapshot = create_home_backup_snapshot(home, reason="synthetic recovery test")
    live.write_text("current live content must survive refusal", encoding="utf-8")
    return home, snapshot.backup_dir, live


@pytest.mark.parametrize("fault", ["missing", "directory", "corrupt_manifest", "missing_checksums", "bad_checksums", "wrong_mode"])
def test_invalid_snapshot_manifest_refuses_checked_restore_without_live_writes(tmp_path, fault):
    home, backup, live = _backup_case(tmp_path)
    path = backup / "manifest.json"
    manifest = json.loads(path.read_text())
    if fault in {"missing", "directory"}:
        path.unlink()
        if fault == "directory":
            path.mkdir()
    elif fault == "corrupt_manifest":
        path.write_text("{")
    else:
        if fault == "missing_checksums":
            manifest.pop("checksums")
        elif fault == "bad_checksums":
            manifest["checksums"] = []
        else:
            manifest["mode"] = "manifest_only"
        path.write_text(json.dumps(manifest))
    before = live.read_bytes()
    verdict = verify_home_backup_snapshot(backup)
    assert verdict.ok is False and verdict.checked == 0
    assert verdict.mismatches == ("manifest.json",)
    with pytest.raises(ValueError, match="拒绝恢复"):
        restore_home_backup_snapshot_checked(home, backup)
    assert live.read_bytes() == before


@pytest.mark.parametrize("fault", ["empty_checksums", "older_subset", "unlisted_payload"])
def test_every_payload_requires_a_manifest_checksum_before_restore(tmp_path, fault):
    home, backup, live = _backup_case(tmp_path)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    relative = str(live.relative_to(home.root))
    if fault == "unlisted_payload":
        relative = "unlisted/payload.txt"
        payload = backup / "files" / relative
        payload.parent.mkdir(parents=True)
        payload.write_text("unverified payload", encoding="utf-8")
    elif fault == "empty_checksums":
        manifest["checksums"] = {}
    else:
        manifest["checksums"].pop(relative)
    manifest_path.write_text(json.dumps(manifest))
    before = live.read_bytes()
    verdict = verify_home_backup_snapshot(backup)
    assert verdict.ok is False and relative in verdict.mismatches
    with pytest.raises(ValueError, match="拒绝恢复"):
        restore_home_backup_snapshot_checked(home, backup)
    assert live.read_bytes() == before
    assert not (home.root / "unlisted/payload.txt").exists()


def test_valid_empty_snapshot_remains_valid(tmp_path):
    backup = tmp_path / "empty-snapshot"
    backup.mkdir()
    (backup / "manifest.json").write_text(json.dumps({"mode": "snapshot", "checksums": {}}))
    verdict = verify_home_backup_snapshot(backup)
    assert verdict.ok is True and verdict.checked == 0 and verdict.mismatches == ()


def test_intact_snapshot_still_restores_the_verified_original_content(tmp_path):
    home, backup, live = _backup_case(tmp_path)
    assert verify_home_backup_snapshot(backup).ok is True
    result = restore_home_backup_snapshot_checked(home, backup)
    assert str(live) in result.restored_paths
    assert live.read_text(encoding="utf-8") == "original backup content"
