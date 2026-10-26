# LLM: T1–T7沿真实临时owner存储测试，执行单元替身不替代真实隔离验证。
# 模块用途: 验证审计组权限、无副作用观察及四类黑盒调用合同。
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.identity_store import (
    ProviderIdentityRecord,
    link_provider_identity,
    resolve_owner_from_provider_identity,
)
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUser, LocalUserStore
from agent_py_agent.agent.user_space.user_auth import _feishu_owner
from agent_py_agent.tests.test_dual_bot_sessions_and_rollover import (
    business_call,
    host_member,
)
from agent_py_agent.tests.test_dual_bot_sessions_and_rollover import (
    wired_host as wired_host,
)


# LLM: 只物化临时受管账号；生产域事务及配额种子不mock。
# 函数用途: 给测试创建可复用的真实本地用户。
def account(root, uid="alice"):
    service = LocalUserLifecycle(LocalUserStore(root))
    service.create(uid, idempotency_key=f"create_{uid}_00000001")
    return service


def test_t1_legacy_account_defaults_and_fixed_web_policy(tmp_path):
    service = account(tmp_path)
    raw = service.store.get("alice").to_dict()
    assert raw.get("in_audit_group") is False
    user = LocalUser.from_dict(raw)
    assert user.allowed_channels == ("web",)
    assert user.im_binding_allowed is False
    assert replace(user, allowed_channels=("feishu",), im_binding_allowed=True).allowed_channels == ("web",)
    assert replace(user, im_binding_allowed=True).im_binding_allowed is False
    assert user.public_view()["in_audit_group"] is False


def test_t2_membership_admin_cas_and_durable_idempotency(tmp_path):
    service = account(tmp_path)
    setter = getattr(service, "set_audit_group_membership", None)
    assert callable(setter), "membership CAS transaction is missing"
    result = setter("alice", in_audit_group=True, expected_revision=1, idempotency_key="membership_00001")
    assert result["in_audit_group"] is True and result["revision"] == 2
    assert result["audit_group_updated_by"] == "local/main"
    assert result["audit_group_updated_at"]
    assert LocalUserLifecycle(service.store).set_audit_group_membership(
        "alice", in_audit_group=True, expected_revision=1, idempotency_key="membership_00001")["replayed"] is True
    with pytest.raises(HostAPIError, match="REVISION_CONFLICT"):
        setter("alice", in_audit_group=False, expected_revision=1, idempotency_key="membership_00002")
    with pytest.raises(HostAPIError, match="IDEMPOTENCY_CONFLICT"):
        setter("alice", in_audit_group=False, expected_revision=1, idempotency_key="membership_00001")
    with pytest.raises(HostAPIError, match="AUDIT_GROUP_ADMIN_ONLY"):
        setter("alice", in_audit_group=True, actor_owner="local/bob")
    for scope in ("local/main", "providers/feishu/users/alice"):
        with pytest.raises(HostAPIError, match="MANAGED_USER_SCOPE_DENIED"):
            setter(scope, in_audit_group=True)


@pytest.mark.parametrize("index,target", [
    ("provider_identity/feishu.jsonl", "alice"),
    ("provider_identity/feishu.jsonl", "local/alice"),
    ("linked_identities.jsonl", "alice"),
    ("linked_identities.jsonl", "local/alice"),
])
def test_t2_membership_rejects_active_historical_im_binding(tmp_path, index, target):
    service = account(tmp_path)
    path = tmp_path / "identity" / index
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"provider": "local" if index.startswith("provider_identity/") else "feishu",
           "provider_subject_id": "ou_legacy", "canonical_user_id": "canonical_alice",
           "owner_kind": "user", "owner_id": target, "status": "active"}
    path.write_text(json.dumps(row) + "\n")
    before, bindings = service.store.read_record("alice"), path.read_bytes()
    with pytest.raises(HostAPIError) as denied:
        service.set_audit_group_membership("alice", in_audit_group=True,
            expected_revision=1, idempotency_key="historical_im_join_01")
    assert (denied.value.code, denied.value.status) == ("MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED", 409)
    assert service.store.read_record("alice") == before
    assert path.read_bytes() == bindings


@pytest.mark.parametrize("index", ["provider_identity/feishu.jsonl", "linked_identities.jsonl"])
def test_t2_membership_follows_each_binding_index_current_record(tmp_path, index):
    service = account(tmp_path)
    path = tmp_path / "identity" / index
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"provider": "local" if index.startswith("provider_identity/") else "feishu",
           "provider_subject_id": "ou_legacy", "canonical_user_id": "canonical_alice",
           "owner_kind": "user", "owner_id": "alice", "status": "active"}
    # provider模型没有status撤销字段；它必须实际改绑，linked账才使用revoked。
    current = row | ({"provider": "feishu"} if index.startswith("provider_identity/") else {"status": "revoked"})
    path.write_text(json.dumps(row) + "\n" + json.dumps(current) + "\n")
    result = service.set_audit_group_membership("alice", in_audit_group=True)
    assert result["in_audit_group"] is True


@pytest.mark.parametrize("status", ["revoked", "disabled", "actve", "", None, 1])
def test_t2_provider_binding_extra_status_cannot_revoke_identity(tmp_path, status):
    service = account(tmp_path)
    path = tmp_path / "identity/provider_identity/feishu.jsonl"
    path.parent.mkdir(parents=True)
    row = ProviderIdentityRecord("local", "ou_legacy", "user", "alice").to_dict() | {"status": status}
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(HostAPIError) as denied:
        service.set_audit_group_membership("alice", in_audit_group=True)
    assert (denied.value.code, denied.value.status) == ("MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED", 409)


@pytest.mark.parametrize("index", ["provider_identity/feishu.jsonl", "linked_identities.jsonl"])
@pytest.mark.parametrize("damage", ["missing_owner", "bad_owner", "missing_subject", "empty_subject", "owner_space"])
def test_t2_membership_semantically_corrupt_binding_fails_closed(tmp_path, index, damage):
    service = account(tmp_path)
    path = tmp_path / "identity" / index
    path.parent.mkdir(parents=True)
    row = {"provider": "local" if index.startswith("provider_identity/") else "feishu",
           "provider_subject_id": "ou_legacy", "canonical_user_id": "canonical_alice",
           "owner_kind": "user", "owner_id": "alice", "status": "active"}
    damaged = dict(row)
    if damage == "missing_owner":
        damaged.pop("owner_id")
    elif damage == "bad_owner":
        damaged["owner_id"] = None
    elif damage == "missing_subject":
        damaged.pop("provider_subject_id")
    elif damage == "empty_subject":
        damaged["provider_subject_id"] = ""
    else:
        damaged["owner_id"] = "alice "
    path.write_text(json.dumps(row) + "\n" + json.dumps(damaged) + "\n")
    before = service.store.read_record("alice")
    with pytest.raises(HostAPIError) as denied:
        service.set_audit_group_membership("alice", in_audit_group=True)
    assert (denied.value.code, denied.value.status) == ("STORE_UNAVAILABLE", 503)
    assert service.store.read_record("alice") == before


@pytest.mark.parametrize("status", ["actve", "", None, 1])
def test_t2_linked_binding_unknown_status_fails_closed(tmp_path, status):
    service = account(tmp_path)
    path = tmp_path / "identity/linked_identities.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"provider": "feishu", "provider_subject_id": "ou_legacy",
        "canonical_user_id": "canonical_alice", "owner_id": "local/alice", "status": status}) + "\n")
    with pytest.raises(HostAPIError) as denied:
        service.set_audit_group_membership("alice", in_audit_group=True)
    assert (denied.value.code, denied.value.status) == ("STORE_UNAVAILABLE", 503)


@pytest.mark.parametrize("index", ["provider_identity/feishu.jsonl", "linked_identities.jsonl"])
@pytest.mark.parametrize("replay", [False, True])
def test_t2_creation_initial_membership_obeys_historical_binding_guard(tmp_path, index, replay):
    service = LocalUserLifecycle(LocalUserStore(tmp_path))
    if replay:
        service.create("alice", in_audit_group=True, idempotency_key="initial_group_im_0001")
    path = tmp_path / "identity" / index
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"provider": "local" if index.startswith("provider_identity/") else "feishu",
        "provider_subject_id": "ou_legacy", "canonical_user_id": "canonical_alice",
        "owner_kind": "user", "owner_id": "local/alice", "status": "active"}) + "\n")
    before = service.store.read_record("alice")
    with pytest.raises(HostAPIError) as denied:
        service.create("alice", in_audit_group=True, idempotency_key="initial_group_im_0001")
    assert (denied.value.code, denied.value.status) == ("MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED", 409)
    assert service.store.read_record("alice") == before


def test_t2_initial_membership_rechecks_before_enabled_publication(tmp_path, monkeypatch):
    service = LocalUserLifecycle(LocalUserStore(tmp_path))
    seed = service.seed_policies

    def seed_then_bind(uid):
        seed(uid)
        path = tmp_path / "identity/linked_identities.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"provider": "feishu", "provider_subject_id": "ou_legacy",
            "canonical_user_id": "canonical_alice", "owner_id": "local/alice", "status": "active"}) + "\n")

    monkeypatch.setattr(service, "seed_policies", seed_then_bind)
    with pytest.raises(HostAPIError) as denied:
        service.create("alice", in_audit_group=True, idempotency_key="initial_group_late_01")
    assert (denied.value.code, denied.value.status) == ("MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED", 409)
    record = service.store.read_record("alice")
    assert record["user"]["status"] == "provision_failed" and not record["operations"]


@pytest.mark.parametrize("limit", ["files", "bytes"])
def test_t2_membership_im_index_budget_is_fail_closed(tmp_path, monkeypatch, limit):
    from agent_py_agent.agent.user_space import local_user_channel_policy as module

    service = account(tmp_path)
    path = tmp_path / "identity/provider_identity/feishu.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(ProviderIdentityRecord("feishu", "ou_native", "user", "ou_native").to_dict()) + "\n")
    monkeypatch.setattr(module, "_MAX_BINDING_FILES" if limit == "files" else "_MAX_BINDING_BYTES", 0)
    with pytest.raises(HostAPIError) as denied:
        service.set_audit_group_membership("alice", in_audit_group=True)
    assert (denied.value.code, denied.value.status) == ("STORE_UNAVAILABLE", 503)


def test_t2_membership_native_provider_same_name_does_not_block(tmp_path):
    service = account(tmp_path)
    link_provider_identity(home_paths(tmp_path), ProviderIdentityRecord("feishu", "ou_native", "user", "alice"))
    link_provider_identity(home_paths(tmp_path), ProviderIdentityRecord("local", "ou_admin", "main", "main"))
    assert service.set_audit_group_membership("alice", in_audit_group=True)["in_audit_group"] is True


def test_t2_membership_rechecks_im_binding_on_replay_but_allows_revocation(tmp_path):
    service = member(tmp_path)
    service.set_audit_group_membership("alice", in_audit_group=True, idempotency_key="legacy_replay_00001")
    path = tmp_path / "identity/linked_identities.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"provider": "feishu", "provider_subject_id": "ou_legacy",
        "canonical_user_id": "canonical_alice", "owner_id": "local/alice", "status": "active"}) + "\n")
    before = service.store.read_record("alice")
    with pytest.raises(HostAPIError, match="MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED"):
        service.set_audit_group_membership("alice", in_audit_group=True, idempotency_key="legacy_replay_00001")
    assert service.store.read_record("alice") == before
    assert service.set_audit_group_membership("alice", in_audit_group=False)["in_audit_group"] is False


@pytest.mark.parametrize("invalid", ["corrupt", "symlink"])
def test_t2_membership_unreadable_im_index_fails_closed(tmp_path, invalid):
    service = account(tmp_path)
    path = tmp_path / "identity/linked_identities.jsonl"
    path.parent.mkdir(parents=True)
    if invalid == "symlink":
        target = tmp_path / "untrusted_index"
        target.write_text("")
        path.symlink_to(target)
    else:
        path.write_text("broken\n")
    before = service.store.read_record("alice")
    with pytest.raises(HostAPIError) as denied:
        service.set_audit_group_membership("alice", in_audit_group=True)
    assert (denied.value.code, denied.value.status) == ("STORE_UNAVAILABLE", 503)
    assert service.store.read_record("alice") == before


def test_t4_shared_interpreter_and_readiness_contract(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import isolated_unit as module
    executable, roots = str(tmp_path / "runtime/bin/python3"), (tmp_path / "runtime",)
    monkeypatch.setattr(module, "_interpreter_runtime", lambda interpreter: (executable, roots), raising=False)
    sandbox = module.execution_sandbox(tmp_path / "workspace", tmp_path / "stage")
    assert roots[0] in sandbox.spec.public_read_roots
    assert sandbox.spec.read_mode == "allowlist" and sandbox.spec.network_access is False
    seen = []
    error = RuntimeError("native-readiness-refused")

    def refuse(_sandbox, argv):
        seen.append(argv)
        raise error

    monkeypatch.setattr(type(sandbox), "build_argv", refuse)
    monkeypatch.setattr(module, "_outer_seatbelt_active", lambda: True)
    with pytest.raises(RuntimeError) as denied:
        module.IsolatedScriptUnit().execute('print("{}")', {}, workspace=tmp_path / "workspace",
                                             staging=tmp_path / "stage", timeout_s=1)
    assert seen[0][0] == executable
    assert denied.value is error


def test_t1_new_binding_denied_before_any_write(tmp_path):
    record = ProviderIdentityRecord("local", "ou_alice", "user", "alice")
    with pytest.raises(HostAPIError, match="MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED"):
        link_provider_identity(home_paths(tmp_path), record)
    assert list(tmp_path.iterdir()) == []


def test_t1_legacy_binding_and_auth_mapping_are_denied(tmp_path):
    home = home_paths(tmp_path)
    path = tmp_path / "identity/provider_identity/feishu.jsonl"
    path.parent.mkdir(parents=True)
    row = ProviderIdentityRecord("local", "ou_alice", "user", "alice").to_dict()
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(HostAPIError, match="MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED"):
        resolve_owner_from_provider_identity(home, provider="feishu", provider_subject_id="ou_alice")
    with pytest.raises(HostAPIError, match="MANAGED_USER_WEB_ONLY_CHANNEL_RESTRICTED"):
        _feishu_owner(tmp_path, "ou_alice", row)
    assert not (tmp_path / "owners/local/alice").exists()


def test_t1_admin_and_native_feishu_bindings_are_preserved(tmp_path):
    home = home_paths(tmp_path)
    assert link_provider_identity(home, ProviderIdentityRecord("feishu", "ou_native", "user", "ou_native")).is_file()
    assert link_provider_identity(home, ProviderIdentityRecord("local", "ou_admin", "main", "main")).is_file()
    assert _feishu_owner(tmp_path, "ou_native", None).provider == "feishu"


# LLM: 模拟标准资产文件而非mock读取器；测试输入的秘密永远是合成值。
# 函数用途: 在临时owner里写一份真实可读的原生资产夹具。
def asset(root, kind, identifier, **patch):
    home = root / "owners/local/main"
    record = {"id": identifier, "kind": kind, "name": identifier, "summary": "test asset",
              "enabled": True, "revision": 1, "io_contract": {"inputs": [], "outputs": ["value"]}, **patch}
    collection = {"script": "scripts", "workflow": "workflows", "skill": "skills", "datasource": "datasources"}[kind]
    folder = home / collection / identifier
    folder.mkdir(parents=True, exist_ok=True)
    if kind == "skill":
        (folder / "SKILL.md").write_text("---\nname: " + identifier + "\ndescription: safe summary\n---\nPRIVATE SKILL BODY")
        (folder / "skill_meta.json").write_text(json.dumps(record))
    else:
        if kind == "script":
            record.update(entrypoint="main.py", interpreter="python3", timeout_seconds=5)
            (folder / "main.py").write_text(record.pop("code", 'print(\'{"value": 7}\')'))
        (folder / f"{kind}.json").write_text(json.dumps(record))
    return folder


# LLM: 只观察原目录字节、权限和mtime；atime是读取副作用，不作为产品写入判定。
# 函数用途: 钉住source目录不能增加锁文件或修改任何现有内容。
def inventory(home):
    return {str(path.relative_to(home)): (path.stat().st_mode, path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None) for path in home.rglob("*")}


def member(root):
    service = account(root)
    service.set_audit_group_membership("alice", in_audit_group=True)
    return service


def test_t1_access_matrix_and_t2_immediate_revocation(tmp_path):
    from agent_py_agent.agent.audit_group.access_policy import (
        AuditGroupError,
        require_audit_group_member,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    service = account(tmp_path)
    assert require_audit_group_member(tmp_path, OwnerIdentity.local_main()) is not None
    for owner in ("local/alice", "providers/feishu/users/ou_native"):
        with pytest.raises(AuditGroupError) as denied:
            require_audit_group_member(tmp_path, owner)
        assert denied.value.code == "AUDIT_GROUP_ACCESS_DENIED" and denied.value.status_code == 403
    service.set_audit_group_membership("alice", in_audit_group=True)
    assert require_audit_group_member(tmp_path, "local/alice")["in_audit_group"] is True
    service.disable("alice", idempotency_key="disable_alice_00001")
    with pytest.raises(AuditGroupError) as disabled:
        require_audit_group_member(tmp_path, "local/alice")
    assert disabled.value.code == "USER_DISABLED"
    service.enable("alice", idempotency_key="enable_alice_000001")
    service.set_audit_group_membership("alice", in_audit_group=False)
    with pytest.raises(AuditGroupError, match="AUDIT_GROUP_ACCESS_DENIED"):
        require_audit_group_member(tmp_path, "local/alice")


def test_t3_whitelist_schema_monitor_and_source_read_only(tmp_path):
    from agent_py_agent.agent.audit_group.observation_service import AuditGroupObservationService
    member(tmp_path)
    schema = {"type": "object", "properties": {"ip": {"type": "string", "default": "/Users/private/source.py",
              "description": "api_key=synthetic-secret", "examples": ["/etc/private"]}}, "$ref": "/tmp/private"}
    for kind in ("script", "skill", "workflow", "datasource"):
        asset(tmp_path, kind, kind + "1", parameters_schema=schema, prompt="PRIVATE PROMPT", steps=[{"prompt": "PRIVATE STEP"}],
              credential_refs=["private-ref"], source_code="PRIVATE SOURCE", summary="safe /Users/private/report api_key=synthetic-secret")
    home = tmp_path / "owners/local/main"
    (home / "data/scheduler").mkdir(parents=True)
    (home / "data/scheduler/store.json").write_text(json.dumps({"jobs": {"monitor1": {
        "job_id": "monitor1", "name": "monitor", "prompt": "PRIVATE MONITOR PROMPT", "status": "paused", "version": 4}}, "runs": {}}))
    (home / "data/monitors").mkdir()
    (home / "data/monitors/monitor1.json").write_text(json.dumps({"id": "monitor1", "revision": 2,
        "enabled": True, "schedule_spec": {"enabled": True}}))
    before = inventory(home)
    service = AuditGroupObservationService(tmp_path)
    result = service.summarize_assets("local/alice", owner_ids=["local/main"],
                                      kinds=["script", "skill", "workflow", "monitor", "datasource"])
    assert result is not None
    assert result["coverage"] == "complete" and len(result["items"]) == 5
    permitted = {"owner_id", "kind", "id", "name", "summary", "parameters_schema", "enabled", "revision",
                 "updated_at", "run_count", "last_run_at", "last_status", "invocable"}
    assert all(set(item) == permitted for item in result["items"])
    public = json.dumps(result)
    for forbidden in ("PRIVATE", "private-ref", "synthetic-secret", "/Users/", "/etc/", "/tmp/", "source_code", "credential_refs", "$ref"):
        assert forbidden not in public
    monitor = next(item for item in result["items"] if item["kind"] == "monitor")
    assert monitor["enabled"] is False and monitor["summary"] == "" and str(monitor["revision"]) == "2:4"
    assert inventory(home) == before


def test_t7_partial_assets_alerts_and_posture_do_not_hide_corruption(tmp_path):
    from agent_py_agent.agent.audit_group.observation_service import AuditGroupObservationService
    member(tmp_path)
    account(tmp_path, "bob")
    asset(tmp_path, "script", "good")
    bad = tmp_path / "owners/local/bob/scripts/bad"
    bad.mkdir(parents=True)
    (bad / "script.json").write_text("broken")
    scheduler = tmp_path / "owners/local/bob/data/scheduler"
    scheduler.mkdir()
    (scheduler / "store.json").write_text("broken")
    service = AuditGroupObservationService(tmp_path)
    for result in (service.summarize_assets("local/alice"), service.list_alerts("local/alice"),
                   service.posture_overview("local/alice")):
        assert result is not None
        assert result["coverage"] == "partial" and result["source_errors"]
        assert "broken" not in json.dumps(result) and str(tmp_path) not in json.dumps(result)


def test_t2_observation_revokes_existing_service_and_t3_owner_pagination(tmp_path):
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    from agent_py_agent.agent.audit_group.observation_service import AuditGroupObservationService
    service = member(tmp_path)
    observed = AuditGroupObservationService(tmp_path)
    page = observed.list_owners("local/alice", limit=1)
    assert page is not None
    assert len(page["items"]) == 1 and page["next_cursor"]
    next_page = observed.list_owners("local/alice", limit=1, cursor=page["next_cursor"])
    assert page["items"][0]["owner_id"] != next_page["items"][0]["owner_id"]
    assert "owner_home" not in json.dumps(page)
    service.set_audit_group_membership("alice", in_audit_group=False)
    for method in (observed.list_owners, observed.summarize_assets, observed.list_alerts, observed.posture_overview):
        with pytest.raises(AuditGroupError):
            method("local/alice")


# LLM: 所有四种原资产依赖同一个source脚本，caller同名脚本故意不同，用于检测错误回退。
# 函数用途: 准备真实文件上的跨owner黑盒依赖图。
def invocation_assets(root):
    service = member(root)
    asset(root, "script", "source_script", code='import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"value": x.get("n", 7)}))')
    asset(root, "skill", "source_skill", metadata={"blackbox": {"executor": "script", "script_id": "source_script"}})
    asset(root, "workflow", "source_workflow", steps=[{"step_id": "step1", "name": "first",
        "mode": "deterministic_script", "script_id": "source_script", "inputs_map": {"n": {"step_id": "$inputs", "key": "n"}}}])
    home = root / "owners/local/main"
    (home / "data/scheduler").mkdir(parents=True)
    (home / "data/scheduler/store.json").write_text(json.dumps({"jobs": {"source_monitor": {
        "job_id": "source_monitor", "status": "active", "version": 1, "run_count": 9, "next_run_at": 999,
        "rules": [{"id": "r1", "enabled": True, "use_llm": False,
                   "tool": {"name": "scripts", "arguments": {"action": "run", "script_id": "source_script"}}}]}}, "runs": {}}))
    return service


@pytest.mark.parametrize("kind,identifier", [("script", "source_script"), ("skill", "source_skill"),
                                          ("workflow", "source_workflow"), ("monitor", "source_monitor")])
def test_t4_four_kind_orchestration_source_unchanged_and_private_receipt(tmp_path, monkeypatch, kind, identifier):
    from agent_py_agent.agent.audit_group.blackbox_invoke_service import (
        AuditGroupBlackboxInvokeService,
    )
    invocation_assets(tmp_path)
    source = tmp_path / "owners/local/main"
    before = inventory(source)
    # 替身仅代替OS执行边界；冻结、鉴权、治理、配额及落盘均为真实产品路径。
    calls = []
    def execute(_unit, code, inputs, *, workspace, staging, timeout_s):
        calls.append((code, inputs, workspace, staging))
        return {"value": inputs.get("n", 7), "code": "PRIVATE SOURCE", "extra": "private-ref"}
    service = AuditGroupBlackboxInvokeService(tmp_path)
    unit_type = getattr(__import__(service.__module__, fromlist=["IsolatedScriptUnit"]), "IsolatedScriptUnit", None)
    if unit_type is not None:
        monkeypatch.setattr(unit_type, "execute", execute)
    result = service.invoke_asset("local/alice", source_owner="local/main", asset_type=kind,
                                  asset_id=identifier, inputs={"n": 17}, rule_id="r1")
    assert result is not None
    assert result["status"] == "succeeded" and result["result"] == {"value": 17}
    assert result["caller_owner_id"] == result["output_workspace_owner"] == "local/alice"
    # R2B的隔离workspace归本次调用，不再借普通项目目录判断文件归属。
    assert calls and calls[0][2].parent == calls[0][3] and calls[0][2].name.startswith("workspace-")
    assert calls[0][3].is_relative_to(tmp_path / "owners/local/alice/data/audit_group_exec")
    assert inventory(source) == before
    receipts = list((tmp_path / "owners/local/alice/data/audit_group_exec").rglob("*.json"))
    assert receipts
    for receipt in receipts:
        text = receipt.read_text()
        assert "workflow_snapshot" not in text and "PRIVATE" not in text and "private-ref" not in text
        assert "import json" not in text and str(tmp_path) not in text


def test_t5_disabled_dependency_and_revision_and_t6_reject_contractless_skill(tmp_path):
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    from agent_py_agent.agent.audit_group.blackbox_invoke_service import (
        AuditGroupBlackboxInvokeService,
    )
    invocation_assets(tmp_path)
    service = AuditGroupBlackboxInvokeService(tmp_path)
    with pytest.raises(AuditGroupError) as stale:
        service.invoke_asset("local/alice", source_owner="local/main", asset_type="script", asset_id="source_script", expected_revision=0)
    assert stale.value.code == "REVISION_CONFLICT" and stale.value.status_code == 409
    asset(tmp_path, "script", "source_script", enabled=False)
    with pytest.raises(AuditGroupError) as disabled:
        service.invoke_asset("local/alice", source_owner="local/main", asset_type="workflow", asset_id="source_workflow")
    assert disabled.value.code == "ASSET_DISABLED_BY_OWNER" and disabled.value.status_code == 409
    asset(tmp_path, "skill", "plain_skill")
    with pytest.raises(AuditGroupError) as skill:
        service.invoke_asset("local/alice", source_owner="local/main", asset_type="skill", asset_id="plain_skill")
    assert skill.value.code == "AUDIT_GROUP_SKILL_BLACKBOX_UNSUPPORTED" and skill.value.status_code == 409
    with pytest.raises(AuditGroupError) as datasource:
        service.invoke_asset("local/alice", source_owner="local/main", asset_type="datasource", asset_id="private_ds")
    assert datasource.value.code == "AUDIT_GROUP_ASSET_NOT_INVOCABLE" and datasource.value.status_code == 400


def test_t4_workflow_never_resolves_caller_or_global_dependency(tmp_path):
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    from agent_py_agent.agent.audit_group.blackbox_invoke_service import (
        AuditGroupBlackboxInvokeService,
    )
    member(tmp_path)
    asset(tmp_path, "workflow", "missing_dependency", steps=[{"step_id": "one", "mode": "deterministic_script", "script_id": "caller_only"}])
    caller = tmp_path / "owners/local/alice/scripts/caller_only"
    caller.mkdir(parents=True)
    (caller / "script.json").write_text(json.dumps({"id": "caller_only", "enabled": True}))
    with pytest.raises(AuditGroupError) as missing:
        AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
            asset_type="workflow", asset_id="missing_dependency")
    assert missing.value.code == "ASSET_DEPENDENCY_MISSING"


def test_t2_revoke_during_execution_withholds_results(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    lifecycle = invocation_assets(tmp_path)
    unit = getattr(module, "IsolatedScriptUnit", None)
    assert unit is not None
    def execute(*_args, **_kwargs):
        lifecycle.set_audit_group_membership("alice", in_audit_group=False)
        return {"value": 17}
    monkeypatch.setattr(unit, "execute", execute)
    with pytest.raises(AuditGroupError, match="AUDIT_GROUP_ACCESS_DENIED"):
        module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
            asset_type="script", asset_id="source_script")
    assert not list((tmp_path / "owners/local/alice/data/audit_group_exec").rglob("result.json"))


def test_t2_tools_real_caller_and_snapshot_revocation(tmp_path):
    from agent_py_agent.agent.tooling.audit_group_tools import (
        AuditGroupInvokeAssetTool,
        AuditGroupQueryTool,
    )
    lifecycle = member(tmp_path)
    query = AuditGroupQueryTool(tmp_path, "local/alice")
    invoke = AuditGroupInvokeAssetTool(tmp_path, "local/alice")
    result = query.execute({"action": "owners"})
    assert result is not None and result.ok
    forged = query.execute({"action": "owners", "caller_owner": "local/main"})
    assert not forged.ok
    lifecycle.set_audit_group_membership("alice", in_audit_group=False)
    assert query.execute({"action": "owners"}).reported_error_code == "AUDIT_GROUP_ACCESS_DENIED"
    assert invoke.execute({"source_owner": "local/main", "asset_type": "script", "asset_id": "source_script"}).reported_error_code == "AUDIT_GROUP_ACCESS_DENIED"


def test_t2_registry_registers_tools_only_for_trusted_owner_binding(tmp_path):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
    member(tmp_path)
    home = tmp_path / "owners/local/alice"
    params = ToolRegistryParams(workspace_root=home / "workspace", max_chars=1000, max_entries=50, max_matches=50,
        web_max_chars=1000, http_timeout=10, catalog_limit=50, retrieval_limit=5, vector_search_enabled=False,
        protected_persona_root=str(home), owner_scope_root=str(home), operation_owner_id="local/alice",
        defer_mcp_connect=True, operation_store_required=False)
    registry = ToolRegistry(params)
    assert "audit_group_query" in registry.tools and "audit_group_invoke_asset" in registry.tools
    snapshot = registry.runtime_snapshot(allowed_tools=["audit_group_query"])
    assert snapshot.runtime("audit_group_query") is not None
    lifecycle = LocalUserLifecycle(LocalUserStore(tmp_path))
    lifecycle.set_audit_group_membership("alice", in_audit_group=False)
    assert snapshot.runtime("audit_group_query").handler.execute({"action": "owners"}).reported_error_code == "AUDIT_GROUP_ACCESS_DENIED"


def test_t3_malformed_revision_cannot_smuggle_private_dto_fields(tmp_path):
    from agent_py_agent.agent.audit_group.observation_service import AuditGroupObservationService
    member(tmp_path)
    asset(tmp_path, "script", "poisoned", revision={"code": "PRIVATE SOURCE", "path": "/Users/private"})
    result = AuditGroupObservationService(tmp_path).summarize_assets("local/alice", owner_ids=["local/main"])
    assert "PRIVATE" not in json.dumps(result) and "/Users/" not in json.dumps(result)
    assert result["coverage"] == "partial" and result["source_errors"]


def test_t4_quota_denial_before_execution_and_canonical_governance_postcheck(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    invocation_assets(tmp_path)
    quota = tmp_path / "owners/local/alice/quota.json"
    data = json.loads(quota.read_text())
    data["max_disk_mb"] = 1
    quota.write_text(json.dumps(data))
    with pytest.raises(AuditGroupError) as denied:
        module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
            asset_type="script", asset_id="source_script")
    assert denied.value.code == "AUDIT_GROUP_QUOTA_DENIED"
    data["max_disk_mb"] = 1024
    quota.write_text(json.dumps(data))
    from agent_py_agent.agent.user_space.managed_user_governance import ManagedUserGovernanceService
    def execute(*_args, **_kwargs):
        ManagedUserGovernanceService(tmp_path).set_policy("alice", {"role_title": "changed policy"})
        return {"value": 1}
    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", execute)
    with pytest.raises(AuditGroupError, match="GOVERNANCE_POLICY_CHANGED"):
        module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
            asset_type="script", asset_id="source_script")


def test_t3_output_values_cannot_echo_private_skill_body(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module
    invocation_assets(tmp_path)
    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", lambda *_a, **_k: {"value": "PRIVATE SKILL BODY"})
    result = module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
        asset_type="skill", asset_id="source_skill")
    assert "PRIVATE SKILL BODY" not in json.dumps(result)
    for path in (tmp_path / "owners/local/alice/data/audit_group_exec").rglob("*.json"):
        assert "PRIVATE SKILL BODY" not in path.read_text()


def test_t4_isolated_execution_boundary_is_read_only_except_output_streams(tmp_path):
    from agent_py_agent.agent.audit_group.isolated_unit import execution_sandbox
    workspace, staging = tmp_path / "workspace", tmp_path / "stage"
    workspace.mkdir()
    staging.mkdir()
    spec = execution_sandbox(workspace, staging).spec
    assert spec.extra_write_roots == (), "unreviewed script files could leak private source/secret strings"
    assert spec.implicit_attempt_write_roots is False and spec.network_access is False


def test_t4_actual_native_isolated_process(tmp_path):
    from agent_py_agent.agent.audit_group.isolated_unit import IsolatedScriptUnit
    workspace, staging = tmp_path / "workspace", tmp_path / "stage"
    workspace.mkdir()
    staging.mkdir()
    result = IsolatedScriptUnit().execute('import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"value":x["n"]+1}))',
        {"n": 16}, workspace=workspace, staging=staging, timeout_s=10)
    assert result == {"value": 17}
    assert not list(workspace.iterdir()) and not list(staging.iterdir())


def test_t4_self_source_is_rejected_without_writes(tmp_path):
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    from agent_py_agent.agent.audit_group.blackbox_invoke_service import (
        AuditGroupBlackboxInvokeService,
    )
    member(tmp_path)
    source = tmp_path / "owners/local/alice"
    before = inventory(source)
    with pytest.raises(AuditGroupError) as denied:
        AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/alice",
            asset_type="script", asset_id="self_script")
    assert denied.value.code == "AUDIT_GROUP_SELF_INVOCATION_UNSUPPORTED"
    assert inventory(source) == before


def test_t4_torn_source_metadata_code_is_rejected_before_execution(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    from agent_py_agent.agent.audit_group.readonly_store import ReadonlyAssetStore
    from agent_py_agent.agent.audit_group.source_snapshot import SourceAssetSnapshot
    invocation_assets(tmp_path)
    original, changed = ReadonlyAssetStore.text, []
    def replace_between_reads(reader, *parts):
        if parts[-1] == "main.py" and not changed:
            changed.append(True)
            path = tmp_path / "owners/local/main/scripts/source_script/script.json"
            raw = json.loads(path.read_text())
            raw["revision"] = 2
            path.write_text(json.dumps(raw))
        return original(reader, *parts)
    monkeypatch.setattr(ReadonlyAssetStore, "text", replace_between_reads)
    with pytest.raises(AuditGroupError, match="REVISION_CONFLICT"):
        SourceAssetSnapshot.freeze(tmp_path, "local/main", "script", "source_script")


def test_t2_workflow_stops_next_unit_after_revocation(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    service = invocation_assets(tmp_path)
    asset(tmp_path, "workflow", "two_steps", steps=[{"step_id": "first", "mode": "deterministic_script", "script_id": "source_script"},
        {"step_id": "second", "mode": "deterministic_script", "script_id": "source_script"}])
    calls = []
    def execute(*_args, **_kwargs):
        calls.append(True)
        service.set_audit_group_membership("alice", in_audit_group=False)
        return {"value": 1}
    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", execute)
    with pytest.raises(AuditGroupError, match="AUDIT_GROUP_ACCESS_DENIED"):
        module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
            asset_type="workflow", asset_id="two_steps")
    assert calls == [True]


def test_t1_create_can_set_initial_membership_and_actor_is_recorded(tmp_path):
    service = LocalUserLifecycle(LocalUserStore(tmp_path))
    created = service.create("alice", idempotency_key="create_member_0001", in_audit_group=True)
    assert created.user.in_audit_group is True
    assert created.user.audit_group_updated_by == "local/main"
    assert created.user.audit_group_updated_at


def test_t7_alerts_filter_uses_actual_time_and_never_raw_run_outputs(tmp_path):
    from agent_py_agent.agent.audit_group.observation_service import AuditGroupObservationService
    member(tmp_path)
    asset(tmp_path, "script", "source_script")
    store = tmp_path / "owners/local/main/data/scheduler"
    store.mkdir(parents=True)
    (store / "store.json").write_text(json.dumps({"jobs": {}, "runs": {"run1": {
        "run_id": "run1", "job_id": "monitor1", "status": "failed", "finished_at": 1793022300,
        "error_code": "READ_FAILED", "error_message": "PRIVATE /Users/private api_key=synthetic-secret",
        "response": "PRIVATE TOOL OUTPUT", "job_snapshot": {"prompt": "PRIVATE PROMPT"}}}}))
    service = AuditGroupObservationService(tmp_path)
    rows = service.list_alerts("local/alice", owner_ids=["local/main"], severity="error", status="open",
                               since="2026-10-26T14:30:00+02:00")
    assert len(rows["items"]) == 1
    assert "PRIVATE" not in json.dumps(rows) and "synthetic-secret" not in json.dumps(rows)
    assert service.list_alerts("local/alice", owner_ids=["local/main"], since="2026-10-26T08:00:00-07:00")["items"] == []


def test_t3_skill_directory_with_readme_remains_complete(tmp_path):
    from agent_py_agent.agent.audit_group.observation_service import AuditGroupObservationService
    invocation_assets(tmp_path)
    (tmp_path / "owners/local/main/skills/README.md").write_text("普通目录说明，不是技能。")
    result = AuditGroupObservationService(tmp_path).summarize_assets("local/alice", owner_ids=["local/main"], kinds=["skill"])
    assert result["coverage"] == "complete"
    assert [row["id"] for row in result["items"]] == ["source_skill"]


def test_t5_source_admin_disable_prevents_execution(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    invocation_assets(tmp_path)
    policy = tmp_path / "owners/local/main/tool_policy.json"
    policy.write_text(json.dumps({"schema_version": "tool-policy.v1", "admin_controls": {
        "schema": "owner_admin_controls.v1", "disabled": True}}))
    calls = []
    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", lambda *_a, **_k: calls.append(True) or {"value": 1})
    with pytest.raises(AuditGroupError) as denied:
        module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
            asset_type="script", asset_id="source_script")
    assert denied.value.code == "ASSET_DISABLED_BY_OWNER" and denied.value.status_code == 409
    assert not calls


def test_t4_native_sandbox_refusal_fails_closed_without_source_changes(tmp_path, monkeypatch):
    from agent_py_agent.agent.attempt.sandbox import SandboxUnavailableError
    from agent_py_agent.agent.audit_group import isolated_unit
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    from agent_py_agent.agent.audit_group.blackbox_invoke_service import (
        AuditGroupBlackboxInvokeService,
    )
    invocation_assets(tmp_path)
    source = tmp_path / "owners/local/main"
    before = inventory(source)
    def refuse(*_args, **_kwargs):
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: synthetic refusal")
    monkeypatch.setattr(isolated_unit.AttemptExecutionSandbox, "build_argv", refuse)
    with pytest.raises(AuditGroupError) as denied:
        AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
            asset_type="script", asset_id="source_script")
    assert denied.value.code == "AUDIT_GROUP_SANDBOX_UNAVAILABLE" and denied.value.status_code == 503
    assert inventory(source) == before
    assert not list((tmp_path / "owners/local/alice/data/audit_group_exec").rglob("result.json"))


def test_t2_malformed_caller_policy_is_closed_domain_error(tmp_path):
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    from agent_py_agent.agent.audit_group.blackbox_invoke_service import (
        AuditGroupBlackboxInvokeService,
    )
    invocation_assets(tmp_path)
    (tmp_path / "owners/local/alice/tool_policy.json").write_text('{"admin_controls":true}')
    with pytest.raises(AuditGroupError) as denied:
        AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice", source_owner="local/main",
            asset_type="script", asset_id="source_script")
    assert denied.value.status_code == 503


# LLM: 仅模拟build成功且已有外层Seatbelt这一分支，仍启动真实子进程；这不代替真实原生require_ready验收。
# 函数用途: 验证省内层包装时，匿名bootstrap仍正常计算并阻断写入、网络与子进程。
@pytest.mark.parametrize("operation", ["calculate", "write", "network", "subprocess"])
def test_outer_seatbelt_successful_build_keeps_python_readonly_guard(tmp_path, monkeypatch, operation):
    from agent_py_agent.agent.audit_group import isolated_unit as module
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    workspace, staging = tmp_path / "workspace", tmp_path / "stage"
    workspace.mkdir()
    staging.mkdir()
    builds = []

    # LLM: 替代点只固定成功的包装参数；原生拒绝回归仍使用另一个独立测试，不吞异常。
    # 函数用途: 记录执行单元必定先经过build_argv。
    def successful_build(_sandbox, base):
        builds.append(tuple(base))
        return ["/usr/bin/sandbox-exec", "-p", "fixture", *base]

    monkeypatch.setattr(module.AttemptExecutionSandbox, "build_argv", successful_build)
    monkeypatch.setattr(module, "_outer_seatbelt_active", lambda: True)
    code = {"calculate": 'import json,sys\nx=json.load(sys.stdin)\nprint(json.dumps({"value":x["n"]+1}))',
            "write": 'open("forbidden.txt","w").write("secret")',
            "network": 'import socket\nsocket.socket()',
            "subprocess": 'import subprocess\nsubprocess.run(["/usr/bin/true"])'}[operation]
    if operation == "calculate":
        assert module.IsolatedScriptUnit().execute(code, {"n": 16}, workspace=workspace,
                                                   staging=staging, timeout_s=10) == {"value": 17}
    else:
        with pytest.raises(AuditGroupError, match="AUDIT_GROUP_EXECUTION_FAILED"):
            module.IsolatedScriptUnit().execute(code, {}, workspace=workspace, staging=staging, timeout_s=10)
    assert len(builds) == 1
    assert not list(workspace.iterdir()) and not list(staging.iterdir())


# LLM: 字符串、bytes与PathLike共享同一allowlist；合法路径不能被类型差异误拒绝，越界路径仍拒绝。
# 函数用途: 回归独立复核发现的Python审计钩子路径类型不一致。
@pytest.mark.parametrize("path_kind", ["str", "bytes", "pathlike"])
@pytest.mark.parametrize("inside", [True, False])
def test_python_read_guard_keeps_path_types_and_read_boundary(tmp_path, monkeypatch, path_kind, inside):
    from agent_py_agent.agent.audit_group import isolated_unit as module
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    workspace, staging = tmp_path / "workspace", tmp_path / "stage"
    workspace.mkdir()
    staging.mkdir()
    target = workspace / "fixture.txt" if inside else tmp_path / "fixture.txt"
    target.write_text("abc")
    monkeypatch.setattr(module.AttemptExecutionSandbox, "build_argv",
                        lambda _sandbox, base: ["/usr/bin/sandbox-exec", "-p", "fixture", *base])
    monkeypatch.setattr(module, "_outer_seatbelt_active", lambda: True)
    path = "fixture.txt" if inside else "../fixture.txt"
    value = repr(path.encode()) if path_kind != "str" else repr(path)
    prefix = f"class Location:\n def __fspath__(self): return {value}\n" if path_kind == "pathlike" else ""
    value = "Location()" if path_kind == "pathlike" else value
    code = f'import json\n{prefix}with open({value}, "rb") as handle:\n print(json.dumps({{"size": len(handle.read())}}))'
    if inside:
        assert module.IsolatedScriptUnit().execute(code, {}, workspace=workspace,
                                                   staging=staging, timeout_s=10) == {"size": 3}
    else:
        with pytest.raises(AuditGroupError, match="AUDIT_GROUP_EXECUTION_FAILED"):
            module.IsolatedScriptUnit().execute(code, {}, workspace=workspace, staging=staging, timeout_s=10)
    assert target.read_text() == "abc" and not list(staging.iterdir())


@pytest.mark.parametrize("field", ["password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "access_key", "secret_key", "private_key", "credential", "cookie", "session", "authorization",
    "bearer", "webhook", "ak", "sk", "clientSecret", "refresh_token"])
def test_r2b_summary_redacts_sensitive_schema_literals_through_nested_nodes(tmp_path, field):
    from agent_py_agent.agent.audit_group.observation_service import AuditGroupObservationService
    member(tmp_path)
    secret_node = {"type": "string", "default": "PlainPass1", "examples": ["Short2"],
                   "enum": ["Short3"], "const": "Short4"}
    schema = {"type": "object", "properties": {
        field: {**secret_node, "items": secret_node, "anyOf": [secret_node],
                "additionalProperties": secret_node, "properties": {"nested": secret_node}},
        "count": {"type": "integer", "default": 3, "enum": [1, 3]}}}
    asset(tmp_path, "script", "sensitive-schema", parameters_schema=schema)
    page = AuditGroupObservationService(tmp_path).summarize_assets("local/alice", owner_ids=["local/main"])
    assert page["coverage"] == "complete"
    projected = page["items"][0]["parameters_schema"]
    assert projected["properties"]["count"]["default"] == 3
    assert all(value not in json.dumps(projected) for value in ("PlainPass1", "Short2", "Short3", "Short4"))


def test_r2b_host_invoke_selects_monitor_rule(wired_host, monkeypatch):
    from agent_py_agent.agent.audit_group.blackbox_invoke_service import IsolatedScriptUnit
    caller = host_member(wired_host)
    root = wired_host[2].root
    asset(root, "script", "source_script")
    scheduler = root / "owners/local/main/data/scheduler"
    scheduler.mkdir(parents=True, exist_ok=True)
    rules = [{"id": name, "enabled": True, "use_llm": False,
              "tool": {"name": "scripts", "arguments": {"action": "run", "script_id": "source_script"}}}
             for name in ("r1", "r2")]
    (scheduler / "store.json").write_text(json.dumps({"jobs": {"multi": {
        "job_id": "multi", "status": "active", "version": 1, "rules": rules}}, "runs": {}}))
    monkeypatch.setattr(IsolatedScriptUnit, "execute", lambda *_a, **_k: {"value": 17})
    result = business_call(wired_host, "audit_group.assets.invoke", {
        "source_owner_id": "local/main", "kind": "monitor", "asset_id": "multi", "rule_id": "r2"},
        key="r2b_monitor_invoke01", caller=caller)
    assert result.http_status == 200 and result.data["result"] == {"value": 17}


def test_r2b_generated_artifacts_are_sanitized_caller_owned_and_hashed(tmp_path, monkeypatch):
    from hashlib import sha256

    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module
    invocation_assets(tmp_path)
    home = tmp_path / "owners/local/alice"
    workspace = home / "workspace"
    (workspace / "output").mkdir(parents=True)
    (workspace / "output/old.txt").write_text("not generated by this call")

    def execute(_unit, _code, _inputs, *, workspace, **_kwargs):
        (workspace / "output").mkdir(exist_ok=True)
        (workspace / "output/PRIVATE SKILL BODY.txt").write_text("PRIVATE SKILL BODY api_key=synthetic-secret")
        (workspace / "output/escape").symlink_to(tmp_path / "owners/local/main/scripts/source_script/main.py")
        return {"value": 17}

    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", execute)
    result = module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice",
        source_owner="local/main", asset_type="skill", asset_id="source_skill")
    artifacts = result["generated_artifacts"]
    assert len(artifacts) == 1
    row = artifacts[0]
    assert row["relative_path"].startswith("data/audit_group_exec/" + result["invocation_id"] + "/")
    published = home / row["relative_path"]
    assert published.is_file() and row["size_bytes"] == published.stat().st_size
    assert row["sha256"] == sha256(published.read_bytes()).hexdigest()
    assert "PRIVATE SKILL BODY" not in published.read_text() + json.dumps(result)
    assert "synthetic-secret" not in published.read_text() + json.dumps(result)
    assert "old.txt" not in json.dumps(artifacts) and "escape" not in json.dumps(artifacts)


def test_r2b_generated_artifact_quota_is_rechecked_after_execution(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    invocation_assets(tmp_path)
    home = tmp_path / "owners/local/alice"

    def execute(_unit, _code, _inputs, *, workspace, **_kwargs):
        (workspace / "result.txt").write_text("output")
        quota = json.loads((home / "quota.json").read_text())
        quota["max_disk_mb"] = 1
        (home / "quota.json").write_text(json.dumps(quota))
        return {"value": 17}

    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", execute)
    with pytest.raises(AuditGroupError, match="AUDIT_GROUP_QUOTA_DENIED"):
        module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice",
            source_owner="local/main", asset_type="script", asset_id="source_script")
    assert not list((home / "data/audit_group_exec").rglob("result.json"))


def test_r2b_concurrent_caller_workspace_changes_are_not_invocation_artifacts(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module
    invocation_assets(tmp_path)
    workspace = tmp_path / "owners/local/alice/workspace"
    workspace.mkdir(exist_ok=True)

    def execute(*_args, **_kwargs):
        (workspace / "private-note.txt").write_text("personal business notes")
        return {"value": 17}

    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", execute)
    result = module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice",
        source_owner="local/main", asset_type="script", asset_id="source_script")
    assert result["generated_artifacts"] == []
    assert (workspace / "private-note.txt").read_text() == "personal business notes"


def test_r2b_invalid_artifact_batch_never_publishes_partial_files(tmp_path, monkeypatch):
    from agent_py_agent.agent.audit_group import blackbox_invoke_service as module
    from agent_py_agent.agent.audit_group.access_policy import AuditGroupError
    invocation_assets(tmp_path)
    home = tmp_path / "owners/local/alice"

    def execute(_unit, _code, _inputs, *, workspace, **_kwargs):
        (workspace / "a.txt").write_text("safe first file")
        (workspace / "z.bin").write_bytes(b"\xff\xfe")
        return {"value": 17}

    monkeypatch.setattr(module.IsolatedScriptUnit, "execute", execute)
    with pytest.raises(AuditGroupError, match="AUDIT_GROUP_ARTIFACT_FORMAT_UNSUPPORTED"):
        module.AuditGroupBlackboxInvokeService(tmp_path).invoke_asset("local/alice",
            source_owner="local/main", asset_type="script", asset_id="source_script")
    assert not list((home / "data/audit_group_exec").rglob("artifact-*.txt"))
