# LLM: 临时真实账号与原控制回执验证跨入口管理；密码仅为测试夹具，绝不连接生产用户。
# 模块用途: 检查用户管理解析、秘密脱敏、管理员权限与原 owner 存储兼容。
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.command_catalog import COMMAND_INDEX, sensitive_command_name
from agent_py_agent.agent.conversation.control_commands import (
    control_command_carries_secret,
    parse_conversation_control,
    persisted_control_command_text,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.local_user_lifecycle import (
    LocalUserLifecycle,
    LocalUserOperation,
)
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_admin_controls import (
    list_owner_home_paths,
    owner_home_paths_by_id,
)
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)


# LLM: 构造冷宿主路径，不启动模型、网络或生产配置。
# 函数用途: 为所有管理入口提供同一临时管理员。
@pytest.fixture
def admin(tmp_path):
    paths = home_paths_with_owner(home_paths(tmp_path), resolve_owner_home(tmp_path, OwnerIdentity.local_main()))
    paths.owner_home_dir.mkdir(parents=True)
    return SimpleNamespace(home_paths=paths)


# LLM: 原事务真实创建，操作对象仅代表内部调用来源，不替代入口权限检查。
# 函数用途: 为 owner 和拒绝测试准备已登记的普通账号。
def seed_user(root, uid="alice"):
    store = LocalUserStore(root)
    LocalUserLifecycle(store).create(uid, LocalUserOperation("test-users", "a" * 64, f"create_{uid}_00000001"))
    return store


# LLM: 别名和所有参数形状沿同一解析器检查，不从帮助文字推断执行事实。
# 函数用途: 验证六类用户命令及别名规范化。
@pytest.mark.parametrize("prefix", ["/users", "/user"])
@pytest.mark.parametrize("suffix,operation,name", [
    ("", "list", ""), ("list", "list", ""), ("info alice", "info", "alice"),
    ("get alice", "info", "alice"), ("show alice", "info", "alice"),
    ("create alice ExamplePass9 张 三", "create", "alice"),
    ("password alice ChangedPass8", "password", "alice"),
    ("passwd alice ChangedPass8", "password", "alice"),
    ("set-password alice ChangedPass8", "password", "alice"),
    ("reset-password alice ChangedPass8", "password", "alice"),
    ("enable alice", "enable", "alice"), ("disable alice 维护 暂停", "disable", "alice"),
])
def test_users_parsing(prefix, suffix, operation, name):
    command = parse_conversation_control(f"{prefix} {suffix}")
    assert command is not None
    assert (command.kind, command.operation, command.name, command.valid) == ("users", operation, name, True)


# LLM: 语法错误仍必须识别为系统控制，不落入模型；有密码操作保留敏感标记。
# 函数用途: 验证缺参、多参和非法动作明确给用法。
@pytest.mark.parametrize("suffix", ["info", "info alice extra", "create", "create alice", "password alice",
                                    "password alice ExamplePass9 extra", "enable alice extra", "disable", "unknown alice"])
def test_users_invalid_shape(suffix):
    command = parse_conversation_control(f"/users {suffix}")
    assert command is not None and command.kind == "users"
    assert not command.valid and "/users" in command.usage


# LLM: 明文只在活命令参数中；公开目录让 TUI/IM 入口也能提前避开历史持久化。
# 函数用途: 验证有效及无效密码命令的回执脱敏。
@pytest.mark.parametrize("text,expected", [
    ("/users create alice ExamplePass9 张 三", "/users create alice ****** 张 三"),
    ("/user passwd alice ExamplePass9", "/users password alice ******"),
    ("/users password alice ExamplePass9 extra", "/users ******"),
])
def test_users_secret_redaction(text, expected):
    command = parse_conversation_control(text)
    assert command is not None
    assert control_command_carries_secret(command)
    assert sensitive_command_name(text) == "users"
    assert persisted_control_command_text(command, text) == expected
    assert "ExamplePass9" not in persisted_control_command_text(command, text)


# LLM: 目录只暴露命令资格，插件的私有/公共资源权限仍归其服务检查。
# 函数用途: 确认普通插件入口开放而用户管理保持管理员专用。
def test_users_catalog_and_plugin_visibility():
    assert "users" in COMMAND_INDEX
    assert COMMAND_INDEX["users"].admin_only and COMMAND_INDEX["users"].sensitive_input
    assert COMMAND_INDEX["user"] is COMMAND_INDEX["users"]
    assert not COMMAND_INDEX["plugins"].admin_only


# LLM: local/users 只是别名，不能复用旧 providers/local/users 空间或获得 main 身份。
# 函数用途: 确认已有普通用户解析到原独立目录。
@pytest.mark.parametrize("ref", ["local/alice", "local/users/alice"])
def test_local_owner_resolution(admin, ref):
    seed_user(admin.home_paths.root)
    target = owner_home_paths_by_id(admin.home_paths, ref)
    assert target is not None
    assert target.owner_id == "local/alice"
    assert target.owner_home_dir == admin.home_paths.root / "owners/local/alice"
    assert target.owner_kind == "user"


# LLM: 只列规范真实目录与登记账号的去重并集；链接和隐藏目录不能引入别的空间。
# 函数用途: 检查排序、去重、分页和 provider 用户保留。
def test_local_owner_listing(admin):
    root = admin.home_paths.root
    seed_user(root)
    local = root / "owners/local"
    (local / "bob").mkdir()
    (local / ".hidden").mkdir()
    (local / "linked").symlink_to(local / "alice", target_is_directory=True)
    (root / "owners/providers/feishu/users/ou-a").mkdir(parents=True)
    owners, truncated = list_owner_home_paths(admin.home_paths, limit=20)
    assert [ref for ref, _ in owners] == ["local/main", "local/alice", "local/bob", "providers/feishu/users/ou-a"]
    assert not truncated
    short, more = list_owner_home_paths(admin.home_paths, limit=2)
    assert [ref for ref, _ in short] == ["local/main", "local/alice"] and more


# LLM: 无效 UID 不得经过 safe_path_segment 的宽松替换认领其它用户。
# 函数用途: 拒绝本地目录穿越、main 别名及链接。
@pytest.mark.parametrize("ref", ["local/users/main", "local/..", "local/a/b", "local/.hidden", "local/linked"])
def test_bad_local_owners_rejected(admin, ref):
    local = admin.home_paths.owner_home_dir.parent
    (local / "linked").symlink_to(admin.home_paths.owner_home_dir, target_is_directory=True)
    assert owner_home_paths_by_id(admin.home_paths, ref) is None


# LLM: 使用实际 Gateway/direct 适配函数，不以 mock 成功替代命令执行。
# 函数用途: 在同一临时用户根执行某条显式管理命令。
def run_users(agent, text, surface="direct", owner=None):
    from agent_py_agent.agent.gateway_parts.control_service import (
        GatewayControlScope,
        execute_gateway_conversation_control,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.chat_parts.control_runtime import (
        ChatControlExecution,
        ChatControlState,
        execute_chat_control,
    )
    command = parse_conversation_control(text)
    assert command is not None
    if surface != "direct":
        scope = GatewayControlScope("actor", "feishu" if surface == "im" else "chat", "session",
            resolved_owner=owner or OwnerIdentity.local_main())
        return execute_gateway_conversation_control(agent, gateway_paths_from_root(agent.home_paths.root / "gateway"), command, scope)
    state = ChatControlState(False, 0, "", 0, "session")
    return execute_chat_control(ChatControlExecution(agent, False, state), command)


# LLM: 六动作都以真实账号、原 hash 校验及修订读回为证，不接触生产账号。
# 函数用途: 验证 direct、Gateway 与 IM 分派使用同一账号和密码。
@pytest.mark.parametrize("surface", ["direct", "gateway", "im"])
def test_users_six_actions(admin, surface):
    from agent_py_agent.agent.session_lock.passwords import verify_password
    created = run_users(admin, "/users create alice ExamplePass9 爱丽丝", surface)
    assert created.ok, created
    assert created.details["user"]["has_password"] is True
    store = LocalUserStore(admin.home_paths.root)
    first = store.get("alice").revision
    record = store.read_record("alice")
    assert verify_password("ExamplePass9", record["user"]["password_hash"])
    listing = run_users(admin, "/users list", surface)
    assert listing.ok and [user["user_id"] for user in listing.details["users"]] == ["alice"]
    info = run_users(admin, "/user info alice", surface)
    assert info.ok and info.details["user"]["owner_id"] == "local/alice"
    changed = run_users(admin, "/users password alice ChangedPass8", surface)
    assert changed.ok and changed.details["user"]["revision"] > first
    assert verify_password("ChangedPass8", store.read_record("alice")["user"]["password_hash"])
    assert not verify_password("ExamplePass9", store.read_record("alice")["user"]["password_hash"])
    disabled = run_users(admin, "/users disable alice 维护", surface)
    assert disabled.ok and store.get("alice").status == "disabled"
    enabled = run_users(admin, "/users enable alice", surface)
    assert enabled.ok and store.get("alice").status == "enabled"
    for result in (created, listing, info, changed, disabled, enabled):
        public = json.dumps(result.to_dict(), ensure_ascii=False)
        assert "ExamplePass9" not in public and "ChangedPass8" not in public and "password_hash" not in public


# LLM: Gateway 基础对象即使属于管理员，权限也只取该请求已经解析的 owner。
# 函数用途: 防止普通用户借 Gateway 管理员宿主跨用户创建账号。
@pytest.mark.parametrize("surface", ["direct", "gateway", "im"])
def test_users_non_admin_rejected(admin, surface):
    if surface == "direct":
        user_home = home_paths_with_owner(admin.home_paths, LocalUserStore(admin.home_paths.root).owner_home("alice"))
        agent = SimpleNamespace(home_paths=user_home)
    else:
        agent = admin
    result = run_users(agent, "/users create bob ExamplePass9", surface, OwnerIdentity("local", "user", "alice"))
    assert not result.ok and result.error_code == "COMMAND_ADMIN_REQUIRED"
    assert LocalUserStore(admin.home_paths.root).get("bob") is None


# LLM: 输入策略须在创建生命周期产生目录或锁前验证，失败回执不能携带参数或 hash。
# 函数用途: 检查弱密码、非法 UID 及缺失用户安全拒绝。
@pytest.mark.parametrize("text", ["/users create bob weak", "/users create ../bob ExamplePass9",
                                  "/users password missing ExamplePass9", "/users info missing"])
def test_users_invalid_input_no_user(admin, text):
    result = run_users(admin, text)
    assert not result.ok
    assert LocalUserStore(admin.home_paths.root).get("bob") is None
    assert "ExamplePass9" not in json.dumps(result.to_dict())


# LLM: 调用真实工具实现并解析其结构化输出，不经过模型或网络。
# 函数用途: 返回管理员管理工具的一次执行结果。
def tool_call(admin, **params):
    from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
    outcome = AdminControlsTool(admin).execute(params)
    assert outcome.ok, outcome.output
    result = json.loads(outcome.output)
    assert "ExamplePass9" not in outcome.output and "ChangedPass8" not in outcome.output
    assert "password_hash" not in outcome.output
    return result


# LLM: 普通账号保持原 tool_policy 作为控制及资源授权唯一权威，兼容原动作和新名称。
# 函数用途: 验证管理员代理直接创建、改密、授权、停启和查询普通用户。
def test_admin_controls_local_user_management(admin):
    created = tool_call(admin, action="create_user", user_id="alice", password="ExamplePass9",
                        display_name="Alice", controls={"audit_allowed": False})
    assert created["owner_id"] == "local/alice" and created["controls"]["audit_allowed"] is False
    users = tool_call(admin, action="list_users")["users"]
    assert users[0]["user_id"] == "alice" and users[0]["controls"]["audit_allowed"] is False
    changed = tool_call(admin, action="set_user_password", owner_id="local/users/alice", password="ChangedPass8")
    assert changed["user"]["has_password"] is True and changed["user"]["revision"] > created["user"]["revision"]
    saved = tool_call(admin, action="set_controls", owner_id="alice", controls={"decision_model_allowed": False})
    assert saved["owner"]["decision_model_allowed"] is False
    grant = {"provider": "feishu", "resource_type": "sheet", "resource_token": "sheet_1", "operations": ["sheet_read"]}
    assert tool_call(admin, action="grant_resource", owner_id="alice", resource=grant)["resources"] == [grant]
    assert tool_call(admin, action="revoke_resource", owner_id="local/users/alice", resource=grant)["resources"] == []
    disabled = tool_call(admin, action="disable_user", user_id="alice", reason="maintenance")
    assert disabled["user"]["status"] == "disabled"
    enabled = tool_call(admin, action="enable_user", owner_id="alice")
    assert enabled["user"]["status"] == "enabled"
    assert tool_call(admin, action="get_user", user_id="alice")["user"]["status"] == "enabled"
    owners = tool_call(admin, action="list_owners")["owners"]
    assert owners[1]["local_user"]["user_id"] == "alice"


# LLM: 新动作仍执行原主代理身份门，不能通过工具参数或普通用户 home 绕过。
# 函数用途: 验证普通 owner 和子代理不能创建账号。
@pytest.mark.parametrize("subagent", [False, True])
def test_admin_tool_user_actions_permission(admin, subagent):
    from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
    if subagent:
        admin.tools = SimpleNamespace(owner_type="subagent")
    else:
        admin.home_paths = home_paths_with_owner(admin.home_paths, LocalUserStore(admin.home_paths.root).owner_home("alice"))
    outcome = AdminControlsTool(admin).execute({"action": "create_user", "user_id": "bob", "password": "ExamplePass9"})
    assert not outcome.ok and outcome.error_code == "ADMIN_CONTROL_DENIED"
    assert LocalUserStore(admin.home_paths.root).get("bob") is None


# LLM: 原 Gateway 收据负责稳定消息幂等；创建和改密明文只传入当次活命令。
# 函数用途: 读回真实控制收据，验证正文、结果和磁盘不保存明文。
@pytest.mark.parametrize("operation", ["create", "password"])
def test_gateway_receipts_redact_user_password(admin, operation):
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        execute_gateway_control_operation,
    )
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    if operation == "password":
        seed_user(admin.home_paths.root)
    text = f"/users {operation} alice ExamplePass9"
    scope = GatewayControlScope("actor", "chat", "session", metadata={"message_id": "users-fixture"},
                                resolved_owner=OwnerIdentity.local_main())
    paths = gateway_paths_from_root(admin.home_paths.root / "gateway")
    receipt = execute_gateway_control_operation(admin, paths, parse_conversation_control(text), scope, command_text=text)
    assert receipt.result["ok"], receipt.result
    assert receipt.command_text == f"/users {operation} alice ******"
    replay = execute_gateway_control_operation(admin, paths, parse_conversation_control(text), scope, command_text=text)
    assert replay.to_dict() == receipt.to_dict()
    for path in admin.home_paths.root.rglob("*.json"):
        assert "ExamplePass9" not in path.read_text()


# LLM: Gateway 序列化保留真实参数仅用于运输，不能只发无参 /users。
# 函数用途: 确认 TUI 往返命令语义不丢失。
def test_users_cli_command_roundtrip():
    from agent_py_agent.cli.chat_parts.control_runtime import _command_text
    command = parse_conversation_control("/user create alice ExamplePass9 Alice")
    assert parse_conversation_control(_command_text(command)) == command


# LLM: 旧策略可能已有 disabled 键；只允许 enable 清除，不能丢掉其它控制和资源授权。
# 函数用途: 验证重新启用同时清除管理员停用控制。
def test_enable_clears_disabled_admin_control(admin):
    seed_user(admin.home_paths.root)
    assert run_users(admin, "/users disable alice").ok
    path = admin.home_paths.root / "owners/local/alice/tool_policy.json"
    policy = json.loads(path.read_text())
    policy["admin_controls"] = {"schema": "owner_admin_controls.v1", "disabled": True, "audit_allowed": False}
    path.write_text(json.dumps(policy))
    assert run_users(admin, "/users enable alice").ok
    block = json.loads(path.read_text())["admin_controls"]
    assert block["disabled"] is False and block["audit_allowed"] is False


# LLM: 参数校验与权限拒绝不得先物化另一账号；错误只报告固定消息，不回显秘密。
# 函数用途: 验证工具输入的非法密码、配额、控制及互相矛盾的目标。
@pytest.mark.parametrize("overrides", [{"password": "weak"}, {"quotas": {"max_active_agents": True}},
    {"controls": {"cross_owner_audit_allowed": True}}, {"owner_id": "local/bob"}, {"user_id": "../bob"}])
def test_tool_invalid_create_has_no_side_effect(admin, overrides):
    from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
    outcome = AdminControlsTool(admin).execute({"action": "create_user", "user_id": "alice", "password": "ExamplePass9", **overrides})
    assert not outcome.ok and "ExamplePass9" not in outcome.output
    assert not (admin.home_paths.root / "local_users").exists()


# LLM: 新方法按声明签名传参，不以捕获异常重做操作；底层创建与密码保存仍为真实事务。
# 函数用途: 验证 c1 原生密码、actor、幂等键和 Gateway 根的接线。
def test_native_lifecycle_signature_contract(admin, monkeypatch):
    from agent_py_agent.agent.gateway_parts import users_control_service as service
    original = service.LocalUserLifecycle
    calls = []

    # LLM: 这是 c1 声明接口的适配替身，持久动作委托原类，不能据此宣称已跑真实 c1 组合。
    # 类用途: 记录新版生命周期接收的参数并执行基线原事务。
    class NativeLifecycle(original):
        # LLM: 仅保存 Gateway 根观察，不改变存储或权限。
        # 函数用途: 模拟新版构造器。
        def __init__(self, store, *, gateway_root=None):
            super().__init__(store)
            calls.append(("root", gateway_root))

        # LLM: 保留单次创建，不重试，也不把密码放入操作来源。
        # 函数用途: 验证创建参数送到声明的新接口。
        def create(self, uid, *, password=None, display_name="", quota=None, idempotency_key, actor_owner):
            calls.append(("create", actor_owner, idempotency_key))
            result = super().create(uid, LocalUserOperation("test-native", "a" * 64, idempotency_key), display_name=display_name, quota=quota)
            if password is not None:
                service.set_local_user_password(original(self.store), uid, password)
            return result

        # LLM: 原密码 helper 在不含原生方法的基类实例上执行，避免假接口自递归。
        # 函数用途: 验证新版改密参数和 CAS 修订。
        def set_password(self, uid, *, password, expected_revision, idempotency_key, actor_owner):
            assert expected_revision == self.store.get(uid).revision
            calls.append(("password", actor_owner, idempotency_key))
            service.set_local_user_password(original(self.store), uid, password)

    monkeypatch.setattr(service, "LocalUserLifecycle", NativeLifecycle)
    assert run_users(admin, "/users create alice ExamplePass9", "gateway").ok
    assert run_users(admin, "/users password alice ChangedPass8", "gateway").ok
    assert [entry[0] for entry in calls] == ["root", "create", "root", "password"]
    assert calls[0][1] == admin.home_paths.root / "gateway"
    assert calls[1][1] == calls[3][1] == OwnerIdentity.local_main()
    assert len(calls[1][2]) >= 16 and len(calls[3][2]) >= 16


# LLM: 原生生命周期撤销需要宿主真实 Gateway 根，不能从模型参数选择或始终传 None。
# 函数用途: 验证管理员工具也把宿主队列路径送到用户生命周期。
def test_admin_tool_forwards_gateway_root(admin, monkeypatch):
    from agent_py_agent.agent.gateway_parts import users_control_service as service
    original = service.LocalUserLifecycle
    observed = []
    admin.root = admin.home_paths.root / "admin-workspace"
    admin.config = SimpleNamespace(gateway_workspace="queue")

    # LLM: 仅替换新增构造器参数观察，其余账号操作均走原存储。
    # 类用途: 捕获原生服务获得的队列根。
    class NativeLifecycle(original):
        # LLM: 构造器不能物化目录或改变授权。
        # 函数用途: 保存 Gateway 路径并委托原生命周期。
        def __init__(self, store, *, gateway_root=None):
            super().__init__(store)
            observed.append(gateway_root)

    monkeypatch.setattr(service, "LocalUserLifecycle", NativeLifecycle)
    tool_call(admin, action="create_user", user_id="alice")
    assert observed == [admin.root / "queue"]


# LLM: 错误语法无法可靠分辨 UID 与密码位置，整个无效用户管理正文都必须保守抹除。
# 函数用途: 防止漏用户名、拼错动作和嵌套错误把密码放入回执。
@pytest.mark.parametrize("suffix", ["pasword alice ExamplePass9", "password ExamplePass9", "create ExamplePass9", "unknown ExamplePass9"])
@pytest.mark.parametrize("nested", [False, True])
def test_invalid_users_redacts_entire_command(suffix, nested):
    text = f"/users {suffix}"
    if nested:
        text = f"/sessions control thread-fixture {text}"
    command = parse_conversation_control(text)
    assert control_command_carries_secret(command)
    saved = persisted_control_command_text(command, text)
    assert "ExamplePass9" not in saved
    assert saved.endswith("/users ******")


# LLM: 检查真实拒绝收据，而非只测脱敏函数；无效输入不得创建普通账号。
# 函数用途: 验证 Gateway 拒绝错误用户命令仍不落密码明文。
@pytest.mark.parametrize("suffix", ["pasword alice ExamplePass9", "password ExamplePass9"])
def test_invalid_users_gateway_receipt_keeps_no_secret(admin, suffix):
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        execute_gateway_control_operation,
    )
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    text = f"/users {suffix}"
    paths = gateway_paths_from_root(admin.home_paths.root / "gateway")
    scope = GatewayControlScope("actor", "chat", "session", metadata={"message_id": "invalid-users-fixture"},
                                resolved_owner=OwnerIdentity.local_main())
    receipt = execute_gateway_control_operation(admin, paths, parse_conversation_control(text), scope, command_text=text)
    assert not receipt.result["ok"]
    assert "ExamplePass9" not in json.dumps(receipt.to_dict())


# LLM: 仅在已提交创建后的真实边界注入故障；必须保留账号、说明未确认阶段，并可不重复创建地补完。
# 函数用途: 验证改密或控制写失败后的部分创建回执与安全恢复。
@pytest.mark.parametrize("surface,stage", [("direct", "password"), ("gateway", "password"),
                                           ("tool", "password"), ("tool", "controls")])
def test_partial_create_reports_stage_and_recovers(admin, monkeypatch, surface, stage):
    from agent_py_agent.agent.gateway_parts import users_control_service as service
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.session_lock.passwords import verify_password
    from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool

    # LLM: 只替换创建之后的密码或控制步骤；真实创建、读取与后续修复均不替换。
    # 函数用途: 模拟并发修订冲突或策略持久化失败。
    def fail_after_create(*args, **kwargs):
        if stage == "password":
            raise HostAPIError("REVISION_CONFLICT", 409)
        raise OSError("ExamplePass9")

    with monkeypatch.context() as patch:
        boundary = "set_local_user_password" if stage == "password" else "set_owner_admin_controls"
        patch.setattr(service, boundary, fail_after_create)
        if surface == "tool":
            result = AdminControlsTool(admin).execute({"action": "create_user", "user_id": "alice",
                "password": "ExamplePass9", "controls": {"audit_allowed": False}})
        else:
            result = run_users(admin, "/users create alice ExamplePass9", surface)
    assert not result.ok
    if surface == "tool":
        assert result.error_code == "TOOL_PERSISTENCE_FAILED"
        assert result.reported_error_code == "USER_INITIALIZATION_INCOMPLETE"
    else:
        assert result.error_code == "USER_INITIALIZATION_INCOMPLETE"
    details = json.loads(result.output)["details"] if surface == "tool" else result.details
    assert details["account_created"] is True and details["owner_id"] == "local/alice"
    assert details["initialization_stage"] == stage and details["stage_outcome"] == "unconfirmed"
    expected = ["set_user_password"] if stage == "password" else []
    if surface == "tool":
        expected.append("set_controls")
    assert details["pending_actions"] == expected
    public = result.output if surface == "tool" else json.dumps(result.to_dict())
    assert "ExamplePass9" not in public and "password_hash" not in public
    store = LocalUserStore(admin.home_paths.root)
    original = store.get("alice")
    assert original is not None
    if "set_user_password" in expected:
        assert run_users(admin, "/users password alice ChangedPass8").ok
        assert verify_password("ChangedPass8", store.read_record("alice")["user"]["password_hash"])
    if "set_controls" in expected:
        fixed = tool_call(admin, action="set_controls", owner_id="alice", controls={"audit_allowed": False})
        assert fixed["owner"]["audit_allowed"] is False
    assert store.get("alice").created_at == original.created_at
    assert [row["user_id"] for row in tool_call(admin, action="list_users")["users"]] == ["alice"]
