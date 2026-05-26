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
        assistant_actions=["已创建子代理，等待父代理继续收口。"],
        dispatch_events=[{
            "source": "subagent_run",
            "request_id": "request-demo",
            "run_id": run_id,
            "task_id": run_id,
            "status": "pending_closeout",
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
        content="已创建子代理，等待父代理继续收口。",
        created_at="2026-04-30T08:00:30+00:00",
    )
    append_snapshot(root, _archive_snapshot(run_id))


def _append_cross_day_raw_event(root: Path, *, run_id: str) -> None:
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-cross-day-1",
            session_id="session-cross-day",
            request_id="request-cross-day",
            run_id=run_id,
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            task_id=run_id,
            content_preview="跨天 handoff：昨天派了 memory worker，今天要继续验收。",
            source="run",
            created_at="2026-04-29T23:58:00+00:00",
        ),
    )


def _cross_day_snapshot(run_id: str) -> CompressionSnapshot:
    return CompressionSnapshot(
        snapshot_id="snapshot-cross-day-1",
        session_id="session-cross-day",
        compression_id="compression-cross-day",
        turn_range={
            "kind": "recovery_snapshot",
            "source": "subagent_run",
            "request_id": "request-cross-day",
            "run_id": run_id,
            "task_id": run_id,
        },
        user_intents=["跨天 handoff：继续 memory worker 验收"],
        assistant_actions=["已经写好 HANDOFF，等待父会话继续读事实源。"],
        dispatch_events=[{
            "source": "subagent_run",
            "request_id": "request-cross-day",
            "run_id": run_id,
            "task_id": run_id,
            "status": "pending_closeout",
        }],
        next_actions=["读取 STATUS.md、WORK_LOG.md、HANDOFF.md 后继续。"],
        task_refs=[run_id],
        content_paths=[
            f"subagents/{run_id}/STATUS.md",
            f"subagents/{run_id}/HANDOFF.md",
        ],
        created_at="2026-04-30T00:05:00+00:00",
    )


def _write_cross_day_handoff_fixture(root: Path, *, run_id: str) -> None:
    _append_cross_day_raw_event(root, run_id=run_id)
    append_snapshot(root, _cross_day_snapshot(run_id))


def _make_echo_agent(root: Path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(
            model_backend="echo",
            subagent_workspace="subagents",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        root,
    )


def _write_task_handoff_files(task) -> None:
    Path(task.status_file).write_text(
        "# STATUS\n\n- status: DONE\n- next: 读取 HANDOFF.md 后继续验收\n",
        encoding="utf-8",
    )
    Path(task.handoff_file).write_text(
        "# HANDOFF\n\n## Current State\n\n- 跨天 handoff 已准备好。\n\n"
        "## Next Step\n\n- 父会话收口。\n",
        encoding="utf-8",
    )


def _assert_cross_day_resume_payload(payload: dict, task_id: str) -> None:
    assert {item["id"] for item in payload["archive_matches"]} == {
        "raw-cross-day-1",
        "snapshot-cross-day-1",
    }
    assert payload["task_fact_sources"][0]["exists"] is True
    assert payload["task_fact_sources"][0]["run_id"] == task_id
    read_paths = payload["resume"]["recommended_read_paths"]
    assert any(path.endswith("reports/checkpoint.json") for path in read_paths)
    assert any(path.endswith("reports/progress.md") for path in read_paths)
    assert any(path.endswith("STATUS.md") for path in read_paths)
    assert any(path.endswith("HANDOFF.md") for path in read_paths)
    assert payload["brief"]["latest_user_intent"] == "跨天 handoff：继续 memory worker 验收"
    assert payload["brief"]["related_ids"]["request_ids"] == ["request-cross-day"]
    assert payload["brief"]["related_ids"]["run_ids"] == [task_id]
    assert task_id in payload["brief"]["context_block"]
    assert "checkpoint.json" in payload["brief"]["context_block"]
    assert "HANDOFF.md" in payload["brief"]["context_block"]


# ── Gateway fixture helpers ───────────────────────────────────────────────────

def _write_gateway_request_response_files(
    paths, request_id: str, request_payload: dict, response_payload: dict
) -> tuple[Path, Path]:
    """Write gateway request.json and response.json files to disk."""
    request_path = paths.done / f"{request_id}.json"
    response_path = gateway_response_path(paths, request_id)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    response_path.write_text(json.dumps(response_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return request_path, response_path


def _write_gateway_processing_and_done_files(
    agent: SimpleAgent, paths, request_id: str
) -> tuple[Path, Path, Path]:
    """Write processing, done and response JSON files for a gateway request."""
    processing_path = paths.processing / f"{request_id}.json"
    done_path = paths.done / f"{request_id}.json"
    response_path = gateway_response_path(paths, request_id)
    processing_path.parent.mkdir(parents=True, exist_ok=True)
    done_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.parent.mkdir(parents=True, exist_ok=True)
    processing_path.write_text(
        json.dumps({"id": request_id, "kind": "ask", "status": "processing"}, ensure_ascii=False),
        encoding="utf-8",
    )
    done_path.write_text(
        json.dumps({"id": request_id, "kind": "ask", "status": "done"}, ensure_ascii=False),
        encoding="utf-8",
    )
    response_path.write_text(
        json.dumps({"id": request_id, "kind": "ask", "ok": True, "status": "done"}, ensure_ascii=False),
        encoding="utf-8",
    )
    log_gateway_payload(
        agent,
        {
            "id": request_id,
            "kind": "ask",
            "ok": True,
            "status": "done",
            "response": "completed after processing path moved",
        },
        event_type="gateway_request_completed",
        request_path=processing_path,
        response_path=response_path,
    )
    return processing_path, done_path, response_path


def _log_gateway_archive_events(*args) -> None:
    """Append raw event and snapshot for gateway cross-day archive."""
    agent, root, request_id, request_path, response_path = args
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-gateway-cross-day-1",
            session_id="session-gateway-cross-day",
            request_id=request_id,
            run_id="",
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            content_preview="gateway handoff：昨天的网关请求今天要继续恢复。",
            source="gateway",
            created_at="2026-04-29T23:58:00+00:00",
        ),
    )
    append_snapshot(
        root,
        CompressionSnapshot(
            snapshot_id="snapshot-gateway-cross-day-1",
            session_id="session-gateway-cross-day",
            compression_id="compression-gateway-cross-day",
            turn_range={
                "kind": "recovery_snapshot",
                "source": "gateway",
                "request_id": request_id,
            },
            user_intents=["gateway handoff：继续昨天网关请求"],
            assistant_actions=["已经写好 gateway response，等待下一轮恢复。"],
            dispatch_events=[
                {
                    "source": "gateway",
                    "request_id": request_id,
                    "status": "done",
                }
            ],
            next_actions=["读取 gateway response JSON 和 LocalStore gateway_request 记录。"],
            content_paths=[str(request_path), str(response_path)],
            created_at="2026-04-30T00:05:00+00:00",
        ),
    )


def _write_cross_day_gateway_fixture(agent: SimpleAgent, *, request_id: str = "gwreq-cross-day") -> tuple[Path, Path]:
    root = agent.root
    paths = gateway_paths(agent)
    request_payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": "gateway handoff：昨天的网关请求今天要继续恢复。",
        "save": True,
        "status": "done",
        "created_at": 1777507080.0,
    }
    response_payload = {
        "id": request_id,
        "kind": "ask",
        "ok": True,
        "status": "done",
        "prompt": "gateway handoff：昨天的网关请求今天要继续恢复。",
        "response": "gateway handoff 已完成，下一轮应读取 response JSON 和 LocalStore 记录。",
        "backend": "echo",
        "tool_rounds": 0,
        "created_at": 1777507080.0,
        "started_at": 1777507100.0,
        "ended_at": 1777507120.0,
    }
    request_path, response_path = _write_gateway_request_response_files(
        paths, request_id, request_payload, response_payload
    )
    log_gateway_payload(
        agent,
        response_payload,
        event_type="gateway_request_completed",
        request_path=request_path,
        response_path=response_path,
    )
    _log_gateway_archive_events(agent, root, request_id, request_path, response_path)
    return request_path, response_path


def test_memory_archive_list_json_reads_raw_layer(tmp_path, capsys):
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
    assert payload["brief"]["latest_assistant_action"] == "已创建子代理，等待父代理继续收口。"
    assert payload["brief"]["related_ids"]["request_ids"] == ["request-demo"]
    assert payload["brief"]["related_ids"]["run_ids"] == [task.id]
    assert payload["brief"]["likely_task_statuses"][0]["status"] == "PLANNING"
    assert "Recovery Brief" in payload["brief"]["context_block"]
    assert "archive/local matches are recovery clues" in payload["brief"]["context_block"]


def test_memory_resume_context_only_prints_recovery_block(tmp_path, capsys):
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


def test_memory_resume_cross_day_handoff_uses_task_fact_sources(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    agent = _make_echo_agent(root)
    task = agent.subagents.create_run(
        goal="跨天 handoff memory worker 验收",
        thought="验证跨天恢复能回到任务事实源。",
        plan=["读取昨天归档", "读取今天 handoff", "继续验收"],
    )
    _write_task_handoff_files(task)
    _write_cross_day_handoff_fixture(root, run_id=task.id)

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-resume",
        "跨天 handoff",
        "--since",
        "2026-04-29",
        "--until",
        "2026-04-30",
    )

    assert code == 0
    _assert_cross_day_resume_payload(payload, task.id)
