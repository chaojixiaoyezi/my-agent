"""LLM: focused tests for subagent runner dispatch limiting.

函数/模块用途: 验证 runner 并发启动前有统一预算入口，后续可扩展到按角色限流。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.dispatch_limiter import (
    RunnerJobLimitRequest,
    limit_runner_jobs,
)
from agent_py_agent.agent.agent_core.dispatch_runner_batches import _limited_runner_jobs


# LLM: _job builds the tuple shape currently used by dispatch runner batches.
# 函数用途: 构造 runner job 测试数据，第二个元素模拟 SubAgentTask 的 role 字段。
def _job(run_id: str, role: str):
    return (run_id, SimpleNamespace(role=role), "")


# LLM: test_limit_runner_jobs_respects_start_rate keeps current worker-pool behavior stable.
# 函数用途: runner_start_rate 小于候选数时，只返回允许启动的前 N 个任务。
def test_limit_runner_jobs_respects_start_rate():
    result = limit_runner_jobs(
        RunnerJobLimitRequest(
            jobs=[_job("a", "reporter"), _job("b", "reporter"), _job("c", "reporter")],
            runner_start_rate=2,
        )
    )

    assert [job[0] for job in result.allowed_jobs] == ["a", "b"]
    assert result.blocked_count == 1
    assert result.reason == "limited_by_start_rate"


# LLM: test_limit_runner_jobs_applies_role_budgets gives reporter/checker DAGs a safe fan-out hook.
# 函数用途: 配置 role_limits 后，每个角色本轮最多启动指定数量。
def test_limit_runner_jobs_applies_role_budgets():
    result = limit_runner_jobs(
        RunnerJobLimitRequest(
            jobs=[
                _job("r1", "reporter"),
                _job("r2", "reporter"),
                _job("r3", "reporter"),
                _job("c1", "checker"),
                _job("c2", "checker"),
            ],
            runner_start_rate=0,
            role_limits={"reporter": 2, "checker": 1},
        )
    )

    assert [job[0] for job in result.allowed_jobs] == ["r1", "r2", "c1"]
    assert result.blocked_count == 2
    assert result.reason == "limited_by_role_budget"


# LLM: test_limited_runner_jobs_reads_optional_role_limits covers integration without new config fields.
# 函数用途: 现有 dispatch 批处理可读取可选 runner_role_limits，同时保留 runner_start_rate 行为。
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
