# LLM: prompt 渲染和检索排序依赖这些结构，字段和文本格式要谨慎调整。
# 模块用途: 工具规格、执行结果和工具检索评分模型。

from __future__ import annotations

"""defines stable tool metadata, retrieval hits, and base execution contracts.

给人看的解释：
这个文件只放工具系统最基础的'名词'和'接口'。
比如一个工具叫什么、适合干什么、执行后返回什么格式，以及工具检索结果长什么样。
后面无论是文件工具、网络工具还是编排工具，都应该沿用这里的结构。
"""

import re
from dataclasses import dataclass, field
from typing import Any


# LLM: ToolSpec 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 工具元数据模型，描述工具用途、参数、示例和检索关键词。
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

    # LLM: ToolSpec.render_catalog_entry 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 render_catalog_entry 转成人或模型可读的展示文本。
    def render_catalog_entry(self) -> str:

        params = "、".join(self.parameters.keys()) or "无"
        use_cases = "；".join(self.use_cases[:2]) or "无"
        avoid_when = "；".join(self.avoid_when[:1]) or "无"
        return (
            f"- {self.name} [{self.category}]：{self.description}\n"
            f"  适用场景：{use_cases}\n"
            f"  关键参数：{params}\n"
            f"  不适用时机：{avoid_when}"
        )

    # LLM: ToolSpec.render_detail_entry 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 render_detail_entry 转成人或模型可读的展示文本。
    def render_detail_entry(self) -> str:

        params = "\n".join(
            f"  - {name}: {self.parameter_details.get(name, desc)}"
            for name, desc in self.parameters.items()
        ) or "  - 无"
        examples = "\n".join(f"  - {item}" for item in self.examples) or "  - 无"
        use_cases = "\n".join(f"  - {item}" for item in self.use_cases) or "  - 无"
        avoid_when = "\n".join(f"  - {item}" for item in self.avoid_when) or "  - 无"
        return (
            f"## {self.name}\n"
            f"类别：{self.category}\n"
            f"一句话说明：{self.description}\n"
            f"适合在这些时候用：\n{use_cases}\n"
            f"关键参数说明：\n{params}\n"
            f"示例：\n{examples}\n"
            f"这些场景别优先选它：\n{avoid_when}"
        )


# LLM: ToolExecutionResult 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 工具执行结果模型，保存成功状态和返回给模型的文本。
@dataclass
class ToolExecutionResult:

    tool: str
    ok: bool
    output: str

    # LLM: ToolExecutionResult.render_for_prompt 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 render_for_prompt 转成人或模型可读的展示文本。
    def render_for_prompt(self) -> str:

        status = "ok" if self.ok else "error"
        return f"[tool={self.tool}; status={status}]\n{self.output}"


# LLM: ToolSearchHit 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 工具检索命中模型，保存分数和召回原因。
@dataclass
class ToolSearchHit:

    name: str
    score: float
    reasons: list[str]


# LLM: BaseToolSearchProvider 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: BaseToolSearchProvider 封装 工具系统 的一组相关操作，供上层组合调用。
class BaseToolSearchProvider:

    name = "base"

    # LLM: BaseToolSearchProvider.search 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 按查询词检索候选工具或本地记录并返回排序结果。
    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        raise NotImplementedError


# LLM: KeywordToolSearchProvider 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: KeywordToolSearchProvider 封装 工具系统 的一组相关操作，供上层组合调用。
class KeywordToolSearchProvider(BaseToolSearchProvider):

    name = "keyword"

    # LLM: KeywordToolSearchProvider.search 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 按查询词检索候选工具或本地记录并返回排序结果。
    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        tokens = _tokenize(query)
        hits: list[ToolSearchHit] = []
        for spec in specs:
            score, reasons = _score_keyword_spec(spec, tokens)
            if score > 0:
                hits.append(ToolSearchHit(name=spec.name, score=score, reasons=reasons[:3]))
        hits.sort(key=lambda item: (-item.score, item.name))
        return hits[:limit]


# LLM: VectorToolSearchProvider 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: VectorToolSearchProvider 封装 工具系统 的一组相关操作，供上层组合调用。
class VectorToolSearchProvider(BaseToolSearchProvider):

    name = "vector"

    # LLM: VectorToolSearchProvider.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 VectorToolSearchProvider 的依赖、配置和运行期字段。
    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    # LLM: VectorToolSearchProvider.search 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 按查询词检索候选工具或本地记录并返回排序结果。
    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        if not self.enabled:
            return []
        return []


# LLM: HybridToolRetriever 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: HybridToolRetriever 封装 工具系统 的一组相关操作，供上层组合调用。
class HybridToolRetriever:

    # LLM: HybridToolRetriever.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 HybridToolRetriever 的依赖、配置和运行期字段。
    def __init__(self, providers: list[BaseToolSearchProvider]):
        self.providers = providers

    # LLM: HybridToolRetriever.search 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 按查询词检索候选工具或本地记录并返回排序结果。
    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        merged: dict[str, ToolSearchHit] = {}
        for provider in self.providers:
            for hit in provider.search(query, specs, limit):
                _merge_tool_hit(merged, provider.name, hit)
        ranked = sorted(merged.values(), key=lambda item: (-item.score, item.name))
        return ranked[:limit]


# LLM: BaseTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: BaseTool 数据模型，集中保存 工具系统 的结构化状态。
class BaseTool:

    spec: ToolSpec

    # LLM: BaseTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 BaseTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raise NotImplementedError



# LLM: _tokenize 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 tokenize 步骤，并保持调用方依赖的数据形状。
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


# LLM: _score_keyword_spec 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 score_keyword_spec 步骤，并保持调用方依赖的数据形状。
def _score_keyword_spec(spec: ToolSpec, tokens: list[str]) -> tuple[float, list[str]]:
    haystacks = _keyword_haystacks(spec)
    score = 0.0
    reasons: list[str] = []
    for token in tokens:
        token_score, token_reasons = _score_keyword_token(token, haystacks)
        score += token_score
        reasons.extend(token_reasons[:1])
    return score, reasons


# LLM: _merge_tool_hit 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 merge_tool_hit 信息，供注册表鉴权或执行使用。
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


# LLM: _append_unique_reasons 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 向结果或告警集合加入 append_unique_reasons，同时保留调用方依赖的顺序。
def _append_unique_reasons(target: list[str], reasons: list[str]) -> None:
    for reason in reasons:
        if reason not in target:
            target.append(reason)
    del target[4:]


# LLM: _keyword_haystacks 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 keyword_haystacks 步骤，并保持调用方依赖的数据形状。
def _keyword_haystacks(spec: ToolSpec) -> dict[str, str]:
    return {
        "name": spec.name.lower(),
        "description": spec.description.lower(),
        "category": spec.category.lower(),
        "keywords": " ".join(spec.keywords).lower(),
        "use_cases": " ".join(spec.use_cases).lower(),
    }


# LLM: _score_keyword_token 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 score_keyword_token 步骤，并保持调用方依赖的数据形状。
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


# LLM: _chinese_subtokens 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 chinese_subtokens 步骤，并保持调用方依赖的数据形状。
def _chinese_subtokens(token: str) -> list[str]:
    if not re.fullmatch(r"[\u4e00-\u9fff]+", token):
        return []
    return [
        token[idx : idx + size]
        for size in range(2, min(4, len(token)) + 1)
        for idx in range(0, len(token) - size + 1)
    ]
