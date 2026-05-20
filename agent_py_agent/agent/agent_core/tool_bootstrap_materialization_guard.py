# LLM: bootstrap materialization guard keeps the very first stage of contract execution from stalling in pure inspection loops.
# 模块用途: 当 bootstrap_contract 的结构化目标一个都还没真实出现时，阻止模型连续只做检查或抓取而不先落最小骨架。

from __future__ import annotations

import json
from pathlib import Path

from ..backend import ModelResponse
from ._runtime_params import ToolLoopExecuteParams

_STATE_DIR = ".agent_delivery"
_STATE_FILE = "bootstrap_materialization_guard.json"
_EXPLORATION_BLOCK_THRESHOLD = 6
_REPEATED_EXPLORATION_BLOCK_THRESHOLD = 4
_BOOTSTRAP_PRODUCTIVE_TOOLS = {
    "append_file",
    "file_write_session",
    "replace_in_file",
    "run_command",
    "write_file",
}
_RUN_COMMAND_INSPECTION_PREFIXES = ("find ", "ls", "pwd")


# LLM: bootstrap_materialization_context exposes structured startup targets when the workspace has not materialized any of them yet.
# 函数用途: 若 bootstrap_contract 里所有目标都还缺失，则给下一轮模型一段结构化开工合同，要求先让至少一个目标文件出现。
def bootstrap_materialization_context(agent: object, params: ToolLoopExecuteParams, repairs: int) -> str:
    payload = _bootstrap_payload(agent, params)
    state = _load_state(agent)
    if not payload or _should_block(state):
        return ""
    return "\n".join(
        [
            "[tool-system bootstrap-materialization]",
            json.dumps(
                {
                    **payload,
                    "exploration_rounds_without_materialization": int(
                        state.get("exploration_rounds_without_materialization") or 0
                    ),
                    "repeated_exploration_count": int(state.get("repeated_exploration_count") or 0),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "你还处在开工阶段，bootstrap_contract 里的目标一个都没真实出现。下一轮必须先让至少一个 target 文件或目录出现，"
            "例如创建目录、写最小骨架文件，之后再继续抓取、分析或整理。",
        ]
    )


# LLM: bootstrap_materialization_block_response deterministically stops startup loops that ignored repeated materialization redirects.
# 函数用途: 模型多次忽略“先物化一个目标”的结构化要求时，返回明确阻断，避免在开工阶段空转。
def bootstrap_materialization_block_response(agent: object, params: ToolLoopExecuteParams) -> ModelResponse | None:
    if not _bootstrap_payload(agent, params) or not _should_block(_load_state(agent)):
        return None
    return ModelResponse(
        text="[BOOTSTRAP_MATERIALIZATION_BLOCKED] bootstrap 目标一个都还没物化，且模型连续没有执行创建/写入动作，已停止本轮以避免继续空转。",
        backend=str(getattr(getattr(agent, "backend", None), "name", "") or ""),
    )


# LLM: has_required_bootstrap_materialization returns True only when all bootstrap targets are still missing.
# 函数用途: 只有在“一个 target 都没出现”时才启用开工 guard；已有任一目标出现后就退出这层强约束。
def has_required_bootstrap_materialization(
    agent: object,
    params: ToolLoopExecuteParams,
    calls: list[dict[str, object]] | None = None,
) -> bool:
    payload = _bootstrap_payload(agent, params)
    if not payload:
        _clear_state(agent)
        return False
    state = _load_state(agent)
    if is_bootstrap_materialization_productive_call(calls or []):
        _write_state(
            agent,
            {
                "exploration_rounds_without_materialization": 0,
                "last_exploration_fingerprint": "",
                "repeated_exploration_count": 0,
            },
        )
        return False
    if calls and not _is_bootstrap_exploration_only_call(calls):
        _write_state(agent, state)
        return True
    fingerprint = _calls_fingerprint(calls)
    count = int(state.get("exploration_rounds_without_materialization") or 0) + 1
    repeated = (
        int(state.get("repeated_exploration_count") or 0) + 1
        if str(state.get("last_exploration_fingerprint") or "") == fingerprint
        else 1
    )
    _write_state(
        agent,
        {
            "exploration_rounds_without_materialization": count,
            "last_exploration_fingerprint": fingerprint,
            "repeated_exploration_count": repeated,
        },
    )
    return True


# LLM: is_bootstrap_materialization_productive_call checks whether a tool call can materially create the first staged target.
# 函数用途: 只有明显会创建目录/文件的动作才算推进 bootstrap；纯检查和抓取不算。
def is_bootstrap_materialization_productive_call(calls: list[dict[str, object]]) -> bool:
    return any(_call_is_bootstrap_productive(call) for call in calls)


# LLM: _bootstrap_payload extracts missing startup targets from the machine delivery contract.
# 函数用途: 读取 delivery_contract.bootstrap_contract.materialization_targets；仅当全部缺失时返回 payload。
def _bootstrap_payload(agent: object, params: ToolLoopExecuteParams) -> dict[str, object]:
    contract = params.delivery_contract if isinstance(params.delivery_contract, dict) else {}
    bootstrap = contract.get("bootstrap_contract")
    targets = bootstrap.get("materialization_targets") if isinstance(bootstrap, dict) else None
    if not isinstance(targets, list):
        return {}
    workspace_root = _workspace_root(agent)
    shape_hints = _checkpoint_shape_hints(contract)
    normalized = [item for item in (_target_record(target, workspace_root, shape_hints) for target in targets) if item is not None]
    if not normalized:
        return {}
    if any(bool(item.get("exists")) for item in normalized):
        return {}
    return {
        "checkpoint_shape_hints": shape_hints,
        "pending_materialization_targets": normalized,
        "productive_tool_names": sorted(_BOOTSTRAP_PRODUCTIVE_TOOLS),
        "startup_actions": _startup_actions(bootstrap),
    }


# LLM: _workspace_root resolves the write boundary root used to evaluate bootstrap target existence.
# 函数用途: 获取当前 agent 的工作区根目录，作为 bootstrap target 解析和越界校验的基准。
def _workspace_root(agent: object) -> Path:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) or getattr(agent, "root", ".")
    return Path(root).expanduser().resolve()


# LLM: _state_path keeps bootstrap guard counters in one deterministic task-local file.
# 函数用途: 计算 bootstrap guard 状态文件路径，保证多轮判断使用同一份任务本地状态。
def _state_path(agent: object) -> Path:
    return Path(getattr(agent, "root", ".")).resolve() / _STATE_DIR / _STATE_FILE


# LLM: _load_state reads bootstrap guard counters defensively so bad JSON never crashes the loop.
# 函数用途: 读取 bootstrap guard 的状态计数；文件缺失或损坏时回退为空状态。
def _load_state(agent: object) -> dict[str, object]:
    path = _state_path(agent)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _write_state persists bootstrap guard counters after each exploration/materialization turn.
# 函数用途: 写入 bootstrap guard 的任务本地状态，供下一轮判断是否继续重定向或阻断。
def _write_state(agent: object, payload: dict[str, object]) -> None:
    path = _state_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


# LLM: _clear_state resets bootstrap guard bookkeeping once bootstrap targets are already materialized.
# 函数用途: 清理 bootstrap guard 状态文件，避免后续正常阶段继续沿用开工计数。
def _clear_state(agent: object) -> None:
    path = _state_path(agent)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


# LLM: _should_block converts persisted exploration counters into a deterministic startup-loop block decision.
# 函数用途: 根据连续探索轮次和重复探索次数判断是否该阻断开工阶段空转。
def _should_block(state: dict[str, object]) -> bool:
    return (
        int(state.get("exploration_rounds_without_materialization") or 0) >= _EXPLORATION_BLOCK_THRESHOLD
        or int(state.get("repeated_exploration_count") or 0) >= _REPEATED_EXPLORATION_BLOCK_THRESHOLD
    )


# LLM: _target_record resolves one bootstrap target into a stable existence record without reading prompt prose.
# 函数用途: 把 materialization_target 变成 {path, exists, kind...} 记录，供开工 guard 读取。
def _target_record(item: object, workspace_root: Path, shape_hints: dict[str, str]) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    raw = str(item.get("workspace_relative_path") or item.get("resolved_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    resolved = path.resolve(strict=False) if path.is_absolute() else (workspace_root / path).resolve()
    try:
        resolved.relative_to(workspace_root)
    except ValueError:
        return None
    record = {
        "artifact_id": str(item.get("artifact_id") or ""),
        "exists": resolved.exists(),
        "kind": str(item.get("kind") or ""),
        "resolved_path": str(resolved),
        "target_type": str(item.get("target_type") or ""),
        "workspace_relative_path": str(item.get("workspace_relative_path") or ""),
    }
    ref = str(record["workspace_relative_path"] or raw)
    if shape_hint := shape_hints.get(ref):
        record["checkpoint_shape_hint"] = shape_hint
    return record


# LLM: _startup_actions normalizes optional bootstrap startup actions into a stable list for prompting.
# 函数用途: 提取 bootstrap_contract.startup_actions，过滤成结构化动作列表供模型参考。
def _startup_actions(bootstrap: object) -> list[dict[str, object]]:
    actions = bootstrap.get("startup_actions") if isinstance(bootstrap, dict) else None
    if not isinstance(actions, list):
        return []
    return [dict(item) for item in actions if isinstance(item, dict)]


# LLM: _checkpoint_shape_hints collects per-checkpoint shape hints from artifact contracts without reading prompt prose.
# 函数用途: 汇总 staged checkpoint 的结构提示，帮助 bootstrap 阶段生成最小正确骨架。
def _checkpoint_shape_hints(contract: dict[str, object]) -> dict[str, str]:
    hints: dict[str, str] = {}
    for artifact in _artifact_items(contract):
        validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
        staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
        raw_hints = staging.get("checkpoint_shape_hints")
        if not isinstance(raw_hints, dict):
            continue
        for key, value in raw_hints.items():
            ref = str(key or "").strip()
            hint = str(value or "").strip()
            if ref and hint:
                hints[ref] = hint
    return hints


# LLM: _artifact_items extracts only structured artifact records from the delivery contract.
# 函数用途: 从 delivery_contract.artifacts 中挑出合法 artifact 项，供 shape hint 和目标扫描复用。
def _artifact_items(contract: dict[str, object]) -> list[dict[str, object]]:
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [item for item in artifacts if isinstance(item, dict)]


# LLM: _call_is_bootstrap_productive distinguishes "create the first target" actions from pure inspection or remote fetch steps.
# 函数用途: bootstrap 阶段只把明确创建目录/文件的工具调用视为推进动作。
def _call_is_bootstrap_productive(call: dict[str, object]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in {"append_file", "file_write_session", "replace_in_file", "write_file"}:
        return True
    if tool != "run_command":
        return False
    command = str(call.get("command") or "").strip().lower()
    if not command:
        shell = call.get("shell")
        if isinstance(shell, dict):
            command = str(shell.get("command") or "").strip().lower()
    if not command:
        return False
    return not any(command.startswith(prefix) for prefix in _RUN_COMMAND_INSPECTION_PREFIXES)


# LLM: _is_bootstrap_exploration_only_call checks whether the whole call batch stayed in inspection/fetch mode.
# 函数用途: 判断一轮工具调用是否全部属于 bootstrap 阶段的探索动作，而没有任何真实物化行为。
def _is_bootstrap_exploration_only_call(calls: list[dict[str, object]]) -> bool:
    return all(_call_is_bootstrap_exploration_only(call) for call in calls)


# LLM: _call_is_bootstrap_exploration_only classifies one call as inspection/fetch-only during startup.
# 函数用途: 把单个工具调用识别成 bootstrap 探索动作，供空转计数和重复指纹使用。
def _call_is_bootstrap_exploration_only(call: dict[str, object]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in {"fetch_url", "http_request", "list_files", "list_tools", "read_artifact", "read_file", "search"}:
        return True
    if tool != "run_command":
        return False
    command = _call_command(call)
    if not command:
        return False
    return any(command.startswith(prefix) for prefix in _RUN_COMMAND_INSPECTION_PREFIXES) or command.startswith(
        ("curl ", "rg ", "cat ", "wget ")
    )


# LLM: _calls_fingerprint gives repeated startup exploration a stable machine signature across turns.
# 函数用途: 为一批 bootstrap 调用生成指纹，便于识别“同样的探索动作又来了一轮”。
def _calls_fingerprint(calls: list[dict[str, object]] | None) -> str:
    if not calls:
        return "NO_TOOL_CALL"
    return "|".join(_call_fingerprint(call) for call in calls)


# LLM: _call_fingerprint keeps one call's identifying shape small enough for state persistence and comparison.
# 函数用途: 为单个工具调用生成短指纹，尤其在 run_command 场景下保留首行命令特征。
def _call_fingerprint(call: dict[str, object]) -> str:
    tool = str(call.get("tool") or "").strip()
    if tool != "run_command":
        return tool
    command = _call_command(call)
    if not command:
        return "run_command"
    return f"{tool}:{command.splitlines()[0].strip()[:120]}"


# LLM: _call_command extracts a normalized shell command string from either direct or nested payload fields.
# 函数用途: 统一读取工具调用中的 command 文本，兼容 shell 嵌套参数结构。
def _call_command(call: dict[str, object]) -> str:
    command = str(call.get("command") or "").strip().lower()
    if command:
        return command
    shell = call.get("shell")
    if isinstance(shell, dict):
        return str(shell.get("command") or "").strip().lower()
    return ""
