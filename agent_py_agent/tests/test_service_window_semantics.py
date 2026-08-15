from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

# A4 持续型委派语义(底座提升)防回归:盯守(无终态持续任务)派给子代理后,子代理按
# "做完即退"提前 DONE,整任务停摆(真机 u-t1b:只 18 条,wake_queue 有 subagent-finished
# DONE)。三把钉子:①声明端 service_window_seconds 随 attributes 透传;②子代理收口层
# 窗口未走完不因"落一次产物"提前收口;③父代理 wake 载荷带"窗口未走完"结构化事实。
from agent_py_agent.agent.agent_core.orchestration.create_policy import _create_attributes
from agent_py_agent.agent.agent_core.subagent.progress_closeout import (
    _progress_ready_for_closeout,
)
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_ATTR,
    AUDIT_DEADLINE_ATTR,
    AUDIT_SOURCE_BINDING_PENDING_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_REQUEST_ID_ATTR,
)
from agent_py_agent.agent.subagents.models import (
    FailureType,
    TaskStatus,
    VerificationStatus,
)
from agent_py_agent.agent.subagents.runner_completion_wake import (
    _metadata,
    _summary,
    notify_parent_on_runner_result,
)
from agent_py_agent.agent.subagents.runner_result_state import (
    _apply_structured_failure_state,
)
from agent_py_agent.agent.subagents.service_window import service_window_remaining_seconds


def _task(*, long_running: bool = True, window: int = 600, created_ago: float = 10.0) -> SimpleNamespace:
    return SimpleNamespace(
        id="run-w1",
        agent_name="watcher",
        attributes={"long_running": long_running, "service_window_seconds": window},
        created_at=time.time() - created_ago,
        output_json="",
        context_packs=[],
    )


def _declare_ready(task: SimpleNamespace, *artifacts) -> None:
    task.attributes["output_files"] = [str(artifact) for artifact in artifacts]
    task.attributes["artifact_registry_refs"] = [
        {
            "run_id": task.id,
            "path": str(artifact),
            "status": "ready",
            "size_bytes": artifact.stat().st_size,
        }
        for artifact in artifacts
    ]


def test_service_window_remaining_semantics():
    assert service_window_remaining_seconds(_task()) > 500
    assert service_window_remaining_seconds(_task(long_running=False)) == 0.0
    assert service_window_remaining_seconds(_task(created_ago=700.0)) == 0.0
    assert service_window_remaining_seconds(SimpleNamespace(attributes={}, created_at=0.0)) == 0.0
    # 坏声明不炸、按无窗口处理。
    bad = SimpleNamespace(attributes={"long_running": True, "service_window_seconds": "abc"}, created_at=time.time())
    assert service_window_remaining_seconds(bad) == 0.0


def test_progress_closeout_suppressed_while_window_open(tmp_path):
    # 窗口未走完:哪怕产物已落盘,也不因"落了一次产物"被系统提前收口。
    artifact = tmp_path / "out.md"
    artifact.write_text("首批发现", encoding="utf-8")
    progress = {"latest_written_path": str(artifact)}
    task = _task()
    _declare_ready(task, artifact)
    assert _progress_ready_for_closeout(progress, task) is False
    # 窗口走完:恢复正常收口判定(声明产物匹配 → 可收口)。
    elapsed = _task(created_ago=700.0)
    _declare_ready(elapsed, artifact)
    assert _progress_ready_for_closeout(progress, elapsed) is True


def test_progress_closeout_is_not_coupled_to_watch_lane_assignment(tmp_path):
    artifact = tmp_path / "out.md"
    artifact.write_text("首批发现", encoding="utf-8")
    progress = {"latest_written_path": str(artifact)}
    task = _task(created_ago=700.0)
    _declare_ready(task, artifact)
    unrelated_watch_state = SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(tmp_path / "owner"))
    )
    assert (
        _progress_ready_for_closeout(
            progress,
            task,
            agent=unrelated_watch_state,
        )
        is True
    )


def test_progress_closeout_requires_every_declared_current_run_artifact(tmp_path):
    report = tmp_path / "report.md"
    evidence = tmp_path / "evidence.json"
    report.write_text("report", encoding="utf-8")
    evidence.write_text("[]", encoding="utf-8")
    progress = {"latest_written_path": str(report)}
    task = _task(created_ago=700.0)
    task.attributes["output_files"] = [str(report), str(evidence)]
    task.attributes["artifact_registry_refs"] = [
        {
            "run_id": task.id,
            "path": str(report),
            "status": "ready",
            "size_bytes": report.stat().st_size,
        }
    ]

    assert _progress_ready_for_closeout(progress, task) is False

    task.attributes["artifact_registry_refs"].append(
        {
            "run_id": task.id,
            "path": str(evidence),
            "status": "ready",
            "size_bytes": evidence.stat().st_size,
        }
    )
    assert _progress_ready_for_closeout(progress, task) is True


def test_progress_closeout_rejects_stale_or_unregistered_artifacts(tmp_path):
    artifact = tmp_path / "index.html"
    artifact.write_text("<html>ready</html>", encoding="utf-8")
    progress = {
        "latest_written_path": str(artifact),
        "artifact_integrity": {
            "kind": "html",
            "ok": True,
            "blocker_codes": [],
            "warning_codes": [],
        },
    }
    task = _task(created_ago=700.0)
    task.attributes["output_files"] = [str(artifact)]
    task.attributes["artifact_registry_refs"] = [
        {
            "run_id": "older-run",
            "path": str(artifact),
            "status": "ready",
            "size_bytes": artifact.stat().st_size,
        }
    ]
    assert _progress_ready_for_closeout(progress, task) is False

    task.attributes["artifact_registry_refs"][0]["run_id"] = task.id
    task.attributes["artifact_registry_refs"][0]["status"] = "pending"
    assert _progress_ready_for_closeout(progress, task) is False

    task.attributes = {"artifact_registry_refs": []}
    assert _progress_ready_for_closeout(progress, task) is False


def test_create_attributes_pass_service_window_through():
    attrs = _create_attributes({"goal": "盯 2 小时", "long_running": True, "service_window_seconds": 7200})
    assert attrs["long_running"] is True
    assert attrs["service_window_seconds"] == 7200
    # 坏值/缺省不进 attributes(零声明零影响)。
    assert "service_window_seconds" not in _create_attributes({"goal": "x", "service_window_seconds": "abc"})
    assert "service_window_seconds" not in _create_attributes({"goal": "x"})


def test_audit_descendant_service_window_is_clamped_to_inherited_deadline():
    deadline = time.time() + 120
    agent = SimpleNamespace(
        _current_run_params=SimpleNamespace(
            task_attributes={
                AUDIT_ATTR: True,
                AUDIT_DEADLINE_ATTR: deadline,
            }
        )
    )

    attrs = _create_attributes(
        {
            "goal": "持续处理来源",
            "long_running": True,
            "service_window_seconds": 3600,
        },
        agent,
    )

    assert attrs[AUDIT_DEADLINE_ATTR] == deadline
    assert 100 <= attrs["service_window_seconds"] <= 120


def test_audit_descendant_shorter_declared_window_is_preserved():
    agent = SimpleNamespace(
        _current_run_params=SimpleNamespace(
            task_attributes={
                AUDIT_ATTR: True,
                AUDIT_DEADLINE_ATTR: time.time() + 120,
            }
        )
    )

    attrs = _create_attributes(
        {
            "goal": "短时复核",
            "long_running": True,
            "service_window_seconds": 30,
        },
        agent,
    )

    assert attrs["service_window_seconds"] == 30


def test_explicit_done_cannot_close_open_service_window():
    task = _task()
    task.status = TaskStatus.DONE.value
    task.verification_status = VerificationStatus.VERIFIED.value
    task.failure_type = ""
    task.blockers = []
    task.ended_at = time.time()
    task.capability_requests = []
    parsed = SimpleNamespace(capability_requests=[], status=TaskStatus.DONE.value)

    _apply_structured_failure_state(task, "", parsed)

    assert task.status == TaskStatus.PENDING.value
    assert task.verification_status == VerificationStatus.UNVERIFIED.value
    assert task.failure_type == FailureType.INCOMPLETE_DELIVERABLES.value
    assert task.ended_at == 0.0


def test_explicit_done_is_accepted_after_service_window_elapsed():
    task = _task(created_ago=700.0)
    task.status = TaskStatus.DONE.value
    task.verification_status = VerificationStatus.VERIFIED.value
    task.failure_type = ""
    task.blockers = []
    task.ended_at = time.time()
    task.capability_requests = []
    parsed = SimpleNamespace(capability_requests=[], status=TaskStatus.DONE.value)

    _apply_structured_failure_state(task, "", parsed)

    assert task.status == TaskStatus.DONE.value
    assert task.verification_status == VerificationStatus.VERIFIED.value
    assert task.failure_type == ""


def test_create_tool_passes_service_window_into_created_run(monkeypatch):
    # 工具层端到端:create_subagents 声明的窗口经 create_policy 落进 create_run 的
    # attributes(mock manager 捕获真实调用参数,验证声明→任务落盘链)。
    from pathlib import Path
    from unittest.mock import MagicMock

    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    mock_agent = MagicMock()
    mock_agent.config.enable_subagents = True
    mock_agent.config.max_subagents = 10
    mock_agent.subagents.workspace_root = Path("/tmp/project")
    mock_agent.subagents.workspace_roots = [Path("/tmp/project")]
    mock_agent.subagents.workspace = Path("/tmp/project/.my-agent/subagents")
    created = MagicMock()
    created.id = "run_w"
    created.goal = "盯 2 小时"
    created.status = "PLANNING"
    created.verification_status = "UNVERIFIED"
    created.task_dir = "/tmp/run_w"
    mock_agent.subagents.create_run.return_value = created
    tool = CreateSubagentsTool(mock_agent)
    result = tool.execute(
        {"goal": "盯 2 小时", "long_running": True, "service_window_seconds": 7200, "defer_start": True}
    )
    assert result.ok, result.output
    params = mock_agent.subagents.create_run.call_args.kwargs.get("params")
    if params is None:
        params = mock_agent.subagents.create_run.call_args.args[0]
    assert params.attributes.get("long_running") is True
    assert params.attributes.get("service_window_seconds") == 7200


def test_wake_payload_carries_window_incomplete_fact():
    task = _task()
    result = SimpleNamespace(status="DONE", verification_status="UNVERIFIED", result_json="", run_id="run-w1")
    meta = _metadata(task, result, {})
    assert meta["service_window_incomplete"] is True
    assert meta["service_window_remaining_seconds"] > 0
    assert "service window" in _summary(task, result, "DONE")
    assert "dispatch_subagents" not in _summary(task, result, "DONE")
    assert "create_subagents" not in _summary(task, result, "DONE")
    # 窗口走完的正常完成:载荷不带该事实,概要不吓唬人。
    done = _task(created_ago=700.0)
    meta_done = _metadata(done, result, {})
    assert "service_window_incomplete" not in meta_done
    assert "service window" not in _summary(done, result, "DONE")


def test_wake_payload_marks_structured_audit_source_worker_lifecycle():
    task = _task()
    audit_id = "audit-1"
    watch_id = "watch-1"
    task.attributes.update(
        {
            AUDIT_SOURCE_WORKER_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_ID_ATTR: "source-1",
            AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
                audit_id,
                watch_id,
            ),
            AUDIT_SOURCE_OWNER_HOME_ATTR: "/owners/user-1",
        }
    )
    result = SimpleNamespace(
        status="TIMEOUT",
        verification_status="UNVERIFIED",
        result_json="",
        run_id=task.id,
    )
    task.failure_type = "provider_quota_exhausted"

    metadata = _metadata(task, result, {})

    assert metadata["audit_source_worker"] is True
    assert metadata["audit_source_worker_phase"] == "bound"
    assert metadata["failure_type"] == "provider_quota_exhausted"
    assert metadata["audit_id"] == audit_id
    assert metadata["source_id"] == "source-1"
    assert metadata["watch_id"] == watch_id
    assert metadata["worker_key"] == audit_source_worker_key(
        audit_id,
        watch_id,
    )


def test_wake_payload_marks_pending_audit_source_binding_lifecycle():
    task = _task()
    task.attributes.update(
        {
            AUDIT_ATTR: True,
            AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-pending",
            AUDIT_SOURCE_ID_ATTR: "source-pending",
        }
    )
    result = SimpleNamespace(
        status=TaskStatus.BLOCKED.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        result_json="",
        run_id=task.id,
    )
    task.failure_type = "status_blocked"

    metadata = _metadata(task, result, {})

    assert metadata["audit_source_worker"] is True
    assert metadata["audit_source_worker_phase"] == "binding_pending"
    assert metadata["audit_id"] == "audit-pending"
    assert metadata["source_id"] == "source-pending"
    assert metadata["watch_id"] == ""
    assert metadata["worker_key"] == ""


def test_pending_source_continuation_does_not_enter_parent_wake_lane():
    class _Store:
        def thread_for_task(self, _task_id):
            raise AssertionError("PENDING source continuation must not publish a wake")

    task = _task()
    task.status = "PENDING"
    task.attributes.update(
        {
            AUDIT_ATTR: True,
            AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-root",
            "runner_session": {
                "session_id": "runsess-3",
                "status": "completed",
            },
        }
    )
    result = SimpleNamespace(
        status="PENDING",
        verification_status="UNVERIFIED",
        result_json="",
        run_id=task.id,
        dry_run=False,
    )

    notify_parent_on_runner_result(
        SimpleNamespace(conversation_store=_Store()),
        task,
        result,
        {},
    )


@pytest.mark.parametrize("created_ago", [10.0, 700.0])
def test_completed_audit_source_slice_updates_state_and_wakes_only_supervisor(
    created_ago,
):
    class _Store:
        def __init__(self):
            self.status_updates = []
            self.wakes = []

        def thread_for_task(self, _task_id):
            return SimpleNamespace(thread_id="thread-audit")

        def update_task_status(self, payload):
            self.status_updates.append(dict(payload))

        def append_observation_with_wake(self, *_args, **_kwargs):
            raise AssertionError("normal Audit slice must not publish a parent wake")

        def raise_wake_signal(self, payload):
            self.wakes.append(dict(payload))

    audit_id = "audit-root"
    watch_id = "watch-source-1"
    task = _task(created_ago=created_ago)
    task.status = TaskStatus.DONE.value
    task.failure_type = ""
    task.parent_id = audit_id
    task.root_id = audit_id
    task.verification_status = VerificationStatus.VERIFIED.value
    task.runner_result_json = ""
    task.attributes.update(
        {
            AUDIT_ATTR: True,
            AUDIT_SOURCE_WORKER_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_ID_ATTR: "source-1",
            AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
                audit_id,
                watch_id,
            ),
            AUDIT_SOURCE_OWNER_HOME_ATTR: "/owners/user-1",
        }
    )
    result = SimpleNamespace(
        status=TaskStatus.DONE.value,
        verification_status=VerificationStatus.VERIFIED.value,
        result_json="",
        run_id=task.id,
        dry_run=False,
    )
    store = _Store()

    notify_parent_on_runner_result(
        SimpleNamespace(conversation_store=store),
        task,
        result,
        {},
    )

    assert store.status_updates == [
        {"task_id": task.id, "status": TaskStatus.DONE.value}
    ]
    assert len(store.wakes) == 1
    wake = store.wakes[0]
    assert wake["reason"] == "subagent_runner_finished"
    assert wake["root_task_id"] == audit_id
    assert wake["metadata"]["audit_source_worker"] is True
    assert wake["metadata"]["status"] == TaskStatus.DONE.value


def test_retryable_audit_provider_failure_updates_state_without_parent_wake():
    class _Store:
        def __init__(self):
            self.status_updates = []
            self.wakes = []

        def thread_for_task(self, _task_id):
            return SimpleNamespace(thread_id="thread-audit")

        def update_task_status(self, payload):
            self.status_updates.append(dict(payload))

        def append_observation_with_wake(self, *_args, **_kwargs):
            raise AssertionError("retryable provider failure must stay in supervisor")

        def raise_wake_signal(self, payload):
            self.wakes.append(dict(payload))

    audit_id = "audit-root"
    watch_id = "watch-source-1"
    task = _task()
    task.status = TaskStatus.BLOCKED.value
    task.failure_type = FailureType.PROVIDER_TIMEOUT.value
    task.parent_id = audit_id
    task.root_id = audit_id
    task.verification_status = VerificationStatus.UNVERIFIED.value
    task.runner_result_json = ""
    task.attributes.update(
        {
            AUDIT_ATTR: True,
            AUDIT_SOURCE_WORKER_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_ID_ATTR: "source-1",
            AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
                audit_id,
                watch_id,
            ),
            AUDIT_SOURCE_OWNER_HOME_ATTR: "/owners/user-1",
        }
    )
    result = SimpleNamespace(
        status=TaskStatus.BLOCKED.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        result_json="",
        run_id=task.id,
        dry_run=False,
    )
    store = _Store()

    notify_parent_on_runner_result(
        SimpleNamespace(conversation_store=store),
        task,
        result,
        {},
    )

    assert store.status_updates == [
        {"task_id": task.id, "status": TaskStatus.BLOCKED.value}
    ]
    assert len(store.wakes) == 1
    assert store.wakes[0]["metadata"]["failure_type"] == FailureType.PROVIDER_TIMEOUT.value


def test_pending_audit_source_failure_wakes_only_supervisor():
    class _Store:
        def __init__(self):
            self.status_updates = []
            self.wakes = []

        def thread_for_task(self, _task_id):
            return SimpleNamespace(thread_id="thread-audit")

        def update_task_status(self, payload):
            self.status_updates.append(dict(payload))

        def append_observation_with_wake(self, *_args, **_kwargs):
            raise AssertionError("pending Audit source failure must stay internal")

        def raise_wake_signal(self, payload):
            self.wakes.append(dict(payload))

    task = _task()
    task.status = TaskStatus.BLOCKED.value
    task.failure_type = "status_blocked"
    task.parent_id = "audit-pending"
    task.root_id = "audit-pending"
    task.verification_status = VerificationStatus.UNVERIFIED.value
    task.runner_result_json = ""
    task.attributes.update(
        {
            AUDIT_ATTR: True,
            AUDIT_SOURCE_BINDING_PENDING_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: "audit-pending",
            AUDIT_SOURCE_ID_ATTR: "source-pending",
        }
    )
    result = SimpleNamespace(
        status=TaskStatus.BLOCKED.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        result_json="",
        run_id=task.id,
        dry_run=False,
    )
    store = _Store()

    notify_parent_on_runner_result(
        SimpleNamespace(conversation_store=store),
        task,
        result,
        {},
    )

    assert store.status_updates == [
        {"task_id": task.id, "status": TaskStatus.BLOCKED.value}
    ]
    assert len(store.wakes) == 1
    assert store.wakes[0]["metadata"]["audit_source_worker_phase"] == "binding_pending"


def test_pending_ordinary_agent_does_not_publish_continuation_wake():
    class _Store:
        def thread_for_task(self, _task_id):
            raise AssertionError("ordinary PENDING task must not publish a wake")

    task = _task()
    task.status = "PENDING"
    result = SimpleNamespace(
        status="PENDING",
        verification_status="UNVERIFIED",
        result_json="",
        run_id=task.id,
        dry_run=False,
    )

    notify_parent_on_runner_result(
        SimpleNamespace(conversation_store=_Store()),
        task,
        result,
        {},
    )
