"""Round 1 rework regression suite for USER.md lossless consolidation and post-publish backup preservation."""

from __future__ import annotations

import errno
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import persona_version_ledger
from agent_py_agent.agent.capability.persona_repository import (
    PersonaConflictError,
    PersonaMutationRequest,
    PersonaRepository,
    persona_entry_id,
)
from agent_py_agent.agent.capability.persona_tool import (
    UpdatePersonaTool,
    _reset_persona_update_rate_limit,
)
from agent_py_agent.agent.capability.persona_version_ledger import PersonaLedgerPublishedError
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.user_profile_dream import (
    _classify_user_entry,
    _preference_key,
    _strip_machine_tags,
    plan_user_profile_consolidation,
    run_user_profile_dream_if_due,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings.config import AgentConfig


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    _reset_persona_update_rate_limit()
    yield
    _reset_persona_update_rate_limit()


def _make_owner_home_1gib(tmp_path: Path, name: str = "main") -> Path:
    owner_home = tmp_path / "owners" / "local" / name
    owner_home.mkdir(parents=True, exist_ok=True)
    # Explicit 1 GiB owner disk quota matching production scale.
    (owner_home / "quota.json").write_text(json.dumps({"max_disk_mb": 1024}), encoding="utf-8")
    return owner_home


def _make_repo(owner_home: Path) -> PersonaRepository:
    return PersonaRepository(
        owner_home=owner_home,
        soul_path=owner_home / "SOUL.md",
        user_path=owner_home / "USER.md",
        agents_path=owner_home / "AGENTS.md",
    )


def _fake_agent(owner_home: Path, repo: PersonaRepository) -> SimpleNamespace:
    home_paths = SimpleNamespace(
        owner_home_dir=owner_home,
        owner_soul_md=owner_home / "SOUL.md",
        owner_user_md=owner_home / "USER.md",
        owner_agents_md=owner_home / "AGENTS.md",
    )
    cfg = AgentConfig()
    return SimpleNamespace(
        home_paths=home_paths,
        persona_repository=repo,
        config=cfg,
        live_agent_config=cfg,
    )


def test_nw3_r1_p1_01_71_distinct_project_records_zero_superseded_and_all_accounted(tmp_path: Path) -> None:
    owner_home = _make_owner_home_1gib(tmp_path)
    repo = _make_repo(owner_home)
    long_term = JsonlMemory(owner_home / "memory" / "long_term" / "memory.jsonl")

    entries = [f"项目记录: item {idx}" for idx in range(1, 72)]
    (owner_home / "USER.md").write_text(
        "\n".join(["# USER.md", *[f"- {text}" for text in entries]]) + "\n",
        encoding="utf-8",
    )

    report = run_user_profile_dream_if_due(
        owner_home=owner_home,
        persona_repository=repo,
        long_term_store=long_term,
        config=AgentConfig(),
        now=datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc),
    )

    assert report["status"] == "consolidated"
    assert report["entry_threshold"] == 70
    assert report["token_threshold"] == 5000
    assert report["entry_count_before"] == 71
    assert report["superseded_ids"] == []
    assert len(report["kept_source_ids"]) == 49
    assert len(report["relocated_ids"]) == 22

    after_user_text = repo.load("user").content
    lt_contents = {rec.content for rec in long_term.all()}
    assert len(lt_contents) == 22
    for text in entries:
        assert (text in after_user_text) or (text in lt_contents), f"missing entry after dream: {text}"


def test_nw3_r1_p1_01_promoted_scope_tagged_entries_never_superseded(tmp_path: Path) -> None:
    owner_home = _make_owner_home_1gib(tmp_path)
    repo = _make_repo(owner_home)
    long_term = JsonlMemory(owner_home / "memory" / "long_term" / "memory.jsonl")

    scoped_entries = [
        "[project:project:alpha] 常用测试框架 pytest",
        "[project:project:alpha] 常用构建入口 scripts/build.py",
        "[project:project:alpha] module owner runtime_core",
        "[project:project:alpha] service port 8080",
        *[f"[task_class:task_class:k{i}] workflow rule {i}" for i in range(1, 6)],
        "[company:acme] review channel release-board",
        "[company:acme] artifact format markdown",
    ]
    scoped_ids = {persona_entry_id("user", item): item for item in scoped_entries}

    # Add >70 total entries with newer tier-2 descriptors so several scoped entries relocate to long_term.
    filler_entries = [f"工作模块记录 {idx}: detail {idx}" for idx in range(1, 66)]
    all_lines = ["# USER.md", *[f"- {item}" for item in scoped_entries], *[f"- {item}" for item in filler_entries]]
    (owner_home / "USER.md").write_text("\n".join(all_lines) + "\n", encoding="utf-8")

    report = run_user_profile_dream_if_due(
        owner_home=owner_home,
        persona_repository=repo,
        long_term_store=long_term,
        config=AgentConfig(),
        now=datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc),
    )

    assert report["status"] == "consolidated"
    assert report["entry_count_before"] == 76
    superseded_ids = set(report["superseded_ids"])
    kept_ids = set(report["kept_source_ids"])
    relocated_ids = set(report["relocated_ids"])

    assert not (superseded_ids & set(scoped_ids))
    assert set(scoped_ids).issubset(kept_ids | relocated_ids)

    relocated_scoped = relocated_ids & set(scoped_ids)
    assert len(relocated_scoped) >= 9
    lt_contents = {rec.content for rec in long_term.all()}
    after_user_text = repo.load("user").content
    for eid, text in scoped_ids.items():
        if eid in relocated_scoped:
            assert text in lt_contents
        else:
            assert text in after_user_text


def test_nw3_r1_p1_01_punctuation_case_dedupe_aliases_and_core_tier(tmp_path: Path) -> None:
    owner_home = _make_owner_home_1gib(tmp_path)
    repo = _make_repo(owner_home)
    long_term = JsonlMemory(owner_home / "memory" / "long_term" / "memory.jsonl")

    assert _classify_user_entry("姓名: 小叶子") == 2
    assert _preference_key("姓名: 小叶子") == "kv:姓名"
    assert _preference_key("[project:project:my-agent] key: val") is None
    assert _preference_key(_strip_machine_tags("[personal:personal] language: English")) == "alias:language"

    # Seed older English preference in ledger, then newer Chinese preference, then other entries >70.
    repo.mutate(
        PersonaMutationRequest(
            target="user",
            action="add",
            content="[personal:personal] language: English",
            confirmed=True,
        )
    )
    repo.mutate(
        PersonaMutationRequest(
            target="user",
            action="add",
            content="[user:preference] language: Chinese",
            confirmed=True,
        )
    )

    profile_entries = [
        "常用编程语言: C++",
        "常用编程语言: C#",
        "常用编程语言 C++",
        "常用编程语言 C#",
        "workspace_id: AlphaOne",
        "workspace_id: alphaone",
        "exact_note: keep_once",
        "exact_note: keep_once",
        "[personal:personal] dup_tagged: same",
        "[user:preference] dup_tagged:   same",
        "姓名: 小叶子",
    ]
    # Use episodic filler (>70 total) so every core entry remains in USER.md while triggering default dream threshold.
    episodic_filler = [f"2026-11-{(idx % 28) + 1:02d} sprint log item {idx}" for idx in range(65)]
    existing_lines = [line for line in repo.load("user").content.splitlines() if line.strip() and not line.startswith("#")]
    user_lines = [
        "# USER.md",
        *existing_lines,
        *[f"- {item}" for item in profile_entries],
        *[f"- {item}" for item in episodic_filler],
    ]
    (owner_home / "USER.md").write_text("\n".join(user_lines) + "\n", encoding="utf-8")

    plan = plan_user_profile_consolidation(repo.load("user").content, persona_history=repo.history("user"))
    assert persona_entry_id("user", "[personal:personal] language: English") in plan.superseded_ids
    assert persona_entry_id("user", "[user:preference] language: Chinese") in plan.kept_source_ids

    report = run_user_profile_dream_if_due(
        owner_home=owner_home,
        persona_repository=repo,
        long_term_store=long_term,
        config=AgentConfig(),
        now=datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc),
    )
    assert report["status"] == "consolidated"

    older_lang_id = persona_entry_id("user", "[personal:personal] language: English")
    newer_lang_id = persona_entry_id("user", "[user:preference] language: Chinese")
    older_dup_id = persona_entry_id("user", "[personal:personal] dup_tagged: same")
    newer_dup_id = persona_entry_id("user", "[user:preference] dup_tagged:   same")

    assert older_lang_id in report["superseded_ids"]
    assert newer_lang_id in report["kept_source_ids"]
    assert older_dup_id in report["superseded_ids"]
    assert newer_dup_id in report["kept_source_ids"]

    after_text = repo.load("user").content
    assert "language: Chinese" in after_text
    assert "language: English" not in after_text
    for expected in (
        "- 常用编程语言: C++",
        "- 常用编程语言: C#",
        "- 常用编程语言 C++",
        "- 常用编程语言 C#",
        "- workspace_id: AlphaOne",
        "- workspace_id: alphaone",
        "- 姓名: 小叶子",
    ):
        assert expected in after_text
    assert after_text.count("- exact_note: keep_once") == 1
    assert after_text.count("- dup_tagged: same") == 1


def _seed_history_then_72_entries(owner_home: Path, repo: PersonaRepository) -> bytes:
    repo.mutate(
        PersonaMutationRequest(
            target="user",
            action="add",
            content="初始偏好: 结论先行",
            confirmed=True,
        )
    )
    lines = ["# USER.md"]
    for idx in range(1, 31):
        lines.append(f"- 2026-11-{(idx % 28) + 1:02d} sprint log checkpoint {idx}")
    for idx in range(31, 73):
        lines.append(f"- 项目记录: item {idx}")
    (owner_home / "USER.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return (owner_home / "USER.md").read_bytes()


def test_nw3_r1_p1_04_zero_error_control_and_post_publish_eio_preserves_backup_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Zero-error positive control with pre_dream_version=None.
    ctrl_home = _make_owner_home_1gib(tmp_path, "ctrl")
    ctrl_repo = _make_repo(ctrl_home)
    ctrl_agent = _fake_agent(ctrl_home, ctrl_repo)
    ctrl_lt = JsonlMemory(ctrl_home / "memory" / "long_term" / "memory.jsonl")
    ctrl_pre_bytes = _seed_history_then_72_entries(ctrl_home, ctrl_repo)

    ctrl_report = run_user_profile_dream_if_due(
        owner_home=ctrl_home,
        persona_repository=ctrl_repo,
        long_term_store=ctrl_lt,
        config=AgentConfig(),
        now=datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc),
    )
    assert ctrl_report["status"] == "consolidated"
    assert ctrl_report["pre_dream_version"] is None
    assert (ctrl_home / str(ctrl_report["pre_dream_backup_ref"])).read_bytes() == ctrl_pre_bytes
    ctrl_rb = UpdatePersonaTool(ctrl_agent).execute({"action": "rollback", "target": "user"})
    assert ctrl_rb.ok is True
    assert (ctrl_home / "USER.md").read_bytes() == ctrl_pre_bytes

    # Single post-publish EIO on persona_version_ledger.remove_private after version publish.
    owner_home = _make_owner_home_1gib(tmp_path, "eio")
    repo = _make_repo(owner_home)
    agent = _fake_agent(owner_home, repo)
    long_term = JsonlMemory(owner_home / "memory" / "long_term" / "memory.jsonl")
    pre_dream_bytes = _seed_history_then_72_entries(owner_home, repo)

    orig_remove_private = persona_version_ledger.remove_private
    eio_trigger_count = 0

    def _fail_once_post_publish_cleanup(path, *, directory=False, coordinator_io=False):
        nonlocal eio_trigger_count
        if eio_trigger_count == 0 and not directory and Path(path).name == "versions.jsonl":
            eio_trigger_count += 1
            raise OSError(errno.EIO, "simulated I/O error")
        return orig_remove_private(path, directory=directory, coordinator_io=coordinator_io)

    with monkeypatch.context() as ctx:
        ctx.setattr(persona_version_ledger, "remove_private", _fail_once_post_publish_cleanup)
        with pytest.raises(PersonaLedgerPublishedError):
            run_user_profile_dream_if_due(
                owner_home=owner_home,
                persona_repository=repo,
                long_term_store=long_term,
                config=AgentConfig(),
                now=datetime(2026, 11, 15, 14, 0, tzinfo=timezone.utc),
            )

    assert eio_trigger_count == 1
    assert (owner_home / "USER.md").read_bytes() != pre_dream_bytes

    dream_rows = [
        row
        for row in repo.history("user")
        if str(row.get("action") or "") == "consolidate" and str(row.get("source") or "") == "user_profile_dream"
    ]
    assert len(dream_rows) == 1
    dream_row = dream_rows[0]
    assert dream_row["pre_dream_version"] is None
    backup_ref = str(dream_row["pre_dream_backup_ref"])
    assert backup_ref
    backup_file = owner_home / backup_ref
    assert backup_file.is_file() and not backup_file.is_symlink()
    assert backup_file.read_bytes() == pre_dream_bytes

    rb_outcome = UpdatePersonaTool(agent).execute({"action": "rollback", "target": "user"})
    assert rb_outcome.ok is True
    assert (owner_home / "USER.md").read_bytes() == pre_dream_bytes


def test_nw3_r1_p1_04_pre_commit_backup_cleanup_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _run_with_commit_error(name: str, exc: Exception) -> list[Path]:
        owner_home = _make_owner_home_1gib(tmp_path, name)
        repo = _make_repo(owner_home)
        long_term = JsonlMemory(owner_home / "memory" / "long_term" / "memory.jsonl")
        lines = ["# USER.md"] + [f"- 项目记录: item {idx}" for idx in range(1, 73)]
        (owner_home / "USER.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

        def _raise_error(**_kwargs):
            raise exc

        with monkeypatch.context() as ctx:
            ctx.setattr(repo, "consolidate_user_profile", _raise_error)
            with pytest.raises(type(exc)):
                run_user_profile_dream_if_due(
                    owner_home=owner_home,
                    persona_repository=repo,
                    long_term_store=long_term,
                    config=AgentConfig(),
                    now=datetime(2026, 11, 15, 15, 0, tzinfo=timezone.utc),
                )

        backup_dir = owner_home / "persona" / "backups" / "user"
        return sorted(backup_dir.glob("*.md")) if backup_dir.exists() else []

    assert _run_with_commit_error("conflict", PersonaConflictError("simulated concurrent USER.md edit")) == []
    assert (
        _run_with_commit_error(
            "gov_disabled",
            HostAPIError("AUTO_GOVERNANCE_SELF_LEARNING_DISABLED", 403, outcome="not_started"),
        )
        == []
    )
    assert (
        _run_with_commit_error(
            "gov_review",
            HostAPIError("AUTO_GOVERNANCE_ADMIN_REVIEW_REQUIRED", 409, outcome="not_started"),
        )
        == []
    )
    assert (
        _run_with_commit_error(
            "gov_admin_not_started",
            HostAPIError("AUTO_GOVERNANCE_ADMIN_ONLY", 403, outcome="not_started"),
        )
        == []
    )

    preserved_unknown = _run_with_commit_error(
        "gov_admin_unknown",
        HostAPIError("AUTO_GOVERNANCE_ADMIN_ONLY", 403, outcome="unknown"),
    )
    assert len(preserved_unknown) == 1

    preserved_committed = _run_with_commit_error(
        "gov_admin_committed",
        HostAPIError("AUTO_GOVERNANCE_ADMIN_ONLY", 403, outcome="committed"),
    )
    assert len(preserved_committed) == 1

    preserved_other_code = _run_with_commit_error(
        "other_host_code",
        HostAPIError("STORE_UNAVAILABLE", 503, outcome="not_started"),
    )
    assert len(preserved_other_code) == 1


@pytest.mark.parametrize("first_commit_conflict", [False, True])
def test_nw3_r2_p1_01_exact_case_distinct_relocation_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_commit_conflict: bool,
) -> None:
    owner_home = _make_owner_home_1gib(tmp_path)
    repo = _make_repo(owner_home)
    long_term = JsonlMemory(owner_home / "memory" / "long_term" / "memory.jsonl")
    assert repo.quota_enforcer.max_bytes == 1024**3

    entries = [
        "workspace_id: AlphaOne",
        "workspace_id: alphaone",
        *[f"工作模块记录 {idx}: detail {idx}" for idx in range(69)],
    ]
    source = "# USER.md\n\n" + "\n".join(f"- {item}" for item in entries) + "\n"
    (owner_home / "USER.md").write_text(source, encoding="utf-8")
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)

    def run(at: datetime) -> dict[str, object]:
        return run_user_profile_dream_if_due(
            owner_home=owner_home,
            persona_repository=repo,
            long_term_store=long_term,
            config=AgentConfig(),
            now=at,
        )

    if first_commit_conflict:
        def conflict(**_kwargs):
            raise PersonaConflictError("simulated concurrent USER.md edit")

        with monkeypatch.context() as patch_context:
            patch_context.setattr(repo, "consolidate_user_profile", conflict)
            with pytest.raises(PersonaConflictError):
                run(now)
        assert (owner_home / "USER.md").read_text(encoding="utf-8") == source
        assert len(long_term.all()) == 22
        assert not (owner_home / "persona" / "user_dream_state.json").exists()

    outcome = run(now + timedelta(days=1))
    assert outcome["status"] == "consolidated"
    assert outcome["entry_threshold"] == 70
    assert outcome["token_threshold"] == 5000
    assert len(outcome["kept_source_ids"]) == 49
    assert len(outcome["relocated_ids"]) == 22
    assert outcome["superseded_ids"] == []

    records = long_term.all()
    assert len(records) == 22
    for row in records:
        assert (row.attributes or {}).get("subject_key") == (
            f"user-profile-dream:{persona_entry_id('user', row.content)}"
        )
    durable = {row.content for row in records}
    assert entries[0] in durable and entries[1] in durable

    kept = {
        line[2:]
        for line in (owner_home / "USER.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    }
    assert len(kept) == 49
    assert kept | durable == set(entries)

    assert run(now + timedelta(days=2))["reason"] == "watermark_unchanged"
    assert len(long_term.all()) == 22


def test_nw3_r2_p1_04_single_post_publish_directory_fsync_eio_preserves_backup_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_home = _make_owner_home_1gib(tmp_path, "dir_fsync")
    repo = _make_repo(owner_home)
    agent = _fake_agent(owner_home, repo)
    long_term = JsonlMemory(owner_home / "memory" / "long_term" / "memory.jsonl")
    before = _seed_history_then_72_entries(owner_home, repo)

    real_transfer = persona_version_ledger.transfer
    real_fsync = os.fsync
    state = {"published": False, "failures": 0}

    def tracked_transfer(source, destination, **kwargs):
        value = real_transfer(source, destination, **kwargs)
        if Path(destination) == repo.versions_path:
            state["published"] = True
        return value

    def fail_one_directory_sync(descriptor: int) -> None:
        if state["published"] and state["failures"] == 0:
            descriptor_stat = os.fstat(descriptor)
            parent_stat = repo.versions_path.parent.stat()
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
                parent_stat.st_dev,
                parent_stat.st_ino,
            ):
                state["failures"] += 1
                raise OSError(errno.EIO, "simulated I/O error")
        return real_fsync(descriptor)

    with monkeypatch.context() as ctx:
        ctx.setattr(persona_version_ledger, "transfer", tracked_transfer)
        ctx.setattr(os, "fsync", fail_one_directory_sync)
        with pytest.raises(PersonaLedgerPublishedError):
            run_user_profile_dream_if_due(
                owner_home=owner_home,
                persona_repository=repo,
                long_term_store=long_term,
                config=AgentConfig(),
                now=datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc),
            )

    assert state["failures"] == 1
    assert (owner_home / "USER.md").read_bytes() != before
    rows = [row for row in repo.history("user") if row.get("action") == "consolidate"]
    assert len(rows) == 1
    backup = owner_home / str(rows[0]["pre_dream_backup_ref"])
    assert backup.is_file() and not backup.is_symlink()
    assert backup.read_bytes() == before

    rolled = UpdatePersonaTool(agent).execute({"action": "rollback", "target": "user"})
    assert rolled.ok is True
    assert (owner_home / "USER.md").read_bytes() == before
