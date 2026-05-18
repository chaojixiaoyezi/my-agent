# LLM: Write safety belongs to structured scopes and execution tools, not prose preflight guesses.
# 模块用途: 保留 create_subagents 调用边界；实际写入边界由工具执行层的 allowed roots 和 RunScope 检查。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .orchestration_create_target_roots import agent_workspace_roots, is_relative_to
from .parameters import _string_list
from .runner_input_dependencies import params_output_refs

WRITE_SUBAGENT_TOOLS = {"write_file", "append_file", "replace_in_file"}


# LLM: ExternalWriteTargetRequest is the structured preflight contract for delegation write targets.
# 类用途: 携带 agent、工具授权和机器参数；普通自然语言 goal 不进入写入边界判断。
@dataclass(frozen=True)
class ExternalWriteTargetRequest:
    agent: object
    allowed_tools: list[str]
    params: dict[str, object]


# LLM: external_write_target_error checks only structured output roots and refs.
# 函数用途: 在 create/schedule 前拦截机器字段声明的工作区外写入目标；普通 goal 文本不作为事实来源。
def external_write_target_error(request: ExternalWriteTargetRequest) -> str:
    if not WRITE_SUBAGENT_TOOLS.intersection({str(item or "") for item in request.allowed_tools or []}):
        return ""
    targets = _structured_write_targets(request.params)
    if not targets:
        return ""
    roots = _workspace_roots(request.agent)
    if not roots:
        return ""
    for target in targets:
        path = _target_path(target, roots[0])
        if path is None or any(is_relative_to(path, root) for root in roots):
            continue
        if suggestion := _workspace_typo_message(path, roots):
            return suggestion
        return f"子代理写入目标在当前工作区外: target={path}; workspace_roots={[str(item) for item in roots]}"
    return ""


# LLM: _structured_write_targets reads tool params, not prose, so URLs/content text cannot become paths.
# 函数用途: 收集 extra_write_roots 与 output refs 这些明确写入目标，保持顺序去重。
def _structured_write_targets(params: dict[str, object]) -> list[str]:
    targets: list[str] = []
    for value in _target_sources(params):
        _append_target_strings(targets, _string_list(value))
    return targets


# LLM: _target_sources keeps supported machine fields in one small table.
# 函数用途: 返回允许作为写入目标事实来源的结构化字段；不要加入 goal/thought 这类自然语言。
def _target_sources(params: dict[str, object]) -> list[object]:
    return [
        params.get("extra_write_roots"),
        params.get("write_roots"),
        params.get("target_roots"),
        params_output_refs(params),
    ]


# LLM: _append_target_strings deduplicates clean structured target strings.
# 函数用途: 过滤 URL/空值并保持顺序，避免写入预检把内容链接当成本地路径。
def _append_target_strings(targets: list[str], values: list[str]) -> None:
    for item in values:
        if item and "://" not in item and item not in targets:
            targets.append(item)


# LLM: _workspace_roots normalizes the manager roots once for preflight comparisons.
# 函数用途: 获取 workspace_root/workspace_roots；mock 或旧 adapter 缺字段时不误拦截。
def _workspace_roots(agent) -> list[Path]:
    raw = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw, str | Path):
        return []
    root = Path(raw).expanduser().resolve(strict=False)
    return agent_workspace_roots(agent, root)


# LLM: _target_path resolves relative structured targets below the primary workspace root.
# 函数用途: 将文件或目录目标变成可比较路径；格式异常时返回 None。
def _target_path(target: str, workspace_root: Path) -> Path | None:
    text = str(target or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


# LLM: _workspace_typo_message handles common username typos without asking for broader permission.
# 函数用途: 当路径尾部和工作区一致但前缀拼错时，返回可直接重试的 suggested_target。
def _workspace_typo_message(target: Path, roots: list[Path]) -> str:
    for root in roots:
        suffix = _shared_workspace_suffix(target, root)
        if not suffix:
            continue
        suggested = root.joinpath(*suffix)
        return (
            "suspected_path_typo=true;"
            f"target={target};"
            f"suggested_target={suggested};"
            "请使用 suggested_target 重新调用 schedule_child_subagents 或 create_subagents，"
            "不要写 capability_request。"
        )
    return ""


# LLM: _shared_workspace_suffix finds a target tail after the workspace directory name.
# 函数用途: `/Users/wrong/project/a` 与 `/Users/right/project` 共享 project/a 时生成修正尾部。
def _shared_workspace_suffix(target: Path, root: Path) -> list[str]:
    root_parts = root.parts
    target_parts = target.parts
    if not root_parts:
        return []
    workspace_name = root_parts[-1]
    matches = [index for index, part in enumerate(target_parts) if part == workspace_name]
    for index in matches:
        if target_parts[index:] and target_parts[index] == workspace_name:
            return list(target_parts[index + 1 :])
    return []
