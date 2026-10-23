# LLM: A0协议测试只处理内存JSON和临时数据，拒绝不受信身份与字段，不运行模型。
# 模块用途: 锁定插件宿主v2的严格解析、动作权限、纯投影和有界传输合同。
from __future__ import annotations

import json

import pytest


# LLM: 示例从设计逐项编写，不从待测schema反向生成，避免错误字段互相证明。
# 函数用途: 给出42个动作的独立合法输入及读写分类。
def action_cases():
    thread = {"thread_id": "thread-test"}
    goal = {**thread, "goal_id": "goal-test", "expected_revision": 1}
    request = {"request_id": "request-test"}
    user = {"user_id": "alice"}
    task = {"task_id": "task-test"}
    return {
        "host.capabilities.get": ({}, "read"),
        "conversations.threads.create": ({"title": "测试"}, "write"),
        "conversations.threads.get": (thread, "read"),
        "conversations.threads.list": ({}, "read"),
        "conversations.threads.rename": ({**thread, "title": "新标题", "expected_revision": 1}, "write"),
        "conversations.threads.archive": ({**thread, "expected_revision": 1}, "write"),
        "conversations.threads.restore": ({**thread, "expected_revision": 1}, "write"),
        "conversations.messages.list": (thread, "read"),
        "conversations.messages.send": ({**thread, "text": "你好"}, "async"),
        "conversations.requests.poll": (request, "read"),
        "conversations.requests.stream": (request, "stream"),
        "conversations.requests.cancel": ({**request, "expected_revision": 1}, "async"),
        "users.local.create": (user, "write"),
        "users.local.list": ({}, "read"),
        "users.local.get": (user, "read"),
        "users.local.disable": ({**user, "expected_revision": 1}, "write"),
        "users.local.enable": ({**user, "expected_revision": 1}, "write"),
        "users.local.quota.get": (user, "read"),
        "execute.tool": ({**thread, "tool_name": "read_file", "arguments": {"path": "a.txt"}}, "async"),
        "execute.script": ({**thread, "script_id": "script-test", "expected_revision": 1}, "async"),
        "execute.workflow": ({**thread, "workflow_id": "workflow-test", "expected_revision": 1}, "async"),
        "execute.workflow_resume": ({**thread, "workflow_run_id": "run-test", "expected_revision": 1,
                                     "from_step_id": "step-one"}, "async"),
        "interactions.pending_approvals.list": (thread, "read"),
        "interactions.pending_inputs.list": (thread, "read"),
        "interactions.approvals.respond": ({"interaction_id": "card-test", "expected_revision": 1,
                                          "response_ticket": "opaque-ticket"}, "write"),
        "interactions.inputs.respond": ({"interaction_id": "card-test", "expected_revision": 1,
                                       "response_ticket": "opaque-ticket"}, "write"),
        "tasks.list": (thread, "read"), "tasks.get": (task, "read"), "tasks.progress": (task, "read"),
        "tasks.cancel": ({**task, "expected_revision": 1}, "async"),
        "goals.list": (thread, "read"), "goals.get": ({**thread, "goal_id": "goal-test"}, "read"),
        "goals.create": ({**thread, "objective": "检查说明"}, "write"),
        "goals.update": ({**goal, "objective": "新的说明"}, "write"),
        "goals.pause": (goal, "write"), "goals.resume": (goal, "write"), "goals.clear": (goal, "write"),
        "memory.search": ({"query": "项目"}, "read"), "memory.get": ({"entry_id": "entry-test"}, "read"),
        "workspace.artifacts.list": (task, "read"),
        "workspace.artifacts.get": ({"artifact_ref": "artifact-test"}, "read"),
        "workspace.artifacts.read": ({"artifact_ref": "artifact-test", "expected_sha256": "a" * 64}, "read"),
    }


# LLM: 构造仅用于测试的公共信封，目标选择与幂等键不进入payload。
# 函数用途: 将合法动作样本编码为真实HTTP字节。
def wire(action="host.capabilities.get", payload=None, mode="read", as_user=None):
    body = {"schema": "plugin_host_call.v2", "action": action, "payload": payload or {}}
    if mode in {"write", "async"}:
        body["idempotency_key"] = "test_operation_0001"
    if as_user is not None:
        body["as_user"] = {"user_id": as_user}
    return json.dumps(body, ensure_ascii=False).encode()


# LLM: 首个红灯直接验证解析行为，不能用模块存在性代替合同。
# 函数用途: 验证合法信封解析为固定版本的不可变调用。
def test_decode_valid_call():
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    call = decode_call(wire())
    assert call is not None
    assert call.schema == "plugin_host_call.v2"
    assert call.action == "host.capabilities.get" and dict(call.payload) == {}
    assert call.idempotency_key is None and call.as_user_id is None


# LLM: 异常输入在任何owner创建、领域调用或文件写入前拒绝。
# 函数用途: 验证重复键、错误版本、非法UTF8及非有限数失败关闭。
@pytest.mark.parametrize("raw", [
    b'{"schema":"plugin_host_call.v2","action":"host.capabilities.get","payload":{},"payload":{}}',
    b'{"schema":"plugin_host_call.v2","action":"host.capabilities.get","payload":{"x":NaN}}',
    b'{"schema":"v1","action":"host.capabilities.get","payload":{}}',
    b'{"schema":"plugin_host_call.v2","action":"host.capabilities.get","payload":{},"admin":true}',
    b'\xff', b'[]', b'{}', b'null',
])
def test_decode_rejects_invalid_json_and_envelopes(raw):
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    with pytest.raises(HostAPIError) as error:
        decode_call(raw)
    assert error.value.code == "INVALID_SCHEMA"


# LLM: 全动作样本来自固定需求，不允许新注册表漏动作或随意变更模式。
# 函数用途: 验证42个闭合动作及各自幂等要求。
@pytest.mark.parametrize("action", tuple(action_cases()))
def test_each_action_has_closed_payload_and_exact_mode(action):
    from agent_py_agent.agent.plugin_host.action_specs import action_specs
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    payload, mode = action_cases()[action]
    assert set(action_specs()) == set(action_cases())
    assert action_specs()[action].mode == mode
    call = decode_call(wire(action, payload, mode, "alice" if action.startswith("execute.") else None))
    assert call.action == action
    with pytest.raises(HostAPIError):
        decode_call(wire(action, {**payload, "extra": 1}, mode))


# LLM: 不能把布尔值当版本或limit，不能静默截断超大正文。
# 函数用途: 验证边界数值、UTF8字节与身份注入。
@pytest.mark.parametrize("action,payload,mode", [
    ("conversations.threads.list", {"limit": True}, "read"),
    ("conversations.threads.list", {"limit": 101}, "read"),
    ("conversations.messages.send", {"thread_id": "t", "text": "中" * 5462}, "async"),
    ("execute.tool", {"thread_id": "t", "tool_name": "read_file", "arguments": {"nested": {"owner_id": "main"}}}, "async"),
    ("conversations.threads.rename", {"thread_id": "t", "title": "a", "expected_revision": False}, "write"),
    ("workspace.artifacts.read", {"artifact_ref": "a", "expected_sha256": "a" * 64, "max_bytes": 65537}, "read"),
])
def test_payload_boundaries(action, payload, mode):
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    with pytest.raises(HostAPIError):
        decode_call(wire(action, payload, mode, "alice" if action.startswith("execute.") else None))


# LLM: 只读不能夹写入幂等键，写操作不能靠默认空键重放。
# 函数用途: 验证键、用户选择器及深层结构限制。
def test_idempotency_selector_depth_and_body_limits():
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    values = [wire(mode="write"), wire("conversations.threads.create"),
              wire("users.local.get", {"user_id": "alice"}, as_user="bob")]
    nested = {}
    for _ in range(14):
        nested = {"nested": nested}
    values.append(wire("execute.tool", {"thread_id": "t", "tool_name": "read_file", "arguments": nested},
                       "async", "alice"))
    for raw in values:
        with pytest.raises(HostAPIError):
            decode_call(raw)
    with pytest.raises(HostAPIError) as error:
        decode_call(b" " * 65537)
    assert error.value.status == 413


# LLM: 能力按AND交集，poll的请求类型必须由领域权威二次确认。
# 函数用途: 验证条件能力、UID和固定普通用户权限。
def test_conditional_capabilities_and_restricted_helpers():
    from agent_py_agent.agent.plugin_host.contracts import (
        required_capabilities,
        restricted_permission,
        validate_local_user_id,
    )
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    assert required_capabilities("host.capabilities.get", {}) == frozenset()
    assert required_capabilities("conversations.messages.send", {"tool_policy": "bounded",
                                 "input_artifact_refs": ["a"]}) == {"chat", "execute", "artifacts.read"}
    assert required_capabilities("execute.tool", {}) == {"execute", "act_as_user"}
    assert required_capabilities("goals.resume", {}) == {"goals.control", "chat"}
    permission = restricted_permission()
    assert not permission.is_admin and not permission.can_access_all_users
    assert permission.access_mode == "workspace_only" and permission.role == "USER"
    assert validate_local_user_id("alice") == "alice"
    for uid in ["main", "admin", "root", "system", "local", "Alice", "../alice", "aa", " alice"]:
        with pytest.raises(HostAPIError):
            validate_local_user_id(uid)


# LLM: 已持久终态优先；取消请求本身不是进程停止证明。
# 函数用途: 锁定请求公开状态投影的真值来源。
def test_request_state_precedence():
    from agent_py_agent.agent.plugin_host.contracts import project_request_state
    inputs = dict(receipt_state="queued", claim_state="running", cancellation_requested=True,
                  canonical_terminal=None, execution_unknown=False)
    assert project_request_state(**inputs) == "cancelling"
    assert project_request_state(**{**inputs, "execution_unknown": True}) == "unknown"
    assert project_request_state(**{**inputs, "canonical_terminal": {"status": "succeeded"}}) == "succeeded"
    assert project_request_state(**{**inputs, "cancellation_requested": False}) == "running"


# LLM: UTC不是仅匹配字符串形状，联合数值类型也不得将布尔当整数。
# 函数用途: 验证事件公共结构的日期和可空整数严格性。
@pytest.mark.parametrize("change", ["bad_date", "bool_usage"])
def test_request_event_strict_timestamp_and_nullable_integer(change):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.plugin_host.schemas import validate_request_event
    row = {"event_id": "event-1", "sequence": 1, "kind": "terminal", "created_at": "2026-10-23T00:00:00Z",
           "data": {"terminal_id": "terminal-1", "status": "succeeded", "completed_at": "2026-10-23T00:00:00Z",
                    "result_message_ids": [], "error_code": None, "usage": {"input_tokens": None, "output_tokens": 1},
                    "resources_stopped": None}}
    assert validate_request_event(row) == row
    if change == "bad_date":
        row["created_at"] = "2026-02-31T00:00:00Z"
    else:
        row["data"]["usage"]["input_tokens"] = True
    with pytest.raises(HostAPIError):
        validate_request_event(row)
