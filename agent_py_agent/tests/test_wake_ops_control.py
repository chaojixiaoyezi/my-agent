"""唤醒毒丸第 4 步：管理员 /wakes 命令（TUI 与飞书共用 Gateway 控制入口）与状态面计数。

钉住：
1. 解析：/wakes、/wakes quarantined 是列表，/wakes replay <ID> 是只读预览，带 confirm 才重放，其它写法无效；
2. 只给管理员；列表与预览只读、只渲染结构化字段，原因码按开放集合显示（已知码带中文说明，认不出的只显示原码）；
3. 确认重放按原 ID 和冻结内容放回待处理队列、结案记录留档到 replayed/、打 wake_replayed 事件；
4. 不存在、已归档、待处理队列已有同 ID、领域已结束（会话任务终态、会话消息回执 consumed/rejected）都拒绝且不改文件；
5. /status 与 gateway_status 显示本 owner 已结案唤醒条数；Gateway 分派、TUI 转发文本与本地模式拒绝。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.command_catalog import COMMAND_INDEX, match_conversation_command
from agent_py_agent.agent.conversation.control_commands import (
    ConversationTaskStatus,
    parse_conversation_control,
    render_conversation_task_status,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.agent.conversation.store_wake_attempts import (
    WAKE_REPLAY_ARCHIVED,
    WAKE_REPLAY_DOMAIN_TERMINAL,
    WAKE_REPLAY_NOT_FOUND,
    WAKE_REPLAY_PENDING_CONFLICT,
    WakeAttemptStart,
)
from agent_py_agent.agent.conversation.store_wake_quarantine_archive import (
    archive_stale_wake_quarantine,
)
from agent_py_agent.agent.conversation.wake_poison import (
    WAKE_POISON_SAME_CAUSE_LIMIT,
    WAKE_VERDICT_FAILURE,
    WakeAttemptVerdict,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import control_service, wake_ops_control
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.wake_ops_control import execute_wake_ops_control
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_parts import control_runtime

QUARANTINED_AT = 1_000_000.0


# LLM: 只在 pytest 临时目录里建管理员 owner 的 SimpleAgent 与一个会话；不启动模型、不发网络请求。
# 函数用途: 返回 agent、会话存储与目标会话 ID。
def _agent(tmp_path):
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0), tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "thread-wakes",
        "channel_user_id": "user-1", "now": 900.0,
    })
    return agent, store, thread.thread_id


# LLM: 用真实尝试账连续记同因失败到上限，再按判定结案；wake 给出 reason 与 metadata（默认一条找不到任务的会话任务唤醒），
#   code 是结案原因码（可以是认不出的新码，验证开放集合显示）。
# 函数用途: 造一条已结案的唤醒，返回原信号。
def _quarantined(store, thread_id: str, *, wake=None, code="error:SKILL_TASK_BINDING_INVALID"):
    wake = wake or {"reason": "session_task", "metadata": {"session_task_id": "stask-missing"}}
    signal = store.wakes.raise_signal({
        "thread_id": thread_id, "reason": wake["reason"], "summary": "派活正文摘要，不应出现在列表里",
        "metadata": wake["metadata"], "now": 10.0,
    })
    verdict = WakeAttemptVerdict(WAKE_VERDICT_FAILURE, code)
    decision = None
    for attempt in range(WAKE_POISON_SAME_CAUSE_LIMIT):
        store.wakes.attempts.begin(signal, WakeAttemptStart(f"claim-{attempt}"), now=100.0 + attempt)
        decision = store.wakes.attempts.record(signal, verdict, now=100.5 + attempt, error=RuntimeError("boom")).decision
    store.wakes.attempts.quarantine(signal.wake_signal_id, decision, now=QUARANTINED_AT)
    return signal


def _run(agent, text: str):
    return execute_wake_ops_control(agent, parse_conversation_control(text))


def test_wakes_parses_list_view_apply_and_rejects_other_forms():
    for text in ("/wakes", "/wakes quarantined", "/wakes QUARANTINED"):
        command = parse_conversation_control(text)
        assert (command.kind, command.operation, command.valid) == ("wakes", "list", True), text
    view = parse_conversation_control("/wakes replay wake-abc")
    assert (view.operation, view.value, view.valid) == ("view", "wake-abc", True)
    apply = parse_conversation_control("/wakes replay wake-abc confirm")
    assert (apply.operation, apply.value, apply.valid) == ("apply", "wake-abc", True)
    for text in ("/wakes replay", "/wakes replay wake-abc now", "/wakes replay 唤醒", "/wakes drop wake-abc",
                 "/wakes replay wake-abc confirm again", "/wakes quarantined extra"):
        assert parse_conversation_control(text).valid is False, text
    assert "wakes" in COMMAND_INDEX and match_conversation_command("/wakes replay x confirm") == ("wakes", "replay x confirm")


def test_every_wakes_error_code_is_registered():
    # 全仓守卫只扫 error_code="X" 字面量；_REFUSALS 字典里的码它扫不到，这里一并钉住。
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    codes = set(wake_ops_control._REFUSALS) | {wake_ops_control._ADMIN_ONLY}
    assert len(codes) == 6
    assert not [code for code in sorted(codes) if error_contract(code).code != code]


def test_only_admins_can_list_or_replay(tmp_path, monkeypatch):
    agent, store, thread_id = _agent(tmp_path)
    signal = _quarantined(store, thread_id)
    monkeypatch.setattr(wake_ops_control, "is_permission_admin", lambda _paths: False)

    for text in ("/wakes", f"/wakes replay {signal.wake_signal_id} confirm"):
        result = _run(agent, text)
        assert (result.ok, result.error_code) == (False, "WAKE_OPS_ADMIN_ONLY"), text
    assert store.storage.wake_quarantine_path(signal.wake_signal_id).exists()


def test_list_shows_structured_rows_with_open_reason_codes(tmp_path):
    agent, store, thread_id = _agent(tmp_path)
    assert _run(agent, "/wakes").message == "没有已结案的后台唤醒。"
    known = _quarantined(store, thread_id)
    novel = _quarantined(store, thread_id, code="brand:new_code")
    broken = store.wakes.raise_signal({"thread_id": thread_id, "reason": "session_task", "summary": "s", "now": 11.0})
    decision = _decision(store, broken)
    store.storage.wake_signal_path(broken).write_bytes(b"{broken")
    store.wakes.attempts.quarantine(broken.wake_signal_id, decision, now=QUARANTINED_AT)

    listing = _run(agent, "/wakes quarantined")

    assert listing.ok is True
    assert f"- {known.wake_signal_id}｜会话 {thread_id}｜session_task｜error:SKILL_TASK_BINDING_INVALID（执行时出错）｜" \
        in listing.message
    assert f"- {novel.wake_signal_id}｜" in listing.message and "｜brand:new_code｜" in listing.message
    assert "同因 5 次、共 5 次" in listing.message and "已重放 0 次" in listing.message
    assert "另有 1 条读不出" in listing.message
    assert "派活正文摘要" not in listing.message, "只渲染结构化字段，不带摘要"


# 函数用途: 给一条唤醒连续记同因失败到上限，返回结案判定。
def _decision(store, signal):
    decision = None
    for attempt in range(WAKE_POISON_SAME_CAUSE_LIMIT):
        decision = store.wakes.attempts.record(
            signal, WakeAttemptVerdict(WAKE_VERDICT_FAILURE, "run:no_report"), now=13.0 + attempt).decision
    return decision


def test_preview_is_read_only_and_confirm_replays_with_an_event(tmp_path, capsys):
    agent, store, thread_id = _agent(tmp_path)
    signal = _quarantined(store, thread_id)

    preview = _run(agent, f"/wakes replay {signal.wake_signal_id}")
    assert preview.ok is True and f"确认请输入 /wakes replay {signal.wake_signal_id} confirm" in preview.message
    assert "结案原因：error:SKILL_TASK_BINDING_INVALID（执行时出错）" in preview.message
    assert store.wakes.pending(limit=0) == [] and store.storage.wake_quarantine_path(signal.wake_signal_id).exists()

    result = _run(agent, f"/wakes replay {signal.wake_signal_id} confirm")

    assert result.ok is True and "第 1 次重放" in result.message
    assert [item.wake_signal_id for item in store.wakes.pending(limit=0)] == [signal.wake_signal_id]
    assert (store.storage.wake_replayed_dir / signal.wake_signal_id / "1.json").exists()
    assert not store.storage.wake_quarantine_path(signal.wake_signal_id).exists()
    events = [json.loads(line.split(" ", 1)[1]) for line in capsys.readouterr().out.splitlines()
              if line.startswith("[background-wake-poison] ")]
    assert events == [{"event": "wake_replayed", "wake_signal_id": signal.wake_signal_id, "thread_id": thread_id,
                       "reason": "session_task", "replay_count": 1}]


def test_refusals_change_nothing(tmp_path):
    agent, store, thread_id = _agent(tmp_path)
    archived = _quarantined(store, thread_id)
    archive_stale_wake_quarantine(store.storage, current=QUARANTINED_AT + 30 * 86400.0)
    conflicted = _quarantined(store, thread_id)
    store.storage.wake_signal_path(conflicted).write_text(json.dumps(conflicted.to_dict()), encoding="utf-8")

    cases = {
        "wake-does-not-exist": WAKE_REPLAY_NOT_FOUND,
        archived.wake_signal_id: WAKE_REPLAY_ARCHIVED,
        conflicted.wake_signal_id: WAKE_REPLAY_PENDING_CONFLICT,
    }
    for wake_id, code in cases.items():
        for text in (f"/wakes replay {wake_id}", f"/wakes replay {wake_id} confirm"):
            result = _run(agent, text)
            assert (result.ok, result.error_code) == (False, code), text
    assert store.storage.wake_quarantine_path(conflicted.wake_signal_id).exists()
    assert store.storage.wake_quarantine_archive_path(archived.wake_signal_id).exists()
    assert not (store.storage.wake_replayed_dir / conflicted.wake_signal_id).exists()


def test_domain_terminal_session_task_and_message_are_refused(tmp_path, monkeypatch):
    agent, store, thread_id = _agent(tmp_path)
    task = store.session_tasks.create(SessionTaskDraft(
        sender_thread_id="thread-sender", target_thread_id=thread_id, goal="派活", dedupe_key="stask-key"))
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="cancelled"))
    ended_task = _quarantined(store, thread_id, wake={"reason": "session_task", "metadata": {"session_task_id": task.task_id}})
    consumed = _quarantined(store, thread_id, wake={"reason": "session_message", "metadata": {"message_dedupe_key": "msg-1"}})
    keyless = _quarantined(store, thread_id, wake={"reason": "session_message", "metadata": {}})
    monkeypatch.setattr(store.guidance, "receipt", lambda key: SimpleNamespace(status="consumed") if key == "msg-1" else None)

    task_refusal = _run(agent, f"/wakes replay {ended_task.wake_signal_id} confirm")
    message_refusal = _run(agent, f"/wakes replay {consumed.wake_signal_id} confirm")

    assert (task_refusal.ok, task_refusal.error_code) == (False, WAKE_REPLAY_DOMAIN_TERMINAL)
    assert "会话任务已是 cancelled" in task_refusal.message
    assert (message_refusal.ok, message_refusal.error_code) == (False, WAKE_REPLAY_DOMAIN_TERMINAL)
    assert "会话消息回执已是 consumed" in message_refusal.message
    assert store.wakes.pending(limit=0) == []
    # 没带消息键的旧唤醒查不到回执：按未结束处理，允许重放（重放后由消费判定自己收口）。
    assert _run(agent, f"/wakes replay {keyless.wake_signal_id} confirm").ok is True


def test_preview_of_a_domain_terminal_wake_is_refused_without_changes(tmp_path, monkeypatch):
    # 只读预览（不带 confirm）同样先核对领域终态：事已结束就直接拒绝，不给出"确认请输入"的提示，也不改任何文件。
    agent, store, thread_id = _agent(tmp_path)
    consumed = _quarantined(store, thread_id, wake={"reason": "session_message", "metadata": {"message_dedupe_key": "msg-1"}})
    monkeypatch.setattr(store.guidance, "receipt", lambda key: SimpleNamespace(status="consumed") if key == "msg-1" else None)
    record = store.storage.wake_quarantine_path(consumed.wake_signal_id)
    before = record.read_bytes()

    preview = _run(agent, f"/wakes replay {consumed.wake_signal_id}")

    assert (preview.ok, preview.error_code) == (False, WAKE_REPLAY_DOMAIN_TERMINAL)
    assert "会话消息回执已是 consumed" in preview.message and "confirm" not in preview.message
    assert record.read_bytes() == before and store.wakes.pending(limit=0) == []


def test_status_and_gateway_status_show_the_quarantined_count(tmp_path):
    agent, store, thread_id = _agent(tmp_path)
    _quarantined(store, thread_id)
    _quarantined(store, thread_id)

    assert control_service._quarantined_wake_count(agent) == 2
    rendered = render_conversation_task_status(ConversationTaskStatus(quarantined_wakes=2))
    assert "已结案的后台唤醒：2 条（反复失败、不再自动领取；管理员可用 /wakes 查看）" in rendered
    assert "已结案的后台唤醒" not in render_conversation_task_status(ConversationTaskStatus())
    parsed = control_runtime._conversation_task_status_from_body({"state": "idle", "quarantined_wakes": 2})
    assert parsed is not None and parsed.quarantined_wakes == 2
    from agent_py_agent.agent.tooling.gateway_status import _quarantined_wake_count

    assert _quarantined_wake_count(agent) == 2
    assert _quarantined_wake_count(SimpleNamespace()) is None


def test_gateway_dispatch_and_tui_serialization(tmp_path, monkeypatch):
    agent, store, thread_id = _agent(tmp_path)
    signal = _quarantined(store, thread_id)
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _base, _scope: agent)
    scope = control_service.GatewayControlScope(
        user_id="local-agent", channel="feishu", conversation_id="chat-1", resolved_owner=OwnerIdentity.local_main(),
    )

    result = control_service.execute_gateway_conversation_control(
        object(), gateway_paths_from_root(tmp_path),
        parse_conversation_control(f"/wakes replay {signal.wake_signal_id} confirm"), scope,
    )

    assert result.ok is True and result.kind == "wakes"
    _quarantined(store, thread_id)
    status = control_service.execute_gateway_conversation_control(
        object(), gateway_paths_from_root(tmp_path), parse_conversation_control("/status"), scope,
    )
    assert status.ok is True and status.status is not None and status.status.quarantined_wakes == 1
    assert "已结案的后台唤醒：1 条" in status.message
    assert control_runtime._command_text(parse_conversation_control("/wakes quarantined")) == "/wakes"
    assert control_runtime._command_text(parse_conversation_control("/wakes replay w-1")) == "/wakes replay w-1"
    confirm = parse_conversation_control("/wakes replay w-1 confirm")
    assert control_runtime._command_text(confirm) == "/wakes replay w-1 confirm"
    execution = control_runtime.ChatControlExecution(
        agent=SimpleNamespace(config=SimpleNamespace()),
        use_gateway=False,
        state=control_runtime.ChatControlState(
            running=False, queued_count=0, prompt="", started_at=0.0, session_id="sess-local",
        ),
    )
    local = control_runtime.execute_chat_control(execution, confirm)
    assert local.ok is False and "Gateway" in local.message
