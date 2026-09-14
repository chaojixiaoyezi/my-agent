from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.agent.memory_archive.compact_work_state import (
    WorkStateFieldSourceRequest,
    build_work_state_field_sources,
)
from agent_py_agent.tests.memory_compact_support import write_compact_fixture


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


def test_memory_compact_work_state_reads_runtime_handoff(tmp_path: Path) -> None:
    """compact 应携带通用运行交接：最近引导和可见下级状态，而不是只恢复任务目标。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    _write_runtime_handoff_sources(root)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    handoff = work_state["runtime_handoff"]
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=result["apply_id"]))

    assert handoff["recent_guidance"][0]["message"] == "写报告时逐个对象收口，不要最后一次性打勾。"
    assert handoff["agent_tree"]["counts"]["total"] == 2
    assert handoff["agent_tree"]["active_agents"][0]["run_id"] == "run-child-a"
    assert "避免高频轮询" in handoff["next_suggestion"]
    assert resume["handoff"]["runtime_handoff"]["recent_guidance_count"] == 1
    assert "写报告时逐个对象收口" in resume["context_block"]


def test_memory_compact_runtime_handoff_keeps_completed_alias_active(tmp_path: Path) -> None:
    """旧别名不能让 compact 误以为子代理已结束；只有 DONE 才关闭。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    guidance_dir = root / "workspace" / "runtime" / "workspaces" / "project-1" / "conversations" / "guidance"
    guidance_dir.mkdir(parents=True, exist_ok=True)
    for run_id, status in (("run-child-alias", "completed"), ("run-child-done", "DONE")):
        run_dir = root / "tasks" / "2026-06-01" / "run-compact" / "work" / "agents" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "canonical_state.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "parent_run_id": "run-compact",
                    "status": status,
                    "last_progress_summary": "state fixture",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    counts = work_state["runtime_handoff"]["agent_tree"]["counts"]
    active_ids = {row["run_id"] for row in work_state["runtime_handoff"]["agent_tree"]["active_agents"]}

    assert counts["unknown"] == 1
    assert "completed" not in counts
    assert "run-child-alias" in active_ids
    assert "run-child-done" not in active_ids


def test_memory_compact_runtime_handoff_does_not_keep_failed_terminal_active(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    for run_id, status in (("run-child-channel", "CHANNEL_ERROR"), ("run-child-timeout", "TIMEOUT")):
        run_dir = root / "tasks" / "2026-06-01" / "run-compact" / "work" / "agents" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "canonical_state.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "parent_run_id": "run-compact",
                    "status": status,
                    "last_progress_summary": "terminal fixture",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    active_ids = {row["run_id"] for row in work_state["runtime_handoff"]["agent_tree"]["active_agents"]}

    assert "run-child-channel" not in active_ids
    assert "run-child-timeout" not in active_ids


def test_memory_compact_runtime_guidance_overrides_stale_progress_next_step(tmp_path: Path) -> None:
    """compact 后续接应优先最近运行中提示，避免旧进度 next_step 把任务带回旧方向。"""
    from agent_py_agent.agent.task_progress import write_task_progress

    root = tmp_path / "workspace"
    write_compact_fixture(root)
    _write_runtime_handoff_sources(root)
    write_task_progress(
        root,
        "request-compact",
        {
            "summary": "旧任务摘要",
            "next_action": "继续读取已经过期的旧目录。",
            "items": [{"id": "old", "title": "旧方向", "status": "in_progress"}],
        },
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert work_state["next_step"].startswith("按最近运行中提示继续：")
    assert "写报告时逐个对象收口" in work_state["next_step"]
    assert "旧目录" not in work_state["next_step"]


def test_memory_compact_current_task_locator_overrides_archive_wrapper_and_old_action(
    tmp_path: Path,
) -> None:
    """当前 run 的 canonical task 是权威来源，旧归档只作兜底。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    canonical = root / "canonical_state.json"
    canonical.write_text(
        json.dumps(
            {
                "id": "run-compact",
                "goal": "审计当前 sample_a 仓库并写出报告。",
                "status": "RUNNING",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "task.json").write_text(
        json.dumps(
            {
                "schema_version": "subagent-state-locator.v1",
                "kind": "subagent_state_locator",
                "run_id": "run-compact",
                # B.6：canonical 由框架重算，锚 = 同 payload 的
                # agent_run_workspace_dir（框架物化字段），不信任自报
                # canonical_state_ref；fixture 按真实 locator 形态补锚字段。
                "canonical_state_ref": str(canonical),
                "task_workspace_dir": str(root),
                "agent_run_workspace_dir": str(root),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert work_state["goal"] == "审计当前 sample_a 仓库并写出报告。"
    assert work_state["next_step"] == ""
    assert "dry-run" not in work_state["goal"]


def test_memory_compact_work_state_records_tool_outputs_without_overriding_task_action(tmp_path: Path) -> None:
    """已读文件保留为事实，但不能反过来取代当前任务的下一步。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    _write_tool_output_index(
        root,
        {
            "call_id": "2-1",
            "kind": "tool_output",
            "parameters": {"path": "/repo/sample_a-main/README.md", "tool": "read_file"},
            "path": str(root / "blobs" / "tool_outputs" / "read_file-2-1.json"),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:2-1",
            "source_input": "/repo/sample_a-main/README.md",
            "tool": "read_file",
            "size_bytes": 1234,
        },
        {
            "call_id": "3-1",
            "kind": "tool_output",
            "parameters": {"path": "/repo/sample_a-main/sample_a-rs", "recursive": False, "tool": "list_files"},
            "path": str(root / "blobs" / "tool_outputs" / "list_files-3-1.json"),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:3-1",
            "source_input": "/repo/sample_a-main/sample_a-rs",
            "tool": "list_files",
            "size_bytes": 4321,
        },
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=result["apply_id"]))

    assert "/repo/sample_a-main/README.md" in work_state["read_files"]
    assert work_state["tool_progress"][0]["source_path"] == "/repo/sample_a-main/README.md"
    assert work_state["next_step"] == "先看 dry-run，再决定是否启用 apply。"
    assert "/repo/sample_a-main/README.md" in resume["context_block"]
    assert "先看 dry-run，再决定是否启用 apply。" in resume["context_block"]


def test_memory_compact_work_state_records_small_tool_calls_without_overriding_task_action(tmp_path: Path) -> None:
    """未外置的小工具调用仍被记录，但只作为恢复事实。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    _write_tool_output_index(
        root,
        {
            "call_id": "1-1",
            "kind": "tool_call",
            "parameters": {"path": "/repo/START.md", "tool": "read_file"},
            "path": "",
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-1",
            "source_input": "/repo/START.md",
            "tool": "read_file",
            "size_bytes": 180,
        },
        {
            "call_id": "2-1",
            "kind": "tool_output",
            "parameters": {"path": "/repo/shard-01.md", "tool": "read_file"},
            "path": str(root / "blobs" / "tool_outputs" / "read_file-2-1.json"),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:2-1",
            "source_input": "/repo/shard-01.md",
            "tool": "read_file",
            "size_bytes": 2048,
        },
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    restore_refs = json.loads(Path(result["refs"]["restore_refs"]).read_text(encoding="utf-8"))

    assert "/repo/START.md" in work_state["read_files"]
    assert "/repo/shard-01.md" in work_state["read_files"]
    assert restore_refs["source_refs"]["tool_calls"][0]["source_path"] == "/repo/START.md"
    assert [item["source_path"] for item in work_state["tool_progress"]] == ["/repo/START.md", "/repo/shard-01.md"]
    assert work_state["next_step"] == "先看 dry-run，再决定是否启用 apply。"


def test_memory_compact_work_state_does_not_promote_succeeded_status_alias_read(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    entries = []
    for index, (source_path, status) in enumerate(
        (
            ("/repo/status-alias.txt", "succeeded"),
            ("/repo/status-uppercase.txt", "OK"),
        ),
        start=1,
    ):
        artifact = root / "blobs" / "tool_outputs" / f"read_file-alias-{index}.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        entries.append(
            {
                "call_id": f"1-{index}",
                "kind": "tool_output",
                "parameters": {"path": source_path, "tool": "read_file", "offset": 0, "max_chars": 100},
                "path": str(artifact),
                "request_id": "request-compact",
                "run_id": "run-compact",
                "task_id": "run-compact",
                "scoped_call_id": f"run-compact:1-{index}",
                "source_input": source_path,
                "tool": "read_file",
                "ok": True,
                "status": status,
                "read_window": _char_window(0, 100, 200),
                "size_bytes": 100,
            }
        )
        artifact.write_text(
            json.dumps({"content": "[char-window offset=0 chars=100 total_chars=200]\nfirst"}, ensure_ascii=False),
            encoding="utf-8",
        )
    _write_tool_output_index(root, *entries)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert "/repo/status-alias.txt" not in work_state["read_files"]
    assert "/repo/status-uppercase.txt" not in work_state["read_files"]
    assert all(item["source_path"] != "/repo/status-alias.txt" for item in work_state["tool_progress"])
    assert all(item["source_path"] != "/repo/status-uppercase.txt" for item in work_state["tool_progress"])


def test_memory_compact_work_state_requires_explicit_ok_for_read_progress(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    artifact = root / "blobs" / "tool_outputs" / "read_file-missing-ok.json"
    _write_tool_output_index(
        root,
        {
            "call_id": "1-1",
            "kind": "tool_output",
            "parameters": {"path": "/repo/missing-ok.txt", "tool": "read_file", "offset": 0, "max_chars": 100},
            "path": str(artifact),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-1",
            "source_input": "/repo/missing-ok.txt",
            "tool": "read_file",
            "ok": None,
            "read_window": _char_window(0, 100, 200),
            "size_bytes": 100,
        },
    )
    artifact.write_text(
        json.dumps({"content": "[char-window offset=0 chars=100 total_chars=200]\nfirst"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert "/repo/missing-ok.txt" not in work_state["read_files"]
    assert all(item["source_path"] != "/repo/missing-ok.txt" for item in work_state["tool_progress"])


def test_memory_compact_work_state_preserves_read_ranges_for_resume_cursor(tmp_path: Path) -> None:
    """连续分片读取同一大文件时，compact 续接要保留每段范围，不能压成“读过这个文件”。"""
    from agent_py_agent.agent.task_progress import write_task_progress

    root = tmp_path / "workspace"
    write_compact_fixture(root)
    write_task_progress(
        root,
        "run-compact",
        {
            "summary": "旧摘要只记录到第一段",
            "next_action": "继续读取 /repo/data/long.txt 的 offset=50000。",
            "items": [{"id": "old-cursor", "title": "旧游标", "status": "in_progress"}],
        },
    )
    read_1 = root / "blobs" / "tool_outputs" / "read_file-1.json"
    read_2 = root / "blobs" / "tool_outputs" / "read_file-2.json"
    read_3 = root / "blobs" / "tool_outputs" / "read_file-3.json"
    _write_tool_output_index(
        root,
        {
            "call_id": "1-1",
            "kind": "tool_output",
            "parameters": {"path": "/repo/data/long.txt", "tool": "read_file", "offset": 0, "max_chars": 50000},
            "path": str(read_1),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-1",
            "source_input": "/repo/data/long.txt",
            "tool": "read_file",
            "read_window": _char_window(0, 50000, 180000),
            "size_bytes": 50000,
        },
        {
            "call_id": "1-2",
            "kind": "tool_output",
            "parameters": {"path": "/repo/data/long.txt", "tool": "read_file", "offset": 50000, "max_chars": 50000},
            "path": str(read_2),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-2",
            "source_input": "/repo/data/long.txt",
            "tool": "read_file",
            "read_window": _char_window(50000, 50000, 180000),
            "size_bytes": 50000,
        },
        {
            "call_id": "1-3",
            "kind": "tool_output",
            "parameters": {"path": "/repo/data/long.txt", "tool": "read_file", "offset": 100000, "max_chars": 50000},
            "path": str(read_3),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-3",
            "source_input": "/repo/data/long.txt",
            "tool": "read_file",
            "read_window": _char_window(100000, 50000, 180000),
            "size_bytes": 50000,
        },
    )
    read_1.write_text(
        '{"content":"[char-window offset=0 chars=50000 total_chars=180000]\\nfirst"}',
        encoding="utf-8",
    )
    read_2.write_text(
        '{"content":"[char-window offset=50000 chars=50000 total_chars=180000]\\nsecond"}',
        encoding="utf-8",
    )
    read_3.write_text(
        '{"content":"[char-window offset=100000 chars=50000 total_chars=180000]\\nthird"}',
        encoding="utf-8",
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    reads = [item for item in work_state["tool_progress"] if item["tool"] == "read_file"]

    assert [item["offset"] for item in reads] == [0, 50000, 100000]
    assert reads[-1]["next_offset"] == 150000
    assert work_state["read_coverage"]["primary"]["covered_until_offset"] == 150000
    assert work_state["next_step"] == "继续读取 /repo/data/long.txt 的 offset=50000。"


def test_memory_compact_work_state_keeps_read_coverage_when_tool_progress_is_clipped(tmp_path: Path) -> None:
    """tool_progress 为了 prompt 体积会裁剪，但续接游标必须来自完整 coverage。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    rows: list[dict[str, object]] = []
    for index in range(60):
        offset = index * 1000
        artifact = root / "blobs" / "tool_outputs" / f"read_file-{index:03d}.json"
        rows.append(
            {
                "call_id": f"1-{index}",
                "kind": "tool_output",
                "parameters": {"path": "/repo/data/long.txt", "tool": "read_file", "offset": offset, "max_chars": 1000},
                "path": str(artifact),
                "request_id": "request-compact",
                "run_id": "run-compact",
                "task_id": "run-compact",
                "scoped_call_id": f"run-compact:1-{index}",
                "source_input": "/repo/data/long.txt",
                "tool": "read_file",
                "read_window": _char_window(offset, 1000, 70000),
                "size_bytes": 1000,
            }
        )
    _write_tool_output_index(root, *rows)
    for index, row in enumerate(rows):
        path = Path(str(row["path"]))
        path.write_text(
            json.dumps(
                {
                    "content": (
                        f"[char-window offset={index * 1000} chars=1000 total_chars=70000]\n"
                        f"segment {index:03d}"
                    )
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    reads = [item for item in work_state["tool_progress"] if item["tool"] == "read_file"]
    coverage = work_state["read_coverage"]["primary"]
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=result["apply_id"]))

    assert len(reads) == 48
    assert reads[0]["offset"] == 12000
    assert coverage["covered_until_offset"] == 60000
    assert coverage["range_count"] == 60
    assert coverage["omitted_range_count"] == 48
    assert work_state["next_step"] == "先看 dry-run，再决定是否启用 apply。"
    assert resume["handoff"]["captured_refs"]["full_read_coverage"]["covered_until_offset"] == 60000


def test_memory_compact_work_state_keeps_per_source_read_coverage(tmp_path: Path) -> None:
    """多文件阅读任务 compact 后要保留每个源文件的覆盖游标，而不只保留一个 primary。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    _write_multi_source_read_coverage_index(root)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=result["apply_id"]))
    sources = {
        item["source_path"]: item
        for item in work_state["read_coverage"]["sources"]
    }

    assert work_state["read_coverage"]["source_count"] == 3
    assert work_state["read_coverage"]["incomplete_source_count"] == 2
    assert [
        item["source_path"]
        for item in work_state["read_coverage"]["incomplete_sources"]
    ] == ["/repo/project-b/core.py", "/repo/project-c/routes.py"]
    assert work_state["next_step"] == "先看 dry-run，再决定是否启用 apply。"
    assert sources["/repo/project-a/README.md"]["complete"] is True
    assert sources["/repo/project-b/core.py"]["covered_until_offset"] == 500
    assert sources["/repo/project-b/core.py"]["next_offset"] == 500
    assert sources["/repo/project-c/routes.py"]["covered_until_line"] == 40
    assert "incomplete_source_coverage: source_path=/repo/project-b/core.py" in resume["context_block"]
    assert "source_coverage: source_path=/repo/project-b/core.py" in resume["context_block"]
    assert "source_coverage: source_path=/repo/project-c/routes.py" in resume["context_block"]


def _write_multi_source_read_coverage_index(root: Path) -> None:
    _write_tool_output_index(
        root,
        _read_file_index_row(
            root,
            {
                "call_id": "1-1",
                "source": "/repo/project-a/README.md",
                "output_name": "project-a-readme.json",
                "parameters": {"offset": 0, "max_chars": 1000},
                "read_window": _char_window(0, 1000, 1000),
                "size_bytes": 1000,
            },
        ),
        _read_file_index_row(
            root,
            {
                "call_id": "1-2",
                "source": "/repo/project-b/core.py",
                "output_name": "project-b-core.json",
                "parameters": {"offset": 0, "max_chars": 500},
                "read_window": _char_window(0, 500, 2000),
                "size_bytes": 500,
            },
        ),
        _read_file_index_row(
            root,
            {
                "call_id": "1-3",
                "source": "/repo/project-c/routes.py",
                "output_name": "project-c-routes.json",
                "parameters": {"start_line": 1, "max_chars": 500},
                "read_window": _line_window(1, 40, 120),
                "size_bytes": 500,
            },
        ),
    )


def _read_file_index_row(root: Path, spec: dict[str, object]) -> dict[str, object]:
    call_id = str(spec["call_id"])
    source = str(spec["source"])
    parameters = spec["parameters"] if isinstance(spec.get("parameters"), dict) else {}
    return {
        "call_id": call_id,
        "kind": "tool_output",
        "parameters": {"path": source, "tool": "read_file", **parameters},
        "path": str(root / "blobs" / "tool_outputs" / str(spec["output_name"])),
        "request_id": "request-compact",
        "run_id": "run-compact",
        "task_id": "run-compact",
        "scoped_call_id": f"run-compact:{call_id}",
        "source_input": source,
        "tool": "read_file",
        "read_window": spec["read_window"],
        "size_bytes": spec["size_bytes"],
    }


def test_memory_compact_work_state_resumes_paginated_search_from_page_window(tmp_path: Path) -> None:
    """分页搜索的续接游标来自结构化 page_window，而不是从工具输出文字里猜。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    _write_tool_output_index(
        root,
        {
            "call_id": "1-1",
            "kind": "tool_output",
            "parameters": {"query": "Agent", "path": "/repo", "tool": "search_text", "offset": 0, "limit": 25},
            "path": str(root / "blobs" / "tool_outputs" / "search-page-1.json"),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-1",
            "source_input": "/repo",
            "tool": "search_text",
            "page_window": {
                "kind": "offset_page",
                "tool": "search_text",
                "source_path": "/repo",
                "offset": 0,
                "limit": 25,
                "returned": 25,
                "next_offset": 25,
                "complete": False,
                "output_mode": "content",
            },
            "size_bytes": 2048,
        },
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    assert work_state["tool_progress"][0]["tool"] == "search_text"
    assert work_state["tool_progress"][0]["next_offset"] == 25
    assert work_state["next_step"] == "先看 dry-run，再决定是否启用 apply。"


def test_memory_compact_work_state_treats_missing_offset_as_zero_for_read_cursor(tmp_path: Path) -> None:
    """read_file 不传 offset 时就是从 0 开始，compact 不能把第一段游标丢掉。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    _write_tool_output_index(
        root,
        {
            "call_id": "1-1",
            "kind": "tool_output",
            "parameters": {"path": "/repo/data/long.txt", "tool": "read_file", "max_chars": 8000},
            "path": str(root / "blobs" / "tool_outputs" / "read_file-1.json"),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-1",
            "source_input": "/repo/data/long.txt",
            "tool": "read_file",
            "size_bytes": 8000,
        },
        {
            "call_id": "1-2",
            "kind": "tool_output",
            "parameters": {"path": "/repo/data/long.txt", "tool": "read_file", "offset": 8000, "max_chars": 100000},
            "path": str(root / "blobs" / "tool_outputs" / "read_file-2.json"),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-2",
            "source_input": "/repo/data/long.txt",
            "tool": "read_file",
            "size_bytes": 100000,
        },
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    reads = [item for item in work_state["tool_progress"] if item["tool"] == "read_file"]

    assert [item["offset"] for item in reads] == [0, 8000]
    assert [item["next_offset"] for item in reads] == [8000, 108000]
    assert work_state["read_coverage"]["primary"]["covered_until_offset"] == 108000
    assert work_state["next_step"] == "先看 dry-run，再决定是否启用 apply。"


def test_memory_compact_work_state_preserves_line_windows_for_resume_cursor(tmp_path: Path) -> None:
    """按行分片读大文件时，compact 续接要从下一行继续，不能退回旧行号。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    read_1 = root / "blobs" / "tool_outputs" / "line-read-1.json"
    read_2 = root / "blobs" / "tool_outputs" / "line-read-2.json"
    read_3 = root / "blobs" / "tool_outputs" / "line-read-3.json"
    _write_tool_output_index(
        root,
        {
            "call_id": "1-1",
            "kind": "tool_output",
            "parameters": {"path": "/repo/data/line-log.txt", "tool": "read_file", "start_line": 1, "max_chars": 50000},
            "path": str(read_1),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-1",
            "source_input": "/repo/data/line-log.txt",
            "tool": "read_file",
            "read_window": _line_window(1, 200, 1000),
            "size_bytes": 50000,
        },
        {
            "call_id": "1-2",
            "kind": "tool_output",
            "parameters": {"path": "/repo/data/line-log.txt", "tool": "read_file", "start_line": 201, "max_chars": 50000},
            "path": str(read_2),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-2",
            "source_input": "/repo/data/line-log.txt",
            "tool": "read_file",
            "read_window": _line_window(201, 400, 1000),
            "size_bytes": 50000,
        },
        {
            "call_id": "1-3",
            "kind": "tool_output",
            "parameters": {"path": "/repo/data/line-log.txt", "tool": "read_file", "start_line": 401, "max_chars": 50000},
            "path": str(read_3),
            "request_id": "request-compact",
            "run_id": "run-compact",
            "task_id": "run-compact",
            "scoped_call_id": "run-compact:1-3",
            "source_input": "/repo/data/line-log.txt",
            "tool": "read_file",
            "read_window": _line_window(401, 600, 1000),
            "size_bytes": 50000,
        },
    )
    read_1.write_text(_line_read_json(1, 200, 201, 1000), encoding="utf-8")
    read_2.write_text(_line_read_json(201, 400, 401, 1000), encoding="utf-8")
    read_3.write_text(_line_read_json(401, 600, 601, 1000), encoding="utf-8")

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact",
                request_id="request-compact",
                run_id="run-compact",
                task_id="run-compact",
            ),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    reads = [item for item in work_state["tool_progress"] if item["tool"] == "read_file"]

    assert [item["start_line"] for item in reads] == [1, 201, 401]
    assert [item["end_line"] for item in reads] == [200, 400, 600]
    assert reads[-1]["next_start_line"] == 601
    assert work_state["read_coverage"]["primary"]["covered_until_line"] == 600
    assert work_state["next_step"] == "先看 dry-run，再决定是否启用 apply。"


def test_runtime_handoff_finds_real_task_workspace_agents(tmp_path: Path) -> None:
    """真实任务目录是 tasks/<date>/<task>/work/agents，compact 续接必须能找回同树子代理。"""
    root = tmp_path / "workspace"
    parent_run_id = "run-parent"
    child_dir = root / "tasks" / "2026-06-04" / "demo-task" / "work" / "agents" / "subagent-child-a"
    child_dir.mkdir(parents=True)
    (child_dir / "canonical_state.json").write_text(
        json.dumps(
            {
                "id": "subagent-child-a",
                "parent_id": parent_run_id,
                "root_id": parent_run_id,
                "status": "RUNNING",
                "current_tool": "read_file",
                "last_progress_summary": "正在读核心模块",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    unrelated_dir = root / "tasks" / "2026-06-04" / "other-task" / "work" / "agents" / "subagent-other"
    unrelated_dir.mkdir(parents=True)
    (unrelated_dir / "canonical_state.json").write_text(
        json.dumps(
            {
                "id": "subagent-other",
                "parent_id": "run-other",
                "root_id": "run-other",
                "status": "RUNNING",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    sources = build_work_state_field_sources(
        WorkStateFieldSourceRequest(
            plan={"workspace_root": str(root), "scope": {"request_id": parent_run_id, "run_id": parent_run_id}},
            source_state={"task_refs": [], "content_paths": []},
        )
    )

    handoff = sources.runtime_handoff
    assert handoff["agent_tree"]["counts"]["total"] == 1
    assert handoff["agent_tree"]["active_agents"][0]["run_id"] == "subagent-child-a"
    assert handoff["agent_tree"]["active_agents"][0]["parent_run_id"] == parent_run_id
    assert "subagent-other" not in json.dumps(handoff, ensure_ascii=False)


def test_runtime_handoff_treats_scope_ids_as_literal_paths(tmp_path: Path) -> None:
    """run_id 里的 * 等字符必须按字面值处理，不能扩大扫描其它任务的状态。"""
    root = tmp_path / "workspace"
    leak_dir = root / "tasks" / "2026-06-01" / "unrelated-task" / "work" / "agents" / "run-leak"
    leak_dir.mkdir(parents=True)
    (leak_dir / "ACCEPTANCE.md").write_text("- leaked acceptance should not be imported\n", encoding="utf-8")
    (leak_dir / "canonical_state.json").write_text(
        json.dumps({"run_id": "run-leak", "status": "running"}),
        encoding="utf-8",
    )

    sources = build_work_state_field_sources(
        WorkStateFieldSourceRequest(
            plan={"workspace_root": str(root), "scope": {"request_id": "*"}},
            source_state={"task_refs": [], "content_paths": []},
        )
    )

    assert sources.acceptance["items"] == []
    assert sources.read_files == []
    assert sources.runtime_handoff == {}


def test_runtime_fact_preserves_task_output_root_from_workspace_prompt(tmp_path: Path) -> None:
    from agent_py_agent.agent.memory_archive.runtime_fact_source import (
        RuntimeFactSourceRequest,
        write_runtime_fact_source,
    )

    output_dir = tmp_path / "owners" / "local" / "main" / "tasks" / "2026-06-01" / "demo" / "output"
    work_dir = output_dir.parent / "work"
    injection = "\n".join(
        [
            "# Current Task Workspace",
            f"- output_dir: {output_dir}",
            f"- work_dir: {work_dir}",
        ]
    )

    fact_dir = write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=tmp_path,
            request_id="request-1",
            user_prompt="分析几个项目并写报告。",
            runtime_injections=(injection,),
        )
    )
    payload = json.loads((Path(fact_dir) / "task.json").read_text(encoding="utf-8"))

    assert payload["desired_outputs"][0]["artifact_id"] == "task_output_dir"
    assert payload["desired_outputs"][0]["target_path"] == str(output_dir)
    assert payload["run_intent"]["desired_outputs"]["items"] == [str(output_dir)]


def _write_runtime_handoff_sources(root: Path) -> None:
    guidance_dir = root / "workspace" / "runtime" / "workspaces" / "project-1" / "conversations" / "guidance"
    guidance_dir.mkdir(parents=True, exist_ok=True)
    (guidance_dir / "thread.session-compact.jsonl").write_text(
        json.dumps(
            {
                "guidance_id": "guidance-1",
                "target_type": "thread",
                "target_id": "session-compact",
                "message": "写报告时逐个对象收口，不要最后一次性打勾。",
                "sender": "user",
                "priority": "normal",
                "created_at": 1.0,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    for run_id, status in (("run-child-a", "running"), ("run-child-b", "completed")):
        run_dir = root / "tasks" / "2026-06-01" / "run-compact" / "work" / "agents" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "canonical_state.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "parent_run_id": "run-compact",
                    "status": status,
                    "current_tool": "read_file" if status == "running" else "",
                    "last_progress_summary": "正在读源码结构" if status == "running" else "已写完摘要",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def _write_tool_output_index(root: Path, *rows: dict[str, object]) -> None:
    index = root / "blobs" / "tool_outputs" / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    normalized_rows: list[dict[str, object]] = []
    for original in rows:
        row = {"ok": True, "status": "ok", **original}
        raw_path = str(row.get("path") or "").strip()
        if not raw_path:
            normalized_rows.append(row)
            continue
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        normalized_rows.append(row)
    index.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in normalized_rows) + "\n", encoding="utf-8")


def _line_read_json(start: int, end: int, next_start: int, total: int) -> str:
    return json.dumps(
        {
            "content": "\n".join(
                [
                    f"{start}: line {start}",
                    f"{end}: line {end}",
                    f"PARTIAL view only; total_lines={total}; next_start_line={next_start};",
                ]
            )
        },
        ensure_ascii=False,
    )
