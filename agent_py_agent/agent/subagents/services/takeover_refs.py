# LLM: Takeover refs preserve source task handoff state for replacement runners.
# 模块用途: 复制 source run 的交接 refs、写入根、链路深度和 JSON-like payload。

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from pathlib import Path

from ..models import SubAgentTask


def takeover_attributes(source: SubAgentTask) -> dict[str, object]:
    attrs = structured_payload(source.attributes)
    return attrs if isinstance(attrs, dict) else {}


def structured_payload(value: object) -> object:
    if is_dataclass(value):
        return asdict(value)
    return deepcopy(value)


def default_takeover_plan() -> list[str]:
    return [
        "读取 takeover_source_refs.latest_continue_packet；不可用则读 checkpoint/summary",
        "继续原 run 未完成的 current_step/next_action",
        "复用原任务目录和 artifacts refs 写入结果与证据",
        "完成后写 final_report/output 并等待最终收口",
    ]


def takeover_agent_name(source: SubAgentTask) -> str:
    base = str(source.agent_name or source.id or "worker").strip()
    return f"{base}-takeover"


def source_refs(source: SubAgentTask) -> dict[str, str]:
    refs = {
        "task_dir": source.task_dir,
        "agent_run_workspace_dir": source.agent_run_workspace_dir,
        "agent_run_artifacts_dir": source.agent_run_artifacts_dir,
        "task_workspace_artifacts_dir": source.task_workspace_artifacts_dir,
        "latest_continue_packet": latest_continue_packet_ref(source),
        "checkpoint": source.agent_run_checkpoint_json,
        "summary": source.agent_run_summary_md,
        "output_json": source.output_json,
        "runner_result": source.runner_result_json,
        "takeover_readiness": source.takeover_readiness_json,
    }
    return {key: str(value) for key, value in refs.items() if str(value or "").strip()}


def latest_continue_packet_ref(source: SubAgentTask) -> str:
    if source.agent_run_latest_session_continue_packet_json:
        return source.agent_run_latest_session_continue_packet_json
    if not source.agent_run_compactions_dir:
        return ""
    return str(Path(source.agent_run_compactions_dir) / "session" / "latest_continue_packet.json")


def takeover_write_roots(source: SubAgentTask) -> list[str]:
    return unique_strings(
        [
            *explicit_source_write_roots(source),
            *existing_dirs(
                [
                    source.task_dir,
                    source.agent_run_artifacts_dir,
                    source.task_workspace_artifacts_dir,
                    source.task_workspace_shared_dir,
                ]
            ),
        ]
    )


def explicit_source_write_roots(source: SubAgentTask) -> list[str]:
    return unique_strings([str(item) for item in source.allowed_write_roots if str(item or "").strip()])


def takeover_chain_limit(manager: object) -> int:
    try:
        return max(0, int(getattr(manager, "takeover_chain_max_depth", 0) or 0))
    except (TypeError, ValueError):
        return 0


def takeover_chain_depth(source: SubAgentTask) -> int:
    try:
        return max(0, int((source.attributes or {}).get("takeover_chain_depth", 0) or 0))
    except (TypeError, ValueError):
        return 0


def takeover_lineage_root(source: SubAgentTask) -> str:
    attrs = source.attributes or {}
    root = str(attrs.get("takeover_lineage_root_run_id") or "").strip()
    return root or source.id


def existing_dirs(values: list[str]) -> list[str]:
    dirs: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and Path(text).is_dir() and text not in dirs:
            dirs.append(text)
    return dirs


def unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in values if str(item or "").strip()))


def unique_structured(values: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    unique: list[dict[str, object]] = []
    for item in values:
        _append_unique_structured(unique, seen, item)
    return unique


def _append_unique_structured(unique: list[dict[str, object]], seen: set[str], item: object) -> None:
    if not isinstance(item, dict):
        return
    payload = deepcopy(item)
    try:
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        key = str(payload)
    if key in seen:
        return
    seen.add(key)
    unique.append(payload)
