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

_REFERENCE_FILE_HINT_RE = re.compile(
    r"(?:引入|引用|链接到|链接|导入|加载|依赖|link(?:s)?\s+to|include|import|load|use(?:s|d)?)",
    re.IGNORECASE,
)


# LLM: LeafTargetDedupeRequest bundles all state needed for same-parent leaf output dedupe.
# 类用途: 集中保存 manager、parent、调度请求和 leaf 判断函数，避免去重入口散参扩张。
@dataclass(frozen=True)
class LeafTargetDedupeRequest:
    manager: Any
    parent: SubAgentTask
    schedule_request: Any
    leaf_like: Callable[[Any], bool]


# LLM: duplicate_verified_leaf_target_warnings audits repeated output targets without blocking collaboration.
# 函数用途: 同父级已有 DONE/VERIFIED leaf 写过同一目标文件时，返回审计提示；不阻断后续修复或协作写入。
def duplicate_verified_leaf_target_warnings(request: LeafTargetDedupeRequest) -> list[str]:
    seen_targets = _completed_leaf_targets(request)
    if not seen_targets:
        return []
    warnings: list[str] = []
    for spec in request.schedule_request.child_specs:
        warning = _duplicate_target_warning(spec, seen_targets, request.leaf_like)
        if warning and warning not in warnings:
            warnings.append(warning)
    return warnings


# LLM: duplicate_verified_leaf_target_reason preserves the legacy query shape for tests and callers.
# 函数用途: 兼容旧调用方需要单个原因字符串的场景；调度主路径只把它作为 warning 使用。
def duplicate_verified_leaf_target_reason(request: LeafTargetDedupeRequest) -> str:
    warnings = duplicate_verified_leaf_target_warnings(request)
    return warnings[0] if warnings else ""


# LLM: _duplicate_target_warning keeps the public warning collector shallow for size guards.
# 函数用途: 判断一个待建 leaf 是否重复已完成产物；修复类 leaf 和非 leaf 不返回 warning。
def _duplicate_target_warning(item: Any, seen_targets: list[set[str]], leaf_like: Callable[[Any], bool]) -> str:
    if not leaf_like(item) or _is_explicit_repair_leaf(item):
        return ""
    duplicate = _first_overlapping_target(_child_target_tokens(item), seen_targets)
    return f"duplicate_leaf_target:{duplicate}" if duplicate else ""


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


# LLM: _is_explicit_repair_leaf lets parent coordinators create bounded fixes for known bad artifacts.
# 函数用途: 判断新 leaf 是否明确是修复/补齐现有文件；这种任务允许写同一目标，避免真实 E2E 修复链被去重误挡。
def _is_explicit_repair_leaf(item: Any) -> bool:
    text = " ".join([
        str(getattr(item, "agent_name", "") or ""),
        str(getattr(item, "role", "") or ""),
        str(getattr(item, "goal", "") or ""),
    ]).lower()
    repair_tokens = {
        "fix",
        "repair",
        "patch",
        "update",
        "补齐",
        "补全",
        "修复",
        "修补",
        "更新",
        "改正",
    }
    return any(token in text for token in repair_tokens)


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
    for match in _target_file_match_candidates(text):
        token = Path(match.strip("`'\" ,;:，。；：、)]}）】")).name.lower()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


# LLM: _target_file_match_candidates keeps regex scanning outside the public token normalizer.
# 函数用途: 从输出目标片段中产出文件路径候选，并过滤 URL，降低去重函数复杂度。
def _target_file_match_candidates(text: str) -> list[str]:
    matches: list[str] = []
    for segment in _output_target_segments(text):
        matches.extend(
            match
            for match in re.findall(r"[\w./~:-]+\.(?:html|css|js|ts|tsx|jsx|py|md|json|txt|csv|yaml|yml)", segment)
            if "://" not in match
        )
    return matches


# LLM: _output_target_segments drops referenced assets from target ownership extraction.
# 函数用途: 只把引用词之前的文件当作当前 leaf 产物，避免“引入 app.js”抢占共享资产 owner。
def _output_target_segments(text: str) -> list[str]:
    segments: list[str] = []
    for raw in re.split(r"[\n。；;]+", str(text or "")):
        segment = _segment_before_reference_hint(raw)
        if segment:
            segments.append(segment)
    return segments


# LLM: _segment_before_reference_hint keeps the subject file but skips imported/linked files.
# 函数用途: “cart.html 引入 app.js”只保留 cart.html；“引入 app.js”整段跳过。
def _segment_before_reference_hint(text: str) -> str:
    match = _REFERENCE_FILE_HINT_RE.search(text or "")
    if not match:
        return text
    return text[: match.start()]


# LLM: _first_overlapping_target keeps duplicate leaf errors deterministic.
# 函数用途: 返回新 leaf 和已完成 leaf 产物文件名的第一个交集。
def _first_overlapping_target(targets: set[str], seen_targets: list[set[str]]) -> str:
    for seen in seen_targets:
        overlap = sorted(targets & seen)
        if overlap:
            return overlap[0]
    return ""
