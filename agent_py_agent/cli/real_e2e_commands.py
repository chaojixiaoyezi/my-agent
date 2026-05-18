# LLM: Real E2E CLI exposes main-agent foundation checks as a stable, refs-first command.
# 模块用途: 提供 `my-agent real-e2e`，把主代理基础测试和产物验收写成可重复运行的报告。

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..agent.contracts.artifact_acceptance import (
    ArtifactAcceptanceRequest,
    validate_artifact,
)
from ..agent.contracts.main_agent_foundation_runner import (
    MainAgentFoundationRequest,
    run_main_agent_foundation,
)


# LLM: add_real_e2e_subcommand registers the formal main-agent E2E test entrypoint.
# 函数用途: 注册 `real-e2e` 参数；默认不调用真实模型，真实模型用例由显式开关和外部报告承接。
def add_real_e2e_subcommand(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("real-e2e", help="运行主代理基础真实 E2E 验收矩阵")
    parser.add_argument("--workspace", default="", help="测试工作区；默认使用当前目录下 .my-agent-real-e2e")
    parser.add_argument("--report", default="", help="报告输出路径；默认写到 workspace/real_e2e_report.json")
    parser.add_argument("--artifact", action="append", default=[], help="可重复：额外验收真实任务产物文件")
    parser.add_argument("--include-real-model", action="store_true", help="标记纳入真实模型用例；CI 默认不开启")
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
    ok = foundation.ok and all(item.get("ok") for item in artifact_reports)
    report_path = _report_path(getattr(args, "report", ""), workspace=workspace)
    payload = _payload(ok=ok, report_path=report_path, foundation=foundation.to_dict(), artifacts=artifact_reports)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    _print_payload(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if ok else 2


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


# LLM: _payload gives CLI, docs, and frontend one stable report shape.
# 函数用途: 组合基础测试结果、产物验收结果和报告引用，保留 summary 便于旧调用方读取。
def _payload(
    *,
    ok: bool,
    report_path: Path,
    foundation: dict[str, object],
    artifacts: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "ok": ok,
        "report_ref": str(report_path),
        "summary": dict(foundation.get("summary", {}) if isinstance(foundation.get("summary"), dict) else {}),
        "foundation": foundation,
        "artifact_acceptance": artifacts,
    }


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
