# 自驱蹲守底座(P1 持续监控)四件套 + M2 长跑声明的单测:
#   ① 定时唤醒轮工具集必须带工作工具(read/write/run/task_progress),不再"读不了文件";
#   ② 定时唤醒提示词=续跑自己任务(不问用户),且 due policy 的 wait_reason 透传进唤醒上下文;
#   ③ wait 通用化:soft-wait 保留模型原文(命中上报不被样板吞)、cancel 停循环提醒、
#      收口(ok=True)自动退休提醒;
#   ④ 问句守卫扩展到 background_main_agent(后台唤醒轮没人应答,不许以提问收尾);
#   ⑤ compact 深度硬顶对结构化声明的 long_running 任务豁免(防跑飞软顶仍在)。
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.finalization_compact_auto import (
    _compact_auto_continue_depth_exhausted,
)
from agent_py_agent.agent.agent_core.orchestration.create_policy import _create_attributes
from agent_py_agent.agent.agent_core.runtime.progress_policy_retirement import (
    retire_task_progress_policies_on_closeout,
)
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    _soft_wait_response,
)
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.final_exit_contract import (
    FinalExitRequest,
    FinalExitState,
    final_exit_closeout_decision,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeChannelHub,
)
from agent_py_agent.agent.conversation.runtime import (
    SCHEDULED_BACKGROUND_ALLOWED_TOOLS,
    BackgroundToolPolicyRequest,
    background_prompt,
    background_tool_policy_decision,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


# ---------------------------------------------------------------------------
# ① 定时唤醒轮工具集
# ---------------------------------------------------------------------------


def test_scheduled_wake_toolset_includes_work_tools() -> None:
    decision = background_tool_policy_decision(
        None, request=BackgroundToolPolicyRequest(reason="scheduled_progress_report")
    )

    assert decision.profile == "scheduled_progress"
    for tool in ("read_file", "list_files", "write_file", "run_command", "task_progress", "submit_for_acceptance"):
        assert tool in decision.allowed_tools, f"定时唤醒轮缺工作工具 {tool}(会复现'读不到文件')"
    assert "create_subagents" not in decision.allowed_tools


def test_default_wake_toolset_includes_work_tools_and_create() -> None:
    decision = background_tool_policy_decision(None, request=BackgroundToolPolicyRequest(reason=""))

    assert "read_file" in decision.allowed_tools
    assert "create_subagents" in decision.allowed_tools


def test_all_wake_profiles_can_resolve_and_cancel_stuck_children() -> None:
    # 真机 0/22 拖死链钉子:收尾门指引"取消/接管/重跑",唤醒轮工具集就必须真有
    # 解阻(resolve)和了结(cancel)工具,否则救不回的 BLOCKED 子代理没有任何出路。
    for reason in ("scheduled_progress_report", "subagent_runner_finished", "", "urgent_wake_signal"):
        decision = background_tool_policy_decision(
            None, request=BackgroundToolPolicyRequest(reason=reason)
        )
        for tool in ("resolve_capability_requests", "cancel_subagents"):
            assert tool in decision.allowed_tools, f"唤醒轮({reason or 'default'})缺 {tool}"


# —— 不足3·续推开路:主账本清单还有未闭环项 → 唤醒/定时轮拿得到 create_subagents ——


def test_lifecycle_wake_gains_create_subagents_when_coverage_open() -> None:
    decision = background_tool_policy_decision(
        None,
        request=BackgroundToolPolicyRequest(reason="subagent_runner_finished", open_coverage_targets=16),
    )
    assert decision.profile == "subagent_integration_continue"
    assert "create_subagents" in decision.allowed_tools
    # 整合工具仍齐(续推轮同时要能读/写/跑/交付)。
    for tool in ("read_file", "write_file", "run_command", "task_progress", "submit_for_acceptance"):
        assert tool in decision.allowed_tools


def test_lifecycle_wake_stays_integration_only_when_coverage_clean() -> None:
    """清单全闭(或本无清单)→ 原整合集合一字不动(防"派读取孙代理绕圈"回归)。"""
    decision = background_tool_policy_decision(
        None,
        request=BackgroundToolPolicyRequest(reason="subagent_runner_finished", open_coverage_targets=0),
    )
    assert decision.profile == "subagent_integration"
    assert "create_subagents" not in decision.allowed_tools


def test_scheduled_wake_gains_create_subagents_when_coverage_open() -> None:
    decision = background_tool_policy_decision(
        None,
        request=BackgroundToolPolicyRequest(reason="scheduled_progress_report", open_coverage_targets=3),
    )
    assert decision.profile == "scheduled_progress_continue"
    assert "create_subagents" in decision.allowed_tools


# ---------------------------------------------------------------------------
# ② 定时唤醒提示词 + wait_reason 透传
# ---------------------------------------------------------------------------


def test_background_prompt_scheduled_wake_is_self_continuation() -> None:
    prompt = background_prompt("scheduled_progress_report")

    assert "续跑轮" in prompt
    assert "wait_reason" in prompt
    assert "不会有人回答" in prompt
    assert "提交验收" in prompt
    # 不点名具体工具(部署可收窄工具集,prompt 不得引用可能不可用的工具名)
    assert "dispatch_subagents" not in prompt and "submit_for_acceptance" not in prompt
    # 旧的"定时汇报"框架不能回潮
    assert "如果只是定时汇报" not in prompt


def test_due_policy_wake_carries_wait_reason_into_prompt(tmp_path) -> None:
    captured: list[str] = []

    class _Backend:
        name = "capturing"

        def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
            captured.append(prompt)
            return ModelResponse(text="本轮检查完成。", backend=self.name)

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _Backend()
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "task-watch", "goal": "盯日志", "now": 11.0})
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-watch",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": "thread-1",
            "metadata": {"kind": "subagent_progress_watch", "tool": "wait", "reason": "盯日志增量有目标行才上报"},
            "now": 12.0,
        }
    )

    reports = scheduler.tick(now=73.0)

    assert len(reports) == 1
    assert len(captured) == 1
    assert "盯日志增量有目标行才上报" in captured[0], "wait 登记的原因必须透传进唤醒上下文"
    assert "续跑轮" in captured[0]


# ---------------------------------------------------------------------------
# ③ wait 通用化:soft-wait 保留原文 / cancel / 收口退休
# ---------------------------------------------------------------------------


def test_soft_wait_response_preserves_model_hit_report() -> None:
    request = ToolRoundCompletionRequest(
        agent=SimpleNamespace(),
        params=SimpleNamespace(source="gateway", executed_tools=["read_file", "wait"]),
        response=ModelResponse(text="命中:第42行出现目标事件,证据如下……", backend="echo"),
        before_executed_count=0,
        subagent_output_written=False,
    )

    text = _soft_wait_response(request).text

    assert "命中:第42行出现目标事件" in text, "模型的命中上报不能被样板文字吞掉"
    assert "已登记非阻塞等待提醒" in text
    assert "子代理继续在后台运行" not in text


def test_soft_wait_response_without_model_text_uses_note_only() -> None:
    request = ToolRoundCompletionRequest(
        agent=SimpleNamespace(),
        params=SimpleNamespace(source="gateway", executed_tools=["wait"]),
        response=ModelResponse(text="", backend="echo"),
        before_executed_count=0,
        subagent_output_written=False,
    )

    assert "已登记非阻塞等待提醒" in _soft_wait_response(request).text


def test_wait_cancel_disables_policies_for_current_task(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    registered = json.loads(
        agent.tools.tools["wait"].execute({"task_id": "task-w1", "seconds": 60, "reason": "盯日志"}).output
    )
    assert registered["scheduled"] is True

    cancelled = json.loads(agent.tools.tools["wait"].execute({"task_id": "task-w1", "cancel": True}).output)

    assert cancelled["ok"] is True
    assert cancelled["mode"] == "cancel"
    assert cancelled["cancelled_count"] == 1
    policy = agent.conversation_store.get_progress_policy(registered["policy_id"])
    assert policy is not None and policy.enabled is False


def test_wait_reregistration_replaces_previous_policy(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    first = json.loads(
        agent.tools.tools["wait"].execute({"task_id": "task-loop", "seconds": 60, "reason": "读到 seq=10"}).output
    )
    second = json.loads(
        agent.tools.tools["wait"].execute({"task_id": "task-loop", "seconds": 60, "reason": "读到 seq=70"}).output
    )

    old = agent.conversation_store.get_progress_policy(first["policy_id"])
    new = agent.conversation_store.get_progress_policy(second["policy_id"])
    assert old is not None and old.enabled is False, "同任务重复登记必须替换旧提醒,不堆积"
    assert new is not None and new.enabled is True
    assert new.metadata["reason"] == "读到 seq=70"


def test_closeout_ok_retires_task_watch_policies(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    registered = json.loads(
        agent.tools.tools["wait"].execute({"task_id": "task-done", "seconds": 60, "reason": "盯守"}).output
    )
    params = SimpleNamespace(task_id="task-done")

    retire_task_progress_policies_on_closeout(agent, params, {"ok": False})
    still_enabled = agent.conversation_store.get_progress_policy(registered["policy_id"])
    assert still_enabled is not None and still_enabled.enabled is True, "ok=False 不许退休提醒"

    retire_task_progress_policies_on_closeout(agent, params, {"ok": True})
    retired = agent.conversation_store.get_progress_policy(registered["policy_id"])
    assert retired is not None and retired.enabled is False, "验收通过后循环提醒必须自动停"


def _write_watch_state(
    agent, *, watch_id: str, window: int, elapsed: int, closed: bool, written: int = 0, consumed: int = 0
) -> None:
    import time

    from agent.ingestion.watch_state import state_dir

    directory = state_dir(Path(agent.home_paths.owner_home_dir))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{watch_id}.json").write_text(
        json.dumps(
            {
                "watch_id": watch_id,
                "opened_at": time.time() - elapsed,
                "watch_window_seconds": window,
                "closed": closed,
                "totals": {"spool_candidates": written},
            }
        ),
        encoding="utf-8",
    )
    (directory / f"{watch_id}.read.json").write_text(
        json.dumps({"read_seq": 0, "candidates_consumed": consumed, "updated_at": time.time()}),
        encoding="utf-8",
    )


def test_closeout_keeps_incomplete_watch_policy_but_retires_others(tmp_path) -> None:
    # §1:模型盯守窗口未满就 ok=True 收口(判读层提前收工),但窗口没走完唤醒链不该断——
    # watch 提醒留活(靠磁盘发现续跑),非 watch 提醒(dispatch 监督)照常退休。
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    _write_watch_state(agent, watch_id="ws-live", window=1200, elapsed=120, closed=False)
    watch_policy = json.loads(
        agent.tools.tools["wait"].execute(
            {"task_id": "task-w", "seconds": 90, "reason": "盯守未满", "run_id": "ws-live"}
        ).output
    )
    non_watch = agent.conversation_store.set_progress_policy(
        {"thread_id": watch_policy["thread_id"], "task_id": "task-w", "interval_seconds": 180,
         "metadata": {"tool": "dispatch_supervision_auto"}}
    )

    retire_task_progress_policies_on_closeout(agent, SimpleNamespace(task_id="task-w"), {"ok": True})

    kept = agent.conversation_store.get_progress_policy(watch_policy["policy_id"])
    dropped = agent.conversation_store.get_progress_policy(non_watch.policy_id)
    assert kept is not None and kept.enabled is True, "窗口未满的盯守提醒不许被收口退休(否则盯守睡死)"
    assert dropped is not None and dropped.enabled is False, "非盯守提醒照常退休,不受保护"


def test_closeout_retires_watch_policy_once_window_complete(tmp_path) -> None:
    # 盯守窗口已满且积压清零(或已 close)→ 保护解除,验收通过照常退休,不再无谓续跑。
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    _write_watch_state(agent, watch_id="ws-done", window=1200, elapsed=1300, closed=False, written=40, consumed=40)
    watch_policy = json.loads(
        agent.tools.tools["wait"].execute(
            {"task_id": "task-c", "seconds": 90, "reason": "窗口已满", "run_id": "ws-done"}
        ).output
    )

    retire_task_progress_policies_on_closeout(agent, SimpleNamespace(task_id="task-c"), {"ok": True})

    retired = agent.conversation_store.get_progress_policy(watch_policy["policy_id"])
    assert retired is not None and retired.enabled is False, "窗口已满且积压清零的盯守提醒收口后应退休"


def test_closeout_keeps_watch_policy_while_endgame_backlog_unjudged(tmp_path) -> None:
    """g8 不足4·窗口末尾清账:窗口已满但 spool 还剩已抬未判候选(真机到期剩 200/97 条)——
    它们是窗口内的事件,判完才算盯完;此时收口不许退休盯守提醒(否则积压无人来判=静默漏报)。
    积压清零后照常退休(上一测试),不会永不收口。"""
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    _write_watch_state(agent, watch_id="ws-tail", window=1200, elapsed=1300, closed=False, written=97, consumed=40)
    watch_policy = json.loads(
        agent.tools.tools["wait"].execute(
            {"task_id": "task-t", "seconds": 90, "reason": "末尾清账", "run_id": "ws-tail"}
        ).output
    )

    retire_task_progress_policies_on_closeout(agent, SimpleNamespace(task_id="task-t"), {"ok": True})
    kept = agent.conversation_store.get_progress_policy(watch_policy["policy_id"])
    assert kept is not None and kept.enabled is True, "窗口已满但积压未清,盯守提醒不许退休(清账再收口)"

    # 积压判完(消费追平)→ 保护解除,照常退休。
    _write_watch_state(agent, watch_id="ws-tail", window=1200, elapsed=1400, closed=False, written=97, consumed=97)
    retire_task_progress_policies_on_closeout(agent, SimpleNamespace(task_id="task-t"), {"ok": True})
    retired = agent.conversation_store.get_progress_policy(watch_policy["policy_id"])
    assert retired is not None and retired.enabled is False, "积压清零后收口照常退休"


# —— 不足3·退休守卫:coverage 清单没对完账,ok=True 收口也不许退休唤醒链 ——


def _seed_task_coverage(agent, task_id: str, *, open_count: int, done_count: int = 0) -> None:
    from agent_py_agent.agent.task_progress import write_task_progress

    targets = [
        {"id": f"req-{i:02d}", "title": f"模块{i}", "status": "pending"} for i in range(1, open_count + 1)
    ] + [
        {"id": f"req-done-{i}", "title": f"已完成{i}", "status": "done"} for i in range(done_count)
    ]
    write_task_progress(Path(agent.home_paths.owner_home_dir), task_id, {"coverage": {"targets": targets}})


def test_closeout_keeps_policies_while_coverage_open(tmp_path) -> None:
    """真机 u-fixtest2 停 8/24 的死因:唤醒轮 ok=True 收口把派工监督提醒退休 → 唤醒链走空,
    再没有未来轮次推剩余项。守卫:清单还有未闭环项 → 整体不退;对完账后照常退。"""
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    registered = json.loads(
        agent.tools.tools["wait"].execute({"task_id": "task-open", "seconds": 60, "reason": "派工监督"}).output
    )
    _seed_task_coverage(agent, "task-open", open_count=16, done_count=8)
    params = SimpleNamespace(task_id="task-open", run_id="task-open", source="cli_run")

    retire_task_progress_policies_on_closeout(agent, params, {"ok": True})
    kept = agent.conversation_store.get_progress_policy(registered["policy_id"])
    assert kept is not None and kept.enabled is True, "清单还有 open 项,唤醒链不许被收口退休"

    # 清单对完账(全 done)→ 保护解除,照常退休。
    _seed_task_coverage(agent, "task-open", open_count=0, done_count=24)
    from agent_py_agent.agent.task_progress import write_task_progress

    write_task_progress(
        Path(agent.home_paths.owner_home_dir),
        "task-open",
        {"coverage": {"targets": [{"id": f"req-{i:02d}", "status": "done"} for i in range(1, 17)]}},
    )
    retire_task_progress_policies_on_closeout(agent, params, {"ok": True})
    retired = agent.conversation_store.get_progress_policy(registered["policy_id"])
    assert retired is not None and retired.enabled is False, "清单全闭后收口应照常退休提醒"


# —— 不足3·唤醒链补登:消费完 subagent-finished 后 open>0 且无 policy → 机制层补登 ——


def test_wake_chain_ensured_when_coverage_open_and_no_policy(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import _ensure_open_coverage_wake_chain

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    store = ConversationStore(tmp_path / "conversations")
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeChannelHub())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u1",
            "channel": "internal",
            "channel_conversation_id": "c1",
            "channel_user_id": "u1",
        }
    )
    _seed_task_coverage(agent, "task-cont", open_count=5)
    signal = SimpleNamespace(root_task_id="task-cont", thread_id=thread.thread_id)

    _ensure_open_coverage_wake_chain(scheduler, signal, now=100.0)

    policies = [p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-cont"]
    assert len(policies) == 1, "open>0 且无 enabled policy → 必须补登一条唤醒"
    assert policies[0].metadata.get("tool") == "coverage_open_continuation"

    # 已有 enabled policy → 不重复登记。
    _ensure_open_coverage_wake_chain(scheduler, signal, now=200.0)
    assert len([p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-cont"]) == 1

    # 清单全闭 → 不登记(收口链正常走完,不无谓闹钟)。
    from agent_py_agent.agent.task_progress import write_task_progress

    write_task_progress(
        Path(agent.home_paths.owner_home_dir),
        "task-clean2",
        {"coverage": {"targets": [{"id": "req-01", "status": "done"}]}},
    )
    clean_signal = SimpleNamespace(root_task_id="task-clean2", thread_id=thread.thread_id)
    _ensure_open_coverage_wake_chain(scheduler, clean_signal, now=300.0)
    assert not [p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-clean2"]


# —— g8 问题B·solo 保底:closeout choke point 的续推补登(纯 solo 没派过子代理/没调 wait,
#    唤醒轮消费侧的补登永不触发;主 run 收口时清单还有 open 项就得有人接着推)——


def test_closeout_ensures_continuation_for_pure_solo_open_coverage(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import sync_run_task_workspace_closeout
    from agent_py_agent.agent.task_progress import write_task_progress

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {"canonical_user_id": "u1", "channel": "internal", "channel_conversation_id": "c1", "channel_user_id": "u1"}
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "task-solo", "goal": "solo 大工程"})
    _seed_task_coverage(agent, "task-solo", open_count=12, done_count=12)
    params = SimpleNamespace(task_id="task-solo", run_id="task-solo", source="gateway")

    sync_run_task_workspace_closeout(agent, params, {"ok": True})

    policies = [p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-solo"]
    assert len(policies) == 1, "solo 收口后清单还有 open 项 → 必须补登续推唤醒(否则链从未建立)"
    assert policies[0].metadata.get("tool") == "coverage_open_continuation"

    # 幂等:再收口一次不重复登记。
    sync_run_task_workspace_closeout(agent, params, {"ok": True})
    assert len([p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-solo"]) == 1

    # 清单全闭 → 收口自动退休续推提醒(不破"清单全闭自动退休"),且不再补登。
    write_task_progress(
        Path(agent.home_paths.owner_home_dir),
        "task-solo",
        {"coverage": {"targets": [{"id": f"req-{i:02d}", "status": "done"} for i in range(1, 13)]}},
    )
    sync_run_task_workspace_closeout(agent, params, {"ok": True})
    assert not [p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-solo"]


def test_closeout_continuation_skips_internal_scopes_and_clean_ledger(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.runtime.progress_policy_retirement import (
        ensure_open_coverage_continuation,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {"canonical_user_id": "u1", "channel": "internal", "channel_conversation_id": "c1", "channel_user_id": "u1"}
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "task-sub", "goal": "子代理轮不替根任务立提醒"})
    _seed_task_coverage(agent, "task-sub", open_count=3)

    # 子代理 runner(task_local)/内部轮(control_plane)收口不补登。
    ensure_open_coverage_continuation(agent, SimpleNamespace(task_id="task-sub", context_scope="task_local"))
    ensure_open_coverage_continuation(agent, SimpleNamespace(task_id="task-sub", context_scope="control_plane"))
    assert not [p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-sub"]

    # 无清单(短任务/纯问答)不补登。
    ensure_open_coverage_continuation(agent, SimpleNamespace(task_id="task-short"))
    assert not [p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-short"]


# ---------------------------------------------------------------------------
# ③b 账本挂 open 项就提交 → 一次性返工(幂等,二次放行)
# ---------------------------------------------------------------------------


def _open_todo_report(statuses: list[str], *, allowed: bool = False) -> dict:
    items = [{"id": f"item-{i}", "status": status} for i, status in enumerate(statuses)]
    return {
        "workspace_root": "",
        "task_progress_closeout_gate": {
            "allowed": allowed,
            "findings": [
                {
                    "code": "TASK_PROGRESS_OPEN_ITEMS",
                    "evidence": {"open_count": len(items), "open_items": items},
                }
            ],
            "evidence": {
                "open_count": len(items),
                "next_action": "继续读完剩余行",
                "progress_ref": "x/progress.json",
            },
        },
    }


def test_submit_with_open_todo_items_bounces_once_then_passes() -> None:
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import _open_todo_rework

    params = SimpleNamespace(tool_context=[])

    first = _open_todo_rework(params, _open_todo_report(["in_progress"]))
    assert first is True, "账本还挂真 open 项就提交,第一次必须打回对账"
    joined = "\n".join(str(item) for item in params.tool_context)
    assert "[open-todo-items-rework]" in joined and "item-0" in joined

    second = _open_todo_rework(params, _open_todo_report(["in_progress"]))
    assert second is False, "同形态第二次放行,幂等不死锁"


def test_submit_with_clean_ledger_passes_open_todo_gate() -> None:
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import _open_todo_rework

    params = SimpleNamespace(tool_context=[])

    assert _open_todo_rework(params, _open_todo_report([], allowed=True)) is False
    assert params.tool_context == []


def test_submit_with_non_canonical_done_alias_stays_advisory() -> None:
    # 四档裁决既有决策:completed/ok 这类"完成别名"是标签不规范,不是没做完——保持
    #   advisory 不打回(与 test_acceptance_submit_keeps_non_canonical_done_status_advisory 同源)。
    from agent_py_agent.agent.agent_core.delivery_closeout.uncontracted import _open_todo_rework

    params = SimpleNamespace(tool_context=[])

    assert _open_todo_rework(params, _open_todo_report(["completed", "ok"])) is False
    assert params.tool_context == []


# ---------------------------------------------------------------------------
# ④ 问句守卫扩展到后台唤醒轮
# ---------------------------------------------------------------------------


def _question_guard_params(tmp_path, source: str) -> ToolLoopExecuteParams:
    params = ToolLoopExecuteParams(
        user_prompt="盯守任务",
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
        task_attributes={},
        request_id="req-q",
        run_id="run-q",
        task_id="task-q",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
    params = replace(params, source=source)
    params.executed_tools.extend(["read_file", "inspect_agent_tree"])
    return params


def _question_agent(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        config=AgentConfig(run_repair_max_continuations=3),
        tools=SimpleNamespace(workspace_root=tmp_path),
        root=tmp_path,
    )


def test_question_exit_in_background_wake_round_is_pulled_back(tmp_path) -> None:
    agent = _question_agent(tmp_path)
    params = _question_guard_params(tmp_path, "background_main_agent")
    state = FinalExitState()

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, ModelResponse(text="接下来我该怎么办？", backend="echo"), state)
    )

    assert decision.should_continue is True, "后台唤醒轮没人应答,以提问收尾必须打回"
    assert state.question_guard_fired is True
    joined = "\n".join(str(item) for item in params.tool_context)
    assert "不会得到任何回复" in joined


def test_question_exit_in_gateway_round_still_passes(tmp_path) -> None:
    agent = _question_agent(tmp_path)
    params = _question_guard_params(tmp_path, "gateway")

    decision = final_exit_closeout_decision(
        FinalExitRequest(agent, params, ModelResponse(text="请您选择方案 A 还是 B?", backend="echo"), FinalExitState())
    )

    assert decision.should_continue is False, "gateway 有用户在线,提问是正当行为"


# ---------------------------------------------------------------------------
# ⑤ compact 深度硬顶:long_running 声明豁免
# ---------------------------------------------------------------------------


def _depth_ctx(depth: int, attrs: dict | None):
    return SimpleNamespace(compact_auto_continue_depth=depth, task_attributes=attrs)


def test_compact_depth_cap_holds_for_regular_runs() -> None:
    agent = SimpleNamespace(config=SimpleNamespace(memory_compact_auto_continue_max_depth=50))

    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(50, None)) is True
    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(49, None)) is False


def test_compact_depth_cap_exempts_declared_long_running() -> None:
    agent = SimpleNamespace(config=SimpleNamespace(memory_compact_auto_continue_max_depth=50))

    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(50, {"long_running": True})) is False
    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(5000, {"long_running": True})) is False
    # 非 True 值不豁免(结构化声明必须是真 bool)
    assert _compact_auto_continue_depth_exhausted(agent, _depth_ctx(50, {"long_running": "yes"})) is True


def test_create_subagents_long_running_lands_in_task_attributes() -> None:
    attrs = _create_attributes({"long_running": True, "goal": "持续盯守日志"}, None)

    assert attrs.get("long_running") is True

    absent = _create_attributes({"goal": "普通任务"}, None)
    assert "long_running" not in absent
