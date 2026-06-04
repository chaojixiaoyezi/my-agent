"""LLM: focused tests for structured file-contract extraction.

函数/模块用途: 验证产品代码只从机器字段读取 required_files/forbidden_files，不再靠中文或英文自然语言猜业务意图。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.required_file_terms import (
    forbidden_file_terms_from_text,
    required_file_terms_from_text,
)


def test_file_contract_extracts_structured_required_files():
    text = """
    required_files: index.html、style.css/app.js, docs/README.md
    required_files:
    - flow-b.html
    - reports/final_report.md
    """

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|md")

    assert required == ["index.html", "style.css", "app.js", "docs/README.md", "flow-b.html", "reports/final_report.md"]


def test_file_contract_extracts_structured_forbidden_files():
    text = """
    required_files: index.html, item-detail.html
    forbidden_files: product.html/stale.html, output.json
    forbidden_files:
    - RUNNER_RESULT.md
    - execution_context.json
    """

    required = required_file_terms_from_text(text, extensions=r"html?|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|json|md")

    assert required == ["index.html", "item-detail.html"]
    assert forbidden == ["product.html", "stale.html", "output.json", "RUNNER_RESULT.md", "execution_context.json"]


def test_file_contract_ignores_natural_language_file_requirements():
    text = (
        "必须包含 index.html、style.css、app.js。"
        "不要创建 product.html，也不要写 output.json。"
        "最终输出 final_report.md，读取 source.md 作为输入。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == []
    assert forbidden == []


def test_file_contract_ignores_non_protocol_labels():
    text = """
    必须文件（禁止改名）：index.html, register.html
    禁止文件名：product.html/old-product.html
    Required files (exact names only): app.js.
    Do not create output.json.
    """

    required = required_file_terms_from_text(text, extensions=r"html?|js|json")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|js|json")

    assert required == []
    assert forbidden == []
