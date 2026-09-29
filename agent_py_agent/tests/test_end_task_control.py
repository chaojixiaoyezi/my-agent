"""/endtask 会话控制：结束卡在等待中的定时会话任务（2026-09-29）。

起因（2026-09-28 生产事故）：定时运行里一次 run_command 结果未知，工作片以 unfinished 停下，会话任务仍是 active；
定时层把这次运行停在 waiting 等后续事件，而没有任何后续事件，同一 job 的到期派发一直被“已有活动运行”挡住，
两个会话从此不再被唤醒。当晚只能用产品 API 手动收口，这个命令把它变成管理员可在 TUI / 飞书里执行的正式入口。

钉住六件事：
1. 解析只认任务 ID 和 confirm，无参数列候选、只给 ID 是只读预览。
2. 列表与预览只读，只渲染结构化事实（ID、状态、等待起点）。
3. confirm 把会话任务按 CAS 记为 cancelled，并立即结算 waiting 运行，同一 job 恢复派发。
4. 非管理员、执行树里还有未结束的 attempt、不是 waiting 的定时执行、任务已不是 active、运行库不可读，一律拒绝且不改任何状态。
5. 核对之后任务被别的路径改了状态时，确认结束不覆盖（expected_status CAS）。
6. Gateway 分派按 scope 解析 owner；TUI 转发文本与本地模式拒绝与 /recover 同一套约定。
7. 列表与预览附后续工作事实码（存在的事实码、读不出的“项目:错误码”、没有为“无”），来自 owner 的
   scheduler_service.follow_up——与定时执行收口同一个判定，不另写一套。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from agent_py_agent.agent.command_catalog import COMMAND_INDEX, match_conversation_command
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_service, end_task_control
from agent_py_agent.agent.gateway_parts.end_task_control import execute_end_task_control
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.scheduler.active_run_closeout import SchedulerFollowUpPolicy
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_parts import control_runtime


# LLM: 只在 pytest 临时目录经真实 create_job / reserve / claim / park_run_waiting 与 record_run_creation 造出生产形态：
#   定时执行 waiting、会话任务 active、执行树的 attempt 已结束（attempt_running=True 时仍在跑）；不启动模型、不发网络请求。
# 函数用途: 造一条卡在等待中的定时会话任务，返回 agent、任务 ID 与主代理执行记录。
def _stuck_scheduled_task(tmp_path, *, attempt_running: bool = False):
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    store, repository = agent.conversation_store, agent.scheduler_repository
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "thread-stuck",
        "channel_user_id": "user-1", "now": 900.0,
    })
    repository.create_job(SchedulerJobCreateRequest(
        name="stuck", prompt="定时整理", thread_id=thread.thread_id, source_task_id="",
        schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1_000, "timezone": "UTC"},
        misfire_grace_seconds=60, skill_refs=[], source_request_id="stuck", now=900,
    ))
    run_id = str(repository.reserve_due_runs(now=1_000)[0]["run_id"])
    claim_id = str(repository.claim_run(run_id, lease_seconds=300, now=1_001)["claim_id"])
    repository.mark_run_running(run_id, claim_id, now=1_002)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": run_id, "goal": "定时整理", "now": 1_002})
    repository.park_run_waiting(run_id, claim_id, now=1_003)
    record = agent.subagents.runtime_db.record_run_creation(
        owner_id="local/main", conversation_task_id=run_id, thread_id=thread.thread_id,
        run_id=f"run-{run_id}", role="main",
    )
    if not attempt_running:
        settled = agent.subagents.runtime_db.settle_agent_attempt(
            agent_run_id=record["agent_run_id"], attempt_id=record["attempt_id"],
        )
        assert settled["settled"] is True
    return agent, run_id, record


# 函数用途: 读取会话任务状态与定时运行状态，断言“没有改动”时用。
def _state(agent, run_id: str) -> tuple[str, str]:
    run = agent.scheduler_repository.get_active_run(run_id) or {}
    return str(agent.conversation_store.tasks.load(run_id).status), str(run.get("status") or "")


def test_endtask_parses_list_view_apply_and_rejects_extra_text():
    listing = parse_conversation_control("/endtask")
    assert (listing.kind, listing.operation, listing.valid) == ("endtask", "list", True)
    view = parse_conversation_control("/endtask srun_abc")
    assert (view.operation, view.value, view.valid) == ("view", "srun_abc", True)
    apply = parse_conversation_control("/endtask srun_abc confirm")
    assert (apply.operation, apply.value, apply.valid) == ("apply", "srun_abc", True)
    for text in ("/endtask srun_abc now", "/endtask a b c", "/endtask 任务", "/endtask srun_abc confirm again"):
        assert parse_conversation_control(text).valid is False, text
    assert "endtask" in COMMAND_INDEX and match_conversation_command("/endtask x confirm") == ("endtask", "x confirm")


def test_every_end_task_error_code_is_registered():
    # 全仓守卫只扫 error_code="X" 字面量；_REFUSALS 字典里的 4 个码它扫不到，这里一并钉住（9a 复审）
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    codes = {code for code, _template in end_task_control._REFUSALS.values()}
    codes |= {"END_TASK_ADMIN_ONLY", "END_TASK_STATE_CHANGED"}
    assert len(codes) == 6
    assert not [code for code in sorted(codes) if error_contract(code).code != code]


def test_list_and_preview_are_read_only_and_show_structured_facts(tmp_path):
    agent, run_id, _record = _stuck_scheduled_task(tmp_path)

    listing = execute_end_task_control(agent, parse_conversation_control("/endtask"))
    preview = execute_end_task_control(agent, parse_conversation_control(f"/endtask {run_id}"))

    assert listing.ok is True and f"- {run_id}｜" in listing.message and "｜任务 active｜可结束" in listing.message
    assert preview.ok is True and f"确认请发：/endtask {run_id} confirm" in preview.message
    assert "不会停止它启动的后台命令" in preview.message, "预览要如实交代不停后台命令的副作用"
    assert "定时整理" not in listing.message + preview.message, "只渲染结构化事实，不带任务正文"
    assert _state(agent, run_id) == ("active", "waiting")


def test_list_and_preview_show_follow_up_fact_codes(tmp_path):
    agent, run_id, _record = _stuck_scheduled_task(tmp_path)
    thread_id = str(agent.scheduler_repository.get_active_run(run_id)["thread_id"])

    quiet_list = execute_end_task_control(agent, parse_conversation_control("/endtask"))
    quiet_view = execute_end_task_control(agent, parse_conversation_control(f"/endtask {run_id}"))
    assert "｜可结束｜后续工作 无" in quiet_list.message and "后续工作事实：无。" in quiet_view.message

    agent.conversation_store.wakes.raise_signal({"thread_id": thread_id, "root_task_id": run_id,
                                                 "reason": "managed_process_exited", "now": 1_004})
    listing = execute_end_task_control(agent, parse_conversation_control("/endtask"))
    preview = execute_end_task_control(agent, parse_conversation_control(f"/endtask {run_id}"))
    assert "｜可结束｜后续工作 pending_wakes" in listing.message
    assert "后续工作事实：pending_wakes。" in preview.message
    assert "定时整理" not in listing.message + preview.message, "只显示事实码，不带任务正文"
    assert _state(agent, run_id) == ("active", "waiting")


def test_grace_bound_facts_carry_the_fixed_note(tmp_path):
    from agent_py_agent.agent.conversation import task_follow_up

    agent, run_id, _record = _stuck_scheduled_task(tmp_path)
    thread_id = str(agent.scheduler_repository.get_active_run(run_id)["thread_id"])
    agent.conversation_store.goals.create({"thread_id": thread_id, "task_id": run_id, "objective": "持续推进"})
    agent.conversation_store.wakes.raise_signal({"thread_id": thread_id, "root_task_id": run_id,
                                                 "reason": "managed_process_exited", "now": 1_004})

    listing = execute_end_task_control(agent, parse_conversation_control("/endtask"))
    preview = execute_end_task_control(agent, parse_conversation_control(f"/endtask {run_id}"))

    assert "｜后续工作 active_goal（宽限期内才算）、pending_wakes" in listing.message
    assert "后续工作事实：active_goal（宽限期内才算）、pending_wakes。" in preview.message
    assert "pending_wakes（" not in listing.message + preview.message, "会自己推进的事实不加标注"
    assert "持续推进" not in listing.message + preview.message, "只显示事实码，不带 Goal 正文"
    assert end_task_control.GRACE_BOUND_FACTS is task_follow_up.GRACE_BOUND_FACTS, "直接引用判定的常量，不另写一份"


def test_unreadable_follow_up_items_show_their_codes(tmp_path):
    agent, run_id, _record = _stuck_scheduled_task(tmp_path)
    broken = agent.conversation_store.storage.wake_queue_dir / "normal" / "wake-broken.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{truncated", encoding="utf-8")

    preview = execute_end_task_control(agent, parse_conversation_control(f"/endtask {run_id}"))
    listing = execute_end_task_control(agent, parse_conversation_control("/endtask"))

    assert "后续工作事实：无；读不出 pending_wakes:data_parse:JSONDecodeError。" in preview.message
    assert "｜后续工作 无；读不出 pending_wakes:data_parse:JSONDecodeError" in listing.message


def test_follow_up_comes_from_the_injected_scheduler_policy(tmp_path, monkeypatch):
    agent, run_id, _record = _stuck_scheduled_task(tmp_path)
    thread_id = str(agent.scheduler_repository.get_active_run(run_id)["thread_id"])
    policy = agent.scheduler_service.follow_up
    seen = []

    # 函数用途: 记下 /endtask 交给调度判定的查询，再转给原判定，证明两边用的是同一个注入的策略。
    def spy(query):
        seen.append(query)
        return policy.facts(query)

    monkeypatch.setattr(agent.scheduler_service, "follow_up", SchedulerFollowUpPolicy(spy, policy.grace_seconds))
    execute_end_task_control(agent, parse_conversation_control(f"/endtask {run_id}"))
    assert [(query.thread_id, query.task_id, query.ignore_wake_ids) for query in seen] == [(thread_id, run_id, ())]

    monkeypatch.setattr(agent.scheduler_service, "follow_up", None)
    listing = execute_end_task_control(agent, parse_conversation_control("/endtask"))
    assert "｜可结束｜后续工作 判定不可用" in listing.message


def test_confirm_ends_the_task_and_unblocks_the_job(tmp_path):
    agent, run_id, _record = _stuck_scheduled_task(tmp_path)
    repository = agent.scheduler_repository
    assert repository.reserve_due_runs(now=1_610) == [], "waiting 运行挡住了同一 job 的到期派发（事故形态）"

    result = execute_end_task_control(agent, parse_conversation_control(f"/endtask {run_id} confirm"))

    assert result.ok is True and "已结算" in result.message and "不会被重做" in result.message
    assert "不会停止它启动的后台命令" in result.message, "确认结果也要交代后台命令的通知会落到已取消的任务上"
    assert str(agent.conversation_store.tasks.load(run_id).status) == "cancelled"
    assert repository.get_active_run(run_id) is None
    history, errors = repository.history(limit=5)
    assert errors == [] and [row["status"] for row in history] == ["cancelled"]
    reserved = repository.reserve_due_runs(now=1_610)
    assert len(reserved) == 1 and reserved[0]["job_id"] == history[0]["job_id"], "同一 job 恢复按计划派发"


def test_refusals_change_nothing(tmp_path):
    agent, run_id, record = _stuck_scheduled_task(tmp_path / "live", attempt_running=True)
    live = execute_end_task_control(agent, parse_conversation_control(f"/endtask {run_id} confirm"))
    assert live.ok is False and live.error_code == "END_TASK_LIVE_ATTEMPT" and record["attempt_id"] in live.message
    assert _state(agent, run_id) == ("active", "waiting")
    listing = execute_end_task_control(agent, parse_conversation_control("/endtask"))
    assert f"- {run_id}｜" in listing.message and "｜还有执行在跑" in listing.message

    # 非管理员 owner 也带着真实存储：拦住它的必须是管理员判定本身，而不是缺少存储
    guest = SimpleNamespace(**{name: getattr(agent, name) for name in (
        "conversation_store", "scheduler_repository", "scheduler_service", "subagents")},
        home_paths=SimpleNamespace(owner_provider="feishu", owner_kind="user"))
    agent.subagents.runtime_db.settle_agent_attempt(agent_run_id=record["agent_run_id"], attempt_id=record["attempt_id"])
    refused = execute_end_task_control(guest, parse_conversation_control(f"/endtask {run_id} confirm"))
    assert refused.ok is False and refused.error_code == "END_TASK_ADMIN_ONLY"
    assert _state(agent, run_id) == ("active", "waiting")

    blind = SimpleNamespace(**{name: getattr(agent, name) for name in (
        "home_paths", "conversation_store", "scheduler_repository", "scheduler_service")},
        subagents=SimpleNamespace(runtime_db=None))
    unknown = execute_end_task_control(blind, parse_conversation_control(f"/endtask {run_id} confirm"))
    assert unknown.ok is False and unknown.error_code == "END_TASK_RUNTIME_UNAVAILABLE"

    def locked(_task_id):
        raise sqlite3.OperationalError("database is locked")

    blind.subagents = SimpleNamespace(runtime_db=SimpleNamespace(task_runs_for_task=locked))
    unreadable = execute_end_task_control(blind, parse_conversation_control(f"/endtask {run_id} confirm"))
    assert unreadable.ok is False and unreadable.error_code == "END_TASK_RUNTIME_UNAVAILABLE"
    assert _state(agent, run_id) == ("active", "waiting")

    other = execute_end_task_control(agent, parse_conversation_control("/endtask task-not-scheduled confirm"))
    assert other.ok is False and other.error_code == "END_TASK_NOT_WAITING_RUN"

    agent.conversation_store.tasks.update_status({"task_id": run_id, "status": "completed"})
    inactive = execute_end_task_control(agent, parse_conversation_control(f"/endtask {run_id} confirm"))
    assert inactive.ok is False and inactive.error_code == "END_TASK_NOT_ACTIVE"
    assert _state(agent, run_id) == ("completed", "waiting"), "拒绝时不替别的收口路径结算"


def test_confirm_does_not_overwrite_a_status_that_changed_after_the_check(tmp_path):
    agent, run_id, _record = _stuck_scheduled_task(tmp_path)
    facts = end_task_control._task_facts(agent, run_id)
    assert facts.refusal == ""
    # 核对之后、写入之前任务被别的路径收口：CAS 只在仍是 active 时改，不覆盖别人的终态
    agent.conversation_store.tasks.update_status({"task_id": run_id, "status": "completed"})

    raced = end_task_control._end_task(agent, facts)

    assert raced.ok is False and raced.error_code == "END_TASK_STATE_CHANGED"
    assert _state(agent, run_id) == ("completed", "waiting")


def test_gateway_dispatch_and_tui_serialization(tmp_path, monkeypatch):
    agent, run_id, _record = _stuck_scheduled_task(tmp_path)
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _base, _scope: agent)
    scope = control_service.GatewayControlScope(user_id="local-agent", channel="feishu", conversation_id="chat-1")

    result = control_service.execute_gateway_conversation_control(
        object(), gateway_paths_from_root(tmp_path), parse_conversation_control(f"/endtask {run_id} confirm"), scope,
    )

    assert result.ok is True and result.kind == "endtask"
    assert _state(agent, run_id)[0] == "cancelled"
    assert control_runtime._command_text(parse_conversation_control("/endtask")) == "/endtask"
    assert control_runtime._command_text(parse_conversation_control("/endtask srun_x")) == "/endtask srun_x"
    confirm = parse_conversation_control("/endtask srun_x confirm")
    assert control_runtime._command_text(confirm) == "/endtask srun_x confirm"
    execution = control_runtime.ChatControlExecution(
        agent=SimpleNamespace(config=SimpleNamespace()),
        use_gateway=False,
        state=control_runtime.ChatControlState(
            running=False, queued_count=0, prompt="", started_at=0.0, session_id="sess-local",
        ),
    )
    local = control_runtime.execute_chat_control(execution, confirm)
    assert local.ok is False and "Gateway" in local.message
