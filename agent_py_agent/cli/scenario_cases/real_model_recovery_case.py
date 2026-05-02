from __future__ import annotations

"""LLM: implements the real-model recovery scenario that verifies a real API round-trip survives cross-day resume.

给人看的解释：
这个文件验证用真实模型 API 跑一轮子代理后，memory-resume 能找回真实响应内容。
比 stub 后端测试更接近真实使用场景。
"""

import json
import os

from ...agent.backend import ModelResponse
from ...agent.backends.base import get_backend
from ..scenario_utils import (
    create_scenario_workspace,
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
    """LLM: wraps a real model backend to verify API connectivity while ensuring structured output succeeds.

    给人看的解释：
    这个类不是新后端，而是把真实后端包一层，顺便记录调用次数和响应内容。
    第一次 generate 调真实模型验证连通性，第二次把真实响应包装成结构化结果。
    """

    name = "scenario_real_model_recovery_backend"

    def __init__(self, real_backend) -> None:
        self.real_backend = real_backend
        self.calls = 0
        self.real_response_text = ""

    def generate(self, prompt: str) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            real = self.real_backend.generate(prompt)
            self.real_response_text = real.text
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool": "read_file", "path": "README.md"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        summary_preview = self.real_response_text[:200]
        return ModelResponse(
            text=(
                "[SUBAGENT_RESULT]\n"
                "{\n"
                '  "status": "AWAITING_ACCEPTANCE",\n'
                f'  "summary": "Real model smoke test: {summary_preview}",\n'
                '  "used_tools": ["read_file"],\n'
                '  "used_skills": [],\n'
                '  "evidence": [\n'
                '    {"kind": "read_file", "summary": "README.md read via real model runner", '
                '"path": "README.md", "ok": true}\n'
                "  ],\n"
                '  "capability_requests": [],\n'
                '  "artifacts": [],\n'
                '  "tests": [\n'
                '    {"name": "real_model_runner", "command": "", "ok": true, '
                '"summary": "real model API call succeeded"}\n'
                "  ],\n"
                '  "patches": [],\n'
                '  "lessons": ["real model API round-trip verified in recovery smoke test"],\n'
                '  "next_actions": ["parent should validate recovery context includes real response content"],\n'
                '  "blocked_reason": "",\n'
                '  "failure_type": ""\n'
                "}\n"
                "[/SUBAGENT_RESULT]"
            ),
            backend=self.name,
        )


def run_scenario_real_model_recovery_case(args) -> int:
    """LLM: run a subagent with a real model API, then prove memory-resume recovers real response content.

    新手说明:
    用真实模型 API 跑一轮子代理，验证模型响应内容能在 memory-resume 恢复后找回。
    这比 stub 后端测试更接近真实使用场景。
    """

    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=real-model-recovery")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    real_backend = get_backend(agent.config.model_backend, agent.config)
    backend = ScenarioRealModelRecoveryBackend(real_backend)
    agent.backend = backend

    print_scenario_step(1, "Create a parent-owned subagent task")
    task = agent.subagents.create_run(
        goal="real model recovery smoke test: read README and wait for parent acceptance",
        thought="verify real model API round-trip survives cross-day recovery.",
        plan=["runner reads README.md via real model", "write structured result", "simulate cross-day", "memory-resume recovers task fact sources"],
        allowed_tools=["read_file"],
        acceptance_checks=["must have read_file evidence", "recovery must recommend task fact sources", "parent decides acceptance after recovery"],
    )
    print(f"run_id={task.id}")

    print_scenario_step(2, "Run the real subagent runner path and stop before acceptance")
    runner = agent.run_subagent(
        task.id,
        dry_run=False,
        probe=False,
        instruction=(
            "Real model recovery smoke test. Read README.md first, then output an AWAITING_ACCEPTANCE "
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
        timeout=120,
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
    echo_signature = "这是 echo 后端的本地响应"
    final_ok = (
        runner.ok
        and loaded.status == "AWAITING_ACCEPTANCE"
        and loaded.verification_status == "NEEDS_ACCEPTANCE"
        and "read_file" in loaded.used_tools
        and backend.calls >= 2
        and bool(backend.real_response_text)
        and echo_signature not in backend.real_response_text
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
        reason="real model recovery smoke test passed" if final_ok else "real model recovery smoke test failed",
        extra={
            "case": "real-model-recovery",
            "run_id": task.id,
            "runner": {
                "ok": runner.ok,
                "status": runner.status,
                "verification_status": runner.verification_status,
                "tool_rounds": runner.tool_rounds,
                "backend_calls": backend.calls,
                "real_response_len": len(backend.real_response_text),
            },
            "resume": resume_payload,
        },
    )
    print(f"\nsummary_json={paths.summary_json}")
    print(f"summary_md={paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2
