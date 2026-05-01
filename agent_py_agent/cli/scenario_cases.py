from __future__ import annotations

"""LLM: implements focused scenario-test cases for verification guards, gateway recovery, structured repair, and runner retry.

给人看的解释：
这里每个函数都是一个“以前容易踩坑”的端到端小剧本。
它们不负责创建通用 fixture，只负责构造极端情况，然后判断系统有没有守住边界。
"""

import json
import os
import sys
import time
from pathlib import Path

from ..agent.backend import ModelResponse
from ..agent.capability_config import load_capability_config
from ..agent.gateway import (
    gateway_paths,
    gateway_response_path,
    new_gateway_request_id,
    requeue_gateway_processing_requests,
    write_json_file,
)
from ..agent.memory_archive import CompressionSnapshot, RawMemoryEvent, append_raw_event, append_snapshot
from ..agent.subagent import VerificationEvidence
from .common import make_capability_router
from .scenario_utils import (
    create_scenario_workspace,
    load_scenario_agent,
    print_scenario_step,
    run_scenario_gateway_ask,
    run_scenario_subprocess,
    scenario_command,
    write_scenario_summary,
)


def run_scenario_verification_case(args) -> int:
    """验证父代理不会接受伪造 artifact / 自称完成。"""

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=verification")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    print_scenario_step(1, "构造伪造完成的子代理记录")
    task = agent.subagents.create_run(
        goal="极端场景：runner 声称写了 artifact，但文件实际不存在",
        thought="验证父代理验收不能只相信模型自称。",
        plan=["伪造 output.json", "触发验收", "确认验收拒绝"],
        allowed_tools=["read_file", "write_file"],
        acceptance_checks=["必须有 read_file 证据", "必须有 write_file 证据", "artifact 文件必须真实存在"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    task.used_tools = ["read_file", "write_file"]
    task.evidence.append(
        VerificationEvidence(
            kind="file_read",
            summary="伪造证据：声称 read_file 成功",
            path="README.md",
            ok=True,
            created_at=time.time(),
        )
    )
    task.evidence.append(
        VerificationEvidence(
            kind="file_write",
            summary="伪造证据：声称 write_file 写入 scenario_outputs/forged.md",
            path="scenario_outputs/forged.md",
            ok=True,
            created_at=time.time(),
        )
    )
    agent.subagents.save(task)
    Path(task.output_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "status": "AWAITING_ACCEPTANCE",
                "artifacts": [
                    {
                        "path": "scenario_outputs/forged.md",
                        "kind": "report",
                        "summary": "这个文件被故意留空不存在，用来测试验收防作弊。",
                    }
                ],
                "tests": [{"name": "fake-test", "command": "echo ok", "ok": True}],
                "patches": [],
                "blockers": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    Path(task.runner_result_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "structured_output_found": True,
                "structured_output_ok": True,
                "structured_parse_error": "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print_scenario_step(2, "执行父代理验收")
    report = agent.subagents.write_acceptance_review_report(
        run_ids=[task.id],
        apply=True,
        reviewer="scenario-verification",
        note="forged artifact must be rejected",
    )
    loaded = agent.subagents.load(task.id)
    for record in report.records:
        print(
            f"- decision={record.decision} ok={record.ok} applied={record.applied} "
            f"{record.before_status}/{record.before_verification_status}->"
            f"{record.after_status}/{record.after_verification_status}"
        )
        for finding in record.findings:
            if not finding.ok:
                print(f"  [finding:{finding.severity}] {finding.name}: {finding.message}")

    final_ok = (
        report.records
        and report.records[0].decision == "REJECT"
        and not report.records[0].ok
        and loaded.status == "BLOCKED"
        and loaded.verification_status == "FAILED"
        and any(
            item.name == "artifact_paths_exist" and not item.ok
            for item in report.records[0].findings
        )
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="verification guard passed" if final_ok else "verification guard failed",
        extra={
            "case": "verification",
            "run_id": task.id,
            "acceptance_report": str(agent.subagents.workspace / "subagent_acceptance_report.json"),
            "acceptance_md": str(agent.subagents.workspace / "SUBAGENT_ACCEPTANCE.md"),
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_restart_case(args) -> int:
    """验证 gateway 启动时会恢复遗留 processing 请求。"""

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-restart")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    gpaths = gateway_paths(agent)
    for path in (gpaths.inbox, gpaths.processing, gpaths.done, gpaths.failed, gpaths.responses):
        path.mkdir(parents=True, exist_ok=True)

    request_id = new_gateway_request_id()
    processing_path = gpaths.processing / f"{request_id}.json"
    payload = {
        "id": request_id,
        "kind": "ask",
        "prompt": "这个请求模拟 gateway 崩溃时卡在 processing。",
        "inject": [],
        "prompt_files": [],
        "save": False,
        "include_prompt": False,
        "created_at": time.time(),
        "client_pid": os.getpid(),
    }
    write_json_file(processing_path, payload)

    print_scenario_step(1, "模拟旧 gateway 崩溃遗留 processing 请求")
    print(f"processing_before={processing_path.exists()} path={processing_path}")
    requeued = requeue_gateway_processing_requests(gpaths)
    pending_path = gpaths.inbox / processing_path.name
    print_scenario_step(2, "执行 gateway 启动恢复步骤")
    print(f"requeued={requeued}")
    print(f"processing_after={processing_path.exists()}")
    print(f"pending_after={pending_path.exists()} path={pending_path}")

    final_ok = requeued == 1 and not processing_path.exists() and pending_path.exists()
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway restart requeue passed" if final_ok else "gateway restart requeue failed",
        extra={
            "case": "gateway-restart",
            "request_id": request_id,
            "pending_path": str(pending_path),
            "processing_path": str(processing_path),
            "requeued": requeued,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_gateway_cross_day_resume_case(args) -> int:
    """Run a real gateway ask, then verify cross-day memory-resume can recover its JSON facts."""

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=gateway-cross-day-resume")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    prompt = "gateway cross-day resume drill: please return a short recoverable gateway response."
    print_scenario_step(1, "Run a real background gateway ask")
    gateway_payload = run_scenario_gateway_ask(paths, prompt, timeout=args.timeout, save=True)
    if not gateway_payload.get("ok"):
        write_scenario_summary(
            paths,
            ok=False,
            reason="gateway ask failed",
            extra={"case": "gateway-cross-day-resume", "gateway": gateway_payload},
        )
        return 2
    request_id = str(gateway_payload.get("id") or "")
    agent = load_scenario_agent(paths.config)
    gpaths = gateway_paths(agent)
    request_path = gpaths.done / f"{request_id}.json"
    if not request_path.exists():
        request_path = gpaths.failed / f"{request_id}.json"
    response_path = gateway_response_path(gpaths, request_id)
    print(f"request_id={request_id}")
    print(f"request_path={request_path} exists={request_path.exists()}")
    print(f"response_path={response_path} exists={response_path.exists()}")

    print_scenario_step(2, "Simulate a previous-day clue and next-day recovery snapshot")
    _append_gateway_cross_day_resume_clues(
        agent.root,
        request_id=request_id,
        request_path=request_path,
        response_path=response_path,
    )

    print_scenario_step(3, "Run memory-resume against the real gateway request id")
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    resume = run_scenario_subprocess(
        scenario_command(
            paths,
            "memory-resume",
            "gateway cross-day resume",
            "--request-id",
            request_id,
            "--since",
            "2026-04-29",
            "--until",
            "2026-04-30",
            "--json",
        ),
        env=env,
        timeout=30,
    )
    try:
        resume_payload = json.loads(resume.stdout)
    except json.JSONDecodeError as exc:
        resume_payload = {"ok": False, "error": f"memory-resume JSON parse failed: {exc}", "stdout": resume.stdout}

    gateway_sources = resume_payload.get("gateway_fact_sources", []) if isinstance(resume_payload, dict) else []
    recommended_reads = resume_payload.get("resume", {}).get("recommended_read_paths", []) if isinstance(resume_payload, dict) else []
    context_block = resume_payload.get("brief", {}).get("context_block", "") if isinstance(resume_payload, dict) else ""
    final_ok = (
        resume.returncode == 0
        and bool(request_id)
        and request_path.exists()
        and response_path.exists()
        and any(item.get("request_id") == request_id for item in gateway_sources if isinstance(item, dict))
        and str(request_path) in recommended_reads
        and str(response_path) in recommended_reads
        and str(response_path) in context_block
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="gateway cross-day resume passed" if final_ok else "gateway cross-day resume failed",
        extra={
            "case": "gateway-cross-day-resume",
            "request_id": request_id,
            "request_path": str(request_path),
            "response_path": str(response_path),
            "gateway": gateway_payload,
            "resume": resume_payload,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def _append_gateway_cross_day_resume_clues(
    root: Path,
    *,
    request_id: str,
    request_path: Path,
    response_path: Path,
) -> None:
    """Write deterministic cross-day archive clues for one real gateway request."""

    append_raw_event(
        root,
        RawMemoryEvent(
            event_id=f"raw-scenario-gateway-cross-day-{request_id}",
            session_id="session-scenario-gateway-cross-day",
            request_id=request_id,
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            content_preview="gateway cross-day resume drill from the previous day",
            source="gateway",
            created_at="2026-04-29T23:58:00+00:00",
        ),
    )
    append_snapshot(
        root,
        CompressionSnapshot(
            snapshot_id=f"snapshot-scenario-gateway-cross-day-{request_id}",
            session_id="session-scenario-gateway-cross-day",
            compression_id=f"compression-scenario-gateway-cross-day-{request_id}",
            turn_range={
                "kind": "recovery_snapshot",
                "source": "gateway",
                "request_id": request_id,
            },
            user_intents=["continue the previous gateway request after a day boundary"],
            assistant_actions=["gateway response is available; read request and response JSON before continuing"],
            dispatch_events=[
                {
                    "source": "gateway",
                    "request_id": request_id,
                    "status": "done",
                }
            ],
            content_paths=[str(request_path), str(response_path)],
            next_actions=["Read the gateway request JSON and response JSON before taking action."],
            created_at="2026-04-30T00:05:00+00:00",
        ),
    )


class ScenarioParentSubagentRecoveryBackend:
    """场景测试用后端：先读 README，再输出可恢复的 runner 结构化结果。"""

    name = "scenario_parent_subagent_recovery_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
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


def run_scenario_parent_subagent_cross_day_resume_case(args) -> int:
    """Run a real subagent runner turn, then prove memory-resume returns task fact sources."""

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

    print_scenario_step(3, "Simulate cross-day archive clues for a resumed parent session")
    _append_parent_subagent_cross_day_resume_clues(agent.root, loaded)
    reloaded_agent = load_scenario_agent(paths.config)
    reloaded_task = reloaded_agent.subagents.load(task.id)
    print(f"reloaded_status={reloaded_task.status} task_dir={reloaded_task.task_dir}")

    print_scenario_step(4, "Run memory-resume and require task fact-source reads")
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    resume = run_scenario_subprocess(
        scenario_command(
            paths,
            "memory-resume",
            "parent subagent cross-day resume",
            "--run-id",
            task.id,
            "--since",
            "2026-04-29",
            "--until",
            "2026-04-30",
            "--json",
        ),
        env=env,
        timeout=30,
    )
    try:
        resume_payload = json.loads(resume.stdout)
    except json.JSONDecodeError as exc:
        resume_payload = {"ok": False, "error": f"memory-resume JSON parse failed: {exc}", "stdout": resume.stdout}

    task_sources = resume_payload.get("task_fact_sources", []) if isinstance(resume_payload, dict) else []
    recommended_reads = resume_payload.get("resume", {}).get("recommended_read_paths", []) if isinstance(resume_payload, dict) else []
    context_block = resume_payload.get("brief", {}).get("context_block", "") if isinstance(resume_payload, dict) else ""
    archive_count = resume_payload.get("resume", {}).get("archive_match_count", 0) if isinstance(resume_payload, dict) else 0
    expected_reads = [
        loaded.status_file,
        loaded.work_log_file,
        loaded.handoff_file,
        loaded.test_checklist_file,
        loaded.output_json,
    ]
    matching_task = next(
        (item for item in task_sources if isinstance(item, dict) and item.get("run_id") == task.id),
        {},
    )
    final_ok = (
        runner.ok
        and loaded.status == "AWAITING_ACCEPTANCE"
        and loaded.verification_status == "NEEDS_ACCEPTANCE"
        and "read_file" in loaded.used_tools
        and runner.tool_rounds == 1
        and backend.calls == 2
        and resume.returncode == 0
        and archive_count >= 2
        and matching_task.get("exists") is True
        and matching_task.get("status") == "AWAITING_ACCEPTANCE"
        and all(path in recommended_reads for path in expected_reads)
        and task.id in context_block
        and "AWAITING_ACCEPTANCE" in context_block
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


def _write_parent_subagent_recovery_fact_files(task) -> None:
    """Keep scenario task fact files aligned with the runner result state."""

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
    """Write deterministic cross-day archive clues for one real subagent runner result."""

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
                task.status_file,
                task.work_log_file,
                task.handoff_file,
                task.test_checklist_file,
                task.runner_result_file,
                task.runner_result_json,
                task.output_json,
            ],
            next_actions=["Read task fact sources, then run parent acceptance only after evidence is checked."],
            created_at="2026-04-30T00:07:00+00:00",
        ),
    )


def print_dispatch_report(report) -> None:
    """打印场景测试里的 dispatch 摘要。"""

    print("summary=" + json.dumps(report.summary, ensure_ascii=False, sort_keys=True))
    for record in report.records:
        status = "OK" if record.ok else "FAIL"
        run = record.run_id or "global"
        print(
            f"- [{status}] {record.step}/{record.action} run={run} "
            f"applied={record.applied} :: {record.message}"
        )


class ScenarioStructuredRepairBackend:
    """场景测试用后端：第一次输出坏结果块，修复回合补齐 JSON。"""

    name = "scenario_structured_repair_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "我已经完成任务，但这次故意输出一个损坏的结构化结果块。\n"
                    "[SUBAGENT_RESULT]\n"
                    "{\n"
                    '  "status": "AWAITING_ACCEPTANCE",\n'
                    '  "summary": "这个 JSON 少了结尾，用来模拟模型输出损坏",\n'
                    '  "evidence": [\n'
                    '    {"kind": "note", "summary": "原始回复声称已有证据", "ok": true}\n'
                ),
                backend=self.name,
            )
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "结构化输出损坏后已通过修复回合补齐。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "修复回合生成了可解析证据", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "structured repair", "command": "", "ok": true, "summary": "坏 JSON 已修复"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": ["结构化输出损坏时先做格式修复，不新增事实"],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def run_scenario_structured_repair_case(args) -> int:
    """验证 runner 坏结构化输出会进入修复回合并通过验收。"""

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=structured-repair")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    backend = ScenarioStructuredRepairBackend()
    agent.backend = backend
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)

    print_scenario_step(1, "创建会输出坏 JSON 的子代理工单")
    task = agent.subagents.create_run(
        goal="极端场景：runner 输出损坏的 SUBAGENT_RESULT，父代理应触发修复回合",
        thought="验证结构化输出坏掉时不会直接把任务丢成无法验收。",
        plan=["输出损坏结果块", "修复结构化结果", "父代理验收"],
        acceptance_checks=["必须触发 structured repair", "修复后必须有证据", "父代理必须验收通过"],
    )
    print(f"run_id={task.id}")

    print_scenario_step(2, "执行 dispatch：runner 输出坏 JSON 后修复并验收")
    report = agent.dispatch_subagents(
        router,
        capability_config,
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="scenario-structured-repair",
        note="structured output damage should be repaired",
    )
    print_dispatch_report(report)
    loaded = agent.subagents.load(task.id)
    runner = json.loads(Path(loaded.runner_result_json).read_text(encoding="utf-8"))
    output = json.loads(Path(loaded.output_json).read_text(encoding="utf-8"))
    print(
        f"final status={loaded.status} verify={loaded.verification_status} "
        f"backend_calls={backend.calls} repair_attempted={runner.get('structured_repair_attempted')} "
        f"repair_ok={runner.get('structured_repair_ok')}"
    )

    final_ok = (
        backend.calls == 2
        and loaded.status == "DONE"
        and loaded.verification_status == "VERIFIED"
        and runner.get("structured_output_found") is True
        and runner.get("structured_output_ok") is True
        and runner.get("structured_repair_attempted") is True
        and runner.get("structured_repair_ok") is True
        and output.get("structured_output", {}).get("repair_attempted") is True
        and any(item.step == "acceptance" and item.ok for item in report.records)
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="structured repair passed" if final_ok else "structured repair failed",
        extra={
            "case": "structured-repair",
            "run_id": task.id,
            "backend_calls": backend.calls,
            "final_status": loaded.status,
            "structured_repair_attempted": runner.get("structured_repair_attempted"),
            "structured_repair_ok": runner.get("structured_repair_ok"),
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


class ScenarioRetryBackend:
    """场景测试用后端：第一次失败，第二次给出可验收结果。"""

    name = "scenario_retry_backend"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("scenario transient runner failure")
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                '  "summary": "runner 在第二次尝试中完成，已生成可验收证据。",\n'
                '  "used_tools": [],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "note", "summary": "第二次 runner 尝试成功", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "runner retry", "command": "", "ok": true, "summary": "第二次尝试通过"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": ["临时 runner 错误可以由父代理有限重试恢复"],\n'
                '  "next_actions": [],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def run_scenario_runner_retry_case(args) -> int:
    """验证临时 runner 失败会被下一轮 dispatch 自动重试。"""

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=runner-retry")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    backend = ScenarioRetryBackend()
    agent.backend = backend
    capability_config = load_capability_config(args.capability_config)
    router = make_capability_router(agent, capability_config, args.skill_dir)

    print_scenario_step(1, "创建会先失败一次的子代理工单")
    task = agent.subagents.create_run(
        goal="极端场景：runner 第一次调用模型失败，下一轮 dispatch 应自动重试",
        thought="验证临时模型/接口错误不会让任务永久卡死。",
        plan=["第一次 runner 失败", "下一轮自动重试", "成功后父代理验收"],
        acceptance_checks=["第二次 runner 必须生成证据", "父代理必须验收通过"],
    )
    print(f"run_id={task.id}")

    print_scenario_step(2, "第一轮 dispatch：模拟 runner 临时失败")
    first = agent.dispatch_subagents(
        router,
        capability_config,
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="scenario-runner-retry",
        note="first attempt should fail",
    )
    print_dispatch_report(first)
    after_first = agent.subagents.load(task.id)
    print(
        f"after_first status={after_first.status} failure_type={after_first.failure_type} "
        f"attempts={after_first.runner_attempts}"
    )

    print_scenario_step(3, "第二轮 dispatch：自动重试并验收")
    second = agent.dispatch_subagents(
        router,
        capability_config,
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="scenario-runner-retry",
        note="retry should succeed",
    )
    print_dispatch_report(second)
    loaded = agent.subagents.load(task.id)
    print(
        f"final status={loaded.status} verify={loaded.verification_status} "
        f"attempts={loaded.runner_attempts} backend_calls={backend.calls}"
    )

    first_runner = [item for item in first.records if item.step == "runner"]
    second_runner = [item for item in second.records if item.step == "runner"]
    final_ok = (
        first_runner
        and first_runner[0].action == "execute_runner"
        and not first_runner[0].ok
        and after_first.status == "BLOCKED"
        and after_first.failure_type == "runner_error"
        and after_first.runner_attempts == 1
        and second_runner
        and second_runner[0].action == "retry_runner"
        and second_runner[0].ok
        and loaded.status == "DONE"
        and loaded.verification_status == "VERIFIED"
        and loaded.runner_attempts == 2
        and backend.calls == 2
    )
    write_scenario_summary(
        paths,
        ok=final_ok,
        reason="runner retry passed" if final_ok else "runner retry failed",
        extra={
            "case": "runner-retry",
            "run_id": task.id,
            "backend_calls": backend.calls,
            "first_status": after_first.status,
            "final_status": loaded.status,
            "runner_attempts": loaded.runner_attempts,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2
