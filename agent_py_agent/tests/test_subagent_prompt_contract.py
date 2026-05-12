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


# LLM: test_runner_prompt_tells_leaf_to_chunk_long_file_writes prevents repeated truncated tool calls.
# 函数用途: 长 CSS/JS/HTML 不能等工具解析失败后才提醒；runner 起步就要要求分块写。
def test_runner_prompt_tells_leaf_to_chunk_long_file_writes():
    context = SubAgentExecutionContext(
        run_id="leaf-css",
        generated_at=1.0,
        goal="写 style.css 和 app.js",
        thought="",
        plan=[],
        role="leaf_worker",
        allowed_tools=["write_file", "append_file"],
        acceptance_checks=["CSS/JS 必须存在"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "长 CSS/JS/HTML" in prompt
    assert "write_file 写短骨架" in prompt
    assert "append_file 分块追加" in prompt
    assert "1500-2000 字符" in prompt
    assert "不超过 800 字符" in prompt
    assert "每轮只输出 1 个写入工具调用" in prompt


def test_runner_prompt_tells_controlled_exec_leaf_to_apply_and_report_refs():
    context = SubAgentExecutionContext(
        run_id="leaf-exec",
        generated_at=1.0,
        goal="用 controlled_exec 执行 pwd、python3 大输出、rm sentinel.txt",
        thought="",
        plan=[],
        role="leaf_worker",
        allowed_tools=["controlled_exec", "write_file"],
        controlled_exec_grants=[
            {
                "grant_id": "grant-1",
                "command_allowlist": ["pwd", "python3"],
                "path_scope": ["/tmp/work"],
            }
        ],
        acceptance_checks=["必须报告 stdout_ref、audit_ref、trash_manifest_ref"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "dry_run/allowed plan 不算完成" in prompt
    assert "apply=true" in prompt
    assert "stdout_ref、audit_ref" in prompt
    assert "command=[\"python3\",\"-c\"" in prompt
    assert "delete_policy.mode=task_trash" in prompt
    assert "moved=true" in prompt
    assert "trash_manifest_ref" in prompt


def test_runner_prompt_tells_coordinator_to_write_reports_but_delegate_deliverables():
    """coordinator 可以写协调报告，但最终业务产物仍要派给 worker/writer。"""
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
    assert "可以在自己的 task_dir 写计划、证据和协调报告" in prompt
    assert "最终产物仍应优先交给 worker/writer" in prompt
    assert "上层权限应覆盖下层" in prompt
    assert "不要误以为只能创建 worker" in prompt
    assert "不要包成" in prompt
    assert "创建 worker/writer/leaf_worker" in prompt
    assert "不要替后代提前提交 capability_request" in prompt
    assert "由真正需要该能力的 runner 正式申请" in prompt
    assert "不要让 worker/writer 代写 coordinator 自己的协调证据" in prompt
    assert "原样传递父级指定的文件名" in prompt
    assert "mixed_coordinator_leaf_children" in prompt
    assert "同一次" in prompt
    assert "domain_mismatch" in prompt
    assert "可用角色模板" in prompt
    assert "bug_finder" in prompt
    assert "找茬子代理" in prompt
    assert "不要直接输出最终 SUBAGENT_RESULT" in prompt
    assert "quality_advice" in prompt
    assert "ready refs" in prompt
    assert "qa_repair_advice" in prompt
    assert "失败 QA refs" in prompt
    assert "模板详情" in prompt
    assert "你是找茬子代理" in prompt
    assert "subagent_message" in prompt
    assert "scope=descendants" in prompt
    assert "scope=peers" in prompt


# LLM: test_runner_prompt_tells_root_not_to_request_capability keeps root as the decision node.
# 函数用途: root 没有上级，提示词不能引导 root 提交 capability_request 卡住自己。
def test_runner_prompt_tells_root_not_to_request_capability():
    context = SubAgentExecutionContext(
        run_id="root-1",
        generated_at=1.0,
        goal="协调购物网站开发",
        thought="",
        plan=[],
        role="coordinator",
        depth=0,
        parent_id="",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "capability_request"],
        acceptance_checks=["由下级完成实现和 QA"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "root 不走 capability_request" in prompt
    assert "root 当前不应缺能力" in prompt
    assert "自毁" not in prompt
    assert "root policy" not in prompt


def test_runner_prompt_keeps_role_template_details_out_of_leaf_prompt():
    """非派工节点不用加载完整角色模板细节，避免每个 leaf prompt 变厚。"""
    context = SubAgentExecutionContext(
        run_id="leaf-compact",
        generated_at=1.0,
        goal="写一个 proof.txt",
        thought="",
        plan=[],
        role="leaf_worker",
        allowed_tools=["write_file", "read_file"],
        acceptance_checks=["proof.txt 必须存在"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "模板详情" not in prompt
    assert "你是找茬子代理" not in prompt


def test_runner_prompt_loads_current_role_template_for_worker():
    """执行型子代理应拿到自己的角色提示片段，但不加载其他角色全集。"""
    context = SubAgentExecutionContext(
        run_id="worker-1",
        generated_at=1.0,
        goal="实现一个小功能",
        thought="",
        plan=[],
        role="worker",
        allowed_tools=["write_file", "read_file"],
        acceptance_checks=["必须输出证据"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "当前角色模板详情" in prompt
    assert "你是执行子代理" in prompt
    assert "你是找茬子代理" not in prompt
