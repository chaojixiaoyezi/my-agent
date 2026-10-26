# LLM: 只用真实隔离宿主目录；范围、权威存储及原领域流水均在临时根验证。
# 模块用途: 验证受管用户治理、人格锁、通用审核与跨用户派单隔离。
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.capability.persona_repository import (
    PersonaMutationRequest,
    PersonaRepository,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.managed_user_governance import (
    ManagedUserGovernanceService,
    ManagedUserScopeDeniedError,
    resolve_managed_local_user,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


@pytest.fixture
def managed(tmp_path):
    root = tmp_path / "host"
    LocalUserLifecycle(LocalUserStore(root)).create("alice", idempotency_key="managed_alice_0001")
    return root, ManagedUserGovernanceService(root), LocalUserStore(root).owner_home("alice")


@pytest.mark.parametrize("target", ["main", "local/main", "providers/feishu/users/ou_x", "nobody", "../alice", "local/bob/../alice", "local/users/main"])
def test_scope_rejects_main_provider_unknown_and_paths_without_writes(managed, target):
    root, _service, _home = managed
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    with pytest.raises(ManagedUserScopeDeniedError) as caught:
        resolve_managed_local_user(root, target)
    assert caught.value.code == "MANAGED_USER_SCOPE_DENIED"
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before


@pytest.mark.parametrize("target", ["alice", "local/alice", "local/users/alice"])
def test_scope_uses_registered_canonical_local_home(managed, target):
    root, _service, home = managed
    result = resolve_managed_local_user(root, target)
    assert result is not None
    uid, identity, resolved = result
    assert uid == "alice" and identity == OwnerIdentity.local_user("alice")
    assert resolved.home_dir == home.home_dir == root / "owners/local/alice"


def test_non_admin_cannot_construct_cross_owner_authority(managed):
    root, _service, _home = managed
    service = ManagedUserGovernanceService(root, actor_owner=OwnerIdentity.local_user("alice"))
    with pytest.raises(ManagedUserScopeDeniedError):
        service.get_overview("alice")


def test_policy_authority_cas_and_untrusted_mirror(managed):
    root, service, home = managed
    policy = service.set_policy("alice", {"persona_mode": "admin_locked"}, expected_revision=0)
    assert policy is not None
    assert policy["revision"] == 1 and policy["owner_id"] == "local/alice"
    authority = root / "data/managed_user_governance/alice/governance_policy.json"
    assert json.loads(authority.read_text()) == policy
    mirror = home.home_dir / "config/governance_policy.json"
    assert json.loads(mirror.read_text()) == policy
    mirror.write_text('{"persona_mode":"user_autonomous","revision":999}')
    assert service.get_overview("local/alice")["governance_policy"] == policy
    with pytest.raises(HostAPIError, match="REVISION_CONFLICT"):
        service.set_policy("alice", {"persona_mode": "user_autonomous"}, expected_revision=0)
    assert json.loads(authority.read_text()) == policy


def test_policy_cas_serializes_concurrent_writers(managed):
    _root, service, _home = managed

    def write(_index):
        try:
            return service.set_policy("alice", {"persona_mode": "admin_locked"}, expected_revision=0)
        except HostAPIError as error:
            assert error.code == "REVISION_CONFLICT"
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))
    assert sum(value is not None for value in results) == 1


@pytest.mark.parametrize("patch", [{"persona_mode": "free"}, {"review_required_for": {"persona_changes": 1}}, {"revision": 900}, {"execution_governance": {"allow_self_created_scripts": "yes"}}])
def test_invalid_policy_is_rejected_before_any_authority_write(managed, patch):
    root, service, _home = managed
    with pytest.raises((ValueError, HostAPIError)):
        service.set_policy("alice", patch)
    assert not (root / "data/managed_user_governance/alice/governance_policy.json").exists()


@pytest.mark.parametrize("target", ["soul", "agents"])
def test_locked_persona_cannot_be_mutated_even_with_confirmed_request(managed, target):
    _root, service, home = managed
    policy = service.set_policy("alice", {"persona_mode": "admin_locked", "review_required_for": {"persona_changes": False}})
    assert policy is not None
    repo = PersonaRepository(owner_home=home.home_dir, soul_path=home.soul_md, user_path=home.user_md, agents_path=home.agents_md)
    with pytest.raises(HostAPIError, match="PERSONA_LOCKED_BY_ADMIN"):
        repo.mutate(PersonaMutationRequest(target=target, action="add", content="user replacement", confirmed=True, source="user_direct"))


@pytest.mark.parametrize("method,options", [
    ("get_persona", {}), ("set_persona", {"soul_md": "x"}), ("list_memories", {}),
    ("mutate_memories", {"operation": "add_memory", "content": "x"}),
    ("provision_assets", {"asset_kind": "script", "asset_id": "demo"}),
    ("toggle_asset", {"asset_kind": "script", "asset_id": "demo", "enabled": True}),
    ("dispatch_task", {"prompt": "x"}), ("list_tasks", {}),
])
def test_every_domain_denies_provider_targets(managed, method, options):
    _root, service, _home = managed
    with pytest.raises(ManagedUserScopeDeniedError):
        getattr(service, method)("providers/feishu/users/ou_x", **options)


def test_admin_persona_base_cas_and_user_mirror_cannot_override(managed):
    _root, service, home = managed
    result = service.set_persona("alice", soul_md="ADMIN SOUL", agents_md="ADMIN RULES", user_md="profile",
                                 persona_mode="admin_base_plus_user", expected_revision=0, idempotency_key="set-persona-1")
    assert result is not None
    assert result["revision"] == 1
    repeated = service.set_persona("alice", soul_md="ADMIN SOUL", agents_md="ADMIN RULES", user_md="profile",
                                   persona_mode="admin_base_plus_user", expected_revision=0, idempotency_key="set-persona-1")
    assert repeated["revision"] == 1
    home.soul_md.write_text("user tail")
    (home.home_dir / "memory/SOUL_ADMIN_BASE.md").write_text("forged base")
    repo = PersonaRepository.from_owner_result(home)
    assert repo.load("soul").content == "ADMIN SOUL\n\nuser tail"
    assert repo.load("agents").content.startswith("ADMIN RULES\n\n")
    assert service.get_persona("alice")["USER.md"] == "profile"
    with pytest.raises(HostAPIError, match="REVISION_CONFLICT"):
        service.set_persona("alice", soul_md="wrong", expected_revision=0)


@pytest.mark.parametrize("operation", ["set_source", "write_text"])
def test_native_agents_source_cannot_bypass_admin_lock(managed, operation):
    from types import SimpleNamespace

    from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository

    root, service, home = managed
    service.set_policy("alice", {"persona_mode": "admin_locked", "review_required_for": {"persona_changes": False}})
    repo = AgentsSourceRepository(owner="local/alice", owner_home=home.home_dir,
                                  path_policy=SimpleNamespace(agent_home_root=root))
    with pytest.raises(HostAPIError, match="PERSONA_LOCKED_BY_ADMIN"):
        if operation == "set_source":
            repo.set_source("owner_default", str(home.agents_md), authorization=None, context=None)
        else:
            repo.write_text("replacement", authorization=None, context=None)


def test_memory_injection_edit_delete_uses_formal_owner_store(managed):
    _root, service, _home = managed
    added = service.mutate_memories("alice", "add_memory", content="Owner fact", locked=True)
    assert added is not None
    entry = added["entry"]
    assert entry["content"] == "Owner fact"
    listed = service.list_memories("alice")
    assert any(row["entry_id"] == entry["entry_id"] for row in listed["memories"])
    assert entry["entry_id"] in service.get_overview("alice")["governance_policy"]["memory_governance"]["admin_locked_memory_ids"]
    changed = service.mutate_memories("alice", "edit_memory", entry_id=entry["entry_id"], content="Correct fact", expected_revision=entry["version"])
    assert changed["entry"]["content"] == "Correct fact"
    service.mutate_memories("alice", "delete_memory", entry_id=entry["entry_id"], expected_revision=changed["entry"]["version"])
    assert not service.list_memories("alice")["memories"]


def test_asset_provision_installs_workflow_script_closure_and_toggles(managed):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.assets.workflow_store import (
        WorkflowDefinition,
        WorkflowStepSpec,
        WorkflowStore,
    )

    root, service, home = managed
    source = root / "owners/local/main"
    ScriptStore(source).save_script(AssetDescriptor("source-code", "script", "Source code"), "print('hello')")
    definition = WorkflowDefinition(AssetDescriptor("work", "workflow", "Work"),
        (WorkflowStepSpec("step", "Step", "deterministic_script", script_id="source-code"),))
    WorkflowStore(source).save_workflow(definition)
    delivered = service.provision_assets("alice", "workflow", "work", auto_enable=False)
    assert delivered is not None
    target = WorkflowStore(home.home_dir).get_workflow("work")
    assert target is not None and target.descriptor.metadata["enabled"] is False
    assert target.descriptor.metadata["delivery_provenance"]["provisioned_by_admin"] is True
    assert ScriptStore(home.home_dir).get_script("source-code")["code"] == "print('hello')"
    service.toggle_asset("alice", "workflow", "work", True)
    assert WorkflowStore(home.home_dir).get_workflow("work").descriptor.metadata["enabled"] is True


def test_dispatch_targets_own_conversation_and_queue_not_admin(managed):
    from agent_py_agent.agent.conversation.store import ConversationStore

    root, service, home = managed
    dispatched = service.dispatch_task("alice", "Do local work", title="Assigned work")
    assert dispatched is not None
    assert dispatched["status"] == "queued" and dispatched["target_owner_id"] == "local/alice"
    store = ConversationStore(home.home_dir / "conversations", initialize=False)
    thread = store.threads.require(dispatched["thread_id"])
    assert thread.owner_id == "local/alice" and thread.owner_home == str(home.home_dir)
    message = store.messages.recent(thread.thread_id)[-1]
    assert message.content == "Do local work" and message.metadata["dispatched_by"] == "local/main"
    assert any(wake.root_task_id == dispatched["task_id"] for wake in store.wakes.pending())
    assert not (root / "owners/local/main/conversations").exists()
    assert any(row["task_id"] == dispatched["task_id"] for row in service.list_tasks("alice")["tasks"])


@pytest.mark.parametrize("review_type", ["capability_elevation", "high_risk_command", "asset_enable", "schedule_create", "memory_promotion", "persona_change"])
def test_review_submission_has_canonical_authority_and_read_only_mirror(managed, review_type):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, _service, home = managed
    store = GovernanceReviewStore(root)
    item = store.submit_review("local/alice", review_type, {"operation": "example"})
    assert item is not None and item.status == "pending_admin_review"
    authority = root / "data/managed_user_governance/alice/governance_reviews" / (item.review_id + ".json")
    mirror = home.home_dir / "data/governance_reviews" / (item.review_id + ".json")
    assert json.loads(authority.read_text())["status"] == "pending_admin_review"
    mirror.write_text('{"status":"approved","revision":999}')
    listed = store.list_reviews("alice", status_filter="pending_admin_review")
    assert listed["items"][0]["review_id"] == item.review_id
    assert listed["items"][0]["revision"] == 1


def test_review_rejection_requires_feedback_cas_and_notifies_applicant(managed):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, _service, home = managed
    conversation = ConversationStore(home.home_dir / "conversations")
    thread = conversation.threads.create(owner_id="local/alice", owner_home=str(home.home_dir))
    reviews = GovernanceReviewStore(root)
    item = reviews.submit_review("alice", "high_risk_command", {"command": "example"}, applicant_thread_id=thread.thread_id)
    assert item is not None
    with pytest.raises(HostAPIError, match="REVIEW_FEEDBACK_REQUIRED"):
        reviews.decide_review(item.review_id, "reject_with_feedback", expected_revision=1)
    result = reviews.decide_review(item.review_id, "reject_with_feedback", "Use a read-only command", expected_revision=1, target_owner_id="alice")
    assert result["status"] == "rejected" and result["feedback"] == "Use a read-only command"
    assert conversation.messages.recent(thread.thread_id)[-1].metadata["governance_review"]["review_id"] == item.review_id
    assert conversation.wakes.pending()
    with pytest.raises(HostAPIError, match="REVISION_CONFLICT"):
        reviews.decide_review(item.review_id, "approve", expected_revision=1)


def test_review_approval_applies_persona_and_rejects_target_mismatch(managed):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, service, home = managed
    store = GovernanceReviewStore(root)
    item = store.submit_review("alice", "persona_change", {"target": "soul", "action": "add", "content": "approved rule"})
    assert item is not None
    with pytest.raises(ManagedUserScopeDeniedError):
        store.decide_review(item.review_id, "approve", expected_revision=1, target_owner_id="local/main")
    result = store.decide_review(item.review_id, "approve", expected_revision=1, target_owner_id="alice")
    assert result["status"] == "applied"
    assert "approved rule" in home.soul_md.read_text()
    assert service.get_persona("alice")["SOUL.md"].endswith("approved rule\n")


def test_admin_tool_declares_twelve_governance_actions():
    from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool

    actions = set(AdminControlsTool.model_spec.input_schema["properties"]["action"]["enum"])
    assert {"user_overview", "set_governance_policy", "get_user_persona", "set_user_persona", "list_user_memories", "mutate_user_memory",
            "provision_user_assets", "toggle_user_asset", "dispatch_user_task", "list_user_tasks", "list_governance_reviews", "decide_governance_review"} <= actions


def test_review_persona_cas_binds_computation_snapshot_not_late_revision(managed, monkeypatch):
    import agent_py_agent.agent.capability.persona_repository as persona_module
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, service, home = managed
    service.set_persona("alice", soul_md="initial", expected_revision=0)
    reviews = GovernanceReviewStore(root)
    item = reviews.submit_review("alice", "persona_change", {"target": "soul", "action": "add", "content": "review rule"})
    original = persona_module._apply_operation_to_text
    committed = False

    def interleave(*args, **kwargs):
        nonlocal committed
        result = original(*args, **kwargs)
        if not committed:
            committed = True
            service.set_persona("alice", soul_md="concurrent admin", expected_revision=1)
        return result

    monkeypatch.setattr(persona_module, "_apply_operation_to_text", interleave)
    decided = reviews.decide_review(item.review_id, "approve", expected_revision=1)
    assert decided["status"] == "failed" and decided["result"]["error_code"] == "REVISION_CONFLICT"
    assert home.soul_md.read_text() == "concurrent admin"


def test_persona_partial_write_is_fenced_and_same_intent_resumes(managed, monkeypatch):
    import agent_py_agent.agent.user_space.managed_user_governance as module

    _root, service, home = managed
    service.set_persona("alice", soul_md="old soul", agents_md="old agents", expected_revision=0)
    original = module.write_bytes_atomic_beneath
    fail = True

    def injected(root, parts, data, **kwargs):
        if fail and tuple(parts) == ("owners", "local", "alice", "AGENTS.md"):
            raise OSError("injected later-file failure")
        return original(root, parts, data, **kwargs)

    monkeypatch.setattr(module, "write_bytes_atomic_beneath", injected)
    with pytest.raises(HostAPIError, match="STORE_UNAVAILABLE"):
        service.set_persona("alice", soul_md="new soul", agents_md="new agents", expected_revision=1, idempotency_key="fault-resume")
    with pytest.raises(HostAPIError, match="GOVERNANCE_OPERATION_INCOMPLETE"):
        service.get_persona("alice")
    fail = False
    result = service.set_persona("alice", soul_md="new soul", agents_md="new agents", expected_revision=1, idempotency_key="fault-resume")
    assert result["revision"] == 2
    assert home.soul_md.read_text() == "new soul" and home.agents_md.read_text() == "new agents"
    with pytest.raises(HostAPIError, match="REVISION_CONFLICT"):
        service.set_persona("alice", soul_md="stale", expected_revision=1)


def test_persona_read_snapshot_waits_for_whole_admin_transaction(managed, monkeypatch):
    from concurrent.futures import TimeoutError
    from threading import Event

    import agent_py_agent.agent.user_space.managed_user_governance as module

    _root, service, _home = managed
    service.set_persona("alice", soul_md="old soul", agents_md="old agents", expected_revision=0)
    original = module.write_bytes_atomic_beneath
    staged, release = Event(), Event()

    def pause(root, parts, data, **kwargs):
        result = original(root, parts, data, **kwargs)
        if tuple(parts) == ("owners", "local", "alice", "SOUL.md"):
            staged.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(module, "write_bytes_atomic_beneath", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(service.set_persona, "alice", soul_md="new soul", agents_md="new agents", expected_revision=1)
        try:
            assert staged.wait(5)
            reader = pool.submit(service.get_persona, "alice")
            with pytest.raises(TimeoutError):
                reader.result(timeout=0.1)
        finally:
            release.set()
        writer.result(timeout=5)
        view = reader.result(timeout=5)
    assert view["revision"] == 2 and view["SOUL.md"] == "new soul" and view["AGENTS.md"] == "new agents"


def test_overview_aggregates_real_owner_counts_and_account_controls(managed):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, service, _home = managed
    service.mutate_memories("alice", "add_memory", content="a counted fact")
    GovernanceReviewStore(root).submit_review("alice", "high_risk_command", {"command": "example"})
    overview = service.get_overview("alice")
    assert overview["disk_usage_bytes"] > 0
    assert overview["memory_counts"]["memories"] == 1
    assert overview["pending_governance_reviews_count"] == 1
    assert set(overview["asset_counts"]) == {"workflows", "monitors", "scripts", "skills"}
    assert "decision_model_allowed" in overview["controls"]
    assert "active_recent_count" in overview["task_counts"]


@pytest.mark.parametrize("action", ["user_overview", "get_user_persona", "list_user_memories", "list_user_tasks", "list_governance_reviews"])
def test_admin_tool_routes_real_read_services(managed, action):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    root, _service, _home = managed
    admin_home = home_paths_with_owner(home_paths(root), resolve_owner_home(root, OwnerIdentity.local_main()))
    tool = AdminControlsTool(SimpleNamespace(home_paths=admin_home))
    result = tool.execute({"action": action, "target_owner_id": "local/alice"})
    assert result.ok, result.output
    assert isinstance(json.loads(result.output), dict)


def test_admin_tool_preserves_persona_cas_and_denies_ordinary_actor(managed):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    root, service, home = managed
    admin_home = home_paths_with_owner(home_paths(root), resolve_owner_home(root, OwnerIdentity.local_main()))
    admin = AdminControlsTool(SimpleNamespace(home_paths=admin_home))
    saved = admin.execute({"action": "set_user_persona", "target_owner_id": "alice", "soul_md": "tool persona", "expected_revision": 0})
    assert saved.ok and json.loads(saved.output)["revision"] == 1
    stale = admin.execute({"action": "set_user_persona", "target_owner_id": "alice", "soul_md": "stale", "expected_revision": 0})
    assert not stale.ok and stale.reported_error_code == "REVISION_CONFLICT"
    assert json.loads(stale.output)["error_code"] == "REVISION_CONFLICT"
    ordinary = AdminControlsTool(SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), home)))
    denied = ordinary.execute({"action": "get_user_persona", "target_owner_id": "alice"})
    assert not denied.ok and denied.error_code == "ADMIN_CONTROL_DENIED"
    assert service.get_persona("alice")["SOUL.md"] == "tool persona"
