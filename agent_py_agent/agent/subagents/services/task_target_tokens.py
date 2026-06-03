
"""Extract compact target labels from structured subagent output refs.

The returned tokens are for boards and summaries only. They help a parent see
which files a subagent appears to have touched, but they do not own scheduling,
deduplication, or delivery acceptance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .agent_run_state import read_agent_state_payload


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


def _target_tokens_from_attributes(attributes: dict[str, object]) -> set[str]:
    if not isinstance(attributes, dict):
        return set()
    targets: set[str] = set()
    for field in ("output_refs", "output_files", "artifact_refs"):
        targets.update(_target_tokens_from_output_values(attributes.get(field)))
    return targets


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


def _task_result_text(item: Any) -> str:
    direct = str(getattr(item, "result", "") or "")
    if direct:
        return direct
    task_dir = Path(str(getattr(item, "task_dir", "") or ""))
    if not str(task_dir):
        return ""
    try:
        payload = read_agent_state_payload(task_dir / "task.json")
    except (OSError, json.JSONDecodeError, TypeError, FileNotFoundError):
        return ""
    return str(payload.get("result") or "")


def _target_tokens_from_text(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _target_file_match_candidates(text):
        token = Path(match.strip("`'\" ,;:，。；：、)]}）】")).name.lower()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _target_file_match_candidates(text: str) -> list[str]:
    import re

    return [
        match
        for match in re.findall(r"[\w./~:-]+\.(?:html|css|js|ts|tsx|jsx|py|md|json|txt|csv|yaml|yml)", str(text or ""))
        if "://" not in match
    ]
