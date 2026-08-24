from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runtime.task_identity import (
    task_path_progress_ledger_id,
)
from agent_py_agent.agent.agent_core.tool_loop.plan_closeout import (
    decide_open_plan_closeout,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.task_progress import write_task_progress


def _agent(root: Path, *, attempts: object = 1):
    owner = root / "owner"
    owner.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        root=root,
        home_paths=SimpleNamespace(owner_home_dir=owner),
        config=SimpleNamespace(
            enable_tools=True,
            task_progress_closeout_repair_attempts=attempts,
        ),
    )


def _params(
    *,
    context_scope: str = "default",
    task_attributes: dict[str, object] | None = None,
    save: bool = True,
    source: str = "run",
    run_id: str = "run-1",
    task_id: str = "task-1",
) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="完成复杂任务",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes=task_attributes or {},
        request_id="request-1",
        run_id=run_id,
        task_id=task_id,
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        save=save,
        context_scope=context_scope,
        source=source,
    )


def _response(text: str = "已经全部完成。") -> ModelResponse:
    return ModelResponse(text=text, backend="fake")


def test_open_plan_final_is_returned_to_same_turn_once(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _params()
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "run-1",
        {
            "summary": "已经完成五项",
            "next_action": "继续运行测试",
            "items": [
                {"id": "build", "title": "构建项目", "status": "done"},
                {"id": "tests", "title": "运行全部测试", "status": "pending"},
            ],
        },
    )

    decision = decide_open_plan_closeout(agent, params, _response())

    assert decision.action == "continue"
    assert decision.response is None
    assert len(params.tool_context) == 1
    assert "task-progress-closeout-reconciliation" in params.tool_context[0]
    assert '"id":"tests"' in params.tool_context[0]
    assert '"id":"build"' not in params.tool_context[0]
    state = params.live_archive_state["task_progress_closeout_reconciliation"]
    assert state["attempts"] == 1
    assert state["last_open_count"] == 1


def test_reconciliation_exhaustion_blocks_instead_of_marking_done(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _params()
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "run-1",
        {"items": [{"id": "tests", "title": "运行测试", "status": "pending"}]},
    )
    first = decide_open_plan_closeout(agent, params, _response("第一次完成草稿"))
    assert first.action == "continue"

    second = decide_open_plan_closeout(agent, params, _response("仍然声称完成"))

    assert second.action == "block"
    assert second.response is not None
    assert second.response.text == "仍然声称完成"
    assert second.response.runtime_status == "blocked"
    assert second.response.runtime_reason == "TASK_PROGRESS_RECONCILIATION_EXHAUSTED"
    assert second.response.runtime_source == "task_progress"
    assert second.response.turn_end_reason == "blocked"


def test_reconciled_plan_allows_model_final(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _params()
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "run-1",
        {"items": [{"id": "tests", "title": "运行测试", "status": "pending"}]},
    )
    assert decide_open_plan_closeout(agent, params, _response()).action == "continue"
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "run-1",
        {"items": [{"id": "tests", "status": "done", "notes": "测试通过"}]},
    )

    decision = decide_open_plan_closeout(agent, params, _response("现在真实完成"))

    assert decision.action == "ignore"
    assert decision.response is None


def test_explicitly_blocked_plan_ends_as_typed_blocked_without_retry(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _params()
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "run-1",
        {
            "items": [
                {
                    "id": "compile",
                    "title": "编译验证",
                    "status": "blocked",
                    "notes": "缺少工具链",
                }
            ]
        },
    )

    decision = decide_open_plan_closeout(agent, params, _response("当前无法编译"))

    assert decision.action == "block"
    assert decision.response is not None
    assert decision.response.runtime_reason == "TASK_PROGRESS_BLOCKED"
    assert params.tool_context == []


def test_open_coverage_target_participates_in_reconciliation(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _params()
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "run-1",
        {
            "coverage": {
                "targets": [
                    {
                        "id": "integration",
                        "title": "集成测试",
                        "status": "in_progress",
                        "checks": {"build": "done", "e2e": "pending"},
                    }
                ]
            }
        },
    )

    decision = decide_open_plan_closeout(agent, params, _response())

    assert decision.action == "continue"
    assert '"id":"integration"' in params.tool_context[0]
    assert '"surface":"coverage_target"' in params.tool_context[0]


def test_zero_config_and_auxiliary_scope_disable_closeout_repair(tmp_path: Path) -> None:
    disabled = _agent(tmp_path / "disabled", attempts=0)
    disabled_params = _params()
    write_task_progress(
        disabled.home_paths.owner_home_dir,
        "run-1",
        {"items": [{"id": "pending", "title": "待办", "status": "pending"}]},
    )
    assert decide_open_plan_closeout(disabled, disabled_params, _response()).action == "ignore"

    isolated = _agent(tmp_path / "isolated")
    isolated_params = _params(context_scope="isolated")
    write_task_progress(
        isolated.home_paths.owner_home_dir,
        "run-1",
        {"items": [{"id": "pending", "title": "待办", "status": "pending"}]},
    )
    assert decide_open_plan_closeout(isolated, isolated_params, _response()).action == "ignore"


def test_missing_active_turn_state_fails_open_without_retry_loop(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _params()
    object.__setattr__(params, "live_archive_state", None)
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "run-1",
        {"items": [{"id": "pending", "title": "待办", "status": "pending"}]},
    )

    assert decide_open_plan_closeout(agent, params, _response()).action == "ignore"


def test_explicit_goal_keeps_its_existing_cross_turn_lifecycle(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    params = _params(task_attributes={"thread_goal_id": "goal-1"})
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "run-1",
        {"items": [{"id": "pending", "title": "待办", "status": "pending"}]},
    )

    decision = decide_open_plan_closeout(agent, params, _response())

    assert decision.action == "ignore"
    assert params.tool_context == []


# LLM: Regression for the real TUI/background failure where save=False skipped
# the stop hook even though the same durable task-path ledger remained open.
# 函数用途: 模拟后台主代理续跑使用新 run 身份且不存档，验证它仍读取原任务目录的 Todo。
def test_background_no_save_turn_reconciles_canonical_task_path_plan(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    task_root = tmp_path / "durable-task"
    task_root.mkdir()

    class _Store:
        def load_task_link(self, task_id: str) -> object | None:
            if task_id != "root-task":
                return None
            return SimpleNamespace(task_path=str(task_root))

    agent.conversation_store = _Store()
    ledger_id = task_path_progress_ledger_id(task_root)
    write_task_progress(
        agent.home_paths.owner_home_dir,
        ledger_id,
        {"items": [{"id": "verify", "title": "完成集成验证", "status": "pending"}]},
    )
    params = _params(
        save=False,
        source="background_main_agent",
        run_id="background-attempt-2",
        task_id="root-task",
        task_attributes={
            "conversation_thread_id": "thread-1",
            "conversation_task_id": "root-task",
        },
    )

    first = decide_open_plan_closeout(agent, params, _response("后台声称完成"))
    second = decide_open_plan_closeout(agent, params, _response("后台仍声称完成"))

    assert first.action == "continue"
    assert '"ledger_id":"' + ledger_id + '"' in params.tool_context[0]
    assert second.action == "block"
    assert second.response is not None
    assert second.response.runtime_reason == "TASK_PROGRESS_RECONCILIATION_EXHAUSTED"
