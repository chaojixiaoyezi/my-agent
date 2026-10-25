# LLM: 仅在临时根使用合成密码、原账号事务和原飞书SQLite，失败登录不得物化用户；不连接真实平台。
# 模块用途: 验证规范用户身份、密码管理、双来源认证及插件领域入口的安全合同。
from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.session_lock.passwords import hash_password, verify_password
from agent_py_agent.agent.session_lock.service import UnlockService
from agent_py_agent.agent.session_lock.store import SessionLockStore
from agent_py_agent.agent.user_space.local_user_lifecycle import (
    LocalUserLifecycle,
    LocalUserOperation,
)
from agent_py_agent.agent.user_space.local_user_store import LocalUser, LocalUserStore
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

PASSWORD = "SyntheticOnly42!"
NEW_PASSWORD = "ChangedSynthetic43!"


# LLM: 内部操作标识不是授权；真实插件入口另外使用原HostFixture核验来源。
# 函数用途: 为临时用户事务生成固定合法幂等标识。
def operation(key="auth_create_alice_01"):
    return LocalUserOperation("auth-test-install", "a" * 64, key)


# LLM: 首个断言证明旧接口缺少密码合同，后续断言必须走真实创建与物化。
# 函数用途: 在临时根建立带密码的普通用户。
def create_user(root, uid="alice", password=PASSWORD):
    service = LocalUserLifecycle(LocalUserStore(root))
    assert "password" in inspect.signature(service.create).parameters
    return service.create(uid, operation("auth_create_" + uid + "_01"), password=password)


# LLM: 模块不存在时以明确断言报红，不让ImportError掩盖验收行为。
# 函数用途: 读取本片新增的统一认证服务。
def auth_module():
    name = "agent_py_agent.agent.user_space.user_auth"
    assert importlib.util.find_spec(name) is not None, "统一认证服务尚未实现"
    return importlib.import_module(name)


# LLM: 模拟IM既有权威事实，不另建本地账号；数据路径与原home_layout/session_lock保持一致。
# 函数用途: 建立已接入飞书用户及真实首设密码库。
def feishu_user(root, uid="ou_first", *, display="同名", bound=None, status="active", tenant=""):
    identity = bound or OwnerIdentity.provider_user("feishu", uid)
    home = resolve_owner_home(root, identity)
    home.home_dir.mkdir(parents=True, exist_ok=True)
    (home.workspace_dir).mkdir(exist_ok=True)
    (home.workspace_dir / "original.txt").write_text(uid)
    row = {"schema_version": "provider-identity.v1", "provider": identity.provider,
           "provider_subject_id": uid, "owner_kind": identity.owner_kind,
           "owner_id": identity.owner_id, "display_name": display, "status": status}
    if tenant:
        row["tenant_or_app_key"] = tenant
    index = root / "identity/provider_identity/feishu.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    with index.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    store = SessionLockStore(root / "session_lock/private_chat_locks.db")
    store.set_password_hash(uid, hash_password(PASSWORD))
    module = auth_module()
    if hasattr(module, "bind_feishu_auth_service"):
        module.bind_feishu_auth_service(root, UnlockService(store))
    return home, store, row


# LLM: 路径解析必须冷且与LocalUserStore完全一致，不能物化第二份provider/local目录。
# 函数用途: 验证普通本地身份、管理员保留编号和规范owner路径。
def test_canonical_local_identity_and_store_agree_without_writes(tmp_path):
    assert callable(getattr(OwnerIdentity, "local_user", None))
    owner = OwnerIdentity.local_user("alice")
    result = resolve_owner_home(tmp_path, owner)
    assert result.owner_id == "local/alice"
    assert result.home_dir == tmp_path / "owners/local/alice"
    assert result == LocalUserStore(tmp_path).owner_home("alice")
    with pytest.raises(ValueError, match="reserved"):
        OwnerIdentity.local_user("main")
    with pytest.raises(ValueError, match="reserved"):
        resolve_owner_home(tmp_path, OwnerIdentity("local", "user", "main"))
    assert list(tmp_path.iterdir()) == []


# LLM: 冷home必须同时核对owner字符串和实际目录，不能只认调用者自报UID。
# 函数用途: 验证管理来源正确识别本地普通用户并拒绝路径调包。
def test_cold_home_identity_accepts_only_matching_local_home(tmp_path):
    from agent_py_agent.agent.gateway_parts.owner_management_control import cold_home_identity

    home = SimpleNamespace(root=tmp_path, owner_id="local/alice", owner_provider="local",
                           owner_kind="user", owner_home_dir=tmp_path / "owners/local/alice")
    assert cold_home_identity(home) == OwnerIdentity("local", "user", "alice")
    home.owner_home_dir = tmp_path / "owners/local/main"
    with pytest.raises(ValueError):
        cold_home_identity(home)


# LLM: 覆盖没有providers目录以及快照缓存已命中后新增local账号的两种入口。
# 函数用途: 证明本地普通用户参与真实分页发现，隐藏项、链接和main不入表。
def test_local_wake_discovery_invalidates_snapshot_and_skips_nonowners(tmp_path):
    from agent_py_agent.agent.owner_wake_discovery import discover_owner_home_page

    local = tmp_path / "owners/local"
    for uid in ("alice", "main", ".hidden"):
        (local / uid).mkdir(parents=True)
    (local / "not-dir").write_text("")
    (local / "linked").symlink_to(local / "alice", target_is_directory=True)
    first = discover_owner_home_page(tmp_path / "owners", limit=1)
    assert [target.identity for target in first.targets] == [OwnerIdentity("local", "user", "alice")]
    (local / "bob").mkdir()
    second = discover_owner_home_page(tmp_path / "owners", limit=1)
    assert second.next_cursor is not None
    last = discover_owner_home_page(tmp_path / "owners", limit=1, after_cursor=second.next_cursor)
    assert [target.identity.owner_id for target in last.targets] == ["bob"]
    assert last.next_cursor is None


# LLM: 原始序列化只供私有账号存储，公开投影及repr不含密码散列。
# 函数用途: 验证新旧账号字段往返、权限和公开秘密隔离。
def test_password_private_storage_and_public_projection(tmp_path):
    result = create_user(tmp_path)
    user = result.user
    assert verify_password(PASSWORD, user.password_hash)
    assert callable(getattr(LocalUser, "from_dict", None))
    assert LocalUser.from_dict(user.to_dict()) == user
    public = user.public_view()
    assert "password_hash" not in public
    assert public["has_password"] is True
    assert public["password_updated_at"]
    assert PASSWORD not in str(public) and user.password_hash not in repr(user)
    path = tmp_path / "local_users/alice.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    assert PASSWORD not in raw and user.password_hash in raw
    assert LocalUserStore(tmp_path).list_users() == [user]


# LLM: 密码策略失败须先于任何锁、注册或home创建，类型也不可宽松强转。
# 函数用途: 验证非法密码无磁盘副作用。
@pytest.mark.parametrize("password", ["short", "nouppercase42", 123, True, [], {}])
def test_invalid_creation_password_has_no_side_effect(tmp_path, password):
    service = LocalUserLifecycle(LocalUserStore(tmp_path))
    assert "password" in inspect.signature(service.create).parameters
    with pytest.raises(HostAPIError, match="INVALID_PASSWORD_POLICY"):
        service.create("alice", operation(), password=password)
    assert list(tmp_path.iterdir()) == []


# LLM: 同密码重试不得因随机盐冲突，修改密码必须在原意图摘要层拒绝。
# 函数用途: 验证创建密码幂等且操作索引没有明文。
def test_create_password_replay_binds_secret_without_plaintext(tmp_path):
    created = create_user(tmp_path)
    replayed = create_user(tmp_path)
    assert replayed.replayed and replayed.user == created.user
    with pytest.raises(HostAPIError, match="IDEMPOTENCY_CONFLICT"):
        create_user(tmp_path, password=NEW_PASSWORD)
    for path in (tmp_path / "local_users/operations").glob("*.json"):
        assert PASSWORD not in path.read_text() and NEW_PASSWORD not in path.read_text()


# LLM: 重放先于CAS且密码修改不重置账号其余字段，CAS类型拒绝bool。
# 函数用途: 验证设密更新、旧密码失效和准确幂等回执。
def test_set_password_revision_replay_and_optional_status_cas(tmp_path):
    create_user(tmp_path)
    service = LocalUserLifecycle(LocalUserStore(tmp_path))
    assert callable(getattr(service, "set_password", None))
    kwargs = {"password": NEW_PASSWORD, "expected_revision": 1, "idempotency_key": "password_set_alice_01"}
    view, replay = service.set_password("alice", **kwargs)
    assert view["revision"] == 2 and not replay and "password_hash" not in view
    assert service.set_password("alice", **kwargs)[1] is True
    assert service.verify_user_password("alice", NEW_PASSWORD).revision == 2
    with pytest.raises(HostAPIError, match="INVALID_CREDENTIALS"):
        service.verify_user_password("alice", PASSWORD)
    for revision in (True, "2", 1):
        with pytest.raises(HostAPIError, match="REVISION_CONFLICT"):
            service.set_password("alice", password=PASSWORD, expected_revision=revision,
                                 idempotency_key="password_bad_revision_" + str(revision).lower())
    stopped = service.disable("alice", operation=operation("auth_disable_alice01"))
    assert stopped.user.status == "disabled" and stopped.user.revision == 3
    with pytest.raises(HostAPIError, match="USER_DISABLED"):
        service.verify_user_password("alice", NEW_PASSWORD)
    assert service.enable("alice", operation=operation("auth_enable_alice_01")).user.revision == 4


# LLM: 完整认证服务用真实密码散列和磁盘状态，两个独立账号不共用owner或会话。
# 函数用途: 验证本地登录、公开字段、签名回读、期限与跨根拒绝。
def test_local_login_signed_session_expiry_and_root_isolation(tmp_path):
    module = auth_module()
    create_user(tmp_path)
    create_user(tmp_path, "bob")
    alice = module.authenticate_user(tmp_path, account_source="local", user_id="local/alice", password=PASSWORD, ttl_seconds=60)
    bob = module.authenticate_user(tmp_path, account_source="local", user_id="bob", password=PASSWORD)
    assert alice is not None and alice.effective_owner == OwnerIdentity.local_user("alice")
    assert alice.owner_id == "local/alice" and bob.owner_id == "local/bob"
    assert alice.principal_id != bob.principal_id and alice.session_token != bob.session_token
    assert module.verify_user_session(tmp_path, alice.session_token, now=alice.issued_at) == alice
    public = alice.public_view()
    assert set(public) == {"session_token", "principal_id", "account_source", "owner_id", "display_name", "revision", "issued_at", "expires_at"}
    assert PASSWORD not in str(public) and "password_hash" not in public
    assert module.verify_user_session(tmp_path, alice.session_token).owner_id == "local/alice"
    with pytest.raises(HostAPIError, match="SESSION_EXPIRED"):
        module.verify_user_session(tmp_path, alice.session_token, now=alice.expires_at)
    with pytest.raises(HostAPIError):
        module.verify_user_session(tmp_path / "unknown-root", alice.session_token)
    assert not (tmp_path / "unknown-root").exists()
    secret = tmp_path / "local_users/.auth_session_secret"
    assert secret.stat().st_size == 32 and stat.S_IMODE(secret.stat().st_mode) == 0o600


# LLM: 冷失败登录不得生成密钥、用户、密码库、锁或第二套owner目录。
# 函数用途: 验证未知账号、非法输入及未知飞书用户的无副作用拒绝。
@pytest.mark.parametrize("source,uid,password", [("local", "missing", PASSWORD), ("feishu", "ou_unknown", PASSWORD),
                                               ("wrong", "alice", PASSWORD), ("local", "../alice", PASSWORD)])
def test_failed_login_does_not_create_anything(tmp_path, source, uid, password):
    module = auth_module()
    with pytest.raises(HostAPIError):
        module.authenticate_user(tmp_path, account_source=source, user_id=uid, password=password)
    assert list(tmp_path.iterdir()) == []


# LLM: HMAC验签须先于凭据和owner解析，原签名搭配改payload或签名均必须拒绝。
# 函数用途: 验证损坏、超长及伪造会话不被当作有效身份。
@pytest.mark.parametrize("kind", ["garbage", "signature", "payload", "oversize", "type"])
def test_signed_session_rejects_tampering(tmp_path, kind):
    module = auth_module()
    create_user(tmp_path)
    session = module.authenticate_user(tmp_path, account_source="local", user_id="alice", password=PASSWORD)
    payload, signature = session.session_token.split(".")
    token = {"garbage": "broken", "signature": payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:],
             "payload": ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature,
             "oversize": "A" * 9000, "type": None}[kind]
    with pytest.raises(HostAPIError):
        module.verify_user_session(tmp_path, token)


# LLM: 每次验签后重新读取账号修订及维护状态，旧token不得因重启新服务或重新启用复活。
# 函数用途: 验证改密码和停用立即撤销已有本地会话。
@pytest.mark.parametrize("change", ["password", "disable", "disable_enable", "maintenance"])
def test_local_sessions_revoke_from_current_disk_state(tmp_path, change):
    module = auth_module()
    create_user(tmp_path)
    session = module.authenticate_user(tmp_path, account_source="local", user_id="alice", password=PASSWORD)
    service = LocalUserLifecycle(LocalUserStore(tmp_path))
    if change == "password":
        service.set_password("alice", password=NEW_PASSWORD, idempotency_key="auth_password_reset_01")
    elif change == "maintenance":
        (service.store.owner_home("alice").home_dir / "owner_status.json").write_text('{"status":"disabled"}')
    else:
        service.disable("alice", operation=operation("auth_disable_alice01"))
        if change == "disable_enable":
            service.enable("alice", operation=operation("auth_enable_alice_01"))
    with pytest.raises(HostAPIError):
        module.verify_user_session(tmp_path, session.session_token)


# LLM: 当前飞书密码权威在root/session_lock同库，不能在owner下另建SessionLockStore或local账号。
# 函数用途: 证明飞书登录回到原资产目录，同名及改名不合并用户。
def test_feishu_uses_existing_im_password_owner_and_stable_subject(tmp_path):
    module = auth_module()
    first, _, row = feishu_user(tmp_path)
    second, _, _ = feishu_user(tmp_path, "ou_second")
    before = sorted(str(p.relative_to(tmp_path / "owners")) for p in (tmp_path / "owners").rglob("*"))
    one = module.authenticate_user(tmp_path, account_source="feishu", user_id="providers/feishu/users/ou_first", password=PASSWORD)
    two = module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_second", password=PASSWORD)
    assert one.owner_id == "providers/feishu/users/ou_first"
    assert resolve_owner_home(tmp_path, one.effective_owner).home_dir == first.home_dir
    assert resolve_owner_home(tmp_path, two.effective_owner).home_dir == second.home_dir
    assert one.effective_owner != two.effective_owner
    row["display_name"] = "alice"
    (tmp_path / "identity/provider_identity/feishu.jsonl").write_text(json.dumps(row) + "\n")
    renamed = module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD)
    assert renamed.effective_owner == one.effective_owner and renamed.display_name == "alice"
    assert module.verify_user_session(tmp_path, renamed.session_token).effective_owner == one.effective_owner
    assert sorted(str(p.relative_to(tmp_path / "owners")) for p in (tmp_path / "owners").rglob("*")) == before
    assert not (tmp_path / "owners/local").exists()
    assert not (tmp_path / "local_users/alice.json").exists()


# LLM: 来源有记录但disabled不可回退到物化目录，无首设密码也不创建密码库；损坏索引失败关闭。
# 函数用途: 验证飞书停用、无密码、密码错误、坏索引和管理员身份均不能登录。
@pytest.mark.parametrize("condition,code", [("disabled", "USER_DISABLED"), ("no_password", "PASSWORD_NOT_CONFIGURED"),
    ("wrong_password", "INVALID_CREDENTIALS"), ("corrupt_index", "STORE_UNAVAILABLE"),
    ("admin", "ADMIN_OWNER_FORBIDDEN"), ("admin_channel", "ADMIN_OWNER_FORBIDDEN")])
def test_feishu_login_denials_preserve_original_owners(tmp_path, condition, code):
    module = auth_module()
    home, store, _ = feishu_user(tmp_path, status="disabled" if condition == "disabled" else "active",
                                 bound=OwnerIdentity.local_main() if condition == "admin" else None)
    if condition == "no_password":
        store.set_password_hash("ou_first", "")
    if condition == "corrupt_index":
        (tmp_path / "identity/provider_identity/feishu.jsonl").write_text("broken\n")
    if condition == "admin_channel":
        from agent_py_agent.agent.user_space.admin_channel_identity import (
            bind_admin_channel_identity,
        )
        bind_admin_channel_identity(tmp_path, "feishu", "ou_first")
    with pytest.raises(HostAPIError, match=code):
        module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first",
                                 password=NEW_PASSWORD if condition == "wrong_password" else PASSWORD)
    assert (home.workspace_dir / "original.txt").read_text() == "ou_first"
    assert not (tmp_path / "local_users/.auth_session_secret").exists()


# LLM: token只证明既有密码与稳定身份，校验阶段必须重新观察飞书密码、身份绑定和维护禁用。
# 函数用途: 验证IM原库改密、绑定变化和停用使飞书会话立即失效。
@pytest.mark.parametrize("change", ["password", "identity", "disable", "maintenance"])
def test_feishu_session_rechecks_current_password_and_identity(tmp_path, change):
    module = auth_module()
    home, store, row = feishu_user(tmp_path)
    session = module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD)
    if change == "password":
        store.set_password_hash("ou_first", hash_password(NEW_PASSWORD))
    elif change == "maintenance":
        (home.home_dir / "owner_status.json").write_text('{"status":"disabled"}')
    else:
        row["owner_id" if change == "identity" else "status"] = "ou_second" if change == "identity" else "disabled"
        (tmp_path / "identity/provider_identity/feishu.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(HostAPIError):
        module.verify_user_session(tmp_path, session.session_token)
    assert not (tmp_path / "owners/providers/feishu/users/ou_second").exists()


# LLM: 有原目录及原库密码、没有展示索引的历史IM账号可认证，但未知用户不能借此物化新目录。
# 函数用途: 验证仅目录回退仍复用原空间和密码且不初始化第二套owner。
def test_feishu_materialized_identity_without_index_and_explicit_local_binding(tmp_path):
    module = auth_module()
    home, _, _ = feishu_user(tmp_path)
    (tmp_path / "identity/provider_identity/feishu.jsonl").unlink()
    session = module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD)
    assert resolve_owner_home(tmp_path, session.effective_owner).home_dir == home.home_dir
    create_user(tmp_path)
    bound, _, _ = feishu_user(tmp_path, "ou_bound", bound=OwnerIdentity.local_user("alice"))
    linked = module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_bound", password=PASSWORD)
    assert linked.owner_id == "local/alice" and bound.home_dir == tmp_path / "owners/local/alice"
    assert module.verify_user_session(tmp_path, linked.session_token).owner_id == "local/alice"


# LLM: 同根并发首次发token必须共用一个32字节密钥，不能相互覆盖导致刚发即失效。
# 函数用途: 验证原文件锁下密钥一次生成且所有并发会话可验证。
def test_session_secret_first_issue_concurrent(tmp_path):
    module = auth_module()
    feishu_user(tmp_path)
    assert not (tmp_path / "local_users/.auth_session_secret").exists()
    with ThreadPoolExecutor(max_workers=4) as pool:
        sessions = list(pool.map(lambda _: module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD), range(4)))
    assert all(module.verify_user_session(tmp_path, session.session_token).owner_id == "providers/feishu/users/ou_first" for session in sessions)


# LLM: 动作必须沿原闭合schema及能力名，密码重置仍是有幂等和CAS的管理动作。
# 函数用途: 验证三个新动作的声明、默认来源与未知字段拒绝。
def test_auth_and_password_action_specs_are_closed():
    from agent_py_agent.agent.plugin_host.action_specs import action_spec, action_specs
    assert {"users.local.password.set", "users.auth.login", "users.auth.verify"} <= set(action_specs())
    create = action_spec("users.local.create").validate_payload({"user_id": "alice", "password": PASSWORD})
    assert create["password"] == PASSWORD
    reset = action_spec("users.local.password.set")
    assert reset.requires_idempotency and reset.capabilities == frozenset({"users.manage"})
    login = action_spec("users.auth.login")
    assert login.validate_payload({"user_id": "alice", "password": PASSWORD})["account_source"] == "local"
    with pytest.raises(HostAPIError):
        login.validate_payload({"user_id": "alice", "password": PASSWORD, "owner_id": "local/main"})
    with pytest.raises(HostAPIError):
        reset.validate_payload({"user_id": "alice", "password": PASSWORD, "expected_revision": True})


# LLM: 原factory校验来源和grant，领域必须调用真实生命周期；响应中的token不得成为任意owner授权。
# 函数用途: 验证密码创建、查询、重置、登录和验会话的真实领域服务链。
def test_plugin_domain_password_and_auth_actions(tmp_path):
    from agent_py_agent.agent.plugin_host.local_users import LocalUsersDomain
    from agent_py_agent.tests.test_plugin_act_as_user import HostFixture
    host = HostFixture(tmp_path)
    domain = LocalUsersDomain(host.factory, host.usage)
    created = domain.call(host.issue("users.local.create", {"user_id": "alice", "password": PASSWORD}, None, "auth_domain_create01"))
    assert created.data.get("has_password") is True and "password_hash" not in str(created.data)
    login = domain.call(host.issue("users.auth.login", {"user_id": "alice", "password": PASSWORD}, None))
    assert login.data["owner_id"] == "local/alice"
    checked = domain.call(host.issue("users.auth.verify", {"session_token": login.data["session_token"]}, None))
    assert checked.data["owner_id"] == "local/alice"
    reset = host.issue("users.local.password.set", {"user_id": "alice", "password": NEW_PASSWORD, "expected_revision": 1}, None, "auth_domain_reset_01")
    assert domain.call(reset).data["revision"] == 2
    assert domain.call(reset).replayed
    with pytest.raises(HostAPIError):
        domain.call(host.issue("users.auth.verify", {"session_token": login.data["session_token"]}, None))
    for action in ("users.local.get", "users.local.list"):
        result = domain.call(host.issue(action, {"user_id": "alice"} if action.endswith("get") else {}, None))
        assert "password_hash" not in str(result.data) and PASSWORD not in str(result.data)


# LLM: 管理范围的通配语义不能从创建范围继承；直接核领域分页算法，上游通配grant接线另由集成者补齐。
# 函数用途: 证明只有manageable星号列出全部登记用户，且不会把星号交给存储读取。
@pytest.mark.parametrize("manageable,creatable,expected", [({"*"}, set(), ["alice", "bob"]),
    ({"alice"}, {"*"}, ["alice"])])
def test_wildcard_list_only_uses_manageable_scope(tmp_path, manageable, creatable, expected):
    from agent_py_agent.agent.plugin_host.local_users import LocalUsersDomain
    from agent_py_agent.agent.plugin_host.owner_context import AuthorizedLocalCall
    from agent_py_agent.tests.test_plugin_act_as_user import HostFixture
    host = HostFixture(tmp_path)
    create_user(tmp_path)
    create_user(tmp_path, "bob")
    grant = replace(host.grant, manageable_user_ids=frozenset(manageable), creatable_user_ids=frozenset(creatable))
    call = AuthorizedLocalCall(grant, "users.local.list")
    result = LocalUsersDomain(host.factory, host.usage).list_users(call, {})
    assert [item["user_id"] for item in result.data["items"]] == expected


# LLM: 原目录、索引及密码库仍是原事实；链接不应使新认证入口读取根外凭据或建立会话密钥。
# 函数用途: 验证飞书密码库链接及坏库均失败关闭，不降级为无密码。
@pytest.mark.parametrize("condition", ["symlink", "corrupt"])
def test_feishu_password_database_is_strict_and_fail_closed(tmp_path, condition):
    module = auth_module()
    feishu_user(tmp_path)
    path = tmp_path / "session_lock/private_chat_locks.db"
    if condition == "symlink":
        original = path.with_name("original.db")
        path.rename(original)
        path.symlink_to(original)
    else:
        path.write_bytes(b"not an sqlite database")
    with pytest.raises(HostAPIError, match="STORE_UNAVAILABLE"):
        module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD)
    assert not (tmp_path / "local_users/.auth_session_secret").exists()


# LLM: 登录不能在慢散列期间使用已撤销修订发出仍有效的会话，原账号变化必须被末次读取捕获。
# 函数用途: 在真实密码校验后插入一次改密，验证不发布旧状态会话。
def test_login_rechecks_password_change_during_verification(tmp_path, monkeypatch):
    module = auth_module()
    create_user(tmp_path)
    secret = (tmp_path / "local_users/.auth_session_secret").read_bytes()
    original = LocalUserLifecycle.verify_user_password
    # LLM: 只在两次磁盘观察之间改一次真实密码，不伪造认证或存储结果。
    # 函数用途: 复现密码验证与token签发之间的撤权窗口。
    def verify_then_reset(service, uid, password):
        user = original(service, uid, password)
        service.set_password(uid, password=NEW_PASSWORD, idempotency_key="auth_midflight_reset01")
        return user
    monkeypatch.setattr(LocalUserLifecycle, "verify_user_password", verify_then_reset)
    with pytest.raises(HostAPIError, match="SESSION_REVOKED"):
        module.authenticate_user(tmp_path, account_source="local", user_id="alice", password=PASSWORD)
    assert (tmp_path / "local_users/.auth_session_secret").read_bytes() == secret


# LLM: 解析可选tenant只能用准确结构字段；同名稳定身份跨tenant有歧义时不能猜任一记录。
# 函数用途: 验证多tenant主体需要显式选择且未知tenant无owner副作用。
def test_feishu_tenant_selection_is_explicit(tmp_path):
    module = auth_module()
    feishu_user(tmp_path, tenant="app-one")
    feishu_user(tmp_path, tenant="app-two")
    with pytest.raises(HostAPIError, match="IDENTITY_AMBIGUOUS"):
        module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD)
    with pytest.raises(HostAPIError, match="IDENTITY_NOT_FOUND"):
        module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD, tenant_or_app_key="absent")
    session = module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD, tenant_or_app_key="app-one")
    assert module.verify_user_session(tmp_path, session.session_token).owner_id == "providers/feishu/users/ou_first"


# LLM: 原IM锁定窗只在实际UnlockService内存中；新入口必须共用原实例，不能新造空失败账来绕过锁。
# 函数用途: 验证共享锁定窗和缺原实例时的失败关闭，正确密码也不能跳过暴破锁。
def test_feishu_auth_requires_original_lockout_service(tmp_path):
    module = auth_module()
    _, store, _ = feishu_user(tmp_path)
    assert callable(getattr(module, "bind_feishu_auth_service", None))
    service = UnlockService(store)
    module.bind_feishu_auth_service(tmp_path, service)
    for _ in range(5):
        assert service.unlock("ou_first", NEW_PASSWORD) is False
    with pytest.raises(HostAPIError, match="ACCOUNT_LOCKED"):
        module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD)
    assert not (tmp_path / "local_users/.auth_session_secret").exists()


# LLM: 有现成目录和密码也不能假定IM锁定窗为空；缺少原实例的宿主接线必须显式未就绪。
# 函数用途: 证明未知锁定状态不降级成可登录。
def test_feishu_unbound_lockout_source_is_not_ready(tmp_path):
    module = auth_module()
    _, store, _ = feishu_user(tmp_path)
    assert callable(getattr(module, "bind_feishu_auth_service", None))
    module.bind_feishu_auth_service(tmp_path, None)
    with pytest.raises(HostAPIError, match="HOST_NOT_READY"):
        module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD)
    assert store.has_password("ou_first")


# LLM: 私有序列化新增凭据时不能丢失原已知运行状态字段；公开投影仍不得出现散列。
# 函数用途: 验证旧账号可选字段在新from_dict中完整往返。
@pytest.mark.parametrize("running", [True, False])
def test_local_user_serialization_keeps_optional_runtime_status(tmp_path, running):
    user = replace(create_user(tmp_path).user, has_running_work=running)
    assert LocalUser.from_dict(user.to_dict()) == user
    assert "password_hash" not in user.public_view()


# LLM: 宿主便捷调用仍使用原持久事务，ordinary actor不得伪造管理员来源；不测试来源缺失兜底提权。
# 函数用途: 验证宿主管理签名和旧插件签名共用账号与幂等结果。
def test_host_lifecycle_keyword_contract_and_ordinary_actor_rejected(tmp_path):
    service = LocalUserLifecycle(LocalUserStore(tmp_path))
    view, replayed = service.create("alice", password=PASSWORD, quotas=None, idempotency_key="host_create_alice01",
                                    actor_owner=OwnerIdentity.local_main())
    assert view["has_password"] and not replayed
    assert tuple(service.create("alice", password=PASSWORD, idempotency_key="host_create_alice01"))[1] is True
    with pytest.raises(HostAPIError, match="CAPABILITY_DENIED"):
        service.create("bob", password=PASSWORD, idempotency_key="host_create_bob_001", actor_owner=OwnerIdentity.local_user("alice"))
    assert service.store.read_user("bob") is None


# LLM: 当前原IM密码库仅按uid索引，tenant参数绝不能把同一密码切换到另一个owner。
# 函数用途: 复现跨租户同uid/不同owner冲突，并要求显式tenant也失败关闭。
@pytest.mark.parametrize("tenant", ["app-one", "app-two"])
def test_feishu_tenant_collision_cannot_select_another_owner(tmp_path, tenant):
    module = auth_module()
    create_user(tmp_path)
    create_user(tmp_path, "bob")
    feishu_user(tmp_path, tenant="app-one", bound=OwnerIdentity.local_user("alice"))
    feishu_user(tmp_path, tenant="app-two", bound=OwnerIdentity.local_user("bob"))
    with pytest.raises(HostAPIError, match="IDENTITY_AMBIGUOUS"):
        module.authenticate_user(tmp_path, account_source="feishu", user_id="ou_first", password=PASSWORD,
                                 tenant_or_app_key=tenant)


# LLM: 虽按任务要求先作password_sha256预处理，持久化摘要仍不能成为不需根密钥的快速离线验证器。
# 函数用途: 证明操作索引不直接保存可凭已知公开实参复算的密码意图摘要。
def test_password_idempotency_digest_is_keyed_at_rest(tmp_path):
    from agent_py_agent.agent.user_space.local_user_lifecycle import _payload_hash
    from agent_py_agent.agent.user_space.owner_quota import LocalUserQuotaLimits
    create_user(tmp_path)
    intent = {"user_id": "alice", "display_name": "", "quota": LocalUserQuotaLimits().to_dict(), "password": PASSWORD}
    indexes = list((tmp_path / "local_users/operations").glob("*.json"))
    assert len(indexes) == 1
    saved = json.loads(indexes[0].read_text())
    assert saved["input_digest"] != _payload_hash("users.local.create", intent)
    assert create_user(tmp_path).replayed
    with pytest.raises(HostAPIError, match="IDEMPOTENCY_CONFLICT"):
        create_user(tmp_path, password=NEW_PASSWORD)


# LLM: 已观察新revision不能继续用旧read_user快照验token；同步点之后改密已落盘，返回前必须拒绝。
# 函数用途: 复现验会话读取与启用检查之间的密码重置竞争。
def test_verify_session_rejects_revision_mixed_reads(tmp_path, monkeypatch):
    module = auth_module()
    create_user(tmp_path)
    session = module.authenticate_user(tmp_path, account_source="local", user_id="alice", password=PASSWORD)
    original = LocalUserStore.read_user
    fired = False
    # LLM: 返回真实旧快照前提交一次真实改密，不能模拟最终校验结果。
    # 函数用途: 在两次账号读取之间制造确定性的修订变化。
    def read_then_reset(store, uid):
        nonlocal fired
        user = original(store, uid)
        if not fired:
            fired = True
            LocalUserLifecycle(store).set_password(uid, password=NEW_PASSWORD, idempotency_key="auth_race_reset_001")
        return user
    monkeypatch.setattr(LocalUserStore, "read_user", read_then_reset)
    with pytest.raises(HostAPIError):
        module.verify_user_session(tmp_path, session.session_token)
    assert fired
