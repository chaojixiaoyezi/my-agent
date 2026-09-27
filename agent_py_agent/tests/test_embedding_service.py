"""嵌入服务合并（参数减量，2026-09-27）：记忆语义召回与工具语义检索共用 embedding_* 一组配置。

锁定：tool_embedding_* 四键删除、memory_embedding_* 改名为 embedding_*，旧键写在 YAML 里只告警并忽略，不转值；
两边的模型、端点与 key 链是同一条；两个功能开关 memory_semantic_recall / tool_vector_search_enabled 各管各的；
参数中心把 embedding_* 归到“模型请求”，安全等级与原来一致。_build_tool_embedder 此前没有任何测试。
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from agent_py_agent.agent.core import _build_memory_embedder, _build_tool_embedder
from agent_py_agent.agent.retrieval.embedding import MiniMaxEmbedder, OpenAICompatibleEmbedder
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry

_PACKAGED = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
_SERVICE = ("embedding_model", "embedding_api_base", "embedding_api_key", "embedding_api_key_env")
_OLD_KEYS = {
    "tool_embedding_model": '"tool-model"', "tool_embedding_api_base": '"https://tool.example/v1"',
    "tool_embedding_api_key": '"k-tool"', "tool_embedding_api_key_env": '"TOOL_KEY"',
    "memory_embedding_model": '"memory-model"', "memory_embedding_api_base": '"https://memory.example/v1"',
    "memory_embedding_api_key": '"k-memory"', "memory_embedding_api_key_env": '"MEMORY_KEY"',
}


def _endpoint(embedder) -> tuple[str, str, str, str]:
    return type(embedder).__name__, embedder._model, embedder._api_base, embedder._api_key


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)  # 断言只看配置，不读真实环境里的 key


@pytest.mark.parametrize("model,kind", [("text-embedding-3-small", OpenAICompatibleEmbedder), ("embo-01", MiniMaxEmbedder)])
def test_tool_and_memory_use_one_service(model, kind):
    config = AgentConfig(memory_semantic_recall=True, embedding_model=model, embedding_api_base="https://emb.example/v1",
                         embedding_api_key="k-emb", api_base="https://chat.example/v1", api_key="k-chat")
    status: dict[str, str] = {}
    memory, tool = _build_memory_embedder(config, diagnostics=status), _build_tool_embedder(config)

    assert isinstance(tool, kind) and status == {"state": "configured"}
    assert _endpoint(tool) == _endpoint(memory) == (kind.__name__, model, "https://emb.example/v1", "k-emb")


def test_blank_endpoint_and_key_fall_back_to_the_chat_model_for_both(monkeypatch):
    monkeypatch.setenv("EMB_KEY_T", "k-env")
    base = dict(memory_semantic_recall=True, embedding_model="m", api_base="https://chat.example/v1", api_key="k-chat")
    for config, key in ((AgentConfig(**base), "k-chat"), (AgentConfig(**base, embedding_api_key_env="EMB_KEY_T"), "k-env")):
        memory, tool = _build_memory_embedder(config), _build_tool_embedder(config)
        assert _endpoint(tool) == _endpoint(memory) == ("OpenAICompatibleEmbedder", "m", "https://chat.example/v1", key)


def test_the_two_switches_stay_independent():
    status: dict[str, str] = {}
    recall_off = AgentConfig(memory_semantic_recall=False, embedding_model="m")
    assert _build_memory_embedder(recall_off, diagnostics=status) is None and status == {"state": "disabled"}
    assert _build_tool_embedder(recall_off) is not None  # 只配模型、不开记忆召回：工具检索照样走语义

    tool_off = AgentConfig(memory_semantic_recall=True, tool_vector_search_enabled=False, embedding_model="m")
    assert _build_tool_embedder(tool_off) is None
    assert _build_memory_embedder(tool_off) is not None

    no_model = AgentConfig(memory_semantic_recall=True)
    assert _build_memory_embedder(no_model, diagnostics=status) is None
    assert status == {"state": "degraded", "error_code": "MEMORY_EMBEDDING_MODEL_MISSING"}
    assert _build_tool_embedder(no_model) is None  # 两边都没模型：不建客户端、不发请求


def test_old_keys_only_warn_and_never_reach_the_service(tmp_path):
    names = {item.name for item in fields(AgentConfig)}
    assert not names & set(_OLD_KEYS) and set(_SERVICE) <= names
    path = tmp_path / "agent_config.yaml"
    path.write_text("memory_semantic_recall: true\n" + "".join(f"{key}: {value}\n" for key, value in _OLD_KEYS.items()),
                    encoding="utf-8")

    config = load_config(path)

    for key in _OLD_KEYS:
        assert f"unknown config key: {key!r}; ignored" in config.config_warnings
    assert [getattr(config, key) for key in _SERVICE] == ["", "", "", ""]
    assert _build_tool_embedder(config) is None and _build_memory_embedder(config) is None


def test_packaged_yaml_and_registry_describe_the_shared_service():
    config = load_config(_PACKAGED)
    assert not [warning for warning in config.config_warnings if "embedding" in warning]
    assert [getattr(config, key) for key in _SERVICE] == [getattr(AgentConfig(), key) for key in _SERVICE]
    registry = parameter_registry()
    assert {key: registry[key].category for key in _SERVICE} == dict.fromkeys(_SERVICE, "模型请求")
    assert all(registry[key].description for key in _SERVICE)
    assert [registry[key].writable for key in _SERVICE] == [True, False, False, False]
    assert registry["embedding_api_key"].masked
    assert (registry["memory_semantic_recall"].category, registry["tool_vector_search_enabled"].category) == (
        "记忆与压缩", "工具")
