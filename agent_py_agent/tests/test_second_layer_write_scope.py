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
