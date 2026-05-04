from __future__ import annotations

"""LLM contract: SubAgentCapabilityMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl
from .models import CapabilityRequest, SubAgentTask
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _action_for_issue,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _make_due_issue,
    _risk_weight,
    _route_card_payload,
    _runner_next_action,
    _select_capability_hits,
    _severity_weight,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from .rendering import render_capability_route_markdown
from .reports import CapabilityRouteRecord, CapabilityRouteReport
from .runner_rendering import _render_runner_item_line
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore

def _route_capability_gap(
    task,
    request,
    query,
    hits,
    gap,
):
    """Build a GAP record when no hits found and apply=True."""
    now = time.time()
    return CapabilityRouteRecord(
        id=_new_id("route"),
        run_id=task.id,
        request_id=request.id,
        status="GAP",
        dry_run=False,
        query=query,
        candidate_count=len(hits),
        gap_id=gap.id,
        message="未找到足够可信的 skill/tool card，已记录 capability gap。",
        created_at=now,
    )


def _route_capability_grant(
    task,
    request,
    query,
    hits,
    selected_hits,
    granted_skills,
    granted_tools,
    selected_cards,
    reasons,
    grant,
):
    """Build a GRANTED record when hits found and apply=True."""
    now = time.time()
    return CapabilityRouteRecord(
        id=_new_id("route"),
        run_id=task.id,
        request_id=request.id,
        status="GRANTED",
        dry_run=False,
        query=query,
        candidate_count=len(hits),
        granted_skills=granted_skills,
        granted_tools=granted_tools,
        selected_cards=selected_cards,
        reasons=reasons,
        grant_id=grant.id,
        message="已生成 capability grant。",
        created_at=now,
    )


class SubAgentCapabilityMixin:
    def _route_capability_no_hits(self, task, request, query, hits, apply):
        """Build a record when no hits found (dry-run WOULD_GAP or real GAP)."""
        now = time.time()
        if not apply:
            return CapabilityRouteRecord(
                id=_new_id("route"),
                run_id=task.id,
                request_id=request.id,
                status="WOULD_GAP",
                dry_run=True,
                query=query,
                candidate_count=len(hits),
                message="未找到足够可信的 skill/tool card；apply 时会记录 capability gap。",
                created_at=now,
            )
        gap = self.record_capability_gap(
            run_id=task.id,
            missing_capability=request.needed_capability,
            why_failed="CapabilityRouter 没有找到匹配的 skill/tool card。",
            attempted_tools=request.tried,
            needed_outputs=[request.expected_output] if request.expected_output else [],
        )
        self._mark_capability_request_status(task.id, request.id, "GAP")
        return CapabilityRouteRecord(
            id=_new_id("route"),
            run_id=task.id,
            request_id=request.id,
            status="GAP",
            dry_run=False,
            query=query,
            candidate_count=len(hits),
            gap_id=gap.id,
            message="未找到足够可信的 skill/tool card，已记录 capability gap。",
            created_at=now,
        )

    def route_capability_requests(
        self,
        router: CapabilityRouter,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        run_ids: list[str] | None = None,
        limit: int = 0,
    ) -> CapabilityRouteReport:
        """把 OPEN capability request 路由到 skill/tool card。

        默认 dry-run，只展示会下发哪些能力。
        `apply=True` 时才会真正生成 capability grant 或 capability gap。
        """

        cfg = config or CapabilityConfig()
        records: list[CapabilityRouteRecord] = []
        selected_runs = self._select_runs(run_ids)
        for task in selected_runs:
            for request in task.capability_requests:
                if request.status != "OPEN":
                    continue
                query = _capability_request_query(task, request)
                hits = router.search(query, limit=cfg.capability_candidate_limit)
                selected_hits = _select_capability_hits(hits, cfg)
                record = self._route_capability_request(
                    task,
                    request,
                    query=query,
                    hits=hits,
                    selected_hits=selected_hits,
                    apply=apply,
                )
                records.append(record)
                if limit > 0 and len(records) >= limit:
                    break
            if limit > 0 and len(records) >= limit:
                break

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary[record.status] = summary.get(record.status, 0) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied",
                0,
            ) + 1
        return CapabilityRouteReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def write_capability_route_report(
        self,
        router: CapabilityRouter,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        run_ids: list[str] | None = None,
        limit: int = 0,
    ) -> CapabilityRouteReport:
        """写出 capability request 路由报告。"""

        report = self.route_capability_requests(
            router,
            config,
            apply=apply,
            run_ids=run_ids,
            limit=limit,
        )
        (self.workspace / "subagent_capability_route_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_CAPABILITY_ROUTE.md").write_text(
            render_capability_route_markdown(report),
            encoding="utf-8",
        )
        self._index_report(
            "subagent_capability_route_report",
            "latest",
            "Subagent capability route report",
            report,
            event_type="subagent_capability_route_report_written",
        )
        if apply:
            for record in report.records:
                self._append_capability_route_log(record)
        return report

    def _route_capability_request(
        self,
        task: SubAgentTask,
        request: CapabilityRequest,
        *,
        query: str,
        hits: list[CapabilitySearchHit],
        selected_hits: list[CapabilitySearchHit],
        apply: bool,
    ) -> CapabilityRouteRecord:
        """路由单条 capability request。"""

        selected_cards = [_route_card_payload(hit) for hit in selected_hits]
        granted_skills = [hit.card.name for hit in selected_hits if hit.card.kind == "skill"]
        granted_tools = [hit.card.name for hit in selected_hits if hit.card.kind == "tool"]
        reasons = _merge_list([], [reason for hit in selected_hits for reason in hit.reasons])

        if not selected_hits:
            return self._route_capability_no_hits(task, request, query, hits, apply)

        if not apply:
            now = time.time()
            return CapabilityRouteRecord(
                id=_new_id("route"),
                run_id=task.id,
                request_id=request.id,
                status="WOULD_GRANT",
                dry_run=True,
                query=query,
                candidate_count=len(hits),
                granted_skills=granted_skills,
                granted_tools=granted_tools,
                selected_cards=selected_cards,
                reasons=reasons,
                message="找到候选能力；apply 时会生成 capability grant。",
                created_at=now,
            )

        grant = self.record_capability_grant(
            task.id,
            request_id=request.id,
            skills=granted_skills,
            tools=granted_tools,
            capability_cards=selected_cards,
            reason=f"CapabilityRouter 命中 {len(selected_hits)} 张能力卡。",
            expires_after_task=True,
        )
        self._mark_capability_request_status(task.id, request.id, "GRANTED")
        routed_task = self.load(task.id)
        self._append_task_work_log(
            routed_task,
            f"capability_route: request {request.id} 已生成 grant {grant.id}，"
            f"skills={','.join(granted_skills) or 'none'} tools={','.join(granted_tools) or 'none'}。",
        )
        return _route_capability_grant(
            task,
            request,
            query,
            hits,
            selected_hits,
            granted_skills,
            granted_tools,
            selected_cards,
            reasons,
            grant,
        )

    def _mark_capability_request_status(self, run_id: str, request_id: str, status: str) -> None:
        """更新 capability request 状态。"""

        task = self.load(run_id)
        for request in task.capability_requests:
            if request.id == request_id:
                request.status = status
        task.updated_at = time.time()
        self.save(task)

    def _append_capability_route_log(self, record: CapabilityRouteRecord) -> None:
        """写入 capability route 审计日志。"""

        jsonl = self.workspace / "subagent_capability_route_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "CAPABILITY_ROUTE_LOG.md"
        if not markdown.exists():
            markdown.write_text("# CAPABILITY ROUTE LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- [{record.status}] {record.id} run={record.run_id} request={record.request_id} "
                f"skills={','.join(record.granted_skills) or 'none'} "
                f"tools={','.join(record.granted_tools) or 'none'} message={record.message}\n"
            )
        self._index_capability_route(record)
