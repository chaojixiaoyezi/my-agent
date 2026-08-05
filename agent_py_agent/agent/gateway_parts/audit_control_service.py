from __future__ import annotations

"""Deterministic owner/thread-scoped `/audit` controls."""

import time
from dataclasses import dataclass

from ..common.audit_activation import AUDIT_ATTR
from ..conversation.audit_lifecycle import (
    AuditLifecycleError,
    audit_scope_payload,
    project_audit_runtime_attributes,
    start_named_audit,
)
from ..conversation.audit_requirements import audit_user_requirement_text
from ..conversation.channels import redact_host_absolute_paths
from ..conversation.control_commands import (
    ConversationControlCommand,
    ConversationControlResult,
)
from ..conversation.models import (
    THREAD_TASK_LINK_INACTIVE_STATUSES,
    thread_task_run_started_at,
)
from ..conversation.named_work import stop_named_conversation_work
from ..ingestion.audit_state import audit_task_source_facts


@dataclass(frozen=True)
class AuditControlRequest:
    owner_agent: object
    store: object
    thread: object
    command: ConversationControlCommand


def execute_audit_control_operation(request: AuditControlRequest) -> ConversationControlResult:
    operation = str(request.command.operation or "").strip().lower()
    if operation == "help":
        return ConversationControlResult("audit", True, _audit_help())
    if operation == "start":
        return _start_audit(request)
    link, error = _exact_audit_link(
        request,
        include_terminal=operation == "status",
    )
    if error:
        return ConversationControlResult("audit", False, error)
    if link is None:
        return ConversationControlResult("audit", False, "没有找到这个 Audit。")
    if operation == "status":
        return ConversationControlResult(
            "audit",
            True,
            _render_audit_status(request.owner_agent, link),
        )
    if operation == "clear":
        stopped = stop_named_conversation_work(
            request.owner_agent,
            thread_id=str(getattr(request.thread, "thread_id", "") or ""),
            kind="audit",
            name=request.command.name,
        )
        if not stopped.ok:
            messages = {
                "NAMED_WORK_NOT_FOUND": "没有找到这个 Audit。",
                "NAMED_WORK_CONFLICT": "同名 Audit 状态冲突，请先用 /status 核对。",
            }
            return ConversationControlResult(
                "audit",
                False,
                messages.get(stopped.error_code, "Audit 状态暂时不可用，请稍后重试。"),
            )
        return ConversationControlResult(
            "audit",
            True,
            f"Audit“{request.command.name}”已停止。",
            request_id=stopped.task_id,
        )
    if operation == "resume":
        from ..ingestion.source_worker import resume_quota_blocked_source_workers
        from ..orphan_supervision import supervise_orphan_runs

        try:
            resumed = resume_quota_blocked_source_workers(
                request.owner_agent,
                str(getattr(link, "task_id", "") or ""),
            )
            if resumed:
                supervise_orphan_runs(request.owner_agent)
        except Exception:
            return ConversationControlResult(
                "audit",
                False,
                "Audit 恢复状态暂时不可用，请稍后重试。",
            )
        if not resumed:
            return ConversationControlResult(
                "audit",
                True,
                f"Audit“{request.command.name}”没有等待人工恢复的额度故障。",
            )
        return ConversationControlResult(
            "audit",
            True,
            f"Audit“{request.command.name}”已恢复 {resumed} 个因额度耗尽暂停的来源工作者。",
            request_id=str(getattr(link, "task_id", "") or ""),
        )
    return ConversationControlResult("audit", False, request.command.usage)


def _start_audit(request: AuditControlRequest) -> ConversationControlResult:
    """Start and provision one named Audit from typed control facts only."""

    thread_id = str(getattr(request.thread, "thread_id", "") or "").strip()
    prompt = str(request.command.value or "").strip()
    duration_seconds = int(request.command.duration_seconds or 0)
    try:
        link = start_named_audit(
            request.owner_agent,
            request.store,
            thread_id=thread_id,
            work_name=request.command.name,
            prompt=prompt,
            duration_seconds=duration_seconds,
        )
    except AuditLifecycleError as exc:
        return ConversationControlResult("audit", False, str(exc))
    except Exception:
        return ConversationControlResult(
            "audit",
            False,
            "Audit 启动状态当前无法可靠建立，请稍后重试。",
        )

    scope = audit_scope_payload(link)
    attrs = project_audit_runtime_attributes(
        {AUDIT_ATTR: True},
        scope,
        thread_id=thread_id,
    )
    from ..ingestion.source_worker import provision_published_audit_source_workers

    task_id = str(getattr(link, "task_id", "") or "").strip()
    try:
        provision = provision_published_audit_source_workers(
            request.owner_agent,
            prompt=prompt,
            task_id=task_id,
            task_attributes=attrs,
            source="conversation_control",
        )
    except Exception:
        return ConversationControlResult(
            "audit",
            True,
            (
                f"Audit“{request.command.name}”已启动；"
                "来源工作者状态暂时无法核验，系统会继续自动接管，可用 status 查看。"
            ),
            request_id=task_id,
        )
    return ConversationControlResult(
        "audit",
        True,
        _render_audit_start_result(request.command.name, provision),
        request_id=str(getattr(link, "task_id", "") or ""),
    )


def _render_audit_start_result(name: str, provision: object) -> str:
    """Render only structured activation facts; no model can contradict them."""

    facts = provision if isinstance(provision, dict) else {}
    required = max(0, int(facts.get("required") or 0))
    ready = max(0, int(facts.get("ready") or 0))
    waiting = max(0, int(facts.get("waiting") or max(0, required - ready)))
    if str(facts.get("error_code") or "") == "AUDIT_NO_PUBLISHED_SOURCES":
        return (
            f"Audit“{name}”已启动；当前没有已发布来源，所以暂时没有来源工作者。"
            "可以继续用 prepare 学习、测试并发布来源。"
        )
    if facts.get("ok") is True and required > 0 and ready == required:
        return f"Audit“{name}”已启动，{ready} 路来源工作者均已就绪。"
    if required > 0:
        return (
            f"Audit“{name}”已启动；{required} 路来源中 {ready} 路已就绪，"
            f"{waiting} 路正在等待容量或自动接管。"
        )
    return f"Audit“{name}”已启动；来源状态将在 /audit {name} status 中持续更新。"


def _exact_audit_link(
    request: AuditControlRequest,
    *,
    include_terminal: bool = False,
) -> tuple[object | None, str]:
    try:
        links, errors = request.store.task_links_report(
            str(getattr(request.thread, "thread_id", "") or "")
        )
    except Exception:
        return None, "Audit 状态暂时不可用，请稍后重试。"
    if errors:
        return None, "Audit 状态暂时不可用，请稍后重试。"
    named = [
        link
        for link in links
        if str(getattr(link, "work_kind", "") or "").strip().lower() == "audit"
        and str(getattr(link, "work_name", "") or "") == request.command.name
    ]
    active = [
        link
        for link in named
        if str(getattr(link, "status", "") or "").strip().lower()
        not in THREAD_TASK_LINK_INACTIVE_STATUSES
    ]
    if len(active) > 1:
        return None, "同名 Audit 状态冲突，请先用 /status 核对。"
    if active:
        return active[0], ""
    if not include_terminal or not named:
        return None, ""
    # Status remains useful after a finite Audit ends.  Select the newest exact
    # name deterministically; clear still resolves active work only and cannot
    # resurrect or mutate this retained history.
    latest = max(
        named,
        key=lambda item: (
            float(getattr(item, "expires_at", 0.0) or 0.0),
            float(getattr(item, "pending_updated_at", 0.0) or 0.0),
            float(getattr(item, "effective_updated_at", 0.0) or 0.0),
            float(getattr(item, "created_at", 0.0) or 0.0),
            str(getattr(item, "task_id", "") or ""),
        ),
    )
    return latest, ""


def _render_audit_status(agent: object, link: object) -> str:
    status = str(getattr(link, "status", "") or "").strip().lower()
    created_at = float(getattr(link, "created_at", 0.0) or 0.0)
    started_at = thread_task_run_started_at(link, fallback=created_at)
    ended_at = _audit_elapsed_end(link, status=status, current=time.time())
    elapsed = max(0, int(ended_at - started_at)) if started_at > 0 else 0
    effective = redact_host_absolute_paths(audit_user_requirement_text(link))
    pending = redact_host_absolute_paths(str(getattr(link, "pending_prompt", "") or "").strip())
    runtime_sources = audit_task_source_facts(
        agent,
        str(getattr(link, "task_id", "") or ""),
    )
    sources = _audit_status_sources(link, runtime_sources)
    active = sum(1 for item in sources if item.get("collection_active") is True)
    prepared = sum(1 for item in sources if item.get("prepared_only") is True)
    draining = sum(
        1
        for item in sources
        if item.get("state_available") is not False
        and item.get("closed") is not True
        and item.get("collection_active") is not True
        and item.get("prepared_only") is not True
        and _source_pending_count(item) > 0
    )
    pending_total = sum(_source_pending_count(item) for item in sources)
    quota_paused = sum(1 for item in sources if _source_worker_state(item) == "awaiting_operator")
    capacity_alerts = sum(1 for item in sources if _source_capacity_alert(item))
    ingest_rate = sum(_source_metric(item, "ingest_records_per_second") for item in sources)
    judge_rate = sum(_source_processing_rate(item) for item in sources)
    oldest_pending = max(
        (_source_metric(item, "oldest_pending_age_seconds") for item in sources),
        default=0.0,
    )
    recent_latency = _slowest_source_latency(sources)
    unavailable = sum(
        1
        for item in sources
        if item.get("state_available") is False
        or (status == "active" and item.get("prepared_only") is True)
    )
    lines = [
        f"Audit：{getattr(link, 'work_name', '')}",
        f"状态：{status or 'unknown'}",
        f"已持续：{_duration_text(elapsed)}",
        f"当前生效要求：{_bounded(effective) if effective else '尚未发布'}",
        f"待处理内容：{_bounded(pending) if pending else '无'}",
        (
            f"来源：{len(sources)}（采集中 {active}，排空中 {draining}，"
            f"已准备 {prepared}，异常或缺岗 {unavailable}）"
        ),
        f"待判积压：{pending_total}",
        f"处理速度：采集 {ingest_rate:.2f} 条/秒，研判 {judge_rate:.2f} 条/秒",
        f"最老待判：{_duration_text(oldest_pending) if oldest_pending > 0 else '无'}",
        (
            "最近处理延迟（最慢来源）："
            f"P50 {_duration_text(recent_latency['p50_seconds'])}，"
            f"P95 {_duration_text(recent_latency['p95_seconds'])}，"
            f"P99 {_duration_text(recent_latency['p99_seconds'])}"
            if recent_latency
            else "最近处理延迟：暂无样本"
        ),
        f"容量告警：{capacity_alerts} 路；额度暂停：{quota_paused} 路",
    ]
    for index, source in enumerate(sources, start=1):
        source_pending = _source_pending_count(source)
        worker_state = _source_worker_state(source)
        health = (
            "状态不可用"
            if source.get("state_available") is False
            else "模型额度耗尽，等待管理员恢复后执行 resume"
            if worker_state == "awaiting_operator"
            else "已关闭"
            if source.get("closed") is True
            else f"容量告警，待判 {source_pending}，最老 {_duration_text(_source_metric(source, 'oldest_pending_age_seconds'))}"
            if _source_capacity_alert(source)
            else "采集中"
            if source.get("collection_active") is True
            else "等待来源工作者"
            if status == "active" and source.get("prepared_only") is True
            else "已准备，尚未采集"
            if source.get("prepared_only") is True
            else f"排空中，待判 {source_pending}"
            if source_pending > 0
            else "等待收口"
        )
        source_name = _bounded(str(source.get("display_name") or ""), 160)
        transport = str(source.get("transport") or "来源")
        lines.append(f"- 来源 {index}：{source_name or '未命名来源'}（{transport}）｜{health}")
    return "\n".join(lines)


def _audit_elapsed_end(link: object, *, status: str, current: float) -> float:
    """Stop terminal Audit duration at its typed monitoring deadline.

    来源排空可以晚于监测窗口，但 terminal status 的“已持续”表示用户
    要求的监测时长，不应随每次 status 查询继续增长。排空是独立的
    运行状态，由来源行的待判数和收口状态展示。
    """
    if status not in THREAD_TASK_LINK_INACTIVE_STATUSES:
        return current
    try:
        expires_at = float(getattr(link, "expires_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        expires_at = 0.0
    return expires_at if expires_at > 0 else current


def _source_pending_count(source: dict[str, object]) -> int:
    """Read one source's durable pending count without interpreting content.

    只投影覆盖回执里的结构化 pending；缺失或损坏时按零展示，不从文字猜积压。
    """
    receipt = source.get("audit_receipt")
    if not isinstance(receipt, dict):
        return 0
    try:
        return max(0, int(receipt.get("pending") or 0))
    except (TypeError, ValueError):
        return 0


def _source_worker_state(source: dict[str, object]) -> str:
    worker = source.get("source_worker")
    if not isinstance(worker, dict):
        return ""
    return str(worker.get("state") or "").strip()


def _source_capacity_alert(source: dict[str, object]) -> bool:
    capacity = source.get("capacity")
    alert = capacity.get("capacity_alert") if isinstance(capacity, dict) else None
    return isinstance(alert, dict) and alert.get("active") is True


def _source_metric(source: dict[str, object], key: str) -> float:
    capacity = source.get("capacity")
    if not isinstance(capacity, dict):
        return 0.0
    try:
        return max(0.0, float(capacity.get(key) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _source_processing_rate(source: dict[str, object]) -> float:
    capacity = source.get("capacity")
    throughput = capacity.get("processing_throughput") if isinstance(capacity, dict) else None
    if not isinstance(throughput, dict):
        return 0.0
    try:
        return max(0.0, float(throughput.get("records_per_second") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _slowest_source_latency(
    sources: list[dict[str, object]],
) -> dict[str, float]:
    samples: list[dict[str, float]] = []
    for source in sources:
        capacity = source.get("capacity")
        latency = capacity.get("processing_latency") if isinstance(capacity, dict) else None
        if not isinstance(latency, dict):
            continue
        try:
            samples.append(
                {
                    key: max(0.0, float(latency.get(key) or 0.0))
                    for key in ("p50_seconds", "p95_seconds", "p99_seconds")
                }
            )
        except (TypeError, ValueError):
            continue
    return max(samples, key=lambda item: item["p95_seconds"]) if samples else {}


def _audit_status_sources(
    link: object,
    runtime_sources: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Merge prepared bindings and runtime facts without exposing internal IDs.

    准备阶段的来源绑定也是权威用户事实；即使 watch 尚未创建，status 也必须显示
    “已准备”，运行阶段缺少对应工作者时则显示真实缺岗，而不是把来源数报成零。
    """
    remaining = [dict(item) for item in runtime_sources]
    merged: list[dict[str, object]] = []
    bindings = getattr(link, "effective_source_bindings", ()) or ()
    for raw in bindings:
        if not isinstance(raw, dict):
            continue
        source_id = str(raw.get("source_id") or "").strip()
        source_url = str(raw.get("url") or "").strip()
        match_index = next(
            (
                index
                for index, item in enumerate(remaining)
                if (source_id and str(item.get("source_id") or "").strip() == source_id)
                or (source_url and str(item.get("source_url") or "").strip() == source_url)
            ),
            None,
        )
        runtime = remaining.pop(match_index) if match_index is not None else {}
        merged.append(
            {
                **runtime,
                "display_name": source_id,
                "transport": _source_transport_label(source_url),
                "prepared_only": not bool(runtime),
            }
        )
    for runtime in remaining:
        # A replace publication makes ``effective_source_bindings`` the current
        # source-set authority.  A removed watch can remain on disk as a closed
        # receipt for recovery/audit history; once its receipt is fully settled
        # it must not be counted as a fourth current source.  Keep an unreadable
        # or still-draining retired watch visible so status never hides backlog.
        if (
            runtime.get("closed") is True
            and runtime.get("state_available") is True
            and _source_pending_count(runtime) == 0
        ):
            continue
        merged.append(
            {
                **runtime,
                "display_name": "",
                "transport": _source_transport_label(str(runtime.get("source_url") or "")),
                "prepared_only": False,
            }
        )
    return merged


def _source_transport_label(source_url: str) -> str:
    """Return only the user-safe transport family for one source.

    这里只区分文件与 HTTP 传输，不展示绝对路径、凭据、watch_id 或内部 source_id。
    """
    value = str(source_url or "").strip().lower()
    return "文件" if value.startswith("file://") or value.startswith("/") else "HTTP"


def _audit_help() -> str:
    return "\n".join(
        [
            "Audit 命令：",
            "/audit <名称> prepare <内容>：围绕这个 Audit 讨论、读文档、试接口或准备修改。",
            "/audit <时长> <名称> <任务内容>：启动这个 Audit，例如 /audit 30d 主机监测 持续检查已确认的来源。",
            "/audit <名称> status：查看这个 Audit 的生效要求、待处理内容、时长和来源概况。",
            "/audit <名称> resume：管理员恢复额度或切换模型后，继续因额度耗尽暂停的来源工作者。",
            "/audit <名称> clear：停止这个 Audit；普通聊天记录不受影响。",
            "/audit help：显示本帮助。",
            "名称必须完全一致并区分大小写。",
        ]
    )


def _duration_text(seconds: int) -> str:
    days, remainder = divmod(max(0, int(seconds)), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}天{hours}小时{minutes}分"
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def _bounded(value: str, limit: int = 4000) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["AuditControlRequest", "execute_audit_control_operation"]
