# LLM: Shared main-agent task helpers keep task and real_task revalidation on one structured core.
# 模块用途: 抽出双轨复验共用的报告读取、路径解析、expected_artifacts ref 和状态摘要逻辑。

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state_machine import normalize_status

_CASE_FILE_NAMES = {
    "config": "config.yaml",
    "command": "command.json",
    "delivery_contract": "delivery_contract.json",
    "stdout": "stdout.txt",
    "stderr": "stderr.txt",
    "acceptance_report": "acceptance_report.json",
    "recovery_packet": "recovery_packet.json",
    "events": "events.jsonl",
}
_CASE_REF_KEYS = {
    "config_ref": "config",
    "command_ref": "command",
    "stdout_ref": "stdout",
    "stderr_ref": "stderr",
    "acceptance_report_ref": "acceptance_report",
    "events_ref": "events",
}


# LLM: InvalidRevalidationReportRequest bundles synthetic failure report construction facts.
# 类用途: 保存 report 路径、workspace、错误码和轨道模型，避免共用函数参数继续扩张。
@dataclass(frozen=True)
class InvalidRevalidationReportRequest:
    report_path: Path
    workspace: Path
    error: dict[str, str]
    schema_version: str
    case_model: Callable[..., Any]
    report_model: Callable[..., Any]


# LLM: ResolveCasePathsRequest bundles one case path lookup request.
# 类用途: 保存 item refs、workspace、case_id 和兼容执行根，保证路径解析只读结构化字段。
@dataclass(frozen=True)
class ResolveCasePathsRequest:
    item: dict[str, object]
    workspace: Path
    case_id: str
    primary_paths: dict[str, Path]
    report_path: Path
    compatible_execution_roots: tuple[str, ...]


# LLM: read_report_payload turns corrupt report files into structured machine errors.
# 函数用途: 读取 execution_report.json；坏 JSON/非对象报告不抛原始异常。
def read_report_payload(report_path: Path) -> tuple[dict[str, object], dict[str, str] | None]:
    try:
        payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, {"code": "REVALIDATION_REPORT_INVALID_JSON", "message": exc.msg}
    except OSError as exc:
        return {}, {"code": "REVALIDATION_REPORT_UNREADABLE", "message": str(exc)}
    if not isinstance(payload, dict):
        return {}, {"code": "REVALIDATION_REPORT_NOT_OBJECT", "message": "execution report must be a JSON object"}
    return payload, None


# LLM: build_invalid_revalidation_report preserves each track's public model while sharing failure facts.
# 函数用途: 用同一 synthetic failed case 承载读报告失败码，避免双轨坏 JSON 行为分叉。
def build_invalid_revalidation_report(request: InvalidRevalidationReportRequest) -> Any:
    case = request.case_model(
        case_id="__report__",
        title="Invalid execution report",
        status="FAILED",
        worker_slot=0,
        timeout_seconds=0,
        prompt_ref="",
        config_ref="",
        command_ref="",
        stdout_ref="",
        stderr_ref="",
        acceptance_report_ref="",
        events_ref="",
        acceptance_summary={"passed": 0, "failed": 1},
        issues=[str(request.error.get("code") or "REVALIDATION_REPORT_INVALID")],
    )
    return request.report_model(
        ok=False,
        schema_version=request.schema_version,
        execution_mode="revalidate",
        summary=status_summary([case]),
        concurrency={"requested_max_workers": 1, "effective_max_workers": 1, "case_count": 1},
        suite_report_ref="",
        report_ref=relative_ref(request.report_path, request.workspace),
        cases=[case],
    )


# LLM: payload_cases returns only structured case records from a stored report.
# 函数用途: 容忍旧报告字段缺失或混入非对象 case，复验入口只处理机器对象。
def payload_cases(payload: object) -> list[dict[str, object]]:
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        return []
    return [dict(item) for item in cases if isinstance(item, dict)]


# LLM: payload_concurrency preserves old concurrency metadata during revalidation.
# 函数用途: 复验报告沿用已有并发事实；旧报告没有该字段时生成最小摘要。
def payload_concurrency(payload: object, case_count: int) -> dict[str, int]:
    value = payload.get("concurrency") if isinstance(payload, dict) else None
    if isinstance(value, dict):
        return {str(key): int(raw) for key, raw in value.items() if isinstance(raw, int)}
    return {"requested_max_workers": 1, "effective_max_workers": 1, "case_count": case_count}


# LLM: expected_artifacts_ref resolves the artifact contract from structured refs only.
# 函数用途: 优先使用报告里的 expected_artifacts_ref，再从 prompt_ref 同目录推导，最后按 case_id/schema fallback。
def expected_artifacts_ref(item: dict[str, object], *, suite_root_ref: str) -> str:
    direct_ref = _item_ref(item, "expected_artifacts_ref")
    if direct_ref:
        return direct_ref
    prompt_ref = _item_ref(item, "prompt_ref")
    prompt_path = Path(prompt_ref)
    if prompt_path.name == "prompt.md":
        return str(prompt_path.with_name("expected_artifacts.json"))
    case_id = str(item.get("case_id") or "")
    return f"{suite_root_ref}/tasks/{case_id}/expected_artifacts.json"


# LLM: resolve_case_paths uses report refs, report path, and compatible roots before falling back to the track default.
# 函数用途: 只根据结构化 item refs、case_id 和执行目录 schema 定位 case 文件，不解析 prompt 文本。
def resolve_case_paths(request: ResolveCasePathsRequest) -> dict[str, Path]:
    candidates = [
        *_case_paths_from_item_refs(request.item, workspace=request.workspace, case_id=request.case_id),
        _case_paths_for_root(Path(request.report_path).parent / "tasks" / request.case_id),
        request.primary_paths,
    ]
    candidates.extend(
        _case_paths_for_root(request.workspace / root_ref / "tasks" / request.case_id)
        for root_ref in request.compatible_execution_roots
    )
    for paths in candidates:
        if _case_paths_exist(paths):
            return paths
    return request.primary_paths


# LLM: case_issues creates short issue codes from process and artifact facts.
# 函数用途: 复验时重算产物失败摘要；详细 findings 仍在 acceptance_report_ref。
def case_issues(exit_code: int, summary: dict[str, int]) -> list[str]:
    issues: list[str] = []
    failed = int(summary.get("failed", 0))
    if exit_code == 124 and not failed:
        return ["process_timeout_after_valid_artifact"]
    if exit_code != 0:
        issues.append(f"exit_code={exit_code}")
    if failed:
        issues.append(f"artifact_acceptance_failed={failed}")
    return issues


# LLM: optional_int keeps restored exit_code compatible with JSON null values.
# 函数用途: 从旧报告恢复可选退出码，非整数值返回 None。
def optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


# LLM: status_summary counts planned and executed case statuses for CLI display.
# 函数用途: 汇总复验报告状态，和执行报告保持同一 summary shape。
def status_summary(cases: list[object]) -> dict[str, int]:
    statuses = [normalize_status(_case_status(case)) for case in cases]
    planning = sum(status == "PLANNING" for status in statuses)
    done = sum(status == "DONE" for status in statuses)
    return {
        "total": len(cases),
        "planning": planning,
        "planned": planning,
        "done": done,
        "completed": done,
        "failed": sum(status == "FAILED" for status in statuses),
    }


# LLM: relative_ref stores portable refs within the selected workspace.
# 函数用途: 把绝对路径转成相对工作区引用，避免报告绑定某台机器的路径。
def relative_ref(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


# LLM: _case_status keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _case_status(case: object) -> str:
    if isinstance(case, dict):
        return str(case.get("status") or "")
    return str(getattr(case, "status", ""))


# LLM: _item_ref keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _item_ref(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    return str(value).strip() if isinstance(value, str) else ""


# LLM: _case_paths_from_item_refs keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _case_paths_from_item_refs(
    item: dict[str, object],
    *,
    workspace: Path,
    case_id: str,
) -> list[dict[str, Path]]:
    paths: list[dict[str, Path]] = []
    for ref_key, path_key in _CASE_REF_KEYS.items():
        ref = _item_ref(item, ref_key)
        if not ref:
            continue
        path = _resolve_ref(ref, workspace)
        if path.name != _CASE_FILE_NAMES[path_key]:
            continue
        root = path.parent
        if root.name == case_id and root.parent.name == "tasks":
            paths.append(_case_paths_for_root(root))
    return paths


# LLM: _resolve_ref keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _resolve_ref(ref: str, workspace: Path) -> Path:
    path = Path(ref).expanduser()
    return path if path.is_absolute() else workspace / path


# LLM: _case_paths_for_root keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _case_paths_for_root(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "workspace": root / "workspace",
        **{key: root / filename for key, filename in _CASE_FILE_NAMES.items()},
    }


# LLM: _case_paths_exist keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _case_paths_exist(paths: dict[str, Path]) -> bool:
    return paths["workspace"].exists() or paths["root"].exists()


__all__ = [
    "InvalidRevalidationReportRequest",
    "ResolveCasePathsRequest",
    "build_invalid_revalidation_report",
    "case_issues",
    "expected_artifacts_ref",
    "optional_int",
    "payload_cases",
    "payload_concurrency",
    "read_report_payload",
    "relative_ref",
    "resolve_case_paths",
    "status_summary",
]
