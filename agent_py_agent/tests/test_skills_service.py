from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import CapabilityRouter, SkillSnapshotError, SkillsService


def _write_skill(root: Path, name: str, description: str, body: str = "按步骤执行。") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_scope_priority_is_workspace_owner_shared_builtin(tmp_path, skill_catalog_factory):
    catalog = skill_catalog_factory(tmp_path / "home")
    _write_skill(Path(catalog.home.shared_builtin_dir), "same", "builtin")
    _write_skill(Path(catalog.home.shared_skills_dir), "same", "shared")
    _write_skill(Path(catalog.home.owner_home_dir) / "skills", "same", "owner")
    _write_skill(catalog.workspace / ".agents" / "skills", "same", "workspace")

    snapshot = catalog.service.snapshot_for(catalog.workspace, force_reload=True)
    entry = snapshot.resolve("same")
    assert entry is not None
    assert (entry.source, entry.description, entry.stable_id) == (
        "workspace",
        "workspace",
        "workspace:same",
    )


def test_policy_disables_and_allowlists_shared_skills(tmp_path, skill_catalog_factory):
    catalog = skill_catalog_factory(
        tmp_path / "home",
        policy_overrides={
            "enabled_shared_skills": ("allowed",),
            "disabled_skills": ("disabled",),
        },
    )
    shared = Path(catalog.home.shared_skills_dir)
    _write_skill(shared, "allowed", "allowed shared skill")
    _write_skill(shared, "not-allowlisted", "hidden shared skill")
    _write_skill(Path(catalog.home.owner_home_dir) / "skills", "disabled", "disabled owner skill")

    snapshot = catalog.service.snapshot_for(catalog.workspace, force_reload=True)
    assert {entry.name for entry in snapshot.enabled_entries()} == {"allowed"}
    assert snapshot.resolve("not-allowlisted") is None
    assert snapshot.resolve("disabled") is None


def test_snapshot_is_stable_for_turn_and_reloads_after_file_change(tmp_path, skill_catalog_factory):
    skill_root = tmp_path / "skills"
    path = _write_skill(skill_root, "triage", "version one")
    catalog = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root])
    first = catalog.snapshot
    assert catalog.service.snapshot_for(catalog.workspace) is first

    path.write_text(
        "---\nname: triage\ndescription: version two\n---\n\nnew body\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillSnapshotError, match="SKILL_SNAPSHOT_STALE"):
        first.read_body("triage")
    second = catalog.service.snapshot_for(catalog.workspace)
    assert second is not first
    assert second.resolve("triage").description == "version two"
    assert "new body" in second.read_body("triage")


def test_invalid_frontmatter_is_reported_without_loading(tmp_path, skill_catalog_factory):
    skill_root = tmp_path / "skills"
    path = skill_root / "broken" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# no frontmatter\n", encoding="utf-8")

    snapshot = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root]).snapshot
    assert snapshot.enabled_entries() == ()
    assert [(error.code, Path(error.path).name) for error in snapshot.errors] == [
        ("SKILL_PARSE_FAILED", "SKILL.md")
    ]


def test_hidden_drafts_and_symlink_escape_are_not_loaded(tmp_path, skill_catalog_factory):
    skill_root = tmp_path / "skills"
    _write_skill(skill_root / ".drafts", "draft", "must stay hidden")
    outside = _write_skill(tmp_path / "outside", "escaped", "must not escape root")
    link = skill_root / "linked" / "SKILL.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    snapshot = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root]).snapshot
    assert snapshot.enabled_entries() == ()
    assert any(error.code == "SKILL_PATH_ESCAPE" for error in snapshot.errors)


def test_owner_catalogs_share_public_skills_but_not_private_skills(tmp_path):
    shared = tmp_path / "shared"
    builtin = tmp_path / "builtin"
    owner_a = tmp_path / "owners" / "a"
    owner_b = tmp_path / "owners" / "b"
    workspace_a = owner_a / "workspace"
    workspace_b = owner_b / "workspace"
    _write_skill(shared, "public", "shared public skill")
    _write_skill(owner_a / "skills", "private-a", "owner A only")
    _write_skill(owner_b / "skills", "private-b", "owner B only")

    def service(owner: Path, owner_id: str, workspace: Path) -> SkillsService:
        home = SimpleNamespace(
            owner_home_dir=owner,
            shared_skills_dir=shared,
            shared_builtin_dir=builtin,
        )
        policy = SimpleNamespace(
            owner_id=owner_id,
            enabled_skill_sources=("workspace", "owner", "shared", "builtin"),
            enabled_shared_skills=(),
            disabled_skills=(),
        )
        return SkillsService(
            home_paths=home,
            workspace_root=workspace,
            policy_provider=lambda: policy,
        )

    names_a = {entry.name for entry in service(owner_a, "a", workspace_a).snapshot_for().enabled_entries()}
    names_b = {entry.name for entry in service(owner_b, "b", workspace_b).snapshot_for().enabled_entries()}
    assert names_a == {"public", "private-a"}
    assert names_b == {"public", "private-b"}


def test_restricted_snapshot_is_fail_closed_and_hides_paths(tmp_path, skill_catalog_factory):
    skill_root = tmp_path / "skills"
    _write_skill(skill_root, "one", "first skill")
    _write_skill(skill_root, "two", "second skill")
    catalog = skill_catalog_factory(tmp_path / "home", extra_roots=[skill_root])
    one = catalog.snapshot.resolve("one")
    restricted = catalog.snapshot.restricted(
        [one.stable_id],
        expected_sha256={one.stable_id: one.content_sha256},
    )
    assert [entry.name for entry in restricted.enabled_entries()] == ["one"]
    assert restricted.resolve("two") is None
    router = CapabilityRouter(skill_snapshot=restricted)
    skill_card = router.cards(kinds={"skill"})[0]
    assert skill_card.path == ""
    assert skill_card.metadata["stable_id"] == one.stable_id
    with pytest.raises(SkillSnapshotError, match="SKILL_NOT_AVAILABLE"):
        catalog.snapshot.restricted(["missing"])
    with pytest.raises(SkillSnapshotError, match="SKILL_SNAPSHOT_STALE"):
        catalog.snapshot.restricted(
            [one.stable_id],
            expected_sha256={one.stable_id: "0" * 64},
        )
