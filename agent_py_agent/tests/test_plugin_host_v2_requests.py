# LLM: B2回归守住原账本、最小权限及不夸大执行结果；不把替身环境当完整接线。
# 模块用途: 验证插件请求、取消和流的公开合同。
import json
from dataclasses import asdict

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import recovery
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.plugin_request_source import require_plugin_source
from agent_py_agent.agent.gateway_parts.plugin_request_submission import build_message_preparation
from agent_py_agent.agent.gateway_parts.queue_service import claim_request, ensure_gateway_folders
from agent_py_agent.agent.plugin_host.contracts import (
    AuthorizedCall,
    PluginIngressSource,
    VerifiedPluginBinding,
    decode_call,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.requests import project_request_state, read_request_facts
from agent_py_agent.agent.plugin_host.streams import RequestEventBuffer
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


# LLM: 取消受理不能替代原canonical terminal。
# 函数用途: 防止把仍运行的取消请求报成已经取消。
def test_cancel_acceptance_is_not_cancelled_terminal():
    state = project_request_state(receipt_state="accepted", claim_state="running",
        cancellation_requested=True, canonical_terminal=None, execution_unknown=False)
    assert state == "cancelling"


# LLM: 无规范终态的未知执行不能猜测为成功。
# 函数用途: 固定unknown语义。
def test_unknown_execution_must_not_be_reported_succeeded():
    state = project_request_state(receipt_state="accepted", claim_state="running",
        cancellation_requested=False, canonical_terminal=None, execution_unknown=True)
    assert state == "unknown"


# LLM: 规范终态优先于取消标记和运输未知。
# 函数用途: 验证所有终态由原权威记录决定。
@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_canonical_terminal_wins(status):
    assert project_request_state(receipt_state="queued", claim_state="running",
        cancellation_requested=True, canonical_terminal={"status": status},
        execution_unknown=True) == status


# LLM: 输入阶段仅按封闭枚举投影，非法或空观察仍未知。
# 函数用途: 覆盖全部公开非终态。
@pytest.mark.parametrize(("receipt", "claim", "expected"), [
    ("queued", "", "queued"), ("admission_wait", "", "admission_wait"),
    ("queued", "claimed", "running"), ("queued", "running", "running"),
    ("queued", "waiting_approval", "waiting_approval"),
    ("queued", "waiting_input", "waiting_input"), ("accepted", "", "unknown"),
])
def test_nonterminal_projection(receipt, claim, expected):
    assert project_request_state(receipt_state=receipt, claim_state=claim,
        cancellation_requested=False, canonical_terminal=None, execution_unknown=False) == expected


# LLM: 仅造公开状态事件；不是插件授权或canonical执行替身。
# 函数用途: 为传输叶子准备封闭事件。
def _event(sequence, state="running"):
    return {"event_id": f"event-{sequence}", "sequence": sequence, "kind": "status",
            "created_at": "2026-10-23T16:00:00Z", "data": {"state": state}}


# LLM: 此回调只用于纯传输单测，不替代真实auth集成验证。
# 函数用途: 标记传输单测没有外部撤销。
def _transport_open():
    return None


# LLM: 缓冲不重放游标之前的事件，不把尚未发送的事件当已确认。
# 函数用途: 验证续传去重和SSE事件形状。
def test_stream_buffer_resume_and_deduplicate():
    buffer = RequestEventBuffer(last_sequence=2, last_cursor="opaque-cursor-2")
    assert buffer.push(_event(2), "opaque-cursor-2") is False
    assert buffer.push(_event(3), "opaque-cursor-3") is True
    assert buffer.push(_event(3), "opaque-cursor-3") is False
    frame = buffer.take(now=1, check_current=_transport_open)
    assert frame.startswith(b"id: opaque-cursor-3\nevent: status\ndata: ")
    assert b'"sequence":3' in frame
    assert frame.endswith(b"\n\n")
    assert buffer.take(now=2, check_current=_transport_open) is None


# LLM: 15秒空闲心跳没有业务事件，也不能制造终态。
# 函数用途: 验证心跳时间边界。
def test_stream_heartbeat_at_fifteen_seconds():
    buffer = RequestEventBuffer(now=10)
    assert buffer.take(now=24.999, check_current=_transport_open) is None
    assert buffer.take(now=25, check_current=_transport_open) == b": heartbeat\n\n"
    assert buffer.take(now=26, check_current=_transport_open) is None


# LLM: 超载清理缓冲并用最后已交付游标提示重连，不交付被丢事件或取消任务。
# 函数用途: 验证慢消费者有界退出。
def test_stream_backpressure_retains_last_delivered_cursor():
    buffer = RequestEventBuffer(capacity=1, last_cursor="opaque-cursor-0")
    assert buffer.push(_event(1), "opaque-cursor-1") is True
    assert buffer.push(_event(2), "opaque-cursor-2") is False
    frame = buffer.take(now=1, check_current=_transport_open)
    assert b"BACKPRESSURE" in frame and b"retry: 1000" in frame
    assert b"opaque-cursor-0" in frame
    assert b"opaque-cursor-1" not in frame and b"opaque-cursor-2" not in frame
    assert buffer.take(now=2, check_current=_transport_open) is None


# LLM: 授权检查失败须丢弃已缓冲正文；只发安全错误码。
# 函数用途: 验证撤销不会再泄露队列中的业务内容。
@pytest.mark.parametrize("code", ["ACTIVATION_REVOKED", "USER_DISABLED"])
def test_stream_revocation_discards_pending_business_data(code):
    buffer = RequestEventBuffer()
    assert buffer.push(_event(1), "opaque-cursor-1") is True

    # LLM: 模拟传输门抛错，未声称验证真实grant/store撤销。
    # 函数用途: 产生不应外泄原文的授权拒绝。
    def revoked():
        raise HostAPIError(code, 403, message="private/raw/path")

    frame = buffer.take(now=1, check_current=revoked)
    assert code.encode() in frame
    assert b"event-1" not in frame and b"private/raw/path" not in frame
    assert buffer.take(now=2, check_current=_transport_open) is None


# LLM: 流不接受未知字段或换行注入，即使数据来自宿主投影也要闭合检查。
# 函数用途: 验证公开事件白名单与SSE单帧边界。
@pytest.mark.parametrize("extra,cursor", [({"raw": "secret"}, "cursor"), ({}, "bad\nid: forged")])
def test_stream_rejects_nonpublic_fields_and_cursor_injection(extra, cursor):
    buffer = RequestEventBuffer()
    with pytest.raises(HostAPIError):
        buffer.push({**_event(1), **extra}, cursor)


# LLM: 原input writer和queue真实运行；fixture只构造存储输入，不声称通过auth或C/W。
# 函数用途: 建立供只读投影测试使用的原账本。
@pytest.fixture
def request_case(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    ensure_gateway_folders(paths)
    owner = OwnerIdentity.provider_user("local", "alice")
    payload = {"id": "plugin-request-example", "kind": "ask", "goal": "test",
        "host_request_owner.v1": asdict(owner), "owner_id": "local/user/alice",
        "metadata": {"client_input_digest": "a" * 64, "plugin_provenance": {
            "installation_ref": "install-one", "thread_id": "thread-one", "kind": "chat"}}}
    with inputs.gateway_input_transition(paths, payload["id"]):
        receipt, _ = inputs.load_or_prepare_gateway_input_locked(paths, request_id=payload["id"],
            client_input_digest="a" * 64, client_message_id="message-one",
            guidance_dedupe_key="guidance-one", prepared_request=payload)
        inputs.queue_gateway_input_locked(paths, receipt)
    return paths, owner, payload


# LLM: 读路径不得mkdir/建锁/修复输入，真实文件集合和字节均保持。
# 函数用途: 验证排队状态来自原回执且只读。
def test_request_reader_is_read_only_and_observes_original_queue(request_case):
    paths, owner, payload = request_case
    before = {str(p.relative_to(paths.root)): p.read_bytes() for p in paths.root.rglob("*") if p.is_file()}
    facts = read_request_facts(paths, payload["id"], expected_owner=owner, installation_ref="install-one")
    assert facts == {"request_id": payload["id"], "thread_id": "thread-one", "kind": "chat",
        "state": "queued", "cancel_requested": False, "terminal": None}
    after = {str(p.relative_to(paths.root)): p.read_bytes() for p in paths.root.rglob("*") if p.is_file()}
    assert before == after


# LLM: responses只是投影，即使ok=true也不得凭它捏造canonical终态。
# 函数用途: 防止假响应文件冒充业务成功。
def test_response_projection_is_not_terminal_authority(request_case):
    paths, owner, payload = request_case
    write_json_file_atomic(paths.responses / (payload["id"] + ".json"),
        {"id": payload["id"], "ok": True, "status": "done"})
    facts = read_request_facts(paths, payload["id"], expected_owner=owner, installation_ref="install-one")
    assert facts is not None and facts["state"] == "queued" and facts["terminal"] is None


# LLM: 使用真实claim及原canonical writer，不手写终态文件充当成功执行证据。
# 函数用途: 验证claim、取消受理和原终态分别投影。
def test_reader_tracks_claim_cancel_and_canonical_terminal(request_case):
    paths, owner, payload = request_case
    claim = claim_request(paths, paths.inbox / (payload["id"] + ".json"))
    assert claim is not None
    running = read_request_facts(paths, payload["id"], expected_owner=owner, installation_ref="install-one")
    assert running is not None and running["state"] == "running"
    current = json.loads(claim.path.read_bytes())
    current["cancel_requested"] = True
    write_json_file_atomic(claim.path, current)
    cancelling = read_request_facts(paths, payload["id"], expected_owner=owner, installation_ref="install-one")
    assert cancelling["state"] == "cancelling" and cancelling["terminal"] is None
    response = {"id": payload["id"], "status": "cancelled", "ok": False, "ended_at": 1792771200.0}
    terminal = recovery._terminal_gateway_request_payload(payload["id"], current, response)
    recovery._commit_gateway_terminal_request(paths, claim.path, paths.failed, payload["id"], terminal)
    final = read_request_facts(paths, payload["id"], expected_owner=owner, installation_ref="install-one")
    assert final["state"] == "cancelled" and final["terminal"]["status"] == "cancelled"
    assert final["terminal"]["resources_stopped"] is None
    assert final["terminal"]["usage"] == {"input_tokens": None, "output_tokens": None}


# LLM: 元数据范围检查不是授权替身；未通过owner/安装检查不能暴露资源。
# 函数用途: 验证跨owner、跨安装均不可读。
@pytest.mark.parametrize("wrong", ["owner", "installation"])
def test_request_reader_denies_foreign_scope(request_case, wrong):
    paths, owner, payload = request_case
    if wrong == "owner":
        owner = OwnerIdentity.provider_user("local", "bob")
    with pytest.raises(HostAPIError) as caught:
        read_request_facts(paths, payload["id"], expected_owner=owner,
            installation_ref="install-two" if wrong == "installation" else "install-one")
    assert caught.value.code == "RESOURCE_NOT_FOUND"


# LLM: 原receipt存在但canonical损坏必须报错，不能当作普通空结果或成功。
# 函数用途: 验证损坏与输入漂移失败关闭。
@pytest.mark.parametrize("damage", ["bad_json", "changed_input", "symlink"])
def test_request_reader_rejects_corrupt_canonical(request_case, tmp_path, damage):
    paths, owner, payload = request_case
    target = paths.terminal / (payload["id"] + ".json")
    if damage == "symlink":
        other = tmp_path / "other.json"
        other.write_text('{}')
        target.symlink_to(other)
    elif damage == "bad_json":
        target.write_text("{broken")
    else:
        target.write_text(json.dumps({**payload, "goal": "tampered"}))
    with pytest.raises(HostAPIError) as caught:
        read_request_facts(paths, payload["id"], expected_owner=owner, installation_ref="install-one")
    assert caught.value.code == "STORE_UNAVAILABLE"


# LLM: 不把公开ID拼接为任意路径，不因只读查询物化冷目录。
# 函数用途: 验证非法ID和冷目录零写。
@pytest.mark.parametrize("request_id", ["../escape", "/absolute", "", "normal-id"])
def test_request_reader_cold_and_unsafe_ids_write_nothing(tmp_path, request_id):
    root = tmp_path / "cold"
    paths = gateway_paths_from_root(root)
    with pytest.raises(HostAPIError):
        read_request_facts(paths, request_id, expected_owner=OwnerIdentity.local_main(), installation_ref="install-one")
    assert not root.exists()


# LLM: 所有公开事件都遵循独立白名单，不运输原工具参数或私卡正文。
# 函数用途: 覆盖状态之外的事件编码。
@pytest.mark.parametrize("kind,data", [
    ("assistant_delta", {"message_id": "msg-one", "part": "commentary", "offset_bytes": 0, "text": "你好"}),
    ("tool_progress", {"call_id": "call-one", "tool_name": "read_file", "status": "started", "summary": "读取"}),
    ("approval_required", {"interaction_id": "approval-one", "summary": "需要确认"}),
    ("input_required", {"interaction_id": "input-one", "summary": "需要输入"}),
    ("terminal", {"terminal_id": "terminal-one", "status": "succeeded", "completed_at": "2026-10-23T16:00:00Z",
        "result_message_ids": ["msg-one"], "error_code": None,
        "usage": {"input_tokens": 10, "output_tokens": None}, "resources_stopped": None}),
])
def test_stream_supports_all_public_event_kinds(kind, data):
    buffer = RequestEventBuffer()
    event = {**_event(1), "kind": kind, "data": data}
    assert buffer.push(event, "opaque-cursor-1") is True
    frame = buffer.take(now=1, check_current=_transport_open)
    encoded = frame.split(b"data: ", 1)[1].strip()
    assert json.loads(encoded) == event


# LLM: 单条消息不能突破64KiB帧上限，不能截掉正文假装完整。
# 函数用途: 验证SSE编码后字节预算。
def test_stream_enforces_encoded_frame_budget():
    buffer = RequestEventBuffer()
    event = {**_event(1), "kind": "assistant_delta", "data": {
        "message_id": "msg-one", "part": "final", "offset_bytes": 0, "text": "x" * 65500}}
    with pytest.raises(HostAPIError) as caught:
        buffer.push(event, "opaque-cursor-1")
    assert caught.value.code == "PAYLOAD_TOO_LARGE"
    assert buffer.take(now=1, check_current=_transport_open) is None


# LLM: 非预期授权读取故障也要废弃缓冲；异常留给上层统一安全处理。
# 函数用途: 验证检查异常后连接不可复活泄露旧正文。
def test_stream_guard_failure_closes_before_propagating():
    buffer = RequestEventBuffer()
    buffer.push(_event(1), "opaque-cursor-1")

    # LLM: 只是纯缓冲的失败注入，不替代权限链验证。
    # 函数用途: 模拟真实上游读取无法完成。
    def unavailable():
        raise OSError("private runtime error")

    with pytest.raises(OSError):
        buffer.take(now=1, check_current=unavailable)
    assert buffer.take(now=2, check_current=_transport_open) is None


# LLM: 手造DTO只测纯数据准备及拒绝路径；不构造可用授权来源。
# 函数用途: 提供明确不具备授权性的调用数据。
def _message_call(*, text="hello", policy="none", key="stable-message-key-0001", installation="install-one", user="alice"):
    call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "conversations.messages.send",
        "idempotency_key": key, "payload": {"thread_id": "thread-one", "text": text, "tool_policy": policy}}).encode())
    binding = VerifiedPluginBinding({"installation_ref": installation, "package_sha256": "a" * 64,
        "activation_id": "b" * 64, "grant_revision": 1}, object(), OwnerIdentity.provider_user("local", user),
        PluginIngressSource(object(), object()), frozenset({"chat", "execute"}))
    return call, binding


# LLM: 原input receipt就是幂等账，同scope/key不另造执行ID；数据准备不落盘。
# 函数用途: 验证消息准备稳定且绑定真实字段而非传输时间。
def test_message_preparation_is_stable_and_freezes_plugin_owner():
    call, binding = _message_call()
    first = build_message_preparation(call, binding)
    second = build_message_preparation(call, binding)
    assert isinstance(first, inputs.GatewayInputPreparation)
    assert first.request_id == second.request_id
    assert first.client_input_digest == second.client_input_digest
    assert first.prepared_request["host_request_owner.v1"] == asdict(binding.effective_owner)
    assert first.prepared_request["metadata"]["origin"] == "plugin"
    assert first.prepared_request["source"] == "plugin"
    assert first.prepared_request["metadata"]["plugin_provenance"]["tool_policy"] == "none"


# LLM: 同scope/key异正文/工具策略必须同request ID但不同摘要，由原receipt拒绝。
# 函数用途: 验证无新幂等账的重复及冲突合同。
@pytest.mark.parametrize("change", [{"text": "different"}, {"policy": "bounded"}])
def test_message_payload_change_keeps_key_but_changes_digest(change):
    first = build_message_preparation(*_message_call())
    second = build_message_preparation(*_message_call(**change))
    assert first is not None and second is not None
    assert first.request_id == second.request_id
    assert first.client_input_digest != second.client_input_digest


# LLM: 幂等范围按安装、owner、action、key分区，不因两个安装同key串输入。
# 函数用途: 验证原request ID的稳定分区。
@pytest.mark.parametrize("change", [{"installation": "install-two"}, {"user": "bob"}, {"key": "other-message-key-0001"}])
def test_message_idempotency_scope_is_partitioned(change):
    first = build_message_preparation(*_message_call())
    other = build_message_preparation(*_message_call(**change))
    assert first is not None and other is not None
    assert first.request_id != other.request_id


# LLM: 本测试走原receipt写者而不声称auth/模型已接线；同key异payload保留原账。
# 函数用途: 验证准备数据能由原回执去重且不覆盖旧输入。
def test_prepared_messages_use_original_input_idempotency(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    original = build_message_preparation(*_message_call())
    assert isinstance(original, inputs.GatewayInputPreparation)
    with inputs.gateway_input_transition(paths, original.request_id):
        first, created = inputs.load_or_prepare_gateway_input_request_locked(paths, original)
        replay, repeated = inputs.load_or_prepare_gateway_input_request_locked(paths, original)
        assert created and not repeated and replay.to_dict() == first.to_dict()
        changed = build_message_preparation(*_message_call(text="changed"))
        with pytest.raises(ValueError, match="different input"):
            inputs.load_or_prepare_gateway_input_request_locked(paths, changed)


# LLM: slash检测是入口限制而不是权限边界，独立执行门仍必需。
# 函数用途: 阻止把插件正文直接送进旧控制命令链。
@pytest.mark.parametrize("text", ["/stop", "  /agents", "\n/settings"])
def test_preparation_rejects_control_commands(text):
    with pytest.raises(HostAPIError) as caught:
        build_message_preparation(*_message_call(text=text))
    assert caught.value.code == "CONTROL_COMMAND_NOT_ALLOWED"


# LLM: 缺真实授权模块时拒绝手造DTO，不能把A0冻结字段当作已接线。
# 函数用途: 固定当前依赖缺失的失败关闭行为。
def test_fabricated_source_cannot_authorize_submission():
    call, binding = _message_call()
    forged = AuthorizedCall(call, binding, binding.effective_owner, {}, None)
    with pytest.raises(HostAPIError) as caught:
        require_plugin_source(forged)
    assert caught.value.status in {401, 403, 410, 503}


# LLM: 深层JSON在4MiB内仍可能递归溢出，不能逃出固定存储错误映射。
# 函数用途: 复现并约束深嵌套损坏回执的报错。
def test_deep_json_receipt_maps_to_safe_store_error(request_case):
    paths, owner, payload = request_case
    receipt_path = paths.root / "input_receipts" / (payload["id"] + ".json")
    receipt_path.write_text('{"layer":' * 2000 + '0' + '}' * 2000)
    with pytest.raises(Exception) as caught:
        read_request_facts(paths, payload["id"], expected_owner=owner, installation_ref="install-one")
    assert isinstance(caught.value, HostAPIError)
    assert caught.value.code == "STORE_UNAVAILABLE"


# LLM: 解析器递归上限因解释器而异，此处明确注入边界故障，不假称本机真实深度溢出。
# 函数用途: 固定解析器抛RecursionError时的安全错误映射。
def test_decoder_recursion_error_is_mapped(request_case, monkeypatch):
    paths, owner, payload = request_case

    # LLM: 只替换标准库解析边界，不伪造授权或输入回执。
    # 函数用途: 模拟某解释器的递归深度拒绝。
    def too_deep(*args, **kwargs):
        raise RecursionError("decoder depth exceeded")

    monkeypatch.setattr(json, "loads", too_deep)
    with pytest.raises(Exception) as caught:
        read_request_facts(paths, payload["id"], expected_owner=owner, installation_ref="install-one")
    assert isinstance(caught.value, HostAPIError)
    assert caught.value.code == "STORE_UNAVAILABLE"


# LLM: 空串是原canonical writer的缺省码；其他falsey类型是损坏，不能归一成null。
# 函数用途: 复现非法终态错误码被吞掉的问题。
@pytest.mark.parametrize("code", [False, 0, [], {}])
def test_terminal_falsey_error_code_is_not_silently_erased(request_case, code):
    paths, owner, payload = request_case
    claim = claim_request(paths, paths.inbox / (payload["id"] + ".json"))
    current = json.loads(claim.path.read_bytes())
    response = {"id": payload["id"], "status": "failed", "ok": False, "ended_at": 1792771200.0}
    terminal = recovery._terminal_gateway_request_payload(payload["id"], current, response)
    terminal["error_code"] = code
    write_json_file_atomic(paths.terminal / (payload["id"] + ".json"), terminal)
    with pytest.raises(HostAPIError) as caught:
        read_request_facts(paths, payload["id"], expected_owner=owner, installation_ref="install-one")
    assert caught.value.code == "STORE_UNAVAILABLE"


# LLM: 使用真实安装/令牌、A发行器、B1存储及A游标；不把DTO替身当授权。
# 函数用途: 建立四动作公共领域集成夹具。
@pytest.fixture
def public_case(tmp_path):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts.plugin_request_source import PluginRequestPorts
    from agent_py_agent.agent.plugin_host.composition import HostComposition
    from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts
    from agent_py_agent.agent.plugin_host.conversations import ConversationDomain
    from agent_py_agent.tests.test_plugin_host_v2_grants import active_grant

    caps = ["read", "chat", "chat.history", "requests.cancel", "execute", "act_as_user"]
    home, install_store, entry, ref = active_grant(tmp_path, caps)
    api.set_host_api_base("http://127.0.0.1:8420")
    token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
    composition = HostComposition(capability_policy=lambda: frozenset(caps))
    store = ConversationStore(home.data_dir / "conversations")
    paths = gateway_paths_from_root(home.data_dir / "gateway")
    ensure_gateway_folders(paths)
    domain = ConversationDomain(lambda context: store, composition.cursors, default_thread=lambda context, tid: False,
                                request_ports=PluginRequestPorts(lambda context: paths))
    composition.ports = HostDomainPorts(conversation=domain)
    source = composition.issuer.ingress(token)
    yield composition, domain, store, paths, source, home, install_store, entry
    composition.close()
    api.set_host_api_base(None)


# LLM: 所有集成调用经真实decode/authorize/dispatch；只有模型执行不在此测试观察点。
# 函数用途: 从公开信封执行一个领域动作。
def _public_call(case, action, payload, key=None):
    from agent_py_agent.agent.plugin_host.contracts import authorize_call, dispatch_call

    composition, _, _, _, source, *_ = case
    row = {"schema": "plugin_host_call.v2", "action": "conversations." + action, "payload": payload}
    if key is not None:
        row["idempotency_key"] = key
    authorized = authorize_call(source, decode_call(json.dumps(row).encode()))
    return dispatch_call(authorized, composition.ports)


# LLM: 四动作应在B1真实领域公开，不可因helper存在就假称已分发。
# 函数用途: 固定公开请求动作的注册接线要求。
def test_public_domain_registers_request_actions(public_case):
    _, domain, *_ = public_case
    expected = {"conversations.messages.send", "conversations.requests.poll",
                "conversations.requests.cancel", "conversations.requests.stream"}
    assert expected <= getattr(domain, "actions", frozenset())


# LLM: B1必须使用A真实CursorCodec签发/读回，不允许测试专用codec掩盖接口错位。
# 函数用途: 复现真实组合根的线程分页断接。
def test_public_thread_paging_uses_real_cursor(public_case):
    for index in range(2):
        _public_call(public_case, "threads.create", {"title": str(index)}, f"public-create-key-{index:04d}")
    first = _public_call(public_case, "threads.list", {"limit": 1})
    token = first.data["next_cursor"]
    assert token and len(token) == 86
    second = _public_call(public_case, "threads.list", {"limit": 1, "cursor": token})
    assert second.data["items"][0]["thread_id"] != first.data["items"][0]["thread_id"]


# LLM: 原回执由真实安装调用准备并由原writer落盘；不需要启动模型来验证读取权限链。
# 函数用途: 创建真实公开线程和已排队的同安装请求。
def _public_request(case):
    from agent_py_agent.agent.plugin_host.contracts import authorize_call

    composition, _, _, paths, source, *_ = case
    thread = _public_call(case, "threads.create", {}, "public-request-thread-0001")
    tid = thread.data["thread_id"]
    call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "conversations.messages.send",
        "idempotency_key": "public-message-key-0001", "payload": {"thread_id": tid, "text": "hello"}}).encode())
    authorized = authorize_call(source, call)
    preparation = build_message_preparation(authorized.call, authorized.binding)
    with inputs.gateway_input_transition(paths, preparation.request_id):
        receipt, _ = inputs.load_or_prepare_gateway_input_request_locked(paths, preparation)
        inputs.queue_gateway_input_locked(paths, receipt)
    return preparation.request_id, tid


# LLM: poll须沿B1线程范围和原receipt取事实，而非transport或responses投影。
# 函数用途: 复现真实组合根缺失poll接线。
def test_public_poll_reads_original_receipt(public_case):
    request_id, tid = _public_request(public_case)
    result = _public_call(public_case, "requests.poll", {"request_id": request_id})
    assert result.http_status == 200
    assert result.data["request"]["thread_id"] == tid
    assert result.data["request"]["state"] == "queued"
    assert result.data["request"]["terminal"] is None
    assert result.data["event_cursor"] and result.data["reply_cursor"]


# LLM: 无原执行接缝不得把ask裸排给main；必须明确未就绪而非OUTCOME_UNKNOWN。
# 函数用途: 要求send在缺少安全执行接缝时失败关闭。
def test_public_send_requires_safe_runtime_binding(public_case):
    thread = _public_call(public_case, "threads.create", {}, "public-send-thread-0001")
    with pytest.raises(HostAPIError) as caught:
        _public_call(public_case, "messages.send", {"thread_id": thread.data["thread_id"], "text": "hello"},
                     "public-send-message-0001")
    assert caught.value.code == "HOST_NOT_READY"


# LLM: 缺少完整回复不得以delta或截断消息充数，私有角色及其他请求正文不能出站。
# 函数用途: 约束poll的请求级最终正文分页及权限隔离。
def test_public_poll_reply_pages_are_request_scoped(public_case):
    from agent_py_agent.agent.plugin_host.schemas import thaw

    request_id, tid = _public_request(public_case)
    store = public_case[2]
    text = "鲸🐳" * 5000
    for role, content, metadata in [("assistant", "secret", {"turn_id": "other"}),
            ("assistant", "reasoning", {"turn_id": request_id, "phase": "analysis"}),
            ("assistant", text, {"turn_id": request_id, "phase": "final"})]:
        store.messages.append({"thread_id": tid, "role": role, "content": content, "metadata": metadata})
    result = thaw(_public_call(public_case, "requests.poll", {"request_id": request_id}).data)
    pieces = list(result["replies"])
    for _ in range(20):
        if not result["has_more_replies"]:
            break
        result = thaw(_public_call(public_case, "requests.poll", {"request_id": request_id,
            "reply_cursor": result["reply_cursor"]}).data)
        pieces.extend(result["replies"])
    assert not result["has_more_replies"]
    assert "".join(row["content"] for row in pieces) == text
    assert all(row["request_id"] == request_id for row in pieces)


# LLM: 经真实公开授权与MAC续页，在旧锚点检查后原子替换文件；不得把新正文拼进旧回复。
# 函数用途: 固定打开句柄与旧回复快照之间的竞态回归。
def test_reply_cursor_rejects_replacement_after_old_anchor_check(public_case, monkeypatch):
    import os

    from agent_py_agent.agent.plugin_host import history
    from agent_py_agent.agent.plugin_host.schemas import thaw

    request_id, tid = _public_request(public_case)
    store = public_case[2]
    store.messages.append({"thread_id": tid, "role": "assistant", "content": "A" * 9000,
                           "metadata": {"turn_id": request_id, "phase": "final"}})
    first = thaw(_public_call(public_case, "requests.poll", {"request_id": request_id}).data)
    assert first["replies"][0]["content"] == "A" * 8192
    original, changed = history._verify_current, []

    # LLM: 只在临时owner文件上注入精确TOCTOU，不替换游标或权限验证结果。
    # 函数用途: 在旧锚点验证与消息文件打开之间换成等长新文件。
    def swap_after_check(store_arg, thread_id, anchor):
        original(store_arg, thread_id, anchor)
        if not changed:
            path = store_arg.storage.message_path(thread_id)
            old = path.read_bytes()
            new = old.replace(b"A" * 9000, b"B" * 9000)
            assert old != new and len(old) == len(new)
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(new)
            os.replace(replacement, path)
            changed.append(True)

    monkeypatch.setattr(history, "_verify_current", swap_after_check)
    with pytest.raises(HostAPIError) as error:
        _public_call(public_case, "requests.poll", {"request_id": request_id,
                     "reply_cursor": first["reply_cursor"]})
    assert (error.value.code, error.value.status) == ("CURSOR_EXPIRED", 409)


# LLM: 只为Count0队列验证绑定准备回调，不表示已验证真实模型或worker执行安全。
# 函数用途: 将本轮公开发送接到原input writer做实际幂等验证。
def test_public_send_queues_once_and_maps_conflict(public_case):
    from agent_py_agent.agent.gateway_parts.plugin_request_source import PluginRequestPorts

    _, domain, _, paths, *_ = public_case
    prepared = []

    # LLM: 这是原队列准备边界的测试接缝；不提供管理员invoker或虚构执行成功。
    # 函数用途: 记录宿主拿到的零工具策略并保持原不可变输入。
    def prepare(authorized, context, execution, payload):
        assert execution is None and not authorized.execution_policy.tool_names
        prepared.append(payload["id"])
        return payload

    domain.request_ports = PluginRequestPorts(lambda context: paths, prepare_request=prepare)
    tid = _public_call(public_case, "threads.create", {}, "public-send-positive-thread").data["thread_id"]
    payload = {"thread_id": tid, "text": "hello"}
    first = _public_call(public_case, "messages.send", payload, "public-send-positive-key")
    second = _public_call(public_case, "messages.send", payload, "public-send-positive-key")
    assert first.http_status == second.http_status == 202
    assert first.data["request_id"] == second.data["request_id"]
    assert first.data["state"] == "queued" and first.data["terminal"] is None
    assert prepared == [first.data["request_id"]]
    with pytest.raises(HostAPIError) as caught:
        _public_call(public_case, "messages.send", {**payload, "text": "different"}, "public-send-positive-key")
    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD" and caught.value.status == 409


# LLM: cancel沿准确原请求revision并写原控制回执，受理不等资源停止或canonical取消。
# 函数用途: 约束取消的CAS、幂等与非终态语义。
def test_public_cancel_uses_original_control_receipt(public_case):
    request_id, _ = _public_request(public_case)
    status = _public_call(public_case, "requests.poll", {"request_id": request_id}).data["request"]
    revision = status.get("revision")
    assert type(revision) is int and revision > 0
    payload = {"request_id": request_id, "expected_revision": revision}
    first = _public_call(public_case, "requests.cancel", payload, "public-cancel-key-0001")
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        GatewayControlOperationReceipt,
    )
    files = list((public_case[3].root / "control_operations").glob("*.json"))
    assert len(files) == 1
    assert GatewayControlOperationReceipt.from_dict(json.loads(files[0].read_bytes())).state == "completed"
    second = _public_call(public_case, "requests.cancel", payload, "public-cancel-key-0001")
    assert first.http_status in {200, 202} and first.data == second.data
    assert first.data["state"] == "cancelling" and first.data["terminal"] is None


# LLM: A真实令牌决定授权，C真实注册表决定enabled/revision/独立路径；桥不得自建main主体。
# 函数用途: 约束A/C真实读取及bounded执行上下文对接。
def test_public_issuer_resolves_c_managed_user(public_case):
    from agent_py_agent.agent.plugin_host import owner_context
    from agent_py_agent.agent.plugin_host.contracts import authorize_call, resolve_execution_context
    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore

    resolver_type = getattr(owner_context, "IngressOwnerResolver", None)
    assert callable(resolver_type)
    composition, _, _, _, source, home, *_ = public_case
    users = LocalUserStore(home.root)
    LocalUserLifecycle(users).create("alice", LocalUserOperation("fixture-install", "a" * 64, "fixture-alice-create"))
    composition.issuer._resolver = resolver_type(users)
    call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "conversations.messages.send",
        "as_user": {"user_id": "alice"}, "idempotency_key": "public-bounded-key-0001",
        "payload": {"thread_id": "thread-test", "text": "hello", "tool_policy": "bounded"}}).encode())
    authorized = authorize_call(source, call)
    context = resolve_execution_context(authorized)
    assert context.identity.owner_id == "alice" and context.identity.owner_kind == "user"
    assert context.source_binding is authorized.binding and context.user_revision > 0
    assert context.permission.is_admin is False and context.execution_policy.network is False
    assert context.owner_home.home_dir != home.home_dir


# LLM: 流使用真实A发行器与原canonical终态，退出订阅不得改任务状态。
# 函数用途: 验证B1流分发真实最终正文、终态和MAC断点重连。
def test_public_stream_delivers_reply_and_terminal(public_case):
    from agent_py_agent.agent.plugin_host.composition import stream_events
    from agent_py_agent.agent.plugin_host.contracts import authorize_call
    from agent_py_agent.agent.plugin_host.schemas import thaw

    request_id, tid = _public_request(public_case)
    composition, _, store, paths, source, *_ = public_case
    store.messages.append({"thread_id": tid, "role": "assistant", "content": "final reply",
                           "metadata": {"turn_id": request_id, "phase": "final"}})
    claim = claim_request(paths, paths.inbox / (request_id + ".json"))
    row = json.loads(claim.path.read_bytes())
    terminal = recovery._terminal_gateway_request_payload(request_id, row,
        {"id": request_id, "status": "succeeded", "ok": True, "ended_at": 1792771200.0})
    recovery._commit_gateway_terminal_request(paths, claim.path, paths.done, request_id, terminal)

    # LLM: 新连接仍走真实入站授权，不替换已发行调用的payload。
    # 函数用途: 用准确MAC断点重建订阅。
    def connect(cursor=None):
        payload = {"request_id": request_id}
        if cursor:
            payload["event_cursor"] = cursor
        call = decode_call(json.dumps({"schema": "plugin_host_call.v2", "action": "conversations.requests.stream",
                                      "payload": payload}).encode())
        return stream_events(authorize_call(source, call), composition.ports)

    rows = [thaw(row) for row in connect()]
    assert [row["kind"] for row in rows] == ["assistant_delta", "terminal"]
    assert rows[0]["data"]["text"] == "final reply" and rows[1]["data"]["status"] == "succeeded"
    assert all(len(row["event_id"]) == 86 for row in rows)
    replay = [thaw(row) for row in connect(rows[0]["event_id"])]
    assert [row["kind"] for row in replay] == ["terminal"]
    assert list(connect(rows[-1]["event_id"])) == []
