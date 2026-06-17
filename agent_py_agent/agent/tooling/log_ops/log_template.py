
from __future__ import annotations

"""日志模板挖掘(轻量 Drain 风格)—— ML 前期分析:把海量日志无监督聚类成"模板",理解格式 + 降噪。

几十万条日志 → 几十种模板(变化的字段用 <*> 占位)+ 各模板占比。LLM 备课时看"模板 + 占比"而非
采样几条,又全又降噪,定方案更准。纯算法、不训练、无第三方依赖,符合"轻量内置、撑几个月"。

分工:ML 定量(有几种模板、各占比、变化在哪) + LLM 定性(这是什么日志、哪种模板是威胁、怎么研判)。
按 token 数分桶,桶内按"同位置 token 匹配率"归并,不同处吸收成 <*> 通配。
"""

from typing import Any

_WILDCARD = "<*>"


def _tokenize(line: str) -> list[str]:
    return line.split()


def _similarity(template: list[str], tokens: list[str]) -> float:
    """同位置 token 匹配率(已是通配的位置也算匹配);长度不同直接 0。"""
    if len(template) != len(tokens):
        return 0.0
    if not template:
        return 1.0
    same = sum(1 for a, b in zip(template, tokens) if a == b or a == _WILDCARD)
    return same / len(template)


def _merge(template: list[str], tokens: list[str]) -> list[str]:
    """把新行吸收进模板:不同位置变 <*>。"""
    return [tok if tok == new else _WILDCARD for tok, new in zip(template, tokens)]


def mine_templates(
    lines: list[str], *, max_templates: int = 50, sim_threshold: float = 0.5
) -> list[dict[str, Any]]:
    """从日志行挖模板。返回 [{template, count, example}],按 count 降序,最多 max_templates 个。

    sim_threshold:同位置匹配率达到才归并到已有模板(否则开新模板)。0.5 偏宽(同结构归一类),调高更细。
    """
    clusters: list[dict[str, Any]] = []
    cap = max(max_templates * 4, max_templates)  # 过程中允许超量,最后按 count 截断
    for raw in lines:
        tokens = _tokenize(raw.strip())
        if not tokens:
            continue
        best: dict[str, Any] | None = None
        best_sim = sim_threshold
        for cluster in clusters:
            sim = _similarity(cluster["tokens"], tokens)
            if sim >= best_sim:
                best, best_sim = cluster, sim
        if best is not None:
            best["tokens"] = _merge(best["tokens"], tokens)
            best["count"] += 1
        elif len(clusters) < cap:
            clusters.append({"tokens": list(tokens), "count": 1, "example": raw.strip()})
    clusters.sort(key=lambda c: c["count"], reverse=True)
    total = sum(c["count"] for c in clusters) or 1
    return [
        {
            "template": " ".join(c["tokens"]),
            "count": c["count"],
            "share": round(c["count"] / total, 4),
            "example": c["example"],
        }
        for c in clusters[:max_templates]
    ]


__all__ = ["mine_templates"]
