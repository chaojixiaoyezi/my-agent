from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.scheduler.repository import SchedulerRepository
from agent_py_agent.agent.scheduler.tool import (
    ScheduleTool,
    _schedule_from_params,
    build_schedule_tool_model_spec,
)


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
    owner_home_dir = tmp_path / "home" / "feishu" / "user-1"
    owner_home_dir.mkdir(parents=True, exist_ok=True)
    RuntimeRepository(runtime_db_path(owner_home_dir))  # 建权威库（真实 owner 必有）
    return SimpleNamespace(
        scheduler_repository=repository,
        config=SimpleNamespace(timezone="Asia/Shanghai"),
        home_paths=SimpleNamespace(
            owner_home_dir=owner_home_dir,
            owner_id="feishu/user-1",
        ),
        model_provider="opencode",
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


def test_schedule_tool_schema_requires_absolute_iso_string_for_one_shot() -> None:
    spec = build_schedule_tool_model_spec()
    schema = spec.input_schema["properties"]["at"]
    assert schema["type"] == "string"
    assert "absolute ISO-8601" in schema["description"]
    assert "relative seconds" in schema["description"]
    relative = spec.input_schema["properties"]["after_seconds"]
    assert relative["type"] == "integer"
    assert relative["minimum"] == 1
    assert "resolved absolute instant" in relative["description"]


def test_relative_one_shot_is_structurally_resolved_to_absolute_time(
    tmp_path, monkeypatch
) -> None:
    agent = _agent(tmp_path)
    schedule = _schedule_from_params(
        agent,
        {"schedule_kind": "at", "after_seconds": 90, "timezone": "UTC"},
        now=1_784_423_093,
    )
    assert schedule == {
        "kind": "at",
        "at": "2026-07-19T01:06:23Z",
        "timezone": "UTC",
    }

    monkeypatch.setattr(
        "agent_py_agent.agent.scheduler.tool.time.time", lambda: 1_784_423_093
    )
    created = ScheduleTool(agent).execute(
        {
            "action": "create",
            "name": "90 秒后提醒",
            "prompt": "提醒用户检查备份",
            "schedule_kind": "at",
            "after_seconds": 90,
            "timezone": "UTC",
            "__tool_call_id": "relative-90-seconds",
        }
    )
    assert created.ok is True
    assert _payload(created)["job"]["schedule"] == schedule

    for bad in (
        {"schedule_kind": "at", "at": "2026-07-19T01:10:00Z", "after_seconds": 90},
        {"schedule_kind": "every", "after_seconds": 90},
        {"schedule_kind": "at", "after_seconds": "90"},
        {"schedule_kind": "at", "at": 1_784_423_200},
    ):
        result = ScheduleTool(agent).execute(
            {"action": "create", "name": "bad", "prompt": "bad", **bad}
        )
        assert result.ok is False
        assert result.error_code == "SCHEDULER_INVALID_SCHEDULE"


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
