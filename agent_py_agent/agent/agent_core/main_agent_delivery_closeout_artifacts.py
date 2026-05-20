# LLM: Delivery closeout artifact helpers validate machine-declared refs and persist report snapshots.
# 模块用途: 处理主代理交付合同中的产物路径、验收报告和 closeout.json 写入，不读取任务自然语言。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts.artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from ._runtime_params import ToolLoopExecuteParams

CLOSEOUT_DIR = ".agent_delivery"
CLOSEOUT_REPORT = "closeout.json"


# LLM: DeliveryContractValidationRequest keeps artifact validation inputs bundled and extensible.
# 类用途: 集中保存交付合同、必交产物、工作区和运行参数，避免 helper 参数继续增长。
@dataclass(frozen=True)
class DeliveryContractValidationRequest:
    contract: dict[str, Any]
    artifacts: list[dict[str, Any]]
    workspace_root: Path
    params: ToolLoopExecuteParams


# LLM: _required_artifacts keeps optional outputs from forcing deterministic closeout.
# 函数用途: 返回合同里 required 不为 false 的产物项，缺少列表时不触发收口。
def _required_artifacts(contract: dict[str, Any]) -> list[dict[str, Any]]:
    raw = contract.get("artifacts")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and item.get("required") is not False]


# LLM: _validate_contract_artifacts converts artifact refs into a single machine-readable delivery report.
# 函数用途: 对每个必交产物按结构化路径验收，汇总 ok、finding 和运行范围字段。
def _validate_contract_artifacts(request: DeliveryContractValidationRequest) -> dict[str, Any]:
    results = [_validate_artifact_item(item, request.workspace_root) for item in request.artifacts]
    return {
        "schema_version": "main_agent_delivery_closeout.v1",
        "ok": all(item["ok"] for item in results),
        "case_id": str(request.contract.get("case_id") or ""),
        "request_id": request.params.request_id,
        "run_id": request.params.run_id,
        "task_id": request.params.task_id,
        "workspace_root": str(request.workspace_root),
        "artifacts": results,
    }


# LLM: _existing_report reuses the prior closeout snapshot so repeated failures can be detected generically.
# 函数用途: 读取上一次 closeout.json；不存在或损坏时返回空对象，不让交付收口崩掉。
def _existing_report(workspace_root: Path) -> dict[str, Any]:
    path = workspace_root / CLOSEOUT_DIR / CLOSEOUT_REPORT
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _validate_artifact_item validates one contract artifact without reading natural-language acceptance prose.
# 函数用途: 解析 preferred_path/path，执行通用产物验收，并把报告转成稳定 JSON 字段。
def _validate_artifact_item(item: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    raw_path = str(item.get("preferred_path") or item.get("path") or "")
    path = _artifact_path(raw_path, workspace_root)
    if path is None:
        return _path_failure(item, raw_path, "ARTIFACT_PATH_INVALID")
    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=workspace_root,
            validation_contract=_validation_contract(item),
        )
    ).to_dict()
    return {
        "artifact_id": str(item.get("artifact_id") or ""),
        "kind": str(item.get("kind") or report.get("artifact_kind") or ""),
        "path": str(path),
        "ok": bool(report.get("ok")),
        "acceptance_report": report,
    }


# LLM: _artifact_path keeps contract paths bounded to the current workspace.
# 函数用途: 把相对路径落到 workspace_root 下；绝对路径必须仍位于 workspace_root 内。
def _artifact_path(raw_path: str, workspace_root: Path) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path).expanduser()
    path = candidate.resolve(strict=False) if candidate.is_absolute() else (workspace_root / candidate).resolve()
    try:
        path.relative_to(workspace_root)
    except ValueError:
        return None
    return path


# LLM: _path_failure gives missing or escaped artifact refs the same report shape as validator failures.
# 函数用途: 生成路径无效时的结构化产物验收结果，方便后续修复流程统一消费。
def _path_failure(item: dict[str, Any], raw_path: str, code: str) -> dict[str, Any]:
    finding = {
        "code": code,
        "severity": "hard",
        "message": "Artifact path is missing or outside the workspace.",
        "location": raw_path,
        "value": raw_path,
    }
    return {
        "artifact_id": str(item.get("artifact_id") or ""),
        "kind": str(item.get("kind") or ""),
        "path": raw_path,
        "ok": False,
        "acceptance_report": {"ok": False, "artifact_ref": raw_path, "artifact_kind": "", "findings": [finding]},
    }


# LLM: _validation_contract extracts machine-only artifact acceptance options from one contract item.
# 函数用途: 将 expected_artifacts 里的 validation_contract 传给底层验收器，不解析自然语言说明。
def _validation_contract(item: dict[str, Any]) -> dict[str, object]:
    value = item.get("validation_contract")
    return dict(value) if isinstance(value, dict) else {}


# LLM: _write_report persists the latest delivery check for audit and resume without embedding artifact bodies.
# 函数用途: 写 `.agent_delivery/closeout.json`，让真实 E2E 和用户排查能看到机器验收结果。
def _write_report(workspace_root: Path, report: dict[str, Any]) -> Path:
    path = workspace_root / CLOSEOUT_DIR / CLOSEOUT_REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


# LLM: _relative_report_ref keeps reports portable in stdout and test fixtures.
# 函数用途: 把 closeout 报告路径尽量显示为工作区相对路径。
def _relative_report_ref(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return str(path)
