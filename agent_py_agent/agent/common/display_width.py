"""终端显示宽度 / 字形簇感知截断(审计 #22):面向几十国内容的表格对齐与安全截断。

终端里 CJK/全角字符占 2 列、组合标记/零宽占 0 列,但 len()/str:<N 按"码点数"算 → 中日韩内容下
审计/看板表格列错位;text[:N] 按码点切会把 emoji ZWJ 家庭序列/国旗(区域指示符对)/组合变音
拦腰截断成残缺字形。本模块基于 stdlib unicodedata 自建(无依赖):
- display_width():按 east_asian_width 算终端列宽(宽=2,组合/格式/零宽=0),字形簇内宽度取一次;
- truncate_display():按显示列宽截断,且只在字形簇边界切,绝不切碎 emoji/组合序列;
- pad_display()/fit_column():按显示列宽右补空格对齐(替换 str:<N)。
按终端列宽和字形簇计数。字形簇用 UAX#29 的实用子集(覆盖组合标记/
变体选择符/ZWJ 序列/区域指示符对),不引重型 grapheme 库。
"""

from __future__ import annotations

import unicodedata

_WIDE = ("W", "F")  # east_asian_width:Wide / Fullwidth → 占 2 列
_ZERO_CATEGORIES = ("Mn", "Me", "Cf")  # 组合标记(非间距/包围)、格式字符(含 ZWJ/零宽) → 占 0 列
_ZWJ = "\u200d"  # 零宽连接符


def _is_regional(ch: str) -> bool:
    return "\U0001f1e6" <= ch <= "\U0001f1ff"  # 区域指示符(成对组成国旗 emoji)


def _extends_cluster(text: str, start: int, i: int) -> bool:
    """text[i] 是否应并入从 start 开始的字形簇(组合/变体选择符/ZWJ 及其后继/区域指示符对)。"""
    ch = text[i]
    if unicodedata.category(ch) in ("Mn", "Mc", "Me"):
        return True  # 组合标记并入基字符
    if ch in (_ZWJ, "\ufe0f", "\ufe0e"):
        return True  # ZWJ / 变体选择符并入
    if text[i - 1] == _ZWJ:
        return True  # ZWJ 后继字符并入(emoji 家庭序列)
    return _is_regional(text[i - 1]) and _is_regional(ch) and i == start + 1  # 国旗:恰好成对


def _cluster_end(text: str, start: int) -> int:
    i = start + 1
    while i < len(text) and _extends_cluster(text, start, i):
        i += 1
    return i


def _grapheme_clusters(text: str) -> list[str]:
    clusters: list[str] = []
    i = 0
    while i < len(text):
        end = _cluster_end(text, i)
        clusters.append(text[i:end])
        i = end
    return clusters


def _is_wide(ch: str) -> bool:
    """该码点是否占 2 显示列:East_Asian_Width=W/F,或国旗区域指示符,或 emoji/象形符号面。"""
    if unicodedata.east_asian_width(ch) in _WIDE:
        return True
    if _is_regional(ch):
        return True  # 区域指示符(国旗)EAW=N 但终端显示宽 2
    return ch >= "\U0001f000"  # emoji/象形符号面(EAW 多为 N,终端显示宽 2)


def _cluster_width(cluster: str) -> int:
    """一个字形簇的显示列宽:含宽字符则 2,纯组合/零宽则 0,否则 1。"""
    if any(_is_wide(ch) for ch in cluster):
        return 2
    if all(unicodedata.category(ch) in _ZERO_CATEGORIES for ch in cluster):
        return 0
    return 1


def display_width(text: str) -> int:
    """终端显示列宽(非码点数):CJK/全角占 2,组合/零宽占 0,ZWJ 序列整簇算一次。"""
    return sum(_cluster_width(cluster) for cluster in _grapheme_clusters(text or ""))


def truncate_display(text: str, max_cols: int) -> str:
    """按显示列宽截断到 <= max_cols,且只在字形簇边界切(不切碎 emoji/组合序列)。"""
    if max_cols <= 0:
        return ""
    out: list[str] = []
    used = 0
    for cluster in _grapheme_clusters(text or ""):
        width = _cluster_width(cluster)
        if used + width > max_cols:
            break
        out.append(cluster)
        used += width
    return "".join(out)


def pad_display(text: str, width: int) -> str:
    """右补空格到 width 显示列(替换 str:<N 的码点对齐;text 已超宽则原样返回)。"""
    pad = width - display_width(text or "")
    return (text or "") + " " * pad if pad > 0 else (text or "")


def fit_column(text: str, width: int) -> str:
    """定宽列:先按显示列宽截断到 width,再右补空格到 width(CJK 不再错位)。"""
    return pad_display(truncate_display(text or "", width), width)
