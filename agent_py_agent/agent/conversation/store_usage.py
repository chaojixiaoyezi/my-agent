# LLM: 模型费用只从原 canonical 账本累计；组件仅持有目录、线程校验与原子更新能力，不继承其它领域。
# 模块用途: 保存模型消耗及数字显示投影，供 finalizer、Compact 和 TUI 使用，锁与事件格式保持原合同。
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import locked_file_transition
from ..io.jsonl import append_jsonl
from ..runtime_errors import DataCorruptionError
from .models import ConversationThread, ThreadModelUsageEvent
from .store_io import jsonl_error, now, read_jsonl_report, safe_file_stem


# LLM: 模型计费事件与 preflight 显示分别保存，各自有唯一口径；共享 store 原子写入，不新增旁路文件。
# 类用途: 保存模型调用的消耗账及最近上下文显示，后者不能作为费用或任务完成证据。
class ModelUsageStore:
    # LLM: 依赖由 Store 一次组装；线程修改必须使用原文件锁和身份检查，组件不另开线程状态源。
    # 函数用途: 接收用量目录及两个线程能力，不创建目录、不读写账本。
    def __init__(
        self,
        directory: Path,
        *,
        require_thread: Callable[[str], ConversationThread],
        update_thread_atomic: Callable[
            [str, Callable[[ConversationThread], ConversationThread]], ConversationThread
        ],
    ) -> None:
        self._directory = directory
        self._require_thread = require_thread
        self._update_thread_atomic = update_thread_atomic

    # LLM: 文件名仍来自精确 thread_id 的既有映射；路径不从提示词、cwd 或模型回复推导。
    # 函数用途: 定位本领域原有的单会话 JSONL 用量账本。
    def _path(self, thread_id: str) -> Path:
        return self._directory / f"{safe_file_stem(thread_id)}.jsonl"

    # LLM: 统计仅保存数字投影，不改活跃时间或历史；延迟到达的旧帧不得覆盖新采样。
    # 函数用途: 原子保存本代理最近模型统计，供重新打开会话读取，不作计费依据。
    def update_metrics(self, thread_id: str, metrics: dict[str, Any]) -> ConversationThread:
        from .model_metrics import newer_model_metrics

        # LLM: 持有会话锁期间合并显示副本；不访问模型和用量文件。
        # 函数用途: 保留最新的合法统计数，不产生新的运行活动。
        def apply(thread: ConversationThread) -> ConversationThread:
            return replace(thread, model_metrics=newer_model_metrics(thread.model_metrics, metrics))

        return self._update_thread_atomic(thread_id, apply)

    # LLM: 显示快照只接受纯数字协议；generation CAS 防止压缩后写回旧数，不更新会话活跃时间或聊天历史。
    # 函数用途: 原子保存当前主/子代理最近一次调用前的上下文，失败或旧代不能覆盖当前值。
    def update_context_usage(
        self,
        thread_id: str,
        usage: dict[str, Any],
        *,
        expected_compact_generation: int,
    ) -> ConversationThread:
        from .context_usage import public_context_usage

        public = public_context_usage(usage)

        # LLM: CAS 在原线程锁内核对代次；迟到遥测不能覆盖压缩后的新一代。
        # 函数用途: 只合并当前代数值显示，不刷新活跃时间。
        def apply(thread: ConversationThread) -> ConversationThread:
            if not public or thread.compact_generation != expected_compact_generation:
                return thread
            return replace(thread, model_context_usage={
                **public, "compact_generation": expected_compact_generation,
            })

        return self._update_thread_atomic(thread_id, apply)

    # LLM: This is the one durable per-thread usage append. The event id is a
    # host-generated idempotency key; conflicting reuse fails closed instead of
    # silently changing historical cost facts.
    # 函数用途: 将一次已结束模型轮的精确用量幂等追加到所属会话。
    def append_once(self, request: dict[str, Any]) -> ThreadModelUsageEvent:
        event = _thread_model_usage_event(request)
        self._require_thread(event.thread_id)
        path = self._path(event.thread_id)
        transition = path.with_name(f".{path.name}.append-once")
        with locked_file_transition(transition):
            events, load_errors = self.events_report(event.thread_id)
            if load_errors:
                raise DataCorruptionError(
                    f"conversation model usage ledger is unreadable: {event.thread_id}"
                )
            existing = next(
                (item for item in events if item.event_id == event.event_id),
                None,
            )
            if existing is not None:
                if _model_usage_identity_payload(existing) != _model_usage_identity_payload(
                    event
                ):
                    raise DataCorruptionError(
                        f"model usage event id reused with different input: {event.event_id}"
                    )
                return existing
            append_jsonl(path, event.to_dict(), sort_keys=True)
            return event

    # LLM: Finalizers submit a cumulative request/run snapshot, while this
    # append-only ledger stores only the monotonic delta since prior snapshots
    # in the same exact scope. The snapshot digest makes replay idempotent even
    # after later deltas exist and rejects conflicting reuse of an event id.
    # 函数用途: 把同一轮多次收口的累计模型用量换算成逐次增量，避免 Compact 重试重复计费或复用事件键失败。
    def append_snapshot_once(
        self,
        request: dict[str, Any],
    ) -> ThreadModelUsageEvent:
        snapshot = _thread_model_usage_event(request)
        self._require_thread(snapshot.thread_id)
        path = self._path(snapshot.thread_id)
        transition = path.with_name(f".{path.name}.append-once")
        snapshot_digest = _model_usage_snapshot_digest(snapshot.model_calls)
        with locked_file_transition(transition):
            events, load_errors = self.events_report(snapshot.thread_id)
            if load_errors:
                raise DataCorruptionError(
                    f"conversation model usage ledger is unreadable: {snapshot.thread_id}"
                )
            existing = next(
                (item for item in events if item.event_id == snapshot.event_id),
                None,
            )
            if existing is not None:
                if (
                    _model_usage_event_scope(existing)
                    != _model_usage_event_scope(snapshot)
                    or _model_usage_snapshot_digest_from_delta(existing.model_calls)
                    != snapshot_digest
                ):
                    raise DataCorruptionError(
                        f"model usage snapshot event id reused with different input: "
                        f"{snapshot.event_id}"
                    )
                return existing
            prior = [
                item.model_calls
                for item in events
                if _model_usage_event_scope(item) == _model_usage_event_scope(snapshot)
            ]
            delta = _model_usage_snapshot_delta(snapshot.model_calls, prior)
            delta["ledger_projection"] = {
                "kind": "cumulative_snapshot_delta",
                "snapshot_physical_model_attempt_count": _usage_int(
                    snapshot.model_calls.get("physical_model_attempt_count")
                ),
                "snapshot_digest": snapshot_digest,
            }
            event = _thread_model_usage_event({**request, "model_calls": delta})
            append_jsonl(path, event.to_dict(), sort_keys=True)
            return event

    # LLM: A corrupt row remains an explicit load error; consumers must never
    # convert missing or unreadable provider usage into a zero-cost turn.
    # 函数用途: 读取指定会话的全部模型用量事件及损坏记录。
    def events_report(
        self,
        thread_id: str,
    ) -> tuple[list[ThreadModelUsageEvent], list[dict[str, Any]]]:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("thread_id is required")
        path = self._path(normalized)
        if not path.exists():
            return [], []
        report = read_jsonl_report(path, context="conversation.model_usage")
        events: list[ThreadModelUsageEvent] = []
        errors = list(report.load_errors)
        for row in report.rows:
            try:
                event = ThreadModelUsageEvent.from_dict(row)
                if event.thread_id != normalized:
                    raise ValueError("thread model usage scope mismatch")
                events.append(event)
            except Exception as exc:
                errors.append(
                    jsonl_error(exc, "conversation.model_usage", path=path)
                )
        return events, errors

    # LLM: Thread totals add only the explicit provider/estimated partitions
    # already frozen in each event; legacy mixed totals are not reverse-engineered.
    # 函数用途: 汇总一个会话的供应商真值和本地估算，供状态面与对照测试读取。
    def summary(self, thread_id: str) -> dict[str, Any]:
        events, load_errors = self.events_report(thread_id)
        if load_errors:
            raise DataCorruptionError(
                f"conversation model usage ledger is unreadable: {thread_id}"
            )
        provider = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "call_count": 0,
        }
        estimated = {"input_tokens": 0, "output_tokens": 0, "call_count": 0}
        for event in events:
            breakdown = event.model_calls.get("usage_breakdown")
            breakdown = breakdown if isinstance(breakdown, dict) else {}
            _sum_usage_partition(provider, breakdown.get("provider"))
            _sum_usage_partition(estimated, breakdown.get("estimated"))
        return {
            "schema": "thread_model_usage_summary.v1",
            "thread_id": str(thread_id or "").strip(),
            "event_count": len(events),
            "provider": provider,
            "estimated": estimated,
        }


# LLM: Event construction accepts only explicit thread/request identities and a
# frozen model-call summary; it never falls back to owner, cwd, prompt, or task prose.
# 函数用途: 校验并构造一次线程模型用量事件。
def _thread_model_usage_event(request: dict[str, Any]) -> ThreadModelUsageEvent:
    model_calls = request.get("model_calls")
    event = ThreadModelUsageEvent(
        event_id=str(request.get("event_id") or "").strip(),
        thread_id=str(request.get("thread_id") or "").strip(),
        request_id=str(request.get("request_id") or "").strip(),
        run_id=str(request.get("run_id") or "").strip(),
        task_id=str(request.get("task_id") or "").strip(),
        source=str(request.get("source") or "").strip(),
        model_calls=dict(model_calls) if isinstance(model_calls, dict) else {},
        created_at=now(request.get("now")),
    )
    return ThreadModelUsageEvent.from_dict(event.to_dict())


# LLM: Replay equality excludes created_at because the first committed timestamp
# is authoritative; every other field must remain byte-for-byte equivalent.
# 函数用途: 生成模型用量事件用于幂等冲突检查的稳定载荷。
def _model_usage_identity_payload(event: ThreadModelUsageEvent) -> dict[str, Any]:
    payload = event.to_dict()
    payload.pop("created_at", None)
    return payload


# LLM: 新账按累计容器代次相减，source 仅供诊断；缺少代次的历史账维持原范围，不隐式迁移费用。
# 函数用途: 区分重复提交与重启后的新调用，前后台切换不再重复计费。
def _model_usage_event_scope(event: ThreadModelUsageEvent) -> tuple[str, ...]:
    scope_id = str(event.model_calls.get("usage_scope_id") or "")
    return (
        event.thread_id,
        event.request_id,
        event.run_id,
        event.task_id,
        "scope:" + scope_id if scope_id else "legacy-source:" + event.source,
    )


# LLM: The digest covers the original cumulative snapshot, not the stored
# delta marker, so an exact replay remains stable after later events append.
# 函数用途: 为累计模型用量快照生成不含时间戳的稳定指纹。
def _model_usage_snapshot_digest(model_calls: dict[str, Any]) -> str:
    payload = dict(model_calls)
    payload.pop("ledger_projection", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# LLM: Only rows written by append_snapshot_once carry this marker;
# a legacy/direct additive event cannot silently impersonate a snapshot replay.
# 函数用途: 从已落盘增量行读取原累计快照指纹，供幂等重放核对。
def _model_usage_snapshot_digest_from_delta(model_calls: dict[str, Any]) -> str:
    marker = model_calls.get("ledger_projection")
    marker = marker if isinstance(marker, dict) else {}
    if str(marker.get("kind") or "") != "cumulative_snapshot_delta":
        return ""
    return str(marker.get("snapshot_digest") or "").strip()


# LLM: Every numeric field in a cumulative model_call_summary is monotonic at
# finalization boundaries. Regression is corruption, never a reason to clamp or
# duplicate spend. Lists are represented as newly observed set members.
# 函数用途: 用当前累计快照减去同范围既有增量，生成可安全求和的一条模型用量事件。
def _model_usage_snapshot_delta(
    snapshot: dict[str, Any],
    prior_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    prior = _sum_model_call_summaries(prior_summaries)
    additive_fields = (
        "logical_model_turn_count",
        "physical_model_attempt_count",
        "model_retry_count",
        "provider_http_attempt_count",
        "provider_http_retry_count",
        "accounted_input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "provider_usage_call_count",
        "estimated_usage_call_count",
    )
    delta: dict[str, Any] = {"schema": "model_call_summary.v1"}
    if snapshot.get("usage_scope_id"):
        delta["usage_scope_id"] = snapshot["usage_scope_id"]
    for key in additive_fields:
        delta[key] = _monotonic_usage_delta(snapshot.get(key), prior.get(key), key)
    delta["status_counts"] = _usage_mapping_delta(
        snapshot.get("status_counts"),
        prior.get("status_counts"),
        "status_counts",
    )
    prior_backends = {str(item) for item in prior.get("backends", []) if str(item)}
    prior_models = {str(item) for item in prior.get("models", []) if str(item)}
    delta["backends"] = sorted(
        {str(item) for item in snapshot.get("backends", []) if str(item)}
        - prior_backends
    )
    delta["models"] = sorted(
        {str(item) for item in snapshot.get("models", []) if str(item)}
        - prior_models
    )
    snapshot_usage = snapshot.get("usage_breakdown")
    snapshot_usage = snapshot_usage if isinstance(snapshot_usage, dict) else {}
    prior_usage = prior.get("usage_breakdown")
    prior_usage = prior_usage if isinstance(prior_usage, dict) else {}
    delta["usage_breakdown"] = {
        "schema": "model_usage_breakdown.v1",
        "provider": _usage_mapping_delta(
            snapshot_usage.get("provider"),
            prior_usage.get("provider"),
            "usage_breakdown.provider",
        ),
        "estimated": _usage_mapping_delta(
            snapshot_usage.get("estimated"),
            prior_usage.get("estimated"),
            "usage_breakdown.estimated",
        ),
    }
    return delta


# LLM: Prior persisted rows are already additive deltas (or one legacy additive
# event), so aggregation must never reinterpret them as fresh cumulative input.
# 函数用途: 合并同一模型用量范围内已经落盘的增量，供下一份累计快照做减法。
def _sum_model_call_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {
        "backends": [],
        "models": [],
        "status_counts": {},
        "usage_breakdown": {"provider": {}, "estimated": {}},
    }
    for summary in summaries:
        for key, value in summary.items():
            if key in {"schema", "usage_scope_id", "ledger_projection", "status_counts", "usage_breakdown"}:
                continue
            if key in {"backends", "models"}:
                known = {str(item) for item in total[key] if str(item)}
                known.update(str(item) for item in value if str(item))
                total[key] = sorted(known)
            else:
                total[key] = _usage_int(total.get(key)) + _usage_int(value)
        _add_usage_mapping(total["status_counts"], summary.get("status_counts"))
        usage = summary.get("usage_breakdown")
        usage = usage if isinstance(usage, dict) else {}
        _add_usage_mapping(total["usage_breakdown"]["provider"], usage.get("provider"))
        _add_usage_mapping(total["usage_breakdown"]["estimated"], usage.get("estimated"))
    return total


# LLM: Mapping deltas stay open to future numeric counters while schema labels
# are ignored; any cumulative regression is explicit data corruption.
# 函数用途: 对状态数或供应商用量分区逐字段做非负累计差值。
def _usage_mapping_delta(current: object, prior: object, label: str) -> dict[str, int]:
    current_row = current if isinstance(current, dict) else {}
    prior_row = prior if isinstance(prior, dict) else {}
    keys = {
        str(key)
        for key in (*current_row.keys(), *prior_row.keys())
        if str(key) != "schema"
    }
    return {
        key: _monotonic_usage_delta(
            current_row.get(key),
            prior_row.get(key),
            f"{label}.{key}",
        )
        for key in sorted(keys)
    }


# LLM: Aggregation accepts only non-negative integer counters; malformed values
# cannot become negative cost or silently erase a previous event.
# 函数用途: 把一个用量分区的数字累加到已有字典。
def _add_usage_mapping(target: dict[str, int], source: object) -> None:
    row = source if isinstance(source, dict) else {}
    for key, value in row.items():
        if str(key) == "schema":
            continue
        normalized = str(key)
        target[normalized] = _usage_int(target.get(normalized)) + _usage_int(value)


# LLM: Cumulative usage snapshots cannot move backward between completed
# finalizations in one exact request/run scope; fail closed if they do.
# 函数用途: 计算单个累计计数器的新增加量并拒绝倒退。
def _monotonic_usage_delta(current: object, prior: object, label: str) -> int:
    current_value = _usage_int(current)
    prior_value = _usage_int(prior)
    if current_value < prior_value:
        raise DataCorruptionError(f"model usage cumulative snapshot regressed: {label}")
    return current_value - prior_value


# LLM: Usage coercion is shared by additive snapshot helpers and treats invalid
# values as zero without permitting negative persisted totals.
# 函数用途: 将模型用量字段归一为非负整数。
def _usage_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: Partition addition is open to future numeric keys only through the
# destination schema; unknown source fields cannot silently enter totals.
# 函数用途: 将一条事件中的非负 token/调用计数累加到指定汇总分区。
def _sum_usage_partition(target: dict[str, int], source: object) -> None:
    row = source if isinstance(source, dict) else {}
    for key in target:
        try:
            value = max(0, int(row.get(key) or 0))
        except (TypeError, ValueError):
            value = 0
        target[key] += value
