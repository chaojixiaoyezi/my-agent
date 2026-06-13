"""markdown 完整性 gate 钉子(organize 整理任务实锤):模型把原始单标题行文件
(# TODO: 修登录bug,标题即完整内容)分类搬运,内容未改,却被
MARKDOWN_TRAILING_EMPTY_HEADING hard 拦,误伤完整交付。修复:纯标题/搬运短文件不判,
只有"含正文段落且末尾停在空标题"才判。"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.artifact_structured_contracts import (
    _has_body_beyond_headings,
    markdown_integrity_findings,
)


def _codes(findings):
    return {f.code for f in findings}


def test_single_heading_short_file_not_flagged():
    # organize 实锤:单标题行搬运文件不该被判 trailing empty heading
    findings = markdown_integrity_findings(Path("todo.md"), "# TODO: 修复登录bug\n")
    assert "MARKDOWN_TRAILING_EMPTY_HEADING" not in _codes(findings)


def test_pure_outline_not_flagged():
    # 纯标题大纲(全是标题无正文)不判
    findings = markdown_integrity_findings(Path("outline.md"), "# 一级\n## 二级\n### 三级\n")
    assert "MARKDOWN_TRAILING_EMPTY_HEADING" not in _codes(findings)


def test_body_then_trailing_heading_flagged():
    # 有正文段落 + 末尾停在空标题 → 真问题,仍判
    findings = markdown_integrity_findings(Path("doc.md"), "# 标题\n\n正文内容。\n\n## 参考资料\n")
    assert "MARKDOWN_TRAILING_EMPTY_HEADING" in _codes(findings)


def test_normal_doc_not_flagged():
    # 正常文档(末尾是正文)不判
    findings = markdown_integrity_findings(Path("ok.md"), "# 标题\n\n正文结尾。\n")
    assert "MARKDOWN_TRAILING_EMPTY_HEADING" not in _codes(findings)


def test_has_body_beyond_headings():
    assert _has_body_beyond_headings("# 只有标题\n") is False
    assert _has_body_beyond_headings("# 标题\n## 子标题\n") is False
    assert _has_body_beyond_headings("# 标题\n正文\n") is True
    assert _has_body_beyond_headings("# 标题\n\n- 列表项\n") is True
