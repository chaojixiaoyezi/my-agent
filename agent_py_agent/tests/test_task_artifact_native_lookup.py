"""原任务登记产物经不带task参数的真实artifact HTTP查询并核版本范围。"""

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.artifacts.registry import ArtifactRegistration, register_artifact
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.tests import test_v13_w0_observation_readers as original
from agent_py_agent.tests.test_acceptance_observation_projections import (
    background_http,
    client_for,
    isolated_home,
    sampled_owner,
)

_FIXTURES = (background_http, isolated_home, sampled_owner)


def _publish(services, task, *, artifact_id="", registry_root=None):
    store = services.host.conversation_store
    root = services.owner_home / "tasks" / "isolated" / task.task_id
    path = root / "output" / "order.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"total_cents": 4200}))
    registry_root = registry_root or root
    registry_root.mkdir(parents=True, exist_ok=True)
    record = register_artifact(
        ArtifactRegistration(
            registry_root,
            path,
            artifact_id=artifact_id,
            task_id=task.task_id,
            mime_type="application/json",
            source="write_file",
            status="ready",
        )
    )
    store.tasks.bind(
        {
            "thread_id": task.target_thread_id,
            "task_id": task.task_id,
            "goal": task.goal,
            "task_path": str(registry_root),
        }
    )
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="accepted"))
    store.session_tasks.advance(
        task.task_id,
        SessionTaskUpdate(
            status="done",
            result_refs=(str(path),),
            result_artifacts=(
                {
                    "path": str(path),
                    "artifact_id": record.artifact_id,
                    "sha256": record.sha256,
                    "size_bytes": record.size_bytes,
                },
            ),
        ),
    )
    return path, record


def test_native_task_artifact_get_uses_original_task_registry_and_verifies_drift(
    background_http, sampled_owner
):  # noqa: F811
    services, task, runtime, _ = sampled_owner
    path, record = _publish(services, task, registry_root=services.owner_home / 'runs' / 'exact-run')
    original.wire(services, runtime)
    client = client_for(background_http)
    result = client.call("artifacts.get", {"artifact_id": record.artifact_id})
    assert result["ok"], result
    artifact = result["data"]["artifact"]
    assert artifact["task_id"] == task.task_id and artifact["content_sha256"] == record.sha256
    assert artifact["available"] and artifact["version_ref"] == record.sha256
    assert json.loads(path.read_text())["total_cents"] == 4200
    foreign = client_for(background_http, user="business-bob").call(
        "artifacts.get", {"artifact_id": record.artifact_id}
    )
    assert not foreign["ok"] and not foreign["data"]
    path.write_text("changed content")
    drift = client.call("artifacts.get", {"artifact_id": record.artifact_id})
    assert not drift["ok"] and drift["error"]["code"] == "STATE_CONFLICT"
    assert "ARTIFACT_CONTENT_DRIFTED" in json.dumps(drift["error"])


def test_duplicate_artifact_id_across_tasks_is_explicit_conflict(background_http, sampled_owner):  # noqa: F811
    services, first, runtime, _ = sampled_owner
    _, record = _publish(services, first, artifact_id="shared-artifact-id")
    store = services.host.conversation_store
    context = BusinessExecutionContext.from_dict(first.metadata["business_context"])
    second = store.session_tasks.create(
        SessionTaskDraft(
            first.sender_thread_id,
            first.target_thread_id,
            "another task",
            dedupe_key="conflict-second",
        ),
        business_context_intent=BusinessContextIntent(
            context.owner_id, context.space_id, None, None
        ),
        business_context_factory=lambda tid: BusinessExecutionContext(
            context.owner_id, context.space_id, None, tid, 1, None, (), (), "business-context.v1"
        ),
    )
    _publish(services, second, artifact_id=record.artifact_id)
    original.wire(services, runtime)
    result = client_for(background_http).call("artifacts.get", {"artifact_id": record.artifact_id})
    assert not result["ok"] and result["error"]["code"] == "STATE_CONFLICT"
    assert "ARTIFACT_TASK_ID_CONFLICT" in json.dumps(result["error"])


def test_old_complete_list_index_backfills_artifact_refs_in_bounded_pages(tmp_path):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.business_observability import ObservationSourceError
    from agent_py_agent.agent.conversation.task_artifact_observation import task_artifact

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(owner_id="owner")
    for index in range(35):
        task = store.session_tasks.create(
            SessionTaskDraft(thread.thread_id, thread.thread_id, "task", dedupe_key=f"old-{index}")
        )
        store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="accepted"))
        store.session_tasks.advance(
            task.task_id,
            SessionTaskUpdate(
                status="done",
                result_refs=("placeholder",),
                result_artifacts=(
                    {"path": "placeholder", "artifact_id": f"artifact-{index}", "sha256": "a" * 64},
                ),
            ),
        )
    projection = store.session_tasks._list_index()
    # 原列表complete，但旧版本还没有artifact反向定位；只删派生内容模拟旧库。
    store.session_tasks.list_page(limit=100)
    with projection._connection() as conn:
        conn.execute("DELETE FROM artifact_refs")
        conn.execute("DELETE FROM meta WHERE key LIKE 'artifact_backfill_%'")
    before = []
    original_read = projection.reader
    projection.reader = lambda path: (before.append(Path(path)), original_read(path))[1]
    first = projection.artifact_tasks("absent")
    assert first["coverage"] == "partial" and first["items"] == []
    assert len(before) <= 34  # 32旧行 + 原列表两条轮转身份探测。
    with pytest.raises(ObservationSourceError) as caught:
        # 第一次未追平不能假404；使用新迁移游标以保留未完成状态。
        with projection._connection() as conn:
            conn.execute("DELETE FROM meta WHERE key LIKE 'artifact_backfill_%'")
        task_artifact(store, tmp_path, "owner", "absent")
    assert caught.value.code == "HOST_NOT_READY"
    assert caught.value.details["reason"] == "ARTIFACT_TASK_INDEX_INCOMPLETE"
    last = projection.artifact_tasks("artifact-34")
    assert last["coverage"] == "complete" and len(last["items"]) == 1


@pytest.mark.parametrize("fault", ["link", "registry"])
def test_bad_original_task_sources_return_specific_recovery_reason(
    background_http, sampled_owner, fault
):  # noqa: F811
    services, task, runtime, _ = sampled_owner
    path, record = _publish(services, task)
    original.wire(services, runtime)
    client = client_for(background_http)
    assert client.call("artifacts.get", {"artifact_id": record.artifact_id})["ok"]
    wrong_version = client.call(
        "artifacts.get", {"artifact_id": record.artifact_id, "version_ref": "b" * 64}
    )
    assert not wrong_version["ok"] and wrong_version["error"]["code"] == "OBSERVATION_NOT_FOUND"
    if fault == "link":
        services.host.conversation_store.storage.task_path(task.task_id).write_text("{broken")
        reason = "ARTIFACT_TASK_LINK_UNAVAILABLE"
    else:
        path.write_text("new version")
        register_artifact(
            ArtifactRegistration(
                path.parents[1],
                path,
                artifact_id=record.artifact_id,
                task_id=task.task_id,
                source="write_file",
                status="ready",
            )
        )
        reason = "ARTIFACT_REGISTRY_PIN_DRIFTED"
    result = client.call("artifacts.get", {"artifact_id": record.artifact_id})
    assert not result["ok"]
    assert reason in json.dumps(result["error"])
