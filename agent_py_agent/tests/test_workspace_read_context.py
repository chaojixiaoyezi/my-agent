"""冻结读取合同的权限与协议回归；不计实际 TUI 或真实模型验收。"""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling.workspace_read_scope import build_workspace_read_context
from agent_py_agent.agent.workspace_read_context import WorkspaceReadContext


# LLM: 只提供显式临时路径，不从真实用户目录补测试权限。
# 函数用途: 创建与 registry 相同的宿主读取快照。
def read_context(cwd, *, boundary=None, owner="", grants=(), mode="normal", dangerous=()):
    return build_workspace_read_context(
        cwd=cwd, write_boundary=boundary, granted_external_roots=grants,
        path_policy=PathAccessPolicy.from_values(mode=mode, dangerous_roots=dangerous, owner_scope_root=owner),
    )


@pytest.mark.parametrize("roots,expected", [([], ()), (["elsewhere"], ()), (["work/input.txt"], ("input.txt",)),
                                          (["work"], (".",)), (["."], (".",))])
def test_exact_scope_intersects_cwd_without_parent_or_empty_fallback(tmp_path, roots, expected):
    cwd = tmp_path / "work"
    context = read_context(cwd, boundary={"read_scope_mode": "exact", "allowed_read_roots": [str(tmp_path / p) for p in roots]})
    assert context.read_roots == tuple((cwd / item).resolve() for item in expected)
    restored = WorkspaceReadContext.from_payload(context.to_payload())
    assert restored == context
    if expected == ("input.txt",):
        assert restored.check(Path("input.txt")).allowed
        assert not restored.check(Path(".")).allowed
    if not expected:
        assert not restored.check(Path(".")).allowed


def test_normal_supplementary_read_roots_cannot_expand_workspace(tmp_path):
    context = read_context(tmp_path / "work", boundary={"allowed_read_roots": [str(tmp_path)]})
    assert context.read_roots == ((tmp_path / "work").resolve(),)
    assert context.check(Path("../outside.txt")).code == "PATH_READ_SCOPE_BLOCKED"


def test_default_and_aliased_dangerous_roots_round_trip(tmp_path):
    target = tmp_path / "restricted"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    for dangerous in ((), (str(target), str(alias), str(target))):
        context = read_context(tmp_path, dangerous=dangerous)
        assert len(context.path_policy.dangerous_roots) == len(set(context.path_policy.dangerous_roots))
        assert WorkspaceReadContext.from_payload(context.to_payload()) == context


def test_each_child_still_obeys_original_credential_and_dangerous_policy(tmp_path):
    context = read_context(tmp_path, dangerous=[str(tmp_path / "restricted")])
    assert context.check(Path(".")).allowed
    assert context.check(Path(".env")).code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert context.check(Path("restricted/child.txt")).code == "PATH_DANGEROUS_ROOT_BLOCKED"
    assert context.check(Path(".env.example")).allowed


def test_symlink_targets_must_satisfy_scope_and_policy(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / "outside").symlink_to(tmp_path / "secret")
    (cwd / "alias").symlink_to(cwd / ".env")
    context = read_context(cwd)
    assert context.check(Path("outside")).code == "PATH_READ_SCOPE_BLOCKED"
    assert context.check(Path("alias")).code == "PATH_CREDENTIAL_FILE_BLOCKED"


def test_owner_wall_full_mode_and_exact_file_are_independent(tmp_path):
    owner = tmp_path / "home/owners/local/first"
    other = tmp_path / "home/owners/local/second"
    own = read_context(owner, owner=str(owner), mode="full")
    assert own.check(Path(".env")).allowed
    forbidden = read_context(other, owner=str(owner), mode="full", grants=(other,))
    assert forbidden.check(Path("data.txt")).code == "PATH_CROSS_OWNER_BLOCKED"
    control = read_context(tmp_path / "home/admin_grants", owner=str(owner), mode="full",
                           grants=(tmp_path / "home/admin_grants",))
    assert control.check(Path("control.json")).code == "PATH_ADMIN_GRANTS_BLOCKED"


def test_explicit_external_grant_is_intersected_and_does_not_override_other_denials(tmp_path):
    owner, cwd = tmp_path / "home/owners/local/main", tmp_path / "work"
    without = read_context(cwd, owner=str(owner))
    assert without.check(Path("note.txt")).code == "PATH_OWNER_SCOPE_BLOCKED"
    context = read_context(cwd, owner=str(owner), grants=(tmp_path,), dangerous=[str(cwd / "restricted")])
    assert context.granted_external_roots == (cwd.resolve(),)
    assert context.check(Path("note.txt")).allowed
    assert context.check(Path(".env")).code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert context.check(Path("restricted/child.txt")).code == "PATH_DANGEROUS_ROOT_BLOCKED"
    narrowed = read_context(cwd, owner=str(owner), grants=(cwd,),
                            boundary={"read_scope_mode": "exact", "allowed_read_roots": ["one.txt"]})
    assert narrowed.granted_external_roots == ((cwd / "one.txt").resolve(),)
    assert not narrowed.check(Path("two.txt")).allowed


def test_host_home_exemption_is_frozen_before_plugin_environment_changes(tmp_path, monkeypatch):
    home = tmp_path / "data"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    context = read_context(home, dangerous=[str(tmp_path)])
    payload = context.to_payload()
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "other"))
    monkeypatch.setenv("HOME", str(tmp_path / "plugin-home"))
    restored = WorkspaceReadContext.from_payload(payload)
    assert context.path_policy.check(home / "note.txt").allowed
    assert restored.check(Path("note.txt")).allowed
    assert not restored.path_policy.check(tmp_path / "other/note.txt").allowed
    assert restored.path_policy.agent_home_root == home.resolve()


def test_owner_layout_and_unscoped_external_policy_keep_distinct_data_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "environment-home"))
    owner = tmp_path / "configured-home/owners/local/main"
    context = read_context(tmp_path / "work", owner=str(owner), grants=(tmp_path / "work",))
    assert context.path_policy.agent_home_root != context.external_policy.agent_home_root
    assert WorkspaceReadContext.from_payload(context.to_payload()) == context


@pytest.mark.parametrize("field,value", [
    ("version", "2"), ("cwd", "."), ("cwd", "/tmp/../private"), ("cwd", "/tmp/\x00bad"),
    ("read_roots", "not-a-list"), ("read_roots", ["relative"]), ("read_roots", ["/"]),
    ("granted_external_roots", ["/"]), ("path_policy", {}),
])
def test_invalid_context_never_guesses_missing_or_wider_paths(tmp_path, field, value):
    payload = read_context(tmp_path).to_payload()
    payload[field] = value
    with pytest.raises(ValueError):
        WorkspaceReadContext.from_payload(payload)


def test_payload_is_independent_and_policy_values_are_not_normalized_again(tmp_path):
    context = read_context(tmp_path)
    payload = context.to_payload()
    payload["read_roots"].clear()
    assert context.read_roots == (tmp_path.resolve(),)
    denied = WorkspaceReadContext.from_payload(payload)
    assert denied.check(Path(".")).code == "PATH_READ_SCOPE_BLOCKED"
    payload = context.to_payload()
    payload["path_policy"]["mode"] = "FULL"
    with pytest.raises(ValueError):
        WorkspaceReadContext.from_payload(payload)
    invalid = replace(context, external_policy=replace(context.external_policy, owner_scope_root=tmp_path))
    with pytest.raises(ValueError):
        WorkspaceReadContext.from_payload(invalid.to_payload())
