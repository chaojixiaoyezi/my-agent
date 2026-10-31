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


@pytest.mark.parametrize("kind", ["script", "workflow"])
def test_stage3_provision_and_toggle_drive_canonical_execution_gates(managed, kind):
    from agent_py_agent.agent.assets.delivery_contracts import AssetDisabledError
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore
    from agent_py_agent.agent.workflows import ScriptRunner, WorkflowEngine

    root, service, home = managed
    source = root / "owners/local/main"
    if kind == "script":
        ScriptStore(source).save_script(AssetDescriptor("gateasset", kind, "Gate asset"), 'print("{}")')
    else:
        WorkflowStore(source).save_workflow(WorkflowDefinition(AssetDescriptor("gateasset", kind, "Gate asset"), ()))
    service.provision_assets("alice", kind, "gateasset", auto_enable=False)
    runner = ScriptRunner(home.home_dir, use_sandbox=False) if kind == "script" else WorkflowEngine(home.home_dir)
    for step, enabled in enumerate((False, True, False)):
        if step:
            service.toggle_asset("alice", kind, "gateasset", enabled)
        if kind == "script":
            descriptor = ScriptStore(home.home_dir).get_script("gateasset")
            assert descriptor["enabled"] is enabled
            assert descriptor["metadata"]["enabled"] is enabled
            result = runner.run_script("gateasset")
            assert result.ok is enabled
            assert result.error_code == ("" if enabled else "ASSET_DISABLED_BY_OWNER")
        else:
            definition = WorkflowStore(home.home_dir).get_workflow("gateasset")
            assert definition.enabled is enabled and definition.descriptor.enabled is enabled
            assert definition.descriptor.metadata["enabled"] is enabled
            if enabled:
                assert runner.run_workflow("gateasset").status == "succeeded"
            else:
                with pytest.raises(AssetDisabledError) as caught:
                    runner.run_workflow("gateasset")
                assert caught.value.error_code == "ASSET_DISABLED_BY_OWNER"


def test_stage3_skill_gate_rechecks_canonical_sidecar_even_for_old_snapshot(managed):
    from hashlib import sha256

    from agent_py_agent.agent.capability.skill_snapshot import (
        SkillSnapshot,
        SkillSnapshotEntry,
        SkillSnapshotError,
    )
    from agent_py_agent.agent.capability.skills import skill_enabled

    root, service, home = managed
    body = "---\nname: gateskill\ndescription: Local test skill\n---\n# Local test\nRead local input.\n"
    source = root / "owners/local/main/skills/gateskill/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text(body)
    service.provision_assets("alice", "skill", "gateskill", auto_enable=False)
    path = home.home_dir / "skills/gateskill/SKILL.md"
    entry = SkillSnapshotEntry("owner:gateskill", "gateskill", "Local test skill", str(path), "owner", "test", sha256(body.encode()).hexdigest())
    snapshot = SkillSnapshot((entry,), (), "test", "local/alice", str(home.workspace_dir))
    for step, enabled in enumerate((False, True, False)):
        if step:
            service.toggle_asset("alice", "skill", "gateskill", enabled)
        assert skill_enabled(path) is enabled
        if enabled:
            assert snapshot.read_body("owner:gateskill") == body
        else:
            with pytest.raises(SkillSnapshotError) as caught:
                snapshot.read_body("owner:gateskill")
            assert caught.value.error_code == "ASSET_DISABLED_BY_OWNER"


def test_stage3_monitor_enabled_and_status_persist_together(managed):
    from agent_py_agent.agent.user_space.managed_user_governance import _managed_scheduler

    _root, service, home = managed
    payload = {"name": "Gate monitor", "prompt": "Inspect local input", "schedule": {"kind": "every", "every_seconds": 300, "anchor_at": 0.0}}
    service.provision_assets("alice", "monitor", "job_gatemonitor", payload_override=payload, auto_enable=False)
    for step, enabled in enumerate((False, True, False)):
        if step:
            service.toggle_asset("alice", "monitor", "job_gatemonitor", enabled)
        job = _managed_scheduler(home).get_job("job_gatemonitor")
        assert job["enabled"] is enabled
        assert job["status"] == ("active" if enabled else "paused")


# LLM: 只模拟进程在真实副作用完成后退出，不替换领域写入或批准判定。
# 类用途: 让故障注入越过普通业务异常处理，留下真实中间态。
class _Stage3Crash(BaseException):
    pass


@pytest.mark.parametrize("phase", ["approved", "executing", "effect_checkpoint", "applied", "after_effect"])
def test_stage3_review_recovers_crashes_without_repeating_persona_effect(managed, monkeypatch, phase):
    import agent_py_agent.agent.user_space.governance_review_store as reviews_module
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.user_space.managed_user_governance import _GovernanceControl

    root, service, home = managed
    conversations = ConversationStore(home.home_dir / "conversations")
    thread = conversations.threads.create(owner_id=home.owner_id, owner_home=str(home.home_dir))
    reviews = reviews_module.GovernanceReviewStore(root)
    item = reviews.submit_review("alice", "persona_change", {"target": "soul", "action": "add", "content": "approved once"}, applicant_thread_id=thread.thread_id)
    write, execute = _GovernanceControl.write, reviews_module._execute_review
    interrupted = False

    def broken_write(control, name, value, mirror=None):
        nonlocal interrupted
        matches = value.get("status") == phase or (phase == "effect_checkpoint" and value.get("status") == "executing" and value.get("effect_applied_at"))
        if not interrupted and matches and name == ("governance_reviews", item.review_id + ".json"):
            interrupted = True
            write(control, name, value)
            raise HostAPIError("GOVERNANCE_MIRROR_FAILED", 503)
        return write(control, name, value, mirror)

    def broken_execute(*args):
        nonlocal interrupted
        result = execute(*args)
        if phase == "after_effect" and not interrupted:
            interrupted = True
            raise _Stage3Crash()
        return result

    monkeypatch.setattr(_GovernanceControl, "write", broken_write)
    monkeypatch.setattr(reviews_module, "_execute_review", broken_execute)
    with pytest.raises((HostAPIError, _Stage3Crash)):
        reviews.decide_review(item.review_id, "approve", expected_revision=1)
    recovered = reviews.decide_review(item.review_id, "approve", expected_revision=1)
    assert interrupted and recovered["status"] == "applied"
    assert recovered["decision_intent"]["pre_decision_revision"] == 1
    assert recovered["effect_applied_at"]
    assert service.get_persona("alice")["revision"] == 1
    assert home.soul_md.read_text().count("approved once") == 1
    mirror = json.loads((home.home_dir / "data/governance_reviews" / (item.review_id + ".json")).read_text())
    assert mirror == recovered
    assert len(conversations.messages.recent(thread.thread_id)) == 1
    assert len(conversations.wakes.pending()) == 1
    assert reviews.decide_review(item.review_id, "approve", expected_revision=1) == recovered
    assert reviews.decide_review(item.review_id, "approve", expected_revision=recovered["revision"]) == recovered
    with pytest.raises(HostAPIError, match="REVISION_CONFLICT"):
        reviews.decide_review(item.review_id, "approve", feedback="different intent", expected_revision=1)


def test_stage3_review_rejection_reentry_is_idempotent(managed):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, _service, _home = managed
    reviews = GovernanceReviewStore(root)
    item = reviews.submit_review("alice", "high_risk_command", {"command": "echo local"})
    first = reviews.decide_review(item.review_id, "reject_with_feedback", "Use local input", expected_revision=1)
    assert reviews.decide_review(item.review_id, "reject_with_feedback", "Use local input", expected_revision=1) == first


@pytest.mark.parametrize("review_type,action", [
    ("high_risk_command", {"command": "echo local", "tool_name": "run_command"}),
    ("capability_elevation", {"tools": ["read_file"]}),
])
def test_stage3_approved_ticket_is_target_bound_and_one_use(managed, review_type, action):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, _service, _home = managed
    reviews = GovernanceReviewStore(root)
    item = reviews.submit_review("alice", review_type, action)
    applied = reviews.decide_review(item.review_id, "approve", expected_revision=1)
    assert applied["status"] == "applied" and applied["result"]["execution_performed"] is True
    assert applied["result"]["approval_ticket"]
    owner = GovernanceReviewStore(root, actor_owner=OwnerIdentity.local_user("alice"))
    with pytest.raises(HostAPIError, match="APPROVAL_ACTION_MISMATCH"):
        owner.consume_approved_action(item.review_id, target_user="alice", review_type=review_type, action_match={"command": "different"})
    denied = GovernanceReviewStore(root, actor_owner=OwnerIdentity.local_user("bob"))
    with pytest.raises(ManagedUserScopeDeniedError):
        denied.consume_approved_action(item.review_id, target_user="alice", review_type=review_type, action_match=action)
    consumed = owner.consume_approved_action(item.review_id, target_user="local/alice", review_type=review_type, action_match=action)
    assert consumed["consumed_at"] and consumed["approval_ticket"] == applied["result"]["approval_ticket"]
    with pytest.raises(HostAPIError, match="APPROVAL_ALREADY_CONSUMED"):
        owner.consume_approved_action(item.review_id, target_user="alice", review_type=review_type, action_match=action)


def test_stage3_schedule_review_creates_real_enabled_target_job(managed):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore
    from agent_py_agent.agent.user_space.managed_user_governance import _managed_scheduler

    root, _service, home = managed
    reviews = GovernanceReviewStore(root)
    action = {"name": "Approved schedule", "prompt": "Inspect local data", "schedule": {"kind": "every", "every_seconds": 300, "anchor_at": 0.0}}
    item = reviews.submit_review("alice", "schedule_create", action)
    result = reviews.decide_review(item.review_id, "approve", expected_revision=1)
    assert result["status"] == "applied" and result["result"]["execution_performed"] is True
    job_id = result["result"]["job_id"]
    job = _managed_scheduler(home).get_job(job_id)
    assert job["enabled"] is True and job["status"] == "active"
    assert job["owner"] == {"provider": "local", "kind": home.identity.owner_kind, "id": "local/alice"}
    assert reviews.decide_review(item.review_id, "approve", expected_revision=1)["result"]["job_id"] == job_id
    assert len(_managed_scheduler(home).list_jobs()[0]) == 1


def test_stage3_locked_memory_blocks_direct_and_forged_source_edits_but_admin_can_unlock(managed):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    _root, service, home = managed
    entry = service.mutate_memories("alice", "add_memory", content="admin fact", locked=True)["entry"]
    memory = JsonlMemory(home.long_term_jsonl)
    for operation in ("replace", "remove"):
        with pytest.raises(HostAPIError, match="MEMORY_LOCKED_BY_ADMIN"):
            if operation == "replace":
                memory.replace(entry["entry_id"], "forged", source="admin_managed_governance")
            else:
                memory.remove(entry["entry_id"], source="admin_managed_governance")
    assert memory.all()[0].content == "admin fact"
    changed = service.mutate_memories("alice", "edit_memory", entry_id=entry["entry_id"], content="admin correction", expected_revision=1)
    assert changed["entry"]["content"] == "admin correction"
    unlocked = service.mutate_memories("alice", "edit_memory", entry_id=entry["entry_id"], content="admin correction", lock_memory=False, expected_revision=2)
    assert unlocked["entry"]["entry_id"] not in service.get_overview("alice")["governance_policy"]["memory_governance"]["admin_locked_memory_ids"]
    assert memory.replace(entry["entry_id"], "allowed owner edit").content == "allowed owner edit"


def test_stage3_memory_batch_cannot_hide_a_locked_edit_after_an_add(managed):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    _root, service, home = managed
    entry = service.mutate_memories("alice", "add_memory", content="locked batch fact", locked=True)["entry"]
    before = home.long_term_jsonl.read_bytes()
    memory = JsonlMemory(home.long_term_jsonl)
    with pytest.raises(HostAPIError, match="MEMORY_LOCKED_BY_ADMIN"):
        memory.apply_batch([{"action": "add", "content": "otherwise allowed fact", "role": "user"},
                            {"action": "replace", "entry_id": entry["entry_id"], "content": "forged"}])
    assert home.long_term_jsonl.read_bytes() == before


def test_stage3_admin_memory_add_and_edit_default_to_locked(managed):
    _root, service, _home = managed
    entry = service.mutate_memories("alice", "add_memory", content="default locked fact")["entry"]
    ids = service.get_overview("alice")["governance_policy"]["memory_governance"]["admin_locked_memory_ids"]
    assert entry["entry_id"] in ids


def test_stage3_admin_only_curate_policy_blocks_owner_add_and_edit(managed):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    _root, service, home = managed
    entry = service.mutate_memories("alice", "add_memory", content="initial", locked=False)["entry"]
    service.set_policy("alice", {"memory_curate_mode": "admin_only"})
    memory = JsonlMemory(home.long_term_jsonl)
    with pytest.raises(HostAPIError, match="MEMORY_LOCKED_BY_ADMIN"):
        memory.add("user", "owner should not add")
    with pytest.raises(HostAPIError, match="MEMORY_LOCKED_BY_ADMIN"):
        memory.replace(entry["entry_id"], "owner should not edit")
    assert service.mutate_memories("alice", "edit_memory", entry_id=entry["entry_id"], content="admin permitted")["entry"]["content"] == "admin permitted"


# LLM: 真实准入和policy锁外加顺序断言；错误顺序立即失败，不用死锁超时当修复证据。
# 函数用途: 同时验证普通批量、复用准入及管理员增改删的锁顺序。
@pytest.mark.parametrize("operation", ["batch", "admitted_batch", "add_memory", "edit_memory", "delete_memory"])
def test_r2_memory_mutations_acquire_quota_before_policy(managed, monkeypatch, operation):
    from contextlib import contextmanager

    from agent_py_agent.agent.user_space.managed_user_governance import (
        _GovernanceControl,
        _managed_memory_stores,
    )
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer

    _root, service, home = managed
    entry = service.mutate_memories("alice", "add_memory", content="before", lock_memory=False)["entry"]
    quota, policy, depth, seen = OwnerQuotaEnforcer.admission, _GovernanceControl.locked, [0], []

    @contextmanager
    def checked_quota(enforcer, **kwargs):
        assert depth[0] == 0, "quota admission must be shared, not reacquired"
        with quota(enforcer, **kwargs) as admission:
            depth[0] += 1
            try:
                yield admission
            finally:
                depth[0] -= 1

    @contextmanager
    def checked_policy(control, name="policy"):
        if name == "policy":
            assert depth[0] == 1, "policy acquired before quota"
            seen.append(name)
        with policy(control, name):
            yield

    monkeypatch.setattr(OwnerQuotaEnforcer, "admission", checked_quota)
    monkeypatch.setattr(_GovernanceControl, "locked", checked_policy)
    if operation in {"batch", "admitted_batch"}:
        memory, _candidates = _managed_memory_stores(home)
        operations = [{"action": "replace", "entry_id": entry["entry_id"], "content": "after"}]
        if operation == "admitted_batch":
            with memory.quota_enforcer.admission() as admission:
                memory.apply_batch(operations, _admission=admission)
        else:
            memory.apply_batch(operations)
    else:
        service.mutate_memories("alice", operation, entry_id=entry["entry_id"], content="after")
    assert seen == ["policy"]


# LLM: 788f9b847后浓缩只按字符预算无损转冷，不再archive+remove；锁定条目固定热层，任何模式都不遗忘或改版本。
# 函数用途: 验证浓缩不能借转冷绕过管理员锁，也不写旧archive或删除回执；仅管理员整理模式下事实同样完整。
@pytest.mark.parametrize("mode", ["locked", "admin_only", "autonomous"])
def test_r2_condense_respects_governance_before_archive(managed, mode):
    from agent_py_agent.agent.user_space.managed_user_governance import _managed_memory_stores

    _root, service, home = managed
    locked = service.mutate_memories("alice", "add_memory", content="old locked fact")["entry"]
    unlocked = service.mutate_memories("alice", "add_memory", content="unlocked fact", lock_memory=False)["entry"]
    memory, _ = _managed_memory_stores(home)
    if mode == "admin_only":
        service.set_policy("alice", {"memory_curate_mode": "admin_only"})
    policy = json.loads(home.memory_policy_json.read_text()) if home.memory_policy_json.exists() else {}
    policy["budget"] = {**policy.get("budget", {}), "active_library_max_chars": 20}
    home.memory_policy_json.write_text(json.dumps(policy))
    before = {record.entry_id: (record.content, record.version) for record in memory.all()}
    archive = home.long_term_jsonl.with_name("memory.jsonl.archive.jsonl")
    removed = memory.condense(max_records=1, target_records=0, max_ratio=1)
    assert [record.entry_id for record in removed] == [unlocked["entry_id"]]
    assert [record.entry_id for record in memory.active_records()] == [locked["entry_id"]]
    assert {record.entry_id: (record.content, record.version) for record in memory.all()} == before
    assert not archive.exists() and memory.committed_removal_record(unlocked["entry_id"]) is None


# LLM: 原迁移接口只按精确版本/hash清除；管理员锁不因备份引用存在而失效。
# 函数用途: 验证迁移删除也经过正式记忆锁，拒绝时原文件不变。
@pytest.mark.parametrize("mode", ["locked", "admin_only"])
def test_r2_migration_cannot_remove_governed_memories(managed, mode):
    from agent_py_agent.agent.user_space.managed_user_governance import _managed_memory_stores

    _root, service, home = managed
    service.mutate_memories("alice", "add_memory", content="migration fact", lock_memory=mode == "locked")
    if mode == "admin_only":
        service.set_policy("alice", {"memory_curate_mode": "admin_only"})
    memory, _ = _managed_memory_stores(home)
    before = home.long_term_jsonl.read_bytes()
    with pytest.raises(HostAPIError, match="MEMORY_LOCKED_BY_ADMIN"):
        memory.remove_migrated_legacy(memory.all(), source="memory-migration-test", backup_ref="durable-backup")
    assert home.long_term_jsonl.read_bytes() == before


# LLM: 两种请求都必须对计算前同一快照做版本和SHA校验，不能到提交时才换成新版本。
# 函数用途: 验证人格审核陈旧CAS失败且不更改正文。
@pytest.mark.parametrize("style", ["fields", "operation"])
@pytest.mark.parametrize("precondition", ["revision", "sha256", "empty_sha256"])
def test_r2_persona_review_rejects_stale_preconditions(managed, style, precondition):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, service, home = managed
    service.set_persona("alice", soul_md="current text", expected_revision=0)
    action = {"soul_md": "replacement"} if style == "fields" else {"target": "soul", "operation": "write_text", "content": "replacement"}
    action.update({"expected_revision": 0} if precondition == "revision" else {"expected_sha256": "0" * 64 if precondition == "sha256" else ""})
    reviews = GovernanceReviewStore(root)
    item = reviews.submit_review("alice", "persona_change", action)
    result = reviews.decide_review(item.review_id, "approve", expected_revision=1)
    assert result["status"] == "failed" and result["result"]["error_code"] == "REVISION_CONFLICT"
    assert home.soul_md.read_text() == "current text" and service.get_persona("alice")["revision"] == 1


@pytest.mark.parametrize("style", ["fields", "operation"])
def test_r2_persona_review_freezes_valid_revision_and_hash(managed, style):
    from hashlib import sha256

    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, service, home = managed
    service.set_persona("alice", soul_md="current text", expected_revision=0)
    action = {"soul_md": "replacement"} if style == "fields" else {"target": "soul", "operation": "write_text", "content": "replacement"}
    action.update(expected_revision=1, expected_sha256=sha256(b"current text").hexdigest())
    reviews = GovernanceReviewStore(root)
    item = reviews.submit_review("alice", "persona_change", action)
    result = reviews.decide_review(item.review_id, "approve", expected_revision=1)
    assert result["status"] == "applied" and home.soul_md.read_text() == "replacement"
    assert result["effect_action"] == {"soul_md": "replacement", "expected_revision": 1}


@pytest.mark.parametrize("action_match", [None, {}, "", "x" * 64, 12, []])
def test_r2_ticket_cannot_be_consumed_without_exact_action_match(managed, action_match):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, _service, _home = managed
    reviews = GovernanceReviewStore(root)
    action = {"command": "echo local"}
    item = reviews.submit_review("alice", "high_risk_command", action)
    reviews.decide_review(item.review_id, "approve", expected_revision=1)
    with pytest.raises(HostAPIError, match="APPROVAL_ACTION_MISMATCH"):
        reviews.consume_approved_action(item.review_id, target_user="alice", review_type="high_risk_command", action_match=action_match)
    assert reviews.consume_approved_action(item.review_id, target_user="alice", review_type="high_risk_command", action_match=action)["consumed_at"]


# LLM: 原消息事实、候选和晋升器均真实使用；默认生成规范personal/personal，调用方可指定合法typed范围。
# 函数用途: 为晋升恢复反例创建可通过原证据门的目标用户候选。
def _r2_explicit_candidate(home, *, scope=None):
    from hashlib import sha256

    from agent_py_agent.agent.conversation import learning_source as ls
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.user_space.managed_user_governance import (
        _managed_conversations,
        _managed_memory_stores,
    )

    store = _managed_conversations(home, initialize=True)
    thread = store.threads.get_or_create({"canonical_user_id": home.owner_id, "channel": "internal",
        "channel_conversation_id": "recovery-thread", "channel_user_id": home.owner_id,
        "owner_id": home.owner_id, "owner_home": str(home.home_dir), "learning_source": ls.user_dialogue_projection()})
    content = "恢复测试的明确事实"
    message = store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": content})
    ref = {"message_id": message.message_id, "thread_id": thread.thread_id, "role": "user",
           "content_hash": "sha256:" + sha256(content.encode()).hexdigest(), "quote": content}
    memory, candidates = _managed_memory_stores(home)
    candidate = candidates.observe(CandidateObservation(candidate_type="long_term_fact", content=content,
        subject_key="recovery.fact", scope=scope or MemoryScope("personal", "personal"), origin="user_explicit",
        source_message_refs=(ref,), learning_source=ls.merge_message_sources([ls.facts_for_message(
            message.metadata, owner=home.owner_id, thread_id=thread.thread_id, thread_source=thread.learning_source)]),
        promotion_target="long_term", promotion_mode="auto_eligible"))
    return memory, candidates, candidate


# LLM: 规范候选经真实晋升后恢复执行中审核；不得改写候选scope来绕过治理校验，两条重入均不重写事实。
# 函数用途: 验证personal/personal候选在审核和直接批准两入口幂等恢复。
def test_r2_memory_promotion_recovers_executing_with_already_promoted_candidate(managed, monkeypatch):
    import agent_py_agent.agent.user_space.governance_review_store as module
    from agent_py_agent.agent.user_space.managed_user_governance import _managed_promotion

    root, service, home = managed
    memory, candidates, candidate = _r2_explicit_candidate(home)
    reviews = module.GovernanceReviewStore(root)
    item = reviews.submit_review("alice", "memory_promotion", {"candidate_id": candidate.candidate_id})
    original = module._execute_review

    def crash(*args):
        raise _Stage3Crash()

    monkeypatch.setattr(module, "_execute_review", crash)
    with pytest.raises(_Stage3Crash):
        reviews.decide_review(item.review_id, "approve", expected_revision=1)
    monkeypatch.setattr(module, "_execute_review", original)
    promotion = _managed_promotion(home, memory, candidates)
    reviewed = promotion.review(candidate, approved=True, reviewer="local/main")
    assert promotion.promote(candidate.candidate_id, reviewer="local/main", confirmed=True, expected_candidate=reviewed).promoted
    assert candidates.get(candidate.candidate_id).status == "promoted"
    assert candidates.get(candidate.candidate_id).scope == {"scope_type": "personal", "scope_key": "personal", "applies_when": "", "excludes_when": ""}
    before = home.long_term_jsonl.read_bytes(), home.memory_candidates_jsonl.read_bytes()
    recovered = reviews.decide_review(item.review_id, "approve", expected_revision=1)
    assert recovered["status"] == "applied" and recovered["result"]["already_promoted"] is True
    assert (home.long_term_jsonl.read_bytes(), home.memory_candidates_jsonl.read_bytes()) == before
    assert len(memory.all()) == 1
    assert service.mutate_memories("alice", "approve_candidate", candidate_id=candidate.candidate_id)["already_promoted"] is True
    assert (home.long_term_jsonl.read_bytes(), home.memory_candidates_jsonl.read_bytes()) == before


@pytest.mark.parametrize("operation", ["approve_candidate", "reject_candidate"])
@pytest.mark.parametrize("scope_type,scope_key", [("personal", "personal"), ("global", "global"), ("project", "project:demo")])
def test_r3_canonical_candidate_scope_uses_target_owner_store(managed, operation, scope_type, scope_key):
    from agent_py_agent.agent.memory_store.candidate_models import MemoryScope

    _root, service, home = managed
    memory, candidates, candidate = _r2_explicit_candidate(home, scope=MemoryScope(scope_type, scope_key))
    result = service.mutate_memories("alice", operation, candidate_id=candidate.candidate_id)
    assert result["ok"] is True
    assert candidates.get(candidate.candidate_id).status == ("promoted" if operation == "approve_candidate" else "rejected")
    assert len(memory.all()) == (1 if operation == "approve_candidate" else 0)


# LLM: 仅污染/历史兼容用例调用；正常创建与恢复测试必须始终走规范候选，不以改盘制造通过。
# 函数用途: 在真实候选文件中注入明确标注的历史或跨owner坏快照。
def _r3_write_candidate_snapshot(home, candidate, *, scope_key=None, source_field=None):
    row = candidate.to_record()
    if scope_key is not None:
        row["scope"]["scope_key"] = scope_key
    if source_field is not None:
        row["learning_source"][source_field] = "local/bob"
        # 模拟携带owner字段的旧来源快照，不让新覆盖投影的一致性检查提前遮住owner门。
        row.pop("learning_source_coverage", None)
    home.memory_candidates_jsonl.write_text(json.dumps(row, ensure_ascii=False) + "\n")


def test_r3_legacy_same_owner_candidate_scope_remains_readable(managed):
    _root, service, home = managed
    _memory, candidates, candidate = _r2_explicit_candidate(home)
    _r3_write_candidate_snapshot(home, candidate, scope_key=home.owner_id)
    result = service.mutate_memories("alice", "reject_candidate", candidate_id=candidate.candidate_id)
    assert result["ok"] is True
    assert candidates.get(candidate.candidate_id).status == "rejected"


@pytest.mark.parametrize("entrypoint", ["approve_candidate", "reject_candidate", "decide_review"])
@pytest.mark.parametrize("already_promoted", [False, True])
@pytest.mark.parametrize("scope_type,scope_key,source_field", [
    ("personal", None, "owner"), ("personal", None, "owner_id"),
    ("personal", "local/alice", "owner"), ("personal", "local/alice", "owner_id"),
    ("personal", "local/bob", None), ("personal", "providers/feishu/users/ou_bob", None),
    ("global", "local/bob", None), ("global", "providers/feishu/users/ou_bob", None),
])
def test_r3_foreign_owner_candidate_is_denied_before_memory_changes(managed, entrypoint, already_promoted, scope_type, scope_key, source_field):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore
    from agent_py_agent.agent.user_space.managed_user_governance import _managed_promotion

    root, service, home = managed
    memory, candidates, candidate = _r2_explicit_candidate(home)
    if already_promoted:
        promotion = _managed_promotion(home, memory, candidates)
        reviewed = promotion.review(candidate, approved=True, reviewer="local/main")
        assert promotion.promote(candidate.candidate_id, reviewer="local/main", confirmed=True, expected_candidate=reviewed).promoted
        candidate = candidates.get(candidate.candidate_id)
    candidate.scope["scope_type"] = scope_type
    candidate.scope["scope_key"] = scope_type
    _r3_write_candidate_snapshot(home, candidate, scope_key=scope_key, source_field=source_field)
    before = home.long_term_jsonl.read_bytes() if home.long_term_jsonl.exists() else None, home.memory_candidates_jsonl.read_bytes()
    if entrypoint == "decide_review":
        reviews = GovernanceReviewStore(root)
        item = reviews.submit_review("alice", "memory_promotion", {"candidate_id": candidate.candidate_id})
        result = reviews.decide_review(item.review_id, "approve", expected_revision=1)
        assert result["status"] == "failed"
        assert result["result"]["error_code"] == "MANAGED_USER_SCOPE_DENIED"
        assert result["result"]["error_type"] == "ManagedUserScopeDeniedError"
    else:
        with pytest.raises(ManagedUserScopeDeniedError):
            service.mutate_memories("alice", entrypoint, candidate_id=candidate.candidate_id)
    after = home.long_term_jsonl.read_bytes() if home.long_term_jsonl.exists() else None, home.memory_candidates_jsonl.read_bytes()
    assert after == before


@pytest.mark.parametrize("object_source", [False, True])
@pytest.mark.parametrize("fields,denied", [
    ({}, False), ({"owner": "local/alice"}, False), ({"owner_id": "local/alice"}, False),
    ({"owner": "", "owner_id": "local/alice"}, False),
    ({"owner": "local/bob"}, True), ({"owner_id": "local/bob"}, True),
    ({"owner": "local/alice", "owner_id": "local/bob"}, True),
    ({"owner": "local/bob", "owner_id": "local/alice"}, True),
])
def test_r3_candidate_source_owner_gate_handles_dict_and_object(managed, object_source, fields, denied):
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.managed_user_governance import _managed_candidate_for_owner

    _root, _service, home = managed
    source = SimpleNamespace(**fields) if object_source else fields
    candidate = SimpleNamespace(scope={"scope_type": "personal", "scope_key": "personal"}, learning_source=source)
    candidates = SimpleNamespace(get=lambda candidate_id: candidate)
    if denied:
        with pytest.raises(ManagedUserScopeDeniedError):
            _managed_candidate_for_owner(home, candidates, "candidate-id")
    else:
        assert _managed_candidate_for_owner(home, candidates, "candidate-id") is candidate


@pytest.mark.parametrize("flags,locked", [({"locked": True}, True), ({"lock_memory": False}, False), ({"locked": False, "lock_memory": True}, False)])
def test_r2_admin_tool_accepts_explicit_memory_lock_parameters(managed, flags, locked):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    root, service, _home = managed
    tool = AdminControlsTool(SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), resolve_owner_home(root, OwnerIdentity.local_main()))))
    result = tool.execute({"action": "mutate_user_memory", "target_owner_id": "alice", "operation": "add_memory", "content": "tool lock fact", **flags})
    assert result.ok, result.output
    entry = json.loads(result.output)["entry"]
    ids = service.get_overview("alice")["governance_policy"]["memory_governance"]["admin_locked_memory_ids"]
    assert (entry["entry_id"] in ids) is locked


def test_r2_admin_tool_consumes_exact_review_ticket_once(managed):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.admin_controls_tool import AdminControlsTool
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    root, _service, _home = managed
    reviews = GovernanceReviewStore(root)
    action = {"tools": ["read_file"]}
    item = reviews.submit_review("alice", "capability_elevation", action)
    reviews.decide_review(item.review_id, "approve", expected_revision=1)
    tool = AdminControlsTool(SimpleNamespace(home_paths=home_paths_with_owner(home_paths(root), resolve_owner_home(root, OwnerIdentity.local_main()))))
    assert "consume_governance_review_ticket" in tool.model_spec.input_schema["properties"]["action"]["enum"]
    assert {"review_type", "action_match"} <= tool.model_spec.input_schema["properties"].keys()
    params = {"action": "consume_governance_review_ticket", "target_owner_id": "alice", "review_id": item.review_id,
              "review_type": "capability_elevation", "action_match": action}
    first = tool.execute(params)
    assert first.ok and json.loads(first.output)["consumed_at"]
    again = tool.execute(params)
    assert not again.ok and again.reported_error_code == "APPROVAL_ALREADY_CONSUMED"


@pytest.mark.parametrize("operation", ["provision_assets", "toggle_asset"])
@pytest.mark.parametrize("kind,asset_id", [("script", "absent"), ("workflow", "absent"), ("skill", "absent"), ("monitor", "job_absent")])
def test_r2_missing_assets_return_not_started_domain_error(managed, operation, kind, asset_id):
    _root, service, _home = managed
    kwargs = {"enabled": True} if operation == "toggle_asset" else {}
    with pytest.raises(HostAPIError) as caught:
        getattr(service, operation)("alice", kind, asset_id, **kwargs)
    assert (caught.value.code, caught.value.status, caught.value.outcome) == ("ASSET_NOT_FOUND", 404, "not_started")


# LLM: 使用真实authority和跨线程review锁，摘要形式也不能令同一票据多次通过。
# 函数用途: 覆盖动作JSON与SHA两种匹配方式的并发单次消费。
@pytest.mark.parametrize("match_kind", ["action", "digest"])
def test_r2_ticket_concurrent_consumers_have_exactly_one_winner(managed, match_kind):
    from agent_py_agent.agent.user_space.governance_review_store import GovernanceReviewStore

    root, _service, _home = managed
    reviews = GovernanceReviewStore(root)
    action = {"command": "echo local", "cwd": "workspace"}
    item = reviews.submit_review("alice", "high_risk_command", action)
    result = reviews.decide_review(item.review_id, "approve", expected_revision=1)
    match = action if match_kind == "action" else result["result"]["action_sha256"]

    def consume(_index):
        try:
            receipt = reviews.consume_approved_action(item.review_id, target_user="alice", review_type="high_risk_command", action_match=match)
            assert receipt["consumed_at"]
            return "consumed"
        except HostAPIError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(consume, range(4)))
    assert outcomes.count("consumed") == 1
    assert outcomes.count("APPROVAL_ALREADY_CONSUMED") == 3
