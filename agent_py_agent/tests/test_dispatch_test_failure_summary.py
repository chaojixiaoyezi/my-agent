"""LLM: Tests for compact acceptance-test failure summaries.

模块用途: 验证父级 dispatch 能拿到失败验收的具体可修线索，而不是只知道 failed=1。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.agent_core.dispatch_test_failure_summary import (
    acceptance_test_failure_payload,
)


# LLM: test_static_site_failure_details_expose_concrete_inert_controls protects repair prompts.
# 函数用途: static_site_check 失败时，父级工具返回应包含具体失效控件，方便重新派修复子代理。
def test_static_site_failure_details_expose_concrete_inert_controls(tmp_path):
    path = tmp_path / "test_execution.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "test_name": "inferred static site check",
                        "executed": True,
                        "exit_code": 1,
                        "error": "inert_control_hits=2",
                        "validation_result": {
                            "ok": False,
                            "inert_control_hits": [
                                "index2.html:a:Collection href=#",
                                "index2.html:a:Contact href=#missing",
                            ],
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = acceptance_test_failure_payload(path)

    assert payload["parent_acceptance_test_failure_summary"] == (
        "inferred static site check: inert_control_hits=2"
    )
    assert payload["parent_acceptance_test_failure_details"] == [
        "inert_control_hits: index2.html:a:Collection href=#; index2.html:a:Contact href=#missing",
    ]


# LLM: structure and repair hints should reach parent repair dispatch without reading page bodies.
# 函数用途: HTML 骨架坏掉时，dispatch payload 要告诉父级先修完整结构，而不是只给失败数量。
def test_static_site_failure_details_include_structure_and_repair_hints(tmp_path):
    path = tmp_path / "test_execution.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "test_name": "inferred static site check",
                        "executed": True,
                        "exit_code": 1,
                        "error": "html_structure_hits=2",
                        "validation_result": {
                            "ok": False,
                            "html_structure_hits": [
                                "index1.html:head_close",
                                "index1.html:unbalanced_style",
                            ],
                            "repair_hints": [
                                "html_structure: repair or regenerate a complete HTML skeleton before DOM/id fixes"
                            ],
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = acceptance_test_failure_payload(path)

    assert payload["parent_acceptance_test_failure_summary"] == (
        "inferred static site check: html_structure_hits=2"
    )
    assert payload["parent_acceptance_test_failure_details"] == [
        "html_structure_hits: index1.html:head_close; index1.html:unbalanced_style",
        "repair_hints: html_structure: repair or regenerate a complete HTML skeleton before DOM/id fixes",
    ]
