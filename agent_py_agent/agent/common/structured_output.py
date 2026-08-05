"""Provider-independent helpers for reliable structured model output.

This module deliberately knows nothing about audit verdicts or any other domain.
It supplies two protocol-level guarantees shared by structured LLM callers:

* find complete JSON objects without assuming the response contains only JSON;
* bound each batch and recursively retry only unresolved rows when one response is
  incomplete, malformed, or truncated by the provider output limit.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Generic, TypeVar

_Row = TypeVar("_Row")
_Value = TypeVar("_Value")


@dataclass(frozen=True)
class StructuredBatchReport:
    values: dict[str, Any]
    unresolved_keys: tuple[str, ...]
    calls: int
    split_retries: int


@dataclass(frozen=True)
class StructuredBatchRequest(Generic[_Row, _Value]):
    rows: Sequence[_Row]
    key_of: Callable[[_Row], str]
    generate: Callable[[list[_Row]], object]
    parse: Callable[[str, set[str]], dict[str, _Value]]
    max_batch_items: int = 12
    max_batch_weight: int = 0
    weight_of: Callable[[_Row], int] | None = None
    max_split_depth: int = 2
    max_workers: int = 1
    should_split_exception: Callable[[Exception], bool] | None = None


def json_objects_from_text(text: object) -> list[dict[str, Any]]:
    """Return complete JSON objects embedded anywhere in a model response."""

    source = str(text or "")
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    index = 0
    while index < len(source):
        start = source.find("{", index)
        if start < 0:
            break
        try:
            payload, consumed = decoder.raw_decode(source[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(payload, dict):
            objects.append({str(key): value for key, value in payload.items()})
        index = start + max(consumed, 1)
    return objects


def collect_structured_batches(request: StructuredBatchRequest[_Row, _Value]) -> StructuredBatchReport:
    """Generate structured values without allowing one truncation to erase a batch.

    Provider/network exceptions propagate so the caller's retry policy remains
    authoritative, unless the caller explicitly identifies one typed error as a
    batch-size failure. Such a batch is split exactly like an incomplete response;
    unrelated failures still propagate. Resolved rows are retained and never sent
    again.
    """

    if request.max_batch_items < 1:
        raise ValueError("max_batch_items must be >= 1")
    if request.max_batch_weight < 0:
        raise ValueError("max_batch_weight must be >= 0")
    if request.max_split_depth < 0:
        raise ValueError("max_split_depth must be >= 0")
    if request.max_workers < 1:
        raise ValueError("max_workers must be >= 1")
    return _StructuredBatchCollector(request).run()


class _StructuredBatchCollector(Generic[_Row, _Value]):
    def __init__(self, request: StructuredBatchRequest[_Row, _Value]) -> None:
        self.request = request
        self.values: dict[str, _Value] = {}
        self.unresolved: list[str] = []
        self.calls = 0
        self.split_retries = 0

    def run(self) -> StructuredBatchReport:
        rows = _unique_rows(self.request.rows, self.request.key_of)
        batches = list(_bounded_chunks(
            rows,
            max_items=self.request.max_batch_items,
            max_weight=self.request.max_batch_weight,
            weight_of=self.request.weight_of,
        ))
        if self.request.max_workers > 1 and len(batches) > 1:
            return self._run_parallel(batches)
        for batch in batches:
            self._resolve(batch, 0)
        return self._report()

    def _run_parallel(self, batches: list[list[_Row]]) -> StructuredBatchReport:
        """Resolve independent top-level chunks concurrently, then merge in input order."""
        workers = min(self.request.max_workers, len(batches))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="structured-batch",
        ) as executor:
            reports = list(executor.map(self._resolve_isolated, batches))
        for report in reports:
            self.values.update(report.values)
            self.unresolved.extend(report.unresolved_keys)
            self.calls += report.calls
            self.split_retries += report.split_retries
        return self._report()

    def _resolve_isolated(self, batch: list[_Row]) -> StructuredBatchReport:
        request = replace(self.request, rows=batch, max_workers=1)
        return _StructuredBatchCollector(request).run()

    def _report(self) -> StructuredBatchReport:
        return StructuredBatchReport(
            values={str(key): value for key, value in self.values.items()},
            unresolved_keys=tuple(dict.fromkeys(self.unresolved)),
            calls=self.calls,
            split_retries=self.split_retries,
        )

    def _resolve(self, batch: list[_Row], depth: int) -> None:
        if not batch:
            return
        expected = {self.request.key_of(row) for row in batch}
        try:
            response = self.request.generate(batch)
        except Exception as exc:
            self.calls += 1
            if not self._should_split_exception(exc):
                raise
            self._split_or_mark_unresolved(batch, depth)
            return
        self.calls += 1
        parsed = self.request.parse(str(getattr(response, "text", "") or ""), expected)
        accepted = {key: value for key, value in parsed.items() if key in expected}
        self.values.update(accepted)
        missing = [row for row in batch if self.request.key_of(row) not in accepted]
        if not missing:
            return
        self._split_or_mark_unresolved(missing, depth)

    def _should_split_exception(self, exc: Exception) -> bool:
        predicate = self.request.should_split_exception
        return bool(predicate is not None and predicate(exc))

    def _split_or_mark_unresolved(self, rows: list[_Row], depth: int) -> None:
        """Split one failed subset, or leave exact keys unresolved at the safe floor."""
        if not rows:
            return
        if depth >= self.request.max_split_depth:
            self.unresolved.extend(self.request.key_of(row) for row in rows)
            return
        self.split_retries += 1
        if len(rows) == 1:
            self._resolve(rows, depth + 1)
            return
        midpoint = max(1, len(rows) // 2)
        self._resolve(rows[:midpoint], depth + 1)
        self._resolve(rows[midpoint:], depth + 1)


def _unique_rows(rows: Iterable[_Row], key_of: Callable[[_Row], str]) -> list[_Row]:
    unique: list[_Row] = []
    seen: set[str] = set()
    for row in rows:
        key = str(key_of(row) or "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _bounded_chunks(
    rows: Sequence[_Row],
    *,
    max_items: int,
    max_weight: int,
    weight_of: Callable[[_Row], int] | None,
) -> Iterable[list[_Row]]:
    if max_weight <= 0 or weight_of is None:
        for start in range(0, len(rows), max_items):
            yield list(rows[start : start + max_items])
        return
    batch: list[_Row] = []
    weight = 0
    for row in rows:
        row_weight = max(1, int(weight_of(row)))
        if batch and (
            len(batch) >= max_items
            or weight + row_weight > max_weight
        ):
            yield batch
            batch = []
            weight = 0
        # A single overweight row is still emitted alone. Domain callers decide how to
        # mark or bound an item that is larger than the provider's whole safe window.
        batch.append(row)
        weight += row_weight
    if batch:
        yield batch


__all__ = [
    "StructuredBatchReport",
    "StructuredBatchRequest",
    "collect_structured_batches",
    "json_objects_from_text",
]
