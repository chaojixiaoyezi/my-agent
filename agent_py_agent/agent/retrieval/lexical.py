"""词面相关性:中文 bigram 分词 + BM25 + RRF 融合(纯 Python,零外部依赖)。

Phase 2 综合三家:学 claw memory/retrieval.py 的 BM25(把"最相关"顶到第一,治 my-agent
裸 n-gram 召回把真相关淹没的短板)+ 通道运行时 的 RRF 混合检索(融合 BM25 与向量两路排序)。
my-agent 原则"能自建就自建"——BM25/分词/RRF 全是经典 IR,纯 Python 自建,不引向量库。
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import NamedTuple

# 任意脚本的字母/数字/下划线连续块(\w 在 Python re 对 str 默认 Unicode):覆盖拉丁含重音、西里尔、
# 阿拉伯、天城文(印地)、谚文(韩)等有空格脚本——修旧 [a-z0-9_] 对非拉丁零 token(审计 #7)。
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# 无空格分词的脚本(按字符 bigram 切,否则整段一个 token 召回差):CJK 表意 + 兼容 + 扩展、
# 日文平/片假名、泰文、Lao、Myanmar、Khmer。旧实现只认基本 CJK [一-鿿],丢日文假名/泰文等。
_NO_SPACE_RANGES = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2EBEF),  # CJK
    (0x3040, 0x30FF), (0x31F0, 0x31FF),  # 日文假名
    (0x0E00, 0x0EFF), (0x1000, 0x109F), (0x1780, 0x17FF),  # 泰/Lao、Myanmar、Khmer
)


def _is_no_space(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _NO_SPACE_RANGES)


def _char_ngrams(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]

# BM25 标准参数(Robertson/Sparck-Jones 经典默认)。
K1 = 1.2
B = 0.75
# 精确短语命中加成:tokenize 把"部署口令"切成 [部署,署口,口令],三个 bigram 分散命中
# 不如整串出现可靠——整串在行内出现时给固定加成。
PHRASE_BONUS = 2.0


def tokenize(text: str) -> list[str]:
    """Unicode 脚本感知分词:NFKC 规范化 + casefold;有空格脚本(拉丁含重音/西里尔/阿拉伯/天城/谚文…)
    按词,无空格脚本(CJK/日文假名/泰…)按相邻双字 bigram。修"非中英语言检索零召回"(审计 #7)。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    for run in _WORD_RE.findall(normalized):
        if any(_is_no_space(ch) for ch in run):
            tokens.extend(_char_ngrams(run))  # CJK/日/泰 等按字符 bigram
        else:
            tokens.append(run)  # 有空格脚本按整词
    return tokens


class _BM25Stats(NamedTuple):
    document_frequency: dict[str, int]
    total: int
    avg_len: float


def _doc_bm25(doc: list[str], query_tokens: set[str], stats: _BM25Stats) -> float:
    """单文档对 query 的 BM25 分(抽出来降主循环嵌套,保持复杂度纪律)。"""
    if not doc:
        return 0.0
    length_norm = K1 * (1 - B + B * (len(doc) / stats.avg_len if stats.avg_len else 1.0))
    score = 0.0
    for token in query_tokens:
        term_frequency = doc.count(token)
        if not term_frequency:
            continue
        df = stats.document_frequency.get(token, 0)
        idf = math.log(1.0 + (stats.total - df + 0.5) / (df + 0.5))
        score += idf * (term_frequency * (K1 + 1)) / (term_frequency + length_norm)
    return score


def bm25_scores(query: str, docs_tokens: list[list[str]], raw_lines: list[str]) -> list[float]:
    """对每个候选文档算 BM25 相关分(含精确短语加成)。返回与 docs_tokens 等长的分数列表。"""
    query_tokens = set(tokenize(query))
    total = len(docs_tokens)
    if not query_tokens or total == 0:
        return [0.0] * total
    avg_len = sum(len(d) for d in docs_tokens) / total if total else 0.0
    document_frequency: dict[str, int] = {}
    for doc in docs_tokens:
        for token in set(doc) & query_tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    stats = _BM25Stats(document_frequency, total, avg_len)
    needle = " ".join(query.lower().split())
    scores: list[float] = []
    for index, doc in enumerate(docs_tokens):
        score = _doc_bm25(doc, query_tokens, stats)
        if needle and index < len(raw_lines) and needle in raw_lines[index].lower():
            score += PHRASE_BONUS
        scores.append(score)
    return scores


def rank(query: str, lines: list[str]) -> list[int]:
    """按与 query 的 BM25 相关度对 ``lines`` 排序,返回索引序(高分在前,只留 >0)。

    稳定排序:同分保持原次序——调用方按"新鲜文件优先"收集候选,同分时新鲜者自然靠前。
    query 切不出 token(纯符号)时返回原序。
    """
    if not lines:
        return []
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return list(range(len(lines)))
    docs = [tokenize(line) for line in lines]
    scores = bm25_scores(query, docs, lines)
    order = sorted(range(len(lines)), key=lambda i: -scores[i])
    return [i for i in order if scores[i] > 0.0]


def reciprocal_rank_fusion(rankings: list[list[str]], *, k: int = 60) -> list[tuple[str, float]]:
    """RRF 融合多路检索排序(BM25 + 向量等):每路给 id 打 1/(k+名次),求和后降序。

    比"归一化分数相加"更稳——不受各路分数量纲差异影响(经典 IR 做法,混合检索的排序融合)。
    输入每路是一个 id 列表(已按相关度降序);返回 [(id, 融合分)] 降序。
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank_index, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank_index + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
