# LLM: A0协议测试只处理内存JSON和临时数据，拒绝不受信身份与字段，不运行模型。
# 模块用途: 锁定插件宿主v2的严格解析、动作权限、纯投影和有界传输合同。
from __future__ import annotations

import json

import pytest


# LLM: 示例从设计逐项编写，不从待测schema反向生成，避免错误字段互相证明。
# 函数用途: 给出45个动作的独立合法输入及读写分类。
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
        "users.local.password.set": ({**user, "password": "ValidPassword1", "expected_revision": 1}, "write"),
        "users.auth.login": ({"account_source": "local", "user_id": "alice", "password": "ValidPassword1"}, "read"),
        "users.auth.verify": ({"session_token": "session_token_valid_sample_01"}, "read"),
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


# LLM: 原动作与扩展矩阵分别独立校验，不能把新增动作误判为原目录变更。
# 函数用途: 验证原45个闭合动作及各自幂等要求。
@pytest.mark.parametrize("action", tuple(action_cases()))
def test_each_action_has_closed_payload_and_exact_mode(action):
    from agent_py_agent.agent.plugin_host.action_specs import action_specs
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    payload, mode = action_cases()[action]
    from agent_py_agent.agent.plugin_host.action_specs_v2_ext import V2_ACTIONS
    assert set(action_specs()) - set(V2_ACTIONS) == set(action_cases())
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


# 21项来自WT1/WT2公共方法契约，不从生产schema生成样例。
DELIVERY_GOVERNANCE_CASES = (
    ("assets.delivery.targets.list", "read", "assets.read", {}),
    ("assets.delivery.preview_bundle", "read", "assets.read", {"root_asset_kind": "workflow", "root_asset_id": "demo"}),
    ("assets.delivery.send", "write", "assets.write", {"recipient_owner_id": "local/alice", "root_asset_kind": "workflow", "root_asset_id": "demo"}),
    ("assets.delivery.inbox.list", "read", "assets.read", {}),
    ("assets.delivery.accept", "write", "assets.write", {"delivery_id": "delivery-one"}),
    ("assets.delivery.reject", "write", "assets.write", {"delivery_id": "delivery-one"}),
    ("assets.delivery.toggle_asset", "write", "assets.write", {"asset_kind": "script", "asset_id": "demo", "enabled": True}),
    ("assets.delivery.blocklist.list", "read", "assets.read", {}),
    ("assets.delivery.blocklist.update", "write", "assets.write", {"target_owner_id": "local/bobby", "mode": "block"}),
    ("users.managed.overview.get", "read", "users.read", {"target_owner_id": "local/alice"}),
    ("users.managed.policy.set", "write", "users.manage", {"target_owner_id": "local/alice", "policy_patch": {}, "expected_revision": 1}),
    ("users.managed.persona.get", "read", "users.read", {"target_owner_id": "local/alice"}),
    ("users.managed.persona.set", "write", "users.manage", {"target_owner_id": "local/alice", "soul_md": "角色", "expected_revision": 1}),
    ("users.managed.memories.list", "read", "users.read", {"target_owner_id": "local/alice"}),
    ("users.managed.memories.mutate", "write", "users.manage", {"target_owner_id": "local/alice", "operation": "add_memory", "content": "事实"}),
    ("users.managed.assets.provision", "write", "users.manage", {"target_owner_id": "local/alice", "asset_kind": "script", "asset_id": "demo"}),
    ("users.managed.assets.toggle", "write", "users.manage", {"target_owner_id": "local/alice", "asset_kind": "script", "asset_id": "demo", "enabled": False}),
    ("users.managed.tasks.dispatch", "write", "users.manage", {"target_owner_id": "local/alice", "prompt": "核对资料"}),
    ("users.managed.tasks.list", "read", "users.read", {"target_owner_id": "local/alice"}),
    ("governance.reviews.list", "read", "users.read", {"user_id": "alice"}),
    ("governance.reviews.decide", "write", "users.manage", {"user_id": "alice", "review_id": "review-one", "decision": "approve", "expected_revision": 1}),
)


# LLM: 扩展矩阵必须完整覆盖所有登记项，防止目录新增后原45项测试误红或漏测。
# 函数用途: 校验WP4原20项与本轮21项的闭合schema、AND能力及幂等门。
def test_all_v2_actions_have_valid_and_invalid_schema_coverage():
    from agent_py_agent.agent.plugin_host.action_specs import action_specs
    from agent_py_agent.agent.plugin_host.action_specs_v2_ext import V2_ACTIONS
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.tests.test_plugin_host_v2_five_capabilities import ACTION_CASES

    cases = [(a, mode, caps, payload) for a, mode, caps, payload in ACTION_CASES]
    cases += [(a, mode, {mode, cap}, payload) for a, mode, cap, payload in DELIVERY_GOVERNANCE_CASES]
    assert {row[0] for row in cases} == set(V2_ACTIONS)
    for action, mode, caps, payload in cases:
        spec = action_specs()[action]
        assert spec.mode == mode and spec.capabilities == caps
        assert decode_call(wire(action, payload, mode)).action == action
        with pytest.raises(HostAPIError):
            decode_call(wire(action, {**payload, "extra": True}, mode))
        with pytest.raises(HostAPIError):
            decode_call(wire(action, payload, "read" if mode == "write" else "write"))


# LLM: IM/main目标应到领域403而非schema400；此处只证selector能通过解码。
# 函数用途: 验证治理目标允许规范Owner字符串，同时仍拒绝嵌套身份注入。
@pytest.mark.parametrize("target", ["providers/feishu/users/ou_xxx", "local/main", "alice"])
@pytest.mark.parametrize("field", ["target_owner_id", "user_id"])
def test_managed_targets_are_domain_checked_not_local_uid_schema(target, field):
    from agent_py_agent.agent.plugin_host.contracts import decode_call

    assert decode_call(wire("users.managed.persona.get", {field: target})).payload[field] == target


# LLM: 清单与批准表必须共同认得新read能力，不从write隐式推导read。
# 函数用途: 验证assets.read和users.read能正常申请与批准。
def test_delivery_and_governance_read_capabilities_are_grantable():
    from agent_py_agent.agent.plugin_manifest_host_v9 import host_capabilities
    from agent_py_agent.agent.plugin_permissions.host_grants import validate_host_grant
    from agent_py_agent.tests.test_plugin_host_v2_grants import grant_payload

    assert {"assets.read", "users.read"} <= host_capabilities()
    grant = {**grant_payload(), "approved_capabilities": ["read", "assets.read", "users.read"]}
    assert validate_host_grant(grant) == grant


# LLM: 安装与批准均用原产品链，虚构账号仅在临时host里创建，不启动插件进程或外网服务。
# 函数用途: 创建带有限治理名单的真实issuer供权限和适配接线测试。
@pytest.fixture
def governed_host(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.plugin_activation import PluginActivationRequest
    from agent_py_agent.agent.plugin_activation_record import (
        PluginActivation,
        plugin_catalog_digest,
    )
    from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
    from agent_py_agent.agent.plugin_host.authorization import IngressIssuer
    from agent_py_agent.agent.plugin_manifest_host_v9 import host_capabilities
    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    from agent_py_agent.tests.test_plugin_host_v2_grants import installed_grant

    owner, store, entry, plan, os_grant, grant = installed_grant(tmp_path, sorted(host_capabilities()))
    grant["manageable_user_ids"] = ["alice"]
    for uid in ("alice", "bobby"):
        LocalUserLifecycle(LocalUserStore(tmp_path)).create(uid, LocalUserOperation("test-install", "a" * 64, "create-user-key-" + uid))
    preparing = PluginActivation(plan, "preparing", permission_json=json.dumps({**os_grant, "host_api_grant": grant}))
    prepared = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, preparing)).installation
    active = replace(preparing, phase="active", catalog_sha256=plugin_catalog_digest(entry.manifest))
    enabled = store.change_activation(PluginActivationRequest(plan.operation_id, prepared.revision, active)).installation
    ref = PluginActivationRef.from_owner(owner, entry.manifest.plugin_id, active.activation_id)
    api.set_host_api_base("http://127.0.0.1:8420")
    env = api.issue_host_api_env(ref, entry.manifest.plugin_id)
    issuer = IngressIssuer()
    try:
        yield issuer, issuer.ingress(env[api.HOST_API_TOKEN_ENV]), owner, store, enabled
    finally:
        issuer.close()
        api.set_host_api_base(None)


# LLM: selector规范化只可去掉固定local前缀，不能将IM、main或名单外账号洗成合法目标。
# 函数用途: 通过真实issuer锁定治理范围的403与合法别名。
@pytest.mark.parametrize("target,allowed", [("alice", True), ("local/alice", True), ("local/users/alice", True),
    ("bobby", False), ("local/bobby", False), ("local/main", False), ("providers/feishu/users/ou_xxx", False)])
def test_governance_authorization_checks_manageable_user_ids(governed_host, target, allowed):
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    issuer, source, *_ = governed_host
    call = decode_call(wire("users.managed.persona.get", {"target_owner_id": target}))
    if allowed:
        assert issuer.authorize(source, call).effective_owner == source._issuer._token(source._lease).activation_ref.owner().identity
    else:
        with pytest.raises(HostAPIError) as failure:
            issuer.authorize(source, call)
        assert (failure.value.status, failure.value.code) == (403, "MANAGED_USER_SCOPE_DENIED")


# LLM: 账号选择不能替代真实管理员来源；公共插件普通使用者不能继承安装者治理权限。
# 函数用途: 拒绝普通用户借管理员公共安装读取别人的人设。
def test_governance_public_plugin_still_requires_actual_admin(governed_host):
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    issuer, source, *_ = governed_host
    caller = issuer.ingress(source._lease, caller_owner=OwnerIdentity.local_user("alice"))
    with pytest.raises(HostAPIError) as failure:
        issuer.authorize(caller, decode_call(wire("users.managed.persona.get", {"user_id": "bobby"})))
    assert failure.value.status == 403


# LLM: 发现页也不应向公共插件普通使用者宣称治理read可用；不能只在最终调用403。
# 函数用途: 验证有效能力投影移除不属于真实管理员的治理读取权。
def test_public_non_admin_discovery_does_not_inherit_users_read(governed_host):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    issuer, source, *_ = governed_host
    caller = issuer.ingress(source._lease, caller_owner=OwnerIdentity.local_user("alice"))
    assert "users.read" not in issuer._live(caller)[2]


# LLM: 领域替身只定义WT1/WT2公共接口；真实解码、issuer、组合根和原控制幂等账不替换。
# 函数用途: 隔离未合并的领域实现，逐动作核对适配参数和真实宿主分派。
@pytest.fixture
def wired_domains(governed_host, monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from agent_py_agent.agent.plugin_host.contracts import HostDomainPorts
    from agent_py_agent.agent.plugin_host.gateway_composition import build_gateway_host_composition

    issuer, source, owner, *_ = governed_host
    calls = []

    # LLM: 可替换业务结果，但不参与身份/权限判断；所有调用实参留下证据供断言。
    # 类用途: 模拟兄弟工作树已声明的公开服务方法，不读写真实业务资产。
    class Service:
        def __init__(self, root_dir, *, actor_owner=None):
            assert root_dir == owner.root
            self.actor_owner = actor_owner

    # LLM: 方法名由测试固定列表产生，插件不能动态选择属性；返回值只用于适配测试。
    # 函数用途: 创建记录准确参数的领域方法。
    def method(name):
        def invoke(self, *args, **kwargs):
            calls.append((name, args, kwargs, self.actor_owner))
            if name == "list_reviews":
                return {"items": [{"review_id": "review-one", "applicant_owner_id": "local/alice"}]}
            return {"method": name, "args": list(args), "kwargs": kwargs}
        return invoke

    methods = ("list_targets", "preview_bundle", "send_bundle", "list_inbox", "accept_delivery", "reject_delivery",
               "toggle_asset", "get_blocklist", "update_blocklist", "get_overview", "set_policy", "get_persona", "set_persona",
               "list_memories", "mutate_memories", "provision_assets", "dispatch_task", "list_tasks", "list_reviews", "decide_review")
    for name in methods:
        setattr(Service, name, method(name))
    for module_name, class_name in [("assets.delivery_service", "AssetDeliveryService"),
                                    ("user_space.managed_user_governance", "ManagedUserGovernanceService"),
                                    ("user_space.governance_review_store", "GovernanceReviewStore")]:
        module = ModuleType("agent_py_agent.agent." + module_name)
        setattr(module, class_name, Service)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    server = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(my_agent_home=owner.root)), control_plane=None)
    composition = build_gateway_host_composition(server, None)
    assert isinstance(composition.ports, HostDomainPorts)
    yield composition.ports, calls
    composition.close()


# LLM: 写入在原账本内实际记录执行与完成，再调用同一授权重放；不把替身业务成功当真实跨用户验收。
# 函数用途: 逐一证明21动作接通、写入仅执行一次、发送/接收身份来自当前owner。
@pytest.mark.parametrize("action,mode,cap,payload", DELIVERY_GOVERNANCE_CASES)
def test_all_twenty_one_actions_delegate_and_writes_replay(governed_host, wired_domains, action, mode, cap, payload):
    from agent_py_agent.agent.plugin_host.composition import dispatch_call, domain_ready
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.schemas import thaw

    issuer, source, *_ = governed_host
    ports, calls = wired_domains
    assert domain_ready(action, ports)
    authorized = issuer.authorize(source, decode_call(wire(action, payload, mode)))
    result = dispatch_call(authorized, ports)
    assert result.http_status == 200 and calls
    before = list(calls)
    if mode == "write":
        assert thaw(dispatch_call(authorized, ports).data) == thaw(result.data)
        assert calls == before or (action == "governance.reviews.decide" and sum(row[0] == "decide_review" for row in calls) == 1)
    if action.startswith("assets.delivery."):
        assert calls[-1][1][0] == "local/main"
    elif action.startswith("users.managed."):
        assert calls[-1][1][0] == "local/alice"


# LLM: 缺领域应503且目录不可用，不能接受写请求后再伪装成功。
# 函数用途: 验证兄弟领域未合入时的安全未就绪行为。
def test_absent_delivery_domain_is_not_advertised_as_ready(governed_host, monkeypatch):
    import sys
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_host.composition import domain_ready
    from agent_py_agent.agent.plugin_host.gateway_composition import build_gateway_host_composition

    _, _, owner, *_ = governed_host
    monkeypatch.setitem(sys.modules, "agent_py_agent.agent.assets.delivery_service", None)
    server = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(my_agent_home=owner.root)), control_plane=None)
    composition = build_gateway_host_composition(server, None)
    assert not domain_ready("assets.delivery.send", composition.ports)
    composition.close()


# LLM: 归属必须到达实际决定写口，不能仅在列表预检验证一次然后丢失目标。
# 函数用途: 验证审核决定显式绑定准确Owner及真实actor。
def test_review_decision_binds_target_at_domain_write(governed_host, wired_domains):
    from agent_py_agent.agent.plugin_host.composition import dispatch_call
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    issuer, source, *_ = governed_host
    ports, calls = wired_domains
    call = decode_call(wire("governance.reviews.decide", {"user_id": "alice", "review_id": "review-one",
                                                        "decision": "approve", "expected_revision": 1}, "write"))
    dispatch_call(issuer.authorize(source, call), ports)
    decision = next(row for row in calls if row[0] == "decide_review")
    assert decision[2]["target_owner_id"] == "local/alice"
    assert decision[3] == OwnerIdentity.local_main()


# LLM: 仅允许三种准确selector形状，不能连续剥前缀接受叠加路径。
# 函数用途: 拒绝非合同别名，保留原白名单约束。
def test_managed_selector_rejects_stacked_local_prefixes(governed_host):
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    issuer, source, *_ = governed_host
    call = decode_call(wire("users.managed.persona.get", {"target_owner_id": "local/users/local/alice"}))
    with pytest.raises(HostAPIError) as failure:
        issuer.authorize(source, call)
    assert failure.value.code == "MANAGED_USER_SCOPE_DENIED"


# LLM: 写入已调用后返回坏DTO不能清掉executing账，否则重试会重复真实副作用。
# 函数用途: 验证未知领域结果保留unknown且同key不再执行业务。
def test_bad_write_result_retains_unknown_receipt(governed_host, wired_domains, monkeypatch):
    import sys

    from agent_py_agent.agent.plugin_host.composition import dispatch_call
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    issuer, source, *_ = governed_host
    ports, _ = wired_domains
    effects = []

    # LLM: 模拟副作用已发生但领域没有返回完整回执，不能把此结果当未执行。
    # 函数用途: 制造需要原unknown协议收口的写入结果。
    def incomplete(self, *args, **kwargs):
        effects.append(True)
        return None

    monkeypatch.setattr(sys.modules["agent_py_agent.agent.assets.delivery_service"].AssetDeliveryService, "send_bundle", incomplete)
    call = decode_call(wire("assets.delivery.send", {"recipient_owner_id": "local/alice", "root_asset_kind": "script", "root_asset_id": "demo"}, "write"))
    authorized = issuer.authorize(source, call)
    for _ in range(2):
        with pytest.raises(HostAPIError) as failure:
            dispatch_call(authorized, ports)
        assert (failure.value.code, failure.value.outcome) == ("OUTCOME_UNKNOWN", "unknown")
    assert effects == [True]


# LLM: 字符串异常正文不参与映射；固定机器字段决定HTTP码，秘密不回显。
# 函数用途: 覆盖八种任务要求的领域错误转换。
@pytest.mark.parametrize("code,status", [("MANAGED_USER_SCOPE_DENIED", 403), ("ASSET_DELIVERY_SECRET_DETECTED", 422),
    ("DELIVERY_BLOCKED_BY_RECIPIENT", 403), ("ASSET_DISABLED_BY_OWNER", 403), ("REVISION_CONFLICT", 409),
    ("DELIVERY_STATE_CONFLICT", 409), ("DELIVERY_NOT_FOUND", 404), ("DELIVERY_TARGET_NOT_FOUND", 404)])
def test_domain_error_codes_have_fixed_http_mapping(governed_host, wired_domains, monkeypatch, code, status):
    import sys

    from agent_py_agent.agent.plugin_host.composition import dispatch_call
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    issuer, source, *_ = governed_host
    ports, _ = wired_domains

    # LLM: 仅error_code可用于公开投影，原exception文字不是安全内容。
    # 函数用途: 提供WT1规定的结构化领域拒绝。
    def rejected(self, *args, **kwargs):
        failure = ValueError("private domain detail")
        failure.error_code = code
        raise failure

    monkeypatch.setattr(sys.modules["agent_py_agent.agent.assets.delivery_service"].AssetDeliveryService, "list_targets", rejected)
    authorized = issuer.authorize(source, decode_call(wire("assets.delivery.targets.list")))
    with pytest.raises(HostAPIError) as failure:
        dispatch_call(authorized, ports)
    assert (failure.value.code, failure.value.status) == (code, status)
    assert "private domain detail" not in str(failure.value)


# LLM: 真实Gateway组合、安装令牌和普通账号均取原fixture；不替换领域仓库或构造模型Agent。
# 函数用途: 为Stage3回归连接实际Host解析器、治理服务和投递事务。
@pytest.fixture
def stage3_host(governed_host):
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_host.gateway_composition import build_gateway_host_composition

    _, source, owner, *_ = governed_host
    server = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(my_agent_home=owner.root)), control_plane=None)
    composition = build_gateway_host_composition(server, None)
    try:
        yield composition, composition.issuer.ingress(source._lease), owner
    finally:
        composition.close()


# LLM: 必须经过正式decode/issuer/dispatch及原幂等账，不用直接领域调用替代Host入口验收。
# 函数用途: 对临时真实Host执行一个固定协议请求。
def stage3_call(host, action, payload=None, mode="read", as_user=None):
    from agent_py_agent.agent.plugin_host.composition import dispatch_call
    from agent_py_agent.agent.plugin_host.contracts import decode_call

    composition, source, _ = host
    call = decode_call(wire(action, payload, mode, as_user))
    return dispatch_call(composition.issuer.authorize(source, call), composition.ports)


@pytest.mark.parametrize("action,payload,field,value", [
    ("users.managed.policy.set", {"policy_patch": {"role_title": "合成角色"}}, "role_title", "合成角色"),
    ("users.managed.persona.set", {"user_md": "合成用户说明"}, "USER.md", "合成用户说明"),
])
def test_stage3_zero_revision_reaches_real_governance_write(stage3_host, action, payload, field, value):
    from agent_py_agent.agent.user_space.managed_user_governance import ManagedUserGovernanceService

    _, _, owner = stage3_host
    result = stage3_call(stage3_host, action, {"target_owner_id": "local/alice", "expected_revision": 0, **payload}, "write")
    assert result.http_status == 200 and result.data["revision"] == 1
    service = ManagedUserGovernanceService(owner.root)
    saved = (service.get_persona("local/alice") if action.endswith("persona.set")
             else service.get_overview("local/alice")["governance_policy"])
    assert saved[field] == value


def test_stage3_zero_revision_memory_add_schema():
    from agent_py_agent.agent.plugin_host.contracts import decode_call

    call = decode_call(wire("users.managed.memories.mutate", {
        "target_owner_id": "local/alice", "operation": "add_memory", "content": "合成事实", "expected_revision": 0}, "write"))
    assert call.payload["expected_revision"] == 0


@pytest.mark.parametrize("action,mode", [("assets.delivery.preview_bundle", "read"), ("assets.delivery.send", "write")])
def test_stage3_real_missing_closure_is_retryable_domain_rejection(stage3_host, action, mode):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.assets.workflow_store import (
        WorkflowDefinition,
        WorkflowStepSpec,
        WorkflowStore,
    )
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    _, _, owner = stage3_host
    WorkflowStore(owner.home_dir).save_workflow(WorkflowDefinition(AssetDescriptor("broken", "workflow", "缺依赖"),
        (WorkflowStepSpec("step", "步骤", "deterministic_script", script_id="absent"),)))
    payload = {"root_asset_kind": "workflow", "root_asset_id": "broken"}
    if mode == "write":
        payload["recipient_owner_id"] = "local/alice"
    for _ in range(2):
        with pytest.raises(HostAPIError) as failure:
            stage3_call(stage3_host, action, payload, mode)
        assert (failure.value.status, failure.value.code, failure.value.outcome) == (
            422, "ASSET_DELIVERY_DEPENDENCY_MISSING", "not_started")
        assert failure.value.to_payload()["details"]["missing_refs"] == ["script:absent"]
    ScriptStore(owner.home_dir).save_script(AssetDescriptor("absent", "script", "补齐依赖"), 'print("{}")')
    assert stage3_call(stage3_host, action, payload, mode).http_status == 200


@pytest.mark.parametrize("action", ["assets.delivery.inbox.list", "assets.delivery.send", "assets.delivery.accept"])
def test_stage3_public_delivery_as_user_uses_real_alice_context(stage3_host, action):
    from agent_py_agent.agent.assets.delivery_service import AssetDeliveryService
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    _, _, owner = stage3_host
    alice = resolve_owner_home(owner.root, OwnerIdentity.local_user("alice"))
    ScriptStore(owner.home_dir).save_script(AssetDescriptor("incoming", "script", "来包"), 'print("{}")')
    service = AssetDeliveryService(owner.root)
    sent = service.send_bundle("local/main", "local/alice", "script", "incoming")
    payload, mode = {}, "read"
    if action.endswith(".send"):
        ScriptStore(alice.home_dir).save_script(AssetDescriptor("outgoing", "script", "发件"), 'print("{}")')
        payload = {"recipient_owner_id": "local/main", "root_asset_kind": "script", "root_asset_id": "outgoing"}
        mode = "write"
    elif action.endswith(".accept"):
        payload, mode = {"delivery_id": sent["delivery_id"]}, "write"
    result = stage3_call(stage3_host, action, payload, mode, "alice")
    assert result.http_status == 200
    if action.endswith("inbox.list"):
        assert [row["delivery_id"] for row in result.data["deliveries"]] == [sent["delivery_id"]]
    elif action.endswith(".accept"):
        assert ScriptStore(alice.home_dir).get_script("incoming")["enabled"] is False
    else:
        inbox = service.list_inbox("local/main")["deliveries"]
        assert inbox[-1]["sender_owner_id"] == "local/alice"


@pytest.mark.parametrize("scope,uid", [("private", "alice"), ("public", "bobby")])
def test_stage3_delivery_as_user_rejected_by_authorization(tmp_path, scope, uid):
    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.plugin_host.authorization import IngressIssuer
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.plugin_manifest_host_v9 import host_capabilities
    from agent_py_agent.tests.test_plugin_host_v2_grants import active_grant

    _, _, entry, ref = active_grant(tmp_path, sorted(host_capabilities()), scope)
    api.set_host_api_base("http://127.0.0.1:8420")
    issuer = IngressIssuer()
    try:
        source = issuer.ingress(api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV])
        call = decode_call(wire("assets.delivery.inbox.list", as_user=uid))
        with pytest.raises(HostAPIError) as failure:
            issuer.authorize(source, call)
        assert failure.value.status == 403
    finally:
        issuer.close()
        api.set_host_api_base(None)


@pytest.mark.parametrize("action,payload,mode", [
    ("users.managed.persona.get", {"target_owner_id": "local/alice"}, "read"),
    ("governance.reviews.list", {"target_owner_id": "local/alice"}, "read"),
])
def test_stage3_governance_still_rejects_as_user_at_decode(action, payload, mode):
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    with pytest.raises(HostAPIError) as failure:
        decode_call(wire(action, payload, mode, "alice"))
    assert failure.value.code == "INVALID_SCHEMA"


@pytest.mark.parametrize("code,status", [
    ("ASSET_DELIVERY_DEPENDENCY_MISSING", 422), ("ASSET_DEPENDENCY_MISSING", 422),
    ("ASSET_DELIVERY_QUOTA_EXCEEDED", 429), ("ASSET_NOT_FOUND", 404), ("DELIVERY_SELF_TARGET_FORBIDDEN", 400),
    ("ASSET_CLOSURE_INVALID", 422), ("ASSET_BUNDLE_CORRUPTED", 422), ("ASSET_DELIVERY_PAYLOAD_INVALID", 400),
    ("PERSONA_LOCKED_BY_ADMIN", 403), ("PERSONA_CHANGE_PENDING_ADMIN_REVIEW", 409),
    ("GOVERNANCE_OPERATION_INCOMPLETE", 409), ("REVIEW_FEEDBACK_REQUIRED", 400), ("MEMORY_LOCKED_BY_ADMIN", 403),
    ("ASSET_DELIVERY_FUTURE_REJECTION", 422),
])
def test_stage3_deterministic_errors_clear_executing_receipt(stage3_host, monkeypatch, code, status):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDeliveryError
    from agent_py_agent.agent.assets.delivery_service import AssetDeliveryService
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    attempts = []

    def rejected(self, *args, **kwargs):
        attempts.append(True)
        raise AssetDeliveryError(code, "合成领域拒绝", missing_refs=["script:absent"])

    monkeypatch.setattr(AssetDeliveryService, "send_bundle", rejected)
    payload = {"recipient_owner_id": "local/alice", "root_asset_kind": "script", "root_asset_id": "fixture"}
    for _ in range(2):
        with pytest.raises(HostAPIError) as failure:
            stage3_call(stage3_host, "assets.delivery.send", payload, "write")
        expected = "ASSET_DELIVERY_DEPENDENCY_MISSING" if code == "ASSET_DEPENDENCY_MISSING" else code
        assert (failure.value.code, failure.value.status, failure.value.outcome) == (expected, status, "not_started")
        assert failure.value.details == {"missing_refs": ["script:absent"]}
        assert failure.value.to_payload()["details"] == failure.value.details
    assert attempts == [True, True]


# LLM: 不替换领域服务或仓库；真实空Store保持稳定404及not_started，不能降成unknown。
# 函数用途: 覆盖四类根预览、发送和启停缺失的HTTP错误合同。
@pytest.mark.parametrize('kind', ['script', 'workflow', 'skill', 'monitor'])
@pytest.mark.parametrize('action,mode', [('assets.delivery.preview_bundle', 'read'),
    ('assets.delivery.send', 'write'), ('assets.delivery.toggle_asset', 'write')])
def test_round2_real_missing_asset_maps_to_404(stage3_host, kind, action, mode):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    payload = {'root_asset_kind': kind, 'root_asset_id': 'absent'}
    if action.endswith('.toggle_asset'):
        payload = {'asset_kind': kind, 'asset_id': 'absent', 'enabled': False}
    elif action.endswith('.send'):
        payload['recipient_owner_id'] = 'local/alice'
    for _ in range(2):
        with pytest.raises(HostAPIError) as failure:
            stage3_call(stage3_host, action, payload, mode)
        assert (failure.value.status, failure.value.code, failure.value.outcome) == (404, 'ASSET_NOT_FOUND', 'not_started')


# LLM: 同一真实Gateway入口执行解析、身份解析、工具和仓库；只禁止无关活动任务路径，绝不替换投递服务。
# 函数用途: 验证生产控制入口冷启动可用，且scope身份不受metadata冒充影响。
def test_stage3_gateway_delivery_send_inbox_accept_real_entry(stage3_host, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import control_service
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    _, _, owner = stage3_host
    agent = SimpleNamespace(home_paths=home_paths_with_owner(home_paths(owner.root), owner))
    paths = gateway_paths_from_root(owner.root / "gateway")
    ScriptStore(owner.home_dir).save_script(AssetDescriptor("gateway-script", "script", "命令投递"), 'print("{}")')

    def no_live_turn(*args, **kwargs):
        pytest.fail("delivery fell through to active-turn controls")

    monkeypatch.setattr(control_service, "_active_control_target", no_live_turn)

    def run(text, identity):
        scope = control_service.GatewayControlScope("untrusted-display", "feishu", "fixture-session",
            metadata={"owner_id": "local/main", "user_id": "main"}, resolved_owner=identity)
        command = parse_conversation_control(text)
        assert command is not None and command.valid
        result = control_service.execute_gateway_conversation_control(agent, paths, command, scope)
        assert result.kind == "delivery" and result.ok, result
        return result.details

    main, alice = OwnerIdentity.local_main(), OwnerIdentity.local_user("alice")
    sent = run("/delivery send local/alice script:gateway-script 合成备注", main)
    inbox = run("/delivery inbox", alice)
    assert [item["delivery_id"] for item in inbox["deliveries"]] == [sent["delivery_id"]]
    accepted = run("/delivery accept " + sent["delivery_id"], alice)
    assert accepted["status"] == "accepted"
    target = resolve_owner_home(owner.root, alice)
    assert ScriptStore(target.home_dir).get_script("gateway-script")["enabled"] is False
    assert ScriptStore(owner.home_dir).get_script("gateway-script")["enabled"] is True


# LLM: 真实Host写入不能将源/目标不存在洗成unknown；修复缺失后原key必须可重试。
# 函数用途: 验证治理资产缺失的领域拒绝经过完整解析、鉴权和幂等门。
@pytest.mark.parametrize("action", ["users.managed.assets.provision", "users.managed.assets.toggle"])
@pytest.mark.parametrize("kind", ["script", "workflow", "skill", "monitor"])
def test_r2_governance_missing_asset_is_retryable_not_started(stage3_host, action, kind):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    payload = {"target_owner_id": "local/alice", "asset_kind": kind, "asset_id": "job_absent" if kind == "monitor" else "absent"}
    if action.endswith("toggle"):
        payload["enabled"] = True
    for _ in range(2):
        with pytest.raises(HostAPIError) as caught:
            stage3_call(stage3_host, action, payload, "write")
        assert (caught.value.code, caught.value.status, caught.value.outcome) == ("ASSET_NOT_FOUND", 404, "not_started")
