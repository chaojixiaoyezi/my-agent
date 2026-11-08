"""R7-W2：被动状态/通知读口在索引装配时只按精确 id 回读，索引失败报未知而不扫描全部历史 run。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop.completion import _delegated_work_facts
from agent_py_agent.agent.conversation.agent_activity import conversation_agent_activity
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.runtime_db.recovery_guard import RuntimeDatabaseDeferred
from agent_py_agent.tests.test_conversation_agent_activity import _run
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


class _IndexedManager:
    """生产形状：有索引与精确读口；全量读口只记账，证明是否发生历史扫描。"""

    def __init__(self, runs, *, tree_error=None):
        self.runs, self.scans, self.selected = list(runs), [], []
        records = [SimpleNamespace(run_id=run.id, root_task_id=run.root_id, parent_run_id=run.parent_id,
                                   depth=run.depth) for run in self.runs]

        def tree(_root_id):
            if tree_error is not None:
                raise tree_error
            return SimpleNamespace(runs=records)

        self.local_store = SimpleNamespace(list_agent_tree=tree)

    def list_runs_by_ids_report(self, run_ids):
        wanted = list(run_ids)
        self.selected.append(wanted)
        return SimpleNamespace(runs=[run for run in self.runs if run.id in wanted], load_errors=[])

    def list_runs_report(self):
        self.scans.append("list_runs_report")
        return SimpleNamespace(runs=list(self.runs), load_errors=[])

    def list_runs(self):
        self.scans.append("list_runs")
        return list(self.runs)


def _active_store():
    return SimpleNamespace(tasks=SimpleNamespace(active_report=lambda _thread_id: (
        [SimpleNamespace(task_id="task-live", status="active")], [])))


def test_activity_index_failure_reports_unknown_without_history_scan():
    manager = _IndexedManager([_run("child-1")], tree_error=RuntimeDatabaseDeferred("generation_busy"))
    activity = conversation_agent_activity(SimpleNamespace(subagents=manager), _active_store(), "thread-1")
    assert manager.scans == [], "索引读失败不得退回全部历史 run 扫描"
    assert activity.subagents == ()
    assert activity.subagent_projection_ok is False
    assert "subagent_runs_unavailable" in activity.warnings


def test_activity_index_success_reads_exact_children_only():
    manager = _IndexedManager([_run("child-1"), _run("child-2", parent_id="child-1", depth=2)])
    activity = conversation_agent_activity(SimpleNamespace(subagents=manager), _active_store(), "thread-1")
    assert manager.scans == [] and manager.selected == [["child-1"]]
    assert [row["run_id"] for row in activity.subagents] == ["child-1"]


def test_legacy_manager_without_index_keeps_compatibility_scan():
    legacy = SimpleNamespace(list_runs_report=lambda: SimpleNamespace(runs=[_run("child-1")], load_errors=[]))
    activity = conversation_agent_activity(SimpleNamespace(subagents=legacy), _active_store(), "thread-1")
    assert [row["run_id"] for row in activity.subagents] == ["child-1"]


def test_gateway_status_counts_read_exact_request_runs():
    runs = [SimpleNamespace(id="r1", status="RUNNING", failure_type=""),
            SimpleNamespace(id="r2", status="DONE", failure_type=""),
            SimpleNamespace(id="other", status="RUNNING", failure_type="")]
    manager = _IndexedManager([])
    manager.runs = runs
    agent = SimpleNamespace(subagent_run_ids_for_request=lambda _request_id: ["r1", "r2"], subagents=manager)
    counts = control_service._subagent_status(agent, ["gwreq-1"])
    assert manager.scans == [] and manager.selected == [["r1", "r2"]]
    assert (counts.running, counts.done) == (1, 1)


def test_delegated_work_facts_read_exact_request_runs():
    params = _tool_loop_params(root_user_prompt="并行完成后汇总",
                               executed_tools=["create_subagents", "dispatch_subagents"])
    manager = _IndexedManager([])
    manager.runs = [SimpleNamespace(id="run-1", status="DONE"), SimpleNamespace(id="run-2", status="RUNNING"),
                    SimpleNamespace(id="unrelated", status="RUNNING")]
    agent = SimpleNamespace(subagent_run_ids_for_request=lambda task_id: ["run-1", "run-2"] if task_id == "task-1" else [],
                            subagents=manager)
    facts = _delegated_work_facts(agent, params)
    assert manager.scans == [] and manager.selected == [["run-1", "run-2"]]
    assert facts["status_counts"] == {"DONE": 1, "RUNNING": 1}


@pytest.mark.parametrize("mode", ["none", "error"])
def test_unavailable_exact_reader_never_enables_a_history_fallback(mode):
    manager = _IndexedManager([_run("child-1")])

    def unavailable(run_ids):
        if mode == "error":
            raise RuntimeDatabaseDeferred("generation_busy")
        return None

    manager.list_runs_by_ids_report = unavailable
    agent = SimpleNamespace(subagents=manager, subagent_run_ids_for_request=lambda _key: ["child-1"])
    activity = conversation_agent_activity(agent, _active_store(), "thread-1")
    assert activity.subagent_projection_ok is False
    assert "subagent_runs_unavailable" in activity.warnings
    facts = _delegated_work_facts(agent, _tool_loop_params(root_user_prompt="汇总当前工作",
        executed_tools=["create_subagents", "dispatch_subagents"]))
    assert facts == {} and manager.scans == []
