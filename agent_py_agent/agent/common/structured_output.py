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
from dataclasses import dataclass
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
    max_split_depth: int = 2


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

    Provider/network exceptions are allowed to propagate so the caller's established
    retry policy remains authoritative. Only a successful response whose parsed keys
    are incomplete is split. Resolved rows are retained and never sent again.
    """

    if request.max_batch_items < 1:
        raise ValueError("max_batch_items must be >= 1")
    if request.max_split_depth < 0:
        raise ValueError("max_split_depth must be >= 0")
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
        for batch in _chunks(rows, self.request.max_batch_items):
            self._resolve(batch, 0)
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
        response = self.request.generate(batch)
        self.calls += 1
        parsed = self.request.parse(str(getattr(response, "text", "") or ""), expected)
        accepted = {key: value for key, value in parsed.items() if key in expected}
        self.values.update(accepted)
        missing = [row for row in batch if self.request.key_of(row) not in accepted]
        if not missing:
            return
        if depth >= self.request.max_split_depth:
            self.unresolved.extend(self.request.key_of(row) for row in missing)
            return
        self.split_retries += 1
        if len(missing) == 1:
            self._resolve(missing, depth + 1)
            return
        midpoint = max(1, len(missing) // 2)
        self._resolve(missing[:midpoint], depth + 1)
        self._resolve(missing[midpoint:], depth + 1)


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


def _chunks(rows: Sequence[_Row], size: int) -> Iterable[list[_Row]]:
    for start in range(0, len(rows), size):
        yield list(rows[start : start + size])


__all__ = [
    "StructuredBatchReport",
    "StructuredBatchRequest",
    "collect_structured_batches",
    "json_objects_from_text",
]
