# LLM: 增量结论账(收尾一公里根治,E1/E2 实锤):长跑子代理把活干完却在最终结果块上崩,
#   "结论只活在最终一次性输出里"→ 收尾崩/被取消=过程价值清零(某轮 12 个漏报里 10 个
#   来自两条被取消的路)。本工具让"确认一条结论就持久化一条"成为可能:确认即入账,
#   账在 findings.jsonl,收尾崩/重派/取消都不丢;整合/收口层从账合并,最终报告只是
#   账本的汇总视图。代码只搬运内容不定性(claim 写什么由模型定,零自然语言判断)。
# 模块用途: record_finding 工具——把一条已确认结论追加进本 run 的结论账本文件。
from __future__ import annotations

import json
import re
import time
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any

from ...common.audit_activation import (
    AUDIT_RUN_EPOCH_ATTR,
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    current_audit_attributes,
    structured_audit_source_worker_attributes,
)
from ...common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
)
from ...conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from ...runtime_context import current_subagent_attempt_id, current_subagent_run_id
from ...tooling.models import ToolHandlerOutcome

_MAX_CLAIM_CHARS = 2000
_MAX_REFS = 20
# Owner-facing finding wakes may inline one complete ordinary-sized source
# record.  Larger records remain exact in the durable source ledger and are
# exposed by source_ref; never cut a record in half for the report prompt.
_FINDING_EVIDENCE_INLINE_MAX_CHARS = 12_000
_WATCH_ID_PATTERN = re.compile(r"^ws-[0-9a-f]{10}$")
_FINDING_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def execute_record_finding(agent: object, params: dict[str, object]) -> ToolHandlerOutcome:
    claim = str(params.get("claim") or "").strip()
    if not claim:
        return ToolHandlerOutcome(
            "record_finding",
            False,
            "缺少 claim:一句话写清你确认了什么。",
            error_code="TOOL_PARAMETER_REQUIRED",
        )
    ledger_path, ledger_scope = _findings_ledger_path(agent)
    if not ledger_path:
        return ToolHandlerOutcome(
            "record_finding",
            False,
            "当前上下文没有可用的结论账本(没有运行中的任务工作区)。",
            error_code="TOOL_UNAVAILABLE",
        )
    audit_context, audit_error = _audit_finding_context(agent, params)
    if audit_error:
        return ToolHandlerOutcome(
            "record_finding",
            False,
            audit_error,
            error_code="TOOL_PERMISSION_DENIED",
        )
    requested_id = str(params.get("finding_id") or "").strip()
    if requested_id and not _FINDING_ID_PATTERN.fullmatch(requested_id):
        return ToolHandlerOutcome(
            "record_finding",
            False,
            "finding_id 格式无效。",
            error_code="TOOL_INVALID_ARGUMENTS",
        )
    record = _finding_record(agent, claim, params, audit_context=audit_context)
    try:
        path = Path(ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        recorded, revision = _append_finding_revision(path, record)
    except OSError as exc:
        return ToolHandlerOutcome(
            "record_finding",
            False,
            f"结论账本写入失败: {exc}",
            error_code="TOOL_PERSISTENCE_FAILED",
        )
    payload = {
        "ok": True,
        "recorded": recorded,
        "reused": not recorded,
        "finding_id": record["id"],
        "revision": revision,
        "ledger_ref": ledger_path,
        "ledger_scope": ledger_scope,
    }
    linked = _link_watch_feedback(agent, params)
    if linked is not None:
        payload["watch_feedback_linked"] = linked
    if audit_context:
        from ...ingestion.source_worker import reconcile_audit_finding_outbox

        payload["coordination_event"] = reconcile_audit_finding_outbox(
            agent,
            current_subagent_run_id(agent),
        )
    return ToolHandlerOutcome(
        "record_finding", True, json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _finding_record(
    agent: object,
    claim: str,
    params: dict[str, object],
    *,
    audit_context: dict[str, Any],
) -> dict[str, object]:
    return _finding_record_for_context(
        claim,
        params,
        audit_context=audit_context,
        run_id=current_subagent_run_id(agent),
        attempt_id=current_subagent_attempt_id(agent),
        source="record_finding_tool",
    )


def _finding_record_for_context(
    claim: str,
    params: dict[str, object],
    *,
    audit_context: dict[str, Any],
    run_id: str,
    attempt_id: str,
    source: str,
) -> dict[str, object]:
    refs_value = params.get("evidence_refs")
    refs = refs_value if isinstance(refs_value, list) else ([refs_value] if refs_value else [])
    evidence_refs = [str(item).strip() for item in refs if str(item or "").strip()][:_MAX_REFS]
    kind = str(params.get("kind") or "finding").strip() or "finding"
    requested_id = str(params.get("finding_id") or "").strip()
    finding_id = requested_id or _default_finding_id(
        audit_context,
        kind=kind,
        evidence_refs=evidence_refs,
    )
    record: dict[str, object] = {
        "version": 1,
        "id": finding_id,
        "kind": kind,
        "claim": claim[:_MAX_CLAIM_CHARS],
        "evidence_refs": evidence_refs,
        "confidence": str(params.get("confidence") or "").strip(),
        "created_at": time.time(),
        "run_id": run_id,
        "attempt_id": attempt_id,
        "source": source,
    }
    if audit_context:
        record.update(audit_context)
        record["version"] = 2
        record["stage"] = str(params.get("stage") or "initial").strip() or "initial"
        record["needs_evidence"] = bool(params.get("needs_evidence", False))
        record["report_status"] = "pending"
        urgency = str(params.get("urgency") or "normal").strip().lower()
        record["urgency"] = urgency if urgency in {"normal", "urgent"} else "normal"
        record["requires_llm_report"] = bool(params.get("requires_llm_report", False))
    return record


def append_inline_audit_finding(
    path: Path,
    *,
    state: object,
    verdict_row: dict[str, Any],
) -> tuple[bool, int, dict[str, object]]:
    """Project one verdict-owned finding into the existing finding ledger.

    The verdict ledger is the durable source.  This projection is idempotent,
    so a crash after the append but before the projection cursor moves simply
    replays the same stable revision.
    """
    finding = verdict_row.get("finding")
    if not isinstance(finding, dict):
        raise ValueError("verdict row has no inline finding")
    source_ref = str(verdict_row.get("source_ref") or "").strip()
    # An inline finding is owned by this exact verdict row.  The model may
    # suggest extra refs in its raw verdict payload, but those are not verified
    # evidence for this finding and must not become authoritative source refs.
    # Cross-record or cross-source support is added later through the ordinary
    # record_finding/investigation path, where every supplied ref is checked.
    refs = [source_ref] if source_ref else []
    from ...ingestion.harvester import inspect_audit_record

    inspected = inspect_audit_record(state, str(verdict_row.get("ack_id") or ""))
    evidence_record = _audit_evidence_record(
        inspected,
        source_ref=source_ref,
        ack_id=str(verdict_row.get("ack_id") or ""),
    )
    audit_context = {
        "audit_finding": True,
        "owner_id": str(verdict_row.get("owner_id") or ""),
        "audit_id": str(verdict_row.get("audit_id") or ""),
        "audit_run_epoch": max(0, int(verdict_row.get("audit_run_epoch") or 0)),
        "ingest_run_epoch": max(0, int(verdict_row.get("ingest_run_epoch") or 0)),
        "source_id": str(verdict_row.get("source_id") or ""),
        "watch_id": str(verdict_row.get("watch_id") or ""),
        "source_url": str(getattr(state, "source_url", "") or ""),
        "source_refs": refs,
        "verdict": verdict_row.get("verdict"),
        "score": verdict_row.get("score"),
        "score_range": verdict_row.get("score_range"),
        "dimensions": verdict_row.get("dimensions"),
        "verdict_note": verdict_row.get("note"),
        "evidence_verdicts": [
            {
                "source_ref": source_ref,
                "ack_id": str(verdict_row.get("ack_id") or ""),
                "audit_run_epoch": max(
                    0,
                    int(verdict_row.get("audit_run_epoch") or 0),
                ),
                "ingest_run_epoch": max(
                    0,
                    int(verdict_row.get("ingest_run_epoch") or 0),
                ),
                "verdict": verdict_row.get("verdict"),
                "score": verdict_row.get("score"),
                "score_range": verdict_row.get("score_range"),
                "dimensions": verdict_row.get("dimensions"),
                "note": verdict_row.get("note"),
            }
        ],
        "evidence_records": [evidence_record],
    }
    params: dict[str, object] = {
        "evidence_refs": refs,
        "kind": str(finding.get("kind") or "finding"),
        "confidence": str(finding.get("confidence") or ""),
        "finding_id": str(finding.get("finding_id") or ""),
        "stage": str(finding.get("stage") or "initial"),
        "needs_evidence": bool(finding.get("needs_evidence", False)),
        "urgency": str(finding.get("urgency") or "normal"),
        "requires_llm_report": bool(finding.get("requires_llm_report", False)),
    }
    record = _finding_record_for_context(
        str(finding.get("claim") or ""),
        params,
        audit_context=audit_context,
        run_id=str(verdict_row.get("processing_run_id") or ""),
        attempt_id=str(verdict_row.get("processing_attempt_id") or ""),
        source="watch_stream.verdict",
    )
    recorded, revision = _append_finding_revision(path, record)
    return recorded, revision, record


def _default_finding_id(
    audit_context: dict[str, Any],
    *,
    kind: str,
    evidence_refs: list[str],
) -> str:
    if not audit_context:
        return f"rf-{uuid.uuid4().hex[:12]}"
    # Audit finding identity is anchored to the host-verified source records.
    # Supplementary URLs/paths may be added later as evidence, but must create
    # a revision of the same finding instead of a second incident id.
    verified_refs = [
        str(row.get("source_ref") or "").strip()
        for row in audit_context.get("evidence_verdicts") or []
        if isinstance(row, dict) and str(row.get("source_ref") or "").strip()
    ]
    identity_refs = verified_refs or evidence_refs
    material = "\0".join(
        [
            str(audit_context.get("audit_id") or ""),
            kind,
            *sorted(identity_refs),
        ]
    )
    return f"af-{sha256(material.encode()).hexdigest()[:20]}"


def _append_finding_revision(
    path: Path,
    record: dict[str, object],
) -> tuple[bool, int]:
    """Append one stable finding revision, reusing an identical retry."""
    with locked_json_path(path):
        report = read_jsonl_objects_report(
            path,
            context="record_finding.idempotency",
        )
        if report.load_errors:
            raise OSError("findings.jsonl 存在损坏行，已按 fail-closed 拒绝追加")
        same_id = [
            row for row in report.records if str(row.get("id") or "") == str(record.get("id") or "")
        ]
        if same_id and _same_finding_projection(same_id[-1], record):
            return False, int(same_id[-1].get("revision") or len(same_id))
        revision = (
            max(
                [int(row.get("revision") or index + 1) for index, row in enumerate(same_id)],
                default=0,
            )
            + 1
        )
        record["revision"] = revision
        record["updated_at"] = time.time()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return True, revision


def _same_finding_projection(
    existing: dict[str, Any],
    current: dict[str, object],
) -> bool:
    ignored = {
        # Run epochs are trace metadata.  Re-projecting the same stable
        # finding through another execution path must not fabricate a business
        # revision merely because that path did not carry identical run tags.
        "audit_run_epoch",
        "ingest_run_epoch",
        "attempt_id",
        "created_at",
        "revision",
        "run_id",
        # The same stable finding may first be projected atomically from a
        # verdict and then be retried through record_finding.  Tool provenance
        # is retained on the first durable row, but it is not a business
        # revision when every finding field and evidence projection is equal.
        "source",
        "updated_at",
    }
    return {key: value for key, value in existing.items() if key not in ignored} == {
        key: value for key, value in current.items() if key not in ignored
    }


def _audit_finding_context(
    agent: object,
    params: dict[str, object],
) -> tuple[dict[str, Any], str]:
    attrs = current_audit_attributes(agent)
    if not structured_audit_source_worker_attributes(attrs):
        return {}, ""
    assert isinstance(attrs, dict)
    owner_home = Path(str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or ""))
    watch_id = str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or "")
    source_id = str(attrs.get(AUDIT_SOURCE_ID_ATTR) or "")
    audit_id = str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "")
    try:
        caller_epoch = max(0, int(attrs.get(AUDIT_RUN_EPOCH_ATTR) or 0))
    except (TypeError, ValueError):
        caller_epoch = -1
    from ...ingestion.watch_state import load_state

    state = load_state(owner_home, watch_id)
    if (
        state is None
        or state.closed
        or state.source_id != source_id
        or state.audit_root_task_id != audit_id
        or caller_epoch != max(0, int(state.audit_run_epoch or 0))
    ):
        return {}, "当前来源工作者的持久来源绑定不可用。"
    refs = _audit_finding_refs(params)
    if not refs:
        return {}, "Audit finding 必须引用当前 watch 已判断的 source_ref。"
    verdicts, evidence_records, evidence_error = _audit_finding_evidence(
        state,
        watch_id=watch_id,
        refs=refs,
    )
    if evidence_error:
        return {}, evidence_error
    if not verdicts:
        return {}, "Audit finding 必须引用当前 watch 已签收判断的 source_ref。"
    primary = verdicts[0]
    return {
        "audit_finding": True,
        "owner_id": state.owner_id,
        "audit_id": audit_id,
        # The finding belongs to the currently active named-Audit run.  Each
        # evidence row separately retains the run in which it entered the
        # queue and the run in which its first verdict was accepted.
        "audit_run_epoch": max(0, int(state.audit_run_epoch or 0)),
        "ingest_run_epoch": max(
            0,
            int(primary.get("ingest_run_epoch") or 0),
        ),
        "source_id": source_id,
        "watch_id": watch_id,
        "source_url": state.source_url,
        "source_refs": refs,
        "verdict": primary.get("verdict"),
        "score": primary.get("score"),
        "score_range": primary.get("score_range"),
        "dimensions": primary.get("dimensions"),
        "verdict_note": primary.get("note"),
        "evidence_verdicts": verdicts,
        "evidence_records": evidence_records,
    }, ""


def _audit_finding_refs(params: dict[str, object]) -> list[str]:
    refs_value = params.get("evidence_refs")
    raw_refs = refs_value if isinstance(refs_value, list) else ([refs_value] if refs_value else [])
    return list(dict.fromkeys(str(item).strip() for item in raw_refs if str(item or "").strip()))[
        :_MAX_REFS
    ]


def _audit_finding_evidence(
    state: object,
    *,
    watch_id: str,
    refs: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    from ...ingestion.harvester import (
        inspect_audit_record,
        parse_audit_source_ref,
    )

    verdicts: list[dict[str, Any]] = []
    evidence_records: list[dict[str, Any]] = []
    for ref in refs:
        parsed = parse_audit_source_ref(ref)
        # Non-Audit refs are supplementary pointers only.  They never grant
        # source authority, but the public ToolModelSpec explicitly allows paths,
        # event ids and URLs, so rejecting them here made Schema and execution
        # disagree.  An audit:// ref for a sibling watch remains a hard denial.
        if parsed is None:
            continue
        if parsed[0] != watch_id:
            return [], [], "Audit finding 只能引用当前来源工作者自己的 source_ref。"
        inspected = inspect_audit_record(state, parsed[1])
        status = inspected.get("processing_status")
        if not inspected.get("ok") or not (
            isinstance(status, dict) and bool(status.get("acknowledged"))
        ):
            return [], [], "Audit finding 的 source_ref 尚无已签收首次判断。"
        verdict = inspected.get("verdict")
        evidence_records.append(_audit_evidence_record(inspected, source_ref=ref, ack_id=parsed[1]))
        if isinstance(verdict, dict):
            verdicts.append(
                {
                    "source_ref": ref,
                    "ack_id": parsed[1],
                    "audit_run_epoch": max(
                        0,
                        int(verdict.get("audit_run_epoch") or 0),
                    ),
                    "ingest_run_epoch": max(
                        0,
                        int(verdict.get("ingest_run_epoch") or 0),
                    ),
                    "verdict": verdict.get("verdict"),
                    "score": verdict.get("score"),
                    "score_range": verdict.get("score_range"),
                    "dimensions": verdict.get("dimensions"),
                    "note": verdict.get("note"),
                }
            )
    return verdicts, evidence_records, ""


def _audit_evidence_record(
    inspected: dict[str, Any],
    *,
    source_ref: str,
    ack_id: str,
) -> dict[str, Any]:
    """Project exact source identity and optionally one complete raw record.

    The durable spool/archive remains authoritative.  This projection is only
    a bounded delivery aid for the owner-facing model: an oversized or already
    reduced record is represented by its exact source_ref and hash, never by a
    silently truncated body.
    """

    record: dict[str, Any] = {
        "source_ref": str(source_ref or inspected.get("source_ref") or ""),
        "ack_id": str(ack_id or inspected.get("ack_id") or ""),
        "source_id": str(inspected.get("source_id") or ""),
        "watch_id": str(inspected.get("watch_id") or ""),
        "stream_pos": inspected.get("stream_pos"),
        "source_cursor": inspected.get("source_cursor"),
        "source_location": inspected.get("source_location"),
        "event_bytes": inspected.get("event_bytes"),
        "event_sha256": str(inspected.get("event_sha256") or ""),
        "observed_at": inspected.get("observed_at"),
        "fetched_at": inspected.get("fetched_at"),
        "raw_complete": bool(inspected.get("raw_complete")),
        "inline": False,
    }
    raw_event = inspected.get("raw_event")
    if not inspected.get("ok") or raw_event is None:
        record["reason"] = "source_record_unavailable"
        return record
    if not record["raw_complete"]:
        record["reason"] = "source_record_requires_exact_read"
        return record
    try:
        rendered = json.dumps(
            raw_event,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        record["reason"] = "source_record_not_json_serializable"
        return record
    if len(rendered) > _FINDING_EVIDENCE_INLINE_MAX_CHARS:
        record["reason"] = "source_record_exceeds_inline_budget"
        return record
    record["inline"] = True
    record["raw_event"] = raw_event
    return record


# LLM: 摄取召回反馈接缝(B3):盯守候选被确认时把 (watch_id, stream_pos) 追加进该
#   watch 的反馈收件箱——引擎属主消费后将该事件的结构特征回灌预筛,自动抬同类、
#   抽检向盲区源倾斜。best-effort:参数缺/格式不符/watch 不存在都静默跳过(None),
#   绝不影响结论账主通道;返回 True/False 只作观测。
# 函数用途: 确认的盯守候选 → 反馈收件箱一行(召回自愈的对账通路)。
def _link_watch_feedback(agent: object, params: dict[str, object]) -> bool | None:
    watch_id = str(params.get("watch_id") or "").strip()
    raw_pos = params.get("stream_pos")
    if not watch_id or raw_pos is None:
        return None
    if not _WATCH_ID_PATTERN.fullmatch(watch_id):
        return False
    try:
        stream_pos = int(str(raw_pos).strip())
    except (TypeError, ValueError):
        return False
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    if not owner_home:
        return False
    from ...ingestion.watch_feedback import append_confirmation

    return append_confirmation(Path(owner_home), watch_id, stream_pos)


def _findings_ledger_path(agent: object) -> tuple[str, str]:
    """账本落点:子代理 runner 轮 → 本 run 的 findings.jsonl(权威通道);
    主代理长任务 → 任务工作区 work/shared/findings.jsonl(同名共享通道)。"""
    run_id = current_subagent_run_id(agent)
    run_path = _run_ledger_path(agent, run_id) if run_id else ""
    if run_path:
        return run_path, run_id
    task_root = str(getattr(agent, "_current_run_task_workspace", "") or "").strip()
    if task_root:
        return str(Path(task_root) / "work" / "shared" / "findings.jsonl"), "task_workspace"
    return "", ""


def _run_ledger_path(agent: object, run_id: str) -> str:
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(manager, "load", None)):
        return ""
    try:
        task = manager.load(run_id)
    except (FileNotFoundError, TypeError):
        return ""
    return str(getattr(task, "agent_run_findings_jsonl", "") or "").strip()


