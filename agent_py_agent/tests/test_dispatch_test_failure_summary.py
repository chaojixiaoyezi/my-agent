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
                                "index2.html:a:Collection",
                                "index2.html:a:Contact",
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
        "inert_control_hits: index2.html:a:Collection; index2.html:a:Contact",
    ]
