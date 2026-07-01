"""孤儿子代理回收钉子(REFACTORING_BACKLOG"孤儿子代理回收",实锤来源 R6a:
主代理 RUN_EXIT 后后台 dispatch 进程继续运行 21 分钟、写占位符产物)。

钉死四层契约:
1. 进程原语(process_control):真实进程的存活探测与两阶段终止(SIGTERM→SIGKILL)。
2. 回收语义(exit_orphan_recovery):RUNNING 任务终止进程后 requeue 回 PENDING
   (保住 resume 可重派);BLOCKED 等非 RUNNING 状态不动;终态任务完全不碰;
   孙代理子树 BFS 覆盖;同 launch 共享 pid 只杀一次;坏 pid/死 pid 容错。
3. 出口合同接线(final_exit_contract):闸断放行且未收口时,配置开 → 回收并把
   报告进 resume 块;配置关 → 不动进程且文案如实声明后台进程不会停止。
4. pid 落盘(background dispatch):进程确认启动后 pid 必须写进任务
   background_start(cancel_subagents 与孤儿回收的定位事实)。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.exit_orphan_recovery import (
    recover_orphan_subagents,
)
from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
    FinalExitRequest,
    FinalExitState,
    final_exit_closeout_decision,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.process_control import (
    is_pid_alive,
    terminate_pid_with_escalation,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 公共 fixture 工具
# ---------------------------------------------------------------------------


def _spawn_sleeper() -> subprocess.Popen:
    """真实后台进程替身:同 dispatch 形态 start_new_session=True。"""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(proc: subprocess.Popen) -> None:
    """测试兜底:无论断言成败都不留进程。"""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _make_child(
    manager: SubAgentManager,
    *,
    status: str,
    pid: int = 0,
    parent_id: str = "",
    attempt_id: str = "",
    launch_id: str = "launch-test",
):
    task = manager.create_run(
        goal=f"测试子代理 {status}",
        thought="孤儿回收钉子",
        plan=["执行"],
        parent_id=parent_id or None,
        depth=1 if parent_id else 0,
    )
    task.status = status
    if attempt_id:
        task.runner_active_attempt_id = attempt_id
    attrs = dict(task.attributes or {})
    if pid > 0:
        attrs["background_start"] = {
            "launch_id": launch_id,
            "status": "running",
            "pid": pid,
            "updated_at": time.time(),
            "error": "",
        }
    task.attributes = attrs
    manager.save(task)
    return task


def _register_in_task_root(task_root: Path, run_id: str, *, status: str = "RUNNING") -> None:
    """出口合同的第一层名单源:任务工作区 work/agents/<run_id>/canonical_state.json。"""
    agent_dir = task_root / "work" / "agents" / run_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "canonical_state.json").write_text(
        json.dumps({"id": run_id, "status": status, "capability_requests": []}, ensure_ascii=False),
        encoding="utf-8",
    )


def _agent_with_manager(tmp_path: Path, manager: SubAgentManager, **config_overrides) -> SimpleNamespace:
    return SimpleNamespace(
        config=AgentConfig(**config_overrides),
        tools=SimpleNamespace(workspace_root=tmp_path),
        root=tmp_path,
        subagents=manager,
    )


def _params(task_root: Path) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="测试任务",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(task_root / "work"),
            }
        },
        request_id="req-orphan",
        run_id="run-orphan",
        task_id="run-orphan",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )


# ---------------------------------------------------------------------------
# 1. 进程原语(真实进程)
# ---------------------------------------------------------------------------


def test_terminate_escalation_kills_real_process() -> None:
    proc = _spawn_sleeper()
    try:
        assert is_pid_alive(proc.pid) is True
        report = terminate_pid_with_escalation(proc.pid)
        assert report["status"] in {"terminated", "killed"}
        assert is_pid_alive(proc.pid) is False or proc.poll() is not None
    finally:
        _reap(proc)


def test_terminate_handles_dead_and_missing_pid() -> None:
    proc = _spawn_sleeper()
    proc.kill()
    proc.wait(timeout=5)
    assert terminate_pid_with_escalation(proc.pid)["status"] == "not_alive"
    assert terminate_pid_with_escalation(0)["status"] == "no_pid"
    assert is_pid_alive(0) is False


# ---------------------------------------------------------------------------
# 2. 回收语义(真实 manager + 真实进程)
# ---------------------------------------------------------------------------


def test_running_child_is_terminated_and_requeued(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t1"
    proc = _spawn_sleeper()
    try:
        child = _make_child(manager, status="RUNNING", pid=proc.pid, attempt_id="attempt-1")
        _register_in_task_root(task_root, child.id)
        agent = _agent_with_manager(tmp_path, manager)

        report = recover_orphan_subagents(agent, task_root)

        assert proc.poll() is not None or not is_pid_alive(proc.pid), "后台进程必须被终止"
        assert report["requeued_run_ids"] == [child.id]
        assert report["terminated_processes"][0]["pid"] == proc.pid
        assert report["terminated_processes"][0]["run_ids"] == [child.id]
        refreshed = manager.load(child.id)
        assert refreshed.status == "PENDING", "被打断的 RUNNING 任务必须回到可重派状态"
        assert refreshed.runner_active_attempt_id == ""
        recovery = refreshed.attributes["orphan_recovery"]
        assert recovery["requeued"] is True
        assert recovery["previous_status"] == "RUNNING"
        assert recovery["reason"] == "parent_run_unfinished_exit"
        assert refreshed.attributes["background_start"]["status"] == "terminated"
    finally:
        _reap(proc)


def test_blocked_child_keeps_status_with_annotation(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t2"
    child = _make_child(manager, status="BLOCKED")
    _register_in_task_root(task_root, child.id, status="BLOCKED")
    agent = _agent_with_manager(tmp_path, manager)

    report = recover_orphan_subagents(agent, task_root)

    assert report["untouched_run_ids"] == [child.id]
    assert report["requeued_run_ids"] == []
    refreshed = manager.load(child.id)
    assert refreshed.status == "BLOCKED", "BLOCKED 的状态语义与进程无关,不得改动"
    assert refreshed.attributes["orphan_recovery"]["requeued"] is False


def test_dead_pid_running_child_still_requeues(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t3"
    proc = _spawn_sleeper()
    proc.kill()
    proc.wait(timeout=5)
    child = _make_child(manager, status="RUNNING", pid=proc.pid)
    _register_in_task_root(task_root, child.id)
    agent = _agent_with_manager(tmp_path, manager)

    report = recover_orphan_subagents(agent, task_root)

    assert report["terminated_processes"][0]["status"] == "not_alive"
    assert report["requeued_run_ids"] == [child.id]
    assert manager.load(child.id).status == "PENDING"


def test_grandchild_subtree_is_covered(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t4"
    proc = _spawn_sleeper()
    try:
        child = _make_child(manager, status="RUNNING")
        grandchild = _make_child(
            manager, status="RUNNING", pid=proc.pid, parent_id=child.id, launch_id="launch-grand"
        )
        _register_in_task_root(task_root, child.id)  # 孙代理不在第一层名单,靠 BFS
        agent = _agent_with_manager(tmp_path, manager)

        report = recover_orphan_subagents(agent, task_root)

        assert not is_pid_alive(proc.pid), "孙代理的后台进程也必须被回收"
        assert set(report["requeued_run_ids"]) == {child.id, grandchild.id}
        assert manager.load(grandchild.id).status == "PENDING"
    finally:
        _reap(proc)


def test_terminal_child_is_ignored(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t5"
    child = _make_child(manager, status="DONE")
    _register_in_task_root(task_root, child.id, status="DONE")
    agent = _agent_with_manager(tmp_path, manager)

    report = recover_orphan_subagents(agent, task_root)

    assert report["requeued_run_ids"] == []
    assert report["untouched_run_ids"] == []
    refreshed = manager.load(child.id)
    assert refreshed.status == "DONE"
    assert "orphan_recovery" not in (refreshed.attributes or {})


def test_shared_launch_pid_terminated_once(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t6"
    proc = _spawn_sleeper()
    try:
        first = _make_child(manager, status="RUNNING", pid=proc.pid)
        second = _make_child(manager, status="RUNNING", pid=proc.pid)
        _register_in_task_root(task_root, first.id)
        _register_in_task_root(task_root, second.id)
        agent = _agent_with_manager(tmp_path, manager)

        report = recover_orphan_subagents(agent, task_root)

        assert len(report["terminated_processes"]) == 1, "同一 dispatch 进程只杀一次"
        assert set(report["terminated_processes"][0]["run_ids"]) == {first.id, second.id}
        assert set(report["requeued_run_ids"]) == {first.id, second.id}
    finally:
        _reap(proc)


def test_agent_without_manager_returns_empty_report(tmp_path: Path) -> None:
    agent = SimpleNamespace(config=AgentConfig())
    report = recover_orphan_subagents(agent, tmp_path)
    assert report["requeued_run_ids"] == []
    assert report["terminated_processes"] == []


# ---------------------------------------------------------------------------
# 2b. live-pid 豁免(P2 非阻塞出口门 Step3):wake-capable 来源只回收真僵尸,
#     还在后台跑的 live 子代理不杀不 requeue(wake 会叫回主代理续处)。
# ---------------------------------------------------------------------------


def test_exempt_live_pids_leaves_running_child_untouched(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t-exempt"
    proc = _spawn_sleeper()
    try:
        child = _make_child(manager, status="RUNNING", pid=proc.pid, attempt_id="attempt-live")
        _register_in_task_root(task_root, child.id)
        agent = _agent_with_manager(tmp_path, manager)

        report = recover_orphan_subagents(agent, task_root, exempt_live_pids=True)

        assert is_pid_alive(proc.pid), "live 后台派工必须豁免,不杀"
        assert report["exempted_live_run_ids"] == [child.id]
        assert report["requeued_run_ids"] == []
        assert report["terminated_processes"] == []
        refreshed = manager.load(child.id)
        assert refreshed.status == "RUNNING", "豁免的 live 子代理状态不动"
        assert "orphan_recovery" not in (refreshed.attributes or {}), "豁免不留回收痕迹"
    finally:
        _reap(proc)


def test_exempt_live_pids_still_recovers_dead_zombie(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t-zombie"
    proc = _spawn_sleeper()
    proc.kill()
    proc.wait(timeout=5)
    child = _make_child(manager, status="RUNNING", pid=proc.pid, attempt_id="attempt-dead")
    _register_in_task_root(task_root, child.id)
    agent = _agent_with_manager(tmp_path, manager)

    report = recover_orphan_subagents(agent, task_root, exempt_live_pids=True)

    assert report["exempted_live_run_ids"] == []
    assert report["requeued_run_ids"] == [child.id], "死 pid 僵尸即使开豁免也照旧回收"
    assert manager.load(child.id).status == "PENDING"


def test_default_no_exemption_still_kills_live_child(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t-default"
    proc = _spawn_sleeper()
    try:
        child = _make_child(manager, status="RUNNING", pid=proc.pid, attempt_id="attempt-x")
        _register_in_task_root(task_root, child.id)
        agent = _agent_with_manager(tmp_path, manager)

        report = recover_orphan_subagents(agent, task_root)  # 默认 exempt_live_pids=False

        assert not is_pid_alive(proc.pid), "默认不豁免:live 也回收(R6a 原样,cli_run 走这条)"
        assert report["exempted_live_run_ids"] == []
        assert report["requeued_run_ids"] == [child.id]
    finally:
        _reap(proc)


def test_wake_source_exit_recovery_exempts_live_and_kills_zombie(tmp_path: Path) -> None:
    """出口合同接线(Step3):wake-capable 来源【真正走到 _exit_orphan_recovery】时,mixed
    子代理里 live 豁免、死僵尸照旧回收。走到回收的 wake 源 = background_main_agent(叫回轮:
    子代理完成事件/定时唤醒的自发整合轮,照常走交付门→未收口则回收孤儿)。gateway/chat 的
    用户交互轮不在此列——它们经 user_interaction_open_children_passthrough 原文放行、不在轮内
    回收(见 test_gateway_user_turn_passes_through_open_children)。"""
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t-mixed"
    live = _spawn_sleeper()
    dead = _spawn_sleeper()
    dead.kill()
    dead.wait(timeout=5)
    try:
        live_child = _make_child(manager, status="RUNNING", pid=live.pid, attempt_id="a-live")
        dead_child = _make_child(manager, status="RUNNING", pid=dead.pid, attempt_id="a-dead")
        _register_in_task_root(task_root, live_child.id)
        _register_in_task_root(task_root, dead_child.id)
        agent = _agent_with_manager(tmp_path, manager)
        params = replace(_params(task_root), source="background_main_agent")

        decision = _drain_continuations(agent, params, FinalExitState())

        text = str(decision.response.text)
        payload = json.loads(text.split("[RUN_UNFINISHED_EXIT]\n")[1].split("\n[/RUN_UNFINISHED_EXIT]")[0])
        recovery = payload["orphan_recovery"]
        assert live_child.id in recovery["exempted_live_run_ids"], "wake 来源:live 子代理豁免"
        assert dead_child.id in recovery["requeued_run_ids"], "wake 来源:死僵尸照旧回收"
        assert is_pid_alive(live.pid), "豁免的 live 后台进程不被杀"
        assert manager.load(live_child.id).status == "RUNNING"
        assert manager.load(dead_child.id).status == "PENDING"
    finally:
        _reap(live)


def test_gateway_user_turn_passes_through_open_children(tmp_path: Path) -> None:
    """P2 非阻塞(用户侧):gateway 用户交互轮(聊天/查进度)+ 上一轮派的 open 子代理还在 →
    final_exit 经 user_interaction_open_children_passthrough 原文放行(should_continue=False、
    response 为模型原文,不注 [RUN_UNFINISHED_EXIT]/不返工),且【不在本轮回收孤儿】——子代理
    生命周期由叫回轮/supervisor 处置,不拿去拦用户当轮的查进度。与上面的 background_main_agent
    叫回轮回收路径对照。"""
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t-gw"
    live = _spawn_sleeper()
    try:
        live_child = _make_child(manager, status="RUNNING", pid=live.pid, attempt_id="a-gw")
        _register_in_task_root(task_root, live_child.id)
        agent = _agent_with_manager(tmp_path, manager)
        params = replace(_params(task_root), source="gateway")

        decision = final_exit_closeout_decision(
            FinalExitRequest(
                agent, params, SimpleNamespace(text="子代理还在跑,进度如下……", backend="echo"), FinalExitState()
            )
        )

        assert decision.should_continue is False and decision.response is not None
        text = str(decision.response.text)
        assert text == "子代理还在跑,进度如下……", "gateway 查进度轮原文放行,不被改写"
        assert "[RUN_UNFINISHED_EXIT]" not in text and "[MAIN_AGENT_DELIVERY_REWORK_REQUIRED]" not in text
        assert is_pid_alive(live.pid), "放行轮不回收 live 后台进程"
        assert manager.load(live_child.id).status == "RUNNING", "放行轮不改子代理状态"
    finally:
        _reap(live)


def test_cli_run_source_full_recovery_no_exemption(tmp_path: Path) -> None:
    """回归(Step5 ②):cli_run 非 wake 来源出口未收口退出,仍带 [RUN_UNFINISHED_EXIT]
    且 orphan 回收照旧杀 live 后台进程(豁免严格 source-gated,不泄漏到 cli_run/R6a)。"""
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t-cli"
    proc = _spawn_sleeper()
    try:
        child = _make_child(manager, status="RUNNING", pid=proc.pid, attempt_id="attempt-cli")
        _register_in_task_root(task_root, child.id)
        agent = _agent_with_manager(tmp_path, manager)
        params = replace(_params(task_root), source="cli_run")

        decision = _drain_continuations(agent, params, FinalExitState())

        text = str(decision.response.text)
        assert "[RUN_UNFINISHED_EXIT]" in text
        payload = json.loads(text.split("[RUN_UNFINISHED_EXIT]\n")[1].split("\n[/RUN_UNFINISHED_EXIT]")[0])
        recovery = payload["orphan_recovery"]
        assert recovery["exempted_live_run_ids"] == [], "cli_run 不豁免任何 live"
        assert recovery["requeued_run_ids"] == [child.id]
        assert not is_pid_alive(proc.pid), "cli_run 照旧回收 live 后台进程(R6a 原样)"
        assert manager.load(child.id).status == "PENDING"
    finally:
        _reap(proc)


# ---------------------------------------------------------------------------
# 3. 出口合同接线
# ---------------------------------------------------------------------------


def _drain_continuations(agent, params, state) -> object:
    """先耗尽续航(签名不变第二次即放行),拿到闸断放行的最终回复。"""
    first = final_exit_closeout_decision(FinalExitRequest(agent, params, SimpleNamespace(text="收尾", backend="echo"), state))
    assert first.should_continue is True
    return final_exit_closeout_decision(
        FinalExitRequest(agent, params, SimpleNamespace(text="我会持续监控,请稍候。", backend="echo"), state)
    )


def test_unfinished_exit_recovers_orphans_when_enabled(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t-exit"
    proc = _spawn_sleeper()
    try:
        child = _make_child(manager, status="RUNNING", pid=proc.pid, attempt_id="attempt-9")
        _register_in_task_root(task_root, child.id)
        agent = _agent_with_manager(tmp_path, manager)
        params = _params(task_root)

        decision = _drain_continuations(agent, params, FinalExitState())

        assert decision.should_continue is False and decision.response is not None
        text = str(decision.response.text)
        assert "[RUN_UNFINISHED_EXIT]" in text
        payload = json.loads(text.split("[RUN_UNFINISHED_EXIT]\n")[1].split("\n[/RUN_UNFINISHED_EXIT]")[0])
        recovery = payload["orphan_recovery"]
        assert recovery["enabled"] is True
        assert recovery["requeued_run_ids"] == [child.id]
        assert "已按出口合同回收" in text
        assert not is_pid_alive(proc.pid), "出口放行前后台进程必须已被回收"
        assert manager.load(child.id).status == "PENDING"
    finally:
        _reap(proc)


def test_unfinished_exit_keeps_processes_when_disabled(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t-exit-off"
    proc = _spawn_sleeper()
    try:
        child = _make_child(manager, status="RUNNING", pid=proc.pid)
        _register_in_task_root(task_root, child.id)
        agent = _agent_with_manager(tmp_path, manager, run_exit_orphan_recovery_enabled=False)
        params = _params(task_root)

        decision = _drain_continuations(agent, params, FinalExitState())

        assert decision.response is not None
        text = str(decision.response.text)
        payload = json.loads(text.split("[RUN_UNFINISHED_EXIT]\n")[1].split("\n[/RUN_UNFINISHED_EXIT]")[0])
        assert payload["orphan_recovery"] == {"enabled": False}
        assert "不会随本进程退出而停止" in text, "关闭回收时必须如实声明后台进程仍在运行"
        assert is_pid_alive(proc.pid), "回收关闭时不得动进程"
        assert manager.load(child.id).status == "RUNNING"
    finally:
        _reap(proc)


def test_tool_limit_passthrough_recovers_orphans(tmp_path: Path) -> None:
    """R5a 形态钉子:工具轮数耗尽等系统截停出口同样要回收孤儿+带 RUN_UNFINISHED_EXIT。"""
    from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
        unfinished_exit_passthrough,
    )

    manager = SubAgentManager(tmp_path / "subagents")
    task_root = tmp_path / "tasks" / "t-limit"
    proc = _spawn_sleeper()
    try:
        child = _make_child(manager, status="RUNNING", pid=proc.pid)
        _register_in_task_root(task_root, child.id)
        agent = _agent_with_manager(tmp_path, manager)

        response = unfinished_exit_passthrough(
            agent, _params(task_root), SimpleNamespace(text="轮数耗尽总结。", backend="echo")
        )

        text = str(response.text)
        assert "[RUN_UNFINISHED_EXIT]" in text
        assert "轮数耗尽总结。" in text, "模型原文必须保留"
        assert not is_pid_alive(proc.pid)
        assert manager.load(child.id).status == "PENDING"
    finally:
        _reap(proc)


def test_tool_limit_passthrough_leaves_plain_response_untouched(tmp_path: Path) -> None:
    from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
        unfinished_exit_passthrough,
    )

    agent = _agent_with_manager(tmp_path, SubAgentManager(tmp_path / "subagents"))
    original = SimpleNamespace(text="纯问答总结。", backend="echo")
    params = _params(tmp_path / "tasks" / "t-plain")  # task_root 无任何子代理

    assert unfinished_exit_passthrough(agent, params, original) is original


# ---------------------------------------------------------------------------
# 4. pid 落盘(background dispatch)
# ---------------------------------------------------------------------------


def test_background_dispatch_marks_pid_into_task(monkeypatch, tmp_path: Path) -> None:
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    manager = SubAgentManager(tmp_path / "subagents")
    child = _make_child(manager, status="PLANNING")
    agent = SimpleNamespace(config=SimpleNamespace(model_backend="minimax"), subagents=manager)
    monkeypatch.setattr(
        background_dispatch,
        "_auto_start_dispatch_args",
        lambda _agent, _run_ids, background_launch_id="": (object(), object(), object()),
    )
    monkeypatch.setattr(
        background_dispatch,
        "_spawn_background_dispatch_process",
        lambda _agent, _request: SimpleNamespace(pid=54321),
    )
    monkeypatch.setattr(background_dispatch, "_safe_agent_tree", lambda _agent: {"schema_version": "tree.v1"})

    result = background_dispatch._start_background_dispatch(agent, [child.id])

    assert result["status"] == "started"
    background = manager.load(child.id).attributes["background_start"]
    assert background["pid"] == 54321, "进程确认启动后 pid 必须落盘(孤儿回收/cancel 的定位事实)"
    assert background["status"] == "running"


def test_mark_background_start_preserves_existing_pid(tmp_path: Path) -> None:
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    manager = SubAgentManager(tmp_path / "subagents")
    child = _make_child(manager, status="PLANNING", pid=777)
    agent = SimpleNamespace(subagents=manager)
    request = background_dispatch._BackgroundDispatchRequest(
        agent=agent,
        run_ids=[child.id],
        launch_id="launch-test",
        router=object(),
        cfg=object(),
        params=object(),
    )

    errors = background_dispatch.mark_background_start(request, status="failed", error="boom")

    assert errors == []
    background = manager.load(child.id).attributes["background_start"]
    assert background["pid"] == 777, "后续无 pid 的 mark 不得抹掉已落盘 pid"
    assert background["status"] == "failed"


def test_cli_mark_background_launch_preserves_pid(tmp_path: Path) -> None:
    """R7a 实锤钉子:CLI dispatch 进程的 running/finished 标记曾手写 dict 抹掉
    主代理落盘的 pid,孤儿回收进程层因此失效(terminated_processes=[])。"""
    from agent_py_agent.cli.dispatch_background import (
        BackgroundLaunchUpdate,
        mark_background_launch,
    )

    manager = SubAgentManager(tmp_path / "subagents")
    child = _make_child(manager, status="PLANNING", pid=888, launch_id="launch-cli")
    agent = SimpleNamespace(subagents=manager)
    options = SimpleNamespace(background_launch_id="launch-cli", run_ids=[child.id])

    for status in ("running", "finished"):
        report = mark_background_launch(agent, options, BackgroundLaunchUpdate(status))
        assert report.ok
        background = manager.load(child.id).attributes["background_start"]
        assert background["pid"] == 888, f"CLI mark({status}) 不得抹掉已落盘 pid"
        assert background["status"] == status
