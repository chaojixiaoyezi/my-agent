# LLM: CLI scenario case definition; keep fixture flow and expected gateway/subagent behavior stable.
# 模块用途: 定义一类命令行情景测试，用来复现和验证端到端流程。

from __future__ import annotations

"""real-model multi-round recovery scenario.

给人看的解释：
这个模块验证真实模型 API 经过多轮工具调用后，跨天恢复仍能找回 evidence 和任务事实源。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...agent.backend import ModelResponse
from ...agent.backends.base import get_backend
from ..scenario_utils import (
    create_scenario_workspace,
    install_scenario_backend,
    load_scenario_agent,
    print_scenario_step,
    write_scenario_summary,
)
from .real_model_recovery_case import (
    _real_model_recovery_resume,
    _scenario_summary_preview,
    _subagent_result_text,
)
from .subagent_cases import _write_parent_subagent_recovery_fact_files


# LLM: ScenarioRealModelMultiRoundBackend 是scenario CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 定义本模块对外传递的数据字段，字段名需要和调用方保持一致。
class ScenarioRealModelMultiRoundBackend:

    name = "scenario_real_model_multi_round_backend"

    # LLM: __init__ 属于scenario CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def __init__(self, real_backend) -> None:
        self.real_backend = real_backend
        self.calls = 0
        self.real_response_text = ""
        self.tool_sequence: list[str] = []

    # LLM: generate 属于scenario CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return self._tool_call_response(prompt, on_chunk, "read_file")
        if self.calls == 2:
            return self._tool_call_response(prompt, on_chunk, "search_text")
        return ModelResponse(text=self._final_structured_result(), backend=self.name)

    # LLM: _tool_call_response 属于scenario CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def _tool_call_response(self, prompt: str, on_chunk, tool_name: str) -> ModelResponse:
        real = self.real_backend.generate(prompt, on_chunk=on_chunk)
        self.real_response_text += real.text
        self.tool_sequence.append(tool_name)
        payload = _tool_payload(tool_name)
        return ModelResponse(text=f"[TOOL_CALL]\n{payload}\n[/TOOL_CALL]", backend=self.name)

    # LLM: _final_structured_result 属于scenario CLI；改行为前先对齐调用方和快照/单测。
    # 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
    def _final_structured_result(self) -> str:
        summary_preview = _scenario_summary_preview(self.real_response_text)
        return _subagent_result_text({
            "status": "AWAITING_ACCEPTANCE",
            "summary": f"Real model multi-round smoke test: {summary_preview}",
            "used_tools": self.tool_sequence,
            "used_skills": [],
            "evidence": [
                {"kind": "read_file", "summary": "README.md read via real model runner", "path": "README.md", "ok": True},
                {"kind": "search_text", "summary": "search_text gateway via real model runner", "query": "gateway", "ok": True},
            ],
            "capability_requests": [],
            "artifacts": [],
            "tests": [{
                "name": "real_model_multi_round_runner",
                "command": "",
                "ok": True,
                "summary": "real model multi-round API call succeeded",
            }],
            "patches": [],
            "lessons": ["real model multi-round API round-trip verified in recovery smoke test"],
            "next_actions": ["parent should validate recovery context includes multi-round evidence"],
            "blocked_reason": "",
            "failure_type": "",
        })


# LLM: _tool_payload 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 生成结构化字段，保持 CLI 输出、报告和测试读取口径一致。
def _tool_payload(tool_name: str) -> str:
    if tool_name == "read_file":
        return '{"tool": "read_file", "path": "README.md"}'
    return '{"tool": "search_text", "query": "gateway", "path": "."}'


# LLM: _multi_round_setup 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _multi_round_setup(args):
    paths = create_scenario_workspace(args)
    print("MY-AGENT SCENARIO TEST")
    print("case=real-model-recovery-multi-round")
    print(f"run_root={paths.run_root}")
    print(f"fixture_root={paths.fixture_root}")
    print(f"config={paths.config}")

    agent = load_scenario_agent(paths.config)
    real_backend = get_backend(agent.config.model_backend, agent.config)
    backend = ScenarioRealModelMultiRoundBackend(real_backend)
    install_scenario_backend(agent, backend)

    print_scenario_step(1, "Create a parent-owned subagent task for multi-round test")
    task = agent.subagents.create_run(
        goal="real model multi-round recovery smoke test: read README, search gateway, wait for parent acceptance",
        thought="verify real model API multi-round round-trip survives cross-day recovery.",
        plan=["runner reads README.md via real model", "runner searches gateway via real model", "write structured result", "simulate cross-day", "memory-resume recovers multi-round evidence"],
        allowed_tools=["read_file", "search_text"],
        acceptance_checks=["must have read_file and search_text evidence", "recovery must recommend task fact sources", "parent decides acceptance after recovery"],
    )
    print(f"run_id={task.id}")

    print_scenario_step(2, "Run the real subagent runner through 2+ tool call rounds")
    runner = agent.run_subagent(
        task.id,
        dry_run=False,
        probe=False,
        instruction=(
            "Real model multi-round recovery smoke test. Read README.md first, then search_text for 'gateway', "
            "then output an AWAITING_ACCEPTANCE [SUBAGENT_RESULT]. Do not mark DONE yourself."
        ),
    )
    loaded = agent.subagents.load(task.id)
    _write_parent_subagent_recovery_fact_files(loaded)
    print(
        f"runner_ok={runner.ok} status={loaded.status} verify={loaded.verification_status} "
        f"tool_rounds={runner.tool_rounds} backend_calls={backend.calls} "
        f"real_response_len={len(backend.real_response_text)} "
        f"tool_sequence={backend.tool_sequence}"
    )
    return paths, agent, backend, task, loaded, runner


# LLM: _multi_round_build_expected_reads 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _multi_round_build_expected_reads(loaded):
    return [
        loaded.status_file,
        loaded.work_log_file,
        loaded.handoff_file,
        loaded.test_checklist_file,
        loaded.output_json,
    ]


# LLM: _MultiRoundVerifyContext 是scenario CLI的数据契约；字段名会被调用方和测试读取。
# 类用途: 集中携带运行期上下文和共享引用，供相邻阶段稳定读取。
@dataclass
class _MultiRoundVerifyContext:
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


# LLM: _multi_round_verify 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _multi_round_verify(ctx: _MultiRoundVerifyContext) -> int:
    expected_reads = _multi_round_build_expected_reads(ctx.loaded)
    final_ok = _multi_round_final_ok(ctx, expected_reads, _output_json_valid(ctx.loaded))
    write_scenario_summary(
        ctx.paths,
        ok=final_ok,
        reason="real model multi-round recovery smoke test passed" if final_ok else "real model multi-round recovery smoke test failed",
        extra={
            "case": "real-model-recovery-multi-round",
            "run_id": ctx.task.id,
            "runner": {
                "ok": ctx.runner.ok,
                "status": ctx.runner.status,
                "verification_status": ctx.runner.verification_status,
                "tool_rounds": ctx.runner.tool_rounds,
                "backend_calls": ctx.backend.calls,
                "real_response_len": len(ctx.backend.real_response_text),
                "tool_sequence": ctx.backend.tool_sequence,
            },
            "resume": ctx.resume_payload,
        },
    )
    print(f"\nsummary_json={ctx.paths.summary_json}")
    print(f"summary_md={ctx.paths.summary_md}")
    print("SCENARIO_PASS" if final_ok else "SCENARIO_FAIL")
    return 0 if final_ok else 2


# LLM: _output_json_valid 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _output_json_valid(loaded) -> bool:
    try:
        json.loads(Path(loaded.output_json).read_text(encoding="utf-8"))
        return True
    except (OSError, json.JSONDecodeError):
        return False


# LLM: _multi_round_final_ok 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _multi_round_final_ok(ctx: _MultiRoundVerifyContext, expected_reads: list, output_json_valid: bool) -> bool:
    echo_signature = "这是 echo 后端的本地响应"
    return (
        ctx.runner.ok
        and ctx.loaded.status == "AWAITING_ACCEPTANCE"
        and ctx.loaded.verification_status == "NEEDS_ACCEPTANCE"
        and "read_file" in ctx.loaded.used_tools
        and "search_text" in ctx.loaded.used_tools
        and ctx.backend.calls >= 3
        and len(ctx.backend.tool_sequence) >= 2
        and "read_file" in ctx.backend.tool_sequence
        and "search_text" in ctx.backend.tool_sequence
        and bool(ctx.backend.real_response_text)
        and echo_signature not in ctx.backend.real_response_text
        and ctx.resume_payload.get("ok") is True
        and ctx.archive_count >= 2
        and ctx.matching_task.get("exists") is True
        and ctx.matching_task.get("status") == "AWAITING_ACCEPTANCE"
        and all(path in ctx.recommended_reads for path in expected_reads)
        and ctx.task.id in ctx.context_block
        and "AWAITING_ACCEPTANCE" in ctx.context_block
        and output_json_valid
    )


# LLM: run_scenario_real_model_recovery_multi_round_case 属于scenario CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_scenario_real_model_recovery_multi_round_case(args) -> int:

    paths, agent, backend, task, loaded, runner = _multi_round_setup(args)

    print_scenario_step(3, "Simulate cross-day archive clues for a resumed parent session")
    resume_payload = _real_model_recovery_resume(paths, agent, task, loaded)

    task_sources = resume_payload.get("task_fact_sources", []) if isinstance(resume_payload, dict) else []
    recommended_reads = resume_payload.get("resume", {}).get("recommended_read_paths", []) if isinstance(resume_payload, dict) else []
    context_block = resume_payload.get("brief", {}).get("context_block", "") if isinstance(resume_payload, dict) else ""
    archive_count = resume_payload.get("resume", {}).get("archive_match_count", 0) if isinstance(resume_payload, dict) else 0
    matching_task = next(
        (item for item in task_sources if isinstance(item, dict) and item.get("run_id") == task.id),
        {},
    )

    return _multi_round_verify(
        _MultiRoundVerifyContext(
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
