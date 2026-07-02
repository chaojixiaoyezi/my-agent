from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.delivery_closeout.closeout import _unique_archive_tool_calls
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
)
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.backends import ModelResponse


def _char_window(offset: int, chars: int, total: int) -> dict[str, object]:
    return {
        "kind": "char_window",
        "offset": offset,
        "chars": chars,
        "next_offset": offset + chars,
        "total_chars": total,
    }


def _line_window(start: int, end: int, total: int) -> dict[str, object]:
    return {
        "kind": "line_window",
        "start_line": start,
        "end_line": end,
        "next_start_line": end + 1 if end < total else 0,
        "total_lines": total,
    }


def test_uncontracted_write_record_requires_explicit_ok() -> None:
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        _successful_write_record,
    )

    assert _successful_write_record({"tool": "write_file", "path": "out.txt"}) is False
    assert _successful_write_record({"tool": "write_file", "path": "out.txt", "ok": True}) is True


def test_required_artifacts_ignore_purpose_text_for_input_detection() -> None:
    from agent_py_agent.agent.agent_core.delivery_closeout.artifacts import _required_artifacts

    contract = {
        "artifacts": [
            {
                "artifact_id": "source_summary",
                "kind": "md",
                "purpose": "source-backed final reference report",
                "preferred_path": "output/source_summary.md",
            },
            {
                "artifact_id": "source_payload",
                "artifact_role": "source",
                "kind": "json",
                "preferred_path": "work/source_payload.json",
            },
        ]
    }

    assert [item["artifact_id"] for item in _required_artifacts(contract)] == ["source_summary"]


def test_uncontracted_prompt_path_tokens_ignore_urls_and_keep_explicit_paths() -> None:
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        _absolute_path_tokens_in_text,
        _absolute_paths_in_text,
    )

    text = "参考 https://example.com/a/b，把报告写到 ~/agent-output/final.md 和 /tmp/final.md。"

    tokens = _absolute_path_tokens_in_text(text)

    assert not any("example.com" in token for token in tokens)
    assert "~/agent-output/final.md" in tokens
    assert "/tmp/final.md" in tokens
    assert [str(path) for path in _absolute_paths_in_text(text)] == [
        str(Path("~/agent-output/final.md").expanduser()),
        "/tmp/final.md",
    ]


def test_uncontracted_prompt_path_token_supports_windows_absolute_forms() -> None:
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        _absolute_path_tokens_in_text,
        _is_absolute_path_token,
    )

    tokens = _absolute_path_tokens_in_text(r"写到 C:\Users\alice\out.md 和 \\server\share\out.md")

    assert r"C:\Users\alice\out.md" in tokens
    assert r"\\server\share\out.md" in tokens
    assert _is_absolute_path_token(r"C:\Users\alice\out.md", platform_name="nt") is True
    assert _is_absolute_path_token(r"\\server\share\out.md", platform_name="nt") is True
    assert _is_absolute_path_token(r"C:\Users\alice\out.md", platform_name="posix") is False


def test_tool_round_without_acceptance_submit_does_not_run_delivery_closeout(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("read_file")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL read_file]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is None
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_tool_round_with_acceptance_submit_runs_delivery_closeout(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text
    assert (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_tool_round_auto_closeout_hint_waits_for_required_target_coverage(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("abcdef", encoding="utf-8")
    params = replace(
        _delivery_params(
            archive_tool_calls=[
                _write_file_archive_record(),
                _read_file_coverage_archive_record(source, offset=0, next_offset=3, total=6),
            ]
        ),
        delivery_contract=_delivery_contract_with_required_source_coverage(source),
    )
    params.tool_context.append("[delivery-completion-soft-hint]\n{}")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is None
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_tool_round_auto_closeout_hint_runs_after_required_target_coverage(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "source.txt"
    source.write_text("abcdef", encoding="utf-8")
    params = replace(
        _delivery_params(
            archive_tool_calls=[
                _write_file_archive_record(),
                _read_file_coverage_archive_record(source, offset=0, next_offset=6, total=6),
            ]
        ),
        delivery_contract=_delivery_contract_with_required_source_coverage(source),
    )
    params.tool_context.append("[delivery-completion-soft-hint]\n{}")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["target_coverage_status"]["should_block"] is False
    assert report["target_coverage_status"]["missing_count"] == 0
    assert response is None or "交付验收通过" in response.text


def test_acceptance_submit_syncs_task_workspace_closeout_state_and_manifest(tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    task_root = tmp_path / "home" / "tasks" / "2026-06-06" / "source-review"
    output_dir = task_root / "output"
    work_dir = task_root / "work"
    output_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    artifact = output_dir / "report.md"
    artifact.write_text("# done\n\nArtifact body.\n", encoding="utf-8")
    (work_dir / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "task_id": "task-1",
                "primary_run_id": "run-1",
                "status": "RUNNING",
                "artifact_refs": [],
                "evidence_refs": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (work_dir / "refs" / "artifacts").mkdir(parents=True)
    (work_dir / "refs" / "artifacts" / "manifest.json").write_text(
        json.dumps({"version": 1, "artifacts": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    write_record = _write_file_archive_record()
    write_record["parameters"] = {"tool": "write_file", "path": str(artifact)}
    write_record["artifact_ref"] = str(artifact)
    params = replace(
        _delivery_params(archive_tool_calls=[write_record]),
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(output_dir),
                "work_dir": str(work_dir),
            }
        },
        delivery_contract={
            "case_id": "task-workspace-artifact",
            "task_workspace": {
                "task_root": str(task_root),
                "output_dir": str(output_dir),
                "work_dir": str(work_dir),
            },
            "artifacts": [{"artifact_id": "report", "path": str(artifact), "kind": "md"}],
        },
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(repo_root)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text
    assert not (repo_root / ".agent_delivery" / "closeout.json").exists()
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["workspace_root"] == str(task_root.resolve(strict=False))
    state = json.loads((work_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "DONE"
    assert state["verification_status"] == "VERIFIED"
    assert state["progress"] == 1.0
    assert str(artifact) in state["artifact_refs"]
    assert state["delivery_closeout"]["report_ref"] == ".agent_delivery/closeout.json"
    manifest = json.loads((work_dir / "refs" / "artifacts" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["closeout_report_ref"] == ".agent_delivery/closeout.json"
    assert [item["path"] for item in manifest["artifacts"]] == [str(artifact)]
    assert (task_root / "data" / "artifacts" / "registry.jsonl").exists()


def test_target_coverage_closeout_uses_primary_workspace_when_run_has_task_workspace(tmp_path: Path):
    case = _task_workspace_source_coverage_case(tmp_path)
    case.params.executed_tools.append("submit_for_acceptance")
    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=_agent(case.repo_root),
            params=case.params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    report = json.loads((case.task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["target_coverage_status"]["missing_count"] == 0
    assert report["target_coverage_status"]["should_block"] is False
    assert response is not None
    assert "交付验收通过" in response.text


def test_uncontracted_closeout_blocks_when_coverage_evidence_not_projected(tmp_path: Path):
    repo_root = tmp_path / "repo"
    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    sources = [
        _write_source_file(repo_root, "alpha/src/engine.py", "print('alpha')"),
        _write_source_file(repo_root, "beta/src/runner.ts", "export const beta = 1"),
        _write_source_file(repo_root, "gamma/src/main.rs", "fn main() {}"),
        _write_source_file(repo_root, "delta/src/agent_loop.ts", "export const loop = 12345"),
        _write_source_file(repo_root, "epsilon/src/planner.py", "class Planner: pass"),
    ]
    artifact = output_dir / "report.md"
    artifact.write_text("# 总览\n只写了泛泛总结，没有列出源码文件。\n", encoding="utf-8")
    write_record = _write_file_archive_record()
    write_record["parameters"] = {"tool": "write_file", "path": str(artifact)}
    write_record["artifact_ref"] = str(artifact)
    read_records = [
        _read_file_coverage_archive_record(source, offset=0, next_offset=len(source.read_text()), total=len(source.read_text()))
        for source in sources
    ]
    params = replace(
        _delivery_params(archive_tool_calls=[write_record, *read_records]),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
        delivery_contract={
            "case_id": "coverage-projection",
            "task_workspace": _workspace_attrs(task_root, output_dir, work_dir),
            "artifacts": [{"artifact_id": "report", "path": str(artifact), "kind": "md"}],
            "target_coverage_contract": {
                "enforcement": "required",
                "target_items": [{"target_id": str(source), "source_path": str(source)} for source in sources],
            },
        },
    )

    response, report, payload = _submit_acceptance(task_root, params)

    assert response is None
    assert report["target_coverage_status"]["missing_count"] == 0
    assert report["target_coverage_projection_gate"]["allowed"] is False
    assert "TARGET_COVERAGE_EVIDENCE_NOT_IN_ARTIFACT" in {
        finding["code"] for finding in report["target_coverage_projection_gate"]["findings"]
    }
    assert "target_coverage_projection" in {
        gate["gate"] for gate in payload["failed_gates"] if isinstance(gate, dict)
    }


def test_uncontracted_closeout_allows_when_coverage_evidence_is_projected(tmp_path: Path):
    repo_root = tmp_path / "repo"
    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    sources = [
        _write_source_file(repo_root, "alpha/src/engine.py", "print('alpha')"),
        _write_source_file(repo_root, "beta/src/runner.ts", "export const beta = 1"),
        _write_source_file(repo_root, "gamma/src/main.rs", "fn main() {}"),
        _write_source_file(repo_root, "delta/src/agent_loop.ts", "export const loop = 12345"),
        _write_source_file(repo_root, "epsilon/src/planner.py", "class Planner: pass"),
    ]
    artifact = output_dir / "report.md"
    artifact.write_text(
        "# 源码报告\n"
        "- engine.py\n"
        "- runner.ts\n"
        "- main.rs\n"
        "- agent_loop.ts\n"
        "- planner.py\n",
        encoding="utf-8",
    )
    write_record = _write_file_archive_record()
    write_record["parameters"] = {"tool": "write_file", "path": str(artifact)}
    write_record["artifact_ref"] = str(artifact)
    read_records = [
        _read_file_coverage_archive_record(source, offset=0, next_offset=len(source.read_text()), total=len(source.read_text()))
        for source in sources
    ]
    params = replace(
        _delivery_params(archive_tool_calls=[write_record, *read_records]),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
        delivery_contract={
            "case_id": "coverage-projection",
            "task_workspace": _workspace_attrs(task_root, output_dir, work_dir),
            "artifacts": [{"artifact_id": "report", "path": str(artifact), "kind": "md"}],
            "target_coverage_contract": {
                "enforcement": "required",
                "target_items": [{"target_id": str(source), "source_path": str(source)} for source in sources],
            },
        },
    )

    response, report, _payload = _submit_acceptance(task_root, params)

    assert response is not None
    assert report["target_coverage_status"]["missing_count"] == 0
    assert report["target_coverage_projection_gate"]["allowed"] is True
    assert "交付验收通过" in response.text


def test_closeout_blocks_code_identifier_claim_not_seen_in_read_source(tmp_path: Path):
    repo_root = tmp_path / "repo"
    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    source = _write_source_file(
        repo_root,
        "alpha/src/engine.py",
        "class RealEngine:\n    def runTask(self):\n        return 'ok'\n",
    )
    artifact = output_dir / "report.md"
    artifact.write_text(
        "# 源码报告\n"
        "- alpha-main: engine.py\n"
        "- 已核对 `RealEngine` 和 `GhostManager`。\n",
        encoding="utf-8",
    )
    write_record = _write_file_archive_record()
    write_record["parameters"] = {"tool": "write_file", "path": str(artifact)}
    write_record["artifact_ref"] = str(artifact)
    params = replace(
        _delivery_params(
            archive_tool_calls=[
                write_record,
                _read_file_coverage_archive_record(
                    source,
                    offset=0,
                    next_offset=len(source.read_text(encoding="utf-8")),
                    total=len(source.read_text(encoding="utf-8")),
                ),
            ]
        ),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
        delivery_contract={
            "case_id": "source-fact-consistency",
            "task_workspace": _workspace_attrs(task_root, output_dir, work_dir),
            "artifacts": [{"artifact_id": "report", "path": str(artifact), "kind": "md"}],
            "target_coverage_contract": {
                "enforcement": "required",
                "target_items": [
                    {
                        "target_id": "alpha-main",
                        "label": "alpha-main",
                        "source_ref": str(source),
                        "coverage_kind": "full_source_read",
                    }
                ],
            },
        },
    )

    response, report, payload = _submit_acceptance(task_root, params)

    assert response is None
    gate = report["source_fact_consistency_gate"]
    assert gate["allowed"] is False
    assert gate["evidence"]["unsupported_identifiers"] == ["GhostManager"]
    assert "SOURCE_CODE_IDENTIFIER_NOT_READ" in {finding["code"] for finding in gate["findings"]}
    assert "source_fact_consistency" in {
        gate["gate"] for gate in payload["failed_gates"] if isinstance(gate, dict)
    }


def test_closeout_allows_code_identifier_claim_seen_in_read_source(tmp_path: Path):
    repo_root = tmp_path / "repo"
    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    source = _write_source_file(
        repo_root,
        "alpha/src/engine.py",
        "class RealEngine:\n    def runTask(self):\n        return 'ok'\n",
    )
    artifact = output_dir / "report.md"
    artifact.write_text(
        "# 源码报告\n"
        "- alpha-main: engine.py\n"
        "- 已核对 `RealEngine` 和 `runTask`。\n",
        encoding="utf-8",
    )
    write_record = _write_file_archive_record()
    write_record["parameters"] = {"tool": "write_file", "path": str(artifact)}
    write_record["artifact_ref"] = str(artifact)
    params = replace(
        _delivery_params(
            archive_tool_calls=[
                write_record,
                _read_file_coverage_archive_record(
                    source,
                    offset=0,
                    next_offset=len(source.read_text(encoding="utf-8")),
                    total=len(source.read_text(encoding="utf-8")),
                ),
            ]
        ),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
        delivery_contract={
            "case_id": "source-fact-consistency",
            "task_workspace": _workspace_attrs(task_root, output_dir, work_dir),
            "artifacts": [{"artifact_id": "report", "path": str(artifact), "kind": "md"}],
            "target_coverage_contract": {
                "enforcement": "required",
                "target_items": [
                    {
                        "target_id": "alpha-main",
                        "label": "alpha-main",
                        "source_ref": str(source),
                        "coverage_kind": "full_source_read",
                    }
                ],
            },
        },
    )

    response, report, _payload = _submit_acceptance(task_root, params)

    assert response is not None
    assert report["source_fact_consistency_gate"]["allowed"] is True
    assert report["source_fact_consistency_gate"]["evidence"]["unsupported_count"] == 0
    assert "交付验收通过" in response.text


def test_uncontracted_closeout_blocks_when_required_target_label_not_projected(tmp_path: Path):
    repo_root = tmp_path / "repo"
    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    project_sources = {
        "alpha-main": _write_source_file(repo_root, "alpha-main/src/engine.py", "print('alpha')"),
        "beta-main": _write_source_file(repo_root, "beta-main/src/runner.ts", "export const beta = 1"),
        "gamma-main": _write_source_file(repo_root, "gamma-main/src/main.rs", "fn main() {}"),
        "delta-main": _write_source_file(repo_root, "delta-main/src/agent_loop.ts", "export const loop = 12345"),
        "epsilon-main": _write_source_file(repo_root, "epsilon-main/src/planner.py", "class Planner: pass"),
    }
    artifact = output_dir / "report.md"
    artifact.write_text(
        "# 源码报告\n"
        "- alpha-main: engine.py\n"
        "- beta-main: runner.ts\n"
        "- gamma-main: main.rs\n"
        "- delta-main: agent_loop.ts\n"
        "- planner.py\n",
        encoding="utf-8",
    )
    write_record = _write_file_archive_record()
    write_record["parameters"] = {"tool": "write_file", "path": str(artifact)}
    write_record["artifact_ref"] = str(artifact)
    read_records = [
        _read_file_coverage_archive_record(source, offset=0, next_offset=len(source.read_text()), total=len(source.read_text()))
        for source in project_sources.values()
    ]
    params = replace(
        _delivery_params(archive_tool_calls=[write_record, *read_records]),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
        delivery_contract={
            "case_id": "coverage-projection-labels",
            "task_workspace": _workspace_attrs(task_root, output_dir, work_dir),
            "artifacts": [{"artifact_id": "report", "path": str(artifact), "kind": "md"}],
            "target_coverage_contract": {
                "enforcement": "required",
                "target_items": [
                    {
                        "target_id": name,
                        "label": name,
                        "source_ref": str(source.parent.parent),
                    }
                    for name, source in project_sources.items()
                ],
            },
        },
    )

    response, report, payload = _submit_acceptance(task_root, params)

    assert response is None
    projection = report["target_coverage_projection_gate"]
    assert projection["allowed"] is False
    assert projection["evidence"]["required_missing_count"] == 1
    assert projection["evidence"]["required_missing_items"][0]["tokens"] == ["epsilon-main"]
    assert "target_coverage_projection" in {
        gate["gate"] for gate in payload["failed_gates"] if isinstance(gate, dict)
    }


def test_acceptance_submit_uses_disk_write_file_index_for_artifact_provenance(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    _write_tool_output_index(
        tmp_path,
        [
            {
                "tool": "write_file",
                "run_id": "run-1",
                "task_id": "task-1",
                "call_id": "write-out",
                "scoped_call_id": "run-1:write-out",
                "sha256": "write-out-sha",
                "parameters": {"tool": "write_file", "path": "out.txt"},
                "source_input": "write_file",
            }
        ],
    )
    params = _delivery_params(archive_tool_calls=[])
    response, report, _ = _submit_acceptance(tmp_path, params)

    assert response is not None
    assert "交付验收通过" in response.text
    assert report["runtime_gate"]["allowed"] is True
    assert report["artifacts"][0]["provenance"]["proof_kind"] == "tool_output_index"


def test_acceptance_submit_blocks_artifact_when_latest_write_is_partial_unclosed(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    write_record = _write_file_archive_record()
    write_record["call_id"] = "partial-write"
    write_record["parameters"] = {
        "tool": "write_file",
        "path": "out.txt",
        "__partial_unclosed_write": True,
    }
    params = _delivery_params(archive_tool_calls=[write_record])

    response, report, payload = _submit_acceptance(tmp_path, params)

    assert response is None
    assert report["ok"] is False
    assert report["artifacts"][0]["ok"] is False
    findings = report["artifacts"][0]["acceptance_report"]["findings"]
    assert "ARTIFACT_LAST_WRITE_PARTIAL_UNCLOSED" in {finding["code"] for finding in findings}
    assert payload["failed_gates"][0]["gate"] == "delivery_closeout"


def test_uncontracted_task_output_uses_latest_write_and_blocks_partial(tmp_path: Path):
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        _current_run_task_output_artifacts,
    )

    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    artifact = _write_task_report(output_dir)
    first = _write_file_archive_record()
    first["call_id"] = "first"
    first["parameters"] = {"tool": "write_file", "path": str(artifact)}
    partial = _write_file_archive_record()
    partial["call_id"] = "partial"
    partial["parameters"] = {
        "tool": "write_file",
        "path": str(artifact),
        "__partial_unclosed_write": True,
    }
    params = replace(
        _delivery_params(archive_tool_calls=[first, partial]),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
    )

    artifacts = _current_run_task_output_artifacts(params, workspace_root=tmp_path)

    assert len(artifacts) == 1
    assert artifacts[0]["artifact_id"] == "partial"
    assert artifacts[0]["ok"] is False
    findings = artifacts[0]["acceptance_report"]["findings"]
    assert "ARTIFACT_LAST_WRITE_PARTIAL_UNCLOSED" in {finding["code"] for finding in findings}


def test_uncontracted_task_output_blocks_markdown_trailing_heading(tmp_path: Path):
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        _current_run_task_output_artifacts,
    )

    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    artifact = output_dir / "report.md"
    artifact.write_text("# Report\n\nIntro.\n\n## Next Section", encoding="utf-8")
    write_record = _write_file_archive_record()
    write_record["call_id"] = "tail-heading"
    write_record["parameters"] = {
        "tool": "write_file",
        "path": str(artifact),
        "content": artifact.read_text(encoding="utf-8"),
    }
    params = replace(
        _delivery_params(archive_tool_calls=[write_record]),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
    )

    artifacts = _current_run_task_output_artifacts(params, workspace_root=tmp_path)

    assert len(artifacts) == 1
    assert artifacts[0]["ok"] is False
    findings = artifacts[0]["acceptance_report"]["findings"]
    assert "MARKDOWN_TRAILING_EMPTY_HEADING" in {finding["code"] for finding in findings}


def test_uncontracted_task_output_collects_pdf_and_blocks_fake_pdf(tmp_path: Path):
    """R3 实测回归：markdown 改名成 .pdf 应被收集并验收失败，不再被无声忽略。"""
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        _current_run_task_output_artifacts,
    )

    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    artifact = output_dir / "report.pdf"
    artifact.write_text("# Report\n\n这其实是 markdown，不是 PDF。\n", encoding="utf-8")
    write_record = _write_file_archive_record()
    write_record["call_id"] = "fake-pdf"
    write_record["parameters"] = {
        "tool": "write_file",
        "path": str(artifact),
        "content": artifact.read_text(encoding="utf-8"),
    }
    params = replace(
        _delivery_params(archive_tool_calls=[write_record]),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
    )

    artifacts = _current_run_task_output_artifacts(params, workspace_root=tmp_path)

    assert len(artifacts) == 1
    assert artifacts[0]["ok"] is False
    findings = artifacts[0]["acceptance_report"]["findings"]
    assert "PDF_INVALID_SIGNATURE" in {finding["code"] for finding in findings}


def test_uncontracted_task_output_skips_temp_and_lock_files(tmp_path: Path):
    """临时/锁文件即使写在输出目录也不算交付候选。"""
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        _current_run_task_output_artifacts,
    )

    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    report = _write_task_report(output_dir)
    lock = output_dir / "report.md.lock"
    lock.write_text("", encoding="utf-8")
    records = []
    for call_id, path in (("report", report), ("lockfile", lock)):
        record = _write_file_archive_record()
        record["call_id"] = call_id
        record["parameters"] = {"tool": "write_file", "path": str(path)}
        records.append(record)
    params = replace(
        _delivery_params(archive_tool_calls=records),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
    )

    artifacts = _current_run_task_output_artifacts(params, workspace_root=tmp_path)

    paths = {Path(a["path"]).name for a in artifacts}
    assert "report.md.lock" not in paths


def test_uncontracted_closeout_text_includes_unvalidated_hint():
    """大白话任务收口文本必须给用户提示:未经结构化验收(validated=false)、请自行确认——避免把
    '验收通过'误读成'产物已被框架核实'(用户要求;uncontracted 路径无合同可逐项验收)。"""
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import _uncontracted_closeout_text

    text = _uncontracted_closeout_text({
        "report_ref": "x/closeout.json",
        "delivery_mode": "uncontracted_task_output",
        "artifacts": [{"artifact_id": "a", "kind": "md", "path": "out/report.md", "ok": True}],
    })
    assert "交付验收通过" in text  # 既定契约字符串保留(10+ 测试 + 下游依赖)
    assert '"validated": false' in text  # 机读信号:未经结构化验收
    assert "请自行确认" in text  # 给用户的提示

    text2 = _uncontracted_closeout_text({
        "report_ref": "x", "delivery_mode": "uncontracted_task_output", "artifacts": [],
        "quality_advisories": [{"gate": "coverage", "status": "advisory"}],
    })
    assert "quality_advisories" in text2  # 有质量项不达标时一并提示用户


def test_uncontracted_task_output_blocks_short_overwrite_after_unclosed_write_recovery(tmp_path: Path):
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import (
        _current_run_task_output_artifacts,
    )

    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    artifact = output_dir / "report.md"
    artifact.write_text("# Report\n\nIntro.\n", encoding="utf-8")
    parse_error = {
        "tool": "__parse_error__",
        "ok": False,
        "error_code": "TOOL_CALL_UNCLOSED",
        "parameters": {
            "path": str(artifact),
            "write_recovery": {
                "path": str(artifact),
                "max_chunk_chars": 800,
                "strategy": "restart_same_file_with_append_chunks",
            },
        },
    }
    second_parse_error = {**parse_error, "call_id": "parse-2"}
    write_record = _write_file_archive_record()
    write_record["call_id"] = "short-overwrite"
    write_record["parameters"] = {
        "tool": "write_file",
        "path": str(artifact),
        "mode": "overwrite",
        "content": artifact.read_text(encoding="utf-8"),
    }
    params = replace(
        _delivery_params(archive_tool_calls=[parse_error, second_parse_error, write_record]),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
    )

    artifacts = _current_run_task_output_artifacts(params, workspace_root=tmp_path)

    assert len(artifacts) == 1
    assert artifacts[0]["ok"] is False
    findings = artifacts[0]["acceptance_report"]["findings"]
    assert "ARTIFACT_UNCLOSED_WRITE_RECOVERY_INCOMPLETE" in {finding["code"] for finding in findings}


def test_acceptance_submit_blocks_report_missing_materialized_required_sections(tmp_path: Path):
    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    report_path = output_dir / "架构分析报告.md"
    report_path.write_text(
        "# all-agent 目录项目架构分析报告\n\n"
        "## 一、概述\n\n"
        "本报告覆盖多个项目，分析维度包括架构、模块、主要功能、实现方式、优点、缺点、适合学习的地方和对比。\n\n"
        "## 二、源码覆盖清单\n\n"
        "| 项目 | 文件 |\n| --- | --- |\n| ECC-main | README.md |\n",
        encoding="utf-8",
    )
    write_record = _write_file_archive_record()
    write_record["parameters"] = {"tool": "write_file", "path": str(report_path)}
    write_record["artifact_ref"] = str(report_path)
    params = replace(
        _delivery_params(archive_tool_calls=[write_record]),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
        delivery_contract={
            "case_id": "project-analysis-report",
            "artifacts": [
                {
                    "artifact_id": "final_report",
                    "kind": "md",
                    "allowed_output_roots": ["output"],
                    "validation_contract": {
                        "required_sections": ["架构", "模块", "主要功能", "实现方式", "优点", "缺点", "适合学习的地方", "对比"],
                    },
                }
            ],
        },
    )

    response, closeout, payload = _submit_acceptance(task_root, params)

    assert response is None
    assert closeout["ok"] is False
    findings = closeout["artifacts"][0]["acceptance_report"]["findings"]
    assert "MARKDOWN_REQUIRED_SECTION_MISSING" in {finding["code"] for finding in findings}
    assert "架构" in findings[0]["value"]
    assert payload["failed_artifacts"][0]["path"] == str(report_path)


def test_closeout_markdown_validation_checks_source_workspace_tree_refs(tmp_path: Path):
    source_root = tmp_path / "all-agent"
    existing = _write_source_file(source_root, "openclaude-main/src/QueryEngine.ts", "export class QueryEngine {}\n")
    (existing.parent.parent / "README.md").write_text("# openclaude\n", encoding="utf-8")
    task_root, output_dir, work_dir = _make_task_workspace(tmp_path)
    report_path = output_dir / "架构分析报告.md"
    report_path.write_text(
        "# all-agent 架构报告\n\n"
        "```\n"
        "openclaude-main/\n"
        "├── src/                  # source files\n"
        "│   ├── QueryEngine.ts    # existing file\n"
        "│   └── Agent.ts          # missing file\n"
        "└── README.md             # existing file\n"
        "```\n",
        encoding="utf-8",
    )
    write_record = _write_file_archive_record()
    write_record["parameters"] = {"tool": "write_file", "path": str(report_path)}
    write_record["artifact_ref"] = str(report_path)

    from agent_py_agent.agent.agent_core.delivery_closeout.artifacts import (
        DeliveryContractValidationRequest,
        _validate_contract_artifacts,
    )

    contract = {
        "case_id": "source-workspace-tree-refs",
        "task_workspace": _workspace_attrs(task_root, output_dir, work_dir),
        "artifacts": [
            {
                "artifact_id": "final_report",
                "kind": "md",
                "path": str(report_path),
                "allowed_output_roots": [str(output_dir)],
            }
        ],
    }
    closeout = _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            contract=contract,
            artifacts=contract["artifacts"],
            workspace_root=task_root,
            params=SimpleNamespace(request_id="req-1", run_id="run-1", task_id="task-1"),
            archive_tool_calls=[write_record],
            coverage_workspace_root=source_root,
        )
    )

    assert closeout["ok"] is False
    findings = closeout["artifacts"][0]["acceptance_report"]["findings"]
    assert [finding["value"] for finding in findings if finding["code"] == "MARKDOWN_LOCAL_REF_MISSING"] == [
        "openclaude-main/src/Agent.ts"
    ]


def test_closeout_archive_dedup_ignores_records_without_structured_identity():
    records = [
        {"tool": "write_file", "path": "out.txt", "created_at": "1"},
        {"tool": "write_file", "run_id": "run-1", "parameters": {"path": "out.txt"}},
        {"tool": "write_file", "run_id": "run-1", "parameters": {"path": "out.txt"}},
        {"tool": "read_file", "scoped_call_id": "run-1:read-1"},
    ]

    assert _unique_archive_tool_calls(records) == [
        {"tool": "write_file", "run_id": "run-1", "parameters": {"path": "out.txt"}},
        {"tool": "read_file", "scoped_call_id": "run-1:read-1"},
    ]


def test_acceptance_submit_does_not_block_materializer_warning_when_contract_is_valid(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.delivery_contract["_contract_doctor"] = {
        "schema_version": "delivery_contract_doctor.v1",
        "ok": True,
        "should_rematerialize": True,
        "findings": [
            {
                "code": "DELIVERY_MATERIALIZER_SOURCE_COVERAGE_UNDECLARED",
                "severity": "warning",
                "location": "source_paths",
                "message": "source coverage not declared",
                "value": "data/long_field_journal.txt",
            }
        ],
        "repair_actions": [
            {
                "code": "DELIVERY_CONTRACT_REMATERIALIZATION_REQUIRED",
                "recommended_action": "rematerialize_delivery_contract_with_source_coverage_decision",
                "source_paths": ["data/long_field_journal.txt"],
            }
        ],
    }
    params.executed_tools.append("submit_for_acceptance")

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=_agent(tmp_path),
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert "交付验收通过" in response.text
    assert report["ok"] is True


def test_acceptance_submit_blocks_when_required_source_read_is_partial(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("x" * 1000, encoding="utf-8")
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": str(source), "offset": 0, "max_chars": 100},
                "read_window": _char_window(0, 100, 1000),
            },
        ]
    )
    params = replace(
        params,
        delivery_contract={
            **params.delivery_contract,
            "target_coverage_contract": {
                "scope_label": "完整读取源文件",
                "enforcement": "required",
                "coverage_requirement": "full_source_read",
                "target_items": [{"target_id": str(source), "source_ref": str(source)}],
            },
        },
    )
    response, report, _ = _submit_acceptance(tmp_path, params)
    payload = _last_tool_context_payload(params)

    assert response is None
    assert report["target_coverage_status"]["missing_count"] == 1
    assert report["runtime_gate"]["allowed"] is False
    assert "TARGET_COVERAGE_MISSING" in {
        finding["code"] for finding in report["runtime_gate"]["findings"]
    }
    assert "不能替代完整阅读证明" in payload["repair_guidance"]["message_zh"]
    assert "recommended_tool_call" in payload["repair_guidance"]["message_zh"]


def test_acceptance_submit_blocks_when_item_level_required_source_read_is_partial(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("x" * 1000, encoding="utf-8")
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": str(source), "offset": 0, "max_chars": 100},
                "read_window": _char_window(0, 100, 1000),
            },
            {
                "tool": "search_text",
                "ok": True,
                "parameters": {"tool": "search_text", "path": str(source), "query": "x"},
                "output_preview": "matches found",
            },
        ]
    )
    params = replace(
        params,
        delivery_contract={
            **params.delivery_contract,
            "target_coverage_contract": {
                "scope_label": "完整读取源文件",
                "target_items": [
                    {
                        "target_id": str(source),
                        "source_ref": str(source),
                        "enforcement": "required",
                        "coverage_kind": "full_source_read",
                    }
                ],
            },
        },
    )
    response, report, _ = _submit_acceptance(tmp_path, params)

    assert response is None
    assert report["target_coverage_status"]["enforcement"] == "required"
    assert report["target_coverage_status"]["missing_count"] == 1
    assert report["target_coverage_status"]["should_block"] is True


def test_acceptance_submit_uses_disk_tool_output_index_for_coverage_after_compact(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("\n".join(f"line {line}" for line in range(1, 1001)), encoding="utf-8")
    _write_tool_output_index(
        tmp_path,
        [
            {
                "tool": "read_file",
                "ok": True,
                "run_id": "run-1",
                "task_id": "task-1",
                "call_id": "read-full-window",
                "scoped_call_id": "run-1:read-full-window",
                "sha256": "read-full-window-sha",
                "parameters": {"tool": "read_file", "path": str(source), "start_line": 1, "end_line": 500},
                "source_input": str(source),
                "read_window": _line_window(1, 500, 1000),
            }
        ],
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "run_id": "run-1",
                "task_id": "task-1",
                "parameters": {"tool": "read_file", "path": str(source)},
                "output_preview": "\n".join(f"{line}: line {line}" for line in range(1, 27)),
            },
        ]
    )
    params = replace(
        params,
        delivery_contract={
            **params.delivery_contract,
            "target_coverage_contract": {
                "scope_label": "完整读取源文件",
                "enforcement": "required",
                "coverage_requirement": "full_source_read",
                "target_items": [{"target_id": str(source), "source_ref": str(source)}],
            },
        },
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["target_coverage_status"]["missing_count"] == 1
    assert report["target_coverage_status"]["repair_hints"][0]["covered_until_line"] == 500
    assert report["target_coverage_status"]["repair_hints"][0]["recommended_tool_call"] == {
        "tool": "read_file",
        "path": str(source),
        "start_line": 501,
    }


def test_acceptance_submit_blocks_stale_artifact_after_required_source_coverage(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "data" / "source.txt"
    source.parent.mkdir()
    source.write_text("abcdef", encoding="utf-8")
    write_record = _write_file_archive_record()
    write_record["created_at"] = "2026-06-07T00:00:01+00:00"
    read_record = _read_file_coverage_archive_record(source, offset=0, next_offset=6, total=6)
    read_record["created_at"] = "2026-06-07T00:00:02+00:00"
    params = replace(
        _delivery_params(archive_tool_calls=[write_record, read_record]),
        delivery_contract=_delivery_contract_with_required_source_coverage(source),
    )

    response, report, payload = _submit_acceptance(tmp_path, params)

    assert response is None
    freshness = report["target_coverage_freshness_status"]
    assert freshness["should_block"] is True
    assert freshness["latest_required_coverage_created_at"] == "2026-06-07T00:00:02+00:00"
    assert freshness["stale_artifacts"][0]["path"].endswith("out.txt")
    assert report["runtime_gate"]["allowed"] is False
    assert "更新最终交付物" in payload["repair_guidance"]["message_zh"]


def test_acceptance_submit_ignores_disk_tool_output_index_from_other_run(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    source = tmp_path / "data" / "journal.txt"
    source.parent.mkdir()
    source.write_text("\n".join(f"line {line}" for line in range(1, 11)), encoding="utf-8")
    _write_tool_output_index(
        tmp_path,
        [
            {
                "tool": "read_file",
                "ok": True,
                "run_id": "old-run",
                "task_id": "old-task",
                "call_id": "old-read-full",
                "scoped_call_id": "old-run:old-read-full",
                "sha256": "old-read-full-sha",
                "parameters": {"tool": "read_file", "path": str(source)},
                "output_preview": "\n".join(f"{line}: line {line}" for line in range(1, 11)),
            }
        ],
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params = replace(
        params,
        delivery_contract={
            **params.delivery_contract,
            "target_coverage_contract": {
                "scope_label": "完整读取源文件",
                "enforcement": "required",
                "coverage_requirement": "full_source_read",
                "target_items": [{"target_id": str(source), "source_ref": str(source)}],
            },
        },
    )

    response, report, _ = _submit_acceptance(tmp_path, params)

    assert response is None
    assert report["target_coverage_status"]["missing_count"] == 1
    assert report["runtime_gate"]["allowed"] is False


def test_acceptance_submit_with_truly_open_items_bounces_once_then_passes(tmp_path: Path):
    # 四档裁决升级(P1 守望真机实锤,2026-07-02):模型自己账本还挂 pending/in_progress/blocked
    # (它【自己声明】没做完)就显式提交 → 打回【一次】让它对账(继续做完,或改状态写明原因),
    # 同形态第二次放行进 advisory。理由:自动收口(completion soft-hint 路)早就有 open 项挡,
    # 显式提交曾是漏洞——真机守望任务第一个唤醒轮账本挂着"第2轮盯守 in_progress/文件持续
    # 增长中"却当轮 submit 收口成功,20 分钟的盯守 3 分钟就死。仍守 R9 底线:对的是模型
    # 自己的账本、幂等一次、双出口、绝不死锁;非规范 done 别名(completed 等)不触发(见下个测试)。
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "summary": "已读 001-012，剩余 013-060 未读",
            "next_action": "继续读取 fragment-013 到 fragment-060",
            "items": [
                {"id": "fragment-001", "status": "done"},
                {"id": "fragment-013", "status": "in_progress"},
                {"id": "fragment-014", "status": "pending"},
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    first_response, first_report, first_payload = _submit_acceptance(tmp_path, params)

    assert first_response is None, "账本挂真 open 项的首次提交要打回一次对账"
    assert first_report["ok"] is False
    assert any("[open-todo-items-rework]" in str(item) for item in params.tool_context)
    assert first_payload["open_count"] == 2

    second_response, second_report, _payload = _submit_acceptance(tmp_path, params)

    assert second_response is not None, "同形态第二次放行,幂等不死锁"
    assert "交付验收通过" in second_response.text
    assert second_report["ok"] is True
    # 门本身仍判 NEED_REPAIR(消费层降级,不改门返回值),事实进 advisory 供把关。
    assert second_report["task_progress_closeout_gate"]["allowed"] is False
    assert second_report["task_progress_closeout_gate"]["status"] == "NEED_REPAIR"
    assert "TASK_PROGRESS_OPEN_ITEMS" in {
        finding["code"] for finding in second_report["task_progress_closeout_gate"]["findings"]
    }
    assert any(
        item.get("gate") == "task_progress_closeout" for item in second_report.get("quality_advisories", [])
    )


def test_acceptance_submit_keeps_non_canonical_done_status_advisory(tmp_path: Path):
    # 交付判定四档统一(本次重构):非规范完成别名("completed")被进度门判为未收口,
    # 但 task_progress 是进度门 → L3 advisory,只记录不阻断(事实仍在报告里供把关)。
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "summary": "全部片段已完成",
            "next_action": "提交验收",
            "items": [
                {"id": "fragment-001", "status": "completed", "evidence": ["fragment-001.txt line 12"]},
                {"id": "fragment-002", "status": "completed", "evidence": ["fragment-002.txt line 18"]},
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    response, report, _payload = _submit_acceptance(tmp_path, params)

    assert response is not None
    assert "交付验收通过" in response.text
    assert report["ok"] is True
    assert report["task_progress_closeout_gate"]["allowed"] is False
    assert report["task_progress_closeout_gate"]["status"] == "NEED_REPAIR"
    assert report["task_progress_closeout_gate"]["evidence"]["counts"] == {"total": 2, "unknown": 2}
    assert "TASK_PROGRESS_OPEN_ITEMS" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }
    assert any(
        item.get("gate") == "task_progress_closeout" for item in report.get("quality_advisories", [])
    )


def test_acceptance_submit_warns_when_done_progress_items_lack_auditable_facts(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {"id": "fragment-001", "status": "done"},
                {"id": "fragment-002", "status": "done", "evidence": ["fragment-002.txt line 12"]},
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_warns_on_incomplete_progress_coverage_even_when_items_done(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "summary": "模型误以为可以提交验收。",
            "next_action": "提交验收",
            "items": [{"id": "fragment-001", "status": "done", "evidence": ["fragment-001.txt:12"]}],
            "coverage": {
                "goal": "读取两个片段并写入报告。",
                "targets": [
                    {
                        "id": "fragment-001",
                        "status": "done",
                        "checks": {"读取": "done", "写报告": "done"},
                        "evidence": ["fragment-001.txt:12"],
                    },
                    {
                        "id": "fragment-002",
                        "status": "pending",
                        "checks": {"读取": "pending", "写报告": "pending"},
                    },
                ],
            },
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_COVERAGE_INCOMPLETE" in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_parse_plain_evidence_text_as_closeout_fact(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    _write_valid_artifact(tmp_path)
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "fragment-014",
                    "status": "done",
                    "evidence": ["输入目录不存在"],
                }
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_require_final_artifact_to_include_progress_note_tokens(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text("finished artifact without expected facts", encoding="utf-8")
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "fragment-001",
                    "status": "done",
                    "evidence": ["CP_CODE=CPX-001-ABCDEF1234", "SECRET_VALUE=SVX-001-1234ABCDEF"],
                }
            ],
        },
    )
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    response, report, _payload = _submit_acceptance(tmp_path, params)

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_scan_artifact_text_for_task_progress_fact_tokens(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "真实编号 CP-001-037；错误编号 CP-001-001",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "chapter-001",
                    "status": "done",
                    "notes": "检查点=CP-001-037",
                    "evidence": ["source.txt@offset=0"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 0, "max_chars": 1000},
                "output_preview": "源文只出现真实编号 CP-001-037",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_parse_checkpoint_facts_from_progress_notes(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "章节001 南京 继续观察 审批延迟 CP-001",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "章节001",
                    "title": "南京现场复盘",
                    "status": "done",
                    "notes": "地点:南京;最终决定:继续观察;风险词:审批延迟;检查点:CP-001-037",
                    "evidence": ["source.txt@offset=0"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 0},
                "output_preview": "章节001 地点:南京;最终决定:继续观察;风险词:审批延迟;检查点:CP-001-037",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_block_short_checkpoint_token_not_seen_in_source(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "章节080 北京 CP-080-966\n章节081 南京 CP-081",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "章节080",
                    "status": "done",
                    "notes": "地点:北京;检查点:CP-080-966",
                    "evidence": ["source.txt@offset=2050000"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 2050000},
                "output_preview": "源文最后一个编号 CP-080-966；后面没有下一章。",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_leaves_artifact_source_fact_validation_to_structured_contracts(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "真实编号 CP-080-966；错误编号 CP-081-003",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "chapter-080",
                    "status": "done",
                    "evidence": ["source.txt@offset=2050000"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 2050000},
                "output_preview": "源文最后一个编号 CP-080-966；没有下一章。",
            },
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "out.txt"},
                "output_preview": "读回报告：错误编号 CP-081-003",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_check_artifact_fact_tokens_against_externalized_source_read(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text("错误编号 CP-027-999", encoding="utf-8")
    source_blob = tmp_path / "read-source.json"
    source_blob.write_text(
        json.dumps({"content": "源文真实编号 CP-027-002"}, ensure_ascii=False),
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "chapter-027",
                    "status": "done",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "path": str(source_blob),
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_infer_dense_numeric_progress_gap_from_item_names(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    items = [
        {
            "id": f"ch{index:03d}",
            "status": "done",
            "notes": f"检查点=CP-{index:03d}-OKAY",
            "evidence": [f"source.txt@ch{index:03d}"],
        }
        for index in [1, 2, 3, 4, 5, 7, 8, 9, 10]
    ]
    (tmp_path / "out.txt").write_text(
        "\n".join(f"CP-{index:03d}-OKAY" for index in [1, 2, 3, 4, 5, 7, 8, 9, 10]),
        encoding="utf-8",
    )
    write_task_progress(tmp_path, "run-1", {"items": items})
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 0, "max_chars": 1000},
                "output_preview": " ".join(f"CP-{index:03d}-OKAY" for index in [1, 2, 3, 4, 5, 7, 8, 9, 10]),
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    finding_codes = {finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]}
    assert "TASK_PROGRESS_NUMERIC_SEQUENCE_GAP" not in finding_codes


def test_acceptance_submit_counts_bare_numeric_progress_items_in_neighbor_sequence(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    indices = list(range(37, 45))
    (tmp_path / "out.txt").write_text(
        "\n".join(f"ch{index:03d} {_checkpoint(index)}" for index in indices),
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                _numeric_progress_item(index, bare=40 <= index <= 42)
                for index in indices
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 900000},
                "output_preview": " ".join(_checkpoint(index) for index in indices),
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] == "TASK_PROGRESS_NUMERIC_SEQUENCE_GAP"
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_does_not_parse_plain_task_progress_facts_in_final_artifact(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "ch002 错误汇总：武汉 / 补充证据 / CP-002-074",
        encoding="utf-8",
    )
    source_content = "ch002 地点：西安；最终决定：暂停发布；风险词：审批延迟；检查点编号：CP-002-074"
    source_artifact = _write_json_source_artifact(
        tmp_path / "source-read.json",
        source_content,
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "ch002",
                    "status": "done",
                    "result": "地点：西安；最终决定：暂停发布；风险词：审批延迟；检查点编号：CP-002-074",
                    "evidence": ["source-read.json"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _read_file_archive(source_artifact, "long_field_journal.txt", source_content),
            _write_file_archive_record(),
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] == "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT"
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_splits_chinese_sentence_key_value_facts(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "章节 017：最终决定 暂停发布，风险词 凭证缺口，检查点 CP-017-629。",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "ch017",
                    "status": "done",
                    "notes": "最终决定：暂停发布。风险词：凭证缺口。检查点编号：CP-017-629",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "output_preview": "章节 017 最终决定：暂停发布；风险词：凭证缺口；检查点编号：CP-017-629",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] in {
            "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT",
            "TASK_PROGRESS_FACTS_NOT_SOURCE_BACKED",
        }
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_ignores_operational_cursor_facts_in_task_progress(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "报告已按章节整理完成，没有复述每个中间读取 offset。",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "read-window-001",
                    "status": "done",
                    "result": "已读取offset=250000；待读取范围=250000~300000；覆盖字符=300000",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt", "offset": 250000},
                "output_preview": "第六段读取窗口，业务内容已归纳。",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] in {
            "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT",
            "TASK_PROGRESS_FACTS_NOT_SOURCE_BACKED",
        }
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_ignores_placeholder_fact_tokens_in_final_artifact(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text(
        "章节 001 使用检查点 CP-001-037；检查点格式示例为 CP-XXX-YYY。",
        encoding="utf-8",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "001",
                    "status": "done",
                    "notes": "检查点编号：CP-001-037",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "output_preview": "章节 001；检查点编号：CP-001-037",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] == "TASK_PROGRESS_ARTIFACT_FACTS_NOT_SOURCE_BACKED"
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_does_not_parse_complete_artifact_sequence_from_text(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    rows = []
    items = []
    source_facts = []
    for index in range(5, 15):
        cp = f"CP-{index:03d}-{index * 37 % 997:03d}"
        rows.append(f"| {index:03d} | 城市 | {cp} | 风险 | 决定 |")
        items.append(
            {
                "id": f"{index:03d}",
                "status": "done",
                "notes": f"检查点编号：{cp}",
                "evidence": ["source.txt"],
            }
        )
        source_facts.append(cp)
    (tmp_path / "out.txt").write_text(
        "# 报告\n\n## 完整章节清单（共 14 项）\n\n"
        "| 章节 | 城市 | 检查点 | 风险词 | 最终决定 |\n"
        "|------|------|--------|--------|----------|\n"
        + "\n".join(rows),
        encoding="utf-8",
    )
    write_task_progress(tmp_path, "run-1", {"items": items})
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "output_preview": " ".join(source_facts),
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_ARTIFACT_NUMERIC_SEQUENCE_GAP" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_acceptance_submit_does_not_treat_repair_notes_as_required_facts(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text("修正后编号 CP-054-004", encoding="utf-8")
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "cp-fix",
                    "status": "done",
                    "notes": "报告曾经使用 CP-054-001，已改回账本编号 CP-054-004",
                    "evidence": ["source.txt"],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _write_file_archive_record(),
            {
                "tool": "read_file",
                "ok": True,
                "parameters": {"tool": "read_file", "path": "source.txt"},
                "output_preview": "源文真实编号 CP-054-004",
            },
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert not any(
        finding["code"] == "TASK_PROGRESS_FACTS_MISSING_FROM_ARTIFACT"
        for finding in report["task_progress_closeout_gate"]["findings"]
    )


def test_acceptance_submit_does_not_parse_progress_facts_to_validate_source_reads(tmp_path: Path):
    from agent_py_agent.agent.task_progress import write_task_progress

    (tmp_path / "out.txt").write_text("CPX-001-FAKE1234 SVX-001-FAKE5678 HOLD-FAKE90", encoding="utf-8")
    source_artifact = _write_json_source_artifact(
        tmp_path / "source-read.json",
        "fragment-001 contains CPX-001-REAL1234 SVX-001-REAL5678 HOLD-REAL90",
    )
    write_task_progress(
        tmp_path,
        "run-1",
        {
            "items": [
                {
                    "id": "fragment-001",
                    "status": "done",
                    "evidence": [
                        "fragment-001.txt line 12",
                        "CPX-001-FAKE1234",
                        "SVX-001-FAKE5678",
                        "HOLD-FAKE90",
                    ],
                }
            ],
        },
    )
    params = _delivery_params(
        archive_tool_calls=[
            _read_file_archive(source_artifact, "fragment-001.txt", "fragment-001 contains CPX-001-REAL1234"),
            _write_file_archive_record(),
        ]
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is None
    assert report["task_progress_closeout_gate"]["allowed"] is True
    assert "TASK_PROGRESS_FACTS_NOT_SOURCE_BACKED" not in {
        finding["code"] for finding in report["task_progress_closeout_gate"]["findings"]
    }


def test_background_wait_tool_round_yields_without_continuing_tool_loop(tmp_path: Path):
    params = replace(_delivery_params(archive_tool_calls=[]), source="background_main_agent")
    params.executed_tools.append("wait")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL wait]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "不继续轮询" in response.text
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_cli_run_wait_tool_round_does_not_finish_task(tmp_path: Path):
    params = replace(_delivery_params(archive_tool_calls=[]), source="cli_run")
    params.executed_tools.append("wait")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL wait]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is None
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_no_tool_final_answer_does_not_submit_delivery_acceptance(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成，文件已经生成。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "任务完成，文件已经生成。"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()
    assert not params.tool_context


def test_failed_closeout_context_includes_unified_repair_guidance(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成。", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    payload = _last_tool_context_payload(params)

    assert response is None
    assert payload["ok"] is False
    assert payload["repair_guidance"]["mode"] == "closeout_rework"
    assert payload["repair_guidance"]["required_actions"]
    assert "如果还需要读取或搜索" in payload["repair_guidance"]["message_zh"]


def test_submit_with_contract_but_no_required_artifact_returns_rework_context(tmp_path: Path):
    params = replace(
        _delivery_params(archive_tool_calls=[]),
        delivery_contract={
            "schema_version": "delivery_contract.v1",
            "case_id": "missing-artifact",
            "artifacts": [],
        },
    )
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    payload = _last_tool_context_payload(params)

    assert response is None
    assert report["reason"] == "required_artifacts_missing"
    assert payload["repair_guidance"]["mode"] == "closeout_rework"
    assert "没有 required artifact" in payload["repair_guidance"]["message_zh"]


def test_tool_loop_service_does_not_replay_existing_failed_closeout_context():
    source = Path("agent_py_agent/agent/agent_core/_tool_loop_service.py").read_text(
        encoding="utf-8"
    )

    assert "append_existing_failed_closeout_context" not in source


def test_no_tool_working_text_does_not_submit_delivery(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="我继续阅读更多项目，然后再汇总写报告。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()
    assert not params.tool_context


def test_existing_closeout_context_does_not_make_plain_text_submit(tmp_path: Path):
    params = _delivery_params(archive_tool_calls=[])
    params.tool_context.append("[delivery-contract-check]\n{}")
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成，文件已经生成。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "任务完成，文件已经生成。"
    assert params.tool_context == ["[delivery-contract-check]\n{}"]


def test_failed_closeout_does_not_intercept_read_tools_with_delivery_repair_gate(tmp_path: Path):
    _write_builder_ready_closeout(tmp_path)
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent_with_calls(tmp_path, [{"tool": "list_files", "path": "output/report"}])

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="CALL_LIST", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "list_files", "path": "output/report"}]
    assert not any("delivery-required-repair" in item for item in params.tool_context)


def test_target_coverage_rework_guard_blocks_premature_final_write(tmp_path: Path):
    source = tmp_path / "repo" / "project-a"
    source.mkdir(parents=True)
    _write_target_coverage_missing_closeout(tmp_path, source)
    output = tmp_path / "out.txt"
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent_with_calls(tmp_path, [{"tool": "write_file", "path": str(output), "content": "done"}])

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="CALL_WRITE", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "continue"
    assert decision.calls == []
    assert any("target-coverage-rework-guard" in item for item in params.tool_context)


def test_target_coverage_rework_guard_allows_missing_source_read(tmp_path: Path):
    source = tmp_path / "repo" / "project-a"
    source.mkdir(parents=True)
    _write_target_coverage_missing_closeout(tmp_path, source)
    read_path = source / "main.py"
    params = _delivery_params(archive_tool_calls=[])
    agent = _agent_with_calls(tmp_path, [{"tool": "read_file", "path": str(read_path)}])

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="CALL_READ", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "read_file", "path": str(read_path)}]
    assert not any("target-coverage-rework-guard" in item for item in params.tool_context)


def test_no_tool_final_answer_does_not_close_when_valid_without_submit(tmp_path: Path):
    _write_valid_artifact(tmp_path)
    params = _delivery_params(archive_tool_calls=[_write_file_archive_record()])
    agent = _agent(tmp_path)

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="任务完成，文件在 out.txt。", backend="test"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "任务完成，文件在 out.txt。"
    assert not (tmp_path / ".agent_delivery" / "closeout.json").exists()


def test_message_delivery_contract_closes_without_artifact_ref_failure(tmp_path: Path):
    params = _message_delivery_params(archive_tool_calls=[])
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="已经检查完，结论直接回复给你。", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text
    report = json.loads((tmp_path / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["delivery_mode"] == "message"
    assert report["artifacts"] == []
    assert report["runtime_gate"]["allowed"] is True


def test_requires_artifact_false_contract_closes_without_artifacts_field(tmp_path: Path):
    params = _message_delivery_params(archive_tool_calls=[], contract={"case_id": "answer-only", "requires_artifact": False})
    params.executed_tools.append("submit_for_acceptance")
    agent = _agent(tmp_path)

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="检查完成，不需要生成文件。", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert "交付验收通过" in response.text


def _agent(root: Path):
    return SimpleNamespace(
        root=root,
        tools=SimpleNamespace(workspace_root=root, parse_tool_calls=lambda _text: []),
        config=SimpleNamespace(enable_tools=True),
        _current_subagent_run_id="",
    )


def _agent_with_calls(root: Path, calls: list[dict[str, object]]):
    return SimpleNamespace(
        root=root,
        tools=SimpleNamespace(workspace_root=root, parse_tool_calls=lambda _text: list(calls)),
        config=SimpleNamespace(enable_tools=True),
        _current_subagent_run_id="",
    )


def _write_valid_artifact(root: Path) -> None:
    (root / "out.txt").write_text("finished artifact", encoding="utf-8")


def _write_tool_output_index(root: Path, rows: list[dict[str, object]]) -> None:
    index = root / "blobs" / "tool_outputs" / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _write_builder_ready_closeout(root: Path) -> None:
    delivery_dir = root / ".agent_delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    (delivery_dir / "closeout.json").write_text(
        json.dumps(
            {
                "ok": False,
                "delivery_progress": {
                    "recovery_actions": [
                        {
                            "code": "STAGING_BUILDER_READY",
                            "recommended_action": "invoke_builder_tool",
                            "builder_tool": "write_file",
                            "source_ref": "output/report/source.json",
                            "output_ref": "output/report/report.xlsx",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_target_coverage_missing_closeout(root: Path, source: Path) -> None:
    delivery_dir = root / ".agent_delivery"
    delivery_dir.mkdir(parents=True, exist_ok=True)
    (delivery_dir / "closeout.json").write_text(
        json.dumps(
            {
                "ok": False,
                "artifacts": [{"path": str(root / "out.txt"), "ok": True}],
                "target_coverage_status": {
                    "should_block": True,
                    "missing_count": 1,
                    "missing_items": [{"target_id": "project-a", "source_ref": str(source)}],
                    "repair_hints": [
                        {
                            "target_id": "project-a",
                            "source_ref": str(source),
                            "recommended_tool_call": {"tool": "list_files", "path": str(source)},
                        }
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _last_tool_context_payload(params: ToolLoopExecuteParams) -> dict[str, object]:
    return json.loads(params.tool_context[-1].split("\n", 1)[1])


def _submit_acceptance(
    root: Path,
    params: ToolLoopExecuteParams,
) -> tuple[object, dict[str, object], dict[str, object]]:
    params.executed_tools.append("submit_for_acceptance")
    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=_agent(root),
            params=params,
            response=ModelResponse(text="[TOOL_CALL submit_for_acceptance]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    report = json.loads((root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    payload = _last_tool_context_payload(params) if params.tool_context else {}
    return response, report, payload


def _checkpoint(index: int) -> str:
    return f"CP-{index:03d}-{index * 37 % 997:03d}"


def _numeric_progress_item(index: int, *, bare: bool = False) -> dict[str, object]:
    item_id = f"{index:03d}" if bare else f"ch{index:03d}"
    return {
        "id": item_id,
        "title": item_id,
        "status": "done",
        "evidence": [_checkpoint(index)],
    }


def _write_json_source_artifact(path: Path, content: str) -> Path:
    path.write_text(
        json.dumps({"tool": "read_file", "content": content}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _read_file_archive(source_artifact: Path, source_path: str, output_preview: str) -> dict[str, object]:
    return {
        "tool": "read_file",
        "ok": True,
        "run_id": "run-1",
        "source_artifact_ref": str(source_artifact),
        "parameters": {"path": source_path},
        "output_preview": output_preview,
    }


def _delivery_params(*, archive_tool_calls: list[dict[str, object]]) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="make artifact",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=archive_tool_calls,
        delivery_contract={
            "case_id": "generic-artifact",
            "artifacts": [{"artifact_id": "out", "path": "out.txt", "kind": "txt"}],
        },
    )


def _task_workspace_source_coverage_case(root: Path):
    repo_root = root / "repo"
    source = _write_source_file(repo_root, "data/source.txt", "abcdef")
    task_root, output_dir, work_dir = _make_task_workspace(root)
    artifact = _write_task_report(output_dir)
    params = replace(
        _delivery_params(archive_tool_calls=_source_coverage_archive_calls(source, artifact)),
        task_attributes={"run_workspace": _workspace_attrs(task_root, output_dir, work_dir)},
        delivery_contract=_relative_source_coverage_contract(task_root, output_dir, work_dir, artifact),
    )
    return SimpleNamespace(repo_root=repo_root, task_root=task_root, params=params)


def _write_source_file(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_task_workspace(root: Path) -> tuple[Path, Path, Path]:
    task_root = root / "home" / "tasks" / "2026-06-07" / "source-review"
    output_dir = task_root / "output"
    work_dir = task_root / "work"
    output_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    return task_root, output_dir, work_dir


def _write_task_report(output_dir: Path) -> Path:
    artifact = output_dir / "report.md"
    artifact.write_text("# done\n\nArtifact body.\n", encoding="utf-8")
    return artifact


def _source_coverage_archive_calls(source: Path, artifact: Path) -> list[dict[str, object]]:
    write_record = _write_file_archive_record()
    write_record["parameters"] = {"tool": "write_file", "path": str(artifact)}
    write_record["artifact_ref"] = str(artifact)
    return [write_record, _read_file_coverage_archive_record(source, offset=0, next_offset=6, total=6)]


def _workspace_attrs(task_root: Path, output_dir: Path, work_dir: Path) -> dict[str, str]:
    return {
        "task_root": str(task_root),
        "output_dir": str(output_dir),
        "work_dir": str(work_dir),
    }


def _relative_source_coverage_contract(task_root: Path, output_dir: Path, work_dir: Path, artifact: Path):
    return {
        "case_id": "task-workspace-relative-source-coverage",
        "task_workspace": _workspace_attrs(task_root, output_dir, work_dir),
        "artifacts": [{"artifact_id": "report", "path": str(artifact), "kind": "md"}],
        "target_coverage_contract": {
            "enforcement": "required",
            "coverage_requirement": "full_source_read",
            "target_items": [
                {
                    "target_id": "data/source.txt",
                    "source_path": "data/source.txt",
                    "coverage_kind": "full_source_read",
                    "enforcement": "required",
                }
            ],
        },
    }


def _message_delivery_params(
    *,
    archive_tool_calls: list[dict[str, object]],
    contract: dict[str, object] | None = None,
) -> ToolLoopExecuteParams:
    params = _delivery_params(archive_tool_calls=archive_tool_calls)
    return replace(
        params,
        delivery_contract=dict(
            contract
            or {
                "case_id": "message-only",
                "delivery_mode": "message",
                "artifacts": [],
            }
        ),
    )


def _write_file_archive_record() -> dict[str, object]:
    return {
        "tool": "write_file",
        "run_id": "run-1",
        "task_id": "task-1",
        "ok": True,
        "parameters": {"tool": "write_file", "path": "out.txt"},
        "runtime_gate": {
            "allowed": True,
            "status": "ALLOW",
            "evidence": {
                "tool_name": "write_file",
                "operation_id": "op-write-1",
                "idempotency_key": "idem-write-1",
            },
        },
    }


def _delivery_contract_with_required_source_coverage(source: Path) -> dict[str, object]:
    contract = dict(_delivery_params(archive_tool_calls=[]).delivery_contract or {})
    contract["target_coverage_contract"] = {
        "enforcement": "required",
        "coverage_requirement": "full_source_read",
        "target_items": [
            {
                "target_id": str(source),
                "source_path": str(source),
                "coverage_kind": "full_source_read",
                "enforcement": "required",
            }
        ],
    }
    return contract


def _read_file_coverage_archive_record(source: Path, *, offset: int, next_offset: int, total: int) -> dict[str, object]:
    call_id = f"read-{offset}-{next_offset}-{total}"
    return {
        "tool": "read_file",
        "run_id": "run-1",
        "task_id": "task-1",
        "call_id": call_id,
        "scoped_call_id": f"run-1:{call_id}",
        "sha256": f"{call_id}-sha",
        "ok": True,
        "parameters": {"path": str(source)},
        "read_window": {
            "kind": "char_window",
            "offset": offset,
            "next_offset": next_offset,
            "total_chars": total,
        },
    }
