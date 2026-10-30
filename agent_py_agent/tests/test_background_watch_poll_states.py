# LLM: 只用临时目录、真实导航/提示与确定性请求交错；不启动服务、改生产记录或授予进程控制权限。
# 模块用途: 按BG-2合同回归快照新鲜度、可信绑定和面板告警的独立状态。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_process_view as bg
from agent_py_agent.cli.chat_parts import tui_threading as tui
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


# LLM: 原消费入口和真实导航保持，只替换进程IO与HTTP边界；记录reader参数以检查可信owner接线。
# 函数用途: 建立已成功绑定的Gateway观察并可切换后续响应。
def gateway_watch(tmp_path, monkeypatch, *, panel=False, first=True):
    runtime = TuiRuntime("poll-states")
    nav = TuiAgentNavigationState(runtime)
    payload = {"ok": True, "thread_id": "thread-one", "notices": [], "cursor": 0,
               "transcript_events": [], "event_cursor": 0, "event_stream_id": "", "execution_target": None}
    state = {"response": payload, "lists": [], "details": [], "seen": set(), "cursor": [0]}
    row = {"session_id": "bg-one", "thread_id": "thread-one", "command": "可信旧命令",
           "status": "running", "alive": True, "wake_pending": False, "stdout_tail": "可信旧日志"}

    # LLM: 只模拟HTTP返回或真实异常类型，不通过文案提供权限/绑定事实。
    # 函数用途: 在同一消费入口注入后续轮询故障。
    def fetch(*_args, **_kwargs):
        value = state["response"]
        if isinstance(value, BaseException):
            raise value
        return value() if callable(value) else dict(value)

    # LLM: 返回完整成功信封，列表调用必须精确绑定thread且携宿主owner。
    # 函数用途: 记录新列表读取次数及可信上下文。
    def listing(*args, **kwargs):
        state["lists"].append((args, kwargs))
        return {"projection_ok": True, "items": [dict(row)], "active_count": 1, "errors": []}

    # LLM: 保留真实导航进入面板的选择流程；不读取任意日志路径。
    # 函数用途: 记录仅在面板打开后发生的详情读取。
    def detail(*args, **kwargs):
        state["details"].append((args, kwargs))
        return dict(row)

    monkeypatch.setattr(bg, "list_background_processes", listing)
    monkeypatch.setattr(bg, "inspect_background_process", detail)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path, owner_id="owner-one"),
                            request_background_notices=fetch)
    if first:
        assert poll(agent, runtime, nav, state)
    if panel:
        if not first:
            nav.update_background_watch_rows([row], active_count=1)
        assert nav.move_selection(1) and nav.enter_selected()
        if not first:
            nav.update_background_watch_rows([], active_count=0)
    return agent, runtime, nav, state


# LLM: 同一游标/去重对象贯穿所有轮询，外层返回必须仍为原bool。
# 函数用途: 通过生产通知入口执行一轮确定性观察。
def poll(agent, runtime, nav, state, session="display"):
    result = tui._consume_background_notices(agent, session, runtime, [None], state["seen"], state["cursor"], nav)
    assert type(result) is bool
    return result


# LLM: 直接走产品渲染器而非拼装提示文字，面板状态和notice来自同一真实导航runtime。
# 函数用途: 获取用户底部实际可见文字，核对提示所有权和陈旧状态文案。
def rendered_footer(nav):
    from agent_py_agent.cli.chat_parts.tui_block_renderer import (
        TuiRenderContext,
        render_tui_snapshot,
    )

    runtime = nav.active_runtime()
    frame = render_tui_snapshot(runtime.store.snapshot(), TuiRenderContext(
        width=240, notice=runtime.notice(), background_watch_active=nav.snapshot().background_watch_active))
    return "".join(fragment[1] for fragment in frame.footer)


# LLM: 可信运输异常与机器瞬态字段只影响快照新鲜度，连续失败不得清绑定或触发新进程读取。
# 函数用途: 验证主页面连续失败静默与同范围恢复。
@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionResetError(),
    {"ok": False, "binding_state": "unavailable", "failure_kind": "transport_transient"}])
def test_r11_main_transport_failure_keeps_binding_without_new_reads(tmp_path, monkeypatch, failure):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    old_rows, old_scope = nav._background_rows, nav._background_watch_scope
    good = state["response"]
    state["response"] = failure
    for _ in range(5):
        assert not poll(agent, runtime, nav, state)
        assert nav._background_rows is old_rows and nav._background_watch_scope == old_scope
        assert tui._background_watch_thread_id(agent, nav, session_id="display", runtime=runtime) == "thread-one"
        assert runtime.notice() == ""
    assert len(state["lists"]) == 1 and not state["details"]
    state["response"] = good
    assert poll(agent, runtime, nav, state) and len(state["lists"]) == 2


# LLM: 使用真实面板runtime与monotonic截止时间，重复故障必须不续期，不以仅检测调用替代渲染状态。
# 函数用途: 验证面板两次宽限、第三次单提示及成功后的清理。
def test_r12_panel_has_two_grace_polls_then_one_stale_notice(tmp_path, monkeypatch):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch, panel=True)
    good = state["response"]
    state["response"] = TimeoutError()
    for count in (1, 2, 3, 4):
        assert not poll(agent, runtime, nav, state)
        assert nav._bg_watch_consecutive_transient_failures == count
        assert nav._background_watch_stale is True
        if count < 3:
            assert runtime.notice() == ""
        elif count == 3:
            assert "仅保留同会话上次结果" in nav.active_runtime().notice()
            assert "尚未刷新" in rendered_footer(nav)
            deadline = nav.active_runtime()._notice_until
        else:
            assert nav.active_runtime()._notice_until == deadline
    assert len(state["lists"]) == 1 and not state["details"]
    state["response"] = good
    assert poll(agent, runtime, nav, state)
    assert nav.active_runtime().notice() == "" and not nav._background_watch_stale
    assert nav._bg_watch_consecutive_transient_failures == 0


# LLM: 没有成功快照不能声称有旧帧，面板空态与主页面安静必须分开核对。
# 函数用途: 断言首次运输失败没有伪造保留结果或读取owner全量。
@pytest.mark.parametrize("panel", [False, True])
def test_r13_no_valid_frame_never_claims_retained_results(tmp_path, monkeypatch, panel):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch, panel=panel, first=False)
    state["response"] = TimeoutError()
    assert not poll(agent, runtime, nav, state)
    assert not state["lists"] and nav._background_rows == ()
    assert "保留" not in nav.active_runtime().notice()
    assert ("暂未取得结果" in nav.active_runtime().notice()) if panel else runtime.notice() == ""


# LLM: 真实权限类型和机器owner码优先，混入瞬态标签也不能借旧帧继续展示。
# 函数用途: 验证主页面/面板拒绝时立即清资格，随后超时不复活旧结果。
@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize("failure", [PermissionError(),
    {"ok": False, "binding_state": "invalid", "failure_kind": "owner_mismatch"},
    {"ok": False, "binding_state": "unavailable", "failure_kind": "transport_transient", "reason_code": "OWNER_MISMATCH"}])
def test_r14_hard_refusal_revokes_frame_and_binding(tmp_path, monkeypatch, panel, failure):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch, panel=panel)
    state["response"] = failure
    assert not poll(agent, runtime, nav, state)
    assert nav._background_rows == () and nav._background_watch_scope is None
    assert nav._background_watch_valid_scope is None and nav.snapshot().background_process_count == 0
    assert "权限或归属校验失败" in nav.active_runtime().notice()
    assert tui._background_watch_thread_id(agent, nav, session_id="display", runtime=runtime) is None
    state["response"] = TimeoutError()
    assert not poll(agent, runtime, nav, state)
    assert nav._background_rows == () and len(state["lists"]) == 1


# LLM: home、owner、显示session与reset任一变化都会失去旧帧资格，线程字符串相同也不例外。
# 函数用途: 验证完整范围变化后的首次失败静默清旧数据。
@pytest.mark.parametrize("change", ["session", "owner_id", "owner_home", "reset"])
def test_r16_transient_cannot_borrow_frame_across_full_context(tmp_path, monkeypatch, change):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    during_fetch = []

    # LLM: 先断言请求尚未返回时旧帧已经撤销，不能只在finally事后清空来掩盖跨范围显示窗口。
    # 函数用途: 模拟新范围的真实取数失败并复核IO前清帧。
    def failed_fetch():
        during_fetch.append((nav._background_rows, nav._background_watch_valid_scope))
        return {"ok": False, "binding_state": "unavailable", "failure_kind": "transport_transient"}

    state["response"] = failed_fetch
    if change == "owner_id":
        agent.home_paths.owner_id = "owner-two"
    elif change == "owner_home":
        agent.home_paths.owner_home_dir = tmp_path / "other"
    elif change == "reset":
        runtime.publish_conversation_reset("thread-new")
    assert not poll(agent, runtime, nav, state, "new-display" if change == "session" else "display")
    assert during_fetch == [((), None)]
    assert nav._background_rows == () and nav._background_watch_valid_scope is None
    assert runtime.notice() == "" and len(state["lists"]) == 1


# LLM: 旧HTTP响应只沿原reset围栏丢弃，外层bool仍沿原合同，不准碰新绑定、帧、提示或游标。
# 函数用途: 构造确定的新旧轮询交错并核对所有可见残留。
def test_r17_late_reset_response_does_not_touch_new_frame_or_notice(tmp_path, monkeypatch):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    good = state["response"]
    expected = {}

    # LLM: HTTP在锁外期间真实reset并完成新代轮询，旧响应随后才返回；不靠sleep制造竞态。
    # 函数用途: 证明旧成功或旧失败均不能发表到新一代。
    def late():
        runtime.publish_conversation_reset("thread-new")
        state["response"] = {**good, "thread_id": "thread-new"}
        assert poll(agent, runtime, nav, state)
        runtime.set_notice("新代别的功能错误", notice_kind="other_error", duration_seconds=30)
        expected["rows"] = nav._background_rows
        expected["scope"] = nav._background_watch_scope
        expected["binding"] = runtime._background_watch_gateway_binding
        return {**good, "thread_id": "old-thread", "cursor": 999}

    state["response"] = late
    # 原reset围栏把已正确丢弃的旧页返回True，保留既有外层bool/退避合同。
    assert poll(agent, runtime, nav, state)
    assert nav._background_rows is expected["rows"] and nav._background_watch_scope == expected["scope"]
    assert runtime._background_watch_gateway_binding == expected["binding"]
    assert runtime.background_message_cursor == 0 and state["cursor"] == [0] and state["seen"] == set()
    assert runtime.notice() == "新代别的功能错误"


# LLM: 子代理页失败不证明主线程失信，父页已成功的游标/DTO合同不回退。
# 函数用途: 确认子视图故障不清主范围且不读取新进程列表。
def test_r17_child_failure_does_not_invalidate_confirmed_main_scope(tmp_path, monkeypatch):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    monkeypatch.setattr(tui, "_consume_selected_agent_view", lambda *a, **kw: (False, False))
    old_scope = nav._background_watch_scope
    assert not poll(agent, runtime, nav, state)
    assert nav._background_watch_scope == old_scope and nav.snapshot().background_process_count == 1
    assert runtime.notice() == "" and len(state["lists"]) == 1


# LLM: 普通ValueError没有安全拒绝和瞬态资格，不按包含owner字样的文案升级权限错误。
# 函数用途: 核对未知错误立即诊断且只保留已验证同范围帧。
@pytest.mark.parametrize("panel", [False, True])
def test_r18_unknown_failure_is_not_transient_or_owner_refusal(tmp_path, monkeypatch, panel):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch, panel=panel)
    state["response"] = ValueError("not a machine-readable owner refusal")
    assert not poll(agent, runtime, nav, state)
    assert nav.snapshot().background_process_count == 1
    assert nav._bg_watch_consecutive_transient_failures == 0
    assert "仅保留同会话上次结果" in nav.active_runtime().notice()
    assert "权限" not in nav.active_runtime().notice() and "范围未确认" not in nav.active_runtime().notice()


# LLM: 提示所有权由kind决定，观察失败、恢复和离开面板均不能吞其它功能提示。
# 函数用途: 用真实notice文字与截止时间验证其它权限错误持续保留。
def test_r19_other_notice_is_neither_overwritten_nor_cleared(tmp_path, monkeypatch):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch, panel=True)
    good = state["response"]
    runtime.set_notice("其他权限错误", notice_kind="permission_error", duration_seconds=30)
    nav.active_runtime().set_notice("其他权限错误", notice_kind="permission_error", duration_seconds=30)
    deadline = nav.active_runtime()._notice_until
    state["response"] = TimeoutError()
    for _ in range(4):
        assert not poll(agent, runtime, nav, state)
        assert nav.active_runtime().notice() == "其他权限错误" and nav.active_runtime()._notice_until == deadline
        assert "其他权限错误" in rendered_footer(nav)
    assert nav.back()
    state["response"] = good
    assert poll(agent, runtime, nav, state)
    assert runtime.notice() == "其他权限错误"
    assert "其他权限错误" in rendered_footer(nav)


# LLM: reader上下文必须来自agent.home_paths，不从进程JSON或显示行猜owner。
# 函数用途: 核对列表与详情均携宿主可信owner与精确线程。
def test_trusted_owner_is_passed_to_list_and_detail(tmp_path, monkeypatch):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch, panel=True)
    assert poll(agent, runtime, nav, state)
    for _args, kwargs in state["lists"] + state["details"]:
        assert kwargs["expected_owner_id"] == "owner-one" and kwargs["thread_id"] == "thread-one"


# LLM: 非法thread/target形状和冲突必须先于任何列表/日志IO拒绝，不能当正常空会话或未知运输抖动。
# 函数用途: 验证Gateway真实绑定坏类型与线程冲突仍清帧告警。
@pytest.mark.parametrize("extra", [{"thread_id": []}, {"thread_id": " "}, {"execution_target": 42},
    {"execution_target": {"thread_id": []}}, {"execution_target": {"thread_id": "thread-other"}}])
def test_r15_gateway_invalid_binding_never_reads_processes(tmp_path, monkeypatch, extra):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    state["response"] = {**state["response"], **extra}
    assert not poll(agent, runtime, nav, state)
    assert nav._background_rows == () and nav._background_watch_valid_scope is None
    assert "会话范围未确认" in runtime.notice() and len(state["lists"]) == 1 and not state["details"]


# LLM: 真实临时ConversationStore和原冷/热入口一致区分坏索引与页读取失败，不修改生产账本。
# 函数用途: 验证服务端绑定状态、线程与失败种类不依赖error文案。
@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("failure", ["binding", "page"])
def test_server_machine_reason_distinguishes_binding_from_page(tmp_path, monkeypatch, cold, failure):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import http_handlers as http
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    owner = OwnerIdentity.provider_user("poll-test", "reader")
    home = resolve_owner_home(tmp_path, owner).home_dir
    store = ConversationStore(home / "workspace/runtime/workspaces/workspace-poll/conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "reader", "channel": "poll-test",
        "channel_conversation_id": "display", "channel_user_id": "reader"})
    if failure == "binding":
        store.storage.bindings_path.write_text("broken-index")
    else:
        monkeypatch.setattr(http, "read_background_response_page", lambda *a, **kw: ([], 0, False))
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path, owner_home_dir=home), conversation_store=store)
    monkeypatch.setattr(http, "resolve_loaded_gateway_scope_agent", lambda *a: None if cold else agent)
    payload = http.read_gateway_client_notices(agent,
        scope=GatewayControlScope("reader", "poll-test", "display", resolved_owner=owner), after=0)
    assert payload["ok"] is False
    assert payload["binding_state"] == ("invalid" if failure == "binding" else "confirmed")
    assert payload["failure_kind"] == ("binding_invalid" if failure == "binding" else "corrupt_or_unknown")
    assert payload["thread_id"] == ("" if failure == "binding" else thread.thread_id)


# LLM: HTTP状态来自原运输回执；即便正文ok=True，401/403仍必须硬拒绝，繁忙不成为绑定失信。
# 函数用途: 验证客户端保留可判定的HTTP失败原因。
@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_client_http_status_never_depends_on_error_prose(status):
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent

    client = SimpleNamespace(post_gateway_json=lambda *a, **kw: (status, {"error": "arbitrary text", "ok": True}))
    payload = GatewayChatClientAgent.request_background_notices(client, "display", after=7)
    assert payload["ok"] is False
    assert payload["failure_kind"] == ("permission_denied" if status in {401, 403} else "transport_transient")
    assert payload["binding_state"] == ("invalid" if status in {401, 403} else "unavailable")


# LLM: 本地主线程解析真实完成，只有消息页IO失败；不能因通知页失败把可信范围重新标invalid。
# 函数用途: 回归本地消息页坏快照和子视图失败的独立绑定语义。
@pytest.mark.parametrize("failure", ["messages", "child"])
def test_r17_local_page_or_child_failure_keeps_confirmed_binding(tmp_path, monkeypatch, failure):
    from agent_py_agent.agent.conversation import (
        agent_activity,
        background_transcript,
        message_stream,
    )

    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch, first=False)
    del agent.request_background_notices
    agent.conversation_store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path),
        threads=SimpleNamespace(resolve_report=lambda **kw: (SimpleNamespace(thread_id="thread-one"), None)))
    monkeypatch.setattr(agent_activity, "conversation_agent_activity", lambda *a: SimpleNamespace(to_dict=lambda: {"active_task_count": 0}))
    monkeypatch.setattr(background_transcript, "read_background_transcript_events", lambda *a, **kw: {"events": [], "cursor": 0, "stream_id": ""})
    page_ok = [True]
    monkeypatch.setattr(message_stream, "read_background_response_page", lambda *a, **kw: ([], 0, page_ok[0]))
    assert poll(agent, runtime, nav, state)
    old_rows, old_scope = nav._background_rows, nav._background_watch_scope
    if failure == "messages":
        page_ok[0] = False
    else:
        monkeypatch.setattr(tui, "_consume_selected_agent_view", lambda *a, **kw: (False, False))
    assert not poll(agent, runtime, nav, state)
    assert nav._background_rows is old_rows and nav._background_watch_scope == old_scope
    assert "范围未确认" not in runtime.notice() and "权限" not in runtime.notice()
    assert len(state["lists"]) == 1 and state["cursor"] == [0] and not state["seen"]


# LLM: 单次Gateway主快照已确认thread但消息页失败；显式机器字段不能折叠为身份失信。
# 函数用途: 核对主消息投影失败仍保留同范围帧且不读取新进程数据。
def test_r17_gateway_confirmed_binding_survives_failed_message_page(tmp_path, monkeypatch):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    old_rows = nav._background_rows
    state["response"] = {**state["response"], "ok": False, "binding_state": "confirmed", "failure_kind": "corrupt_or_unknown"}
    assert not poll(agent, runtime, nav, state)
    assert nav._background_rows is old_rows and len(state["lists"]) == 1
    assert "仅保留同会话上次结果" in runtime.notice() and "范围未确认" not in runtime.notice()


# LLM: 实际列表IO期间reset且新代已发表后，旧列表的成功与硬拒绝都不能落到新帧；不靠sleep调度。
# 函数用途: 复核刷新返回前的完整上下文围栏，而非只测试HTTP围栏。
@pytest.mark.parametrize("denied", [False, True])
def test_r17_late_list_result_cannot_replace_new_generation(tmp_path, monkeypatch, denied):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    listing = bg.list_background_processes
    saved = {}

    # LLM: 保留原确定性成功reader，嵌套轮询只用于构造明确的新代发表交错。
    # 函数用途: 在旧列表返回前真实reset并发表新范围与其它kind提示。
    def late_list(*args, **kwargs):
        monkeypatch.setattr(bg, "list_background_processes", listing)
        runtime.publish_conversation_reset("new-thread")
        state["response"] = {**state["response"], "thread_id": "new-thread"}
        assert poll(agent, runtime, nav, state)
        saved["rows"], saved["scope"] = nav._background_rows, nav._background_watch_scope
        runtime.set_notice("新代用户提示", notice_kind="interaction", duration_seconds=30)
        if denied:
            raise PermissionError()
        return listing(*args, **kwargs)

    monkeypatch.setattr(bg, "list_background_processes", late_list)
    assert poll(agent, runtime, nav, state)
    assert nav._background_rows is saved["rows"] and nav._background_watch_scope == saved["scope"]
    assert runtime.notice() == "新代用户提示"


# LLM: 使用真实冷投影产生错误信封，再交真实TUI消费；缺宿主根不能伪装成功空绑定。
# 函数用途: 回归冷owner无数据根时保留原可信帧但明确快照失败。
def test_review_cold_owner_missing_root_is_not_successful_empty_binding(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import http_handlers as http
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope

    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    old_rows, old_scope = nav._background_rows, nav._background_watch_scope
    base = SimpleNamespace(home_paths=SimpleNamespace(root=None))
    monkeypatch.setattr(http, "resolve_loaded_gateway_scope_agent", lambda *a: None)
    payload = http.read_gateway_client_notices(base,
        scope=GatewayControlScope("reader", "poll-test", "display"), after=0)
    state["response"] = payload
    assert not poll(agent, runtime, nav, state)
    assert payload["ok"] is False and payload["snapshot_ok"] is False
    assert payload["binding_state"] == "unavailable" and payload["failure_kind"] == "corrupt_or_unknown"
    assert nav._background_rows is old_rows and nav._background_watch_scope == old_scope
    assert len(state["lists"]) == 1


# LLM: 原sync到刷新调用之间若已有新代发表，旧scope不能重新采样新context为自己背书；不依靠sleep。
# 函数用途: 覆盖列表IO之前发生reset的窗口，旧刷新不得清新帧或多读列表。
def test_review_reset_before_refresh_cannot_reauthorize_old_scope(tmp_path, monkeypatch):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    original = tui._refresh_background_watch
    saved = {}

    # LLM: 只在真实刷新调用边界确定性交错；新轮询经原入口建立新scope。
    # 函数用途: 新代先完成，随后放行旧调用以验证其开头围栏。
    def reset_then_refresh(*args, **kwargs):
        monkeypatch.setattr(tui, "_refresh_background_watch", original)
        runtime.publish_conversation_reset("new-thread")
        state["response"] = {**state["response"], "thread_id": "new-thread"}
        assert poll(agent, runtime, nav, state)
        saved["rows"], saved["scope"] = nav._background_rows, nav._background_watch_scope
        runtime.set_notice("新代用户提示", notice_kind="interaction", duration_seconds=30)
        return original(*args, **kwargs)

    monkeypatch.setattr(tui, "_refresh_background_watch", reset_then_refresh)
    assert poll(agent, runtime, nav, state)
    assert nav._background_rows is saved["rows"] and nav._background_watch_scope == saved["scope"]
    assert runtime.notice() == "新代用户提示" and len(state["lists"]) == 2


# LLM: 真实scope读取的第二次访问位于入口锁之后，确定性交错新帧发表，不篡改任何范围检查结果。
# 函数用途: 覆盖reset与同代换线程在入口检查后发生时旧刷新不得清帧。
@pytest.mark.parametrize("change", ["reset", "thread"])
def test_review_scope_changed_after_entry_guard_never_clears_new_frame(tmp_path, monkeypatch, change):
    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    old_scope = nav._background_watch_scope
    original_get = type(nav).__getattribute__
    reads, saved = [0], {}

    # LLM: 只观察原属性读；第二次读返回最新真实值前先完成新轮询，模拟释放入口锁后的抢占。
    # 函数用途: 稳定复现检查与锁外scope写入之间的窗口，不使用时间等待。
    def interleaved_get(obj, name):
        if obj is nav and name == "_background_watch_scope" and reads[0] < 2:
            reads[0] += 1
            if reads[0] == 2:
                if change == "reset":
                    runtime.publish_conversation_reset("new-thread")
                state["response"] = {**state["response"], "thread_id": "new-thread"}
                assert poll(agent, runtime, nav, state)
                saved["rows"] = nav._background_rows
                saved["scope"] = nav._background_watch_scope
                saved["valid"] = nav._background_watch_valid_scope
                runtime.set_notice("新帧用户提示", notice_kind="interaction", duration_seconds=30)
        return original_get(obj, name)

    monkeypatch.setattr(type(nav), "__getattribute__", interleaved_get)
    tui._refresh_background_watch(agent, nav, thread_id="thread-one", scope=old_scope)
    assert saved and nav._background_rows is saved["rows"]
    assert nav._background_watch_scope == saved["scope"] and nav._background_watch_valid_scope == saved["valid"]
    assert runtime.notice() == "新帧用户提示"


# LLM: 同代换线程在真实同步的列表IO期间发生，迟到成功和权限异常都不得修改新线程显示或提示。
# 函数用途: 确认sync的成功清提示和失败清帧出口也受原scope围栏约束。
@pytest.mark.parametrize("late_error", [False, True])
def test_review_thread_changed_during_list_drops_old_sync_cleanup(tmp_path, monkeypatch, late_error):
    from agent_py_agent.agent.conversation import background_process_view as bg

    agent, runtime, nav, state = gateway_watch(tmp_path, monkeypatch)
    listing, saved = bg.list_background_processes, {}

    # LLM: 包装原测试列表读，读后建立新线程帧；不伪造poll report或修改检查返回值。
    # 函数用途: 固定旧IO返回之前新线程已发表的交错。
    def list_then_switch(*args, **kwargs):
        result = listing(*args, **kwargs)
        monkeypatch.setattr(bg, "list_background_processes", listing)
        state["response"] = {**state["response"], "thread_id": "new-thread"}
        assert poll(agent, runtime, nav, state)
        saved["rows"], saved["scope"] = nav._background_rows, nav._background_watch_valid_scope
        runtime.set_notice("新线程观察提示", notice_kind="background_watch_error", duration_seconds=30)
        if late_error:
            raise PermissionError("迟到的旧线程拒绝")
        return result

    monkeypatch.setattr(bg, "list_background_processes", list_then_switch)
    poll(agent, runtime, nav, state)
    assert nav._background_rows is saved["rows"] and nav._background_watch_valid_scope == saved["scope"]
    assert nav._background_watch_scope == saved["scope"] and runtime.notice() == "新线程观察提示"
