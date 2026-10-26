# LLM: 用临时真实安装、账号、原授权和领域存储测试C3；不读真实用户资料，不调用模型。
# 模块用途: 覆盖公共调用者、私有安装归属、通配授权、普通用户沙箱和鲜活撤权。
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_host_api as api
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation, plugin_catalog_digest
from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
from agent_py_agent.agent.plugin_environment_plan import plan_plugin_environment
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_permissions.grants import PermissionSelection, permission_details
from agent_py_agent.agent.plugin_permissions.host_grants import (
    default_selection,
    prepare_host_confirmation,
    validate_selection,
)
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_plugin_act_as_user import HostFixture
from agent_py_agent.tests.test_plugin_manifest_v8 import _bundle
from agent_py_agent.tests.test_plugin_manifest_v9 import v9_payload


# LLM: 令牌只供本例原安装使用，finally清空，不把虚构凭据留给其他用例。
# 函数用途: 隔离原宿主API的进程内令牌寿命。
@pytest.fixture(autouse=True)
def isolated_api():
    api.set_host_api_base("http://127.0.0.1:8420")
    yield
    api.set_host_api_base(None)


# LLM: 安装和prepare/activate均走唯一Store及原CAS；只省掉外部程序启动，不能冒称进程验收。
# 函数用途: 建立一个真实可撤销的v9安装及API令牌。
def active_install(root, identity=None, *, scope="private", caps=("read", "chat"), selection=None):
    identity = identity or OwnerIdentity.local_main()
    owner = (LocalUserStore(root).owner_home(identity.owner_id)
             if identity.provider == "local" and identity.owner_kind == "user"
             else resolve_owner_home(root, identity))
    store = PluginInstallStore(owner)
    package = inspect_plugin_package(_bundle(v9_payload(list(caps))))
    entry = store.install(PluginInstallRequest(package, "install-test", 0, scope)).installation
    plan = plan_plugin_environment(entry, "enable-test")
    os_grant = permission_details(entry, plan, PermissionSelection(), "restricted")
    grant = {**default_selection(tuple(caps)), **(selection or {}), "schema": "plugin_host_grant.v1",
             "grant_revision": 1, "installation_ref": entry.installation_ref,
             "package_sha256": entry.package_sha256,
             "activation_id": PluginActivation(plan, "preparing").activation_id}
    preparing = PluginActivation(plan, "preparing", permission_json=json.dumps({**os_grant, "host_api_grant": grant}))
    prepared = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, preparing)).installation
    active = replace(preparing, phase="active", catalog_sha256=plugin_catalog_digest(entry.manifest))
    entry = store.change_activation(PluginActivationRequest(plan.operation_id, prepared.revision, active)).installation
    ref = PluginActivationRef.from_owner(owner, entry.manifest.plugin_id, entry.activation_id)
    token = api.issue_host_api_env(ref, entry.manifest.plugin_id)[api.HOST_API_TOKEN_ENV]
    return owner, store, entry, token


# LLM: 账号物化使用原生命周期，不以手写registry伪装有效用户。
# 函数用途: 为隔离测试准备两个各有独立原home的普通账号。
def local_users(root):
    host = HostFixture(root)
    host.create_user("alice")
    host.create_user("bob")
    return host.factory.store


# LLM: CT-01公共程序包必须经真实安装表保存，不能伪装纯内容包。
# 函数用途: 核对管理员公共程序安装、激活和令牌往返。
def test_ct01_admin_can_publish_executable_plugin(tmp_path):
    owner, store, entry, token = active_install(tmp_path, scope="public")
    assert owner.identity == OwnerIdentity.local_main()
    assert not entry.manifest.is_content_only
    assert entry.installation_scope == "public" and entry.enabled
    assert store.snapshot() == (entry,)
    assert api.verify_host_api_token(token) is not None


# LLM: CT-06通配符仅针对三类UID集合，资源/工具通配不在本片扩展范围。
# 函数用途: 验证原授权schema与C投影共同保留显式星号。
@pytest.mark.parametrize("field", ["target_user_ids", "manageable_user_ids", "creatable_user_ids"])
def test_ct06_wildcard_survives_original_grant_validation(field):
    from agent_py_agent.agent.plugin_host.owner_context import supported_user_ids
    selection = default_selection(("read",))
    selection[field] = ["*"]
    try:
        actual = validate_selection(selection)
    except HostAPIError:
        actual = None
    assert actual is not None, "宿主三类UID授权应接受显式星号"
    assert actual[field] == ["*"]
    assert supported_user_ids(["*", "alice"]) == frozenset({"*", "alice"})


# LLM: 通配不会接受保留账号或路径输入，不能将星号逻辑变为UID免校验。
# 函数用途: 防止通配授权扩大用户编号语法。
@pytest.mark.parametrize("uid", ["main", "../alice", "Alice", "alice/bob"])
def test_wildcard_does_not_relax_uid_validation(uid):
    selected = {**default_selection(("read",)), "target_user_ids": [uid]}
    with pytest.raises(HostAPIError):
        validate_selection(selected)


# LLM: CT-06在原工厂解析一个未硬编码进目标名单的真实账号。
# 函数用途: 验证星号代用户范围仍要求账号enabled及原策略。
def test_ct06_factory_resolves_new_user_under_wildcard(tmp_path):
    host = HostFixture(tmp_path)
    host.create_user("bob")
    host.grant = replace(host.grant, allowed_target_users=frozenset({"*"}))
    try:
        resolved = host.factory.resolve_read_context(host.issue(as_user="bob"))
    except HostAPIError:
        resolved = None
    assert resolved is not None, "通配目标必须走真实普通账号工厂"
    assert resolved.identity == OwnerIdentity("local", "user", "bob")


# LLM: CT-02普通私有v9授权只允许本人、restricted和无治理范围。
# 函数用途: 证明原确认生成器能为普通用户生成合法受限授权。
def test_ct02_ordinary_private_host_confirmation(tmp_path):
    store = local_users(tmp_path)
    owner = store.owner_home("alice")
    entry = PluginInstallStore(owner).install(PluginInstallRequest(
        inspect_plugin_package(_bundle(v9_payload(["read"]))), "install-test", 0)).installation
    plan = plan_plugin_environment(entry, "enable-test")
    os_grant = permission_details(entry, plan, PermissionSelection(), "restricted")
    context = SimpleNamespace(owner=owner, arguments={})
    try:
        preview = prepare_host_confirmation(context, entry, plan, os_grant)
    except ValueError:
        preview = None
    assert preview is not None, "普通用户应能受限启用自己的私有v9插件"
    assert preview["mode"] == "restricted"
    assert preview["host_api_grant"]["approved_capabilities"] == ["read"]


# LLM: 直接复用Gateway生产组合根；不替换会话、观察或执行端口。
# 函数用途: 提供有原账号解析器的真实领域组合。
def composition_for(root):
    from agent_py_agent.agent.plugin_host.gateway_composition import build_gateway_host_composition
    server = SimpleNamespace(agent=None, control_plane=None)
    return build_gateway_host_composition(server, SimpleNamespace(state=root / "state.json"))


# LLM: 严格解码和正式授权/分发一并使用，主体只来自source，不从业务参数伪造。
# 函数用途: 执行一次确定性的Host v2调用。
def call_domain(composition, source, action, payload=None, *, key=None, as_user=None):
    from agent_py_agent.agent.plugin_host.contracts import (
        authorize_call,
        decode_call,
        dispatch_call,
    )
    row = {"schema": "plugin_host_call.v2", "action": action, "payload": payload or {}}
    if key is not None:
        row["idempotency_key"] = key
    if as_user is not None:
        row["as_user"] = {"user_id": as_user}
    authorized = authorize_call(source, decode_call(json.dumps(row).encode()))
    return authorized, dispatch_call(authorized, composition.ports)


# LLM: 先断言真实发行器承运接口存在；新增接口前失败于合同断言而不是TypeError。
# 函数用途: 为一个已由宿主核验的调用者登记真实安装来源。
def caller_source(issuer, token, owner):
    import inspect
    assert "caller_owner" in inspect.signature(issuer.ingress).parameters, "真实IngressIssuer必须承运宿主caller_owner"
    return issuer.ingress(token, caller_owner=owner)


# LLM: CT-01实际写原ConversationStore，随后交换准确thread_id验证直接访问不能绕开owner。
# 函数用途: 验证同一公共安装为两个普通用户提供隔离会话。
def test_ct01_public_callers_use_separate_original_stores(tmp_path):
    users = local_users(tmp_path)
    admin, _, _, token = active_install(tmp_path, scope="public", caps=("read", "chat", "users.manage", "act_as_user"))
    composition = composition_for(tmp_path)
    sources = {uid: caller_source(composition.issuer, token, users.owner_home(uid).identity) for uid in ("alice", "bob")}
    threads = {}
    for uid, source in sources.items():
        authorized, result = call_domain(composition, source, "conversations.threads.create", {"title": uid}, key="same_create_key_1")
        assert authorized.binding.actor == admin.identity
        assert authorized.effective_owner == users.owner_home(uid).identity
        assert not {"users.manage", "act_as_user"} & authorized.binding.capabilities
        threads[uid] = result.data["thread_id"]
        _, listed = call_domain(composition, source, "conversations.threads.list")
        assert [row["title"] for row in listed.data["items"]] == [uid]
    with pytest.raises(HostAPIError) as denied:
        call_domain(composition, sources["alice"], "conversations.messages.list", {"thread_id": threads["bob"]})
    assert denied.value.status in {403, 404}
    assert not (admin.home_dir / "conversations").exists()


# LLM: CT-02/03私有来源缺省调用者就是安装者，仍必须走原账号enabled/revision闸门。
# 函数用途: 验证普通私有v9可读本人会话，禁止其他人借用或传as_user。
def test_ct02_private_source_is_self_only(tmp_path):
    from agent_py_agent.agent.plugin_host.contracts import resolve_read_context
    users = local_users(tmp_path)
    owner, _, _, token = active_install(tmp_path, users.owner_home("alice").identity)
    composition = composition_for(tmp_path)
    try:
        authorized, result = call_domain(composition, composition.issuer.ingress(token), "conversations.threads.list")
    except HostAPIError:
        authorized = result = None
    assert authorized is not None, "普通私有安装须接原Host v2读取链"
    assert result.data["items"] == ()
    assert resolve_read_context(authorized).owner_home == owner
    source = caller_source(composition.issuer, token, users.owner_home("bob").identity)
    with pytest.raises(HostAPIError) as denied:
        call_domain(composition, source, "conversations.threads.list")
    assert denied.value.status == 403
    with pytest.raises(HostAPIError, match="CAPABILITY_DENIED"):
        call_domain(composition, composition.issuer.ingress(token), "conversations.threads.list", as_user="alice")


# LLM: CT-07/09真实公共调用即便安装获批治理也不能恢复普通调用者的治理/委托资格。
# 函数用途: 验证普通调用者不能管理账号、选择其他用户或进入管理员私有安装。
@pytest.mark.parametrize("attack", ["users", "delegate", "admin_private"])
def test_ct07_public_caller_never_becomes_admin(tmp_path, attack):
    users = local_users(tmp_path)
    scope = "private" if attack == "admin_private" else "public"
    _, _, _, token = active_install(tmp_path, scope=scope, caps=("read", "users.manage", "act_as_user"),
                                     selection={"target_user_ids": ["bob"]})
    composition = composition_for(tmp_path)
    source = caller_source(composition.issuer, token, users.owner_home("alice").identity)
    with pytest.raises(HostAPIError) as denied:
        if attack == "users":
            call_domain(composition, source, "users.local.list")
        else:
            call_domain(composition, source, "conversations.threads.list", as_user="bob" if attack == "delegate" else None)
    assert denied.value.status == 403


# LLM: CT-10持续使用同一已发行对象检查原账号禁用/修订与插件撤销，不能只检查新登录。
# 函数用途: 验证私有调用与公共调用都逐次经过鲜活撤权门。
@pytest.mark.parametrize("kind", ["private", "public"])
@pytest.mark.parametrize("change", ["disable", "revision", "activation"])
def test_ct10_original_authorized_call_is_revoked(tmp_path, kind, change):
    from agent_py_agent.agent.plugin_host.authorization import revalidate_call
    from agent_py_agent.agent.user_space.local_user_lifecycle import (
        LocalUserLifecycle,
        LocalUserOperation,
    )
    users = local_users(tmp_path)
    identity = users.owner_home("alice").identity
    _, store, entry, token = active_install(tmp_path, identity if kind == "private" else None, scope=kind)
    composition = composition_for(tmp_path)
    source = caller_source(composition.issuer, token, identity)
    authorized, _ = call_domain(composition, source, "conversations.threads.list")
    if change == "activation":
        store.change_activation(PluginActivationRequest("revoke-test", entry.revision, replace(entry.activation, phase="revoked")))
    else:
        lifecycle = LocalUserLifecycle(users)
        lifecycle.disable("alice", 1, LocalUserOperation("installation-one", "a" * 64, "disable_alice_test"))
        if change == "revision":
            lifecycle.enable("alice", 2, LocalUserOperation("installation-one", "a" * 64, "reenable_alice_test"))
    with pytest.raises(HostAPIError) as denied:
        revalidate_call(authorized)
    assert denied.value.status in {401, 403, 409, 410}


# LLM: CT-06目标通配仍由真正issuer和普通用户factory解析，不能只让schema接受星号。
# 函数用途: 验证管理员可以访问通配许可下的新账号。
def test_ct06_real_issuer_delegates_with_wildcard(tmp_path):
    users = local_users(tmp_path)
    _, _, _, token = active_install(tmp_path, scope="public", caps=("read", "act_as_user"), selection={"target_user_ids": ["*"]})
    composition = composition_for(tmp_path)
    try:
        authorized, _ = call_domain(composition, composition.issuer.ingress(token), "conversations.threads.list", as_user="bob")
    except HostAPIError:
        authorized = None
    assert authorized is not None, "真正发行器应将显式目标通配投影给C工厂"
    assert authorized.effective_owner == users.owner_home("bob").identity


# LLM: CT-06账号创建/读取经原LocalUsersDomain，不用直接生命周期调用冒充通配接口贯通。
# 函数用途: 验证获批三类星号的管理员能创建并管理未硬编码的新账号。
def test_ct06_wildcard_admin_creates_and_manages_new_account(tmp_path):
    _, _, _, token = active_install(tmp_path, scope="public", caps=("read", "users.manage", "act_as_user"), selection={
        "target_user_ids": ["*"], "manageable_user_ids": ["*"], "creatable_user_ids": ["*"]})
    composition = composition_for(tmp_path)
    source = composition.issuer.ingress(token)
    try:
        _, created = call_domain(composition, source, "users.local.create", {"user_id": "charlie"}, key="create_charlie_01")
    except HostAPIError:
        created = None
    assert created is not None, "原账号领域必须接收鲜活通配授权下的准确目标"
    assert created.data["user_id"] == "charlie"
    _, fetched = call_domain(composition, source, "users.local.get", {"user_id": "charlie"})
    assert fetched.data["status"] == "enabled"
    _, listing = call_domain(composition, source, "users.local.list")
    assert [row["user_id"] for row in listing.data["items"]] == ["charlie"]
    authorized, _ = call_domain(composition, source, "conversations.threads.list", as_user="charlie")
    assert authorized.effective_owner == OwnerIdentity("local", "user", "charlie")


# LLM: 使用原provider物化器种测试空间，不创建冒名local账号；真实业务读取仍走冷resolver。
# 函数用途: 为双来源策略测试建立已有飞书owner。
def provider_home(root):
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
    return ensure_owner_home(root, OwnerIdentity.provider_user("feishu", "ou_bob"))


# LLM: CT-01/06同名local UID不得干扰provider；quota缺revision或v1是旧owner有效格式，不许种第二空间。
# 函数用途: 验证provider公共调用沿原空间读取并按本人权限求交。
@pytest.mark.parametrize("quota_schema", ["quota.v1", "quota.v2"])
def test_ct01_provider_public_call_uses_original_space(tmp_path, quota_schema):
    home = provider_home(tmp_path)
    quota = json.loads(home.quota_json.read_text())
    quota["schema_version"] = quota_schema
    home.quota_json.write_text(json.dumps(quota))
    _, _, _, token = active_install(tmp_path, scope="public")
    composition = composition_for(tmp_path)
    source = caller_source(composition.issuer, token, home.identity)
    authorized, result = call_domain(composition, source, "conversations.threads.create", {"title": "飞书原空间"}, key="provider_thread_01")
    assert authorized.effective_owner == home.identity
    assert result.data["title"] == "飞书原空间"
    assert not (tmp_path / "owners/local/ou_bob").exists()
    assert not (tmp_path / "local_users/ou_bob.json").exists()


# LLM: provider策略必须交工具/写权限和有限插件配额，旧quota的0磁盘表示owner不限，不扩大插件上限。
# 函数用途: 验证两版provider配额都生成本人受限执行策略。
@pytest.mark.parametrize("schema", ["quota.v1", "quota.v2"])
def test_provider_execution_policy_intersects_current_documents(tmp_path, schema):
    from agent_py_agent.agent.plugin_host.execution_policy import (
        ExecutionAllowlist,
        build_execution_policy,
    )
    home = provider_home(tmp_path)
    home.quota_json.write_text(json.dumps({**json.loads(home.quota_json.read_text()), "schema_version": schema}))
    permissions = json.loads(home.permissions_json.read_text())
    permissions["filesystem"]["access_mode"] = "read-only"
    home.permissions_json.write_text(json.dumps(permissions))
    grant = replace(HostFixture(tmp_path).grant, execution_policy=ExecutionAllowlist(frozenset({"read_file", "write_file"})))
    try:
        policy = build_execution_policy(LocalUserStore(tmp_path), home.identity, grant)
    except HostAPIError:
        policy = None
    assert policy is not None, "provider不能被当作local UID或强制要求quota修订"
    assert policy.read_roots == (home.workspace_dir, home.tasks_dir)
    assert policy.write_roots == ()
    assert policy.allowed_tools == frozenset({"read_file"})
    assert policy.quota.max_disk_mb == grant.quota_ceiling.max_disk_mb


# LLM: 缺失provider不创建home，已禁用者不能仅凭活安装令牌继续读取。
# 函数用途: 覆盖飞书owner缺失、停用和目录替换的拒绝路径。
@pytest.mark.parametrize("condition", ["missing", "disabled", "symlink"])
def test_provider_status_is_cold_and_fails_closed(tmp_path, condition):
    identity = OwnerIdentity.provider_user("feishu", "ou_bob")
    if condition != "missing":
        home = provider_home(tmp_path)
        if condition == "disabled":
            (home.home_dir / "owner_status.json").write_text('{"status":"disabled"}')
        else:
            home.home_dir.rename(home.home_dir.with_name("original"))
            home.home_dir.symlink_to(home.home_dir.with_name("original"), target_is_directory=True)
    _, _, _, token = active_install(tmp_path, scope="public")
    composition = composition_for(tmp_path)
    source = caller_source(composition.issuer, token, identity)
    with pytest.raises(HostAPIError):
        call_domain(composition, source, "conversations.threads.list")
    assert not (tmp_path / "owners/local/ou_bob").exists()
    if condition == "missing":
        assert not resolve_owner_home(tmp_path, identity).home_dir.exists()


# LLM: 原会话验证器是唯一HTTP调用者来源，模拟的是认证依赖，不替换原安装或领域。
# 函数用途: 验证会话失效立即撤销已发行调用，不能回落main。
def test_ct10_session_revocation_invalidates_existing_source(tmp_path):
    import inspect

    from agent_py_agent.agent.plugin_host.authorization import IngressIssuer, revalidate_call
    users = local_users(tmp_path)
    _, _, _, token = active_install(tmp_path, scope="public")
    composition = composition_for(tmp_path)
    sessions = {"alice-session": users.owner_home("alice").identity}
    assert "session_verifier" in inspect.signature(IngressIssuer).parameters
    composition.issuer = IngressIssuer(composition.issuer._resolver, session_verifier=lambda root, session: sessions.get(session))
    source = composition.issuer.ingress(token, user_session_token="alice-session")
    authorized, _ = call_domain(composition, source, "conversations.threads.list")
    assert authorized.effective_owner == sessions["alice-session"]
    sessions.clear()
    with pytest.raises(HostAPIError) as denied:
        revalidate_call(authorized)
    assert denied.value.status == 401


# LLM: HTTP观察点是真实回环socket及原handler；只注入认证依赖，不用自报owner头当会话。
# 函数用途: 验证真实会话头把公共调用绑定本人，并保留未知会话和重复头的拒绝。
def test_real_http_session_header_binds_user(tmp_path):
    from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer
    from agent_py_agent.tests.test_plugin_host_v2_grants import http_request
    users = local_users(tmp_path)
    _, _, _, token = active_install(tmp_path, scope="public")
    server = GatewayHTTPServer(0, SimpleNamespace(state=tmp_path / "state.json"))
    server.plugin_host.composition.issuer._session_verifier = lambda root, session: (
        users.owner_home("alice").identity if session == "alice-session" else None)
    server.start()
    try:
        row = {"schema": "plugin_host_call.v2", "action": "conversations.threads.list", "payload": {}}
        status, result = http_request(server, token, row, {"X-Plugin-User-Session": "alice-session"})
        assert status == 200, result
        assert result["meta"]["effective_owner"] == {"provider": "local", "owner_kind": "user", "owner_id": "alice"}
        status, result = http_request(server, token, row, {"X-Plugin-User-Session": "unknown-session"})
        assert status == 401 and result["error"]["code"] == "USER_SESSION_INVALID"
        assert "effective_owner" not in result["meta"]
    finally:
        server.stop()


# LLM: 头校验早于正文和授权；重复、空值或含空白的认证头不得被HTTP库悄悄合并。
# 函数用途: 固定唯一ASCII会话头合同。
@pytest.mark.parametrize("values", [["alice", "bob"], [""], ["with space"], ["\u4e2d\u6587"]])
def test_session_header_is_single_nonempty_ascii_token(values):
    from email.message import Message

    from agent_py_agent.agent.plugin_host.http_transport import _validate_headers
    headers = Message()
    headers["X-Plugin-Host-Token"] = "fixture-install-token"
    for value in values:
        headers["X-Plugin-User-Session"] = value
    with pytest.raises(HostAPIError) as denied:
        _validate_headers(SimpleNamespace(client_address=("127.0.0.1", 1), headers=headers))
    assert denied.value.status == 401


# LLM: CT-03原确认器必须拒治理、委托、非私有和未受限模式，不因普通启用开放而放松。
# 函数用途: 核对普通用户确认的所有提权变异。
@pytest.mark.parametrize("attack", ["users.manage", "act_as_user", "target", "manageable", "creatable", "wide", "public"])
def test_ct03_private_confirmation_rejects_elevation(tmp_path, attack):
    users = local_users(tmp_path)
    owner = users.owner_home("alice")
    caps = ["read", "users.manage", "act_as_user"]
    entry = PluginInstallStore(owner).install(PluginInstallRequest(inspect_plugin_package(_bundle(v9_payload(caps))), "install-test", 0)).installation
    plan = plan_plugin_environment(entry, "enable-test")
    os_grant = permission_details(entry, plan, PermissionSelection(), "wide" if attack == "wide" else "restricted")
    selected = default_selection(("read",))
    if attack in {"users.manage", "act_as_user"}:
        selected["approved_capabilities"].append(attack)
    elif attack in {"target", "manageable", "creatable"}:
        selected[{"target": "target_user_ids", "manageable": "manageable_user_ids", "creatable": "creatable_user_ids"}[attack]] = ["*"]
    if attack == "public":
        entry = SimpleNamespace(installation_scope="public", manifest=entry.manifest)
    with pytest.raises(ValueError):
        prepare_host_confirmation(SimpleNamespace(owner=owner, arguments={"host_api_selection": selected}), entry, plan, os_grant)


# LLM: 原管理服务+原线程与RuntimeDB，普通owner本身来自宿主账号Store，不接受命令正文指定路径。
# 函数用途: 构造普通用户的插件管理入口。
def private_manager(root, uid="alice"):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy
    from agent_py_agent.agent.plugin_management import PluginManagement, PluginManagementContext
    owner = LocalUserStore(root).owner_home(uid)
    threads = ConversationStore(owner.home_dir / "conversations", initialize=False).threads
    context = PluginManagementContext(owner, uid, "local", "fixture-session", threads, owner.workspace_dir,
                                     PathAccessPolicy.from_values(mode="full"), False, True,
                                     legacy_sandbox_default=False)
    return PluginManagement(context)


# LLM: CT-08全过程安装/配置/更新/卸载用原HostCommand和安装表；程序启动另在CT-02测试，不混称为已启用。
# 函数用途: 验证普通用户能管理自己的私有安装，其他用户不可见或操作。
def test_ct08_private_lifecycle_management_and_cross_owner_denial(tmp_path):
    local_users(tmp_path)
    manager = private_manager(tmp_path)
    source = manager.context.owner.workspace_dir / "fixture.zip"
    source.write_bytes(_bundle(v9_payload(["read"])))
    result = manager.command(f'/plugins install "{source}"', revision=manager.catalog().revision, request_id="private-install")
    assert result["state"] == "succeeded", result
    plugin = manager.installations.snapshot()[0]
    config = source.with_name("settings.json")
    config.write_text("{}")
    configured = manager.command(f'/plugins configure {plugin.manifest.plugin_id} --file "{config}"',
        revision=manager.catalog().revision, request_id="private-configure")
    assert configured["state"] == "succeeded", configured
    updated = manager.command(f'/plugins update {plugin.manifest.plugin_id} "{source}"',
        revision=manager.catalog().revision, request_id="private-update")
    assert updated["state"] == "succeeded", updated
    other = private_manager(tmp_path, "bob")
    assert not other.catalog().plugins
    denied = other.command(f'/plugins remove {plugin.manifest.plugin_id}', revision=other.catalog().revision, request_id="other-remove")
    assert not denied["ok"]
    assert manager.installations.snapshot()
    removed = manager.command(f'/plugins remove {plugin.manifest.plugin_id}', revision=manager.catalog().revision, request_id="private-remove")
    assert removed["state"] == "succeeded", removed
    assert manager.installations.snapshot() == ()


# LLM: 公共条目只读发现来自管理员原Store，普通私有同名优先；普通人不得改公共安装。
# 函数用途: 核对公共可见性、管理拒绝及跨owner私有隐藏。
def test_public_catalog_visible_but_not_mutable_and_private_shadows(tmp_path):
    users = local_users(tmp_path)
    _, admin_store, public, _ = active_install(tmp_path, scope="public")
    manager = private_manager(tmp_path)
    assert [row.plugin_id for row in manager.catalog().plugins] == [public.manifest.plugin_id]
    for action in ("disable", "remove", "enable"):
        denied = manager.command(f'/plugins {action} {public.manifest.plugin_id}', revision=manager.catalog().revision,
                                 request_id="public-" + action)
        assert denied["error_code"] == "COMMAND_ADMIN_REQUIRED", denied
    assert admin_store.snapshot() == (public,)
    own = PluginInstallStore(users.owner_home("alice")).install(PluginInstallRequest(
        inspect_plugin_package(_bundle(v9_payload(["read"]))), "private-install", 0)).installation
    assert [row.installation_ref for row in manager.catalog().plugins] == [own.installation_ref]
    assert private_manager(tmp_path, "bob").catalog().plugins[0].installation_ref == public.installation_ref


# LLM: A5普通owner不能关闭进程沙箱或沿旧默认wide；构造授权仍不运行外部程序。
# 函数用途: 验证原启用工具即便全局两个沙箱开关都关也保持restricted。
def test_a5_private_enable_forces_restricted_sandbox(tmp_path):
    from agent_py_agent.agent.plugin_enable_tool import PluginEnablePolicy, PluginEnableTool
    from agent_py_agent.agent.runtime_db.host_commands import HostCommandIdentity
    users = local_users(tmp_path)
    owner = users.owner_home("alice")
    entry = PluginInstallStore(owner).install(PluginInstallRequest(inspect_plugin_package(_bundle(v9_payload(["read"]))), "install-test", 0)).installation
    identity = HostCommandIdentity(owner.owner_id, "alice", "local", "fixture-thread", "private-enable")
    policy = PluginEnablePolicy(process_sandbox=False, legacy_sandbox_default=False)
    tool = PluginEnableTool(owner, None, SimpleNamespace(request=identity), entry, "fixture-catalog", policy=policy)
    assert tool.process_sandbox is True
    assert tool.grant["mode"] == "restricted"
    assert tool.context.legacy_sandbox_default is True


# LLM: CT-01管理命令也必须承认公共程序包，不能只有直接Store构造可用。
# 函数用途: 验证唯一安装工具与原命令链不再按内容/程序拒公共范围。
def test_admin_install_command_accepts_public_program(tmp_path):
    from agent_py_agent.tests.test_plugin_management import manager as admin_manager
    manager, source = admin_manager(tmp_path)
    source.write_bytes(_bundle(v9_payload(["read"])))
    result = manager.command(f'/plugins install "{source}" --scope public',
        revision=manager.catalog().revision, request_id="public-program-install")
    assert result["state"] == "succeeded", result
    assert manager.installations.snapshot()[0].installation_scope == "public"
