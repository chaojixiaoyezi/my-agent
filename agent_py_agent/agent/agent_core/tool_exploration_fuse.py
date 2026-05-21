# LLM: Exploration fuse prevents unbounded read/fetch loops when no local deliverable progress happens.
# 模块用途: 无 delivery_contract 时也能拦住长任务只抓资料、不写 checkpoint/草稿/产物的空转。

from __future__ import annotations

import json
import shlex
from pathlib import Path

from ..backend import ModelResponse

_STATE_DIR = ".agent_delivery"
_STATE_FILE = "exploration_fuse.json"
_EXPLORATION_ROUND_THRESHOLD = 10
_MAX_REDIRECTS = 2
_EXPLORATION_TOOL_NAMES = {
    "fetch_url",
    "http_request",
    "list_files",
    "read_artifact",
    "read_file",
    "search",
    "search_text",
}
_LOCAL_PROGRESS_TOOL_NAMES = {
    "append_file",
    "data_to_workbook",
    "file_write_session",
    "markdown_to_pdf",
    "replace_in_file",
    "write_structured_json",
    "write_file",
}
_RUN_COMMAND_LOCAL_TOOLS = {"cp", "mkdir", "mv", "python", "python3", "touch"}
_RUN_COMMAND_EXPLORATION_TOOLS = {"cat", "curl", "find", "grep", "ls", "pwd", "rg", "wget"}


# LLM: has_required_exploration_fuse increments a generic no-local-progress counter.
# 函数用途: 当一轮调用全是只读/抓取且连续次数过多时，触发先写本地进展的通用纠偏。
def has_required_exploration_fuse(agent: object, calls: list[dict[str, object]] | None) -> bool:
    state = _load_state(agent)
    if _has_local_progress_call(calls):
        _write_state(agent, {"exploration_rounds_without_local_progress": 0})
        return False
    if not _is_exploration_only_call(calls):
        _write_state(agent, state)
        return False
    count = int(state.get("exploration_rounds_without_local_progress") or 0) + 1
    _write_state(agent, {"exploration_rounds_without_local_progress": count})
    return count >= _EXPLORATION_ROUND_THRESHOLD


# LLM: has_pending_exploration_fuse keeps final prose from bypassing a materialization redirect.
# 函数用途: 读取结构化探索轮次；达到阈值后，无工具回复也必须先落地本地进展。
def has_pending_exploration_fuse(agent: object) -> bool:
    state = _load_state(agent)
    return int(state.get("exploration_rounds_without_local_progress") or 0) >= _EXPLORATION_ROUND_THRESHOLD


# LLM: exploration_fuse_context tells the model to materialize local progress before further exploration.
# 函数用途: 输出结构化空转事实和下一步机器动作建议，不读取用户自然语言作为事实。
def exploration_fuse_context(agent: object, redirects: int) -> str:
    if redirects >= _MAX_REDIRECTS:
        return ""
    state = _load_state(agent)
    envelope = {
        "exploration_rounds_without_local_progress": int(state.get("exploration_rounds_without_local_progress") or 0),
        "required_next_action": "materialize_local_progress",
        "productive_tool_names": sorted(_LOCAL_PROGRESS_TOOL_NAMES),
    }
    return "\n".join(
        [
            "[tool-system exploration-fuse]",
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            "你已经连续多轮只做抓取/读取/搜索，没有新的本地交付推进。下一轮必须先写出本地 checkpoint、"
            "草稿、脚本、数据文件或阶段产物，再继续远程抓取；不要继续只 fetch/read/search。",
        ]
    )


# LLM: exploration_fuse_block_response stops loops that ignored local-materialization redirects.
# 函数用途: 连续忽略探索空转纠偏后确定性停止本轮，避免真实任务一直烧模型时间。
def exploration_fuse_block_response(agent: object) -> ModelResponse:
    return ModelResponse(
        text=(
            "[EXPLORATION_FUSE_BLOCKED] 模型连续只做抓取/读取/搜索，没有物化新的本地进展；"
            "本轮已停止，保留已抓取 artifacts，后续应从 checkpoint/script/report 草稿继续。"
        ),
        backend=str(getattr(getattr(agent, "backend", None), "name", "") or ""),
        runtime_status="blocked",
        runtime_reason="EXPLORATION_FUSE",
    )


# LLM: _has_local_progress_call recognizes tools that create durable local progress.
# 函数用途: 判断一轮工具调用是否包含写文件、构建脚本或本地变更命令。
def _has_local_progress_call(calls: list[dict[str, object]] | None) -> bool:
    return any(_call_has_local_progress(call) for call in calls or [])


# LLM: _call_has_local_progress classifies one call without reading prompt prose.
# 函数用途: 根据工具名和结构化 command 字段判断单次调用是否能推进本地产物。
def _call_has_local_progress(call: dict[str, object]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in _LOCAL_PROGRESS_TOOL_NAMES:
        return True
    if tool != "run_command":
        return False
    command = _call_command(call)
    return bool(command) and (_command_name(command) in _RUN_COMMAND_LOCAL_TOOLS or ">" in command)


# LLM: _is_exploration_only_call detects turns that stayed entirely in read/fetch/search mode.
# 函数用途: 判断一轮调用是否全部为探索动作；空调用不在这里阻断。
def _is_exploration_only_call(calls: list[dict[str, object]] | None) -> bool:
    if not calls:
        return False
    return all(_call_is_exploration(call) for call in calls)


# LLM: _call_is_exploration classifies one call as read-only exploration.
# 函数用途: 用工具名和 command 前缀识别只读探索，不从自然语言内容推断任务事实。
def _call_is_exploration(call: dict[str, object]) -> bool:
    tool = str(call.get("tool") or "").strip()
    if tool in _EXPLORATION_TOOL_NAMES:
        return True
    if tool != "run_command":
        return False
    command = _call_command(call)
    return bool(command) and _command_name(command) in _RUN_COMMAND_EXPLORATION_TOOLS


# LLM: _call_command extracts command text across direct and nested tool payloads.
# 函数用途: 统一读取 run_command.command 或 shell.command，供分类器使用。
def _call_command(call: dict[str, object]) -> str:
    command = str(call.get("command") or "").strip().lower()
    if command:
        return command
    shell = call.get("shell")
    if isinstance(shell, dict):
        return str(shell.get("command") or "").strip().lower()
    return ""


# LLM: _command_name extracts the executable token without relying on natural-language prose.
# 函数用途: 用 shell lexer 读取 run_command 的第一个命令词，避免 ls/lsof 这类前缀误判。
def _command_name(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""


# LLM: _state_path stores exploration counters beside other delivery guard state.
# 函数用途: 获取探索熔断状态文件路径，限定在当前 agent root 下。
def _state_path(agent: object) -> Path:
    return Path(getattr(agent, "root", ".")).resolve() / _STATE_DIR / _STATE_FILE


# LLM: _load_state reads exploration fuse state defensively.
# 函数用途: 文件缺失或 JSON 损坏时返回空状态，不影响主循环运行。
def _load_state(agent: object) -> dict[str, object]:
    path = _state_path(agent)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _write_state persists exploration fuse counters.
# 函数用途: 写入连续探索轮次，供下一轮工具决策使用。
def _write_state(agent: object, payload: dict[str, object]) -> None:
    path = _state_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


__all__ = [
    "exploration_fuse_block_response",
    "exploration_fuse_context",
    "has_required_exploration_fuse",
    "has_pending_exploration_fuse",
]
