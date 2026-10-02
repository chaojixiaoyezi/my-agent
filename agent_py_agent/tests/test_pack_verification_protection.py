"""能力包 v2 块 4：核验账本防伪造（9b 复审定的开关前提 b）。

宿主把核验账本、原件清单和原件副本放在 <规范任务根>/data/pack_verification/，靠 be 的 H3 把它设成对模型只读。
这里用块 4 的真实流程落盘，再分别以主代理和子代理的身份、用文件工具和真实 Shell 去写它，确认都写不进、读照常。
只读结构化事实（错误码、退出状态、文件字节），不起 Gateway、不碰真实 owner home；真实沙箱用例按平台跳过。
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.pack_verification_hooks import capture_baseline_before_tool
from agent_py_agent.agent.capability.pack_verification_ledger import PACK_VERIFICATION_DIRECTORY
from agent_py_agent.agent.path_access_policy import (
    HOST_STATE_TASK_PARTS,
    host_state_for_path,
    task_root_containing,
)
from agent_py_agent.tests.test_host_files_access import (
    STATE_CODE,
    _registry,
    _sandbox_ready,
    needs_sandbox,
)
from agent_py_agent.tests.test_pack_verification_service import _write, build_env

FORGED_RECORD = '{"kind": "result", "status": "passed"}'


# 函数用途: 跑一遍块 4 的真实落盘（基线、账本、原件清单和副本），返回环境和账本目录里每个文件的原始字节。
def _recorded(tmp_path, monkeypatch):
    env = build_env(tmp_path, monkeypatch)
    _write(env.workspace / "in/source.json", {"schema": "source.v1"})
    capture_baseline_before_tool(env.agent, env.params, "write_file")
    pack = env.task_root.joinpath(*PACK_VERIFICATION_DIRECTORY)
    data_root = Path(env.task_root).parents[5]
    monkeypatch.setenv("MY_AGENT_HOME", str(data_root))
    env.pack, env.data_root, env.owner_home = pack, data_root, Path(env.task_root).parents[2]
    env.child = env.task_root / "work" / "agents" / "child-run"
    for path in (env.task_root / "work", env.task_root / "output", env.child):
        path.mkdir(parents=True, exist_ok=True)
    return env, _snapshot(pack)


# 函数用途: 账本目录里每个文件的相对路径和字节。
def _snapshot(pack: Path) -> dict[str, bytes]:
    return {path.relative_to(pack).as_posix(): path.read_bytes() for path in sorted(pack.rglob("*")) if path.is_file()}


def test_ledger_location_is_exactly_what_h3_protects(tmp_path, monkeypatch):
    env, files = _recorded(tmp_path, monkeypatch)
    assert {"run-1.jsonl", "originals.json"} <= set(files) and any(name.startswith("originals/") for name in files)
    assert PACK_VERIFICATION_DIRECTORY in HOST_STATE_TASK_PARTS, "块 4 的落盘位置和 H3 的只读声明是同一个"
    assert env.owner_home.parts[-3:] == ("owners", "local", "main")
    for name in files:
        assert host_state_for_path(env.pack / name, env.data_root) == env.pack, name
    assert task_root_containing(env.child, env.owner_home) == env.task_root, "子代理工作目录往上认得出同一个任务根"


def test_ledger_directory_exists_before_the_first_shell_command(tmp_path, monkeypatch):
    import types

    env = build_env(tmp_path, monkeypatch)
    pack = env.task_root.joinpath(*PACK_VERIFICATION_DIRECTORY)
    shell = types.SimpleNamespace(runtime_policy=types.SimpleNamespace(mutates_workspace=True))
    env.params.tool_runtime_snapshot = types.SimpleNamespace(runtime=lambda name: shell if name == "run_command" else None)
    capture_baseline_before_tool(env.agent, env.params, "run_command")
    # Linux bwrap 只能只读挂载已存在的路径：第一个 Shell 命令执行前目录就得在
    assert pack.is_dir() and not pack.is_symlink() and (pack.stat().st_mode & 0o777) == 0o700


# 文件工具的伪造方式：追加、整份覆盖、改写、删除、补丁删除原件副本
def _file_tool_forgeries(pack: Path, files: dict[str, bytes]) -> list[tuple[str, dict]]:
    ledger, originals = pack / "run-1.jsonl", pack / "originals.json"
    copy = pack / next(name for name in files if name.startswith("originals/"))
    first_line = files["run-1.jsonl"].decode().splitlines()[0]
    return [
        ("write_file", {"path": str(ledger), "content": FORGED_RECORD + "\n"}),
        ("write_file", {"path": str(originals), "content": "{}"}),
        ("write_file", {"path": str(pack / "run-2.jsonl"), "content": FORGED_RECORD + "\n"}),
        ("edit_file", {"path": str(ledger), "old_string": first_line, "new_string": FORGED_RECORD}),
        ("apply_patch", {"patch": f"*** Begin Patch\n*** Delete File: {copy}\n*** End Patch"}),
    ]


@pytest.mark.parametrize("actor", ["main", "subagent"])
@pytest.mark.parametrize("isolated", [False, True])
def test_file_tools_cannot_forge_the_ledger(tmp_path, monkeypatch, actor, isolated):
    env, files = _recorded(tmp_path, monkeypatch)
    workspace = env.workspace if actor == "main" else env.child
    tools = _registry(workspace, owner_scope=env.owner_home if isolated else None,
                      roots=[workspace, env.task_root / "work"] if isolated else None)
    for name, params in _file_tool_forgeries(env.pack, files):
        outcome = tools[name].execute(params)
        assert (outcome.ok, outcome.error_code) == (False, STATE_CODE), (actor, isolated, name, outcome.output)
    assert _snapshot(env.pack) == files, "一个字节都没改，也没多出文件"
    assert tools["read_file"].execute({"path": str(env.pack / "run-1.jsonl")}).ok, "宿主核验记录只拒写，读照常"


# Shell 的伪造方式：追加、覆盖原件清单、新建账本、删除、整个目录改名
def _shell_forgeries(pack: Path) -> list[str]:
    ledger, originals = shlex.quote(str(pack / "run-1.jsonl")), shlex.quote(str(pack / "originals.json"))
    return [f"printf '%s\\n' {shlex.quote(FORGED_RECORD)} >> {ledger}", f"printf '{{}}' > {originals}",
            f"printf x > {shlex.quote(str(pack / 'run-2.jsonl'))}", f"rm -f {ledger}",
            f"mv {shlex.quote(str(pack))} {shlex.quote(str(pack) + '.moved')}"]


# LLM: 真实 run_command。where = (工作目录, 写根)；isolated=False 是本机管理员 Full Access（人格根 = 本机主用户），
#   True 是隔离 owner。Full Access 的沙箱参数按生产路径由 registry 投影：写边界带宿主写入的结构化 task_root
#   （H3 定：本任务的核验记录只从它推，不再从工作目录或写根猜），子代理的 task_root 也是共享的规范任务根。
# 函数用途: 以给定工作目录和写根跑一条命令。
def _run(env, command: str, where: tuple[Path, list[Path]], isolated: bool):
    cwd, roots = where
    from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions

    options = (ShellToolOptions(owner_scope_root=str(env.owner_home), host_private_roots=(str(env.data_root),)) if isolated
               else ShellToolOptions(protected_persona_root=str(env.owner_home)))
    params = {"command": command, "working_dir": str(cwd)}
    if isolated:
        params["__sandbox_write_roots"] = [str(root) for root in roots]
    else:
        params.update(_projected_sandbox_params(env, cwd, roots))
    return ShellTool(env.owner_home, options=options).execute(params)


# LLM: 和生产一样经 registry_invoke._tool_params_with_runtime_boundary 组沙箱参数，只取 __sandbox_ 开头的键并进命令参数。
# 函数用途: 按写边界 {task_root, allowed_write_roots} 生成 Full Access 命令的沙箱参数。
def _projected_sandbox_params(env, cwd: Path, roots: list[Path]) -> dict:
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.registry_invoke import (
        AuthorizedToolDispatchRequest,
        _tool_params_with_runtime_boundary,
    )

    boundary = {"task_root": str(env.task_root), "allowed_write_roots": [str(root) for root in roots]}
    projected = _tool_params_with_runtime_boundary(AuthorizedToolDispatchRequest(
        tool_name="run_command", tool=SimpleNamespace(), tool_params={}, workspace_root=cwd, write_boundary=boundary))
    return {key: value for key, value in projected.items() if key.startswith("__sandbox_")}


# LLM: 主代理在本任务 work/ 里；主代理在用户项目目录、写根带本任务 work/ 和 output/（tool_runtime_ledger 给主链任务注入的形状；
#   隔离 owner 的项目目录只能在自家 home 里）；子代理在自己的工作目录。返回 (工作目录, 写根)。
# 函数用途: 按角色和模式给出一条命令的工作目录和写根。
def _shell_actor(env, actor: str, isolated: bool) -> tuple[Path, list[Path]]:
    task_roots = [env.task_root / "work", env.task_root / "output"]
    if actor == "main-in-task-work":
        return env.task_root / "work", task_roots
    if actor == "main-with-task-write-roots":
        project = env.owner_home / "ws" if isolated else env.workspace
        project.mkdir(parents=True, exist_ok=True)
        return project, [project, *task_roots]
    return env.child, [env.child]


SHELL_ACTORS = ("main-in-task-work", "main-with-task-write-roots", "subagent")


@needs_sandbox
@pytest.mark.parametrize("actor", SHELL_ACTORS)
@pytest.mark.parametrize("isolated", [False, True])
def test_real_shell_cannot_forge_the_ledger(tmp_path, monkeypatch, actor, isolated):
    if not _sandbox_ready():
        pytest.skip("平台沙箱不可用")
    env, files = _recorded(tmp_path, monkeypatch)
    cwd, roots = _shell_actor(env, actor, isolated)
    for command in _shell_forgeries(env.pack):
        outcome = _run(env, command, (cwd, roots), isolated)
        assert not outcome.ok, (actor, isolated, command, outcome.output)
    assert _snapshot(env.pack) == files and not Path(str(env.pack) + ".moved").exists()
    # 上面的拒绝要是“只读挡住”，不是“这条命令整个跑不起来”：同样的工作目录和写根下，读账本、写自己的目录都照常
    readable = _run(env, f"cat {shlex.quote(str(env.pack / 'originals.json'))}", (cwd, roots), isolated)
    assert readable.ok and "in/source.json" in readable.output, "读照常（返工时 cp 副本要用）"
    restored = _run(env, f"printf ok > {shlex.quote(str(cwd / 'restored.txt'))}", (cwd, roots), isolated)
    assert restored.ok, "自己的工作目录照常可写"


# 曾经的结构缺口（ae 报、be 修，3a 定为必须修）：命令的工作目录和写根都在任务树外时，旧版 H3 找不到当前任务根。
# 现在本任务的核验记录只从写边界里的结构化 task_root 推出，和工作目录、写根无关。
@needs_sandbox
def test_full_access_shell_outside_the_task_tree_cannot_forge_the_ledger(tmp_path, monkeypatch):
    """主代理 Full Access、工作目录和写根都在任务树外（用户项目目录），账本目录也要只读。"""
    if not _sandbox_ready():
        pytest.skip("平台沙箱不可用")
    env, files = _recorded(tmp_path, monkeypatch)
    where = (env.workspace, [env.workspace])
    for command in _shell_forgeries(env.pack):
        outcome = _run(env, command, where, False)
        assert not outcome.ok, (command, outcome.output)
    assert _snapshot(env.pack) == files and not Path(str(env.pack) + ".moved").exists()
    assert _run(env, f"cat {shlex.quote(str(env.pack / 'originals.json'))}", where, False).ok, "读照常"
    assert _run(env, f"printf ok > {shlex.quote(str(env.workspace / 'restored.txt'))}", where, False).ok, "项目目录照常可写"
