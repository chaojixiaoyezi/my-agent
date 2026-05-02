"""Archive-related memory runtime integration tests.
记忆运行时归档相关集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import CompressionSnapshot, RawMemoryEvent, append_raw_event, append_snapshot


def _write_cross_day_handoff_archive(root: Path, run_id: str) -> None:
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-runtime-cross-day",
            session_id="session-runtime-cross-day",
            request_id="request-runtime-cross-day",
            run_id=run_id,
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            task_id=run_id,
            content_preview="跨天 handoff runtime：昨天的 worker 需要继续。",
            source="run",
            created_at="2026-04-29T23:50:00+00:00",
        ),
    )
    append_snapshot(
        root,
        CompressionSnapshot(
            snapshot_id="snapshot-runtime-cross-day",
            session_id="session-runtime-cross-day",
            compression_id="compression-runtime-cross-day",
            turn_range={
                "kind": "recovery_snapshot",
                "source": "subagent_run",
                "request_id": "request-runtime-cross-day",
                "run_id": run_id,
                "task_id": run_id,
            },
            user_intents=["跨天 handoff runtime：继续昨天 worker"],
            assistant_actions=["已留下恢复锚点，下一轮应读任务事实源。"],
            dispatch_events=[
                {
                    "source": "subagent_run",
                    "request_id": "request-runtime-cross-day",
                    "run_id": run_id,
                    "task_id": run_id,
                    "status": "awaiting_acceptance",
                }
            ],
            task_refs=[run_id],
            next_actions=["读取 STATUS.md 和 HANDOFF.md。"],
            created_at="2026-04-30T00:10:00+00:00",
        ),
    )


def _write_cross_day_gateway_archive(agent: SimpleAgent, request_id: str = "gwreq-runtime-cross-day") -> Path:
    root = agent.root
    response_path = root / "gateway" / "responses" / f"{request_id}.json"
    request_path = root / "gateway" / "requests" / "done" / f"{request_id}.json"
    response_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(
        json.dumps(
            {
                "id": request_id,
                "kind": "ask",
                "ok": True,
                "status": "done",
                "prompt": "gateway handoff runtime：昨天的 gateway 请求需要恢复。",
                "response": "已完成 gateway 请求，下一轮请读取 response JSON。",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    request_path.write_text(
        json.dumps(
            {
                "id": request_id,
                "kind": "ask",
                "prompt": "gateway handoff runtime：昨天的 gateway 请求需要恢复。",
                "status": "done",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    agent.local_store.log_record(
        source_type="gateway_request",
        source_id=request_id,
        title=f"Gateway ask {request_id}",
        content="gateway handoff runtime：昨天的 gateway 请求需要恢复。",
        metadata={
            "request_id": request_id,
            "status": "done",
            "ok": True,
            "request_path": str(request_path),
            "response_path": str(response_path),
        },
        event_type="gateway_request_completed",
    )
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-runtime-gateway-cross-day",
            session_id="session-runtime-gateway-cross-day",
            request_id=request_id,
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            content_preview="gateway handoff runtime：昨天的 gateway 请求需要恢复。",
            source="gateway",
            created_at="2026-04-29T23:40:00+00:00",
        ),
    )
    append_snapshot(
        root,
        CompressionSnapshot(
            snapshot_id="snapshot-runtime-gateway-cross-day",
            session_id="session-runtime-gateway-cross-day",
            compression_id="compression-runtime-gateway-cross-day",
            turn_range={
                "kind": "recovery_snapshot",
                "source": "gateway",
                "request_id": request_id,
            },
            user_intents=["gateway handoff runtime：继续昨天 gateway 请求"],
            assistant_actions=["gateway response 已落盘，下一轮应读响应 JSON。"],
            dispatch_events=[
                {
                    "source": "gateway",
                    "request_id": request_id,
                    "status": "done",
                }
            ],
            content_paths=[str(request_path), str(response_path)],
            next_actions=["读取 gateway response JSON。"],
            created_at="2026-04-30T00:20:00+00:00",
        ),
    )
    return response_path


def test_auto_resume_context_recovers_cross_day_handoff_task(tmp_path):
    """LLM: Tests that auto resume context recovers cross-day handoff tasks with task fact sources."""
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            memory_resume_auto_context_enabled=True,
            memory_resume_auto_context_limit=5,
        ),
        tmp_path,
    )
    task = agent.subagents.create_run(
        goal="跨天 handoff runtime worker 验收",
        thought="验证 run() 能自动注入跨天恢复上下文。",
        plan=["读 archive", "读 HANDOFF", "继续验收"],
    )
    Path(task.status_file).write_text(
        "# STATUS\n\n- status: AWAITING_ACCEPTANCE\n- next: 继续验收\n",
        encoding="utf-8",
    )
    Path(task.handoff_file).write_text(
        "# HANDOFF\n\n## Next Step\n\n- 继续跨天 worker 验收。\n",
        encoding="utf-8",
    )
    _write_cross_day_handoff_archive(tmp_path, task.id)

    result = agent.run("继续跨天 handoff runtime", save=False)

    assert result.memory_resume_context_injected is True
    assert result.memory_resume_context_query in {"继续跨天 handoff runtime", "handoff", "runtime"}
    assert result.memory_resume_context_matches >= 2
    assert "### Auto Recovery Context" in result.prompt
    assert task.id in result.prompt
    assert "HANDOFF.md" in result.prompt
    assert "latest_user_intent: 跨天 handoff runtime：继续昨天 worker" in result.prompt


def test_auto_resume_context_recovers_cross_day_gateway_request(tmp_path):
    """LLM: Tests that auto resume context recovers cross-day gateway requests with gateway fact sources."""
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            memory_resume_auto_context_enabled=True,
            memory_resume_auto_context_limit=5,
        ),
        tmp_path,
    )
    response_path = _write_cross_day_gateway_archive(agent)

    result = agent.run("继续 gateway handoff runtime", save=False)

    assert result.memory_resume_context_injected is True
    assert result.memory_resume_context_matches >= 2
    assert "### Auto Recovery Context" in result.prompt
    assert "gwreq-runtime-cross-day" in result.prompt
    assert str(response_path) in result.prompt
    assert "latest_user_intent: gateway handoff runtime：继续昨天 gateway 请求" in result.prompt
