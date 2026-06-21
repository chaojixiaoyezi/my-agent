"""统一文本规范化层(审计 #20):面向几十国几十语言用户的输入鲁棒性。

不是翻译,是"正确处理各种人输入的同一内容"。同一逻辑内容在跨平台/跨输入法下有多种书写形式:
- 跨平台 NFC/NFD:macOS 文件名用 NFD(é = e + 组合重音),多数输入用 NFC(é 单码点),字节不等;
- 全角/半角:CJK 输入法常出全角字母数字(ＡＢＣ１２３)、全角空格 U+3000;
- 大小写:跨语言折叠(德语 ß→ss、土耳其 İ),.lower() 覆盖不全;
- 零宽噪声:复制粘贴带入 ZWSP/BOM/方向标记,肉眼不可见却破坏相等性。

不归一化,这些差异会让"同一内容"被去重/匹配/哈希/检索当成两份 → 记忆重复堆积、跨平台引用断链、
非中文检索漏命中(直接喂"文件丢失"类顽疾)。本层提供两档,全部基于 stdlib unicodedata,自建无依赖:
- nfc():仅规范等价折叠(NFC),不改可见内容——内容哈希/文件名字节统一用它;
- fold_key():激进折叠(NFKC+去零宽噪声+casefold+折叠空白)——去重/匹配/标签/检索键用它。
"""

from __future__ import annotations

import re
import unicodedata

# 零宽/方向"噪声"字符(显式码点,规范化键时剥离防"看不见的差异"):
#   U+200B 零宽空格 / U+2060 word joiner / U+FEFF BOM·零宽不换行 / U+200E·200F 方向标记 /
#   U+00AD 软连字符 / U+180E 蒙文元音分隔符。
# 刻意不含 ZWJ(U+200D)/ZWNJ(U+200C)——它们在阿拉伯/波斯/印度系文字与 emoji 序列里有语义,
# 剥掉会把不同词/字形误并(i18n 正确性优先)。
_NOISE_CHARS = "\u200b\u2060\ufeff\u200e\u200f\u00ad\u180e"
_NOISE_TABLE = dict.fromkeys(map(ord, _NOISE_CHARS), None)
_WHITESPACE_RUN = re.compile(r"\s+")


def nfc(text: str) -> str:
    """规范等价折叠到 NFC:统一跨平台编码形式(NFD↔NFC),不改任何可见内容。

    内容哈希、文件名字节比较前过它:macOS NFD 文件名与 NFC 输入归一后相等,去重/引用不再断链。
    """
    return unicodedata.normalize("NFC", text or "")


def strip_invisibles(text: str) -> str:
    """剥离零宽/方向噪声字符(保留有语义的 ZWJ/ZWNJ 连接符)。"""
    return (text or "").translate(_NOISE_TABLE)


def fold_key(text: str) -> str:
    """规范化匹配/去重键:NFKC(全角→半角、兼容字符折叠)+ 去零宽噪声 + casefold(跨语言大小写)
    + 折叠各类空白(NFKC 已把全角空格 U+3000/NBSP 折成普通空格)为单空格 + 去首尾空白。

    同一内容的不同书写形式(全角/半角、大小写、NFD/NFC、夹零宽)映射到同一键。enum/flag/tag/topic
    匹配、dedup、检索归一都过它——修掉 CJK 输入法全角空格致匹配静默失效等顽疾。
    """
    folded = unicodedata.normalize("NFKC", text or "")
    folded = strip_invisibles(folded).casefold()
    return _WHITESPACE_RUN.sub(" ", folded).strip()
