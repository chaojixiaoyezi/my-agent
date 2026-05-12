"""LLM: focused tests for required/forbidden file term extraction.

函数/模块用途: 验证父级文件名合同能正确拆成 required_files 和 forbidden_files，避免下级误创建反例文件。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.required_file_terms import (
    forbidden_file_terms_from_text,
    required_file_terms_from_text,
)


# LLM: test_file_contract_extracts_required_and_forbidden_terms_separately locks the R27 root cause.
# 函数用途: 父级 prompt 同时包含必需文件和禁止反例时，结构化提取要把两类文件分开。
def test_file_contract_extracts_required_and_forbidden_terms_separately():
    text = (
        "必须包含 index.html、products.html、product-detail.html、style.css、app.js。"
        "不允许把 product-detail.html 改名成 product.html 或 old-product.html；"
        "不得改名为 legacy.html，也不要创建 obsolete.html。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js")

    assert required == ["index.html", "products.html", "product-detail.html", "style.css", "app.js"]
    assert forbidden == ["product.html", "old-product.html", "legacy.html", "obsolete.html"]


# LLM: Parent examples like "禁止改名（如 x.html）" must not become required deliverables.
# 函数用途: 防止 root 用括号举 forbidden 文件名反例时，把 product.html/legacy.html 误传成下级必需文件。
def test_file_contract_treats_negative_examples_as_forbidden_terms():
    text = (
        "必须包含 index.html、products.html、product-detail.html、style.css、app.js。"
        "文件名禁止改名（如 product.html、old-detail.html、legacy.html 等均不允许）。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js")

    assert required == ["index.html", "products.html", "product-detail.html", "style.css", "app.js"]
    assert forbidden == ["product.html", "old-detail.html", "legacy.html"]


# LLM: R41 exposed bare negative targets after colon/list wording.
# 函数用途: 防止“禁止 product.html / 禁止 output.json”这种裸禁止写法再次进入 required_files。
def test_file_contract_treats_bare_negative_targets_as_forbidden_terms():
    text = (
        "必须包含 index.html、register.html、login.html、products.html、product-detail.html、"
        "cart.html、checkout.html、order-success.html、style.css、app.js。"
        "文件名禁止改名：不允许 product.html/old-product.html/legacy.html。"
        "不允许在 build 目录写 output.json、RUNNER_RESULT.md、execution_context.json 或其他内部文件。"
        "不允许把 style.css/app.js 放进 css/ 或 js/ 子目录。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == [
        "index.html",
        "register.html",
        "login.html",
        "products.html",
        "product-detail.html",
        "cart.html",
        "checkout.html",
        "order-success.html",
        "style.css",
        "app.js",
    ]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: R59 used "禁止创建：" and polluted descendant required_files.
# 函数用途: 确认“禁止创建：a.html、b.json”整段只进入 forbidden_files，不能混入必需产物。
def test_file_contract_treats_forbidden_create_label_as_forbidden_terms():
    text = (
        "必须包含 index.html、register.html、login.html、products.html、product-detail.html、"
        "cart.html、checkout.html、order-success.html、style.css、app.js。"
        "禁止创建：product.html、old-product.html、legacy.html、obsolete.html、"
        "output.json、RUNNER_RESULT.md、execution_context.json。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == [
        "index.html",
        "register.html",
        "login.html",
        "products.html",
        "product-detail.html",
        "cart.html",
        "checkout.html",
        "order-success.html",
        "style.css",
        "app.js",
    ]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: R42 exposed the natural Chinese label "禁止文件名：..." as another forbidden-list form.
# 函数用途: 防止父级/子级用“禁止文件名”列反例时，下层又把反例当 required_files。
def test_file_contract_treats_forbidden_filename_label_as_forbidden_terms():
    text = (
        "核心文件（必须命名完全一致）：index.html、register.html、login.html、products.html、"
        "product-detail.html、cart.html、checkout.html、order-success.html、style.css、app.js。"
        "禁止文件名：product.html/old-product.html/legacy.html/obsolete.html。"
        "禁止在 build 写 output.json/RUNNER_RESULT.md/execution_context.json。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == [
        "index.html",
        "register.html",
        "login.html",
        "products.html",
        "product-detail.html",
        "cart.html",
        "checkout.html",
        "order-success.html",
        "style.css",
        "app.js",
    ]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: R46 showed root-seed machine inheritance labels must stay parseable as forbidden files.
# 函数用途: 防止“用户原始禁止文件/反例名（禁止创建...）：...”补块被 context bundle 当成 required_files。
def test_file_contract_treats_root_seed_forbidden_inheritance_block_as_forbidden_terms():
    text = (
        "必须文件：index.html、products.html、product-detail.html、style.css、app.js。\n"
        "用户原始禁止文件/反例名（禁止创建，不得当成 required_files）："
        "product.html、old-product.html、legacy.html、obsolete.html、"
        "output.json、RUNNER_RESULT.md、execution_context.json"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == ["index.html", "products.html", "product-detail.html", "style.css", "app.js"]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: R47 exposed inherited forbidden headings followed by bullet files.
# 函数用途: 防止“父级禁止文件/反例名：\n- product.html”这类继承块同时污染 required_files。
def test_file_contract_carries_negative_header_into_bulleted_forbidden_terms():
    text = (
        "父级必需文件/产物名（structured required_files，必须原样传给下一层）：\n"
        "- index.html\n"
        "- product-detail.html\n"
        "- style.css\n"
        "父级禁止文件/反例名（structured forbidden_files，不得创建，不得当成 required_files）：\n"
        "- output.json\n"
        "- RUNNER_RESULT.md\n"
        "- product.html\n"
        "- old-product.html\n"
        "- legacy.html\n"
        "- obsolete.html\n"
        "- execution_context.json\n"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == ["index.html", "product-detail.html", "style.css"]
    assert forbidden == [
        "output.json",
        "RUNNER_RESULT.md",
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
        "execution_context.json",
    ]


# LLM: R49 showed positive file labels can contain "禁止改名" without becoming forbidden lists.
# 函数用途: “必须文件（禁止改名）：index.html...”表示必需且不能改名，不表示这些文件禁止创建。
def test_file_contract_keeps_required_files_when_positive_label_says_no_rename():
    text = (
        "必须文件（禁止改名）：index.html, register.html, login.html, products.html, "
        "product-detail.html, cart.html, checkout.html, order-success.html, style.css, app.js\n"
        "禁止：product.html/old-product.html/legacy.html\n"
        "禁止写 output.json/RUNNER_RESULT.md/execution_context.json"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == [
        "index.html",
        "register.html",
        "login.html",
        "products.html",
        "product-detail.html",
        "cart.html",
        "checkout.html",
        "order-success.html",
        "style.css",
        "app.js",
    ]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: R50 showed acceptance checks can state forbidden files as "无 forbidden_files(...)".
# 函数用途: 验收项里的“无 forbidden_files/无内部文件污染”不能把反例文件加入 required_files。
def test_file_contract_treats_no_forbidden_files_acceptance_as_forbidden_terms():
    text = (
        "必须包含 index.html、products.html、product-detail.html、style.css、app.js。\n"
        "无 forbidden_files（product.html/old-product.html/legacy.html/obsolete.html）\n"
        "无内部文件污染（output.json/RUNNER_RESULT.md/execution_context.json）"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == ["index.html", "products.html", "product-detail.html", "style.css", "app.js"]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: R51 showed "forbid putting style.css in a subdir" is a placement rule, not a forbidden filename.
# 函数用途: 防止验收项里的“禁止 style.css/app.js 放进子目录”把两个必需资源误标成 forbidden_files。
def test_file_contract_keeps_required_files_when_forbidden_location_mentions_them():
    text = (
        "必须交付 index.html/register.html/login.html/products.html/product-detail.html/"
        "cart.html/checkout.html/order-success.html/style.css/app.js 共10个文件\n"
        "禁止 style.css/app.js 放进 css/ 或 js/ 子目录\n"
        "禁止写 output.json/RUNNER_RESULT.md/execution_context.json"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == [
        "index.html",
        "register.html",
        "login.html",
        "products.html",
        "product-detail.html",
        "cart.html",
        "checkout.html",
        "order-success.html",
        "style.css",
        "app.js",
    ]
    assert forbidden == ["output.json", "RUNNER_RESULT.md", "execution_context.json"]


# LLM: R51 also showed parent goals can label forbidden files with parentheses instead of colons.
# 函数用途: 防止“禁止文件名（a.html/b.html）”和“禁止内部文件（output.json）”里的反例进入 required_files。
def test_file_contract_treats_parenthesized_forbidden_labels_as_forbidden_terms():
    text = (
        "必须遵守：10个精确文件名（index.html/register.html/login.html/products.html/"
        "product-detail.html/cart.html/checkout.html/order-success.html/style.css/app.js），"
        "禁止文件名（product.html/old-product.html/legacy.html/obsolete.html），"
        "禁止内部文件（output.json/RUNNER_RESULT.md/execution_context.json）"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == [
        "index.html",
        "register.html",
        "login.html",
        "products.html",
        "product-detail.html",
        "cart.html",
        "checkout.html",
        "order-success.html",
        "style.css",
        "app.js",
    ]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: R52 showed model summaries often shorten "禁止写 output.json" to "不写output.json".
# 函数用途: 防止 root 摘要里的“不写output.json/RUNNER_RESULT.md”丢出 forbidden_files 机器合同。
def test_file_contract_treats_no_write_short_negative_as_forbidden_terms():
    text = (
        "【必须交付的文件】index.html、products.html、product-detail.html、style.css、app.js。\n"
        "【产出约束】- build目录写产物，不写output.json/RUNNER_RESULT.md等内部文件。\n"
        "不创建 legacy.html，不生成 obsolete.html，不产出 debug.json，不包含 draft.md。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == ["index.html", "products.html", "product-detail.html", "style.css", "app.js"]
    assert forbidden == [
        "output.json",
        "RUNNER_RESULT.md",
        "legacy.html",
        "obsolete.html",
        "debug.json",
        "draft.md",
    ]


# LLM: R54 showed negative headings may be followed by a plain filename line, not bullets.
# 函数用途: 防止“禁止创建文件（forbidden_files）：\nproduct.html...”这种格式污染 required_files。
def test_file_contract_carries_negative_header_into_plain_file_list_line():
    text = (
        "必须包含文件：index.html、products.html、product-detail.html、style.css、app.js。\n"
        "禁止创建文件（forbidden_files）：\n"
        "product.html、old-product.html、legacy.html、obsolete.html、output.json、"
        "RUNNER_RESULT.md、execution_context.json\n"
        "你需要继续创建 depth=2 的 child coordinator。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == ["index.html", "products.html", "product-detail.html", "style.css", "app.js"]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: R57 showed bracket-only Chinese labels can omit the colon before forbidden examples.
# 函数用途: 防止 `【禁止文件名】product.html/...` 这类 root goal 把 forbidden examples 当 required。
def test_file_contract_treats_bracket_forbidden_labels_as_forbidden_terms():
    text = (
        "【必须完成】交付文件：\n"
        "- index.html\n"
        "- register.html\n"
        "- style.css\n"
        "- app.js\n"
        "【禁止文件名】product.html/old-product.html/legacy.html/obsolete.html\n"
        "【禁止内部文件】禁止在 build 写 output.json/RUNNER_RESULT.md/execution_context.json"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == ["index.html", "register.html", "style.css", "app.js"]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: R58 showed final required filenames may be followed by sentence punctuation.
# 函数用途: 必需文件列表最后的 app.js. 要识别为 app.js，但不能把 app.js.map 截断成 app.js。
def test_file_contract_keeps_required_filename_before_sentence_period():
    text = (
        "Required files (exact names only): index.html, register.html, login.html, products.html, "
        "product-detail.html, cart.html, checkout.html, order-success.html, style.css, app.js. "
        "Do not treat app.js.map as the deliverable."
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js")

    assert required == [
        "index.html",
        "register.html",
        "login.html",
        "products.html",
        "product-detail.html",
        "cart.html",
        "checkout.html",
        "order-success.html",
        "style.css",
        "app.js",
    ]


# LLM: R67 showed Markdown negative headings can still pollute inherited required_files.
# 函数用途: `## 禁止文件` 后面的文件名必须只进入 forbidden_files，不进入 required_files。
def test_file_contract_treats_markdown_forbidden_heading_as_negative_scope():
    text = (
        "## 必须文件（共10个）\n"
        "index.html, register.html, login.html, products.html, product-detail.html, "
        "cart.html, checkout.html, order-success.html, style.css, app.js\n\n"
        "## 禁止文件\n"
        "product.html, old-product.html, legacy.html, obsolete.html, "
        "output.json, RUNNER_RESULT.md, execution_context.json\n"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == [
        "index.html",
        "register.html",
        "login.html",
        "products.html",
        "product-detail.html",
        "cart.html",
        "checkout.html",
        "order-success.html",
        "style.css",
        "app.js",
    ]
    assert forbidden == [
        "product.html",
        "old-product.html",
        "legacy.html",
        "obsolete.html",
        "output.json",
        "RUNNER_RESULT.md",
        "execution_context.json",
    ]


# LLM: task.json is an internal state reference unless a positive deliverable label owns it.
# 函数用途: 验收/看板语境里的 `task.json 里的状态` 不能变成下级必须创建的产物。
def test_file_contract_ignores_internal_state_file_references_without_deliverable_label():
    text = (
        "必须包含 index.html、style.css、app.js。\n"
        "只有所有真实 task.json 里的 root/child/grandchild 状态一致时，才能说完整通过。\n"
        "父级状态报告会读取 execution_context.json refs，但这些不是用户产物。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js|json|md")

    assert required == ["index.html", "style.css", "app.js"]
