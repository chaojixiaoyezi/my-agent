"""Memory archive CLI route and doctor command tests.
记忆归档 CLI 路由和诊断命令测试。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    gateway_paths,
    gateway_response_path,
    log_gateway_payload,
)
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    append_snapshot,
)


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subagents"\n'
        'local_store_path: "local_store/local.db"\n'
        'local_store_files_dir: "local_store/files"\n'
        'local_store_events_path: "local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def _workspace(config_path: Path) -> Path:
    return config_path.parent / "workspace"


def _run_cli_json(capsys, config_path: Path, *argv: str) -> tuple[int, dict]:
    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), *argv, "--json"])
    code = args.func(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


@dataclass(frozen=True)
class _ArchiveRawEventParams:
    # LLM: archive fixture fields stay bundled so tests mirror product bundle rules.
    event_id: str
    run_id: str
    speaker: str
    content: str
    created_at: str


def _append_archive_raw_event(
    root: Path,
    *,
    params: _ArchiveRawEventParams | None = None,
    event_id: str = "",
    run_id: str = "",
    speaker: str = "",
    content: str = "",
    created_at: str = "",
) -> None:
    values = params or _ArchiveRawEventParams(event_id, run_id, speaker, content, created_at)
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id=values.event_id,
            session_id="session-demo",
            request_id="request-demo",
            run_id=values.run_id,
            speaker=values.speaker,
            target="assistant" if values.speaker == "user" else "user",
            action="message" if values.speaker == "user" else "response",
            status="ok",
            task_id=values.run_id,
            content_preview=values.content,
            source="run",
            created_at=values.created_at,
        ),
    )


def _archive_snapshot(run_id: str) -> CompressionSnapshot:
    return CompressionSnapshot(
        snapshot_id="snapshot-demo-1",
        session_id="session-demo",
        compression_id="compression-demo",
        turn_range={"start": 1, "end": 2},
        user_intents=["继续 README 场景测试任务"],
        assistant_actions=["已创建子代理，等待父代理继续验收。"],
        dispatch_events=[{
            "source": "subagent_run",
            "request_id": "request-demo",
            "run_id": run_id,
            "task_id": run_id,
            "status": "awaiting_acceptance",
        }],
        next_actions=["读取 STATUS.md 和 WORK_LOG.md"],
        task_refs=[run_id],
        created_at="2026-04-30T08:01:00+00:00",
    )


def _write_archive_fixture(root: Path, *, run_id: str = "subagent-archive-demo") -> None:
    _append_archive_raw_event(
        root,
        event_id="raw-demo-1",
        run_id=run_id,
        speaker="user",
        content="继续 README 场景测试任务",
        created_at="2026-04-30T08:00:00+00:00",
    )
    _append_archive_raw_event(
        root,
        event_id="raw-demo-2",
        run_id=run_id,
        speaker="assistant",
        content="已创建子代理，等待父代理继续验收。",
        created_at="2026-04-30T08:00:30+00:00",
    )
    append_snapshot(root, _archive_snapshot(run_id))


def test_memory_archive_list_json_reads_raw_layer(tmp_path, capsys):
    """LLM: Tests that the memory-archive-list CLI command reads raw layer records correctly."""
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    _write_archive_fixture(root)

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-archive-list",
        "--layer",
        "raw",
        "--date",
        "2026-04-30",
    )

    assert code == 0
    assert payload["records"][0]["layer"] == "raw"
    assert payload["records"][0]["request_id"] == "request-demo"


def test_memory_archive_search_filters_by_run_and_speaker(tmp_path, capsys):
    """LLM: Tests that memory-archive-search filters results by run-id and speaker."""
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    _write_archive_fixture(root)

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-archive-search",
        "README",
        "--run-id",
        "subagent-archive-demo",
        "--speaker",
        "user",
    )

    assert code == 0
    assert [item["id"] for item in payload["matches"]] == ["raw-demo-1"]


def test_memory_resume_links_archive_clue_to_task_fact_source(tmp_path, capsys):
    """LLM: Tests that memory-resume connects archive clues to task fact sources for recovery."""
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            subagent_workspace="subagents",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        root,
    )
    task = agent.subagents.create_run(
        goal="继续 README 场景测试任务",
        thought="验证恢复命令能找到任务事实源。",
        plan=["读取 STATUS", "继续执行"],
    )
    _write_archive_fixture(root, run_id=task.id)

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-resume",
        "README",
        "--run-id",
        task.id,
    )

    assert code == 0
    assert payload["archive_matches"][0]["run_id"] == task.id
    assert payload["task_fact_sources"][0]["exists"] is True
    assert payload["task_fact_sources"][0]["run_id"] == task.id
    assert any(
        item.endswith("STATUS.md")
        for item in payload["resume"]["recommended_read_paths"]
    )
    assert payload["brief"]["latest_user_intent"] == "继续 README 场景测试任务"
    assert payload["brief"]["latest_assistant_action"] == "已创建子代理，等待父代理继续验收。"
    assert payload["brief"]["related_ids"]["request_ids"] == ["request-demo"]
    assert payload["brief"]["related_ids"]["run_ids"] == [task.id]
    assert payload["brief"]["likely_task_statuses"][0]["status"] == "PLANNING"
    assert "Recovery Brief" in payload["brief"]["context_block"]
    assert "archive/local matches are recovery clues" in payload["brief"]["context_block"]


def test_memory_resume_context_only_prints_recovery_block(tmp_path, capsys):
    """LLM: Tests that --context-only flag prints just the recovery block without extra headers."""
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            subagent_workspace="subagents",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        root,
    )
    task = agent.subagents.create_run(
        goal="继续 README 场景测试任务",
        thought="验证 context-only 能输出稳定恢复块。",
        plan=["读取 STATUS", "继续执行"],
    )
    _write_archive_fixture(root, run_id=task.id)

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "memory-resume",
            "README",
            "--run-id",
            task.id,
            "--context-only",
        ]
    )
    code = args.func(args)
    output = capsys.readouterr().out

    assert code == 0
    assert output.startswith("# Recovery Brief")
    assert "latest_user_intent: 继续 README 场景测试任务" in output
    assert "must_read" in output
    assert "MY-AGENT MEMORY RESUME" not in output
