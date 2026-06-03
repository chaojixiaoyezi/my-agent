from __future__ import annotations

"""adapter late response tracking tests."""

import json
import time
from pathlib import Path

from agent_py_agent.agent.gateway_parts.adapter import _record_late_pending, check_late_responses
from agent_py_agent.agent.gateway_parts.adapter_late import check_late_responses_report
from agent_py_agent.agent.gateway_parts.paths import AdapterPaths


def _make_adapter_paths(tmp_path):
    return AdapterPaths(
        root=tmp_path / "adapter",
        inbox=tmp_path / "adapter" / "inbox",
        processing=tmp_path / "adapter" / "processing",
        done=tmp_path / "adapter" / "done",
        failed=tmp_path / "adapter" / "failed",
        outbox=tmp_path / "adapter" / "outbox",
    )


def test_late_pending_recorded_on_timeout(tmp_path):
    paths = _make_adapter_paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)

    _record_late_pending(paths, "req-123", 30.0)

    late_path = paths.root / "late_pending.jsonl"
    assert late_path.exists()
    lines = late_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["request_id"] == "req-123"
    assert entry["original_timeout"] == 30.0
    assert entry["checked"] is False
    assert "timeout_at" in entry


def test_check_late_responses_empty_when_no_file(tmp_path):
    paths = _make_adapter_paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)

    results = check_late_responses(paths)
    assert results == []


def test_check_late_responses_finds_arrival(tmp_path):
    paths = _make_adapter_paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)

    # Write a late pending entry
    late_path = paths.root / "late_pending.jsonl"
    late_path.write_text(
        json.dumps({"request_id": "req-456", "timeout_at": time.time(), "original_timeout": 30.0, "checked": False}) + "\n",
        encoding="utf-8",
    )

    # Create a mock gateway response file
    gateway_responses_dir = paths.root.parent / "gateway" / "responses"
    gateway_responses_dir.mkdir(parents=True, exist_ok=True)
    response_path = gateway_responses_dir / "req-456.json"
    response_path.write_text(
        json.dumps({"id": "req-456", "ok": True, "status": "done", "response": "late response"}),
        encoding="utf-8",
    )

    results = check_late_responses(paths)
    assert len(results) == 1
    assert results[0]["request_id"] == "req-456"
    assert results[0]["checked"] is True
    assert "late_response_at" in results[0]


def test_check_late_responses_cleans_index(tmp_path):
    paths = _make_adapter_paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)

    # Write multiple entries
    late_path = paths.root / "late_pending.jsonl"
    entries = [
        {"request_id": "req-1", "timeout_at": time.time(), "original_timeout": 30.0, "checked": False},
        {"request_id": "req-2", "timeout_at": time.time(), "original_timeout": 30.0, "checked": False},
    ]
    late_path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )

    # Only req-1 has a response
    gateway_responses_dir = paths.root.parent / "gateway" / "responses"
    gateway_responses_dir.mkdir(parents=True, exist_ok=True)
    (gateway_responses_dir / "req-1.json").write_text(
        json.dumps({"id": "req-1", "ok": True}),
        encoding="utf-8",
    )

    results = check_late_responses(paths)
    assert len(results) == 1
    assert results[0]["request_id"] == "req-1"

    # Index should only contain unchecked entries
    remaining_lines = late_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(remaining_lines) == 1
    remaining = json.loads(remaining_lines[0])
    assert remaining["request_id"] == "req-2"
    assert remaining["checked"] is False


def test_check_late_responses_report_keeps_bad_jsonl_visible(tmp_path):
    paths = _make_adapter_paths(tmp_path)
    paths.root.mkdir(parents=True, exist_ok=True)
    late_path = paths.root / "late_pending.jsonl"
    late_path.write_text(
        "{bad json\n"
        + json.dumps({"request_id": "req-ok", "timeout_at": time.time(), "original_timeout": 30.0, "checked": False})
        + "\n",
        encoding="utf-8",
    )

    report = check_late_responses_report(paths)

    assert report.results == []
    assert report.load_errors
    assert report.load_errors[0]["context"] == "gateway.adapter_late_pending.read"
    assert "{bad json" in late_path.read_text(encoding="utf-8")
