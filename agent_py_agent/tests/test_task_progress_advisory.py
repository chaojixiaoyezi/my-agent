from __future__ import annotations

"""task_progress is a bounded advisory ledger, never a host continuation gate."""

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
