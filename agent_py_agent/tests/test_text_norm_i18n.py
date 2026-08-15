"""审计 #20(medium/基础·i18n)真测:统一规范化层 + 路由匹配跨书写形式命中同一条。

i18n 定调 = 正确服务几十国几十语言用户的输入鲁棒性(非翻译)。真喂韩/俄/阿/泰/印地/日文假名/
重音拉丁,断言规范化键非空、保留脚本;真造 NFC vs NFD + 全角空格 + 零宽 + 大小写,断言映射到
同一键(去重命中同一条);真跑 matcher 用全角/NFD/大小写变体查询,断言召回 > 0(原 .lower()
不折全角/不规范 NFD 会漏命中)。不可见字符一律用显式 \\u 转义。学 通道运行时 NFC/casefold 全边界规范化。
"""

from __future__ import annotations

import unicodedata

from agent_py_agent.agent.common.text_norm import fold_key, nfc, strip_invisibles
from agent_py_agent.agent.memory_routing.matcher import match_routes
from agent_py_agent.agent.memory_routing.models import MemoryRoute

_FULLWIDTH_SPACE = "\u3000"  # 全角空格
_NBSP = "\u00a0"  # 不换行空格
_ZWSP = "\u200b"  # 零宽空格
_BOM = "\ufeff"  # BOM/零宽不换行空格
_ZWJ = "\u200d"  # 零宽连接符(有语义,保留)


# ---------- 规范化层本身 ----------

def test_nfd_and_nfc_fold_to_same_key() -> None:
    nfc_cafe = unicodedata.normalize("NFC", "café")  # é 单码点
    nfd_cafe = unicodedata.normalize("NFD", "café")  # e + 组合重音
    assert nfc_cafe != nfd_cafe  # 原始字节不等(macOS 文件名 NFD vs 输入 NFC 的真实差异)
    assert fold_key(nfc_cafe) == fold_key(nfd_cafe)  # 规范化后同键
    assert nfc(nfc_cafe) == nfc(nfd_cafe)  # 内容哈希用 nfc 也归一


def test_fullwidth_and_halfwidth_fold_to_same_key() -> None:
    assert fold_key("ＨＥＬＬＯ") == fold_key("hello")  # 全角字母 NFKC→半角 + casefold
    assert fold_key("１２３") == fold_key("123")  # 全角数字
    assert fold_key(f"a{_FULLWIDTH_SPACE}b") == fold_key("a b")  # 全角空格 U+3000 折成普通空格
    assert fold_key(f"x{_NBSP}y") == fold_key("x y")  # NBSP 折成普通空格


def test_zero_width_noise_stripped_but_joiners_kept() -> None:
    assert fold_key(f"hi{_ZWSP}there") == fold_key("hithere")  # 零宽空格噪声剥离
    assert fold_key(f"a{_BOM}b") == fold_key("ab")  # BOM/零宽不换行剥离
    assert _ZWJ in strip_invisibles(f"a{_ZWJ}b")  # ZWJ 有语义(阿拉伯/印度/emoji),保留


def test_casefold_cross_language() -> None:
    assert fold_key("ß") == fold_key("ss")  # 德语 eszett:casefold 覆盖,.lower() 不覆盖
    assert fold_key("ПРИВЕТ") == fold_key("привет")  # 俄语大小写
    assert fold_key("ΑΘΗΝΑ") == fold_key("αθηνα")  # 希腊语大小写


def test_multilingual_keys_nonempty_and_stripped() -> None:
    samples = {
        "korean": "안녕하세요",
        "russian": "Привет мир",
        "arabic": "مرحبا بالعالم",
        "thai": "สวัสดีชาวโลก",
        "hindi": "नमस्ते दुनिया",
        "japanese_kana": "こんにちは",
        "accented_latin": "Niño Über Œuvre",
    }
    for name, text in samples.items():
        key = fold_key(text)
        assert key, f"{name} 规范化键不应为空(非中英脚本不能被清成空 → 零召回)"
        assert key == key.strip()


def test_dedup_set_collapses_writing_variants() -> None:
    variants = [
        unicodedata.normalize("NFC", "Café"),  # NFC + 大写 C
        unicodedata.normalize("NFD", "café"),  # NFD + 小写
        "café",                                # 普通 NFC 小写
        "ｃａｆé",                                # 全角 cafe + é
    ]
    keys = {fold_key(v) for v in variants}
    assert len(keys) == 1  # 所有书写变体去重为一条(记忆不再重复堆积)


# ---------- matcher 真实召回(跨书写形式) ----------

def _route() -> MemoryRoute:
    return MemoryRoute(route_id="r1", topic="机器学习", related_terms=["machine learning", "深度学习"], priority=5)


def test_recall_hits_with_fullwidth_query() -> None:
    # 用户用 CJK 输入法打出全角字母 + 全角空格的查询
    query = f"ＭＡＣＨＩＮＥ{_FULLWIDTH_SPACE}ＬＥＡＲＮＩＮＧ"
    hits = match_routes(query, [_route()])
    assert len(hits) >= 1  # 全角变体召回到同一条(原 .lower() 不折全角 → 零命中)


def test_recall_hits_with_nfd_and_case_query() -> None:
    route = MemoryRoute(route_id="r2", topic="Über-Café", related_terms=["Über-Café"], priority=1)
    nfd_query = unicodedata.normalize("NFD", "über-café")  # NFD + 小写
    hits = match_routes(nfd_query, [route])
    assert len(hits) >= 1  # NFD + 大小写变体仍召回(跨平台输入鲁棒)


def test_recall_unaffected_for_plain_ascii() -> None:
    hits = match_routes("machine learning", [_route()])
    assert len(hits) >= 1  # 回归:普通英文查询行为不变(规范化不破现有匹配)


# ---------- 记忆去重 / 检索(跨书写形式归一) ----------

def test_memory_dedup_collapses_nfd_nfc_content() -> None:
    from agent_py_agent.agent.memory_store.jsonl import MemoryRecord, _dedupe_memory_records

    nfc_rec = MemoryRecord(role="user", content=unicodedata.normalize("NFC", "café 笔记"), created_at=123.0)
    nfd_rec = MemoryRecord(role="user", content=unicodedata.normalize("NFD", "café 笔记"), created_at=123.0)
    assert nfc_rec.content != nfd_rec.content  # 原始字节不等
    deduped = _dedupe_memory_records([nfc_rec, nfd_rec])
    assert len(deduped) == 1  # NFD/NFC 同一条记忆去重为一(不再重复堆积)


def test_memory_search_recalls_fullwidth_query() -> None:
    from agent_py_agent.agent.memory_store.jsonl import MemoryRecord, _search_memory_records

    records = [
        MemoryRecord(role="user", content="machine learning 深度学习入门", created_at=1.0),
        MemoryRecord(role="user", content="无关内容", created_at=2.0),
    ]
    hits = _search_memory_records(records, f"ＭＡＣＨＩＮＥ{_FULLWIDTH_SPACE}ＬＥＡＲＮＩＮＧ", top_k=5)
    assert hits and hits[0].content.startswith("machine")  # 全角查询召回半角内容(检索不再漏命中)
