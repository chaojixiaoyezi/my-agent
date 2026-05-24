from __future__ import annotations

import json

from agent_py_agent.agent.tooling.api_json_collection_builder import build_checkpoint
from agent_py_agent.agent.tooling.api_json_collection_request import collection_request


def test_api_collection_leaves_llm_generated_fields_empty_without_mapping(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps([{"name": "repo", "url": "https://example.com/repo"}]), encoding="utf-8")
    request = collection_request(
        {
            "requests": [{"artifact_ref": str(source), "name": "Data", "source_id": "src-1"}],
            "columns": ["记录名", "地址", "中文说明", "说明依据"],
            "fields": {"记录名": {"path": "name"}, "地址": {"path": "url"}},
            "llm_generated_fields": ["中文说明", "说明依据"],
            "evidence_fields": ["记录名", "地址"],
        }
    )
    checkpoint = build_checkpoint(
        request,
        timeout=1,
        resolver=None,
        allowed_private_hosts=(),
        allow_private_resolution=True,
    )

    assert request["columns"] == ["记录名", "地址", "中文说明", "说明依据"]
    assert request["llm_generated_fields"] == ["中文说明", "说明依据"]
    assert "中文说明" not in request["fields"]
    row = checkpoint["sheets"][0]["rows"][0]
    assert row["记录名"] == "repo"
    assert row["中文说明"] == ""
    assert "中文说明" not in row["field_source_ids"]
