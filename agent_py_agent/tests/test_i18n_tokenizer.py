"""审计 #7 修复真测:分词器 Unicode 脚本感知,非中英语言不再零召回。

旧实现 [a-z0-9_]+|[一-鿿]+ 对韩/俄/阿/泰/印地语切出 0 token → BM25 零召回(覆盖几十国语言的核心 bug)。
真喂各语言文档+同语言查询,断言能召回(rank 命中);中英行为不变。学 长期助手 CJK-aware FTS sanitizer 思路。
"""

from __future__ import annotations

from agent_py_agent.agent.retrieval.lexical import rank, tokenize


def test_tokenize_nonzero_for_all_scripts() -> None:
    for s in ["한국어", "привет", "مرحبا", "สวัสดี", "नमस्ते", "ファイル検索", "café", "Ελληνικά"]:
        assert tokenize(s), f"{s!r} 切出零 token(零召回)"


def test_chinese_english_tokenize_unchanged() -> None:
    assert tokenize("hello world") == ["hello", "world"]
    assert tokenize("部署口令") == ["部署", "署口", "口令"]  # 中文 bigram 行为保持


def test_accented_latin_not_shredded() -> None:
    assert tokenize("café résumé") == ["café", "résumé"]  # 旧实现切成 caf/r/sum


def test_bm25_recall_across_languages() -> None:
    lines = [
        "한국어 문서 검색",          # 0 韩
        "привет русский текст",      # 1 俄
        "العربية بحث وثيقة",         # 2 阿
        "中文文档检索",               # 3 中
        "english document search",   # 4 英
    ]
    assert 0 in rank("한국어 검색", lines)   # 韩:同语言查询能召回(旧实现零召回)
    assert 1 in rank("русский", lines)        # 俄
    assert 2 in rank("بحث", lines)            # 阿
    assert 3 in rank("中文检索", lines)        # 中(仍工作)
    assert 4 in rank("english", lines)        # 英(仍工作)


def test_query_and_doc_fragment_identically_so_recall_holds() -> None:
    # 印地语(天城文)在 virama 处会切碎,但查询与文档切法一致 → 仍能匹配召回
    lines = ["नमस्ते दुनिया", "unrelated text"]
    assert 0 in rank("नमस्ते", lines)
