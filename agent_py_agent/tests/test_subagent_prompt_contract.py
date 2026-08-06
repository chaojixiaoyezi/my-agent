from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.agent_core.runner.prompts import _build_subagent_runner_prompt
from agent_py_agent.agent.subagents.models import SubAgentExecutionContext


def test_runner_prompt_tells_leaf_to_defer_command_execution_to_parent():
    """叶子没有命令工具时，应写测试文件并交给最终收口器执行。"""
    context = SubAgentExecutionContext(
        run_id="leaf-1",
        generated_at=1.0,
        goal="实现算法并生成 test_solution.py",
        thought="",
        plan=[],
        role="worker",
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


def test_runner_prompt_tells_leaf_to_chunk_long_file_writes():
    context = SubAgentExecutionContext(
        run_id="leaf-css",
        generated_at=1.0,
        goal="写 style.css 和 app.js",
        thought="",
        plan=[],
        role="worker",
        allowed_tools=["write_file", "apply_patch"],
        acceptance_checks=["CSS/JS 必须存在"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "长 CSS/JS/HTML" in prompt
    assert 'mode="overwrite"' in prompt
    assert 'mode="append"' in prompt
    assert "WRITE_FILE_RAW" not in prompt
    assert "apply_patch" in prompt
    assert "data_base64" in prompt


def test_runner_prompt_distinguishes_owner_workspace_from_task_output(
    tmp_path: Path,
):
    owner_workspace = tmp_path / "owners" / "user-a" / "workspace"
    task_root = tmp_path / "owners" / "user-a" / "tasks" / "task-a"
    context = SubAgentExecutionContext(
        run_id="leaf-workspace",
        generated_at=1.0,
        goal="审计 workspace/input/reference_repos/project-a",
        thought="",
        plan=[],
        role="worker",
        task_dir=str(task_root / "work" / "agents" / "leaf-workspace"),
        allowed_tools=["list_files", "read_file", "write_file"],
        context_bundle={
            "workspace_refs": {
                "owner_workspace_dir": str(owner_workspace),
                "task_root": str(task_root),
                "task_work_dir": str(task_root / "work"),
                "task_output_dir": str(task_root / "output"),
            }
        },
    )

    prompt = _build_subagent_runner_prompt(context)

    assert f"Primary working directory（长期项目/输入资料）: {owner_workspace}" in prompt
    assert f"Current task root（本任务 work/output）: {task_root}" in prompt
    assert "不要把 owner workspace 拼到 task root 下面" in prompt
    assert f'"owner_workspace_dir": "{owner_workspace}"' in prompt


def test_runner_prompt_tells_controlled_exec_leaf_to_apply_and_report_refs():
    context = SubAgentExecutionContext(
        run_id="leaf-exec",
        generated_at=1.0,
        goal="用 controlled_exec 执行 pwd、python3 大输出、rm sentinel.txt",
        thought="",
        plan=[],
        role="worker",
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
        goal="创建 worker_text 写入 solution.py、test_solution.py、README.md",
        thought="",
        plan=[],
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree", "read_file"],
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
    assert "不要包二级参数对象" in prompt
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
    assert "send_guidance" in prompt
    assert "scope=descendants" not in prompt
    assert "scope=peers" not in prompt


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
        role="worker",
        allowed_tools=["write_file", "read_file"],
        acceptance_checks=["proof.txt 必须存在"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "模板详情" not in prompt
    assert "你是找茬子代理" not in prompt


def test_runner_prompt_keeps_worker_template_details_compact():
    """执行型子代理不额外加载当前模板详情，避免每个 worker prompt 变厚。"""
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

    assert "当前角色模板详情" not in prompt
    assert "你是执行子代理" not in prompt
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
            "raise_collaboration",
            "raise_collaboration",
            "submit_collaboration_result",
            "update_collaboration",
            "update_collaboration",
            "inspect_collaboration",
        ],
        acceptance_checks=["有协作需要时留下 case/request/evidence 引用"],
    )

    prompt = _build_subagent_runner_prompt(context)

    assert "协作控制面" in prompt
    assert "raise_collaboration" in prompt
    assert "submit_collaboration_result" in prompt
    assert "update_collaboration" in prompt
    assert "inspect_collaboration" in prompt
    assert "优先用这个单步工具打开 case 并发出 request" in prompt
    assert "不要因为缺 case_id 就新开重复 case" in prompt
    assert "query_hints 是软提示" in prompt
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
            "raise_collaboration",
            "inspect_collaboration",
            "submit_collaboration_result",
            "update_collaboration",
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
                            "inspect_collaboration",
                            "submit_collaboration_result",
                            "update_collaboration",
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
    assert "不要另开 raise_collaboration" in prompt
    assert "inspect_collaboration -> submit_collaboration_result -> update_collaboration" in prompt


def test_runner_prompt_uses_normal_context_summary_for_task_local_recovery(tmp_path: Path):
    context = _task_local_recovery_context(tmp_path)

    prompt = _build_subagent_runner_prompt(context)

    assert '"context_scope": "task_local"' in prompt
    assert '"writes_main_memory": false' in prompt
    assert "checkpoint.json" in prompt
    assert "continue checkout tests" in prompt
    assert "继续补齐 checkout tests" in prompt
    assert "已完成条目列表" in prompt
    assert "SOUL.md" not in prompt
    assert "USER.md" not in prompt


def _task_local_recovery_context(tmp_path: Path) -> SubAgentExecutionContext:
    run_workspace = _prepare_task_local_recovery_workspace(tmp_path)
    context = SubAgentExecutionContext(
        run_id="leaf-recovery",
        generated_at=1.0,
        goal="继续示例网站子任务",
        thought="",
        plan=["从 checkpoint 接续"],
        role="worker",
        task_dir=str(tmp_path / "subagents" / "leaf-recovery"),
        context_bundle={
            "gate": {"ok": True, "missing_fields": []},
            "workspace_refs": {
                "agent_work_dir": str(run_workspace),
                "agent_run_task": str(run_workspace / "task.md"),
                "agent_run_checkpoint": str(run_workspace / "checkpoint.json"),
                "agent_run_summary": str(run_workspace / "summary.md"),
                "agent_run_final_report": str(run_workspace / "final_report.md"),
                "agent_run_findings": str(run_workspace / "findings.jsonl"),
                "agent_run_timeline": str(run_workspace / "timeline.jsonl"),
            },
            "runner_recovery_preflight": {
                "context_scope": "task_local",
                "writes_main_memory": False,
                "recovery_refs": [
                    str(run_workspace / "checkpoint.json"),
                    str(run_workspace / "summary.md"),
                ],
                "runner_instruction": "continue checkout tests",
                "current_step": "继续补齐 checkout tests",
                "latest_summary": "已完成条目列表",
            },
        },
    )
    return context


def _prepare_task_local_recovery_workspace(tmp_path: Path) -> Path:
    run_workspace = tmp_path / "tasks" / "root-1" / "work" / "agents" / "leaf-recovery"
    run_workspace.mkdir(parents=True)
    (run_workspace / "task.md").write_text("实现流程状态结算按钮\n", encoding="utf-8")
    (run_workspace / "checkpoint.json").write_text(
        json.dumps({"current_step": "继续补齐 checkout tests", "next_action": "write tests"}),
        encoding="utf-8",
    )
    (run_workspace / "summary.md").write_text("已完成条目列表，剩余流程状态验收。\n", encoding="utf-8")
    (run_workspace / "final_report.md").write_text("还没有最终验收。\n", encoding="utf-8")
    (run_workspace / "findings.jsonl").write_text('{"claim":"cart missing tests"}\n', encoding="utf-8")
    (run_workspace / "timeline.jsonl").write_text('{"event":"checkpoint_written"}\n', encoding="utf-8")
    return run_workspace
