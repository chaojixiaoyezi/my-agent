"""使用查询按原来源页推进，过滤空页不丢尾部，坏源不伪装空成功。"""

import base64
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.business_capabilities import BusinessCapabilityService
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.tests.test_business_v12_f6_capabilities import _host, registry

_FIXTURES = (registry,)


def _setup(tmp_path, registry):
    host, context, _ = _host(tmp_path, registry)
    calls = []
    host.task_business_context = lambda task: (
        calls.append(task) or (context if task == context.task_id else None)
    )
    repo = RuntimeRepository(runtime_db_path(host.home_paths.owner_home_dir))
    return host, context, calls, repo


def _operations(repo, context, count, *, owner=None, task=None):
    current = replace(context, task_id=task or context.task_id)
    run = repo.record_run_creation(
        owner_id=owner or context.owner_id,
        run_id="run-" + (task or context.task_id),
        conversation_task_id=current.task_id,
        thread_id="thread",
        task_run_metadata={
            "business_context": current.to_dict(),
            "session_task_ref": current.task_id,
        },
    )
    results = []
    for index in range(count):
        row = repo.create_tool_operation(
            agent_run_id=run["agent_run_id"],
            attempt_id=run["attempt_id"],
            operation_type="read_file",
            canonical_scope="workspace",
        )
        if index % 2:
            repo.mark_operation_executing(row["operation_id"])
        results.append(row["operation_id"])
    return results


def test_runtime_usage_bounded_pages_advance_empty_filters_without_missing_or_duplicate(
    tmp_path, registry, monkeypatch
):
    host, context, calls, repo = _setup(tmp_path, registry)
    _operations(repo, context, 3, task="unrelated")
    expected = _operations(repo, context, 7)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("分页不得再全量读任务/轮次/attempt/operation")

    for name in (
        "task_runs_for_task",
        "agent_runs_for_task_run",
        "attempts_for_run",
        "operations_for_attempt",
    ):
        monkeypatch.setattr(RuntimeRepository, name, forbidden)
    service, request, seen = (
        BusinessCapabilityService(host),
        {"space_id": context.space_id, "limit": 2},
        [],
    )
    first = service.execute("spaces.usage.list", request, context=context)
    assert not first["items"] and first["next_cursor"]
    cursor = first["next_cursor"]
    pages = 1
    while cursor:
        before = len(calls)
        page = service.execute("spaces.usage.list", {**request, "cursor": cursor}, context=context)
        assert len(page["items"]) <= 2
        assert len(calls) - before <= 1  # 同一精确任务每页最多解析一次。
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        pages += 1
        assert pages <= 6
    assert [row["event_id"].split(":", 1)[1] for row in seen] == expected
    assert len({row["event_id"] for row in seen}) == 7
    assert [row["phase"] for row in seen] == [
        "selected",
        "executed",
        "selected",
        "executed",
        "selected",
        "executed",
        "selected",
    ]
    assert all(row["task_id"] == context.task_id for row in seen)


def test_usage_cursor_expires_on_legacy_scope_but_status_updates_keep_window(tmp_path, registry):
    host, context, _calls, repo = _setup(tmp_path, registry)
    _operations(repo, context, 3)
    service = BusinessCapabilityService(host)
    request = {"space_id": context.space_id, "limit": 1}
    page = service.execute("spaces.usage.list", request, context=context)
    legacy = base64.urlsafe_b64encode(
        json.dumps({"version": 1, "identity": "old", "offset": 1}).encode()
    ).decode()
    for cursor in (legacy, page["next_cursor"]):
        with pytest.raises(BusinessError) as caught:
            service.execute(
                "spaces.usage.list",
                {**request, "cursor": cursor, "task_id": context.task_id},
                context=context,
            )
        assert caught.value.code == "CURSOR_EXPIRED"
        assert caught.value.details["next_action"]
    repo.mark_operation_executing(
        repo.operations_for_attempt(repo.agent_run_for_run_id("run-stask")["current_attempt_id"])[
            0
        ]["operation_id"]
    )
    continued = service.execute(
        "spaces.usage.list", {**request, "cursor": page["next_cursor"]}, context=context
    )
    assert continued["items"]  # 当前行状态可以推进；游标固定截止而非整个WAL。



def test_runtime_usage_bad_database_returns_specific_error(tmp_path, registry):
    host, context, _calls, _repo = _setup(tmp_path, registry)
    path = runtime_db_path(host.home_paths.owner_home_dir)
    # 故障只作用于隔离源，缺项不能被except pass投影为空成功。
    path.write_bytes(b"not sqlite")
    with pytest.raises(BusinessError) as caught:
        BusinessCapabilityService(host).execute(
            "spaces.usage.list", {"space_id": context.space_id}, context=context
        )
    assert caught.value.code == "STORE_UNAVAILABLE"
    assert caught.value.details["reason"] == "RUNTIME_USAGE_SOURCE_UNREADABLE"


def test_skill_pages_continue_into_runtime_without_reordering_or_loss(tmp_path, registry):
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore

    host, context, calls, repo = _setup(tmp_path, registry)
    store = SkillLearningStore.for_home(host.home_paths)
    for _ in range(3):
        store.record_skill_usage(
            event="manual_get",
            skill_id="owner:method",
            source="skill_search",
            task_id=context.task_id,
        )
    expected = _operations(repo, context, 3)
    service = BusinessCapabilityService(host)
    cursor, items = None, []
    for _ in range(5):
        request = {"space_id": context.space_id, "limit": 2}
        if cursor:
            request["cursor"] = cursor
        before = len(calls)
        page = service.execute("spaces.usage.list", request, context=context)
        assert len(calls) - before <= 1
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert [row["event_id"] for row in items[:3]] == [
        "skill-usage:1",
        "skill-usage:2",
        "skill-usage:3",
    ]
    assert [row["event_id"].split(":", 1)[1] for row in items[3:]] == expected


def test_skill_source_incomplete_coverage_is_not_an_empty_success(tmp_path, registry):
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore

    host, context, _calls, _repo = _setup(tmp_path, registry)
    store = SkillLearningStore.for_home(host.home_paths)
    store.directory.mkdir(parents=True, exist_ok=True)
    (store.directory / "usage_ledger.jsonl").write_text("{broken}\n")
    with pytest.raises(BusinessError) as caught:
        BusinessCapabilityService(host).execute(
            "spaces.usage.list", {"space_id": context.space_id}, context=context
        )
    assert caught.value.code == "STORE_UNAVAILABLE"
    assert caught.value.details["reason"] == "SKILL_USAGE_SOURCE_UNREADABLE"
    assert caught.value.details["source_coverage"]["status"] != "complete"
    assert caught.value.details["source_coverage"]["reason"]
    from agent_py_agent.agent.gateway_parts.business_api import _error_details

    public = {item["key"]: item["value"] for item in _error_details(caught.value)}
    assert public["source_coverage.reason"] == "invalid_json"
    assert public["source_coverage.status"] != "complete"
    assert public["source_coverage.through"] == 0
    assert public["source_coverage.source_size"] == len(b"{broken}\n")
    assert all(key.removeprefix("source_coverage.") in ("reason", "status", "through", "source_size")
               for key in public if key.startswith("source_coverage."))
