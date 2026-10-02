"""P13：嵌入服务改引用固定模型档案（embedding_model_profile）。

锁定：四个平铺键 embedding_model / embedding_api_base / embedding_api_key / embedding_api_key_env 已删除，不留别名；
唯一入口 _embedding_client(agent) 按档案的服务商凭据与端点构建；空档案 = 不建客户端、只走关键词；
工具与记忆两个开关各自独立；参数中心把 embedding_model_profile 归为边界项（模型不能改，仅管理员 /settings）。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.core import (
    _build_memory_embedder,
    _build_tool_embedder,
    _embedding_client,
)
from agent_py_agent.agent.retrieval.embedding import MiniMaxEmbedder, OpenAICompatibleEmbedder
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.settings.parameter_registry import parameter_registry

_PACKAGED = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
# 平铺四键（P13 删除）+ 更早的 tool_/memory_ 前缀旧键：全部只告警，不转值。
_LEGACY_KEYS = {
    "tool_embedding_model": '"tool-model"', "tool_embedding_api_base": '"https://tool.example/v1"',
    "tool_embedding_api_key": '"k-tool"', "tool_embedding_api_key_env": '"TOOL_KEY"',
    "memory_embedding_model": '"memory-model"', "memory_embedding_api_base": '"https://memory.example/v1"',
    "memory_embedding_api_key": '"k-memory"', "memory_embedding_api_key_env": '"MEMORY_KEY"',
    "embedding_model": '"text-embedding-3-small"', "embedding_api_base": '"https://emb.example/v1"',
    "embedding_api_key": '"k-emb"', "embedding_api_key_env": '"EMB_KEY"',
}


def _endpoint(embedder) -> tuple[str, str, str, str]:
    return type(embedder).__name__, embedder._model, embedder._api_base, embedder._api_key


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    home = SimpleNamespace(root=tmp_path, config_dir=tmp_path / "config", owner_provider="local",
                           owner_kind="main", owner_id="main")
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "emb-test"\n', encoding="utf-8")
    config = load_config(path)
    return SimpleNamespace(home_paths=home, config=config)


def _add(host, **values):
    profile_id = str(uuid4())
    profile = {"model_name": "text-embedding-3-small", "model_backend": "openai_compatible",
               "api_base": "https://emb.example.test/v1", "api_key": "fake-emb-credential",
               "model_context_window_tokens": 32768, "capability": "embedding", **values}
    listing = execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": profile})
    return profile_id, listing


def _agent(host, **cfg):
    return SimpleNamespace(home_paths=host.home_paths, config=AgentConfig(**cfg))


@pytest.mark.parametrize("model,kind", [("text-embedding-3-small", OpenAICompatibleEmbedder), ("embo-01", MiniMaxEmbedder)])
def test_profile_drives_both_services(host, model, kind):
    profile_id, _ = _add(host, model_name=model)
    agent = _agent(host, memory_semantic_recall=True, embedding_model_profile=profile_id,
                   api_base="https://chat.example/v1", api_key="k-chat")
    status: dict[str, str] = {}
    memory, tool = _build_memory_embedder(agent, diagnostics=status), _build_tool_embedder(agent)

    assert isinstance(tool, kind) and status == {"state": "configured"}
    # 端点与凭据全部来自档案，绝不落到聊天模型上。
    assert _endpoint(tool) == _endpoint(memory) == (kind.__name__, model, "https://emb.example.test/v1", "fake-emb-credential")


def test_blank_profile_builds_nothing_and_never_sends(host):
    status: dict[str, str] = {}
    agent = _agent(host, memory_semantic_recall=True)
    assert _build_memory_embedder(agent, diagnostics=status) is None
    assert status == {"state": "degraded", "error_code": "MEMORY_EMBEDDING_MODEL_MISSING"}
    assert _build_tool_embedder(agent) is None  # 两边都没档案：不建客户端、不发请求
    assert _embedding_client(agent) is None


def test_the_two_switches_stay_independent(host):
    profile_id, _ = _add(host)
    status: dict[str, str] = {}
    recall_off = _agent(host, embedding_model_profile=profile_id)
    assert _build_memory_embedder(recall_off, diagnostics=status) is None and status == {"state": "disabled"}
    assert _build_tool_embedder(recall_off) is not None  # 只配档案、不开记忆召回：工具检索照样走语义

    tool_off = _agent(host, memory_semantic_recall=True, tool_vector_search_enabled=False,
                      embedding_model_profile=profile_id)
    assert _build_tool_embedder(tool_off) is None
    assert _build_memory_embedder(tool_off) is not None


def test_legacy_keys_only_warn_and_never_reach_the_service(tmp_path, host):
    from dataclasses import fields
    names = {item.name for item in fields(AgentConfig)}
    assert not names & set(_LEGACY_KEYS) and "embedding_model_profile" in names
    path = tmp_path / "agent_config.yaml"
    path.write_text("memory_semantic_recall: true\n" + "".join(f"{key}: {value}\n" for key, value in _LEGACY_KEYS.items()),
                    encoding="utf-8")

    config = load_config(path)

    for key in _LEGACY_KEYS:
        assert f"unknown config key: {key!r}; ignored" in config.config_warnings
    assert config.embedding_model_profile == ""
    agent = SimpleNamespace(home_paths=host.home_paths, config=config)
    assert _build_tool_embedder(agent) is None and _build_memory_embedder(agent) is None


def test_packaged_yaml_and_registry_describe_the_profile():
    config = load_config(_PACKAGED)
    assert not [warning for warning in config.config_warnings if "embedding" in warning]
    assert config.embedding_model_profile == AgentConfig().embedding_model_profile
    registry = parameter_registry()
    assert registry["embedding_model_profile"].category == "模型请求"
    assert registry["embedding_model_profile"].description
    assert registry["embedding_model_profile"].safety == "boundary"  # 边界项：模型不能改
    assert registry["embedding_model_profile"].writable is False
    assert (registry["memory_semantic_recall"].category, registry["tool_vector_search_enabled"].category) == (
        "记忆与压缩", "工具")