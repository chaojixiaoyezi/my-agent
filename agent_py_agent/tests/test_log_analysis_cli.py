from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.log_analysis.config import LogAnalysisConfig
from agent_py_agent.agent.log_analysis.storage import LocalLogStore
from agent_py_agent.cli import logs as logs_cli


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fixture_path() -> Path:
    return _project_root() / "validation" / "security_fixtures" / "security_alert_v1.jsonl"


def _run_cli_json(capsys, *argv: str) -> tuple[int, dict]:
    parser = build_parser()
    args = parser.parse_args([*argv, "--json"])
    code = args.func(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def _use_log_config(monkeypatch, *, default_limit: int, max_limit: int) -> None:
    monkeypatch.setattr(
        logs_cli,
        "load_log_analysis_config",
        lambda: LogAnalysisConfig(query_default_limit=default_limit, query_max_limit=max_limit),
    )


def _populate_query_store(root: Path, *, count: int = 6, attacker_ip: str = "198.51.100.77") -> LocalLogStore:
    store = LocalLogStore(root)
    store.upsert_events(
        {
            "event_id": f"evt-cli-{index}",
            "event_time": f"2026-04-30T10:{index:02d}:00Z",
            "source_id": "cli-limit",
            "alert_type": "web_attack",
            "attacker_ip": attacker_ip,
            "victim_ip": "10.0.0.5",
        }
        for index in range(count)
    )
    return store


def test_logs_status_json_reports_default_disabled_without_worker(capsys):
    code, payload = _run_cli_json(capsys, "logs", "status")

    assert code == 0
    assert payload["module"] == "log_analysis"
    assert payload["state"] == "disabled"
    assert payload["enabled"] is False
    assert payload["capability_level"] == "L0"
    assert payload["heavy_dependencies_loaded"] is False
    assert payload["config"]["effective"]["worker_enabled"] is False
    assert payload["data_dir"].endswith(str(Path("agent_py_agent") / "data" / "log_analysis"))
    assert isinstance(payload["config"]["warnings"], list)


def test_logs_ingest_fixture_uses_cli_root_and_source_id(tmp_path, capsys):
    root = tmp_path / "logs"

    code, payload = _run_cli_json(
        capsys,
        "logs",
        "ingest",
        str(_fixture_path()),
        "--root",
        str(root),
        "--source-id",
        "cli-fixture",
        "--format",
        "jsonl",
    )

    result = payload["result"]
    assert code == 0
    assert payload["ok"] is True
    assert payload["root"] == str(root)
    assert result["source_id"] == "cli-fixture"
    assert result["parsed_count"] == 3
    assert result["stored_count"] == 3
    assert Path(result["manifest_path"]).exists()
    assert Path(result["checkpoint_path"]).exists()


def test_logs_query_fixture_uses_default_limit_and_returns_rows(tmp_path, capsys):
    root = tmp_path / "logs"
    ingest_code, _ = _run_cli_json(
        capsys,
        "logs",
        "ingest",
        str(_fixture_path()),
        "--root",
        str(root),
        "--source-id",
        "query-fixture",
    )
    assert ingest_code == 0

    code, payload = _run_cli_json(
        capsys,
        "logs",
        "query",
        "--root",
        str(root),
        "--start-time",
        "2026-04-30T00:00:00Z",
        "--end-time",
        "2026-04-30T23:59:59Z",
    )

    result = payload["result"]
    assert code == 0
    assert payload["limit"] == 100
    assert result["row_count"] == 3
    assert result["truncated"] is False
    assert len(result["preview_rows"]) == 3


def test_logs_query_uses_configured_default_limit(tmp_path, capsys, monkeypatch):
    _use_log_config(monkeypatch, default_limit=2, max_limit=5)
    root = tmp_path / "logs"
    _populate_query_store(root, count=6)

    code, payload = _run_cli_json(
        capsys,
        "logs",
        "query",
        "--root",
        str(root),
        "--start-time",
        "2026-04-30T00:00:00Z",
        "--end-time",
        "2026-04-30T23:59:59Z",
    )

    result = payload["result"]
    assert code == 0
    assert payload["limit"] == 2
    assert payload["limit_warnings"] == []
    assert result["parameters"]["limit"] == 2
    assert result["row_count"] == 6
    assert result["truncated"] is True
    assert len(result["preview_rows"]) == 2


def test_logs_query_truncates_requested_limit_to_configured_max(tmp_path, capsys, monkeypatch):
    _use_log_config(monkeypatch, default_limit=2, max_limit=4)
    root = tmp_path / "logs"
    _populate_query_store(root, count=6)

    code, payload = _run_cli_json(
        capsys,
        "logs",
        "query",
        "--root",
        str(root),
        "--start-time",
        "2026-04-30T00:00:00Z",
        "--end-time",
        "2026-04-30T23:59:59Z",
        "--limit",
        "10",
    )

    result = payload["result"]
    assert code == 0
    assert payload["limit"] == 4
    assert [warning["field_name"] for warning in payload["limit_warnings"]] == ["limit"]
    assert result["parameters"]["limit"] == 4
    assert result["row_count"] == 6
    assert result["truncated"] is True
    assert len(result["preview_rows"]) == 4


def test_logs_hunt_and_trace_use_configured_max_limit(tmp_path, capsys, monkeypatch):
    _use_log_config(monkeypatch, default_limit=2, max_limit=3)
    root = tmp_path / "logs"
    store = _populate_query_store(root, count=6)
    store.upsert_case(
        {
            "case_id": "case-1",
            "title": "limit case",
            "attributes": {"attacker_ip": ["198.51.100.77"]},
        }
    )

    hunt_code, hunt_payload = _run_cli_json(
        capsys,
        "logs",
        "hunt-ip",
        "198.51.100.77",
        "--root",
        str(root),
        "--role",
        "attacker",
        "--start-time",
        "2026-04-30T00:00:00Z",
        "--end-time",
        "2026-04-30T23:59:59Z",
        "--limit",
        "10",
    )
    trace_code, trace_payload = _run_cli_json(
        capsys,
        "logs",
        "trace-case",
        "case-1",
        "--root",
        str(root),
        "--start-time",
        "2026-04-30T00:00:00Z",
        "--end-time",
        "2026-04-30T23:59:59Z",
        "--limit",
        "10",
    )

    assert hunt_code == 0
    assert hunt_payload["limit"] == 3
    assert hunt_payload["result"]["parameters"]["limit"] == 3
    assert len(hunt_payload["result"]["preview_rows"]) == 3

    assert trace_code == 0
    assert trace_payload["limit"] == 3
    first_query = trace_payload["result"]["queries"][0]
    assert first_query["parameters"]["limit"] == 3
    assert len(first_query["preview_rows"]) == 3
