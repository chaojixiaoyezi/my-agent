# LLM: 验证插件代普通用户执行合同，真实临时用户与工具门不能由字段断言替代。
# 模块用途: 防止插件继承管理员身份、跨用户访问及撤销后继续执行。
from __future__ import annotations

import pytest

from agent_py_agent.agent.auth.models import Role
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.owner_context import (
    restricted_permission,
    validate_local_user_id,
)


# LLM: 非法选择器在任何目录或代理创建之前拒绝，保留原输入而不清洗。
# 函数用途: 覆盖保留账户、路径注入、大小写及严格长度类型。
@pytest.mark.parametrize("uid", [
    "main", "admin", "root", "system", "default", "owner", "local",
    "local/main", "../alice", "Alice", "alice/bob", "alice\\bob",
    "alice.", " alice", "alice\n", "a", "a" * 33, "", "用户", None, 12, True,
])
def test_invalid_or_reserved_local_user_rejected(uid):
    with pytest.raises(HostAPIError) as caught:
        validate_local_user_id(uid)
    assert caught.value.code == "INVALID_SCHEMA"
    assert caught.value.outcome == "not_started"


# LLM: 本轮派工优先于旧设计的三字符限制，接受数字开头及二至三十二字符。
# 函数用途: 验证合法UID逐字返回且不扩大字符集合。
@pytest.mark.parametrize("uid", ["ab", "1a", "alice", "demo-user", "a_b", "a" * 32])
def test_valid_local_user_roundtrips(uid):
    assert validate_local_user_id(uid) == uid


# LLM: 固定受限权限不得读取基础配置或认证开关，任何调用都不得继承管理员。
# 函数用途: 锁住代用户主体的身份、跨用户和路径权限上限。
def test_restricted_permission_has_no_admin_inheritance():
    permission = restricted_permission()
    assert permission is not None
    assert permission.role is Role.USER
    assert permission.is_admin is False
    assert permission.can_access_all_users is False
    assert permission.access_mode == "workspace_only"


# LLM: 该fixture只替代A尚未合入的授权端口，来源必须是已登记对象，不能直接信任DTO形状。
# 类用途: 为C的真实领域和执行组件提供可撤销的宿主测试来源。
class HostFixture:
    # LLM: 账号和目录仍调用真实C实现，不模拟权限解析或文件行为。
    # 函数用途: 建立一个封闭的测试授权签发者。
    def __init__(self, root):
        from contextlib import nullcontext

        from agent_py_agent.agent.plugin_host.owner_context import (
            LocalUserGrant,
            OwnerContextFactory,
            PluginSourceBinding,
        )
        from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
        self.calls = {}
        self.grant = LocalUserGrant(
            PluginSourceBinding("installation-one", "c" * 64, "a" * 64, 1),
            frozenset({"execute", "act_as_user", "users.manage", "read"}),
            frozenset({"alice"}), frozenset({"alice", "bob"}), frozenset({"alice", "bob"}),
        )
        self.factory = OwnerContextFactory(LocalUserStore(root), self.verify, lambda source: nullcontext())

    # LLM: 真实上游端口必须验证自身活对象与持久grant；测试仅隔离这一外部依赖。
    # 函数用途: 拒绝拼装投影并返回当前源的权限版本。
    def verify(self, source):
        from dataclasses import replace
        if source not in self.calls:
            raise HostAPIError("SOURCE_INVALID", 403)
        return replace(self.calls[source], grant=self.grant)

    # LLM: source是不透明对象而不是字段正确就能伪造的JSON。
    # 函数用途: 生成一个测试宿主已核验的具体调用。
    def issue(self, action="chat.send", payload=None, as_user="alice", key=None):
        import json

        from agent_py_agent.agent.plugin_host.owner_context import AuthorizedLocalCall
        source = object()
        self.calls[source] = AuthorizedLocalCall(self.grant, action, json.dumps(payload or {}), as_user, key)
        return source

    # LLM: 调用真实生命周期种用户，不构造假的registry响应。
    # 函数用途: 为执行测试建立可核对的独立账号。
    def create_user(self, uid="alice"):
        from agent_py_agent.agent.user_space.local_user_lifecycle import (
            LocalUserLifecycle,
            LocalUserOperation,
        )
        op = LocalUserOperation("fixture-install", "a" * 64, "fixture_create_" + uid)
        return LocalUserLifecycle(self.factory.store).create(uid, op)

    # LLM: 测试fixture没有启动队列或agent，因此此依赖观察可精确返回零运行；磁盘仍未统计。
    # 函数用途: 为域测试提供明确的宿主用量观察。
    def usage(self, uid):
        from agent_py_agent.agent.plugin_host.local_users import UserRuntimeUsage
        return UserRuntimeUsage(0, 0, False)


# LLM: 没有目标账号时，context解析不得初始化owner或回落local/main。
# 函数用途: 验证冷读缺失用户返回错误且磁盘零变化。
def test_read_context_is_cold_and_missing_user_fails(tmp_path):
    host = HostFixture(tmp_path / "missing")
    with pytest.raises(HostAPIError, match="USER_NOT_FOUND"):
        host.factory.resolve_read_context(host.issue("settings.get"))
    assert not host.factory.store.root.exists()


# LLM: 真实registry和home路径构成观察点，不将permission标签本身当作完整隔离证据。
# 函数用途: 验证执行身份、全部owner路径和固定权限不继承主用户。
def test_context_is_registered_non_admin_owner(tmp_path):
    from dataclasses import fields
    from pathlib import Path

    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    host = HostFixture(tmp_path)
    host.create_user()
    context = host.factory.resolve_execution_context(host.issue())
    assert context is not None
    assert context.identity == OwnerIdentity("local", "user", "alice")
    assert context.permission == restricted_permission()
    assert context.user_revision == 1
    assert context.owner_home.home_dir == tmp_path / "owners/local/alice"
    assert context.execution_policy.allowed_tools == frozenset()
    for item in fields(context.owner_home):
        value = getattr(context.owner_home, item.name)
        if isinstance(value, Path) and item.name != "root":
            assert value.is_relative_to(context.owner_home.home_dir)


# LLM: 全局auth关闭或终端full-access不参与工厂，来源仍须原宿主核验口。
# 函数用途: 验证复制DTO和未装配权威端口都不能形成授权。
def test_forged_authorized_projection_and_missing_host_port_fail(tmp_path):
    from agent_py_agent.agent.plugin_host.owner_context import OwnerContextFactory
    host = HostFixture(tmp_path)
    source = host.issue()
    with pytest.raises(HostAPIError, match="SOURCE_INVALID"):
        host.factory.resolve_execution_context(host.calls[source])
    with pytest.raises(HostAPIError, match="HOST_NOT_READY"):
        OwnerContextFactory(host.factory.store).resolve_execution_context(source)


# LLM: 创建范围与执行范围独立，capability二者缺一均不执行。
# 函数用途: 验证目标白名单和双能力检查。
@pytest.mark.parametrize("condition", ["target", "execute", "act_as_user", "none"])
def test_context_rejects_unapproved_scope(tmp_path, condition):
    from dataclasses import replace
    host = HostFixture(tmp_path)
    host.create_user()
    if condition == "target":
        host.grant = replace(host.grant, allowed_target_users=frozenset())
    elif condition != "none":
        host.grant = replace(host.grant, capabilities=host.grant.capabilities - {condition})
    source = host.issue(as_user=None if condition == "none" else "alice")
    with pytest.raises(HostAPIError):
        host.factory.resolve_execution_context(source)


# LLM: context即使字段正确，复制后也不能冒充工厂签发的活对象。
# 函数用途: 验证对象身份、激活换代与用户revision撤销。
@pytest.mark.parametrize("change", ["copy", "activation", "grant", "disable_enable"])
def test_context_revalidation_rejects_stale_or_copied_context(tmp_path, change):
    from dataclasses import replace

    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    host = HostFixture(tmp_path)
    host.create_user()
    context = host.factory.resolve_execution_context(host.issue())
    assert context is not None
    if change == "copy":
        context = replace(context)
    elif change in {"activation", "grant"}:
        binding = host.grant.source_binding
        binding = replace(binding, activation_id="b" * 64) if change == "activation" else replace(binding, grant_revision=2)
        host.grant = replace(host.grant, source_binding=binding)
    else:
        service = LocalUserLifecycle(host.factory.store)
        service.disable("alice", 1, LocalUserOperation("fixture-install", "a" * 64, "fixture_disable_01"))
        service.enable("alice", 2, LocalUserOperation("fixture-install", "a" * 64, "fixture_enable_001"))
    with pytest.raises(HostAPIError):
        host.factory.check(context)


# LLM: 只替换宿主grant依赖，文件读取与快照、canonical executor全走真实实现。
# 函数用途: 建立批准了两个文件工具的普通用户invoker。
def file_invoker(root):
    from dataclasses import replace

    from agent_py_agent.agent.plugin_host.execution import RestrictedToolInvoker
    from agent_py_agent.agent.plugin_host.execution_policy import ExecutionAllowlist
    host = HostFixture(root)
    host.create_user()
    host.grant = replace(host.grant, execution_policy=ExecutionAllowlist(frozenset({"read_file", "write_file"})))
    context = host.factory.resolve_execution_context(host.issue(key="fixture_invoker_key01"))
    return host, RestrictedToolInvoker(host.factory, context)


# LLM: 正向观察真实文件内容，防止仅返回权限标签或空快照冒充实现。
# 函数用途: 验证目标用户可以通过原工具读取本人业务数据。
def test_restricted_invoker_reads_real_workspace(tmp_path):
    host, invoker = file_invoker(tmp_path)
    workspace = host.factory.store.owner_home("alice").workspace_dir
    (workspace / "owned.txt").write_text("alice-only-content")
    result = invoker.invoke("read_file", {"path": "owned.txt"})
    assert result is not None
    assert result.result.ok
    assert "alice-only-content" in str(result.result.to_dict())
    assert invoker.snapshot.available_tool_names == frozenset({"read_file", "write_file"})


# LLM: 即使白名单包含管理名，受限构造不注册它们；调用端再次拒绝别名、嵌套和任意进程。
# 函数用途: 验证所有高权限/包装入口不能从文件invoker借道恢复。
@pytest.mark.parametrize("tool", ["admin_controls", "manage_models", "runtime_governance", "package_install",
                                     "run_command", "tool_program", "create_subagents", "workflows", "read_file "])
def test_restricted_invoker_rejects_admin_and_wrapper_tools(tmp_path, tool):
    _, invoker = file_invoker(tmp_path)
    with pytest.raises(HostAPIError, match="TOOL_DENIED"):
        invoker.invoke(tool, {})


# LLM: 原业务根之外一律拒绝，不能利用owner墙比task根宽来读取账号配置或兄弟用户。
# 函数用途: 验证跨用户、控制文件和符号链接均不泄露正文。
@pytest.mark.parametrize("location", ["main", "bob", "quota", "registry", "symlink"])
def test_restricted_invoker_cannot_read_control_or_other_owners(tmp_path, location):
    host, invoker = file_invoker(tmp_path)
    home = host.factory.store.owner_home("alice")
    targets = {"main": tmp_path / "owners/local/main/secret.txt", "bob": tmp_path / "owners/local/bob/secret.txt",
               "quota": home.home_dir / "quota.json", "registry": tmp_path / "local_users/alice.json"}
    target = targets.get(location, tmp_path / "owners/local/main/secret.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    if location not in {"quota", "registry"}:
        target.write_text("never-return-this-secret")
    if location == "symlink":
        link = home.workspace_dir / "leak.txt"
        link.symlink_to(target)
        target = link
    result = invoker.invoke("read_file", {"path": str(target)})
    assert result is not None
    assert not result.result.ok
    assert "never-return-this-secret" not in str(result.result.to_dict())


# LLM: 内置schema必须在handler前校验，拒绝实参注入权限与未知字段。
# 函数用途: 验证execute.tool不是任意工具RPC。
def test_restricted_invoker_uses_exact_original_schema(tmp_path):
    _, invoker = file_invoker(tmp_path)
    result = invoker.invoke("read_file", {"path": "owned.txt", "is_admin": True})
    assert result is not None
    assert not result.result.ok
    assert result.result.handler_executed is False


# LLM: grant的准确资产版本是运行前置条件，同名换版不能继续旧许可。
# 函数用途: 验证脚本/工作流引用、修订和hash逐项绑定。
@pytest.mark.parametrize("change", ["none", "kind", "asset_id", "revision", "sha256"])
def test_asset_allowlist_requires_exact_version(tmp_path, change):
    from dataclasses import replace

    from agent_py_agent.agent.plugin_host.execution_policy import (
        AssetGrant,
        ExecutionAllowlist,
        require_asset,
    )
    asset = AssetGrant("script", "script-one", 1, "c" * 64)
    policy = ExecutionAllowlist(allowed_assets=(asset,))
    if change == "none":
        assert require_asset(policy, asset) == asset
    else:
        value = {"kind": "workflow", "asset_id": "script-two", "revision": 2, "sha256": "d" * 64}[change]
        with pytest.raises(HostAPIError, match="ASSET_DENIED"):
            require_asset(policy, replace(asset, **{change: value}))


# LLM: 停用后启用只允许新上下文，已有脚本/工作流步骤持旧revision不能复活。
# 函数用途: 验证独立fence与原invoker均阻止撤权后的下一步。
def test_each_step_rechecks_revocation(tmp_path):
    from agent_py_agent.agent.plugin_host.revocation import UserRevocationFence
    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    host, invoker = file_invoker(tmp_path)
    fence = UserRevocationFence(host.factory, invoker.context)
    service = LocalUserLifecycle(host.factory.store)
    service.disable("alice", 1, LocalUserOperation("fixture-install", "a" * 64, "fixture_disable_01"))
    with pytest.raises(HostAPIError, match="USER_DISABLED"):
        fence.check()
    with pytest.raises(HostAPIError, match="USER_DISABLED"):
        invoker.invoke("read_file", {"path": "owned.txt"})


# LLM: 注册enabled不能替代当前quota/权限可读，读取失败必须阻止构造而不是回退主默认。
# 函数用途: 验证坏策略和quota在任何执行器创建之前关闭。
@pytest.mark.parametrize("filename", ["quota.json", "permissions.json", "tool_policy.json", "skill_policy.json"])
def test_context_rejects_corrupt_current_owner_policy(tmp_path, filename):
    host = HostFixture(tmp_path)
    host.create_user()
    (host.factory.store.owner_home("alice").home_dir / filename).write_text("not-json")
    with pytest.raises(HostAPIError, match="STORE_UNAVAILABLE"):
        host.factory.resolve_execution_context(host.issue())


# LLM: 工具策略变更必须使已签发的执行实例退役，不能热改旧snapshot扩大或混用许可。
# 函数用途: 验证原策略内容变化触发撤权，并使新快照排除目标用户禁用的工具。
def test_policy_change_revokes_old_context_and_filters_new_snapshot(tmp_path):
    import json

    from agent_py_agent.agent.plugin_host.execution import RestrictedToolInvoker
    host, invoker = file_invoker(tmp_path)
    path = host.factory.store.owner_home("alice").home_dir / "tool_policy.json"
    policy = json.loads(path.read_text())
    policy["disabled_tools"] = ["read_file"]
    path.write_text(json.dumps(policy))
    with pytest.raises(HostAPIError, match="POLICY_CHANGED"):
        invoker.invoke("read_file", {"path": "owned.txt"})
    context = host.factory.resolve_execution_context(host.issue())
    fresh = RestrictedToolInvoker(host.factory, context)
    assert "read_file" not in fresh.snapshot.available_tool_names
    assert len(context.execution_policy.policy_digest) == 64


# LLM: 0子代理和0深度是有效禁用值，不能被原宽松positive_int恢复成50/4。
# 函数用途: 验证受限上下文保留零预算并固定无网络目的地。
def test_context_preserves_zero_budget_and_separate_cache_identity(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    host = HostFixture(tmp_path)
    LocalUserLifecycle(host.factory.store).create("alice", LocalUserOperation("fixture-install", "a" * 64, "fixture_create_zero"),
        quota={"max_active_agents": 1, "max_subagents": 0, "max_depth": 0, "max_disk_mb": 10})
    first = host.factory.resolve_execution_context(host.issue())
    assert getattr(first.execution_policy, "quota", None) is not None
    assert first.execution_policy.quota.max_subagents == 0
    assert first.execution_policy.quota.max_depth == 0
    assert first.execution_policy.network_destinations == ()
    binding = replace(host.grant.source_binding, installation_ref="second-install")
    host.grant = replace(host.grant, source_binding=binding)
    second = host.factory.resolve_execution_context(host.issue())
    assert first.cache_key != second.cache_key


# LLM: 使用真实LocalStore和ToolExecutor写入，审批读取口只替代尚未合入的宿主入口。
# 函数用途: 验证获准业务写入成功且控制文件写入零变化。
def test_write_uses_original_operation_store_and_exact_boundary(tmp_path):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.plugin_host.execution import ExecutionHostPorts, RestrictedToolInvoker
    host, original = file_invoker(tmp_path)
    home = original.context.owner_home
    ledger = LocalStore(home.home_dir / "data/tool-operations.db")
    invoker = RestrictedToolInvoker(host.factory, original.context, ExecutionHostPorts(ledger))
    result = invoker.invoke("write_file", {"path": "saved.txt", "content": "persisted as alice"})
    assert result.result.ok, result.result.to_dict()
    assert (home.workspace_dir / "saved.txt").read_text() == "persisted as alice"
    protected = home.home_dir / "quota.json"
    before = protected.read_bytes()
    denied = invoker.invoke("write_file", {"path": str(protected), "content": "forged"}, "step-2")
    assert not denied.result.ok
    assert protected.read_bytes() == before


# LLM: A签发投影也必须使用字节上限和严格JSON，不能通过多字节正文或重复字段扩展合同。
# 函数用途: 验证C领域参数严格解析且无需创建owner。
@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":"' + '中' * 24000 + '"}'], ids=["duplicate", "nonfinite", "utf8-limit"])
def test_payload_rejects_duplicates_nonfinite_and_oversized_utf8(tmp_path, raw):
    from dataclasses import replace
    host = HostFixture(tmp_path)
    source = host.issue()
    host.calls[source] = replace(host.calls[source], payload_json=raw)
    with pytest.raises(HostAPIError, match="INVALID_SCHEMA"):
        host.factory.verify(source)


# LLM: 候选runtime的存在不等于父快照已开放它，过滤必须保留原允许集合上限。
# 函数用途: 验证父快照禁用的工具不能被插件allowlist恢复。
def test_restricted_snapshot_intersects_original_availability(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.plugin_host.execution_policy import build_restricted_snapshot
    _, invoker = file_invoker(tmp_path)
    original = replace(invoker.snapshot, allowed_tools=frozenset())
    result = build_restricted_snapshot(original, invoker.context.execution_policy)
    assert result.available_tool_names == frozenset()


# LLM: 原通用池不得为已登记本地账号克隆main配置，缓存命中也须重核停用状态。
# 函数用途: 验证所有渠道进入原pool时均关闭非受限构造路径。
@pytest.mark.parametrize("disabled", [False, True])
def test_legacy_owner_pool_cannot_bypass_registered_user_gate(tmp_path, disabled):
    from types import SimpleNamespace

    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    host = HostFixture(tmp_path)
    host.create_user()
    if disabled:
        LocalUserLifecycle(host.factory.store).disable("alice", 1, LocalUserOperation("fixture-install", "a" * 64, "fixture_disable_pool"))
    pool = OwnerScopedAgentPool(SimpleNamespace(my_agent_home=str(tmp_path)), tmp_path / "unrelated")
    pool._hard_agents[("local", "user", "alice")] = SimpleNamespace(is_admin=True)
    with pytest.raises(HostAPIError, match="USER_DISABLED" if disabled else "HOST_NOT_READY"):
        pool.get(OwnerIdentity("local", "user", "alice"))


# LLM: 公开execute先验证完整schema及允许工具，队列依赖缺失不返回伪202。
# 函数用途: 验证工具submit正确映射原请求并在拒绝时不调用宿主队列。
def test_execution_submit_validates_then_delegates_to_original_queue(tmp_path):
    from agent_py_agent.agent.plugin_host.execution import (
        ExecutionAccepted,
        ExecutionDomain,
        ExecutionQueuePorts,
    )
    host, _ = file_invoker(tmp_path)
    seen = []
    # LLM: 只替代B未合入的原队列端口，C身份与schema仍全真。
    # 函数用途: 记录被提交的冻结计划并返回队列观察投影。
    def enqueue(context, plan, fence):
        fence.check()
        seen.append((context, plan))
        return ExecutionAccepted("request-one", "thread-one", False)
    domain = ExecutionDomain(host.factory, ExecutionQueuePorts(enqueue, lambda context, thread: None))
    payload = {"thread_id": "thread-one", "tool_name": "read_file", "arguments": {"path": "owned.txt"}}
    source = host.issue("execute.tool", payload, key="execute_key_00001")
    result = domain.submit(source)
    assert result is not None
    assert result.status == 202
    assert result.data == {"request_id": "request-one", "thread_id": "thread-one", "accepted": True, "state": "queued", "replayed": False}
    assert seen[0][0].identity.owner_id == "alice"
    assert seen[0][1].action == "execute.tool"
    assert seen[0][1].payload_json == host.calls[source].payload_json
    with pytest.raises(HostAPIError, match="HOST_NOT_READY"):
        ExecutionDomain(host.factory).submit(source)


# LLM: 入队前拒绝缺键、未知工具、权限注入及跨线程，不产生任何队列副作用。
# 函数用途: 覆盖execute闭合参数和原线程权限核验。
@pytest.mark.parametrize("case", ["key", "extra", "arguments", "tool", "thread"])
def test_execution_submit_denial_never_enqueues(tmp_path, case):
    from agent_py_agent.agent.plugin_host.execution import ExecutionDomain, ExecutionQueuePorts
    host, _ = file_invoker(tmp_path)
    seen = []
    payload = {"thread_id": "thread-one", "tool_name": "read_file", "arguments": {"path": "owned.txt"}}
    if case == "extra":
        payload["owner_home"] = "main"
    if case == "arguments":
        payload["arguments"]["is_admin"] = True
    if case == "tool":
        payload["tool_name"] = "run_command"
    # LLM: 模拟B的准确线程归属拒绝，不替代C的schema检查。
    # 函数用途: 确认线程边界失败先于队列提交。
    def thread_gate(context, thread):
        if case == "thread":
            raise HostAPIError("THREAD_NOT_FOUND", 404)
    ports = ExecutionQueuePorts(lambda context, plan, fence: seen.append(plan), thread_gate)
    source = host.issue("execute.tool", payload, key=None if case == "key" else "execute_key_00001")
    with pytest.raises(HostAPIError):
        ExecutionDomain(host.factory, ports).submit(source)
    assert seen == []


# LLM: 资产步骤检查发生在每个调用及审批回来的位置，固定版本不同即停止后续执行。
# 函数用途: 验证资产hash变化、停用和安装撤销分别使步骤栅栏失效。
@pytest.mark.parametrize("change", ["hash", "disable", "activation"])
def test_asset_step_fence_rechecks_current_source_and_content(tmp_path, change):
    from dataclasses import replace

    from agent_py_agent.agent.plugin_host.execution_policy import AssetGrant, ExecutionAllowlist
    from agent_py_agent.agent.plugin_host.revocation import AssetStepFence
    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    host = HostFixture(tmp_path)
    host.create_user()
    asset = AssetGrant("workflow", "workflow-one", 1, "c" * 64)
    host.grant = replace(host.grant, execution_policy=ExecutionAllowlist(allowed_assets=(asset,)))
    context = host.factory.resolve_execution_context(host.issue())
    observed = [asset]
    fence = AssetStepFence(host.factory, context, asset, lambda current, bound: observed[0])
    assert fence.check() == asset
    if change == "hash":
        observed[0] = replace(asset, sha256="d" * 64)
    elif change == "activation":
        host.grant = replace(host.grant, source_binding=replace(host.grant.source_binding, activation_id="e" * 64))
    else:
        LocalUserLifecycle(host.factory.store).disable("alice", 1, LocalUserOperation("fixture-install", "a" * 64, "fixture_disable_step"))
    with pytest.raises(HostAPIError):
        fence.check()


# LLM: 这里只替代原资产目录和B队列端口，不启动未装配的脚本沙箱或工作流引擎。
# 函数用途: 验证三种资产动作按准确引用入队，下一步骤会拒绝同名新内容。
@pytest.mark.parametrize("action", ["execute.script", "execute.workflow", "execute.workflow_resume"])
def test_execution_asset_plan_and_step_gate(tmp_path, action):
    from dataclasses import replace

    from agent_py_agent.agent.plugin_host.execution import (
        ExecutionAccepted,
        ExecutionDomain,
        ExecutionQueuePorts,
    )
    from agent_py_agent.agent.plugin_host.execution_policy import AssetGrant, ExecutionAllowlist
    host = HostFixture(tmp_path)
    host.create_user()
    kind = "script" if action == "execute.script" else "workflow"
    asset = AssetGrant(kind, kind + "-one", 3, "c" * 64)
    host.grant = replace(host.grant, execution_policy=ExecutionAllowlist(allowed_assets=(asset,)))
    payload = {"thread_id": "thread-one", "expected_revision": 3}
    payload["workflow_run_id" if action.endswith("resume") else kind + "_id"] = "run-one" if action.endswith("resume") else asset.asset_id
    if action.endswith("resume"):
        payload["from_step_id"] = "step-one"
    observed, submitted = [asset], []
    # LLM: 回执来自被替代的B端口，仅证明C的提交映射；不代表真实HTTP或资产执行已验证。
    # 函数用途: 保留冻结资产计划及每步栅栏供测试检查。
    def enqueue(context, plan, fence):
        submitted.append((plan, fence))
        return ExecutionAccepted("request-one", "thread-one", False)
    ports = ExecutionQueuePorts(enqueue, lambda context, thread: None, lambda context, selection: observed[0])
    result = ExecutionDomain(host.factory, ports).submit(host.issue(action, payload, key="execute_asset_key01"))
    assert result.status == 202
    assert submitted[0][0].asset == asset
    assert submitted[0][1].check() == asset
    observed[0] = replace(asset, sha256="d" * 64)
    with pytest.raises(HostAPIError, match="ASSET_CHANGED"):
        submitted[0][1].check()


# LLM: 未获准版本、缺原资产/断点或开放覆盖参数均不能受理；检查必须发生在队列前。
# 函数用途: 覆盖资产授权及封闭schema拒绝路径。
@pytest.mark.parametrize("case", ["revision", "hash", "identity", "extra", "inputs", "missing_backend", "checkpoint"])
def test_execution_assets_reject_before_enqueue(tmp_path, case):
    from dataclasses import replace

    from agent_py_agent.agent.plugin_host.execution import ExecutionDomain, ExecutionQueuePorts
    from agent_py_agent.agent.plugin_host.execution_policy import AssetGrant, ExecutionAllowlist
    host = HostFixture(tmp_path)
    host.create_user()
    asset = AssetGrant("workflow", "workflow-one", 3, "c" * 64)
    host.grant = replace(host.grant, execution_policy=ExecutionAllowlist(allowed_assets=(asset,)))
    payload = {"thread_id": "thread-one", "workflow_id": "workflow-one", "expected_revision": 3}
    current = asset
    if case == "revision":
        payload["expected_revision"] = 4
    elif case == "hash":
        current = replace(asset, sha256="d" * 64)
    elif case == "identity":
        payload["workflow_id"] = "workflow-other"
    elif case == "extra":
        payload["override_data_sources"] = {}
    elif case == "inputs":
        payload["inputs"] = []
    seen = []
    # LLM: 原运行/断点拒绝来自宿主资产读口，不能被C转为新建工作流。
    # 函数用途: 提供当前资产或原断点拒绝。
    def read_asset(context, selection):
        if case == "checkpoint":
            raise HostAPIError("CHECKPOINT_NOT_FOUND", 404)
        return current
    ports = ExecutionQueuePorts(lambda context, plan, fence: seen.append(plan), lambda context, thread: None,
                                None if case == "missing_backend" else read_asset)
    with pytest.raises(HostAPIError):
        ExecutionDomain(host.factory, ports).submit(host.issue("execute.workflow", payload, key="execute_asset_key01"))
    assert seen == []


# LLM: 两组各100个固定UID的合法并集可达200，分页仍只查询此有界集合。
# 函数用途: 验证创建与管理范围不相同时的list不会错误拒绝。
def test_local_user_list_supports_bounded_scope_union(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.plugin_host.local_users import LocalUsersDomain
    host = HostFixture(tmp_path)
    host.create_user("alice")
    managed = frozenset(["alice"] + [f"a{i:02}" for i in range(99)])
    created = frozenset(f"b{i:02}" for i in range(100))
    host.grant = replace(host.grant, manageable_user_ids=managed, creatable_user_ids=created)
    result = LocalUsersDomain(host.factory, host.usage).call(host.issue("users.local.list", as_user=None))
    assert [item["user_id"] for item in result.data["items"]] == ["alice"]


# LLM: 用户传入数组/对象不能在集合判断中泄出TypeError，必须安全拒绝。
# 函数用途: 验证列表筛选类型的闭合schema。
@pytest.mark.parametrize("status", [[], {}, 1])
def test_local_user_list_rejects_invalid_status_type(tmp_path, status):
    from agent_py_agent.agent.plugin_host.local_users import LocalUsersDomain
    host = HostFixture(tmp_path)
    with pytest.raises(HostAPIError, match="INVALID_SCHEMA"):
        LocalUsersDomain(host.factory, host.usage).call(host.issue("users.local.list", {"status": status}, as_user=None))


# LLM: 控制文件重复JSON字段属于损坏，不能让最后字段覆盖前项恢复enabled。
# 函数用途: 验证注册表拒绝重复键而不是沿宽松JSON解析器继续使用。
def test_local_user_registry_rejects_duplicate_control_fields(tmp_path):
    host = HostFixture(tmp_path)
    host.create_user()
    path = tmp_path / "local_users" / "alice.json"
    raw = path.read_text()
    path.write_text(raw.replace('"schema": "local-user.v1"', '"schema": "broken", "schema": "local-user.v1"'))
    with pytest.raises(HostAPIError, match="STORE_UNAVAILABLE"):
        host.factory.store.get("alice")


# LLM: 实测原LocalStore和append副作用，同一原请求重建不能创建新的写操作。
# 函数用途: 验证恢复invoker后稳定复用原操作而不是重复追加。
def test_rebuilt_invoker_replays_original_durable_operation(tmp_path):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.plugin_host.execution import ExecutionHostPorts, RestrictedToolInvoker
    host, initial = file_invoker(tmp_path)
    context = initial.context
    ports = ExecutionHostPorts(LocalStore(context.owner_home.home_dir / "data/replay.db"))
    args = {"path": "once.txt", "content": "one", "mode": "append"}
    first = RestrictedToolInvoker(host.factory, context, ports).invoke("write_file", args, "stable-step")
    assert first.result.ok, first.result.to_dict()
    recovered = host.factory.resolve_execution_context(context.source)
    second = RestrictedToolInvoker(host.factory, recovered, ports).invoke("write_file", args, "stable-step")
    assert second.result.ok, second.result.to_dict()
    assert (context.owner_home.workspace_dir / "once.txt").read_text() == "one"
    assert first.call.operation_id == second.call.operation_id


# LLM: 直接execute.tool的原工具名和参数是用户已授权意图，不能借上下文调换。
# 函数用途: 验证工具或实参变化均在handler前拒绝。
@pytest.mark.parametrize("changed", ["tool", "arguments"])
def test_direct_tool_invoker_rejects_changed_original_intent(tmp_path, changed):
    from agent_py_agent.agent.plugin_host.execution import RestrictedToolInvoker
    host, _ = file_invoker(tmp_path)
    payload = {"thread_id": "thread-one", "tool_name": "read_file", "arguments": {"path": "original.txt"}}
    source = host.issue("execute.tool", payload, key="direct_tool_key01")
    invoker = RestrictedToolInvoker(host.factory, host.factory.resolve_execution_context(source))
    name = "write_file" if changed == "tool" else "read_file"
    with pytest.raises(HostAPIError, match="CALL_MISMATCH"):
        invoker.invoke(name, {"path": "changed.txt"})


# LLM: 管理和读取调用即使capability充分，也不能被转作新的执行动作。
# 函数用途: 验证context解析的执行动作范围闭合。
@pytest.mark.parametrize("action", ["users.local.disable", "settings.get", "unknown"])
def test_context_rejects_non_execution_action(tmp_path, action):
    host = HostFixture(tmp_path)
    host.create_user()
    with pytest.raises(HostAPIError, match="ACTION_NOT_AVAILABLE"):
        host.factory.resolve_execution_context(host.issue(action))


# LLM: 没有原幂等键不允许随机生成持久写身份，避免重建后重复副作用。
# 函数用途: 验证缺失稳定原请求身份的写入会安全关闭。
def test_write_without_original_idempotency_key_is_rejected(tmp_path):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.plugin_host.execution import ExecutionHostPorts, RestrictedToolInvoker
    host, _ = file_invoker(tmp_path)
    context = host.factory.resolve_execution_context(host.issue())
    ports = ExecutionHostPorts(LocalStore(context.owner_home.home_dir / "data/missing-key.db"))
    with pytest.raises(HostAPIError, match="INVALID_SCHEMA"):
        RestrictedToolInvoker(host.factory, context, ports).invoke("write_file", {"path": "missing.txt", "content": "no"})
    assert not (context.owner_home.workspace_dir / "missing.txt").exists()


# LLM: 不用mock执行器，原持久账缺失必须阻止写handler进入。
# 函数用途: 核对独立审查提出的缺失操作账风险不会变成非耐久写。
def test_restricted_write_requires_real_operation_ledger(tmp_path):
    _, invoker = file_invoker(tmp_path)
    result = invoker.invoke("write_file", {"path": "unlogged.txt", "content": "no"})
    assert not result.result.ok
    assert result.result.handler_executed is False
    assert not (invoker.context.owner_home.workspace_dir / "unlogged.txt").exists()


# LLM: 审批读口处模拟用户停用，验证返回后真实提交门拒绝；这不替代A/B真实审批UI验证。
# 函数用途: 锁住审批端口返回与短写之间的撤销复查。
def test_revocation_during_approval_lookup_blocks_original_write(tmp_path):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.plugin_host.execution import ExecutionHostPorts, RestrictedToolInvoker
    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    host, original = file_invoker(tmp_path)
    # LLM: 此回调在用户锁外调用，停用提交完成后才返回审批结果。
    # 函数用途: 复现审批查询期间目标被停用的顺序。
    def approved(context, call):
        LocalUserLifecycle(host.factory.store).disable("alice", 1, LocalUserOperation("fixture-install", "a" * 64, "review_disable_01"))
        return []
    ports = ExecutionHostPorts(LocalStore(original.context.owner_home.home_dir / "data/revoke.db"), approved)
    invoker = RestrictedToolInvoker(host.factory, original.context, ports)
    with pytest.raises(HostAPIError, match="USER_DISABLED"):
        invoker.invoke("write_file", {"path": "revoked.txt", "content": "no"})
    assert not (original.context.owner_home.workspace_dir / "revoked.txt").exists()
