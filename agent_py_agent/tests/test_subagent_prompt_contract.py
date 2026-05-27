from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.agent_core.runner_prompts import _build_subagent_runner_prompt
from agent_py_agent.agent.subagents.models import SubAgentExecutionContext


def test_runner_prompt_tells_leaf_to_defer_command_execution_to_parent():
    """叶子没有命令工具时，应写测试文件并交给最终收口器执行。"""
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
    assert "最终收口认可" in prompt
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
        allowed_tools=["write_file", "apply_patch"],
        acceptance_checks=["CSS/JS 必须存在"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "长 CSS/JS/HTML" in prompt
    assert "WRITE_FILE_RAW" in prompt
    assert "apply_patch" in prompt
    assert "data_base64" in prompt


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


def test_runner_prompt_tells_coordinator_to_stay_capable_and_delegate_when_useful():
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
    assert "拥有完整基础读写能力" in prompt
    assert "先读取最小必要材料" in prompt
    assert "不要在派工前把所有正文" in prompt
    assert "先创建并 dispatch child" in prompt
    assert "不要把所有 child 正文一次性吞回自己的上下文" in prompt
    assert "派工是为了把活做好，不是硬流程" in prompt
    assert "你可以直接完成" in prompt
    assert "不要误以为只能创建 worker" in prompt
    assert "不要包成" in prompt
    assert "创建 child 时" in prompt
    assert "不要替后代提前提交 capability_request" in prompt
    assert "由真正需要该能力的 runner 正式申请" in prompt
    assert "不要让 worker/writer 代写 coordinator 自己的协调证据" in prompt
    assert "原样传递父级指定的文件名" in prompt
    assert "可以混建" in prompt
    assert "scheduling_warnings" not in prompt
    assert "domain_mismatch" not in prompt
    assert "forbidden_child_scope" not in prompt
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
        goal="协调示例网站开发",
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


def test_runner_prompt_exposes_generic_collaboration_control_plane_when_tools_are_allowed():
    """协作工具已授权时，真实子代理要看到通用控制面动作，而不是只靠自然语言猜。"""
    context = SubAgentExecutionContext(
        run_id="worker-collab",
        generated_at=1.0,
        goal="观察本地线索，需要其他代理补充证据时发起协作。",
        thought="",
        plan=[],
        role="worker",
        allowed_tools=[
            "open_case",
            "request_collaboration",
            "submit_evidence",
            "update_collaboration_request",
            "reroute_collaboration_request",
            "case_status",
        ],
        acceptance_checks=["有协作需要时留下 case/request/evidence 引用"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "协作控制面" in prompt
    assert "open_case" in prompt
    assert "request_collaboration" in prompt
    assert "submit_evidence" in prompt
    assert "update_collaboration_request" in prompt
    assert "reroute_collaboration_request" in prompt
    assert "case_status" in prompt
    assert "open_case 后" in prompt
    assert "建议继续调用 request_collaboration" in prompt
    assert "只记录事件时可以停在 open_case" in prompt
    assert "collaboration://case/" in prompt
    assert "collaboration://request/" in prompt
    assert "不要只在 summary 里说已经协作" in prompt


def test_runner_prompt_tells_targeted_responder_to_reuse_existing_collaboration_request():
    """已有请求点名当前 runner 时，优先响应 request，不应再开新 case。"""
    context = SubAgentExecutionContext(
        run_id="agent-b",
        generated_at=1.0,
        goal="补充协作证据。",
        thought="",
        plan=[],
        role="worker",
        allowed_tools=[
            "open_case",
            "case_status",
            "submit_evidence",
            "update_collaboration_request",
        ],
        context_bundle={
            "collaboration": {
                "targeted_requests": [
                    {
                        "case_id": "case-123",
                        "request_id": "creq-456",
                        "case_ref": "collaboration://case/case-123",
                        "request_ref": "collaboration://request/creq-456",
                        "question": "请补一条证据引用。",
                        "recommended_tools": [
                            "case_status",
                            "submit_evidence",
                            "update_collaboration_request",
                        ],
                    }
                ]
            }
        },
        acceptance_checks=["提交证据并更新请求状态"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "点名给你的协作请求" in prompt
    assert "case-123" in prompt
    assert "creq-456" in prompt
    assert "优先复用已有 case/request" in prompt
    assert "不要另开 open_case" in prompt
    assert "case_status -> submit_evidence -> update_collaboration_request" in prompt


# LLM: compacted subagents must resume from their task-local run workspace, not parent long-term memory.
# 函数用途: 验证子代理 runner prompt 会引用本地 checkpoint、summary 和 latest continue packet，避免压缩后丢失任务状态。
def test_runner_prompt_includes_task_local_compact_continuation_refs(tmp_path: Path):
    context = _compact_continuation_context(tmp_path)

    prompt = _build_subagent_runner_prompt(context)

    assert "Task-Local Compact Continuation" in prompt
    assert "memory_scope: task_local" in prompt
    assert "writes_main_memory: false" in prompt
    assert "latest_continue_packet.json" in prompt
    assert "continue checkout tests" in prompt
    assert "继续补齐 checkout tests" in prompt
    assert "已完成条目列表" in prompt
    assert "SOUL.md" not in prompt
    assert "USER.md" not in prompt


# LLM: _compact_continuation_context builds realistic task-local refs without bloating the assertion test.
# 函数用途: 准备带 compact packet、checkpoint 和 summary 的子代理执行上下文，复用标准 workspace_refs 形状。
def _compact_continuation_context(tmp_path: Path) -> SubAgentExecutionContext:
    run_workspace, compactions = _prepare_compact_continuation_workspace(tmp_path)
    context = SubAgentExecutionContext(
        run_id="leaf-compact",
        generated_at=1.0,
        goal="继续示例网站子任务",
        thought="",
        plan=["从 checkpoint 接续"],
        role="leaf_worker",
        task_dir=str(tmp_path / "subagents" / "leaf-compact"),
        context_bundle={
            "gate": {"ok": True, "missing_fields": []},
            "workspace_refs": {
                "agent_run_workspace": str(run_workspace),
                "agent_run_compactions": str(compactions),
                "agent_run_task": str(run_workspace / "task.md"),
                "agent_run_checkpoint": str(run_workspace / "checkpoint.json"),
                "agent_run_summary": str(run_workspace / "summary.md"),
                "agent_run_final_report": str(run_workspace / "final_report.md"),
                "agent_run_findings": str(run_workspace / "findings.jsonl"),
                "agent_run_timeline": str(run_workspace / "timeline.jsonl"),
            },
        },
    )
    return context


# LLM: _prepare_compact_continuation_workspace keeps the prompt contract test data realistic but local.
# 函数用途: 创建子代理 run workspace、checkpoint、summary 和 latest_continue_packet 测试文件。
def _prepare_compact_continuation_workspace(tmp_path: Path) -> tuple[Path, Path]:
    run_workspace = tmp_path / "tasks" / "root-1" / "agents" / "leaf-compact"
    compactions = run_workspace / "compactions"
    session_compactions = compactions / "session"
    session_compactions.mkdir(parents=True)
    (run_workspace / "task.md").write_text("实现流程状态结算按钮\n", encoding="utf-8")
    (run_workspace / "checkpoint.json").write_text(
        json.dumps({"current_step": "继续补齐 checkout tests", "next_action": "write tests"}),
        encoding="utf-8",
    )
    (run_workspace / "summary.md").write_text("已完成条目列表，剩余流程状态验收。\n", encoding="utf-8")
    (run_workspace / "final_report.md").write_text("还没有最终验收。\n", encoding="utf-8")
    (run_workspace / "findings.jsonl").write_text('{"claim":"cart missing tests"}\n', encoding="utf-8")
    (run_workspace / "timeline.jsonl").write_text('{"event":"checkpoint_written"}\n', encoding="utf-8")
    _write_latest_continue_packet(session_compactions, run_workspace)
    return run_workspace, compactions


# LLM: _write_latest_continue_packet isolates the packet fixture shape from workspace setup.
# 函数用途: 写入最小 task-local continue packet，供 prompt 续接测试读取。
def _write_latest_continue_packet(session_compactions: Path, run_workspace: Path) -> None:
    (session_compactions / "latest_continue_packet.json").write_text(
        json.dumps(
            {
                "ready_to_continue": True,
                "continue_mode": "subagent_task_local",
                "next_action": "continue checkout tests",
                "recommended_read_paths": [str(run_workspace / "checkpoint.json")],
            }
        ),
        encoding="utf-8",
    )
