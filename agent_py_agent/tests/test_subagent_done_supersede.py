"""已结束子代理被显式接替：终态不改写，只追加 superseded_by（2026-09-28）。

背景：脚本模型端到端验证发现，接替一个 DONE 子代理时 create_subagents 回执报 recorded、也写了 TAKEOVER.md，
但持久化边界把 TAKEN_OVER 静默还原成 DONE，takeover_by 为空，回执与权威状态不符。

锁定：
- 已关闭来源（DONE/ABANDONED/CANCELLED）被接替时状态保持不变，只追加 superseded_by 与一条 TakeoverRecord；
  未关闭来源（BLOCKED 等）照旧转 TAKEN_OVER；回执带 disposition（superseded/taken_over）。
- 只要接替关系没有真正落盘，就不能回报 recorded：落账入口抛 TakeoverNotPersistedError 且不写 TAKEOVER.md，
  回执层重新读取权威状态，读不到接替关系就报 not_persisted。
- 防重复接替统一读 takeover_by/superseded_by 这组权威字段，第二次接替同一来源被预检拒绝。
- 持久化边界：已关闭记录整份拷回时只允许单调追加接替关系；同状态的旧快照写回不能冲掉已落盘的接替关系。
- 父级视图（kernel 节点、list_agents 模型视图）对被接替的子代理标出 replaced_by。
- 接管 run 的幂等查找同样读这组字段：已结束来源被接管后，重复发起直接找回原接管 run。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.agent_tree.model_view import agent_tree_model_payload
from agent_py_agent.agent.agent_core.agent_tree.node_rendering import node_from_kernel_run
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.subagents.kernel import SubagentKernel, SubagentKernelQuery
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import TakeoverRecord
from agent_py_agent.agent.subagents.services.takeover import run as takeover_run
from agent_py_agent.agent.subagents.services.takeover.record import (
    TakeoverEdgeRequest,
    TakeoverNotPersistedError,
    record_takeover_edge,
)
from agent_py_agent.agent.subagents.services.takeover.run import TakeoverRunRequest


def _source(tmp_path: Path, status: str) -> tuple[SubAgentManager, str]:
    manager = SubAgentManager(tmp_path, workspace_root=tmp_path)
    source = manager.create_run(goal="旧子代理整理三条要点", thought="执行", plan=["整理"], role="worker")
    manager.lifecycle.set_status(source.id, status)
    return manager, source.id


def _replace(manager: SubAgentManager, source_id: str, goal: str = "接替旧子代理整理三条要点"):
    agent = SimpleNamespace(
        config=SimpleNamespace(enable_subagents=True, max_subagents=10, access_mode="workspace-write"),
        subagents=manager,
        tools=SimpleNamespace(specs=lambda: []),
    )
    result = CreateSubagentsTool(agent).execute(
        {"goal": goal, "role": "worker", "replacement_for_run_ids": [source_id], "defer_start": True}
    )
    return result, json.loads(result.output)


@pytest.mark.parametrize("status", ["DONE", "CANCELLED", "ABANDONED"])
def test_closed_source_keeps_its_status_and_records_the_supersede(tmp_path, status):
    manager, source_id = _source(tmp_path, status)

    result, payload = _replace(manager, source_id)

    replacement_id = payload["created_run_ids"][0]
    source = manager.load(source_id)
    assert result.ok is True
    assert source.status == status
    assert source.superseded_by == replacement_id and source.takeover_by == ""
    assert [record.take_over_by for record in source.takeover_records] == [replacement_id]
    assert Path(source.takeover_file).is_file()
    assert payload["replacement_records"] == [{
        "source_run_id": source_id, "replacement_run_id": replacement_id, "status": "recorded",
        "disposition": "superseded",
    }]


def test_blocked_source_still_turns_taken_over(tmp_path):
    manager, source_id = _source(tmp_path, "BLOCKED")

    result, payload = _replace(manager, source_id)

    replacement_id = payload["created_run_ids"][0]
    source = manager.load(source_id)
    assert result.ok is True
    assert source.status == "TAKEN_OVER"
    assert source.takeover_by == replacement_id and source.superseded_by == ""
    assert payload["replacement_records"] == [{
        "source_run_id": source_id, "replacement_run_id": replacement_id, "status": "recorded",
        "disposition": "taken_over",
    }]


def test_takeover_write_that_does_not_persist_raises_and_skips_the_takeover_file(tmp_path, monkeypatch):
    manager, source_id = _source(tmp_path, "BLOCKED")
    monkeypatch.setattr(manager, "save", lambda task, **_kwargs: None)

    with pytest.raises(TakeoverNotPersistedError) as caught:
        manager.record_takeover(source_id, take_over_by="subagent-new", reason="接替", locked_files=[])

    assert (caught.value.run_id, caught.value.successor, caught.value.disposition) == (
        source_id, "subagent-new", "taken_over",
    )
    source = manager.load(source_id)
    assert source.status == "BLOCKED" and source.takeover_by == ""
    assert not Path(source.takeover_file).exists()


def test_receipt_is_not_recorded_when_the_source_does_not_show_the_edge(tmp_path, monkeypatch):
    manager, source_id = _source(tmp_path, "DONE")
    # 写入入口没有报错却什么也没写：回执只能按重新读取的权威状态说话。
    monkeypatch.setattr(manager, "record_takeover", lambda *_args, **_kwargs: None)

    result, payload = _replace(manager, source_id)

    replacement = next(task for task in manager.list_runs() if task.id != source_id)
    assert result.ok is False
    assert payload["error_code"] == "SUBAGENT_REPLACEMENT_RECORD_FAILED"
    assert [record["status"] for record in payload["replacement_records"]] == ["not_persisted"]
    assert replacement.status == "CANCELLED"
    assert manager.load(source_id).superseded_by == ""


@pytest.mark.parametrize("status,disposition", [("DONE", "superseded"), ("BLOCKED", "taken_over")])
def test_second_replacement_of_the_same_source_is_rejected(tmp_path, status, disposition):
    manager, source_id = _source(tmp_path, status)
    first, first_payload = _replace(manager, source_id)
    first_id = first_payload["created_run_ids"][0]

    second, payload = _replace(manager, source_id, goal="再接替一次旧子代理")

    assert first.ok is True and second.ok is False
    assert payload["error_code"] == "SUBAGENT_REPLACEMENT_INVALID"
    assert payload["issues"] == [{
        "index": 0, "source_run_id": source_id, "reason": "source_already_taken_over",
        "takeover_by": first_id, "disposition": disposition,
    }]
    assert sorted(task.id for task in manager.list_runs()) == sorted([source_id, first_id])


def test_closed_record_keeps_an_appended_supersede_when_a_status_write_is_refused(tmp_path):
    manager, source_id = _source(tmp_path, "DONE")
    stale = manager.load(source_id)
    stale.status = "RUNNING"
    stale.superseded_by = "subagent-new"
    stale.takeover_records.append(TakeoverRecord(id="takeover-1", run_id=source_id, take_over_by="subagent-new",
                                                 reason="接替"))

    manager.save(stale)

    source = manager.load(source_id)
    assert source.status == "DONE"
    assert source.superseded_by == "subagent-new"
    assert [record.id for record in source.takeover_records] == ["takeover-1"]


def test_takeover_that_loses_the_race_to_done_is_not_reported_and_leaves_no_record(tmp_path):
    manager, source_id = _source(tmp_path, "BLOCKED")
    snapshot = manager.load(source_id)
    # 接管读到的还是 BLOCKED，写入前子代理已收口为 DONE：按 TAKEN_OVER 写入被已关闭边界拒绝。
    manager.lifecycle.set_status(source_id, "DONE")

    with pytest.raises(TakeoverNotPersistedError):
        record_takeover_edge(manager, snapshot, TakeoverEdgeRequest(take_over_by="subagent-new", reason="接替"))

    source = manager.load(source_id)
    assert (source.status, source.takeover_by, source.superseded_by) == ("DONE", "", "")
    assert source.takeover_records == []
    assert not Path(source.takeover_file).exists()


def test_same_status_stale_snapshot_does_not_erase_the_supersede(tmp_path):
    manager, source_id = _source(tmp_path, "DONE")
    stale = copy.deepcopy(manager.load(source_id))
    record = manager.record_takeover(source_id, take_over_by="subagent-new", reason="接替", locked_files=[])

    manager.save(stale)

    source = manager.load(source_id)
    assert source.status == "DONE"
    assert source.superseded_by == "subagent-new"
    assert [item.id for item in source.takeover_records] == [record.id]


def test_parent_views_mark_the_superseded_child_and_its_successor(tmp_path):
    manager, source_id = _source(tmp_path, "DONE")
    _result, payload = _replace(manager, source_id)
    replacement_id = payload["created_run_ids"][0]

    snapshot = SubagentKernel(manager).snapshot(SubagentKernelQuery(include_refs=False))
    rows = {row.run_id: row for row in snapshot.runs}
    node = node_from_kernel_run(SimpleNamespace(subagents=manager), rows[source_id])
    model = agent_tree_model_payload({"nodes": [node], "child_result_index": []})

    expected = {"run_id": replacement_id, "disposition": "superseded"}
    assert rows[source_id].replaced_by == expected and rows[replacement_id].replaced_by == {}
    assert node["replaced_by"] == expected
    assert model["nodes"][0]["status"] == "DONE" and model["nodes"][0]["replaced_by"] == expected


def test_takeover_run_on_a_done_source_supersedes_it_and_stays_idempotent(tmp_path, monkeypatch):
    manager, source_id = _source(tmp_path, "DONE")
    first = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source_id, reason="first"))
    # 关掉按来源引用的全量扫描兜底，证明重复发起是靠 superseded_by 找回原接管 run。
    monkeypatch.setattr(takeover_run, "_existing_takeover_by_source_ref", lambda *_args: (None, {}))

    second = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source_id, reason="retry"))

    source = manager.load(source_id)
    assert source.status == "DONE" and source.superseded_by == first.takeover_run_id
    assert second.created is False and second.takeover_run_id == first.takeover_run_id
