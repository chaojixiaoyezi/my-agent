"""LLM: focused tests for subagent runner dispatch limiting.

函数/模块用途: 验证 runner 并发启动前有统一预算入口，后续可扩展到按角色限流。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch.limiter import (
    limit_runner_jobs,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_batches import (
    _limited_runner_jobs,
)


def _job(run_id: str, role: str):
    return (run_id, SimpleNamespace(role=role), "")


def test_limit_runner_jobs_respects_start_rate():
    result = limit_runner_jobs(
        [_job("a", "reporter"), _job("b", "reporter"), _job("c", "reporter")],
        runner_start_rate=2,
    )

    assert [job[0] for job in result.allowed_jobs] == ["a", "b"]
    assert result.blocked_count == 1
    assert result.reason == "limited_by_start_rate"


def test_limit_runner_jobs_applies_role_budgets():
    result = limit_runner_jobs(
        [
            _job("r1", "reporter"),
            _job("r2", "reporter"),
            _job("r3", "reporter"),
            _job("c1", "checker"),
            _job("c2", "checker"),
        ],
        runner_start_rate=0,
        role_limits={"reporter": 2, "checker": 1},
    )

    assert [job[0] for job in result.allowed_jobs] == ["r1", "r2", "c1"]
    assert result.blocked_count == 2
    assert result.reason == "limited_by_role_budget"


def test_limited_runner_jobs_reads_optional_role_limits():
    agent = SimpleNamespace(
        config=SimpleNamespace(
            runner_timeout_seconds="0",
            runner_concurrency="auto",
            runner_start_rate="0",
            runner_role_limits={"reporter": 1, "checker": 1},
        )
    )

    limited = _limited_runner_jobs(
        agent,
        [_job("r1", "reporter"), _job("r2", "reporter"), _job("c1", "checker")],
    )

    assert [job[0] for job in limited] == ["r1", "c1"]
