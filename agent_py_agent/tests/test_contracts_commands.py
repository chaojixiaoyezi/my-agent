from __future__ import annotations

from agent_py_agent.cli.contracts_commands import _migrate_file


def test_contracts_migrate_reports_bad_input_json_without_writing_output(tmp_path):
    input_path = tmp_path / "contract.json"
    output_path = tmp_path / "migrated.json"
    input_path.write_text("{bad json", encoding="utf-8")

    result = _migrate_file(input_path, output_path, in_place=False)

    assert result["changed"] is False
    assert result["lint_ok"] is False
    assert result["error_codes"] == ["CONTRACT_LOAD_ERROR"]
    assert result["load_error"]["context"] == "cli.contracts.migrate.read"
    assert not output_path.exists()
