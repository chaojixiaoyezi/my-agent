"""Pure, source-labelled runtime snapshots; no prompt or business I/O.

W5 owns production wiring. A withdrawn snapshot means only that the host has no
current snapshot for this source, never that its business objects are absent.
Producers use the audited sources below, not per-child/field/hash source names.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

RUNTIME_INJECTION_SOURCE = "prompt.runtime_injection"
RUNTIME_DIRECT_CHILDREN_SOURCE = "prompt.runtime_injection.direct_children"
RUNTIME_DELIVERY_CONTRACT_SOURCE = "prompt.runtime_injection.delivery_contract"
RUNTIME_PUBLIC_RECEIPT_SOURCE = "prompt.runtime_injection.public_receipt"
RUNTIME_GOAL_SOURCE = "prompt.runtime_injection.goal"


def is_runtime_injection_source(source: object) -> bool:
    """Recognize this exact source family without coercion or user callbacks."""
    return isinstance(source, str) and (
        str.__eq__(source, RUNTIME_INJECTION_SOURCE)
        or (str.startswith(source, RUNTIME_INJECTION_SOURCE + ".")
            and str.__len__(source) > len(RUNTIME_INJECTION_SOURCE) + 1)
    )


def _validate_fragment(text: str | None, source: str) -> None:
    if text is not None and not isinstance(text, str):
        raise TypeError("runtime injection text must be str or None")
    if not isinstance(source, str):
        raise TypeError("runtime injection source must be str")
    if (not source or source != source.strip() or source == RUNTIME_INJECTION_SOURCE
            or not is_runtime_injection_source(source)):
        raise ValueError("typed runtime injection source must be a strict child without edge whitespace")


@dataclass(frozen=True)
class RuntimeInjectionFragment:
    """Immutable source snapshot. ``None`` explicitly withdraws this source."""

    text: str | None
    source: str

    def __post_init__(self) -> None:
        _validate_fragment(self.text, self.source)


InjectionValue = str | RuntimeInjectionFragment


def freeze_runtime_injections(values: Iterable[InjectionValue] | None) -> tuple[InjectionValue, ...]:
    """Copy an input iterable, preserving raw bytes/order and unique sources."""
    if values is None:
        return ()
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError("runtime injections must be an iterable of entries, not a bare string or bytes")
    frozen: list[InjectionValue] = []
    sources: set[str] = set()
    for value in values:
        if isinstance(value, RuntimeInjectionFragment):
            _validate_fragment(value.text, value.source)
            if value.source in sources:
                raise ValueError("duplicate typed runtime injection source")
            sources.add(value.source)
        elif not isinstance(value, str):
            raise TypeError("runtime injection entry must be str or RuntimeInjectionFragment")
        frozen.append(value)
    return tuple(frozen)


def runtime_injection_texts(values: Iterable[InjectionValue] | None) -> tuple[str, ...]:
    """Legacy text view; omit only explicit typed withdrawals, never blanks."""
    return tuple(
        value if isinstance(value, str) else value.text
        for value in freeze_runtime_injections(values)
        if isinstance(value, str) or value.text is not None
    )


def replace_runtime_injection_text(value: InjectionValue, text: str) -> InjectionValue:
    """Replace content without changing its source; cannot withdraw a source."""
    if not isinstance(text, str):
        raise TypeError("replacement runtime injection text must be str")
    if isinstance(value, RuntimeInjectionFragment):
        _validate_fragment(value.text, value.source)
        return RuntimeInjectionFragment(text=text, source=value.source)
    if not isinstance(value, str):
        raise TypeError("runtime injection entry must be str or RuntimeInjectionFragment")
    return text


def runtime_injection_sections(values: Iterable[InjectionValue] | None) -> tuple[tuple[str, str], ...]:
    """One legacy base first, then typed sources verbatim in producer order.

    An empty typed section is a structured tombstone and must not be dropped by
    consumers. Headers inside raw text are opaque, never source declarations.
    """
    frozen = freeze_runtime_injections(values)
    raw_text = "\n".join(value for value in frozen if isinstance(value, str))
    sections = [(RUNTIME_INJECTION_SOURCE, "# Runtime Injection\n" + (raw_text or "（无）"))]
    sections.extend(
        (value.source, "" if value.text is None else "# Runtime Injection: " + value.source + "\n" + value.text)
        for value in frozen if isinstance(value, RuntimeInjectionFragment)
    )
    return tuple(sections)


def runtime_injection_clear_text(source: str) -> str:
    """Plain-text fallback for a structured withdrawal, not a domain fact."""
    if not isinstance(source, str):
        raise TypeError("runtime injection source must be str")
    if not is_runtime_injection_source(source):
        raise ValueError("source is outside the runtime injection family")
    return f"[runtime-injection-cleared source={source}] 当前无有效快照；该来源更早的快照不代表当前状态。"
