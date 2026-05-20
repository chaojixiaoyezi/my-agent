# LLM: Real task execution file helpers keep config, command, and report writes deterministic.
# 模块用途: 为真实任务执行器提供路径、配置文件、命令 JSON 和 refs-first 写入工具。

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .main_agent_execution_contract_artifacts import (
    build_artifacts_with_path_contracts,
    build_bootstrap_contract,
    load_json_payload,
    prepare_artifact_workspace,
    write_json,
)
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
    prepare_artifact_workspace(delivery_contract_path, artifact_manifest_path)
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
    artifacts = load_json_payload(workspace / case.expected_artifacts_ref)
    acceptance = load_json_payload(workspace / case.acceptance_ref)
    payload = {
        "case_id": case.case_id,
        "expected_artifacts_ref": case.expected_artifacts_ref,
        "acceptance_ref": case.acceptance_ref,
        "artifact_path_manifest_ref": rel(task_workspace / ".my_agent_artifact_paths.json", workspace),
        "artifacts": build_artifacts_with_path_contracts(artifacts.get("artifacts", []), task_workspace),
        "acceptance": acceptance,
    }
    payload["bootstrap_contract"] = build_bootstrap_contract(payload["artifacts"])
    recovery = recovery_delivery_contract_payload(recovery_packet_path, workspace=workspace)
    if recovery:
        payload["recovery"] = recovery
    return payload


# LLM: write_case_config creates an isolated agent config for one real task.
# 函数用途: 从基础配置或仓库默认 agent_config 复制，并覆盖 workspace_root，保证真实任务继承正常模型后端。
def write_case_config(path: Path, base_config_path: Path | None, task_workspace: Path) -> None:
    if base_config_path:
        text = Path(base_config_path).expanduser().read_text(encoding="utf-8")
    else:
        text = default_config_text()
    text = without_config_key(text, "workspace_root")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{text.rstrip()}\nworkspace_root: "{task_workspace}"\n', encoding="utf-8")


# LLM: default_config_text reuses the repo default agent config so controlled runs match real runtime defaults.
# 函数用途: 读取仓库默认 agent_config.yaml；只有读不到时才回退到最小 anthropic_compatible 配置。
def default_config_text() -> str:
    config_path = Path(__file__).resolve().parents[2] / "config" / "agent_config.yaml"
    try:
        return config_path.read_text(encoding="utf-8")
    except OSError:
        return "\n".join(
            [
                'agent_name: "real-task-suite"',
                'model_backend: "anthropic_compatible"',
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
