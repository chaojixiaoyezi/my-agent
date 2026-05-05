from __future__ import annotations

"""LLM: implements parent-subagent cross-day resume scenario and shared recovery helpers for subagent tasks.

给人看的解释：
这个文件包含子代理跨天恢复场景测试和共用的恢复辅助函数。
验证子代理执行结果能被 memory-resume 命令正确找回。
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ...agent.backend import ModelResponse
from ...agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    append_snapshot,
)
from ..scenario_utils import (
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_step,
    run_scenario_subprocess,
    scenario_command,
    write_scenario_summary,
)


@dataclass
class SubagentRunResults:
    """Bundle of subagent runner results for _verify_subagent_resume."""
    runner: object
    loaded: object
    backend: object


@dataclass
class ResumeCommandResults:
    """Bundle of resume command results for _verify_subagent_resume."""
    payload: object
    returncode: int


class ScenarioParentSubagentRecoveryBackend:
    """LLM: stub backend that produces a tool call on first generate, then a structured SUBAGENT_RESULT on second.

    新手说明:
    场景测试用后端——第一次 generate 输出工具调用（读 README），
    第二次 generate 输出可恢复的结构化结果（AWAITING_ACCEPTANCE）。
    """

    name = "scenario_parent_subagent_recovery_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool": "read_file", "path": "README.md"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "parent/subagent 跨天恢复演练：runner 已读取 README，等待父级验收。",\n'
                '  "used_tools": ["read_file"],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "read_file", "summary": "README.md 已通过 read_file 读取", "path": "README.md", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "read_file README.md", "command": "read_file README.md", "ok": true, "summary": "runner 工具回合成功"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": ["跨天恢复必须回到 task fact sources，而不是只相信 archive 摘要"],\n'
                '  "next_actions": ["父级恢复后读取 STATUS/WORK_LOG/HANDOFF/output.json，再决定是否验收"],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def _write_parent_subagent_recovery_fact_files(task) -> None:
    """LLM: keep scenario task fact files aligned with the runner result state.

    新手说明:
    把子代理任务的 STATUS、HANDOFF、TEST_CHECKLIST 文件写好，
    确保 memory-resume 恢复时能看到一致的任务状态。
    """

    Path(task.status_file).write_text(
        "# STATUS\n\n"
        f"- id: {task.id}\n"
        f"- status: {task.status}\n"
        f"- verification_status: {task.verification_status}\n"
        "- next: 父级恢复后读取 output.json / RUNNER_RESULT.md，再决定是否验收。\n",
        encoding="utf-8",
    )
    Path(task.handoff_file).write_text(
        "# HANDOFF\n\n"
        "## Current State\n\n"
        "- runner 已执行真实模型/工具循环并写回结构化结果。\n"
        f"- status: {task.status}\n"
        f"- verification_status: {task.verification_status}\n\n"
        "## Done\n\n"
        "- README.md 已通过 read_file 读取。\n"
        "- output.json 和 reports/runner_result.json 已落盘。\n\n"
        "## Not Done\n\n"
        "- 父级验收尚未执行。\n\n"
        "## Next Step\n\n"
        "- 恢复后先读 STATUS、WORK_LOG、HANDOFF、TEST_CHECKLIST 和 output.json，再决定是否运行 acceptance。\n",
        encoding="utf-8",
    )
    Path(task.test_checklist_file).write_text(
        "# TEST_CHECKLIST\n\n"
        "- [x] runner 调用 read_file README.md\n"
        "- [x] runner 输出 AWAITING_ACCEPTANCE 结构化结果\n"
        "- [ ] 父级恢复后执行验收\n",
        encoding="utf-8",
    )


def _append_parent_subagent_cross_day_resume_clues(root: Path, task) -> None:
    """LLM: write deterministic cross-day archive clues for one real subagent runner result.

    新手说明:
    向归档系统写入模拟的跨天线索，让 memory-resume 命令能找到这个子代理任务的上下文。
    """

    append_raw_event(
        root,
        RawMemoryEvent(
            event_id=f"raw-scenario-parent-subagent-cross-day-{task.id}",
            session_id="session-scenario-parent-subagent-cross-day",
            request_id=f"subagent-run:{task.id}",
            run_id=task.id,
            task_id=task.id,
            speaker="assistant",
            target="parent",
            action="subagent_runner_result",
            status=str(task.status).lower(),
            content_preview="parent subagent cross-day resume: runner finished and waits for parent acceptance",
            source="subagent_run",
            created_at="2026-04-29T23:55:00+00:00",
        ),
    )
    _append_subagent_snapshot(root, task)


def _append_subagent_snapshot(root: Path, task) -> None:
    """Append a recovery snapshot for the subagent task."""
    append_snapshot(
        root,
        CompressionSnapshot(
            snapshot_id=f"snapshot-scenario-parent-subagent-cross-day-{task.id}",
            session_id="session-scenario-parent-subagent-cross-day",
            compression_id=f"compression-scenario-parent-subagent-cross-day-{task.id}",
            turn_range={
                "kind": "recovery_snapshot",
                "source": "subagent_run",
                "request_id": f"subagent-run:{task.id}",
                "run_id": task.id,
                "task_id": task.id,
            },
            user_intents=["parent subagent cross-day resume: continue yesterday's runner task"],
            assistant_actions=["runner result is on disk; read task fact sources before acceptance"],
            dispatch_events=[
                {
                    "source": "subagent_run",
                    "request_id": f"subagent-run:{task.id}",
                    "run_id": task.id,
                    "task_id": task.id,
                    "status": str(task.status).lower(),
                }
            ],
            task_refs=[task.id],
            content_paths=[
                task.status_file, task.work_log_file, task.handoff_file,
                task.test_checklist_file, task.runner_result_file,
                task.runner_result_json, task.output_json,
            ],
            next_actions=["Read task fact sources, then run parent acceptance only after evidence is checked."],
            created_at="2026-04-30T00:07:00+00:00",
        ),
    )


def _parent_subagent_setup(args):
    """Setup for parent subagent cross-day resume: create workspace, agent, task, and run subagent."""
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=parent-subagent-cross-day-resume")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    backend = ScenarioParentSubagentRecoveryBackend()
    agent.backend = backend

    print_scenario_step(1, "Create a parent-owned subagent task")
    task = agent.subagents.create_run(
        goal="parent/subagent 跨天恢复演练：读取 README 后等待父级恢复验收",
        thought="验证真实 runner 写回后，隔天恢复必须回到子代理任务事实源。",
        plan=["runner 读取 README.md", "写回结构化结果", "模拟跨天恢复", "memory-resume 找回任务事实源"],
        allowed_tools=["read_file"],
        acceptance_checks=["必须有 read_file 证据", "恢复时必须推荐任务事实源", "父级恢复后再决定是否验收"],
    )
    print(f"run_id={task.id}")

    print_scenario_step(2, "Run the real subagent runner path and stop before acceptance")
    runner = agent.run_subagent(
        task.id,
        dry_run=False,
        probe=False,
        instruction=(
            "这是 parent/subagent 跨天恢复演练。先读 README.md，再输出 AWAITING_ACCEPTANCE 的 "
            "[SUBAGENT_RESULT]，不要自行标记 DONE。"
        ),
    )
    loaded = agent.subagents.load(task.id)
    _write_parent_subagent_recovery_fact_files(loaded)
    print(
        f"runner_ok={runner.ok} status={loaded.status} verify={loaded.verification_status} "
        f"tool_rounds={runner.tool_rounds} backend_calls={backend.calls}"
    )
    return paths, agent, backend, task, loaded, runner


def _run_subagent_resume(paths, task):
    """Run memory-resume subprocess for subagent cross-day recovery. Returns parsed payload."""
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    resume = run_scenario_subprocess(
        scenario_command(
            paths, "memory-resume", "parent subagent cross-day resume",
            "--run-id", task.id,
            "--since", "2026-04-29", "--until", "2026-04-30", "--json",
        ),
        env=env, timeout=30,
    )
    try:
        return json.loads(resume.stdout), resume.returncode
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"memory-resume JSON parse failed: {exc}", "stdout": resume.stdout}, resume.returncode


def _verify_subagent_resume(run: SubagentRunResults, resume: ResumeCommandResults, task, expected_reads):
    """Verify subagent cross-day resume found task fact sources."""
    task_sources = resume.payload.get("task_fact_sources", []) if isinstance(resume.payload, dict) else []
    recommended_reads = resume.payload.get("resume", {}).get("recommended_read_paths", []) if isinstance(resume.payload, dict) else []
    context_block = resume.payload.get("brief", {}).get("context_block", "") if isinstance(resume.payload, dict) else ""
    archive_count = resume.payload.get("resume", {}).get("archive_match_count", 0) if isinstance(resume.payload, dict) else 0
    matching_task = next(
        (item for item in task_sources if isinstance(item, dict) and item.get("run_id") == task.id), {},
    )
    return (
        run.runner.ok
        and run.loaded.status == "AWAITING_ACCEPTANCE"
        and run.loaded.verification_status == "NEEDS_ACCEPTANCE"
        and "read_file" in run.loaded.used_tools
        and run.runner.tool_rounds == 1
        and run.backend.calls == 2
        and resume.returncode == 0
        and archive_count >= 2
        and matching_task.get("exists") is True
        and matching_task.get("status") == "AWAITING_ACCEPTANCE"
        and all(path in recommended_reads for path in expected_reads)
        and task.id in context_block
        and "AWAITING_ACCEPTANCE" in context_block
    )


def run_scenario_parent_subagent_cross_day_resume_case(args) -> int:
    """LLM: run a real subagent runner turn, then prove memory-resume returns task fact sources.

    新手说明:
    让子代理真正执行一轮（读 README），然后模拟跨天恢复。
    验证 memory-resume 命令能找回子代理的任务事实源文件路径。
    """

    paths, agent, backend, task, loaded, runner = _parent_subagent_setup(args)

    print_scenario_step(3, "Simulate cross-day archive clues for a resumed parent session")
    _append_parent_subagent_cross_day_resume_clues(agent.root, loaded)
    reloaded_agent = load_scenario_agent(paths.config)
    reloaded_task = reloaded_agent.subagents.load(task.id)
    print(f"reloaded_status={reloaded_task.status} task_dir={reloaded_task.task_dir}")

    print_scenario_step(4, "Run memory-resume and require task fact-source reads")
    resume_payload, returncode = _run_subagent_resume(paths, task)

    expected_reads = [
        loaded.status_file, loaded.work_log_file, loaded.handoff_file,
        loaded.test_checklist_file, loaded.output_json,
    ]
    final_ok = _verify_subagent_resume(
        SubagentRunResults(runner=runner, loaded=loaded, backend=backend),
        ResumeCommandResults(payload=resume_payload, returncode=returncode),
        task, expected_reads,
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="parent/subagent cross-day resume passed" if final_ok else "parent/subagent cross-day resume failed",
        extra={
            "case": "parent-subagent-cross-day-resume",
            "run_id": task.id,
            "runner": {
                "ok": runner.ok,
                "status": runner.status,
                "verification_status": runner.verification_status,
                "tool_rounds": runner.tool_rounds,
                "backend_calls": backend.calls,
            },
            "resume": resume_payload,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2
