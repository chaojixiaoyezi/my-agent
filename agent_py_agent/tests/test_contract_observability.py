from __future__ import annotations

import json
from pathlib import Path


def test_summarize_contract_status_counts_nested_findings(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.contract_status import summarize_contract_status

    report = tmp_path / "run" / "acceptance_report.json"
    report.parent.mkdir()
    report.write_text(
        json.dumps(
            {
                "acceptance": {
                    "findings": [
                        {
                            "code": "ARTIFACT_MISSING",
                            "severity": "hard",
                            "stage_ref": "outputs/report.md",
                            "trace": [{"stage": "acceptance", "ref": "outputs/report.md"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    status = summarize_contract_status(tmp_path)

    assert status.finding_count == 1
    assert status.files_with_findings == 1
    assert status.by_code == {"ARTIFACT_MISSING": 1}
    assert status.by_severity == {"hard": 1}
    assert status.recent_findings[0]["trace"] == [{"stage": "acceptance", "ref": "outputs/report.md"}]


def test_contract_trace_is_bounded() -> None:
    from agent_py_agent.agent.contracts.contract_trace import trace_entry, with_contract_trace

    finding = with_contract_trace(
        {"code": "CHECK_FAILED"},
        (
            trace_entry("one"),
            trace_entry("two"),
            trace_entry("three"),
            trace_entry("four"),
        ),
    )

    assert finding["trace"] == [
        {"stage": "one"},
        {"stage": "two"},
        {"stage": "three"},
    ]


def test_staged_checkpoint_finding_includes_trace(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.staged_checkpoint_acceptance import (
        one_staged_checkpoint_findings,
    )

    path = tmp_path / "source_data.json"
    path.write_text("[]", encoding="utf-8")

    finding = one_staged_checkpoint_findings("source_data.json", tmp_path)[0]

    assert finding["code"] == "STAGED_JSON_NO_ROWS"
    assert finding["trace"] == [
        {
            "stage": "staged_checkpoint_acceptance",
            "ref": "source_data.json",
            "path": str(path),
            "code": "STAGED_JSON_NO_ROWS",
        }
    ]


def test_contracts_cli_status_and_migrate(tmp_path: Path, capsys) -> None:
    from agent_py_agent.cli.parser import build_parser

    report = tmp_path / "acceptance_report.json"
    report.write_text('{"findings":[{"code":"CONTRACT_FAILED","severity":"hard"}]}', encoding="utf-8")
    args = build_parser().parse_args(["contracts", "status", "--root", str(tmp_path), "--json"])

    assert args.func(args) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["by_code"] == {"CONTRACT_FAILED": 1}

    old_contract = tmp_path / "old_contract.json"
    old_contract.write_text('{"version":1,"artifact_path":"outputs/report.md"}', encoding="utf-8")
    migrated = tmp_path / "new_contract.json"
    args = build_parser().parse_args(
        [
            "contracts",
            "migrate",
            "--input",
            str(old_contract),
            "--output",
            str(migrated),
            "--json",
        ]
    )

    assert args.func(args) == 0
    migrate_payload = json.loads(capsys.readouterr().out)
    assert migrate_payload["migrated"][0]["changed"] is True
    assert json.loads(migrated.read_text(encoding="utf-8"))["version"] == 2
