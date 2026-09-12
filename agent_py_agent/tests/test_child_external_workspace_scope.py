"""LLM: 子代理在 owner 墙外的"已授权工作目录"必须真的可用——写根与执行根同源。

真机证据（2026-09-11 四路换语言复刻）：4 个并行子代理各被要求写一个 core_*.go，
结构化事实里 `allowed_write_roots` **已经包含** `/private/tmp/ma-eval/port-click-go`，
但 `write_file` 仍报 PATH_OWNER_SCOPE_BLOCKED（handler 未执行），`run_command`
在目标目录写文件报 Operation not permitted，`read_file` 读源库同样被拦。
根因不是"模型只读不写"，而是：授权写根只进了 `allowed_write_roots`，没有投影成
执行根（`execution_workspace_roots` / `execution_cwd`），于是路径门前一关就按
owner home 拦下，后面的写边界门根本没机会跑。

规则（沿用用户 2026-09-11 的裁决）：
- 子代理继承"父代理当时真正工作的那个目录及其子树"，不继承父代理的权限档位；
- 文件系统根级目录（`/`、`/etc`、`/System` …）永不自动继承；
- 没有 owner 墙（管理员 full-access）时行为不变；
- owner 墙内的一切仍按原规则，本文件只覆盖"墙外已授权根"的投影。
"""

from __future__ import annotations

import types
from pathlib import Path

from agent_py_agent.agent.agent_core.tool_runtime_ledger import (
    write_boundary_with_runtime_ledger,
)
from agent_py_agent.agent.contracts.gates.path_url_command import (
    PathUrlCommandFacts,
    evaluate_path_url_command_gate,
)
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
)


def _owner_home(tmp_path: Path) -> Path:
    """按真实布局摆放：<my-agent home>/owners/<provider>/<owner>，项目目录在 home 之外。"""

    return tmp_path / "my-agent" / "owners" / "local" / "main"


def _child_params(
    *,
    project: Path,
    owner_home: Path,
    task_root: Path,
    granted: list[str],
    declared_cwd: str = "",
) -> types.SimpleNamespace:
    """构造一个子代理 run 的结构化事实：task_local 作用域 + 宿主已授权的写根。

    真机形态（2026-09-11 subagent-1789151302-7f4f828f 的 canonical_state）：
    `allowed_write_roots` 含目标目录，但 boundary 里**没有** `execution_workspace_roots`，
    也没有 `effective_owner_scope_root`——owner 墙由子代理 registry 携带，账本看不到。
    """

    attrs: dict[str, object] = {
        "conversation_thread_id": "thread-1",
        "run_workspace": {
            "task_root": str(task_root),
            "work_dir": str(task_root / "work"),
            "output_dir": str(task_root / "output"),
        },
    }
    if declared_cwd:
        attrs[CONVERSATION_EXECUTION_CWD_ATTR] = declared_cwd
        attrs[CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR] = [declared_cwd]
    return types.SimpleNamespace(
        task_attributes=attrs,
        delivery_contract=None,
        write_boundary={"allowed_write_roots": list(granted)},
        context_scope="task_local",
        source="subagent_runner",
        run_id="subagent-1",
    )


def _child_agent(*, owner_home: Path, manager_scope: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            my_agent_owner_provider="local",
            access_mode="full-access",
        ),
        home_paths=types.SimpleNamespace(
            owner_home_dir=str(owner_home),
            owner_provider="local",
            owner_kind="main",
        ),
        tools=types.SimpleNamespace(owner_scope_root=""),
        subagents=types.SimpleNamespace(owner_scope_root=manager_scope),
    )


# LLM: owner 墙存在时，账本里的 owner-home 收窄不能覆盖宿主创建时下发的墙外授权根：
#   真机复测（2026-09-11 deadlock-test5）就是被这一步覆盖后，运行时 allowed_write_roots 只剩
#   owner home，写门直接给出 WRITE_FORBIDDEN，子代理只能上抛能力申请。
# 函数用途: 验证 owner 作用域收窄保留已授权的墙外工作目录。
def test_owner_scoped_child_keeps_granted_external_root(tmp_path):
    owner_home = _owner_home(tmp_path)
    project = tmp_path / "work" / "port-click-go"
    task_root = owner_home / "runs" / "2026-09-11" / "abc"
    for path in (owner_home, project, task_root):
        path.mkdir(parents=True, exist_ok=True)

    params = _child_params(
        project=project,
        owner_home=owner_home,
        task_root=task_root,
        granted=[str(task_root), str(owner_home), str(project)],
        declared_cwd=str(project),
    )
    boundary = write_boundary_with_runtime_ledger(
        _child_agent(owner_home=owner_home, manager_scope=str(owner_home)),
        params,
    )

    assert str(project) in boundary["allowed_write_roots"]
    assert str(project) in boundary["execution_workspace_roots"]
    assert str(owner_home) in boundary["allowed_write_roots"]
    assert boundary.get("execution_cwd") == str(project)


# LLM: 这是本文件的核心回归——墙外已授权根必须出现在执行根里，否则路径门只会看到 owner home。
# 函数用途: 验证子代理 boundary 把 allowed_write_roots 的墙外根投影成 execution_workspace_roots。
def test_child_boundary_projects_granted_external_root(tmp_path):
    owner_home = _owner_home(tmp_path)
    project = tmp_path / "work" / "port-click-go"
    task_root = owner_home / "runs" / "2026-09-11" / "abc"
    for path in (owner_home, project, task_root):
        path.mkdir(parents=True, exist_ok=True)

    params = _child_params(
        project=project,
        owner_home=owner_home,
        task_root=task_root,
        granted=[str(task_root), str(owner_home), str(project)],
        declared_cwd=str(project),
    )
    boundary = write_boundary_with_runtime_ledger(
        _child_agent(owner_home=owner_home),
        params,
    )

    execution_roots = [Path(str(item)) for item in boundary.get("execution_workspace_roots") or []]
    assert project in execution_roots, boundary.get("execution_workspace_roots")
    assert boundary.get("execution_cwd") == str(project)


# LLM: 端到端合同：执行根齐了，路径门才允许写墙外已授权目录；没有执行根时必须仍是硬拒绝。
# 函数用途: 验证"写根同源"确实把 PATH_OWNER_SCOPE_BLOCKED 变成放行。
def test_path_gate_allows_granted_external_root_only_with_execution_root(tmp_path):
    owner_home = _owner_home(tmp_path)
    project = tmp_path / "work" / "port-click-go"
    owner_home.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)

    payload = {"tool": "write_file", "path": str(project / "core_types.go")}
    without_root = evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload=payload,
            workspace_root=owner_home,
            workspace_roots=[owner_home],
            owner_scope_root=str(owner_home),
        )
    )
    assert without_root.allowed is False
    assert "PATH_OWNER_SCOPE_BLOCKED" in without_root.finding_codes

    with_root = evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload=payload,
            workspace_root=owner_home,
            workspace_roots=[owner_home, project],
            owner_scope_root=str(owner_home),
        )
    )
    assert with_root.allowed is True


# LLM: 投影只覆盖"已授权"的墙外根；owner 墙外的未授权路径仍旧是硬拒绝，口子不放大。
# 函数用途: 验证未授权外部路径仍被 PATH_OWNER_SCOPE_BLOCKED。
def test_unrelated_external_path_still_blocked(tmp_path):
    owner_home = _owner_home(tmp_path)
    project = tmp_path / "work" / "port-click-go"
    other = tmp_path / "work" / "someone-else"
    for path in (owner_home, project, other):
        path.mkdir(parents=True, exist_ok=True)

    decision = evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload={"tool": "write_file", "path": str(other / "x.go")},
            workspace_root=owner_home,
            workspace_roots=[owner_home, project],
            owner_scope_root=str(owner_home),
        )
    )
    assert decision.allowed is False
    assert "PATH_OWNER_SCOPE_BLOCKED" in decision.finding_codes


# LLM: 根级目录是操作系统本体，不能因为出现在 allowed_write_roots 里就变成执行根。
# 函数用途: 验证 / 与 /etc 等根级目录永不被投影成子代理执行根。
def test_root_level_dirs_never_projected(tmp_path):
    owner_home = _owner_home(tmp_path)
    task_root = owner_home / "runs" / "abc"
    owner_home.mkdir(parents=True, exist_ok=True)
    task_root.mkdir(parents=True, exist_ok=True)

    params = _child_params(
        project=tmp_path,
        owner_home=owner_home,
        task_root=task_root,
        granted=[str(task_root), str(owner_home), "/etc", "/"],
    )
    boundary = write_boundary_with_runtime_ledger(
        _child_agent(owner_home=owner_home),
        params,
    )
    roots = list(boundary.get("execution_workspace_roots") or [])
    assert "/etc" not in roots
    assert "/" not in roots


# LLM: 投影只把"已经授权"的根搬到执行层，绝不引入任何新写权限；这条守住"不放宽边界"的底线。
# 函数用途: 验证 execution_workspace_roots 的每一项都在 allowed_write_roots 或 owner home 之内。
def test_projection_never_widens_allowed_write_roots(tmp_path):
    owner_home = _owner_home(tmp_path)
    project = tmp_path / "work" / "port-click-go"
    task_root = owner_home / "runs" / "abc"
    for path in (owner_home, project, task_root):
        path.mkdir(parents=True, exist_ok=True)

    params = _child_params(
        project=project,
        owner_home=owner_home,
        task_root=task_root,
        granted=[str(task_root), str(owner_home), str(project)],
        declared_cwd=str(project),
    )
    boundary = write_boundary_with_runtime_ledger(_child_agent(owner_home=owner_home), params)

    allowed = [Path(str(item)) for item in boundary["allowed_write_roots"]]
    for raw in boundary.get("execution_workspace_roots") or []:
        root = Path(str(raw))
        assert root == owner_home or any(
            root == item or root.is_relative_to(item) for item in allowed
        ), raw
    for name in ("task_root", "task_work_dir", "task_output_dir"):
        if boundary.get(name):
            assert Path(str(boundary[name])) in allowed or Path(str(boundary[name])).is_relative_to(owner_home)


# LLM: 没有任何墙外授权根时不得凭空派生——没有结构化事实就没有执行根，保持 fail-closed。
# 函数用途: 验证只授权 owner home 内部时 boundary 不新增 execution_workspace_roots。
def test_no_granted_external_root_keeps_boundary_unchanged(tmp_path):
    owner_home = _owner_home(tmp_path)
    project = tmp_path / "work" / "port-click-go"
    task_root = owner_home / "runs" / "abc"
    for path in (owner_home, project, task_root):
        path.mkdir(parents=True, exist_ok=True)

    params = _child_params(
        project=project,
        owner_home=owner_home,
        task_root=task_root,
        granted=[str(task_root), str(owner_home)],
    )
    boundary = write_boundary_with_runtime_ledger(_child_agent(owner_home=owner_home), params)
    assert not boundary.get("execution_workspace_roots")


# LLM: handler 自己的 owner 墙是第四份墙：中央门和写边界门放行后，文件工具仍会用
#   `path_access_policy.check` 再判一次。逃生前必须只认宿主下发的 granted_external_roots，
#   不能认 workspace_roots（否则"改一个可变列表"就能放权）。
# 函数用途: 验证文件工具在宿主授权根内读写放行，未授权路径与凭据文件仍拒。
def test_file_tool_owner_wall_escape_requires_host_grant(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling._filesystem_read import filesystem_access_options
    from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool, WriteFileToolOptions

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "my-agent"))
    owner_home = _owner_home(tmp_path)
    project = tmp_path / "work" / "port-click-go"
    other = tmp_path / "work" / "someone-else"
    for path in (owner_home, project, other):
        path.mkdir(parents=True, exist_ok=True)

    tool = WriteFileTool(
        owner_home,
        [owner_home],
        WriteFileToolOptions(
            access_options=filesystem_access_options(owner_scope_root=str(owner_home))
        ),
    )

    blocked = tool.execute({"path": str(project / "a.go"), "content": "package a"})
    assert blocked.ok is False
    assert not (project / "a.go").exists()

    tool.workspace_roots = [owner_home.resolve(), project.resolve()]
    still_blocked = tool.execute({"path": str(project / "a.go"), "content": "package a"})
    assert still_blocked.ok is False, "改 workspace_roots 不能放宽 owner 墙"
    assert not (project / "a.go").exists()

    tool.granted_external_roots = (project.resolve(),)
    written = tool.execute({"path": str(project / "a.go"), "content": "package a"})
    assert written.ok is True, written.output
    assert (project / "a.go").read_text(encoding="utf-8") == "package a"

    outside = tool.execute({"path": str(other / "b.go"), "content": "package b"})
    assert outside.ok is False
    assert not (other / "b.go").exists()


# LLM: 命令执行是第四份 owner 墙：文件工具放行后，run_command 的 working_dir 仍会用
#   path_access_policy 再判一次（真机报 COMMAND_ACCESS_DENIED: 当前 owner 只能在
#   WorkspaceOnly 范围内执行命令）。逃生口与文件工具同一规则、同一份宿主授权。
# 函数用途: 验证命令工作目录在墙外已授权根内放行，未授权目录仍拒。
def test_shell_working_dir_escape_requires_host_grant(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.shell import (
        ShellTool,
        ShellToolOptions,
        _working_dir_from_params,
    )

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "my-agent"))
    owner_home = _owner_home(tmp_path)
    project = tmp_path / "work" / "port-click-go"
    other = tmp_path / "work" / "someone-else"
    for path in (owner_home, project, other):
        path.mkdir(parents=True, exist_ok=True)

    tool = ShellTool(
        owner_home,
        options=ShellToolOptions(
            workspace_roots=[owner_home],
            owner_scope_root=str(owner_home),
            access_mode="workspace-write",
        ),
    )

    denied = _working_dir_from_params(
        {"working_dir": str(project)},
        tool.workspace_root,
        workspace_roots=[owner_home, project.resolve()],
        path_access_policy=tool.path_access_policy,
        access_mode="workspace-write",
    )
    assert not isinstance(denied, Path)

    allowed = _working_dir_from_params(
        {"working_dir": str(project)},
        tool.workspace_root,
        workspace_roots=[owner_home, project.resolve()],
        path_access_policy=tool.path_access_policy,
        access_mode="workspace-write",
        granted_external_roots=(project.resolve(),),
    )
    assert isinstance(allowed, Path) and allowed == project.resolve()

    still_denied = _working_dir_from_params(
        {"working_dir": str(other)},
        tool.workspace_root,
        workspace_roots=[owner_home, project.resolve()],
        path_access_policy=tool.path_access_policy,
        access_mode="workspace-write",
        granted_external_roots=(project.resolve(),),
    )
    assert not isinstance(still_denied, Path)
