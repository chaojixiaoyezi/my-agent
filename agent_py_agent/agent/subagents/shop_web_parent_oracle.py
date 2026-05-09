# LLM: Parent-owned static shopping webapp contract generator for real shared-repo E2E.
# 模块用途: 给购物网站 E2E 写父级验收 pytest 和 parent_test_pack，检查按钮、图片、流程入口和布局线索。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .parent_test_pack import ParentTestPackWriteRequest, write_parent_test_pack

_SHOP_CONTRACT_SOURCE = '''from html.parser import HTMLParser
from pathlib import Path


REQUIRED_SECTIONS = {
    "register",
    "login",
    "catalog",
    "cart",
    "checkout",
    "order-confirmation",
}
REQUIRED_ACTIONS = {
    "register",
    "login",
    "add-to-cart",
    "checkout",
    "place-order",
}


class ShopParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.actions = set()
        self.images = []
        self.scripts = []
        self.stylesheets = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if values.get("data-action"):
            self.actions.add(values["data-action"])
        if tag == "img":
            self.images.append(values.get("src", ""))
        if tag == "script" and values.get("src"):
            self.scripts.append(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.stylesheets.append(values["href"])


def _parse_index(root):
    parser = ShopParser()
    parser.feed((root / "index.html").read_text(encoding="utf-8"))
    return parser


def _asset(root, value):
    return (root / value).resolve()


def test_required_shop_flow_sections_exist():
    root = Path.cwd()
    parser = _parse_index(root)
    missing = REQUIRED_SECTIONS - parser.ids
    assert not missing, f"missing required shop sections: {sorted(missing)}"


def test_images_and_local_assets_resolve():
    root = Path.cwd()
    parser = _parse_index(root)
    assert parser.images, "catalog must render product images"
    for src in parser.images:
        assert src and not src.startswith(("http://", "https://")), f"image must be local: {src}"
        assert _asset(root, src).is_file(), f"missing image asset: {src}"
    for src in [*parser.scripts, *parser.stylesheets]:
        assert _asset(root, src).is_file(), f"missing local asset: {src}"


def test_buttons_cover_purchase_flow_and_have_js_handlers():
    root = Path.cwd()
    parser = _parse_index(root)
    missing = REQUIRED_ACTIONS - parser.actions
    assert not missing, f"missing required button actions: {sorted(missing)}"
    js_text = "\\n".join((root / script).read_text(encoding="utf-8") for script in parser.scripts)
    for action in REQUIRED_ACTIONS:
        assert action in js_text, f"button action has no JS handler: {action}"


def test_layout_has_grid_and_responsive_breakpoint():
    root = Path.cwd()
    parser = _parse_index(root)
    css_text = "\\n".join((root / css).read_text(encoding="utf-8") for css in parser.stylesheets)
    assert "display: grid" in css_text or "display:grid" in css_text, "catalog layout should use a grid"
    assert "@media" in css_text, "site must include at least one responsive media query"
'''


# LLM: ShopWebParentOracleRequest bundles where the oracle test lives and how subagents-tests should run it.
# 类用途: 保存购物网站父级测试包写入上下文；共享仓库 E2E 只需要传 webapp 和 reports 目录。
@dataclass(frozen=True)
class ShopWebParentOracleRequest:
    """Bundle for writing a shop-webapp parent oracle."""

    __test__: ClassVar[bool] = False

    reports_dir: str | Path
    webapp_dir: str | Path
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: write_shop_web_parent_oracle writes a pytest contract plus matching parent_test_pack.
# 函数用途: 在 webapp 内写父级验收测试，并在 reports 目录登记 parent-owned test command。
def write_shop_web_parent_oracle(request: ShopWebParentOracleRequest) -> Path:
    """Write the shop webapp parent oracle and test pack."""

    webapp = Path(request.webapp_dir)
    test_path = webapp / "tests" / "parent" / "test_shop_contract.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(_SHOP_CONTRACT_SOURCE, encoding="utf-8")
    return write_parent_test_pack(
        ParentTestPackWriteRequest(
            reports_dir=request.reports_dir,
            tests=[{
                "name": "parent shop web contract",
                "validation_method": "command",
                "command": "python3 -m pytest tests/parent/test_shop_contract.py -q",
                "working_dir": str(webapp),
            }],
            source="shop_web_parent_oracle",
            reserved={
                "checks_buttons": True,
                "checks_image_refs": True,
                "checks_purchase_flow": True,
                "checks_responsive_layout": True,
                **dict(request.reserved),
            },
        )
    )

