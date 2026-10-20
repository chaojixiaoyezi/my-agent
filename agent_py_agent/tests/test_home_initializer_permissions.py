# LLM: C 真实初始化与只读 W 消费的权限合同；只用临时目录/echo，不放宽 W 或修改进程外 umask。
# 模块用途: 验证宽松宿主 umask 下规范新资源仍私有，以及既有内容和危险链接边界。
import os
import stat

import pytest

from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_resources import (
    WorkerSharedResourceError,
)
from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    resolve_owner_home,
)
from agent_py_agent.tests.test_gateway_multiprocess_worker_agent import _shared_snapshot, worker


# LLM: 与原 worker fixture 使用完全相同真实 C/冻结参数，只在本测试生命周期内控制 mask 并保证恢复。
# 函数用途: 构造新共享 home，覆盖生产常见三类 umask。
@pytest.fixture(params=[0o002, 0o022, 0o077], ids=["0002", "0022", "0077"])
def masked_worker(request, tmp_path, monkeypatch):
    previous = os.umask(request.param)
    try:
        yield worker.__wrapped__(tmp_path, monkeypatch)
    finally:
        os.umask(previous)


def test_new_c_resources_work_with_readonly_real_worker(masked_worker):
    frozen, config, root, owner, _ = masked_worker
    home = frozen.launch.owner_root
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "config").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "templates/SOUL.md").stat().st_mode) == 0o600
    before = _shared_snapshot(home)
    workspace_mode = root.stat().st_mode
    pool = create_worker_pool(frozen, config, root)
    result = ensure_owner_home(home, owner)
    assert stat.S_IMODE(result.home_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(result.soul_md.stat().st_mode) == 0o600
    agent = pool.get(owner)
    assert agent.home_paths.owner_home_dir == result.home_dir
    assert agent.local_store.get_record("absent") is None
    assert _shared_snapshot(home) == before
    assert root.stat().st_mode == workspace_mode


# LLM: 两个初始化入口必须分别核验，不能靠 shared 的调用先替 owner 修好形状。
# 函数用途: 为测试返回一次规范初始化和首个种子路径，不接用户真实目录。
def _case(tmp_path, kind):
    home = tmp_path / "home"
    if kind == "shared":
        return home, lambda: ensure_my_agent_home(home), home / "templates", home / "templates/SOUL.md"
    owner = OwnerIdentity.provider_user("test", "one")
    result = resolve_owner_home(home, owner)
    return home, lambda: ensure_owner_home(home, owner), result.home_dir, result.soul_md


@pytest.mark.parametrize("kind", ["shared", "owner"])
def test_existing_safe_directory_and_custom_seed_are_unchanged(tmp_path, kind):
    _, initialize, directory, seed = _case(tmp_path, kind)
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    seed.write_text("custom persona", encoding="utf-8")
    seed.chmod(0o644)
    before = (directory.stat().st_ino, directory.stat().st_mode, seed.stat().st_ino, seed.stat().st_mode, seed.read_bytes())
    initialize()
    assert (directory.stat().st_ino, directory.stat().st_mode, seed.stat().st_ino, seed.stat().st_mode, seed.read_bytes()) == before


@pytest.mark.parametrize("kind", ["shared", "owner"])
def test_managed_directory_symlink_is_rejected_before_external_seed(tmp_path, kind):
    _, initialize, directory, _ = _case(tmp_path, kind)
    directory.parent.mkdir(parents=True)
    external = tmp_path / "unrelated_owner"
    external.mkdir()
    (external / "sentinel").write_bytes(b"untouched")
    directory.symlink_to(external, target_is_directory=True)
    with pytest.raises(NoFollowPathError):
        initialize()
    assert sorted(p.name for p in external.iterdir()) == ["sentinel"]
    assert (external / "sentinel").read_bytes() == b"untouched"


@pytest.mark.parametrize("kind", ["shared", "owner"])
def test_seed_symlink_is_rejected_without_touching_target(tmp_path, kind):
    _, initialize, directory, seed = _case(tmp_path, kind)
    directory.mkdir(parents=True)
    external = tmp_path / "external.txt"
    external.write_bytes(b"untouched")
    seed.symlink_to(external)
    with pytest.raises(NoFollowPathError):
        initialize()
    assert external.read_bytes() == b"untouched"
    assert seed.is_symlink()


def test_existing_group_writable_directory_is_not_chmoded_or_accepted(masked_worker):
    frozen, config, root, _, _ = masked_worker
    path = frozen.launch.owner_root / "config"
    path.chmod(0o775)
    before = (path.stat().st_ino, path.stat().st_mode)
    with pytest.raises(WorkerSharedResourceError, match="resource_permissions"):
        create_worker_pool(frozen, config, root)
    assert (path.stat().st_ino, path.stat().st_mode) == before


def test_portable_canonical_root_rejects_internal_link_and_default_anchor_is_preserved(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs

    root = tmp_path / "root"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / "linked").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(nofollow_fs, "_supports_dir_fd", lambda: False)
    with pytest.raises(NoFollowPathError):
        nofollow_fs.ensure_private_dir(root / "linked/new", canonical_root=root)
    assert list(external.iterdir()) == []
    nofollow_fs.ensure_private_dir(root / "linked/new")
    assert stat.S_IMODE((external / "new").stat().st_mode) == 0o700


def test_portable_exclusive_seed_loses_create_race_without_overwriting(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs

    target = tmp_path / "seed.txt"
    original_open = os.open
    monkeypatch.setattr(nofollow_fs, "_supports_dir_fd", lambda: False)

    def concurrent_create(path, flags, mode=0o777, **kwargs):
        target.write_bytes(b"winner")
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(nofollow_fs.os, "open", concurrent_create)
    with pytest.raises(FileExistsError):
        nofollow_fs.create_private_file_beneath(tmp_path, (target.name,))
    assert target.read_bytes() == b"winner"


@pytest.mark.parametrize("location", ["root", "shared_parent", "category"])
def test_builtin_mirror_rejects_root_or_parent_symlink_without_external_write(tmp_path, monkeypatch, location):
    from agent_py_agent.agent.capability import builtin_seed

    source = tmp_path / "source"
    skill = source / "category/one"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# skill", encoding="utf-8")
    shared = tmp_path / "shared"
    shared.mkdir()
    target = shared / "builtin"
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel").write_bytes(b"untouched")
    if location == "root":
        target.symlink_to(external, target_is_directory=True)
    elif location == "shared_parent":
        shared.rmdir()
        shared.symlink_to(external, target_is_directory=True)
    else:
        target.mkdir()
        original_rmtree = builtin_seed.shutil.rmtree

        def insert_category_link(path, **kwargs):
            original_rmtree(path, **kwargs)
            target.mkdir()
            (target / "category").symlink_to(external, target_is_directory=True)

        monkeypatch.setattr(builtin_seed.shutil, "rmtree", insert_category_link)
    with pytest.raises(NoFollowPathError):
        builtin_seed._mirror_builtin(source, target)
    assert sorted(p.name for p in external.iterdir()) == ["sentinel"]
    assert (external / "sentinel").read_bytes() == b"untouched"
