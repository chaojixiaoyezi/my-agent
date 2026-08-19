"""watch_stream 工具的载荷渲染:候选批/覆盖账目/盯守进度 → 给模型的 JSON。"""

from __future__ import annotations

import json
import time
from typing import Any

from .engine import CallDigest, Candidate, GroupDigest
from .source_http import public_source_envelope
from .watch_state import WatchState

_EVENT_JSON_CAP = 1600
_EXEMPLAR_JSON_CAP = 500
_OVERFLOW_SAMPLE_CAP = 20
_CONTENT_RULES_SHOWN = 32

# 判读优先序(B 回炉②:有限判力先给高价值车道):反馈车道(与已确认真目标同特征)最先,
# 判据命中次之,通用稀有车道再次,随机抽检殿后。只排序不丢行(逐条送达不破)。
# full_stream_read(正常量直通,全量非筛选)与稀有车道同级:直通批通常整批同级=保持流序。
_JUDGE_PRIORITY = {
    "confirmed_target_similar": 0,
    "spec_target_value": 1,
    "minority_field_value": 2,
    "structurally_rare_signature": 3,
    "full_stream_read": 3,
    "audit_sample": 4,
}


def order_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """候选行按 (车道价值, 流序) 稳定排序:主代理判力被淹时,先判到的是最可能真的。"""
    def _key(row: dict[str, Any]) -> tuple[int, int]:
        triage = row.get("triage") if isinstance(row.get("triage"), dict) else {}
        reason = str(triage.get("reason") or "")
        try:
            pos = int(row.get("stream_pos") or 0)
        except (TypeError, ValueError):
            pos = 0
        return (_JUDGE_PRIORITY.get(reason, 3), pos)

    return sorted(rows, key=_key)

PULL_GUIDANCE = (
    "candidates 是当前批次的模型可读记录；triage、频次和 spec 只解释系统为何展示该条，"
    "都不是业务结论。结合用户目标、完整记录和可用工具自主分析、复核、委派和汇报。"
    "coverage、watch 和 backlog 是结构化运行事实；追平流尾只表示当前没有新记录，"
    "是否满足用户要求仍以窗口、覆盖账和任务目标为准。"
)


AUDIT_PULL_GUIDANCE = (
    "这是 /audit 的 durable 批次。每个 candidate 都带稳定 ack_id/source_ref 和完整记录；"
    "batch_context 给出本批预计 token、当前上下文余量、模型输出预算和安全批量，领取前应"
    "同时估算为每条记录写显式结论所需的返回空间；条数请求只是领取意图，"
    "程序会在完整记录边界按累计字节和统一上下文预算收口。"
    "程序不会自动判真、按分数路由、替你上报或指定子代理。依据用户原始任务和自定义评分要求"
    "自主选择分析、工具、复核、委派与汇报方式。没有自定义格式时可使用"
    " hit/clear/unsure、总分、理由；有自定义维度时同时提交各维度名称、范围、分数和理由。"
    "结论通过 watch_stream(action=verdict) 与耐久记录一一对账。你必须实际审查每条记录，"
    "并为本轮已经完成的每条返回相邻 verdict_token、verdict 和 score；clear 且没有额外"
    "依据时可省略 note，hit/unsure 必须说明理由。程序不接受批量默认值，也不会替模型猜"
    "遗漏记录的结论；遗漏记录保持 pending，下一次 pull 只重投这些记录并生成新的引用。"
    "令牌不能重复或跨批引用，数组顺序本身不作为身份。"
    "首次需要升级的发现随同"
    "对应 verdict 的 finding 字段一次落账，后续补证或组合结论同样随该行 finding 落账。"
    "delivery_ref 始终是 watch_stream 的顶层参数，绝不能放进 verdicts。首次判断提交"
    "顶层 delivery_ref，以及本轮各条结果中相邻的 verdict_token 和判断；程序会从可信"
    "交付账绑定每行的 ack_id/source_ref/event_sha256，无需机械复制。历史复核才显式带"
    " ack_id；"
    "显式提供但不匹配仍会拒绝。旧引用不能跨"
    "重投或接管复用。未提交的 ack 保持 pending 并可"
    "原样重投。coverage.audit_receipt "
    "是入队、已判、待判和丢弃的权威覆盖事实。"
)


def attach_audit_receipt(
    payload: dict[str, Any],
    state: WatchState,
    *,
    include_objective: bool = True,
) -> None:
    """保证档任务事实与覆盖回执挂载(pull/status/open/close 同一块)。

    用户原始目标跟 durable watch 一起持久化，供协调者重启后继续查询。
    一源工作者的每轮运行已由共享 runner 投影当前 run prompt 和它自己的
    source profile，因此其工具回包不再重复所有来源的合并目标。这里仅搬运原文，
    不解析或执行其中的业务语义。
    """
    if not state.audit_guarantee:
        return
    from .harvester import audit_receipt_facts

    if include_objective and state.audit_objective:
        # The named Audit keeps the exact chronological prepare history.
        # Coordinators may inspect the current bounded operating notes.  A
        # source-scoped caller opts out because its pinned source profile and
        # run prompt are already projected by the shared runner; repeating the
        # combined Audit notes would expose sibling sources and dilute the
        # current batch.
        from ..conversation.audit_requirements import audit_runtime_requirement_text

        payload["audit_objective"] = audit_runtime_requirement_text(
            state.audit_objective
        )
    coverage = payload.get("coverage")
    receipt = audit_receipt_facts(state)
    if isinstance(coverage, dict):
        coverage["audit_receipt"] = receipt
    else:
        payload["audit_receipt"] = receipt
    if int(receipt.get("dropped") or 0) > 0:
        payload["audit_dropped_alert"] = (
            f"覆盖回执 dropped={receipt['dropped']}>0；逐条覆盖约束已经不成立。"
            "这是可查询的运行失败事实，当前任务不能据此声明完整覆盖。"
        )


def render_pull_payload(state: WatchState, digest: CallDigest, extras: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "ok": True,
        "action": "pull",
        "watch_id": state.watch_id,
        "source_binding": source_binding_block(state),
        "source_envelope": public_source_envelope(state.source_envelope),
        "source_spec_configured": bool(state.source_spec),
        "candidates": order_candidate_rows(candidate_rows(digest)),
        "suppressed_groups": group_rows(digest),
        "suppressed_groups_total": digest.groups_total,
        "suppressed_events_this_call": digest.suppressed_total,
        "overflow": _overflow_block(digest),
        "coverage": coverage_block(state, extras),
        "watch": watch_block(state),
        "engine_totals": dict(state.engine.totals),
        "guidance": PULL_GUIDANCE,
    }
    if state.last_error:
        payload["last_source_error"] = state.last_error
    attach_judgment_note(payload, state)
    attach_frequent_hit_alert(payload, frequent_hit_rows(digest))
    attach_content_rules_count(payload, digest.normal_rule_hits)
    attach_source_progress(payload)
    return payload


def attach_judgment_note(payload: dict[str, Any], state: WatchState) -> None:
    """轻量记忆回显:用户教过的判读须知(configure 的 judgment_note)随每批候选带回——
    重启或消费者切换后仍能看到"这个来源该怎么看",不用重教。纯搬运,不解读。"""
    if not state.audit_guarantee and state.judgment_note:
        payload["judgment_note"] = state.judgment_note


def frequent_hit_rows(digest: CallDigest) -> list[dict[str, Any]]:
    """调查告警的可落盘/可渲染行:示例事件按被压组示例同一预算截断(告警要能直接看到
    该类的请求端+结果端内容,又不能撑爆 spool/payload)。inline 与 harvester 共用。"""
    rows: list[dict[str, Any]] = []
    for row in digest.frequent_hits.values():
        shaped = dict(row)
        shaped["exemplar_event"] = _capped_json(dict(row.get("exemplar_event") or {}), _EXEMPLAR_JSON_CAP)
        rows.append(shaped)
    return rows


def attach_frequent_hit_alert(payload: dict[str, Any], alerts: list[dict[str, Any]]) -> None:
    """高频命中类的调查告警(频率只触发调查、内容决定去留):某取值类当前在窗口内高频
    出现——命中【没有被丢弃】,仍按车道名额逐条抬升(稀有优先、高频沉底)。告警给计数
    事实+示例事件,由模型按内容定性;代码不替模型决定去留。
    inline pull 与 spool 消费(watch_tool._render_spool_pull)共用,契约不漂移。"""
    if not alerts:
        return
    payload["frequent_hit_investigation"] = alerts
    payload["frequent_hit_note"] = (
        "这些行只表示某类结构化取值在当前窗口内高频出现，并附带计数和示例。"
        "频率不决定业务结论，也不强制采用特定复核、配置或汇报路线。"
    )


def attach_content_rules_count(payload: dict[str, Any], hits_this_call: int) -> None:
    """内容过滤规则的本批命中数(>0 才带):减负是"研判过、认得它了",不是静默丢——
    累计账在 engine_totals.spec_normal_rule_hits,per-规则明细在 status 的 content_rules。"""
    if hits_this_call > 0:
        payload["content_rules_filtered_this_call"] = hits_this_call


def content_rules_block(engine) -> dict[str, Any]:
    """status 的内容过滤规则审计块:建了哪条(mode+条目)、各拦了多少、合计多少。
    纯账目搬运;规则本体在 source_spec(normal_*),账随 spec 版本重置。"""
    ranked = sorted(engine.rule_hits.items(), key=lambda kv: (-kv[1], kv[0]))
    rules = []
    for key, hits in ranked[:_CONTENT_RULES_SHOWN]:
        mode, _sep, entry = key.partition("\x1e")
        rules.append({"mode": mode, "rule": entry, "hits": hits})
    return {
        "hits_total": int(engine.totals.get("spec_normal_rule_hits", 0) or 0),
        "rules_count": len(engine.rule_hits),
        "rules": rules,
    }


def merge_frequent_hits(record_rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """spool 消费批的调查告警合并:同 (字段,取值类) 折叠,取最新窗口计数、累加本批命中
    次数、保留最早的示例事件(示例只为看内容,一条就够)。"""
    merged: dict[str, dict[str, Any]] = {}
    for rows in record_rows:
        for row in rows:
            _merge_frequent_hit_row(merged, row)
    return list(merged.values())


def _merge_frequent_hit_row(merged: dict[str, dict[str, Any]], row: object) -> None:
    if not isinstance(row, dict):
        return
    key = f"{row.get('path')}\x1e{row.get('value_class') or row.get('value')}"
    previous = merged.get(key)
    if previous is None:
        merged[key] = dict(row)
        return
    previous.update({k: row[k] for k in ("value_window_count", "field_window_count", "mode") if k in row})
    previous["hits_this_call"] = int(previous.get("hits_this_call") or 0) + int(row.get("hits_this_call") or 0)


def attach_source_progress(payload: dict[str, Any]) -> None:
    """Project collection-window and pending-record facts without choosing a workflow."""
    watch = payload.get("watch") or {}
    if watch.get("window_complete") is False:
        payload["source_progress"] = {
            "complete": False,
            "reason": "collection_window_active",
            "remaining_seconds": watch.get("remaining_seconds"),
        }
    coverage = payload.get("coverage") or {}
    backlog = _int(coverage.get("spool_backlog_candidates"))
    if backlog > 0:
        current = payload.setdefault("source_progress", {})
        if isinstance(current, dict):
            current["complete"] = False
            current["pending_records"] = backlog
            current.setdefault("reason", "pending_records")


def attach_overload_note(payload: dict[str, Any], unjudged_backlog: int, *, threshold: int, backpressure_active: bool) -> None:
    """只投影积压与背压事实；不据此改变结论、委派方式或分析路线。"""
    if threshold <= 0 or unjudged_backlog < threshold:
        return
    payload["overload"] = {
        "unjudged_backlog": unjudged_backlog,
        "backpressure_active": bool(backpressure_active),
        "note": (
            f"当前未研判积压 {unjudged_backlog} 条。"
            + ("采集端已启用背压，等待持久队列推进。" if backpressure_active else "采集端尚未进入背压。")
            + "该状态只反映吞吐和覆盖，不代表任何记录的业务结论。"
        ),
    }


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def candidate_rows(digest: CallDigest) -> list[dict[str, Any]]:
    """候选批的模型可读行(harvester 落 spool 与 inline pull 共用同一渲染,契约不漂移)。"""
    return [_candidate_row(item) for item in digest.candidates]


# LLM: This is the sole projection from durable audit rows to model-visible rows.
# Normal events remain complete; only a single event larger than the model-safe window is bounded.
# 函数用途: 正常审计记录整条给模型；仅单条自身超出安全上下文时生成明确标记的头尾视图。
def candidate_model_view(
    row: dict[str, Any],
    *,
    max_event_tokens: int = 0,
    include_triage: bool = True,
) -> dict[str, Any]:
    event = row.get("event")
    excluded = {"event", "event_sha256", "source_record_key"}
    if not include_triage:
        excluded.add("triage")
    visible = {
        key: value
        for key, value in row.items()
        if key not in excluded
    }
    normalized = event if isinstance(event, dict) else {"value": event}
    visible["event"] = _bounded_extreme_event(
        normalized,
        source_ref=str(row.get("source_ref") or ""),
        max_tokens=max_event_tokens,
    )
    if row.get("event_sha256"):
        visible["event_sha256"] = str(row["event_sha256"])
    return visible


# LLM: Normal audit events pass through unchanged. Only a single event that exceeds the
# model-safe budget gets an explicit head+tail view; the complete event remains in raw storage.
# 函数用途: 仅处理单条日志自己就大到放不进模型的极端情况，并明确告诉模型中段被省略。
def _bounded_extreme_event(
    event: dict[str, Any],
    *,
    source_ref: str,
    max_tokens: int,
) -> dict[str, Any]:
    if max_tokens <= 0:
        return event
    from ..memory_archive import estimate_tokens

    if estimate_tokens(event) <= max_tokens:
        return event
    text = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    keep = max(128, max_tokens - 2000)
    head = max(1, int(keep * 0.6))
    tail = max(1, keep - head)
    return {
        "__oversize_truncated__": True,
        "source_ref": source_ref,
        "original_chars": len(text),
        "head": text[:head],
        "tail": text[-tail:],
        "note": "单条记录超过模型安全上下文；完整原文仍在本地审计账，可用 source_ref/ack_id inspect。",
    }


def group_rows(digest: CallDigest) -> list[dict[str, Any]]:
    """被压组的模型可读行(同上,spool 与 inline 同源)。"""
    return [_group_row(group) for group in digest.groups]


def _candidate_row(candidate: Candidate) -> dict[str, Any]:
    triage: dict[str, Any] = {
        "signature_window_count": candidate.window_count,
        "first_seen_signature": candidate.first_seen,
        "reason": candidate.reason,
    }
    if candidate.reason == "minority_field_value":
        # 少数派取值通道的结构化依据:哪个字段、哪个取值、窗口内出现几次/字段样本量——
        # 只给"为什么被抬上来"的计数事实,真假仍由模型看两端字段亲自定。
        triage["minority_value"] = {
            "path": candidate.value_path,
            "token": candidate.value_token,
            "value_window_count": candidate.value_window_count,
            "field_window_count": candidate.field_window_count,
        }
    if candidate.reason == "spec_target_value":
        # per-源判据命中的结构化依据:哪个字段、什么取值、命中哪种匹配模式
        # (target_value 精确/target_contains 子串/outside_normal 常态之外)。
        # 取值窗口频次一并给(>0 时):目标通常稀疏,命中取值若在窗口内大量出现,
        # 多半是判据配错(把常态配成了 target)——这是重判环推翻错误判据的关键证据。
        triage["spec_match"] = {
            "path": candidate.value_path,
            "value": candidate.value_token,
            "mode": candidate.spec_mode,
        }
        if candidate.value_window_count > 0:
            triage["spec_match"]["value_window_count"] = candidate.value_window_count
            triage["spec_match"]["field_window_count"] = candidate.field_window_count
    if candidate.reason == "confirmed_target_similar":
        # 反馈车道依据:命中了哪条已确认真目标的结构特征(字段+记号+窗口频次)。
        triage["feedback_match"] = {
            "path": candidate.value_path,
            "token": candidate.value_token,
            "value_window_count": candidate.value_window_count,
            "field_window_count": candidate.field_window_count,
        }
    if candidate.reason == "audit_sample":
        # 抽检车道依据:该常态组的体量事实(这是分层抽出来复核的普通流示例,非判据命中)。
        triage["audit_sample"] = {
            "group_window_count": candidate.window_count,
            "count_this_call": candidate.audit_group_count,
        }
    if candidate.reason == "full_stream_read":
        # 正常量直通:本批量在判读预算内,整批全量上(非筛选命中)——每条都要认真读。
        triage["full_read"] = True
    return {
        "stream_pos": candidate.seq_hint,
        "event": _capped_json(candidate.event, _EVENT_JSON_CAP),
        "triage": triage,
    }


def _group_row(group: GroupDigest) -> dict[str, Any]:
    return {
        "shape": group.sketch,
        "window_count": group.window_count,
        "count_this_call": group.call_count,
        "exemplar_stream_pos": group.exemplar_seq,
        "exemplar_event": _capped_json(group.exemplar, _EXEMPLAR_JSON_CAP),
    }


def _overflow_block(digest: CallDigest) -> dict[str, Any]:
    return {
        "count": len(digest.overflow),
        "note": "达标但超出本批候选上限的事件(坐标见 sample;完整清单在审计账)",
        "sample_stream_pos": [record.seq_hint for record in digest.overflow[:_OVERFLOW_SAMPLE_CAP]],
    }


def coverage_block(state: WatchState, extras: dict[str, Any]) -> dict[str, Any]:
    return {
        "cursor": state.cursor,
        "reached_stream_end": state.last_reached_end,
        "gap_events_total": state.totals.get("gap_events", 0),
        "events_seen_total": state.engine.totals.get("events_seen", 0),
        **extras,
    }


def source_binding_block(state: WatchState) -> dict[str, Any]:
    """Return the one durable source binding used by collection and workers."""
    return {
        "owner_id": state.owner_id,
        "source_id": state.source_id,
        "watch_id": state.watch_id,
        "source_profile_ref": state.source_profile_ref or None,
        "document_refs": list(state.document_refs),
        "source_config_version": state.source_config_version,
    }


def watch_block(state: WatchState) -> dict[str, Any]:
    elapsed = max(0.0, time.time() - state.opened_at)
    block: dict[str, Any] = {
        "opened_at": state.opened_at,
        "elapsed_seconds": round(elapsed, 1),
        "watch_window_seconds": state.watch_window_seconds,
        "closed": state.closed,
    }
    if state.closed_at > 0:
        block["closed_at"] = state.closed_at
        block["close_reason"] = state.close_reason
        block["close_pending_records"] = state.close_pending_records
    if state.watch_window_seconds > 0:
        block["remaining_seconds"] = round(max(0.0, state.watch_window_seconds - elapsed), 1)
        block["window_complete"] = elapsed >= state.watch_window_seconds
    return block


def _capped_json(event: dict, cap: int) -> Any:
    text = json.dumps(event, ensure_ascii=False, sort_keys=True)
    if len(text) <= cap:
        return event
    return {"__truncated__": text[:cap]}


def render_open_payload(state: WatchState, resumed: bool, *, unjudged_backlog: int = 0) -> dict[str, Any]:
    payload = {
        "ok": True,
        "action": "open",
        "watch_id": state.watch_id,
        "source_binding": source_binding_block(state),
        "named_audit_effect": {
            "source_bound": bool(state.audit_guarantee and state.audit_root_task_id),
            "effective_prompt_updated": False,
        },
        "resumed_existing_watch": resumed,
        "cursor": state.cursor,
        "watch": watch_block(state),
        "source_spec": (
            None
            if state.audit_guarantee
            else (dict(state.source_spec) if state.source_spec else None)
        ),
        "tuning": {
            "rare_threshold": state.tuning.rare_threshold,
            "window_seconds": state.tuning.window_seconds,
            "max_candidates_per_pull": state.tuning.max_candidates_per_pull,
        },
        "guidance": _open_guidance(state),
    }
    if unjudged_backlog > 0:
        # 续开/接管的第一眼就把"前面留下的账"怼到脸上(纯结构计数触发):继任者若只
        # 盯"从游标续读",前任已抬升未判完的候选就成孤儿(真机实锤 3 条真事躺 spool)。
        payload["spool_backlog_candidates"] = unjudged_backlog
        payload["backlog_note"] = (
            f"这路 watch 有 {unjudged_backlog} 条已持久化但尚未确认完成的记录。"
            "这是覆盖事实，不指定由哪个 Agent、以何种顺序或工具处理。"
        )
    if state.source_envelope:
        payload["source_envelope"] = public_source_envelope(state.source_envelope)
    attach_judgment_note(payload, state)
    return payload


def _open_guidance(state: WatchState) -> str:
    """Describe tool semantics without prescribing an Agent workflow."""
    common = (
        "watch_stream(action=pull) 负责取得后续批次；source_envelope、coverage、watch 和 backlog"
        "是结构化采集事实，不是业务结论。结合用户目标和可用工具自主决定分析、委派和汇报方式。"
    )
    if state.audit_guarantee:
        return (
            "已打开 /audit 保证档。完整记录进入耐久队列，并按等待时间或累计数据量"
            "形成一个可领取批次；batch_context 会给出待判体积和当前安全上下文估算。"
            "普通单条记录不会为凑批而截断；只有单条自身超过模型安全窗口的极端情况，"
            "模型才看带截断标记的头尾视图，完整原文仍在本地审计账可按 ack_id 回查。"
            "工具不会自动判读、上报、指定子代理或按分数路由。sample 可用于只读查看来源结构，"
            "但 configure 不适用于 /audit，不能覆盖用户原始任务。" + common
        )
    if state.judgment_note:
        return (
            "已打开数据源；用户先前提供的 judgment_note 会作为任务上下文原样显示。判据 spec "
            + ("已存在。" if state.source_spec else "尚未配置。")
            + common
        )
    if state.source_spec:
        return (
            "已打开数据源，现有结构化候选 spec 见 source_spec；它可被重新配置或移除。" + common
        )
    return (
        "已打开数据源，尚未配置可选的结构化候选 spec；sample/configure 与 passthrough "
        "均可用，是否使用由用户目标和当前证据决定。" + common
    )


def build_audit_record(drain, digest: CallDigest) -> dict[str, Any]:
    return {
        "t": round(time.time(), 3),
        "cursor_to": drain.cursor,
        "seen": len(drain.events),
        "reached_end": drain.reached_end,
        "gap_events": drain.gap_events,
        "escalated_pos": [c.seq_hint for c in digest.candidates],
        # 少数派取值/spec 判据通道单列(漏报归因"引擎抬没抬、哪条车道抬的"要能分通道审计)。
        "escalated_value_pos": [
            c.seq_hint for c in digest.candidates if c.reason == "minority_field_value"
        ],
        "escalated_spec_pos": [
            c.seq_hint for c in digest.candidates if c.reason == "spec_target_value"
        ],
        "overflow": [{"pos": o.seq_hint, "sig": o.signature, "wc": o.window_count} for o in digest.overflow],
        "groups": [{"sig": g.signature, "n": g.call_count, "win": g.window_count} for g in digest.groups],
    }


__all__ = [
    "AUDIT_PULL_GUIDANCE",
    "PULL_GUIDANCE",
    "attach_audit_receipt",
    "attach_content_rules_count",
    "attach_frequent_hit_alert",
    "attach_judgment_note",
    "attach_overload_note",
    "attach_source_progress",
    "build_audit_record",
    "candidate_model_view",
    "candidate_rows",
    "content_rules_block",
    "coverage_block",
    "frequent_hit_rows",
    "group_rows",
    "merge_frequent_hits",
    "order_candidate_rows",
    "render_open_payload",
    "render_pull_payload",
    "source_binding_block",
    "watch_block",
]
