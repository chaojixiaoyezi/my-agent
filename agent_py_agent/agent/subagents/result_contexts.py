from __future__ import annotations

"""LLM: compact context objects used while recording runner results.

给人看的解释：
runner 结果写回时参数很多，用 dataclass 打包，既减少长参数列表也方便测试构造。
"""

from dataclasses import dataclass
from typing import Any

from .models import SubAgentParsedOutput, SubAgentTask


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
    tests: list[dict[str, Any]]
    patches: list[dict[str, Any]]
    lessons: list[str]
    blockers: list[str]
    next_actions: list[str]
    structured_repair_attempted: bool
    structured_repair_ok: bool
    structured_repair_error: str
    now: float


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
