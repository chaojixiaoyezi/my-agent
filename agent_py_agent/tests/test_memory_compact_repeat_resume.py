from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    TurnTokenUsage,
    append_raw_event,
    append_session_token_usage,
    append_snapshot,
    write_compression_snapshot_file,
)
from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.user_space.context_bundle import (
    MainContextBundleRequest,
    build_main_context_bundle,
)
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


def _write_repeat_compact_fixture(root: Path, home: Path) -> tuple[str, str]:
    _write_repeat_memory_facts(root)
    artifact_ref = _write_repeat_tool_output(root)
    bundle = build_main_context_bundle(
        MainContextBundleRequest(
            root=root,
            home_paths=ensure_my_agent_home(home),
            user_prompt="继续高端家具网站真实任务，保持验收条件和下一步不丢。",
            request_id="request-repeat",
            run_id="run-repeat",
            task_id="task-repeat",
            save=True,
        )
    )
    return bundle.json_path, artifact_ref


def _write_repeat_memory_facts(root: Path) -> None:
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-repeat-1",
            session_id="session-repeat",
            request_id="request-repeat",
            run_id="run-repeat",
            task_id="task-repeat",
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            content_preview="做高端家具品牌首页并多次 compact 后继续验收。",
            source="run",
            archive_level=2,
            created_at="2026-05-17T08:00:00+00:00",
        ),
    )
    snapshot = CompressionSnapshot(
        snapshot_id="snapshot-repeat-1",
        session_id="session-repeat",
        compression_id="compression-repeat",
        turn_range={"start": 1, "end": 1, "request_id": "request-repeat", "run_id": "run-repeat"},
        user_intents=["用单文件 HTML 做高端现代家具品牌首页。"],
        assistant_actions=["已完成结构规划，并把完整工具输出外置为 artifact。"],
        task_refs=["task-repeat"],
        next_actions=["继续读取 context bundle 和 artifact read hints，再做 QA/验收。"],
        archive_level=2,
        created_at="2026-05-17T08:01:00+00:00",
    )
    append_snapshot(root, snapshot)
    write_compression_snapshot_file(root, snapshot)
    append_session_token_usage(
        root,
        usage=TurnTokenUsage(
            session_id="session-repeat",
            turn_id="turn-repeat-1",
            input_tokens=1200,
            output_tokens=700,
            tool_tokens=300,
            created_at="2026-05-17T08:02:00+00:00",
        ),
    )


def _write_repeat_tool_output(root: Path) -> str:
    record = externalize_tool_output_record(
        ExternalizeToolOutputRequest(
            root=root,
            tool="shell",
            call_id="repeat-1",
            output="完整页面草稿和检查清单\n" + ("家具网站细节\n" * 200),
            ok=True,
            request_id="request-repeat",
            run_id="run-repeat",
            task_id="task-repeat",
            min_chars=0,
            parameters={"command": "cat workspace/drafts/furniture.md"},
        )
    )
    return str(record["artifact_ref"])


def _append_live_raw_message(root: Path, *, speaker: str, action: str, content: str) -> None:
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id=f"raw-live-{action}",
            session_id="session-live",
            request_id="request-live",
            run_id="run-live",
            task_id="task-live",
            speaker=speaker,
            target="tool_loop" if action == "assistant_tool_round" else "assistant",
            action=action,
            status="ok",
            content_preview=content,
            source="live_tool_loop" if action == "assistant_tool_round" else "run",
            archive_level=2,
            created_at="2026-05-17T08:00:00+00:00",
        ),
    )


def test_repeated_compact_apply_resume_preserves_lineage_and_state(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    bundle_ref, artifact_ref = _write_repeat_compact_fixture(root, tmp_path / "home")
    options = MemoryCompactApplyOptions(
        plan_options=MemoryCompactPlanOptions(
            session_id="session-repeat",
            request_id="request-repeat",
            run_id="run-repeat",
            task_id="task-repeat",
        ),
        main_context_bundle_ref=bundle_ref,
    )

    previous_apply_id = ""
    apply_ids: list[str] = []
    for cycle in range(1, 6):
        apply_result = apply_memory_compact(root, options)
        resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"]))

        assert apply_result["lineage"]["cycle_index"] == cycle
        assert apply_result["lineage"]["previous_apply_id"] == previous_apply_id
        assert resume["lineage"]["cycle_index"] == cycle
        assert resume["lineage"]["previous_apply_id"] == previous_apply_id
        assert resume["main_context_bundle"]["ref"] == bundle_ref
        assert artifact_ref in resume["recommended_read_paths"]
        assert resume["continue_packet"]["artifact_read_hints"][0]["artifact_path"] == artifact_ref
        assert "高端现代家具品牌首页" in json.dumps(resume["work_state"], ensure_ascii=False)
        previous_apply_id = apply_result["apply_id"]
        apply_ids.append(previous_apply_id)

    ledger_rows = [
        json.loads(line)
        for line in (root / "memory_archive" / "compact_applies" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len({item["apply_id"] for item in ledger_rows}) == 5
    assert [item["lineage"]["cycle_index"] for item in ledger_rows] == [1, 2, 3, 4, 5]
    assert ledger_rows[-1]["lineage"]["previous_apply_id"] == apply_ids[-2]


def test_compact_apply_writes_machine_state_and_handoff_summary(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    bundle_ref, artifact_ref = _write_repeat_compact_fixture(root, tmp_path / "home")

    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-repeat",
                request_id="request-repeat",
                run_id="run-repeat",
                task_id="task-repeat",
            ),
            main_context_bundle_ref=bundle_ref,
        ),
    )
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"]))

    state = apply_result["compaction_state"]
    assert state["compact_id"] == apply_result["apply_id"]
    assert state["previous_compact_id"] == ""
    assert state["compact_index"] == 1
    assert state["source_refs"]["tool_outputs"][0]["artifact_ref"] == artifact_ref
    assert state["work"]["goal"] == "用单文件 HTML 做高端现代家具品牌首页。"
    assert state["work"]["next_actions"] == ["继续读取 context bundle 和 artifact read hints，再做 QA/验收。"]
    assert state["handoff_summary_ref"].endswith(".handoff.md")
    assert "## 当前任务" in apply_result["handoff_summary"]
    assert "高端现代家具品牌首页" in apply_result["handoff_summary"]
    assert resume["compaction_state"]["compact_id"] == apply_result["apply_id"]
    assert resume["continue_packet"]["compaction_state"]["compact_id"] == apply_result["apply_id"]
    assert resume["continue_packet"]["handoff_summary"]["ref"] == state["handoff_summary_ref"]
    assert "## Compaction Handoff Summary" in resume["context_block"]


def test_repeated_compact_rolls_forward_previous_handoff_summary(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    bundle_ref, _artifact_ref = _write_repeat_compact_fixture(root, tmp_path / "home")
    options = MemoryCompactApplyOptions(
        plan_options=MemoryCompactPlanOptions(
            session_id="session-repeat",
            request_id="request-repeat",
            run_id="run-repeat",
            task_id="task-repeat",
        ),
        main_context_bundle_ref=bundle_ref,
    )

    first = apply_memory_compact(root, options)
    second = apply_memory_compact(root, options)
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=second["apply_id"]))

    assert second["compaction_state"]["compact_index"] == 2
    assert second["compaction_state"]["previous_compact_id"] == first["apply_id"]
    assert second["compaction_state"]["previous_handoff_summary_ref"] == first["compaction_state"]["handoff_summary_ref"]
    assert first["apply_id"] in second["handoff_summary"]
    assert "上一轮压缩" in second["handoff_summary"]
    assert resume["handoff"]["compaction_state"]["previous_compact_id"] == first["apply_id"]


def test_compact_apply_uses_live_raw_assistant_round_as_continuation_hint(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _append_live_raw_message(
        root,
        speaker="user",
        action="message",
        content="找最近几周 GitHub 项目并生成表格。",
    )
    _append_live_raw_message(
        root,
        speaker="assistant",
        action="assistant_tool_round",
        content="已查到前三周，下一步继续查剩余周并汇总 workbook。",
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-live",
                request_id="request-live",
                run_id="run-live",
                task_id="task-live",
            )
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    assert work_state["goal"] == "找最近几周 GitHub 项目并生成表格。"
    assert work_state["next_actions"] == ["已查到前三周，下一步继续查剩余周并汇总 workbook。"]


def test_compact_apply_ignores_raw_tool_call_as_continuation_hint(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _append_live_raw_message(
        root,
        speaker="user",
        action="message",
        content="看几个项目并写一份中文报告。",
    )
    _append_live_raw_message(
        root,
        speaker="assistant",
        action="assistant_tool_round",
        content='[TOOL_CALL]\n{"tool": "read_file", "filesystem": {"path": "README.md"}}\n[/TOOL_CALL]\ntools: read_file',
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-live",
                request_id="request-live",
                run_id="run-live",
                task_id="task-live",
            )
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    assert work_state["goal"] == "看几个项目并写一份中文报告。"
    assert work_state["next_actions"] == []
