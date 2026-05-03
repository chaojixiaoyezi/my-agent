from __future__ import annotations

"""gateway client regression tests."""

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    gateway_paths,
    gateway_response_path,
    read_json_file,
    write_gateway_request,
)
from agent_py_agent.agent.gateway_parts import runtime as gateway_runtime
from agent_py_agent.cli import gateway_client


def test_default_gateway_entry_can_reach_chat_handler():
    """默认 gateway 入口必须能找到 chat 处理函数。"""

    assert callable(gateway_client.cmd_chat)


def test_gateway_worker_continues_when_processing_lease_write_fails(tmp_path, monkeypatch):
    """A lease file write failure must not strand a user request in processing."""

    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )
    paths = gateway_paths(agent)
    request_id = "gwreq-lease-fallback"
    write_gateway_request(
        paths,
        {
            "id": request_id,
            "kind": "ask",
            "prompt": "lease fallback should still answer",
            "inject": [],
            "prompt_files": [],
            "save": False,
            "include_prompt": False,
            "created_at": 1.0,
            "status": "pending",
            "attempts": 0,
        },
    )

    def fail_processing_lease_write(path, payload):
        if path.name == f"{request_id}.json":
            raise OSError("simulated long-path lease write failure")
        return None

    monkeypatch.setattr(gateway_runtime, "write_json_file_atomic", fail_processing_lease_write)

    processed = gateway_runtime._process_gateway_requests(agent, paths)

    assert processed == 1
    assert gateway_response_path(paths, request_id).exists()
    assert (paths.done / f"{request_id}.json").exists()
    assert not (paths.processing / f"{request_id}.json").exists()
    assert read_json_file(gateway_response_path(paths, request_id))["ok"] is True
