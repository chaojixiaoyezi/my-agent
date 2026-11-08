# LLM: 模型费用只从原 canonical 账本累计；快照身份绑定同范围摘要，迟到物理事实只补增量，不改旧账。
# 模块用途: 保存模型消耗及数字投影；上下文在原锁内校验压缩及模型选择版本，不改费用合同。
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..common.json_io import (
    append_private_jsonl_records_unlocked,
    locked_json_path,
    seal_private_jsonl_tail_unlocked,
)
from ..contracts.model_call_latency import MODEL_LATENCY_FIELDS
from ..gateway_parts.io import locked_file_transition
from ..runtime_errors import DataCorruptionError
from ..user_space.storage_lanes import STORAGE_LANE_SLICE_BYTES
from .models import ConversationThread, ThreadModelUsageEvent
from .observation_index import _reader
from .store_io import json_row, jsonl_error, now, safe_file_stem
from .thread_ledger_segments import (
    archive_dir_for,
    archive_stamp,
    read_manifest,
    recover_pending,
    seal_if_due,
)

_PURPOSE_SCHEMA = "model_call_purpose_breakdown.v1"
# LLM: 用途桶必须与 model_call_ledger._PURPOSE_BUCKETS 保持同一集合；探测调用独立成桶，持久账校验按同一集合放行。
_PURPOSES = ("main", "auxiliary", "decision", "probe:tool_capability")

# LLM: 与 _model_usage_snapshot_delta 共用的可加数字段名单；写端判断用途桶“有没有用量”也只读这些字段，
#   未来加新加法字段时这里与增量函数同步补，判断自动覆盖。
_ADDITIVE_USAGE_FIELDS = (
    *MODEL_LATENCY_FIELDS,
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
# 身份字段成对：名字列表 + 按名物理尝试次数（模型账本 to_summary 产出）。增量行的名字由次数差值决定。
_IDENTITY_FIELDS = (("backends", "backend_attempt_counts"), ("models", "model_attempt_counts"))

# LLM: 读取端放行未知用途键的进程内诊断计数：按键名计次，只给 GET /status 的 usage_accounting 段投影
#   （gateway_parts/http_handlers._usage_accounting_diagnostics）。不写日志、不改求和结果、不报错，开放世界读法照旧。
#   计的是“读到的行次”：同一持久行每被求和一次就 +1，用来发现有没有拼错的键、是什么键，不是行数。
#   不同键名最多单独记 _UNKNOWN_PURPOSE_KEY_MAX_COUNT 个，超出只进 overflow_count，坏文件也撑不爆内存。
# 不同的未知用途键最多单独计 16 个：拼错的键通常只有一两个，再多就是坏数据，只记总数不记键名。
_UNKNOWN_PURPOSE_KEY_MAX_COUNT = 16
# 未知键名进计数（进而进 /status）前先截断：坏文件里的超长键名不能原样展示，64 个字符足够认出拼错的键。
_UNKNOWN_PURPOSE_KEY_MAX_CHARS = 64
# 键名只保留 ASCII 字母、数字和 :_-.，其它字符（空白、控制符、标记）一律替换成 _，/status 里不会出现怪字符。
_UNSAFE_PURPOSE_KEY_CHARS = re.compile(r"[^0-9A-Za-z:_.\-]")
_UNKNOWN_PURPOSE_KEYS_LOCK = threading.Lock()
_UNKNOWN_PURPOSE_KEYS: dict[str, Any] = {"keys": {}, "overflow_count": 0}


# LLM: 只影响诊断计数里的键名，读取端求和仍用原键。先替换不安全字符再截断，所以结果只含安全字符且不超过上限；
#   不同原键脱敏后可能撞成同一个名字，计数合并，这是诊断可接受的损失。
# 函数用途: 把未知用途键名变成能安全展示的形式（只留字母数字和 :_-.，最长 64 个字符）。
def _display_purpose_key(purpose: object) -> str:
    return _UNSAFE_PURPOSE_KEY_CHARS.sub("_", str(purpose))[:_UNKNOWN_PURPOSE_KEY_MAX_CHARS]


# LLM: 只在锁内改计数；已记过的键继续累加，新键超过上限只进 overflow_count。调用方传全部用途键，已知桶在这里过滤，
#   未知键按脱敏后的名字计。
# 函数用途: 把一批用途键里不认识的记进诊断计数（已知四桶不计，键名先脱敏截断）。
def _count_unknown_purpose_keys(purposes: Iterable[str]) -> None:
    unknown = [_display_purpose_key(purpose) for purpose in purposes if purpose not in _PURPOSES]
    if not unknown:
        return
    with _UNKNOWN_PURPOSE_KEYS_LOCK:
        keys = _UNKNOWN_PURPOSE_KEYS["keys"]
        for purpose in unknown:
            _UNKNOWN_PURPOSE_KEYS["overflow_count"] += int(not _bump_bounded_count(keys, purpose))


# LLM: 调用方持锁；只有“新键且已记满上限”才拒记，已有键不受上限影响。
# 函数用途: 给一个键 +1；键是新的且已记满上限就不记，返回 False。
def _bump_bounded_count(counts: dict[str, int], key: str) -> bool:
    if key not in counts and len(counts) >= _UNKNOWN_PURPOSE_KEY_MAX_COUNT:
        return False
    counts[key] = counts.get(key, 0) + 1
    return True


# LLM: 只读快照（复制），键按名字排序，可直接 JSON 化；改字段名须同步 http_handlers 的投影与 test_store_usage_open_world。
# 函数用途: 返回当前进程读到的未知用途键及次数（诊断用，只读）。
def unknown_purpose_key_counts() -> dict[str, Any]:
    with _UNKNOWN_PURPOSE_KEYS_LOCK:
        keys = _UNKNOWN_PURPOSE_KEYS["keys"]
        return {"keys": dict(sorted(keys.items())), "overflow_count": int(_UNKNOWN_PURPOSE_KEYS["overflow_count"])}


# LLM: 生产路径不调用；测试夹具用它隔离进程级计数。
# 函数用途: 清零未知用途键计数；只供测试和显式管理入口使用。
def reset_unknown_purpose_key_counts() -> None:
    with _UNKNOWN_PURPOSE_KEYS_LOCK:
        _UNKNOWN_PURPOSE_KEYS["keys"].clear()
        _UNKNOWN_PURPOSE_KEYS["overflow_count"] = 0


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

    # LLM: 只作显示缓存失效信号，不作计费身份或 CAS；计费仍读取原事件并遵守原追加锁。
    # 函数用途: 在模型边界用一次 stat 判断后台用量是否更新，避免逐 token 扫描历史。
    def revision(self, thread_id: str) -> tuple[int, ...]:
        try:
            stat = self._path(thread_id).stat()
        except FileNotFoundError:
            active = ()
        else:
            active = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        archive = archive_stamp(self._path(thread_id))
        return (*active, *(archive[0] or ()), *(archive[1] or ()))

    # LLM: 统计仅保存数字投影，不改活跃时间或历史；延迟到达的旧帧不得覆盖新采样。
    # 函数用途: 原子保存本代理最近模型统计，供重新打开会话读取，不作计费依据。
    def update_metrics(self, thread_id: str, metrics: dict[str, Any]) -> ConversationThread:
        from .model_metrics import newer_model_metrics

        # LLM: 持有会话锁期间合并显示副本；不访问模型和用量文件。
        # 函数用途: 保留最新的合法统计数，不产生新的运行活动。
        def apply(thread: ConversationThread) -> ConversationThread:
            return replace(thread, model_metrics=newer_model_metrics(thread.model_metrics, metrics))

        return self._update_thread_atomic(thread_id, apply)

    # LLM: 显示快照只接受原公开协议；compact及模型选择代次防止旧数回写；代次存在原字段中，不另建状态或改活跃时间。
    # 函数用途: 原子保存当前主/子代理的上下文估算及选择绑定，失败或旧代不覆盖当前值。
    def update_context_usage(
        self,
        thread_id: str,
        usage: dict[str, Any],
        *,
        expected_compact_generation: int,
    ) -> ConversationThread:
        from .context_usage import public_context_usage

        public = public_context_usage(usage)

        # LLM: 两种代次在原线程锁内核对；兼容旧调用未带选择预期，但新调用必须绑定采样时的模型选择。
        # 函数用途: 只合并同代数值显示和其模型选择绑定，不刷新活跃时间。
        def apply(thread: ConversationThread) -> ConversationThread:
            revision = usage.get("model_selection_revision", thread.model_selection_revision)
            if (not public or thread.compact_generation != expected_compact_generation
                    or type(revision) is not int or revision != thread.model_selection_revision):
                return thread
            return replace(thread, model_context_usage={
                **public, "compact_generation": expected_compact_generation,
                "model_selection_revision": revision,
            })

        return self._update_thread_atomic(thread_id, apply)

    # LLM: 新投影逐ID去重；迁移未完成只对本线程沿原精确路径，原追加锁覆盖整个判定和提交。
    # 函数用途: 幂等追加精确用量，不在正常热路扫描历史。
    def append_once(self, request: dict[str, Any]) -> ThreadModelUsageEvent:
        event = _thread_model_usage_event(request)
        self._require_thread(event.thread_id)
        return self._append_indexed(event)

    # LLM: 仍按同范围摘要计算单调delta；点查和投影更新在原追加协议内，不改变迟到事实口径。
    # 函数用途: 保存累计用量的新事实并保留重放冲突检查。
    def append_snapshot_once(
        self,
        request: dict[str, Any],
    ) -> ThreadModelUsageEvent:
        request = _snapshot_event_request(request)
        snapshot = _thread_model_usage_event(request)
        self._require_thread(snapshot.thread_id)
        return self._append_indexed(snapshot, snapshot_request=request)

    # LLM: 局部导入避免数字求和器与投影初始化形成模块循环；不缓存可变事件。
    # 函数用途: 取得当前线程独立的用量定位器。
    def _index(self, thread_id):
        from .usage_index import UsageIndex

        return UsageIndex(self._path(thread_id), thread_id)

    # LLM: canonical先提交，投影后提交；中间失败的字节后缀会在下一次prepare精确重放。
    # 函数用途: 在原锁内选择完整索引热路或迁移期间单线程精确回退。
    def _append_indexed(self, snapshot, *, snapshot_request=None, max_bytes=STORAGE_LANE_SLICE_BYTES):
        path = self._path(snapshot.thread_id)
        transition = path.with_name(f".{path.name}.append-once")
        with locked_file_transition(transition), locked_json_path(path):
            # 锁外预检后线程可能已退休，须在恢复或追加账本前重新核验。
            self._require_thread(snapshot.thread_id)
            recover_pending(path)
            seal_private_jsonl_tail_unlocked(path)
            index = self._index(snapshot.thread_id)
            with index._connection() as conn:
                complete = index.prepare(conn)
                if complete and index._get(conn, "coverage", "complete") == "complete":
                    existing = index.event(conn, snapshot.event_id)
                    if existing is not None:
                        return self._replay(existing, snapshot, snapshot_request is not None)
                    prior = index.prior(conn, snapshot) if snapshot_request is not None else []
                    event = self._delta_event(snapshot, snapshot_request, prior)
                    offset = path.stat().st_size if path.exists() else 0
                    append_private_jsonl_records_unlocked(path, [event.to_dict()], sort_keys=True)
                    index.committed(conn, event, offset)
                    self._seal_indexed(path, index, conn, max_bytes)
                    return event
            # 未完整的投影绝不能输出partial费用；只回退当前会话的既有精确逻辑。
            existing, prior = None, {}
            scope = _model_usage_scope(snapshot.to_dict())
            for item in self._exact_events(snapshot.thread_id):
                if item.event_id == snapshot.event_id:
                    existing = item
                if snapshot_request is not None and _model_usage_scope(item.to_dict()) == scope:
                    prior = _sum_model_call_summaries([prior, item.model_calls], count_unknown=False)
            if existing is not None:
                return self._replay(existing, snapshot, snapshot_request is not None)
            event = self._delta_event(snapshot, snapshot_request, [prior])
            append_private_jsonl_records_unlocked(path, [event.to_dict()], sort_keys=True)
            with index._connection() as conn:
                # 旧账按原 128 行预算续跑；投影未完整不能签发 carry，也不扩大单拍扫描。
                if path.stat().st_size >= max_bytes and index.prepare(conn, with_events=False):
                    self._seal_indexed(path, index, conn, max_bytes)
            return event

    def _seal_indexed(self, path, index, conn, max_bytes):
        if path.stat().st_size < max_bytes:
            return
        entry = seal_if_due(path, max_bytes=max_bytes, carry=index.carry(conn))
        if entry is not None:
            index.sealed(conn, entry)

    # LLM: 快照和加法事件保留各自原冲突合同，created_at沿首次原账。
    # 函数用途: 严格核验重复事件并返回首次提交结果。
    def _replay(self, existing, event, snapshot):
        equal = (_model_usage_scope(existing.to_dict()) == _model_usage_scope(event.to_dict()) and
                 _model_usage_snapshot_digest_from_delta(existing.model_calls) == _model_usage_snapshot_digest(event.model_calls)) if snapshot else (
                     _model_usage_identity_payload(existing) == _model_usage_identity_payload(event))
        if not equal:
            label = "model usage snapshot" if snapshot else "model usage"
            raise DataCorruptionError(f"{label} event id reused with different input: {event.event_id}")
        return existing

    # LLM: 所有delta字段和摘要仍由原函数生成，不能从展示数字推断费用。
    # 函数用途: 构造累计快照的新增事件或保留原加法事件。
    def _delta_event(self, snapshot, request, prior):
        if request is None:
            return snapshot
        delta = _model_usage_snapshot_delta(snapshot.model_calls, prior)
        delta["ledger_projection"] = {
            "kind": "cumulative_snapshot_delta",
            "snapshot_physical_model_attempt_count": _usage_int(snapshot.model_calls.get("physical_model_attempt_count")),
            "snapshot_digest": _model_usage_snapshot_digest(snapshot.model_calls),
        }
        return _thread_model_usage_event({**request, "model_calls": delta})

    # LLM: 仅迁移未完整时使用，不能把坏账当作空用量。
    # 函数用途: 读取本线程完整事件供过渡期精确计算。
    def _exact_events(self, thread_id):
        for event, error in self._event_records(thread_id):
            if error is not None:
                raise DataCorruptionError(f"conversation model usage ledger is unreadable: {thread_id}; coverage=partial")
            if event is not None:
                yield event

    # LLM: A corrupt row remains an explicit load error; consumers must never
    # convert missing or unreadable provider usage into a zero-cost turn.
    # 函数用途: 读取用量事件及损坏记录；limit<=0 全量，after 为跨段稳定的完整行逻辑字节偏移。
    def events_report(
        self,
        thread_id: str,
        *,
        limit: int = 0,
        after: int = 0,
    ) -> tuple[list[ThreadModelUsageEvent], list[dict[str, Any]]]:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("thread_id is required")
        events: list[ThreadModelUsageEvent] = []
        errors = []
        path = self._path(normalized)
        with locked_file_transition(path.with_name(f".{path.name}.append-once")), locked_json_path(path):
            recover_pending(path)
            records = self._event_records(normalized, after=after)
            try:
                for event, error in records:
                    if error is not None:
                        errors.append(error)
                    elif event is not None:
                        events.append(event)
                        if limit > 0 and len(events) >= limit:
                            break
            finally:
                records.close()
        return events, errors

    def _event_records(self, thread_id, *, after=0):
        """按逻辑字节游标流读封存段和活动尾巴；只物化调用方请求的事件页。"""
        if type(after) is not int or after < 0:
            raise ValueError("after must be a non-negative logical byte offset")
        active = self._path(thread_id)
        directory = archive_dir_for(active)
        manifest = read_manifest(directory)
        for error in manifest.get("load_errors", []):
            yield None, error
        if manifest["coverage"] != "complete" and not manifest.get("load_errors"):
            # 重建清单仍可读出原行；用量缺少结转来源时必须显式保留未知。
            yield None, {"coverage": "partial", "path": str(directory / "manifest.json"),
                         "message": "thread ledger manifest was rebuilt without carried totals"}
        base = 0
        entries = [*( (directory / entry["file"], entry) for entry in manifest["segments"]), (active, None)]
        for path, entry in entries:
            size = entry["bytes"] if entry else (path.stat().st_size if path.exists() else 0)
            if (entry is None and not size) or base + size <= after:
                base += size
                continue
            start = max(0, after - base)
            try:
                with _reader(path) as handle:
                    if start:
                        handle.seek(start - 1)
                        if handle.read(1) != b"\n":
                            raise ValueError("model usage cursor is not a complete row boundary")
                    digest = hashlib.sha256()
                    handle.seek(start)
                    for line_number, raw in enumerate(handle, start=1):
                        digest.update(raw)
                        if not raw.strip():
                            continue
                        row, error = json_row(raw.decode("utf-8", errors="replace"),
                                              context="conversation.model_usage", path=path, line_number=line_number)
                        if error is not None:
                            yield None, error
                            continue
                        try:
                            event = ThreadModelUsageEvent.from_dict(row)
                            if event.thread_id != thread_id:
                                raise ValueError("thread model usage scope mismatch")
                        except Exception as exc:
                            yield None, jsonl_error(exc, "conversation.model_usage", path=path)
                        else:
                            yield event, None
                    if entry and not start and digest.hexdigest() != entry["sha256"]:
                        yield None, {"coverage": "partial", "path": str(path), "message": "thread ledger segment digest mismatch"}
            except OSError as exc:
                yield None, {**jsonl_error(exc, "conversation.model_usage", path=path), "coverage": "partial"}
            base += size
        if after > base:
            raise ValueError("model usage cursor exceeds canonical ledger")

    # LLM: 完整投影输出精确数字；旧账迁移未完成仅回退该线程，不发布partial总量。
    # 函数用途: 读取累计用量，正常热路不再扫描JSONL。
    def summary(self, thread_id: str) -> dict[str, Any]:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("thread_id is required")
        path = self._path(normalized)
        with locked_file_transition(path.with_name(f".{path.name}.append-once")), locked_json_path(path):
            recover_pending(path)
            index = self._index(normalized)
            with index._connection() as conn:
                if index.prepare(conn, with_events=False):
                    return index.summary(conn)
            return self._exact_summary(normalized)

    # LLM: 原求和口径仅用于未迁移的当前线程，保持未知用途和旧账语义。
    # 函数用途: 在迁移期间完整重算当前线程数字。
    def _exact_summary(self, thread_id):
        provider = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "call_count": 0,
        }
        estimated = {"input_tokens": 0, "output_tokens": 0, "call_count": 0,
                     "unfinished_input_tokens": 0, "unfinished_call_count": 0}
        calls, count, known = {}, 0, True
        for event in self._exact_events(thread_id):
            breakdown = event.model_calls.get("usage_breakdown")
            breakdown = breakdown if isinstance(breakdown, dict) else {}
            _sum_usage_partition(provider, breakdown.get("provider"))
            _sum_usage_partition(estimated, breakdown.get("estimated"))
            calls = _sum_model_call_summaries([calls, event.model_calls], count_unknown=False)
            _count_unknown_purpose_keys(_purpose_rows(event.model_calls))
            count += 1
            known = known and "purpose_breakdown" in event.model_calls
        purposes = calls.get("purpose_breakdown")
        return {
            "schema": "thread_model_usage_summary.v1",
            "thread_id": str(thread_id or "").strip(),
            "event_count": count,
            "provider": provider,
            "estimated": estimated,
            **({"purpose_breakdown": purposes,
                "purpose_totals_known": known} if purposes else {}),
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


# LLM: 新账按累计容器代次相减，source 仅供诊断；缺少代次的历史账维持原范围，身份与增量共用此定义。
# 函数用途: 从结构化事件字段取得唯一用量范围，前后台交接和重启分别正确去重。
def _model_usage_scope(request: dict[str, Any]) -> tuple[str, ...]:
    calls = request.get("model_calls")
    scope_id = str(calls.get("usage_scope_id") or "") if isinstance(calls, dict) else ""
    identity = tuple(str(request.get(key) or "").strip() for key in ("thread_id", "request_id", "run_id", "task_id"))
    return (*identity, "scope:" + scope_id if scope_id else "legacy-source:" + str(request.get("source") or "").strip())


# LLM: 省略 ID 才启用宿主派生，显式空/冲突 ID 仍沿旧校验；摘要不含时间或展示来源，不暴露正文。
# 函数用途: 让所有实际收口入口使用同一稳定事件编号，迟到尝试可以补记而重放保持幂等。
def _snapshot_event_request(request: dict[str, Any]) -> dict[str, Any]:
    if "event_id" in request:
        return request
    calls = request.get("model_calls")
    digest = _model_usage_snapshot_digest(calls if isinstance(calls, dict) else {})
    identity = json.dumps([*_model_usage_scope(request), digest], ensure_ascii=True, separators=(",", ":"))
    event_id = "usage-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return {**request, "event_id": event_id}


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


# LLM: 同范围累计数不可倒退；用途桶复用同一增量规则且只下钻一层，不将分区重复加进根总量。
#   backends/models 记“本行这些调用用到的名字”，由 _identity_delta 按次数差值算，不再是“本范围新出现的名字”。
# 函数用途: 保存快照相对原增量的新增事实，用途只是同一本账的互斥分区。
def _model_usage_snapshot_delta(
    snapshot: dict[str, Any],
    prior_summaries: list[dict[str, Any]],
    *,
    include_purposes: bool = True,
) -> dict[str, Any]:
    prior = _sum_model_call_summaries(prior_summaries, include_purposes=include_purposes)
    delta: dict[str, Any] = {"schema": "model_call_summary.v1"}
    if snapshot.get("usage_scope_id"):
        delta["usage_scope_id"] = snapshot["usage_scope_id"]
    for key in _ADDITIVE_USAGE_FIELDS:
        delta[key] = _monotonic_usage_delta(snapshot.get(key), prior.get(key), key)
    delta["status_counts"] = _usage_mapping_delta(
        snapshot.get("status_counts"),
        prior.get("status_counts"),
        "status_counts",
    )
    for names_key, counts_key in _IDENTITY_FIELDS:
        names, counts = _identity_delta(snapshot, prior, names_key, counts_key)
        delta[names_key] = names
        if counts is not None:
            delta[counts_key] = counts
    snapshot_usage = snapshot.get("usage_breakdown")
    prior_usage = prior.get("usage_breakdown")
    delta["usage_breakdown"] = _usage_breakdown_delta(snapshot_usage, prior_usage)
    if include_purposes:
        current_rows, prior_rows = _purpose_rows(snapshot), _purpose_rows(prior)
        if current_rows or prior_rows:
            delta["purpose_breakdown"] = _purpose_breakdown_delta(current_rows, prior_rows)
    return delta


# LLM: 同一累计账（前台回合与随后的唤醒回合共用 request_id 与 usage_scope_id）分几次落盘时，本行用到的后端/模型
#   只能从按名尝试次数的差值得出：差值大于 0 的名字就是本行调用用到的，次数增量随行保存供下次相减（只留正数）。
#   旧快照没有按名次数时退回旧口径“本范围新出现的名字”，同名续用会是空，这是旧账已知局限，不猜。
#   次数倒退按数据损坏报错（_usage_mapping_delta），名字只认结构化字段。
# 函数用途: 算一条增量行的后端或模型名单，以及要随行保存的按名尝试次数。
def _identity_delta(
    snapshot: dict[str, Any],
    prior: dict[str, Any],
    names_key: str,
    counts_key: str,
) -> tuple[list[str], dict[str, int] | None]:
    counts = snapshot.get(counts_key)
    if not isinstance(counts, dict):
        prior_names = {str(item) for item in prior.get(names_key, []) if str(item)}
        return sorted({str(item) for item in snapshot.get(names_key, []) if str(item)} - prior_names), None
    delta_counts = _usage_mapping_delta(counts, prior.get(counts_key), counts_key)
    positive = {name: count for name, count in delta_counts.items() if count > 0}
    return sorted(positive), positive


# LLM: 与根加法字段同一增量口径，usage_breakdown 分 provider/estimated 两个映射做非负差值；schema 标签不参与。
# 函数用途: 组装快照相对先行的用量分区增量，供快照增量根引用。
def _usage_breakdown_delta(snapshot_usage: object, prior_usage: object) -> dict[str, Any]:
    current = snapshot_usage if isinstance(snapshot_usage, dict) else {}
    prior = prior_usage if isinstance(prior_usage, dict) else {}
    return {
        "schema": "model_usage_breakdown.v1",
        "provider": _usage_mapping_delta(
            current.get("provider"),
            prior.get("provider"),
            "usage_breakdown.provider",
        ),
        "estimated": _usage_mapping_delta(
            current.get("estimated"),
            prior.get("estimated"),
            "usage_breakdown.estimated",
        ),
    }


# LLM: 用途增量只落有事实的桶；探测桶只在真有探测用量时写（本快照或同范围先前行任一有结构化计数），
#   不给每行都写空桶；老三个桶照写，多数线程的行与上一版逐字节一致。
# 函数用途: 按需组装快照相对先行的用途增量结构（probe 空桶省略，其余照写）。
def _purpose_breakdown_delta(
    current_rows: dict[str, dict[str, Any]],
    prior_rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    purpose_breakdown: dict[str, Any] = {"schema": _PURPOSE_SCHEMA}
    for purpose in _PURPOSES:
        current_row = current_rows.get(purpose, {})
        prior_row = prior_rows.get(purpose, {})
        if (
            purpose == "probe:tool_capability"
            and not _purpose_bucket_has_usage(current_row)
            and not _purpose_bucket_has_usage(prior_row)
        ):
            continue
        purpose_breakdown[purpose] = _model_usage_snapshot_delta(
            current_row, [prior_row], include_purposes=False
        )
    return purpose_breakdown


# LLM: 用途桶是否“真有用量”只按结构化加法字段判断：调用/尝试/token 计数任一大于 0 才算有；
#   全 0 骨架（真实账本 to_summary 的桶、空桶求和得到的骨架）不算有，避免给每行写空探测桶。
#   判断只读数值字段，不看字典真假、不看文字；当前快照与先前累计共用，未来加法字段进 _ADDITIVE_USAGE_FIELDS 自动覆盖。
# 函数用途: 判断一个用途桶里有没有结构化用量，供写端决定是否落键。
def _purpose_bucket_has_usage(row: dict[str, Any]) -> bool:
    if any(_usage_int(row.get(key)) > 0 for key in _ADDITIVE_USAGE_FIELDS):
        return True
    status_counts = row.get("status_counts")
    if isinstance(status_counts, dict) and any(
        _usage_int(value) > 0 for value in status_counts.values()
    ):
        return True
    usage = row.get("usage_breakdown")
    if isinstance(usage, dict) and _purpose_usage_partition_has_value(usage):
        return True
    return False


# LLM: 分区映射（provider/estimated）里任何数字大于 0 都算该桶有用量；schema 字符串按非数值忽略。
# 函数用途: 只检查用量分区数值，配合 _purpose_bucket_has_usage 控制嵌套深度。
def _purpose_usage_partition_has_value(usage: dict[str, Any]) -> bool:
    for partition in ("provider", "estimated"):
        part = usage.get(partition)
        if isinstance(part, dict) and any(
            _usage_int(value) > 0 for value in part.values()
        ):
            return True
    return False


# LLM: 持久行已经是增量，不能当作新累计；用途复用同一求和且不递归嵌套，不改变旧字段含义。
#   按名尝试次数是映射，由 _add_identity_counts 按名相加，不能落进下面的整数求和（会被当成 0，下次增量就重复记名）。
# 函数用途: 合并用量增量；纯投影维护可关闭读取诊断，不能把写入计作用户读取。
def _sum_model_call_summaries(summaries: list[dict[str, Any]], *, include_purposes: bool = True, count_unknown: bool = True) -> dict[str, Any]:
    total: dict[str, Any] = {
        "backends": [],
        "models": [],
        "status_counts": {},
        "usage_breakdown": {"provider": {}, "estimated": {}},
    }
    for summary in summaries:
        for key, value in summary.items():
            if key in {"schema", "usage_scope_id", "ledger_projection", "status_counts", "usage_breakdown", "purpose_breakdown",
                       "backend_attempt_counts", "model_attempt_counts"}:
                continue
            if key in {"backends", "models"}:
                known = {str(item) for item in total[key] if str(item)}
                known.update(str(item) for item in value if str(item))
                total[key] = sorted(known)
            else:
                total[key] = _usage_int(total.get(key)) + _usage_int(value)
        _add_usage_mapping(total["status_counts"], summary.get("status_counts"))
        _add_identity_counts(total, summary)
        usage = summary.get("usage_breakdown")
        usage = usage if isinstance(usage, dict) else {}
        _add_usage_mapping(total["usage_breakdown"]["provider"], usage.get("provider"))
        _add_usage_mapping(total["usage_breakdown"]["estimated"], usage.get("estimated"))
    if include_purposes:
        purposes = _sum_purpose_breakdowns(summaries, count_unknown=count_unknown)
        if purposes:
            total["purpose_breakdown"] = purposes
    return total


# LLM: 只有行里真带按名尝试次数时才在总量里建这个键；旧行没有就不建，相减时按先前次数为 0 处理。
#   是否退回旧口径只看当前快照有没有按名次数（见 _identity_delta），不看这里。
# 函数用途: 把一条用量行的按名尝试次数按名累加进总量。
def _add_identity_counts(total: dict[str, Any], summary: dict[str, Any]) -> None:
    for _, counts_key in _IDENTITY_FIELDS:
        if isinstance(summary.get(counts_key), dict):
            _add_usage_mapping(total.setdefault(counts_key, {}), summary[counts_key])


# LLM: 用途是宿主定义的互斥角色分区，不是模型自述；开放世界只认 schema 与桶值形状，
#   不认识的用途键按“出现过的键”保留并参与求和（以后再加用途桶，旧读法也不会读坏新行）。
# 函数用途: 读取明确版本的用途快照，未知用途键原样保留；版本、非对象与递归桶仍严格报错。
def _purpose_rows(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if "purpose_breakdown" not in summary:
        return {}
    value = summary["purpose_breakdown"]
    if not isinstance(value, dict) or value.get("schema") != _PURPOSE_SCHEMA:
        raise DataCorruptionError("model usage purpose breakdown is invalid")
    rows = {purpose: row for purpose, row in value.items() if purpose != "schema"}
    if any(not isinstance(row, dict) or "purpose_breakdown" in row for row in rows.values()):
        raise DataCorruptionError("model usage purpose bucket is invalid")
    return rows


# LLM: 各桶互斥但复用根摘要加法；按“已知桶 + 出现过的桶”求和并保留，不能倒推旧调用用途。
#   出现过的未知键同时记进程内诊断计数（unknown_purpose_key_counts），只计不拦，拼错的键靠 /status 发现。
# 函数用途: 合并用途数字；显式读取才计未知键，投影写入保持无诊断副作用。
def _sum_purpose_breakdowns(summaries: list[dict[str, Any]], *, count_unknown: bool = True) -> dict[str, Any]:
    rows = [_purpose_rows(summary) for summary in summaries]
    if not any(rows):
        return {}
    if count_unknown:
        _count_unknown_purpose_keys(purpose for row in rows for purpose in row)
    purposes = [*_PURPOSES, *sorted({purpose for row in rows for purpose in row} - set(_PURPOSES))]
    return {"schema": _PURPOSE_SCHEMA, **{
        purpose: _sum_model_call_summaries([row.get(purpose, {}) for row in rows], include_purposes=False)
        for purpose in purposes
    }}


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
