# LLM: Delivery completion soft hints nudge finished artifact work toward explicit closeout.
# 模块用途: 当声明的交付文件已经落盘后，给下一轮模型一个非阻断收口提醒，避免继续无目的探索。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._runtime_params import ToolLoopExecuteParams

_HINT_MARKER = "[delivery-completion-soft-hint]"
_MUTATING_TOOLS = {"write_file", "apply_patch", "run_command", "controlled_exec"}


# LLM: maybe_append_delivery_completion_soft_hint adds advice only after concrete local delivery exists.
# 函数用途: 根据工具记录和 delivery_contract 判断是否提醒模型收口；只写 tool_context，不改变状态或阻断任务。
def maybe_append_delivery_completion_soft_hint(
    agent: object,
    params: ToolLoopExecuteParams,
    archive_record: dict[str, object],
    *,
    tool_ok: bool,
) -> None:
    if _hint_already_added(params):
        return
    if not _is_successful_mutation(archive_record, tool_ok=tool_ok):
        return
    contract = params.delivery_contract if isinstance(params.delivery_contract, dict) else {}
    if not contract:
        return
    workspace_root = Path(getattr(agent, "root", ".")).expanduser().resolve(strict=False)
    target_paths = _required_target_paths(contract, workspace_root)
    ready_targets = [str(path) for path in target_paths if path.exists()]
    if target_paths and len(ready_targets) < len(target_paths):
        return
    produced_refs = _produced_refs(archive_record)
    if not target_paths and not produced_refs:
        return
    payload = {
        "status": "delivery_artifacts_present",
        "ready_target_paths": ready_targets,
        "produced_refs": produced_refs,
        "next_step_hint": "final_check_then_submit_or_report",
    }
    params.tool_context.append(
        "\n".join(
            [
                _HINT_MARKER,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "如果这些已经是本次任务要交付的最终产物，可以做一次必要的查漏补缺；"
                "确认无明显遗漏后请尽快调用 submit_for_acceptance 提交验收，"
                "或在无需落盘验收的任务里直接用简短中文汇报产物位置和完成情况。"
                "不要因为想再看看就无限重复读取同一批材料。",
            ]
        )
    )


# LLM: _is_successful_mutation limits hints to tools that can materially change local delivery.
# 函数用途: 避免 read/search/fetch 的归档文件被误当成用户交付物。
def _is_successful_mutation(record: dict[str, object], *, tool_ok: bool) -> bool:
    if not tool_ok:
        return False
    tool = str(record.get("tool") or "").strip()
    if tool not in _MUTATING_TOOLS:
        return False
    return True


# LLM: _required_target_paths extracts explicit artifact targets without interpreting prose.
# 函数用途: 从 delivery_contract.artifacts 的 path/preferred_path 获取目标路径；开放格式但未声明路径时不猜。
def _required_target_paths(contract: dict[str, Any], workspace_root: Path) -> list[Path]:
    raw = contract.get("artifacts")
    if not isinstance(raw, list):
        return []
    paths: list[Path] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("required") is False:
            continue
        raw_path = str(item.get("preferred_path") or item.get("path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        paths.append(path.resolve(strict=False) if path.is_absolute() else (workspace_root / path).resolve(strict=False))
    return _unique_paths(paths)


# LLM: _produced_refs reads structured artifact refs from archive records only.
# 函数用途: 收集工具产物/registry refs，给提示提供可追踪路径，不解析自然语言输出。
def _produced_refs(record: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for key in ("artifact_ref", "output_path"):
        _append_text(refs, record.get(key))
    _append_ref_items(refs, record.get("artifact_registry_refs"), ("path", "artifact_id"))
    _append_ref_items(refs, record.get("tool_result_refs"), ("path",))
    return list(dict.fromkeys(refs))


# LLM: _hint_already_added keeps the soft prompt one-shot per tool loop.
# 函数用途: 防止长任务每次写文件都重复插入同一类收口提示。
def _hint_already_added(params: ToolLoopExecuteParams) -> bool:
    return any(str(item).startswith(_HINT_MARKER) for item in params.tool_context)


# LLM: _append_text normalizes optional tool reference fields without inventing missing paths.
# 函数用途: 将非空文本加入 refs 列表，忽略空值。
def _append_text(items: list[str], value: object) -> None:
    text = str(value or "").strip()
    if text:
        items.append(text)


# LLM: _append_ref_items extracts structured refs defensively from registry-like lists.
# 函数用途: 从列表对象里按候选字段收集第一个可用引用。
def _append_ref_items(items: list[str], value: object, keys: tuple[str, ...]) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        _append_first_ref_value(items, item, keys)


# LLM: _append_first_ref_value keeps each structured ref row to one stable display value.
# 函数用途: 从一个字典行里按字段顺序加入首个非空引用。
def _append_first_ref_value(items: list[str], value: object, keys: tuple[str, ...]) -> None:
    if not isinstance(value, dict):
        return
    for key in keys:
        text = str(value.get(key) or "").strip()
        if text:
            items.append(text)
            return


# LLM: _unique_paths preserves discovery order while de-duplicating candidate output files.
# 函数用途: 对 Path 列表按字符串值去重，保留首次出现顺序。
def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


__all__ = ["maybe_append_delivery_completion_soft_hint"]
