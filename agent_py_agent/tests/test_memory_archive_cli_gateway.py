from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    gateway_paths,
    gateway_response_path,
    log_gateway_payload,
)
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    append_snapshot,
)
from agent_py_agent.tests.test_memory_archive_cli import (
    _run_cli_json,
    _workspace,
    _write_config,
    _write_cross_day_gateway_fixture,
    _write_gateway_processing_and_done_files,
)


def test_memory_resume_cross_day_gateway_request_uses_response_fact_source(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(config_path.parent / "home"),
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        root,
    )
    request_path, response_path = _write_cross_day_gateway_fixture(agent)

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-resume",
        "gateway handoff",
        "--since",
        "2026-04-29",
        "--until",
        "2026-04-30",
    )

    assert code == 0
    assert {item["id"] for item in payload["archive_matches"]} == {
        "raw-gateway-cross-day-1",
        "snapshot-gateway-cross-day-1",
    }
    assert payload["task_fact_sources"] == []
    assert payload["gateway_fact_sources"][0]["request_id"] == "gwreq-cross-day"
    assert payload["gateway_fact_sources"][0]["response_path"] == str(response_path)
    assert str(request_path) in payload["resume"]["recommended_read_paths"]
    assert str(response_path) in payload["resume"]["recommended_read_paths"]
    assert payload["resume"]["gateway_fact_source_count"] == 1
    assert payload["brief"]["related_ids"]["request_ids"] == ["gwreq-cross-day"]
    assert "gateway handoff：继续昨天网关请求" in payload["brief"]["context_block"]
    assert str(response_path) in payload["brief"]["context_block"]

def _create_processing_done_fallback_agent(tmp_path) -> tuple[Path, SimpleAgent, str, Path, Path]:
    """Create agent and paths for the processing-fallback test."""
    config_path = _write_config(tmp_path)
    root = _workspace(config_path)
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(config_path.parent / "home"),
            model_backend="echo",
            subagent_workspace="subagents",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        root,
    )
    request_id = "gwreq-processing-moved"
    paths = gateway_paths(agent)
    return root, agent, request_id, paths, config_path

def test_memory_resume_gateway_processing_path_falls_back_to_done_request(tmp_path, capsys):
    root, agent, request_id, paths, config_path = _create_processing_done_fallback_agent(tmp_path)
    processing_path, done_path, response_path = _write_gateway_processing_and_done_files(
        agent, paths, request_id
    )

    code, payload = _run_cli_json(
        capsys,
        config_path,
        "memory-resume",
        "processing moved",
        "--request-id",
        request_id,
    )

    reads = payload["resume"]["recommended_read_paths"]
    assert code == 0
    assert str(done_path) in reads
    assert str(processing_path) not in reads
    assert payload["gateway_fact_sources"][0]["request_path"] == str(done_path)
