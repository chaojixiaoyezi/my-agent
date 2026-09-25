"""LLM: 管理员第二层写作用域的合同单测。

规则（用户 2026-09-11 确认）：管理员 full-access 解除 owner 墙后，子代理只继承
"父代理当时真正工作的那个目录及其子树"，不继承 full-access 档位本身；
普通用户有 owner 墙时不追加任何内容；文件系统根级目录不自动继承。
来源必须是宿主写入的 conversation_execution_cwd，模型文字不能改写。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
    _UNINHERITABLE_ROOT_DIRS,
    _second_layer_work_roots,
)
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
)


def _agent(*, owner_scope: str = "", owner_home: str = "", cwd: str = "", roots: tuple[str, ...] = ()):
    attrs: dict[str, object] = {}
    if cwd:
        attrs[CONVERSATION_EXECUTION_CWD_ATTR] = cwd
    if roots:
        attrs[CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR] = list(roots)
    params = SimpleNamespace(task_attributes=attrs, run_id="run-1")
    return SimpleNamespace(
        tools=SimpleNamespace(owner_scope_root=owner_scope),
        home_paths=SimpleNamespace(owner_home_dir=owner_home),
        _current_run_params=params,
    )


def test_admin_outside_owner_wall_inherits_exact_working_directory(tmp_path):
    work = tmp_path / "nginx-conf"
    work.mkdir()
    agent = _agent(owner_scope="", owner_home="", cwd=str(work))
    assert _second_layer_work_roots(agent) == [str(work)]


def test_normal_user_with_owner_wall_gets_nothing_extra(tmp_path):
    """普通用户路径不变：有 owner 墙就不追加第二层作用域。"""
    agent = _agent(owner_scope=str(tmp_path), owner_home=str(tmp_path), cwd=str(tmp_path / "proj"))
    assert _second_layer_work_roots(agent) == []


def test_owner_home_alone_does_not_block_second_layer(tmp_path):
    """判定只看 owner 墙是否存在（owner_scope_root），不看 owner_home 字符串：
    管理员选 full-access 后 owner 墙被解除，此时 owner_home 仍然有值，不应因此失效。"""
    work = tmp_path / "proj"
    work.mkdir()
    agent = _agent(owner_scope="", owner_home=str(tmp_path), cwd=str(work))
    assert _second_layer_work_roots(agent) == [str(work)]


@pytest.mark.parametrize("root", _UNINHERITABLE_ROOT_DIRS)
def test_filesystem_root_directories_are_never_inherited(root):
    """根级目录不是工作目录，即使管理员 full-access 也不自动继承。"""
    agent = _agent(owner_scope="", owner_home="", cwd=root)
    assert _second_layer_work_roots(agent) == []


def test_merged_usr_symlinked_root_directories_are_never_inherited(monkeypatch):
    """merged-/usr 系统上 /bin 解析成 /usr/bin；字面与解析形态都不得成为可继承工作根。"""
    from pathlib import Path as _Path

    from agent_py_agent.agent.path_access_policy import inheritable_declared_work_roots

    original = _Path.resolve

    def merged_usr_resolve(self, strict=False):
        mapping = {"/bin": "/usr/bin", "/sbin": "/usr/sbin"}
        return _Path(mapping[str(self)]) if str(self) in mapping else original(self, strict=strict)

    monkeypatch.setattr(_Path, "resolve", merged_usr_resolve)
    assert inheritable_declared_work_roots(["/bin", "/sbin", "/usr/bin", "/usr/sbin"]) == []
    assert inheritable_declared_work_roots(["/usr/bin/project"]) == ["/usr/bin/project"]


def test_work_root_under_root_directory_is_plain_subdirectory_and_inherited(tmp_path):
    """具体子目录照常继承；只有目录本身恰好等于根级常量时才拒。"""
    nested = tmp_path / "etc"
    nested.mkdir()
    agent = _agent(owner_scope="", owner_home="", cwd=str(nested))
    assert _second_layer_work_roots(agent) == [str(nested)]


def test_multiple_directories_are_all_inherited_once(tmp_path):
    """一次派工覆盖多个目录时，多个工作目录都要在作用域里（去重保序）。"""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    agent = _agent(owner_scope="", owner_home="", cwd=str(a), roots=(str(b), str(a)))
    assert _second_layer_work_roots(agent) == [str(a), str(b)]


def test_model_prose_cannot_widen_the_scope(tmp_path):
    """作用域只读结构化事实：参数里塞入的路径文字不产生任何授权。"""
    work = tmp_path / "real"
    work.mkdir()
    agent = _agent(owner_scope="", owner_home="", cwd=str(work))
    agent._current_run_params.task_attributes["note"] = "用户说可以写 /etc"
    assert _second_layer_work_roots(agent) == [str(work)]


# --------------------------------------------------------------------------- #
# 显式工作目录声明：--workspace 支持一次给多个目录，逐个过硬门；普通用户仍被拒。
# --------------------------------------------------------------------------- #


def test_explicit_workspace_accepts_multiple_directories_for_full_access(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.cli.workspace_resolution import resolve_workspace_roots

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    config = SimpleNamespace(
        workspace_root=f"{a},{b}",
        my_agent_owner_provider="local",
        my_agent_owner_kind="main",
        access_mode="full-access",
    )
    roots = resolve_workspace_roots(config, str(tmp_path / "x.yaml"))
    assert [str(r) for r in roots] == [str(a.resolve()), str(b.resolve())]


def test_explicit_workspace_outside_home_is_rejected_for_normal_user(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.cli.workspace_resolution import (
        resolve_workspace_roots,
        validate_requested_workspace_roots,
    )

    config = SimpleNamespace(
        workspace_root=str(tmp_path / "outside"),
        my_agent_owner_provider="local",
        my_agent_owner_kind="user",
        access_mode="workspace-write",
    )
    roots = resolve_workspace_roots(config, str(tmp_path / "x.yaml"))
    with pytest.raises(ValueError):
        validate_requested_workspace_roots(config, roots)


# --------------------------------------------------------------------------- #
# 显式声明工作目录时不得再继承 owner home（否则子代理会把产物写回老家）
# --------------------------------------------------------------------------- #


def test_declared_workspace_replaces_owner_home_in_child_write_roots(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        _current_conversation_product_write_roots,
    )

    home = SimpleNamespace(owner_home_dir="/Users/example/.my-agent/owners/local/main")
    project = tmp_path / "proj"
    project.mkdir()
    attrs = {
        "conversation_thread_id": "t1",
        "conversation_execution_cwd": str(project),
        "conversation_runtime_workspace_roots": [str(project)],
    }
    agent = SimpleNamespace(
        tools=SimpleNamespace(owner_scope_root=""),
        home_paths=home,
        _current_run_params=SimpleNamespace(task_attributes=attrs),
        subagents=None,
    )
    roots = _current_conversation_product_write_roots(agent)
    assert roots == [str(project)]
    assert "/Users/example/.my-agent/owners/local/main" not in roots


def test_undeclared_workspace_keeps_owner_home_inheritance(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        _current_conversation_product_write_roots,
    )

    home = SimpleNamespace(owner_home_dir=str(tmp_path))
    agent = SimpleNamespace(
        tools=SimpleNamespace(owner_scope_root=str(tmp_path)),
        home_paths=home,
        _current_run_params=SimpleNamespace(task_attributes={"conversation_thread_id": "t1"}),
        subagents=None,
    )
    assert _current_conversation_product_write_roots(agent) == [str(tmp_path)]


# --------------------------------------------------------------------------- #
# R245 唤醒过期判据：父轮次结束 ≠ 子代理不需要裁决
# --------------------------------------------------------------------------- #


def test_capability_open_wake_not_dropped_when_parent_turn_ended(tmp_path):
    """受控真机场景固化：父代理轮次已 done、子代理仍 OPEN，唤醒不得被当过期丢弃。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _source_child_awaits_parent_decision
    from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest

    child = SimpleNamespace(
        capability_requests=[CapabilityRequest(
            id="capreq-1", from_run_id="sub-1", problem="越界写入",
            needed_capability="write_file", status="OPEN",
        )],
        capability_gaps=[],
    )
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: child))
    signal = SimpleNamespace(source_agent_id="sub-1")
    assert _source_child_awaits_parent_decision(agent, signal) is True


def test_granted_capability_wake_still_follows_terminal_rule(tmp_path):
    """已批准/已关闭的申请不阻止原有过期判定，避免放宽唤醒。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _source_child_awaits_parent_decision
    from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest

    child = SimpleNamespace(
        capability_requests=[CapabilityRequest(
            id="capreq-1", from_run_id="sub-1", problem="越界写入",
            needed_capability="write_file", status="GRANTED",
        )],
        capability_gaps=[],
    )
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: child))
    assert _source_child_awaits_parent_decision(agent, SimpleNamespace(source_agent_id="sub-1")) is False


def test_unreadable_child_keeps_original_terminal_rule():
    """读不到子代理时不放宽：仍按原过期判定处理。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _source_child_awaits_parent_decision

    def _boom(run_id):
        raise FileNotFoundError(run_id)

    agent = SimpleNamespace(subagents=SimpleNamespace(load=_boom))
    assert _source_child_awaits_parent_decision(agent, SimpleNamespace(source_agent_id="sub-x")) is False
    assert _source_child_awaits_parent_decision(SimpleNamespace(), SimpleNamespace(source_agent_id="")) is False


def test_open_capability_gap_also_awaits_parent_decision():
    """真机实测：路由后 request 进入终态 GAP，真正等待父级的是留下的 OPEN gap。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _source_child_awaits_parent_decision
    from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest

    child = SimpleNamespace(
        capability_requests=[CapabilityRequest(
            id="capreq-1", from_run_id="sub-1", problem="越界写入",
            needed_capability="write_file", status="GAP",
        )],
        capability_gaps=[SimpleNamespace(status="OPEN")],
    )
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: child))
    assert _source_child_awaits_parent_decision(agent, SimpleNamespace(source_agent_id="sub-1")) is True


def test_closed_gap_does_not_hold_the_wake():
    """gap 已关闭时不再拦唤醒，保持原过期判定。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _source_child_awaits_parent_decision

    child = SimpleNamespace(capability_requests=[], capability_gaps=[SimpleNamespace(status="CLOSED")])
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: child))
    assert _source_child_awaits_parent_decision(agent, SimpleNamespace(source_agent_id="sub-1")) is False
