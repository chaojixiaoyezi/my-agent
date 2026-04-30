from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser


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
