from __future__ import annotations

"""task_progress stays advisory and never adds a hidden ordinary-task model turn."""

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
    build_task_progress_model_spec,
)
from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.task_progress import (
    merge_task_progress,
    progress_path,
    read_task_progress,
    rebind_task_progress_display_plan,
    task_progress_display_identity,
    task_progress_display_items,
    with_task_progress_display_plan,
    write_task_progress,
)


@pytest.mark.parametrize("same_thread,child,detached", [(True, False, False), (False, False, False), (True, True, False), (True, False, True)])
def test_explicit_progress_update_targets_existing_same_thread_plan(tmp_path, same_thread, child, detached):
    from agent_py_agent.agent.agent_core.runtime.task_identity import task_path_progress_ledger_id

    agent = _agent(tmp_path)
    store = agent.conversation_store
    thread = store.get_or_create_thread({
        "channel": "chat", "channel_conversation_id": "original-session", "canonical_user_id": "owner",
    })
    root = agent.home_paths.owner_home_dir
    work = root / "tasks" / "previous-project"
    work.mkdir(parents=True)
    store.bind_task({
        "thread_id": thread.thread_id, "task_id": "original-task", "goal": "原任务",
        "task_path": str(work), "cancellation_scope": "detached" if detached else "foreground",
    })
    store.update_task_status({"task_id": "original-task", "status": "completed"})
    ledger = task_path_progress_ledger_id(work)
    write_task_progress(root, ledger, {"items": [{
        "id": "verify", "title": "验证", "status": "done", "notes": "旧验证记录",
    }]})
    agent._current_run_params = SimpleNamespace(
        context_scope="subagent" if child else "conversation", run_id="current-run", task_id="current-run",
        task_attributes={"conversation_thread_id": thread.thread_id if same_thread else "another-thread"},
    )
    before = progress_path(root, ledger).read_bytes()
    result = TaskProgressTool(agent).execute({
        "action": "update", "run_id": ledger, "items": [{"id": "verify", "notes": "补充验证结果"}],
    })
    allowed = same_thread and not child and not detached
    assert result.ok is allowed
    if allowed:
        item = read_task_progress(root, ledger)["items"][0]
        assert item["status"] == "done"
        assert item["notes"] == "补充验证结果"
    else:
        assert result.error_code == "TOOL_INVALID_ARGUMENTS"
        assert json.loads(result.output)["reason"] == "task_progress_scope_mismatch"
        assert progress_path(root, ledger).read_bytes() == before
    assert not progress_path(root, "current-run").exists()
    assert store.load_task_link("original-task").status == "completed"
    assert "conversation_task_id" not in agent._current_run_params.task_attributes


def test_explicit_unknown_progress_target_does_not_create_or_rebind(tmp_path):
    agent = _agent(tmp_path)
    agent._main_agent_run_id = "current-run"
    result = TaskProgressTool(agent).execute({
        "action": "update", "run_id": "other-owner-plan",
        "items": [{"id": "1", "title": "新计划", "status": "pending"}],
    })
    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert json.loads(result.output)["reason"] == "task_progress_scope_mismatch"
    assert not progress_path(agent.home_paths.owner_home_dir, "other-owner-plan").exists()


def test_explicit_current_progress_target_keeps_same_scope(tmp_path):
    agent = _agent(tmp_path)
    agent._main_agent_run_id = "current-run"
    result = TaskProgressTool(agent).execute({
        "action": "update", "run_id": "current-run", "items": [{"id": "1", "title": "新计划"}],
    })
    assert result.ok is True
    assert json.loads(result.output)["run_id"] == "current-run"


class _CaptureBackend:
    name = "task-progress-advisory"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.messages: list[object] = []

    def probe_tool_capability(self):
        from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability

        return ProviderToolCapability(
            provider=self.name,
            endpoint="local://task-progress-advisory",
            model="",
            stream=False,
            native_supported=True,
            evidence="test_capture_native",
        )

    def generate(self, prompt: str, on_chunk=None, **kwargs):  # noqa: ARG002
        self.prompts.append(prompt)
        self.messages.append(kwargs.get("messages"))
        return ModelResponse(text="这一回合的最终回复。", backend=self.name)


def _agent(tmp_path, **config_overrides) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            tool_protocol="native",
            **config_overrides,
        ),
        tmp_path,
    )


def test_open_progress_ledger_does_not_reconcile_or_resume_ordinary_final(tmp_path) -> None:
    agent = _agent(tmp_path)
    backend = _CaptureBackend()
    agent.backend = backend
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "run-progress-run",
        {
            "items": [
                {
                    "id": "core",
                    "title": "核心模块",
                    "status": "in_progress",
                }
            ]
        },
    )

    result = agent.run(
        "继续普通任务",
        save=True,
        request_id="req-progress-run",
        run_id="run-progress-run",
        task_id="task-progress-run",
    )

    assert len(backend.prompts) == 1
    assert "task-progress-closeout-reconciliation" not in json.dumps(
        backend.messages[0], ensure_ascii=False
    )
    assert result.response == "这一回合的最终回复。"
    assert result.runtime_status == "ok"
    assert result.runtime_reason == ""
    assert not hasattr(result, "task_progress_auto_continued")


def test_task_progress_schema_requires_id_but_allows_partial_updates() -> None:
    schema = build_task_progress_model_spec().input_schema
    item_schema = schema["properties"]["items"]["items"]

    assert item_schema["required"] == ["id"]
    assert "additionalProperties" not in item_schema
    assert "unknown" not in item_schema["properties"]
    checks = schema["properties"]["coverage"]["properties"]["targets"]["items"][
        "properties"
    ]["checks"]
    assert checks["additionalProperties"] == {"type": "string"}


def test_new_progress_item_requires_title_but_existing_item_can_update_by_id(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent._main_agent_run_id = "run-main"
    tool = TaskProgressTool(agent)

    rejected = tool.execute(
        {"action": "update", "items": [{"id": "blank-row", "status": "pending"}]}
    )
    created = tool.execute(
        {
            "action": "update",
            "items": [
                {"id": "real-row", "title": "真实工作", "status": "in_progress"}
            ],
        }
    )
    updated = tool.execute(
        {"action": "update", "items": [{"id": "real-row", "status": "done"}]}
    )

    assert rejected.ok is False
    assert rejected.error_code == "TOOL_INVALID_ARGUMENTS"
    assert "new_item_title_required" in rejected.output
    assert created.ok is True
    assert updated.ok is True


def test_open_progress_result_gives_soft_continue_and_covers_guidance(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent._main_agent_run_id = "run-main"
    tool = TaskProgressTool(agent)

    result = tool.execute(
        {
            "action": "update",
            "items": [
                {"id": "req-core", "title": "实现核心", "status": "in_progress"},
                {"id": "req-test", "title": "补齐测试", "status": "pending"},
            ],
        }
    )
    payload = json.loads(result.output)

    assert payload["execution_guidance"]["blocking"] is False
    assert payload["execution_guidance"]["schema_version"] == "task-progress-execution-guidance.v2"
    assert payload["execution_guidance"]["open_item_ids"] == ["req-core", "req-test"]
    delegation = payload["execution_guidance"]["delegation_contract"]
    assert delegation["same_work_field"] == "items[].covers"
    assert delegation["same_work_rule"] == "copy_exact_id"
    assert delegation["independent_or_uncertain_rule"] == "omit_covers"
    assert delegation["unbound_completion_followup"]["matching"] == "exact_id_only"
    closeout = payload["execution_guidance"]["closeout_contract"]
    assert closeout["schema_version"] == "task-progress-closeout-guidance.v1"
    assert closeout["blocking"] is False
    assert closeout["open_item_ids"] == ["req-core", "req-test"]
    assert closeout["before_final"]["matching"] == "exact_id_only"
    assert closeout["host_behavior"] == "never_auto_close_never_completion_gate"
    assert "不要用列出未完成项代替继续工作" in payload["execution_guidance"]["message"]
    assert "covers" in payload["execution_guidance"]["message"]
    assert "correction=true" in payload["execution_guidance"]["message"]
    assert "不能拿无关 open id 顶替" in payload["execution_guidance"]["message"]


def test_metadata_only_update_keeps_model_and_display_status(tmp_path) -> None:
    agent = _agent(tmp_path)
    agent._main_agent_run_id = "run-main"
    tool = TaskProgressTool(agent)
    created = tool.execute({
        "action": "update",
        "items": [{"id": "review", "title": "检查实际结果", "status": "in_progress"}],
    })
    updated = tool.execute({
        "action": "update",
        "items": [{"id": "review", "notes": "已确认边界", "overwrite": True}],
    })

    assert created.ok and updated.ok
    assert json.loads(updated.output)["items"][0]["status"] == "in_progress"
    projection = updated.result_envelope["task_progress_projection"]
    assert projection["items"][0]["status"] == "in_progress"


def test_open_progress_closeout_guidance_can_be_disabled(tmp_path) -> None:
    agent = _agent(tmp_path, task_progress_closeout_guidance_enabled=False)
    agent._main_agent_run_id = "run-main"
    result = TaskProgressTool(agent).execute(
        {
            "action": "update",
            "items": [{"id": "req-core", "title": "实现核心", "status": "pending"}],
        }
    )

    payload = json.loads(result.output)
    assert "closeout_contract" not in payload["execution_guidance"]


def test_closed_progress_item_requires_explicit_correction_to_reopen(tmp_path) -> None:
    """返工必须显式 correction；普通更新不能无声冲掉已经记录的完成事实。"""
    agent = _agent(tmp_path)
    agent._main_agent_run_id = "run-main"
    tool = TaskProgressTool(agent)

    created = tool.execute(
        {
            "action": "update",
            "items": [{"id": "git", "title": "Git 模块", "status": "done"}],
        }
    )
    implicit = tool.execute(
        {"action": "update", "items": [{"id": "git", "status": "in_progress"}]}
    )
    corrected = tool.execute(
        {
            "action": "update",
            "items": [
                {"id": "git", "status": "in_progress", "correction": True}
            ],
        }
    )

    assert created.ok is True
    assert json.loads(implicit.output)["items"][0]["status"] == "done"
    assert json.loads(corrected.output)["items"][0]["status"] == "in_progress"


def test_display_plan_switch_hides_unrelated_open_history_without_deleting_it() -> None:
    first = merge_task_progress(
        {},
        with_task_progress_display_plan(
            {
                "items": [
                    {"id": "old-a", "title": "旧任务 A", "status": "done"},
                    {
                        "id": "still-open",
                        "title": "跨回合继续工作",
                        "status": "in_progress",
                    },
                    {"id": "old-b", "title": "旧任务 B", "status": "done"},
                ]
            },
            generation_id="turn-old",
            item_ids=["old-a", "still-open", "old-b"],
        ),
        run_id="durable-task",
    )
    second = merge_task_progress(
        first,
        with_task_progress_display_plan(
            {
                "items": [
                    {"id": "new-a", "title": "新任务 A", "status": "in_progress"}
                ]
            },
            generation_id="turn-new",
            item_ids=["new-a"],
        ),
        run_id="durable-task",
    )
    third = merge_task_progress(
        second,
        with_task_progress_display_plan(
            {
                "items": [
                    {"id": "new-b", "title": "新任务 B", "status": "pending"}
                ]
            },
            generation_id="turn-new",
            item_ids=["new-b"],
        ),
        run_id="durable-task",
    )
    reconciled = merge_task_progress(
        third,
        {"items": [{"id": "new-a", "status": "done"}]},
        run_id="durable-task",
    )

    assert [item["id"] for item in reconciled["items"]] == [
        "old-a",
        "still-open",
        "old-b",
        "new-a",
        "new-b",
    ]
    assert [item["id"] for item in task_progress_display_items(reconciled)] == [
        "new-a",
        "new-b",
    ]
    assert task_progress_display_identity(reconciled) == ("turn-new", 3)


def test_progress_rebind_refuses_a_different_display_generation(tmp_path) -> None:
    source_id = "task-path:source"
    target_id = "task-path:target"
    write_task_progress(
        tmp_path,
        source_id,
        with_task_progress_display_plan(
            {"items": [{"id": "old", "title": "旧回合", "status": "pending"}]},
            generation_id="turn-old",
            item_ids=["old"],
        ),
    )

    result = rebind_task_progress_display_plan(
        tmp_path,
        source_id,
        target_id,
        generation_id="turn-current",
    )

    assert result["status"] == "generation_mismatch"
    assert progress_path(tmp_path, source_id).exists()
    assert not progress_path(tmp_path, target_id).exists()


def test_task_progress_tool_emits_only_current_turn_display_projection(tmp_path) -> None:
    from types import SimpleNamespace

    agent = _agent(tmp_path)
    agent._main_agent_run_id = "run-main"
    agent._current_run_params = SimpleNamespace(
        request_id="attempt-current",
        run_id="run-main",
        task_id="run-main",
        task_attributes={"conversation_request_id": "gateway-turn-current"},
    )
    outcome = TaskProgressTool(agent).execute(
        {
            "action": "update",
            "items": [
                {"id": "current", "title": "当前任务", "status": "in_progress"}
            ],
        }
    )

    assert outcome.ok is True
    assert "display_plan" not in json.loads(outcome.output)
    assert outcome.result_envelope == {
        "task_progress_projection": {
            "generation_id": "gateway-turn-current",
            "plan_revision": 1,
            "items": [
                {"id": "current", "title": "当前任务", "status": "in_progress"}
            ],
        }
    }
