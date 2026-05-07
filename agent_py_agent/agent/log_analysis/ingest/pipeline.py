# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...file_io import append_jsonl
from ..parsers.base import LogParser, ParserError
from ..parsers.common import DEFAULT_PAYLOAD_MAX_CHARS, sha256_json, utc_now
from ..parsers.registry import ParserRegistry, default_registry
from .checkpoint import CheckpointStore, safe_source_id, write_json_atomic
from .dead_letter import DeadLetterWriter
from .dedup import DedupStore

if TYPE_CHECKING:
    from .pipeline_stages import EventWriter, ManifestWriter, RecordIteratorFactory


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 IngestResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 IngestResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class IngestResult:
    batch_id: str
    source_id: str
    source_path: str
    file_format: str
    status: str
    parsed_count: int
    stored_count: int
    duplicate_count: int
    dead_letter_count: int
    skipped_count: int
    content_hash: str
    manifest_path: str
    checkpoint_path: str
    events_path: str | None
    dead_letter_refs: list[dict[str, Any]]
    stored_event_ids: list[str]


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _RecordIteratorRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _RecordIteratorRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _RecordIteratorRequest:
    source_path: Path
    file_format: str
    parser: LogParser
    batch_id: str
    source_id: str
    source_product: str | None
    dead_letters: DeadLetterWriter


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 IngestPipelineOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 IngestPipelineOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class IngestPipelineOptions:
    registry: ParserRegistry | None = None
    store: Any | None = None
    payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS
    write_batch_size: int = 1000


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 IngestFileOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 IngestFileOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class IngestFileOptions:
    # LLM: File ingest options are the public bundle for legacy keyword callers.
    source_id: str | None = None
    source_product: str | None = None
    parser_id: str = "security_alert_v1"
    file_format: str | None = None


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 JsonlEventSink 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 JsonlEventSink 的状态和协作方法，作为当前模块对外复用的领域对象。
class JsonlEventSink:
    """Fallback event sink used until Worker C's LocalLogStore is available."""

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.events_path = self.root / "events.jsonl"

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 write_events 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 write events 相关记录，集中处理目标路径、格式化和状态更新。
    def write_events(self, events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        count = 0
        for event in events:
            append_jsonl(self.events_path, dict(event), sort_keys=True)
            count += 1
        return {"count": count, "path": str(self.events_path)}


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 IngestPipeline 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 IngestPipeline 的状态和协作方法，作为当前模块对外复用的领域对象。
class IngestPipeline:
    """Local file ingest pipeline for SecurityAlertV1 CSV/JSONL files."""

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(
        self,
        root: str | Path,
        *,
        options: IngestPipelineOptions | None = None,
        registry: ParserRegistry | None = None,
        store: Any | None = None,
        payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS,
        write_batch_size: int = 1000,
    ):
        ingest_options = options or IngestPipelineOptions(
            registry=registry,
            store=store,
            payload_max_chars=int(payload_max_chars),
            write_batch_size=int(write_batch_size),
        )
        self.root = Path(root)
        self.payload_max_chars = ingest_options.payload_max_chars
        self.write_batch_size = max(1, ingest_options.write_batch_size)
        self.registry = ingest_options.registry or default_registry(payload_max_chars=ingest_options.payload_max_chars)
        self.fallback_sink = JsonlEventSink(self.root)
        self.store = ingest_options.store or self._default_store()
        self.checkpoints = CheckpointStore(self.root)
        self.dedup = DedupStore(self.root / "dedup.sqlite3")

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _default_store 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 default store 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def _default_store(self) -> Any:
        from ..storage.local_store import LocalLogStore

        return LocalLogStore(self.root)

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 ingest_file 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 ingest file 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def ingest_file(
        self,
        path: str | Path,
        *,
        options: IngestFileOptions | None = None,
        source_id: str | None = None,
        source_product: str | None = None,
        parser_id: str = "security_alert_v1",
        file_format: str | None = None,
    ) -> IngestResult:
        """Ingest a single file and return structured result."""
        from .pipeline_enrich import enrich_ingest_file

        ingest_options = options or IngestFileOptions(
            source_id=source_id,
            source_product=source_product,
            parser_id=str(parser_id),
            file_format=file_format,
        )
        return enrich_ingest_file(self, path, options=ingest_options)

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _iter_parsed_records 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 iter parsed records 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def _iter_parsed_records(
        self,
        source_path: Path | None = None,
        *,
        request: _RecordIteratorRequest | None = None,
        file_format: str = "",
        parser: LogParser | None = None,
        batch_id: str = "",
        source_id: str = "",
        source_product: str | None = None,
        dead_letters: DeadLetterWriter | None = None,
    ):
        if request is None:
            request = _RecordIteratorRequest(
                source_path=source_path or Path(""),
                file_format=str(file_format),
                parser=parser,
                batch_id=str(batch_id),
                source_id=str(source_id),
                source_product=source_product,
                dead_letters=dead_letters,
            )
        if request.file_format in {"jsonl", "log"}:
            yield from self._iter_jsonl_records(request)
            return
        if request.file_format == "csv":
            yield from self._iter_csv_records(request)
            return
        raise ParserError(f"unsupported ingest file format: {request.file_format}")

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _iter_jsonl_records 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 iter jsonl records 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def _iter_jsonl_records(self, request: _RecordIteratorRequest):
        from .pipeline_stages import RecordIterator, RecordIteratorOptions

        yield from RecordIterator(
            request.source_path,
            options=RecordIteratorOptions(
                file_format="jsonl",
                parser=request.parser,
                batch_id=request.batch_id,
                source_id=request.source_id,
                source_product=request.source_product,
                dead_letters=request.dead_letters,
            ),
        ).iter_records()

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _iter_csv_records 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 iter csv records 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def _iter_csv_records(self, request: _RecordIteratorRequest):
        from .pipeline_stages import RecordIterator, RecordIteratorOptions

        yield from RecordIterator(
            request.source_path,
            options=RecordIteratorOptions(
                file_format="csv",
                parser=request.parser,
                batch_id=request.batch_id,
                source_id=request.source_id,
                source_product=request.source_product,
                dead_letters=request.dead_letters,
            ),
        ).iter_records()

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _write_events 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 write events 相关记录，集中处理目标路径、格式化和状态更新。
    def _write_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Write events to store or fallback sink (delegated to pipeline_enrich)."""
        from .pipeline_enrich import write_events as _write_events

        return _write_events(self, events)

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _flush_events 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 flush events 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def _flush_events(
        self, events: list[dict[str, Any]], *, batch_id: str
    ) -> tuple[dict[str, Any], list[str]]:
        """Flush events to storage and dedup store (delegated to pipeline_enrich)."""
        from .pipeline_enrich import flush_events as _flush_events

        return _flush_events(self, events, batch_id=batch_id)


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 ingest_file 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 ingest file 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def ingest_file(
    path: str | Path,
    *,
    params: IngestFileOptions | None = None,
    root: str | Path | None = None,
    pipeline_options: IngestPipelineOptions | None = None,
    file_options: IngestFileOptions | None = None,
    store: Any | None = None,
    payload_max_chars: int = DEFAULT_PAYLOAD_MAX_CHARS,
    source_id: str | None = None,
    source_product: str | None = None,
    parser_id: str = "security_alert_v1",
    file_format: str | None = None,
) -> IngestResult:
    if pipeline_options is None and (store is not None or payload_max_chars != DEFAULT_PAYLOAD_MAX_CHARS):
        pipeline_options = IngestPipelineOptions(
            store=store,
            payload_max_chars=int(payload_max_chars),
        )
    file_options = params or file_options
    if file_options is None and any(value is not None for value in (source_id, source_product, file_format)):
        file_options = IngestFileOptions(
            source_id=source_id,
            source_product=source_product,
            parser_id=str(parser_id),
            file_format=file_format,
        )
    pipeline = IngestPipeline(
        root or default_log_analysis_root(),
        options=pipeline_options,
    )
    return pipeline.ingest_file(path, options=file_options)


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 default_log_analysis_root 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 default log analysis root 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def default_log_analysis_root() -> Path:
    return Path.cwd() / "agent_py_agent" / "data" / "log_analysis"


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 normalize_file_format 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalize file format 涉及的字段，让后续匹配和存储使用同一形态。
def normalize_file_format(value: str) -> str:
    fmt = value.lower().lstrip(".") or "jsonl"
    if fmt == "json":
        return "jsonl"
    if fmt in {"jsonl", "csv", "log"}:
        return fmt
    raise ParserError(f"unsupported file format: {value}")


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 file_digest 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 file digest 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, f"sha256:{digest.hexdigest()}"


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 make_batch_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 make batch id 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def make_batch_id(*, source_id: str, source_path: str, content_hash: str) -> str:
    digest = sha256_json(
        {
            "source_id": source_id,
            "source_path": source_path,
            "content_hash": content_hash,
        }
    )
    return f"batch-{digest[:24]}"


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _storage_result 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 storage result 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def _storage_result(result: Any, *, count: int, store: Any | None = None) -> dict[str, Any]:
    store_path = getattr(store, "events_path", None)
    if isinstance(result, Mapping):
        payload = dict(result)
        payload.setdefault("count", count)
        if payload.get("path") is None and store_path is not None:
            payload["path"] = str(store_path).replace("\\", "/")
        return payload
    if isinstance(result, (str, Path)):
        return {"count": count, "path": str(result).replace("\\", "/")}
    if isinstance(result, int):
        return {"count": result, "path": str(store_path).replace("\\", "/") if store_path is not None else None}
    return {"count": count, "path": str(store_path).replace("\\", "/") if store_path is not None else None}


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _storage_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 storage summary 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def _storage_summary(infos: list[dict[str, Any]], *, fallback_path: Path) -> dict[str, Any]:
    count = sum(int(info.get("count") or 0) for info in infos)
    paths = sorted({str(info.get("path")).replace("\\", "/") for info in infos if info.get("path")})
    if not paths:
        paths = [str(fallback_path).replace("\\", "/")]
    summary: dict[str, Any] = {"count": count, "path": paths[0]}
    if len(paths) > 1:
        summary["paths"] = paths
    return summary


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _jsonable_mapping 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 jsonable mapping 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def _jsonable_mapping(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in mapping.items()}
