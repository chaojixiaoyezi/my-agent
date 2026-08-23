from __future__ import annotations

"""task_progress is a bounded advisory ledger, never a host continuation gate."""

import json

from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
    build_task_progress_model_spec,
)
from agent_py_agent.agent.agent_core.task_progress_tool import TaskProgressTool
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.task_progress import write_task_progress


class _CaptureBackend:
    name = "task-progress-advisory"

    def __init__(self) -> None:
        self.prompts: list[str] = []

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
        return ModelResponse(text="这一回合的最终回复。", backend=self.name)


def _agent(tmp_path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            tool_protocol="native",
        ),
        tmp_path,
    )


def test_open_progress_ledger_does_not_call_model_again(tmp_path) -> None:
    agent = _agent(tmp_path)
    backend = _CaptureBackend()
    agent.backend = backend
    write_task_progress(
        agent.home_paths.owner_home_dir,
        "task-progress-run",
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
    assert result.runtime_status == "ok"
    assert not hasattr(result, "task_progress_auto_continued")


def test_task_progress_schema_leaves_nested_validation_to_handler() -> None:
    schema = build_task_progress_model_spec().input_schema
    item_schema = schema["properties"]["items"]["items"]

    assert "required" not in item_schema
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
    assert payload["execution_guidance"]["open_item_ids"] == ["req-core", "req-test"]
    assert "不要用列出未完成项代替继续工作" in payload["execution_guidance"]["message"]
    assert "covers" in payload["execution_guidance"]["message"]
    assert "correction=true" in payload["execution_guidance"]["message"]
    assert "不能拿无关 open id 顶替" in payload["execution_guidance"]["message"]


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
