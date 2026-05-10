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
            "order-success.html": '<a href="index.html">继续购物</a><script>const msg = `${name}`;</script>',
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
