"""Memory archive CLI query command tests.
记忆归档 CLI 查询命令测试。"""

from __future__ import annotations

import json
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
        f'my_agent_home: "{tmp_path / "home"}"\n'
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


def _home_root(config_path: Path) -> Path:
    return config_path.parent / "home"


def _archive_root_for_workspace(root: Path) -> Path:
    return root.parent / "home" / "owners" / "local" / "main"


def _run_cli_json(capsys, config_path: Path, *argv: str) -> tuple[int, dict]:
    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), *argv, "--json"])
    code = args.func(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


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
    archive_root = _archive_root_for_workspace(root)
    _append_cross_day_raw_event(archive_root, run_id=run_id)
    append_snapshot(archive_root, _cross_day_snapshot(run_id))


def _make_echo_agent(root: Path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(
            my_agent_home=str(root.parent / "home"),
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
    archive_root = Path(agent.home_paths.owner_home_dir)
    append_raw_event(
        archive_root,
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
        archive_root,
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


def _create_processing_done_fallback_agent(
    tmp_path,
) -> tuple[Path, SimpleAgent, str, Path, Path]:
    """Create agent and paths for the processing-fallback test."""
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(_home_root(config_path)),
            model_backend="echo",
            subagent_workspace="subagents",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        root,
    )
    request_id = "gwreq-processing-moved"
    paths = gateway_paths(agent)
    return root, agent, request_id, paths, config_path


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


def test_memory_resume_cross_day_gateway_request_uses_response_fact_source(tmp_path, capsys):
    """LLM: Tests that memory-resume uses gateway response fact sources for cross-day gateway recovery."""
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(_home_root(config_path)),
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        root,
    )
    request_path, response_path = _write_cross_day_gateway_fixture(agent)

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-resume",
        "gateway handoff",
        "--since",
        "2026-04-29",
        "--until",
        "2026-04-30",
    )

    assert code == 0
    archive_ids = {item["id"] for item in payload["archive_matches"]}
    assert {
        "raw-gateway-cross-day-1",
        "snapshot-gateway-cross-day-1",
    }.issubset(archive_ids)
    assert payload["task_fact_sources"] == []
    assert payload["gateway_fact_sources"][0]["request_id"] == "gwreq-cross-day"
    assert payload["gateway_fact_sources"][0]["response_path"] == str(response_path)
    assert str(request_path) in payload["resume"]["recommended_read_paths"]
    assert str(response_path) in payload["resume"]["recommended_read_paths"]
    assert payload["resume"]["gateway_fact_source_count"] == 1
    assert payload["brief"]["related_ids"]["request_ids"] == ["gwreq-cross-day"]
    assert "gateway handoff：继续昨天网关请求" in payload["brief"]["context_block"]
    assert str(response_path) in payload["brief"]["context_block"]


def test_memory_resume_gateway_processing_path_falls_back_to_done_request(tmp_path, capsys):
    """LLM: Tests that memory-resume falls back from processing to done gateway request path."""
    root, agent, request_id, paths, config_path = _create_processing_done_fallback_agent(tmp_path)
    processing_path, done_path, response_path = _write_gateway_processing_and_done_files(
        agent, paths, request_id
    )

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-resume",
        "processing moved",
        "--request-id",
        request_id,
    )

    reads = payload["resume"]["recommended_read_paths"]
    assert code == 0
    assert str(done_path) in reads
    assert str(processing_path) not in reads
    assert payload["gateway_fact_sources"][0]["request_path"] == str(done_path)
