# LLM: 使用公开CLI构造入口与真实私有Home/W消费原语，离线echo不调用模型、不启动服务；不得替换owner解析或放宽W权限。
# 模块用途: 守住首次公开构造先初始化规范Home，再创建用户工作区的顺序，避免宽松umask污染受管父目录。
import json
import os
import stat
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_resources import (
    WorkerSharedResourceError,
)
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
from agent_py_agent.cli.common import make_agent


# LLM: YAML位于Home之外，MY_AGENT_HOME只指向本case临时目录；不预建规范Home，不给构造器补权限或配置。
# 函数用途: 按公开参数装配离线echo配置，保留真实CLI配置及owner/workspace解析。
def _arguments(tmp_path, monkeypatch, *, external=None):
    home = tmp_path / "home"
    config = tmp_path / "config.yaml"
    config.write_text(f"my_agent_home: {json.dumps(str(home))}\nmodel_backend: echo\nprompt_files: []\naccess_mode: full-access\n", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.delenv("MY_AGENT_GATEWAY_WORKER", raising=False)
    monkeypatch.setattr(mode, "_WORKER_PARTITION", None)
    return home, SimpleNamespace(config=str(config), workspace_root=external)


@pytest.mark.parametrize("mask", [0o002, 0o022, 0o077], ids=["0002", "0022", "0077"])
def test_fresh_public_agent_prepares_private_home_before_default_workspace(tmp_path, monkeypatch, mask):
    home, args = _arguments(tmp_path, monkeypatch)
    assert not home.exists()
    previous = os.umask(mask)
    try:
        agent = make_agent(args)
    finally:
        os.umask(previous)
    assert agent.root == agent.home_paths.owner_home_dir == home / "owners/local/main"
    for path in (home, home / "owners", home / "owners/local", agent.root):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    partition = WorkerPartition(0, 2, "public-home-order")
    launch = mode.WorkerLaunch(partition, home / "gateway", home, os.getppid(), workspace_roots=(agent.root,))
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"])
    assert mode.establish_worker_mode(launch) == {"ok": True}
    frozen = freeze_worker_bootstrap(launch)
    pool = create_worker_pool(frozen, agent.config, agent.root)
    owner = next(OwnerIdentity.provider_user("test", f"u{i}") for i in range(30)
                 if owner_worker(OwnerIdentity.provider_user("test", f"u{i}"), 2) == 0)
    canonical = ensure_owner_home(home, owner)
    assert pool.get(owner).home_paths.owner_home_dir == canonical.home_dir


def test_public_external_workspace_retains_original_creation_and_existing_permissions(tmp_path, monkeypatch):
    workspace = tmp_path / "user-workspace/new"
    workspace.parent.mkdir(mode=0o755)
    workspace.parent.chmod(0o755)
    home, args = _arguments(tmp_path, monkeypatch, external=str(workspace))
    previous = os.umask(0o002)
    try:
        agent = make_agent(args)
    finally:
        os.umask(previous)
    assert agent.root == workspace
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o775
    assert stat.S_IMODE(workspace.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


@pytest.mark.parametrize("relative", ["owners", "owners/local", "owners/local/main"])
def test_public_managed_symlink_is_rejected_before_external_workspace_creation(tmp_path, monkeypatch, relative):
    home, args = _arguments(tmp_path, monkeypatch)
    link = home / relative
    link.parent.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "unrelated"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(NoFollowPathError):
        make_agent(args)
    assert link.is_symlink()
    assert not outside.exists()


def test_public_existing_home_permissions_are_not_repaired(tmp_path, monkeypatch):
    home, args = _arguments(tmp_path, monkeypatch)
    home.mkdir(mode=0o775)
    home.chmod(0o775)
    before = (home.stat().st_ino, home.stat().st_mode)
    agent = make_agent(args)
    assert agent.home_paths.root == home
    assert (home.stat().st_ino, home.stat().st_mode) == before


def test_existing_group_writable_home_remains_rejected_by_worker_parent_gate(tmp_path, monkeypatch):
    home, args = _arguments(tmp_path, monkeypatch)
    home.mkdir(mode=0o775)
    home.chmod(0o775)
    agent = make_agent(args)
    partition = WorkerPartition(0, 2, "existing-unsafe-home")
    launch = mode.WorkerLaunch(partition, home / "gateway", home, os.getppid(), workspace_roots=(agent.root,))
    monkeypatch.setenv("MY_AGENT_GATEWAY_WORKER", mode.worker_environment(launch, {})["MY_AGENT_GATEWAY_WORKER"])
    assert mode.establish_worker_mode(launch) == {"ok": True}
    frozen = freeze_worker_bootstrap(launch)
    before = (home.stat().st_ino, home.stat().st_mode)
    with pytest.raises(WorkerSharedResourceError, match="resource_permissions"):
        create_worker_pool(frozen, agent.config, agent.root)
    assert (home.stat().st_ino, home.stat().st_mode) == before
