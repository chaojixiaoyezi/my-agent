"""LLM: focused tests for static-site parent acceptance validation."""

from pathlib import Path

from agent_py_agent.agent.subagents.execution_executor import TestExecutor


# LLM: _write_site creates a tiny static site fixture inside the pytest tmp workspace.
# 函数用途: 生成静态页面文件，方便测试 static_site_check 对必需文件、链接和占位符的判断。
def _write_site(root: Path, files: dict[str, str]) -> Path:
    site = root / "site"
    for rel_path, content in files.items():
        path = site / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return site


# LLM: The happy path proves local links, remote images, buttons, and required files can pass together.
# 函数用途: 验证完整静态站点检查通过时，执行记录是 static_site_check 且 validation_result.ok 为真。
def test_static_site_check_passes_valid_site(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": '<a href="products.html">Products</a><img src="https://example.com/p.png">',
            "products.html": '<a href="cart.html">Cart</a><button onclick="location.href=\'cart.html\'">Buy</button>',
            "cart.html": '<form action="checkout.html"><button type="submit">Checkout</button></form>',
            "checkout.html": '<a href="order-success.html">Submit</a>',
            "order-success.html": '<a href="products.html">Continue</a>',
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "shop site",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "products.html", "cart.html", "checkout.html", "order-success.html"],
        }
    )

    assert record.executed is True
    assert record.passed is True
    assert record.validation_method == "static_site_check"
    assert record.validation_result["broken_local_refs"] == []


# LLM: This regression captures the R11 failure shape from the real shopping-site E2E.
# 函数用途: 缺失页面、本地坏链接、`${...}` 占位符和无动作按钮都必须让静态站点验收失败。
def test_static_site_check_blocks_common_generated_site_failures(tmp_path):
    _write_site(
        tmp_path,
        {
            "login.html": '<button>孤立按钮</button><script>location.href = "product_list.html";</script>',
            "order-success.html": '<a href="index.html">继续购物</a><main>${name}</main>',
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "broken shop site",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["products.html", "order-success.html"],
        }
    )

    assert record.executed is True
    assert record.passed is False
    assert "missing_required_files=1" in record.error
    assert "placeholder_hits=1" in record.error
    assert "broken_local_refs=1" in record.error
    assert "inert_control_hits=1" in record.error
    assert record.validation_result["missing_required_files"] == ["products.html"]
    assert record.validation_result["placeholder_hits"] == ["order-success.html"]


# LLM: Placeholder hash links from real E2E must fail when pages claim buttons/links work.
# 函数用途: 家具网站真实测试生成 href="#" 后，静态验收要把这类假链接当成失效控件。
def test_static_site_check_blocks_placeholder_hash_links(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                '<main id="home">Home</main>'
                '<a href="#home">真实锚点</a>'
                '<a href="#" class="cta">查看详情</a>'
                '<a href="#missing">缺失锚点</a>'
            ),
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "no dead links",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html"],
        }
    )

    assert record.executed is True
    assert record.passed is False
    assert "inert_control_hits=2" in record.error
    assert record.validation_result["inert_control_hits"] == [
        "index.html:a:查看详情",
        "index.html:a:缺失锚点",
    ]


# LLM: Leaf acceptance must not fail because sibling pages in the same deliverables folder are still broken.
# 函数用途: 单个 worker 只负责 index1.html 时，static_site_check 可以限定检查文件，避免 sibling 串扰。
def test_static_site_check_can_scope_to_declared_html_files(tmp_path):
    _write_site(
        tmp_path,
        {
            "index1.html": '<main id="home">Home</main><a href="#home">真实锚点</a>',
            "index2.html": '<a href="#">坏 sibling</a>',
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "single leaf page",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index1.html"],
            "html_files": ["index1.html"],
        }
    )

    assert record.executed is True
    assert record.passed is True
    assert record.validation_result["checked_files"] == ["index1.html"]


# LLM: R59 shopping E2E generated loginForm/registerForm but JS bound login-form/register-form.
# 函数用途: 父级静态验收要能发现表单 id 和本地 app.js 绑定目标不一致，避免按钮假可用。
def test_static_site_check_blocks_missing_validate_form_targets(tmp_path):
    _write_site(
        tmp_path,
        {
            "login.html": '<form id="loginForm"><button type="submit">登录</button><script src="app.js"></script>',
            "app.js": "document.addEventListener('DOMContentLoaded',()=>{validateForm('login-form')});",
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "broken form binding",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["login.html", "app.js"],
        }
    )

    assert record.executed is True
    assert record.passed is False
    assert "form_binding_hits=1" in record.error
    assert record.validation_result["form_binding_hits"] == ["validateForm:login-form"]


# LLM: external app.js listeners should count as real button behavior.
# 函数用途: 本地脚本里绑定按钮事件时，静态验收不能只因为 HTML 没有 onclick 就误报 inert control。
def test_static_site_check_allows_external_script_button_handlers(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": '<button id="sendBtn">发送</button><script src="app.js"></script>',
            "app.js": "document.getElementById('sendBtn').addEventListener('click', handleSend);",
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "external button handler",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "app.js"],
        }
    )

    assert record.passed is True
    assert record.validation_result["inert_control_hits"] == []


# LLM: DOM id binding mismatches catch generated buttons that look clickable but break at runtime.
# 函数用途: app.js 读取不存在的按钮 id 时，父级静态验收要失败并给出具体缺失 id。
def test_static_site_check_blocks_missing_dom_id_targets(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": '<button id="sendBtn">发送</button><script src="app.js"></script>',
            "app.js": (
                "document.getElementById('sendBtn').addEventListener('click', handleSend);"
                "document.getElementById('retry-btn').style.display = 'none';"
            ),
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "missing dom id",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "app.js"],
        }
    )

    assert record.passed is False
    assert "missing_dom_id_hits=1" in record.error
    assert record.validation_result["missing_dom_id_hits"] == ["getElementById:retry-btn"]


# LLM: test_static_site_check_allows_javascript_template_literals preserves real shop pages.
# 函数用途: JS 运行时模板字符串可以包含 `${...}`，但不应被当成未替换的 HTML 占位符。
def test_static_site_check_allows_javascript_template_literals(tmp_path):
    _write_site(
        tmp_path,
        {
            "cart.html": """
            <html>
              <body>
                <div id="cart"></div>
                <script>
                  const row = `<tr data-index="${index}"><td>${item.name}</td></tr>`;
                  document.getElementById('cart').innerHTML = row;
                </script>
              </body>
            </html>
            """,
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "shop site",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["cart.html"],
        }
    )

    assert record.passed is True
    assert record.validation_result["placeholder_hits"] == []


# LLM: The validator must never scan outside the configured workspace.
# 函数用途: 验证 site_root 越界时返回未执行失败记录，而不是读取外部目录。
def test_static_site_check_rejects_outside_site_root(tmp_path):
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "outside",
            "validation_method": "static_site_check",
            "site_root": "../outside",
        }
    )

    assert record.executed is False
    assert record.passed is False
    assert record.error == "site_root 超出 workspace 边界"
