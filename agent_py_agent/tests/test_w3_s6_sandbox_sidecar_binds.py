"""S6：父目录已只读或已隐藏的数据库侧车/锁文件不再逐项 try-bind；其余侧车保留逐项只读与裁剪重试兜底。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.path_access_policy import HOST_MANAGED_OWNER_STORE_PARTS
from agent_py_agent.agent.tooling import sandbox, shell

_ZERO_LIMITS = sandbox.ResourceLimits(nice_increment=0, oom_score_adj=0)
_SIDECARS = ("runtime.db-wal", "runtime.db-shm", "state.json.lock", "work/task.db-wal", "store/index.db-wal")
_FORMS = {
    "owner_rw": {},
    "task_boundary": {"write_roots": ("work",)},
    "full_access": {"full_access": True},
    "read_only_root": {"read_only_root": True, "write_roots": ("work",)},
}


def _tree(tmp_path, *, sidecars=True):
    home = tmp_path.resolve() / ".my-agent/owners/local/main"
    for name in ("work", "store"):
        (home / name).mkdir(parents=True)
    (home / "runtime.db").write_text("db")
    if sidecars:
        for name in _SIDECARS:
            (home / name).write_text("sidecar")
    return home


def _spec(home, form):
    options = dict(_FORMS[form])
    if "write_roots" in options:
        options["write_roots"] = tuple(home / name for name in options["write_roots"])
    return sandbox.SandboxSpec(
        owner_home=home, workspace=home, protected_persona_root=home, bwrap_path="/fake/bwrap",
        read_only_paths=(home / "runtime.db", *(home / name for name in _SIDECARS)),
        hidden_paths=(home / "store",), resource_limits=_ZERO_LIMITS, **options)


def _try_binds(argv):
    # 只看侧车/锁文件；缺失人格目录的 try-bind 占位不属于本切片。
    return {argv[index + 2] for index, flag in enumerate(argv)
            if flag == "--ro-bind-try" and sandbox._is_ephemeral_readonly_sidecar(Path(argv[index + 2]))}


def _original_argv(monkeypatch, spec):
    with monkeypatch.context() as scoped:
        scoped.setattr(sandbox, "_without_redundant_sidecar_mounts", lambda argv: argv)
        return sandbox.build_bwrap_argv(spec)


# 函数用途: 证明新 argv 只是从原 argv 删去若干同源侧车 try-bind 三元组，其余参数与顺序逐项不变。
def _removed_sidecar_triples(original, pruned):
    removed, index, kept = [], 0, 0
    while index < len(original):
        triple = original[index:index + 3]
        sidecar = (triple[0] == "--ro-bind-try" and len(triple) == 3 and triple[1] == triple[2]
                   and sandbox._is_ephemeral_readonly_sidecar(Path(triple[2])))
        if sidecar and pruned[kept:kept + 3] != triple:
            removed.append(triple[2])
            index += 3
            continue
        assert kept < len(pruned) and pruned[kept] == original[index], (index, original[index:index + 3])
        index, kept = index + 1, kept + 1
    assert kept == len(pruned)
    return removed


def test_sidecars_under_a_readonly_owner_home_are_omitted(tmp_path):
    home = _tree(tmp_path)
    argv = sandbox.build_bwrap_argv(_spec(home, "task_boundary"))
    mounts = sandbox._mount_snapshot(argv)
    # owner home 已整体 --ro-bind：侧车随父目录只读，不再逐项 try-bind（exec 前消失也不会让 bwrap 失败）。
    assert _try_binds(argv) == {str(home / "work/task.db-wal")}
    for name in ("runtime.db-wal", "runtime.db-shm", "state.json.lock"):
        mount = sandbox._effective_mount(home / name, mounts)
        assert mount.arguments == ("--ro-bind", str(home), str(home))


def test_sidecars_under_a_hidden_directory_are_omitted(tmp_path):
    home = _tree(tmp_path)
    for form in ("owner_rw", "task_boundary", "full_access"):
        argv = sandbox.build_bwrap_argv(_spec(home, form))
        assert str(home / "store/index.db-wal") not in _try_binds(argv), form
        mount = sandbox._effective_mount(home / "store/index.db-wal", sandbox._mount_snapshot(argv))
        store = str(home / "store")
        assert mount.arguments == ("--tmpfs", store, "--remount-ro", store), form


def test_sidecars_with_a_writable_parent_keep_explicit_readonly_entries(tmp_path):
    home = _tree(tmp_path)
    argv = sandbox.build_bwrap_argv(_spec(home, "owner_rw"))
    mounts = sandbox._mount_snapshot(argv)
    kept = {str(home / name) for name in ("runtime.db-wal", "runtime.db-shm", "state.json.lock", "work/task.db-wal")}
    assert _try_binds(argv) == kept
    for path in kept:
        mount = sandbox._effective_mount(Path(path), mounts)
        assert mount.arguments == ("--ro-bind-try", path, path) and not mount.writable
    # 保留的侧车仍由裁剪重试兜底：exec 前消失时只删它，再次启动。
    wal = str(home / "runtime.db-wal")
    pruned = sandbox.prune_transient_bwrap_sidecar_mounts(
        argv, f"bwrap: Can't find source path {wal}: No such file or directory")
    assert pruned is not None and wal not in _try_binds(pruned) and len(pruned) == len(argv) - 3


def test_read_only_root_keeps_only_sidecars_inside_write_roots(tmp_path):
    home = _tree(tmp_path)
    argv = sandbox.build_bwrap_argv(_spec(home, "read_only_root"))
    assert _try_binds(argv) == {str(home / "work/task.db-wal")}
    mount = sandbox._effective_mount(home / "runtime.db-wal", sandbox._mount_snapshot(argv))
    assert mount.arguments[0] == "--ro-bind" and mount.arguments[1] == mount.arguments[2]


@pytest.mark.parametrize("sidecars", [True, False], ids=["sidecars-present", "sidecars-absent"])
@pytest.mark.parametrize("form", sorted(_FORMS))
def test_omission_changes_no_effective_mount(tmp_path, monkeypatch, form, sidecars):
    home = _tree(tmp_path, sidecars=sidecars)
    spec = _spec(home, form)
    original = _original_argv(monkeypatch, spec)
    pruned = sandbox.build_bwrap_argv(spec)
    removed = set(_removed_sidecar_triples(original, pruned))
    if not sidecars:
        assert pruned == original
    before, after = sandbox._mount_snapshot(original), sandbox._mount_snapshot(pruned)
    probes = {home, home / "work", home / "store", *spec.read_only_paths, *(home / "work/new-file",)}
    for path in probes:
        old, new = sandbox._effective_mount(path, before), sandbox._effective_mount(path, after)
        if str(path) not in removed:
            assert new == old, path
            continue
        # 被省略的侧车：要么原本就被后续覆盖（视图相同），要么由同源硬 RO 祖先提供同一宿主文件的只读视图。
        assert new == old or (new.arguments[0] == "--ro-bind" and new.source == new.target
                              and new.target in path.parents), path
        assert not new.writable and not new.alias
    for path in spec.read_only_paths:
        if path.exists():
            assert not sandbox._effective_mount(path, after).writable, path


@pytest.mark.parametrize("sidecars", [True, False], ids=["sidecars-present", "sidecars-absent"])
def test_multi_owner_wall_invariants_hold_with_and_without_sidecars(tmp_path, monkeypatch, sidecars):
    root = tmp_path.resolve() / "app/.my-agent/data"
    main = root / "owners/local/main"
    for home in (main, *(root / f"owners/providers/feishu/users/user_{index:02d}" for index in range(3))):
        (home / "persona").mkdir(parents=True)
        (home / "AGENTS.md").write_text("# rules\n")
        for parts in HOST_MANAGED_OWNER_STORE_PARTS:
            home.joinpath(*parts).mkdir(parents=True, exist_ok=True)
    (main / "work").mkdir()
    read_only = shell.host_readonly_paths_for("", str(main))
    hidden = shell._host_managed_hidden_paths("", str(main))
    if sidecars:
        for path in read_only:
            if sandbox._is_ephemeral_readonly_sidecar(path) and path.parent.is_dir():
                path.write_text("sidecar")
    assert any(path.exists() for path in read_only if sandbox._is_ephemeral_readonly_sidecar(path)) is sidecars
    for options in ({"full_access": True}, {}, {"write_roots": (main / "work",)}):
        spec = sandbox.SandboxSpec(owner_home=main, workspace=main, protected_persona_root=main,
                                   bwrap_path="/fake/bwrap", read_only_paths=read_only, hidden_paths=hidden,
                                   resource_limits=_ZERO_LIMITS, **options)
        argv = sandbox.build_bwrap_argv(spec)
        removed = _removed_sidecar_triples(_original_argv(monkeypatch, spec), argv)
        # 结构化写边界把 owner home 整体只读：本 owner 的侧车随父目录保护而省略。
        assert bool(removed) is (sidecars and "write_roots" in options), (options, removed)
        mounts = sandbox._mount_snapshot(argv)
        # 隐藏区仍是最终只读空目录，所有现存受保护文件（含侧车）在沙箱里仍不可写。
        for path in hidden:
            mount = sandbox._effective_mount(path, mounts)
            assert mount.source is None and mount.arguments == ("--tmpfs", str(path), "--remount-ro", str(path))
        for path in read_only:
            if path.exists():
                assert not sandbox._effective_mount(path, mounts).writable, (options, path)


def test_a_deeper_mount_below_a_sidecar_name_keeps_the_entry():
    argv = ["bwrap", "--ro-bind", "/data", "/data", "--ro-bind-try", "/data/x.db-wal", "/data/x.db-wal",
            "--bind", "/elsewhere", "/data/x.db-wal/child"]
    # 侧车名下还有更深挂载时，省略会改变其挂载点所在的文件系统：保守保留。
    assert sandbox._without_redundant_sidecar_mounts(argv) == argv


def test_a_later_try_bind_parent_does_not_count_as_cover():
    argv = ["bwrap", "--bind", "/data", "/data", "--ro-bind-try", "/data/x.db-wal", "/data/x.db-wal",
            "--ro-bind-try", "/data", "/data"]
    # 后发 try-bind 的源可能在 exec 前消失而被跳过，不能证明它覆盖了侧车；可写父目录下的侧车保留。
    assert sandbox._without_redundant_sidecar_mounts(argv) == argv
