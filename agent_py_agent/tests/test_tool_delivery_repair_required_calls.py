from __future__ import annotations

import json


# LLM: generated_rows repair skeletons must be directly executable by write_structured_json.
# 函数用途: 验证大表返工时，shape hint 会拆成 data/generated_rows 参数，而不是塞进 data 里让模型猜。
def test_required_tool_calls_expand_generated_rows_shape_hint() -> None:
    from agent_py_agent.agent.agent_core.tool_delivery_repair_required_calls import (
        required_tool_calls,
    )

    hint = {
        "data": {"completion_evidence": {"scope": "synthetic_analysis_dataset", "row_count": 1000}},
        "generated_rows": {
            "count": 1000,
            "columns": ["订单ID", "月份", "地区", "品类", "销售额", "利润"],
            "fields": {
                "订单ID": {"format": "ORD-{index:04d}", "start": 1},
                "月份": {"cycle": ["2026-01", "2026-02"]},
                "地区": {"cycle": ["华东", "华南"]},
                "品类": {"cycle": ["电子产品", "服装"]},
                "销售额": {"number": {"start": 1000, "step": 73}},
                "利润": {"multiply": {"source": "销售额", "factor": 0.22, "decimals": 2}},
            },
            "sheets": {"count": 3, "prefix": "原始数据"},
        },
    }

    calls = required_tool_calls(
        [
            {
                "recommended_action": "repair_structured_checkpoint_json",
                "checkpoint_ref": "outputs/data_analysis/source_data.json",
                "checkpoint_shape_hint": json.dumps(hint, ensure_ascii=False),
                "writer_tool": "write_structured_json",
                "write_tools": ["write_structured_json", "api_json_collection"],
            }
        ]
    )

    assert calls == [
        {
            "tool": "write_structured_json",
            "path": "outputs/data_analysis/source_data.json",
            "data": hint["data"],
            "generated_rows": hint["generated_rows"],
        }
    ]
