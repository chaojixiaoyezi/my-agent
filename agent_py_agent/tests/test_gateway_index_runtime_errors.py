from __future__ import annotations

"""Runtime error visibility tests for rebuilding the gateway search index."""

import json
from pathlib import Path
from unittest.mock import MagicMock


def _gateway_paths(tmp_path: Path):
    from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

    root = tmp_path / "gateway"
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "state.json",
        heartbeat=root / "heartbeat.json",
        stop_request=root / "stop.request",
        log=root / "gateway.log",
        inbox=root / "requests/pending",
        processing=root / "requests/processing",
        done=root / "requests/done",
        failed=root / "requests/failed",
        responses=root / "responses",
        history=root / "gateway_requests.jsonl",
    )


def _agent() -> MagicMock:
    agent = MagicMock()
    agent.local_store.log_record.return_value = None
    return agent


def test_rebuild_gateway_index_report_keeps_good_records_and_reports_bad_json(tmp_path: Path):
    """Index rebuild should not turn corrupt gateway files into invisible missing records."""
    from agent_py_agent.agent.gateway_parts.queue_service import rebuild_gateway_index_report

    paths = _gateway_paths(tmp_path)
    paths.inbox.mkdir(parents=True)
    paths.responses.mkdir(parents=True)
    paths.history.parent.mkdir(parents=True, exist_ok=True)
    paths.history.write_text(
        "\n".join(
            [
                json.dumps({"id": "hist-good", "kind": "ask", "status": "done"}),
                "{bad history",
            ]
        ),
        encoding="utf-8",
    )
    (paths.inbox / "req-good.json").write_text(
        json.dumps({"id": "req-good", "kind": "ask", "status": "queued"}),
        encoding="utf-8",
    )
    (paths.inbox / "req-bad.json").write_text("{bad request", encoding="utf-8")
    (paths.responses / "resp-bad.json").write_text("{bad response", encoding="utf-8")

    report = rebuild_gateway_index_report(_agent(), paths)

    assert report.indexed_count == 2
    contexts = {str(error.get("context")) for error in report.load_errors}
    assert "gateway.index.history.read" in contexts
    assert "gateway.index.request.read" in contexts
    assert "gateway.index.response.read" in contexts
