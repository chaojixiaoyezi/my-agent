# LLM: Real task execution file helpers keep config, command, and report writes deterministic.
# 模块用途: 为真实任务执行器提供路径、配置文件、命令 JSON 和 refs-first 写入工具。

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .main_agent_real_task_execution_models import MainAgentRealTaskExecutionRequest
from .main_agent_real_task_recovery_reconcile import reconcile_recovery_open_write_sessions
from .main_agent_real_task_recovery_resume import recovery_delivery_contract_payload
from .main_agent_real_task_suite import MainAgentRealTaskCasePlan


# LLM: command_for_case builds an argv list and never shells through natural language.
# 函数用途: 根据 prompt ref、交付合同和隔离配置生成 `python -m agent_py_agent ... run` 命令。
def command_for_case(
    case: MainAgentRealTaskCasePlan,
    request: MainAgentRealTaskExecutionRequest,
    *,
    config_path: Path,
    workspace: Path,
    delivery_contract_path: Path | None = None,
) -> list[str]:
    prompt = prompt_for_case(case, workspace=workspace)
    delivery_contract_path = delivery_contract_path or case_paths(workspace, case.case_id)["delivery_contract"]
    task_workspace = case_paths(workspace, case.case_id)["workspace"]
    artifact_manifest_path = task_workspace / ".my_agent_artifact_paths.json"
    recovery_reconciliation = reconcile_recovery_open_write_sessions(
        request.recovery_packet_path,
        workspace=workspace,
    )
    delivery_contract = delivery_contract_for_case(
        case,
        workspace=workspace,
        recovery_packet_path=request.recovery_packet_path,
    )
    if recovery_reconciliation:
        delivery_contract["recovery_reconciliation"] = recovery_reconciliation
    write_json(
        delivery_contract_path,
        delivery_contract,
    )
    _prepare_artifact_workspace(delivery_contract_path, task_workspace, artifact_manifest_path)
    return [
        sys.executable,
        "-m",
        "agent_py_agent",
        "--config",
        str(config_path),
        "run",
        prompt,
        "--delivery-contract-file",
        str(delivery_contract_path),
        "--save",
    ]


# LLM: prompt_for_case returns only the user-facing task text.
# 函数用途: 读取真实任务自然语言 prompt；机器交付合同通过 `--delivery-contract-file` 单独传递。
def prompt_for_case(case: MainAgentRealTaskCasePlan, *, workspace: Path) -> str:
    return (workspace / case.prompt_ref).read_text(encoding="utf-8")


# LLM: delivery_contract_for_case builds the machine contract outside prompt prose.
# 函数用途: 从 suite refs 生成结构化交付合同，供 CLI RunParams.delivery_contract 读取。
def delivery_contract_for_case(
    case: MainAgentRealTaskCasePlan,
    *,
    workspace: Path,
    recovery_packet_path: Path | None = None,
) -> dict[str, object]:
    task_workspace = case_paths(workspace, case.case_id)["workspace"]
    artifacts = _json_payload(workspace / case.expected_artifacts_ref)
    acceptance = _json_payload(workspace / case.acceptance_ref)
    payload = {
        "case_id": case.case_id,
        "expected_artifacts_ref": case.expected_artifacts_ref,
        "acceptance_ref": case.acceptance_ref,
        "artifact_path_manifest_ref": rel(task_workspace / ".my_agent_artifact_paths.json", workspace),
        "artifacts": _artifacts_with_path_contracts(artifacts.get("artifacts", []), task_workspace),
        "acceptance": acceptance,
    }
    recovery = recovery_delivery_contract_payload(recovery_packet_path, workspace=workspace)
    if recovery:
        payload["recovery"] = recovery
    return payload


# LLM: _artifacts_with_path_contracts binds artifact refs to exact task-workspace paths.
# 函数用途: 给 expected artifacts 增加 resolved path 和 checkpoint path 合同，避免模型从旧目录猜输出位置。
def _artifacts_with_path_contracts(items: object, task_workspace: Path) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    return [_artifact_with_path_contract(dict(item), task_workspace) for item in items if isinstance(item, dict)]


# LLM: _artifact_with_path_contract preserves existing artifact fields while adding machine path facts.
# 函数用途: 将 preferred_path 解析成 workspace 内绝对路径，并补齐 staging checkpoint 的绝对路径。
def _artifact_with_path_contract(item: dict[str, object], task_workspace: Path) -> dict[str, object]:
    preferred = str(item.get("preferred_path") or item.get("path") or "")
    resolved = _workspace_path(task_workspace, preferred)
    item["path_contract"] = {
        "workspace_relative_path": preferred,
        "resolved_path": str(resolved),
        "parent_dir": str(resolved.parent),
        "artifact_root": _root_segment(preferred),
    }
    contract = item.get("validation_contract")
    if isinstance(contract, dict):
        item["validation_contract"] = _validation_contract_with_checkpoint_paths(contract, task_workspace)
    return item


# LLM: _validation_contract_with_checkpoint_paths resolves staging refs without changing validator semantics.
# 函数用途: 给 staging_contract.checkpoint_refs 补充 checkpoint_paths，真实执行和恢复都读结构化路径。
def _validation_contract_with_checkpoint_paths(contract: dict[str, object], task_workspace: Path) -> dict[str, object]:
    updated = dict(contract)
    staging = updated.get("staging_contract")
    if not isinstance(staging, dict):
        return updated
    checkpoint_refs = staging.get("checkpoint_refs")
    if not isinstance(checkpoint_refs, list):
        return updated
    staging = dict(staging)
    staging["checkpoint_paths"] = [
        _checkpoint_path_contract(task_workspace, str(ref))
        for ref in checkpoint_refs
        if str(ref).strip()
    ]
    updated["staging_contract"] = staging
    return updated


# LLM: _checkpoint_path_contract keeps staged deliverables refs-first and workspace-bound.
# 函数用途: 为一个阶段产物生成相对路径、绝对路径和父目录三元组。
def _checkpoint_path_contract(task_workspace: Path, ref: str) -> dict[str, str]:
    resolved = _workspace_path(task_workspace, ref)
    return {
        "workspace_relative_path": ref,
        "resolved_path": str(resolved),
        "parent_dir": str(resolved.parent),
    }


# LLM: _prepare_artifact_workspace materializes path contracts before the model starts.
# 函数用途: 预创建产物父目录并写 manifest，让真实任务从一开始就有稳定输出根。
def _prepare_artifact_workspace(delivery_contract_path: Path, task_workspace: Path, manifest_path: Path) -> None:
    payload = _json_payload(delivery_contract_path)
    entries = _artifact_manifest_entries(payload)
    for entry in entries:
        Path(str(entry["parent_dir"])).mkdir(parents=True, exist_ok=True)
    write_json(manifest_path, {"schema_version": "artifact-path-manifest.v1", "artifacts": entries})


# LLM: _artifact_manifest_entries extracts resolved artifact and checkpoint paths from one contract.
# 函数用途: 生成单独 manifest，便于工具、恢复和人工审计统一查看目标路径。
def _artifact_manifest_entries(payload: dict[str, object]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for artifact in payload.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        path_contract = artifact.get("path_contract")
        if isinstance(path_contract, dict):
            entries.append({key: str(path_contract.get(key) or "") for key in _PATH_CONTRACT_KEYS})
        contract = artifact.get("validation_contract")
        staging = contract.get("staging_contract") if isinstance(contract, dict) else None
        if isinstance(staging, dict):
            entries.extend(_checkpoint_manifest_entries(staging.get("checkpoint_paths")))
    return entries


# LLM: _checkpoint_manifest_entries normalizes staged path contracts for the manifest.
# 函数用途: 收集 source_data/build_script/final_artifact 等阶段路径，统一预创建父目录。
def _checkpoint_manifest_entries(items: object) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    entries = []
    for item in items:
        if isinstance(item, dict):
            entries.append({key: str(item.get(key) or "") for key in _CHECKPOINT_CONTRACT_KEYS})
    return entries


# LLM: _workspace_path resolves a structured relative ref under the task workspace.
# 函数用途: 解析产物路径并拒绝 `..` 逃逸，保持真实任务只能写入自己的 workspace。
def _workspace_path(task_workspace: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = candidate if candidate.is_absolute() else (task_workspace / candidate).resolve()
    try:
        resolved.relative_to(task_workspace.resolve())
    except ValueError:
        return (task_workspace / "_invalid_artifact_path").resolve()
    return resolved


# LLM: _root_segment records the first relative path segment for output-root diagnostics.
# 函数用途: 提供 artifact_root 元数据，帮助后续 UI/恢复识别 outputs、reports 等产物区。
def _root_segment(value: str) -> str:
    parts = Path(value).parts
    return str(parts[0]) if parts and parts[0] not in {"", "."} else ""


# LLM: _json_payload reads small suite contracts generated by the planner.
# 函数用途: 读取 expected_artifacts/acceptance JSON；失败时返回空对象并让后续验收失败。
def _json_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: write_case_config creates an isolated agent config for one real task.
# 函数用途: 从基础配置复制或生成 echo 配置，并覆盖 workspace_root，保证任务写入专属目录。
def write_case_config(path: Path, base_config_path: Path | None, task_workspace: Path) -> None:
    if base_config_path:
        text = Path(base_config_path).expanduser().read_text(encoding="utf-8")
    else:
        text = default_config_text()
    text = without_config_key(text, "workspace_root")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{text.rstrip()}\nworkspace_root: "{task_workspace}"\n', encoding="utf-8")


# LLM: default_config_text keeps plan-only and echo tests independent from user secrets.
# 函数用途: 生成最小离线配置；真实 API 测试应显式传 base_config_path。
def default_config_text() -> str:
    return "\n".join(
        [
            'agent_name: "real-task-suite"',
            'model_backend: "echo"',
            'system_prompt: "你是受控真实任务测试里的主代理。"',
            "prompt_files: []",
            "auto_save_memory: false",
            "enable_subagents: true",
        ]
    )


# LLM: without_config_key removes one top-level simple YAML key before appending overrides.
# 函数用途: 删除基础配置里的 workspace_root，避免同一文件里出现多个冲突工作区。
def without_config_key(text: str, key: str) -> str:
    prefix = f"{key}:"
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith(prefix))


# LLM: case_paths returns all per-case runtime paths in the workspace.
# 函数用途: 集中定义每个真实任务的 workspace/config/log/command 文件位置。
def case_paths(workspace: Path, case_id: str) -> dict[str, Path]:
    root = execution_root(workspace) / "tasks" / case_id
    return {
        "root": root,
        "workspace": root / "workspace",
        "config": root / "config.yaml",
        "command": root / "command.json",
        "delivery_contract": root / "delivery_contract.json",
        "stdout": root / "stdout.txt",
        "stderr": root / "stderr.txt",
        "acceptance_report": root / "acceptance_report.json",
        "recovery_packet": root / "recovery_packet.json",
        "events": root / "events.jsonl",
    }


# LLM: execution_root keeps execution artifacts separate from suite prompt/contracts.
# 函数用途: 返回真实任务执行记录根目录，避免污染计划目录和用户产物目录。
def execution_root(workspace: Path) -> Path:
    return workspace / "main_agent_real_task_execution"


# LLM: package_root resolves where `python -m agent_py_agent` should be launched.
# 函数用途: 解析包根目录；测试可传入 Path.cwd，默认按当前文件向上定位仓库根。
def package_root(request: MainAgentRealTaskExecutionRequest) -> Path:
    if request.package_root:
        return Path(request.package_root).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


# LLM: write_json centralizes deterministic UTF-8 JSON writing for execution records.
# 函数用途: 写命令和总报告文件，字段排序便于 diff、恢复和审计。
def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# LLM: append_event writes one bounded machine event for real task observability.
# 函数用途: 给每个 case 的 events.jsonl 追加结构化事件，方便长任务运行中只读观察。
def append_event(path: Path, event_type: str, payload: dict[str, object] | None = None) -> None:
    event = {
        "event_type": event_type,
        "timestamp_unix": round(time.time(), 3),
        "payload": dict(payload or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: rel stores portable refs within the selected workspace.
# 函数用途: 把绝对路径转成相对工作区引用，避免报告绑定某台机器的路径。
def rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


_PATH_CONTRACT_KEYS = ("workspace_relative_path", "resolved_path", "parent_dir", "artifact_root")
_CHECKPOINT_CONTRACT_KEYS = ("workspace_relative_path", "resolved_path", "parent_dir")


__all__ = [
    "case_paths",
    "command_for_case",
    "delivery_contract_for_case",
    "execution_root",
    "append_event",
    "package_root",
    "prompt_for_case",
    "rel",
    "write_case_config",
    "write_json",
]
