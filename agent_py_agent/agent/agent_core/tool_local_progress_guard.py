# LLM: local-progress guard gives soft rework hints when closeout machine facts show no new local work progress.
# 模块用途: 基于 closeout.json 的结构化失败/进展指纹，提醒模型从只读探索切回本地 checkpoint/draft/builder，不阻断任务。

from __future__ import annotations

import json
from pathlib import Path

from ._runtime_params import ToolLoopExecuteParams
from .exploration_fuse_config import exploration_fuse_config
from .tool_local_progress_hints import (
    HintDeliveryInput,
    due_hint_round,
    hint_message,
    mark_hint_delivered,
    recovery_signature,
    should_prompt,
)

_STATE_DIR = ".agent_delivery"
_STATE_FILE = "local_progress_guard.json"
_LOCAL_PROGRESS_TOOL_NAMES = {
    "apply_patch",
    "run_command",
    "write_file",
}
_EXPLORATION_TOOL_NAMES = {
    "list_files",
    "list_tools",
    "read_artifact",
    "read_file",
    "search",
    "web_fetch",
    "web_search",
}
_RUN_COMMAND_LOCAL_MUTATION_PREFIXES = ("mkdir ", "mkdir -p", "touch ", "cp ", "mv ", "tee ")
_RUN_COMMAND_EXPLORATION_PREFIXES = ("curl ", "find ", "ls", "pwd", "rg ", "cat ", "wget ")


# LLM: has_required_local_progress_guard increments a task-local exploration counter and activates only on soft hint rounds.
# 函数用途: 若 closeout 一直显示同一失败和同一本地进展指纹，而模型连续多轮只做远程/只读探索，则在提示节点提醒回到本地推进。
def has_required_local_progress_guard(agent: object, params: ToolLoopExecuteParams, calls: list[dict[str, object]] | None) -> bool:
    payload = _guard_payload(agent)
    if not payload:
        return False
    state = _load_state(agent)
    recovery_sig = recovery_signature(params)
    fingerprint = str(payload.get("work_progress_fingerprint") or "")
    failure_fingerprint = str(payload.get("failure_fingerprint") or "")
    if (
        str(state.get("work_progress_fingerprint") or "") != fingerprint
        or str(state.get("failure_fingerprint") or "") != failure_fingerprint
        or str(state.get("recovery_signature") or "") != recovery_sig
    ):
        state = {
            "failure_fingerprint": failure_fingerprint,
            "exploration_rounds_without_local_progress": 0,
            "recovery_signature": recovery_sig,
            "work_progress_fingerprint": fingerprint,
        }
    if _is_local_progressive_call(payload, calls):
        state["exploration_rounds_without_local_progress"] = 0
        _write_state(agent, state)
        return False
    if not _is_exploration_only_call(calls):
        _write_state(agent, state)
        return False
    count = int(state.get("exploration_rounds_without_local_progress") or 0) + 1
    state["exploration_rounds_without_local_progress"] = count
    _write_state(agent, state)
    config = exploration_fuse_config(agent)
    return should_prompt(config, state, count)


# LLM: local_progress_guard_context exposes structured no-progress facts so the next model turn can switch from exploration to local work.
# 函数用途: 当 guard 触发时，把连续探索轮次、待处理恢复动作和缺失目标作为结构化提示喂给下一轮模型。
def local_progress_guard_context(agent: object, redirects: int) -> str:
    del redirects
    payload = _guard_payload(agent)
    config = exploration_fuse_config(agent)
    if not payload:
        return ""
    state = _load_state(agent)
    count = int(state.get("exploration_rounds_without_local_progress") or 0)
    hint_round = due_hint_round(config, state, count)
    if hint_round is None:
        return ""
    envelope = {
        "exploration_rounds_without_local_progress": count,
        "failure_fingerprint": payload.get("failure_fingerprint", ""),
        "local_progress_hint_round": hint_round,
        "local_progress_hint_interval": config.local_progress_unlimited_hint_interval,
        "pending_materialization_targets": payload.get("pending_materialization_targets", []),
        "recovery_actions": payload.get("recovery_actions", []),
        "work_progress_fingerprint": payload.get("work_progress_fingerprint", ""),
    }
    mark_hint_delivered(HintDeliveryInput(agent, state, config, hint_round, _write_state))
    return "\n".join(
        [
            "[tool-system local-progress-guard]",
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            hint_message(count, hint_round),
        ]
    )

# LLM: reset_local_progress_guard clears stale no-progress debt after a successful machine closeout.
# 函数用途: 任务已通过结构化收口时重置本地进展计数，避免旧失败状态污染后续恢复 attempt。
def reset_local_progress_guard(agent: object, params: ToolLoopExecuteParams | None = None) -> None:
    recovery_sig = recovery_signature(params) if params is not None else ""
    _write_state(
        agent,
        {
            "failure_fingerprint": "",
            "exploration_rounds_without_local_progress": 0,
            "recovery_signature": recovery_sig,
            "work_progress_fingerprint": "",
        },
    )


# LLM: _guard_payload extracts the no-progress facts that justify redirecting the model back to local work.
# 函数用途: 从 closeout 报告里提取 failure/work-progress 指纹、恢复动作和待物化目标。
def _guard_payload(agent: object) -> dict[str, object]:
    report = _closeout_report(agent)
    if not report or report.get("ok") is True:
        return {}
    progress = report.get("delivery_progress")
    if not isinstance(progress, dict):
        return {}
    fingerprint = str(progress.get("work_progress_fingerprint") or "")
    failure_fingerprint = str(progress.get("failure_fingerprint") or "")
    if not fingerprint or not failure_fingerprint:
        return {}
    recovery_actions = progress.get("recovery_actions")
    pending_targets = progress.get("pending_materialization_targets")
    if not isinstance(recovery_actions, list):
        recovery_actions = []
    if not isinstance(pending_targets, list):
        pending_targets = []
    if not recovery_actions and not pending_targets:
        return {}
    return {
        "failure_fingerprint": failure_fingerprint,
        "pending_materialization_targets": pending_targets,
        "recovery_actions": [item for item in recovery_actions if isinstance(item, dict)],
        "work_progress_fingerprint": fingerprint,
    }


# LLM: _closeout_report reads the machine closeout report that local-progress decisions are based on.
# 函数用途: 读取 .agent_delivery/closeout.json；缺失或损坏时安全回退为空对象。
def _closeout_report(agent: object) -> dict[str, object]:
    path = Path(getattr(agent, "root", ".")).resolve() / ".agent_delivery" / "closeout.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _state_path keeps local-progress guard counters in one deterministic task-local file.
# 函数用途: 计算 local-progress guard 状态文件路径，供多轮探索计数复用。
def _state_path(agent: object) -> Path:
    return Path(getattr(agent, "root", ".")).resolve() / _STATE_DIR / _STATE_FILE


# LLM: _load_state reads persisted no-progress counters without letting bad JSON break the loop.
# 函数用途: 读取 local-progress guard 的状态；不存在或坏文件时返回空状态。
def _load_state(agent: object) -> dict[str, object]:
    path = _state_path(agent)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _write_state persists local-progress guard counters after each guarded turn.
# 函数用途: 写入 local-progress guard 状态，记录连续无本地推进的探索轮次。
def _write_state(agent: object, payload: dict[str, object]) -> None:
    path = _state_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


# LLM: _is_local_progressive_call checks whether any tool call advances checkpoint/draft/builder work locally.
# 函数用途: 判断一轮工具调用里是否包含 builder 或本地写入动作，从而重置 no-progress 计数。
def _is_local_progressive_call(payload: dict[str, object], calls: list[dict[str, object]] | None) -> bool:
    if not calls:
        return False
    action_tools = {
        str(item.get(key) or "").strip()
        for item in payload.get("recovery_actions", [])
        if isinstance(item, dict)
        for key in ("builder_tool", "writer_tool")
    }
    productive_tools = {tool for tool in action_tools if tool} | _LOCAL_PROGRESS_TOOL_NAMES
    return any(_call_is_local_progressive(call, productive_tools) for call in calls)


# LLM: _call_is_local_progressive classifies one call as genuine local progress rather than exploration.
# 函数用途: 识别单个工具调用是否属于本地推进动作，例如写文件、调用 builder 或执行本地变更命令。
def _call_is_local_progressive(call: dict[str, object], productive_tools: set[str]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in productive_tools:
        return True
    if tool != "run_command":
        return False
    command = _call_command(call)
    if not command:
        return False
    return any(command.startswith(prefix) for prefix in _RUN_COMMAND_LOCAL_MUTATION_PREFIXES) or ">" in command


# LLM: _is_exploration_only_call detects turns that stayed entirely in fetch/read/list mode.
# 函数用途: 判断一轮调用是否全是探索动作；若是，就继续累计无本地推进轮次。
def _is_exploration_only_call(calls: list[dict[str, object]] | None) -> bool:
    if not calls:
        return True
    return all(_call_is_exploration_only(call) for call in calls)


# LLM: _call_is_exploration_only classifies one call as remote/read-only exploration for no-progress accounting.
# 函数用途: 识别单个工具调用是否属于只读/抓取类探索动作。
def _call_is_exploration_only(call: dict[str, object]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in _EXPLORATION_TOOL_NAMES:
        return True
    if tool != "run_command":
        return False
    command = _call_command(call)
    if not command:
        return False
    return any(command.startswith(prefix) for prefix in _RUN_COMMAND_EXPLORATION_PREFIXES)


# LLM: _call_command extracts a normalized shell command string for both local-progress and exploration classifiers.
# 函数用途: 从工具调用里统一取出 command 文本，兼容 shell 嵌套参数结构。
def _call_command(call: dict[str, object]) -> str:
    command = str(call.get("command") or "").strip().lower()
    if command:
        return command
    shell = call.get("shell")
    if isinstance(shell, dict):
        return str(shell.get("command") or "").strip().lower()
    return ""
