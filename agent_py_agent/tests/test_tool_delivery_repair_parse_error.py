from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_delivery_repair_guard import (
    is_delivery_repair_productive_call,
)


# LLM: parse-error calls are runtime recovery primitives, not ordinary inspection loops.
# 函数用途: 验证大结构化工具调用断裂后，delivery repair guard 不会拦住 __parse_error__ 的恢复上下文注入。
def test_delivery_repair_guard_allows_parse_error_recovery_in_strict_mode(tmp_path: Path):
    _write_closeout(
        tmp_path,
        {
            "ok": False,
            "delivery_progress": {
                "unchanged_failure_count": 9,
                "no_progress_block_threshold": 5,
                "recovery_actions": [
                    {
                        "code": "STAGED_JSON_NO_ROWS",
                        "recommended_action": "write_non_empty_structured_rows",
                        "checkpoint_ref": "outputs/report/source_data.json",
                        "writer_tool": "write_structured_json",
                    }
                ],
            },
        },
    )
    agent = SimpleNamespace(root=tmp_path)

    assert (
        is_delivery_repair_productive_call(
            agent,
            [
                {
                    "tool": "__parse_error__",
                    "error": "工具调用缺少结束标记 [/TOOL_CALL]",
                    "raw": '{"tool":"write_structured_json","path":"outputs/report/source_data.json","sheets":[{"rows":[',
                }
            ],
        )
        is True
    )


# LLM: _write_closeout keeps the fixture tied to the same machine report used at runtime.
# 函数用途: 写入 closeout.json，让测试直接走真实 guard 输入格式。
def _write_closeout(root: Path, payload: dict[str, object]) -> None:
    path = root / ".agent_delivery" / "closeout.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
