
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .common.nofollow_fs import open_readonly_file_beneath
from .common.value_parsing import dedupe_strings
from .run_intent_index import bootstrap_locator, intent_candidates
from .runtime_errors import DataCorruptionError, runtime_error_report

_PATH_RE = re.compile(r"(~?/[^ \t\r\n，。；;：:、)）\]】\"'<>`]+)")
# 唤醒事实最多来自 12 个文件：限制后台唤醒读取范围，防扫描爆炸。
_MAX_FACT_FILES = 12


def build_run_intent(
    *,
    user_prompt: str,
    delivery_contract: dict[str, Any] | None,
    workspace_root: Path,
) -> dict[str, Any]:
    desired = desired_outputs_from_contract(delivery_contract)
    references = reference_roots_from_prompt(
        user_prompt,
        desired_outputs=desired,
        workspace_root=workspace_root,
    )
    return run_intent_payload(reference_roots=references, desired_outputs=desired)


def desired_outputs_from_contract(contract: dict[str, Any] | None) -> list[str]:
    if not isinstance(contract, dict):
        return []
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, list | tuple):
        return []
    outputs: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        path = str(
            artifact.get("path")
            or artifact.get("preferred_path")
            or artifact.get("target_path")
            or artifact.get("output_path")
            or ""
        ).strip()
        if path:
            outputs.append(path)
    return dedupe_strings(outputs)


def reference_roots_from_prompt(
    user_prompt: str,
    *,
    desired_outputs: list[str],
    workspace_root: Path,
) -> list[str]:
    if not desired_outputs:
        return []
    desired_paths = [_resolve_path(path, workspace_root) for path in desired_outputs]
    roots: list[str] = []
    for raw in _PATH_RE.findall(user_prompt or ""):
        candidate = _resolve_path(raw, workspace_root)
        if not candidate.exists() or not candidate.is_dir():
            continue
        if any(_same_or_related(candidate, output) for output in desired_paths):
            continue
        roots.append(str(candidate))
    return dedupe_strings(roots)


def run_intent_payload(*, reference_roots: list[str], desired_outputs: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "run_intent.v1",
        "soft_only": True,
        "reference_roots": _field_payload(reference_roots),
        "desired_outputs": _field_payload(desired_outputs),
    }


def latest_run_intent(workspace_root: Path, *, fact_roots: list[Path] | None = None) -> dict[str, Any]:
    return latest_run_intent_report(workspace_root, fact_roots=fact_roots)[0]


def latest_run_intent_report(
    workspace_root: Path,
    *,
    fact_roots: list[Path] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    load_errors: list[dict[str, Any]] = []
    for facts_dir in _runtime_fact_dirs(workspace_root, fact_roots):
        intent, errors = _latest_run_intent_from_dir_report(facts_dir)
        load_errors.extend(errors)
        if intent:
            return intent, load_errors
    return {}, load_errors


def _latest_run_intent_from_dir(facts_dir: Path) -> dict[str, Any]:
    return _latest_run_intent_from_dir_report(facts_dir)[0]


# LLM: 只消费准确身份或已发布的有界候选；热miss披露unknown，绝不枚举历史目录。
#   缺失locator由read_locator报RUN_INTENT_LOCATOR_MISSING；已有locator无匹配行或匹配运行未记录desired_outputs只是没有意图，不算读取失败。
# 函数用途: 读取本事实根内最近或当前运行意图，保留坏文件诊断。
def _latest_run_intent_from_dir_report(facts_dir: Path, *, request_id: str = "", run_id: str = "") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates, load_errors = intent_candidates(facts_dir, limit=_MAX_FACT_FILES, request_id=request_id, run_id=run_id)
    for path in candidates:
        payload, load_error = _runtime_fact_payload(path)
        if load_error:
            load_errors.append(load_error)
            continue
        if (request_id or run_id) and not (request_id and payload.get("request_id") == request_id
                                           or run_id and payload.get("run_id") == run_id):
            continue
        intent = payload.get("run_intent") if isinstance(payload, dict) else None
        if isinstance(intent, dict) and _items(intent.get("desired_outputs")):
            return intent, load_errors
    return {}, load_errors


# LLM: 只有宿主身份可指定当前意图；无身份时用locator，返回仍为原非阻断软提示。
# 函数用途: 为本轮写入反馈准确输出/参考目录，不借另一个较新运行推断本轮意图。
def reference_write_feedback(*, workspace_root: Path, target: Path, fact_roots: list[Path] | None = None,
                             request_id: str = "", run_id: str = "") -> dict[str, Any]:
    if request_id or run_id:
        intent, load_errors = {}, []
        for facts_dir in _runtime_fact_dirs(workspace_root, fact_roots):
            intent, errors = _latest_run_intent_from_dir_report(facts_dir, request_id=request_id, run_id=run_id)
            load_errors.extend(errors)
            if intent:
                break
    else:
        intent, load_errors = latest_run_intent_report(workspace_root, fact_roots=fact_roots)
    desired = _items(intent.get("desired_outputs"))
    references = _items(intent.get("reference_roots"))
    if not desired or not references:
        if load_errors:
            return {
                "severity": "soft",
                "blocking": False,
                "message": "软提醒：运行意图账本读取失败；不要把它当成没有目标路径或参考目录，请按用户原话确认输出位置。",
                "run_intent_load_errors": load_errors[:5],
            }
        return {}
    target = target.resolve(strict=False)
    desired_paths = [_resolve_path(path, workspace_root) for path in desired]
    if any(_same_or_related(target, output) for output in desired_paths):
        return {}
    reference_paths = [_resolve_path(path, workspace_root) for path in references]
    if not any(_is_relative_to(target, root) for root in reference_paths):
        return {}
    return {
        "severity": "soft",
        "blocking": False,
        "message": (
            "软提醒：这个写入路径看起来位于本轮参考目录内，不是明确的目标产物路径。"
            f"如果你正在写最终产物，请写到 desired_outputs：{'; '.join(desired[:5])}"
        ),
        "desired_outputs": desired[:8],
        "reference_roots": references[:8],
    }


def _field_payload(items: list[str]) -> dict[str, Any]:
    return {
        "items": dedupe_strings(items),
        "source_status": "recorded" if items else "not_recorded",
    }


def _items(value: Any) -> list[str]:
    payload = value if isinstance(value, dict) else {}
    raw = payload.get("items")
    if not isinstance(raw, list | tuple):
        return []
    return dedupe_strings([str(item).strip() for item in raw if str(item).strip()])


def _resolve_path(path: str, workspace_root: Path) -> Path:
    raw = Path(str(path).strip()).expanduser()
    if not raw.is_absolute():
        raw = workspace_root / raw
    return raw.resolve(strict=False)


def _runtime_fact_dirs(workspace_root: Path, fact_roots: list[Path] | None) -> list[Path]:
    roots = [*(fact_roots or []), workspace_root]
    dirs: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(Path(root).expanduser().resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        dirs.append(Path(key) / "memory_archive" / "runtime_facts")
    return dirs


# LLM: 候选路径仅由同根目录名产生，逐级no-follow；原子task.json一次读取保证意图字段不混代。
# 函数用途: 读取一个准确事实文件，缺失/坏内容仍给原结构化诊断。
def _runtime_fact_payload(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        descriptor = open_readonly_file_beneath(path.parent.parent, (path.parent.name, path.name))
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, _load_error(exc, path)
    if not isinstance(payload, dict):
        return {}, _load_error(DataCorruptionError(f"runtime fact root must be a JSON object: {path}"), path)
    return payload, None


def _load_error(exc: BaseException, path: Path) -> dict[str, Any]:
    report = runtime_error_report(exc, context="run_intent.runtime_fact.read")
    report["path"] = str(path)
    return report


# LLM: 冷装配只迁移最多12个旧事实；大目录保留明确缺失状态，不在write热路径重试枚举。
# 函数用途: 在注册独立写工具时接住小型旧事实集，正常事实writer负责之后的locator更新。
def prepare_run_intent_locators(workspace_root: Path, *, fact_roots: list[Path] | None = None) -> None:
    for facts_dir in _runtime_fact_dirs(workspace_root, fact_roots):
        try:
            bootstrap_locator(facts_dir, limit=_MAX_FACT_FILES)
        except OSError:
            # 非关键投影的装配失败不影响写工具，反馈读口会披露unknown。
            continue


def _same_or_related(path: Path, target: Path) -> bool:
    return path == target or _is_relative_to(path, target) or _is_relative_to(target, path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "build_run_intent",
    "desired_outputs_from_contract",
    "latest_run_intent",
    "latest_run_intent_report",
    "reference_roots_from_prompt",
    "reference_write_feedback",
    "run_intent_payload",
]
