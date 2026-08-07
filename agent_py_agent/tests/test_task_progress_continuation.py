from __future__ import annotations

"""task_progress 账本驱动自动续跑单测(#35 假 DONE 根修)。

真机铁证(scrapy/celery→Go 复刻,2026-08-07):普通任务模型开了 task_progress 账本、
跑一轮工具调用、回中间汇报("接下来会…继续推进")→ runtime_status=ok → 被判定
terminal → workspace 标 DONE(账本 1 pending + coverage 9 项 incomplete 被无视)。
本测试验证:账本 open → 请求内自动续跑;账本 closed → 正常收口;达上限 → 如实
收口 unfinished(BLOCKED),绝不 DONE 撒谎。
"""

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.task_progress_continuation import (
    build_task_progress_continue_injection,
    configured_task_progress_continue_limit,
    mark_task_progress_continued,
    mark_task_progress_limit_reached,
    task_progress_continuation_decision,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _params(task_attributes=None):
    return SimpleNamespace(
        task_attributes=task_attributes,
        task_id="task-progress-1",
        run_id="run-progress-1",
        request_id="req-progress-1",
    )


def _ok_result(text="继续推进", reason="tool_result"):
    return SimpleNamespace(runtime_status="ok", runtime_reason=reason)


def _not_ok_result():
    return SimpleNamespace(runtime_status="wait", runtime_reason="wait")


def _ledger_payload(items=None, coverage=None, next_action="", summary=""):
    items = items if items is not None else [{"id": "main", "status": "pending", "title": "核心模块"}]
    counts = {"pending": 0, "in_progress": 0, "blocked": 0, "unknown": 0, "done": 0}
    for item in items:
        status = str(item.get("status") or "pending")
        if status in counts:
            counts[status] += 1
    payload = {
        "schema_version": "task_progress.v1",
        "run_id": "run-progress-1",
        "items": items,
        "counts": counts,
        "next_action": next_action,
        "summary": summary,
    }
    if coverage is not None:
        payload["coverage"] = {"targets": coverage}
    return payload


def _write_ledger(agent, payload, task_id="task-progress-1"):
    # 与 runtime_owner_root(agent) 一致:账本在 owner home 下。
    path = (
        agent.home_paths.owner_home_dir
        / "memory_archive"
        / "task_progress"
        / task_id
        / "progress.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class TestContinuationDecision:
    def test_ledger_open_continues(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="text"),
            tmp_path,
        )
        _write_ledger(agent, _ledger_payload(next_action="写核心包 task/broker/worker"))
        decision = task_progress_continuation_decision(agent, _params(), _ok_result())
        assert decision.should_continue
        assert decision.reason == "ledger_open"
        assert "open_ledger_items: 1" in decision.injection
        assert "写核心包 task/broker/worker" in decision.injection
        assert "不要总结" in decision.injection

    def test_coverage_open_continues(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="text"),
            tmp_path,
        )
        _write_ledger(
            agent,
            _ledger_payload(
                items=[{"id": "main", "status": "done", "title": "全部完成"}],
                coverage=[
                    {"id": f"req-{i}", "title": f"需求 {i}", "status": "in_progress"}
                    for i in range(9)
                ],
            ),
        )
        decision = task_progress_continuation_decision(agent, _params(), _ok_result())
        assert decision.should_continue
        assert decision.reason == "ledger_open"

    def test_ledger_closed_stops(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="text"),
            tmp_path,
        )
        _write_ledger(
            agent,
            _ledger_payload(
                items=[
                    {"id": "main", "status": "done", "title": "核心模块"},
                    {"id": "extra", "status": "skipped", "title": "外围"},
                ]
            ),
        )
        decision = task_progress_continuation_decision(agent, _params(), _ok_result())
        assert not decision.should_continue
        assert decision.reason == "ledger_closed"

    def test_no_ledger_stops(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="text"),
            tmp_path,
        )
        decision = task_progress_continuation_decision(agent, _params(), _ok_result())
        assert not decision.should_continue
        assert decision.reason == "ledger_closed"

    def test_conversation_scoped_stops(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="text"),
            tmp_path,
        )
        _write_ledger(agent, _ledger_payload())
        params = _params(
            {
                "conversation_thread_id": "thread-1",
                "conversation_task_id": "task-1",
            }
        )
        decision = task_progress_continuation_decision(agent, params, _ok_result())
        assert not decision.should_continue
        assert decision.reason == "conversation_scoped"

    def test_thread_goal_scoped_stops(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="text"),
            tmp_path,
        )
        _write_ledger(agent, _ledger_payload())
        params = _params({"thread_goal_id": "goal-1"})
        decision = task_progress_continuation_decision(agent, params, _ok_result())
        assert not decision.should_continue
        assert decision.reason == "conversation_scoped"

    def test_not_ok_stops(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="text"),
            tmp_path,
        )
        _write_ledger(agent, _ledger_payload())
        decision = task_progress_continuation_decision(agent, _params(), _not_ok_result())
        assert not decision.should_continue
        assert decision.reason == "not_ok"

    def test_depth_limit_stops(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="text"),
            tmp_path,
        )
        _write_ledger(agent, _ledger_payload())
        decision = task_progress_continuation_decision(
            agent, _params(), _ok_result(), depth=3
        )
        assert not decision.should_continue
        assert decision.reason == "limit_reached"

    def test_attributes_override_limit(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), tool_protocol="text"),
            tmp_path,
        )
        _write_ledger(agent, _ledger_payload())
        params = _params({"task_progress_continue_limit": 5})
        assert configured_task_progress_continue_limit(agent, params) == 5
        decision = task_progress_continuation_decision(agent, params, _ok_result(), depth=3)
        assert decision.should_continue
        decision = task_progress_continuation_decision(agent, params, _ok_result(), depth=5)
        assert not decision.should_continue
        assert decision.reason == "limit_reached"

    def test_agent_config_limit_fallback(self, tmp_path):
        agent = SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                tool_protocol="text",
                task_progress_auto_continue_limit=7,
            ),
            tmp_path,
        )
        assert configured_task_progress_continue_limit(agent, _params()) == 7


class TestInjection:
    def test_includes_ledger_facts(self):
        injection = build_task_progress_continue_injection(
            _ledger_payload(next_action="写 broker"),
            depth=1,
            limit=3,
            task_id="task-x",
            open_count=1,
        )
        assert "continuation_round: 1/3" in injection
        assert "task_id: task-x" in injection
        assert "open_ledger_items: 1" in injection
        assert "next_action: 写 broker" in injection

    def test_omits_empty_next_action(self):
        injection = build_task_progress_continue_injection(
            _ledger_payload(),
            depth=2,
            limit=3,
            task_id="task-x",
            open_count=2,
        )
        assert "next_action:" not in injection
        assert "continuation_round: 2/3" in injection

    def test_counts_line_lists_nonzero_statuses(self):
        payload = _ledger_payload(
            items=[
                {"id": "a", "status": "pending"},
                {"id": "b", "status": "in_progress"},
                {"id": "c", "status": "done"},
            ]
        )
        injection = build_task_progress_continue_injection(
            payload, depth=1, limit=3, task_id="task-x", open_count=2
        )
        assert "pending:1" in injection
        assert "in_progress:1" in injection
        assert "done:1" in injection

    def test_nudge_instruction_present(self):
        injection = build_task_progress_continue_injection(
            _ledger_payload(), depth=1, limit=3, task_id="task-x", open_count=1
        )
        assert "不要总结" in injection
        assert "全部 closed" in injection


class TestMarkers:
    def test_continued_marks_depth(self):
        result = SimpleNamespace()
        source = SimpleNamespace()
        marked = mark_task_progress_continued(result, source, depth=2)
        assert marked.task_progress_auto_continued is True
        assert marked.task_progress_auto_continue_depth == 2

    def test_limit_reached_marks_unfinished_on_ok(self):
        result = SimpleNamespace(runtime_status="ok", runtime_reason="tool_result")
        marked = mark_task_progress_limit_reached(result)
        assert marked.runtime_status == "unfinished"
        assert marked.runtime_reason == "TASK_PROGRESS_LIMIT_REACHED"
        assert marked.runtime_source == "task_progress"

    def test_limit_reached_never_overrides_non_ok(self):
        result = SimpleNamespace(runtime_status="wait", runtime_reason="wait")
        marked = mark_task_progress_limit_reached(result)
        assert marked.runtime_status == "wait"
        assert marked.runtime_reason == "wait"

    def test_limit_reached_never_overrides_exhausted(self):
        result = SimpleNamespace(runtime_status="blocked", runtime_reason="repeated_failure_exhausted")
        marked = mark_task_progress_limit_reached(result)
        assert marked.runtime_status == "blocked"


class _CaptureBackend:
    """每轮返回纯文本中间汇报,模拟"开了账本但不 close"的模型行为。"""

    name = "task-progress-capture"

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None):
        self.prompts.append(prompt)
        return ModelResponse(
            text=f"中间汇报 {len(self.prompts)}:继续推进。",
            backend=self.name,
        )


class TestRunIntegration:
    def _agent(self, tmp_path):
        return SimpleAgent(
            AgentConfig(
                model_backend="echo",
                my_agent_home=str(tmp_path / "home"),
                tool_protocol="text",
            ),
            tmp_path,
        )

    def test_open_ledger_auto_continues_then_limits(self, tmp_path):
        agent = self._agent(tmp_path)
        backend = _CaptureBackend()
        agent.backend = backend
        _write_ledger(
            agent,
            _ledger_payload(next_action="写核心包"),
            task_id="task-progress-run",
        )
        result = agent.run(
            "用 Go 复刻 celery",
            save=True,
            request_id="req-progress-run",
            run_id="run-progress-run",
            task_id="task-progress-run",
        )
        # 首轮 + 3 轮账本驱动续跑(默认上限 3)→ 达上限如实收口 unfinished。
        assert len(backend.prompts) == 4
        assert result.task_progress_auto_continued is True
        assert result.task_progress_auto_continue_depth == 3
        assert result.runtime_status == "unfinished"
        assert result.runtime_reason == "TASK_PROGRESS_LIMIT_REACHED"
        continuation_injections = [
            p for p in backend.prompts if "# Task Progress Continuation" in p
        ]
        assert len(continuation_injections) == 3
        assert "1/3" in continuation_injections[0]
        assert "3/3" in continuation_injections[2]

    def test_closed_ledger_finishes_normally(self, tmp_path):
        agent = self._agent(tmp_path)
        backend = _CaptureBackend()
        agent.backend = backend
        _write_ledger(
            agent,
            _ledger_payload(
                items=[{"id": "main", "status": "done", "title": "全部完成"}],
            ),
            task_id="task-progress-closed",
        )
        result = agent.run(
            "写一个简单脚本",
            save=True,
            request_id="req-progress-closed",
            run_id="run-progress-closed",
            task_id="task-progress-closed",
        )
        assert len(backend.prompts) == 1
        assert not getattr(result, "task_progress_auto_continued", False)
        assert result.runtime_status == "ok"

    def test_no_ledger_single_turn(self, tmp_path):
        agent = self._agent(tmp_path)
        backend = _CaptureBackend()
        agent.backend = backend
        result = agent.run(
            "写一个简单脚本",
            save=True,
            request_id="req-progress-none",
            run_id="run-progress-none",
            task_id="task-progress-none",
        )
        assert len(backend.prompts) == 1
        assert not getattr(result, "task_progress_auto_continued", False)
        assert result.runtime_status == "ok"
