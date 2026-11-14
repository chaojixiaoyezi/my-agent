"""授权代理树内的原验证、裁决及交接账小窗；只作展示，不签发恢复或审批。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ...common.nofollow_fs import read_bytes_snapshot_beneath, read_tail_snapshot_beneath
from ...common.opaque_id import validate_opaque_id
from ...verification.repository import VerificationContext, VerificationEvidenceRepository
from ..runtime.owner_roots import runtime_owner_root

# 每次查询只取最近观察事件和对应的少量精确引用，不回退全历史。
_COORDINATION_WINDOW_MAX_RECORD_COUNT = 16
# 原单文件读取的总字节上限；case、交接和裁决分别守同一固定小窗。
_COORDINATION_WINDOW_MAX_BYTES = 65536


def coordination_source_facts(agent, nodes, *, deadline=None):
    """只消费已授权节点；canonical 任务身份缺失时不猜线程或父任务。"""
    owner_root = runtime_owner_root(agent)
    owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or owner_root.name)
    manager = getattr(agent, "subagents", None)
    allowed = frozenset(str(node.get("run_id") or "") for node in nodes)
    result, windows = {}, {}
    deadline = time.monotonic() + 1.0 if deadline is None else deadline
    budget = {"references": _COORDINATION_WINDOW_MAX_RECORD_COUNT, "deadline": deadline}
    for node in nodes:
        run_id = str(node.get("run_id") or "")
        if not run_id:
            continue
        facts = result.setdefault(run_id, {})
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("coordination read budget exhausted")
            task = manager.load(run_id)
            if task.owner != owner or task.id != run_id:
                raise ValueError("canonical task identity mismatch")
            attrs = task.attributes
            thread = str(attrs.get("conversation_thread_id") or "")
            root = str(attrs.get("conversation_task_id") or task.root_id or "")
            if not thread or not root:
                facts["verification"] = facts["coordination"] = {"state": "not_applicable"}
                continue
            facts["verification"] = VerificationEvidenceRepository(owner_root).progress_metadata(
                VerificationContext(owner, thread, run_id),
                requirement_id=str(attrs.get("review_requirement_id") or ""),
                related_task_ids=allowed | {root})
            key = thread, root
            if key not in windows:
                windows[key] = _coordination_window(agent, thread, root, allowed, budget)
            facts["coordination"] = windows[key].get(run_id, {"state": "unknown", "reason_code": "NO_MATCHING_RECENT_REFERENCE"})
        except (OSError, sqlite3.Error, AttributeError, KeyError, TypeError, ValueError) as exc:
            failure = {"state": "unreadable", "reason_code": "COORDINATION_SOURCE_READ_FAILED",
                       "error_type": type(exc).__name__,
                       "next_action": "核对原任务与原账可读性；不要仅因进展缺失重派或重放工具。"}
            facts["coordination"] = failure
            facts.setdefault("verification", dict(failure))
    return result


def _snapshot(path, *, tail=False):
    path = Path(path).absolute()
    reader = read_tail_snapshot_beneath if tail else read_bytes_snapshot_beneath
    snapshot = reader(path.anchor, path.parts[1:], max_bytes=_COORDINATION_WINDOW_MAX_BYTES, require_dir_fd=True)
    if snapshot is not None and snapshot.stat.st_nlink != 1:
        raise ValueError("canonical source has multiple hard links")
    return snapshot


def _tail_rows(path):
    snapshot = _snapshot(path, tail=True)
    if snapshot is None:
        return [], "missing"
    content = snapshot.content
    if content and not content.endswith(b"\n"):
        raise ValueError("incomplete canonical record")
    truncated = snapshot.stat.st_size > len(content)
    if truncated:
        content = content.partition(b"\n")[2]
    # 字节窗与行数窗双重有界；不把未覆盖的较老记录当作不存在。
    lines = content.splitlines()
    rows = [json.loads(line) for line in lines[-_COORDINATION_WINDOW_MAX_RECORD_COUNT:] if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("invalid canonical record")
    return rows, "partial" if truncated or len(lines) > _COORDINATION_WINDOW_MAX_RECORD_COUNT else "complete"


def _coordination_window(agent, thread, root, allowed, budget):
    store = getattr(agent, "collaboration_store", None)
    conversation = getattr(agent, "conversation_store", None)
    if store is None or conversation is None:
        return {run: {"state": "not_applicable"} for run in allowed}
    events, errors, coverage = conversation.observations.recent_window_report(
        thread, limit=_COORDINATION_WINDOW_MAX_RECORD_COUNT, max_bytes=_COORDINATION_WINDOW_MAX_BYTES, max_segments=2)
    result = {run: {"state": "unknown", "reason_code": "NO_MATCHING_RECENT_REFERENCE",
                    "observation_window": coverage, "read_errors": len(errors)} for run in allowed}
    cases, seen = {}, set()
    for event in events:
        if time.monotonic() >= budget["deadline"] or budget["references"] <= 0:
            for fact in result.values():
                fact["reference_window"] = "partial"
            break
        if event.thread_id != thread or event.root_task_id != root:
            continue
        metadata = event.metadata
        if not isinstance(metadata, dict) or not metadata.get("case_id"):
            continue
        case_id = validate_opaque_id(metadata["case_id"], kind="case_id")
        budget["references"] -= 1
        if case_id not in cases:
            snapshot = _snapshot(store.root / "cases" / f"{case_id}.json")
            case = json.loads(snapshot.content) if snapshot is not None else {}
            if not isinstance(case, dict) or case.get("thread_id") != thread or case.get("task_id") != root:
                continue
            cases[case_id] = case
            decisions, decision_coverage = _tail_rows(store.root / "decisions" / f"{case_id}.jsonl")
            case_actor = str(case.get("created_by") or "")
            if case_actor in allowed:
                fact = result[case_actor]
                fact.update(state="read", reason_code="EXACT_CANONICAL_REFERENCE")
                fact.setdefault("case_decisions", []).append({"case_id": case_id, "scope": "case",
                    "coverage": decision_coverage,
                    "records": [{key: row.get(key) for key in ("decision_id", "decision_type", "created_at")}
                                for row in decisions if row.get("case_id") == case_id]})
        if event.event_type != "handoff_item_published" or not metadata.get("item_id"):
            continue
        item_id = validate_opaque_id(metadata["item_id"], kind="item_id")
        if (case_id, item_id) in seen:
            continue
        seen.add((case_id, item_id))
        rows, item_coverage = _tail_rows(store.root / "handoff_items" / f"{case_id}.jsonl")
        item = next((row for row in reversed(rows) if row.get("item_id") == item_id and row.get("case_id") == case_id), None)
        if item is None or item.get("actor_kind") != "internal_run" or item.get("responsible_actor") not in allowed:
            creator = str(cases[case_id].get("created_by") or "")
            if creator in allowed:
                result[creator]["handoff_reference_coverage"] = "unknown"
            continue
        run = item["responsible_actor"]
        fact = result[run]
        fact.update(state="read", reason_code="EXACT_CANONICAL_REFERENCE", observation_window=coverage)
        fact.setdefault("handoffs", []).append({"case_id": case_id, "item_id": item_id, "state": item.get("state"),
            "updated_at": item.get("updated_at"), "coverage": item_coverage, "observation_id": event.observation_id})
    return result
