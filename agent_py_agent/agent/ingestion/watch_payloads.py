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
    "candidates 是【结构化宽筛】抬上来的原始事件(宁多勿漏,带该源自己的唯一 ID 字段)。"
    "【triage 只解释这条为什么被抬上来,绝不是判真依据——spec_target_value 也一样】:"
    "判据是你自己学的,可能配错(真机实锤:把常态取值配成 target,照判据报=全误报);"
    "每条候选都必须独立重判——同时读触发/输入端和结果/响应端字段、对照源信封的判据说明,"
    "结果端才定真假;是不是目标由你这一步重判说了算,不由判据/抬升通道说了算。"
    "triage 里的取值窗口频次(value_window_count/field_window_count)是重判证据:"
    "目标通常稀疏,命中取值若在窗口内大量出现(占字段样本量比例高),多半是判据配反了"
    "——别照报,先重新 sample+configure(把该取值列进常态、盯常态之外)。"
    "若本源还没配判据 spec(open 返回里有提示),先 action=sample 学判据再 configure,"
    "花杂源不配判据会漏(噪声淹信号);判据只是引擎侧宽筛器,配了也不免逐条重判。"
    "suppressed_groups 是被压缩的高频形状(每组给一条完整示例事件+窗口计数)——取值高频时"
    "即便'常态之外'也按常态压组(高频≈常态,防漏列常态刷屏);抽查各组示例,确认某组是你"
    "漏列的常态记号就补进 normal_* 重新 configure,真可疑再人工排查;"
    "triage.reason=audit_sample 是【常态流抽检样本】(预筛放过的普通流按轮换抽出来复核,"
    "不是判据命中):独立定性,是目标照常入账上报,不是就放过——它专为撞出'语义上真、"
    "结构上和常态一样'的预筛盲区;reason=confirmed_target_similar 是【反馈车道】"
    "(与你之前确认过的真目标同结构特征),同样逐条独立重判,不因来源直接判真。"
    "【一条重判为真=一条结论】确认一条就立刻 record_finding 入账一条(claim=事件唯一 ID+结果端依据,"
    "并带上 watch_id 与该候选行的 stream_pos 两个参数原样复制——系统会把它的结构特征喂回预筛,"
    "以后自动抬同类、抽检也会向这个源倾斜,这是召回自愈的关键一步),"
    "再逐条上报(带事件唯一 ID 和理由)——禁止把多条命中折叠成'计数在涨/新增 N 条'式聚合概述;"
    "重判为假/拿不准的不入账不上报;入账后继续 pull 盯守,别停。"
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
        "source_spec_configured": bool(state.source_spec),
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
        "source_spec": dict(state.source_spec) if state.source_spec else None,
        "tuning": {
            "rare_threshold": state.tuning.rare_threshold,
            "window_seconds": state.tuning.window_seconds,
            "max_candidates_per_pull": state.tuning.max_candidates_per_pull,
        },
        "guidance": _open_guidance(state),
    }
    if state.source_envelope:
        payload["source_envelope"] = dict(state.source_envelope)
    return payload


def _open_guidance(state: WatchState) -> str:
    """open 后的下一步引导:未配判据先走 learn→configure,已配直接盯。纯机制话术。"""
    common = (
        "之后循环调 watch_stream(action=pull, watch_id=…, max_wait_seconds=30~55):"
        "pull 持续消费数据流、只把结构化初筛的候选批给你判;每条候选自己看触发+结果两端定性,"
        "确认命中立即上报事件唯一 ID,然后继续 pull。盯满 watch_window_seconds 才算完成。"
        "source_envelope 是数据源自带的元数据(若含该源的结果端判据说明,严格按它定真假)。"
    )
    if state.source_spec:
        return (
            "已打开盯守,本源已配 per-源判据 spec(见 source_spec,重启/换人自动生效)——直接 pull。"
            "若判据过时(长期零候选/候选明显不对),重新 action=sample 学、configure 覆盖。" + common
        )
    return (
        "已打开盯守。【本源还没配 per-源判据 spec】——真实数据流常常又花又杂(高基数噪声字段"
        "淹掉结果端信号),不学判据直接盯会漏。先 action=sample 抓样本和字段分布,由你判断:"
        "结果端字段是哪个、目标/常态取值是什么、哪些是噪声字段;再 action=configure 提交结构化 "
        "spec(工具参数说明里有格式),配好才进入长期 pull。" + common
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
    "PULL_GUIDANCE",
    "build_audit_record",
    "candidate_rows",
    "coverage_block",
    "group_rows",
    "render_open_payload",
    "render_pull_payload",
    "watch_block",
]
