# LLM: Main-agent delivery closeout stops productive real tasks once machine contracts pass.
# 模块用途: 工具轮后读取机器交付合同、验收产物并写收口报告，避免主代理产物已合格还继续跑到超时。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..backends import ModelResponse
from ..contracts.artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from ..tooling.file_write_session_inspection import open_file_write_sessions
from ._runtime_params import ToolLoopExecuteParams

CLOSEOUT_DIR = ".agent_delivery"
CLOSEOUT_REPORT = "closeout.json"


# LLM: MainAgentDeliveryCloseoutRequest bundles post-tool-loop state for contract validation.
# 类用途: 保存主代理、工具循环参数和后端名，供交付收口逻辑在不扩散参数的情况下运行。
@dataclass(frozen=True)
class MainAgentDeliveryCloseoutRequest:
    agent: object
    params: ToolLoopExecuteParams
    backend: str


# LLM: DeliveryContractValidationRequest keeps artifact validation inputs bundled and extensible.
# 类用途: 集中保存交付合同、必交产物、工作区和运行参数，避免 helper 参数继续增长。
@dataclass(frozen=True)
class DeliveryContractValidationRequest:
    contract: dict[str, Any]
    artifacts: list[dict[str, Any]]
    workspace_root: Path
    params: ToolLoopExecuteParams


# LLM: main_agent_delivery_closeout_response returns a deterministic final response only after all required refs pass.
# 函数用途: 根据结构化 delivery_contract 验收必交产物；通过则停止工具循环，失败则写结构化反馈让模型修复。
def main_agent_delivery_closeout_response(request: MainAgentDeliveryCloseoutRequest) -> ModelResponse | None:
    contract = _delivery_contract(request.params)
    artifacts = _required_artifacts(contract)
    if not artifacts:
        return None
    workspace_root = _workspace_root(request.agent)
    if open_sessions := open_file_write_sessions(workspace_root):
        _append_open_session_context(request.params, open_sessions)
        return None
    report = _validate_contract_artifacts(
        DeliveryContractValidationRequest(
            contract=contract,
            artifacts=artifacts,
            workspace_root=workspace_root,
            params=request.params,
        )
    )
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    _write_report(workspace_root, report)
    if not report["ok"]:
        _append_failed_contract_context(request.params, report)
        return None
    return ModelResponse(text=_closeout_text(report), backend=request.backend)


# LLM: _delivery_contract reads machine contracts from runtime fields, never from prompt prose.
# 函数用途: 优先读取 ToolLoopExecuteParams.delivery_contract；兼容读取 task_attributes.delivery_contract。
def _delivery_contract(params: ToolLoopExecuteParams) -> dict[str, Any]:
    value = params.delivery_contract
    if isinstance(value, dict):
        return dict(value)
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    value = attrs.get("delivery_contract")
    return dict(value) if isinstance(value, dict) else {}


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


# LLM: _append_failed_contract_context feeds structured repair facts into the next model turn.
# 函数用途: 验收失败时追加 JSON 反馈，不把自然语言说明当机器事实。
def _append_failed_contract_context(params: ToolLoopExecuteParams, report: dict[str, Any]) -> None:
    params.tool_context.append(
        "[delivery-contract-check]\n"
        + json.dumps(
            {
                "ok": False,
                "report_ref": report.get("report_ref", ""),
                "failed_artifacts": [item for item in report["artifacts"] if not item["ok"]],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


# LLM: _append_open_session_context blocks delivery completion from open file-write manifests.
# 函数用途: 有未 finish/abort 的分块写入时，只追加结构化 session 事实，让下一轮先处理这些会话。
def _append_open_session_context(params: ToolLoopExecuteParams, sessions: list[dict[str, Any]]) -> None:
    params.tool_context.append(
        "[delivery-contract-open-file-write-sessions]\n"
        + json.dumps(
            {
                "ok": False,
                "reason": "open_file_write_sessions",
                "open_file_write_sessions": sessions,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


# LLM: _closeout_text makes the final message copyable while keeping the machine payload explicit.
# 函数用途: 生成主代理完成响应，告知上层工具循环不用再请求下一轮模型。
def _closeout_text(report: dict[str, Any]) -> str:
    payload = {
        "ok": True,
        "case_id": report.get("case_id", ""),
        "report_ref": report.get("report_ref", ""),
        "artifacts": [
            {
                "artifact_id": item["artifact_id"],
                "kind": item["kind"],
                "path": item["path"],
                "ok": item["ok"],
            }
            for item in report["artifacts"]
        ],
    }
    return (
        "[MAIN_AGENT_DELIVERY_COMPLETE]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n[/MAIN_AGENT_DELIVERY_COMPLETE]\n结构化交付合同已通过，主代理停止继续工具循环。"
    )


# LLM: _workspace_root reads the same tool registry root used by file tools.
# 函数用途: 获取主代理真实工作区；测试替身缺工具时退回 agent.root。
def _workspace_root(agent: object) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).expanduser().resolve()


# LLM: _relative_report_ref keeps reports portable in stdout and test fixtures.
# 函数用途: 把 closeout 报告路径尽量显示为工作区相对路径。
def _relative_report_ref(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return str(path)
