# LLM: Sample neutrality checks keep runtime-facing examples task-neutral.
# 模块用途: 提供结构化样板污染扫描器，调用方传入文件和 marker，模块只产出机器可读 finding。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# LLM: SampleNeutralityRule describes one file/surface scan without embedding domain vocabulary.
# 类用途: 保存待扫描文件、模型可见面或夹具面，以及由调用方配置的禁止 marker。
@dataclass(frozen=True)
class SampleNeutralityRule:
    """Structured rule for scanning one runtime-facing sample surface."""

    path: str
    disallowed_markers: tuple[str, ...]
    surface: str
    severity: str = "hard"


# LLM: SampleNeutralityFinding is the structured result consumed by tests and future doctor commands.
# 类用途: 保存样板污染命中的文件、marker、面向位置和严重级别。
@dataclass(frozen=True)
class SampleNeutralityFinding:
    """Finding emitted when a sample surface contains a caller-provided marker."""

    path: str
    marker: str
    surface: str
    severity: str


# LLM: find_sample_neutrality_violations scans only configured paths and caller-provided markers.
# 函数用途: 返回样板污染 finding；不存在的文件和空 marker 不产生命中，避免代码内置专项词表。
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


# LLM: _find_rule_violations keeps each sample scan shallow and deterministic.
# 函数用途: 扫描单条样板污染规则；文件不存在时返回空 finding，命中时输出结构化记录。
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
