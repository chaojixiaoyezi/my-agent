
from __future__ import annotations

"""implements the real-model recovery scenario that verifies a real API round-trip survives cross-day resume.

给人看的解释：
这个文件验证用真实模型 API 跑一轮子代理后，memory-resume 能找回真实响应内容。
比 stub 后端测试更接近真实使用场景。
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ...agent.backends import ModelResponse
from ...agent.backends.base import get_backend
from ..scenario_utils import (
    create_scenario_workspace,
    install_scenario_backend,
    load_scenario_agent,
    print_scenario_step,
    run_scenario_subprocess,
    scenario_command,
    write_scenario_summary,
)
from .subagent_cases import (
    _append_parent_subagent_cross_day_resume_clues,
    _write_parent_subagent_recovery_fact_files,
)


class ScenarioRealModelRecoveryBackend:

    name = "scenario_real_model_recovery_backend"

    def __init__(self, real_backend) -> None:
        self.real_backend = real_backend
        self.calls = 0
        self.real_response_text = ""

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            real = self.real_backend.generate(prompt, on_chunk=on_chunk)
            self.real_response_text = real.text
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool": "read_file", "path": "README.md"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        summary_preview = _scenario_summary_preview(self.real_response_text)
        payload = {
            "status": "DONE",
            "summary": f"Real model smoke test: {summary_preview}",
            "used_tools": ["read_file"],
            "used_skills": [],
            "evidence": [{
                "kind": "read_file",
                "summary": "README.md read via real model runner",
                "path": "README.md",
                "ok": True,
            }],
            "capability_requests": [],
            "artifacts": [],
            "tests": [{
                "name": "real_model_runner",
                "command": "",
                "ok": True,
                "summary": "real model API call succeeded",
            }],
            "patches": [],
            "lessons": ["real model API round-trip verified in recovery smoke test"],
            "next_actions": ["parent should validate recovery context includes real response content"],
            "blocked_reason": "",
            "failure_type": "",
        }
        return ModelResponse(
            text=_subagent_result_text(payload),
            backend=self.name,
        )


def _subagent_result_text(payload: dict[str, object]) -> str:
    return "[SUBAGENT_RESULT]\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n[/SUBAGENT_RESULT]"


def _scenario_summary_preview(text: str, limit: int = 200) -> str:
    preview = str(text or "")[:limit]
    replacements = {
        "[TOOL_CALL]": "<TOOL_CALL>",
        "[/TOOL_CALL]": "</TOOL_CALL>",
        "[SUBAGENT_CALL]": "<SUBAGENT_CALL>",
        "[/SUBAGENT_CALL]": "</SUBAGENT_CALL>",
        "[SUBAGENT_RESULT]": "<SUBAGENT_RESULT>",
        "[/SUBAGENT_RESULT]": "</SUBAGENT_RESULT>",
        "[PARENT_PLANNER_RESULT]": "<PARENT_PLANNER_RESULT>",
        "[/PARENT_PLANNER_RESULT]": "</PARENT_PLANNER_RESULT>",
    }
    for marker, safe in replacements.items():
        preview = preview.replace(marker, safe)
    return preview


def _real_model_recovery_setup(args):
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=real-model-recovery")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    real_backend = get_backend(agent.config.model_backend, agent.config)
    backend = ScenarioRealModelRecoveryBackend(real_backend)
    install_scenario_backend(agent, backend)

    print_scenario_step(1, "Create a parent-owned subagent task")
    task = agent.subagents.create_run(
        goal="real model recovery smoke test: read README and wait for closeout",
        thought="verify real model API round-trip survives cross-day recovery.",
        plan=["runner reads README.md via real model", "write structured result", "simulate cross-day", "memory-resume recovers task fact sources"],
        allowed_tools=["read_file"],
        acceptance_checks=["must have read_file evidence", "recovery must recommend task fact sources", "caller decides closeout after recovery"],
    )
    print(f"run_id={task.id}")

    print_scenario_step(2, "Run the real subagent runner path and stop before acceptance")
    runner = agent.run_subagent(
        task.id,
        dry_run=False,
        probe=False,
        instruction=(
            "Real model recovery smoke test. Read README.md first, then output an DONE "
            "[SUBAGENT_RESULT]. Do not mark DONE yourself."
        ),
    )
    loaded = agent.subagents.load(task.id)
    _write_parent_subagent_recovery_fact_files(loaded)
    print(
        f"runner_ok={runner.ok} status={loaded.status} verify={loaded.verification_status} "
        f"tool_rounds={runner.tool_rounds} backend_calls={backend.calls} "
        f"real_response_len={len(backend.real_response_text)}"
    )
    return paths, agent, backend, task, loaded, runner


def _real_model_recovery_resume(paths, agent, task, loaded):
    _append_parent_subagent_cross_day_resume_clues(Path(agent.home_paths.owner_home_dir), loaded)
    reloaded_agent = load_scenario_agent(paths.config)
    reloaded_task = reloaded_agent.subagents.load(task.id)
    print(f"reloaded_status={reloaded_task.status} task_dir={reloaded_task.task_dir}")

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
        timeout=120,
    )
    try:
        resume_payload = json.loads(resume.stdout)
    except json.JSONDecodeError as exc:
        resume_payload = {"ok": False, "error": f"memory-resume JSON parse failed: {exc}", "stdout": resume.stdout}
    return resume_payload


def _real_model_recovery_build_expected_reads(loaded):
    return [
        loaded.status_file,
        loaded.work_log_file,
        loaded.handoff_file,
        loaded.test_checklist_file,
        loaded.output_json,
    ]


@dataclass
class _RealModelRecoveryVerifyContext:
    paths: Any
    task: Any
    loaded: Any
    runner: Any
    backend: Any
    resume_payload: dict
    task_sources: list
    recommended_reads: list
    context_block: str
    archive_count: int
    matching_task: dict


def _real_model_recovery_verify(ctx: _RealModelRecoveryVerifyContext) -> int:
    expected_reads = _real_model_recovery_build_expected_reads(ctx.loaded)
    echo_signature = "这是 echo 后端的本地响应"
    final_ok = (
        ctx.runner.ok
        and ctx.loaded.status == "DONE"
        and ctx.loaded.verification_status == "VERIFIED"
        and "read_file" in ctx.loaded.used_tools
        and ctx.backend.calls >= 2
        and bool(ctx.backend.real_response_text)
        and echo_signature not in ctx.backend.real_response_text
        and ctx.resume_payload.get("ok") is True
        and ctx.archive_count >= 2
        and ctx.matching_task.get("exists") is True
        and ctx.matching_task.get("status") == "DONE"
        and all(path in ctx.recommended_reads for path in expected_reads)
        and ctx.task.id in ctx.context_block
        and "DONE" in ctx.context_block
    )
    write_scenario_summary(
        ctx.paths,
        ok=final_ok,
        reason="real model recovery smoke test passed" if final_ok else "real model recovery smoke test failed",
        extra={
            "case": "real-model-recovery",
            "run_id": ctx.task.id,
            "runner": {
                "ok": ctx.runner.ok,
                "status": ctx.runner.status,
                "verification_status": ctx.runner.verification_status,
                "tool_rounds": ctx.runner.tool_rounds,
                "backend_calls": ctx.backend.calls,
                "real_response_len": len(ctx.backend.real_response_text),
            },
            "resume": ctx.resume_payload,
        },
    )
    print(f"\nsummary_json={ctx.paths.summary_json}")
    print(f"summary_md={ctx.paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


def run_scenario_real_model_recovery_case(args) -> int:

    paths, agent, backend, task, loaded, runner = _real_model_recovery_setup(args)

    print_scenario_step(3, "Simulate cross-day archive clues for a resumed parent session")
    resume_payload = _real_model_recovery_resume(paths, agent, task, loaded)

    task_sources = resume_payload.get("task_fact_sources", []) if isinstance(resume_payload, dict) else []
    recommended_reads = resume_payload.get("resume", {}).get("recommended_read_paths", []) if isinstance(resume_payload, dict) else []
    context_block = resume_payload.get("brief", {}).get("context_block", "") if isinstance(resume_payload, dict) else ""
    archive_count = resume_payload.get("resume", {}).get("archive_match_count", 0) if isinstance(resume_payload, dict) else 0
    expected_reads = _real_model_recovery_build_expected_reads(loaded)
    matching_task = next(
        (item for item in task_sources if isinstance(item, dict) and item.get("run_id") == task.id),
        {},
    )

    return _real_model_recovery_verify(
        _RealModelRecoveryVerifyContext(
            paths=paths,
            task=task,
            loaded=loaded,
            runner=runner,
            backend=backend,
            resume_payload=resume_payload,
            task_sources=task_sources,
            recommended_reads=recommended_reads,
            context_block=context_block,
            archive_count=archive_count,
            matching_task=matching_task,
        )
    )
