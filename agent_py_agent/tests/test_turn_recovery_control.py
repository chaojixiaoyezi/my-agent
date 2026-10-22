"""/recover 会话控制：未知执行轮的查看与显式恢复。

钉住四件事：
1. 解析只认结构化处置值，无参数是只读查看。
2. 查看只列结构化操作事实，不改运行库。
3. 显式处置走唯一出口 recover_attempt_unknown，解除阻塞后下一轮可以挂载。
4. 被 unknown 拒绝挂载时抛带错误码的异常，客户端文案指向 /recover。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams, RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import _base_runtime_injections
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.gateway_parts.turn_recovery_control import execute_turn_recovery_control
from agent_py_agent.agent.runtime_db.operations import (
    RuntimeConflictError,
    RuntimeRecoveryRequiredError,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.cli.chat_parts import control_runtime

_DEAD_PID = 999999999


def _dead_pid_or_skip() -> int:
    try:
        os.kill(_DEAD_PID, 0)
    except ProcessLookupError:
        return _DEAD_PID
    pytest.skip("测试用 pid 竟然存活")


def _insert_operation(repo, rec, *, status: str, started_at: float, tool: str = "run_command") -> str:
    operation_id = f"tool_operation:{uuid.uuid4().hex}"
    now = time.time()
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,1,1,?,?,?,?,?)",
            (operation_id, rec["agent_run_id"], rec["attempt_id"], tool, status, started_at, now, now),
        )
    return operation_id


def _unknown_turn(tmp_path, task_id: str = "task-recover"):
    """造一条执行中断的主代理执行轮：一条已成功操作 + 一条执行中操作，再按进程死亡归 unknown。"""
    repo = RuntimeRepository(str(tmp_path / f"runtime-{uuid.uuid4().hex[:8]}.db"))
    rec = repo.record_run_creation(
        owner_id="local/main",
        goal="排查通道",
        conversation_task_id=task_id,
        thread_id="thread-recover",
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        role="main",
    )
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_attempts SET metadata_json = ? WHERE attempt_id = ?",
            (json.dumps({"runner_pid": _dead_pid_or_skip(), "runner_start_time": None}), rec["attempt_id"]),
        )
    _insert_operation(repo, rec, status="SUCCEEDED", started_at=time.time() - 60)
    executing = _insert_operation(repo, rec, status="EXECUTING", started_at=time.time() - 5)
    assert repo.recover_stale_attempts() == [rec["agent_run_id"]]
    owner = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    thread = SimpleNamespace(workspace_task_id=task_id)
    return repo, rec, owner, thread, executing


def _status(repo, rec) -> tuple[str, str]:
    with repo._runtime_connection() as conn:
        run = conn.execute("SELECT status FROM agent_runs WHERE agent_run_id=?", (rec["agent_run_id"],)).fetchone()
        attempt = conn.execute("SELECT status FROM agent_attempts WHERE attempt_id=?", (rec["attempt_id"],)).fetchone()
    return str(run["status"]), str(attempt["status"])


def test_recover_parses_view_apply_and_rejects_free_text():
    view = parse_conversation_control("/recover")
    assert (view.kind, view.operation, view.valid) == ("recover", "view", True)
    applied = parse_conversation_control("/recover  RECORDED ")
    assert (applied.kind, applied.operation, applied.value, applied.valid) == ("recover", "apply", "recorded", True)
    for value in ("confirmed_noop", "abandoned"):
        assert parse_conversation_control(f"/recover {value}").valid is True
    rejected = parse_conversation_control("/recover 已经好了")
    assert rejected.kind == "recover" and rejected.valid is False
    assert "recorded|confirmed_noop|abandoned" in rejected.usage

    targeted = parse_conversation_control("/recover recorded subagent-1790930000-deadbeef")
    assert targeted.valid is True and targeted.operation == "apply"
    assert targeted.value == "recorded"
    assert getattr(targeted, "target_id", "") == "subagent-1790930000-deadbeef"
    assert parse_conversation_control("/recover recorded ../other").valid is False

    owner_view = parse_conversation_control("/recover owner")
    assert owner_view.valid is True and owner_view.operation == "owner_view"
    owner_preview = parse_conversation_control("/recover owner abandoned")
    assert owner_preview.valid is True and owner_preview.operation == "owner_preview"
    owner_apply = parse_conversation_control("/recover owner abandoned --confirm 0123456789ab")
    assert owner_apply.valid is True and owner_apply.operation == "owner_apply"
    assert getattr(owner_apply, "confirmation_code", "") == "0123456789ab"
    assert parse_conversation_control("/recover owner abandoned --confirm not-hex").valid is False


def test_view_lists_unsettled_operations_without_changing_state(tmp_path):
    repo, rec, owner, thread, _executing = _unknown_turn(tmp_path)

    result = execute_turn_recovery_control(owner, thread, parse_conversation_control("/recover"))

    assert result.ok is True and result.kind == "recover"
    assert "run_command｜执行中断，结果未回传" in result.message
    assert result.message.count("run_command") == 1, "已成功的操作不算未确认"
    assert "/recover recorded" in result.message and "/recover abandoned" in result.message
    assert _status(repo, rec) == ("unknown", "unknown")


def test_mount_is_refused_with_typed_code_until_explicit_recovery(tmp_path):
    repo, rec, owner, thread, executing = _unknown_turn(tmp_path)
    with pytest.raises(RuntimeRecoveryRequiredError) as refused:
        repo.create_attempt(rec["agent_run_id"])
    assert isinstance(refused.value, RuntimeConflictError)
    assert refused.value.error_code == "RUN_RECOVERY_REQUIRED"
    assert "/recover" in gateway_client_error_message("RUN_RECOVERY_REQUIRED")
    assert "/recover" in gateway_client_error_message("ACTIVE_TURN_OUTCOME_UNCERTAIN")

    result = execute_turn_recovery_control(owner, thread, parse_conversation_control("/recover recorded"))

    assert result.ok is True and "已核实生效并记下" in result.message
    assert _status(repo, rec) == ("created", "recovered")
    assert repo.main_agent_recovery_block_for_task("task-recover") is None
    with repo._runtime_connection() as conn:
        event = conn.execute(
            "SELECT payload_json FROM runtime_events WHERE event_type='attempt_recovered' AND attempt_id=?",
            (rec["attempt_id"],),
        ).fetchone()
    payload = json.loads(event["payload_json"])
    assert payload["operator"] == "conversation-control:/recover"
    assert payload["effect_disposition"] == "recorded"
    next_attempt = repo.create_attempt(rec["agent_run_id"])
    assert next_attempt["attempt_id"] != rec["attempt_id"]
    again = execute_turn_recovery_control(owner, thread, parse_conversation_control("/recover recorded"))
    assert again.ok is True and "无需恢复" in again.message
    assert executing not in again.message


def test_nothing_to_recover_without_thread_task_or_block(tmp_path):
    repo, _rec, owner, _thread, _executing = _unknown_turn(tmp_path)
    view = parse_conversation_control("/recover")
    assert "无需恢复" in execute_turn_recovery_control(owner, None, view).message
    other = SimpleNamespace(workspace_task_id="task-without-runs")
    assert "无需恢复" in execute_turn_recovery_control(owner, other, view).message
    assert repo.main_agent_recovery_block_for_task("task-recover") is not None


def test_block_that_is_not_an_unknown_attempt_is_refused(tmp_path):
    repo, rec, owner, thread, _executing = _unknown_turn(tmp_path)
    with repo.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='running', ended_at=0 WHERE attempt_id=?", (rec["attempt_id"],))

    result = execute_turn_recovery_control(owner, thread, parse_conversation_control("/recover abandoned"))

    assert result.ok is False and result.error_code == "RUN_RECOVERY_REJECTED"
    assert _status(repo, rec) == ("unknown", "running")


def test_gateway_dispatch_resolves_scope_thread_for_recover(tmp_path, monkeypatch):
    repo, rec, owner, thread, _executing = _unknown_turn(tmp_path)
    seen: dict[str, object] = {}

    def resolve(**kwargs):
        seen.update(kwargs)
        return thread

    owner.conversation_store = SimpleNamespace(threads=SimpleNamespace(resolve=resolve))
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _base, _scope: owner)
    scope = control_service.GatewayControlScope(user_id="local-agent", channel="chat", conversation_id="sess-1")

    result = control_service.execute_gateway_conversation_control(
        object(), gateway_paths_from_root(tmp_path), parse_conversation_control("/recover confirmed_noop"), scope,
    )

    assert result.ok is True and "已核实没有生效" in result.message
    assert seen == {"channel": "chat", "channel_conversation_id": "sess-1", "channel_user_id": "local-agent"}
    assert _status(repo, rec) == ("created", "recovered")


def test_tui_serializes_recover_and_local_mode_requires_gateway():
    assert control_runtime._command_text(parse_conversation_control("/recover")) == "/recover"
    applied = parse_conversation_control("/recover abandoned")
    assert control_runtime._command_text(applied) == "/recover abandoned"
    targeted = parse_conversation_control("/recover recorded subagent-1790930000-deadbeef")
    assert control_runtime._command_text(targeted) == "/recover recorded subagent-1790930000-deadbeef"
    owner_preview = parse_conversation_control("/recover owner confirmed_noop")
    assert control_runtime._command_text(owner_preview) == "/recover owner confirmed_noop"
    owner_apply = parse_conversation_control("/recover owner confirmed_noop --confirm 0123456789ab")
    assert control_runtime._command_text(owner_apply) == "/recover owner confirmed_noop --confirm 0123456789ab"
    restored = control_runtime._gateway_control_result_from_body(
        "recover",
        {"kind": "recover", "ok": True, "message": "ok",
         "details": {"scope": "owner", "target_count": 2, "success_count": 2}},
    )
    assert restored.details == {"scope": "owner", "target_count": 2, "success_count": 2}
    execution = control_runtime.ChatControlExecution(
        agent=SimpleNamespace(config=SimpleNamespace()),
        use_gateway=False,
        state=control_runtime.ChatControlState(
            running=False, queued_count=0, prompt="", started_at=0.0, session_id="sess-local",
        ),
    )
    local = control_runtime.execute_chat_control(execution, applied)
    assert local.ok is False and "Gateway" in local.message


# LLM: record_run_creation回执不包含run_id，须从真实agent_run行读出，不能凭返回协议猜字段。
# 函数用途: 为新绑定回归补齐原未知执行夹具的真实运行编号。
def _opt7_unknown_turn(tmp_path):
    repo, rec, owner, thread, operation = _unknown_turn(tmp_path)
    rec = {**rec, "run_id": str(repo.get_agent_run(rec["agent_run_id"])["run_id"])}
    return repo, rec, owner, thread, operation


# LLM: 简写只在显式/recover中归一化；编号、owner预览和确认码仍沿原解析与权限边界。
# 函数用途: 覆盖全部中文/数字别名及大小写兼容，不让普通聊天变成控制命令。
@pytest.mark.parametrize("alias,disposition", [
    ("1", "recorded"), ("已生效", "recorded"), ("生效", "recorded"),
    ("2", "confirmed_noop"), ("未生效", "confirmed_noop"), ("没生效", "confirmed_noop"),
    ("noop", "confirmed_noop"), ("NOOP", "confirmed_noop"),
    ("3", "abandoned"), ("继续", "abandoned"), ("跳过", "abandoned"),
    ("不再核对", "abandoned"), ("auto", "abandoned"), ("AUTO", "abandoned"),
])
@pytest.mark.parametrize("template,operation", [
    ("/recover {alias}", "apply"),
    ("/recover {alias} subagent-1790930000-deadbeef", "apply"),
    ("/recover owner {alias}", "owner_preview"),
    ("/recover owner {alias} --confirm 0123456789ab", "owner_apply"),
])
def test_opt7_friendly_recover_aliases(alias, disposition, template, operation):
    command = parse_conversation_control(template.format(alias=alias))
    assert command.valid and command.kind == "recover"
    assert (command.value, command.operation) == (disposition, operation)
    assert parse_conversation_control(alias) is None
    assert not parse_conversation_control(f"/recover {alias} ../other").valid
    assert not parse_conversation_control(f"/recover owner {alias} --confirm bad").valid


# LLM: 旧英文处置行必须原样保留，新增提示只帮助用户选择新消息接手或简写，不隐式写运行库。
# 函数用途: 验证/recover查看兼顾旧协议和新中文引导。
def test_opt7_recover_view_explains_foreground_and_shortcuts(tmp_path):
    repo, rec, owner, thread, _operation = _unknown_turn(tmp_path)
    result = execute_turn_recovery_control(owner, thread, parse_conversation_control("/recover"))
    for disposition in ("recorded", "confirmed_noop", "abandoned"):
        assert f"/recover {disposition}" in result.message
    assert "也可直接发送新消息由助手自动接手核实" in result.message
    assert "/recover 1、/recover 2、/recover 3、/recover 继续" in result.message
    assert _status(repo, rec) == ("unknown", "unknown")


# LLM: 使用真实RuntimeRepository与绑定入口；核对原attempt审计、新attempt身份及真实模型注入列表，不重放工具。
# 函数用途: 验证用户新回合自动解阻后先获得只读核实提示，既有注入和宿主回调引用不丢失。
def test_opt7_foreground_recovers_and_injects_verification_before_model(tmp_path, monkeypatch):
    repo, rec, owner, _thread, operation = _opt7_unknown_turn(tmp_path)
    old_operations = repo.unsettled_attempt_operations(rec["attempt_id"])
    original = repo.unsettled_attempt_operations

    # LLM: 只增加不得进入模型的噪声字段；操作名、状态和开始时间仍来自真实仓库读口。
    # 函数用途: 检查核实提示不泄露工具参数、结果、路径或操作编号。
    def unsettled(attempt_id):
        return [{**item, "arguments": "PRIVATE_ARGUMENTS", "result": "PRIVATE_RESULT"}
                for item in original(attempt_id)]

    monkeypatch.setattr(repo, "unsettled_attempt_operations", unsettled)
    captured = []
    callback = SimpleNamespace(bind_runtime_authority=lambda binding: captured.append(binding) or True)
    params = RunParams(
        source="gateway", run_id=rec["run_id"], task_id="task-recover", request_id="followup",
        inject=["原有注入"], conversation_task_binding_callback=callback,
    )
    params.compact_continuation_injection = "已有恢复提示"
    bound = runtime_mixin._bind_main_agent_authority(owner, params)
    assert repo.get_attempt(rec["attempt_id"])["status"] == "recovered"
    assert bound.attempt_id != rec["attempt_id"]
    assert repo.get_attempt(bound.attempt_id)["status"] == "running"
    assert bound.run_id == rec["run_id"] and bound.request_id == "followup"
    assert bound.conversation_task_binding_callback is callback
    assert captured[-1]["attempt_id"] == bound.attempt_id
    notice = bound.compact_continuation_injection
    assert notice.startswith("已有恢复提示\n\n【系统恢复提示】")
    assert "run_command" in notice and "只读" in notice and "不会自动重做" in notice
    for item in old_operations:
        assert item["status"] in notice and str(item["handler_started_at"]) in notice
    assert "PRIVATE_ARGUMENTS" not in notice and "PRIVATE_RESULT" not in notice and operation not in notice
    request = RuntimeContextRequest("继续", bound.inject, None)
    injections = _base_runtime_injections(request, "", SimpleNamespace(injected_sections=[]))
    assert "原有注入" in injections and notice in injections
    assert params.inject == ["原有注入"] and params.compact_continuation_injection == "已有恢复提示"
    continued = runtime_mixin._compact_auto_continue_params(
        bound, "# Compact Auto Continuation\n续接", SimpleNamespace(archive_tool_calls=[]),
    )
    assert continued.compact_continuation_injection == notice and continued.inject.count(notice) == 1
    compact_bound = runtime_mixin._bind_main_agent_authority(owner, continued)
    assert compact_bound.inject.count(notice) == 1 and compact_bound.attempt_id != bound.attempt_id
    with repo._runtime_connection() as conn:
        events = conn.execute("SELECT payload_json FROM runtime_events WHERE event_type='attempt_recovered'").fetchall()
        assert conn.execute("SELECT count(*) FROM tool_operations").fetchone()[0] == 2
    assert len(events) == 1
    event = json.loads(events[0]["payload_json"])
    assert event["operator"] == "foreground-user-turn" and event["effect_disposition"] == "abandoned"
    assert event["reason"] == "用户发起新回合自动解除未知阻塞并交由模型核实"


# LLM: source、普通技术续跑和真实Compact深度都是结构化门，不能靠“继续”正文解锁后台或压缩片。
# 函数用途: 保持非前台入口的unknown拒绝、状态与审计不变。
@pytest.mark.parametrize("source,sequence,depth", [
    ("background_main_agent", 0, 0), ("run", 0, 0), ("", 0, 0),
    ("gateway", 1, 0), ("gateway", 0, 1),
])
def test_opt7_nonforeground_unknown_stays_blocked(tmp_path, source, sequence, depth):
    repo, rec, owner, _thread, _operation = _opt7_unknown_turn(tmp_path)
    params = RunParams(source=source, continuation_seq=sequence, compact_auto_continue_depth=depth,
                       run_id=rec["run_id"], task_id="task-recover", root_user_prompt="继续")
    with pytest.raises(RuntimeRecoveryRequiredError):
        runtime_mixin._bind_main_agent_authority(owner, params)
    assert _status(repo, rec) == ("unknown", "unknown")
    assert len(repo.attempts_for_run(rec["agent_run_id"])) == 1


# LLM: 准备真实子链与未确认操作，再用SQL仅模拟进程死亡状态；不调用恢复API代替待测路径。
# 函数用途: 为本线程和别线程的未知子代理提供明确身份夹具。
def _opt7_unknown_child(repo, parent_run_id, run_id, tool="write_file"):
    child = repo.record_run_creation(owner_id="local/main", run_id=run_id, role="worker", parent_run_id=parent_run_id)
    _insert_operation(repo, child, status="UNKNOWN", started_at=1234.5, tool=tool)
    with repo.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='unknown', ended_at=? WHERE attempt_id=?",
                     (time.time(), child["attempt_id"]))
        conn.execute("UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?", (child["agent_run_id"],))
    return child


# LLM: 自动处置只取当前线程未关TaskRun的未知子链；主链健康也不能漏子链，别线程始终不动。
# 函数用途: 验证主/子同时未知和仅子代理未知两种前台恢复，并核对线程隔离与提示。
@pytest.mark.parametrize("main_unknown", [False, True])
def test_opt7_foreground_recovers_only_this_thread_children(tmp_path, main_unknown):
    repo, rec, owner, _thread, _operation = _opt7_unknown_turn(tmp_path)
    if not main_unknown:
        assert repo.recover_attempt_unknown(rec["attempt_id"], operator="fixture", effect_disposition="abandoned")["recovered"]
    child = _opt7_unknown_child(repo, rec["run_id"], "subagent-here")
    repo.record_run_creation(owner_id="local/main", conversation_task_id="other-task",
                             thread_id="other-thread", run_id="other-root", role="main")
    foreign = _opt7_unknown_child(repo, "other-root", "subagent-other", "foreign_tool")
    bound = runtime_mixin._bind_main_agent_authority(owner, RunParams(
        source="gateway", run_id=rec["run_id"], task_id="task-recover",
        task_attributes={"conversation_thread_id": "thread-recover"},
    ))
    assert repo.get_attempt(child["attempt_id"])["status"] == "recovered"
    assert repo.get_attempt(foreign["attempt_id"])["status"] == "unknown"
    assert "write_file" in bound.compact_continuation_injection
    assert "foreign_tool" not in bound.compact_continuation_injection
    assert len(repo.attempts_for_run(child["agent_run_id"])) == 1


# LLM: 新任务无旧主链时仍可处理本线程子链；不能顺带解除同线程另一个根任务的未知状态。
# 函数用途: 覆盖首次绑定路径的子代理核实提示，而非只覆盖已有run的换代分支。
def test_opt7_fresh_foreground_task_recovers_thread_children_without_other_root(tmp_path):
    repo, rec, owner, _thread, _operation = _opt7_unknown_turn(tmp_path)
    child = _opt7_unknown_child(repo, rec["run_id"], "subagent-here")
    bound = runtime_mixin._bind_main_agent_authority(owner, RunParams(
        source="gateway", run_id="new-root", task_id="new-task",
        task_attributes={"conversation_thread_id": "thread-recover"},
    ))
    assert repo.get_attempt(child["attempt_id"])["status"] == "recovered"
    assert repo.get_attempt(rec["attempt_id"])["status"] == "unknown"
    assert "write_file" in bound.compact_continuation_injection
    assert "run_command" not in bound.compact_continuation_injection


# LLM: 恢复CAS拒绝不能被当作成功，也不能为了启动模型绕过原仓库挂载闸。
# 函数用途: 验证失败恢复不新建attempt、不生成假成功，原未知状态仍保留。
def test_opt7_refused_recovery_does_not_mount_new_attempt(tmp_path, monkeypatch):
    repo, rec, owner, _thread, _operation = _opt7_unknown_turn(tmp_path)
    calls = []
    monkeypatch.setattr(repo, "recover_attempt_unknown", lambda *args, **kwargs: calls.append(kwargs) or {
        "recovered": False, "reason": "not_current_attempt",
    })
    with pytest.raises(RuntimeRecoveryRequiredError):
        runtime_mixin._bind_main_agent_authority(owner, RunParams(
            source="gateway", run_id=rec["run_id"], task_id="task-recover",
        ))
    assert len(calls) == 1
    assert _status(repo, rec) == ("unknown", "unknown")
    assert len(repo.attempts_for_run(rec["agent_run_id"])) == 1


# LLM: 各恢复CAS各自提交；后续子链、挂载或凭据发布失败后，冷启动重试也必须重建已恢复目标的提示。
# 函数用途: 复现独立评审指出的跨失败核实提示丢失，不把已恢复目标遗忘掉。
@pytest.mark.parametrize("failure", ["child", "mount", "publish", "fresh_root"])
def test_opt7_review_retry_keeps_committed_recovery_notice(tmp_path, monkeypatch, failure):
    from agent_py_agent.agent.runtime_db import child_recovery

    repo, rec, owner, _thread, _operation = _opt7_unknown_turn(tmp_path)
    child = _opt7_unknown_child(repo, rec["run_id"], "subagent-interrupted") if failure in {"child", "fresh_root"} else None
    params = RunParams(source="gateway", run_id=rec["run_id"], task_id="task-recover",
                       task_attributes={"conversation_thread_id": "thread-recover"})
    with monkeypatch.context() as patch:
        if failure == "child":
            patch.setattr(child_recovery, "recover_child_attempt_unknown", lambda *args: {
                "recovered": False, "reason": "target_changed",
            })
        elif failure == "publish":
            params.conversation_task_binding_callback = SimpleNamespace(bind_runtime_authority=lambda _binding: False)
        else:
            # LLM: 故障仅注入恢复之后的实际挂载/创建出口，前置真实CAS及审计仍执行。
            # 函数用途: 模拟新执行轮尚未进入模型就失败，检查下一次请求不丢交接。
            def fail(*args, **kwargs):
                raise RuntimeConflictError("injected mount failure")

            method = "record_run_creation" if failure == "fresh_root" else "create_attempt"
            patch.setattr(repo, method, fail)
            if failure == "fresh_root":
                params.run_id, params.task_id = "fresh-root", "fresh-task"
        with pytest.raises(RuntimeError):
            runtime_mixin._bind_main_agent_authority(owner, params)
    recovered_id = child["attempt_id"] if failure == "fresh_root" else rec["attempt_id"]
    assert repo.get_attempt(recovered_id)["status"] == "recovered"
    cold_repo = RuntimeRepository(str(next(tmp_path.glob("runtime-*.db"))))
    cold_owner = SimpleNamespace(subagents=SimpleNamespace(runtime_db=cold_repo))
    retried = runtime_mixin._bind_main_agent_authority(cold_owner, RunParams(
        source="gateway", run_id=params.run_id, task_id=params.task_id,
        task_attributes={"conversation_thread_id": "thread-recover"},
    ))
    injected = "\n\n".join(retried.inject or [])
    assert ("write_file" if failure == "fresh_root" else "run_command") in injected
    if failure == "child":
        assert "write_file" in injected


# LLM: 已提交恢复但尚未交给模型的事项仍须阻止无人后台；没有携带核实提示的续跑不能借已解锁状态绕过交接。
# 函数用途: 确认恢复后挂载失败不会意外放行后台续跑。
def test_opt7_review_pending_handoff_still_blocks_background(tmp_path, monkeypatch):
    repo, rec, owner, _thread, _operation = _opt7_unknown_turn(tmp_path)

    # LLM: 仅拦截恢复之后的挂载，不改变unknown恢复的持久事件。
    # 函数用途: 制造数据库已恢复但模型还没见到核实提示的状态。
    def fail(*args, **kwargs):
        raise RuntimeConflictError("mount failed")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "create_attempt", fail)
        with pytest.raises(RuntimeConflictError):
            runtime_mixin._bind_main_agent_authority(owner, RunParams(
                source="gateway", run_id=rec["run_id"], task_id="task-recover",
            ))
    with pytest.raises(RuntimeRecoveryRequiredError):
        runtime_mixin._bind_main_agent_authority(owner, RunParams(
            source="background_main_agent", run_id=rec["run_id"], task_id="task-recover",
        ))


# LLM: 模型桩只代替外部推理，运行绑定/收口/审计均真实；仅成功返回才确认“提示已交接”，绝不确认外部副作用已生效。
# 函数用途: 验证交接确认阻止重复注入，确认落账失败则保留下一轮核实要求。
@pytest.mark.parametrize("ack_fails", [False, True])
def test_opt7_review_successful_turn_acknowledges_notice_only(tmp_path, monkeypatch, ack_fails):
    repo, rec, owner, _thread, _operation = _opt7_unknown_turn(tmp_path)
    monkeypatch.setattr(runtime_mixin, "bind_cli_run_conversation", lambda _agent, params, _prompt: params)
    monkeypatch.setattr(runtime_mixin, "attach_run_task_workspace_context", lambda _agent, params, _prompt: params)
    seen = []

    # LLM: 记录真实绑定传给执行接缝的提示，只把模型回答替换为本地桩，不声称实测了供应商。
    # 函数用途: 验证核实提示先到执行入口，再产生交接确认。
    def run_once(_agent, _prompt, params):
        seen.extend(params.inject or [])
        return SimpleNamespace(runtime_status="ok", runtime_reason="", runtime_source="gateway", tool_rounds=0)

    monkeypatch.setattr(runtime_mixin, "_run_once_with_params", run_once)
    monkeypatch.setattr(runtime_mixin, "compact_auto_continuation_decision", lambda *_args, **_kwargs: SimpleNamespace(should_continue=False))
    monkeypatch.setattr(runtime_mixin, "finish_run_task_workspace_if_needed", lambda *_args: None)
    monkeypatch.setattr(runtime_mixin, "persist_cli_run_assistant", lambda *_args: None)
    if ack_fails:
        # LLM: 不影响原恢复CAS内部事件，只模拟可选交接确认写入口故障。
        # 函数用途: 保持业务成功返回而核实提醒不被错误清掉。
        def fail_ack(**kwargs):
            raise OSError("audit unavailable")

        monkeypatch.setattr(repo, "append_event", fail_ack)
    result = runtime_mixin._run_with_params(owner, "继续", RunParams(
        source="gateway", run_id=rec["run_id"], task_id="task-recover",
    ))
    assert result.runtime_status == "ok" and "run_command" in "\n".join(seen)
    with repo._runtime_connection() as conn:
        count = conn.execute("SELECT count(*) FROM runtime_events WHERE event_type='foreground_recovery_notice_delivered'").fetchone()[0]
    assert count == (0 if ack_fails else 1)
    again = runtime_mixin._bind_main_agent_authority(owner, RunParams(
        source="gateway", run_id=rec["run_id"], task_id="task-recover",
    ))
    assert ("【系统恢复提示】" in "\n\n".join(again.inject or [])) is ack_fails


# LLM: 另一恢复者可在pending查询与子unknown投影之间提交CAS；审计与状态同事务，最终交接查询必须在扫描之后。
# 函数用途: 用确定性交错复现并守住子代理刚恢复时的提示交接窗口，不假装穷尽全部并发。
def test_opt7_review_recovery_between_pending_read_and_child_scan_keeps_notice(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_db import child_recovery

    repo, rec, owner, _thread, _operation = _opt7_unknown_turn(tmp_path)
    assert repo.recover_attempt_unknown(rec["attempt_id"], operator="fixture", effect_disposition="abandoned")["recovered"]
    _opt7_unknown_child(repo, rec["run_id"], "subagent-interleaved")
    original = child_recovery.unknown_child_attempts_for_thread

    # LLM: 只在指定读边界插入另一调用者真实提交的恢复；被测调用会看到子链已不再unknown。
    # 函数用途: 模拟先前调用已解阻却没进入模型，使本调用必须通过审计接过核实事项。
    def interleaved(repository, thread_id):
        for target in original(repository, thread_id):
            assert child_recovery.recover_child_attempt_unknown(
                repository, target, "abandoned", "foreground-user-turn",
            )["recovered"]
        return original(repository, thread_id)

    monkeypatch.setattr(child_recovery, "unknown_child_attempts_for_thread", interleaved)
    bound = runtime_mixin._bind_main_agent_authority(owner, RunParams(
        source="gateway", run_id=rec["run_id"], task_id="task-recover",
        task_attributes={"conversation_thread_id": "thread-recover"},
    ))
    assert "write_file" in "\n\n".join(bound.inject or [])
