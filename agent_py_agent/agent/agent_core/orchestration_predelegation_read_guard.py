# LLM: Pre-delegation read guard nudges roots to hand source refs to children before reading bodies.
# 模块用途: 用户要求派小傻妞时，root 先读 brief/目录，再把 data 正文路径交给子代理。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tools import ToolExecutionResult
from .orchestration_body_read_refs import BODY_READ_TOOLS, is_orchestration_artifact_read
from .orchestration_delegation_intent import prompt_requests_subagent_delegation
from .orchestration_run_scope import remembered_orchestration_run_ids
from .runner_context import current_subagent_run_id

_BRIEF_FILE_NAMES = {
    "AGENTS.md",
    "README.md",
    "TARGET_OBJECT.md",
    "USER.md",
    "agent_config.yaml",
    "human_prompt.md",
    "memory.md",
    "rubric.md",
}
_SOURCE_BODY_DIR_NAMES = {"data", "datasets", "docs", "source", "sources", "materials", "fixtures"}
_SOURCE_BODY_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".txt", ".yaml", ".yml"}


# LLM: PreDelegationReadGuardRequest bundles one pending read with current root prompt scope.
# 类用途: 保存派工前读正文检查所需的 agent、payload 和用户任务文本，避免散传参数。
@dataclass(frozen=True)
class PreDelegationReadGuardRequest:
    agent: object
    payload: object
    user_prompt: str = ""


# LLM: maybe_block_predelegation_source_read keeps roots refs-first before initial subagent creation.
# 函数用途: 明确派工任务里，root 未创建子代理前不吞 data 正文，而是把路径放进子代理上下文。
def maybe_block_predelegation_source_read(
    request: PreDelegationReadGuardRequest,
) -> ToolExecutionResult | None:
    if not _is_predelegation_root_turn(request):
        return None
    if not isinstance(request.payload, dict):
        return None
    tool = str(request.payload.get("tool") or "")
    if tool not in BODY_READ_TOOLS:
        return None
    if _allowed_predelegation_read(request.agent, request.payload):
        return None
    return ToolExecutionResult(tool, False, _blocked_message(tool, request.payload))


# LLM: _is_predelegation_root_turn limits this policy to top-level roots before current dispatch ids exist.
# 函数用途: 只在 root 当前轮尚未创建/记住 run_id、且用户确实要求派工时启用。
def _is_predelegation_root_turn(request: PreDelegationReadGuardRequest) -> bool:
    if current_subagent_run_id(request.agent):
        return False
    if remembered_orchestration_run_ids(request.agent):
        return False
    return prompt_requests_subagent_delegation(request.user_prompt)


# LLM: _allowed_predelegation_read lets roots inspect brief files and orchestration summaries.
# 函数用途: 派工前允许读 README/目标/rubric/配置和编排摘要，阻止 data 正文和大 artifact 正文。
def _allowed_predelegation_read(agent: object, payload: dict[str, Any]) -> bool:
    tool = str(payload.get("tool") or "")
    if tool == "run_command":
        return True
    if tool == "read_artifact":
        return is_orchestration_artifact_read(payload)
    if tool == "read_file":
        path = _payload_path(agent, payload)
        return path is not None and not _looks_like_source_body(path)
    return False


# LLM: _payload_path resolves model read_file params without requiring the target to exist.
# 函数用途: 支持绝对和相对路径；解析失败时返回 None，让调用方保守阻断正文读取。
def _payload_path(agent: object, payload: dict[str, Any]) -> Path | None:
    raw = str(_payload_value(payload, ("path", "file")) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(str(getattr(agent, "root", "") or ".")).expanduser() / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


# LLM: _looks_like_source_body recognizes data/material files while allowing brief task files.
# 函数用途: 判断某个 read_file 是否在派工前会吞正文；README/rubric/TARGET 等 brief 文件放行。
def _looks_like_source_body(path: Path) -> bool:
    if path.name in _BRIEF_FILE_NAMES:
        return False
    parts = {part.lower() for part in path.parts}
    return bool(parts & _SOURCE_BODY_DIR_NAMES and path.suffix.lower() in _SOURCE_BODY_SUFFIXES)


# LLM: _blocked_message gives the model a direct recovery path instead of asking the user.
# 函数用途: 告诉 root 改用 create_subagents 的 required_read_paths/context_manifest 下发正文路径。
def _blocked_message(tool: str, payload: dict[str, Any]) -> str:
    target = str(_payload_value(payload, ("path", "artifact_ref", "ref")) or "")
    return (
        "predelegation_source_read_blocked=true "
        f"tool={tool} target={target}。"
        "当前用户任务要求派小傻妞/子代理协作；root 派工前只读 README、目标、rubric 和目录即可。"
        "不要先把 data/docs/materials 正文或大 artifact 读进 root 上下文。"
        "请调用 create_subagents，用 items/tasks 为每个小傻妞写独立 goal，"
        "并把这些资料路径放进 required_read_paths、context_manifest 或 context_packs。"
    )


# LLM: _payload_value keeps the guard independent from parser normalization.
# 函数用途: 同时读取扁平参数和 filesystem/orchestration 等 bundle 里的参数，避免真实模型换形态后提示丢路径。
def _payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> object:
    direct = _first_payload_value(payload, keys)
    if direct:
        return direct
    for wrapper in ("filesystem", "orchestration", "request"):
        value = payload.get(wrapper)
        if not isinstance(value, dict):
            continue
        wrapped = _first_payload_value(value, keys)
        if wrapped:
            return wrapped
    return ""


# LLM: _first_payload_value flattens alias lookup so guard logic stays predictable.
# 函数用途: 从当前 payload 层按 key 顺序取第一个非空值；bundle 外层和内层都复用同一规则。
def _first_payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> object:
    for key in keys:
        value = payload.get(key)
        if value:
            return value
    return ""
