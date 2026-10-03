"""H3 合同单测：宿主托管文件对模型只读，宿主凭据对文件工具不可读。

来源：be 复审语义记忆时发现管理员的文件工具和命令能直接写 ~/.my-agent/config/（绕过参数中心的边界键、修改账本、撤销和
manage_models 的检查）；3a 定为硬门（2026-10-02），随后扩成“宿主托管文件对模型只读”：
- 宿主配置：数据根 config/、system/config/，每个 owner home 的 config/ → PATH_HOST_CONFIG_WRITE_BLOCKED，读照常；
- 宿主运行状态（A 类，绝对只读，任何模式、不可穿透）：9b 家目录盘点的各项（权限/配额/策略文件及其 .lock、审计流水、
  runtime.db 及 -wal/-shm/-journal、workspace/runtime、audit、Curator 事务、记忆流水与候选、记忆归档、缓存、回收站、
  owner 级正式 skill 等），规范任务根的 data/pack_verification（ae 能力包核验记录）→ PATH_HOST_STATE_WRITE_BLOCKED，读照常；
- B 类（runs/、agents/、data/、tasks/）仍是 tool_runtime_ledger 的 forbidden_write_roots：只在隔离模式生效，可被本任务工作目录穿透；
- 宿主凭据：管理员密码、模型目录、共享模型档案、数据根 config/ 里的 YAML 配置及备份、数据根里 owner home 之外的 secrets
  目录 → 文件工具读写都拒 PATH_HOST_CREDENTIAL_BLOCKED；命令只拒写不拒读（读取不在 H3 范围，已知边界）。
- 命令沙箱：这些路径并进只读覆盖（Full Access 也生效）；Seatbelt 给每个受保护路径的上级目录加写拒绝，堵住“先改名上级目录
  再写”的绕过（探针证据 ~/.my-agent/decision-evidence/host-config-guard-design/）。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \\
        agent_py_agent/tests/test_host_files_access.py

真实沙箱用例按平台跳过：macOS 需要 sandbox-exec，Linux 需要可用的 bwrap（Docker Linux 车道覆盖）。
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.workspace_read_context import WorkspaceReadContext

IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"
SECRET = "SECRET-MARKER"
CONFIG_CODE = "PATH_HOST_CONFIG_WRITE_BLOCKED"
STATE_CODE = "PATH_HOST_STATE_WRITE_BLOCKED"
CREDENTIAL_CODE = "PATH_HOST_CREDENTIAL_BLOCKED"


# 9b 盘点 + 3a 口径（2026-10-02）里 owner home 下的 A 类宿主文件，逐项写死：文件、旁边的 .lock、SQLite 伴随文件，
# 目录给一个里面的文件。runtime.db-wal 夹具里写 "HOST"，其余同。
_STATE_FILES = ("permissions.json", "quota.json", "retention.json", "memory_policy.json", "skill_policy.json",
                "tool_policy.json", "audit_log.jsonl", "memory/ops.jsonl", "memory/candidates.jsonl")
OWNER_STATE_ITEMS = (
    *_STATE_FILES, *(f"{name}.lock" for name in _STATE_FILES),
    "runtime.db", "runtime.db-wal", "runtime.db-shm", "runtime.db-journal",
    "capability_requests/req-1.json", "temporary_grants/g-1.json", "compact/conversations/c.json", "logs/gateway.log",
    "audit/2026-10-02.jsonl", "workspace/runtime/workspaces/w-1/conversations/messages/m.jsonl",
    "workspace/runtime/services/gateway/gateway_state.json", "memory/curator/state.json",
    "memory/curator/transactions/t-1.json", "memory_archive/runtime_facts/r-1/task.json", "cache/retention/scan.json",
    "trash/item-1/meta.json", "skills/my-skill/SKILL.md", ".agents/skills/home-skill/SKILL.md",
    "data/scheduler/store.json", "data/decision/outcomes.jsonl", "data/context/calibration.json",
    "data/skill_proposals/p-1.json", "data/skill_learning/l-1.json", "data/artifact_backups/b-1/report.md",
    "data/verification/evidence.sqlite3", "data/verification/evidence.sqlite3-wal", "data/maintenance.json",
    "data/some-future-store/state.json",
    "agents/subagent-1/report.json", "agents/subagent_dispatch_log.jsonl",
)
# 仍属于模型的位置（3a 口径）：交付区 artifacts/、workspace/ 其余、tmp/、记忆正文、用户在家目录根的文件。
OWNER_MODEL_ITEMS = ("artifacts/report.md", "workspace/notes.md", "tmp/scratch.txt", "memory.md", "memory/daily/2026-10-02.md",
                     "notes/hello.txt", "memory/curatorX/a.json", "skills2/a.md")


# 函数用途: 写一个文件（自动建上级目录）。
def _put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# 函数用途: 造一个规范布局的数据根：宿主配置与凭据、本机主用户（配置、runtime.db、工作区、两个任务）、一个有配置的飞书用户、
#   一个还没有 config/ 的飞书用户，以及数据根里的一个密钥目录。返回各路径。
def _home(tmp_path: Path) -> dict[str, Path]:
    root = (tmp_path / "my-agent-home").resolve()
    config = root / "config"
    main = root / "owners" / "local" / "main"
    feishu = root / "owners" / "providers" / "feishu" / "users" / "u-1"
    bare = root / "owners" / "providers" / "feishu" / "users" / "u-2"
    task = main / "runs" / "2026-10-02" / "k1"
    for name in ("desktop.yaml", "desktop.yaml.bak-20261002", "desktop.supply-recovery-20260921.yaml"):
        _put(config / name, f"feishu_app_secret: {SECRET}\n")
    _put(config / "settings-changes.jsonl", '{"key": "x", "note": "LEDGER-MARKER"}\n')
    _put(config / "admin-password.json", f'{{"scrypt": "{SECRET}"}}')
    _put(config / "admin-password-attempts.json", "{}")
    _put(config / "shared-model-profiles.json", f'{{"k": "{SECRET}"}}')
    _put(config / "model-profiles" / "abc.json", f'{{"api_key": "{SECRET}"}}')
    _put(config / "tests" / "fixture.yaml", "a: 1\n")
    (root / "system" / "config").mkdir(parents=True)
    _put(main / "config" / "capability_config.yaml", "subagent_max: 3\n")
    for relative in OWNER_STATE_ITEMS:
        _put(main / relative, "HOST")
    _put(main / "ws" / "notes.md", "hello")
    _put(main / "ws" / "config" / "x.yaml", "user: 1\n")
    _put(main / "ws" / "secrets" / "token.txt", "USER-TOKEN")
    _put(main / "ws" / "desktop.yaml", "user file\n")
    _put(task / "work" / "out.md", "draft")
    _put(task / "data" / "pack_verification" / "originals.json", "{}")
    _put(task / "data" / "other.json", "{}")
    _put(main / "runs" / "2026-10-02" / "k2" / "data" / "pack_verification" / "r.jsonl", "{}\n")
    _put(feishu / "config" / "capability_config.yaml", "subagent_max: 1\n")
    _put(bare / "runtime.db", "HOST")
    _put(root / "releases" / "claude-tools" / "secrets" / "k.key", SECRET)
    return {"root": root, "config": config, "system_config": root / "system" / "config", "main": main,
            "feishu": feishu, "bare": bare, "ws": main / "ws", "task": task,
            "verification": task / "data" / "pack_verification"}


# 函数用途: 本机管理员的路径策略（没有 owner 墙，数据根取自 MY_AGENT_HOME）。
def _admin_policy(home: dict[str, Path], monkeypatch, mode: str = "full") -> PathAccessPolicy:
    monkeypatch.setenv("MY_AGENT_HOME", str(home["root"]))
    return PathAccessPolicy.from_values(mode=mode)


# ---------------------------------------------------------------- 路径策略


@pytest.mark.parametrize("mode", ["full", "normal"])
def test_admin_policy_blocks_writes_into_host_config_but_reads_them(tmp_path, monkeypatch, mode):
    home = _home(tmp_path)
    policy = _admin_policy(home, monkeypatch, mode)
    config, main, feishu, bare = home["config"], home["main"], home["feishu"], home["bare"]

    for target, root in [(config / "settings-changes.jsonl", config), (config / "new.txt", config), (config, config),
                         (home["system_config"] / "x.yaml", home["system_config"]),
                         (main / "config" / "capability_config.yaml", main / "config"),
                         (feishu / "config" / "new.yaml", feishu / "config"),
                         (bare / "config" / "capability_config.yaml", bare / "config")]:
        decision = policy.check_write(target)
        assert (decision.allowed, decision.code, decision.dangerous_root) == (False, CONFIG_CODE, str(root)), target
        assert "user_config" in decision.message and "manage_models" in decision.message
    # 读取照常；用户工作区里同名的 config/、名字只是前缀相同的目录都能写。
    for target in (config / "settings-changes.jsonl", main / "config" / "capability_config.yaml",
                   home["system_config"] / "x.yaml"):
        assert policy.check(target).allowed, target
    for target in (home["ws"] / "config" / "x.yaml", home["ws"] / "notes.md", home["root"] / "configs" / "x",
                   main / "configX" / "a", tmp_path / "elsewhere.txt"):
        assert policy.check_write(target).allowed, target


@pytest.mark.parametrize("mode", ["full", "normal"])
def test_admin_policy_blocks_writes_into_host_state_but_reads_them(tmp_path, monkeypatch, mode):
    home = _home(tmp_path)
    policy = _admin_policy(home, monkeypatch, mode)
    main, bare, verification = home["main"], home["bare"], home["verification"]
    other_task = main / "runs" / "2026-10-02" / "k2" / "data" / "pack_verification"

    for target, root in [(main / "runtime.db", main / "runtime.db"), (main / "runtime.db-wal", main / "runtime.db-wal"),
                         (bare / "runtime.db-shm", bare / "runtime.db-shm"),  # 还不存在也拒
                         (main / "runtime.db-journal", main / "runtime.db-journal"),
                         (bare / "runtime.db", bare / "runtime.db"),
                         (verification / "originals.json", verification),
                         (verification / "originals" / "abc", verification), (verification, verification),
                         (other_task / "r.jsonl", other_task),
                         (main / "tasks" / "2026-09-01" / "old" / "data" / "pack_verification" / "a", None),
                         (main / "audits" / "a-1" / "data" / "pack_verification" / "a", None)]:
        decision = policy.check_write(target)
        assert (decision.allowed, decision.code) == (False, STATE_CODE), target
        if root is not None:
            assert decision.dangerous_root == str(root)
        assert policy.check(target).allowed, "运行状态只拒写，读照常"
    # 任务里别的数据、工作目录，名字相近的库文件，不在规范任务根下的同名目录都照常可写。
    for target in (home["task"] / "data" / "other.json", home["task"] / "work" / "out.md", main / "runtime.db.bak",
                   main / "runtime.dbx", main / "ws" / "data" / "pack_verification" / "a",
                   main / "runs" / "2026-10-02" / "data" / "pack_verification" / "a"):
        assert policy.check_write(target).allowed, target


@pytest.mark.parametrize("mode", ["full", "normal"])
def test_host_credentials_are_unreadable_by_file_tools(tmp_path, monkeypatch, mode):
    home = _home(tmp_path)
    policy = _admin_policy(home, monkeypatch, mode)
    config, root = home["config"], home["root"]

    for target, hit in [(config / "admin-password.json", None), (config / "shared-model-profiles.json", None),
                        (config / "model-profiles" / "abc.json", config / "model-profiles"),
                        (config / "model-profiles", None), (config / "desktop.yaml", None),
                        (config / "desktop.yaml.bak-20261002", None),
                        (config / "desktop.supply-recovery-20260921.yaml", None),
                        (root / "releases" / "claude-tools" / "secrets" / "k.key", root / "releases" / "claude-tools" / "secrets")]:
        decision = policy.check(target)
        assert (decision.allowed, decision.code, decision.dangerous_root) == (False, CREDENTIAL_CODE, str(hit or target))
        assert "user_config" in decision.message and "manage_models" in decision.message
        assert policy.check_write(target).code == CREDENTIAL_CODE, "读写都拒，凭据码优先"
    # 其它配置照旧可读；用户自己工作区里的 secrets/ 和同名 desktop.yaml 不受影响。
    for target in (config / "settings-changes.jsonl", config / "admin-password-attempts.json",
                   config / "tests" / "fixture.yaml", config / "archive.yaml.d" / "notes.txt",  # 只认 config/ 下直接的 YAML 文件
                   home["main"] / "config" / "capability_config.yaml",
                   home["ws"] / "secrets" / "token.txt", home["ws"] / "desktop.yaml"):
        assert policy.check(target).allowed, target


def test_case_variants_cannot_slip_past_the_declarations(tmp_path, monkeypatch):
    """macOS/Windows 默认大小写不敏感，Path.resolve 保留输入大小写：CONFIG/Desktop.YAML 打开的就是 config/desktop.yaml。"""
    home = _home(tmp_path)
    policy = _admin_policy(home, monkeypatch)
    root, main = home["root"], home["main"]
    upper_root = root.parent / root.name.upper()

    assert policy.check(root / "CONFIG" / "Desktop.YAML").code == CREDENTIAL_CODE
    assert policy.check(upper_root / "config" / "model-profiles" / "abc.json").code == CREDENTIAL_CODE
    assert policy.check_write(root / "Config" / "settings-changes.jsonl").code == CONFIG_CODE
    assert policy.check_write(root / "Owners" / "Local" / "Main" / "Config" / "capability_config.yaml").code == CONFIG_CODE
    assert policy.check_write(main / "Runtime.DB-WAL").code == STATE_CODE
    assert policy.check_write(main / "RUNS" / "2026-10-02" / "k1" / "Data" / "Pack_Verification" / "x").code == STATE_CODE
    # H2 的插件库判定同样修好（原来是大小写敏感的字符串比较）。
    assert policy.check(main / "Data" / "Plugins" / "packages" / "old.zip").code == "PATH_HOST_MANAGED_STORE_BLOCKED"
    if (root / "CONFIG" / "desktop.yaml").exists():
        assert (root / "CONFIG" / "desktop.yaml").samefile(home["config"] / "desktop.yaml"), "确实是同一个文件"


def test_links_into_host_files_are_blocked(tmp_path, monkeypatch):
    home = _home(tmp_path)
    policy = _admin_policy(home, monkeypatch)
    ws, config, main = home["ws"], home["config"], home["main"]

    (ws / "cfg-link").symlink_to(config)
    assert policy.check_write(ws / "cfg-link" / "settings-changes.jsonl").code == CONFIG_CODE
    os.link(config / "settings-changes.jsonl", ws / "ledger-copy.jsonl")
    linked = policy.check_write(ws / "ledger-copy.jsonl")
    assert (linked.code, linked.dangerous_root) == (CONFIG_CODE, str(config)), "顺着已有硬链接写进配置也拒"
    os.link(main / "runtime.db", ws / "db-copy")
    assert policy.check_write(ws / "db-copy").code == STATE_CODE
    _put(ws / "two.txt", "x")
    os.link(ws / "two.txt", ws / "two-again.txt")
    assert policy.check_write(ws / "two.txt").allowed, "有多个链接但不是宿主文件照常可写"


def test_isolated_owner_keeps_old_codes_and_cannot_write_its_own_host_files(tmp_path):
    home = _home(tmp_path)
    main = home["main"]
    policy = PathAccessPolicy.from_values(owner_scope_root=main)

    assert policy.check(main / "config" / "capability_config.yaml").allowed, "读自家配置照常"
    assert policy.check_write(main / "config" / "capability_config.yaml").code == CONFIG_CODE
    assert policy.check_write(main / "runtime.db-journal").code == STATE_CODE
    assert policy.check_write(home["verification"] / "originals.json").code == STATE_CODE
    # 原有拒绝码不变：数据根配置仍是 owner 墙，别人的配置仍是跨 owner。
    assert policy.check(home["config"] / "desktop.yaml").code == "PATH_OWNER_SCOPE_BLOCKED"
    assert policy.check(home["feishu"] / "config" / "capability_config.yaml").code == "PATH_CROSS_OWNER_BLOCKED"


def test_declarations_match_the_canonical_layout(tmp_path):
    """守卫：路径策略为能原样打进插件 SDK 写成路径片段，必须与宿主布局函数一致。"""
    from agent_py_agent.agent.capability.runtime_config_reload import default_capability_config_path
    from agent_py_agent.agent.conversation.workspace_paths import canonical_task_root
    from agent_py_agent.agent.path_access_policy import (
        HOST_CONFIG_HOME_PARTS,
        HOST_STATE_OWNER_SQLITE,
        host_config_root_for_path,
        host_credential_for_path,
        host_state_for_path,
        task_root_containing,
    )
    from agent_py_agent.agent.runtime_db.schema import RUNTIME_DB_FILENAME, runtime_db_path
    from agent_py_agent.agent.settings.model_profiles import model_profiles_path
    from agent_py_agent.agent.settings.shared_model_catalog import shared_catalog_path
    from agent_py_agent.agent.user_space.admin_password import admin_password_path
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    root = tmp_path.resolve()
    paths = home_paths(root)
    assert {paths.config_dir, paths.system_config_dir} == {root.joinpath(*parts) for parts in HOST_CONFIG_HOME_PARTS}
    owner = resolve_owner_home(root, OwnerIdentity.provider_user("feishu", "u-1")).home_dir
    capability = default_capability_config_path(owner)
    assert host_config_root_for_path(capability, root) == capability.parent
    assert RUNTIME_DB_FILENAME in HOST_STATE_OWNER_SQLITE
    assert host_state_for_path(runtime_db_path(owner), root) == runtime_db_path(owner)
    for credential in (model_profiles_path(SimpleNamespace(config_dir=paths.config_dir, owner_provider="local",
                                                           owner_kind="main", owner_id="main")),
                       shared_catalog_path(paths), admin_password_path(root)):
        assert host_credential_for_path(credential, root) is not None, credential
    for task in (owner / "runs" / "2026-10-02" / "k1", owner / "tasks" / "2026-09-01" / "slug", owner / "audits" / "a-1"):
        assert canonical_task_root(owner, task) is not None
        assert task_root_containing(task / "data" / "x", owner) == task


def test_new_owner_home_has_a_config_dir(tmp_path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    result = ensure_owner_home(tmp_path, OwnerIdentity.provider_user("feishu", "u-9"))

    assert (result.home_dir / "config").is_dir(), "Linux 命令沙箱只能把已存在的目录挂成只读"


# ---------------------------------------------------------------- 文件工具、写边界、插件上下文


# 函数用途: 装一套文件工具；给 owner_scope 时是隔离 owner，否则是本机管理员 Full Access。
def _registry(workspace: Path, *, owner_scope: Path | None = None, roots: list[Path] | None = None):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    return ToolRegistry(ToolRegistryParams(
        workspace_root=workspace, workspace_roots=roots, max_chars=6000, max_entries=200, max_matches=50,
        web_max_chars=12000, http_timeout=30, catalog_limit=20, retrieval_limit=3, vector_search_enabled=False,
        shell_tool_timeout=30, path_access_mode="normal" if owner_scope else "full",
        owner_scope_root=str(owner_scope or ""),
    )).tools


# 函数用途: 拼一段 apply_patch 文本。
def _patch(body: str) -> dict[str, str]:
    return {"patch": f"*** Begin Patch\n{body}*** End Patch"}


def test_file_tools_refuse_every_write_kind_into_host_files(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setenv("MY_AGENT_HOME", str(home["root"]))
    tools = _registry(home["ws"])
    config, main, ws = home["config"], home["main"], home["ws"]
    ledger = config / "settings-changes.jsonl"
    before = {path: path.read_bytes() for path in (ledger, main / "runtime.db", home["verification"] / "originals.json")}

    cases = [
        ("write_file", {"path": str(ledger), "content": "x"}, CONFIG_CODE),
        ("write_file", {"path": str(config / "new.txt"), "content": "x"}, CONFIG_CODE),
        ("edit_file", {"path": str(ledger), "old_string": "LEDGER-MARKER", "new_string": "y"}, CONFIG_CODE),
        ("apply_patch", _patch(f"*** Add File: {config / 'added.txt'}\n+x\n"), CONFIG_CODE),
        ("apply_patch", _patch(f"*** Update File: {ledger}\n-{ledger.read_text().strip()}\n+y\n"), CONFIG_CODE),
        ("apply_patch", _patch(f"*** Delete File: {ledger}\n"), CONFIG_CODE),
        ("apply_patch", _patch(f"*** Update File: {ws / 'notes.md'}\n*** Move to: {config / 'moved.md'}\n-hello\n+hello\n"),
         CONFIG_CODE),
        ("apply_patch", _patch(f"*** Update File: {ledger}\n*** Move to: {ws / 'stolen.jsonl'}\n"
                               f"-{ledger.read_text().strip()}\n+y\n"), CONFIG_CODE),
        ("write_file", {"path": str(main / "runtime.db"), "content": "x"}, STATE_CODE),
        ("write_file", {"path": str(home["bare"] / "runtime.db-shm"), "content": "x"}, STATE_CODE),
        ("edit_file", {"path": str(main / "runtime.db-wal"), "old_string": "HOST", "new_string": "y"}, STATE_CODE),
        ("write_file", {"path": str(home["verification"] / "originals.json"), "content": "{}"}, STATE_CODE),
        ("apply_patch", _patch(f"*** Delete File: {home['verification'] / 'originals.json'}\n"), STATE_CODE),
    ]
    for name, params, code in cases:
        outcome = tools[name].execute(params)
        assert (outcome.ok, outcome.error_code) == (False, code), (name, params, outcome.output)
    assert {path: path.read_bytes() for path in before} == before, "一个字节都没改"
    assert not any((config / name).exists() for name in ("new.txt", "added.txt", "moved.md"))
    assert not (home["bare"] / "runtime.db-shm").exists()
    assert (ws / "notes.md").exists() and not (ws / "stolen.jsonl").exists()
    assert tools["write_file"].execute({"path": str(ws / "fine.md"), "content": "ok"}).ok


def test_file_tools_read_config_and_state_but_not_credentials(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setenv("MY_AGENT_HOME", str(home["root"]))
    tools = _registry(home["ws"])
    config = home["config"]

    assert tools["read_file"].execute({"path": str(config / "settings-changes.jsonl")}).ok
    assert tools["read_file"].execute({"path": str(home["verification"] / "originals.json")}).ok
    for target in (config / "desktop.yaml", config / "model-profiles" / "abc.json", config / "admin-password.json"):
        denied = tools["read_file"].execute({"path": str(target)})
        assert (denied.ok, denied.error_code) == (False, CREDENTIAL_CODE) and SECRET not in denied.output
    found = tools["search_text"].execute({"path": str(home["root"]), "query": SECRET, "output_mode": "files_with_matches"})
    assert found.ok and not any(name in found.output for name in ("desktop", "abc.json", "admin-password.json",
                                                                   "shared-model-profiles.json", "k.key")), found.output
    ledger = tools["search_text"].execute({"path": str(config), "query": "LEDGER-MARKER", "output_mode": "files_with_matches"})
    assert "settings-changes.jsonl" in ledger.output


def test_isolated_owner_file_tools_cannot_write_own_host_files_from_a_declared_home_root(tmp_path):
    home = _home(tmp_path)
    main = home["main"]
    tools = _registry(main, owner_scope=main, roots=[main])

    for target, code in ((main / "config" / "capability_config.yaml", CONFIG_CODE), (main / "runtime.db", STATE_CODE)):
        outcome = tools["write_file"].execute({"path": str(target), "content": "x"})
        assert (outcome.ok, outcome.error_code) == (False, code), outcome.output
    assert tools["write_file"].execute({"path": str(main / "notes2.md"), "content": "ok"}).ok


def test_write_boundary_and_plugin_contexts_apply_the_same_rules(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.workspace_write_scope import build_workspace_write_context
    from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary
    from agent_py_agent.agent.workspace_write_context import WorkspaceWriteContext

    home = _home(tmp_path)
    policy = _admin_policy(home, monkeypatch)
    main, config = home["main"], home["config"]

    for target in (config / "x.txt", main / "runtime.db-wal", home["verification"] / "a.json"):
        error = validate_write_boundary("write_file", {"path": str(target)}, workspace_root=home["ws"],
                                        path_access_mode="full", write_boundary={})
        assert error.startswith("写入被阻止") and ("user_config" in error or "宿主运行状态" in error), target
    context = build_workspace_write_context(cwd=main, write_boundary={"allowed_write_roots": [str(main), str(config)]},
                                            path_policy=policy)
    for current in (context, WorkspaceWriteContext.from_payload(context.to_payload())):
        assert current.check(str(main / "config" / "capability_config.yaml")).code == CONFIG_CODE
        assert current.check(str(main / "runtime.db")).code == STATE_CODE
        assert current.check(str(main / "ws" / "out.md")).allowed
    read = WorkspaceReadContext.from_payload(WorkspaceReadContext(
        home["root"], (home["root"],), policy, (), PathAccessPolicy.from_values(mode="full")).to_payload())
    assert read.check(config / "desktop.yaml").code == CREDENTIAL_CODE
    assert read.check(config / "settings-changes.jsonl").allowed and read.check(main / "runtime.db").allowed


# ---------------------------------------------------------------- 命令沙箱


def test_shell_lists_host_files_per_mode(tmp_path):
    from agent_py_agent.agent.tooling.shell import host_readonly_paths_for

    home = _home(tmp_path)
    root, main, feishu, bare, task = home["root"], home["main"], home["feishu"], home["bare"], home["task"]
    state = (*_STATE_FILES, *(f"{name}.lock" for name in _STATE_FILES),
             "runtime.db", "runtime.db-wal", "runtime.db-shm", "runtime.db-journal",
             "capability_requests", "temporary_grants", "compact", "logs", "audit", "workspace/runtime", "memory/curator",
             "memory_archive", "cache", "trash", "skills", ".agents/skills", "data", "agents")

    scoped = set(host_readonly_paths_for(str(main), ""))
    assert scoped == {main / "config", *(main / name for name in state)}
    full = set(host_readonly_paths_for("", str(main)))
    owners = (main, feishu, bare)
    assert full == {root / "config", root / "system" / "config", *(home_dir / "config" for home_dir in owners),
                    *(home_dir / name for home_dir in owners for name in state)}
    assert home["verification"] not in full | scoped and task, "任务核验记录只按写边界的 task_root 给（3a 定），不从路径反推"
    assert host_readonly_paths_for(str(tmp_path / "loose-owner"), "") == (), "非规范布局推不出数据根时不加覆盖"


# 函数用途: 一个 Full Access 风格的沙箱规格：只读覆盖、一个隐藏目录、人格根，可选断网。
def _spec(home: dict[str, Path], *, network: bool = True, **extra) -> AttemptSandboxSpec:
    main = home["main"]
    return AttemptSandboxSpec(
        attempt_view=home["ws"], staging_root=home["ws"], shared_workspace=main, owner_home=main,
        protected_persona_root=main, full_access=True, network_access=network, macos_sandbox_exec="/usr/bin/sandbox-exec",
        protected_write_paths=(home["config"], home["bare"] / "runtime.db-shm"), hidden_paths=(main / "data" / "plugins",),
        **extra,
    )


def test_seatbelt_rules_keep_their_order(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    lines = AttemptExecutionSandbox(_spec(home, network=False))._macos_argv(["/bin/true"])[2].splitlines()
    shm = json_text(home["bare"] / "runtime.db-shm")

    assert f"(deny file-write* (literal {shm}) (subpath {shm}))" in lines, "还不存在的受保护路径也写拒绝"
    ancestors = next(index for index, line in enumerate(lines) if line.startswith("(deny file-write* (literal")
                     and json_text(home["root"]) in line and "subpath" not in line)
    for parent in (home["root"], home["main"], home["main"].parent, tmp_path.resolve()):
        assert f"(literal {json_text(parent)})" in lines[ancestors]
    hidden = next(index for index, line in enumerate(lines) if line.startswith("(deny file-read* file-write*"))
    assert ancestors < hidden, "上级目录写拒绝在隐藏路径之前"
    assert lines[-1] == "(deny network*)", "断网仍在最后"
    assert "(deny network*)" not in AttemptExecutionSandbox(_spec(home))._macos_argv(["/bin/true"])[2]


# 函数用途: 把路径编码成 Seatbelt 规则里的字符串字面量（与沙箱实现同一写法）。
def json_text(path: Path) -> str:
    import json

    return json.dumps(str(Path(path).resolve(strict=False)))


# 函数用途: 平台沙箱是否可用（macOS 有 sandbox-exec，Linux 有 bwrap）。
def _sandbox_ready() -> bool:
    if IS_MACOS:
        return shutil.which("sandbox-exec") is not None
    return IS_LINUX and shutil.which("bwrap") is not None


# 函数用途: 装一个真实的 run_command：scoped 时是隔离 owner，否则是本机管理员 Full Access（人格根 = 本机主用户）。
def _shell(home: dict[str, Path], *, scoped: bool):
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    main = home["main"]
    options = (ShellToolOptions(owner_scope_root=str(main), host_private_roots=(str(home["root"]),)) if scoped
               else ShellToolOptions(protected_persona_root=str(main)))
    return ShellTool(main, options=options)


needs_sandbox = pytest.mark.skipif(not (IS_MACOS or IS_LINUX), reason="需要 macOS Seatbelt 或 Linux bwrap")


@needs_sandbox
def test_real_full_access_shell_cannot_write_host_files(tmp_path):
    if not _sandbox_ready():
        pytest.skip("平台沙箱不可用")
    home = _home(tmp_path)
    shell = _shell(home, scoped=False)
    config, main, task = home["config"], home["main"], home["task"]
    ledger = config / "settings-changes.jsonl"
    before = {path: path.read_bytes() for path in (ledger, main / "runtime.db-wal", home["verification"] / "originals.json")}

    # 生产里命令都经 registry 投影：写边界带结构化 task_root，本任务核验记录才进只读覆盖。
    sandbox = {key: value for key, value in _dispatch_params(home, main).items() if key.startswith("__sandbox_")}
    sandbox.pop("__sandbox_write_roots", None)  # Full Access 没有写根限制

    def run(command: str, cwd: Path = main):
        return shell.execute({"command": command, "working_dir": str(cwd), **sandbox})

    assert not run(f"printf x >> {ledger}").ok
    assert not run(f"printf x >> {main / 'runtime.db-wal'}").ok
    assert not run(f"printf x > {home['verification'] / 'originals.json'}", task / "work").ok
    assert not run(f"rm {main / 'runtime.db'}").ok and (main / "runtime.db").exists()
    # 先改名上级目录、写完再改回：macOS 上改名本身被拒；Linux 只读挂载跟着目录走，写照样失败。
    moved = main.parent / "main-moved"
    sneaky = run(f"mv {main} {moved} && (printf x >> {moved / 'config' / 'capability_config.yaml'}; rc=$?; "
                 f"mv {moved} {main}; exit $rc)", home["root"])
    assert not sneaky.ok and main.exists() and not moved.exists()
    assert (main / "config" / "capability_config.yaml").read_text() == "subagent_max: 3\n"
    if IS_MACOS:
        assert not run(f"printf x > {home['bare'] / 'runtime.db-shm'}").ok and not (home["bare"] / "runtime.db-shm").exists()
        assert not run(f"mkdir {home['bare'] / 'config'}").ok and not (home["bare"] / "config").exists()
    assert {path: path.read_bytes() for path in before} == before
    # 命令只拒写不拒读（读取不在 H3 范围，已知边界）；普通写照常。
    assert run(f"cat {config / 'desktop.yaml'}").ok
    assert run(f"printf ok > {home['ws'] / 'new.txt'}").ok and run(f"printf ok > {task / 'work' / 'x.md'}", task / "work").ok


@needs_sandbox
def test_real_isolated_shell_with_home_root_write_cannot_write_own_host_files(tmp_path):
    if not _sandbox_ready():
        pytest.skip("平台沙箱不可用")
    home = _home(tmp_path)
    shell = _shell(home, scoped=True)
    main = home["main"]

    def run(command: str):
        return shell.execute({"command": command, "working_dir": str(main), "__sandbox_write_roots": [str(main)]})

    assert not run(f"printf x >> {main / 'config' / 'capability_config.yaml'}").ok
    assert not run(f"printf x >> {main / 'runtime.db'}").ok
    assert (main / "config" / "capability_config.yaml").read_text() == "subagent_max: 3\n"
    assert run(f"printf ok > {main / 'ws' / 'new.txt'}").ok, "声明的写根里其它位置照常可写"


@needs_sandbox
def test_real_network_off_readonly_and_ancestor_rules_hold_together(tmp_path):
    """断网、只读覆盖、上级目录写拒绝三条规则同时生效（Seatbelt 规则顺序；bwrap 断网与只读挂载）。"""
    home = _home(tmp_path)
    sandbox = AttemptExecutionSandbox(_spec(home, network=False, bwrap_path=shutil.which("bwrap")))
    if not sandbox.probe().ready:
        pytest.skip("平台沙箱不可用")
    try:
        sandbox.require_ready()
    except Exception:
        pytest.skip("本机沙箱无法切断网络")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    config, main = home["config"], home["main"]

    def run(script: str) -> subprocess.CompletedProcess:
        return sandbox.run(["/bin/sh", "-c", script], timeout=30)

    try:
        assert run(f"printf x >> {config / 'settings-changes.jsonl'}").returncode != 0
        moved = home["root"].parent / "moved-home"
        sneaky = run(f"mv {home['root']} {moved} && (printf x >> {moved / 'config' / 'settings-changes.jsonl'}; rc=$?; "
                     f"mv {moved} {home['root']}; exit $rc)")
        assert sneaky.returncode != 0 and home["root"].exists() and not moved.exists()
        assert run(f"printf ok > {home['ws'] / 'fine.txt'}").returncode == 0
        connect = run(f"{sys.executable} -I -S -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 3)\"")
        assert connect.returncode != 0, "断网仍然生效"
    finally:
        listener.close()
    assert "LEDGER-MARKER" in (config / "settings-changes.jsonl").read_text()
    assert main.exists()


def test_isolated_boundary_facts_list_host_files_inside_a_writable_root(tmp_path, monkeypatch):
    home = _home(tmp_path)
    main = home["main"]
    tool = _shell(home, scoped=True)

    def failed(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["bash"], returncode=1, stdout="", stderr="Operation not permitted\n")

    monkeypatch.setattr(tool, "_run_command", failed)
    declared = tool.execute({"command": "true", "working_dir": str(main), "__sandbox_write_roots": [str(main)]})
    read_only = declared.result_envelope["sandbox"]["allowed_roots"]["read_only"]
    assert {str(main / "config"), str(main / "runtime.db"), str(main / "runtime.db-wal")} <= set(read_only)
    assert not any(str(home["feishu"]) in item for item in read_only), "不列别人的"
    narrow = tool.execute({"command": "true", "working_dir": str(home["ws"]), "__sandbox_write_roots": [str(home["ws"])]})
    assert narrow.result_envelope["sandbox"]["allowed_roots"]["read_only"] == [str(main)], "不在可写根里的不多列"


@needs_sandbox
def test_real_persona_root_cannot_be_renamed_to_dodge_its_overlay(tmp_path):
    """沙箱通用规则：只有人格文件受保护时，改名人格根（owner home）再改写 SOUL.md 也被拒（macOS 改名被拒，Linux 挂载跟着走）。"""
    persona = (tmp_path / "persona").resolve()
    soul = _put(persona / "SOUL.md", "# SOUL\n")
    workspace = persona / "ws"
    workspace.mkdir()
    sandbox = AttemptExecutionSandbox(AttemptSandboxSpec(
        attempt_view=workspace, staging_root=workspace, shared_workspace=persona, owner_home=persona,
        protected_persona_root=persona, full_access=True, macos_sandbox_exec="/usr/bin/sandbox-exec",
        bwrap_path=shutil.which("bwrap")))
    if not sandbox.probe().ready:
        pytest.skip("平台沙箱不可用")
    moved = tmp_path / "persona-moved"

    result = sandbox.run(["/bin/sh", "-c", f"mv {persona} {moved} && (printf x >> {moved / 'SOUL.md'}; rc=$?; "
                                          f"mv {moved} {persona}; exit $rc)"], timeout=30)

    assert result.returncode != 0 and persona.exists() and not moved.exists()
    assert soul.read_text(encoding="utf-8") == "# SOUL\n"


# ---------------------------------------------------------------- A 类逐项（9b 盘点 + 3a 口径）


@pytest.mark.parametrize("relative", OWNER_STATE_ITEMS)
def test_every_host_state_item_is_read_only_for_file_tools(tmp_path, monkeypatch, relative):
    home = _home(tmp_path)
    main = home["main"]
    target = main / relative
    before = target.read_bytes()
    policy = _admin_policy(home, monkeypatch)

    decision = policy.check_write(target)
    assert (decision.allowed, decision.code) == (False, STATE_CODE)
    assert policy.check(target).allowed, "只拒写，读照常"
    for tools in (_registry(home["ws"]), _registry(main, owner_scope=main, roots=[main])):
        outcome = tools["write_file"].execute({"path": str(target), "content": "x"})
        assert (outcome.ok, outcome.error_code) == (False, STATE_CODE), outcome.output
    assert target.read_bytes() == before


@pytest.mark.parametrize("relative", OWNER_MODEL_ITEMS)
def test_model_owned_locations_stay_writable(tmp_path, monkeypatch, relative):
    home = _home(tmp_path)
    main = home["main"]
    policy = _admin_policy(home, monkeypatch)

    assert policy.check_write(main / relative).allowed
    tools = _registry(main, owner_scope=main, roots=[main])
    assert tools["write_file"].execute({"path": str(main / relative), "content": "ok"}).ok


@needs_sandbox
@pytest.mark.parametrize("relative", OWNER_STATE_ITEMS)
def test_every_host_state_item_is_read_only_for_commands(tmp_path, relative):
    import shlex

    if not _sandbox_ready():
        pytest.skip("平台沙箱不可用")
    home = _home(tmp_path)
    main = home["main"]
    target = main / relative
    quoted = shlex.quote(str(target))
    before = target.read_bytes()

    for shell, extra in ((_shell(home, scoped=False), {}), (_shell(home, scoped=True), {"__sandbox_write_roots": [str(main)]})):
        for command in (f"printf x >> {quoted}", f"rm -f {quoted}", f"mv {quoted} {quoted}.moved"):
            outcome = shell.execute({"command": command, "working_dir": str(main), **extra})
            assert not outcome.ok, (relative, command, outcome.output)
    assert target.read_bytes() == before


def test_task_tree_roots_stay_in_the_pierceable_owner_boundary(tmp_path, monkeypatch):
    """B 类：只剩 runs/、tasks/ 留在 tool_runtime_ledger（data/、agents/ 整体归 A，3a 定），只在隔离模式挂、可被本任务工作目录穿透。"""
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        _attach_owner_control_write_guards,
    )
    from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary

    home = _home(tmp_path)
    main = home["main"]
    monkeypatch.setenv("MY_AGENT_HOME", str(home["root"]))
    names = {"owner_runs_dir": "runs", "owner_agents_dir": "agents", "owner_data_dir": "data", "owner_tasks_dir": "tasks",
             "owner_permissions_json": "permissions.json", "owner_logs_dir": "logs", "owner_compact_dir": "compact",
             "owner_audit_log_jsonl": "audit_log.jsonl"}
    agent = SimpleNamespace(home_paths=SimpleNamespace(**{key: main / value for key, value in names.items()}))
    scoped: dict[str, object] = {"effective_owner_scope_root": str(main)}
    _attach_owner_control_write_guards(scoped, agent)
    assert scoped["forbidden_write_roots"] == [str(main / name) for name in ("runs", "tasks")]
    full: dict[str, object] = {}
    _attach_owner_control_write_guards(full, agent)
    assert "forbidden_write_roots" not in full, "B 类只在隔离模式挂"

    legacy_work = main / "tasks" / "2026-09-01" / "old" / "work"
    boundary = {"allowed_write_roots": [str(main), str(legacy_work)], "forbidden_write_roots": [str(main / "tasks")]}
    allowed = validate_write_boundary("write_file", {"path": str(legacy_work / "out.md")}, workspace_root=main,
                                      write_boundary=boundary)
    blocked = validate_write_boundary("write_file", {"path": str(main / "tasks" / "2026-09-01" / "other" / "x.md")},
                                      workspace_root=main, write_boundary=boundary)
    assert allowed == "" and "forbidden_write_roots" in blocked, "本任务工作目录穿透，别的任务仍禁写"


@pytest.mark.parametrize("scoped", [False, True], ids=["full-access", "isolated"])
def test_any_new_subdir_under_owner_data_is_read_only_but_host_writes_go_on(tmp_path, monkeypatch, scoped):
    """owner 根的 data/ 整体归 A 类（3a 定：开放世界不靠写死清单，宿主以后加的新目录默认就受保护）：模型在 data/ 下新建一个原本
    没有的子目录，文件工具和真实 Shell 都写不进、目录也建不出来；宿主自己的维护流程照常写 data/maintenance.json。"""
    import shlex

    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    root = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    home = ensure_my_agent_home(root)
    main = Path(home.owner_home_dir).resolve()
    data = Path(home.owner_data_dir).resolve()
    assert data == main / "data" and data.is_dir()
    fresh = data / "brand-new-store" / "state.json"
    tools = _registry(main, owner_scope=main, roots=[main]) if scoped else _registry(main / "ws")
    for target in (fresh, data / "loose.txt"):
        outcome = tools["write_file"].execute({"path": str(target), "content": "x"})
        assert (outcome.ok, outcome.error_code) == (False, STATE_CODE), (target, outcome.output)
    if _sandbox_ready():
        shell = _shell({"main": main, "root": root}, scoped=scoped)
        extra = {"__sandbox_write_roots": [str(main)]} if scoped else {}
        command = f"mkdir -p {shlex.quote(str(fresh.parent))} && printf x > {shlex.quote(str(fresh))}"
        assert not shell.execute({"command": command, "working_dir": str(main), **extra}).ok
        assert shell.execute({"command": f"printf ok > {shlex.quote(str(main / 'notes.txt'))}", "working_dir": str(main),
                              **extra}).ok, "家目录里别处照常可写"
    assert not fresh.parent.exists() and not (data / "loose.txt").exists()

    assert run_owner_retention_if_due(home, now=200_000).ran is True
    assert (data / "maintenance.json").is_file(), "宿主在自己的进程里写，不经过模型工具"


def test_host_memory_tool_still_writes_its_own_files(tmp_path, monkeypatch):
    """记忆正文和候选由宿主的记忆工具在进程里写，不走模型的文件工具和 Shell；保护打开后它照常写，模型直接写同一文件被拒。"""
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path / "work")
    candidates = Path(agent.home_paths.owner_memory_candidates_jsonl)
    tools = agent.tools.tools

    remembered = tools["remember"].execute({"content": "用户偏好结论先行的简报", "origin": "user_explicit",
                                            "subject_key": "preference.brief",
                                            "scope": {"scope_type": "personal", "scope_key": "personal"}})

    assert remembered.ok and candidates.exists() and "preference.brief" in candidates.read_text(encoding="utf-8")
    blocked = tools["write_file"].execute({"path": str(candidates), "content": "forged"})
    assert (blocked.ok, blocked.error_code) == (False, STATE_CODE)
    assert "forged" not in candidates.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 命令在任务树外（ae 块 4 发现，2026-10-02）


# 函数用途: 按 registry 的真实投影，算出本次命令沙箱拿到的内部参数（写根在任务树外，写边界带结构化 task_root）。
def _dispatch_params(home: dict, project, tool_name: str = "run_command") -> dict:
    from agent_py_agent.agent.tooling.registry_invoke import (
        AuthorizedToolDispatchRequest,
        _tool_params_with_runtime_boundary,
    )

    boundary = {"task_root": str(home["task"]), "allowed_write_roots": [str(project)]}
    return _tool_params_with_runtime_boundary(AuthorizedToolDispatchRequest(
        tool_name=tool_name, tool=SimpleNamespace(), tool_params={"command": "true"},
        workspace_root=project, write_boundary=boundary,
    ))


def test_task_records_reach_the_sandbox_from_the_boundary_task_root(tmp_path):
    home = _home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    for tool_name in ("run_command", "terminal_session"):
        assert str(home["verification"]) in _dispatch_params(home, project, tool_name)["__sandbox_protected_write_paths"]
    from agent_py_agent.agent.tooling.registry_invoke import (
        AuthorizedToolDispatchRequest,
        _tool_params_with_runtime_boundary,
    )

    no_task = _tool_params_with_runtime_boundary(AuthorizedToolDispatchRequest(
        tool_name="run_command", tool=SimpleNamespace(), tool_params={}, workspace_root=project,
        write_boundary={"allowed_write_roots": [str(project)]}))
    assert no_task["__sandbox_protected_write_paths"] == [], "没有 task_root 时不臆造"


@needs_sandbox
def test_real_full_access_shell_outside_the_task_tree_cannot_forge_the_records(tmp_path):
    if not _sandbox_ready():
        pytest.skip("平台沙箱不可用")
    home = _home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    shell = _shell(home, scoped=False)
    params = _dispatch_params(home, project)
    record = home["verification"] / "originals.json"

    def run(command: str):
        return shell.execute({"command": command, "working_dir": str(project), **{
            key: value for key, value in params.items() if key.startswith("__sandbox_")}})

    assert not run(f"printf forged >> {record}").ok
    assert record.read_text(encoding="utf-8") == "{}"
    assert run(f"cat {record}").ok and run(f"printf ok > {project / 'out.txt'}").ok, "读和写项目目录照常"


def test_receipt_says_whether_task_records_were_protected(tmp_path, monkeypatch):
    """3a 定：没有 task_root（没有任务上下文的直聊命令）时给结构化原因 no_task_root——那种情况本来就没有核验记录。"""
    from agent_py_agent.agent.tooling.registry_invoke import (
        AuthorizedToolDispatchRequest,
        _tool_params_with_runtime_boundary,
    )

    home = _home(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    shell = _shell(home, scoped=False)
    monkeypatch.setattr(shell, "_run_command", lambda *_args, **_kwargs: subprocess.CompletedProcess(
        args=["bash"], returncode=0, stdout="", stderr=""))
    with_task = _dispatch_params(home, project)
    without_task = _tool_params_with_runtime_boundary(AuthorizedToolDispatchRequest(
        tool_name="run_command", tool=SimpleNamespace(), tool_params={"command": "true"}, workspace_root=project,
        write_boundary={"allowed_write_roots": [str(project)]}))
    for params, state in ((with_task, "protected"), (without_task, "no_task_root")):
        assert params["__sandbox_task_records"] == state
        outcome = shell.execute({"command": "true", "working_dir": str(project),
                                 **{key: value for key, value in params.items() if key.startswith("__sandbox_")}})
        assert outcome.result_envelope["sandbox"]["task_records"] == state
    plain = shell.execute({"command": "true", "working_dir": str(project)})
    assert "task_records" not in plain.result_envelope["sandbox"], "没经 registry 时不加这一项，旧回执不变"


# ---------------------------------------------------------------- 数据根只有一个权威来源（9b 二审，3a 必须改第 1 条）


def test_configured_home_wins_over_a_different_env_home(tmp_path, monkeypatch):
    """配置写了 my_agent_home、环境变量 MY_AGENT_HOME 指向别处时，宿主文件判定跟宿主解析出的数据根走（原来只看环境变量，
    9b 用真实 SimpleAgent 在 Full Access 下写进了 permissions.json、tool_policy.json、runtime.db 旁的文件和 config/）。"""
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.registry_invoke import (
        RegistryToolInvokeRequest,
        _request_local_tool_for_invocation,
    )
    from agent_py_agent.agent.tooling.write_boundary import validate_write_boundary
    from agent_py_agent.tests.test_runtime_gate_ledger import _loop_params

    configured, decoy = (tmp_path / "configured-home").resolve(), tmp_path / "env-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(decoy))
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(configured), path_access_mode="full",
                                    access_mode="full-access"), tmp_path / "work")
    assert agent.tools.tools["write_file"].path_access_policy.owner_scope_root is None, "本机管理员 Full Access，没有 owner 墙"
    owner = Path(agent.home_paths.owner_home_dir)
    assert owner.is_relative_to(configured)
    targets = (owner / "permissions.json", owner / "tool_policy.json", owner / "runtime.db-wal", configured / "config" / "x.txt")
    blocked = {CONFIG_CODE, STATE_CODE}

    write = agent.tools.tools["write_file"]
    for target in targets:
        outcome = write.execute({"path": str(target), "content": "x"})
        assert (outcome.ok, outcome.error_code in blocked) == (False, True), (target, outcome.output)
        assert not target.exists() or target.read_text(encoding="utf-8") != "x"
    boundary = write_boundary_with_runtime_ledger(agent, _loop_params(write_boundary={}))
    for target in targets:
        error = validate_write_boundary("write_file", {"path": str(target)}, workspace_root=owner, path_access_mode="full",
                                        write_boundary=boundary)
        assert error.startswith("写入被阻止"), target
    request = RegistryToolInvokeRequest(tool_name="write_file", arguments={}, tools={}, workspace_root=owner,
                                        workspace_roots=[owner], allowed_tools=None, write_boundary=boundary,
                                        path_access_mode="full")
    scoped = _request_local_tool_for_invocation(write, request=request, workspace_roots=[owner])
    assert scoped.path_access_policy.agent_home_root == configured, "每次调用换的策略也沿用宿主数据根"
    assert scoped.path_access_policy.check_write(owner / "permissions.json").code == STATE_CODE


def test_every_tool_policy_and_plugin_context_use_the_host_data_root(tmp_path, monkeypatch):
    """数据根只有一个来源：agent 构造出的每个工具策略（包括眼下不靠它判的 Shell）都是宿主解析出的数据根；registry 每次调用给
    插件组装的读写上下文也跟写边界里宿主写的 owner home 走，不退回环境变量。"""
    from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
        write_boundary_with_runtime_ledger,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.registry_invoke import (
        RegistryToolInvokeRequest,
        _invocation_context,
    )
    from agent_py_agent.tests.test_runtime_gate_ledger import _loop_params

    configured = (tmp_path / "configured-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "env-home"))
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(configured), path_access_mode="full",
                                    access_mode="full-access"), tmp_path / "work")
    owner = Path(agent.home_paths.owner_home_dir)
    roots = {name: tool.path_access_policy.agent_home_root for name, tool in agent.tools.tools.items()
             if getattr(tool, "path_access_policy", None) is not None}
    assert {"write_file", "run_command"} <= set(roots) and set(roots.values()) == {configured}, roots
    boundary = write_boundary_with_runtime_ledger(agent, _loop_params(write_boundary={}))
    request = RegistryToolInvokeRequest(tool_name="write_file", arguments={}, tools={}, workspace_root=owner,
                                        workspace_roots=[owner], allowed_tools=None, write_boundary=boundary,
                                        path_access_mode="full", runtime_snapshot=SimpleNamespace())
    # 快照只原样透传给插件，这里只看路径上下文。
    context = _invocation_context(request)
    assert context.workspace_write_context.check(owner / "permissions.json").code == STATE_CODE
    assert context.workspace_read_context.external_policy.agent_home_root == configured
    # 子代理创建前的写目标预检（编排层，9b 三审建议）也按宿主数据根认出宿主凭据。
    from agent_py_agent.agent.agent_core.orchestration.write_guard import (
        ExternalWriteTargetRequest,
        external_write_target_error,
    )

    precheck = ExternalWriteTargetRequest(agent=agent, allowed_tools=["write_file"],
                                          params={"extra_write_roots": [str(configured / "config" / "desktop.yaml")]})
    assert external_write_target_error(precheck)


# ---------------------------------------------------------------- 真实链路


# 类用途: 假模型：第一轮发出给定的工具调用，第二轮收尾；记下第二轮收到的请求内容，用来核对模型看到的拒绝码。
class _ToolCallsBackend:
    name = "fake_h3"

    def __init__(self, blocks: list[dict]) -> None:
        self.blocks, self.calls, self.seen = blocks, 0, ""

    # 函数用途: 声明支持原生工具调用。
    def probe_tool_capability(self):
        from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability

        return ProviderToolCapability(provider=self.name, endpoint="local://fake", model="", stream=False,
                                      native_supported=True, evidence="test_fake_native")

    # 函数用途: 第一轮返回工具调用，之后返回收尾文字并记下请求。
    def generate(self, prompt, on_chunk=None, **kwargs):
        from agent_py_agent.agent.backends import ModelResponse

        self.calls += 1
        if self.calls == 1:
            return ModelResponse(text="", backend=self.name, tool_use_blocks=self.blocks)
        self.seen = repr(prompt) + repr(kwargs)
        return ModelResponse(text="done", backend=self.name)


# 函数用途: 按生产方式造一个本机管理员 Full Access 的 SimpleAgent（审批模式经宿主操作设成 full-access），工作目录在家目录外。
def _full_access_agent(home: Path, project: Path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.approval_mode import execute_approval_mode_operation

    config = AgentConfig(enable_tools=True, max_tool_rounds=3, my_agent_home=str(home))
    project.mkdir(parents=True, exist_ok=True)
    execute_approval_mode_operation(SimpleAgent(config, root=project).home_paths, "set", "full-access")
    return SimpleAgent(config, root=project)


# 函数用途: 读出隔离 home 里工具结果索引的结构化行（call_id、ok、error_code）。
def _tool_rows(home: Path) -> dict[str, tuple[bool, str]]:
    import json

    rows = [json.loads(line) for index in home.rglob("blobs/tool_outputs/index.jsonl")
            for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {str(row.get("call_id")): (bool(row.get("ok")), str(row.get("error_code") or "")) for row in rows}


def test_real_chain_full_access_write_reports_the_specific_host_code(tmp_path, monkeypatch):
    """真实链路（SimpleAgent + 假模型，审批模式 full-access，配置的 home ≠ 环境变量的 home）：模型用 write_file 写 A 类和宿主配置
    被拒，工具结果和模型下一轮看到的都是具体码 PATH_HOST_*，不是通用的 WRITE_FORBIDDEN（9b 二审建议）；字节不变；
    模型自己的文件照常写。"""
    home = (tmp_path / "configured-home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "env-home"))
    agent = _full_access_agent(home, tmp_path / "proj")
    owner = Path(agent.home_paths.owner_home_dir).resolve()
    assert owner.is_relative_to(home)
    state, config = _put(owner / "permissions.json", "HOST"), _put(owner / "config" / "capability_config.yaml", "HOST")
    # 项目和家目录之外也能写，证明这一轮确实是 Full Access（审批模式按回合换算，构造时的工具还带 owner 墙）。
    own, elsewhere = tmp_path / "proj" / "notes.md", tmp_path / "elsewhere" / "x.md"
    blocks = [{"id": f"call_w{index}", "name": "write_file", "input": {"path": str(path), "content": "x"}}
              for index, path in enumerate((state, config, own, elsewhere))]
    agent.backend = _ToolCallsBackend(blocks)

    agent.run("h3", save=False, allowed_tools=["write_file"])

    rows = _tool_rows(home)
    assert [rows[f"call_w{index}"] for index in range(4)] == [(False, STATE_CODE), (False, CONFIG_CODE), (True, ""), (True, "")]
    assert STATE_CODE in agent.backend.seen and CONFIG_CODE in agent.backend.seen, "模型下一轮看到的是具体码"
    assert (state.read_text(encoding="utf-8"), config.read_text(encoding="utf-8")) == ("HOST", "HOST")
    assert own.read_text(encoding="utf-8") == elsewhere.read_text(encoding="utf-8") == "x"


def test_host_write_denials_are_terminal_failures_not_unknown():
    """文件工具在写之前按路径拒写宿主文件，零副作用：结果归 FAILED（模型可换路子），不能归 UNKNOWN 触发“结果未知”停机。"""
    from agent_py_agent.agent.local_storage import TOOL_OPERATION_FAILED
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
    from agent_py_agent.agent.tooling.tool_operation_coordinator import _operation_status_for_result

    for code in (CONFIG_CODE, STATE_CODE):
        outcome = ToolHandlerOutcome("write_file", False, "blocked", error_code=code, handler_executed=True)
        assert _operation_status_for_result(outcome) == TOOL_OPERATION_FAILED, code


def test_write_boundary_reports_the_specific_host_code(tmp_path, monkeypatch):
    """写边界拒在宿主托管文件上时原码上报（PATH_HOST_*），其余拒绝仍是 WRITE_FORBIDDEN；两个调用方都只读结构化 code。"""
    from agent_py_agent.agent.tooling.registry_invoke import (
        RegistryToolInvokeRequest,
        _write_boundary_denied,
    )
    from agent_py_agent.agent.tooling.write_boundary import (
        validate_write_boundary,
        write_boundary_error_code,
    )

    home = _home(tmp_path)
    main = home["main"]
    boundary = {"canonical_owner_home_root": str(main), "allowed_write_roots": [str(main)],
                "forbidden_write_roots": [str(main / "runs")]}
    cases = {main / "permissions.json": STATE_CODE, main / "config" / "x.yaml": CONFIG_CODE,
             home["config"] / "desktop.yaml": CREDENTIAL_CODE, main / "data" / "plugins" / "p" / "x.py": "PATH_HOST_MANAGED_STORE_BLOCKED",
             main / "runs" / "2026-10-02" / "k9" / "x.md": "WRITE_FORBIDDEN"}
    for target, code in cases.items():
        error = validate_write_boundary("write_file", {"path": str(target)}, workspace_root=main, path_access_mode="full",
                                        write_boundary=boundary)
        assert (error.startswith("写入被阻止"), write_boundary_error_code(error)) == (True, code), target
        request = RegistryToolInvokeRequest(tool_name="write_file", arguments={}, tools={}, workspace_root=main,
                                            workspace_roots=[main], allowed_tools=None, write_boundary=boundary,
                                            path_access_mode="full")
        assert _write_boundary_denied(request, {"path": str(target)}, (main,)).error_code == code
    assert write_boundary_error_code("写入被阻止: 旧调用方自己拼的") == "WRITE_FORBIDDEN"
    # 只透传宿主托管文件的码：路径策略按别的码拒（normal 模式的危险目录）时仍是 WRITE_FORBIDDEN。
    dangerous = validate_write_boundary("write_file", {"path": "/etc/my-agent-h3-probe.txt"}, workspace_root=main,
                                        path_access_mode="normal", write_boundary=boundary)
    assert dangerous.startswith("写入被阻止") and write_boundary_error_code(dangerous) == "WRITE_FORBIDDEN"


def test_task_record_patterns_cover_every_task_root_layout(tmp_path):
    """一组正则盖住 owner home 下全部规范任务根的核验记录（runs/<日期>/<键>、tasks/<日期>/<名>、audits/<编号>），以及布局各级
    目录本身和任务根下的 data 目录本身（9b 三审：只盖记录时能靠改名上级目录伪造）；目录里的其它内容不误伤；owner home 路径里的
    正则元字符按字面匹配。owner 集合与 host_readonly_paths 相同。"""
    import re

    from agent_py_agent.agent.path_access_policy import host_readonly_patterns

    home = _home(tmp_path)
    root, main = home["root"], home["main"]
    patterns = host_readonly_patterns(root, main)
    assert len(host_readonly_patterns(root)) == 3 * len(patterns), "Full Access：全部已有 owner，每个一组"
    assert not any("{" in pattern for pattern in patterns), "Seatbelt 不认 {m,n} 区间，可选层级只能写成显式分组"

    def hits(path: Path | str) -> bool:
        return any(re.search(pattern, str(path)) for pattern in patterns)

    records = ("runs/2026-10-02/k2/data/pack_verification", "runs/2026-10-02/k2/data/pack_verification/r.jsonl",
               "tasks/2026-09-01/old/data/pack_verification/x.json", "audits/a-1/data/pack_verification")
    layout = ("runs", "runs/2026-10-02", "runs/2026-10-02/k2", "tasks", "tasks/2026-09-01/old", "audits", "audits/a-1",
              "runs/2026-10-02/k2/data", "tasks/2026-09-01/old/data", "audits/a-1/data")
    miss = ("runs/2026-10-02/k2/data/other.json", "runs/2026-10-02/data/pack_verification", "runs/2026-10-02/k2/work",
            "runs/2026-10-02/k2/work/data/pack_verification", "runs/2026-10-02/k2/work/data", "runs/a/b/c",
            "runs/2026-10-02/k2/data/pack_verification2", "runs/2026-10-02/k2/data2", "audits/a-1/b",
            "audits/a-1/b/data/pack_verification", "runs2", "notes.txt")
    assert all(hits(main / rel) for rel in (*records, *layout))
    assert not [rel for rel in miss if hits(main / rel)]
    assert not hits(home["feishu"] / records[0]), "隔离：只本 owner"
    assert not hits(str(tmp_path / "mirror") + str(main / records[0])), "从路径开头匹配：别处的同名拷贝不误伤"

    odd = (tmp_path / "my.agent+(x)").resolve()
    (odd_main := odd / "owners" / "local" / "main").mkdir(parents=True)
    odd_patterns = host_readonly_patterns(odd, odd_main)
    assert any(re.search(pattern, str(odd_main / records[0])) for pattern in odd_patterns)
    twin = tmp_path / "myXagent+(x)" / "owners" / "local" / "main"
    assert not any(re.search(pattern, str(twin / rel)) for pattern in odd_patterns for rel in (*records, *layout))


def test_seatbelt_pattern_denies_come_after_the_write_root_allow(tmp_path):
    """Seatbelt 后写覆盖先写：正则拒写必须排在写根放行之后；没有正则时配置不变。"""
    from dataclasses import replace

    home = _home(tmp_path)
    pattern = "^/x/(runs/[^/]+/[^/]+)/(data/pack_verification)(/|$)"
    scoped = replace(_spec(home), full_access=False, extra_write_roots=(home["main"],), implicit_attempt_write_roots=False)
    profile = AttemptExecutionSandbox(replace(scoped, protected_write_patterns=(pattern,)))._macos_argv(["true"])[2]
    assert profile.index("(allow file-write*") < profile.index(f"(deny file-write* (regex {_quoted_regex(pattern)}))")
    assert "(regex" not in AttemptExecutionSandbox(scoped)._macos_argv(["true"])[2]


# 函数用途: 按规则里的写法（JSON 字符串）引用一个正则。
def _quoted_regex(pattern: str) -> str:
    import json

    return json.dumps(pattern)


# 函数用途: 9b 三审列的“改名上级目录 → 写记录 → 改回”各种手法（data、DATA、日期目录挪深一层、任务根挪深一层、任务根改名、
#   整个 runs/、tasks/ 和 audits/ 下的 data、整个 audits/、直接追加）。先在 tasks/、audits/ 下各建一份记录，返回记录文件和命令表。
def _layout_bypass_attacks(main: Path) -> tuple[list[Path], dict[str, str]]:
    import shlex

    k2 = main / "runs" / "2026-10-02" / "k2"
    records = [_put(main / rel / "data" / "pack_verification" / "r.jsonl", "HOSTREC\n")
               for rel in ("tasks/2026-10-01/t1", "audits/a1")]
    records.append(k2 / "data" / "pack_verification" / "r.jsonl")
    q = {name: shlex.quote(str(path)) for name, path in {
        "k2": k2, "runs": main / "runs", "date": main / "runs" / "2026-10-02", "t1": main / "tasks" / "2026-10-01" / "t1",
        "audits": main / "audits", "a1": main / "audits" / "a1"}.items()}

    def forge(moved: str, back: str, record: str, prepare: str = "") -> str:
        return f"{prepare}mv {back} {moved} && printf FORGED >> {record} && mv {moved} {back}"

    return records, {
        "rename_data": forge(f"{q['k2']}/data.x", f"{q['k2']}/data", f"{q['k2']}/data.x/pack_verification/r.jsonl"),
        "rename_data_upper": f"mv {q['k2']}/DATA {q['k2']}/data.y && printf FORGED >> {q['k2']}/data.y/pack_verification/r.jsonl",
        "nest_date_dir": forge(f"{q['runs']}/X/2026-10-02", q["date"], f"{q['runs']}/X/2026-10-02/k2/data/pack_verification/r.jsonl",
                               f"mkdir -p {q['runs']}/X && "),
        "nest_task_root": forge(f"{q['date']}/Y/k2", q["k2"], f"{q['date']}/Y/k2/data/pack_verification/r.jsonl",
                                f"mkdir -p {q['date']}/Y && "),
        "rename_task_root": forge(f"{q['k2']}.x", q["k2"], f"{q['k2']}.x/data/pack_verification/r.jsonl"),
        "rename_runs": forge(f"{q['runs']}.x", q["runs"], f"{q['runs']}.x/2026-10-02/k2/data/pack_verification/r.jsonl"),
        "tasks_rename_data": forge(f"{q['t1']}/d.x", f"{q['t1']}/data", f"{q['t1']}/d.x/pack_verification/r.jsonl"),
        "audits_rename_data": forge(f"{q['a1']}/d.x", f"{q['a1']}/data", f"{q['a1']}/d.x/pack_verification/r.jsonl"),
        "rename_audits": forge(f"{q['audits']}.x", q["audits"], f"{q['audits']}.x/a1/data/pack_verification/r.jsonl"),
        "append_record": f"printf FORGED >> {shlex.quote(str(records[-1]))}",
    }


@pytest.mark.skipif(not IS_MACOS, reason="正则规则只有 macOS Seatbelt 能表达；Linux 只保护本任务（registry 按 task_root 给，已知边界）")
def test_real_seatbelt_protects_every_task_record_without_task_params(tmp_path):
    """不带本任务的 task_root 参数时，Full Access 与隔离的命令照样改不了别的任务的核验记录，也建不出新的（含大小写变体）；
    9b 三审列的“改名上级目录 → 写记录 → 改回”各种手法都失败，记录一个字节不变。任务目录里其它位置照常可写；副作用：命令不能
    自己新建任务根。"""
    import shlex

    if not _sandbox_ready():
        pytest.skip("平台沙箱不可用")
    home = _home(tmp_path)
    main = home["main"]
    records, attacks = _layout_bypass_attacks(main)
    before = {path: path.read_bytes() for path in records}
    work = main / "runs" / "2026-10-02" / "k2" / "work"
    legit = (f"printf ok > {shlex.quote(str(work.parent / 'data' / 'notes.json'))}",
             f"mkdir -p {shlex.quote(str(work / 'sub' / 'deep'))}",
             f"printf ok > {shlex.quote(str(work / 'tmp.txt'))} && mv {shlex.quote(str(work / 'tmp.txt'))} "
             f"{shlex.quote(str(work / 'tmp2.txt'))}",
             f"printf ok > {shlex.quote(str(main / 'notes.txt'))}")
    for scoped, extra in ((False, {}), (True, {"__sandbox_write_roots": [str(main)]})):
        shell = _shell(home, scoped=scoped)

        def run(command: str, shell=shell, extra=extra):
            return shell.execute({"command": command, "working_dir": str(main), **extra})

        for name, command in attacks.items():
            assert not run(command).ok, (scoped, name)
            assert {path: path.read_bytes() for path in records} == before, (scoped, name)
        for fresh in ("runs/2026-10-03/k9/data/pack_verification", "tasks/2026-10-03/t9/DATA/Pack_Verification",
                      "audits/a-9/data/pack_verification", "runs/2026-10-09/k9/work"):
            assert not run(f"mkdir -p {main / fresh}").ok and not (main / fresh).exists(), (scoped, fresh)
        assert [command for command in legit if not run(command).ok] == [], scoped
    assert all(path.exists() for path in (work.parent, main / "runs", main / "audits" / "a1" / "data"))


def test_sandboxed_commands_carry_the_host_state_read_only_marker(monkeypatch):
    """沙箱里的命令带“宿主状态只读”标记；不进沙箱的宿主命令不带，继承来的同名变量也去掉（标记只由宿主设）。"""
    from agent_py_agent.agent.path_access_policy import HOST_STATE_READ_ONLY_ENV
    from agent_py_agent.agent.tooling.shell import _subprocess_text_env

    monkeypatch.setenv(HOST_STATE_READ_ONLY_ENV, "1")
    assert HOST_STATE_READ_ONLY_ENV not in _subprocess_text_env()
    monkeypatch.delenv(HOST_STATE_READ_ONLY_ENV)
    assert _subprocess_text_env(sandboxed=True)[HOST_STATE_READ_ONLY_ENV] == "1"
    assert _subprocess_text_env("/x/owner", sandboxed=True)[HOST_STATE_READ_ONLY_ENV] == "1"


def test_cli_guard_only_rewrites_permission_failures_inside_the_sandbox(tmp_path, monkeypatch, capsys):
    """CLI 守卫只看宿主标记和结构化错误码（errno、sqlite 错误码），不看报错文字；沙箱外或非权限类错误原样抛出。"""
    import errno
    import sqlite3

    from agent_py_agent.agent.path_access_policy import HOST_STATE_READ_ONLY_ENV
    from agent_py_agent.cli.host_state_guard import (
        CLI_HOST_STATE_READ_ONLY,
        host_state_read_only_guard,
    )

    cant_open = None
    try:
        sqlite3.connect(tmp_path / "missing" / "local.db")
    except sqlite3.OperationalError as exc:
        cant_open = exc
    denied = PermissionError(errno.EPERM, "denied", str(tmp_path / "home" / "config"))
    missing = FileNotFoundError(errno.ENOENT, "missing", "x")

    def guarded(exc: BaseException):
        with host_state_read_only_guard():
            raise exc

    with pytest.raises(PermissionError):
        guarded(denied)
    monkeypatch.setenv(HOST_STATE_READ_ONLY_ENV, "1")
    for failure in (denied, *((cant_open,) if hasattr(cant_open, "sqlite_errorcode") else ())):
        with pytest.raises(SystemExit) as stopped:
            guarded(failure)
        assert stopped.value.code == 1
        assert f"error_code={CLI_HOST_STATE_READ_ONLY}" in capsys.readouterr().err
    with pytest.raises(FileNotFoundError):
        guarded(missing)
    with pytest.raises(ValueError):
        guarded(ValueError("x"))


# 函数用途: 收集 owner home 里全部 A 类宿主状态（文件和目录里的文件）的字节，用来核对命令没改它们。
def _host_state_bytes(root: Path, owner: Path) -> dict[Path, bytes]:
    from agent_py_agent.agent.path_access_policy import host_readonly_paths

    found: dict[Path, bytes] = {}
    for path in host_readonly_paths(root, owner):
        files = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
        found.update({item: item.read_bytes() for item in files})
    return found


@needs_sandbox
@pytest.mark.parametrize("scoped", [False, True], ids=["full-access", "isolated"])
def test_real_sandbox_cli_cannot_start_and_says_so(tmp_path, monkeypatch, scoped):
    """锁住现状（3a 最终裁定）：模型在命令里跑 my-agent CLI 会失败——每条命令都要构造完整 SimpleAgent，启动时要写 workspace/runtime
    下的本地库，而宿主状态在沙箱里只读（隔离 owner 原来就这样）。失败给结构化码 CLI_HOST_STATE_READ_ONLY，A 类字节不变。
    只读子命令走只读启动是台账里的待做项。"""
    import agent_py_agent
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
    from agent_py_agent.cli.host_state_guard import CLI_HOST_STATE_READ_ONLY

    if not _sandbox_ready():
        pytest.skip("平台沙箱不可用")
    root = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    project = tmp_path / "proj"
    project.mkdir()
    agent = SimpleAgent(AgentConfig(my_agent_home=str(root)), root=project)
    main = Path(agent.home_paths.owner_home_dir).resolve()
    before = _host_state_bytes(root, main)
    assert any("local_store" in str(path) for path in before), "宿主已建好本地库"
    config = _put(tmp_path / "cli.yaml", f'my_agent_home: "{root}"\n')
    options = (ShellToolOptions(owner_scope_root=str(main), host_private_roots=(str(root),)) if scoped
               else ShellToolOptions(protected_persona_root=str(main)))
    shell = ShellTool(main, options=options)
    work = main if scoped else project
    extra = {"__sandbox_write_roots": [str(main)]} if scoped else {}
    # 钉住本检出的源码：venv 的可编辑安装可能指向别的检出。
    python = f"PYTHONPATH={Path(agent_py_agent.__file__).resolve().parents[1]} {sys.executable}"
    probe = shell.execute({"command": f"{python} -c 'import agent_py_agent'", "working_dir": str(work), **extra})
    if not probe.ok:
        pytest.skip("沙箱视图里看不到本仓库源码（Linux 隔离视图只挂授权根）")

    outcome = shell.execute({"command": f"{python} -m agent_py_agent --config {config} status",
                             "working_dir": str(work), "timeout": 120, **extra})

    assert not outcome.ok
    assert f"error_code={CLI_HOST_STATE_READ_ONLY}" in outcome.output, outcome.output[-2000:]
    assert _host_state_bytes(root, main) == before
