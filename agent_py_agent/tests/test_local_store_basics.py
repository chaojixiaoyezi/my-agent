"""LLM: Basic local store tests — records, search, timeline, memory, subagent and gateway indexing.

新手说明:
这个文件测试 LocalStore 的基础功能：记录的增删查、事件日志、
LIKE 回退搜索、时间线过滤，以及 JsonlMemory、子代理运行和
简单 Gateway 请求对 LocalStore 的索引联动。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    gateway_paths,
    submit_gateway_ask,
)
from agent_py_agent.agent.gateway_parts.io import gateway_response_path, read_json_file
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_request_path
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store import JsonlMemory
from agent_py_agent.agent.settings import AgentConfig


def test_jsonl_memory_can_backfill_existing_records():
    """LLM: Verify index_all backfills existing JSONL records into the owner read index.

    新手说明:
    测试在 JsonlMemory 已有数据时，调用 index_all 可以把旧记录补建到
    owner 读索引（788f9b847 起正式召回只读该索引）；LocalStore 只留旧副本删除
    与审计脱敏，不再写入记忆文本投影。
    """
    from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        memory_path = root / "memory.jsonl"
        JsonlMemory(memory_path).add("assistant", "旧 JSONL 记录也能补建索引。", kind="note")

        store = LocalStore(root / "local.db")
        memory = JsonlMemory(memory_path, local_store=store)
        assert memory.index_all() == 1
        coverage = MemoryReadIndex(memory_path).coverage()
        assert (coverage["status"], coverage["indexed_records"]) == ("complete", 1)
        assert store.stats()["record_count"] == 0
        assert memory.search("补建", top_k=1)[0].role == "assistant"


def test_gateway_request_indexes_logs_to_local_store():
    """LLM: Verify gateway request handling indexes into LocalStore.

    新手说明:
    测试 gateway 请求处理后会写入 gateway_request 类型的记录到
    LocalStore，且可通过 LocalStore 的 search 搜索到。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            memory_path="memory.jsonl",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        # 有 Home 时目录未选模型即未配置；离线 echo 后端须构造后显式注入，才在 Gateway 模型作用域里保留。
        agent.backend = get_backend("echo", cfg)
        paths = gateway_paths(agent)
        request_id, request_path, _ = submit_gateway_ask(
            paths,
            params=GatewayAskParams(
                prompt="gateway 日志测试：这条请求应该能被搜索到",
                save=False,
                agent=agent,
            ),
        )

        ensure_gateway_folders(paths)
        assert _process_gateway_request_path(agent, paths, request_path, "local-store-test")
        response = read_json_file(gateway_response_path(paths, request_path.stem))
        hits = agent.local_store.search("gateway 日志测试", source_type="gateway_request")

        assert response["ok"] is True
        assert request_id == response["id"]
        assert hits
        assert hits[0].source_id == request_id


def test_gateway_request_writes_runtime_fact_when_saved():
    """LLM: Verify saved gateway request writes a runtime fact source.

    新手说明:
    测试当 gateway 请求带 save=True 时，会写
    memory_archive/runtime_facts/<request_id>/task.json 作为 compact/resume 事实源。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        # 有 Home 时目录未选模型即未配置；离线 echo 后端须构造后显式注入，才在 Gateway 模型作用域里保留。
        agent.backend = get_backend("echo", cfg)
        paths = gateway_paths(agent)
        request_id, request_path, _ = submit_gateway_ask(
            paths,
            params=GatewayAskParams(
                prompt="gateway runtime fact 测试",
                save=True,
                agent=agent,
            ),
        )

        ensure_gateway_folders(paths)
        assert _process_gateway_request_path(agent, paths, request_path, "local-store-test")
        response = read_json_file(gateway_response_path(paths, request_path.stem))

        assert response["ok"] is True
        fact_path = (
            agent.home_paths.owner_home_dir
            / "memory_archive"
            / "runtime_facts"
            / request_id
            / "task.json"
        )
        assert fact_path.exists()
        payload = json.loads(fact_path.read_text(encoding="utf-8"))
        assert payload["request_id"] == request_id
        assert payload["runtime_progress"]["source"] == "gateway"


def test_gateway_request_can_override_resume_context():
    """LLM: Verify gateway request with resume_context=True injects auto-recovery context.

    新手说明:
    测试当 gateway 请求带 resume_context=True 时，响应中
    memory_resume_context_injected 为 True，并且 prompt 中
    包含 "### Auto Recovery Context" 段落。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        # 有 Home 时目录未选模型即未配置；离线 echo 后端须构造后显式注入，才在 Gateway 模型作用域里保留。
        agent.backend = get_backend("echo", cfg)
        agent.run("gateway README 恢复开关任务", save=True)
        paths = gateway_paths(agent)
        _, request_path, _ = submit_gateway_ask(
            paths,
            params=GatewayAskParams(
                prompt="继续 README",
                save=False,
                include_prompt=True,
                resume_context=True,
                agent=agent,
            ),
        )

        ensure_gateway_folders(paths)
        assert _process_gateway_request_path(agent, paths, request_path, "local-store-test")
        response = read_json_file(gateway_response_path(paths, request_path.stem))

        assert response["ok"] is True
        assert response["memory_resume_context_injected"] is True
        assert response["memory_resume_context_token_estimate"] > 0
        assert "### Auto Recovery Context" in response["prompt"]
