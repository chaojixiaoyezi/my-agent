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
        "index.html:a:查看详情 href=#",
        "index.html:a:缺失锚点 href=#missing",
    ]


# LLM: Disabled HTML controls are generic inert UI, independent of the task domain.
# 函数用途: 固定静态网页产物的通用验收合同；页面打开时默认失效的控件不能通过。
def test_static_site_check_blocks_disabled_html_controls(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                '<button id="checkoutBtn" onclick="checkout()" disabled>去结算</button>'
                "<script>function checkout(){}</script>"
            ),
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "disabled checkout button",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html"],
        }
    )

    assert record.executed is True
    assert record.passed is False
    assert "inert_control_hits=1" in record.error
    assert record.validation_result["inert_control_hits"] == ["index.html:button:去结算 disabled"]


# LLM: CSS pseudo-classes and runtime JS disabled assignments are not initial disabled controls.
# 函数用途: 只拦截 HTML 初始 disabled 属性，不误伤样式选择器或运行时状态切换代码。
def test_static_site_check_ignores_css_and_runtime_disabled_mentions(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                "<style>button:disabled{opacity:.6}</style>"
                '<button id="checkoutBtn" onclick="checkout()">去结算</button>'
                "<script>function checkout(){document.getElementById('checkoutBtn').disabled=false}</script>"
            ),
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "runtime disabled state",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html"],
        }
    )

    assert record.passed is True
    assert record.validation_result["inert_control_hits"] == []


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


# LLM: optional DOM hooks should not create repair loops when a UI branch is absent by design.
# 函数用途: `const el = getElementById(...); el && ...` 是安全可选绑定，不能被误判成硬失败。
def test_static_site_check_allows_optional_missing_dom_binding(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                "<main id='home'>Home</main>"
                "<script>const v=document.getElementById('view-btn');v&&v.addEventListener('click',()=>{});</script>"
            ),
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "optional dom hook",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html"],
        }
    )

    assert record.passed is True
    assert record.validation_result["missing_dom_id_hits"] == []


# LLM: Strict DOM mode still respects explicit optional hooks; required ids are declared separately.
# 函数用途: 验证 strict_dom_bindings 不会把 `v&&...` 这种安全可选 hook 误判成硬失败。
def test_static_site_check_strict_mode_allows_optional_missing_dom_binding(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                "<main id='home'>Home</main>"
                "<script>const v=document.getElementById('view-btn');v&&v.addEventListener('click',()=>{});</script>"
            ),
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "strict optional dom hook",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html"],
            "strict_dom_bindings": True,
        }
    )

    assert record.passed is True
    assert record.validation_result["missing_dom_id_hits"] == []


# LLM: Grouped null checks such as `if (a && b)` should also count as guarded optional hooks.
# 函数用途: 避免真实生成站点里可选移动菜单 hook 被 strict DOM 检查误判。
def test_static_site_check_allows_group_guarded_missing_dom_binding(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                "<main id='home'>Home</main><script>"
                "const menuBtn=document.getElementById('menu-toggle');"
                "const nav=document.querySelector('.nav-links');"
                "if(menuBtn&&nav){menuBtn.addEventListener('click',()=>nav.classList.toggle('open'));}"
                "</script>"
            ),
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "strict grouped optional dom hook",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html"],
            "strict_dom_bindings": True,
        }
    )

    assert record.passed is True
    assert record.validation_result["missing_dom_id_hits"] == []


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
    assert record.validation_result["repair_hints"] == [
        "missing_dom_ids: add the referenced id to a real element or remove the stale unguarded JS lookup"
    ]


# LLM: DOM binding checks should be on by default for generated apps, not a hidden expert option.
# 函数用途: 固定购物站真实测试暴露的问题；HTML/JS 的 id 不一致时默认验收失败。
def test_static_site_check_blocks_missing_dom_id_targets_by_default(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": '<div id="homeProducts"></div><script src="app.js"></script>',
            "app.js": "document.getElementById('productGrid').innerHTML = '<p>商品</p>';",
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "generated shop app",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "app.js"],
        }
    )

    assert record.passed is False
    assert "missing_dom_id_hits=1" in record.error
    assert record.validation_result["missing_dom_id_hits"] == ["getElementById:productGrid"]


# LLM: Explicit required DOM ids let parent acceptance preserve business flow contracts.
# 函数用途: 当任务声明必须存在某些页面区域时，static_site_check 要检查这些 id，而不是只看 HTML 结构。
def test_static_site_check_blocks_missing_required_dom_ids(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": "<!doctype html><html><head></head><body><section id='catalog'></section></body></html>",
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "required business sections",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html"],
            "required_dom_ids": ["register", "login", "catalog"],
        }
    )

    assert record.passed is False
    assert "missing_dom_id_hits=2" in record.error
    assert record.validation_result["missing_dom_id_hits"] == [
        "required_dom_id:login",
        "required_dom_id:register",
    ]


# LLM: inferred full-page checks must catch malformed HTML that browsers would render incorrectly.
# 函数用途: 完整 HTML 产物正文落进 style/head 时，父级验收应提示先修骨架，而不是只追 DOM id。
def test_static_site_check_blocks_malformed_complete_html(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                "<!DOCTYPE html><html><head><style>.btn{color:red}"
                "<main><button>Buy</button></main><script>console.log(1)</script></body></html>"
            ),
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "complete html shape",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html"],
            "require_complete_html": True,
        }
    )

    assert record.passed is False
    assert "html_structure_hits=" in record.error
    assert "index.html:head_close" in record.validation_result["html_structure_hits"]
    assert "index.html:body_open" in record.validation_result["html_structure_hits"]
    assert "index.html:unbalanced_style" in record.validation_result["html_structure_hits"]
    assert record.validation_result["repair_hints"][0] == (
        "html_structure: repair or regenerate a complete HTML skeleton before DOM/id fixes"
    )


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
