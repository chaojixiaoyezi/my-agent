# LLM: 仅隔离资料与本地模型；真实认证、HTTP、writer、worker 和会话库不替换，不操作生产服务。
# 模块用途: 锁定管理员请求级路径展示、轮询者边界及连续会话，不改变通道路由。
from __future__ import annotations

import ast
import http.client
import inspect
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
from agent_py_agent.agent.conversation import channels
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import http_handlers, request_history
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.stream_writer import (
    BufferedChunkStreamWriter,
    request_stream_permission,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_committed_model_messages import _commit_writer_fixture

PATH_TEXT = "${APP_ROOT}/Antigravity.app/Contents/MacOS/Antigravity"
CASES = [("chat", False, "", True), ("http", True, "p2p", True),
         ("http", False, "p2p", False), ("feishu", True, "p2p", False),
         ("http", True, "group", False)]


# LLM: 策略必须只读结构化权限，缺接口以断言报告，不用导入错误替代回归红。
# 函数用途: 取得唯一纯展示策略。
def _visible(channel, admin, chat_type):
    helper = getattr(channels, "host_paths_visible_to_viewer", None)
    assert callable(helper), "missing structured viewer policy"
    return helper(channel, can_access_all_users=admin, chat_type=chat_type)


# LLM: 展示资格只由通道、结构化权限和聊天类型决定，矩阵不得从用户名或正文推断。
# 函数用途: 验证本地、HTTP、IM和群的展示边界。
@pytest.mark.parametrize("channel,admin,chat_type,full", CASES)
def test_viewer_policy_matrix(channel, admin, chat_type, full):
    assert _visible(channel, admin, chat_type) is full


# LLM: 冻结旧位置参数与默认False；完整消息才附说明，无条件写时脱敏不得随权限变化。
# 函数用途: 核验四个投影接口的签名兼容和路径说明规则。
@pytest.mark.parametrize("channel,admin,chat_type,full", CASES)
def test_projectors_are_keyword_compatible_and_whole_message_note_only(channel, admin, chat_type, full):
    for projector in (channels.project_host_paths_report, channels.project_host_paths_for_channel,
                      channels.project_message_paths_for_channel, channels.redact_structured_identifiers):
        parameter = inspect.signature(projector).parameters.get("preserve_host_paths")
        assert parameter is not None and parameter.kind == inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is False
    flag = _visible(channel, admin, chat_type)
    expected = PATH_TEXT if full else "${APP_ROOT}Antigravity"
    projected, count = channels.project_host_paths_report(PATH_TEXT, channel, preserve_host_paths=flag)
    assert projected == expected and count == (0 if full else 1)
    assert channels.project_host_paths_for_channel(PATH_TEXT, channel, preserve_host_paths=flag) == expected
    whole = channels.project_message_paths_for_channel(PATH_TEXT, channel, preserve_host_paths=flag)
    assert whole == expected + ("" if full else "\n\n" + channels.HOST_PATH_REDACTION_NOTE)
    # 旧位置调用仍沿私有通道策略；显式标志不能改变无条件写时投影。
    assert channels.project_host_paths_for_channel(PATH_TEXT, channel) == (
        PATH_TEXT if channel in channels.LOCAL_PRIVATE_CHANNELS else "${APP_ROOT}Antigravity")
    assert channels.redact_host_absolute_paths(PATH_TEXT) == "${APP_ROOT}Antigravity"


# LLM: 保留宿主路径不豁免正文中的其它身份净化。
# 函数用途: 验证路径中的ID保留、路径外的ID仍替换。
def test_full_paths_keep_identifiers_only_inside_paths():
    assert "preserve_host_paths" in inspect.signature(channels.redact_structured_identifiers).parameters
    raw = "/home/example-user/work/file.txt；用户 example-user"
    assert channels.redact_structured_identifiers(raw, (("example-user", "当前用户"),), channel="http",
        preserve_host_paths=True) == "/home/example-user/work/file.txt；用户 当前用户"


# LLM: 使用真实writer与公开进度读取，提交夹具只提供已验来源，不更改投影或认证。
# 函数用途: 验证助手、思考、终稿、commentary及progress的整套矩阵。
@pytest.mark.parametrize("channel,admin,chat_type,full", CASES)
def test_writer_and_progress_matrix(tmp_path, channel, admin, chat_type, full):
    assert "preserve_host_paths" in inspect.signature(BufferedChunkStreamWriter).parameters
    path = tmp_path / "chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, delivery_channel=channel,
        preserve_host_paths=_visible(channel, admin, chat_type), rich_transcript=True, text_delta_holdback_chars=1)
    writer.set_verbose_level("stream")
    writer.bind_model_call("path-call")(PATH_TEXT + "\n")
    writer.write_thinking_delta(PATH_TEXT + "\n")
    writer.flush_thinking_delta()
    writer.write_thinking(PATH_TEXT)
    _commit_writer_fixture(writer, PATH_TEXT, call_id="path-call")
    _commit_writer_fixture(writer, PATH_TEXT, call_id="path-final", boundary="terminal")
    writer.close()
    expected = PATH_TEXT if full else "${APP_ROOT}Antigravity"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for kind in ("assistant_text_delta", "thinking_delta", "assistant_thinking", "assistant_commentary", "assistant_final"):
        text = "".join(row["text"] for row in rows if row["kind"] == kind)
        assert expected in text, (kind, rows)
        assert channels.HOST_PATH_REDACTION_NOTE not in text
        assert (PATH_TEXT in text) is full
    events, _ = http_handlers._read_public_progress_events(path, 0, channel=channel,
        include=frozenset({"assistant_text_delta"}), preserve_host_paths=_visible(channel, admin, chat_type))
    for kind in ("assistant_text_delta", "assistant_commentary"):
        text = "".join(row.get("text", "") for row in events if row["kind"] == kind)
        assert expected in text and (PATH_TEXT in text) is full
        assert channels.HOST_PATH_REDACTION_NOTE not in text
    assert writer.delivery_channel == channel


# LLM: 历史请求缺标志时保守处理，不能以其它字段替代服务端键。
# 函数用途: 核验请求级标志的精确取值与缺失默认值。
def test_legacy_flag_is_false_and_server_key_is_the_only_fact():
    helper = getattr(request_history, "gateway_request_preserves_host_paths", None)
    assert callable(helper)
    for request in (None, {}, {"metadata": None}, {"preserve_host_paths": True},
                    {"metadata": {"host_path_display": True}}, {"metadata": {"host_path_display": "basename"}}):
        assert helper(request) is False
    assert helper({"metadata": {"host_path_display": "full"}}) is True


# LLM: 此静态守卫扫描实际调用语法，不按行号或正文词表认定权限；用于本片四个投影消费者。
# 函数用途: 返回漏传展示标志或用用户名判展示的准确位置。
def _projection_guard(source):
    errors = []
    targets = {"project_host_paths_report", "project_host_paths_for_channel",
               "project_message_paths_for_channel", "redact_structured_identifiers"}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name in targets and not any(key.arg == "preserve_host_paths" for key in node.keywords):
                errors.append((node.lineno, "missing display flag"))
        if isinstance(node, ast.Compare):
            values = [node.left, *node.comparators]
            if (any(isinstance(value, ast.Constant) and value.value == "admin" for value in values)
                    and any((isinstance(value, ast.Name) and "user_id" in value.id)
                            or (isinstance(value, ast.Attribute) and "user_id" in value.attr) for value in values)):
                errors.append((node.lineno, "identity string display rule"))
    return errors


# LLM: 扫描四个实际消费者的全部调用，不依赖固定行号。
# 函数用途: 防止新增调用漏传展示标志。
@pytest.mark.parametrize("filename", ["stream_writer.py", "stream_events.py", "request_history.py", "http_handlers.py"])
def test_projection_callers_carry_explicit_flag(filename):
    assert _projection_guard((Path(http_handlers.__file__).parent / filename).read_text()) == []


# LLM: 守卫自身必须拒绝无标志调用和用户名权限判断，正确关键字调用应接受。
# 函数用途: 证明静态守卫能报出真实违规。
def test_projection_guard_rejects_invalid_fixtures():
    assert _projection_guard("project_host_paths_for_channel(text, channel)")
    assert _projection_guard("full = user_id == 'admin'")
    assert not _projection_guard("project_host_paths_for_channel(text, channel, preserve_host_paths=full)")


# LLM: 本例仅替代审批域副作用以观察路由，不声称真实IM服务端验证通过。
# 函数用途: 证明展示标志不会改变飞书审批分派。
@pytest.mark.parametrize("flag", [False, True])
def test_feishu_approval_routing_does_not_depend_on_display(tmp_path, monkeypatch, flag):
    assert "preserve_host_paths" in inspect.signature(BufferedChunkStreamWriter).parameters
    from agent_py_agent.agent.conversation import im_tool_approval
    calls = []
    monkeypatch.setattr(im_tool_approval, "request_foreground_im_permission",
        lambda agent, request, **kwargs: calls.append((agent, request)) or "im-result")
    agent, request = object(), object()
    writer = BufferedChunkStreamWriter(tmp_path / "approval.jsonl", delivery_channel="feishu",
        preserve_host_paths=flag, im_approval_agent=agent)
    assert request_stream_permission(writer, request, None) == "im-result"
    assert calls == [(agent, request)] and writer.delivery_channel == "feishu"
    writer.close()


# LLM: 只替代模型供应商；认证、准入、原生提交、流 writer、HTTP 读取和线程切换均执行产品代码。
# 类用途: 在无外网测试中提供包含路径的确定性模型正文和思考增量。
class _PathBackend(EchoBackend):
    # LLM: 不调用工具、不改状态；回调沿 core 绑定的同次模型调用出口发出。
    # 函数用途: 生成可核对的流式样本，返回完整终稿。
    def generate(self, prompt, on_chunk=None, on_thinking_delta=None, **kwargs):
        if on_thinking_delta:
            on_thinking_delta(PATH_TEXT + "\n")
        if on_chunk:
            on_chunk(PATH_TEXT + "\n" + "核对中。" * 100)
        return ModelResponse(text=PATH_TEXT, backend="echo")


# LLM: 临时随机端口、真实中间件；不替换 handler 或权限裁决，退出时关闭本测试服务器。
# 函数用途: 准备可以完成 ask/control/progress/result 的同一隔离 Gateway。
@pytest.fixture
def wire(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
        gateway_per_user_owner_scoping=False, memory_curator_enabled=False, enable_self_learning=False,
        model_context_window_tokens=1_000_000), tmp_path)
    agent.backend = _PathBackend()
    manager = AuthManager(admin_user_id="viewer-admin", auth_enabled=True)
    middleware = AuthMiddleware(manager)
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "gateway"),
        params=GatewayHTTPServerParams(agent=agent, auth_middleware=middleware))
    server.start()
    try:
        yield agent, server, manager
    finally:
        server.stop()


# LLM: 使用真实 socket/JSON 编码返回状态及解析后的响应，不调用服务端函数替代运输。
# 函数用途: 给隔离 HTTP 服务器发一次带真实宿主凭据的请求。
def _http(wire, method, path, *, channel="http", user="viewer-admin", chat_type="p2p", body=None):
    _, server, _ = wire
    from agent_py_agent.agent.gateway_parts.local_client_token import load_local_client_credential
    headers = {"Content-Type": "application/json", "X-Gateway-Token": load_local_client_credential(server.agent.home_paths.root),
               "X-User-Id": user, "X-Channel": channel, "X-Channel-Chat-Type": chat_type}
    connection = http.client.HTTPConnection("127.0.0.1", server.server.server_address[1], timeout=10)
    try:
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# LLM: 真实 admission 的服务端字段由落盘请求读回，随后沿原 worker 认领、提交并通过 HTTP 查询终稿。
# 函数用途: 跑一轮完整请求，验证路径策略、delta 与终稿都来自同一请求级事实。
def _wire_turn(wire, *, channel, user, chat_type="p2p", full, message_id="", conversation="viewer-session"):
    from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_requests
    agent, server, _ = wire
    metadata = {"host_path_display": "full" if not full else "basename"}
    if message_id:
        metadata["message_id"] = message_id
    status, admitted = _http(wire, "POST", "/ask", channel=channel, user=user, chat_type=chat_type,
        body={"goal": "核对程序入口", "conversation_id": conversation, "metadata": metadata,
              "verbose_level": "stream", "client_capabilities": {"rich_transcript": True}})
    assert status == 202, admitted
    request_id = admitted["request_id"]
    pending = json.loads((server.paths.inbox / f"{request_id}.json").read_text())
    assert pending["metadata"]["host_path_display"] == ("full" if full else "basename")
    assert pending["metadata"]["channel"] == pending["conversation"]["channel"] == channel
    assert _process_gateway_requests(agent, server.paths) == 1
    status, result = _http(wire, "GET", "/result/" + request_id, channel=channel, user=user, chat_type=chat_type)
    assert status == 200 and result["ok"], result
    text = result["channel_delivery"]["content"]
    assert text == (PATH_TEXT if full else "${APP_ROOT}Antigravity\n\n" + channels.HOST_PATH_REDACTION_NOTE)
    status, progress = _http(wire, "GET", "/progress/" + request_id + "?include=assistant_text_delta",
        channel=channel, user=user, chat_type=chat_type)
    assert status == 200, progress
    deltas = "".join(row.get("text", "") for row in progress["events"] if row["kind"] == "assistant_text_delta")
    assert (PATH_TEXT in deltas) is full
    assert (PATH_TEXT if full else "${APP_ROOT}Antigravity") in deltas
    assert channels.HOST_PATH_REDACTION_NOTE not in deltas
    return request_id, pending, result


# LLM: 真实HTTP认证和两种准入分支都必须覆盖客户端伪造展示字段，不替换核心链路。
# 函数用途: 核验每种通道身份的落盘策略与公开终稿。
@pytest.mark.parametrize("channel,admin,chat_type,full", CASES)
@pytest.mark.parametrize("idempotent", [False, True])
def test_real_http_matrix_and_server_metadata(wire, channel, admin, chat_type, full, idempotent):
    _wire_turn(wire, channel=channel, user="viewer-admin" if admin or channel == "chat" else "viewer-user",
        chat_type=chat_type, full=full, message_id="stable-message" if idempotent else "")


# LLM: 公共结果不承诺内部thread_id；线程连续性以真实会话绑定读取验证，不扩大结果DTO。
# 函数用途: 验证管理员切新会话后完整路径保留，同Gateway普通用户仍脱敏。
def test_headless_admin_new_and_non_admin_same_gateway(wire):
    first, _, _ = _wire_turn(wire, channel="http", user="viewer-admin", full=True, message_id="first-message")
    threads = wire[0].conversation_store.threads
    before = threads.resolve(channel="http", channel_conversation_id="viewer-session", channel_user_id="viewer-admin")
    assert before is not None
    status, reset = _http(wire, "POST", "/ask", body={"goal": "/new", "conversation_id": "viewer-session",
        "metadata": {"message_id": "new-control-message"}})
    assert status == 200 and reset["ok"], reset
    second, _, _ = _wire_turn(wire, channel="http", user="viewer-admin", full=True, message_id="second-message")
    after = threads.resolve(channel="http", channel_conversation_id="viewer-session", channel_user_id="viewer-admin")
    assert after is not None
    assert first != second and before.thread_id != after.thread_id
    _wire_turn(wire, channel="http", user="viewer-user", full=False, conversation="other-session")
    status, compact = _http(wire, "POST", "/ask", body={"goal": "/compact status", "conversation_id": "viewer-session",
        "metadata": {"message_id": "compact-status-message"}})
    assert status == 200 and compact["ok"], compact
    policy = compact["details"]["idle_policy"]
    assert policy["trigger_percent_of_compact_window"] == 60
    assert policy["effective_compact_trigger_percent"] == 80
    assert policy["effective_window_percent"] == 48.0


# LLM: 轮询按当前查看者裁决，但不得反写历史请求资格；计数包装仍调用真实认证。
# 函数用途: 证明多行progress只认证一次且普通查看者不能继承管理员路径。
def test_poll_uses_current_viewer_once_and_never_upgrades_request_metadata(wire, monkeypatch):
    request_id, pending, _ = _wire_turn(wire, channel="http", user="viewer-admin", full=True)
    _, server, manager = wire
    original = http_handlers._request_identity
    calls = []

    # LLM: 只计数实际认证函数，返回原结果，不构造权限或替代中间件。
    # 函数用途: 证明多行投影不重复访问认证服务。
    def observed(handler):
        calls.append(handler.path)
        return original(handler)

    monkeypatch.setattr(http_handlers, "_request_identity", observed)
    # 同一身份撤去全用户能力后仍可读自己的请求，但不再获得完整路径。
    manager.admin_user_id = "new-admin"
    status, progress = _http(wire, "GET", "/progress/" + request_id + "?include=assistant_text_delta")
    assert status == 200 and len(calls) == 1, (status, calls)
    assert PATH_TEXT not in json.dumps(progress, ensure_ascii=False)
    terminal = json.loads((server.paths.terminal / f"{request_id}.json").read_text())
    assert pending["metadata"]["host_path_display"] == terminal["metadata"]["host_path_display"] == "full"
    calls.clear()
    _wire_turn(wire, channel="http", user="viewer-user", full=False, conversation="counted-session")
    assert sum(path == "/ask" for path in calls) == 1


# LLM: 扫描每个路径内部切点，包括中文首段、Windows和超过默认holdback的路径；完整路径后仍可刷新。
# 函数用途: 保证已识别宿主路径不会被原安全切点拆开。
@pytest.mark.parametrize("host_path", [
    "/Users/ascii/project/entry.txt", "/Users/测试用户/项目/入口.txt", "/测试用户/项目/入口.txt",
    "~/测试用户/入口.txt", "C:\\测试用户\\项目\\入口.txt", "/Users/" + "目录/" * 200 + "入口.txt",
], ids=["ascii", "unicode", "unicode-root", "tilde", "windows", "long"])
def test_host_path_safe_cut_is_atomic(host_path):
    from agent_py_agent.agent.gateway_parts.progress_text_delta import _safe_cut

    prefix = "前缀 "
    raw = prefix + host_path + " 后缀"
    start, end = len(prefix), len(prefix) + len(host_path)
    # 原凭据/协议规则可以更早保留，但不能落入路径内部；此处不放松既有安全切点。
    assert _safe_cut(raw, start, ()) <= start
    for cut in range(start + 1, end):
        assert _safe_cut(raw, cut, ()) <= start, (host_path, cut)
    end_cut = _safe_cut(raw, end, ())
    assert end_cut <= start or end_cut == end


# LLM: 真实HTTP准入冻结资格，真实writer在终态前写增量；降权后的同用户按since读取，不能继承管理员路径。
# 函数用途: 验证ASCII、中文、首段和长路径跨两轮游标只剩basename，管理员仍能读完整路径。
@pytest.mark.parametrize("host_path,split,basename", [
    ("/Users/ascii/project/entry.txt", 10, "entry.txt"),
    ("/Users/测试用户/项目/入口.txt", 9, "入口.txt"),
    ("/测试用户/项目/入口.txt", 2, "入口.txt"),
    ("/测试用户/项目/入口.txt", 6, "入口.txt"),
    ("/Users/" + "目录/" * 200 + "入口.txt", 300, "入口.txt"),
], ids=["ascii", "unicode", "unicode-root-open", "unicode-root-slash", "long"])
def test_host_path_live_progress_across_since(wire, host_path, split, basename):
    from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path

    _, server, manager = wire
    status, admitted = _http(wire, "POST", "/ask", body={"goal": "核对路径切点",
        "conversation_id": "path-boundary", "metadata": {"host_path_display": "basename"}, "verbose_level": "stream"})
    assert status == 202, admitted
    request_id = admitted["request_id"]
    pending_path = server.paths.inbox / f"{request_id}.json"
    pending = json.loads(pending_path.read_text())
    assert pending["metadata"]["host_path_display"] == "full"
    writer = BufferedChunkStreamWriter(gateway_chunk_path(server.paths, request_id), delivery_channel="http",
        preserve_host_paths=request_history.gateway_request_preserves_host_paths(pending), text_delta_holdback_chars=1)
    writer.set_verbose_level("stream")
    try:
        callback = writer.bind_model_call("host-path-live")
        callback("开始\n")
        callback(host_path[:split])
        manager.admin_user_id = "new-admin"
        url = "/progress/" + request_id + "?include=assistant_text_delta"
        status, first = _http(wire, "GET", url)
        assert status == 200, first
        first_text = "".join(row.get("text", "") for row in first["events"] if row["kind"] == "assistant_text_delta")
        assert first_text.rstrip() == "开始", first
        callback(host_path[split:] + "\n后续")
        status, second = _http(wire, "GET", url + "&since=" + str(first["next"]))
        assert status == 200, second
        second_text = "".join(row.get("text", "") for row in second["events"] if row["kind"] == "assistant_text_delta")
        assert first_text + second_text == "开始\n" + basename + "\n后"
        assert second["next"] > first["next"]
        status, admin = _http(wire, "GET", url, user="new-admin")
        assert status == 200, admin
        admin_text = "".join(row.get("text", "") for row in admin["events"] if row["kind"] == "assistant_text_delta")
        assert admin_text == "开始\n" + host_path + "\n后"
        assert channels.HOST_PATH_REDACTION_NOTE not in first_text + second_text
        assert json.loads(pending_path.read_text())["metadata"]["host_path_display"] == "full"
    finally:
        writer.close()


# LLM: 沿原容量/累计投影预算熔断，不强刷长路径尾部；新调用必须清除旧上下文并可继续输出。
# 函数用途: 验证长Unicode路径达到缓冲或扫描预算时只撤销显示、不泄露目录片段。
@pytest.mark.parametrize("budget", ["capacity", "projection"])
def test_host_path_long_buffer_budget_is_fail_closed(budget):
    from agent_py_agent.agent.gateway_parts.progress_text_delta import TextDeltaBuffer

    host_path = "/Users/" + "目录/" * 300 + "入口.txt"
    buffer = TextDeltaBuffer(holdback_chars=4, max_buffer_chars=512 if budget == "capacity" else 8192,
        max_projection_chars=10000 if budget == "capacity" else 768)
    assert buffer.begin("long-path") == []
    assert buffer.append(host_path[:256], model_call_id="long-path") == []
    rows = buffer.append(host_path[256:512], model_call_id="long-path")
    rows += buffer.append(host_path[512:], model_call_id="long-path")
    assert rows == [{"kind": "assistant_text_reset", "model_call_id": "long-path", "reason": "superseded"}]
    assert buffer.append("目录尾部", model_call_id="long-path") == []
    assert buffer.finish("long-path") == [{"kind": "assistant_text_done", "model_call_id": "long-path", "seq": 0}]
    assert buffer.begin("after-long-path") == []
    rows = buffer.append("安全文本\n", model_call_id="after-long-path") + buffer.finish("after-long-path")
    assert "".join(row.get("text", "") for row in rows) == "安全文本\n"
