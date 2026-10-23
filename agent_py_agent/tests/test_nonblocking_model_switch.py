# LLM: 使用真实控制 outbox/claim/线程与受控传输边界；不调用商业模型，不修改用户模型目录。
# 模块用途: 锁定会话设置非阻塞派发、短事务及菜单收口合同。
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
)
from agent_py_agent.cli.chat_parts.tui_control_delivery import TuiControlOperationEntry
from agent_py_agent.tests.test_tui_control_delivery import _reconciler


# LLM: 普通控制在 Event 上挂起，真实两条设置必须按顺序执行；一条 command 只由共享 claim 取得一次。
# 函数用途: 验证模型和档位不会排在慢 Compact 后面，也不会互相乱序。
def test_m2_setting_lane_bypasses_slow_control_in_order(tmp_path):
    blocked, release, changed = threading.Event(), threading.Event(), threading.Event()
    calls = []

    # LLM: 只替换网络，文件/claim/完成删除都走真实实现；退出测试必释放慢请求。
    # 函数用途: 记录派发顺序，模拟一条阻塞普通控制。
    def submit(entry):
        calls.append(entry.message_id)
        if entry.command_kind == "compact":
            blocked.set()
            release.wait(3)
        if entry.command_kind == "effort":
            changed.set()
        return ConversationControlResult(entry.command_kind, True, "saved", control_state="completed")

    reconciler = _reconciler(tmp_path, submit=submit, status=lambda _id: None)
    reconciler.stop_event.clear()
    try:
        reconciler.enqueue(TuiControlOperationEntry("slow", "/compact", "compact"))
        assert blocked.wait(1)
        reconciler.enqueue(TuiControlOperationEntry("model-b", "/model B", "model"))
        reconciler.enqueue(TuiControlOperationEntry("effort-high", "/effort high", "effort"))
        assert changed.wait(0.8), "设置仍被慢控制队首阻塞"
        assert calls == ["slow", "model-b", "effort-high"]
    finally:
        release.set()
        reconciler.stop_event.set()
        reconciler._wake.set()


# LLM: 用真实文字控制函数，执行快照明确抛异常；若保存路径依赖快照就会失败，不放宽成功断言。
# 函数用途: 保存本会话选择不能被附加的执行快照阻塞或反向判为失败。
def test_m1_text_model_save_does_not_wait_for_execution_snapshot(monkeypatch):
    from agent_py_agent.agent.gateway_parts import model_profile_service as service

    calls = []
    listing = {"ok": True, "selected": "A", "profiles": [{"id": "B", "model_name": "B"}]}
    monkeypatch.setattr(service, "_scoped_model_host", lambda *args, **opts: (object(), SimpleNamespace(thread_id="thread")))
    monkeypatch.setattr(service, "_model_operation", lambda host, operation, payload, thread, source: (calls.append(operation), listing)[1])
    monkeypatch.setattr(service, "_model_execution_snapshot", lambda *args: (_ for _ in ()).throw(RuntimeError("slow snapshot")))
    result = service.execute_model_text_control(object(), ConversationControlCommand("model", operation="select", value="B"),
        SimpleNamespace(channel="chat"))
    assert result.ok, result.message
    assert calls == ["list", "select"]


# LLM: 占住真实 outbox 进程锁，调用真实按键控制分派；用单调时钟断言回调不等锁释放，不把开始线程当持久成功。
# 函数用途: 模型文字选择在磁盘状态忙时仍迅速归还 TUI 输入。
@pytest.mark.parametrize("command", ["/model B", "/effort high", "/status"])
def test_m3_model_key_callback_does_not_wait_for_outbox_lock(tmp_path, command):
    import time

    from agent_py_agent.cli.chat_parts.tui_actions import _tui_submit_control_operation
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    held, release = threading.Event(), threading.Event()
    reconciler = _reconciler(tmp_path, submit=lambda _entry: None, status=lambda _id: None)
    params = SimpleNamespace(use_gateway=True, tui_runtime=TuiRuntime("setting"), control_operation_reconciler=reconciler)

    # LLM: 只拿当前测试对账锁，有限超时确保失败测试也会退出。
    # 函数用途: 模拟另一个 IO 转换正在持锁。
    def hold():
        with reconciler._lock:
            held.set()
            release.wait(1.0)
    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(1)
    try:
        started = time.monotonic()
        assert _tui_submit_control_operation(params, command)
        assert time.monotonic() - started < 0.25
    finally:
        release.set()
        holder.join(2)


# LLM: 旧保存回执只可补历史，不可把已确认的新选择覆盖回去；revision 来自原线程状态。
# 函数用途: 验证模型选择显示拒绝乱序回执。
def test_m4_old_selection_receipt_does_not_replace_new_choice():
    from agent_py_agent.cli.chat_parts.tui_model_menu import _publish_selection
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("revision")
    profiles = [{"id": "B", "model_name": "model-B"}, {"id": "C", "model_name": "model-C"}]
    _publish_selection(runtime, {"ok": True, "profiles": profiles, "selected": "C", "selection_revision": 3})
    _publish_selection(runtime, {"ok": True, "profiles": profiles, "selected": "B", "selection_revision": 2})
    assert runtime.store.snapshot().selected_model_name == "model-C"
    _publish_selection(runtime, {"ok": True, "profiles": profiles, "selected": "C", "selection_revision": 4,
                                "selection_available": False, "warning": "已撤销"})
    assert "不可用" in runtime.store.snapshot().selected_model_name
    _publish_selection(runtime, {"ok": True, "profiles": profiles, "selected": "B", "selection_revision": 2,
                                "selection_available": False, "warning": "过期提示"})
    assert runtime.notice() == "已撤销"


# LLM: 菜单用真实管道按键和本地配置读写，不发网络；选择保存后必须自行收起且不停止正在运行的任务。
# 函数用途: 保存模型后直接回聊天输入，不再要求手动 Esc。
@pytest.mark.parametrize("escape", [False, True])
def test_m1_menu_selection_auto_closes_and_restores_typing(tmp_path, monkeypatch, escape):
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli.chat_parts import tui_model_menu
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
    from agent_py_agent.tests.test_model_profiles import add
    from agent_py_agent.tests.test_thread_model_selection import host_with_store
    from agent_py_agent.tests.test_tui_decision_menu import wait_app, wait_dialog_ready
    from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

    entered, release = threading.Event(), threading.Event()
    original = tui_model_menu._request_data_sync
    from agent_py_agent.cli.chat_parts import tui_control_delivery
    saved_rows = []
    enqueue_setting = tui_control_delivery.TuiControlOperationReconciler.enqueue_setting

    # LLM: 仅观察真实持久队列入口，不替换投递或结果，菜单必须使用同一个会话设置账。
    # 函数用途: 记录菜单保存交给原 outbox 的完整输入。
    def observed(reconciler, entry):
        saved_rows.append(entry)
        return enqueue_setting(reconciler, entry)
    monkeypatch.setattr(tui_control_delivery.TuiControlOperationReconciler, "enqueue_setting", observed)

    # LLM: 只延迟真实保存前的网络边界，不修改保存回执；Esc 不能取消该已发起工作。
    # 函数用途: 让等待状态可通过真实按键观察。
    def delayed(agent, session, operation, payload):
        if escape and operation == "select":
            entered.set()
            release.wait(3)
        return original(agent, session, operation, payload)
    monkeypatch.setattr(tui_model_menu, "_request_data_sync", delayed)

    # LLM: 仅等待当前 UI 的结构化状态，不靠猜测按键结果；finally 始终关闭测试 app。
    # 函数用途: 按菜单路径选中一份真实临时档案并继续编辑文本。
    async def scenario():
        runtime = TuiRuntime("menu-close")
        params = _app_params(tmp_path, runtime)
        host = host_with_store(tmp_path)
        host.config.session_workspace = str(tmp_path / "sessions")
        add(host, model_name="model-B")
        params.agent.home_paths, params.agent.config = host.home_paths, host.config
        params.agent.conversation_store = host.conversation_store
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = make_tui_app(params)
            running = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "启动")
                pipe.send_text("/model\r")
                await wait_dialog_ready(app, "模型配置 /model", "菜单")
                pipe.send_bytes(b"\x1b[B\r")
                await wait_dialog_ready(app, "对话模型（本会话", "选择类型")
                pipe.send_bytes(b"\r")
                await wait_dialog_ready(app, "当前会话模型 ·", "选择档案")
                pipe.send_bytes(b"\x1b[B\r")
                if escape:
                    await wait_app(app, entered.is_set, "提交开始")
                    pipe.send_bytes(b"\x1b")
                    await wait_app(app, lambda: not app._my_agent_model_menu_active, "等待期间退出菜单")
                    assert "确认" in runtime.notice()
                    release.set()
                await wait_app(app, lambda: runtime.store.snapshot().selected_model_name == "model-B", "保存")
                assert not app._my_agent_model_menu_active
                assert not app._my_agent_model_float_container.floats
                pipe.send_text("继续编辑")
                await wait_app(app, lambda: app.current_buffer.text == "继续编辑", "焦点恢复")
                assert not params.stop_event.is_set() and params.jobs.empty()
                assert len(saved_rows) == 1 and saved_rows[0].command_kind == "model"
            finally:
                release.set()
                app.exit()
                await running
    asyncio.run(scenario())


# LLM: 真实本地设置事务与线程文件，不把执行快照当作保存前置；保存后读取版本和时间。
# 函数用途: 确保本地菜单保存是短路径，并给出可比较的持久回执。
def test_m5_local_save_receipt_without_execution_snapshot(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import model_scope
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
    from agent_py_agent.tests.test_thread_model_selection import host_with_store

    host = host_with_store(tmp_path)
    monkeypatch.setattr(model_scope, "thread_model_execution_status", lambda *args: (_ for _ in ()).throw(RuntimeError("snapshot busy")))
    try:
        result = execute_local_model_operation(host, "fast-save", "select", {"profile_id": "default"})
    except RuntimeError:
        result = {"ok": False}
    assert result["ok"], "保存仍依赖执行快照"
    thread = host.conversation_store.threads.require(result["thread_id"])
    assert result["selection_revision"] == thread.model_selection_revision
    assert result["saved_at"] == thread.model_selection_selected_at
    assert result["adoption_status"] == "pending"


# LLM: 本机真实 HTTP/C/原 source/原 receipt；只把执行状态快照设为失败，保存不应调用它。
# 函数用途: 多进程冷菜单也能快速保存，并且原控制操作幂等。
@pytest.mark.parametrize("route", ["/client/models", "/control"])
def test_m5_coordinator_save_skips_slow_snapshot(tmp_path, monkeypatch, route):
    from agent_py_agent.agent.gateway_parts import model_metadata_transaction as domain
    from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
    from agent_py_agent.tests.test_coordinator_model_menu import model_body, post_model

    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, host, _path, row, _body, server, _runtime, _sent = case
        monkeypatch.setattr(domain, "model_execution_snapshot", lambda *args: (_ for _ in ()).throw(RuntimeError("snapshot busy")))
        selected = "b00f0289-a209-40fe-a3b7-a01cdf972abb"
        body = ({**model_body(operation="select"), "profile_id": selected} if route == "/client/models"
                else model_body(command=f"/model {selected}"))
        status, result = post_model(server, body, route=route, actor=row["actor_id"])
        assert status == 200 and result["ok"], result
        receipt = result if route == "/client/models" else result["details"]
        assert receipt["selected"] == selected and receipt["selection_revision"] > 0
        assert receipt["saved_at"] > 0 and receipt["adoption_status"] == "pending"
        thread = host.conversation_store.threads.require(receipt["thread_id"])
        assert thread.model_profile_id == selected and not delivered
        if route == "/control":
            assert post_model(server, body, route=route, actor=row["actor_id"])[1] == result


# LLM: 真实冷 C 的同一设置权限路径；探测入口若被调用立即失败，保存不依赖加载业务 Agent。
# 函数用途: 档位设置作为纯元数据操作保存，而非排队等待业务工作者。
def test_m6_cold_effort_save_without_probe_or_business_agent(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings import reasoning_probe
    from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
    from agent_py_agent.tests.test_coordinator_model_menu import model_body, post_model

    monkeypatch.setattr(reasoning_probe, "effort_probe_lines", lambda *args: pytest.fail("保存不得探测"))
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, host, _path, row, _body, server, _runtime, _sent = case
        body = model_body(command="/effort high")
        status, result = post_model(server, body, route="/control", actor=row["actor_id"])
        assert status == 200 and result["ok"], result
        thread = host.conversation_store.threads.require(result["details"]["thread_id"])
        assert thread.reasoning_effort == "high"
        assert post_model(server, body, route="/control", actor=row["actor_id"])[1] == result
        assert server.agent is None and not delivered


# LLM: 同一个逻辑请求在冻结点后不重新读档位，下一次安全点才采用新值；直接使用原模型依赖作用域。
# 函数用途: 防止模型与档位跨两次读取拼成从未存在的配置，重试也保持原档位。
def test_m7_reasoning_is_frozen_with_model_at_request_boundary(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.model.selection_refresh import refresh_selected_model
    from agent_py_agent.agent.settings.model_scope import (
        model_dependency_lifetime,
        selected_model_scope,
    )
    from agent_py_agent.agent.settings.reasoning_effort import (
        run_reasoning_level,
        set_thread_reasoning_level,
    )
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
    from agent_py_agent.tests.test_model_selection_history import _params
    from agent_py_agent.tests.test_thread_model_selection import host_with_store

    host = host_with_store(tmp_path)
    row = execute_local_model_operation(host, "freeze", "select", {"profile_id": "default"})
    params = replace(_params(live_archive_state={}), task_attributes={"conversation_thread_id": row["thread_id"]})
    set_thread_reasoning_level(host.conversation_store, row["thread_id"], "low")
    with selected_model_scope(host, thread_id=row["thread_id"]), model_dependency_lifetime(host):
        refresh_selected_model(host, params)
        set_thread_reasoning_level(host.conversation_store, row["thread_id"], "high")
        assert run_reasoning_level(host, params) == "low"
        refresh_selected_model(host, params)
        assert run_reasoning_level(host, params) == "high"


# LLM: 磁盘错误只自动尝试三次；用户通过实际键盘 F8 明确恢复同一输入，不能换 ID 或无限循环。
# 函数用途: 验证暂停保存有真实 UI 恢复入口且不丢掉排队消息。
def test_m3_paused_persistence_recovers_original_identity_via_keyboard(tmp_path, monkeypatch):
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
    from agent_py_agent.tests.test_tui_decision_menu import wait_app
    from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

    attempts, errors = [], []
    reconciler = _reconciler(tmp_path, submit=lambda _entry: None, status=lambda _id: None)
    reconciler.stop_event.clear()
    original = reconciler.enqueue
    disk_ready, persisted = threading.Event(), threading.Event()

    # LLM: 仅在原 enqueue 前模拟磁盘故障；恢复后真实落盘，不替换身份和序号。
    # 函数用途: 制造有限失败并记录实际尝试的 ID。
    def persist(entry, *, on_persisted_before_dispatch=None):
        attempts.append(entry.message_id)
        if not disk_ready.is_set():
            raise OSError("disk busy")
        original(entry, on_persisted_before_dispatch=on_persisted_before_dispatch)
        persisted.set()
    monkeypatch.setattr(reconciler, "enqueue", persist)
    reconciler.on_error = errors.append
    monkeypatch.setattr(tui_actions, "_ensure_control_operation_reconciler", lambda params: setattr(params, "control_operation_reconciler", reconciler) or reconciler)

    # LLM: 真管道键盘触发恢复，不直接调用 retry_pending 冒充用户入口。
    # 函数用途: 操作当前 app 并等待原 ID 持久成功。
    async def scenario():
        runtime = TuiRuntime("recover-setting")
        from dataclasses import replace
        params = replace(_app_params(tmp_path, runtime), use_gateway=True)
        params.paths.root = tmp_path
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = make_tui_app(params)
            running = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "启动")
                pipe.send_text("/effort high\r")
                await wait_app(app, lambda: len(attempts) == 3, "三次有限保存尝试")
                await asyncio.sleep(0.3)
                assert len(attempts) == 3, "持续错误不能无限自动重试"
                original_id = attempts[0]
                disk_ready.set()
                pipe.send_bytes(b"\x1b[19~")
                await wait_app(app, persisted.is_set, "F8 恢复原保存")
                assert attempts == [original_id] * 4
                assert list(reconciler._read_entries()) == [original_id]
            finally:
                reconciler.stop_event.set()
                app.exit()
                await running
    asyncio.run(scenario())


# LLM: 原 outbox 满时必须仍能编辑原命令，不能因控制函数返回 handled 就丢掉草稿。
# 函数用途: 真实 Enter 的队满拒绝保留用户输入。
def test_m3_full_enqueue_keeps_real_input_draft(tmp_path, monkeypatch):
    import asyncio
    from queue import Full

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
    from agent_py_agent.tests.test_tui_decision_menu import wait_app
    from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

    reconciler = _reconciler(tmp_path, submit=lambda _entry: None, status=lambda _id: None)
    monkeypatch.setattr(reconciler, "enqueue_setting", lambda _entry: (_ for _ in ()).throw(Full()))
    monkeypatch.setattr(tui_actions, "_ensure_control_operation_reconciler", lambda params: setattr(params, "control_operation_reconciler", reconciler) or reconciler)

    # LLM: 只模拟队列拒绝，按键和实际 buffer 均使用产品实现。
    # 函数用途: 检查提交拒绝后的草稿和提示。
    async def scenario():
        runtime = TuiRuntime("full-setting")
        from dataclasses import replace
        params = replace(_app_params(tmp_path, runtime), use_gateway=True)
        params.paths.root = tmp_path
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = make_tui_app(params)
            running = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "启动")
                pipe.send_text("/effort high\r")
                await wait_app(app, lambda: "无法保存" in runtime.notice(), "拒绝提示")
                assert app.current_buffer.text == "/effort high"
                assert not reconciler._read_entries()
            finally:
                app.exit()
                await running
    asyncio.run(scenario())


# LLM: 本机真实 HTTP/C/原收据及 CAS；同 ID 不可换 revision，旧菜单不能覆盖新窗口选择。
# 函数用途: 验证 Gateway 菜单控制沿原消息携带完整 CAS 输入。
def test_m4_gateway_menu_keeps_revision_and_identity(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
    from agent_py_agent.tests.test_coordinator_model_menu import model_body, post_model

    with delivery_case(tmp_path, monkeypatch) as (case, _delivered):
        _agent, _host, _path, row, _body, server, _runtime, _sent = case
        profile = "b00f0289-a209-40fe-a3b7-a01cdf972abb"
        body = model_body(command=f"/model {profile}")
        body["metadata"]["model_payload"] = {"profile_id": profile, "expected_revision": 999}
        status, result = post_model(server, body, route="/control", actor=row["actor_id"])
        assert status == 200 and not result["ok"], result
        assert post_model(server, body, route="/control", actor=row["actor_id"])[1] == result
        changed = {**body, "metadata": {**body["metadata"], "model_payload": {"profile_id": profile, "expected_revision": 0}}}
        status, conflict = post_model(server, changed, route="/control", actor=row["actor_id"])
        assert status == 409 or conflict.get("control_state") == "conflict"
