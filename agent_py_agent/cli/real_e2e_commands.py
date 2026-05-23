# LLM: Real E2E CLI exposes main-agent foundation checks as a stable, refs-first command.
# 模块用途: 提供 `my-agent real-e2e`，把主代理基础测试和产物验收写成可重复运行的报告。

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from ..agent.contracts.artifact_acceptance import (
    ArtifactAcceptanceRequest,
    validate_artifact,
)
from ..agent.contracts.main_agent_foundation_runner import (
    MainAgentFoundationRequest,
    run_main_agent_foundation,
)
from ..agent.contracts.main_agent_task_execution import (
    MainAgentTaskExecutionRequest,
    revalidate_main_agent_task_execution,
    run_main_agent_task_execution,
)
from ..agent.contracts.main_agent_task_suite import (
    MainAgentTaskSuiteRequest,
    plan_main_agent_task_suite,
)

# LLM: Legacy aliases keep existing tests and scripts working while the runtime moves to generic task contracts.
# 模块用途: 提供旧符号名的兼容入口；真正实现已经迁到通用任务合同模块。
run_main_agent_real_task_execution = run_main_agent_task_execution
revalidate_main_agent_real_task_execution = revalidate_main_agent_task_execution
plan_main_agent_real_task_suite = plan_main_agent_task_suite
MainAgentRealTaskExecutionRequest = MainAgentTaskExecutionRequest
MainAgentRealTaskSuiteRequest = MainAgentTaskSuiteRequest


# LLM: RealE2EPayloadRequest bundles report parts before JSON serialization.
# 类用途: 描述 real-e2e 报告包含哪些部分，避免 payload helper 随新增区域拉长签名。
@dataclass(frozen=True)
class RealE2EPayloadRequest:
    ok: bool
    report_path: Path
    foundation: dict[str, object]
    artifacts: list[dict[str, object]]
    real_task_suite: dict[str, object] | None = None
    real_task_execution: dict[str, object] | None = None
    real_task_revalidation: dict[str, object] | None = None


# LLM: add_real_e2e_subcommand registers the formal main-agent E2E test entrypoint.
# 函数用途: 注册 `real-e2e` 参数；默认不调用真实模型，真实模型用例由显式开关和外部报告承接。
def add_real_e2e_subcommand(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    parser = subparsers.add_parser("real-e2e", help="运行主代理基础真实 E2E 验收矩阵")
    parser.add_argument(
        "--workspace", default="", help="测试工作区；默认使用当前目录下 .my-agent-real-e2e"
    )
    parser.add_argument(
        "--report", default="", help="报告输出路径；默认写到 workspace/real_e2e_report.json"
    )
    parser.add_argument(
        "--artifact", action="append", default=[], help="可重复：额外验收真实任务产物文件"
    )
    parser.add_argument(
        "--include-real-model", action="store_true", help="标记纳入真实模型用例；CI 默认不开启"
    )
    parser.add_argument(
        "--real-task-suite", action="store_true", help="生成主代理真实任务批量测试计划"
    )
    parser.add_argument(
        "--real-task-max-workers", type=int, default=4, help="真实任务计划的最大并发工位"
    )
    parser.add_argument(
        "--real-task-timeout", type=int, default=480, help="真实任务计划的单任务超时秒数"
    )
    parser.add_argument("--run-real-tasks", action="store_true", help="显式执行受控真实任务")
    parser.add_argument(
        "--real-task-case", action="append", default=[], help="可重复：只执行/计划指定 case_id"
    )
    parser.add_argument(
        "--real-task-base-config", default="", help="真实任务执行使用的基础配置文件"
    )
    parser.add_argument(
        "--real-task-prompt-override",
        action="append",
        default=[],
        help="可重复：case_id=prompt_file，用外部文件覆盖指定真实任务 prompt",
    )
    parser.add_argument(
        "--revalidate-real-task-report",
        default="",
        help="只读复验已有真实任务执行报告，不重新启动模型进程",
    )
    parser.add_argument(
        "--resume-real-task-recovery-packet",
        default="",
        help="按 recovery_packet.json 续跑同一个真实任务 case",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.set_defaults(func=cmd_real_e2e)


# LLM: cmd_real_e2e runs deterministic foundation checks plus optional artifact acceptance.
# 函数用途: 执行主代理基础测试、验收指定产物、写 JSON 报告，并用退出码表达是否通过。
def cmd_real_e2e(args) -> int:
    workspace = _workspace_path(getattr(args, "workspace", ""))
    foundation = run_main_agent_foundation(
        MainAgentFoundationRequest(
            workspace=workspace,
            include_real_model=bool(getattr(args, "include_real_model", False)),
        )
    )
    artifact_reports = _artifact_reports(getattr(args, "artifact", []) or [], workspace=workspace)
    real_task_suite = _real_task_suite(args, workspace=workspace)
    real_task_execution = _real_task_execution(args, workspace=workspace)
    real_task_revalidation = _real_task_revalidation(args, workspace=workspace)
    ok = foundation.ok and all(item.get("ok") for item in artifact_reports)
    report_path = _report_path(getattr(args, "report", ""), workspace=workspace)
    payload = _payload(
        RealE2EPayloadRequest(
            ok=ok
            and _real_task_suite_ok(real_task_suite)
            and _real_task_suite_ok(real_task_execution)
            and _real_task_suite_ok(real_task_revalidation),
            report_path=report_path,
            foundation=foundation.to_dict(),
            artifacts=artifact_reports,
            real_task_suite=real_task_suite,
            real_task_execution=real_task_execution,
            real_task_revalidation=real_task_revalidation,
        )
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    _print_payload(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if payload.get("ok") else 2


# LLM: _workspace_path keeps generated E2E files isolated from user project files by default.
# 函数用途: 解析 real-e2e 工作区，没传时使用当前目录下 `.my-agent-real-e2e`。
def _workspace_path(value: str) -> Path:
    return Path(value).expanduser() if value else Path.cwd() / ".my-agent-real-e2e"


# LLM: _report_path co-locates the machine report with the E2E workspace unless overridden.
# 函数用途: 解析报告路径，支持用户显式传入，也支持默认工作区报告。
def _report_path(value: str, *, workspace: Path) -> Path:
    return Path(value).expanduser() if value else workspace / "real_e2e_report.json"


# LLM: _artifact_reports validates user-supplied deliverables through the artifact acceptance contract.
# 函数用途: 对真实任务产物逐个运行通用验收器，返回可序列化 findings。
def _artifact_reports(values: list[str], *, workspace: Path) -> list[dict[str, object]]:
    return [
        validate_artifact(
            ArtifactAcceptanceRequest(path=Path(value).expanduser(), workspace_root=workspace)
        ).to_dict()
        for value in values
    ]


# LLM: _real_task_suite optionally writes controlled task plans without launching model workers.
# 函数用途: 当 CLI 显式请求时，生成主代理真实任务批量测试的 refs-first 计划。
def _real_task_suite(args, *, workspace: Path) -> dict[str, object] | None:
    if not bool(getattr(args, "real_task_suite", False)):
        return None
    report = plan_main_agent_real_task_suite(
        MainAgentRealTaskSuiteRequest(
            workspace=workspace,
            max_workers=int(getattr(args, "real_task_max_workers", 4)),
            task_timeout_seconds=int(getattr(args, "real_task_timeout", 480)),
            execute=False,
            prompt_overrides=_prompt_overrides(args),
        )
    )
    return report.to_dict()


# LLM: _real_task_suite_ok keeps optional real-task planning part of the command status.
# 函数用途: 如果真实任务计划被请求且生成失败，则让 real-e2e 用非零退出码暴露问题。
def _real_task_suite_ok(value: dict[str, object] | None) -> bool:
    return True if value is None else bool(value.get("ok"))


# LLM: _real_task_execution optionally runs controlled main-agent tasks behind an explicit flag.
# 函数用途: 只有用户传 --run-real-tasks 时，才按结构化计划启动主代理任务并收集日志引用。
def _real_task_execution(args, *, workspace: Path) -> dict[str, object] | None:
    recovery_value = str(getattr(args, "resume_real_task_recovery_packet", "") or "").strip()
    if not bool(getattr(args, "run_real_tasks", False)) and not recovery_value:
        return None
    config_value = str(getattr(args, "real_task_base_config", "") or "").strip()
    base_config = Path(config_value).expanduser() if config_value else None
    recovery_packet = Path(recovery_value).expanduser() if recovery_value else None
    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=workspace,
            max_workers=int(getattr(args, "real_task_max_workers", 4)),
            task_timeout_seconds=int(getattr(args, "real_task_timeout", 480)),
            execute=True,
            case_ids=tuple(getattr(args, "real_task_case", []) or ()),
            prompt_overrides=_prompt_overrides(args),
            base_config_path=base_config,
            recovery_packet_path=recovery_packet,
        )
    )
    return report.to_dict()


# LLM: _real_task_revalidation rechecks stored execution reports without launching workers.
# 函数用途: 当用户传 --revalidate-real-task-report 时，只读复验产物合同并返回报告。
def _real_task_revalidation(args, *, workspace: Path) -> dict[str, object] | None:
    value = str(getattr(args, "revalidate_real_task_report", "") or "").strip()
    if not value:
        return None
    report = revalidate_main_agent_real_task_execution(Path(value).expanduser(), workspace=workspace)
    return report.to_dict()


# LLM: Prompt overrides are external structured inputs, so production case code stays generic.
# 函数用途: 解析 `case_id=文件` 列表并读取 prompt 内容，供 suite/execution request 注入。
def _prompt_overrides(args) -> dict[str, str]:
    values = getattr(args, "real_task_prompt_override", []) or []
    overrides: dict[str, str] = {}
    for raw in values:
        case_id, prompt_path = _split_prompt_override(str(raw))
        overrides[case_id] = Path(prompt_path).expanduser().read_text(encoding="utf-8")
    return overrides


# LLM: Override specs fail early when the user gives an ambiguous CLI shape.
# 函数用途: 将 `case_id=path` 拆成结构化二元组，不从自然语言中猜 case。
def _split_prompt_override(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError("--real-task-prompt-override must be case_id=prompt_file")
    case_id, path = value.split("=", 1)
    case_id = case_id.strip()
    path = path.strip()
    if not case_id or not path:
        raise ValueError("--real-task-prompt-override must include non-empty case_id and prompt_file")
    return case_id, path


# LLM: _payload gives CLI, docs, and frontend one stable report shape.
# 函数用途: 组合基础测试结果、产物验收结果和报告引用，保留 summary 便于旧调用方读取。
def _payload(request: RealE2EPayloadRequest) -> dict[str, object]:
    payload = {
        "ok": request.ok,
        "report_ref": str(request.report_path),
        "summary": dict(
            request.foundation.get("summary", {})
            if isinstance(request.foundation.get("summary"), dict)
            else {}
        ),
        "foundation": request.foundation,
        "artifact_acceptance": request.artifacts,
    }
    if request.real_task_suite is not None:
        payload["main_agent_task_suite"] = request.real_task_suite
        payload["main_agent_real_task_suite"] = request.real_task_suite
    if request.real_task_execution is not None:
        payload["main_agent_task_execution"] = request.real_task_execution
        payload["main_agent_real_task_execution"] = request.real_task_execution
    if request.real_task_revalidation is not None:
        payload["main_agent_task_revalidation"] = request.real_task_revalidation
        payload["main_agent_real_task_revalidation"] = request.real_task_revalidation
    return payload


# LLM: _print_payload keeps human mode short and JSON mode exact.
# 函数用途: 根据 --json 输出完整 JSON 或简短人类摘要，不打印大产物正文。
def _print_payload(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    print("MY-AGENT REAL E2E")
    print(f"status={'ok' if payload.get('ok') else 'failed'}")
    print(f"report_ref={payload.get('report_ref')}")
    print(
        "foundation="
        f"passed={summary.get('passed', 0)} failed={summary.get('failed', 0)} skipped={summary.get('skipped', 0)}"
    )
    print(f"artifact_acceptance_count={len(payload.get('artifact_acceptance') or [])}")


__all__ = ["add_real_e2e_subcommand", "cmd_real_e2e"]
