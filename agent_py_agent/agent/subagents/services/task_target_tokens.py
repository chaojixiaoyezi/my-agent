# LLM: task target tokens expose structured artifact refs without duplicate-output gates.
# 模块用途: 从任务机器字段和 runner 输出中提取实际产物文件名，供看板展示使用；不做调度阻断。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# LLM: task_actual_target_tokens prefers structured runner refs before any fallback file refs.
# 函数用途: 返回任务实际触碰的产物文件名；只给状态看板使用，不参与调度 hard gate。
def task_actual_target_tokens(item: Any) -> set[str]:
    attribute_targets = _target_tokens_from_attributes(getattr(item, "attributes", {}) or {})
    if attribute_targets:
        return attribute_targets
    output_targets = _target_tokens_from_output_json(getattr(item, "output_json", "") or "")
    if output_targets:
        return output_targets
    result_targets = _target_tokens_from_result_json(_task_result_text(item))
    if result_targets:
        return result_targets
    return _target_tokens_from_output_values(getattr(item, "extra_write_roots", []) or [])


# LLM: _target_tokens_from_attributes reads machine output refs for target ownership.
# 函数用途: 从 attributes.output_refs/output_files/artifact_refs 提取目标文件名；不解析 goal 文本。
def _target_tokens_from_attributes(attributes: dict[str, object]) -> set[str]:
    if not isinstance(attributes, dict):
        return set()
    targets: set[str] = set()
    for field in ("output_refs", "output_files", "artifact_refs"):
        targets.update(_target_tokens_from_output_values(attributes.get(field)))
    return targets


# LLM: _target_tokens_from_output_json reads artifact path refs without expanding artifact contents.
# 函数用途: 从 output.json 的 artifacts 里提取文件名，让看板显示真实产物 refs。
def _target_tokens_from_output_json(output_json: str) -> set[str]:
    path = Path(str(output_json or ""))
    if not output_json or not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()
    targets: set[str] = set()
    targets.update(_target_tokens_from_output_values(payload.get("artifact_path")))
    targets.update(_target_tokens_from_output_values(payload.get("artifacts")))
    targets.update(_target_tokens_from_output_values(payload.get("files_modified")))
    targets.update(_target_tokens_from_output_values(payload.get("patches")))
    return targets


# LLM: _target_tokens_from_result_json recovers refs from the runner's explicit SUBAGENT_RESULT block.
# 函数用途: output.json 未带 files_modified 时，从 task.result 的结构化结果补回产物 refs。
def _target_tokens_from_result_json(result_text: str) -> set[str]:
    text = str(result_text or "")
    import re

    match = re.search(r"\[SUBAGENT_RESULT\]\s*(\{.*\})\s*\[/SUBAGENT_RESULT\]", text, re.DOTALL)
    if not match:
        return set()
    try:
        payload = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return set()
    targets: set[str] = set()
    targets.update(_target_tokens_from_output_values(payload.get("artifact_path")))
    targets.update(_target_tokens_from_output_values(payload.get("artifacts")))
    targets.update(_target_tokens_from_output_values(payload.get("files_modified")))
    targets.update(_target_tokens_from_output_values(payload.get("patches")))
    return targets


# LLM: _target_tokens_from_output_values supports the small structured ref shapes models commonly emit.
# 函数用途: 递归兼容 string/list/dict 形式的 path/file/ref 字段，只提取文件名 token。
def _target_tokens_from_output_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_target_tokens_from_text(value))
    if isinstance(value, dict):
        values = [
            value.get(key)
            for key in ("path", "file", "file_path", "artifact_path", "ref", "href")
        ]
        return {token for item in values for token in _target_tokens_from_output_values(item)}
    if isinstance(value, list):
        return {token for item in value for token in _target_tokens_from_output_values(item)}
    return set()


# LLM: _task_result_text avoids adding result text to board rows while still letting status logic recover refs.
# 函数用途: 优先用 task.result；看板条目只有 task_dir 时，读取小型 task.json 里的 result 字段作为回退。
def _task_result_text(item: Any) -> str:
    direct = str(getattr(item, "result", "") or "")
    if direct:
        return direct
    task_dir = Path(str(getattr(item, "task_dir", "") or ""))
    if not str(task_dir):
        return ""
    try:
        payload = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    return str(payload.get("result") or "")


# LLM: _target_tokens_from_text keeps board target labels tied to concrete local files.
# 函数用途: 从文本中提取常见代码/文档/网页文件名；跳过目录根和 URL。
def _target_tokens_from_text(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _target_file_match_candidates(text):
        token = Path(match.strip("`'\" ,;:，。；：、)]}）】")).name.lower()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


# LLM: _target_file_match_candidates keeps regex scanning outside the public token normalizer.
# 函数用途: 从输出目标片段中产出文件路径候选，并过滤 URL，降低提取函数复杂度。
def _target_file_match_candidates(text: str) -> list[str]:
    import re

    return [
        match
        for match in re.findall(r"[\w./~:-]+\.(?:html|css|js|ts|tsx|jsx|py|md|json|txt|csv|yaml|yml)", str(text or ""))
        if "://" not in match
    ]
