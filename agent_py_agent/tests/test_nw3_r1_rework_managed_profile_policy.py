"""Behavioral regression suite for NW3R1-P1-03: managed profile dream governance policy enforcement."""

from __future__ import annotations

import errno
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.capability import persona_repository as persona_repo_mod
from agent_py_agent.agent.capability.persona_repository import (
    PersonaBatchMutationRequest,
    PersonaMutationRequest,
    PersonaRepository,
)
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    MemoryCuratorDependencies,
    MemoryCuratorService,
)
from agent_py_agent.agent.memory_store.curator_formal import FormalMemorySource
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
from agent_py_agent.agent.memory_store.curator_run_log import CuratorRunLog
from agent_py_agent.agent.memory_store.curator_state import MemoryCuratorStateStore
from agent_py_agent.agent.memory_store.daily import DailyMemoryStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.user_profile_dream import plan_user_profile_consolidation
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space import local_user_store as local_user_store_mod
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.managed_user_governance import (
    ManagedUserGovernanceService,
    _GovernanceControl,
)
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer


def _build_71_single_valued_preferences() -> str:
    """Build 71 historical single-valued preferences that fold with zero long_term relocations."""
    lines = ["# USER.md"]
    for idx in range(24):
        lines.append(f"- language: lang-{idx}")
    for idx in range(24):
        lines.append(f"- timezone: tz-{idx}")
    for idx in range(22):
        lines.append(f"- theme: theme-{idx}")
    lines.append("- language: lang-current")
    return "\n".join(lines) + "\n"


def _seed_managed_user_profile(tmp_path: Path, uid: str = "alice"):
    root = tmp_path / "host"
    users = LocalUserStore(root)
    LocalUserLifecycle(users).create(uid, idempotency_key=f"nw3-r1-3-{uid}-0001")
    home = users.owner_home(uid)
    admin = ManagedUserGovernanceService(root)
    repo = PersonaRepository.from_owner_result(home)
    assert repo.quota_enforcer.max_bytes == 1024 * 1024 * 1024

    # Materialize versions.jsonl first so tests prove both USER.md and versions.jsonl stay byte-identical.
    repo.mutate(
        PersonaMutationRequest(
            target="user",
            action="add",
            content="language: lang-current",
            confirmed=True,
        )
    )
    content = _build_71_single_valued_preferences()
    home.user_md.write_text(content, encoding="utf-8")

    plan = plan_user_profile_consolidation(content, persona_history=repo.history("user"))
    assert plan.entry_count_before == 71
    assert plan.relocated_entries == ()
    assert len(plan.kept_entries) == 3
    return root, admin, home, repo, plan


def _build_curator(owner_home: Path, repo: PersonaRepository, long_term: JsonlMemory) -> MemoryCuratorService:
    live_cfg = AgentConfig()
    curator_cfg = MemoryCuratorConfig.from_agent_config(live_cfg)
    assert curator_cfg.user_profile_dream_entry_threshold == 70
    assert curator_cfg.user_profile_dream_token_threshold == 5000
    return MemoryCuratorService(
        config=curator_cfg,
        dependencies=MemoryCuratorDependencies(
            backend=None,
            conversation_store=None,
            audit_dir=owner_home / "memory" / "curator" / "audit",
            state_store=MemoryCuratorStateStore(owner_home / "memory" / "curator" / "state.json"),
            daily_store=DailyMemoryStore(owner_home / "memory" / "daily"),
            candidate_service=CandidateService(owner_home / "memory" / "candidates.jsonl"),
            run_log=CuratorRunLog(owner_home / "memory" / "curator" / "runs"),
            formal_memory_source=FormalMemorySource(long_term=long_term, lessons=None, hot=None),
            persona=repo,
            config_provider=lambda: live_cfg,
        ),
    )


def _snapshot_user_profile_state(home_dir: Path, repo: PersonaRepository) -> tuple[bytes, bytes, list[str]]:
    backups_dir = home_dir / "persona" / "backups" / "user"
    backup_names = sorted(p.name for p in backups_dir.glob("*.md")) if backups_dir.exists() else []
    return home_dir.joinpath("USER.md").read_bytes(), repo.versions_path.read_bytes(), backup_names


def test_managed_profile_dream_blocked_when_self_learning_disabled(tmp_path: Path) -> None:
    _root, admin, home, repo, plan = _seed_managed_user_profile(tmp_path, "alice")
    admin.set_policy("alice", {"memory_governance": {"allow_self_learning": False}})
    long_term = JsonlMemory(home.long_term_jsonl)
    curator = _build_curator(home.home_dir, repo, long_term)
    before = _snapshot_user_profile_state(home.home_dir, repo)

    with pytest.raises(HostAPIError) as exc_info:
        repo.consolidate_user_profile(
            target="user",
            consolidated_content=plan.consolidated_content,
            expected_sha256=repo.current_sha256("user"),
        )
    assert exc_info.value.code == "AUTO_GOVERNANCE_SELF_LEARNING_DISABLED"
    assert exc_info.value.outcome == "not_started"
    assert _snapshot_user_profile_state(home.home_dir, repo) == before

    maint = curator.run_maintenance_if_due(now=datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc))
    assert maint["status"] in {"failed", "partial"}
    assert maint.get("user_profile_dream", {}).get("status") != "consolidated"
    assert maint["reason"] == "AUTO_GOVERNANCE_SELF_LEARNING_DISABLED"
    assert maint["errors"]["user_profile_dream"] == "AUTO_GOVERNANCE_SELF_LEARNING_DISABLED"
    assert _snapshot_user_profile_state(home.home_dir, repo) == before
    assert long_term.all() == []
    assert not (home.home_dir / "persona" / "user_dream_state.json").exists()
    assert not (home.home_dir / "persona" / "user_dream_ledger.jsonl").exists()


def test_managed_profile_dream_blocked_when_admin_review_required(tmp_path: Path) -> None:
    _root, admin, home, repo, plan = _seed_managed_user_profile(tmp_path, "alice")
    admin.set_policy("alice", {"memory_governance": {"require_admin_review_for_long_term": True}})
    long_term = JsonlMemory(home.long_term_jsonl)
    curator = _build_curator(home.home_dir, repo, long_term)
    before = _snapshot_user_profile_state(home.home_dir, repo)

    with pytest.raises(HostAPIError) as exc_info:
        repo.consolidate_user_profile(
            target="user",
            consolidated_content=plan.consolidated_content,
            expected_sha256=repo.current_sha256("user"),
        )
    assert exc_info.value.code == "AUTO_GOVERNANCE_ADMIN_REVIEW_REQUIRED"
    assert exc_info.value.outcome == "not_started"
    assert _snapshot_user_profile_state(home.home_dir, repo) == before

    maint = curator.run_maintenance_if_due(now=datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc))
    assert maint["status"] in {"failed", "partial"}
    assert maint.get("user_profile_dream", {}).get("status") != "consolidated"
    assert maint["reason"] == "AUTO_GOVERNANCE_ADMIN_REVIEW_REQUIRED"
    assert maint["errors"]["user_profile_dream"] == "AUTO_GOVERNANCE_ADMIN_REVIEW_REQUIRED"
    assert _snapshot_user_profile_state(home.home_dir, repo) == before
    assert long_term.all() == []


def test_managed_profile_dream_blocked_when_admin_only_curate_mode(tmp_path: Path) -> None:
    _root, admin, home, repo, plan = _seed_managed_user_profile(tmp_path, "alice")
    admin.set_policy("alice", {"memory_curate_mode": "admin_only"})
    long_term = JsonlMemory(home.long_term_jsonl)
    curator = _build_curator(home.home_dir, repo, long_term)
    before = _snapshot_user_profile_state(home.home_dir, repo)

    with pytest.raises(HostAPIError) as exc_info:
        repo.consolidate_user_profile(
            target="user",
            consolidated_content=plan.consolidated_content,
            expected_sha256=repo.current_sha256("user"),
        )
    assert exc_info.value.code == "AUTO_GOVERNANCE_ADMIN_ONLY"
    assert exc_info.value.outcome == "not_started"
    assert _snapshot_user_profile_state(home.home_dir, repo) == before

    maint = curator.run_maintenance_if_due(now=datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc))
    assert maint["status"] in {"failed", "partial"}
    assert maint.get("user_profile_dream", {}).get("status") != "consolidated"
    assert maint["reason"] == "AUTO_GOVERNANCE_ADMIN_ONLY"
    assert maint["errors"]["user_profile_dream"] == "AUTO_GOVERNANCE_ADMIN_ONLY"
    assert _snapshot_user_profile_state(home.home_dir, repo) == before
    assert long_term.all() == []


def test_managed_profile_dream_allowed_for_permissive_managed_and_unmanaged_main(tmp_path: Path) -> None:
    # 1. Managed user with default permissive learning policy consolidates cleanly via curator.
    root, _admin, home, repo, _plan = _seed_managed_user_profile(tmp_path, "alice")
    long_term = JsonlMemory(home.long_term_jsonl)
    curator = _build_curator(home.home_dir, repo, long_term)
    maint = curator.run_maintenance_if_due(now=datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc))
    assert maint["status"] == "succeeded", maint
    assert maint["user_profile_dream"]["status"] == "consolidated", maint
    assert maint["user_profile_dream"]["relocated_ids"] == []
    assert long_term.all() == []
    after_managed = home.user_md.read_text(encoding="utf-8")
    assert "language: lang-current" in after_managed
    assert "timezone: tz-23" in after_managed
    assert "theme: theme-21" in after_managed
    assert len(repo.list_entries("user")["entries"]) == 3

    # 2. Unmanaged local/main owner continues to consolidate unchanged.
    main_home = root / "owners" / "local" / "main"
    main_home.mkdir(parents=True, exist_ok=True)
    main_repo = PersonaRepository(
        owner_home=main_home,
        soul_path=main_home / "SOUL.md",
        user_path=main_home / "USER.md",
        agents_path=main_home / "AGENTS.md",
    )
    main_repo.mutate(
        PersonaMutationRequest(
            target="user",
            action="add",
            content="language: lang-current",
            confirmed=True,
        )
    )
    (main_home / "USER.md").write_text(_build_71_single_valued_preferences(), encoding="utf-8")
    main_lt = JsonlMemory(main_home / "memory" / "long_term" / "memory.jsonl")
    main_curator = _build_curator(main_home, main_repo, main_lt)
    main_maint = main_curator.run_maintenance_if_due(now=datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc))
    assert main_maint["status"] == "succeeded", main_maint
    assert main_maint["user_profile_dream"]["status"] == "consolidated", main_maint
    after_main = main_home.joinpath("USER.md").read_text(encoding="utf-8")
    assert "language: lang-current" in after_main
    assert "timezone: tz-23" in after_main
    assert "theme: theme-21" in after_main
    assert len(main_repo.list_entries("user")["entries"]) == 3


def test_explicit_user_persona_mutate_and_batch_succeed_while_soul_agents_governed(tmp_path: Path) -> None:
    _root, admin, home, repo, plan = _seed_managed_user_profile(tmp_path, "alice")
    admin.set_policy(
        "alice",
        {
            "memory_governance": {"allow_self_learning": False},
            "persona_mode": "admin_locked",
        },
    )
    admin.set_persona("alice", soul_md="# SOUL\n- Locked soul\n", agents_md="# AGENTS\n- Locked agents\n")
    soul_before = home.soul_md.read_bytes()
    agents_before = home.agents_md.read_bytes()

    with pytest.raises(HostAPIError) as dream_exc:
        repo.consolidate_user_profile(
            target="user",
            consolidated_content=plan.consolidated_content,
        )
    assert dream_exc.value.code == "AUTO_GOVERNANCE_SELF_LEARNING_DISABLED"

    mutate_res = repo.mutate(
        PersonaMutationRequest(
            target="user",
            action="add",
            content="explicit style: concise",
            confirmed=True,
        )
    )
    assert mutate_res["changed"] is True
    assert "explicit style: concise" in home.user_md.read_text(encoding="utf-8")

    batch_res = repo.mutate_batch(
        PersonaBatchMutationRequest(
            target="user",
            operations=(
                PersonaMutationRequest(
                    target="user",
                    action="add",
                    content="explicit format: bullet",
                    confirmed=True,
                ),
            ),
        )
    )
    assert batch_res["changed"] is True
    assert "explicit format: bullet" in home.user_md.read_text(encoding="utf-8")

    for forbidden_target in ("soul", "agents"):
        with pytest.raises(HostAPIError) as persona_exc:
            repo.mutate(
                PersonaMutationRequest(
                    target=forbidden_target,
                    action="add",
                    content="unauthorized edit",
                    confirmed=True,
                )
            )
        assert persona_exc.value.code == "PERSONA_CHANGE_PENDING_ADMIN_REVIEW"
    assert home.soul_md.read_bytes() == soul_before
    assert home.agents_md.read_bytes() == agents_before


def test_managed_profile_dream_single_io_error_at_governance_policy_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, admin, home, repo, plan = _seed_managed_user_profile(tmp_path, "alice")
    admin.set_policy("alice", {"memory_governance": {"allow_self_learning": True}})
    long_term = JsonlMemory(home.long_term_jsonl)
    curator = _build_curator(home.home_dir, repo, long_term)
    before = _snapshot_user_profile_state(home.home_dir, repo)

    orig_read_bytes = local_user_store_mod.read_bytes_beneath
    injection_count = 0

    def _inject_single_eio(root_dir: Path, parts: tuple[str, ...], **kwargs):
        nonlocal injection_count
        if parts and parts[-1] == "governance_policy.json" and injection_count == 0:
            injection_count += 1
            raise OSError(errno.EIO, "simulated I/O error")
        return orig_read_bytes(root_dir, parts, **kwargs)

    monkeypatch.setattr(local_user_store_mod, "read_bytes_beneath", _inject_single_eio)

    maint = curator.run_maintenance_if_due(now=datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc))
    assert injection_count == 1
    assert maint["status"] in {"failed", "partial"}
    assert maint.get("user_profile_dream", {}).get("status") != "consolidated"
    assert maint["reason"] == "STORE_UNAVAILABLE"
    assert maint["errors"]["user_profile_dream"] == "STORE_UNAVAILABLE"
    assert _snapshot_user_profile_state(home.home_dir, repo) == before

    # Direct consolidate_user_profile also fails closed when single EIO hits policy read leaf.
    injection_count = 0
    with pytest.raises(HostAPIError) as exc_info:
        repo.consolidate_user_profile(
            target="user",
            consolidated_content=plan.consolidated_content,
            expected_sha256=repo.current_sha256("user"),
        )
    assert injection_count == 1
    assert exc_info.value.code == "STORE_UNAVAILABLE"
    assert exc_info.value.outcome == "not_started"
    assert _snapshot_user_profile_state(home.home_dir, repo) == before


def test_managed_profile_dream_lock_order_and_concurrent_policy_flip_interleaving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Part A + Interleaving 1: verify exact lock order quota -> policy -> user_target -> versions,
    # and prove _managed_policy_scope stays held across _consolidate_user_profile_document.
    _root, admin, home, repo, plan = _seed_managed_user_profile(tmp_path / "case1", "alice")
    admin.set_policy("alice", {"memory_governance": {"allow_self_learning": True}})

    orig_admission = OwnerQuotaEnforcer.admission
    orig_policy_locked = _GovernanceControl.locked
    orig_locked_json_path = persona_repo_mod.locked_json_path
    orig_versions_lock = persona_repo_mod.versions_lock
    orig_consolidate_doc = persona_repo_mod._consolidate_user_profile_document

    active_stack: list[str] = []
    observed_transitions: list[tuple[str, str, tuple[str, ...]]] = []
    commit_held_stack: list[tuple[str, ...]] = []

    entered_commit = threading.Event()
    admin_waiting_on_policy = threading.Event()
    admin_finished = threading.Event()
    dream_tid_box: list[int] = []
    admin_tid_box: list[int] = []
    dream_errors: list[BaseException] = []
    dream_results: list[dict[str, object]] = []
    admin_errors: list[BaseException] = []

    @contextmanager
    def traced_admission(enforcer, **kwargs):
        with orig_admission(enforcer, **kwargs) as admission:
            if dream_tid_box and threading.get_ident() == dream_tid_box[0]:
                active_stack.append("quota")
                observed_transitions.append(("enter", "quota", tuple(active_stack)))
            try:
                yield admission
            finally:
                if dream_tid_box and threading.get_ident() == dream_tid_box[0]:
                    observed_transitions.append(("exit", "quota", tuple(active_stack)))
                    assert active_stack.pop() == "quota"

    @contextmanager
    def traced_policy_locked(control, name="policy"):
        if name == "policy" and admin_tid_box and threading.get_ident() == admin_tid_box[0]:
            admin_waiting_on_policy.set()
        with orig_policy_locked(control, name):
            if name == "policy" and dream_tid_box and threading.get_ident() == dream_tid_box[0]:
                assert active_stack == ["quota"], f"expected quota before policy, got {active_stack}"
                active_stack.append("policy")
                observed_transitions.append(("enter", "policy", tuple(active_stack)))
            try:
                yield
            finally:
                if name == "policy" and dream_tid_box and threading.get_ident() == dream_tid_box[0]:
                    observed_transitions.append(("exit", "policy", tuple(active_stack)))
                    assert active_stack.pop() == "policy"

    @contextmanager
    def traced_target_lock(path, **kwargs):
        is_user_target = dream_tid_box and threading.get_ident() == dream_tid_box[0] and path == home.user_md
        with orig_locked_json_path(path, **kwargs):
            if is_user_target:
                assert active_stack == ["quota", "policy"], f"expected quota->policy before user_target, got {active_stack}"
                active_stack.append("user_target")
                observed_transitions.append(("enter", "user_target", tuple(active_stack)))
            try:
                yield
            finally:
                if is_user_target:
                    observed_transitions.append(("exit", "user_target", tuple(active_stack)))
                    assert active_stack.pop() == "user_target"

    @contextmanager
    def traced_versions_lock(path, **kwargs):
        is_dream = dream_tid_box and threading.get_ident() == dream_tid_box[0]
        with orig_versions_lock(path, **kwargs):
            if is_dream:
                assert active_stack == ["quota", "policy", "user_target"], (
                    f"expected quota->policy->user_target before versions, got {active_stack}"
                )
                active_stack.append("versions")
                observed_transitions.append(("enter", "versions", tuple(active_stack)))
            try:
                yield
            finally:
                if is_dream:
                    observed_transitions.append(("exit", "versions", tuple(active_stack)))
                    assert active_stack.pop() == "versions"

    def traced_consolidate_doc(repository, admission, **kwargs):
        if dream_tid_box and threading.get_ident() == dream_tid_box[0]:
            commit_held_stack.append(tuple(active_stack))
            entered_commit.set()
            assert admin_waiting_on_policy.wait(timeout=5.0)
            # Admin policy flip must stay blocked until _managed_policy_scope releases after commit.
            assert admin_finished.wait(timeout=0.05) is False
        return orig_consolidate_doc(repository, admission, **kwargs)

    monkeypatch.setattr(OwnerQuotaEnforcer, "admission", traced_admission)
    monkeypatch.setattr(_GovernanceControl, "locked", traced_policy_locked)
    monkeypatch.setattr(persona_repo_mod, "locked_json_path", traced_target_lock)
    monkeypatch.setattr(persona_repo_mod, "versions_lock", traced_versions_lock)
    monkeypatch.setattr(persona_repo_mod, "_consolidate_user_profile_document", traced_consolidate_doc)

    expected_sha = repo.current_sha256("user")

    def run_dream() -> None:
        dream_tid_box.append(threading.get_ident())
        try:
            dream_results.append(
                repo.consolidate_user_profile(
                    target="user",
                    consolidated_content=plan.consolidated_content,
                    expected_sha256=expected_sha,
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            dream_errors.append(exc)

    def run_admin_flip() -> None:
        admin_tid_box.append(threading.get_ident())
        try:
            assert entered_commit.wait(timeout=5.0)
            admin.set_policy("alice", {"memory_governance": {"allow_self_learning": False}})
            admin_finished.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            admin_errors.append(exc)

    t_dream = threading.Thread(target=run_dream)
    t_admin = threading.Thread(target=run_admin_flip)
    t_dream.start()
    t_admin.start()
    t_dream.join(timeout=10.0)
    t_admin.join(timeout=10.0)
    assert not t_dream.is_alive() and not t_admin.is_alive()
    assert dream_errors == [] and admin_errors == []
    assert len(dream_results) == 1 and dream_results[0]["changed"] is True
    assert admin_finished.is_set()
    assert commit_held_stack == [("quota", "policy")]
    assert observed_transitions[:4] == [
        ("enter", "quota", ("quota",)),
        ("enter", "policy", ("quota", "policy")),
        ("enter", "user_target", ("quota", "policy", "user_target")),
        ("enter", "versions", ("quota", "policy", "user_target", "versions")),
    ]
    assert observed_transitions[-2:] == [
        ("exit", "policy", ("quota", "policy")),
        ("exit", "quota", ("quota",)),
    ]

    # Immediately after admin flip completes, subsequent dream consolidation is blocked.
    with pytest.raises(HostAPIError) as post_flip_exc:
        repo.consolidate_user_profile(
            target="user",
            consolidated_content="# USER.md\n\n- language: lang-99\n",
        )
    assert post_flip_exc.value.code == "AUTO_GOVERNANCE_SELF_LEARNING_DISABLED"
    assert post_flip_exc.value.outcome == "not_started"

    # Interleaving 2: admin flips policy WHILE quota admission is held and before _managed_policy_scope acquires policy lock.
    monkeypatch.undo()
    _root2, admin2, home2, repo2, plan2 = _seed_managed_user_profile(tmp_path / "case2", "bob")
    admin2.set_policy("bob", {"memory_governance": {"allow_self_learning": True}})
    before2 = _snapshot_user_profile_state(home2.home_dir, repo2)

    quota_held = threading.Event()
    policy_flipped = threading.Event()
    case2_dream_tid: list[int] = []
    case2_errors: list[BaseException] = []
    case2_admin_errors: list[BaseException] = []
    commit_called = False

    @contextmanager
    def gate_after_quota(enforcer, **kwargs):
        with orig_admission(enforcer, **kwargs) as admission:
            if case2_dream_tid and threading.get_ident() == case2_dream_tid[0]:
                quota_held.set()
                assert policy_flipped.wait(timeout=5.0)
            yield admission

    def fail_if_commit(*args, **kwargs):
        nonlocal commit_called
        commit_called = True
        return orig_consolidate_doc(*args, **kwargs)

    monkeypatch.setattr(OwnerQuotaEnforcer, "admission", gate_after_quota)
    monkeypatch.setattr(persona_repo_mod, "_consolidate_user_profile_document", fail_if_commit)

    expected_sha2 = repo2.current_sha256("user")

    def run_dream2() -> None:
        case2_dream_tid.append(threading.get_ident())
        try:
            repo2.consolidate_user_profile(
                target="user",
                consolidated_content=plan2.consolidated_content,
                expected_sha256=expected_sha2,
            )
        except BaseException as exc:
            case2_errors.append(exc)

    def run_admin_flip2() -> None:
        try:
            assert quota_held.wait(timeout=5.0)
            admin2.set_policy("bob", {"memory_governance": {"allow_self_learning": False}})
            policy_flipped.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            case2_admin_errors.append(exc)

    t_dream2 = threading.Thread(target=run_dream2)
    t_admin2 = threading.Thread(target=run_admin_flip2)
    t_dream2.start()
    t_admin2.start()
    t_dream2.join(timeout=10.0)
    t_admin2.join(timeout=10.0)
    assert not t_dream2.is_alive() and not t_admin2.is_alive()
    assert case2_admin_errors == []
    assert commit_called is False
    assert len(case2_errors) == 1
    assert isinstance(case2_errors[0], HostAPIError)
    assert case2_errors[0].code == "AUTO_GOVERNANCE_SELF_LEARNING_DISABLED"
    assert case2_errors[0].outcome == "not_started"
    assert _snapshot_user_profile_state(home2.home_dir, repo2) == before2
