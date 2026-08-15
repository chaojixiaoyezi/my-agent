
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SampleNeutralityRule:
    """Structured rule for scanning one runtime-facing sample surface."""

    path: str
    disallowed_markers: tuple[str, ...]
    surface: str
    severity: str = "hard"


@dataclass(frozen=True)
class SampleNeutralityFinding:
    """Finding emitted when a sample surface contains a caller-provided marker."""

    path: str
    marker: str
    surface: str
    severity: str


def find_sample_neutrality_violations(
    root: str | Path,
    rules: list[SampleNeutralityRule] | tuple[SampleNeutralityRule, ...],
) -> list[SampleNeutralityFinding]:
    """Scan configured files for caller-provided sample pollution markers."""

    base = Path(root)
    findings: list[SampleNeutralityFinding] = []
    for rule in rules:
        findings.extend(_find_rule_violations(base, rule))
    return findings


def _find_rule_violations(base: Path, rule: SampleNeutralityRule) -> list[SampleNeutralityFinding]:
    target = base / rule.path
    if not target.is_file():
        return []
    text = target.read_text(encoding="utf-8")
    return [
        SampleNeutralityFinding(
            path=rule.path,
            marker=marker,
            surface=rule.surface,
            severity=rule.severity,
        )
        for marker in rule.disallowed_markers
        if marker and marker in text
    ]


__all__ = [
    "SampleNeutralityFinding",
    "SampleNeutralityRule",
    "find_sample_neutrality_violations",
]
