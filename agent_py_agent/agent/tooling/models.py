
from __future__ import annotations

"""Defines stable tool metadata, retrieval hits, and base execution contracts."""

import re
from dataclasses import dataclass, field
from typing import Any

from ..contracts.error_taxonomy import classify_error, error_contract


@dataclass
class ToolSpec:

    name: str
    category: str
    description: str
    use_cases: list[str]
    avoid_when: list[str]
    keywords: list[str]
    parameters: dict[str, str]
    parameter_details: dict[str, str] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)
    effect: str = ""
    default_mode: str = ""
    requires_idempotency: bool = False
    requires_approval: bool = False
    timeout_seconds: int = 0
    output_refs: list[str] = field(default_factory=list)

    def render_catalog_entry(
        self,
        *,
        include_examples: bool = True,
        max_chars: int = 0,
    ) -> str:

        params = "、".join(self.parameters.keys()) or "无"
        example = f"\n  示例：{self.examples[0]}" if include_examples and self.examples else ""
        traits = _tool_traits(self)
        trait_text = f"；{traits}" if traits else ""
        rendered = (
            f"- {self.name} [{self.category}{trait_text}]：{self.description}\n"
            f"  关键参数：{params}"
            f"{example}"
        )
        return _truncate_rendered_tool_entry(
            rendered,
            max_chars=max_chars,
            label="tool_catalog_entry_max_chars",
        )

    def render_recommended_entry(self, *, max_chars: int = 0) -> str:

        params = "\n".join(
            f"  - {name}: {self.parameters.get(name, '')}" for name in self.parameters
        ) or "  - 无"
        traits = _tool_traits(self)
        trait_line = f"\n  属性：{traits}" if traits else ""
        rendered = (
            f"- {self.name} [{self.category}]：{self.description}"
            f"{trait_line}\n"
            f"  参数：\n{params}"
        )
        return _truncate_rendered_tool_entry(
            rendered,
            max_chars=max_chars,
            label="tool_detail_max_chars",
        )

    def render_detail_entry(self, *, max_chars: int = 0) -> str:

        params = "\n".join(
            f"  - {name}: {self.parameter_details.get(name, desc)}"
            for name, desc in self.parameters.items()
        ) or "  - 无"
        examples = "\n".join(f"  - {item}" for item in self.examples) or "  - 无"
        use_cases = "\n".join(f"  - {item}" for item in self.use_cases) or "  - 无"
        avoid_when = "\n".join(f"  - {item}" for item in self.avoid_when) or "  - 无"
        rendered = (
            f"## {self.name}\n"
            f"类别：{self.category}\n"
            f"一句话说明：{self.description}\n"
            f"适合在这些时候用：\n{use_cases}\n"
            f"关键 \n{params}\n"
            f"示例：\n{examples}\n"
            f"这些场景别优先选它：\n{avoid_when}"
        )
        return _truncate_rendered_tool_entry(
            rendered,
            max_chars=max_chars,
            label="tool_detail_max_chars",
        )


@dataclass
class ToolExecutionResult:

    tool: str
    ok: bool
    output: str
    call_id: str = ""
    result_envelope: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_category: str = ""
    retryable: bool = False
    recommended_action: str = ""
    recovery_hint: str = ""

    def __post_init__(self) -> None:
        if self.ok:
            self.error_code = ""
            self.error_category = ""
            self.retryable = False
            self.recommended_action = ""
            self.recovery_hint = ""
            return
        contract = error_contract(self.error_code) if self.error_code else classify_error(self.output)
        self.error_code = contract.code
        self.error_category = contract.category
        self.retryable = contract.retryable
        self.recommended_action = contract.recommended_action
        self.recovery_hint = contract.recovery_hint

    def render_for_prompt(self) -> str:

        status = "ok" if self.ok else "error"
        fields = f"tool={self.tool}; status={status}"
        if not self.ok and self.error_code:
            fields = f"{fields}; error_code={self.error_code}; recommended_action={self.recommended_action}"
        return f"[{fields}]\n{self.output}"


@dataclass
class ToolSearchHit:

    name: str
    score: float
    reasons: list[str]


class BaseToolSearchProvider:

    name = "base"

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        raise NotImplementedError


class KeywordToolSearchProvider(BaseToolSearchProvider):

    name = "keyword"

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        tokens = _tokenize(query)
        hits: list[ToolSearchHit] = []
        for spec in specs:
            score, reasons = _score_keyword_spec(spec, tokens)
            if score > 0:
                hits.append(ToolSearchHit(name=spec.name, score=score, reasons=reasons[:3]))
        hits.sort(key=lambda item: (-item.score, item.name))
        return hits[:limit]


class VectorToolSearchProvider(BaseToolSearchProvider):

    name = "vector"

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        if not self.enabled:
            return []
        return []


class HybridToolRetriever:

    def __init__(self, providers: list[BaseToolSearchProvider]):
        self.providers = providers

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        merged: dict[str, ToolSearchHit] = {}
        for provider in self.providers:
            for hit in provider.search(query, specs, limit):
                _merge_tool_hit(merged, provider.name, hit)
        ranked = sorted(merged.values(), key=lambda item: (-item.score, item.name))
        return ranked[:limit]


class BaseTool:

    spec: ToolSpec

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raise NotImplementedError


def _tool_traits(spec: ToolSpec) -> str:
    parts: list[str] = []
    if spec.effect:
        parts.append(f"effect={spec.effect}")
    if spec.default_mode:
        parts.append(f"default={spec.default_mode}")
    if spec.requires_idempotency:
        parts.append("idempotent")
    if spec.requires_approval:
        parts.append("approval")
    if spec.timeout_seconds:
        parts.append(f"timeout={spec.timeout_seconds}s")
    return "；".join(parts)


def _truncate_rendered_tool_entry(text: str, *, max_chars: int, label: str) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + f"\n  ... 已按 {label} 截断"



def _tokenize(text: str) -> list[str]:

    lowered = (text or "").lower()
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", lowered)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        expanded.extend(_chinese_subtokens(token))
    seen: set[str] = set()
    unique: list[str] = []
    for token in expanded:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def _score_keyword_spec(spec: ToolSpec, tokens: list[str]) -> tuple[float, list[str]]:
    haystacks = _keyword_haystacks(spec)
    score = 0.0
    reasons: list[str] = []
    for token in tokens:
        token_score, token_reasons = _score_keyword_token(token, haystacks)
        score += token_score
        reasons.extend(token_reasons[:1])
    return score, reasons


def _merge_tool_hit(
    merged: dict[str, ToolSearchHit],
    provider_name: str,
    hit: ToolSearchHit,
) -> None:
    existing = merged.get(hit.name)
    provider_reason = f"{provider_name} 召回"
    if existing is None:
        merged[hit.name] = ToolSearchHit(
            name=hit.name,
            score=hit.score,
            reasons=[provider_reason, *hit.reasons][:4],
        )
        return
    existing.score += hit.score
    _append_unique_reasons(existing.reasons, [provider_reason, *hit.reasons])


def _append_unique_reasons(target: list[str], reasons: list[str]) -> None:
    for reason in reasons:
        if reason not in target:
            target.append(reason)
    del target[4:]


def _keyword_haystacks(spec: ToolSpec) -> dict[str, str]:
    return {
        "name": spec.name.lower(),
        "description": spec.description.lower(),
        "category": spec.category.lower(),
        "keywords": " ".join(spec.keywords).lower(),
        "use_cases": " ".join(spec.use_cases).lower(),
    }


def _score_keyword_token(token: str, haystacks: dict[str, str]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    checks = [
        ("name", 6.0, f"命中工具名'{token}'"),
        ("keywords", 4.0, f"命中关键词'{token}'"),
        ("category", 2.5, f"命中类别'{token}'"),
    ]
    for key, weight, reason in checks:
        if token in haystacks[key]:
            score += weight
            reasons.append(reason)
    if token in haystacks["description"] or token in haystacks["use_cases"]:
        score += 1.5
        reasons.append(f"命中用途描述'{token}'")
    return score, reasons


def _chinese_subtokens(token: str) -> list[str]:
    if not re.fullmatch(r"[\u4e00-\u9fff]+", token):
        return []
    return [
        token[idx : idx + size]
        for size in range(2, min(4, len(token)) + 1)
        for idx in range(0, len(token) - size + 1)
    ]
