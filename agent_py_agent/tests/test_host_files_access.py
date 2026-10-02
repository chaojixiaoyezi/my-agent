"""H3 合同单测：宿主托管文件对模型只读，宿主凭据对文件工具不可读。

来源：be 复审语义记忆时发现管理员的文件工具和命令能直接写 ~/.my-agent/config/（绕过参数中心的边界键、修改账本、撤销和
manage_models 的检查）；3a 定为硬门（2026-10-02），随后扩成“宿主托管文件对模型只读”：
- 宿主配置：数据根 config/、system/config/，每个 owner home 的 config/ → PATH_HOST_CONFIG_WRITE_BLOCKED，读照常；
- 宿主运行状态（A 类，绝对只读，任何模式、不可穿透）：9b 家目录盘点的各项（权限/配额/策略文件及其 .lock、审计流水、
  runtime.db 及 -wal/-shm/-journal、workspace/runtime、audit、Curator 事务、记忆流水与候选、记忆归档、缓存、回收站、
  owner 级正式 skill 等），规范任务根的 data/pack_verification（ae 能力包核验记录）→ PATH_HOST_STATE_WRITE_BLOCKED，读照常；
- B 类（runs/、agents/、data/、tasks/）仍是 tool_runtime_ledger 的 forbidden_write_roots：只在隔离模式生效，可被本任务工作目录穿透；
- 宿主凭据：管理员密码、模型目录、共享模型档案、数据根 config/ 里的 YAML 配置及备份、数据根里 owner home 之外的 secrets
  目录 → 文件工具读写都拒 PATH_HOST_CREDENTIAL_BLOCKED；命令只拒写不拒读（my-agent CLI 要读，已知边界）。
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
             "memory_archive", "cache", "trash", "skills", ".agents/skills")

    scoped = set(host_readonly_paths_for(str(main), "", (task / "work",)))
    assert scoped == {main / "config", *(main / name for name in state), home["verification"]}
    full = set(host_readonly_paths_for("", str(main), (task / "work" / "agents" / "r-1",)))
    owners = (main, feishu, bare)
    assert full == {root / "config", root / "system" / "config", *(home_dir / "config" for home_dir in owners),
                    *(home_dir / name for home_dir in owners for name in state), home["verification"]}
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

    def run(command: str, cwd: Path = main):
        return shell.execute({"command": command, "working_dir": str(cwd)})

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
    # 命令只拒写不拒读（my-agent CLI 要读配置，已知边界）；普通写照常。
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
    """B 类：runs/、agents/、data/、tasks/ 留在 tool_runtime_ledger，只在隔离模式挂、可被本任务工作目录穿透；A 类不再重复列。"""
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
    assert scoped["forbidden_write_roots"] == [str(main / name) for name in ("runs", "agents", "data", "tasks")]
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
