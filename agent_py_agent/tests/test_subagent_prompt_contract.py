from __future__ import annotations

from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt
from agent_py_agent.agent.subagents.models import SubAgentExecutionContext


def test_runner_prompt_tells_leaf_to_defer_command_execution_to_parent():
    """叶子没有命令工具时，应写测试文件并交给父级验收器执行。"""
    context = SubAgentExecutionContext(
        run_id="leaf-1",
        generated_at=1.0,
        goal="实现算法并生成 test_solution.py",
        thought="",
        plan=[],
        role="leaf_worker",
        allowed_tools=["write_file", "read_file"],
        acceptance_checks=["生成 test_solution.py 并建议 pytest 命令"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "没有 shell/command/terminal 工具" in prompt
    assert "不要因为不能自己运行 pytest 就提交 capability_request" in prompt
    assert "父级验收器" in prompt
    assert "不要写 cd ... &&" in prompt
    assert '"working_dir"' in prompt
    assert "逐条对照验收条件" in prompt
    assert "从 working_dir 运行能导入被测模块" in prompt


def test_runner_prompt_tells_coordinator_to_schedule_leaf_before_requesting_write_tool():
    """coordinator 没有写工具时，应先创建 leaf，而不是上抛 write_file 缺口。"""
    context = SubAgentExecutionContext(
        run_id="child-1",
        generated_at=1.0,
        goal="创建 leaf_worker_text 写入 solution.py、test_solution.py、README.md",
        thought="",
        plan=[],
        role="child_coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board", "read_file"],
        acceptance_checks=["leaf 必须写出三个文件"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "coordinator" in prompt
    assert "先使用 schedule_child_subagents 创建 leaf_worker" in prompt
    assert "不要因为自己没有 write_file 就提交 capability_request" in prompt
    assert "coordinator/lead 子节点不要授予 write_file" in prompt
    assert "原样传递父级指定的文件名" in prompt
    assert "mixed_coordinator_leaf_children" in prompt
    assert "同一次" in prompt
    assert "domain_mismatch" in prompt
    assert "可用角色模板" in prompt
    assert "bug_finder" in prompt
    assert "找茬子代理" in prompt
