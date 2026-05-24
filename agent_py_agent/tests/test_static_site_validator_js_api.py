from __future__ import annotations

from agent_py_agent.agent.subagents.execution_executor import TestExecutor
from agent_py_agent.tests.static_site_validator_fixtures import _write_site


# LLM: generated apps often expose window.app methods that inline handlers call.
# 函数用途: 验证 onclick/app.js 模板调用未导出的 app 方法时，静态验收能拦住假可用流程。
def test_static_site_check_blocks_missing_window_app_methods(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                '<button onclick="app.showCart()">流程状态</button>'
                '<script src="app.js"></script>'
            ),
            "app.js": "function showProducts(){} window.app = { showProducts };",
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "missing app method",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "app.js"],
        }
    )

    assert record.passed is False
    assert "missing_js_api_hits=1" in record.error
    assert record.validation_result["missing_js_api_hits"] == ["app.showCart"]


# LLM: inline handlers can reference a top-level lexical app object in normal browser scripts.
# 函数用途: 验证 validator 不强迫站点必须写 window.app，只要结构化 app 对象导出对应方法即可。
def test_static_site_check_accepts_top_level_app_object_methods(tmp_path):
    _write_site(
        tmp_path,
        {
            "index.html": (
                '<button onclick="app.showCart()">流程状态</button>'
                '<script src="app.js"></script>'
            ),
            "app.js": "function showCart(){} const app = { showCart };",
        },
    )
    executor = TestExecutor(tmp_path)

    record = executor.execute(
        {
            "name": "top level app object",
            "validation_method": "static_site_check",
            "site_root": "site",
            "required_files": ["index.html", "app.js"],
        }
    )

    assert record.passed is True
    assert record.validation_result["missing_js_api_hits"] == []
