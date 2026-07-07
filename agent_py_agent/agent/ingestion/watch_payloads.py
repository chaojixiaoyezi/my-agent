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
    "candidates 是抬上来的原始事件(宁多勿漏,带该源自己的唯一 ID 字段),"
    "已按车道价值排序(反馈/判据命中在前,audit_sample 抽检殿后)——按序逐条重判,"
    "一条判完立刻处置一条,别整批看完再统一处理。"
    "【triage.reason=full_stream_read 是正常量全量直通】:当前量在你判读预算内,系统没有"
    "做任何稀有度筛选,这一批就是流里的全部事件(内容过滤规则命中的常态除外,已记账)——"
    "每一条都认真读两端字段独立定性,禁止扫一眼说'都没事'打发;量涨到判不过来时系统才"
    "自动切回结构化宽筛。"
    "【triage 只解释这条为什么被抬上来,绝不是判真依据——spec_target_value 也一样】:"
    "判据是你自己学的,可能配错(真机实锤:把常态取值配成 target,照判据报=全误报);"
    "每条候选都必须独立重判——同时读触发/输入端和结果/响应端字段、对照源信封的判据说明,"
    "结果端才定真假;是不是目标由你这一步重判说了算,不由判据/抬升通道说了算。"
    "triage 里的取值窗口频次(value_window_count/field_window_count)是重判证据之一,"
    "但【频率不定真假】:命中取值在窗口内大量出现,既可能是判据配反(把常态配成 target),"
    "也可能是真事高发——按内容(触发端+结果端)定,配反就重新 sample+configure,"
    "真事就照报,绝不因'它太常见'弃报。"
    "若本源还没配判据 spec(open 返回里有提示),先 action=sample 学判据再 configure,"
    "花杂源不配判据会漏(噪声淹信号);判据只是引擎侧宽筛器,配了也不免逐条重判。"
    "suppressed_groups 是被压缩的高频【形状】(每组给一条完整示例事件+窗口计数)——"
    "抽查各组示例,确认某组是你漏列的常态记号就补进 normal_* 重新 configure"
    "(=建一条内容过滤规则,规则命中逐条记账、status 可查),真可疑再人工排查;"
    "spec 命中永不因取值频率被丢:高频命中类只会触发 frequent_hit_investigation 调查告警"
    "(带示例),由你按内容定去留;"
    "triage.reason=audit_sample 是【常态流抽检样本】(预筛放过的普通流按轮换抽出来复核,"
    "不是判据命中):独立定性,是目标照常入账上报,不是就放过——它专为撞出'语义上真、"
    "结构上和常态一样'的预筛盲区;reason=confirmed_target_similar 是【反馈车道】"
    "(与你之前确认过的真目标同结构特征),同样逐条独立重判,不因来源直接判真。"
    "【一条重判为真=一条结论】确认一条就立刻 record_finding 入账一条(claim=事件唯一 ID+结果端依据,"
    "并带上 watch_id 与该候选行的 stream_pos 两个参数原样复制——系统会把它的结构特征喂回预筛,"
    "以后自动抬同类、抽检也会向这个源倾斜,这是召回自愈的关键一步),"
    "再逐条上报(带事件唯一 ID 和理由)——禁止把多条命中折叠成'计数在涨/新增 N 条'式聚合概述;"
    "重判为假/拿不准的不入账不上报;【单凭本条两端字段定不了性、而任务给了可查的接口/"
    "有别的相关来源时,把该对起来看的对起来看】:用 web_fetch 带参数定向查相关接口"
    "(按事件里的 ID/时间等结构化线索查),或对照本任务其他 watch 路同时段的候选,"
    "多来源交叉印证后再定真假——查证后仍拿不准的如实说拿不准,别硬判。"
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
    attach_keep_watching_note(payload)
    return payload


def attach_judgment_note(payload: dict[str, Any], state: WatchState) -> None:
    """轻量记忆回显:用户教过的判读须知(configure 的 judgment_note)随每批候选带回——
    重启/补岗/换人接手都能看到"这个来源该怎么看",不用重教。纯搬运,不解读。"""
    if state.judgment_note:
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
        "spec 命中里有取值类正在窗口内高频出现(计数事实见各行,exemplar_event 是该类示例)。"
        "【这些命中一条都没被丢弃】:仍按车道名额逐条抬升,车道内稀有命中优先、高频命中沉底,"
        "超出名额的进 overflow 账。频率不定真假,这条告警只是请你按内容调查这一类"
        "(看示例的触发端+结果端,对照源信封判据):"
        "判为噪声——mode 是 target_* 说明判据大概率配反,mode 是 outside_normal 说明常态清单"
        "漏列了它——立即 action=configure 把该取值列进 normal_values/normal_value_contains"
        "(=建一条内容过滤规则:此后这一类不再进 spec 车道,规则命中逐条记账、status 的 "
        "content_rules 可查,规则可随时再 configure 调整/撤销);"
        "判为真事——照常逐条重判 + record_finding 上报(真事高发更是大事,绝不因'太常见'弃报),"
        "候选量大可 configure 提高 spec.max_per_pull 扩车道名额。"
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


def attach_keep_watching_note(payload: dict[str, Any]) -> None:
    """窗口未满时置顶结构化续蹲信号(治"追平即停"误判:读游标追上写游标≠盯守完成——
    spool 某刻为空只是慢产,窗口未满就歇工会漏掉之后写入的目标)。纯结构化条件。
    窗口已满但 spool 还有已抬未判积压时,置顶【清账再收工】信号(不足4·窗口末尾弃判:
    真机 90 分钟窗到期时还剩 ~100-260 条已抬候选无人重判=直接漏报;这些是窗口内的事件,
    判完才算盯完)。两个条件都是纯结构信号(窗口计时/积压计数)。"""
    watch = payload.get("watch") or {}
    if watch.get("window_complete") is False:
        payload["keep_watching"] = True
        remaining = watch.get("remaining_seconds")
        payload["keep_watching_note"] = (
            f"盯守窗口还剩 {remaining}s 未满:本批无候选/追平流尾都不是收工信号,"
            "继续 pull(长轮询)或登记 wait 到点回来接着盯,直到 window_complete=true。"
        )
        return
    coverage = payload.get("coverage") or {}
    backlog = _int(coverage.get("spool_backlog_candidates"))
    if watch.get("window_complete") is True and backlog > 0:
        payload["keep_watching"] = True
        payload["drain_before_close_note"] = (
            f"盯守窗口已走完,但还有 {backlog} 条已初筛抬升的候选没被你逐条重判"
            "(它们是窗口内发生的事件,弃判=漏报)——继续 pull 把这批积压判完再收工:"
            "重判为真的照常 record_finding 逐条入账,判完积压清零才算盯守完整结束。"
        )


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
    attach_judgment_note(payload, state)
    return payload


def _open_guidance(state: WatchState) -> str:
    """open 后的下一步引导:未配判据先走 learn→configure,已配直接盯。纯机制话术。"""
    common = (
        "之后循环调 watch_stream(action=pull, watch_id=…, max_wait_seconds=30~55):"
        "pull 持续消费数据流、只把结构化初筛的候选批给你判;每条候选自己看触发+结果两端定性,"
        "确认命中立即上报事件唯一 ID,然后继续 pull。盯满 watch_window_seconds 才算完成。"
        "source_envelope 是数据源自带的元数据(若含该源的结果端判据说明,严格按它定真假)。"
    )
    if state.judgment_note:
        return (
            "已打开盯守,本源已有判读须知(judgment_note,之前教过怎么看,重启/换人自动带回)"
            "——严格按须知定真假,不用让用户重教;判据 spec "
            + ("也已配好,直接 pull。" if state.source_spec else "还没配,若源花杂先 sample+configure。")
            + common
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
    "attach_content_rules_count",
    "attach_frequent_hit_alert",
    "attach_judgment_note",
    "build_audit_record",
    "candidate_rows",
    "content_rules_block",
    "coverage_block",
    "frequent_hit_rows",
    "group_rows",
    "merge_frequent_hits",
    "order_candidate_rows",
    "render_open_payload",
    "render_pull_payload",
    "watch_block",
]
