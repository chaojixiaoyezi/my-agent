# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""preflight checks for write-capable orchestration tasks.

Subagent tool execution still owns the real write boundary. This module only catches
obvious outside-workspace write requests before a work order is created.
"""

import re
from pathlib import Path

from ..path_recovery_hints import overlaps_spans, suggest_workspace_typo_target, url_spans

WRITE_SUBAGENT_TOOLS = {"write_file", "append_file", "replace_in_file"}

_ABSOLUTE_PATH_RE = re.compile(
    r"(?:(?<![A-Za-z0-9+.\-/])[A-Za-z]:[\\/][^\s\"'<>|]+|"
    r"~[\\/][^\s\"'<>|]+|(?<![\w.\-<+])/[^\s\"'<>|]+)"
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WRITE_INTENT_WORDS = (
    "写",
    "创建",
    "生成",
    "保存",
    "修改",
    "write",
    "create",
    "generate",
    "save",
    "modify",
)
_NEGATED_WRITE_MARKERS = (
    "不要",
    "不能",
    "禁止",
    "别",
    "do not",
    "don't",
    "avoid",
    "not ",
    "no ",
)
_TARGET_LEFT_MARKERS = (
    "到",
    "至",
    "在",
    "into",
    "to",
    "under",
    "inside",
)


# LLM: external_write_target_error 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理externalwritetargeterror相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def external_write_target_error(agent, goal: str, allowed_tools: list[str]) -> str:
    if not WRITE_SUBAGENT_TOOLS.intersection(allowed_tools):
        return ""
    if not _goal_has_write_intent(goal):
        return ""
    workspace_roots = getattr(agent.subagents, "workspace_roots", [agent.subagents.workspace_root])
    roots = _workspace_roots(workspace_roots)
    external_paths = _external_absolute_paths(goal, roots)
    if not external_paths:
        return ""
    typo_hint = _first_workspace_typo_hint(external_paths, roots)
    if typo_hint:
        target, suggested_target = typo_hint
        return (
            "子代理写入目标疑似路径拼写错误，已拒绝创建任务以避免越权写入。"
            f" suspected_path_typo=true target={target} workspace_root={agent.subagents.workspace_root}"
            f" suggested_target={suggested_target}。"
            " 这是路径拼写错误，不是权限缺口；"
            "请使用 suggested_target 重新调用 schedule_child_subagents，不要写 capability_request。"
        )
    preview = ", ".join(external_paths[:3])
    return (
        "子代理写入目标在当前工作区外，已拒绝创建任务，避免后续 dispatch 超时或越权写入。"
        f" target={preview} workspace_root={agent.subagents.workspace_root}。"
        " 请把目标目录放入 workspace_root，或先在工作区内生成文件后再人工移动。"
    )


# LLM: _goal_has_write_intent 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理目标haswriteintent相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _goal_has_write_intent(goal: str) -> bool:
    lowered = goal.lower()
    return any(word in lowered for word in _WRITE_INTENT_WORDS)


# LLM: _external_absolute_paths 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理externalabsolute路径相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _external_absolute_paths(goal: str, workspace_roots: Path | list[Path]) -> list[str]:
    roots = _workspace_roots(workspace_roots)
    spans = url_spans(goal)
    external: list[str] = []
    for match in _ABSOLUTE_PATH_RE.finditer(goal):
        if overlaps_spans(match.start(), match.end(), spans):
            continue
        if not _path_candidate_is_write_target(goal, match.start(), match.end()):
            continue
        raw = _trim_path_candidate(match.group())
        if raw and _is_external_absolute_path(raw, roots) and raw not in external:
            external.append(raw)
    return external


# LLM: _path_candidate_is_write_target avoids treating negative route examples as filesystem writes.
# 函数用途: 只拦截局部语境像“写到/保存到/在 X 创建”的路径；`不要写 /collections` 这类示例不算写入目标。
def _path_candidate_is_write_target(goal: str, start: int, end: int) -> bool:
    before = goal[max(0, start - 36) : start].casefold()
    after = goal[end : min(len(goal), end + 36)].casefold()
    if _negated_near_path(before):
        return False
    return _left_marks_target(before) or _right_marks_write(after)


# LLM: _negated_near_path recognizes "do not write /route" style examples before path guard checks.
# 函数用途: 过滤中文/英文否定语境，避免写入守卫把禁止示例当真实目标。
def _negated_near_path(before: str) -> bool:
    return any(marker in before for marker in _NEGATED_WRITE_MARKERS)


# LLM: _left_marks_target keeps path target syntax explicit without parsing full natural language.
# 函数用途: 判断路径左侧是否有“到/在/to/under”这类目标提示。
def _left_marks_target(before: str) -> bool:
    stripped = before.rstrip()
    return any(stripped.endswith(marker) or stripped.endswith(f"{marker} ") for marker in _TARGET_LEFT_MARKERS)


# LLM: _right_marks_write supports "在 /path 创建" ordering without global write-intent leakage.
# 函数用途: 判断路径右侧短窗口是否出现创建、保存、写入等动作词。
def _right_marks_write(after: str) -> bool:
    return any(word in after for word in _WRITE_INTENT_WORDS)


# LLM: _trim_path_candidate 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理trim路径candidate相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _trim_path_candidate(raw: str) -> str:
    return raw.strip().rstrip(".,;:，。；：、)]}）】")


# LLM: _first_workspace_typo_hint turns near-miss workspace paths into retry instructions while preserving the deny.
# 函数用途: 在外部路径看起来只是工作区前缀拼错时，生成一个安全的建议路径；不会自动放行写入。
def _first_workspace_typo_hint(external_paths: list[str], workspace_roots: list[Path]) -> tuple[str, str] | None:
    for raw in external_paths:
        suggested = suggest_workspace_typo_target(raw, workspace_roots)
        if suggested:
            return raw, suggested
    return None


# LLM: _is_external_absolute_path 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 判断externalabsolute路径条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _is_external_absolute_path(raw: str, workspace_roots: Path | list[Path]) -> bool:
    text = raw.replace("\\", "/")
    if _WINDOWS_ABSOLUTE_RE.match(raw) and not Path(text).is_absolute():
        return True
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return True
    return not any(_is_relative_to(resolved, root) for root in _workspace_roots(workspace_roots))


# LLM: _workspace_roots 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理workspaceroots相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _workspace_roots(workspace_roots: Path | list[Path]) -> list[Path]:
    raw_roots = workspace_roots if isinstance(workspace_roots, list) else [workspace_roots]
    return [Path(root).resolve(strict=False) for root in raw_roots]


# LLM: _is_relative_to 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 判断relativeto条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
