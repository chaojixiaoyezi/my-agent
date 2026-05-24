"""LLM: focused tests for structured file-contract extraction.

函数/模块用途: 验证产品代码只从机器字段读取 required_files/forbidden_files，不再靠中文或英文自然语言猜业务意图。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.required_file_terms import (
    forbidden_file_terms_from_text,
    required_file_terms_from_text,
)


# LLM: structured required_files fields are the only source of required deliverable filenames.
# 函数用途: required_files 机器字段里的文件名会进入必需产物合同，逗号、顿号、斜杠和 bullet 写法都可读。
def test_file_contract_extracts_structured_required_files():
    text = """
    required_files: index.html、style.css/app.js, docs/README.md
    required_files:
    - flow-b.html
    - reports/final_report.md
    """

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|md")

    assert required == ["index.html", "style.css", "app.js", "docs/README.md", "flow-b.html", "reports/final_report.md"]


# LLM: structured forbidden_files fields stay separate from deliverables.
# 函数用途: forbidden_files 机器字段只进入禁止文件合同，不会污染 required_files。
def test_file_contract_extracts_structured_forbidden_files():
    text = """
    required_files: index.html, item-detail.html
    forbidden_files: product.html/legacy.html, output.json
    forbidden_files:
    - RUNNER_RESULT.md
    - execution_context.json
    """

    required = required_file_terms_from_text(text, extensions=r"html?|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|json|md")

    assert required == ["index.html", "item-detail.html"]
    assert forbidden == ["product.html", "legacy.html", "output.json", "RUNNER_RESULT.md", "execution_context.json"]


# LLM: natural prose is no longer a product-code file contract source.
# 函数用途: 即便句子里出现“必须/禁止/输出/读取”等自然语言，代码层也不能据此猜 required/forbidden 文件。
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


# LLM: unknown natural labels are deliberately ignored instead of becoming new product rules.
# 函数用途: 中文标题、英文 prose 和括号解释都不能替代 required_files/forbidden_files 机器字段。
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
