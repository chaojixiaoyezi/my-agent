from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.scheduler.repository import SchedulerRepository
from agent_py_agent.agent.scheduler.tool import ScheduleTool


class _Snapshot:
    def __init__(self) -> None:
        self.entry = SimpleNamespace(
            stable_id="shared:reporting",
            name="reporting",
            content_sha256="a" * 64,
        )

    def resolve(self, reference: str):
        return self.entry if reference in {self.entry.stable_id, self.entry.name} else None


def _agent(tmp_path, *, with_thread: bool = True):
    repository = SchedulerRepository(
        tmp_path / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="user-1",
        default_timezone="Asia/Shanghai",
    )
    attrs = (
        {"conversation_thread_id": "thread-1", "conversation_task_id": "task-1"}
        if with_thread
        else {}
    )
    return SimpleNamespace(
        scheduler_repository=repository,
        config=SimpleNamespace(timezone="Asia/Shanghai"),
        _current_run_params=RunParams(
            request_id="request-1",
            task_id="task-1",
            task_attributes=attrs,
        ),
        current_skill_snapshot=lambda: _Snapshot(),
        conversation_store=None,
    )


def _payload(result) -> dict[str, object]:
    return json.loads(result.output)


def test_schedule_tool_crud_uses_current_thread_and_hides_owner_paths(tmp_path) -> None:
    agent = _agent(tmp_path)
    tool = ScheduleTool(agent)
    created = tool.execute(
        {
            "action": "create",
            "name": "日报",
            "prompt": "整理今天的工作并回复我",
            "schedule_kind": "every",
            "every_seconds": 600,
            "skill_ids": ["reporting"],
            "__tool_call_id": "call-1",
        }
    )
    assert created.ok is True
    body = _payload(created)
    job = body["job"]
    assert job["skill_refs"] == [{"stable_id": "shared:reporting", "content_sha256": "a" * 64}]
    serialized = json.dumps(body, ensure_ascii=False)
    assert "thread-1" not in serialized
    assert str(tmp_path) not in serialized

    repeated = tool.execute(
        {
            "action": "create",
            "name": "日报",
            "prompt": "整理今天的工作并回复我",
            "schedule_kind": "every",
            "every_seconds": 600,
            "skill_ids": ["reporting"],
            "__tool_call_id": "call-1",
        }
    )
    assert _payload(repeated)["deduplicated"] is True

    listed = _payload(tool.execute({"action": "list"}))
    assert listed["jobs"][0]["prompt_preview"] == "整理今天的工作并回复我"
    paused = tool.execute(
        {
            "action": "pause",
            "job_id": job["job_id"],
            "expected_version": job["version"],
        }
    )
    assert _payload(paused)["job"]["status"] == "paused"


def test_schedule_create_refuses_to_guess_thread_or_skill(tmp_path) -> None:
    no_thread = ScheduleTool(_agent(tmp_path, with_thread=False)).execute(
        {
            "action": "create",
            "name": "提醒",
            "prompt": "提醒我",
            "schedule_kind": "every",
            "every_seconds": 600,
        }
    )
    assert no_thread.ok is False
    assert no_thread.error_code == "SCHEDULER_THREAD_REQUIRED"

    missing_skill = ScheduleTool(_agent(tmp_path)).execute(
        {
            "action": "create",
            "name": "提醒",
            "prompt": "提醒我",
            "schedule_kind": "every",
            "every_seconds": 600,
            "skill_ids": ["private:missing"],
        }
    )
    assert missing_skill.ok is False
    assert missing_skill.error_code == "SCHEDULER_SKILL_NOT_AVAILABLE"

    invalid_grace = ScheduleTool(_agent(tmp_path)).execute(
        {
            "action": "create",
            "name": "提醒",
            "prompt": "提醒我",
            "schedule_kind": "every",
            "every_seconds": 600,
            "misfire_grace_seconds": "not-an-integer",
        }
    )
    assert invalid_grace.ok is False
    assert invalid_grace.error_code == "SCHEDULER_INVALID_SCHEDULE"


def test_schedule_mutations_require_version_and_run_now_is_durable(tmp_path) -> None:
    agent = _agent(tmp_path)
    tool = ScheduleTool(agent)
    job = _payload(
        tool.execute(
            {
                "action": "create",
                "name": "test",
                "prompt": "test now",
                "schedule_kind": "every",
                "every_seconds": 600,
            }
        )
    )["job"]
    denied = tool.execute({"action": "delete", "job_id": job["job_id"]})
    assert denied.ok is False
    assert denied.error_code == "TOOL_INVALID_ARGUMENTS"

    run = _payload(tool.execute({"action": "run_now", "job_id": job["job_id"]}))["run"]
    assert run["status"] == "queued"
    assert run["run_id"].startswith("srun_")
