"""watch_stream 工具的载荷渲染:候选批/覆盖账目/盯守进度 → 给模型的 JSON。"""

from __future__ import annotations

import json
import time
from typing import Any

from .engine import CallDigest, Candidate, GroupDigest
from .watch_state import WatchState

_EVENT_JSON_CAP = 1600
_EXEMPLAR_JSON_CAP = 500
_OVERFLOW_SAMPLE_CAP = 20

PULL_GUIDANCE = (
    "candidates 是【结构化初筛】抬上来的原始事件(带该源自己的唯一 ID 字段;triage.reason "
    "标注抬升通道:稀有形状或少数派取值),不代表就是目标——每条你都要亲自判:"
    "同时看触发/输入端和结果/响应端字段,结果端才定真假。"
    "suppressed_groups 是被压缩的高频形状(每组给一条完整示例事件+窗口计数),值得抽查示例确认没漏判;"
    "确认命中就立刻按任务要求上报(带事件唯一 ID 和理由),然后继续 pull 盯守,别停。"
    "coverage 如实记录本次覆盖到哪、有没有缺口;coverage.spool_backlog_candidates>0 表示"
    "初筛候选还有积压等你判,立即继续 pull 消化别闲等;盯满窗口前不要收工。"
    "【追平流尾≠盯守结束】本批候选为空/reached_stream_end=true 只代表此刻没新事件:"
    "watch.window_complete=false 就必须继续——要么直接下一轮 pull(带 max_wait_seconds 长轮询),"
    "要么登记 wait 提醒到点回来接着 pull;绝不因'当前没货'提前收工。"
)


def render_pull_payload(state: WatchState, digest: CallDigest, extras: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "ok": True,
        "action": "pull",
        "watch_id": state.watch_id,
        "source_envelope": dict(state.source_envelope),
        "candidates": candidate_rows(digest),
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
    _attach_keep_watching_note(payload)
    return payload


def _attach_keep_watching_note(payload: dict[str, Any]) -> None:
    """窗口未满时置顶结构化续蹲信号(治"追平即停"误判:读游标追上写游标≠盯守完成——
    spool 某刻为空只是慢产,窗口未满就歇工会漏掉之后写入的目标)。纯结构化条件。"""
    watch = payload.get("watch") or {}
    if watch.get("window_complete") is False:
        payload["keep_watching"] = True
        remaining = watch.get("remaining_seconds")
        payload["keep_watching_note"] = (
            f"盯守窗口还剩 {remaining}s 未满:本批无候选/追平流尾都不是收工信号,"
            "继续 pull(长轮询)或登记 wait 到点回来接着盯,直到 window_complete=true。"
        )


def candidate_rows(digest: CallDigest) -> list[dict[str, Any]]:
    """候选批的模型可读行(harvester 落 spool 与 inline pull 共用同一渲染,契约不漂移)。"""
    return [_candidate_row(item) for item in digest.candidates]


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


def watch_block(state: WatchState) -> dict[str, Any]:
    elapsed = max(0.0, time.time() - state.opened_at)
    block: dict[str, Any] = {
        "opened_at": state.opened_at,
        "elapsed_seconds": round(elapsed, 1),
        "watch_window_seconds": state.watch_window_seconds,
        "closed": state.closed,
    }
    if state.watch_window_seconds > 0:
        block["remaining_seconds"] = round(max(0.0, state.watch_window_seconds - elapsed), 1)
        block["window_complete"] = elapsed >= state.watch_window_seconds
    return block


def _capped_json(event: dict, cap: int) -> Any:
    text = json.dumps(event, ensure_ascii=False, sort_keys=True)
    if len(text) <= cap:
        return event
    return {"__truncated__": text[:cap]}


def render_open_payload(state: WatchState, resumed: bool) -> dict[str, Any]:
    payload = {
        "ok": True,
        "action": "open",
        "watch_id": state.watch_id,
        "resumed_existing_watch": resumed,
        "cursor": state.cursor,
        "watch": watch_block(state),
        "tuning": {
            "rare_threshold": state.tuning.rare_threshold,
            "window_seconds": state.tuning.window_seconds,
            "max_candidates_per_pull": state.tuning.max_candidates_per_pull,
        },
        "guidance": (
            "已打开盯守。接下来循环调 watch_stream(action=pull, watch_id=…, max_wait_seconds=30~55):"
            "pull 会持续消费数据流并只把结构化稀有的候选批给你判;每条候选自己看触发+结果两端定性,"
            "确认命中立即上报事件唯一 ID,然后继续 pull。盯满 watch_window_seconds 才算完成。"
            "source_envelope 是数据源自带的元数据(常含该源的结果端判据说明),研判前先读一遍、"
            "严格按它定真假,别自立判据。"
        ),
    }
    if state.source_envelope:
        payload["source_envelope"] = dict(state.source_envelope)
    return payload


def build_audit_record(drain, digest: CallDigest) -> dict[str, Any]:
    return {
        "t": round(time.time(), 3),
        "cursor_to": drain.cursor,
        "seen": len(drain.events),
        "reached_end": drain.reached_end,
        "gap_events": drain.gap_events,
        "escalated_pos": [c.seq_hint for c in digest.candidates],
        # 少数派取值通道单列(漏报归因"引擎抬没抬"要能分通道审计)。
        "escalated_value_pos": [
            c.seq_hint for c in digest.candidates if c.reason == "minority_field_value"
        ],
        "overflow": [{"pos": o.seq_hint, "sig": o.signature, "wc": o.window_count} for o in digest.overflow],
        "groups": [{"sig": g.signature, "n": g.call_count, "win": g.window_count} for g in digest.groups],
    }


__all__ = [
    "PULL_GUIDANCE",
    "build_audit_record",
    "candidate_rows",
    "coverage_block",
    "group_rows",
    "render_open_payload",
    "render_pull_payload",
    "watch_block",
]
