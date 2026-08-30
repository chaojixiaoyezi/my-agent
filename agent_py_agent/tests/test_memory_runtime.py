from __future__ import annotations

"""memory runtime integration tests."""

import json
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    append_snapshot,
)
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.settings import AgentConfig


def _promote_formal_route(agent: SimpleAgent) -> str:
    candidate = None
    for task_id in ("task-route-a", "task-route-b"):
        candidate = agent.memory_candidates.observe(
            CandidateObservation(
                candidate_type="lesson",
                content="长期规则正文：回答 memory routing 问题时先核对正式 lesson。",
                subject_key="memory.routing",
                scope=MemoryScope("global", "global"),
                origin="subagent_lesson",
                evidence_refs=({"ref_id": task_id, "kind": "task"},),
                source_task_ids=(task_id,),
                observation_id=f"route-observation:{task_id}",
                promotion_target="lesson",
            )
        )
    assert candidate is not None
    result = agent.memory_promotion.promote(
        candidate.candidate_id,
        automatic=True,
    )
    assert result.promoted is True
    return result.promotion_ref.split("#", 1)[0]


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _test_config(tmp_path: Path, **kwargs) -> AgentConfig:
    return AgentConfig(my_agent_home=str(tmp_path / "home"), **kwargs)


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
                    "status": "pending_closeout",
                }
            ],
            task_refs=[run_id],
            next_actions=["读取 STATUS.md 和 HANDOFF.md。"],
            created_at="2026-04-30T00:10:00+00:00",
        ),
    )


# ── Gateway archive helpers ────────────────────────────────────────────────────

def _write_gateway_response_file(response_path: Path, request_id: str) -> None:
    """Write the gateway response JSON file."""
    response_path.parent.mkdir(parents=True, exist_ok=True)
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


def _write_gateway_request_file(request_path: Path, request_id: str) -> None:
    """Write the gateway request JSON file (done status)."""
    request_path.parent.mkdir(parents=True, exist_ok=True)
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


def _log_gateway_request_to_local_store(agent: SimpleAgent, request_id: str, request_path: Path, response_path: Path) -> None:
    """Log gateway request to LocalStore."""
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


def _append_gateway_archive_events(root: Path, request_id: str, request_path: Path, response_path: Path) -> None:
    """Append raw event and snapshot for gateway cross-day archive."""
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


def _write_cross_day_gateway_archive(agent: SimpleAgent, request_id: str = "gwreq-runtime-cross-day") -> Path:
    root = agent.root
    archive_root = Path(agent.home_paths.owner_home_dir)
    response_path = root / "gateway" / "responses" / f"{request_id}.json"
    request_path = root / "gateway" / "requests" / "done" / f"{request_id}.json"
    _write_gateway_response_file(response_path, request_id)
    _write_gateway_request_file(request_path, request_id)
    _log_gateway_request_to_local_store(agent, request_id, request_path, response_path)
    _append_gateway_archive_events(archive_root, request_id, request_path, response_path)
    return response_path


def test_run_injects_only_formal_routed_lesson_in_memory_envelope(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            memory_rule_routing_enabled=True,
            memory_rule_routing_mode="soft",
            memory_rule_auto_read_limit=1,
        ),
        tmp_path,
    )
    lesson_path = _promote_formal_route(agent)

    result = agent.run("请按长期规则处理 memory index", save=False)

    assert result.memory_route_matches == 1
    assert result.memory_route_paths == [lesson_path]
    assert "### Routed memory authority:" not in result.prompt
    assert result.prompt.count("<memory-context") == 1
    assert "长期规则正文" in result.prompt
    assert result.archive_events == 0


def test_run_writes_raw_archive_when_saved(tmp_path):
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)

    result = agent.run("请归档这轮对话", save=True, request_id="req-archive-save")

    raw_dir = Path(agent.home_paths.owner_audit_dir)
    fact_path = Path(agent.home_paths.owner_home_dir) / "memory_archive" / "runtime_facts" / "req-archive-save" / "task.json"
    files = sorted(raw_dir.glob("*.jsonl"))
    assert result.archive_events == 2
    assert result.archive_token_estimate > 0
    assert result.recovery_snapshot_path == ""
    assert fact_path.exists()
    assert len(files) == 1
    records = _read_jsonl(files[0])
    assert [record["speaker"] for record in records] == ["user", "assistant"]
    assert records[0]["content_preview"] == "请归档这轮对话"
    assert records[1]["action"] == "response"
    facts = json.loads(fact_path.read_text(encoding="utf-8"))
    assert facts["goal"] == "请归档这轮对话"
    assert facts["runtime_progress"]["phase"] == "final"


def test_run_no_save_does_not_write_raw_archive(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)

    result = agent.run("不要归档这轮对话", save=False)

    assert result.archive_events == 0
    assert result.recovery_snapshot_path == ""
    assert not (tmp_path / "audit").exists()
    assert not (tmp_path / "memory" / "hooks").exists()


def test_run_no_save_does_not_write_runtime_fact(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)

    result = agent.run(
        "子代理已完成，请写恢复锚点",
        save=False,
        request_id="req-1",
        run_id="subagent-1",
        task_id="subagent-1",
        source="subagent_run",
        recovery_content_paths=["subagents/subagent-1/STATUS.md"],
        recovery_next_actions=["读取 STATUS.md 后继续验收"],
    )

    assert result.archive_events == 0
    assert result.recovery_snapshot_path == ""
    assert not (tmp_path / "audit").exists()
    assert not (tmp_path / "memory" / "hooks").exists()
    assert not (tmp_path / "memory_archive" / "runtime_facts").exists()


def test_auto_resume_context_is_disabled_by_default(tmp_path):
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)
    agent.run("README 恢复上下文任务", save=True)

    result = agent.run("继续 README", save=False)

    assert result.memory_resume_context_injected is False
    assert "### Auto Recovery Context" not in result.prompt


def test_auto_resume_context_injects_when_always_mode_enabled(tmp_path):
    agent = SimpleAgent(
        _test_config(
            tmp_path,
            model_backend="echo",
            memory_resume_auto_context_enabled=True,
            memory_resume_auto_context_mode="always",
            memory_resume_auto_context_limit=3,
        ),
        tmp_path,
    )
    agent.run("README 恢复上下文任务", save=True, request_id="request-auto-1")

    result = agent.run("继续 README", save=False)

    assert result.memory_resume_context_injected is True
    assert result.memory_resume_context_query == "README"
    assert result.memory_resume_context_matches >= 1
    assert "### Auto Recovery Context" in result.prompt
    assert "# Recovery Brief" in result.prompt
    assert "latest_user_intent: README 恢复上下文任务" in result.prompt
    assert result.memory_resume_context_error == ""


def test_auto_resume_context_can_be_enabled_per_run(tmp_path):
    agent = SimpleAgent(_test_config(tmp_path, model_backend="echo"), tmp_path)
    agent.run("README 临时恢复开关任务", save=True, request_id="request-auto-override")

    result = agent.run("继续 README", save=False, resume_context=True)

    assert result.memory_resume_context_injected is True
    assert result.memory_resume_context_token_estimate > 0
    assert result.prompt_token_estimate >= result.memory_resume_context_token_estimate


def test_auto_resume_context_can_be_disabled_per_run(tmp_path):
    agent = SimpleAgent(
        _test_config(tmp_path, model_backend="echo", memory_resume_auto_context_enabled=True),
        tmp_path,
    )
    agent.run("README 禁用恢复开关任务", save=True)

    result = agent.run("继续 README", save=False, resume_context=False)

    assert result.memory_resume_context_injected is False
    assert "### Auto Recovery Context" not in result.prompt


def test_auto_resume_context_recovers_cross_day_handoff_task(tmp_path):
    agent = SimpleAgent(
        _test_config(
            tmp_path,
            model_backend="echo",
            memory_resume_auto_context_enabled=True,
            memory_resume_auto_context_mode="always",
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
        "# STATUS\n\n- status: DONE\n- next: 继续验收\n",
        encoding="utf-8",
    )
    Path(task.handoff_file).write_text(
        "# HANDOFF\n\n## Next Step\n\n- 继续跨天 worker 验收。\n",
        encoding="utf-8",
    )
    _write_cross_day_handoff_archive(agent.home_paths.owner_home_dir, task.id)

    result = agent.run("继续跨天 handoff runtime", save=False)

    assert result.memory_resume_context_injected is True
    assert result.memory_resume_context_query in {"继续跨天 handoff runtime", "handoff", "runtime"}
    assert result.memory_resume_context_matches >= 2
    assert "### Auto Recovery Context" in result.prompt
    assert task.id in result.prompt
    assert "checkpoint.json" in result.prompt
    assert "HANDOFF.md" in result.prompt
    assert "latest_user_intent: 跨天 handoff runtime：继续昨天 worker" in result.prompt


def test_auto_resume_context_recovers_cross_day_gateway_request(tmp_path):
    agent = SimpleAgent(
        _test_config(
            tmp_path,
            model_backend="echo",
            memory_resume_auto_context_enabled=True,
            memory_resume_auto_context_mode="always",
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
