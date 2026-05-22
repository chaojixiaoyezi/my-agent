# LLM: Main-agent task runtime adapters keep track differences out of orchestration.
# 模块用途: 用结构化 adapter 描述 task/real_task 的差异，公共 runtime 只执行统一流程。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# LLM: MainAgentTaskRuntimeAdapter is the single contract between wrappers and runtime.
# 类用途: 收纳 suite、路径、subprocess、验收、恢复这些差异，避免复制整套执行流程。
@dataclass(frozen=True)
class MainAgentTaskRuntimeAdapter:
    track_id: str
    schema_version: str
    suite_root_ref: str
    compatible_execution_roots: tuple[str, ...]
    request_model: type[Any]
    report_model: type[Any]
    case_result_model: type[Any]
    case_runtime_model: type[Any]
    case_result_bundle_model: type[Any]
    suite_request_model: type[Any]
    plan_suite: Callable[..., Any]
    case_paths: Callable[[Path, str], dict[str, Path]]
    execution_root: Callable[[Path], Path]
    package_root: Callable[[Any], Path]
    rel: Callable[[Path, Path], str]
    write_json: Callable[[Path, dict[str, object]], None]
    write_case_config: Callable[[Path, Path | None, Path], None]
    command_for_case: Callable[..., list[str]]
    append_event: Callable[[Path, str, dict[str, object] | None], None]
    subprocess_request_model: type[Any]
    subprocess_result_model: type[Any]
    run_subprocess: Callable[[Any], Any]
    activity_timeout_seconds: Callable[[int], int]
    validate_case_artifacts: Callable[[Any], Any]
    append_acceptance_event: Callable[[Any, Any], None]
    case_issues: Callable[[Any, int, Any], tuple[str, ...]]
    case_result: Callable[[Any], Any]
    timeout_issues: Callable[..., tuple[str, ...]]
    write_recovery_packet_ref: Callable[[Any], str]
    case_ids_for_recovery_request: Callable[[tuple[str, ...], Path | None], tuple[str, ...]]
    resume_attempt_paths: Callable[[dict[str, Path], Path | None], dict[str, Path]]


__all__ = ["MainAgentTaskRuntimeAdapter"]
