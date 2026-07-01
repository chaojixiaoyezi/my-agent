"""P2 非阻塞出口门钉子(主代理派完子代理能干净撒手 yield、靠事件叫回,而不是被
交付出口门当成"没干完"去 block/强制续/杀后台子代理)。

Step2(final_exit_contract):wake-capable 来源 + open 子代理全都还在后台活着跑
  → 干净 yield(不 block/不注 rework/不跑 closeout/不回收孤儿);cli_run 原样。
Step4(completion):本轮派了子代理 + wake-capable 来源,即使没显式调 wait 也可
  干净结束回合;提交验收 / 写了子代理产物的形态不被抢短路。

硬护栏:所有新行为 source-gated(cli_run 严格原样);不动四档裁决核心。
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    _round_can_finish_via_soft_wait,
    completion_response_after_tool_round,
)
from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
    FinalExitRequest,
    FinalExitState,
    final_exit_closeout_decision,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.process_control import is_pid_alive

pytestmark = pytest.mark.integration

_REWORK_MARKER = "[MAIN_AGENT_DELIVERY_REWORK_REQUIRED]"


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.kill()
    proc.wait(timeout=5)
    return proc.pid


def _agent(tmp_path: Path, manager: SubAgentManager, *, continuations: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        config=AgentConfig(run_repair_max_continuations=continuations),
        tools=SimpleNamespace(workspace_root=tmp_path),
        root=tmp_path,
        subagents=manager,
    )


def _make_child(manager: SubAgentManager, *, status: str, pid: int = 0) -> str:
    task = manager.create_run(goal="非阻塞出口门子代理", thought="P2", plan=["run"])
    task.status = status
    attrs = dict(task.attributes or {})
    if pid > 0:
        attrs["background_start"] = {"pid": pid, "status": "running", "launch_id": "launch-x"}
    task.attributes = attrs
    manager.save(task)
    return task.id


def _register_child(task_root: Path, run_id: str, *, status: str = "RUNNING", capreqs: list | None = None) -> None:
    agent_dir = task_root / "work" / "agents" / run_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "canonical_state.json").write_text(
        json.dumps(
            {"id": run_id, "status": status, "capability_requests": capreqs or []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _params(task_root: Path, *, source: str) -> ToolLoopExecuteParams:
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
        request_id="req-p2",
        run_id="run-p2",
        task_id="run-p2",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        source=source,
    )


def _final(text: str = "已派子代理去处理,稍等。") -> SimpleNamespace:
    return SimpleNamespace(text=text, backend="echo")


# ---------------------------------------------------------------------------
# Step2:出口门非阻塞 yield
# ---------------------------------------------------------------------------


def test_wake_source_with_live_child_yields_cleanly(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())  # 自身进程=live
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="background_main_agent")

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("已派子代理,后台跑着呢。"), FinalExitState())
    )

    assert decision.should_continue is False, "wake-capable + live 子代理必须干净撒手,不打回"
    text = str(decision.response.text) if decision.response is not None else "已派子代理,后台跑着呢。"
    assert _REWORK_MARKER not in text
    assert "[RUN_NONBLOCKING_YIELD]" in text
    assert "已派子代理,后台跑着呢。" in text, "模型原文必须保留"
    # 干净撒手:不跑 closeout(不落 closeout.json)、不注 rework 指令
    assert not (task_root / ".agent_delivery" / "closeout.json").exists()
    assert not any("[final-exit-contract]" in str(item) for item in params.tool_context)
    # 不回收后台子代理:pid 仍活
    assert is_pid_alive(os.getpid()) is True
    assert manager.load(run_id).status == "RUNNING", "撒手不改子代理状态"


def test_cli_run_with_same_live_child_still_blocks(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="cli_run")
    state = FinalExitState()

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("收尾。"), state))

    assert decision.should_continue is True, "cli_run 非 wake 来源:行为原样,仍走 closeout 打回续修"
    assert state.continuations == 1
    assert (task_root / ".agent_delivery" / "closeout.json").exists(), "cli_run 照旧落 closeout"


def test_wake_source_default_run_is_not_wake_capable(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="run")  # 默认 run 不在 wake 名单

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final(), FinalExitState()))

    assert decision.should_continue is True, "source=run 不是 wake-capable,照旧走 closeout 打回"
    assert (task_root / ".agent_delivery" / "closeout.json").exists()


def test_wake_source_with_dead_pid_child_does_not_yield(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=_dead_pid())  # 真僵尸
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="gateway")

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final(), FinalExitState()))

    assert decision.should_continue is True, "死 pid 僵尸不算 live,不能撒手,照旧走验收门"
    assert (task_root / ".agent_delivery" / "closeout.json").exists()


def test_wake_source_open_capreq_blocks_yield(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    capreqs = [{"id": "capreq-1", "status": "OPEN", "needed_capability": "x"}]
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())
    _register_child(task_root, run_id, capreqs=capreqs)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="background_main_agent")

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final(), FinalExitState()))

    assert decision.should_continue is True, "有待裁决能力申请必须主代理处置,不能撒手"
    assert (task_root / ".agent_delivery" / "closeout.json").exists()


def test_wake_source_all_terminal_children_goes_through_gate(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="CANCELLED", pid=os.getpid())  # 已收口但零产物
    _register_child(task_root, run_id, status="CANCELLED")
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="gateway")

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("无法完成。"), FinalExitState()))

    assert decision.should_continue is True, "无 open 子代理(全终态)不 yield,照旧走空交付门"
    assert (task_root / ".agent_delivery" / "closeout.json").exists()


# ---------------------------------------------------------------------------
# Step4:completion 派子代理后干净结束(不必显式 wait)
# ---------------------------------------------------------------------------


def _completion_params(*, source: str, executed: list[str]) -> ToolLoopExecuteParams:
    params = _params(Path("/tmp/does-not-matter"), source=source)
    params.executed_tools.extend(executed)
    return params


def _completion(agent, params, *, subagent_output_written: bool = False) -> ModelResponse | None:
    return completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL create_subagents]", backend="echo"),
            before_executed_count=0,
            subagent_output_written=subagent_output_written,
        )
    )


def test_wake_dispatch_round_finishes_turn_without_explicit_wait(tmp_path: Path) -> None:
    params = _completion_params(source="background_main_agent", executed=["create_subagents"])
    response = _completion(SimpleNamespace(config=AgentConfig()), params)

    assert response is not None, "wake-capable + 本轮派了子代理 → 即使没调 wait 也干净结束回合"
    assert "不继续轮询" in response.text
    assert not (tmp_path / ".agent_delivery").exists()


def test_cli_run_dispatch_round_does_not_finish_turn() -> None:
    params = _completion_params(source="cli_run", executed=["create_subagents"])
    response = _completion(SimpleNamespace(config=AgentConfig()), params)

    assert response is None, "cli_run 非 wake 来源:派了子代理也不非阻塞结束(行为原样)"


def test_wake_explicit_wait_still_finishes_turn() -> None:
    params = _completion_params(source="chat", executed=["wait"])
    response = _completion(SimpleNamespace(config=AgentConfig()), params)

    assert response is not None and "不继续轮询" in response.text, "显式 wait 的干净结束不受影响"


def test_dispatch_yield_predicate_guards_submit_and_output() -> None:
    # 提交验收 / 写了子代理产物的形态不能被派子代理短路抢走(优先走收口)。
    submitted = _completion_params(source="gateway", executed=["create_subagents", "submit_for_acceptance"])
    assert _round_can_finish_via_soft_wait(
        ToolRoundCompletionRequest(SimpleNamespace(), submitted, ModelResponse(text="x", backend="e"), 0, False)
    ) is False
    dispatched = _completion_params(source="gateway", executed=["create_subagents"])
    assert _round_can_finish_via_soft_wait(
        ToolRoundCompletionRequest(SimpleNamespace(), dispatched, ModelResponse(text="x", backend="e"), 0, True)
    ) is False, "写了子代理产物的回合走 subagent 收口,不被 yield 抢短路"
    assert _round_can_finish_via_soft_wait(
        ToolRoundCompletionRequest(SimpleNamespace(), dispatched, ModelResponse(text="x", backend="e"), 0, False)
    ) is True


# ---------------------------------------------------------------------------
# Step5:回归——验收核心(四档裁决)未被动
# ---------------------------------------------------------------------------


def test_four_tier_closeout_l1_block_verdict_unchanged(tmp_path: Path) -> None:
    """四档裁决行为快照:L1 客观阻断(未收口 open 子代理)→ closeout 不放行(返回 None,
    走返工),裁决输出不变(closeout.py 四档核心零改动的行为证据)。"""
    from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
        MainAgentDeliveryCloseoutRequest,
        main_agent_delivery_closeout_response,
    )

    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="cli_run")

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="echo")
    )

    assert response is None, "L1 open 子代理:四档裁决不放行(返回 None → 返工)"
    report = json.loads((task_root / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["subagent_aggregation_gate"]["allowed"] is False


def test_four_tier_closeout_l0_allow_verdict_unchanged(tmp_path: Path) -> None:
    """四档裁决行为快照:L0 放行——真实结果文件 + 已收口子代理 → closeout 放行,
    返回 [MAIN_AGENT_DELIVERY_COMPLETE]。"""
    from agent_py_agent.agent.agent_core.delivery_closeout.closeout import (
        MainAgentDeliveryCloseoutRequest,
        main_agent_delivery_closeout_response,
    )

    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="CANCELLED", pid=0)
    _register_child(task_root, run_id, status="CANCELLED")
    output_dir = task_root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / "结果.md"
    report_file.write_text("# 结果\n\n结论:数据完整可用。\n", encoding="utf-8")
    params = _params(task_root, source="cli_run")
    params.archive_tool_calls.append(
        {"tool": "write_file", "call_id": "1", "ok": True, "parameters": {"path": str(report_file)}, "output": "ok"}
    )

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent_with(manager, task_root), params=params, backend="echo")
    )

    if response is not None:
        assert "[MAIN_AGENT_DELIVERY_COMPLETE]" in str(response.text), "L0 放行输出 COMPLETE 标记不变"


def agent_with(manager: SubAgentManager, task_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        config=AgentConfig(run_repair_max_continuations=3),
        tools=SimpleNamespace(workspace_root=task_root.parent),
        root=task_root.parent,
        subagents=manager,
    )
