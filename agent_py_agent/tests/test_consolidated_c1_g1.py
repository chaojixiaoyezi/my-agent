"""Wave 3 consolidated tests for test_consolidated_c1_g1.py.

- test_cpack_w4_track2_build_tool.py: W4 Track 2：沿真实构建工具收文件，执行位只认声明。
- test_r7_a_w6.py: 领域读口先按既有索引选范围，再读取真实 canonical；历史规模不进入热路径。
- test_r7_a_w8.py: 辅助调用的真实宿主收口：保留累计事实，只释放已完成的逻辑身份。
- test_r8_artifact_lookup_work.py: 以真实索引和正式prepare口记录查找工作量，不把小正文等同廉价定位。
- test_r8_current_obligations.py: 真实SQLite VM预算/EXPLAIN证明终态历史不会扩大当前义务查询。
- test_r8_fts_catchup.py: 有限追赶共享预算、原owner公平调度及片间真实writer进展。
- test_r8_gateway_context_bounds.py: 长期线程的Gateway上下文读取量只随窗口、活动集和准确引用增长。
- test_v15_w9_quote_expansion.py: 引用展开只读 canonical 正文；索引仅定位，不提供 owner/chat/thread 权限。
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.protocol import provider_message_reference
from agent_py_agent.agent.agent_core.model import llm_metrics
from agent_py_agent.agent.agent_core.model.call_runtime import record_model_call_finished
from agent_py_agent.agent.backends import ModelResponse, gateway_helpers
from agent_py_agent.agent.capability.learnpack_store import LearnpackStore
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger
from agent_py_agent.agent.conversation import ConversationStore, store_tasks, task_promotion
from agent_py_agent.agent.conversation import auxiliary_model_call as service
from agent_py_agent.agent.conversation.compact import ConversationScope
from agent_py_agent.agent.conversation.execution_threads import EXECUTION_ORIGIN_KEY
from agent_py_agent.agent.conversation.models import (
    ChannelBinding,
    ConversationThread,
    ThreadTaskLink,
)
from agent_py_agent.agent.conversation.outbound_message_index import OutboundMessageIndex
from agent_py_agent.agent.conversation.quoted_context import quoted_context_facts
from agent_py_agent.agent.gateway_parts import request_context, request_history
from agent_py_agent.agent.gateway_parts.request_execution import _gateway_message_references
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage import fts_upgrade as upgrade
from agent_py_agent.agent.memory_archive.artifact import reader
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.runtime_db.current_obligations import (
    closed_attempt_bindings,
    closed_task_ids,
    recorded_run_ids,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.cancellation import (
    CancelSubagentTaskRequest,
    prepare_subagent_stops,
)
from agent_py_agent.agent.subagents.kernel import SubagentKernelQuery
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import SubAgentBoardOptions, SubAgentTask
from agent_py_agent.agent.subagents.run_budget import (
    SubagentRunBudgetRequest,
    build_subagent_run_budget_report,
)
from agent_py_agent.agent.subagents.services.persistence.run_index import (
    SubagentRunIndexUnavailable,
)
from agent_py_agent.agent.subagents.services.recovery.orchestrator import (
    RecoveryOrchestrationRequest,
)
from agent_py_agent.agent.tooling.package_build_tool import PackageBuildTool
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
from agent_py_agent.tests.test_cpack_w4_import_synthesis import put
from agent_py_agent.tests.test_cpack_w4_pack_grammar_rollback import make_pack
from agent_py_agent.tests.test_package_build_tool import _agent
from agent_py_agent.tests.test_r8_fts_automatic_upgrade import fts_slice_clock  # noqa: F401

# ============================================================================
# Source: agent_py_agent/tests/test_cpack_w4_track2_build_tool.py
# ============================================================================

@pytest.mark.parametrize("executable", [False, True])
def test_auto_collection_marks_only_declared_executable_entry(tmp_path, monkeypatch, executable):
    agent = _agent(tmp_path, monkeypatch)
    _, declaration, contents = make_pack(executable=executable)
    declaration.pop("files")
    root = agent.home_paths.owner_home_dir / "fixture-source"
    for path, data in contents.items():
        put(root, path, data).chmod(0o755)  # 模式位不能额外授予执行权。
    put(root, "mcp/demo/data", "data").chmod(0o755)
    outcome = agent.tools.tools["package_build"].execute({"kind": "capability_pack", "source_dir": str(root),
        "declaration": declaration, "origin": "自己写的", "license": "MIT"})
    assert outcome.ok, outcome.output
    report = json.loads(outcome.output)
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    record = store.build(report["build"]["sha256"])
    manifest = inspect_plugin_package(store.build_path(record.sha256).read_bytes()).manifest
    assert manifest.has_mcp_runtime
    assert {f.path for f in manifest.files if f.executable} == ({"mcp/demo/server.py"} if executable else set())


def test_model_description_explains_single_mcp_runtime_and_preserves_language_tokens():
    text = PackageBuildTool.model_spec.description
    for token in ("mcp_servers", "root", "pinned_on_enable", "permissions.network", "npx", "uvx",
                  "summary", "capability.description", "capability.keywords", "和用户对话时用的语言",
                  "另一种语言", "中英文", "目录展示", "关键词推荐"):
        assert token in text



# ============================================================================
# Source: agent_py_agent/tests/test_r7_a_w6.py
# ============================================================================

def _r7_a_w6_history(manager, count):
    # 使用 reader 支持的旧 canonical 和原 upsert 批量装配隔离历史，不伪造查询结果。
    with manager.persistence.run_index.connect() as db:
        for number in range(count):
            run_id = f"history-{number:05}"
            payload = {"id": run_id, "goal": "历史", "thought": "", "plan": [],
                       "status": "DONE", "created_at": 1.0, "updated_at": 2.0}
            path = manager.workspace / run_id / "task.json"
            path.parent.mkdir()
            path.write_text(json.dumps(payload), encoding="utf-8")
            manager.persistence.run_index.upsert(SubAgentTask(**payload), connection=db)


def _reads(manager, monkeypatch):
    reads = Counter()
    original_stat = Path.stat

    def observed_stat(path, *args, **kwargs):
        if path.name == "task.json" and path.parent.name.startswith("history-"):
            reads[path.parent.name] += 1
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", observed_stat)
    for name in ("load", "_cached_run_ref"):
        original = getattr(manager.persistence, name)

        def observed(run_id, *args, _original=original, **kwargs):
            reads[run_id] += 1
            return _original(run_id, *args, **kwargs)

        monkeypatch.setattr(manager.persistence, name, observed)
    return reads


@pytest.mark.parametrize("history_count", [100, 1000, 10000])
def test_scoped_domain_reads_do_not_hydrate_unrelated_history(tmp_path, monkeypatch, history_count, record_property):
    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        activate_run_workspace,
    )

    manager = SubAgentManager(tmp_path / "runs")
    root = manager.create_run(goal="当前根", thought="", plan=[], attributes={"conversation_thread_id": "thread-current"})
    activate_run_workspace(root.task_workspace_dir, EnsureRunWorkspaceRequest(
        home=tmp_path, template="", task_name="当前根", user_prompt="当前任务", task_id=root.id, run_id=root.id, request_id=root.id))
    child = manager.create_run(goal="失败孩子", thought="", plan=[], parent_id=root.id, root_id=root.id)
    child.status = "FAILED"
    manager.save(child)
    done = manager.create_run(goal="已结束孩子", thought="", plan=[], parent_id=root.id, root_id=root.id)
    done.status = "DONE"
    manager.save(done)
    _r7_a_w6_history(manager, history_count)
    reads = _reads(manager, monkeypatch)
    operations = (
        lambda: manager.board.build_board(options=SubAgentBoardOptions(root_id=root.id)),
        lambda: manager.board.due_check(root_id=root.id),
        lambda: manager.kernel_snapshot(SubagentKernelQuery(root_id=root.id)),
        lambda: manager.kernel_snapshot(SubagentKernelQuery(scope="conversation_thread", conversation_thread_id="thread-current")),
        lambda: manager.kernel_snapshot(SubagentKernelQuery(scope="task_workspace", task_workspace_dir=root.task_workspace_dir)),
        lambda: build_subagent_run_budget_report(SubagentRunBudgetRequest(manager, root_id=root.id)),
        lambda: manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(run_ids=[child.id])),
        lambda: prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(root, "stop", False)]),
    )
    for number, operation in enumerate(operations):
        reads.clear()
        result = operation()
        assert result is not None
        if hasattr(result, "load_errors"):
            assert result.load_errors == []
        assert not {run_id for run_id in reads if run_id.startswith("history-")}
        assert sum(reads.values()) <= 40
        record_property(f"history_{history_count}_operation_{number}_canonical_reads", sum(reads.values()))
    assert manager.load(done.id).status == "DONE"


def test_board_explicit_empty_candidates_and_forwarded_plan(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="当前", thought="", plan=[])
    assert manager.board.build_board(candidate_ids=[]).summary == {"total": 0}
    assert manager.board.build_board(candidate_ids=None).summary["total"] == 1
    assert manager.board.due_check(candidate_ids=[]).issues == []
    assert manager.board.plan_actions(candidate_ids=[]).actions == []
    board = manager.board.build_board(candidate_ids=[task.id])
    assert [item.id for item in board.items] == [task.id]


def test_board_selected_parent_does_not_mislabel_real_excluded_child(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="父级", thought="", plan=[])
    child = manager.create_run(goal="已结束孩子", thought="", plan=[], parent_id=parent.id, root_id=parent.id)
    child.status = "DONE"
    manager.save(child)
    board = manager.board.build_board(candidate_ids=[parent.id])
    assert board.summary["total"] == 1
    assert board.items[0].child_status_counts == {"DONE": 1}


def test_dispatch_selects_current_before_canonical_reads(tmp_path, monkeypatch):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="待派工", thought="", plan=[])
    _r7_a_w6_history(manager, 100)
    reads = _reads(manager, monkeypatch)
    assert [row.id for row in manager.indexing.select_runs(None)] == [task.id]
    assert set(reads) == {task.id}
    assert manager.indexing.select_runs([]) == []


def test_board_current_approval_scope_and_filters_precede_item_construction(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.models import CapabilityRequest
    from agent_py_agent.agent.subagents.services.board import service

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="已结束但授权未结", thought="", plan=[], owner="worker", supervisor="parent")
    task.status = "DONE"
    task.capability_requests = [CapabilityRequest("approval", task.id, "访问", "read_file")]
    manager.save(task)
    _r7_a_w6_history(manager, 100)
    ids = [row.id for row in manager.list_current_runs_report(include_open_capabilities=True).runs]
    board = manager.board.build_board(candidate_ids=ids, options=SubAgentBoardOptions(owner="parent"))
    assert board.summary["total"] == 1 and board.items[0].open_request_count == 1
    assert board.items[0].status == "DONE"
    original = service.to_board_item
    constructed = []

    def observed(manager, row, **kwargs):
        constructed.append(row.id)
        return original(manager, row, **kwargs)

    monkeypatch.setattr(service, "to_board_item", observed)
    assert manager.board.build_board(options=SubAgentBoardOptions(status="RUNNING")).summary == {"total": 0}
    assert constructed == []


def test_corrupt_selected_canonical_is_unknown_before_any_stop_signal(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents import cancellation
    from agent_py_agent.agent.subagents.services.agent_run_state import (
        canonical_state_path_from_payload,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="待查", thought="", plan=[])
    canonical = canonical_state_path_from_payload(asdict(task))
    canonical.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(cancellation, "signal_runner_attempt", lambda *args: pytest.fail("坏账不能广播停止"))
    with pytest.raises(SubagentRunIndexUnavailable):
        manager.board.build_board(candidate_ids=[task.id])
    with pytest.raises(SubagentRunIndexUnavailable):
        build_subagent_run_budget_report(SubagentRunBudgetRequest(manager, root_id=task.id))
    with pytest.raises(json.JSONDecodeError):
        prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(task, "stop", False)])
    snapshot = manager.kernel_snapshot(SubagentKernelQuery(root_id=task.id))
    assert snapshot.load_errors and snapshot.warnings and snapshot.runs == []
    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(run_ids=[task.id]))
    assert report.load_errors and report.steps == []


def test_partial_and_replaced_index_never_fall_back_to_owner_history(tmp_path, monkeypatch):
    import shutil

    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="当前", thought="", plan=[])
    _r7_a_w6_history(manager, 100)
    monkeypatch.setattr(manager.persistence, "list_runs", lambda: pytest.fail("索引未知不能全扫"))
    index = manager.persistence.run_index
    with index.connect() as db:
        db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    # 禁止异步回填改变单一故障；测试不替换查询结果。
    monkeypatch.setattr(manager.persistence, "_start_run_index_maintenance", lambda: None)
    with pytest.raises(SubagentRunIndexUnavailable):
        manager.board.build_board(options=SubAgentBoardOptions(root_id=root.id))
    with index.connect() as db:
        db.execute("UPDATE meta SET value=1 WHERE key='complete'")
    original = index.request_ids

    def replaced(*args):
        ids = original(*args)
        replacement = tmp_path / "replacement.sqlite3"
        shutil.copy2(index.path, replacement)
        os.replace(replacement, index.path)
        return ids

    monkeypatch.setattr(index, "request_ids", replaced)
    with pytest.raises(SubagentRunIndexUnavailable, match="identity changed"):
        manager.board.build_board(options=SubAgentBoardOptions(root_id=root.id))


def test_cancel_rechecks_identity_after_event_controlled_attempt_change(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
    from agent_py_agent.agent.subagents import cancellation

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="原授权", thought="", plan=[])
    release, committed = Event(), Event()

    def replace_attempt():
        assert release.wait(5)
        with manager.creation_guard():
            current = manager.load(task.id)
            current.runner_active_attempt_id = "new-attempt"
            manager.save(current)
        committed.set()

    monkeypatch.setattr(cancellation, "signal_runner_attempt", lambda *args: pytest.fail("旧身份不能发中断"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(replace_attempt)
        release.set()
        assert committed.wait(5)
        with pytest.raises(RuntimeConflictError):
            prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(task, "stop", False)])
        future.result(timeout=5)
    assert manager.load(task.id).runner_active_attempt_id == "new-attempt"


def test_cancel_keeps_new_descendant_and_terminal_exit_proof_across_cache_eviction(tmp_path):
    from agent_py_agent.agent.subagents.cancellation import cleanup_subagent_stops

    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(goal="原父任务", thought="", plan=[])
    ready, published = Event(), Event()

    def create_before_stop():
        assert ready.wait(5)
        child = manager.create_run(goal="已发表孩子", thought="", plan=[], parent_id=parent.id, root_id=parent.id)
        child.status = "DONE"
        child.attributes["task_control"] = {"cleanup": {"state": "confirmed", "attempt_id": "original-exit"}}
        manager.save(child)
        published.set()
        return child

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(create_before_stop)
        ready.set()
        assert published.wait(5)
        child = future.result(timeout=5)
        batch = prepare_subagent_stops(SimpleNamespace(subagents=manager), [CancelSubagentTaskRequest(parent, "stop", False)])
    assert {stop.report["run_id"] for stop in batch.stops} == {parent.id, child.id}
    _r7_a_w6_history(manager, manager.persistence._MAX_RUN_CACHE_ENTRIES + 1)
    manager.list_runs_by_ids_report([path.name for path in manager.workspace.iterdir() if path.name.startswith("history-")])
    assert child.id not in manager.persistence._run_cache
    result = cleanup_subagent_stops(batch)
    assert result["ok"] and manager.load(parent.id).status == "CANCELLED"
    current = manager.load(child.id)
    assert current.status == "DONE"
    assert current.attributes["task_control"]["cleanup"] == {"state": "confirmed", "attempt_id": "original-exit"}


def test_failed_parent_and_reused_takeover_remain_in_exact_recovery_scope(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.models import SubAgentLeadershipRecoveryPlanOptions
    from agent_py_agent.agent.subagents.services.takeover.run import TakeoverRunRequest

    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="根", thought="", plan=[])
    source = manager.create_run(goal="失败协调者", thought="", plan=[], role="coordinator", parent_id=root.id, root_id=root.id)
    child = manager.create_run(goal="仍运行", thought="", plan=[], parent_id=source.id, root_id=root.id)
    source = manager.load(source.id)
    source.status = "TIMEOUT"
    manager.save(source)
    replacement = manager.create_run(goal="接替", thought="", plan=[], parent_id=root.id, root_id=root.id,
                                     attributes={"takeover_source_run_id": source.id})
    _r7_a_w6_history(manager, 100)
    reads = _reads(manager, monkeypatch)
    plan = manager.hierarchy.plan_leadership_recovery(params=SubAgentLeadershipRecoveryPlanOptions(root_id=root.id))
    assert plan.summary["failed_parent_nodes"] == 1
    assert plan.unassigned[0].coordinator_id == source.id and plan.unassigned[0].child_ids == [child.id]
    result = manager.create_takeover_run(TakeoverRunRequest(source_run_id=source.id, reason="retry"))
    assert not result.created and result.takeover_run_id == replacement.id
    assert not any(run_id.startswith("history-") for run_id in reads)


def test_retry_exhaustion_is_not_lost_when_recovery_hydrates_only_root(tmp_path):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.tests.test_opt_recovery_counters import _slice

    manager = SubAgentManager(tmp_path)
    manager.recovery_capability_config = lambda: CapabilityConfig(subagent_no_progress_attempt_limit=2)
    task = manager.create_run(goal="耗尽重试", thought="", plan=[])
    for number in (1, 2):
        _slice(manager, task.id, number, failed=True)
    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(run_ids=[task.id]))
    assert report.steps[0].strategy_snapshot["no_progress_fuse"] is True
    assert not report.steps[0].requires_dispatch


def test_automatic_recovery_keeps_terminal_closeout_candidates_and_drops_completed_obligations(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.runtime_closeout import (
        RUNTIME_CLOSEOUT_ATTR,
        RUNTIME_CLOSEOUT_SCHEMA,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="失败但收口未结", thought="", plan=[])
    task.status = "FAILED"
    task.attributes[RUNTIME_CLOSEOUT_ATTR] = {"schema_version": RUNTIME_CLOSEOUT_SCHEMA}
    manager.save(task)
    _r7_a_w6_history(manager, 100)
    reads = _reads(manager, monkeypatch)
    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest())
    assert [step.run_id for step in report.steps] == [task.id]
    assert not any(run_id.startswith("history-") for run_id in reads)
    manager.mutate(task.id, lambda current: current.attributes.pop(RUNTIME_CLOSEOUT_ATTR, None))
    assert manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest()).steps == []


def test_limited_probe_selects_ids_before_reading_real_work_orders(tmp_path, monkeypatch):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="真实探测", thought="", plan=[])
    _r7_a_w6_history(manager, 100)
    reads = _reads(manager, monkeypatch)
    report = manager.channel_probe.probe_channels(limit=1)
    assert len(report.results) == 1 and report.results[0].run_id == task.id
    assert set(reads) == {task.id}
    assert (Path(task.logs_dir) / "last_channel_probe.json").is_file()


def test_index_scope_connections_release_descriptors_on_success_and_error(tmp_path, record_property):
    from agent_py_agent.agent.subagents.services.agent_run_state import (
        canonical_state_path_from_payload,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="fd", thought="", plan=[], attributes={"conversation_thread_id": "session"})
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = len(os.listdir(fd_dir))
    for _ in range(4):
        manager.board.build_board(options=SubAgentBoardOptions(root_id=task.id))
        manager.kernel_snapshot(SubagentKernelQuery(root_id=task.id))
    canonical_state_path_from_payload(asdict(task)).write_text("{broken", encoding="utf-8")
    for _ in range(4):
        with pytest.raises(SubagentRunIndexUnavailable):
            manager.board.build_board(options=SubAgentBoardOptions(root_id=task.id))
    delta = len(os.listdir(fd_dir)) - before
    record_property("fd_delta", delta)
    assert delta == 0


def test_missing_index_is_reported_by_kernel_instead_of_healthy_empty(tmp_path):
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="缺索引", thought="", plan=[])
    manager.persistence.run_index.path.unlink()
    snapshot = manager.kernel_snapshot(SubagentKernelQuery(root_id=task.id))
    assert snapshot.load_errors and snapshot.warnings and snapshot.runs == []



# ============================================================================
# Source: agent_py_agent/tests/test_r7_a_w8.py
# ============================================================================

class _OfflineBackend:
    name = "offline-auxiliary"
    model_name = "offline-model"
    context_window_tokens = 4096
    max_tokens = 128

    def __init__(self):
        self.calls = 0
        self.operation = None
        self.response = ModelResponse(text="摘要", backend=self.name, usage={"input_tokens": 7, "output_tokens": 3})

    def generate(self, prompt, *, on_chunk=None, request_options=None):
        self.calls += 1
        gateway_helpers._emit_provider_attempt({"attempt_id": f"http-{self.calls}", "status": "started"})
        if on_chunk is not None:
            on_chunk("摘要")
        return self.operation(on_chunk) if self.operation is not None else self.response


@pytest.fixture
def aux_request(monkeypatch):
    # 仅替换遥测出口；生成、准入、预算、取消、调用账本和观察者都走真实实现。
    metrics = {"calls": [], "cost": [], "run_cost": []}
    monkeypatch.setattr(service, "record_llm_call", lambda *a, **kw: metrics["calls"].append(kw["ok"]))
    monkeypatch.setattr(llm_metrics, "record_llm_cost", lambda model, response: metrics["cost"].append((model, response)))
    monkeypatch.setattr(llm_metrics, "record_run_cost", lambda *args: metrics["run_cost"].append(args))
    agent = SimpleNamespace(
        backend=_OfflineBackend(),
        config=AgentConfig(enable_tools=False, model_name="offline-model", max_tokens=128, my_agent_owner_id="offline-owner",
                           model_context_window_tokens=4096, memory_compact_summary_max_output_tokens=64),
        _model_call_ledger=ModelCallLedger(),
        metrics=metrics,
    )
    return service.AuxiliaryModelCallRequest(agent, "离线摘要", request_id="request", run_id="run",
                                             purpose="conversation_compact_summary")


def _assert_closed(aux_request, count):
    ledger = aux_request.agent._model_call_ledger
    for aggregate in ledger._scope_aggregates.values():
        for bucket in (aggregate, aggregate.purpose_totals["auxiliary"]):
            assert not bucket.logical_call_counts
            assert bucket.closed_logical_call_count == count
        assert not aggregate.retry_logical_call_ids
    for scope in ({"request_id": aux_request.request_id}, {"run_id": aux_request.run_id}):
        if not next(iter(scope.values())):
            continue
        summary = ledger.cumulative_summary(**scope)
        if not count:
            assert summary is None
            continue
        assert summary["logical_model_turn_count"] == summary["physical_model_attempt_count"] == count
        assert summary["purpose_breakdown"]["auxiliary"]["logical_model_turn_count"] == count


def test_ten_thousand_actual_compact_dispatches_release_only_completed_keys(aux_request, record_property):
    backend, ledger = aux_request.agent.backend, aux_request.agent._model_call_ledger
    peaks, checkpoints = [], {}

    def observe_active(_chunk):
        peaks.append(max(len(aggregate.logical_call_counts) for aggregate in ledger._scope_aggregates.values()))
        return backend.response

    backend.operation = observe_active
    for number in range(1, 10001):
        assert service.generate_auxiliary_model_response(aux_request) is backend.response
        if number in (100, 1000, 10000):
            aggregate = ledger._scope_aggregates[("run", "run")]
            checkpoints[number] = (aggregate.closed_logical_call_count, len(aggregate.logical_call_counts))
    assert backend.calls == 10000
    assert max(peaks) == 1
    assert checkpoints == {number: (number, 0) for number in (100, 1000, 10000)}
    _assert_closed(aux_request, 10000)
    assert len(ledger.records()) == 128
    assert all(row.status == "finished" for row in ledger.records())
    assert ledger.cumulative_summary(run_id="run")["accounted_input_tokens"] == 70000
    record_property("closed_and_open_at_history_sizes", str(checkpoints))


def test_backend_failure_preserves_original_exception_and_observation(aux_request):
    error, observations = RuntimeError("offline failure"), []

    def fail(_chunk):
        raise error

    aux_request.agent.backend.operation = fail
    with pytest.raises(RuntimeError) as caught:
        service.generate_auxiliary_model_response(replace(aux_request, on_observation=observations.append))
    assert caught.value is error and caught.value.__cause__ is None
    assert len(observations) == 1 and observations[0].provider_http_attempt_count == 1
    assert aux_request.agent.metrics == {"calls": [False], "cost": [], "run_cost": []}
    _assert_closed(aux_request, 1)
    assert aux_request.agent._model_call_ledger.records()[0].status == "failed"


@pytest.mark.parametrize("before_start", [False, True])
def test_cancellation_keeps_the_original_boundary(aux_request, before_start):
    token = CancellationToken()

    def cancel(_chunk):
        token.cancel("offline cancellation")
        return aux_request.agent.backend.response

    aux_request.agent.backend.operation = cancel
    if before_start:
        token.cancel("offline cancellation")
    with bind_cancellation_token(token), pytest.raises(ToolCancelled, match="offline cancellation"):
        service.generate_auxiliary_model_response(aux_request)
    assert aux_request.agent.backend.calls == int(not before_start)
    _assert_closed(aux_request, int(not before_start))
    assert aux_request.agent.metrics["cost"] == []


def test_observer_exception_keeps_response_and_costs(aux_request, caplog):
    observations = []

    def broken_observer(observation):
        observations.append(observation)
        raise ValueError("observer unavailable")

    response = service.generate_auxiliary_model_response(replace(aux_request, on_observation=broken_observer))
    assert response is aux_request.agent.backend.response
    assert len(observations) == 1 and observations[0].provider_http_attempt_count == 1
    assert aux_request.agent.metrics == {"calls": [True], "cost": [("offline-model", response)],
                                         "run_cost": [("offline-owner", "run", "offline-model", response)]}
    assert "辅助模型调用观察暂不可用" in caplog.text
    _assert_closed(aux_request, 1)


def test_observer_interrupt_still_releases_completed_logical_identity(aux_request):
    error = KeyboardInterrupt("observer interrupted")

    def interrupted_observer(_observation):
        raise error

    with pytest.raises(KeyboardInterrupt) as caught:
        service.generate_auxiliary_model_response(replace(aux_request, on_observation=interrupted_observer))
    assert caught.value is error and caught.value.__cause__ is None
    _assert_closed(aux_request, 1)
    assert aux_request.agent._model_call_ledger.records()[0].status == "finished"
    assert aux_request.agent.metrics == {"calls": [], "cost": [], "run_cost": []}


@pytest.mark.parametrize(("request_id", "run_id"), [("request", ""), ("", "run"), ("", "")])
def test_missing_scope_does_not_create_another_authority(aux_request, request_id, run_id):
    aux_request = replace(aux_request, request_id=request_id, run_id=run_id)
    assert service.generate_auxiliary_model_response(aux_request) is aux_request.agent.backend.response
    _assert_closed(aux_request, 1)
    assert len(aux_request.agent._model_call_ledger._scope_aggregates) == bool(request_id) + bool(run_id)


def test_late_physical_outcome_cannot_reopen_closed_auxiliary_group(aux_request):
    ready, release = Event(), Event()
    errors, workers = [], []
    ledger = aux_request.agent._model_call_ledger

    def launch_late_worker(on_chunk):
        call_id = ledger.records()[0].call_id
        observe_http = gateway_helpers._PROVIDER_ATTEMPT_OBSERVER.callback

        def late():
            try:
                ready.set()
                assert release.wait(5)
                on_chunk("迟到输出")
                with gateway_helpers.provider_attempt_observer(observe_http):
                    gateway_helpers._emit_provider_attempt({"attempt_id": "late-http", "status": "failed"})
                record_model_call_finished(ledger, call_id, ModelResponse(text="迟到", backend="offline",
                                                                          usage={"input_tokens": 999, "output_tokens": 999}))
            except BaseException as exc:
                errors.append(exc)

        worker = Thread(target=copy_context().run, args=(late,))
        workers.append(worker)
        worker.start()
        assert ready.wait(5)
        return aux_request.agent.backend.response

    aux_request.agent.backend.operation = launch_late_worker
    try:
        service.generate_auxiliary_model_response(aux_request)
        _assert_closed(aux_request, 1)
        release.set()
    finally:
        release.set()
        for worker in workers:
            worker.join(5)
    assert not errors and all(not worker.is_alive() for worker in workers)
    _assert_closed(aux_request, 1)
    summary = ledger.cumulative_summary(run_id="run")
    assert summary["status_counts"]["finished"] == 1
    assert sum(summary["status_counts"].values()) == 1
    assert summary["provider_http_attempt_count"] == 2
    assert summary["accounted_input_tokens"] == 7 and summary["output_tokens"] == 3



# ============================================================================
# Source: agent_py_agent/tests/test_r8_artifact_lookup_work.py
# ============================================================================

def test_legacy_scoped_ref_preparation_does_not_decode_owner_history_twice(tmp_path, monkeypatch):
    primary = tmp_path / "blobs/tool_outputs/index.jsonl"
    primary.parent.mkdir(parents=True)
    primary.write_text("".join(json.dumps({"scoped_call_id": f"old:{i}", "pad": "x" * 128}) + "\n"
                               for i in range(300)), encoding="utf-8")
    target = tmp_path / "runs/2001-01-01/old-task/work/blobs/tool_outputs/index.jsonl"
    target.parent.mkdir(parents=True)
    body = target.parent / "body.json"
    body.write_text("{}", encoding="utf-8")
    ref = "legacy-run:call-target"
    target.write_text(json.dumps({"scoped_call_id": ref, "run_id": "legacy-run",
        "call_id": "call-target", "path": str(body)}) + "\n", encoding="utf-8")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(1024))
    visits = []
    original = reader.iter_decoded_tool_index_objects

    def counted(source, *args, **kwargs):
        visits.append(source)
        yield from original(source, *args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", counted)
    prepared = reader.prepare_tool_output_artifact_read(
        reader.ReadToolOutputArtifactRequest(tmp_path, ref, max_chars=5))
    assert prepared.record is not None
    assert len(visits) == 2, "同次prepare只应读取主索引与目标索引各一次"


def test_prepare_negative_reuse_expires_at_request_boundary(tmp_path, monkeypatch):
    index = tmp_path / "blobs/tool_outputs/index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"pad": "x" * 4096}) + "\n", encoding="utf-8")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(128))
    calls = []
    original = reader.iter_decoded_tool_index_objects

    def counted(*args, **kwargs):
        calls.append(1)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", counted)
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "missing:call")
    assert reader.prepare_tool_output_artifact_read(request).record is None
    assert reader.prepare_tool_output_artifact_read(request).record is None
    assert len(calls) == 2
    index.write_text(json.dumps({"scoped_call_id": "missing:call", "run_id": "missing", "path": ""}) + "\n")
    assert reader.prepare_tool_output_artifact_read(request).record is not None


@pytest.mark.parametrize("change", ["append", "replace", "truncate", "same_size_mtime", "same_observed_stamp", "corrupt"])
def test_prepare_reopens_and_revalidates_changed_primary(tmp_path, monkeypatch, change):
    index = tmp_path / "blobs/tool_outputs/index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({"pad": "x" * 4096}) + "\n", encoding="utf-8")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(128))
    if change == "same_observed_stamp":
        signature = reader._blob_stat_signature
        monkeypatch.setattr(reader, "_blob_stat_signature", lambda info: (*signature(info)[:3], 0, 0))
    discover = reader.tool_output_index_paths_for_lookup
    seen = []
    original = reader.iter_decoded_tool_index_objects

    def counted(*args, **kwargs):
        seen.append(1)
        yield from original(*args, **kwargs)

    def changed(root):
        data = json.dumps({"scoped_call_id": "wanted:call", "run_id": "wanted", "path": ""}) + "\n"
        if change == "append":
            with index.open("a") as handle:
                handle.write(data)
        elif change == "replace":
            replacement = index.with_suffix(".new")
            replacement.write_text(data)
            replacement.replace(index)
        elif change == "truncate":
            index.write_text(data)
        elif change in {"same_size_mtime", "same_observed_stamp"}:
            before = index.stat()
            index.write_text(data.rstrip() + " " * (before.st_size - len(data)) + "\n")
            os.utime(index, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert index.stat().st_size == before.st_size
        else:
            index.write_text('{"bad":' + "1" * 5000 + '}\n')
        return discover(root)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", counted)
    monkeypatch.setattr(reader, "tool_output_index_paths_for_lookup", changed)
    request = reader.ReadToolOutputArtifactRequest(tmp_path, "wanted:call")
    if change == "corrupt":
        with pytest.raises(ValueError):
            reader.prepare_tool_output_artifact_read(request)
    else:
        assert reader.prepare_tool_output_artifact_read(request).record is not None
    assert len(seen) == 2
    assert reader._PREPARE_INDEX_READ.get() is None


def test_prepare_tokens_are_isolated_between_concurrent_threads(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    roots = [tmp_path / str(i) for i in range(2)]
    for root in roots:
        index = root / "blobs/tool_outputs/index.jsonl"
        index.parent.mkdir(parents=True)
        index.write_text(json.dumps({"pad": "x" * 4096}) + "\n")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(128))
    barrier = threading.Barrier(2)
    original = reader.tool_output_index_paths_for_lookup

    def discover(root):
        barrier.wait(timeout=5)
        token = reader._PREPARE_INDEX_READ.get()
        assert token and str(root) in token[0][1]
        return original(root)

    monkeypatch.setattr(reader, "tool_output_index_paths_for_lookup", discover)

    def prepare(root):
        result = reader.prepare_tool_output_artifact_read(
            reader.ReadToolOutputArtifactRequest(root, "absent:call"))
        assert reader._PREPARE_INDEX_READ.get() is None
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, roots))
    assert all(result.record is None for result in results)
    assert reader._PREPARE_INDEX_READ.get() is None


def test_prepare_negative_token_keeps_no_parsed_source_rows(tmp_path, monkeypatch):
    index = tmp_path / "blobs/tool_outputs/index.jsonl"
    index.parent.mkdir(parents=True)
    index.write_text("".join(json.dumps({"scoped_call_id": f"noise:{i}", "pad": "x" * 100}) + "\n"
                             for i in range(300)))
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(0))
    original = reader.tool_output_index_paths_for_lookup

    def inspect(root):
        token = reader._PREPARE_INDEX_READ.get()
        assert type(token) is tuple and len(token) == 3
        identity, literal, digest = token
        assert type(digest) is bytes and len(digest) == 32
        assert all(type(item) in (str, int) for item in identity)
        assert literal == "absent:call"
        return original(root)

    monkeypatch.setattr(reader, "tool_output_index_paths_for_lookup", inspect)
    assert reader.prepare_tool_output_artifact_read(
        reader.ReadToolOutputArtifactRequest(tmp_path, "absent:call")).record is None


@pytest.mark.parametrize("cache_bytes", [4096, 16 * 1024 * 1024])
def test_positive_index_cache_verifies_bytes_with_identical_stat(tmp_path, monkeypatch, cache_bytes):
    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    line = (json.dumps({"scoped_call_id": ref, "marker": "old"}) + "\n"
            + json.dumps({"scoped_call_id": "noise:call", "pad": "x" * 8192}) + "\n")
    index.write_text(line)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(cache_bytes))
    signature = reader._blob_stat_signature
    monkeypatch.setattr(reader, "_blob_stat_signature", lambda info: (*signature(info)[:3], 0, 0))
    decoded = []
    original = reader.iter_decoded_tool_index_objects

    def count(*args, **kwargs):
        decoded.append(1)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", count)
    cold = reader._parsed_artifact_index(index, literal_ref=ref)
    assert cold[1][ref][0]["marker"] == "old"
    assert reader._parsed_artifact_index(index, literal_ref=ref) is cold
    assert len(decoded) == 1
    index.write_text(line.replace('"old"', '"new"'))
    fresh = reader._parsed_artifact_index(index, literal_ref=ref)
    assert fresh[1][ref][0]["marker"] == "new" and len(decoded) == 2
    assert reader._parsed_artifact_index(index, literal_ref=ref) is fresh
    assert len(decoded) == 2
    # 同一物理长度也可变成资源损坏；旧正结果不能掩盖原严格解析错误。
    index.write_text('{"bad":' + "1" * 5000 + '}' + " " * (len(line) - 5009) + "\n")
    assert index.stat().st_size == len(line.encode())
    with pytest.raises(ValueError):
        reader._parsed_artifact_index(index, literal_ref=ref)
    assert len(decoded) == 3
    index.write_text(line.replace(ref, "newest:call"))
    newest = reader._parsed_artifact_index(index, literal_ref="newest:call")
    assert newest[1]["newest:call"][0]["marker"] == "old"


@pytest.mark.parametrize("cache_bytes", [4096, 16 * 1024 * 1024])
def test_positive_cache_hot_hit_still_reads_current_index_bytes(tmp_path, monkeypatch, cache_bytes):
    from contextlib import contextmanager

    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    index.write_text(json.dumps({"scoped_call_id": ref}) + "\n"
                     + json.dumps({"scoped_call_id": "noise:call", "pad": "x" * 8192}) + "\n")
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(cache_bytes))
    consumed = []

    class MeasuredRead:
        def __init__(self, handle):
            self.handle = handle

        def fileno(self):
            return self.handle.fileno()

        def seek(self, offset):
            return self.handle.seek(offset)

        def read(self, size):
            assert 0 < size <= 64 * 1024
            block = self.handle.read(size)
            consumed.append(len(block))
            return block

    class Source:
        path = index

        @contextmanager
        def open(self, mode):
            with index.open(mode) as handle:
                yield MeasuredRead(handle)

    source = Source()
    cold = reader._parsed_artifact_index(source, literal_ref=ref)
    assert sum(consumed) == index.stat().st_size
    consumed.clear()
    assert reader._parsed_artifact_index(source, literal_ref=ref) is cold
    assert sum(consumed) == index.stat().st_size


def test_oversized_literal_retains_only_structured_candidates_in_shared_budget(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    noise = [{"scoped_call_id": f"noise:{i}", "pad": ref + "x" * 512} for i in range(256)]
    expected = {"scoped_call_id": ref, "run_id": "wanted", "path": ""}
    index.write_text("".join(json.dumps(row) + "\n" for row in [*noise, expected]))
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    result = reader._parsed_artifact_index(index, literal_ref=ref)
    assert result[0] == (expected,)
    assert result[1][ref] == [expected]
    assert 0 < cache.bytes <= cache.max_bytes and len(cache.entries) == 1
    assert reader._parsed_artifact_index(index, literal_ref=ref) is result


def test_oversized_literal_over_budget_keeps_all_matches_without_caching(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    rows = [{"scoped_call_id": ref, "sequence": i, "pad": "x" * 512} for i in range(128)]
    index.write_text("".join(json.dumps(row) + "\n" for row in rows))
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    decoded = []
    original = reader.iter_decoded_tool_index_objects

    def count(*args, **kwargs):
        decoded.append(1)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", count)
    for _ in range(2):
        result = reader._parsed_artifact_index(index, literal_ref=ref)
        assert list(result[0]) == rows and result[1][ref] == rows
        assert cache.bytes == 0 and not cache.entries
    assert len(decoded) == 2


def test_oversized_literal_filter_preserves_unrelated_decode_resource_errors(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    ref = "wanted:call"
    index.write_text(json.dumps({"scoped_call_id": ref, "path": ""}) + "\n"
                     + '{"bad":' + "1" * 5000 + '}\n')
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    with pytest.raises(ValueError):
        reader._parsed_artifact_index(index, literal_ref=ref)
    assert not cache.entries and cache.bytes == 0


def test_literal_and_full_results_use_separate_shared_budget_entries(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    refs = ["wanted:one", "wanted:two"]
    records = [{"scoped_call_id": ref, "path": ""} for ref in refs]
    index.write_text("".join(json.dumps(row) + "\n" for row in records)
                     + json.dumps({"pad": "x" * 8192}) + "\n")
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    results = [reader._parsed_artifact_index(index, literal_ref=ref) for ref in refs]
    assert [result[0] for result in results] == [(records[0],), (records[1],)]
    for ref, result in zip(refs, results):
        assert reader._parsed_artifact_index(index, literal_ref=ref) is result
    assert 0 < cache.bytes <= 4096 and len(cache.entries) == 2
    full = reader._parsed_artifact_index(index)
    assert len(full[0]) == 3
    assert cache.bytes <= 4096 and len(cache.entries) == 2


def test_concurrent_oversized_literal_cache_keeps_ref_partitions(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    index = tmp_path / "index.jsonl"
    refs = ["wanted:one", "wanted:two"]
    index.write_text("".join(json.dumps({"scoped_call_id": ref, "path": ""}) + "\n" for ref in refs)
                     + json.dumps({"pad": "x" * 8192}) + "\n")
    cache = reader._VerifiedArtifactCache(4096)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", cache)
    original = reader.iter_decoded_tool_index_objects
    barrier = threading.Barrier(2)

    def simultaneous(*args, **kwargs):
        barrier.wait(timeout=5)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda ref: reader._parsed_artifact_index(index, literal_ref=ref), refs))
    for ref, result in zip(refs, results):
        assert [row["scoped_call_id"] for row in result[0]] == [ref]
        assert reader._parsed_artifact_index(index, literal_ref=ref) is result
    assert cache.bytes <= 4096 and len(cache.entries) == 2



# ============================================================================
# Source: agent_py_agent/tests/test_r8_current_obligations.py
# ============================================================================

@pytest.mark.parametrize('history', [10, 10_000])
def test_terminal_inventory_proofs_seek_current_indexes_inside_fixed_vm_budget(tmp_path, history):
    path = tmp_path / 'runtime.db'
    RuntimeRepository(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.executemany('INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES(?,?,?,?,?)',
            ((f't-{i}', 'target', 'done', 1, 1) for i in range(history)))
        conn.executemany('INSERT INTO agent_runs(agent_run_id,task_run_id,run_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
            ((f'a-{i}', f't-{i}', 'same-run', 'done', 1, 1) for i in range(history)))
        conn.executemany('INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) VALUES(?,?,?,?,?,?)',
            ((f'at-{i}', f'a-{i}', 1, 'done', 1, 2) for i in range(history)))
        conn.executemany('INSERT INTO wake_queue(wake_id,root_task_id,root_run_id,kind,next_due_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            ((f'w-{i}', 'target', 'same-run', 'test', 1, 'consumed', 1, 1) for i in range(history)))
        conn.commit()
        queries, steps = [], []
        conn.set_trace_callback(queries.append)
        def progress():
            steps.append(True)
            return len(steps) >= 20
        conn.set_progress_handler(progress, 100)
        try:
            assert recorded_run_ids(conn, ['same-run', 'missing']) == {'same-run'}
            assert closed_task_ids(conn, ['target', 'missing']) == {'target'}
            assert closed_attempt_bindings(conn, {'same-run': f'at-{history-1}'}) == (True, {'target'})
        finally:
            conn.set_progress_handler(None, 0)
            conn.set_trace_callback(None)
        assert len(steps) < 20  # 10与10000条终态库存共用<2000条VM指令的同一上限。
        plans = [detail for query in queries for *_unused, detail in conn.execute('EXPLAIN QUERY PLAN ' + query)]
        for index in ('idx_task_runs_current_task', 'idx_agent_runs_current_run', 'idx_wake_queue_current_task', 'idx_wake_queue_current_run'):
            assert any(index in plan for plan in plans), plans
        assert not any('SCAN task_runs' in plan or 'SCAN t ' in plan for plan in plans), plans


def test_unknown_and_current_obligations_are_never_empty_terminal_proofs(tmp_path):
    path = tmp_path / 'runtime.db'
    RuntimeRepository(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES('t','target','done',1,1)")
        conn.execute("INSERT INTO agent_runs(agent_run_id,task_run_id,run_id,status,created_at,updated_at) VALUES('a','t','run','unknown',1,1)")
        conn.execute("INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) VALUES('at','a',1,'done',1,2)")
        assert closed_task_ids(conn, ['target']) == set()
        assert closed_attempt_bindings(conn, {'run': 'at'})[0] is False
        conn.execute("UPDATE agent_runs SET status='done'")
        conn.execute("INSERT INTO wake_queue(wake_id,root_task_id,root_run_id,kind,next_due_at,status,created_at,updated_at) VALUES('w','target','run','test',1,'pending',1,1)")
        assert closed_task_ids(conn, ['target']) == set()
        assert closed_attempt_bindings(conn, {'run': 'at'})[0] is False
        conn.execute("UPDATE wake_queue SET status='consumed'")
        assert closed_task_ids(conn, ['target']) == {'target'}
        assert closed_attempt_bindings(conn, {'run': 'at'}) == (True, {'target'})
        conn.execute('DROP INDEX idx_wake_queue_current_task')
        with pytest.raises(sqlite3.OperationalError):
            closed_task_ids(conn, ['target'])


def test_old_schema_is_unknown_until_normal_canonical_schema_migration(tmp_path):
    path = tmp_path / 'runtime.db'
    RuntimeRepository(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES('t','target','done',1,1)")
        conn.execute('DROP INDEX idx_task_runs_current_task')
        conn.execute("UPDATE metadata SET value='pre-r8-schema' WHERE key='sqlite_schema_fingerprint'")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError):
            closed_task_ids(conn, ['target'])
    RuntimeRepository(path)
    with closing(sqlite3.connect(path)) as conn:
        assert closed_task_ids(conn, ['target']) == {'target'}
        assert conn.execute("SELECT value FROM metadata WHERE key='sqlite_schema_fingerprint'").fetchone()[0] != 'pre-r8-schema'



# ============================================================================
# Source: agent_py_agent/tests/test_r8_fts_catchup.py
# ============================================================================

def _seed(home, count=4):
    store = LocalStore(home.owner_data_dir / 'local_store' / 'local.db')
    items = [store.upsert_record(source_type='memory', source_id=str(i), title=str(i),
                                 content=f'catchupneedle old body {i}') for i in range(count)]
    with store._connection() as conn:
        conn.execute('UPDATE records SET rowid=rowid+?', (count + 100,))
        conn.execute("DELETE FROM metadata WHERE key IN ('records_fts_rowid_aligned','records_fts_rebuild_pending')")
        conn.commit()
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    return store, items


def _home(tmp_path, count=4):
    home = ensure_my_agent_home(tmp_path)
    store, items = _seed(home, count)
    return home, store, items


def _fake_step(calls, status='partial', progressed=True, advance=None):
    def step(home, previous, now, **kwargs):
        calls.append(kwargs.get('deadline'))
        if advance is not None:
            advance()
        return {'status': status, 'progressed': progressed, 'complete': False,
                'cursor': {'pending': ['data/local_store/local.db']}, 'next_at': now + 60}
    return step


@pytest.mark.usefixtures('fts_slice_clock')
def test_owner_step_cap_and_nested_global_budget_are_not_reset(tmp_path, monkeypatch):
    home, _, _ = _home(tmp_path)
    calls = []
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', _fake_step(calls))
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20) as budget:
        report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
        assert report['catchup']['steps'] == upgrade.FTS_UPGRADE_CATCHUP_MAX_STEPS_COUNT
        assert budget.remaining_steps == 4096 - 1024
    calls.clear()
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=3) as outer:
        with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20):
            report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
        assert report['catchup']['steps'] == len(calls) == 3
        assert outer.remaining_steps == 0
        assert upgrade.advance_upgrade(home.owner_home_dir, {}, 1000) is None
    assert upgrade.current_tick_budget() is None


@pytest.mark.parametrize('status', ['partial', 'deferred', 'unsupported_document', 'unknown'])
@pytest.mark.usefixtures('fts_slice_clock')
def test_no_progress_and_deferred_stop_the_burst(tmp_path, monkeypatch, status):
    home, _, _ = _home(tmp_path)
    calls = []
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', _fake_step(calls, status, False))
    report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['catchup']['steps'] == len(calls) == 1
    assert report['status'] == status
    assert report['next_at'] == 1060


def test_owner_and_global_deadlines_are_shared_without_restarting_clock(tmp_path, monkeypatch):
    home, _, _ = _home(tmp_path)
    clock, calls = SimpleNamespace(now=1000.0), []
    proxy = SimpleNamespace(**vars(upgrade.time))
    proxy.monotonic = lambda: clock.now
    monkeypatch.setattr(upgrade, 'time', proxy)
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice',
        _fake_step(calls, advance=lambda: setattr(clock, 'now', clock.now + 0.6)))
    with upgrade.fts_upgrade_tick_scope(deadline=1004):
        first = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
        assert first['catchup']['steps'] == 4
        assert calls[0] == pytest.approx(1000.025)
        assert set(calls[1:]) == {1002.0}
        calls.clear()
        second_started = clock.now
        second = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
        assert second['catchup']['steps'] == 3
        assert calls[0] == pytest.approx(second_started + .025)
        assert set(calls[1:]) == {1004.0}
        assert upgrade.advance_upgrade(home.owner_home_dir, {}, 1000) is None


@pytest.mark.usefixtures('fts_slice_clock')
def test_business_writer_progresses_between_real_slices(tmp_path, monkeypatch):
    home, store, items = _home(tmp_path, 400)
    original, calls = upgrade._advance_upgrade_slice, []
    ready, written, errors = Event(), Event(), []
    writer_after = []
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if not ready.is_set():
            if result['progressed']:
                # 冷首片可能只提交job或耗尽准备预算；在真实复制进展后才交错writer。
                assert result['progressed']
                writer_after.append(len(calls))
                ready.set()
                assert written.wait(3)
            else:
                assert result['status'] in {'partial', 'budget_exhausted', 'discovery'}
        return result
    def write():
        try:
            assert ready.wait(3)
            other = LocalStore(store.db_path)
            other.upsert_record(source_type='memory', source_id='0', title='changed', content='betweenstepsneedle')
            assert other.delete_record(items[1].id)
        except BaseException as error:
            errors.append(error)
        finally:
            written.set()
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', observed)
    worker = Thread(target=write)
    worker.start()
    try:
        state = {}
        for turn in range(8):
            maintenance_now = 1000 + turn * 61
            report = upgrade.advance_upgrade(home.owner_home_dir, state, maintenance_now)
            state = {'local_store_fts_upgrade': report}
            if ready.is_set():
                break
        assert ready.is_set(), calls
    finally:
        written.set()
        worker.join(5)
    assert not worker.is_alive() and not errors and writer_after
    for turn in range(30):
        with store._connection() as conn:
            value = conn.execute("SELECT value FROM metadata WHERE key='records_fts_rowid_aligned'").fetchone()
        if value is not None and value[0] == 'true':
            break
        report = upgrade.advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': report}, maintenance_now + 61 + turn * 61)
    assert {row.id for row in store._search_fts('betweenstepsneedle', limit=5,
        source_type=None, visibility=None)} == {items[0].id}
    assert store.get_record(items[1].id) is None
    assert any(result['progressed'] for result in calls[writer_after[0]:]), calls
    with store._connection() as conn:
        assert {row[0] for row in conn.execute('SELECT id FROM records_fts')} == {
            item.id for item in items if item.id != items[1].id}


@pytest.mark.usefixtures('fts_slice_clock')
def test_elapsed_slice_keeps_partial_job_and_resumes_under_fresh_budget(tmp_path, monkeypatch, request):
    clock = request.getfixturevalue('fts_slice_clock')
    home, store, items = _home(tmp_path)
    original = upgrade._advance_upgrade_slice

    def expire_after_real_slice(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now += .030
        return result

    with monkeypatch.context() as patch:
        patch.setattr(upgrade, '_advance_upgrade_slice', expire_after_real_slice)
        with upgrade.fts_upgrade_tick_scope(deadline=clock.now + .025, max_steps=10) as budget:
            partial = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
            assert partial['status'] == 'partial' and partial['complete'] is False
            assert partial['cursor']['pending'] == ['data/local_store/local.db']
            assert budget.remaining_steps == 9 and clock.now > budget.deadline
            assert upgrade.advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': partial}, 1061) is None
    with store._connection() as conn:
        job_id = conn.execute('SELECT job_id FROM maintenance_fts_job').fetchone()[0]
        assert conn.execute("SELECT value FROM metadata WHERE key='records_fts_rowid_aligned'").fetchone()[0] == 'false'
    report = partial
    for turn in range(20):
        with upgrade.fts_upgrade_tick_scope(deadline=clock.now + .025, max_steps=1):
            report = upgrade.advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': report}, 1061 + turn * 61)
        if report['complete']:
            break
    assert report['complete']
    with store._connection() as conn:
        assert conn.execute('SELECT job_id FROM maintenance_fts_job').fetchone()[0] == job_id
        assert conn.execute("SELECT value FROM metadata WHERE key='records_fts_rowid_aligned'").fetchone()[0] == 'true'
    assert {row.id for row in store.search('catchupneedle')} == {item.id for item in items}


@pytest.mark.usefixtures('fts_slice_clock')
def test_exhausted_index_budget_does_not_consume_ttl_permission_or_work(tmp_path):
    homes = [_home(tmp_path / name)[0] for name in ('one', 'two')]
    for home in homes:
        policy = json.loads(home.owner_retention_json.read_text())
        policy.update(maintenance_enabled=True, cache_days=1)
        home.owner_retention_json.write_text(json.dumps(policy))
        cache = home.owner_cache_dir / 'expired.tmp'
        cache.write_text('expired')
        os.utime(cache, (1, 1))
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1) as budget:
        for home in homes:
            outcome = run_owner_retention_if_due(home, now=200_000)
            assert outcome.ran and outcome.apply_outcome == 'applied'
            if (home.owner_cache_dir / 'expired.tmp').exists():
                assert outcome.retention is not None and outcome.retention.scan_partial
        assert budget.remaining_steps == 0
        # TTL沿原partial/due后继推进；同一已耗尽FTS预算不能吞掉清理权限或要求一次扫完。
        for turn in range(8):
            if all(not (home.owner_cache_dir / 'expired.tmp').exists() for home in homes):
                break
            for home in homes:
                if (home.owner_cache_dir / 'expired.tmp').exists():
                    outcome = run_owner_retention_if_due(home, now=200_001 + turn)
                    assert outcome.ran and outcome.apply_outcome == 'applied'
            assert budget.remaining_steps == 0
    assert all(not (home.owner_cache_dir / 'expired.tmp').exists() for home in homes)
    second = LocalStore(homes[1].owner_data_dir / 'local_store' / 'local.db')
    with second._connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_job'").fetchone() is None


@pytest.mark.usefixtures('fts_slice_clock')
def test_controller_retains_unserved_owner_and_future_hint(tmp_path):
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        ensure_owner_home,
        home_paths_with_owner,
    )
    from agent_py_agent.cli.gateway_loops import (
        STARTUP_GRACE_WINDOW_SECONDS,
        _GatewayOwnerMaintenanceController,
    )

    home = ensure_my_agent_home(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    identities = [OwnerIdentity.provider_user('feishu', name) for name in ('a-first', 'b-second')]
    stores = []
    for identity in identities:
        scoped = home_paths_with_owner(home, ensure_owner_home(home.root, identity))
        stores.append(_seed(scoped)[0])
    agent = SimpleNamespace(home_paths=home,
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64))
    controller = _GatewayOwnerMaintenanceController(SimpleNamespace(agent=agent))
    controller._startup_started_at = time.monotonic() - STARTUP_GRACE_WINDOW_SECONDS - 1
    hints = []
    controller._wake_hints = SimpleNamespace(note_maintenance=lambda *args, **kwargs: hints.append((args, kwargs)))
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1):
        controller.tick(now=1000)
    second_key = (identities[1].provider, identities[1].owner_kind, identities[1].owner_id)
    assert second_key in controller._priority
    assert hints[-1][1]['not_before'] == 1060
    assert controller._fts_hint_not_before == 1060
    with stores[1]._connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_job'").fetchone() is None
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1):
        controller.tick(now=1061)
    with stores[1]._connection() as conn:
        assert conn.execute("SELECT 1 FROM maintenance_fts_job").fetchone()


def test_hint_sampling_failure_preserves_original_business_exception(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_maintenance
    from agent_py_agent.cli import gateway_loops

    home = ensure_my_agent_home(tmp_path)
    agent = SimpleNamespace(home_paths=home,
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64))
    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace(agent=agent))
    controller._startup_started_at = time.monotonic() - gateway_loops.STARTUP_GRACE_WINDOW_SECONDS - 1
    original = PermissionError('original maintenance source revoked')
    def failure(*args, **kwargs):
        proxy = SimpleNamespace(**vars(upgrade.time))
        def broken_clock():
            raise RuntimeError('hint clock failed')
        proxy.monotonic = broken_clock
        monkeypatch.setattr(upgrade, 'time', proxy)
        raise original
    monkeypatch.setattr(owner_maintenance, 'invoke_owner_maintenance', failure)
    with pytest.raises(PermissionError) as caught:
        controller.tick(now=1000)
    assert caught.value is original


def test_original_controller_deadline_starts_before_base_owner(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_maintenance
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
    from agent_py_agent.cli import gateway_loops

    home = ensure_my_agent_home(tmp_path)
    for name in ('one', 'two', 'three'):
        ensure_owner_home(home.root, OwnerIdentity.provider_user('feishu', name))
    clock = SimpleNamespace(now=1000.0)
    for module in (upgrade, gateway_loops):
        proxy = SimpleNamespace(**vars(module.time))
        proxy.monotonic = lambda: clock.now
        monkeypatch.setattr(module, 'time', proxy)
    agent = SimpleNamespace(home_paths=home,
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64))
    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace(agent=agent))
    controller._startup_started_at = 1000 - gateway_loops.STARTUP_GRACE_WINDOW_SECONDS - 1
    calls = []
    def consume(callback, scoped, **kwargs):
        calls.append(upgrade.current_tick_budget().deadline)
        clock.now += 19 if len(calls) == 1 else 2
        return owner_maintenance.OwnerMaintenanceResult(False, 'not_due')
    monkeypatch.setattr(owner_maintenance, 'invoke_owner_maintenance', consume)
    controller.tick(now=1000)
    assert calls == [1020, 1020]
    assert len(controller._priority) >= 2
    assert controller._fts_hint_not_before == 1060


def test_foreign_pid_budget_never_touches_inherited_lock():
    class PoisonLock:
        def __enter__(self):
            raise AssertionError('inherited lock must not be acquired')
        def __exit__(self, *_args):
            raise AssertionError('inherited lock must not be released')
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20) as budget:
        budget.pid = -1
        budget.lock = PoisonLock()
        assert not budget.reserve()
        assert budget.exhausted()
        assert upgrade.current_tick_budget() is None
    assert upgrade.current_tick_budget() is None


def test_closed_copied_budget_cannot_restart_catchup(tmp_path, monkeypatch):
    from contextvars import copy_context

    home, _, _ = _home(tmp_path)
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20):
        captured = copy_context()
    def forbidden(*args, **kwargs):
        pytest.fail('closed copied budget must not enter a new slice')
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', forbidden)
    assert captured.run(lambda: upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)) is None


@pytest.mark.usefixtures('fts_slice_clock')
def test_current_receipt_also_discharges_manifest_delta_and_map(tmp_path, monkeypatch):
    home, store, _ = _home(tmp_path, 130)
    report = None
    for turn in range(20):
        report = upgrade.advance_upgrade(home.owner_home_dir,
            {'local_store_fts_upgrade': report} if report else {}, 1000 + turn * 61)
        if report['status'] == 'current' and report['complete']:
            break
    assert report['status'] == 'current' and report['complete']
    with store._connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_retired'").fetchone() is None
        for table in ('maintenance_fts_docs', 'maintenance_fts_delta', 'maintenance_fts_retired_map'):
            assert conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is None


def _workspace_legacy(home, name):
    path = home.owner_home_dir / 'workspace' / 'runtime' / 'workspaces' / name / 'local_store' / 'local.db'
    store = LocalStore(path)
    item = store.upsert_record(source_type='memory', source_id='hot', title='hot', content='hothintneedle')
    with store._connection() as conn:
        conn.execute('UPDATE records SET rowid=rowid+100')
        conn.execute("DELETE FROM metadata WHERE key IN ('records_fts_rowid_aligned','records_fts_rebuild_pending')")
        conn.commit()
    return store, item


@pytest.mark.usefixtures('fts_slice_clock')
def test_recent_legacy_store_precedes_cold_workspace_discovery(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path)
    cold, _ = _workspace_legacy(home, 'cold-first')
    hot, item = _workspace_legacy(home, 'recent-hot')
    # 原目录页可晚到任意位置；真实候选及SQLite仍由正式入口复核。
    pages = []
    def cold_page(*args, **kwargs):
        pages.append(True)
        return ['cold-first'], {}, False
    monkeypatch.setattr(upgrade, 'directory_page', cold_page)
    LocalStore(hot.db_path)
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1):
        report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['store'] == hot.db_path.relative_to(home.owner_home_dir).as_posix()
    assert pages == [] and report['status'] in {'partial', 'complete'}
    assert {row.id for row in hot.search('hothintneedle')} == {item.id}
    # 提示不改变60秒节流，不让新消息把下一拍变成热自旋。
    assert upgrade.advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': report}, 1001) is None


@pytest.mark.usefixtures('fts_slice_clock')
def test_hot_hint_turn_keeps_cold_discovery_and_original_shared_budget(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path)
    cold, _ = _workspace_legacy(home, 'cold')
    hot, _ = _workspace_legacy(home, 'hot')
    cold.rebuild_fts()
    hot.rebuild_fts()
    pages = []
    def cold_page(*args, **kwargs):
        pages.append(True)
        return ['cold'], {}, False
    monkeypatch.setattr(upgrade, 'directory_page', cold_page)
    previous = {}
    selected = []
    for turn in range(4):
        upgrade.note_active_store(hot.db_path)
        with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1) as budget:
            report = upgrade.advance_upgrade(home.owner_home_dir, previous, 1000 + 61 * turn)
        assert budget.remaining_steps == 0
        selected.append(report['store'])
        previous = {'local_store_fts_upgrade': report}
    def relative(store):
        return store.db_path.relative_to(home.owner_home_dir).as_posix()
    assert selected == [relative(hot), relative(cold), relative(hot), relative(cold)]
    assert len(pages) == 2


@pytest.mark.usefixtures('fts_slice_clock')
def test_hot_hint_cannot_select_other_owner_or_bypass_revoked_commit(tmp_path, monkeypatch):
    first = ensure_my_agent_home(tmp_path / 'first')
    other = ensure_my_agent_home(tmp_path / 'other')
    local, _ = _workspace_legacy(first, 'first')
    foreign, _ = _workspace_legacy(other, 'foreign')
    LocalStore(foreign.db_path)
    LocalStore(local.db_path)
    from agent_py_agent.agent.io import sqlite_scope
    original_connect = sqlite_scope.connect_projection_sqlite
    opened = []
    def connect(path, *, read_only=False, timeout=5):
        opened.append(path)
        return original_connect(path, read_only=read_only, timeout=timeout)
    monkeypatch.setattr(sqlite_scope, 'connect_projection_sqlite', connect)
    original = PermissionError('maintenance source revoked')
    def revoked(operation):
        raise original
    with pytest.raises(PermissionError) as caught:
        upgrade.advance_upgrade(first.owner_home_dir, {}, 1000, commit=revoked)
    assert caught.value is original
    assert opened and all(path == local.db_path for path in opened)
    for store in (local, foreign):
        with store._connection() as conn:
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_job'").fetchone() is None


def test_hot_hint_source_symlink_is_not_opened(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path / 'owner')
    store, _ = _workspace_legacy(home, 'hot')
    LocalStore(store.db_path)
    original = store.db_path.with_name('original.db')
    store.db_path.rename(original)
    store.db_path.symlink_to(original)
    def forbidden(*args, **kwargs):
        pytest.fail('revoked hinted path must not open SQLite')
    monkeypatch.setattr(LocalStore, '_init_schema', forbidden)
    report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['scope'] == 'discovery_page' and 'store' not in report


def test_hot_hint_capacity_pid_boundary_and_noncanonical_paths(tmp_path, monkeypatch):
    from collections import OrderedDict
    from threading import Lock

    owners = OrderedDict()
    monkeypatch.setattr(upgrade, '_HOT_HINTS', (os.getpid(), Lock(), owners))
    for owner in range(70):
        for workspace in range(10):
            upgrade.note_active_store(tmp_path / f'owner-{owner}' / 'workspace' / 'runtime' /
                                      'workspaces' / str(workspace) / 'local_store' / 'local.db')
    assert len(owners) == upgrade._FTS_HINT_OWNER_MAX_COUNT
    assert all(len(stores) == upgrade._FTS_HINT_STORE_MAX_COUNT for stores in owners.values())
    before = tuple(owners)
    upgrade.note_active_store(tmp_path / 'external' / 'custom.db')
    upgrade.note_active_store('x' * (upgrade._FTS_HINT_PATH_MAX_CHARS + 1))
    assert tuple(owners) == before
    class ParentLock:
        def __enter__(self):
            pytest.fail('child must never enter inherited hint lock')
    monkeypatch.setattr(upgrade, '_HOT_HINTS', (os.getpid() - 1, ParentLock(), owners))
    upgrade.note_active_store(tmp_path / 'child' / 'data' / 'local_store' / 'local.db')
    assert upgrade._HOT_HINTS[0] == os.getpid() and upgrade._HOT_HINTS[2] is not owners
    assert tuple(owners) == before


def test_hint_failure_does_not_change_constructor_or_legacy_write(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path)
    def unavailable(path):
        raise RuntimeError('optional hint cache failed')
    monkeypatch.setattr(upgrade, 'note_active_store', unavailable)
    store, item = _workspace_legacy(home, 'hot')
    LocalStore(store.db_path)
    updated = store.upsert_record(source_type='memory', source_id='hot', title='changed', content='changedneedle')
    assert updated.id == item.id and store.get_record(item.id).content == 'changedneedle'
    assert {row.id for row in store.search('changedneedle')} == {item.id}


def test_legacy_write_refreshes_only_hint_without_extra_sql_queries(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path)
    store, item = _workspace_legacy(home, 'hot')
    seen, sql = [], []
    def note(path):
        seen.append(path)
    monkeypatch.setattr(upgrade, 'note_active_store', note)
    with store._connection() as conn:
        # 先完成SQLite原虚表连接的config懒加载；只计本次替换及提示新增的语句。
        conn.execute('SELECT 1 FROM records_fts LIMIT 0')
        conn.set_trace_callback(sql.append)
        store._replace_fts_row(conn, item.id, 'changed', 'newneedle')
        conn.commit()
        conn.set_trace_callback(None)
    assert seen == [store.db_path]
    assert [text for text in sql if text.startswith(('SELECT', 'PRAGMA'))] == []
    assert {row.id for row in store.search('newneedle')} == {item.id}


@pytest.mark.parametrize('slow_stage', ['precheck', 'constructor'])
def test_consumed_hot_hint_keeps_selected_store_when_slice_expires(tmp_path, monkeypatch, slow_stage):
    from agent_py_agent.agent.local_storage import maintenance

    home = ensure_my_agent_home(tmp_path)
    hot, item = _workspace_legacy(home, 'hot')
    LocalStore(hot.db_path)
    relative = hot.db_path.relative_to(home.owner_home_dir).as_posix()
    clock = SimpleNamespace(now=time.monotonic())
    proxy = SimpleNamespace(**vars(upgrade.time))
    proxy.monotonic = lambda: clock.now
    original_check = maintenance._local_events_alignment_needed
    original_init = LocalStore.__init__

    def check(connection):
        result = original_check(connection)
        if slow_stage == 'precheck':
            clock.now += .03
        return result

    def initialize(current, db_path, *args, **kwargs):
        original_init(current, db_path, *args, **kwargs)
        if slow_stage == 'constructor' and current.db_path == hot.db_path:
            clock.now += .03

    with monkeypatch.context() as patch:
        patch.setattr(upgrade, 'time', proxy)
        patch.setattr(maintenance, '_local_events_alignment_needed', check)
        patch.setattr(LocalStore, '__init__', initialize)
        report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['store'] == relative and report['status'] == 'budget_exhausted'
    assert report['cursor']['pending'] == [relative], 'consumed hot hint must retain its canonical candidate'
    assert upgrade._take_active_store(home.owner_home_dir, ()) is None
    with hot._connection() as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_job'").fetchone() is None
    # 上段已确定性证明真实耗尽不丢候选；续作只核原job完整性，不靠3轮实时磁盘延迟签完成。
    resumed_clock = SimpleNamespace(**vars(upgrade.time))
    resumed_at = time.monotonic()
    resumed_clock.monotonic = lambda: resumed_at
    with monkeypatch.context() as patch:
        patch.setattr(upgrade, 'time', resumed_clock)
        patch.setattr(maintenance, 'time', resumed_clock)
        for turn in range(3):
            report = upgrade.advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': report}, 1061 + turn * 61)
            if report['complete']:
                break
    assert report['complete']
    with hot._connection() as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='records_fts_rowid_aligned'").fetchone()[0] == 'true'
    assert {row.id for row in hot.search('hothintneedle')} == {item.id}



# ============================================================================
# Source: agent_py_agent/tests/test_r8_gateway_context_bounds.py
# ============================================================================

def _r8_gateway_history(tmp_path, count):
    store = ConversationStore(tmp_path / 'conversations')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    thread = store.threads.get_or_create({'canonical_user_id': 'local', 'channel': 'internal',
                                          'owner_home': str(tmp_path)})
    ids = tuple(f'old-{number}' for number in range(count))
    for number, identifier in enumerate(ids):
        link = ThreadTaskLink(thread.thread_id, identifier, '历史工作',
                              status='active' if number == 0 else 'completed',
                              created_at=float(number), task_path=str(workspace))
        store.storage.task_path(identifier).write_text(json.dumps(link.to_dict()))
    thread = replace(thread, task_ids=ids, active_task_ids=(ids[0],), workspace_task_id=ids[1])
    store.storage.thread_path(thread.thread_id).write_text(json.dumps(thread.to_dict()))
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'current', 'status': 'completed',
                      'task_path': str(workspace)})
    # 只用冷构造实例量测，迁移与建夹具成本不混入稳态读取。
    cold = ConversationStore(store.storage.root)
    return cold, cold.threads.require(thread.thread_id), workspace


def _read_counter(monkeypatch):
    original = store_tasks._read_task_link
    reads = []

    def read(path, task_id, **kwargs):
        reads.append(task_id)
        return original(path, task_id, **kwargs)

    monkeypatch.setattr(store_tasks, '_read_task_link', read)
    return reads


@pytest.mark.parametrize('count', [140, 1000])
def test_gateway_context_task_links_are_bounded_after_restart(tmp_path, monkeypatch, count):
    store, thread, _ = _r8_gateway_history(tmp_path, count)
    reads = _read_counter(monkeypatch)
    errors = request_context._GatewayLoadErrors()
    links, failures = request_context._gateway_task_links(store, thread.thread_id, errors)
    assert not failures
    assert {'old-0', 'old-1', 'current'} <= {link.task_id for link in links}
    assert len(reads) <= 130
    assert len(links) <= 130
    assert any(row['code'] == 'task_context_window' for row in errors.warnings)


def test_gateway_bound_old_task_outside_window_remains_selectable(tmp_path, monkeypatch):
    store, thread, _ = _r8_gateway_history(tmp_path, 160)
    reads = _read_counter(monkeypatch)
    errors = request_context._GatewayLoadErrors()
    selected = request_context._gateway_workspace_task(
        store, thread, None, errors, request_id='request',
        request={'id': 'request', 'conversation_runtime': {'request_id': 'request',
                 'thread_id': thread.thread_id, 'task_id': 'old-3'}},
    )
    assert not errors
    assert selected is not None and selected.task_id == 'old-3'
    assert len(reads) <= 132


def test_gateway_redactions_do_not_enumerate_terminal_history(tmp_path, monkeypatch):
    store, thread, workspace = _r8_gateway_history(tmp_path, 160)
    reads = _read_counter(monkeypatch)
    context = request_context.GatewayAskRunContext(
        agent=SimpleNamespace(conversation_store=store), request={'task_id': 'old-3'},
        request_path=tmp_path / 'request', response_path=tmp_path / 'response',
        request_id='request', on_chunk=None,
    )
    conversation = request_context.GatewayConversationContext(
        thread_id=thread.thread_id, workspace_task=request_context.GatewayWorkspaceSelection(
            task_id='old-3', status='completed', goal='原工作', task_path=str(workspace)),
    )
    pairs = request_history.gateway_identifier_redactions(context, conversation)
    assert ('old-3', '当前任务') in pairs
    assert ('old-0', '当前任务') in pairs
    assert len(reads) <= 131


def _completion(store, thread_id, number, *, root='old-3', handled=False):
    return store.observations.append({'thread_id': thread_id, 'event_type': 'subagent_runner_finished',
        'source_agent_id': f'child-{number}', 'parent_agent_id': root, 'root_task_id': root,
        'requires_main_agent': True, 'handled_at': 1.0 if handled else 0.0,
        'now': float(number + 1), 'metadata': {'task_id': f'child-{number}', 'status': 'DONE',
        'completion_schema_version': 'subagent-completion.v1', 'completion_message': '已完成'}})


def test_recent_completion_keeps_old_root_without_full_history(tmp_path, monkeypatch):
    store, thread, workspace = _r8_gateway_history(tmp_path, 160)
    _completion(store, thread.thread_id, 1)
    reads = _read_counter(monkeypatch)
    original = store.observations.recent_report

    def no_unbounded(*args, **kwargs):
        assert kwargs.get('limit', 20) > 0, '普通请求不能读取完整观察历史'
        return original(*args, **kwargs)

    monkeypatch.setattr(store.observations, 'recent_report', no_unbounded)
    selected = request_context.GatewayWorkspaceSelection(
        task_id='current', status='active', goal='继续', task_path=str(workspace))
    errors = request_context._GatewayLoadErrors()
    context = request_context._gateway_subagent_completion_context(store, thread.thread_id, selected, errors)
    assert not errors
    assert context['completion_root_task_ids'] == ['old-3']
    assert context['items'][0]['task_id'] == 'child-1'
    assert len(reads) <= 132
    assert context['coverage']['source_exhausted'] is True


def test_completion_window_discloses_unknown_older_total(tmp_path):
    store, thread, workspace = _r8_gateway_history(tmp_path, 160)
    for number in range(160):
        _completion(store, thread.thread_id, number, handled=True)
    selected = request_context.GatewayWorkspaceSelection(
        task_id='current', status='active', goal='继续', task_path=str(workspace))
    context = request_context._gateway_subagent_completion_context(store, thread.thread_id, selected, [])
    assert context['total'] <= 128
    assert context['items'][-1]['task_id'] == 'child-159'
    assert context['coverage']['source_exhausted'] is False
    assert context['coverage']['history_coverage'] == 'unknown'


def test_exact_workspace_successor_reads_only_its_chain(tmp_path, monkeypatch):
    store, thread, _ = _r8_gateway_history(tmp_path, 160)
    old = store.tasks.load('old-3')
    store.storage.task_path('old-3').write_text(json.dumps(
        replace(old, status='superseded', superseded_by_task_id='old-4').to_dict()))
    reads = _read_counter(monkeypatch)
    link = task_promotion._reusable_conversation_workspace_link(store, thread.thread_id, 'old-3')
    assert link is not None and link.task_id == 'old-4'
    assert reads == ['old-3', 'old-4']


def test_successor_allocation_does_not_load_other_terminal_links(tmp_path, monkeypatch):
    store, thread, workspace = _r8_gateway_history(tmp_path, 160)
    reads = _read_counter(monkeypatch)
    identifier = task_promotion._terminal_successor_task_id(store, thread.thread_id,
        base_id='old-3', source_id='source', task_path=str(workspace))
    assert identifier.startswith('old-3-continue-')
    assert len(reads) <= 6


def test_completion_ids_actually_sent_to_model_are_redacted(tmp_path):
    store, thread, workspace = _r8_gateway_history(tmp_path, 160)
    context = request_context.GatewayAskRunContext(
        agent=SimpleNamespace(conversation_store=store), request={},
        request_path=tmp_path / 'request', response_path=tmp_path / 'response',
        request_id='request', on_chunk=None)
    conversation = request_context.GatewayConversationContext(
        thread_id=thread.thread_id,
        workspace_task=request_context.GatewayWorkspaceSelection(
            task_id='current', status='active', goal='继续', task_path=str(workspace)),
        subagent_completions={'completion_root_task_ids': ['old-3'],
                             'items': [{'task_id': 'child-1', 'root_task_id': 'old-3'}]})
    pairs = request_history.gateway_identifier_redactions(context, conversation)
    assert ('old-3', '当前任务') in pairs
    assert ('child-1', '子代理任务') in pairs


def test_empty_recent_completion_window_keeps_unknown_coverage(tmp_path):
    store, thread, workspace = _r8_gateway_history(tmp_path, 160)
    for number in range(160):
        store.observations.append({'thread_id': thread.thread_id,
                                   'event_type': 'ordinary_status', 'now': float(number + 1)})
    selected = request_context.GatewayWorkspaceSelection(
        task_id='current', status='active', goal='继续', task_path=str(workspace))
    context = request_context._gateway_subagent_completion_context(store, thread.thread_id, selected, [])
    assert context['items'] == []
    assert context['coverage']['history_coverage'] == 'unknown'


def test_unhandled_completion_survives_many_newer_ordinary_events(tmp_path):
    store, thread, workspace = _r8_gateway_history(tmp_path, 160)
    _completion(store, thread.thread_id, 1)
    for number in range(300):
        store.observations.append({'thread_id': thread.thread_id,
            'event_type': 'ordinary_status', 'now': float(number + 10), 'handled_at': 1.0})
    # 只推进真实投影的固定片；迁移追平后，当前义务不能因物理尾窗移动而消失。
    for _ in range(20):
        if store.observations._index.maintenance()['coverage'] == 'complete':
            break
    else:
        pytest.fail('观察投影未持续推进到完整')
    selected = request_context.GatewayWorkspaceSelection(
        task_id='current', status='active', goal='继续', task_path=str(workspace))
    errors = request_context._GatewayLoadErrors()
    context = request_context._gateway_subagent_completion_context(store, thread.thread_id, selected, errors)
    assert not errors
    assert context['items'][0]['task_id'] == 'child-1'
    assert context['coverage']['pending_coverage'] == 'complete'
    assert context['coverage']['history_coverage'] == 'unknown'



# ============================================================================
# Source: agent_py_agent/tests/test_v15_w9_quote_expansion.py
# ============================================================================

def _case(tmp_path):
    home = tmp_path / 'owner'
    store = ConversationStore(home / 'conversations')
    thread = ConversationThread('thread-origin', 'canonical-user', owner_id='owner-one', owner_home=str(home),
        channel_bindings=(ChannelBinding('feishu', 'chat-one', 'user-one', 'canonical-user', 'thread-origin'),))
    store.threads.write(thread)
    scope = ConversationScope('owner-one', str(home), thread.thread_id, 'canonical-user', 'feishu', 'chat-one', 'user-one')
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_id='owner-one', owner_home_dir=home), conversation_store=store)
    conversation = SimpleNamespace(thread_id=thread.thread_id, scope=scope)
    request = {'prompt': '解释引用通知', 'metadata': {'channel_chat_id': 'chat-one', 'message_id': 'om_current',
        'channel_chat_type': 'group', 'parent_message_id': 'om_notice'}}
    return agent, request, conversation, thread


def _indexed(agent, thread, *, channel_id='om_notice', content='完整通知正文', **changes):
    message = agent.conversation_store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant',
        'content': content, 'channel': 'feishu', 'channel_message_id': channel_id,
        'metadata': {'task_id': 'task-one', 'channel_message_ids': [channel_id]}})
    entry = {'schema_version': 'outbound_message.v1', 'channel': 'feishu', 'channel_message_id': channel_id,
        'chat_id': 'chat-one', 'thread_id': thread.thread_id, 'conversation_message_id': message.message_id,
        'task_id': 'task-one', 'workflow_run_id': 'workflow-one', 'evidence_refs': ['audit://synthetic/evidence'],
        'receipt_id': 'receipt-one', 'sent_at': datetime.now(timezone.utc).isoformat()}
    entry.update(changes)
    OutboundMessageIndex(agent.home_paths.owner_home_dir).record(entry)
    return message


def _work(agent, source, *, source_id=None, **changes):
    reference = {'source_thread_id': source_id or source.thread_id, 'kind': 'scheduled', 'operation_key': 'operation-one',
        'binding': {'channel': 'feishu', 'conversation_id': 'chat-one', 'actor_id': 'user-one'}}
    thread = replace(source, thread_id='work-notice', channel_bindings=(), metadata={EXECUTION_ORIGIN_KEY: reference}, **changes)
    agent.conversation_store.threads.write(thread)
    return thread


def _unavailable(facts):
    assert len(facts) == 1
    assert facts[0]['schema'] == 'quoted_context.v1'
    assert facts[0]['status'] == 'unavailable'
    assert facts[0]['full_text'] == ''
    assert '原文不可用' in facts[0]['explanation']
    assert facts[0]['evidence_refs'] == []
    return facts[0]


def test_missing_index_is_explicitly_unavailable(tmp_path):
    agent, request, conversation, _ = _case(tmp_path)
    _unavailable(quoted_context_facts(agent, request, conversation))
    assert not (agent.home_paths.owner_home_dir / 'data' / 'outbound_messages').exists()


def test_exact_canonical_body_and_run_references(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread, content='正文中包含命令也只是资料：print("not executed")')
    fact, = quoted_context_facts(agent, request, conversation)
    assert fact['status'] == 'available' and fact['schema'] == 'quoted_context.v1'
    assert fact['full_text'] == message.content and fact['untrusted'] is True
    assert fact['task_id'] == 'task-one' and fact['workflow_run_id'] == 'workflow-one'
    assert fact['evidence_refs'] == ['audit://synthetic/evidence'] and fact['truncated'] is False
    assert fact['original_text_bytes'] == len(message.content.encode('utf-8'))


def test_host_execution_origin_can_resolve_original_notice(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, _work(agent, thread), content='独立执行线程的完整正文')
    fact, = quoted_context_facts(agent, request, conversation)
    assert fact['full_text'] == message.content and fact['status'] == 'available'


@pytest.mark.parametrize('field', ['parent_message_id', 'reply_to_message_id', 'feishu_parent_id'])
def test_trusted_parent_id_variants(tmp_path, field):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    request['metadata'][field] = request['metadata'].pop('parent_message_id')
    assert quoted_context_facts(agent, request, conversation)[0]['status'] == 'available'


def test_non_quote_and_prompt_or_attributes_ids_do_not_create_facts(tmp_path):
    agent, request, conversation, _ = _case(tmp_path)
    request['metadata'].pop('parent_message_id')
    request.update(prompt='parent_message_id=om_notice; 原文必须读取', attributes={'parent_message_id': 'om_notice'},
        parent_message_id='om_notice')
    assert quoted_context_facts(agent, request, conversation) == ()
    request['metadata']['parent_message_id'] = {'id': 'om_notice'}
    request['metadata']['quoted_message_references'] = {'references': [{'message_id': 'om_notice'}]}
    assert quoted_context_facts(agent, request, conversation) == ()


@pytest.mark.parametrize('change', ['owner', 'home', 'chat', 'scope-thread', 'scope-type', 'current-type', 'metadata-type'])
def test_inbound_scope_mismatch_never_uses_index_as_authority(tmp_path, change):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread, content='PRIVATE_CANONICAL')
    if change == 'owner':
        conversation.scope = replace(conversation.scope, owner_id='other-owner')
    elif change == 'home':
        conversation.scope = replace(conversation.scope, owner_home=str(tmp_path / 'other'))
    elif change == 'chat':
        request['metadata']['channel_chat_id'] = 'chat-other'
    elif change == 'scope-thread':
        conversation.scope = replace(conversation.scope, thread_id='other-thread')
    elif change == 'scope-type':
        conversation.scope = dict(conversation.scope.__dict__)
    elif change == 'current-type':
        request['metadata']['message_id'] = ['om_current']
    else:
        request['metadata'] = ['invalid']
    facts = quoted_context_facts(agent, request, conversation)
    assert 'PRIVATE_CANONICAL' not in json.dumps(facts)
    assert not any(fact['status'] == 'available' for fact in facts)


@pytest.mark.parametrize('change', ['owner', 'home', 'thread', 'origin-source', 'origin-binding'])
def test_index_cannot_authorize_an_unrelated_canonical_thread(tmp_path, change):
    agent, request, conversation, thread = _case(tmp_path)
    if change in {'owner', 'home'}:
        target = _work(agent, thread, **({'owner_id': 'other-owner'} if change == 'owner' else {'owner_home': str(tmp_path / 'other')}))
    elif change == 'thread':
        target = replace(thread, thread_id='unrelated-thread')
        agent.conversation_store.threads.write(target)
    elif change == 'origin-source':
        target = _work(agent, thread, source_id='missing-origin')
    else:
        target = _work(agent, thread)
        target = replace(target, metadata={EXECUTION_ORIGIN_KEY: {**target.metadata[EXECUTION_ORIGIN_KEY],
            'binding': {'channel': 'feishu', 'conversation_id': 'chat-other', 'actor_id': 'user-one'}}})
        agent.conversation_store.threads.write(target)
    _indexed(agent, target, content='PRIVATE_OTHER_THREAD')
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_index_other_chat_is_unavailable(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread, chat_id='chat-other', content='PRIVATE_OTHER_CHAT')
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_unicode_budget_is_utf8_and_hard_capped_at_eight_kib(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    text = '告警🙂' * 3000
    _indexed(agent, thread, content=text)
    for budget in (1, 7, 8192, 20000):
        fact, = quoted_context_facts(agent, request, conversation, max_text_bytes=budget)
        assert fact['status'] == 'available' and fact['truncated'] is True
        assert text.startswith(fact['full_text'])
        assert len(fact['full_text'].encode('utf-8')) <= min(budget, 8192)
        assert fact['original_text_bytes'] == len(text.encode('utf-8'))
        assert '截断' in fact['explanation']


def test_verified_references_share_total_budget_and_deduplicate_parent(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    request['metadata'].pop('parent_message_id')
    refs = []
    for number in range(10):
        channel_id = f'om_quote_{number}'
        _indexed(agent, thread, channel_id=channel_id, content='中🙂' * 1000)
        refs.append((provider_message_reference(channel_id), channel_id))
    request['metadata']['parent_message_id'] = 'om_quote_0'
    facts = quoted_context_facts(agent, request, conversation, references=tuple(refs), max_text_bytes=17)
    assert len(facts) == 8 and len({fact['source_message_ref'] for fact in facts}) == 8
    assert sum(len(fact['full_text'].encode('utf-8')) for fact in facts) <= 17
    assert all(fact['truncated'] is True for fact in facts)
    assert facts[0]['full_text'] == ('中🙂' * 1000).encode()[:17].decode('utf-8', errors='ignore')


def test_canonical_missing_or_read_error_never_returns_cached_text(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread, content='NO_CACHED_TEXT')
    path = agent.conversation_store.storage.message_path(thread.thread_id)
    path.write_text('')
    _unavailable(quoted_context_facts(agent, request, conversation))
    for result in ((message, [{'code': 'READ_ERROR'}]), (None, [])):
        monkeypatch.setattr(agent.conversation_store.messages, 'by_id_report', lambda *_args: result)
        _unavailable(quoted_context_facts(agent, request, conversation))

    def failed(*_args):
        raise OSError('CANONICAL_PRIVATE_ERROR')

    monkeypatch.setattr(agent.conversation_store.messages, 'by_id_report', failed)
    assert 'CANONICAL_PRIVATE_ERROR' not in json.dumps(_unavailable(quoted_context_facts(agent, request, conversation)))


def test_canonical_thread_error_is_unavailable(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    monkeypatch.setattr(agent.conversation_store.threads, 'load_report', lambda *_args: (thread, {'code': 'READ_ERROR'}))
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_passive_read_creates_no_files_or_history_and_uses_only_exact_ids(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    home = agent.home_paths.owner_home_dir
    before = {str(path.relative_to(home)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.rglob('*') if path.is_file()}

    def forbidden(*_args, **_kwargs):
        pytest.fail('引用展开不能扫描历史、写入或启动宿主')

    for name in ('append', 'append_once', 'recent_report'):
        monkeypatch.setattr(agent.conversation_store.messages, name, forbidden)
    monkeypatch.setattr(agent.conversation_store.threads, 'write', forbidden)
    facts = quoted_context_facts(agent, request, conversation)
    after = {str(path.relative_to(home)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.rglob('*') if path.is_file()}
    assert facts[0]['status'] == 'available' and before == after


def test_original_gateway_reference_validation_is_not_replaced(tmp_path):
    agent, request, conversation, _ = _case(tmp_path)
    ref = provider_message_reference('om_notice')
    request['metadata']['quoted_message_references'] = {'schema': 'quoted_message_references.v1', 'channel': 'feishu',
        'chat_id': 'chat-one', 'current_message_id': 'om_current', 'references': [{'ref': ref, 'message_id': 'om_notice'}]}
    assert _gateway_message_references(request, conversation) == ((ref, 'om_notice'),)
    request['metadata']['quoted_message_references']['current_message_id'] = 'om_other'
    with pytest.raises(ValueError, match='引用身份无效'):
        _gateway_message_references(request, conversation)
    request['metadata'].pop('parent_message_id')
    assert quoted_context_facts(agent, request, conversation) == ()


def test_only_verified_references_can_expand_without_parent_metadata(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread)
    request['metadata'].pop('parent_message_id')
    pair = (provider_message_reference('om_notice'), 'om_notice')
    fact, = quoted_context_facts(agent, request, conversation, references=(pair,))
    assert fact['full_text'] == message.content
    for references in ([pair], (('unbound-ref', 'om_notice'),), ((pair[0], []),), ({'message_id': 'om_notice'},)):
        assert quoted_context_facts(agent, request, conversation, references=references) == ()


@pytest.mark.parametrize('kind', ['schema', 'entries-type', 'entry-type', 'field-type', 'json'])
def test_malformed_index_contract_does_not_repair_or_expose_body(tmp_path, kind):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread, content='PRIVATE_INDEX_BODY')
    path = agent.home_paths.owner_home_dir / 'data' / 'outbound_messages' / (hashlib.sha256(b'om_notice').hexdigest()[:2] + '.json')
    payload = json.loads(path.read_text())
    if kind == 'schema':
        payload['schema_version'] = 'unknown.v2'
    elif kind == 'entries-type':
        payload['entries'] = 'not-a-list'
    elif kind == 'entry-type':
        payload['entries'] = [17]
    elif kind == 'field-type':
        payload['entries'][0]['conversation_message_id'] = ['not-an-id']
    raw = '{' if kind == 'json' else json.dumps(payload)
    path.write_text(raw)
    _unavailable(quoted_context_facts(agent, request, conversation))
    assert path.read_text() == raw


@pytest.mark.parametrize('change', ['id', 'thread', 'role', 'content-type', 'empty'])
def test_canonical_message_identity_and_type_are_rechecked(tmp_path, monkeypatch, change):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread)
    values = {'id': {'message_id': 'different-message'}, 'thread': {'thread_id': 'other-thread'},
        'role': {'role': 'user'}, 'content-type': {'content': ['invalid']}, 'empty': {'content': ''}}
    altered = replace(message, **values[change])
    monkeypatch.setattr(agent.conversation_store.messages, 'by_id_report', lambda *_args: (altered, []))
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_broken_canonical_thread_and_typed_exception_are_unavailable(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    agent.conversation_store.storage.thread_path(thread.thread_id).write_text('{')
    _unavailable(quoted_context_facts(agent, request, conversation))

    def failed(*_args):
        raise DataCorruptionError('PRIVATE_CANONICAL_DETAIL')

    monkeypatch.setattr(agent.conversation_store.threads, 'load_report', failed)
    facts = quoted_context_facts(agent, request, conversation)
    _unavailable(facts)
    assert 'PRIVATE_CANONICAL_DETAIL' not in json.dumps(facts)


def test_frozen_origin_must_match_current_chat_even_if_source_has_two_bindings(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    source = replace(thread, channel_bindings=(*thread.channel_bindings,
        ChannelBinding('feishu', 'chat-other', 'user-one', 'canonical-user', thread.thread_id)))
    agent.conversation_store.threads.write(source)
    work = _work(agent, source)
    reference = {**work.metadata[EXECUTION_ORIGIN_KEY],
        'binding': {'channel': 'feishu', 'conversation_id': 'chat-other', 'actor_id': 'user-one'}}
    work = replace(work, metadata={EXECUTION_ORIGIN_KEY: reference})
    agent.conversation_store.threads.write(work)
    _indexed(agent, work, content='PRIVATE_OTHER_ORIGIN_BINDING')
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_no_network_or_directory_history_scan_or_write_in_expansion(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread)
    original = agent.conversation_store.messages.by_id_report
    calls = []

    def exact_read(*args):
        calls.append(args)
        return original(*args)

    def forbidden(*_args, **_kwargs):
        pytest.fail('引用展开不能联网、目录扫描或写入')

    with monkeypatch.context() as guard:
        guard.setattr(agent.conversation_store.messages, 'by_id_report', exact_read)
        for name in ('glob', 'rglob', 'iterdir', 'write_text', 'write_bytes', 'mkdir'):
            guard.setattr(Path, name, forbidden)
        guard.setattr(socket, 'socket', forbidden)
        facts = quoted_context_facts(agent, request, conversation)
    assert facts[0]['full_text'] == message.content
    assert calls == [(thread.thread_id, message.message_id)]


def test_zero_quotes_and_non_quote_do_not_access_any_store(tmp_path):
    _agent, request, conversation, _thread = _case(tmp_path)
    assert quoted_context_facts(object(), request, conversation, max_quotes=0) == ()
    request['metadata'].pop('parent_message_id')
    assert quoted_context_facts(object(), request, conversation) == ()


def test_current_message_is_not_a_parent_lookup(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    request['metadata']['message_id'] = request['metadata']['parent_message_id']
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_gateway_params_include_quote_only_in_volatile_injections(tmp_path, monkeypatch):
    from copy import deepcopy

    from agent_py_agent.agent.gateway_parts import multiprocess_active_input, request_execution
    from agent_py_agent.agent.gateway_parts.request_context import GatewayConversationContext

    agent, request, minimal, thread = _case(tmp_path)
    message = _indexed(agent, thread, content='VOLATILE_CANONICAL_ONLY：完整正文')
    conversation = GatewayConversationContext(thread_id=thread.thread_id, cwd=str(tmp_path), scope=minimal.scope)
    request['inject'] = ['原有动态输入']
    before = deepcopy(request)
    # 本用例检查真实参数组装和正文读取，身份适配器与后台输入源不在此处重建。
    monkeypatch.setattr(request_execution, '_gateway_host_actor', lambda *_args: None)
    monkeypatch.setattr(multiprocess_active_input, 'pending_turn_input_source', lambda *_args: None)
    context = SimpleNamespace(agent=agent, request_id='turn-current', request=request,
        request_path=tmp_path / 'request.json', response_path=tmp_path / 'response.json', on_chunk=lambda _chunk: None)
    params = request_execution._gateway_run_params(request_execution._GatewayRunParamsRequest(
        request, context, conversation, request['prompt']))
    quote_blocks = [item for item in params.inject if 'quoted_context.v1' in item]
    assert len(quote_blocks) == 1
    fact, = json.loads(quote_blocks[0].split('\n', 1)[1])
    assert fact['full_text'] == message.content and fact['workflow_run_id'] == 'workflow-one'
    assert '原有动态输入' in params.inject
    assert request == before
    assert 'VOLATILE_CANONICAL_ONLY' not in json.dumps(params.task_attributes)
    assert 'VOLATILE_CANONICAL_ONLY' not in str(params.conversation_history_seed)
    assert 'VOLATILE_CANONICAL_ONLY' not in conversation.compact_summary


def test_canonical_provider_binding_must_match_the_index(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread, content='STALE_INDEX_MUST_NOT_EXPAND')
    altered = replace(message, channel_message_id='om_other', metadata={'channel_message_ids': ['om_other']})
    monkeypatch.setattr(agent.conversation_store.messages, 'by_id_report', lambda *_args: (altered, []))
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_feishu_topic_uses_base_chat_index_and_exact_logical_scope(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    base, logical = 'oc_topic_chat', 'oc_topic_chat:thread:omt_original'
    topic = replace(thread, channel_bindings=(replace(thread.channel_bindings[0], channel_conversation_id=logical),))
    agent.conversation_store.threads.write(topic)
    conversation.scope = replace(conversation.scope, channel_conversation_id=logical)
    request['metadata']['channel_chat_id'] = base
    message = _indexed(agent, topic, chat_id=base, content='原话题通知正文')
    fact, = quoted_context_facts(agent, request, conversation)
    assert fact['status'] == 'available' and fact['full_text'] == message.content
    request['metadata']['channel_chat_id'] = 'oc_another_chat'
    _unavailable(quoted_context_facts(agent, request, conversation))
