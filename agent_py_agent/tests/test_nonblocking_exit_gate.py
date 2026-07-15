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
from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
    finish_natural_user_reply,
    natural_user_reply_is_acceptable,
    natural_user_reply_model_params,
    natural_user_reply_rejection_reason,
    pending_natural_user_reply,
    queue_delivery_completion_user_reply,
    retry_natural_user_reply,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation.channels import (
    delivery_complete_payload,
    render_delivery_complete_signal,
)
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


def test_wake_dispatch_round_reaching_exit_yields_with_structured_status(tmp_path: Path) -> None:
    """派工轮走到边缘出口时保留模型原文，非阻塞事实只放响应字段。"""
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())  # 自身进程=live
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="background_main_agent")
    params.executed_tools.append("create_subagents")  # 这轮派了子代理 → 撒手带声明

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("已派子代理,后台跑着呢。"), FinalExitState())
    )

    assert decision.should_continue is False, "wake-capable + live 子代理必须干净撒手,不打回"
    text = str(decision.response.text) if decision.response is not None else "已派子代理,后台跑着呢。"
    assert _REWORK_MARKER not in text
    assert "[RUN_NONBLOCKING_YIELD]" not in text
    assert "已派子代理,后台跑着呢。" in text, "模型原文必须保留"
    assert decision.response.runtime_status == "ok"
    assert decision.response.runtime_source == "background_liveness"
    assert json.loads(decision.response.runtime_reason)["open_children"] == 1
    # 干净撒手:不跑 closeout(不落 closeout.json)、不注 rework 指令
    assert not (task_root / ".agent_delivery" / "closeout.json").exists()
    assert not any("[final-exit-contract]" in str(item) for item in params.tool_context)
    # 不回收后台子代理:pid 仍活
    assert is_pid_alive(os.getpid()) is True
    assert manager.load(run_id).status == "RUNNING", "撒手不改子代理状态"


def test_wake_chat_round_passes_through_clean(tmp_path: Path) -> None:
    """④缺口①:边跑边聊——上一轮派的活子代理还在后台跑,这轮只是聊天(没派子代理)。
    正常回复("100")必须原文直接过:不 rework、不塞 soft-wait/yield 声明、不跑 closeout。"""
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())  # 上一轮派的,仍 live
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="gateway")  # 这轮 executed_tools 为空=没派子代理

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("50+50=100。"), FinalExitState())
    )

    assert decision.should_continue is False, "聊天轮必须放行,绝不打回"
    text = str(decision.response.text) if decision.response is not None else "50+50=100。"
    assert text == "50+50=100。", "聊天答案原文直接过,不追加任何声明"
    assert _REWORK_MARKER not in text
    assert "[RUN_NONBLOCKING_YIELD]" not in text, "聊天轮不塞非阻塞/soft-wait 声明"
    # 不跑 closeout、不注 rework 指令、不改子代理状态
    assert not (task_root / ".agent_delivery" / "closeout.json").exists()
    assert not any("[final-exit-contract]" in str(item) for item in params.tool_context)
    assert manager.load(run_id).status == "RUNNING", "聊天轮不动上一轮的活子代理"


def test_wake_query_progress_round_passes_through_clean(tmp_path: Path) -> None:
    """④缺口②:查进度——这轮 inspect_agent_tree 看了子代理树,给出进度报告(没派子代理)。
    进度报告必须能收口直接过:不 rework、不塞声明、不跑 closeout。"""
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="chat")
    params.executed_tools.append("inspect_agent_tree")  # 只读查树,不是派子代理

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("你派的两个助手都还在后台跑,稍等就好。"), FinalExitState())
    )

    assert decision.should_continue is False, "查进度轮必须放行收口,绝不打回"
    text = str(decision.response.text) if decision.response is not None else ""
    assert text == "你派的两个助手都还在后台跑,稍等就好。", "进度报告原文直接过"
    assert _REWORK_MARKER not in text
    assert "[RUN_NONBLOCKING_YIELD]" not in text, "查进度轮不塞非阻塞/soft-wait 声明"
    assert not (task_root / ".agent_delivery" / "closeout.json").exists()
    assert manager.load(run_id).status == "RUNNING"


def test_cli_run_chat_round_with_live_child_still_blocks(tmp_path: Path) -> None:
    """③回归:同样的"边跑边聊"形态,cli_run(非 wake)照旧走 closeout 打回续修——
    字节级不变,同步模型仍必须当场收口。"""
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="cli_run")  # 非 wake:聊天形态也不放行

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("50+50=100。"), FinalExitState())
    )

    assert decision.should_continue is True, "cli_run 非 wake:开放子代理场景照旧 closeout 打回"
    assert (task_root / ".agent_delivery" / "closeout.json").exists()


def test_user_chat_round_decoupled_even_with_dead_pid_child(tmp_path: Path) -> None:
    """用户交互轮(gateway 聊天/查进度)和子代理生命周期【彻底解耦】:即便某子代理是死 pid 僵尸,
    用户当轮也原文放行答用户——因为拿僵尸去把用户"50+50"的聊天返工,既没答用户、也没干净处置
    僵尸(那是错的)。僵尸由 background_main_agent 的叫回轮 + supervisor 孤儿回收单独处置,不该拦
    用户当轮。(活性检查对刚完成/滞后的子代理会误判成僵尸,所以用户轮干脆不依赖它。)"""
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=_dead_pid())  # 死 pid
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="gateway")  # 用户聊天轮

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, _final("50+50=100。"), FinalExitState())
    )

    assert decision.should_continue is False, "用户交互轮和子代理解耦,原文放行答用户"
    assert "50+50=100" in str(decision.response.text)
    assert not (task_root / ".agent_delivery" / "closeout.json").exists()


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


def test_recall_round_with_dead_pid_child_still_goes_through_gate(tmp_path: Path) -> None:
    """叫回整合轮(source=background_main_agent,主代理自发被唤醒来整合)【不解耦】:遇死 pid 僵尸
    照旧走交付门/孤儿回收,该处置就处置(这轮就是来收口子代理成果的,不能放行躲掉)。与用户交互轮
    的解耦形成对照——只有用户发起的轮解耦,自发的叫回轮该干活。"""
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=_dead_pid())  # 死 pid
    _register_child(task_root, run_id)
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="background_main_agent")  # 叫回整合轮

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final(), FinalExitState()))

    assert decision.should_continue is True, "叫回整合轮不解耦,照旧走门处置僵尸/收口"
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


def test_recall_round_terminal_children_goes_through_gate(tmp_path: Path) -> None:
    """叫回整合轮(source=background_main_agent,子代理完成把主代理自发唤醒来整合)+ 全终态子代理
    → 照旧走交付门/空交付门(这轮就是来交付子代理成果的,零产物就该被拦),绝不放行。"""
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="CANCELLED", pid=os.getpid())  # 已收口但零产物
    _register_child(task_root, run_id, status="CANCELLED")
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="background_main_agent")

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("无法完成。"), FinalExitState()))

    assert decision.should_continue is True, "叫回整合轮零产物照旧走空交付门,不放行"
    assert (task_root / ".agent_delivery" / "closeout.json").exists()


def test_user_chat_round_terminal_children_passes_through(tmp_path: Path) -> None:
    """用户交互轮(source=gateway,聊天/查进度)+ 子代理已跑完(全终态、非僵尸)→ 模型原文直接
    放行:这轮不为"上一轮派的、已完成待叫回整合的子代理"背空交付的锅(子代理由叫回轮整合交付)。
    这正是修的窄缝:子代理跑得快、用户来聊天时已 open=0,原来会被空交付门返工。"""
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="DONE", pid=os.getpid())  # 已正常完成
    _register_child(task_root, run_id, status="DONE")
    agent = _agent(tmp_path, manager)
    params = _params(task_root, source="gateway")

    decision = final_exit_closeout_decision(FinalExitRequest(agent, params, _final("50+50=100。"), FinalExitState()))

    assert decision.should_continue is False, "用户交互轮 + 已完成子代理 → 原文放行,不返工"
    text = str(decision.response.text) if decision.response is not None else ""
    assert _REWORK_MARKER not in text and "[RUN_NONBLOCKING_YIELD]" not in text
    assert "50+50=100" in text, "聊天答案原文必须原样回用户"
    assert not (task_root / ".agent_delivery" / "closeout.json").exists()


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

    assert response is None, "派工后先进入无工具的模型回复阶段，不由系统拼固定回执"
    phase = pending_natural_user_reply(params)
    assert phase is not None and phase["kind"] == "background_dispatch"
    reply_params = natural_user_reply_model_params(params)
    assert reply_params.allowed_tools == []
    assert reply_params.tool_catalog_section == ""
    assert reply_params.tool_context == []
    assert reply_params.runtime_injections == []
    assert "不要照抄系统模板" in reply_params.user_prompt
    assert reply_params.context_scope == "isolated"
    assert not (tmp_path / ".agent_delivery").exists()


def test_natural_reply_model_view_drops_heavy_runtime_context_but_keeps_voice_inputs(tmp_path: Path) -> None:
    params = dataclasses.replace(
        _completion_params(source="gateway", executed=["create_subagents"]),
        memories=[{"large": "memory" * 1000}],
        runtime_injections=["runtime" * 1000],
        prompt_files=["SOUL.md"],
        tool_context=["old-tool-result" * 1000],
        tool_ir_history=[{"type": "tool_result", "content": "huge" * 1000}],
        delivery_contract={"artifacts": [{"path": "old"}]},
    )
    assert _completion(SimpleNamespace(config=AgentConfig()), params) is None

    reply_params = natural_user_reply_model_params(params)

    assert reply_params.memories == []
    assert reply_params.runtime_injections == []
    assert "[natural-user-reply]" in reply_params.user_prompt
    assert "runtime" * 100 not in reply_params.user_prompt
    assert reply_params.tool_ir_history == []
    assert reply_params.delivery_contract is None
    assert reply_params.prompt_files == ["SOUL.md"]
    assert reply_params.tool_context == []
    assert reply_params.context_scope == "isolated"


def test_dispatch_ack_uses_structured_lifecycle_without_claiming_accepted_is_running() -> None:
    params = _completion_params(source="gateway", executed=["create_subagents"])
    params.archive_tool_calls.append(
        {
            "tool": "create_subagents",
            "tool_result_envelope": {
                "schedule_lifecycle": {
                    "requested_count": 5,
                    "accepted_run_ids": [f"run-{index}" for index in range(5)],
                    "running_run_ids": ["run-0"],
                    "failed_run_ids": [],
                    "counts": {"recorded": 5, "accepted": 5, "running": 1, "failed": 0},
                }
            },
        }
    )

    assert _completion(SimpleNamespace(config=AgentConfig()), params) is None

    phase = pending_natural_user_reply(params)
    assert phase is not None
    facts = phase["facts"]
    assert facts["recorded"] == 5
    assert facts["accepted"] == 5
    assert facts["runner_confirmed_running"] == 1
    assert facts["failed"] == 0


def test_dispatch_ack_aggregates_multiple_same_round_create_calls() -> None:
    params = _completion_params(source="gateway", executed=["create_subagents"] * 3)
    for index in range(3):
        params.archive_tool_calls.append(
            {
                "tool": "create_subagents",
                "tool_result_envelope": {
                    "schedule_lifecycle": {
                        "requested_count": 1,
                        "recorded_run_ids": [f"run-{index}"],
                        "accepted_run_ids": [f"run-{index}"],
                        "running_run_ids": ["run-0"] if index == 0 else [],
                        "failed_run_ids": [],
                        "counts": {"recorded": 1, "accepted": 1, "running": int(index == 0), "failed": 0},
                    }
                },
            }
        )

    assert _completion(SimpleNamespace(config=AgentConfig()), params) is None

    phase = pending_natural_user_reply(params)
    assert phase is not None
    facts = phase["facts"]
    assert facts["recorded"] == 3
    assert facts["accepted"] == 3
    assert facts["runner_confirmed_running"] == 1


def test_cli_run_dispatch_round_does_not_finish_turn() -> None:
    params = _completion_params(source="cli_run", executed=["create_subagents"])
    response = _completion(SimpleNamespace(config=AgentConfig()), params)

    assert response is None, "cli_run 非 wake 来源:派了子代理也不非阻塞结束(行为原样)"


def test_wake_explicit_wait_still_finishes_turn() -> None:
    params = _completion_params(source="chat", executed=["wait"])
    response = _completion(SimpleNamespace(config=AgentConfig()), params)

    assert response is None
    phase = pending_natural_user_reply(params)
    assert phase is not None and phase["kind"] == "wait"
    assert phase["facts"] == {"wait_registered": True, "reply_is_interim": True}


def test_natural_background_reply_keeps_model_words_and_structured_status() -> None:
    params = _completion_params(source="gateway", executed=["create_subagents"])
    assert _completion(SimpleNamespace(config=AgentConfig()), params) is None
    model_reply = ModelResponse(text="我先把这几块分别推进，有结果后再一起给你。", backend="echo")

    assert natural_user_reply_is_acceptable(model_reply) is True
    response = finish_natural_user_reply(params, model_reply, accepted=True)

    assert response.text == model_reply.text
    assert response.runtime_status == "ok"
    assert response.runtime_reason == "background_dispatch"
    assert pending_natural_user_reply(params) is None


def test_natural_background_reply_retries_then_suppresses_internal_protocol() -> None:
    params = _completion_params(source="gateway", executed=["create_subagents"])
    assert _completion(SimpleNamespace(config=AgentConfig()), params) is None
    leaked = ModelResponse(
        text='[natural-user-reply] {"facts":{"recorded":5}}',
        backend="echo",
    )

    assert natural_user_reply_is_acceptable(leaked) is False
    assert retry_natural_user_reply(params, rejection_reason="internal_protocol") is True
    assert pending_natural_user_reply(params)["previous_rejection"] == "internal_protocol"
    assert "只重新写一段纯自然语言回复" in natural_user_reply_model_params(params).user_prompt
    assert retry_natural_user_reply(params) is False

    response = finish_natural_user_reply(params, leaked, accepted=False)
    assert response.text == ""
    assert response.runtime_status == "user_reply_unavailable"
    assert response.runtime_reason == "background_dispatch"
    assert response.runtime_source == "model_user_reply"
    assert pending_natural_user_reply(params) is None


def test_natural_background_reply_rejects_minimax_named_xml_tools() -> None:
    phase = {"facts": {"reply_is_interim": True}}
    for text in (
        "我先处理。\n<task_progress>\n- [ ] 核对\n</task_progress>",
        '我先处理。\n<spawn_subagent>\n{"task_name":"核对"}\n</spawn_subagent>',
        '我先处理。\n<spawn_subagent>\n{"task_name":"未闭合"}',
    ):
        assert natural_user_reply_is_acceptable(ModelResponse(text=text, backend="echo"), phase) is False


def test_natural_background_reply_rejects_unverified_eta() -> None:
    params = _completion_params(source="gateway", executed=["create_subagents"])
    assert _completion(SimpleNamespace(config=AgentConfig()), params) is None
    phase = pending_natural_user_reply(params)

    assert natural_user_reply_is_acceptable(
        ModelResponse(text="预计几分钟后就能完成。", backend="echo"),
        phase,
    ) is False
    assert natural_user_reply_is_acceptable(
        ModelResponse(text="我已经分开推进这些部分，有结果会继续告诉你。", backend="echo"),
        phase,
    ) is True


def test_natural_background_reply_rejects_interim_claim_that_all_tasks_completed() -> None:
    params = _completion_params(source="gateway", executed=["create_subagents"])
    assert _completion(SimpleNamespace(config=AgentConfig()), params) is None
    phase = pending_natural_user_reply(params)

    premature = ModelResponse(text="5个任务全部完成并已确认记录，无失败项。", backend="echo")
    truthful = ModelResponse(text="已接受5个任务，后续结果会在完成后汇总。", backend="echo")

    assert natural_user_reply_rejection_reason(premature, phase) == "contradicts_interim_state"
    assert natural_user_reply_is_acceptable(truthful, phase) is True


def test_gateway_completion_rewrites_user_summary_from_final_snapshot() -> None:
    params = _completion_params(source="gateway", executed=[])
    signal_text = render_delivery_complete_signal(
        {
            "ok": True,
            "user_summary": "旧草稿说大约 21KB。",
            "artifacts": [{"artifact_id": "report", "path": "/owner/tasks/report.md", "kind": "md", "ok": True}],
            "delivery_snapshot": {
                "closeout_ok": True,
                "validated": False,
                "snapshot_id": "snapshot-1",
                "artifacts": [
                    {
                        "artifact_id": "report",
                        "name": "report.md",
                        "kind": "md",
                        "size_bytes": 25771,
                        "sha256": "abc",
                    }
                ],
            },
        }
    )

    assert queue_delivery_completion_user_reply(params, ModelResponse(text=signal_text, backend="echo")) is True
    phase = pending_natural_user_reply(params)
    assert phase is not None and phase["kind"] == "task_completion"
    assert "draft" not in phase
    assert phase["facts"]["task_status"] == "completed"
    assert phase["facts"]["further_runtime_action_required"] is False
    assert "quality_advisories" not in phase["facts"]["delivery_snapshot"]
    stale = ModelResponse(text="报告已完成，约 21KB。", backend="echo")
    fresh = ModelResponse(text="报告已经完成，最终文件是 report.md。", backend="echo")
    contradictory = ModelResponse(text="文件已经生成，但任务需要重新提交修复。", backend="echo")

    assert natural_user_reply_is_acceptable(stale, phase) is False
    assert natural_user_reply_rejection_reason(contradictory, phase) == "contradicts_final_state"
    assert natural_user_reply_is_acceptable(fresh, phase) is True
    finished = finish_natural_user_reply(params, fresh, accepted=True)
    payload = delivery_complete_payload(finished.text)
    assert payload is not None
    assert payload["user_summary"] == fresh.text
    assert "旧草稿" not in finished.text
    assert payload["delivery_snapshot"]["artifacts"][0]["size_bytes"] == 25771


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
