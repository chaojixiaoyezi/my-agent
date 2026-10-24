# LLM: 真实进程原回归保留；新增退避与投影用有界时钟/系统边界替身，不冒充真实宿主或网络验收。
# 模块用途: 验证后台句柄、退出保护、低频等待和完成唤醒的同源事实。
"""run_command 后台执行钉子(P0-2 对照能力补齐:持久/后台 shell)。

钉死契约:run_in_background 启动后台进程立即返回 session_id+output_file 不阻塞;
输出落 .background_jobs/ 日志;registry 留痕;参数各形态识别;同步模式不受影响。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.tooling.process_registry import process_registry
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.agent.tooling.shell import (
    ShellTool,
    _wants_background,
)
from agent_py_agent.agent.tooling.shell_syntax import contains_unmanaged_background_operator
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    runtime_snapshot_for_tools,
)


def _background_policy_decision(tmp_path: Path, command: str, *, approval_mode: str = "ask"):
    tool = ShellTool(tmp_path)
    snapshot = runtime_snapshot_for_tools({tool.model_spec.name: tool}, run_id="run-bg")
    call: ToolCall = canonical_test_call(
        snapshot,
        tool.model_spec.name,
        {"command": command, "run_in_background": True},
    )
    return ActionPolicy().decide(
        ActionPolicyRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=tmp_path,
            workspace_roots=(tmp_path,),
            owner_scope_root=str(tmp_path),
            approval_mode=approval_mode,
        )
    )


def test_managed_background_command_keeps_explicit_approval_and_sandbox(tmp_path):
    decision = _background_policy_decision(
        tmp_path,
        'python3 -c "import time; time.sleep(2)"',
    )

    assert decision.status == "ask"
    assert not decision.sandbox_plan
    approved = _background_policy_decision(
        tmp_path, 'python3 -c "import time; time.sleep(2)"', approval_mode="auto",
    )
    assert approved.status == "allow"
    assert approved.sandbox_plan["mode"] == "required"


def test_managed_background_still_rejects_destructive_command(tmp_path):
    decision = _background_policy_decision(tmp_path, "rm -rf /tmp/forbidden")

    assert decision.status == "deny"
    assert "COMMAND_DESTRUCTIVE_DELETE_BLOCKED" in decision.reason_codes


def test_background_start_returns_session_handle_and_output_file(tmp_path):
    res = ShellTool(tmp_path).execute(
        {"command": "echo started; sleep 1; echo done", "run_in_background": True}
    )
    assert res.ok
    payload = json.loads(res.output)
    assert payload["status"] == "started" and payload["session_id"].startswith("bg-")
    assert "pid" not in payload and "process_pid" not in payload
    assert "唯一稳定" in payload["hint"]
    out = Path(payload["output_file"])
    assert out.parent.name == ".background_jobs"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "done" not in out.read_text(encoding="utf-8"):
        time.sleep(0.1)
    content = out.read_text(encoding="utf-8")
    assert "started" in content and "done" in content, "后台输出必须落到 output_file"


def test_background_does_not_block(tmp_path):
    """后台模式的 execute 在子进程还在跑的时候就返回。

    判定只看结构化事实：回执 status=started、没有 exit_code，且返回那一刻登记表里的会话仍是 running；
    不用墙钟——满载下启动观察（宿主启动 + 0.5 秒 settle）本身就可能超过 1 秒，原来的"耗时 < 1.2 秒"会误报。
    子进程跑 15 秒（非纯 sleep，避免被 wait 引导拦），用例结束时经登记表杀掉。
    """
    process_registry.clear()
    res = ShellTool(tmp_path).execute(
        {"command": 'python3 -c "import time; time.sleep(15); print(1)"', "run_in_background": True}
    )
    assert res.ok, res.output
    payload = json.loads(res.output)
    session_id = payload["session_id"]
    try:
        assert payload["status"] == "started" and "exit_code" not in payload
        status = process_registry.status(session_id)
        assert status is not None and status["status"] == "running"  # 返回时子进程仍在跑：没有等它结束
    finally:
        process_registry.kill(session_id)
        process_registry.clear()


def test_background_immediate_success_returns_terminal_result(tmp_path):
    res = ShellTool(tmp_path).execute(
        {"command": "printf immediate-ok", "run_in_background": True}
    )

    assert res.ok is True
    payload = json.loads(res.output)
    assert payload["status"] == "exited"
    assert payload["exit_code"] == 0
    assert "immediate-ok" in payload["output_tail"]
    assert "不要说服务已启动" in payload["hint"]


def test_background_immediate_failure_is_not_reported_started(tmp_path):
    res = ShellTool(tmp_path).execute(
        {
            "command": 'python3 -c "import sys; print(\'startup-boom\'); sys.exit(23)"',
            "run_in_background": True,
        }
    )

    assert res.ok is False
    assert res.error_code == "COMMAND_FAILED"
    assert res.effect_outcome == "failed"
    payload = json.loads(res.output)
    assert payload["status"] == "exited"
    assert payload["exit_code"] == 23
    assert "startup-boom" in payload["output_tail"]


def test_background_records_registry(tmp_path):
    ShellTool(tmp_path).execute({"command": "echo hi", "run_in_background": True})
    registry = tmp_path / ".background_jobs" / "registry.jsonl"
    assert registry.exists()
    row = json.loads(registry.read_text(encoding="utf-8").splitlines()[0])
    assert row["pid"] > 0 and "hi" in row["command"]


def test_wants_background_param_forms():
    assert _wants_background({"run_in_background": True}) is True
    assert _wants_background({"run_in_background": "true"}) is True
    assert _wants_background({"run_in_background": "1"}) is True
    assert _wants_background({"run_in_background": False}) is False
    assert _wants_background({"run_in_background": "no"}) is False
    assert _wants_background({}) is False


def test_sync_mode_unaffected(tmp_path):
    res = ShellTool(tmp_path).execute({"command": "echo sync-result"})
    assert res.ok and "sync-result" in res.output
    assert not (tmp_path / ".background_jobs").exists(), "同步模式不创建后台目录"


@pytest.mark.parametrize("run_in_background", [False, True])
def test_shell_background_operator_requires_managed_mode(tmp_path, run_in_background):
    res = ShellTool(tmp_path).execute(
        {
            "command": "nohup python3 -c 'import time; time.sleep(10)' >/tmp/job.log 2>&1 &",
            "run_in_background": run_in_background,
        }
    )

    assert res.ok is False
    assert res.error_code == "BACKGROUND_PROCESS_MODE_REQUIRED"
    assert res.effect_outcome == "not_started"
    assert json.loads(res.output)["required_call_shape"]["run_in_background"] is True
    assert not (tmp_path / ".background_jobs").exists()


def test_background_operator_parser_ignores_quotes_and_fd_redirects():
    assert contains_unmanaged_background_operator("sleep 1&") is True
    assert contains_unmanaged_background_operator("(sleep 1)&") is True
    assert contains_unmanaged_background_operator('printf "a&b"') is False
    assert contains_unmanaged_background_operator("echo hi 2>&1") is False


@pytest.mark.parametrize(
    "command, expected",
    [
        ("bash -c 'sleep 1 &'", True),
        ("cd . && /bin/sh -lc 'sleep 1&'", True),
        ("env FLAG=1 bash -c 'sleep 1 &'", True),
        ("timeout 10 sh -c 'sleep 1 &'", True),
        ('''bash -c "sh -c 'sleep 1 &'"''', True),
        ('''bash -c "printf '%s' 'a&b'"''', False),
        ("echo bash -c 'sleep 1 &'", False),
        ('''python3 -c "value='&'"''', False),
    ],
)
def test_background_operator_checks_literal_shell_programs(command, expected):
    assert contains_unmanaged_background_operator(command) is expected


def test_nested_background_is_rejected_before_launch(tmp_path, monkeypatch):
    tool = ShellTool(tmp_path)
    monkeypatch.setattr(tool, "_run_command", lambda *a, **k: pytest.fail("must not spawn"))
    result = tool.execute({"command": "cd . && bash -c 'echo ready && sleep 30 &'"})
    assert result.error_code == "BACKGROUND_PROCESS_MODE_REQUIRED"
    assert result.effect_outcome == "not_started"


def test_background_operator_parser_ignores_heredoc_body_but_keeps_shell_lines():
    go_source = """cat > main.go <<'EOF'
value := &Context{}
callback := func() { left & right }
EOF
echo done
"""
    background_after_body = """cat <<EOF
body & data
EOF
sleep 1 &
"""
    background_on_opener = """cat <<EOF &
body
EOF
"""
    commented_fake_opener = """# cat <<EOF
sleep 1 &
EOF
"""

    assert contains_unmanaged_background_operator(go_source) is False
    assert contains_unmanaged_background_operator(background_after_body) is True
    assert contains_unmanaged_background_operator(background_on_opener) is True
    assert contains_unmanaged_background_operator(commented_fake_opener) is True


def test_fd_redirect_remains_valid_in_foreground(tmp_path):
    res = ShellTool(tmp_path).execute({"command": "echo redirect-ok 2>&1"})

    assert res.ok is True
    assert "redirect-ok" in res.output


def test_structured_background_process_remains_managed_after_execute_returns(tmp_path):
    process_registry.clear()
    res = ShellTool(tmp_path).execute(
        {
            "command": 'python3 -c "import time; time.sleep(10)"',
            "run_in_background": True,
        }
    )
    assert res.ok is True
    session_id = json.loads(res.output)["session_id"]
    try:
        time.sleep(0.1)
        status = process_registry.status(session_id)
        assert status is not None
        assert status["status"] == "running"
    finally:
        process_registry.kill(session_id)
        process_registry.clear()


# LLM: 真Store/投影/monitor保留；仅替代时钟、内核观察和终止树边界，提前poll/wait立即失败。
# 函数用途: 核对三段退避、稳定记录复用及不回收组长的原顺序。
def test_w16_monitor_adapts_sleep_without_reaping_or_reparsing_each_tick(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling import background_process_host as host
    from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
    from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
    from agent_py_agent.tests.test_process_session_store import _running

    clock, sleeps, reads, terminations = [0.0], [], [], []
    store = ProcessSessionStore(tmp_path)
    store.write(_running(handoff_confirmed=True))
    original_load = store.load

    # LLM: 原Store读取和验证照常执行，不用缓存替身预造成功。
    # 函数用途: 记录实际完整读取的次数。
    def load(session_id):
        reads.append(clock[0])
        return original_load(session_id)

    # LLM: 只推进单调时钟，最多400次以便旧固定50ms实现也有界退出。
    # 函数用途: 采集monitor真正选择的间隔。
    def sleep(interval):
        assert len(sleeps) < 400
        sleeps.append((clock[0], interval))
        clock[0] += interval

    # LLM: 原观察为已退出但未回收时才进入终止边界，调用身份不可换。
    # 函数用途: 检查自然退出仍把原句柄和出生令牌交给树回收入口。
    def terminate(pid, child, **kwargs):
        assert pid == child.pid == 102 and kwargs["expected_birth_token"] == "child-birth"
        terminations.append(pid)
        return ProcessTerminationReceipt("already_gone", True, 0, 1)

    child = SimpleNamespace(pid=102, poll=lambda: pytest.fail("提前poll"), wait=lambda **kw: pytest.fail("提前wait"))
    monkeypatch.setattr(store, "load", load)
    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host.time, "sleep", sleep)
    monkeypatch.setattr(host, "_direct_child_exited_unreaped", lambda child: clock[0] >= 11)
    monkeypatch.setattr(host, "_process_instance_terminated", lambda *args: pytest.fail("不得代替未回收观察"))
    monkeypatch.setattr(host, "terminate_process_tree", terminate)
    assert host._monitor_child(store, "bg-a", child, "child-birth", 0, host._MonitorPolicy(listen_scope="lan")) == 0
    assert {interval for _, interval in sleeps} == {0.05, 0.15, 0.25}
    for elapsed, interval in sleeps:
        assert interval == (0.05 if elapsed < 1 else 0.15 if elapsed < 10 else 0.25)
    assert len(reads) < len(sleeps) / 2
    assert terminations == [102] and original_load("bg-a").record["status"] == "exited"


# LLM: 等待仍通过原get/取消口；只替代存储状态和时钟，不启动进程或改变超时、终态语义。
# 函数用途: 核对显式wait三段退避、剩余预算截断，以及取消令牌的等待入口。
@pytest.mark.parametrize("use_token", [False, True])
def test_w16_registry_wait_adapts_and_clamps_remaining_budget(monkeypatch, use_token):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling import process_registry as module

    clock, sleeps = [0.0], []
    registry = module.ProcessRegistry()
    record = module.BackgroundProcess("bg-wait", "test", 123, 0.0)

    # LLM: 假等待只推进时钟；保留真实wait循环与deadline算法，超出次数立即失败而非卡住。
    # 函数用途: 收集两种等待分支的实际预算。
    def wait(interval):
        assert len(sleeps) < 200
        sleeps.append((clock[0], interval))
        clock[0] += interval

    monkeypatch.setattr(registry, "get", lambda *args: record)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", wait)
    monkeypatch.setattr(module, "current_cancellation_token", lambda: SimpleNamespace(wait=wait) if use_token else None)
    result = registry.wait("bg-wait", 4.07)
    assert result["wait_timed_out"] is True and result["status"] == "running"
    assert sleeps[0][1] == 0.05
    assert any(interval == 0.25 for _, interval in sleeps)
    for elapsed, interval in sleeps:
        expected = 0.05 if elapsed < 0.5 else 0.1 if elapsed < 3 else 0.25
        assert interval == pytest.approx(min(expected, 4.07 - elapsed))
    assert clock[0] == pytest.approx(4.07)


# LLM: 原ConversationStore/进程Store/Registry均真实；PID为合成数据，仅在内核存活观察边界返回仍运行，不启动或停止系统进程。
# 函数用途: 准备一条属于当前owner/thread/task的完整受管记录，供返回事实与收口测试共用。
def _w16_wake_fixture(tmp_path, monkeypatch, *, armed=True, status="running", target_changes=None, owner=None):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.process_events import process_completion_target
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.tooling import process_registry as module
    from agent_py_agent.agent.tooling.process_session_store import (
        ProcessSessionStore,
        process_session_store_root,
    )
    from agent_py_agent.tests.test_process_session_store import _running

    owner = owner or tmp_path / "owner"
    conversations = ConversationStore(owner / "conversations")
    thread = conversations.threads.get_or_create({"canonical_user_id": "owner-a", "channel": "tui", "channel_conversation_id": "fixture"})
    agent = SimpleNamespace(conversation_store=conversations, home_paths=SimpleNamespace(owner_home_dir=owner),
        effective_workspace_root=owner, config=SimpleNamespace(background_process_notifications=True))
    params = SimpleNamespace(context_scope="conversation", run_id="run-a", task_id="task-a", request_id="request-a",
        task_attributes={"conversation_thread_id": thread.thread_id, "conversation_task_id": "task-a"},
        root_user_prompt="后台采样后告知结果", user_prompt="后台采样后告知结果", executed_tools=[], archive_tool_calls=[],
        active_turn_user_inputs=[], live_archive_state={})
    target = {**process_completion_target(agent, params), **(target_changes or {})} if armed else {}
    target_thread = target.get("thread_id", thread.thread_id)
    payload = _running("bg-w16", status=status, completion_target=target,
        access_scope={"owner_id": "owner-a", "conversation_id": target_thread, "owner_home": str(owner)},
        execution_scope={"owner_home": str(owner), "thread_id": target_thread, "root_task_id": target.get("task_id", "task-a"),
                         "run_id": "run-a", "attempt_id": "attempt-a"}, handoff_confirmed=True)
    if status == "exited":
        payload.update(finished_at=3.0, exit_code=0)
    authority = ProcessSessionStore(process_session_store_root(owner, owner))
    authority.write(payload)
    monkeypatch.setattr(module, "_process_instance_terminated", lambda *args: False)
    registry = agent.process_registry = module.ProcessRegistry()
    record = registry.get("bg-w16", store_root=authority.root)
    return agent, params, authority, registry, record


# LLM: 真实启动回执投影和Store读取，不调用启动器；不将enabled当成已收到完成事件。
# 函数用途: 后台启动结果明确区分已绑定完成唤醒与未绑定会话。
@pytest.mark.parametrize("armed", [False, True])
def test_w16_background_start_exposes_completion_wake(tmp_path, monkeypatch, armed):
    from agent_py_agent.agent.tooling import shell

    _agent, _params, _authority, registry, record = _w16_wake_fixture(tmp_path, monkeypatch, armed=armed)
    monkeypatch.setattr(shell, "process_registry", registry)
    result = shell._background_start_outcome(tool_name="run_command", record=record, log_path=tmp_path / "log")
    expected = {"enabled": armed, "reason": "managed_process_exited" if armed else "", "yield_supported": armed}
    assert result.ok and json.loads(result.output)["status"] == "started"
    assert json.loads(result.output).get("completion_wake") == expected
    assert result.result_envelope.get("completion_wake") == expected
    if armed:
        assert "唤醒" in json.loads(result.output)["hint"]


# LLM: status与零秒wait走原权限/原Registry；只替换此测试使用的Registry实例，不伪造控制结果。
# 函数用途: 两个模型入口一致返回当前运行记录的唤醒事实，不暴露收件路径或任务身份。
@pytest.mark.parametrize("action", ["status", "wait"])
@pytest.mark.parametrize("armed", [False, True])
def test_w16_process_session_exposes_completion_wake(tmp_path, monkeypatch, action, armed):
    from agent_py_agent.agent.tooling import process_sessions

    agent, params, _authority, registry, _record = _w16_wake_fixture(tmp_path, monkeypatch, armed=armed)
    monkeypatch.setattr(process_sessions, "process_registry", registry)
    tool = process_sessions.ProcessSessionTool(agent.home_paths.owner_home_dir, agent.home_paths.owner_home_dir)
    result = tool.execute({"action": action, "session_id": "bg-w16", "timeout_seconds": 0,
        "__run_scope": {"owner_id": "owner-a", "owner_home": str(agent.home_paths.owner_home_dir),
                        "session_id": params.task_attributes["conversation_thread_id"]}})
    expected = {"enabled": armed, "reason": "managed_process_exited" if armed else "", "yield_supported": armed}
    assert result.ok, result.output
    payload = json.loads(result.output)
    assert payload.get("completion_wake") == expected
    assert result.result_envelope.get("completion_wake") == expected
    assert "completion_target" not in payload


# LLM: 当前thread/task的真实记录通过原Registry可见；事实只提供计数和唤醒能力，不携带收件地址。
# 函数用途: 验证原阶段回执确实识别当前任务的后台依赖。
def test_w16_interim_facts_recognize_current_wake_backed_process(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop import completion

    agent, params, _authority, _registry, _record = _w16_wake_fixture(tmp_path, monkeypatch)
    facts = completion._interim_reply_facts(agent, params, tool_rounds=2)
    assert facts.get("active_background_process_count") == 1
    assert facts.get("active_background_process_count_is_minimum") is True
    assert facts.get("completion_wake") == {"enabled": True, "reason": "managed_process_exited", "yield_supported": True}
    assert facts["reply_is_interim"] and facts["task_continues_without_more_user_input"]
    assert "store_root" not in json.dumps(facts) and "completion_target" not in facts


# LLM: 原普通plain-final收口入口和零工具表达阶段真实执行；后台运行记录不等于业务成功，不强制中断实际工具轮。
# 函数用途: 没有命名Audit时也能让出当前回合，并保留完成唤醒继续的阶段事实。
def test_w16_plain_final_queues_background_interim_and_can_settle(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop import completion
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        finish_natural_user_reply,
        pending_natural_user_reply,
    )
    from agent_py_agent.agent.backends import ModelResponse

    agent, params, _authority, _registry, _record = _w16_wake_fixture(tmp_path, monkeypatch)
    assert completion.queue_interim_reply_for_active_named_work(agent, params, tool_rounds=2)
    phase = pending_natural_user_reply(params)
    assert phase["kind"] == "background_process_active"
    assert phase["facts"]["active_background_process_count"] == 1
    result = finish_natural_user_reply(params, ModelResponse(text="已启动后台采样，完成事件到达后继续核对。", backend="test"), accepted=True)
    assert result.runtime_reason == "background_process_active" and not result.tool_use_blocks
    assert pending_natural_user_reply(params) is None


# LLM: 记录属于其它任务、线程或存储不能决定当前回合；未知/终态、关闭通知、子代理和无绑定也不制造自动续跑承诺。
# 函数用途: 锁住后台依赖投影的归属和普通前台回合的原收口。
@pytest.mark.parametrize("case", ["unarmed", "exited", "unknown", "other_task", "other_thread", "other_store",
    "disabled", "child", "foreground_task", "event_reply", "prepare_reply"])
def test_w16_unrelated_or_unarmed_process_does_not_yield_foreground(tmp_path, monkeypatch, case):
    from agent_py_agent.agent.agent_core.tool_loop import completion
    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_AUDIT_PREPARE_ATTR,
        CONVERSATION_BACKGROUND_EVENT_REASON_ATTR,
    )

    changes = {"other_task": {"task_id": "task-other"}, "other_thread": {"thread_id": "thread-other"},
               "other_store": {"store_root": str(tmp_path / "foreign-conversations")}}.get(case)
    agent, params, _authority, _registry, _record = _w16_wake_fixture(tmp_path, monkeypatch, armed=case != "unarmed",
        status=case if case in {"exited", "unknown"} else "running", target_changes=changes)
    if case == "disabled":
        agent.config.background_process_notifications = False
    elif case == "child":
        params.context_scope = "task_local"
    elif case == "foreground_task":
        params.task_id = "unrelated-foreground"
    elif case == "event_reply":
        params.task_attributes[CONVERSATION_BACKGROUND_EVENT_REASON_ATTR] = "managed_process_exited"
    elif case == "prepare_reply":
        params.task_attributes[CONVERSATION_AUDIT_PREPARE_ATTR] = True
    assert not completion.queue_interim_reply_for_active_named_work(agent, params, tool_rounds=1)
    assert params.live_archive_state == {}


# LLM: 真实Agent没有额外挂载Registry时应复用既有全局实例，只读当前owner根，不创建第二本记录账。
# 函数用途: 防止新计数只在有测试替身属性的agent上生效。
def test_w16_wake_count_uses_existing_registry_when_agent_has_none(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop import completion
    from agent_py_agent.agent.tooling import process_registry as module

    agent, params, _authority, registry, _record = _w16_wake_fixture(tmp_path, monkeypatch)
    del agent.process_registry
    monkeypatch.setattr(module, "process_registry", registry)
    assert completion._interim_reply_facts(agent, params, tool_rounds=1).get("active_background_process_count") == 1


# LLM: 真实工具循环/表达片，仅模型采样为确定性替身；不启动进程，不将此测试外推为真实完成事件E2E。
# 函数用途: 核对普通plain-final确实进入一次零工具阶段后释放，而未绑定唤醒的前台仍只采样一次。
@pytest.mark.parametrize("armed", [False, True])
def test_w16_real_tool_loop_settles_once_without_polling(tmp_path, monkeypatch, armed):
    from agent_py_agent.agent.agent_core._tool_loop_service import execute_tool_loop
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
    )
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params

    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=False), tmp_path / "work")
    prepared, source, _authority, registry, _record = _w16_wake_fixture(
        tmp_path, monkeypatch, armed=armed, owner=agent.home_paths.owner_home_dir)
    agent.conversation_store, agent.process_registry = prepared.conversation_store, registry
    params = _tool_loop_params(context_scope="conversation", task_id=source.task_id, run_id=source.run_id,
        request_id=source.request_id, task_attributes=source.task_attributes, root_user_prompt=source.root_user_prompt,
        allowed_tools=[], live_archive_state={})
    calls = []

    # LLM: 只提供两个有界模型响应，不能偷偷执行工具或把第三次忙采样当成成功。
    # 类用途: 模拟先给plain-final草稿，再根据宿主事实写阶段回执。
    class Backend:
        name = "w16-scripted-model"

        # LLM: 检查表达片仍无工具，并从当前真实pending事实读取，而非从提示文字猜生命周期。
        # 函数用途: 在限定调用次数内返回模型正文。
        def generate(self, prompt, **kwargs):
            calls.append(prompt)
            assert len(calls) <= 2
            if len(calls) == 1:
                return ModelResponse(text="采样已经启动。", backend=self.name)
            phase = pending_natural_user_reply(params)
            assert phase["kind"] == "background_process_active"
            assert phase["facts"]["active_background_process_count"] == 1
            assert not kwargs.get("tools")
            return ModelResponse(text="后台采样仍在运行，完成后继续核对。", backend=self.name)

    agent.backend = Backend()
    response = execute_tool_loop(agent, params).final_response
    assert len(calls) == (2 if armed else 1)
    assert response.text == ("后台采样仍在运行，完成后继续核对。" if armed else "采样已经启动。")
    assert not params.executed_tools and pending_natural_user_reply(params) is None
    if armed:
        assert response.runtime_status == "ok" and response.runtime_reason == "background_process_active"


# LLM: 完成事件对比的是原record.target与signal.source_agent_id，不要求后来同任务的模型片run_id相同。
# 函数用途: 用真实发表/回执/投递资格证明同一任务跨模型执行片仍可接收原完成事件，避免误加run过滤。
def test_w16_same_task_new_run_keeps_original_completion_delivery(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop import completion
    from agent_py_agent.agent.conversation import process_events

    agent, params, authority, registry, _record = _w16_wake_fixture(tmp_path, monkeypatch)
    params.run_id = "later-model-slice"
    assert completion._active_wake_backed_process_count(agent, params) == 1
    agent.conversation_store.tasks.bind({"thread_id": params.task_attributes["conversation_thread_id"],
        "task_id": params.task_id, "status": "completed", "goal": params.root_user_prompt})
    payload = authority.load("bg-w16").record
    authority.write({**payload, "status": "exited", "finished_at": 3.0, "exit_code": 0})
    monkeypatch.setattr(process_events, "process_registry", registry)
    assert process_events.reconcile_process_completions(agent) == 1
    signal, = agent.conversation_store.wakes.pending(limit=0)
    assert signal.source_agent_id == "run-a" != params.run_id
    assert process_events.process_completion_delivery_state(agent, signal) == "ready"


# LLM: 返回值是原Store核实的健康记录下限；一条无关坏记录不能把已核实的当前依赖抹成零。
# 函数用途: 钉住独立复核发现的跨任务局部错误影响，分别覆盖列表局部错与后续单条读取错。
@pytest.mark.parametrize("error_stage", ["list", "get"])
def test_w16_unrelated_record_error_preserves_verified_wake_count(tmp_path, monkeypatch, error_stage):
    from agent_py_agent.agent.agent_core.tool_loop import completion

    agent, params, _authority, registry, _record = _w16_wake_fixture(tmp_path, monkeypatch)
    original_report, original_get = registry.list_report, registry.get

    # LLM: 当前记录仍走真Registry，仅在公共读取边界注入另一条记录的局部故障。
    # 函数用途: 先交付当前健康摘要，再提供列表错误或待读取的坏摘要。
    def report(**kwargs):
        rows, errors = original_report(**kwargs)
        if error_stage == "list":
            return rows, [*errors, {"error": "unrelated record unreadable"}]
        return [*rows, {"session_id": "bg-zbroken", "status": "running"}], errors

    # LLM: 不改当前记录的原始读取，只让另一条记录在枚举后发生读取错误。
    # 函数用途: 模拟同owner其它任务的文件故障。
    def get(session_id, **kwargs):
        if session_id == "bg-zbroken":
            raise OSError("unrelated record unreadable")
        return original_get(session_id, **kwargs)

    monkeypatch.setattr(registry, "list_report", report)
    monkeypatch.setattr(registry, "get", get)
    assert completion._active_wake_backed_process_count(agent, params) == 1
