# LLM: Leaf target dedupe keeps concrete output-file checks out of broad hierarchy guards.
# 模块用途: 根据已完成 leaf 的 artifact 路径引用阻断同父级重复写同一目标文件。

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import SubAgentTask


# LLM: LeafTargetDedupeRequest bundles all state needed for same-parent leaf output dedupe.
# 类用途: 集中保存 manager、parent、调度请求和 leaf 判断函数，避免去重入口散参扩张。
@dataclass(frozen=True)
class LeafTargetDedupeRequest:
    manager: Any
    parent: SubAgentTask
    schedule_request: Any
    leaf_like: Callable[[Any], bool]


# LLM: duplicate_verified_leaf_target_reason blocks rerunning the same finished leaf output.
# 函数用途: 同父级已有 DONE/VERIFIED leaf 写过同一目标文件时，阻断重复 leaf 创建。
def duplicate_verified_leaf_target_reason(request: LeafTargetDedupeRequest) -> str:
    seen_targets = _completed_leaf_targets(request)
    if not seen_targets:
        return ""
    for spec in request.schedule_request.child_specs:
        if not request.leaf_like(spec):
            continue
        duplicate = _first_overlapping_target(_child_target_tokens(spec), seen_targets)
        if duplicate:
            return f"duplicate_leaf_target:{duplicate}"
    return ""


# LLM: _completed_leaf_targets reads only direct child metadata and output artifact path refs.
# 函数用途: 收集已验证 leaf 的产物文件名；不读取 artifact 正文，只读取 output.json 的结构化路径。
def _completed_leaf_targets(request: LeafTargetDedupeRequest) -> list[set[str]]:
    targets: list[set[str]] = []
    for child_id in request.parent.child_ids:
        try:
            child = request.manager.load(child_id)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            continue
        if not _is_verified_leaf(child, request.leaf_like):
            continue
        child_targets = _child_target_tokens(child)
        if child_targets:
            targets.append(child_targets)
    return targets


# LLM: _is_verified_leaf narrows dedupe to finished implementation leaves.
# 函数用途: 只有已 DONE/VERIFIED 的 leaf/worker 才作为重复产物依据，未完成 sibling 不会挡住正常拆分。
def _is_verified_leaf(item: Any, leaf_like: Callable[[Any], bool]) -> bool:
    return (
        leaf_like(item)
        and str(getattr(item, "status", "") or "").upper() == "DONE"
        and str(getattr(item, "verification_status", "") or "").upper() == "VERIFIED"
    )


# LLM: _child_target_tokens extracts explicit output filenames from specs, task text, and output refs.
# 函数用途: 识别 register.html、cart.py 这类具体目标文件；用于 leaf 去重，不把普通领域词当产物。
def _child_target_tokens(item: Any) -> set[str]:
    text = " ".join([
        str(getattr(item, "agent_name", "") or ""),
        str(getattr(item, "role", "") or ""),
        str(getattr(item, "goal", "") or ""),
        " ".join(str(root or "") for root in getattr(item, "extra_write_roots", []) or []),
    ])
    targets = set(_target_tokens_from_text(text))
    targets.update(_target_tokens_from_output_json(getattr(item, "output_json", "") or ""))
    return targets


# LLM: _target_tokens_from_output_json reads artifact path refs without expanding artifact contents.
# 函数用途: 从 output.json 的 artifacts 里提取文件名，让已完成 leaf 的真实产物参与去重。
def _target_tokens_from_output_json(output_json: str) -> set[str]:
    path = Path(str(output_json or ""))
    if not output_json or not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()
    targets: set[str] = set()
    for artifact in payload.get("artifacts") or []:
        targets.update(_target_tokens_from_artifact(artifact))
    return targets


# LLM: _target_tokens_from_artifact normalizes string or dict artifact path refs.
# 函数用途: 兼容 runner 输出的常见 artifact 形状，只保存文件名 token，不保存正文。
def _target_tokens_from_artifact(artifact: Any) -> set[str]:
    if isinstance(artifact, str):
        return set(_target_tokens_from_text(artifact))
    if not isinstance(artifact, dict):
        return set()
    values = [
        artifact.get(key)
        for key in ("path", "file", "file_path", "artifact_path", "ref", "href")
    ]
    return {
        token
        for value in values
        for token in _target_tokens_from_text(str(value or ""))
    }


# LLM: _target_tokens_from_text keeps dedupe tied to concrete local files.
# 函数用途: 从文本中提取常见代码/文档/网页文件名；跳过目录根和 URL。
def _target_tokens_from_text(text: str) -> list[str]:
    tokens: list[str] = []
    for match in re.findall(r"[\w./~:-]+\.(?:html|css|js|ts|tsx|jsx|py|md|json|txt|csv|yaml|yml)", text):
        if "://" in match:
            continue
        token = Path(match.strip("`'\" ,;:，。；：、)]}）】")).name.lower()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


# LLM: _first_overlapping_target keeps duplicate leaf errors deterministic.
# 函数用途: 返回新 leaf 和已完成 leaf 产物文件名的第一个交集。
def _first_overlapping_target(targets: set[str], seen_targets: list[set[str]]) -> str:
    for seen in seen_targets:
        overlap = sorted(targets & seen)
        if overlap:
            return overlap[0]
    return ""
