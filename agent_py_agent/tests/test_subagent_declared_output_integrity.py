"""Declared outputs are expectations, never instructions to fabricate files."""

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.subagents.models import SubAgentParsedOutput
from agent_py_agent.agent.subagents.result_structured import _normalized_structured_artifacts


def test_one_real_artifact_does_not_fill_other_declared_outputs(tmp_path: Path) -> None:
    output = tmp_path / "output"
    actual = output / "samples" / "normal_policy.json"
    actual.parent.mkdir(parents=True)
    actual.write_text('{"policy": "real"}\n', encoding="utf-8")
    missing = [
        output / "samples" / "normal_expenses.csv",
        output / "samples" / "mixed_data.csv",
        output / "samples" / "empty.csv",
        output / "samples" / "broken.json",
        output / "samples" / "missing_headers.csv",
        output / "tests" / "test_trip_verify.py",
        output / "README.md",
    ]
    task = SimpleNamespace(
        id="subagent-1",
        root_id="task-1",
        task_workspace_dir=str(tmp_path),
        agent_run_workspace_dir="",
        task_dir="",
        allowed_write_roots=[str(output)],
        locked_files=[],
        attributes={
            "output_files": [str(actual), *(str(path) for path in missing)],
            "run_workspace": {"output_dir": str(output)},
        },
    )
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        parse_error="",
        status="DONE",
        artifacts=[{"path": str(actual), "kind": "json", "summary": "真实策略"}],
    )

    normalized = _normalized_structured_artifacts(task, parsed)

    assert [item["path"] for item in normalized["artifacts"]] == [str(actual)]
    assert actual.read_text(encoding="utf-8") == '{"policy": "real"}\n'
    assert all(not path.exists() for path in missing)
    refs = task.attributes["artifact_registry_refs"]
    assert [item["path"] for item in refs] == [str(actual)]


def test_missing_declared_output_is_not_recovered_by_filename_search(tmp_path: Path) -> None:
    output = tmp_path / "output"
    declared = output / "report.md"
    lookalike = tmp_path / "work" / "nested" / "report.md"
    lookalike.parent.mkdir(parents=True)
    lookalike.write_text("unrelated child-local report\n", encoding="utf-8")
    task = SimpleNamespace(
        id="subagent-1",
        root_id="task-1",
        task_workspace_dir=str(tmp_path),
        agent_run_workspace_dir="",
        task_dir="",
        allowed_write_roots=[str(output)],
        locked_files=[],
        attributes={
            "output_files": [str(declared)],
            "run_workspace": {"output_dir": str(output)},
        },
    )
    parsed = SubAgentParsedOutput(found=True, ok=True, parse_error="", status="DONE")

    normalized = _normalized_structured_artifacts(task, parsed)

    assert normalized["artifacts"] == []
    assert not declared.exists()
    assert lookalike.read_text(encoding="utf-8") == "unrelated child-local report\n"
    assert "artifact_registry_refs" not in task.attributes
