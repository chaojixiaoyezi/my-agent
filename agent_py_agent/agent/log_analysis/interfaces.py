from __future__ import annotations

"""Public protocols for pluggable log analysis implementations."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .models import (
    CaseRecord,
    Checkpoint,
    EvidenceRef,
    Finding,
    NormalizedEvent,
    RawBatch,
    SourceSpec,
)


@dataclass
class ParseResult:
    batch_id: str
    parser_id: str
    events: list[NormalizedEvent] = field(default_factory=list)
    malformed_count: int = 0
    dead_letter_refs: list[str] = field(default_factory=list)
    parser_confidence: float = 0.0
    schema_summary: dict[str, Any] = field(default_factory=dict)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QueryResult:
    query_id: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    evidence_ref: EvidenceRef | None = None
    elapsed_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MLScoreResult:
    scoring_engine: str
    model_id: str
    model_version: str = ""
    feature_schema_version: str = ""
    risk_score: float = 0.0
    labels: list[str] = field(default_factory=list)
    explanations: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    error: str = ""


@dataclass
class DispatchResult:
    case_id: str
    status: str
    run_id: str = ""
    queued_at: str = ""
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResponseActionResult:
    action_id: str
    connector_id: str
    status: str
    dry_run: bool = True
    message: str = ""
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LogSource(Protocol):
    @property
    def source_spec(self) -> SourceSpec:
        ...

    def read_batch(self, checkpoint: Checkpoint | None = None) -> RawBatch | None:
        ...

    def commit(self, checkpoint: Checkpoint) -> None:
        ...

    def health(self) -> dict[str, Any]:
        ...


@runtime_checkable
class Parser(Protocol):
    @property
    def parser_id(self) -> str:
        ...

    def can_parse(self, sample: str | bytes | Sequence[str], source_spec: SourceSpec) -> float:
        ...

    def parse_batch(self, raw_batch: RawBatch) -> ParseResult:
        ...


@runtime_checkable
class EventStore(Protocol):
    def write_events(self, events: Sequence[NormalizedEvent], *, batch: RawBatch | None = None) -> int:
        ...

    def get_event(self, event_id: str) -> NormalizedEvent | None:
        ...

    def health(self) -> dict[str, Any]:
        ...


@runtime_checkable
class QueryEngine(Protocol):
    def query(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        limit: int = 100,
    ) -> QueryResult:
        ...

    def topn(
        self,
        field_name: str,
        *,
        window: tuple[str, str] | None = None,
        limit: int = 20,
    ) -> QueryResult:
        ...

    def sample(
        self,
        filters: Mapping[str, Any],
        *,
        limit: int = 100,
    ) -> QueryResult:
        ...


@runtime_checkable
class Detector(Protocol):
    @property
    def detector_id(self) -> str:
        ...

    def detect(
        self,
        query_engine: QueryEngine,
        *,
        window: tuple[str, str],
        context: Mapping[str, Any] | None = None,
    ) -> Sequence[Finding]:
        ...


@runtime_checkable
class ScoringEngine(Protocol):
    def score(
        self,
        features: Mapping[str, Any],
        *,
        model_ref: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> MLScoreResult:
        ...


@runtime_checkable
class DispatchEngine(Protocol):
    def enqueue_case(
        self,
        case: CaseRecord,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> DispatchResult:
        ...

    def health(self) -> dict[str, Any]:
        ...


@runtime_checkable
class ResponseConnector(Protocol):
    @property
    def connector_id(self) -> str:
        ...

    def plan(
        self,
        case: CaseRecord,
        action: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def execute(
        self,
        case: CaseRecord,
        action: Mapping[str, Any],
        *,
        dry_run: bool = True,
    ) -> ResponseActionResult:
        ...


__all__ = [
    "Detector",
    "DispatchEngine",
    "DispatchResult",
    "EventStore",
    "LogSource",
    "MLScoreResult",
    "ParseResult",
    "Parser",
    "QueryEngine",
    "QueryResult",
    "ResponseActionResult",
    "ResponseConnector",
    "ScoringEngine",
]
