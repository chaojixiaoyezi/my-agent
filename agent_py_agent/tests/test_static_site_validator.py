"""LLM: focused tests for static-site closeout validation."""

from pathlib import Path

from agent_py_agent.agent.subagents.execution import TestExecutor
from agent_py_agent.tests.static_site_validator_fixtures import _write_site


def test_static_site_check_passes_valid_site(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": '<a href="items.html">Products</a><img src="https://example.com/p.png">',
            "items.html": '<a href="flow-a.html">Cart</a><button onclick="location.href=\'flow-a.html\'">Buy</button>',
            "flow-a.html": '<form action="flow-b.html"><button type="submit">Checkout</button></form>',
            "flow-b.html": '<a href="flow-done.html">Submit</a>',
            "flow-done.html": '<a href="items.html">Continue</a>',
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "shop site",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": [
                "index.html",
                "items.html",
                "flow-a.html",
                "flow-b.html",
                "flow-done.html",
            ],
        }
    )

    assert record.executed is True
    assert record.passed is True
    assert record.validation_method == "static_site_check"
    assert record.validation_result["broken_local_refs"] == []


def test_static_site_check_blocks_common_generated_site_failures(tmp_path):
    _write_site(
        tmp_path,
        {
            "login.html": '<button>孤立按钮</button><script>location.href = "product_list.html";</script>',
            "flow-done.html": '<a href="index.html">继续示例流程</a><main>${name}</main>',
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "broken shop site",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["items.html", "flow-done.html"],
        }
    )

    assert record.executed is True
    assert record.passed is False
    assert "missing_required_files=1" in record.error
    assert "placeholder_hits=1" in record.error
    assert "broken_local_refs=1" in record.error
    assert "inert_control_hits=1" in record.error
    assert record.validation_result["missing_required_files"] == ["items.html"]
    assert record.validation_result["placeholder_hits"] == ["flow-done.html"]


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


def test_static_site_check_blocks_missing_dom_id_targets_by_default(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": '<div id="homeProducts"></div><script src="app.js"></script>',
            "app.js": "document.getElementById('productGrid').innerHTML = '<p>条目</p>';",
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


def test_static_site_check_blocks_trailing_markup_after_html_close(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                "<!doctype html><html><head></head><body><main>OK</main></body></html>"
                "<section>late fragment</section></body></html>"
            ),
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "trailing html fragments",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html"],
            "require_complete_html": True,
        }
    )

    assert record.passed is False
    assert "index.html:duplicate_html_close" in record.validation_result["html_structure_hits"]
    assert "index.html:trailing_markup_after_html_close" in record.validation_result["html_structure_hits"]


def test_static_site_check_allows_javascript_template_literals(tmp_path):
    _write_site(
        tmp_path,
        {
            "flow-a.html": """
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
            "required_files": ["flow-a.html"],
        }
    )

    assert record.passed is True
    assert record.validation_result["placeholder_hits"] == []


def test_static_site_check_allows_dom_ids_declared_in_javascript_templates(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": '<html><body><div id="app"></div><script src="app.js"></script></body></html>',
            "app.js": """
                const html = `<button id="btn-submit">提交</button><div id="contact-error"></div>`;
                document.getElementById('app').innerHTML = html;
                document.getElementById('btn-submit').addEventListener('click', () => {
                  document.getElementById('contact-error').textContent = '';
                });
            """,
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "dynamic static app",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "app.js"],
        }
    )

    assert record.passed is True
    assert record.validation_result["missing_dom_id_hits"] == []


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


def test_static_site_check_allows_workspace_symlink_alias(tmp_path):
    from agent_py_agent.agent.subagents.static_site import run_static_site_check

    real_workspace = tmp_path / "real"
    real_workspace.mkdir()
    alias_workspace = tmp_path / "alias"
    alias_workspace.symlink_to(real_workspace, target_is_directory=True)
    _write_site(
        real_workspace,
        {
            "index.html": '<a href="items.html">Products</a>',
            "items.html": '<a href="index.html">Home</a>',
        },
    )
    record = run_static_site_check(
        {
            "name": "site",
            "validation_method": "static_site_check",
            "site_root": "site",
        },
        alias_workspace,
    )

    assert record.executed is True
    assert record.passed is True
