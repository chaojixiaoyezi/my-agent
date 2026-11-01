"""真实SQLite身份链；不以模型attrs或legacy parent_id授予空间。"""
import inspect
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.business_contracts import (
    BusinessExecutionContext,
    HostParentExecutionRef,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def context(task_id="stask-exact", owner="owner"):
    return BusinessExecutionContext(owner, "space-a", "project", task_id, 3, None, ("method@1",), (), "business-context.v1")


def repository(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    assert "task_run_metadata" in inspect.signature(repo.record_run_creation).parameters
    assert "host_parent_execution" in inspect.signature(repo.record_run_creation).parameters
    return repo


def test_root_context_commits_with_exact_task_run_and_distinct_task_id(tmp_path):
    repo = repository(tmp_path)
    ctx = context()
    record = repo.record_run_creation(owner_id="owner", run_id="root", conversation_task_id="conversation-task",
                                      task_run_metadata={"business_context": ctx.to_dict(), "session_task_ref": ctx.task_id})
    assert record["task_id"] != ctx.task_id
    assert json.loads(repo.get_task_run(record["task_run_id"])["metadata_json"]) == {
        "business_context": ctx.to_dict(), "session_task_ref": "stask-exact"}
    assert repo.business_context_for_execution(owner_id="owner", run_id="root", attempt_id=record["attempt_id"]) == ctx


def test_pending_parent_and_grandchild_share_one_task_run(tmp_path):
    repo = repository(tmp_path)
    root = repo.record_run_creation(owner_id="owner", run_id="root", attempt_status="pending",
                                   task_run_metadata={"business_context": context().to_dict(), "session_task_ref": "stask-exact"})
    parent = HostParentExecutionRef("owner", "root", root["agent_run_id"], root["task_run_id"])
    child = repo.record_run_creation(owner_id="owner", run_id="child", host_parent_execution=parent)
    grand = repo.record_run_creation(owner_id="owner", run_id="grand", host_parent_execution=HostParentExecutionRef(
        "owner", "child", child["agent_run_id"], child["task_run_id"], child["attempt_id"]))
    assert child["task_run_id"] == grand["task_run_id"] == root["task_run_id"]
    assert repo.get_agent_run(grand["agent_run_id"])["parent_agent_run_id"] == child["agent_run_id"]


@pytest.mark.parametrize("changed", [dict(owner_id="foreign"), dict(run_id="wrong"), dict(agent_run_id="wrong"),
                                    dict(task_run_id="wrong"), dict(observed_attempt_id="wrong")])
def test_forged_parent_rejected_before_any_child_record(tmp_path, changed):
    repo = repository(tmp_path)
    root = repo.record_run_creation(owner_id="owner", run_id="root")
    parent = HostParentExecutionRef("owner", "root", root["agent_run_id"], root["task_run_id"])
    with pytest.raises(ValueError):
        repo.record_run_creation(owner_id="owner", run_id="forged", host_parent_execution=replace(parent, **changed))
    assert repo.agent_run_for_run_id("forged") is None


def test_wrong_owner_context_and_session_locator_rejected_atomically(tmp_path):
    repo = repository(tmp_path)
    for metadata in ({"business_context": context(owner="foreign").to_dict(), "session_task_ref": "stask-exact"},
                     {"business_context": context().to_dict(), "session_task_ref": "different"}):
        with pytest.raises(ValueError):
            repo.record_run_creation(owner_id="owner", run_id="bad", task_run_metadata=metadata)
        assert repo.agent_run_for_run_id("bad") is None
