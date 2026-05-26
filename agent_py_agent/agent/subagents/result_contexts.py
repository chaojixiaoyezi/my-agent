# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""compact context objects used while recording runner results.

给人看的解释：
runner 结果写回时参数很多，用 dataclass 打包，既减少长参数列表也方便测试构造。
"""

from dataclasses import dataclass
from typing import Any

from .models import SubAgentParsedOutput, SubAgentTask


# LLM: OutputPayloadContext 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存output载荷上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class OutputPayloadContext:
    """Bundle of all _build_output_payload parameters into a single object."""

    task: SubAgentTask
    dry_run: bool
    ok: bool
    message: str
    backend: str
    tool_rounds: int
    parsed: SubAgentParsedOutput
    actual_tools: list[str] | None
    structured_evidence_count: int
    structured_request_count: int
    created_request_ids: list[str]
    ignored_tools: list[str]
    ignored_skills: list[str]
    artifacts: list[dict[str, Any]]
    # LLM: 最终收口把证据包和发现项当作一等事实消费，不再只读文本摘要。
    evidence_packets: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    tests: list[dict[str, Any]]
    patches: list[dict[str, Any]]
    lessons: list[str]
    blockers: list[str]
    next_actions: list[str]
    structured_repair_attempted: bool
    structured_repair_ok: bool
    structured_repair_error: str
    now: float


# LLM: RunnerResultContext 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存执行器结果上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerResultContext:
    """Bundle of all _build_runner_result parameters into a single object."""

    task: SubAgentTask
    dry_run: bool
    ok: bool
    message: str
    backend: str
    tool_rounds: int
    prompt: str
    response: str
    parsed: SubAgentParsedOutput
    structured_repair_attempted: bool
    structured_repair_ok: bool
    structured_repair_error: str
    structured_evidence_count: int
    structured_request_count: int
    artifact_count: int
    test_count: int
    patch_count: int
    lesson_count: int
    now: float
