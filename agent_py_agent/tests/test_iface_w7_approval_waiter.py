# LLM: 仅用临时邮箱和可信宿主替身验证等待缝，不运行真实工具或改写用户授权。
# 模块用途: 守住默认兼容、宿主注入及审批权限生命周期。
"""IFACE-W7：宿主只替换等待传输，原审批策略、精确身份和事件顺序不变。"""
from __future__ import annotations

import inspect
import json
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    ToolApprovalRequest,
    ToolApprovalWaitOptions,
)
from agent_py_agent.agent.gateway_parts import permission_bridge, stream_approval
from agent_py_agent.agent.gateway_parts.approval_session import (
    ToolApprovalSessionCache,
    tool_approval_session_scope,
)
from agent_py_agent.agent.gateway_parts.stream_approval import (
    StreamApproval,
    StreamApprovalRequestOptions,
)

DECISIONS = ("approved", "approved_session", "approved_owner", "denied", "cancelled", "unavailable")


# LLM: 旧位置参数必须保序；新参数只能追加且默认关闭，不可强制调用方迁移。
# 函数用途: 核对协议和构造接口的向后兼容约定。
def test_waiter_is_an_appended_optional_host_constructor_argument():
    parameters = inspect.signature(StreamApproval).parameters
    assert list(parameters) == [
        "chunk_path", "publish", "before_wait", "cache", "scope_provider", "grant_recorder", "decision_waiter",
    ]
    assert parameters["decision_waiter"].default is None
    assert stream_approval.ApprovalDecisionWaiter._is_protocol


# LLM: 每例生成完整且未过期的规范请求，变体也必须经过合同校验。
# 函数用途: 构造测试专用的精确审批身份。
def _request(**changes):
    now = time.time()
    request = ToolApprovalRequest(
        permission_id="approval:w7", request_id="request:w7", tool_name="probe",
        round=1, call_index=0, title="工具授权", description="已脱敏的操作摘要",
        binding={"tool_name": "probe", "run_id": "run:w7", "operation_id": "op:w7",
                 "idempotency_key": "key:w7", "args_hash": "hash:w7"},
        options=tuple({"id": value, "label": value, "decision": value} for value in DECISIONS),
        requested_at=now, expires_at=now + 60,
    )
    return replace(request, **changes)


# LLM: 结构化实现协议但故意为假值，调用记录仅用于观察路由与选项。
# 类用途: 模拟可信宿主等待器并返回明确决定。
@dataclass
class RecordingWaiter:
    """可信宿主替身记录真实入参；故意为假值，防止被误选为默认文件传输。"""

    decision: str = "denied"
    trace: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    # LLM: callable 为假值仍是有效注入，不能触发默认等待器。
    # 函数用途: 暴露使用布尔回退选择等待器的回归。
    def __bool__(self):
        return False

    # LLM: 不读写真实授权，保留规范入参与可观测事件顺序。
    # 函数用途: 记录等待调用并返回同一审批身份的测试决定。
    def __call__(self, request: ToolApprovalRequest, options: ToolApprovalWaitOptions) -> ToolApprovalDecision:
        self.trace.append("wait")
        self.calls.append((request, options))
        return ToolApprovalDecision(request.permission_id, self.decision, feedback="用户反馈", decided_at=17)


# LLM: 禁止分支一旦被调用立即失败，不能把调用当成授权成功。
# 函数用途: 检出策略短路或插件确认被绕过。
def _forbidden(*args, **kwargs):
    pytest.fail("此审批分支不得调用等待器或策略提供者")


# LLM: 不传参和显式 None 均须消费真实临时邮箱，覆盖全部决定及缓存副作用。
# 函数用途: 证明默认路径的事件与返回值保持原样。
@pytest.mark.parametrize("explicit_none", [False, True])
@pytest.mark.parametrize("decision", DECISIONS)
def test_default_mailbox_roundtrip_and_event_order(tmp_path, decision, explicit_none):
    request, events, trace, grants = _request(), [], [], []
    request = replace(request, binding={**request.binding, "grant_key": "probe:mode=write"})
    chunk = tmp_path / "chunks.jsonl"
    expected = ToolApprovalDecision(request.permission_id, decision, feedback="用户反馈", decided_at=17)

    # LLM: 只有请求发布后才写决定，禁止预先伪造已完成审批。
    # 函数用途: 记录事件并通过原邮箱回复测试决定。
    def publish(event):
        trace.append(event["kind"])
        events.append(event)
        if event["kind"] == "permission_requested":
            permission_bridge.write_gateway_permission_decision(chunk, request, expected)

    kwargs = {"decision_waiter": None} if explicit_none else {}
    stream = StreamApproval(chunk, publish, lambda: trace.append("before_wait"), **kwargs)
    cache = ToolApprovalSessionCache()
    stream.configure(cache, lambda: "scope:w7", grants.append)
    assert stream.request(request.to_dict(), options=StreamApprovalRequestOptions(True)) == expected.to_dict()
    assert trace == ["before_wait", "permission_requested", "permission_resolved"]
    assert events == [
        {"kind": "permission_requested", "permission": request.to_dict()},
        {"kind": "permission_resolved", **expected.to_dict()},
    ]
    assert cache.is_approved("scope:w7", "probe:hash:w7") == (decision == "approved_session")
    assert grants == (["probe:mode=write"] if decision == "approved_owner" else [])
    assert not permission_bridge.gateway_permission_decision_path(chunk, request).exists()


# LLM: 捕获后仍调用原等待入口，断言原路径、取消令牌和选项没有丢失。
# 函数用途: 验证默认适配未改变原等待函数的入参。
def test_default_forwards_original_path_token_and_wait_options(tmp_path, monkeypatch):
    request, captured = _request(), []
    token = SimpleNamespace(cancelled=False)
    chunk = tmp_path / "chunks.jsonl"
    original = stream_approval.wait_for_gateway_permission_decision

    # LLM: provider 不自动授权，让测试继续走真实文件传输。
    # 函数用途: 检查 provider 收到规范请求。
    def provider(req):
        assert req == request
        return None

    # LLM: 保存原入口，既捕获参数也实际消费邮箱，避免包装器自递归。
    # 函数用途: 包装原等待器以观测精确参数。
    def capture(path, req, *, cancellation_token=None, options=None):
        captured.append((path, req, cancellation_token, options))
        return original(path, req, cancellation_token=cancellation_token, options=options)

    # LLM: 使用规范写入器而非 mock 决定文件，保留绑定校验。
    # 函数用途: 在发布请求时写入拒绝决定。
    def publish(event):
        if event["kind"] == "permission_requested":
            permission_bridge.write_gateway_permission_decision(
                chunk, request, ToolApprovalDecision(request.permission_id, "denied"))

    monkeypatch.setattr(stream_approval, "wait_for_gateway_permission_decision", capture)
    result = StreamApproval(chunk, publish, lambda: None).request(
        request.to_dict(), options=StreamApprovalRequestOptions(True, provider, token))
    assert result["decision"] == "denied"
    assert len(captured) == 1
    path, normalized, actual_token, options = captured[0]
    assert path is chunk and normalized == request and actual_token is token
    assert options == ToolApprovalWaitOptions(mode_decision_provider=provider)


# LLM: 自定义路径不得回退文件等待；反馈、身份、选项及事件均须逐项相同。
# 函数用途: 验证协议实现替换的只是等待传输。
@pytest.mark.parametrize("decision", DECISIONS)
def test_custom_protocol_preserves_request_options_and_events(tmp_path, monkeypatch, decision):
    request, events, trace = _request(), [], []
    waiter = RecordingWaiter(decision, trace)
    monkeypatch.setattr(stream_approval, "wait_for_gateway_permission_decision", _forbidden)

    # LLM: 返回 None 以保留人工等待路径，不添加自动授权。
    # 函数用途: 检查注入路径仍收到原宿主 provider。
    def provider(req):
        assert req == request
        return None

    # LLM: 事件序列与等待器共用 trace，以检测前后越序。
    # 函数用途: 记录自定义等待前后的审批事件。
    def publish(event):
        events.append(event)
        trace.append(event["kind"])

    stream = StreamApproval(tmp_path / "chunks", publish, lambda: trace.append("before_wait"),
                            decision_waiter=waiter)
    result = stream.request(request.to_dict(), options=StreamApprovalRequestOptions(True, provider))
    assert result == ToolApprovalDecision(request.permission_id, decision, feedback="用户反馈", decided_at=17).to_dict()
    assert trace == ["before_wait", "permission_requested", "wait", "permission_resolved"]
    assert events == [{"kind": "permission_requested", "permission": request.to_dict()},
                      {"kind": "permission_resolved", **result}]
    assert len(waiter.calls) == 1
    received, options = waiter.calls[0]
    assert isinstance(received, ToolApprovalRequest) and received == request
    assert options == ToolApprovalWaitOptions(mode_decision_provider=provider)
    assert not (tmp_path / ".approvals").exists()


# LLM: 两条等待路径均不得越过取消、截止、非交互或既有模式决定。
# 函数用途: 验证外层策略在等待器之前完成短路。
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("guard", ["cancelled", "expired", "noninteractive", "auto_approved", "auto_denied"])
def test_outer_policy_short_circuits_before_either_waiter(tmp_path, monkeypatch, custom, guard):
    request, events, grants = _request(), [], []
    token = SimpleNamespace(cancelled=guard == "cancelled")
    if guard in {"expired", "cancelled"}:
        request = replace(request, expires_at=1)

    # LLM: 只有自动模式分支可读取 provider，其余调用立即判失败。
    # 函数用途: 提供明确批准或拒绝以验证短路优先级。
    def provider(req):
        if guard.startswith("auto_"):
            return ToolApprovalDecision(req.permission_id, guard.removeprefix("auto_"))
        return _forbidden()

    stream = StreamApproval(tmp_path / "chunks", events.append, _forbidden,
                            decision_waiter=_forbidden if custom else None)
    cache = ToolApprovalSessionCache()
    cache.approve("scope:w7", "probe:hash:w7")
    stream.configure(cache, lambda: "scope:w7", grants.append)
    monkeypatch.setattr(stream_approval, "wait_for_gateway_permission_decision", _forbidden)
    result = stream.request(request.to_dict(), options=StreamApprovalRequestOptions(
        interactive=False, mode_decision_provider=None if guard == "noninteractive" else provider,
        cancellation_token=token))
    expected = {"cancelled": ("cancelled", ""), "expired": ("denied", "APPROVAL_TIMEOUT"),
                "noninteractive": ("denied", "APPROVAL_NONINTERACTIVE"),
                "auto_approved": ("approved", ""), "auto_denied": ("denied", "")}[guard]
    assert (result["decision"], result.get("reason_code", "")) == expected
    assert result["permission_id"] == request.permission_id
    assert events == grants == []


# LLM: 两种邮箱适配均须精确校验身份、绑定和决定枚举，不得污染授权缓存。
# 函数用途: 验证错配审批内容始终失败关闭。
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("field_name", ["permission_id", "request_id", "binding", "malformed"])
def test_mailbox_waiters_still_reject_wrong_binding(tmp_path, field_name, custom):
    request, events, grants = _request(), [], []
    chunk = tmp_path / "chunks"
    transport = tmp_path / "custom" / "chunks" if custom else chunk
    path = permission_bridge.gateway_permission_decision_path(transport, request)

    # LLM: 替代路径只绑定传输位置，身份核验继续走原桥接入口。
    # 函数用途: 提供协议兼容的可信邮箱适配器。
    def waiter(req, options):
        return permission_bridge.wait_for_gateway_permission_decision(transport, req, options=options)

    # LLM: 恶意内容仅写临时目录，不能被当作有效会话批准。
    # 函数用途: 制造错配身份或非法枚举的决定文件。
    def publish(event):
        events.append(event)
        if event["kind"] != "permission_requested":
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**ToolApprovalDecision(request.permission_id, "approved_session").to_dict(),
                   "request_id": request.request_id, "binding": dict(request.binding)}
        if field_name == "binding":
            payload["binding"]["args_hash"] = "foreign:hash"
        elif field_name == "malformed":
            payload["decision"] = "untrusted-choice"
        else:
            payload[field_name] = "foreign:identity"
        path.write_text(json.dumps(payload), encoding="utf-8")

    cache = ToolApprovalSessionCache()
    stream = StreamApproval(chunk, publish, lambda: None, decision_waiter=waiter if custom else None)
    stream.configure(cache, lambda: "scope:w7", grants.append)
    result = stream.request(request.to_dict(), options=StreamApprovalRequestOptions(True))
    assert result["decision"] == "unavailable"
    assert result["permission_id"] == request.permission_id
    assert [event["kind"] for event in events] == ["permission_requested", "permission_resolved"]
    assert not cache.is_approved("scope:w7", "probe:hash:w7") and not grants
    assert not path.exists()


# LLM: 插件强制确认时不得读取作用域或自主 provider，也不得新记缓存或长期授权。
# 函数用途: 验证两条等待路径都保留插件确认边界。
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("decision", ["approved_session", "approved_owner", "denied"])
def test_plugin_gate_bypasses_neither_confirmation_nor_cache_policy(tmp_path, custom, cached, decision):
    request = _request()
    request = replace(request, binding={**request.binding, "plugin_gate_ref": "host:gate", "grant_key": "probe:mode=write"})
    cache, events, grants = ToolApprovalSessionCache(), [], []
    if cached:
        cache.approve("scope:w7", "probe:hash:w7")
    chunk = tmp_path / "chunks"
    waiter = RecordingWaiter(decision)

    # LLM: 默认路径使用真实临时邮箱，自定义路径由已绑定的可信替身答复。
    # 函数用途: 记录插件审批事件并按传输方式提供决定。
    def publish(event):
        events.append(event)
        if not custom and event["kind"] == "permission_requested":
            permission_bridge.write_gateway_permission_decision(
                chunk, request, ToolApprovalDecision(request.permission_id, decision))

    stream = StreamApproval(chunk, publish, lambda: None, decision_waiter=waiter if custom else None)
    stream.configure(cache, _forbidden, grants.append)
    result = stream.request(request.to_dict(), options=StreamApprovalRequestOptions(True, _forbidden))
    assert result["decision"] == decision
    assert [event["kind"] for event in events] == ["permission_requested", "permission_resolved"]
    assert cache.is_approved("scope:w7", "probe:hash:w7") == cached
    assert not grants
    assert len(waiter.calls) == int(custom)


# LLM: 复用只容许同 owner/线程/聊天/目录/权限和同一调用，身份更新不能扩大作用域。
# 函数用途: 逐项验证会话缓存命中及跨边界重询。
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("boundary", [
    "owner_id", "thread_id", "conversation_id", "cwd", "access_mode", "path_access_mode", "tool_name", "args_hash",
])
def test_session_approval_remains_exact_across_all_authority_boundaries(tmp_path, custom, boundary):
    request, events = _request(), []
    context = dict(owner_id="alice", thread_id="thread:w7", conversation_id="chat:w7", cwd="/workspace/w7",
                   access_mode="workspace-write", path_access_mode="normal")
    cache = ToolApprovalSessionCache()
    waiter = RecordingWaiter("approved_session")
    chunk = tmp_path / "chunks"
    decision = ["approved_session"]

    # LLM: 决定绑定事件内的当前请求，避免使用旧 permission_id 掩盖错配。
    # 函数用途: 在实际询问时回写当前审批的决定。
    def publish(event):
        events.append(event)
        if not custom and event["kind"] == "permission_requested":
            pending = ToolApprovalRequest.from_mapping(event["permission"])
            permission_bridge.write_gateway_permission_decision(
                chunk, pending, ToolApprovalDecision(pending.permission_id, decision[0]))

    stream = StreamApproval(chunk, publish, lambda: None, cache, lambda: tool_approval_session_scope(**context),
                            None, waiter if custom else None)
    options = StreamApprovalRequestOptions(True)
    assert stream.request(request.to_dict(), options=options)["decision"] == "approved_session"
    old_scope = tool_approval_session_scope(**context)
    assert cache.is_approved(old_scope, "probe:hash:w7")
    waiter.decision, decision[0] = "denied", "denied"
    renewed = replace(request, permission_id="approval:renewed", request_id="request:renewed")
    assert stream.request(renewed.to_dict(), options=options)["decision"] == "approved"
    assert events[-1]["session_cached"] is True
    assert events[-1]["permission_id"] == renewed.permission_id
    if boundary in context:
        context[boundary] = "different:authority"
    else:
        binding = {**renewed.binding, boundary: "different:call"}
        renewed = replace(renewed, binding=binding, tool_name=binding["tool_name"])
    assert stream.request(renewed.to_dict(), options=options)["decision"] == "denied"
    assert [event["kind"] for event in events] == [
        "permission_requested", "permission_resolved", "permission_resolved", "permission_requested", "permission_resolved",
    ]
    assert "session_cached" not in events[-1]
    assert len(waiter.calls) == (2 if custom else 0)


# LLM: 只有显式 approved_session/approved_owner 可记对应授权；缺少 grant 或 recorder 不得持久化。
# 函数用途: 验证注入等待后的缓存与长期授权副作用。
@pytest.mark.parametrize("decision", DECISIONS)
@pytest.mark.parametrize("with_grant", [False, True])
@pytest.mark.parametrize("with_recorder", [False, True])
def test_custom_waiter_records_only_explicit_scoped_session_or_owner_grants(tmp_path, decision, with_grant, with_recorder):
    request, grants = _request(), []
    if with_grant:
        request = replace(request, binding={**request.binding, "grant_key": "probe:mode=write"})
    cache = ToolApprovalSessionCache()
    stream = StreamApproval(tmp_path / "chunks", lambda event: None, lambda: None,
                            decision_waiter=RecordingWaiter(decision))
    stream.configure(cache, lambda: "scope:w7", grants.append if with_recorder else None)
    assert stream.request(request.to_dict(), options=StreamApprovalRequestOptions(True))["decision"] == decision
    assert cache.is_approved("scope:w7", "probe:hash:w7") == (decision == "approved_session")
    assert grants == (["probe:mode=write"] if decision == "approved_owner" and with_grant and with_recorder else [])


# LLM: 自定义适配器显式闭包绑定宿主 token；时间与取消在等待中变化仍须停止。
# 函数用途: 验证默认及可信适配器等待中的取消和绝对截止。
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("outcome", ["cancelled", "expired"])
def test_waiter_adapters_preserve_cancellation_and_absolute_deadline_during_wait(tmp_path, monkeypatch, custom, outcome):
    request = _request(requested_at=100, expires_at=101)
    now, events, calls = [100], [], []
    token = SimpleNamespace(cancelled=False)
    chunk = tmp_path / "chunks"
    transport = tmp_path / "custom" / "chunks" if custom else chunk
    monkeypatch.setattr(permission_bridge.time, "time", lambda: now[0])

    # LLM: 不实际睡眠，以确定性时钟触发同一等待循环的下一次核验。
    # 函数用途: 模拟等待中取消或到期。
    def advance(interval):
        assert interval > 0
        if outcome == "cancelled":
            token.cancelled = True
        else:
            now[0] = request.expires_at

    # LLM: token 由可信宿主闭包绑定而非来自模型参数。
    # 函数用途: 将协议调用接入原精确邮箱等待器。
    def waiter(req, options):
        calls.append(req)
        return permission_bridge.wait_for_gateway_permission_decision(
            transport, req, cancellation_token=token, options=options)

    monkeypatch.setattr(permission_bridge.time, "sleep", advance)
    stream = StreamApproval(chunk, events.append, lambda: None, decision_waiter=waiter if custom else None)
    result = stream.request(request.to_dict(), options=StreamApprovalRequestOptions(True, cancellation_token=token))
    assert result["decision"] == ("cancelled" if outcome == "cancelled" else "denied")
    assert result.get("reason_code", "") == ("" if outcome == "cancelled" else "APPROVAL_TIMEOUT")
    assert [event["kind"] for event in events] == ["permission_requested", "permission_resolved"]
    assert calls == ([request] if custom else [])


# LLM: 不可信请求字典的额外键没有宿主注入资格，不修改工具 JSON schema。
# 函数用途: 验证请求内容不能覆盖已绑定的等待器。
def test_untrusted_request_payload_cannot_select_waiter(tmp_path):
    request = _request()
    payload = {**request.to_dict(), "decision_waiter": _forbidden}
    waiter = RecordingWaiter("denied")
    stream = StreamApproval(tmp_path / "chunks", lambda event: None, lambda: None, decision_waiter=waiter)
    assert stream.request(payload, options=StreamApprovalRequestOptions(True))["decision"] == "denied"
    assert waiter.calls[0][0] == request


# LLM: 只写 tmp_path 的两份策略位置，使用原持久化函数并核对其它字段、权限和隔离。
# 函数用途: 证明两条等待路径的长期批准会真实落盘并供新流读取。
@pytest.mark.parametrize("custom", [False, True])
def test_owner_grant_persists_without_touching_other_owner_policy(tmp_path, custom):
    from agent_py_agent.agent.user_space.operation_grants import (
        owner_operation_granted,
        record_owner_operation_grant,
    )

    policy = tmp_path / "alice" / "tool_policy.json"
    policy.parent.mkdir()
    original = {"schema_version": "tool-policy.v1", "permission_mode": "ask", "disabled_tools": ["other_tool"]}
    policy.write_text(json.dumps(original), encoding="utf-8")
    home = SimpleNamespace(owner_tool_policy_json=policy)
    other_home = SimpleNamespace(owner_tool_policy_json=tmp_path / "bob" / "tool_policy.json")
    request, events = _request(), []
    key = "probe:mode=write"
    request = replace(request, binding={**request.binding, "grant_key": key})
    chunk = tmp_path / "chunks"

    # LLM: 持久化位置仅由宿主引用决定，不从审批 payload 选择 owner。
    # 函数用途: 通过原 recorder 将当前批准写入本例的策略文件。
    def record(grant_key):
        record_owner_operation_grant(home, grant_key, source="w7:test")

    # LLM: 默认路径仍由真实决定文件驱动，未注入替代授权来源。
    # 函数用途: 记录请求事件并回复长期批准。
    def publish(event):
        events.append(event)
        if not custom and event["kind"] == "permission_requested":
            permission_bridge.write_gateway_permission_decision(
                chunk, request, ToolApprovalDecision(request.permission_id, "approved_owner"))

    stream = StreamApproval(chunk, publish, lambda: None,
                            decision_waiter=RecordingWaiter("approved_owner") if custom else None)
    stream.configure(ToolApprovalSessionCache(), lambda: "scope:w7", record)
    assert stream.request(request.to_dict(), options=StreamApprovalRequestOptions(True))["decision"] == "approved_owner"
    stored = json.loads(policy.read_text(encoding="utf-8"))
    assert {name: stored[name] for name in original} == original
    assert stored["operation_grants"][key]["source"] == "w7:test"
    assert policy.stat().st_mode & 0o777 == 0o600
    assert owner_operation_granted(SimpleNamespace(owner_tool_policy_json=policy), key)
    assert not owner_operation_granted(other_home, key)
    assert not other_home.owner_tool_policy_json.exists()

    # LLM: 新流重新读取已落盘策略，不借用上一条流的会话缓存。
    # 函数用途: 模拟宿主按原长期授权提供者读取同一精确操作键。
    def provider(pending):
        if owner_operation_granted(SimpleNamespace(owner_tool_policy_json=policy), pending.binding["grant_key"]):
            return ToolApprovalDecision(pending.permission_id, "approved")
        return None

    renewed = replace(request, permission_id="approval:next", request_id="request:next")
    fresh = StreamApproval(tmp_path / "fresh", _forbidden, _forbidden, decision_waiter=_forbidden)
    assert fresh.request(renewed.to_dict(), options=StreamApprovalRequestOptions(True, provider))["decision"] == "approved"
