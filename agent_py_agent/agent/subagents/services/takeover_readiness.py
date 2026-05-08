# LLM: Takeover readiness builds read-only recovery packets from task refs without loading artifact bodies.
# 模块用途: 生成接管前必读包，告诉后续代理先读哪些恢复线索、哪些内容不能当事实源。

from __future__ import annotations

"""Takeover readiness packet builders for failed or blocked subagent runs."""

import json
from pathlib import Path
from typing import Any

from ..models import SubAgentTask

_BOUNDARY_NOTES = [
    "summary_is_not_verified_fact",
    "finding_requires_evidence_packet_or_acceptance",
    "artifact_ref_is_pointer_not_prompt_content",
    "blackboard_is_not_source_of_truth",
]


# LLM: build_takeover_readiness_packet returns refs and ordering only; it never reads full artifact content.
# 函数用途: 为接管/恢复代理整理必读 refs、状态和边界提示，避免接手者到处散扫或误把摘要当事实。
def build_takeover_readiness_packet(task: SubAgentTask) -> dict[str, object]:
    manifest_records = _read_manifest_records(task.agent_run_artifact_manifest_jsonl)
    read_order = _recommended_read_order(task)
    return {
        "schema_name": "subagent_takeover_readiness_packet",
        "schema_version": 1,
        "run": _run_identity(task),
        "status": task.status,
        "current_step": task.current_step or task.status,
        "latest_summary": task.latest_summary,
        "blockers": _unique_strings(task.blockers),
        "failure_handoff_ref": task.failure_handoff_json if task.failure_handoff.run_id else "",
        "checkpoint_refs": _checkpoint_refs(task),
        "artifact_refs": _unique_strings(task.artifact_refs),
        "artifact_manifest_ref": task.agent_run_artifact_manifest_jsonl or task.task_artifact_manifest_jsonl,
        "artifact_manifest_records": manifest_records,
        "evidence_refs": _unique_strings(task.evidence_refs),
        "status_report_ref": task.status_report_json,
        "recommended_next_action": _recommended_next_action(task),
        "recommended_read_order": read_order,
        "boundary_notes": list(_BOUNDARY_NOTES),
        "reserved": {
            "auto_takeover": False,
            "reads_artifact_bodies": False,
            "packet_is_recovery_index": True,
        },
    }


# LLM: render_takeover_readiness_markdown mirrors the machine packet for human status/takeover views.
# 函数用途: 把接管必读包渲染成人能扫读的 Markdown；仍只展示 refs，不展开 artifact 正文。
def render_takeover_readiness_markdown(packet: dict[str, object]) -> str:
    run = _dict_value(packet.get("run"))
    checkpoint_refs = _dict_value(packet.get("checkpoint_refs"))
    return "\n".join(
        [
            "# TAKEOVER_READINESS",
            "",
            f"- run_id: {run.get('run_id', '')}",
            f"- parent_id: {run.get('parent_id', '') or 'none'}",
            f"- root_id: {run.get('root_id', '') or 'none'}",
            f"- status: {packet.get('status', '')}",
            f"- current_step: {packet.get('current_step', '')}",
            f"- failure_handoff_ref: {packet.get('failure_handoff_ref', '') or 'none'}",
            f"- legacy_checkpoint: {checkpoint_refs.get('legacy_checkpoint', '') or 'none'}",
            f"- agent_run_checkpoint: {checkpoint_refs.get('agent_run_checkpoint', '') or 'none'}",
            "",
            "## 建议读取顺序",
            _render_list(_string_list(packet.get("recommended_read_order"))),
            "",
            "## 阻塞项",
            _render_list(_string_list(packet.get("blockers"))),
            "",
            "## Artifact Refs",
            _render_list(_string_list(packet.get("artifact_refs"))),
            "",
            "## Artifact Manifest",
            f"- manifest: {packet.get('artifact_manifest_ref', '') or 'none'}",
            _render_manifest_rows(_list_dicts(packet.get("artifact_manifest_records"))),
            "",
            "## 边界提示",
            _render_list(_string_list(packet.get("boundary_notes"))),
            "",
        ]
    )


# LLM: write_takeover_readiness_files persists the recovery index beside other task reports.
# 函数用途: 写入机器可读和人类可读的接管入口；只写 refs 和摘要，不复制 artifact 正文。
def write_takeover_readiness_files(task: SubAgentTask) -> dict[str, object]:
    packet = build_takeover_readiness_packet(task)
    if task.takeover_readiness_json:
        Path(task.takeover_readiness_json).write_text(
            json.dumps(packet, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if task.takeover_readiness_md:
        Path(task.takeover_readiness_md).write_text(render_takeover_readiness_markdown(packet), encoding="utf-8")
    return packet


# LLM: _run_identity captures ownership refs for the takeover packet without resolving parents.
# 函数用途: 记录当前 run/root/workspace 身份，让接管者知道恢复包属于谁。
def _run_identity(task: SubAgentTask) -> dict[str, object]:
    return {
        "run_id": task.id,
        "parent_id": task.parent_id,
        "root_id": task.root_id or task.id,
        "task_dir": task.task_dir,
        "agent_run_workspace_dir": task.agent_run_workspace_dir,
    }


# LLM: _checkpoint_refs groups compact and legacy checkpoint pointers for ordered recovery.
# 函数用途: 收集旧工单和 run workspace 的 checkpoint/compact 引用。
def _checkpoint_refs(task: SubAgentTask) -> dict[str, str]:
    return {
        "legacy_checkpoint": task.checkpoint_json or task.checkpoint_ref,
        "agent_run_checkpoint": task.agent_run_checkpoint_json,
        "compact_metadata": task.agent_run_latest_compaction_metadata_json,
        "compact_summary": task.agent_run_latest_compaction_summary_md,
    }


# LLM: _recommended_read_order lists refs to read later without opening them now.
# 函数用途: 生成接管者的建议读取顺序，从 failure handoff 到 artifact refs 逐步展开。
def _recommended_read_order(task: SubAgentTask) -> list[str]:
    refs = [
        task.failure_handoff_json if task.failure_handoff.run_id else "",
        task.takeover_readiness_json,
        task.agent_run_checkpoint_json,
        task.checkpoint_json or task.checkpoint_ref,
        task.status_report_json,
        task.agent_run_artifact_manifest_jsonl or task.task_artifact_manifest_jsonl,
        *task.evidence_refs,
        *task.artifact_refs,
    ]
    return _unique_strings(refs)


# LLM: _recommended_next_action reuses handoff/status advice before falling back to blockers.
# 函数用途: 给接管者一个下一步提示，但不自动执行任何接管动作。
def _recommended_next_action(task: SubAgentTask) -> str:
    if task.failure_handoff.recommended_next_action:
        return task.failure_handoff.recommended_next_action
    if task.latest_status_report.next_recommended_action:
        return task.latest_status_report.next_recommended_action
    if task.blockers:
        return f"先处理阻塞项：{task.blockers[0]}"
    return "按 recommended_read_order 读取 refs 后再决定是否接管。"


# LLM: _read_manifest_records loads artifact manifest metadata but never artifact bodies.
# 函数用途: 读取 manifest JSONL 的安全字段，跳过损坏行和缺失文件。
def _read_manifest_records(path_text: str) -> list[dict[str, object]]:
    if not path_text:
        return []
    path = Path(path_text)
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(_safe_manifest_record(item))
    return records


# LLM: _safe_manifest_record whitelists manifest fields safe for prompt/status display.
# 函数用途: 只保留 ref、path、exists、size、hash 等元数据，避免正文混入接管包。
def _safe_manifest_record(item: dict[str, object]) -> dict[str, object]:
    keys = [
        "ref",
        "path",
        "exists",
        "size_bytes",
        "sha256",
        "content_externalized",
        "resolution_status",
    ]
    return {
        key: item.get(key, "" if key not in {"exists", "content_externalized", "size_bytes"} else False)
        for key in keys
    }


# LLM: _unique_strings preserves read order while removing empty and duplicate refs.
# 函数用途: 给接管包里的引用列表去重，保留首次出现顺序。
def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


# LLM: _dict_value normalizes loose packet sections for Markdown rendering.
# 函数用途: 渲染时安全读取 dict 字段，非 dict 输入返回空字典。
def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


# LLM: _string_list normalizes packet list fields into printable strings.
# 函数用途: 渲染 Markdown 前把列表字段转成字符串列表，过滤空项。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


# LLM: _list_dicts accepts only dict manifest rows for Markdown rendering.
# 函数用途: 过滤非 dict 记录，避免坏数据影响接管包展示。
def _list_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# LLM: _render_list keeps empty Markdown sections explicit and compact.
# 函数用途: 将字符串列表渲染为 Markdown bullet，空列表显示“暂无”。
def _render_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 暂无"


# LLM: _render_manifest_rows prints manifest metadata only, never artifact content.
# 函数用途: 渲染 artifact manifest 摘要行，展示 ref、exists、size 和 hash。
def _render_manifest_rows(records: list[dict[str, Any]]) -> str:
    if not records:
        return "- records: 暂无"
    rows = []
    for item in records:
        rows.append(
            "- "
            f"ref={item.get('ref', '')}; "
            f"exists={item.get('exists', False)}; "
            f"size={item.get('size_bytes', 0)}; "
            f"sha256={item.get('sha256', '')}"
        )
    return "\n".join(rows)
