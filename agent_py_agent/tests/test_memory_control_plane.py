from __future__ import annotations

"""LLM: regression tests for the runtime memory control-plane query API.

给人看的解释：
这些测试确认 control-plane 能从 daily ledger、compact apply ledger 和 tool output index
读出同一 task/run 的轻量引用，避免后续 compact/resume 各自散扫路径。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.memory_archive import (
    RUNTIME_MEMORY_SCHEMA_VERSION,
    AppendSubagentTaskEventRequest,
    DailyLedgerWorkspaceRefs,
    ExternalizeToolOutputRequest,
    MemoryCompactApplyOptions,
    MemoryControlPlaneQueryOptions,
    append_subagent_task_event,
    apply_memory_compact,
    externalize_tool_output_record,
    query_memory_control_plane,
)
from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions


# LLM: this test covers the minimum unified control-plane contract across all new ledgers.
# 函数用途: 验证同一 task/run 可以统一查到 daily event、task/run refs、compact apply 和外置工具输出。
def test_query_memory_control_plane_returns_scoped_runtime_refs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_control_plane_fixture(root)

    result = query_memory_control_plane(
        root,
        MemoryControlPlaneQueryOptions(task_id="task-control", run_id="run-control"),
    )

    assert result["ok"] is True
    _assert_schema_v2(result, "control_plane_query")
    assert result["scope"]["task_id"] == "task-control"
    assert result["counts"] == {
        "daily_events": 1,
        "task_run_refs": 1,
        "compact_applies": 1,
        "tool_outputs": 1,
    }
    _assert_schema_v2(result["daily_events"][0], "daily_ledger_event")
    _assert_schema_v2(result["task_run_refs"][0], "control_plane_task_run_ref")
    _assert_schema_v2(result["compact_applies"][0], "compact_apply_ledger")
    _assert_schema_v2(result["tool_outputs"][0], "tool_output_index")
    assert result["task_run_refs"][0]["refs"]["agent_run_workspace"].endswith("/tasks/task-control/agents/run-control")
    assert result["compact_applies"][0]["compact_status"] == "applied_non_destructive"
    assert result["tool_outputs"][0]["path"].endswith(".json")


# LLM: this test protects date/event filters and missing-ref filtering semantics.
# 函数用途: 验证 control-plane 支持按日期和事件类型查询，并可过滤掉路径缺失的 task/run 引用。
def test_query_memory_control_plane_filters_daily_events_and_missing_refs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_control_plane_fixture(root)

    result = query_memory_control_plane(
        root,
        MemoryControlPlaneQueryOptions(
            date="2026-05-07",
            event_type="subagent_task_saved",
            include_missing_refs=False,
        ),
    )

    assert result["counts"]["daily_events"] == 1
    assert result["counts"]["task_run_refs"] == 0
    assert result["counts"]["compact_applies"] == 0
    assert result["counts"]["tool_outputs"] == 0
    daily_record = _read_last_jsonl(root / "daily" / "2026-05-07" / "events.jsonl")
    _assert_schema_v2(daily_record, "daily_ledger_event")


# LLM: _assert_schema_v2 protects the shared version/schema/reserved contract for runtime memory indexes.
# 函数用途: 校验记录使用 v2 schema，并包含统一 reserved 扩展槽，方便未来安全加字段。
def _assert_schema_v2(record: dict[str, object], name: str) -> None:
    assert record["version"] == RUNTIME_MEMORY_SCHEMA_VERSION
    assert record["schema"] == {
        "name": name,
        "version": RUNTIME_MEMORY_SCHEMA_VERSION,
        "reserved_keys": ["extensions", "compat", "future"],
    }
    assert record["reserved"] == {
        "schema_name": name,
        "schema_version": RUNTIME_MEMORY_SCHEMA_VERSION,
        "extensions": {},
        "compat": {},
        "future": {},
    }


# LLM: _read_last_jsonl reads one fixture ledger row without depending on production readers.
# 函数用途: 读取测试夹具 JSONL 的最后一行，供 schema 断言使用。
def _read_last_jsonl(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


# LLM: _write_control_plane_fixture creates only lightweight index rows, not full memory contents.
# 函数用途: 建立 daily ledger、compact apply ledger 和 tool output index 的最小测试夹具。
def _write_control_plane_fixture(root: Path) -> None:
    now = datetime(2026, 5, 7, 8, 0, tzinfo=timezone.utc).timestamp()
    task = _task_fixture(root)
    append_subagent_task_event(
        AppendSubagentTaskEventRequest(
            root=root,
            task=task,
            workspace_refs=_workspace_refs(root),
            now=now,
        )
    )
    apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(task_id="task-control", run_id="run-control"),
            actor="test",
        ),
    )
    externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="read_file",
            call_id="1-1",
            output="x" * 1300,
            ok=True,
            task_id="task-control",
            run_id="run-control",
            request_id="req-control",
        )
    )


# LLM: _task_fixture mirrors the fields daily ledger reads from SubAgentTask without importing the full service.
# 函数用途: 生成 append_subagent_task_event 所需的最小 task-like 对象。
def _task_fixture(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        id="run-control",
        root_id="task-control",
        parent_id="",
        status="RUNNING",
        verification_status="pending",
        progress=0.5,
        created_at=datetime(2026, 5, 7, 7, 59, tzinfo=timezone.utc).timestamp(),
        latest_summary="control-plane fixture",
        current_step="query refs",
        task_dir=str(root / "legacy" / "run-control"),
        artifact_refs=["artifact-a"],
        evidence_refs=["evidence-a"],
    )


# LLM: _workspace_refs returns deterministic paths so tests can assert exact control-plane references.
# 函数用途: 生成 daily ledger 中登记的 task workspace、run workspace、artifact manifest 和 compact ledger 路径。
def _workspace_refs(root: Path) -> DailyLedgerWorkspaceRefs:
    task_workspace = root / "tasks" / "task-control"
    run_workspace = task_workspace / "agents" / "run-control"
    return DailyLedgerWorkspaceRefs(
        task_workspace_root=task_workspace,
        agent_run_workspace_root=run_workspace,
        task_artifact_manifest_jsonl=task_workspace / "artifacts" / "manifest.jsonl",
        agent_artifact_manifest_jsonl=run_workspace / "artifacts" / "manifest.jsonl",
        agent_compaction_ledger_jsonl=run_workspace / "compactions" / "ledger.jsonl",
    )
