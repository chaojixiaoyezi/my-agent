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
from .main_agent_task_execution_models import MainAgentTaskExecutionRequest
from .main_agent_task_recovery_reconcile import reconcile_recovery_open_write_sessions
from .main_agent_task_recovery_resume import recovery_delivery_contract_payload
from .main_agent_task_suite import MainAgentTaskCasePlan

_RUNTIME_ISOLATION_KEYS = (
    "workspace_root",
    "my_agent_home",
    "memory_path",
    "local_store_path",
    "local_store_files_dir",
    "local_store_events_path",
    "subagent_workspace",
    "gateway_workspace",
    "adapter_workspace",
)
_DIRECT_REPAIR_ACTIONS = {
    "invoke_builder_tool",
    "repair_evidence_refs",
    "repair_structured_checkpoint_json",
    "write_non_empty_structured_rows",
}


# LLM: command_for_case builds an argv list and never shells through natural language.
# 函数用途: 根据 prompt ref、交付合同和隔离配置生成 `python -m agent_py_agent ... run` 命令。
def command_for_case(
    case: MainAgentTaskCasePlan,
    request: MainAgentTaskExecutionRequest,
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
    _write_recovery_attempt_marker(
        task_workspace,
        request.recovery_packet_path,
        workspace=workspace,
        delivery_contract_path=delivery_contract_path,
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
def prompt_for_case(case: MainAgentTaskCasePlan, *, workspace: Path) -> str:
    return (workspace / case.prompt_ref).read_text(encoding="utf-8")


# LLM: delivery_contract_for_case builds the machine contract outside prompt prose.
# 函数用途: 从 suite refs 生成结构化交付合同，供 CLI RunParams.delivery_contract 读取。
def delivery_contract_for_case(
    case: MainAgentTaskCasePlan,
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
    text = without_config_keys(text, _RUNTIME_ISOLATION_KEYS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text.rstrip()}\n{isolated_runtime_config(task_workspace)}\n", encoding="utf-8")


# LLM: Recovery attempts get a machine marker so guards can distinguish a new attempt from stale closeout debt.
# 函数用途: 续跑开始时写结构化 attempt 标记；repair guard 用它给新 attempt 一个有限检查窗口。
def _write_recovery_attempt_marker(
    task_workspace: Path,
    packet_path: Path | None,
    *,
    workspace: Path,
    delivery_contract_path: Path,
) -> None:
    if packet_path is None:
        return
    marker = task_workspace / ".agent_delivery" / "recovery_attempt.json"
    write_json(
        marker,
        {
            "schema_version": "delivery-recovery-attempt.v1",
            "packet_ref": rel(Path(packet_path).expanduser().resolve(), workspace),
            "delivery_contract_ref": rel(delivery_contract_path, workspace),
            **_recovery_attempt_baseline(task_workspace),
            "inspection_round_budget": recovery_attempt_inspection_budget(task_workspace),
            "started_at_unix": round(time.time(), 3),
        },
    )


# LLM: recovery_attempt_inspection_budget is derived from structured closeout actions, not task wording.
# 函数用途: 直接写入/构建类恢复不给额外读取窗口；仍需定位缺失产物时才保留小检查预算。
def recovery_attempt_inspection_budget(task_workspace: Path) -> int:
    progress = _closeout_progress(task_workspace)
    actions = progress.get("recovery_actions") if isinstance(progress, dict) else None
    action_rows = [item for item in actions if isinstance(item, dict)] if isinstance(actions, list) else []
    if any(_requires_direct_repair(item, task_workspace) for item in action_rows):
        return 0
    return 2 if action_rows else 4


# LLM: _recovery_attempt_baseline keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _recovery_attempt_baseline(task_workspace: Path) -> dict[str, object]:
    progress_payload = _closeout_progress(task_workspace)
    return {
        "baseline_failure_fingerprint": str(progress_payload.get("failure_fingerprint") or ""),
        "baseline_unchanged_failure_count": _safe_int(progress_payload.get("unchanged_failure_count")),
    }


# LLM: _closeout_progress reads only the machine delivery_progress envelope.
# 函数用途: 从 closeout.json 提取结构化恢复状态；坏文件按空状态处理。
def _closeout_progress(task_workspace: Path) -> dict[str, object]:
    closeout = task_workspace / ".agent_delivery" / "closeout.json"
    try:
        payload = json.loads(closeout.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    progress = payload.get("delivery_progress") if isinstance(payload, dict) else {}
    return progress if isinstance(progress, dict) else {}


# LLM: _requires_direct_repair identifies recovery actions that should mutate before inspecting again.
# 函数用途: 根据 recommended_action、artifact_path 和 finding_codes 的结构化字段计算恢复窗口。
def _requires_direct_repair(action: dict[str, object], task_workspace: Path) -> bool:
    recommended = str(action.get("recommended_action") or "").strip()
    if recommended in _DIRECT_REPAIR_ACTIONS:
        return True
    if recommended != "repair_artifact_against_findings":
        return False
    if _artifact_missing_only(action):
        return False
    return _action_target_exists(action, task_workspace)


# LLM: _artifact_missing_only keeps missing-artifact lookup separate from invalid-artifact repair.
# 函数用途: 只有纯 ARTIFACT_MISSING 才允许续跑先查找；结构错误、DOM 缺失等都要求写入修复。
def _artifact_missing_only(action: dict[str, object]) -> bool:
    codes = action.get("finding_codes")
    values = {str(code) for code in codes if str(code)} if isinstance(codes, list) else set()
    return bool(values) and values.issubset({"ARTIFACT_MISSING"})


# LLM: _action_target_exists resolves structured refs inside the task workspace.
# 函数用途: 判断当前恢复对象是否已经有本地目标，避免把已存在但坏的产物继续当成“先找找看”。
def _action_target_exists(action: dict[str, object], task_workspace: Path) -> bool:
    for key in ("artifact_path", "checkpoint_ref", "output_ref", "source_ref"):
        ref = str(action.get(key) or "").strip()
        if ref and _resolve_action_ref(ref, task_workspace).exists():
            return True
    return False


# LLM: _resolve_action_ref normalizes absolute and workspace-relative refs.
# 函数用途: 将 closeout 里的结构化路径解析成 Path，不解析自然语言描述。
def _resolve_action_ref(ref: str, task_workspace: Path) -> Path:
    path = Path(ref).expanduser()
    return path if path.is_absolute() else task_workspace / path


# LLM: _safe_int keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


# LLM: without_config_keys removes top-level simple YAML keys before appending scoped overrides.
# 函数用途: 删除会跨 case 串状态的运行时目录配置，避免同一文件里出现多个冲突工作区。
def without_config_keys(text: str, keys: tuple[str, ...]) -> str:
    prefixes = tuple(f"{key}:" for key in keys)
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith(prefixes)
    )


# LLM: isolated_runtime_config binds every controlled task subprocess to its own state home.
# 函数用途: 生成真实任务 case 的运行目录覆盖，避免多个主代理并发时共享 memory/gateway/subagent 状态。
def isolated_runtime_config(task_workspace: Path) -> str:
    task_root = _posix(task_workspace)
    home = _posix(task_workspace / ".my_agent" / "home")
    return "\n".join(
        [
            f'workspace_root: "{task_root}"',
            f'my_agent_home: "{home}"',
            'memory_path: ".my_agent/memory.jsonl"',
            'local_store_path: ".my_agent/local_store/local.db"',
            'local_store_files_dir: ".my_agent/local_store/files"',
            'local_store_events_path: ".my_agent/local_store/events.jsonl"',
            'subagent_workspace: ".my_agent/subagents"',
            'gateway_workspace: ".my_agent/gateway"',
            'adapter_workspace: ".my_agent/adapters/file"',
        ]
    )


# LLM: _posix keeps generated YAML paths stable across macOS/Linux/Windows-style workspaces.
# 函数用途: 输出配置路径字符串，不影响真实 Path 边界校验。
def _posix(path: Path) -> str:
    return str(Path(path).expanduser()).replace("\\", "/")


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
    return workspace / "main_agent_task_execution"


# LLM: package_root resolves where `python -m agent_py_agent` should be launched.
# 函数用途: 解析包根目录；测试可传入 Path.cwd，默认按当前文件向上定位仓库根。
def package_root(request: MainAgentTaskExecutionRequest) -> Path:
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
    "isolated_runtime_config",
    "without_config_keys",
]
