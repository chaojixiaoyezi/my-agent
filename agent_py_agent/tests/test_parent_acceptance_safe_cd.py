"""Parent acceptance tests for safe runner cd-chain normalization."""

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.parent_acceptance_auto_execution import (
    ParentAcceptanceAutoExecutionOptions,
)


# LLM: _agent_and_task builds a minimal real manager/task pair for parent acceptance tests.
# 函数用途: 创建临时 workspace、SimpleAgent 和待验收子代理任务，供本文件测试复用。
def _agent_and_task():
    root_ctx = tempfile.TemporaryDirectory()
    root = Path(root_ctx.name)
    cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
    agent = SimpleAgent(cfg, root)
    task = agent.subagents.create_run(
        goal="parent acceptance safe cd normalization",
        thought="runner declared a cwd-wrapped pytest command",
        plan=["write tests", "wait for parent acceptance"],
        acceptance_checks=["safe cwd wrapper must not require human confirmation"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    agent.subagents.save(task)
    return root_ctx, agent, task


# LLM: _write_output writes the runner output fact source without touching task state.
# 函数用途: 写入最小 output.json 和 runner_result.json，模拟真实 runner 输出 tests。
def _write_output(task, tests) -> None:
    Path(task.output_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "status": "AWAITING_ACCEPTANCE",
                "summary": "runner says tests are ready",
                "tests": tests,
                "artifacts": [],
                "patches": [],
                "blockers": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    Path(task.runner_result_json).write_text(
        json.dumps({"run_id": task.id, "structured_output_found": True}, ensure_ascii=False),
        encoding="utf-8",
    )


# LLM: test_parent_acceptance_plan_normalizes_safe_cd_chain_before_safety_preflight covers real runner drift.
# 函数用途: 父级预检先把安全 `cd 目录 && pytest` 拆成 working_dir，避免已受限 pytest 被误判成人审。
def test_parent_acceptance_plan_normalizes_safe_cd_chain_before_safety_preflight():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        root = Path(root_ctx.name)
        package = root / "deliverables" / "product"
        package.mkdir(parents=True)
        _write_output(task, [_pytest_with_cd_test(package)])

        decision = agent.subagents.plan_parent_acceptance(task.id)
        action = agent.subagents.plan_parent_acceptance_next_action(task.id)

        assert decision.decision == "execute_tests"
        assert decision.requires_human_confirmation is False
        assert action.action == "run_tests"


# LLM: test_parent_acceptance_auto_execution_runs_normalized_safe_cd_chain keeps manual execution aligned with preflight.
# 函数用途: 显式执行 tests 时也复用归一化层，安全 cwd 包装能真实运行但不放开 shell。
def test_parent_acceptance_auto_execution_runs_normalized_safe_cd_chain():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        root = Path(root_ctx.name)
        package = root / "deliverables" / "product"
        package.mkdir(parents=True)
        (package / "test_example.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        _write_output(task, [_pytest_with_cd_test(package)])

        result = agent.subagents.plan_parent_acceptance_auto_execution(
            task.id,
            options=ParentAcceptanceAutoExecutionOptions(execute_tests=True),
        )

        report_payload = json.loads(Path(task.reports_dir, "test_execution.json").read_text(encoding="utf-8"))
        record = report_payload["records"][0]
        assert result.status == "tests_executed"
        assert result.test_failed == 0
        assert record["command"] == "python3 -m pytest test_example.py -q"
        assert record["validation_result"]["working_dir"] == str(package.resolve())


# LLM: _pytest_with_cd_test returns the exact command shape emitted by real runner models.
# 函数用途: 构造同时带 leading cd 和 working_dir 的 pytest 测试项，覆盖真实 E2E 漂移。
def _pytest_with_cd_test(package: Path) -> dict[str, str]:
    return {
        "name": "pytest with cwd",
        "validation_method": "command",
        "command": f"cd {package} && python3 -m pytest test_example.py -q",
        "working_dir": str(package),
    }
