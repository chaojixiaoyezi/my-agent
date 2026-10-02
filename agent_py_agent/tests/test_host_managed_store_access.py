"""H2 合同单测：宿主托管存储（插件安装库、包库）对模型的文件工具和 shell 都不开放。

来源：ae 的 C3 真实补测（2026-10-01，证据 ~/.my-agent/decision-evidence/c3-unhit-branches-39059/）。Gateway 请求的工作区根就是
owner home，Goal 续跑被宿主拒绝后，模型用 run_command 执行 `unzip -p data/plugins/packages/<旧包摘要>.zip steps/step2.md` 读出了已停用、
已换代的旧包，照旧完成任务。宿主自己的读包路径是对的；漏洞在路径策略把 owner home 整个放行（owner 墙内“自己家随便读写”、
无墙时整个数据根豁免），shell 沙箱也同样放行 owner home。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \
        agent_py_agent/tests/test_host_managed_store_access.py

真实沙箱用例按平台跳过：macOS 需要 sandbox-exec，Linux 需要可用的 bwrap（Docker Linux 车道覆盖）。
"""

from __future__ import annotations

import os
import platform
import shutil
import zipfile
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling.sandbox import SandboxSpec, build_bwrap_argv
from agent_py_agent.agent.workspace_read_context import WorkspaceReadContext

IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"


def _home(tmp_path: Path) -> dict[str, Path]:
    """规范布局的数据根：本机主用户与一个飞书用户各有插件库；主用户工作区里另有用户自己的 zip 和同名 data/plugins 目录。"""
    root = (tmp_path / "my-agent-home").resolve()
    owner = root / "owners" / "local" / "main"
    store = owner / "data" / "plugins"
    (store / "packages").mkdir(parents=True)
    with zipfile.ZipFile(store / "packages" / "old.zip", "w") as archive:
        archive.writestr("steps/step2.md", "OLD-PACKAGE-STEP-2")
    (store / "environments" / "env-1" / "steps").mkdir(parents=True)
    (store / "environments" / "env-1" / "steps" / "step2.md").write_text("OLD-PACKAGE-STEP-2", encoding="utf-8")
    other_store = root / "owners" / "providers" / "feishu" / "users" / "u-1" / "data" / "plugins"
    other_store.mkdir(parents=True)
    (other_store / "installations.json").write_text("{}", encoding="utf-8")
    workspace = owner / "ws"
    (workspace / "data" / "plugins").mkdir(parents=True)
    (workspace / "data" / "plugins" / "mine.md").write_text("USER-PLUGIN-NOTE", encoding="utf-8")
    with zipfile.ZipFile(workspace / "user.zip", "w") as archive:
        archive.writestr("readme.md", "USER-ZIP")
    (workspace / "notes.md").write_text("OLD-PACKAGE-STEP-2 is mentioned by the user too", encoding="utf-8")
    return {"root": root, "owner": owner, "store": store, "other_store": other_store, "workspace": workspace}


def test_owner_scoped_policy_blocks_the_store_but_not_user_files(tmp_path):
    home = _home(tmp_path)
    policy = PathAccessPolicy.from_values(owner_scope_root=home["owner"])
    store = home["store"]

    for target in (store, store / "packages" / "old.zip", store / "environments" / "env-1" / "steps" / "step2.md"):
        decision = policy.check(target)
        assert not decision.allowed and decision.code == "PATH_HOST_MANAGED_STORE_BLOCKED"
        assert decision.dangerous_root == str(store)
    # 用户自己的 zip、工作区里同名的 data/plugins 目录、名字只是前缀相同的目录都照常可读。
    for target in (home["workspace"] / "user.zip", home["workspace"] / "data" / "plugins" / "mine.md",
                   home["owner"] / "data" / "pluginsX" / "a.md", home["owner"] / "data" / "scheduler"):
        assert policy.check(target).allowed


def test_full_access_policy_blocks_every_owner_store(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setenv("MY_AGENT_HOME", str(home["root"]))
    policy = PathAccessPolicy.from_values(mode="full")

    assert policy.check(home["store"] / "packages" / "old.zip").code == "PATH_HOST_MANAGED_STORE_BLOCKED"
    assert policy.check(home["other_store"] / "installations.json").code == "PATH_HOST_MANAGED_STORE_BLOCKED"
    assert policy.check(home["workspace"] / "user.zip").allowed
    assert policy.check(tmp_path / "elsewhere.txt").allowed


def test_plugin_read_context_round_trip_keeps_the_rule(tmp_path):
    """隔离插件进程按协议字段重建策略，宿主托管存储同样拒绝（agent_home_root 随协议运输）。"""
    home = _home(tmp_path)
    policy = PathAccessPolicy.from_values(owner_scope_root=home["owner"])
    context = WorkspaceReadContext(home["owner"], (home["owner"],), policy, (), PathAccessPolicy.from_values())

    rebuilt = WorkspaceReadContext.from_payload(context.to_payload())

    assert rebuilt.check(Path("data/plugins/packages/old.zip")).code == "PATH_HOST_MANAGED_STORE_BLOCKED"
    assert rebuilt.check(Path("ws/user.zip")).allowed


def _registry(owner: Path):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    return ToolRegistry(ToolRegistryParams(
        workspace_root=owner, max_chars=6000, max_entries=200, max_matches=50, web_max_chars=12000, http_timeout=30,
        catalog_limit=20, retrieval_limit=3, vector_search_enabled=False, shell_tool_timeout=30,
        owner_scope_root=str(owner),
    ))


def test_file_tools_never_return_store_contents(tmp_path):
    home = _home(tmp_path)
    tools = _registry(home["owner"]).tools

    denied = tools["read_file"].execute({"path": "data/plugins/environments/env-1/steps/step2.md"})
    assert not denied.ok and denied.error_code == "PATH_HOST_MANAGED_STORE_BLOCKED"
    assert tools["read_file"].execute({"path": "ws/notes.md"}).ok

    searched = tools["search_text"].execute({"path": ".", "query": "OLD-PACKAGE-STEP-2", "output_mode": "files_with_matches"})
    assert searched.ok and "ws/notes.md" in searched.output and "data/plugins/environments" not in searched.output

    found = tools["find_files"].execute({"path": ".", "pattern": "*.md"})
    assert "ws/data/plugins/mine.md" in found.output and "environments" not in found.output
    listed = tools["list_files"].execute({"path": ".", "recursive": True})
    assert "ws/user.zip" in listed.output and "data/plugins/packages" not in listed.output


def test_shell_sandbox_hides_only_the_owner_store_when_scoped(tmp_path):
    from agent_py_agent.agent.tooling.shell import _host_managed_hidden_paths

    home = _home(tmp_path)

    # owner 隔离：只盖本 owner 的（其它 owner 的家本来就看不到，不为隐藏去暴露它们的路径）。
    assert _host_managed_hidden_paths(str(home["owner"]), "") == (home["store"],)
    # Full Access：没有 owner 墙，盖数据根下全部 owner 的。
    assert set(_host_managed_hidden_paths("", str(home["owner"]))) == {home["store"], home["other_store"]}
    # 非规范布局推不出数据根时什么都不盖。
    assert _host_managed_hidden_paths(str(tmp_path / "loose-owner"), "") == ()


def test_seatbelt_denies_the_store_after_every_allow(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    spec = AttemptSandboxSpec(
        attempt_view=home["workspace"], staging_root=home["workspace"], shared_workspace=home["owner"],
        owner_home=home["owner"], private_read_roots=(home["root"],), hidden_paths=(home["store"],),
        macos_sandbox_exec="/usr/bin/sandbox-exec",
    )
    for current in (spec, AttemptSandboxSpec(**{**spec.__dict__, "full_access": True})):
        profile = AttemptExecutionSandbox(current)._macos_argv(["/bin/true"])[2]
        # Seatbelt 后写覆盖先写：拒绝必须是最后一条，压过 owner home 的读放行；Full Access 同样生效。
        assert profile.splitlines()[-1] == f'(deny file-read* file-write* (subpath "{home["store"]}"))'


def test_bwrap_covers_the_store_with_a_read_only_tmpfs_last(tmp_path):
    home = _home(tmp_path)
    for full_access in (False, True):
        argv = build_bwrap_argv(SandboxSpec(
            owner_home=home["owner"], workspace=home["workspace"], bwrap_path="/usr/bin/bwrap",
            full_access=full_access, hidden_paths=(home["store"],),
        ))
        store = str(home["store"])
        index = argv.index("--tmpfs")
        assert argv[index:index + 4] == ["--tmpfs", store, "--remount-ro", store]
        # 在所有 bind 之后、chdir 之前：盖住可写父目录里的这一层。
        assert index > max(i for i, item in enumerate(argv) if item in {"--bind", "--ro-bind"})
        assert argv[-2] == "--chdir"


def _shell(owner: Path, *, scoped: bool):
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    root = owner.parents[2]
    options = (ShellToolOptions(owner_scope_root=str(owner), host_private_roots=(str(root),)) if scoped
               else ShellToolOptions(protected_persona_root=str(owner)))
    return ShellTool(owner, options=options)


def _sandbox_ready() -> bool:
    if IS_MACOS:
        return shutil.which("sandbox-exec") is not None
    return IS_LINUX and shutil.which("bwrap") is not None


@pytest.mark.skipif(not (IS_MACOS or IS_LINUX), reason="需要 macOS Seatbelt 或 Linux bwrap")
@pytest.mark.parametrize("scoped", [True, False])
def test_real_shell_cannot_read_the_store(tmp_path, scoped):
    if not _sandbox_ready():
        pytest.skip("平台沙箱不可用")
    home = _home(tmp_path)
    shell = _shell(home["owner"], scoped=scoped)

    def run(command: str):
        return shell.execute({"command": command, "working_dir": str(home["owner"])})

    # 原场景：从包库直接解出旧包内容；读解压环境里的同一文件。两条都必须失败，且输出里没有旧包内容。
    unzip = shutil.which("unzip")
    if unzip:
        leaked = run(f"{unzip} -p {home['store'] / 'packages' / 'old.zip'} steps/step2.md")
        assert not leaked.ok and "OLD-PACKAGE-STEP-2" not in leaked.output
    leaked = run(f"cat {home['store'] / 'environments' / 'env-1' / 'steps' / 'step2.md'}")
    assert not leaked.ok and "OLD-PACKAGE-STEP-2" not in leaked.output
    # 用户自己的 zip 与工作区里同名的 data/plugins 目录不受影响。
    assert run(f"cat {home['workspace'] / 'data' / 'plugins' / 'mine.md'}").ok
    if unzip:
        assert "USER-ZIP" in run(f"{unzip} -p {home['workspace'] / 'user.zip'} readme.md").output
    assert os.path.exists(home["store"] / "packages" / "old.zip")


def test_declared_store_matches_the_canonical_owner_layout(tmp_path):
    """守卫：路径策略里的存储声明与 owner 反推（为了能原样打进插件 SDK 写成路径片段）必须和规范布局一致。"""
    from agent_py_agent.agent.path_access_policy import (
        HOST_MANAGED_OWNER_STORE_PARTS,
        owner_home_containing,
    )
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    root = tmp_path.resolve()
    owners = [resolve_owner_home(root, identity) for identity in (
        OwnerIdentity.local_main(), OwnerIdentity.provider_user("feishu", "u-1"),
        OwnerIdentity.provider_group("feishu", "g-1"))]

    assert (owners[0].plugins_dir.relative_to(owners[0].home_dir).parts,) == HOST_MANAGED_OWNER_STORE_PARTS
    for owner in owners:
        assert owner_home_containing(owner.plugins_dir / "packages" / "x.zip", root) == owner.home_dir
        assert owner_home_containing(owner.home_dir, root) == owner.home_dir


def test_find_files_python_fallback_skips_the_store(tmp_path, monkeypatch):
    """没有 rg 时 find_files 走 Python 后备遍历，同样不交出存储里的文件。"""
    from agent_py_agent.agent.tooling import _filesystem_find

    home = _home(tmp_path)
    monkeypatch.setattr(_filesystem_find.shutil, "which", lambda _name: None)
    found = _registry(home["owner"]).tools["find_files"].execute({"path": ".", "pattern": "*.md"})

    assert found.ok and "ws/data/plugins/mine.md" in found.output
    assert "environments" not in found.output
