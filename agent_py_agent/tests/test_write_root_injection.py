from __future__ import annotations

"""WRITE-02 回归：主链任务无既有写根时注入任务 work/output 为沙箱写根（2026-08-15 真机）。

覆盖三象限：
- 主链任务（run_workspace 存在 + 无写根）→ 注入 [work_dir, output_dir]
- 既有写根（子代理窄授权）→ 不覆盖
- task_local/control_plane → 不注入（防授权放大）
"""

import types

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger


def _params_with_workspace(work: str, output: str, root: str, *, scope: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        task_attributes={
            "run_workspace": {
                "task_root": root,
                "work_dir": work,
                "output_dir": output,
            }
        },
        delivery_contract=None,
        write_boundary=None,
        context_scope=scope,
    )


def _agent() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        config=types.SimpleNamespace(my_agent_owner_provider="local"),
        home_paths=types.SimpleNamespace(owner_home_dir=""),
        tools=types.SimpleNamespace(owner_scope_root=""),
    )


# LLM: 主链任务无写根时必须注入任务 work/output（WRITE-02 核心），沙箱才有任务写能力。
# 函数用途: 验证 CLI run 场景 allowed_write_roots 被注入任务目录。
def test_main_task_injects_task_write_roots():
    boundary = write_boundary_with_runtime_ledger(
        _agent(),
        _params_with_workspace("/t/work", "/t/output", "/t", scope="main"),
    )
    roots = boundary["allowed_write_roots"]
    assert roots == ["/t/work", "/t/output"]


# LLM: 既有写根（子代理窄授权/远程任务墙）不能被覆盖——防授权放大。
# 函数用途: 验证已有 allowed_write_roots 保持不变。
def test_existing_write_roots_kept():
    params = _params_with_workspace("/t/work", "/t/output", "/t", scope="main")
    params.write_boundary = {"allowed_write_roots": ["/narrow"]}
    boundary = write_boundary_with_runtime_ledger(_agent(), params)
    assert boundary["allowed_write_roots"] == ["/narrow"]


# LLM: task_local/control_plane 不注入——子代理 run 的授权不得因主链任务根而放大。
# 函数用途: 验证子代理场景不被注入任务写根。
def test_subagent_scope_not_injected():
    boundary = write_boundary_with_runtime_ledger(
        _agent(),
        _params_with_workspace("/t/work", "/t/output", "/t", scope="task_local"),
    )
    assert "allowed_write_roots" not in boundary
