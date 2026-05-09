"""Tests for the parent-owned shop webapp oracle."""

import subprocess

from agent_py_agent.agent.subagents.shop_web_parent_oracle import (
    ShopWebParentOracleRequest,
    write_shop_web_parent_oracle,
)


def test_shop_web_parent_oracle_passes_on_complete_fixture(tmp_path):
    """LLM: The parent oracle should accept a complete static shopping flow fixture."""

    webapp = tmp_path / "shop-webapp"
    _write_shop_fixture(webapp, broken_image=False, missing_handler=False)

    pack_path = write_shop_web_parent_oracle(
        ShopWebParentOracleRequest(reports_dir=tmp_path / "reports", webapp_dir=webapp)
    )
    completed = subprocess.run(
        ["python3", "-m", "pytest", "tests/parent/test_shop_contract.py", "-q"],
        cwd=webapp,
        capture_output=True,
        text=True,
        check=False,
    )

    assert pack_path.is_file()
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_shop_web_parent_oracle_catches_broken_image_and_button_handler(tmp_path):
    """LLM: The parent oracle should expose broken image refs and unhandled data-action buttons."""

    webapp = tmp_path / "shop-webapp"
    _write_shop_fixture(webapp, broken_image=True, missing_handler=True)
    write_shop_web_parent_oracle(
        ShopWebParentOracleRequest(reports_dir=tmp_path / "reports", webapp_dir=webapp)
    )

    completed = subprocess.run(
        ["python3", "-m", "pytest", "tests/parent/test_shop_contract.py", "-q"],
        cwd=webapp,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    combined = completed.stdout + completed.stderr
    assert "missing image asset" in combined
    assert "button action has no JS handler" in combined


def _write_shop_fixture(webapp, *, broken_image: bool, missing_handler: bool) -> None:
    """LLM: Create a small but representative shopping-site fixture for oracle tests."""

    (webapp / "assets").mkdir(parents=True)
    (webapp / "src").mkdir()
    image_src = "assets/missing.svg" if broken_image else "assets/product.svg"
    if not broken_image:
        (webapp / "assets" / "product.svg").write_text("<svg></svg>\n", encoding="utf-8")
    actions = ["register", "login", "add-to-cart", "checkout", "place-order"]
    buttons = "\n".join(f'<button data-action="{action}">{action}</button>' for action in actions)
    (webapp / "index.html").write_text(
        f"""
        <html>
          <head><link rel="stylesheet" href="src/styles.css"><script defer src="src/app.js"></script></head>
          <body>
            <main id="app">
              <section id="register"></section>
              <section id="login"></section>
              <section id="catalog"><img src="{image_src}" alt="Product"></section>
              <section id="cart"></section>
              <section id="checkout"></section>
              <section id="order-confirmation"></section>
              {buttons}
            </main>
          </body>
        </html>
        """,
        encoding="utf-8",
    )
    handled = actions[:-1] if missing_handler else actions
    (webapp / "src" / "app.js").write_text(
        "\n".join(f"handleAction('{action}')" for action in handled),
        encoding="utf-8",
    )
    (webapp / "src" / "styles.css").write_text(
        ".product-grid { display: grid; grid-template-columns: repeat(3, 1fr); }\n"
        "@media (max-width: 720px) { .product-grid { grid-template-columns: 1fr; } }\n",
        encoding="utf-8",
    )
