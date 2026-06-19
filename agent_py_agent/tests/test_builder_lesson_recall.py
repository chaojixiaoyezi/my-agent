"""P2 检索召回:lessons 中文 n-gram 模糊召回单测(补 R7 头号短板"检索偏窄")。"""

from agent.prompting_parts.builder import _ngram_hit, _stem_matches


def test_ngram_hit_chinese_fuzzy_recall():
    assert _ngram_hit("日志运营值守", "帮我盯日志运营") is True
    assert _ngram_hit("安全告警研判", "帮忙研判安全告警") is True


def test_ngram_hit_controls_noise():
    assert _ngram_hit("日志运营值守", "写个待办工具") is False
    assert _ngram_hit("数据库优化", "帮我查天气") is False


def test_ngram_hit_short_word_fallback_to_substring():
    assert _ngram_hit("ab", "xabz") is True  # 短词(<3字)回退完全子串
    assert _ngram_hit("ab", "xyz") is False


def test_stem_matches_ngram_recall(tmp_path):
    (tmp_path / "日志运营值守.md").write_text("lesson", encoding="utf-8")
    (tmp_path / "无关主题abc.md").write_text("lesson", encoding="utf-8")
    hits = [p.stem for p in _stem_matches(tmp_path, "帮我做日志运营".casefold())]
    assert "日志运营值守" in hits  # 中文模糊召回
    assert "无关主题abc" not in hits  # 不滥召


def test_stem_matches_exact_substring_still_works(tmp_path):
    (tmp_path / "todo-cli.md").write_text("lesson", encoding="utf-8")
    hits = [p.stem for p in _stem_matches(tmp_path, "帮我用 todo-cli 工具".casefold())]
    assert "todo-cli" in hits  # 完全子串仍命中(回归保护)
