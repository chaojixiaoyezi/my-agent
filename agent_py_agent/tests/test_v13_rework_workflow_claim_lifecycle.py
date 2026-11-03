"""R1 原生工作流准入与清理生命周期回归。"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.business_api import (
    BusinessServices,
    _BusinessBackgroundService,
    bind_business_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.workflows import concurrency_policy as policy
from agent_py_agent.agent.workflows import run_store as run_records
from agent_py_agent.agent.workflows.agent_steps import WorkflowAgentSteps
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.tests.test_rework_r8_business_wire_contract import (
    MAIN,
    thin_client,
)
from agent_py_agent.tests.test_rework_r8_business_wire_contract import (
    gateway as gateway,
)


def _definition(*, delegated=True):
    steps = [{"step_id": "prefix", "name": "原前缀", "mode": "llm_skill_analysis"}]
    if delegated:
        steps.append({"step_id": "agent", "name": "原任务", "mode": "agent_task",
                      "config": {"instruction": "核对原工作流输入"}})
    return {"descriptor": {"id": "lifecycle-flow", "kind": "workflow", "name": "准入生命周期",
                           "access_targets": [], "data_sources": [], "credential_refs": [], "io_contract": {}},
            "steps": steps}


@pytest.fixture
def native(tmp_path):
    """采用正式后台提交服务、规范任务/引导 Store 和真实 Engine，不调用模型。"""
    home = replace(home_paths(tmp_path / "home"), owner_id="alice", owner_provider="local", owner_kind="main")
    store = ConversationStore(home.owner_home_dir / "conversations")
    source = store.threads.create(owner_id="alice", canonical_user_id="source-user")
    actor = HostChannelActor("alice", source.thread_id, "original-request", "local", "local", "user")
    host = SimpleNamespace(home_paths=home, conversation_store=store,
                           config=AgentConfig(business_spaces_enabled=True, model_backend="echo"))
    services = BusinessServices(host)
    authority = bind_business_context(host, actor=actor, authorize=lambda *args: True)
    background = _BusinessBackgroundService(services, authority)
    space = services.spaces.create({"name": "原工作流空间"}, actor=services.actor(authority), idempotency_key="space")
    context = BusinessExecutionContext("alice", space["space_id"], None, "original-workflow-task",
                                       space["revision"], None, (), (), "business-context.v1")
    calls = []
    agents = WorkflowAgentSteps(background, actor_provider=lambda run: actor,
                               completion_reader=lambda *args, **kwargs: None,
                               cancel_handler=lambda task_id, **kwargs: background.cancel_task(
                                   task_id, actor=kwargs["actor"], include_descendants=True))
    engine = WorkflowEngine(home.owner_home_dir, agent_steps=agents, run_authorizer=lambda *args: None,
                            analysis_handler=lambda spec, *args: calls.append(spec["step_id"]) or {"value": 7})
    assert Path(policy.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    return SimpleNamespace(home=home.owner_home_dir, store=store, engine=engine, agents=agents, background=background,
                           actor=actor, context=context, calls=calls)


# LLM: 仅沿原引擎和可信fixture上下文调用，策略/输入显式传入，不跳过原运行保存。
# 函数用途: 从原生领域入口发起待测工作流。
def _start(case, run_id, *, delegated=True, strategy="skip_if_running", inputs=None):
    return case.engine.run_workflow(_definition(delegated=delegated), inputs, owner_id="alice",
                                    business_context=case.context, workflow_run_id=run_id,
                                    concurrency_policy=strategy)


def test_restricted_run_blocks_prefix_while_prior_agent_task_is_accepted(native):
    first = _start(native, "original")
    task = native.store.session_tasks.require(first.steps[-1].escalation_result["task_id"])
    assert first.status == "waiting_for_agent" and task.status in ("queued", "accepted")
    assert native.store.guidance.receipt(task.body_dedupe_key) is not None
    second = _start(native, "different")
    assert isinstance(second, dict) and second["status"] == "skipped"
    assert second["running_run_id"] == first.workflow_run_id
    assert native.calls == ["prefix"]
    rows, errors = native.store.session_tasks.list_report(limit=0)
    assert not errors and [row.task_id for row in rows] == [task.task_id]


def test_single_release_error_must_not_leave_live_process_claim(native, monkeypatch):
    original = policy._unlink_same
    errors = []

    def remove(parent, name, expected):
        if name == "claim.json" and not errors:
            errors.append(name)
            raise OSError(errno.EIO, "one claim removal error")
        return original(parent, name, expected)

    monkeypatch.setattr(policy, "_unlink_same", remove)
    returned = None
    try:
        returned = _start(native, "finished", delegated=False)
    except OSError:
        pass
    assert native.engine.store.load_run("finished").status == "succeeded"
    following = _start(native, "following", delegated=False)
    assert not isinstance(following, dict), "settled claim remained held after normal I/O resumed"
    assert following.status == "succeeded"
    assert returned is not None and returned.workflow_run_id == "finished"
    assert errors == ["claim.json"] and native.calls == ["prefix", "prefix"]


def test_accepted_queued_run_not_abandoned_after_one_poll_error(native, monkeypatch):
    entered, release, read_error, queued_finished = (threading.Event() for _ in range(4))
    original_queue = policy._queue
    order = []

    def queue(scope, parent):
        if threading.current_thread().name.startswith("workflow-queue-") and not read_error.is_set():
            read_error.set()
            raise OSError(errno.EIO, "one queue read error")
        return original_queue(scope, parent)

    def analyze(spec, context, skills):
        order.append(context.run_inputs["label"])
        if context.run_inputs["label"] == "first":
            entered.set()
            assert release.wait(10)
        else:
            queued_finished.set()
        return {"value": 7}

    native.engine.analysis_handler = analyze
    monkeypatch.setattr(policy, "_queue", queue)
    with ThreadPoolExecutor(1) as pool:
        holder = pool.submit(native.engine.run_workflow, _definition(delegated=False), {"label": "first"},
                             workflow_run_id="holder", concurrency_policy="queue")
        try:
            assert entered.wait(5)
            receipt = native.engine.run_workflow(_definition(delegated=False), {"label": "queued"},
                                                 workflow_run_id="accepted", concurrency_policy="queue")
            assert receipt["status"] == "queued" and read_error.wait(5)
            release.set()
            assert holder.result(5).status == "succeeded"
            assert queued_finished.wait(3), "accepted original workflow lost its queue worker"
            with native.engine.store._lock("accepted"):
                assert native.engine.store.load_run("accepted").status == "succeeded"
            assert order == ["first", "queued"]
        finally:
            release.set()


# LLM: 直接观察当前进程FD；调用方须先热身并等待真实worker/子进程退出。
# 函数用途: 统计真实打开的文件描述符。
def _fd_count():
    return len(os.listdir("/proc/self/fd" if sys.platform == "linux" else "/dev/fd"))


# LLM: 仅准确run首次executing写入故障；发表后错误仍执行原写叶，绝不伪造成功回执。
# 函数用途: 在原JSON写路径注入一次绑定前或绑定后IO错误。
def _one_bind_error(monkeypatch, run_id, *, published):
    write = run_records._write_json
    errors = []

    def save(root, parts, value):
        proof = value.get("concurrency_claim", {}) if isinstance(value, dict) else {}
        if parts[-2:] == (run_id, "run.json") and proof.get("phase") == "executing" and not errors:
            errors.append(proof["record"]["nonce"])
            if published:
                write(root, parts, value)
            raise OSError(errno.EIO, "one binding write error")
        return write(root, parts, value)

    monkeypatch.setattr(run_records, "_write_json", save)
    return errors


# LLM: 比较热身后的真实FD与规范run，不把捕获异常或返回字典当作新工作已执行。
# 函数用途: 验证一次直接绑定错误后的同资产真实准入。
@pytest.mark.parametrize("published", [False, True], ids=["before-publication", "after-publication"])
def test_binding_write_error_releases_exact_unstarted_claim(native, monkeypatch, published):
    assert _start(native, "warm", delegated=False).status == "succeeded"
    errors = _one_bind_error(monkeypatch, "binding-error", published=published)
    before = _fd_count()
    with pytest.raises(OSError, match="one binding write error"):
        _start(native, "binding-error", delegated=False)
    try:
        following = _start(native, "following-binding-error", delegated=False)
    except Exception as exc:
        following = exc
    after = _fd_count()
    print(f"binding published={published} fd_before={before} fd_after={after} fd_delta={after - before}")
    assert after - before == 0
    assert len(errors) == 1
    assert not isinstance(following, (dict, Exception)), f"unstarted claim prevented admission: {following!r}"
    assert following.status == "succeeded" and native.calls == ["prefix", "prefix"]
    assert native.engine.store.load_run("binding-error").status == "queued"
    proof = native.engine.store._read("binding-error", "run.json")["concurrency_claim"]
    assert proof["phase"] == "released" and proof["record"]["nonce"] == errors[0]


# LLM: 原票据、真实worker和分析回调计数同验，失败不得靠移除票据或新建替代任务转绿。
# 函数用途: 验证排队绑定故障的恢复、FIFO与同范围最大并发一。
@pytest.mark.parametrize("published", [False, True], ids=["before-publication", "after-publication"])
def test_queued_binding_error_retries_original_ticket_without_overlap(native, monkeypatch, published):
    assert _start(native, "warm-queue", delegated=False).status == "succeeded"
    entered, release, queued_finished = (threading.Event() for _ in range(3))
    errors = _one_bind_error(monkeypatch, "queued-bind", published=published)
    active, maximum, order = 0, 0, []
    lock = threading.Lock()

    def analyze(spec, context, skills):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            order.append(context.run_inputs["label"])
        try:
            if context.run_inputs["label"] == "holder":
                entered.set()
                assert release.wait(10)
            else:
                queued_finished.set()
            return {"value": 7}
        finally:
            with lock:
                active -= 1

    native.engine.analysis_handler = analyze
    before = _fd_count()
    with ThreadPoolExecutor(1) as pool:
        holder = pool.submit(_start, native, "queue-holder", delegated=False, strategy="queue", inputs={"label": "holder"})
        try:
            assert entered.wait(5)
            receipt = _start(native, "queued-bind", delegated=False, strategy="queue", inputs={"label": "queued"})
            assert receipt["status"] == "queued"
            scope = policy._scope(native.home, "workflow", "lifecycle-flow", "queued-bind")
            with policy._gate(scope) as parent:
                ticket = policy._queue(scope, parent)[0]
            worker = next(item for item in threading.enumerate() if item.name == "workflow-queue-" + ticket["nonce"])
            release.set()
            assert holder.result(5).status == "succeeded"
            assert queued_finished.wait(3), "binding error stranded an already accepted queue ticket"
            worker.join(5)
            assert not worker.is_alive()
            assert native.engine.store.load_run("queued-bind").status == "succeeded"
            assert native.engine.store.load_run("queue-holder").status == "succeeded"
            assert order == ["holder", "queued"] and maximum == 1 and active == 0 and len(errors) == 1
        finally:
            release.set()
    after = _fd_count()
    print(f"queued-binding published={published} maximum={maximum} fd_before={before} fd_after={after} fd_delta={after - before}")
    assert after - before == 0


# LLM: 使用原引擎与原保存链；成功退出严格位于succeeded提交后、摘要正文前，不补运行状态。
# 函数用途: 在真实新进程执行成功退出、暂停退出或原运行恢复。
def _terminal_process(home, mode):
    """退出点只在规范运行提交后的摘要入口；成功用例仍严格核对succeeded。"""
    home = Path(home)
    effects = home / "observed-effects.jsonl"

    def analyze(spec, *args):
        with effects.open("a", encoding="utf-8") as stream:
            stream.write((mode if mode in ("exit", "following") else mode + ":" + spec["step_id"]) + "\n")
        if mode == "pause-exit" and spec["step_id"] == "pause":
            return {"value": 7, "_pause_for_resume": True}
        return {"value": 7}

    engine = WorkflowEngine(home, analysis_handler=analyze)
    if mode in ("exit", "pause-exit"):
        def checkpoint(record):
            expected = "succeeded" if mode == "exit" else "paused_for_resume"
            assert engine.store.load_run(record.workflow_run_id).status == expected
            os._exit(73)
        engine._record_summary = checkpoint
    before = engine.store.load_run("exited-run").status if mode in ("following", "resume") else None
    if mode == "resume":
        result = engine.resume_workflow_run("exited-run")
        proof = engine.store._read("exited-run", "run.json")["concurrency_claim"]
        if before == "paused_for_resume":
            assert proof["callback_identity"] == policy._self_identity()
            assert proof["callback_identity"] != proof["record"]["identity"]
    else:
        definition = _definition(delegated=False)
        if mode == "pause-exit":
            definition["steps"].append({"step_id": "pause", "name": "原暂停点", "mode": "llm_skill_analysis"})
        result = engine.run_workflow(definition, owner_id="alice", workflow_run_id=(
            "exited-run" if mode in ("exit", "pause-exit") else "fresh-following"), concurrency_policy="skip_if_running")
    print(json.dumps({"result_status": result.get("status") if isinstance(result, dict) else result.status,
                      "old_before": before, "old_after": engine.store.load_run("exited-run").status}))


# LLM: 子进程继承隔离HOME/TMPDIR/PYTHONPATH，沿同一工作树import，等待准确返回码。
# 函数用途: 启动并回收真实独立引擎进程。
def _run_terminal_process(home, mode):
    return subprocess.run([sys.executable, "-c", (
        "import sys; from agent_py_agent.tests.test_v13_rework_workflow_claim_lifecycle "
        "import _terminal_process; _terminal_process(sys.argv[1], sys.argv[2])"), str(home), mode],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=15)


# LLM: 不手改claim/状态/PID；实际os._exit后另起进程完成下一次同资产工作。
# 函数用途: 验证成功提交后崩溃的自动回收及原步骤不重跑。
def test_real_exit_after_succeeded_commit_allows_fresh_process_admission(native):
    # 先热身真实执行、进程启动和FD观测，避免把一次性库初始化算成泄漏。
    assert _start(native, "warm-exit", delegated=False).status == "succeeded"
    warm = subprocess.run([sys.executable, "-c", "pass"], capture_output=True, timeout=10)
    assert warm.returncode == 0
    before = _fd_count()
    exited = _run_terminal_process(native.home, "exit")
    assert exited.returncode == 73, exited.stderr
    original = native.engine.store.load_run("exited-run")
    assert original.status == "succeeded"
    proof = native.engine.store._read("exited-run", "run.json")["concurrency_claim"]
    assert proof["phase"] == "executing"
    assert policy._identity_live(proof["record"]["identity"]) is False
    following = _run_terminal_process(native.home, "following")
    assert following.returncode == 0, following.stderr
    observed = json.loads(following.stdout)
    after = _fd_count()
    print(f"real-exit fd_before={before} fd_after={after} fd_delta={after - before}")
    assert after - before == 0
    assert observed == {"result_status": "succeeded", "old_before": "succeeded", "old_after": "succeeded"}
    assert native.engine.store.load_run("exited-run").steps == original.steps
    assert native.engine.store._read("exited-run", "run.json")["concurrency_claim"]["phase"] == "released"
    assert (native.home / "observed-effects.jsonl").read_text().splitlines() == ["exit", "following"]


# LLM: 原委派任务保持同一ID和原前缀，只有真实取消结清后才准入不同运行。
# 函数用途: 验证原retained运行重绑故障恢复、原任务恢复与规范结算。
@pytest.mark.parametrize("published", [False, True], ids=["before-publication", "after-publication"])
def test_failed_retained_rebind_preserves_obligation_and_original_resume(native, monkeypatch, published):
    original = _start(native, "retained-original")
    task_id = original.steps[-1].escalation_result["task_id"]
    errors = _one_bind_error(monkeypatch, original.workflow_run_id, published=published)
    with pytest.raises(OSError, match="one binding write error"):
        native.engine.resume_workflow_run(original.workflow_run_id)
    resumed = native.engine.resume_workflow_run(original.workflow_run_id)
    assert not isinstance(resumed, dict), f"original retained run lost resume ownership: {resumed!r}"
    assert resumed.status == "waiting_for_agent" and native.calls == ["prefix"]
    assert resumed.steps[-1].escalation_result["task_id"] == task_id and len(errors) == 1
    rows, failures = native.store.session_tasks.list_report(limit=0)
    assert failures == [] and [row.task_id for row in rows] == [task_id]
    blocked = _start(native, "blocked-by-original", delegated=False)
    assert blocked["status"] == "skipped" and blocked["running_run_id"] == original.workflow_run_id
    proof = native.engine.store._read(original.workflow_run_id, "run.json")["concurrency_claim"]
    assert proof["phase"] == "retained" and proof["callback_identity"] == policy._self_identity()
    native.engine.cancel_workflow_run(resumed.workflow_run_id,
        expected_revision=resumed.revision, operation_key="settle-original")
    assert native.store.session_tasks.require(task_id).status == "cancelled"
    assert native.engine.store.load_run(resumed.workflow_run_id).status == "cancelled"
    assert _start(native, "after-original-settlement", delegated=False).status == "succeeded"
    assert native.calls == ["prefix", "prefix"]
    assert native.engine.store._read("retained-original", "run.json")["concurrency_claim"]["phase"] == "released"


# LLM: 非终态死亡回调只能转retained；原快照恢复须换当前callback身份且不重跑已完成前缀。
# 函数用途: 验证真实进程退出后的原运行恢复和重复恢复没有重复副作用。
def test_exited_paused_callback_retains_scope_and_resumes_original_snapshot(native):
    assert _start(native, "warm-resume", delegated=False).status == "succeeded"
    before = _fd_count()
    exited = _run_terminal_process(native.home, "pause-exit")
    assert exited.returncode == 73, exited.stderr
    original = native.engine.store.load_run("exited-run")
    assert original.status == "paused_for_resume"
    blocked = _start(native, "blocked-by-paused-original", delegated=False)
    assert blocked["status"] == "skipped" and blocked["running_run_id"] == "exited-run"
    assert native.calls == ["prefix"]
    proof = native.engine.store._read("exited-run", "run.json")["concurrency_claim"]
    assert proof["phase"] == "retained"
    resumed = _run_terminal_process(native.home, "resume")
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout) == {"result_status": "succeeded", "old_before": "paused_for_resume", "old_after": "succeeded"}
    effects = (native.home / "observed-effects.jsonl").read_text().splitlines()
    assert effects == ["pause-exit:prefix", "pause-exit:pause", "resume:pause"]
    replay = _run_terminal_process(native.home, "resume")
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout) == {"result_status": "succeeded", "old_before": "succeeded", "old_after": "succeeded"}
    assert (native.home / "observed-effects.jsonl").read_text().splitlines() == effects
    assert native.engine.store.load_run("exited-run").steps[0] == original.steps[0]
    after = _fd_count()
    print(f"original-process-resume fd_before={before} fd_after={after} fd_delta={after - before}")
    assert after == before


# LLM: 阻塞点是规范成功提交后的真实摘要入口；后续准入不得等待或把业务终态当作回调退出。
# 函数用途: 验证活回调、不同资产和不同Owner的真实并发边界。
def test_live_terminal_callback_stays_occupied_without_blocking_other_admissions(native):
    assert _start(native, "warm-live", delegated=False).status == "succeeded"
    entered, release = threading.Event(), threading.Event()
    original_summary = native.engine._record_summary

    def summarize(record):
        if record.workflow_run_id == "live-terminal":
            assert native.engine.store.load_run(record.workflow_run_id).status == "succeeded"
            entered.set()
            assert release.wait(10)
        return original_summary(record)

    native.engine._record_summary = summarize
    before = _fd_count()
    with ThreadPoolExecutor(2) as pool:
        holder = pool.submit(_start, native, "live-terminal", delegated=False)
        try:
            assert entered.wait(5)
            blocked = pool.submit(_start, native, "live-blocked", delegated=False).result(1)
            assert not holder.done() and blocked["status"] == "skipped"
            assert blocked["running_run_id"] == "live-terminal"
            assert native.calls == ["prefix", "prefix"]
            other = _definition(delegated=False)
            other["descriptor"]["id"] = "other-asset"
            assert native.engine.run_workflow(other, owner_id="alice", concurrency_policy="skip_if_running").status == "succeeded"
            foreign = WorkflowEngine(native.home.parent / "foreign-owner", analysis_handler=lambda *args: {"value": 7})
            assert foreign.run_workflow(_definition(delegated=False), owner_id="bob",
                                        concurrency_policy="skip_if_running").status == "succeeded"
            assert native.engine.store.load_run("live-terminal").status == "succeeded"
        finally:
            release.set()
        assert holder.result(5).status == "succeeded"
    assert _start(native, "after-live-terminal", delegated=False).status == "succeeded"
    after = _fd_count()
    print(f"live-terminal fd_before={before} fd_after={after} fd_delta={after - before}")
    assert after == before


# LLM: 使用真实死亡进程，只在该PID的signal-0探测注入一次EPERM观察类型，不伪造退出证明。
# 函数用途: 验证身份未知时继续占位，恢复可靠观察后才自动结清。
def test_unknown_process_identity_cannot_release_even_a_succeeded_run(native, monkeypatch):
    assert _start(native, "warm-unknown", delegated=False).status == "succeeded"
    exited = _run_terminal_process(native.home, "exit")
    assert exited.returncode == 73, exited.stderr
    proof = native.engine.store._read("exited-run", "run.json")["concurrency_claim"]
    pid = proof["callback_identity"]["pid"]
    kill = policy.os.kill
    observations = []

    def probe(target, signal):
        if target == pid and signal == 0:
            observations.append(target)
            raise PermissionError(errno.EPERM, "process identity is not observable")
        return kill(target, signal)

    before = _fd_count()
    with monkeypatch.context() as fault:
        fault.setattr(policy.os, "kill", probe)
        blocked = _start(native, "unknown-blocked", delegated=False)
        assert blocked["status"] == "skipped" and observations
        assert native.engine.store._read("exited-run", "run.json")["concurrency_claim"] == proof
        assert native.calls == ["prefix"]
    assert _start(native, "known-following", delegated=False).status == "succeeded"
    assert native.engine.store.load_run("exited-run").status == "succeeded"
    after = _fd_count()
    print(f"unknown-identity fd_before={before} fd_after={after} fd_delta={after - before}")
    assert after == before


# LLM: 两代租约都来自真实_create_claim；迟到/错作用域清理不能触碰新代或改写已确认的旧证明。
# 函数用途: 验证清理的完整记录、inode、资产与Owner边界。
@pytest.mark.parametrize("change", ["stale", "inode", "asset", "owner"])
def test_stale_or_foreign_binding_cleanup_does_not_touch_another_generation(native, monkeypatch, change):
    created = []
    create = policy._create_claim

    def capture(*args):
        lease = create(*args)
        created.append(lease)
        return lease

    monkeypatch.setattr(policy, "_create_claim", capture)
    assert _start(native, "old-generation", delegated=False).status == "succeeded"
    old = created[0]
    original_proof = native.engine.store._read("old-generation", "run.json")
    entered, release = threading.Event(), threading.Event()

    def analyze(*args):
        entered.set()
        assert release.wait(10)
        return {"value": 7}

    native.engine.analysis_handler = analyze
    before = _fd_count()
    with ThreadPoolExecutor(1) as pool:
        holder = pool.submit(_start, native, "new-generation", delegated=False)
        try:
            assert entered.wait(5)
            current = created[-1]
            scope = policy._scope(native.home, "workflow", "lifecycle-flow", "old-generation")
            if change == "inode":
                old = replace(old, stamp=(old.stamp[0], old.stamp[1] + 1))
            elif change == "asset":
                old = replace(old, record={**old.record, "asset_id": "other-asset"})
            elif change == "owner":
                foreign_home = native.home.parent / "foreign-owner"
                foreign_home.mkdir(parents=True, exist_ok=True)
                scope = policy._scope(foreign_home, "workflow", "lifecycle-flow", "old-generation")
            with policy._gate(scope) as parent:
                policy._abort_binding(scope, parent, old)
            actual_scope = policy._scope(native.home, "workflow", "lifecycle-flow", "new-generation")
            with policy._gate(actual_scope) as parent:
                assert policy._claim(actual_scope, parent) == current
            assert native.engine.store._read("old-generation", "run.json") == original_proof
            assert not holder.done()
        finally:
            release.set()
        assert holder.result(5).status == "succeeded"
    assert _start(native, "after-generations", delegated=False).status == "succeeded"
    after = _fd_count()
    print(f"generation-control={change} fd_before={before} fd_after={after} fd_delta={after - before}")
    assert after == before


# LLM: 终态通过真实引擎成功、失败和原生任务取消形成，不预写终态；迟到保存不得倒退。
# 函数用途: 核对三类规范终态和步骤写入的单调性。
@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_terminal_run_and_step_states_do_not_regress(native, terminal):
    if terminal == "cancelled":
        original = _start(native, "monotonic")
        native.engine.cancel_workflow_run(original.workflow_run_id,
            expected_revision=original.revision, operation_key="original-cancel")
        record = native.engine.store.load_run(original.workflow_run_id)
    else:
        if terminal == "failed":
            def fail(*args):
                raise RuntimeError("real analysis callback failure")
            native.engine.analysis_handler = fail
        record = _start(native, "monotonic", delegated=False)
    assert record.status == terminal
    with pytest.raises(ValueError):
        native.engine.store.save_run(replace(record, status="running"))
    with pytest.raises(ValueError):
        native.engine.store.save_step_result(record.workflow_run_id, replace(record.steps[-1], status="running"))
    assert native.engine.store.load_run(record.workflow_run_id).status == terminal


# LLM: 只注入一次真实unlink错误，逐事件观察原run的release_pending到同代unlink再到ack。
# 函数用途: 证明清理凭据在确认前持久保留，且原生清理重试不泄漏FD。
def test_release_proof_remains_durable_until_exact_unlink_is_acknowledged(native, monkeypatch):
    assert _start(native, "warm-proof", delegated=False).status == "succeeded"
    scope = policy._scope(native.home, "workflow", "lifecycle-flow", "proof-run")
    unlink, acknowledge = policy._unlink_same, run_records.WorkflowRunStore._ack_concurrency_release
    events = []

    def remove(parent, name, expected):
        current = policy._claim(scope, parent) if name == "claim.json" else None
        if current is None or current.record["run_id"] != "proof-run":
            return unlink(parent, name, expected)
        assert native.engine.store._read("proof-run", "run.json")["concurrency_claim"]["phase"] == "release_pending"
        if not events:
            events.append("unlink-error")
            raise OSError(errno.EIO, "one unlink observation error")
        removed = unlink(parent, name, expected)
        assert removed and policy._claim(scope, parent) is None
        assert native.engine.store._read("proof-run", "run.json")["concurrency_claim"]["phase"] == "release_pending"
        events.append("unlinked")
        return removed

    def ack(store, lease):
        if lease.record["run_id"] != "proof-run":
            return acknowledge(store, lease)
        assert events == ["unlink-error", "unlinked"]
        assert native.engine.store._read("proof-run", "run.json")["concurrency_claim"]["phase"] == "release_pending"
        events.append("before-ack")
        result = acknowledge(store, lease)
        assert native.engine.store._read("proof-run", "run.json")["concurrency_claim"]["phase"] == "released"
        events.append("acknowledged")
        return result

    monkeypatch.setattr(policy, "_unlink_same", remove)
    monkeypatch.setattr(run_records.WorkflowRunStore, "_ack_concurrency_release", ack)
    before = _fd_count()
    assert _start(native, "proof-run", delegated=False).status == "succeeded"
    pending = native.engine.store._read("proof-run", "run.json")["concurrency_claim"]
    assert pending["phase"] == "release_pending" and events == ["unlink-error"]
    assert _start(native, "after-proof", delegated=False).status == "succeeded"
    confirmed = native.engine.store._read("proof-run", "run.json")["concurrency_claim"]
    assert confirmed == {**pending, "phase": "released"}
    assert events == ["unlink-error", "unlinked", "before-ack", "acknowledged"]
    after = _fd_count()
    print(f"durable-release fd_before={before} fd_after={after} fd_delta={after - before}")
    assert after == before


# LLM: 真实原生Gateway/客户端/空间授权/Engine只替换分析叶；绑定写入错须从HTTP到规范占位闭环。
# 函数用途: 核对公开入口的身份、错误信封、后续成功、幂等和原运行持久化。
@pytest.mark.parametrize("published", [False, True], ids=["before-publication", "after-publication"])
def test_native_http_binding_error_preserves_identity_envelope_and_persistence(gateway, monkeypatch, published):
    from agent_py_agent.agent.conversation.business_context import resolve_business_context
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli.chat_client_context import post_gateway_json
    from agent_py_agent.cli.space_commands import business_client_for_agent

    calls = []

    def engine_factory(owner_home, **options):
        engine = WorkflowEngine(owner_home, analysis_handler=lambda spec, *args: calls.append(spec["step_id"]) or {"value": 7},
                                **options)
        engine.default_concurrency_policy = "skip_if_running"
        return engine

    def context_provider(actor, request):
        return resolve_business_context(actor=actor, task_id="native-workflow-source",
            requested_space_id=request.get("space_id"), project_ref=None, binding_id=None)

    gateway.agent.workflow_engine_factory = engine_factory
    gateway.agent.business_context_provider = context_provider
    client = business_client_for_agent(thin_client(gateway, MAIN))
    space = client.call("spaces.create", {"name": "真实准入线缆空间"})
    assert (space["http_status"], space["state"], space["error"]) == (200, "completed", None)
    space_id = space["data"]["space_id"]
    created = client.call("workflows.create", {"definition": _definition(delegated=False),
        "scope": {"scope_mode": "spaces", "space_ids": [space_id]}})
    assert (created["http_status"], created["state"], created["error"]) == (200, "completed", None)
    request = {"workflow_ref": "lifecycle-flow", "space_id": space_id}
    warm = client.send(client.prepare("workflows.run", request, idempotency_key="r1-wire-warm-admission"))
    assert (warm["http_status"], warm["state"], warm["error"]) == (200, "completed", None), warm
    assert warm["data"]["state"] == "completed"
    [services] = [item for item in gateway.server._business_services.values()
                  if item.owner_id == gateway.agent.home_paths.owner_id]
    store = run_records.WorkflowRunStore(services.owner_home)
    key = "r1-wire-binding-error"
    run_id = hashlib.sha256((services.owner_id + "\0workflows.run\0" + key).encode()).hexdigest()
    errors = _one_bind_error(monkeypatch, run_id, published=published)
    failed = client.send(client.prepare("workflows.run", request, idempotency_key=key))
    assert (failed["http_status"], failed["state"], failed["effect_outcome"]) == (503, "failed", "unknown")
    assert failed["error"]["code"] == "OUTCOME_UNKNOWN" and failed["error"]["retryable"] is True
    assert store.load_run(run_id).status == "queued" and calls == ["prefix"] and len(errors) == 1
    original = store._read(run_id, "run.json")
    assert original["owner_id"] == services.owner_id and original["business_context"]["space_id"] == space_id
    assert original["concurrency_claim"]["phase"] == "released"
    following_request = client.prepare("workflows.run", request, idempotency_key="r1-wire-following-admission")
    following = client.send(following_request)
    assert (following["http_status"], following["state"], following["effect_outcome"]) == (200, "completed", "committed")
    assert following["error"] is None and following["data"]["state"] == "completed"
    following_id = following["data"]["workflow_run_id"]
    assert store.load_run(following_id).status == "succeeded" and calls == ["prefix", "prefix"]
    assert client.send(following_request)["data"] == following["data"] and calls == ["prefix", "prefix"]
    assert store._read(run_id, "run.json") == original
    forged_status, forged = post_gateway_json(thin_client(gateway, MAIN), "/client/business", {
        "action": "workflows.run", "request_id": "wire-forged-owner", "idempotency_key": "wire-forged-owner",
        "params": {**request, "owner_id": "another-owner"}}, timeout=10)
    assert (forged_status, forged["state"], forged["error"]["code"], forged["effect_outcome"]) == (
        400, "failed", "INVALID_SCHEMA", "not_started")
    bob = business_client_for_agent(thin_client(gateway, OwnerIdentity.provider_user("feishu", "business-bob")))
    hidden = bob.call("workflow_runs.list", {})
    assert (hidden["http_status"], hidden["state"], hidden["error"], hidden["data"]["items"]) == (200, "completed", None, [])
    assert store.load_run(following_id).owner_id == services.owner_id and calls == ["prefix", "prefix"]
