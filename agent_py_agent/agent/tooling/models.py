
from __future__ import annotations

"""Defines stable tool metadata, retrieval hits, and base execution contracts."""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..contracts.error_taxonomy import error_contract
from ..retrieval.embedding import EmbeddingProvider, cosine


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
    # 可选精确 JSON Schema 片段：{参数名: {"type": "array", "items": {...}, "enum": [...]}}。
    # 声明了的参数走精确类型（消除 native tool_use 弱推导致的 TOOL_INVALID_ARGUMENTS），
    # 未声明的回退到全 string 弱推导；required_parameters 列出必填参数名。
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    required_parameters: list[str] = field(default_factory=list)
    internal_parameters: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    effect: str = ""
    default_mode: str = ""
    requires_idempotency: bool = False
    requires_approval: bool = False
    # 结构化任务晋升标志：只有真正开始产物/命令工作时才把普通聊天提升为 TaskRun。
    promotes_task: bool = False
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
    # 原始工具/提供方报码必须保留；error_code 仍是控制流使用的归一化分类。
    reported_error_code: str = ""
    error_category: str = ""
    retryable: bool = False
    recommended_action: str = ""
    recovery_hint: str = ""

    def __post_init__(self) -> None:
        if self.ok:
            self.error_code = ""
            self.reported_error_code = ""
            self.error_category = ""
            self.retryable = False
            self.recommended_action = ""
            self.recovery_hint = ""
            return
        code = self.error_code or self.reported_error_code or _error_code_from_output(self.output)
        self.reported_error_code = str(code or "UNKNOWN_ERROR").strip().upper()
        contract = error_contract(code) if code else error_contract("UNKNOWN_ERROR")
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


def _error_code_from_output(output: str) -> str:
    """工具失败时若没显式传 error_code，从其 JSON output 里兜底提取一个。

    许多工具把含 error_code 的错误 payload json.dumps 进 output 字符串，却忘了
    同时传 error_code= 给构造函数，导致 __post_init__ 兜底成 UNKNOWN_ERROR
    （retryable=False）误导模型放弃。这里通用地把它捞回来——error_contract 对
    返回值做大小写归一化，所以 reader 的小写语义码（如 artifact_not_registered）
    也能命中其已注册的大写契约。提取不到合法 dict.error_code 时返回 ""，保持原有
    UNKNOWN_ERROR 兜底，未注册的码也会被 error_contract 自然回落到 UNKNOWN_ERROR。
    """
    if not output:
        return ""
    try:
        payload = json.loads(output)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if isinstance(payload, dict):
        return str(payload.get("error_code") or "").strip()
    return ""


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

    def __init__(
        self,
        enabled: bool = False,
        embedder: EmbeddingProvider | None = None,
        *,
        min_score: float = 0.15,
    ):
        self.enabled = enabled
        self.embedder = embedder
        self.min_score = float(min_score)
        self._document_key: tuple[str, ...] = ()
        self._document_vectors: list[list[float]] = []
        self.last_error = ""

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        if not self.enabled or self.embedder is None or not query.strip() or not specs:
            return []
        documents = tuple(_tool_semantic_document(spec) for spec in specs)
        try:
            hits = self._semantic_search(query, specs, documents)
        except Exception as exc:
            # Semantic retrieval is additive: an endpoint outage falls back to
            # keyword results, but the status remains machine-visible.
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        self.last_error = ""
        return hits[:limit]

    def _semantic_search(
        self,
        query: str,
        specs: list[ToolSpec],
        documents: tuple[str, ...],
    ) -> list[ToolSearchHit]:
        self._ensure_document_vectors(documents)
        query_vectors = self.embedder.embed([query])
        if len(query_vectors) != 1:
            raise ValueError("tool query embedding count mismatch")
        return _semantic_tool_hits(
            specs,
            query_vectors[0],
            self._document_vectors,
            min_score=self.min_score,
        )

    def _ensure_document_vectors(self, documents: tuple[str, ...]) -> None:
        if documents == self._document_key:
            return
        vectors = self.embedder.embed(list(documents))
        if len(vectors) != len(documents):
            raise ValueError(
                f"tool document embedding count mismatch expected={len(documents)} got={len(vectors)}"
            )
        self._document_key = documents
        self._document_vectors = vectors

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "configured": self.embedder is not None,
            "ready": bool(self.enabled and self.embedder is not None and not self.last_error),
            "provider": type(self.embedder).__name__ if self.embedder is not None else "",
            "last_error": self.last_error,
        }


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

    def status(self) -> dict[str, Any]:
        providers: dict[str, Any] = {}
        for provider in self.providers:
            status = getattr(provider, "status", None)
            providers[provider.name] = status() if callable(status) else {"enabled": True}
        return {"mode": "hybrid", "providers": providers}


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
    # [^\W\u4e00-\u9fff]+ = \u4efb\u610f\u811a\u672c\u8bcd\u5b57\u7b26(\u9664 CJK)\u2192 \u975e\u4e2d\u82f1\u8bed\u8a00\u4e0d\u518d\u96f6 token(\u5ba1\u8ba1 #7);CJK \u4ecd\u5355\u5217\u8d70 subtoken
    tokens = re.findall(r"[^\W\u4e00-\u9fff]+|[\u4e00-\u9fff]+", lowered)
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


def _tool_semantic_document(spec: ToolSpec) -> str:
    return "\n".join(
        (
            f"name: {spec.name}",
            f"category: {spec.category}",
            f"description: {spec.description}",
            "use cases: " + " | ".join(spec.use_cases),
            "avoid when: " + " | ".join(spec.avoid_when),
            "keywords: " + " | ".join(spec.keywords),
        )
    )


def _semantic_tool_hits(
    specs: list[ToolSpec],
    query_vector: list[float],
    document_vectors: list[list[float]],
    *,
    min_score: float,
) -> list[ToolSearchHit]:
    hits: list[ToolSearchHit] = []
    for spec, vector in zip(specs, document_vectors, strict=True):
        score = cosine(query_vector, vector)
        if score < min_score:
            continue
        hits.append(
            ToolSearchHit(
                name=spec.name,
                score=score * 8.0,
                reasons=[f"语义相似度 {score:.3f}"],
            )
        )
    return sorted(hits, key=lambda item: (-item.score, item.name))


def _score_keyword_token(token: str, haystacks: dict[str, str]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    checks = [
        ("name", 6.0, f"命中工具名'{token}'"),
        ("keywords", 4.0, f"命中关键词'{token}'"),
        ("category", 2.5, f"命中类别'{token}'"),
    ]
    for key, weight, reason in checks:
        if _keyword_token_matches(token, haystacks[key]):
            score += weight
            reasons.append(reason)
    if _keyword_token_matches(token, haystacks["description"]) or _keyword_token_matches(
        token, haystacks["use_cases"]
    ):
        score += 1.5
        reasons.append(f"命中用途描述'{token}'")
    return score, reasons


def _keyword_token_matches(token: str, haystack: str) -> bool:
    """Do not let short ASCII extensions match arbitrary name substrings."""
    if re.fullmatch(r"[a-z0-9]{1,2}", token):
        return token in re.findall(r"[a-z0-9]+", haystack)
    return token in haystack


def _chinese_subtokens(token: str) -> list[str]:
    if not re.fullmatch(r"[\u4e00-\u9fff]+", token):
        return []
    return [
        token[idx : idx + size]
        for size in range(2, min(4, len(token)) + 1)
        for idx in range(0, len(token) - size + 1)
    ]
