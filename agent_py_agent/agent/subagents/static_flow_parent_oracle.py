# LLM: Parent-owned static flow oracle generator for shared-repo E2E.
# 模块用途: 按结构化请求写父级验收 pytest 和 parent_test_pack，检查静态页面流程入口、资源引用和布局线索。

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .parent_test_pack import ParentTestPackWriteRequest, write_parent_test_pack

_STATIC_FLOW_CONTRACT_SOURCE = '''import json
from html.parser import HTMLParser
from pathlib import Path

CONTRACT = json.loads({contract_json_literal})


class StaticFlowParser(HTMLParser):
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
    parser = StaticFlowParser()
    parser.feed((root / "index.html").read_text(encoding="utf-8"))
    return parser


def _asset(root, value):
    return (root / value).resolve()


def _assert_inside_root(root, path, label):
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise AssertionError(f"{{label}} escapes webapp root: {{path}}") from exc


def test_required_flow_sections_exist():
    root = Path.cwd()
    parser = _parse_index(root)
    missing = set(CONTRACT["required_sections"]) - parser.ids
    assert not missing, f"missing required flow sections: {{sorted(missing)}}"


def test_local_assets_resolve():
    root = Path.cwd()
    parser = _parse_index(root)
    if CONTRACT["require_local_images"]:
        assert parser.images, "page must render declared local images"
    for src in parser.images:
        assert src and not src.startswith(("http://", "https://")), f"image must be local: {{src}}"
        asset_path = _asset(root, src)
        _assert_inside_root(root, asset_path, "image")
        assert asset_path.is_file(), f"missing image asset: {{src}}"
    for src in [*parser.scripts, *parser.stylesheets]:
        asset_path = _asset(root, src)
        _assert_inside_root(root, asset_path, "asset")
        assert asset_path.is_file(), f"missing local asset: {{src}}"


def test_required_actions_have_handlers():
    root = Path.cwd()
    parser = _parse_index(root)
    required_actions = set(CONTRACT["required_actions"])
    missing = required_actions - parser.actions
    assert not missing, f"missing required button actions: {{sorted(missing)}}"
    js_text = "\\n".join((root / script).read_text(encoding="utf-8") for script in parser.scripts)
    for action in required_actions:
        assert action in js_text, f"button action has no JS handler: {{action}}"


def test_layout_has_responsive_breakpoint():
    if not CONTRACT["require_responsive_layout"]:
        return
    root = Path.cwd()
    parser = _parse_index(root)
    css_text = "\\n".join((root / css).read_text(encoding="utf-8") for css in parser.stylesheets)
    assert "@media" in css_text, "site must include at least one responsive media query"
'''


# LLM: StaticFlowParentOracleRequest carries generic static-flow checks from structured contract data.
# 类用途: 保存静态页面父级验收测试的目录、必需 section/action 和资源/响应式检查开关。
@dataclass(frozen=True)
class StaticFlowParentOracleRequest:
    """Bundle for writing a generic static-flow parent oracle."""

    __test__: ClassVar[bool] = False

    reports_dir: str | Path
    webapp_dir: str | Path
    required_sections: tuple[str, ...] = ()
    required_actions: tuple[str, ...] = ()
    require_local_images: bool = False
    require_responsive_layout: bool = False
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: write_static_flow_parent_oracle writes a generic pytest contract plus parent_test_pack.
# 函数用途: 根据结构化请求生成父级验收测试，不在代码里内置业务流程名称。
def write_static_flow_parent_oracle(request: StaticFlowParentOracleRequest) -> Path:
    """Write the generic static-flow parent oracle and test pack."""

    webapp = Path(request.webapp_dir)
    test_path = webapp / "tests" / "parent" / "test_static_flow_contract.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(
        _STATIC_FLOW_CONTRACT_SOURCE.format(contract_json_literal=repr(_contract_json(request))),
        encoding="utf-8",
    )
    return write_parent_test_pack(
        ParentTestPackWriteRequest(
            reports_dir=request.reports_dir,
            tests=[{
                "name": "parent static flow contract",
                "validation_method": "command",
                "command": "python3 -m pytest tests/parent/test_static_flow_contract.py -q",
                "working_dir": str(webapp),
            }],
            source="static_flow_parent_oracle",
            reserved={
                "checks_actions": bool(request.required_actions),
                "checks_image_refs": request.require_local_images,
                "checks_responsive_layout": request.require_responsive_layout,
                "required_sections": list(request.required_sections),
                **dict(request.reserved),
            },
        )
    )


# LLM: _contract_json serializes only structured oracle options into generated pytest.
# 函数用途: 把必需 section/action 和检查开关写成 JSON 常量，避免生成测试依赖自然语言解释。
def _contract_json(request: StaticFlowParentOracleRequest) -> str:
    return json.dumps(
        {
            "required_sections": list(request.required_sections),
            "required_actions": list(request.required_actions),
            "require_local_images": bool(request.require_local_images),
            "require_responsive_layout": bool(request.require_responsive_layout),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


__all__ = ["StaticFlowParentOracleRequest", "write_static_flow_parent_oracle"]
