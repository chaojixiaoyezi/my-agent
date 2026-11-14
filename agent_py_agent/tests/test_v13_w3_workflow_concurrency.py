"""真实文件claim、Event交错与引擎快照；不使用sleep安排顺序。"""

import importlib
import json
import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets import asset_root_lock
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows import concurrency_policy as policy
from agent_py_agent.agent.workflows.engine import WorkflowEngine


def flow():
    return WorkflowDefinition(
        AssetDescriptor("shared", "workflow", "共享诊断"),
        (
            WorkflowStepSpec("a", "A", "llm_skill_analysis"),
            WorkflowStepSpec("b", "B", "llm_skill_analysis"),
        ),
    )


def test_dispatch_skip_queue_fifo_capacity_and_allow(tmp_path):
    entered, release = threading.Event(), threading.Event()
    second, release_second, third = threading.Event(), threading.Event(), threading.Event()
    order = []

    def first():
        order.append("a")
        entered.set()
        assert release.wait(10)
        return "first-result"

    def queued():
        order.append("b")
        second.set()
        assert release_second.wait(10)

    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(policy.dispatch, tmp_path, "workflow", "shared", "a", "queue", first)
        try:
            assert entered.wait(5)
            skipped = policy.dispatch(
                tmp_path,
                "workflow",
                "shared",
                "skip",
                "skip_if_running",
                lambda: pytest.fail("skip executed"),
            )
            assert skipped == {
                "status": "skipped",
                "error_code": "WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING",
                "running_run_id": "a",
            }
            assert (
                policy.dispatch(
                    tmp_path, "workflow", "shared", "allow", "allow", lambda: "parallel"
                )
                == "parallel"
            )
            queued_b = policy.dispatch(
                tmp_path, "workflow", "shared", "b", "queue", queued, queue_limit=2
            )
            queued_c = policy.dispatch(
                tmp_path,
                "workflow",
                "shared",
                "c",
                "queue",
                lambda: (order.append("c"), third.set()),
                queue_limit=2,
            )
            assert queued_b["status"] == queued_c["status"] == "queued"
            assert (queued_b["queue_position"], queued_c["queue_position"]) == (1, 2)
            full = policy.dispatch(
                tmp_path,
                "workflow",
                "shared",
                "d",
                "queue",
                lambda: pytest.fail("full executed"),
                queue_limit=2,
            )
            assert full["error_code"] == "WORKFLOW_RUN_QUEUE_FULL"
            assert not second.is_set() and not third.is_set()
            release.set()
            assert running.result(5) == "first-result"
            assert second.wait(5) and not third.is_set()
            release_second.set()
            assert third.wait(5)
            assert order == ["a", "b", "c"]
        finally:
            release.set()
            release_second.set()


@pytest.mark.parametrize("invalid", ["bad", "", None, 1, []])
def test_invalid_concurrency_policy_is_structured(tmp_path, invalid):
    result = policy.dispatch(
        tmp_path, "workflow", "shared", "bad", invalid, lambda: pytest.fail("invalid executed")
    )
    assert result["error_code"] == "INVALID_CONCURRENCY_POLICY"


def test_callback_exception_releases_claim(tmp_path):
    error = RuntimeError("fixture callback")

    def fail():
        raise error

    with pytest.raises(RuntimeError) as caught:
        policy.dispatch(tmp_path, "workflow", "shared", "one", "queue", fail)
    assert caught.value is error
    assert policy.dispatch(tmp_path, "workflow", "shared", "two", "queue", lambda: 8) == 8


def _die_holding_claim(home, entered):
    def die():
        entered.set()
        os._exit(0)

    policy.dispatch(home, "workflow", "shared", "dead", "queue", die)


def test_real_dead_process_claim_can_be_reclaimed(tmp_path):
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    process = context.Process(target=_die_holding_claim, args=(str(tmp_path), entered))
    process.start()
    process.join(10)
    assert entered.is_set() and process.exitcode == 0
    assert (
        policy.dispatch(tmp_path, "workflow", "shared", "alive", "queue", lambda: "reclaimed")
        == "reclaimed"
    )


def test_engine_event_interleaving_freezes_definition_and_queue(tmp_path):
    definition = flow()
    WorkflowStore(tmp_path).save_workflow(definition)
    entered, release, queued_done = threading.Event(), threading.Event(), threading.Event()
    active, maximum, results = 0, 0, []
    lock = threading.Lock()

    def analyze(spec, context, skills):
        nonlocal active, maximum
        if spec["step_id"] == "a":
            with lock:
                active += 1
                maximum = max(maximum, active)
            if context.run_inputs.get("first"):
                entered.set()
                assert release.wait(10)
        if spec["step_id"] == "b":
            with lock:
                active -= 1
            if not context.run_inputs.get("first"):
                queued_done.set()
        results.append(spec["name"])
        return {"value": 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(
            engine.run_workflow, "shared", {"first": True}, concurrency_policy="queue"
        )
        try:
            assert entered.wait(5)
            skip = engine.run_workflow("shared", concurrency_policy="skip_if_running")
            assert skip["error_code"] == "WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING"
            queued = engine.run_workflow("shared", concurrency_policy="queue")
            assert queued["status"] == "queued"
            # 入队完成后编辑资产，两个run都只能消费入队前快照。
            WorkflowStore(tmp_path).save_workflow(
                replace(
                    definition,
                    steps=(definition.steps[0], replace(definition.steps[1], name="edited")),
                )
            )
            release.set()
            done = first.result(5)
            assert queued_done.wait(5)
            assert maximum == 1 and results == ["A", "B", "A", "B"]
            assert done.execution_definition_ref == definition.execution_definition_ref
        finally:
            release.set()


def test_tool_policy_is_in_schema_and_invalid_has_specific_code(tmp_path):
    WorkflowStore(tmp_path).save_workflow(flow())
    tool = WorkflowsTool(tmp_path, analysis_handler=lambda *args: {"value": 1})
    assert tool.model_spec.input_schema["properties"]["concurrency_policy"]["enum"] == [
        "allow",
        "skip_if_running",
        "queue",
    ]
    result = tool.execute(
        {"action": "run", "workflow_id": "shared", "concurrency_policy": "invalid"}
    )
    assert not result.ok and json.loads(result.output)["error_code"] == "INVALID_CONCURRENCY_POLICY"
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"


def test_terminal_run_cannot_be_reversed_by_late_step_or_run(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def analyze(*args):
        entered.set()
        assert release.wait(10)
        return {"value": 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(engine.run_workflow, flow())
        try:
            assert entered.wait(5)
            release.set()
            done = running.result(5)
            stale = replace(done.steps[-1], status="running")
            with pytest.raises(ValueError):
                engine.store.save_step_result(done.workflow_run_id, stale)
            reverse = replace(done, status="running")
            with pytest.raises(ValueError):
                engine.store.save_run(reverse)
            assert engine.store.load_run(done.workflow_run_id).status == "succeeded"
        finally:
            release.set()


@pytest.fixture
def store(tmp_path):
    # 合成owners祖先使默认全局发现也留在测试树内，不落到运行宿主的真实全局根。
    value = ScriptStore(tmp_path / "app" / "owners" / "local" / "sample")
    value.save_script(AssetDescriptor("sample", "script", "样本"), "print(1)")
    return value


# LLM: 主线程一直持有真实根锁直到写线程完成；宽等待只容纳调度/I/O，不容许等锁后通过。
# 函数用途: 验证非阻塞脚本摘要不依赖Owner根锁，显式fold仍只记一次。
def test_summary_write_does_not_wait_for_asset_lock(store):
    started = time.monotonic()
    with ThreadPoolExecutor(1) as pool:
        with asset_root_lock(store.owner_home):
            pool.submit(
                store.record_run_summary, "sample", "succeeded", 12,
                blocking=False, run_id="run-1",
            ).result(timeout=5)
            print(f"owner-lock-contention elapsed_ms={(time.monotonic() - started) * 1000:.3f}")
    result = store.fold_run_summaries("sample")
    assert result["runtime_summary"]["run_count"] == 1
    assert result["runtime_summary"]["last_duration_ms"] == 12
    store.record_run_summary("sample", "succeeded", 12, blocking=False, run_id="run-1")
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1


# LLM: 新完成直接入聚合，旧输入才有spool ACK；保留提交后失败及重启去重的原断言。
# 函数用途: 用旧叶准备原迁移窗口，核对提交后ACK失败不会导致重复累计。
def test_commit_before_ack_replay_survives_new_store(store, monkeypatch):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    # 新完成没有spool ACK；此处沿原内部入口准备旧输入，保留迁移ACK重放的全部断言。
    spool.append(store.owner_home, "script", "sample",
                 dict(status="failed", duration_ms=4, run_id="durable"))
    unlink = spool.unlink_file_beneath

    def fail_ack(*args, **kwargs):
        raise PermissionError("ACK故障")

    monkeypatch.setattr(spool, "unlink_file_beneath", fail_ack)
    with pytest.raises(PermissionError):
        store.fold_run_summaries("sample")
    assert store.get_script("sample")["runtime_summary"]["run_count"] == 1
    monkeypatch.setattr(spool, "unlink_file_beneath", unlink)
    reopened = ScriptStore(store.owner_home)
    assert reopened.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1
    reopened.record_run_summary("sample", "failed", 4, blocking=True, run_id="durable")
    assert reopened.get_script("sample")["runtime_summary"]["run_count"] == 1


def test_failed_commit_leaves_pending(store, monkeypatch):
    store.record_run_summary("sample", "succeeded", 3, blocking=False, run_id="retry")
    write = store._write

    def fail(*args, **kwargs):
        raise OSError("写入故障")

    monkeypatch.setattr(store, "_write", fail)
    with pytest.raises(OSError):
        store.fold_run_summaries("sample")
    monkeypatch.setattr(store, "_write", write)
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1


def test_append_during_fold_snapshot_is_not_lost(store, monkeypatch):
    store.record_run_summary("sample", "succeeded", 1, blocking=False, run_id="first")
    entered, release = threading.Event(), threading.Event()
    write = store._write

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return write(*args, **kwargs)

    monkeypatch.setattr(store, "_write", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        folding = pool.submit(store.fold_run_summaries, "sample")
        assert entered.wait(2)
        try:
            appending = pool.submit(
                store.record_run_summary, "sample", "succeeded", 2, blocking=False, run_id="late"
            )
            appending.result(timeout=1)
        finally:
            release.set()
        assert folding.result(timeout=3)["runtime_summary"]["run_count"] == 1
    monkeypatch.setattr(store, "_write", write)
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 2


def test_parallel_append_and_repeated_fold(store):
    def add(index):
        store.record_run_summary(
            "sample", "succeeded", index, blocking=False, run_id=f"run-{index}"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(32)))
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 32
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 32


@pytest.mark.parametrize("asset_id", ["../escape", "/absolute", "a/b", ".."])
def test_spool_rejects_traversal(tmp_path, asset_id):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    with pytest.raises(ValueError):
        spool.append(
            tmp_path, "script", asset_id, {"run_id": "r", "status": "succeeded", "duration_ms": 1}
        )


def test_spool_rejects_symlink(store, tmp_path):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.owner_home / ".run-summary-spool").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        spool.append(
            store.owner_home,
            "script",
            "sample",
            {"run_id": "r", "status": "succeeded", "duration_ms": 1},
        )
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), -1, True])
def test_invalid_record_is_not_queued(store, duration):
    with pytest.raises(ValueError):
        store.record_run_summary("sample", "succeeded", duration, blocking=False, run_id="bad")
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 0


def test_workspace_shadow_and_global_are_not_written(store, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    overlay = ScriptStore(store.owner_home, workspace_root=workspace)
    overlay.save_script(
        AssetDescriptor("sample", "script", "覆盖"), "print(2)", target_scope="workspace"
    )
    before = (store.owner_home / "scripts/sample/script.json").read_bytes()
    assert (
        overlay.record_run_summary("sample", "succeeded", 1, blocking=False)["scope_layer"]
        == "workspace"
    )
    assert (store.owner_home / "scripts/sample/script.json").read_bytes() == before
    assert not (store.owner_home / ".run-summary-spool").exists()
    global_root = tmp_path / "global"
    ScriptStore(global_root).save_script(AssetDescriptor("builtin", "script", "全局"), "print(3)")
    reader = ScriptStore(store.owner_home, extra_roots=(global_root,))
    assert (
        reader.record_run_summary("builtin", "succeeded", 1, blocking=False)["scope_layer"]
        == "global"
    )
    assert not (store.owner_home / ".run-summary-spool").exists()


def test_runner_nonblocking_and_bounded_redacted_warning(tmp_path, monkeypatch, caplog):
    from agent_py_agent.agent.workflows import script_runner

    calls = []

    class FakeStore:
        def __init__(self, owner_home):
            pass

        def record_run_summary(self, script_id, **kwargs):
            calls.append(kwargs)
            raise PermissionError("token=synthetic-secret " + "x" * 2000)

    monkeypatch.setattr(script_runner, "ScriptStore", FakeStore)
    result = SimpleNamespace(
        script_id="sample",
        ok=True,
        error_code="",
        stderr_preview="",
        duration_ms=1,
        started_at=1,
        ended_at=2,
    )
    with caplog.at_level("WARNING"):
        script_runner._record_summary(tmp_path, result)
        script_runner._record_summary(tmp_path, result)
    assert calls[0]["blocking"] is False
    assert calls[0]["run_id"] == calls[1]["run_id"]
    records = [row for row in caplog.records if "summary" in row.getMessage().lower()]
    assert len(records) == 1
    assert records[0].error_type == "PermissionError"
    assert "synthetic-secret" not in records[0].getMessage()
    assert len(records[0].getMessage()) < 800


def test_append_during_open_asset_transaction(store, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    store.record_run_summary("sample", "succeeded", 1, blocking=False, run_id="initial")
    put = store._put

    def paused(asset_id, filename, content):
        result = put(asset_id, filename, content)
        if filename == "asset-transaction.json" and content is not None:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(store, "_put", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        folding = pool.submit(store.fold_run_summaries, "sample")
        assert entered.wait(2)
        try:
            pool.submit(
                store.record_run_summary,
                "sample",
                "failed",
                2,
                blocking=False,
                run_id="during-transaction",
            ).result(timeout=1)
        finally:
            release.set()
        folding.result(timeout=3)
    monkeypatch.setattr(store, "_put", put)
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 2


def test_edit_does_not_erase_idempotency_receipt(store):
    store.record_run_summary("sample", "succeeded", 1, run_id="stable")
    store.save_script(AssetDescriptor("sample", "script", "编辑后"), "print(2)")
    store.record_run_summary("sample", "succeeded", 1, run_id="stable")
    assert store.get_script("sample")["runtime_summary"]["run_count"] == 1


# LLM: 只按实际生命周期定位旧输入目录，坏JSON和叶链接仍必须拒绝且不污染摘要。
# 函数用途: 保持旧输入损坏和真实链接的安全拒绝回归。
def test_spool_corrupt_record_and_leaf_symlink_fail_closed(store, tmp_path):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    spool.append(
        store.owner_home,
        "script",
        "sample",
        {"run_id": "r", "status": "succeeded", "duration_ms": 1},
    )
    # 只跟随内部spool生命周期分区，不改变损坏记录和链接拒绝断言。
    directory = store.owner_home.joinpath(*spool._parts("script", "sample",
        store._summary_snapshot("sample")["metadata"].get("_run_summary_generation", "")))
    leaf = next(directory.glob("*.json"))
    leaf.write_text("{bad json", encoding="utf-8")
    with pytest.raises(ValueError):
        store.fold_run_summaries("sample")
    leaf.write_text("{}", encoding="utf-8")
    outside = tmp_path / "not-a-record"
    outside.write_text("unchanged", encoding="utf-8")
    (directory / ("f" * 32 + ".json")).symlink_to(outside)
    # 先修复第一条，确保此断言观察的是链接而非前面的坏JSON。
    leaf.write_text(
        '{"run_id":"r","status":"succeeded","duration_ms":1,"run_at":"2026-11-02T00:00:00+00:00"}',
        encoding="utf-8",
    )
    with pytest.raises(OSError):
        store.fold_run_summaries("sample")
    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert store.get_script("sample")["runtime_summary"]["run_count"] == 0


def test_spool_requires_root_lock_for_fold(store):
    spool = importlib.import_module("agent_py_agent.agent.workflows.run_summary_spool")
    from agent_py_agent.agent.assets.descriptor import RuntimeSummary

    with pytest.raises(RuntimeError):
        spool.fold(store.owner_home, "script", "sample", RuntimeSummary(), {}, lambda *args: None)


# LLM: 真实启动临时脚本，关闭内核沙箱不代表验证内核隔离；摘要明确通过正式fold收口。
# 函数用途: 验证执行器结果经spool到持久摘要的完整本地链路。
def test_real_script_runner_spools_then_folds(store):
    from agent_py_agent.agent.workflows import ScriptRunner, script_runner

    store.save_script(AssetDescriptor("sample", "script", "真实运行"), 'print("{\\"value\\": 7}")')
    result = ScriptRunner(store.owner_home, use_sandbox=False).run_script("sample")
    assert result.ok, (result.error_code, result.stderr_preview)
    assert result.output_data == {"value": 7}
    folded = store.fold_run_summaries("sample")["runtime_summary"]
    assert folded["run_count"] == 1 and folded["last_status"] == "succeeded"
    script_runner._record_summary(store.owner_home, result)
    assert store.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1
    print("real-script -> spool -> fold -> replay: succeeded")


# LLM: 子进程真正退出，不用内存缓存模拟重启；只在测试资产根注入中断点。
# 函数用途: 验证事务中断回滚与提交后ACK中断均可重放且最终只计一次。
@pytest.mark.parametrize("phase", ["journal", "ack"])
# LLM: 原journal/ACK退出边界仍属于旧spool迁移；真实子进程和重启断言不替换。
# 函数用途: 准备旧输入后在原两个持久化边界退出，核对重开不丢失、不双算。
def test_process_crash_and_reopen(store, phase):
    import os
    import subprocess
    import sys

    from agent_py_agent.agent.workflows import run_summary_spool
    # journal/ACK是旧输入迁移的两个边界；新完成事务的退出重放另有独立回归。
    run_summary_spool.append(store.owner_home, "script", "sample",
                             dict(status="succeeded", duration_ms=1, run_id="crash-run"))
    code = """import os, sys
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.workflows import run_summary_spool
store = ScriptStore(sys.argv[1])
if sys.argv[2] == 'journal':
    original = store._put
    def stop(asset_id, filename, content):
        original(asset_id, filename, content)
        if filename == 'asset-transaction.json' and content is not None:
            os._exit(17)
    store._put = stop
else:
    run_summary_spool.unlink_file_beneath = lambda *args: os._exit(17)
store.fold_run_summaries('sample')
"""
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(store.owner_home), phase],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert crashed.returncode == 17, crashed.stderr
    reopened = ScriptStore(store.owner_home)
    assert reopened.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1
    assert reopened.fold_run_summaries("sample")["runtime_summary"]["run_count"] == 1


# LLM: 与脚本摘要一样在主线程根锁未释放时等待真正完成，不能靠持锁线程超时解锁通过。
# 函数用途: 验证工作流摘要的非阻塞写入、显式fold及编辑后的幂等回执。
def test_workflow_summary_nonblocking_fold_and_edit_receipt(tmp_path):
    owner = tmp_path / "app/owners/local/test"
    store = WorkflowStore(owner)
    store.save_workflow(flow())
    with ThreadPoolExecutor(1) as pool:
        with asset_root_lock(owner):
            result = pool.submit(
                store.record_run_summary,
                "shared",
                "succeeded",
                12,
                blocking=False,
                run_id="workflow-stable",
            ).result(timeout=5)
            assert result.descriptor.runtime_summary.run_count == 0
    assert store.fold_run_summaries("shared").descriptor.runtime_summary.run_count == 1
    store.save_workflow(flow())
    store.record_run_summary("shared", "succeeded", 12, blocking=False, run_id="workflow-stable")
    assert store.fold_run_summaries("shared").descriptor.runtime_summary.run_count == 1


@pytest.mark.parametrize("action", ["workflows.run", "workflow_runs.resume"])
@pytest.mark.parametrize("strategy", ["skip_if_running", "queue"])
def test_business_entry_preserves_typed_receipts_under_overlap(tmp_path, action, strategy):
    from agent_py_agent.agent.common.cancellation import (
        CancellationToken,
        ToolCancelled,
        bind_cancellation_token,
    )
    from agent_py_agent.tests.test_business_v12_f4_workflows import service

    api, actor, _ = service(tmp_path)
    WorkflowStore(tmp_path).save_workflow(flow())
    calls = []
    finished = threading.Event()
    engine = WorkflowEngine(
        tmp_path, analysis_handler=lambda spec, *args: calls.append(spec["step_id"]) or {"value": 1}
    )
    params = {
        "action": action,
        "workflow_ref": "shared",
        "idempotency_key": "requested",
        "concurrency_policy": strategy,
    }
    if action == "workflow_runs.resume":
        token = CancellationToken()
        token.cancel("preflight fixture")
        with pytest.raises(ToolCancelled) as caught, bind_cancellation_token(token):
            engine.run_workflow("shared", owner_id="alice")
        source = caught.value.record
        assert source.effect_outcome == "not_started" and not calls
        params.pop("workflow_ref")
        params.update(workflow_run_id=source.workflow_run_id, expected_revision=source.revision)
    original_summary = engine._record_summary

    def summarize(record):
        original_summary(record)
        finished.set()

    engine._record_summary = summarize
    tool = WorkflowsTool(
        tmp_path, engine=engine, business_service=api, business_actor_provider=lambda: actor
    )
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(10)

    with ThreadPoolExecutor(1) as pool:
        holder = pool.submit(
            policy.dispatch, tmp_path, "workflow", "shared", "holder", "queue", hold
        )
        try:
            assert entered.wait(3)
            outcome = tool.execute(params)
            assert outcome.ok, outcome.output
            receipt = json.loads(outcome.output)
            assert receipt["status"] == ("queued" if strategy == "queue" else "skipped")
            assert receipt["state"] == ("pending" if strategy == "queue" else "completed")
            replay = tool.execute(params)
            assert replay.ok, replay.output
            assert json.loads(replay.output)["state"] == receipt["state"]
            if action == "workflows.run":
                assert json.loads(replay.output) == receipt
            if strategy == "skip_if_running":
                assert receipt["error_code"] == "WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING"
            assert not calls
            # 并发协议目录不能被当作run_id，业务公开列表仍可读取。
            listing = tool.execute({"action": "workflow_runs.list", "workflow_ref": "shared"})
            assert listing.ok, listing.output
            release.set()
            holder.result(5)
            if strategy == "queue":
                assert finished.wait(5)
                assert engine.store.load_run(receipt["workflow_run_id"]).status == "succeeded"
                assert calls == ["a", "b"]
            else:
                assert not calls
        finally:
            release.set()


def test_queued_cancel_keeps_recoverable_ledger_and_never_executes(tmp_path):
    from agent_py_agent.agent.common.cancellation import CancellationToken, bind_cancellation_token

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: calls.append("executed"))
    original_summary = engine._record_summary

    def summarize(record):
        original_summary(record)
        finished.set()

    engine._record_summary = summarize

    def hold():
        entered.set()
        assert release.wait(10)

    with ThreadPoolExecutor(1) as pool:
        holder = pool.submit(
            policy.dispatch, tmp_path, "workflow", "shared", "holder", "queue", hold
        )
        try:
            assert entered.wait(3)
            token = CancellationToken()
            with bind_cancellation_token(token):
                receipt = engine.run_workflow(flow(), concurrency_policy="queue")
                token.cancel("cancel while queued")
            assert receipt["status"] == "queued"
            release.set()
            holder.result(5)
            assert finished.wait(5)
            saved = engine.store.load_run(receipt["workflow_run_id"])
            assert saved.status == "paused_for_resume" and saved.effect_outcome == "not_started"
            assert saved.breakpoint["can_resume"] and not calls
        finally:
            release.set()


def test_summary_pause_resume_final_status_and_repeated_fold(tmp_path):
    store = WorkflowStore(tmp_path)
    store.save_workflow(flow())
    state = {"fail": True}

    def analyze(*args):
        if state["fail"]:
            raise ValueError("first attempt")
        return {"value": 1}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    failed = engine.run_workflow("shared")
    assert failed.status == "failed"
    assert store.fold_run_summaries("shared").descriptor.runtime_summary.last_status == "failed"
    state["fail"] = False
    resumed = engine.resume_workflow_run(failed.workflow_run_id)
    assert resumed.status == "succeeded"
    summary = store.fold_run_summaries("shared").descriptor.runtime_summary
    assert (
        summary.run_count == 2
        and summary.last_status == "succeeded"
        and summary.consecutive_failures == 0
    )
    engine._record_summary(resumed)
    assert store.fold_run_summaries("shared").descriptor.runtime_summary == summary


def test_admission_write_failure_preserves_queued_execution_unknown(tmp_path, monkeypatch):
    import agent_py_agent.agent.workflows.engine as engine_module

    WorkflowStore(tmp_path).save_workflow(flow())
    engine = WorkflowEngine(tmp_path)
    monkeypatch.setattr(
        engine_module,
        "dispatch",
        lambda *args, **kwargs: {"status": "queued", "queue_position": 1, "run_id": args[3]},
    )

    def fail(*args):
        raise OSError("admission ACK failed")

    monkeypatch.setattr(engine.store, "save_admission_receipt", fail)
    outcome = WorkflowsTool(tmp_path, engine=engine).execute(
        {"action": "run", "workflow_id": "shared", "concurrency_policy": "queue"}
    )
    assert not outcome.ok and outcome.effect_outcome == "unknown"
    assert json.loads(outcome.output)["status"] == "queued"


def test_queue_full_is_not_a_successful_skip(tmp_path, monkeypatch):
    import agent_py_agent.agent.workflows.engine as engine_module

    WorkflowStore(tmp_path).save_workflow(flow())
    monkeypatch.setattr(
        engine_module,
        "dispatch",
        lambda *args, **kwargs: {"status": "skipped", "error_code": "WORKFLOW_RUN_QUEUE_FULL"},
    )
    outcome = WorkflowsTool(tmp_path).execute(
        {"action": "run", "workflow_id": "shared", "concurrency_policy": "queue"}
    )
    assert not outcome.ok and json.loads(outcome.output)["error_code"] == "WORKFLOW_RUN_QUEUE_FULL"
