from __future__ import annotations

"""LLM: defines stable tool metadata, retrieval hits, and base execution contracts.

给人看的解释：
这个文件只放工具系统最基础的“名词”和“接口”。
比如一个工具叫什么、适合干什么、执行后返回什么格式，以及工具检索结果长什么样。
后面无论是文件工具、网络工具还是编排工具，都应该沿用这里的结构。
"""

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    """单个工具的说明书。

    这份结构同时服务两类场景：
    - 生成给模型看的工具目录
    - 做工具检索和排序

    字段设计上尽量说人话，方便你后面继续扩展：
    - `description` 是一句话总述
    - `use_cases` 是“什么时候该用它”
    - `avoid_when` 是“什么时候别用它”
    - `keywords` 给检索器做召回
    - `parameters` / `parameter_details` 负责把参数说明拆成简版和详版
    """

    name: str
    category: str
    description: str
    use_cases: list[str]
    avoid_when: list[str]
    keywords: list[str]
    parameters: dict[str, str]
    parameter_details: dict[str, str] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)

    def render_catalog_entry(self) -> str:
        """渲染工具目录里的中等详细条目。

        这里故意不把所有细节都展开，只保留足够帮助模型做初步判断的信息。
        简单说，就是先给它看“工具菜单”，别一上来就把整本说明书塞过去。
        """

        params = "、".join(self.parameters.keys()) or "无"
        use_cases = "；".join(self.use_cases[:2]) or "无"
        avoid_when = "；".join(self.avoid_when[:1]) or "无"
        return (
            f"- {self.name} [{self.category}]：{self.description}\n"
            f"  适用场景：{use_cases}\n"
            f"  关键参数：{params}\n"
            f"  不适用时机：{avoid_when}"
        )

    def render_detail_entry(self) -> str:
        """渲染当前任务候选工具的详细说明。

        这里只给少数高相关工具展开，目的是减少误判，但不把所有工具都铺满 prompt。
        """

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


@dataclass
class ToolExecutionResult:
    """工具执行结果。

    不管底层工具是读文件、写文件，还是发 HTTP 请求，最终都统一成这个结构。
    这样核心调度器只要认一种返回格式，后面加新工具也不用再改主循环。
    """

    tool: str
    ok: bool
    output: str

    def render_for_prompt(self) -> str:
        """把执行结果转成可直接塞回 prompt 的文本。"""

        status = "ok" if self.ok else "error"
        return f"[tool={self.tool}; status={status}]\n{self.output}"


@dataclass
class ToolSearchHit:
    """一次工具检索的命中结果。"""

    name: str
    score: float
    reasons: list[str]


class BaseToolSearchProvider:
    """工具检索提供者接口。

    先把接口定下来，后面无论你接本地 embedding 还是远端向量服务，都按这个协议接入。
    """

    name = "base"

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        raise NotImplementedError


class KeywordToolSearchProvider(BaseToolSearchProvider):
    """关键词检索器。

    这是当前真正生效的第一层召回，优先保证稳定和可解释。
    说白了，它不够聪明，但胜在不容易胡来。
    """

    name = "keyword"

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        tokens = _tokenize(query)
        hits: list[ToolSearchHit] = []
        for spec in specs:
            reasons: list[str] = []
            score = 0.0
            haystacks = {
                "name": spec.name.lower(),
                "description": spec.description.lower(),
                "category": spec.category.lower(),
                "keywords": " ".join(spec.keywords).lower(),
                "use_cases": " ".join(spec.use_cases).lower(),
            }
            for token in tokens:
                token_score = 0.0
                token_reasons: list[str] = []
                if token in haystacks["name"]:
                    token_score += 6.0
                    token_reasons.append(f"命中工具名“{token}”")
                if token in haystacks["keywords"]:
                    token_score += 4.0
                    token_reasons.append(f"命中关键词“{token}”")
                if token in haystacks["category"]:
                    token_score += 2.5
                    token_reasons.append(f"命中类别“{token}”")
                if token in haystacks["description"] or token in haystacks["use_cases"]:
                    token_score += 1.5
                    token_reasons.append(f"命中用途描述“{token}”")
                score += token_score
                reasons.extend(token_reasons[:1])
            if score > 0:
                hits.append(ToolSearchHit(name=spec.name, score=score, reasons=reasons[:3]))
        hits.sort(key=lambda item: (-item.score, item.name))
        return hits[:limit]


class VectorToolSearchProvider(BaseToolSearchProvider):
    """向量检索接口的占位实现。

    这版先不真的做 embedding 计算，只把扩展点留好。
    这样后面你要接向量库时，不需要再动核心调度器和 prompt 结构。
    """

    name = "vector"

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        if not self.enabled:
            return []
        return []


class HybridToolRetriever:
    """混合检索器。

    逻辑很简单：
    - 先把多个召回器的结果合并
    - 再按总分排
    - 最后给出“为什么推荐这个工具”

    现在真正起作用的是关键词层，向量层只是接口预留。
    """

    def __init__(self, providers: list[BaseToolSearchProvider]):
        self.providers = providers

    def search(self, query: str, specs: list[ToolSpec], limit: int) -> list[ToolSearchHit]:
        merged: dict[str, ToolSearchHit] = {}
        for provider in self.providers:
            for hit in provider.search(query, specs, limit):
                existing = merged.get(hit.name)
                provider_reason = f"{provider.name} 召回"
                if existing is None:
                    merged[hit.name] = ToolSearchHit(
                        name=hit.name,
                        score=hit.score,
                        reasons=[provider_reason, *hit.reasons][:4],
                    )
                    continue
                existing.score += hit.score
                for reason in [provider_reason, *hit.reasons]:
                    if reason not in existing.reasons:
                        existing.reasons.append(reason)
                existing.reasons = existing.reasons[:4]
        ranked = sorted(merged.values(), key=lambda item: (-item.score, item.name))
        return ranked[:limit]


class BaseTool:
    """所有具体工具的基类。"""

    spec: ToolSpec

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        raise NotImplementedError



def _tokenize(text: str) -> list[str]:
    """把自然语言查询切成适合粗检索的小片段。

    这里不追求花哨，只做够用的切分：
    - 英文、数字、下划线按连续片段切
    - 中文按连续中文片段保留，再拆出 2 到 4 字的小片段补召回
    """

    lowered = (text or "").lower()
    tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", lowered)
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            for size in range(2, min(4, len(token)) + 1):
                for idx in range(0, len(token) - size + 1):
                    expanded.append(token[idx : idx + size])
    seen: set[str] = set()
    unique: list[str] = []
    for token in expanded:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique
