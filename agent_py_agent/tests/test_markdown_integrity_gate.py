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


# --- 未完成占位符门(r18a 多子代理实锤:子代理幻觉没产出,兜底 Final Report 占位
#     "待完成后填写" 被当 ok=true 交付、closeout 验收通过)---


def test_unfinished_placeholder_flagged():
    # my-agent 兜底生成的 Final Report 占位模板(子代理没真产出)→ 必须判未完成
    placeholder = (
        "# Final Report\n\n- task_id: run-x\n- run_id: sub-y\n- status: PLANNING\n\n## Result\n\n待完成后填写\n"
    )
    assert "MARKDOWN_UNFINISHED_PLACEHOLDER" in _codes(markdown_integrity_findings(Path("flask.md"), placeholder))


def test_short_real_report_not_flagged_as_placeholder():
    # 短但有真实内容(无占位短语)→ 不误判为占位(不限制模型产出短而完整的内容)
    short = "# 错误分析\n\n第 1235 行 DB_TIMEOUT,数据库连接池耗尽导致超时,建议扩大连接池并加重试。\n"
    assert "MARKDOWN_UNFINISHED_PLACEHOLDER" not in _codes(markdown_integrity_findings(Path("r.md"), short))


def test_generic_placeholder_words_not_hard_flagged():
    # 通用占位词(__FILL__/待补充/TODO)归 document_quality 软门(warning)管,不触发本 hard 门——
    # 即使内容很短(handoff 实锤:# 摘要/很好。/# 结果/__FILL__ 该是 warning 不是 hard,不阻断交付)。
    handoff = "# 摘要\n很好。\n\n# 结果\n__FILL__\n"
    assert "MARKDOWN_UNFINISHED_PLACEHOLDER" not in _codes(markdown_integrity_findings(Path("handoff.md"), handoff))
    short_todo = "# 计划\n\n细节待补充。\n"
    assert "MARKDOWN_UNFINISHED_PLACEHOLDER" not in _codes(markdown_integrity_findings(Path("p.md"), short_todo))
