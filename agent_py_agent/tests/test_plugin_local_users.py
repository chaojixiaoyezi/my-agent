# LLM: 用真实临时目录验证账号、配额和回执，只在故障边界注入异常。
# 模块用途: 覆盖普通本地用户创建、启停、幂等和失效关闭。
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.local_user_lifecycle import (
    LocalUserLifecycle,
    LocalUserOperation,
)
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore


# LLM: 这只是宿主内部写事务的原操作标识，不模拟插件令牌授权。
# 函数用途: 给临时账号创建或启停分配稳定幂等来源。
def operation(key="create_alice_0001", activation="a" * 64):
    return LocalUserOperation("installation-one", activation, key)


# LLM: 不创建宿主或owner目录，初始化副作用必须由首次合法写入承担。
# 函数用途: 创建冷的用户领域对象，供各测试检查真实落盘。
def lifecycle(root):
    return LocalUserLifecycle(LocalUserStore(root))


# LLM: 先测核心成功合同，空壳返回None应停在断言，不把导入错误当红灯证据。
# 函数用途: 验证账号发布前已具备独立目录和有限配额。
def test_create_publishes_identity_quota_and_atomic_receipt(tmp_path):
    service = lifecycle(tmp_path)
    result = service.create("alice", operation())
    assert result is not None
    assert result.user.status == "enabled"
    assert result.user.revision == 1
    assert result.replayed is False
    home = tmp_path / "owners" / "local" / "alice"
    assert service.store.owner_home("alice").home_dir == home
    quota = json.loads((home / "quota.json").read_text())
    assert quota["max_active_agents"] == 2
    assert quota["max_subagents"] == 2
    assert quota["max_depth"] == 1
    assert quota["max_disk_mb"] == 1024
    assert quota["revision"] == 1
    assert (home / "workspace").is_dir()
    assert not (tmp_path / "owners" / "local" / "main").exists()
    record = json.loads((tmp_path / "local_users" / "alice.json").read_text())
    assert record["provision"]["phase"] == "committed"
    assert record["provision"]["operation_id"] == result.operation_id
    assert lifecycle(tmp_path).store.get("alice") == result.user


# LLM: 两个同key并发请求与重启重放必须指向同一操作，不能反复物化或种配额。
# 函数用途: 验证真实文件锁、跨实例幂等和已存在用户冲突。
def test_create_concurrent_replay_and_conflict(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: lifecycle(tmp_path).create("alice", operation()), range(4)))
    assert all(result is not None for result in results)
    assert len({result.operation_id for result in results}) == 1
    assert sum(not result.replayed for result in results) == 1
    replay = lifecycle(tmp_path).create("alice", operation())
    assert replay.replayed
    with pytest.raises(HostAPIError, match="IDEMPOTENCY_CONFLICT"):
        lifecycle(tmp_path).create("alice", operation(), display_name="different")
    with pytest.raises(HostAPIError, match="USER_EXISTS"):
        lifecycle(tmp_path).create("alice", operation("new_creation_0001"))


# LLM: 旧激活来源不能使用同一key恢复写意图，即使管理员已经重新启用插件。
# 函数用途: 验证账号创建回执不因激活换代重跑。
def test_activation_change_cannot_replay_create(tmp_path):
    assert lifecycle(tmp_path).create("alice", operation()) is not None
    with pytest.raises(HostAPIError, match="ACTIVATION_REVOKED"):
        lifecycle(tmp_path).create("alice", operation(activation="b" * 64))


# LLM: 任何未登记目录都不能被插件创建接口认领，包括旧provider布局。
# 函数用途: 确认已有资料原封不动且不被发布成新账号。
@pytest.mark.parametrize("relative", ["owners/local/alice", "owners/providers/local/users/alice"])
def test_unmanaged_existing_directory_is_not_claimed(tmp_path, relative):
    old = tmp_path / relative
    old.mkdir(parents=True)
    marker = old / "keep.txt"
    marker.write_text("original")
    with pytest.raises(HostAPIError, match="USER_UNMANAGED"):
        lifecycle(tmp_path).create("alice", operation())
    assert marker.read_text() == "original"
    assert not (tmp_path / "local_users" / "alice.json").exists()


# LLM: 非法参数先于锁文件及目录物化，bool不能被当作整数配额。
# 函数用途: 防止输入校验失败留下用户或控制记录。
@pytest.mark.parametrize("quota", [
    {"max_active_agents": True, "max_subagents": 2, "max_depth": 1, "max_disk_mb": 1024},
    {"max_active_agents": 2, "max_subagents": 2, "max_depth": 1, "max_disk_mb": 0},
    {"max_active_agents": 2},
])
def test_invalid_quota_does_not_write(tmp_path, quota):
    with pytest.raises(HostAPIError, match="INVALID_SCHEMA"):
        lifecycle(tmp_path).create("alice", operation(), quota=quota)
    assert list(tmp_path.iterdir()) == []


# LLM: 停用先提交revision fence；重新启用不能让旧修订继续执行。
# 函数用途: 验证启停CAS、幂等和普通账号持久状态。
def test_disable_enable_revision_and_replay(tmp_path):
    service = lifecycle(tmp_path)
    assert service.create("alice", operation()) is not None
    stopped = service.disable("alice", 1, operation("disable_alice_01"))
    assert stopped.user.status == "disabled"
    assert stopped.user.revision == 2
    assert stopped.resources_stopped is None
    assert service.disable("alice", 1, operation("disable_alice_01")).replayed
    with pytest.raises(HostAPIError, match="REVISION_CONFLICT"):
        service.enable("alice", 1, operation("enable_alice_001"))
    # 已绑定的错误修订不能同key改参；新意图必须换key，不能靠重试篡改原操作。
    with pytest.raises(HostAPIError, match="IDEMPOTENCY_CONFLICT"):
        lifecycle(tmp_path).enable("alice", 2, operation("enable_alice_001"))
    active = lifecycle(tmp_path).enable("alice", 2, operation("enable_alice_002"))
    assert active.user.status == "enabled"
    assert active.user.revision == 3


# LLM: 冷查询不取得会产生文件的锁，不创建home；坏记录不能被当作用户不存在。
# 函数用途: 验证只读和损坏失效关闭。
def test_cold_reads_and_corruption_are_not_initialization(tmp_path):
    store = LocalUserStore(tmp_path / "missing")
    assert store.get("alice") is None
    assert store.list_registered(("alice", "bob")) == ()
    assert not store.root.exists()
    service = lifecycle(tmp_path)
    assert service.create("alice", operation()) is not None
    (tmp_path / "local_users" / "alice.json").write_text("broken")
    with pytest.raises(HostAPIError, match="STORE_UNAVAILABLE"):
        service.store.get("alice")


# LLM: 故障只注入quota边界，其余目录与回执均为真实IO。
# 函数用途: 验证种配额失败不发布enabled，原操作可恢复且其它操作不能接管。
def test_provision_failure_retains_receipt_and_resumes(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import local_user_lifecycle as module
    original = module.seed_local_user_quota

    # LLM: 模拟实际配额存储不可用，不替换整个生命周期。
    # 函数用途: 在目录已创建后中断物化。
    def unavailable(root, home, limits):
        raise module.OwnerQuotaUnavailable("disk unavailable")

    monkeypatch.setattr(module, "seed_local_user_quota", unavailable)
    with pytest.raises(HostAPIError, match="STORE_UNAVAILABLE"):
        lifecycle(tmp_path).create("alice", operation())
    assert lifecycle(tmp_path).store.get("alice").status == "provision_failed"
    with pytest.raises(HostAPIError, match="USER_EXISTS"):
        lifecycle(tmp_path).create("alice", operation("new_creation_0001"))
    monkeypatch.setattr(module, "seed_local_user_quota", original)
    assert lifecycle(tmp_path).create("alice", operation()).user.status == "enabled"


# LLM: 测真实路径替换和维护状态，不能只断言UID字符串。
# 函数用途: 拒绝链接调包、重建目录以及独立owner维护停用。
def test_enabled_checks_directory_identity_and_maintenance(tmp_path):
    service = lifecycle(tmp_path)
    service.create("alice", operation())
    assert service.store.require_enabled("alice", 1).user_id == "alice"
    home = tmp_path / "owners/local/alice"
    (home / "owner_status.json").write_text('{"status":"disabled"}')
    with pytest.raises(HostAPIError, match="USER_DISABLED"):
        service.store.require_enabled("alice", 1)
    home.rename(home.with_name("original"))
    home.symlink_to(home.with_name("original"), target_is_directory=True)
    with pytest.raises(HostAPIError, match="STORE_UNAVAILABLE"):
        service.store.require_enabled("alice", 1)


# LLM: 管理动作以安装owner为幂等范围，换目标用户不能规避同key输入冲突。
# 函数用途: 验证跨用户重用原创建key无第二份账号副作用。
def test_key_cannot_change_target_user(tmp_path):
    lifecycle(tmp_path).create("alice", operation())
    with pytest.raises(HostAPIError, match="IDEMPOTENCY_CONFLICT"):
        lifecycle(tmp_path).create("bob", operation())
    assert lifecycle(tmp_path).store.get("bob") is None


# LLM: 六动作都经过真实域入口和存储；只把上游A来源签发抽象成宿主端口。
# 函数用途: 验证管理域创建、限域读取、配额和启停完整本地链路。
def test_local_user_domain_six_actions(tmp_path):
    from agent_py_agent.tests.test_plugin_act_as_user import HostFixture
    from agent_py_agent.agent.plugin_host.local_users import LocalUsersDomain
    host = HostFixture(tmp_path)
    domain = LocalUsersDomain(host.factory, host.usage)
    created = domain.call(host.issue("users.local.create", {"user_id": "alice"}, None, "domain_create_0001"))
    assert created is not None
    assert created.data["owner"]["owner_kind"] == "user"
    listing = domain.call(host.issue("users.local.list", {}, None))
    assert [user["user_id"] for user in listing.data["items"]] == ["alice"]
    fetched = domain.call(host.issue("users.local.get", {"user_id": "alice"}, None))
    assert fetched.data["revision"] == 1
    quota = domain.call(host.issue("users.local.quota.get", {"user_id": "alice"}, None))
    assert quota.data["limits"]["max_disk_mb"] == 1024
    assert quota.data["complete"] is False
    assert quota.data["usage"]["disk_bytes"] is None
    stopped = domain.call(host.issue("users.local.disable", {"user_id": "alice", "expected_revision": 1}, None, "domain_disable_01"))
    assert stopped.data["user"]["revision"] == 2
    enabled = domain.call(host.issue("users.local.enable", {"user_id": "alice", "expected_revision": 2}, None, "domain_enable_001"))
    assert enabled.data["status"] == "enabled"
    assert enabled.data["revision"] == 3


# LLM: 域拒绝发生在创建锁和目录前，范围、schema与配额上限不靠底层兜底。
# 函数用途: 防止额外参数、管理越权、配额超授权和伪装代执行。
@pytest.mark.parametrize("case", ["extra", "quota", "capability", "manageable", "creatable", "as_user", "key"])
def test_domain_denial_has_no_side_effect(tmp_path, case):
    from dataclasses import replace
    from agent_py_agent.tests.test_plugin_act_as_user import HostFixture
    from agent_py_agent.agent.plugin_host.local_users import LocalUsersDomain
    host = HostFixture(tmp_path)
    payload = {"user_id": "alice"}
    if case == "extra":
        payload["owner"] = "main"
    elif case == "quota":
        payload["quota"] = {"max_active_agents": 3, "max_subagents": 2, "max_depth": 1, "max_disk_mb": 1024}
    elif case == "capability":
        host.grant = replace(host.grant, capabilities=frozenset())
    elif case == "manageable":
        host.grant = replace(host.grant, manageable_user_ids=frozenset())
    elif case == "creatable":
        host.grant = replace(host.grant, creatable_user_ids=frozenset())
    source = host.issue("users.local.create", payload, "alice" if case == "as_user" else None,
                        None if case == "key" else "domain_create_0001")
    with pytest.raises(HostAPIError):
        LocalUsersDomain(host.factory, host.usage).call(source)
    assert list(tmp_path.iterdir()) == []


# LLM: 账号安全种子必须能被原owner策略读取，不能另起不兼容schema字段。
# 函数用途: 核验真实配额和权限文件沿原宿主schema发布。
def test_seed_uses_original_owner_policy_schema(tmp_path):
    import json
    from agent_py_agent.tests.test_plugin_act_as_user import HostFixture
    host = HostFixture(tmp_path)
    host.create_user()
    home = host.factory.store.owner_home("alice").home_dir
    assert json.loads((home / "permissions.json").read_text()).get("schema_version") == "permissions.v1"
    assert json.loads((home / "quota.json").read_text()).get("schema_version") == "quota.v2"


# LLM: 停止失败不能回滚已提交disabled，也不能让调用方误以为账号尚未停用。
# 函数用途: 验证取消故障作为警告返回且同键不重复触发取消。
def test_disable_commits_even_if_cancellation_fails_and_reason_binds_key(tmp_path):
    from agent_py_agent.tests.test_plugin_act_as_user import HostFixture
    from agent_py_agent.agent.plugin_host.local_users import LocalUsersDomain
    host = HostFixture(tmp_path)
    host.create_user()
    cancellations = []
    # LLM: 只注入外部资源取消故障，账号提交仍使用真实事务。
    # 函数用途: 记录取消调用并模拟资源停止不可确认。
    def stop(uid, revision):
        cancellations.append((uid, revision))
        raise OSError("resource not stopped")
    domain = LocalUsersDomain(host.factory, host.usage, stop)
    payload = {"user_id": "alice", "expected_revision": 1, "reason": "maintenance"}
    source = host.issue("users.local.disable", payload, None, "domain_disable_01")
    result = domain.call(source)
    assert result.data["user"]["status"] == "disabled"
    assert result.data["resources_stopped"] is None
    assert result.warnings == ("RESOURCE_STOP_UNCONFIRMED",)
    assert domain.call(source).replayed
    assert cancellations == [("alice", 2)]
    changed = host.issue("users.local.disable", dict(payload, reason="changed"), None, "domain_disable_01")
    with pytest.raises(HostAPIError, match="IDEMPOTENCY_CONFLICT"):
        domain.call(changed)


# LLM: 原子发布后响应丢失不能抹掉已提交回执或把enabled改成失败。
# 函数用途: 验证发布点故障后从原文件确认提交并只重放原结果。
def test_create_post_commit_fault_preserves_original_receipt(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_plugin_act_as_user import HostFixture
    from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle, LocalUserOperation
    host = HostFixture(tmp_path)
    store = host.factory.store
    original = store.save
    # LLM: 故障在真实原子写成功后产生，不能让测试替代落盘本身。
    # 函数用途: 模拟提交已生效但调用方未收到正常返回。
    def save(uid, record):
        original(uid, record)
        if record["user"]["status"] == "enabled":
            raise OSError("response lost after commit")
    monkeypatch.setattr(store, "save", save)
    operation = LocalUserOperation("fixture-install", "a" * 64, "fixture_commit_fault")
    service = LocalUserLifecycle(store)
    result = service.create("alice", operation)
    assert result.user.status == "enabled"
    assert service.create("alice", operation).replayed


# LLM: 仅把JSON status改成enabled不能洗白未完成物化；账号记录须保留准确成功receipt。
# 函数用途: 验证损坏的物化终态不被只读或执行路径采信。
def test_enabled_record_without_committed_receipt_is_rejected(tmp_path):
    from agent_py_agent.tests.test_plugin_act_as_user import HostFixture
    host = HostFixture(tmp_path)
    host.create_user()
    record = host.factory.store.read_record("alice")
    record["provision"]["phase"] = "reserved"
    host.factory.store.save("alice", record)
    with pytest.raises(HostAPIError, match="STORE_UNAVAILABLE"):
        host.factory.store.require_enabled("alice")
