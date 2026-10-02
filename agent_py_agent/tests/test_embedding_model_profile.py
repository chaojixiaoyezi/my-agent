"""P13：嵌入服务引用固定模型档案（embedding_model_profile）的失败语义与管理员边界。

锁定：档案必须存在且具备 embedding 能力，否则按结构化 reason 降级（只走关键词），绝不回退到聊天模型；
空档案 = 不建客户端、不发请求；该键是边界项，模型/聊天动作改不了，只有管理员经 /settings 能改；
/settings show 展示编号、模型名或失效原因，不暴露连接字段。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent import core
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import settings_control_service as settings
from agent_py_agent.agent.retrieval import embedding as embedding_module
from agent_py_agent.agent.settings import model_profiles as model_profiles_module
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
)
from agent_py_agent.agent.settings.parameter_changes import (
    ChangeOrigin,
    WritePaths,
    reset_parameter,
    set_parameter,
)
from agent_py_agent.agent.settings.parameter_registry import parameter_registry

KEY = "embedding_model_profile"


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    home = SimpleNamespace(root=tmp_path, config_dir=tmp_path / "config", owner_provider="local",
                           owner_kind="main", owner_id="main")
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "emb-profile-test"\n', encoding="utf-8")
    config = load_config(path)
    return SimpleNamespace(home_paths=home, config=config)


def _add(host, **values):
    profile_id = str(uuid4())
    profile = {"model_name": "text-embedding-3-small", "model_backend": "openai_compatible",
               "api_base": "https://emb.example.test/v1", "api_key": "fake-emb-credential",
               "model_context_window_tokens": 32768, "capability": "embedding", **values}
    listing = execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": profile})
    return profile_id, listing


def _agent(host, profile_id):
    setattr(host.config, KEY, profile_id)
    host.config.memory_semantic_recall = True  # 走记忆语义构建路径
    return SimpleNamespace(home_paths=host.home_paths, config=host.config)


def _command(host, monkeypatch, text):
    monkeypatch.setattr(settings, "_scoped_home", lambda _agent, _scope: host.home_paths)
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.valid
    return settings.execute_settings_control(host, command, None)


@pytest.mark.parametrize("case,reason", [
    ("missing", "profile_not_found"), ("default", "profile_not_found"),
    ("capability", "capability_mismatch"), ("disabled", "profile_disabled"),
    ("provider_disabled", "provider_disabled"), ("provider_capability", "capability_mismatch"),
    ("credential", "credential_missing"), ("catalog", "catalog_invalid"),
])
def test_unusable_reference_is_typed_failure_with_reason(host, tmp_path, case, reason):
    from agent_py_agent.agent.core import _build_memory_embedder

    profile_id, _ = _add(host, capability="agentic" if case == "capability" else "embedding",
                         enabled=case != "disabled")
    path = model_profiles_path(host.home_paths)
    data = json.loads(path.read_text(encoding="utf-8"))
    provider = data["providers"][data["profiles"][profile_id]["provider_id"]]
    if case in {"provider_disabled", "provider_capability", "credential"}:
        provider.update(enabled=case != "provider_disabled",
                        capabilities=["agentic"] if case == "provider_capability" else ["embedding"],
                        api_key="" if case == "credential" else provider["api_key"])
        path.write_text(json.dumps(data), encoding="utf-8")
    if case == "catalog":
        path.write_text("broken", encoding="utf-8")
    reference = str(uuid4()) if case == "missing" else "default" if case == "default" else profile_id
    status: dict[str, str] = {}
    assert _build_memory_embedder(_agent(host, reference), diagnostics=status) is None
    assert status["error_code"] == "MEMORY_EMBEDDING_PROFILE_UNAVAILABLE"
    assert status["profile_id"] == reference and status["profile_reason"] == reason


def test_empty_reference_builds_nothing_and_never_sends(host):
    from agent_py_agent.agent.core import _build_memory_embedder

    host.config.memory_semantic_recall = True
    status: dict[str, str] = {}
    assert _build_memory_embedder(_agent(host, ""), diagnostics=status) is None
    assert status == {"state": "degraded", "error_code": "MEMORY_EMBEDDING_MODEL_MISSING"}


def test_admin_settings_writes_and_resets_but_model_actions_cannot(host, monkeypatch):
    profile_id, _ = _add(host)
    result = _command(host, monkeypatch, f"/settings set {KEY} {profile_id}")
    assert result.ok and "/restart" in result.message
    assert getattr(load_config(host.config.config_path), KEY) == profile_id
    assert parameter_registry()[KEY].writable is False
    for actor in ("model", "chat"):
        assert set_parameter(KEY, "", paths=WritePaths(user_path=host.config.config_path),
                             origin=ChangeOrigin(actor)).get("code") == "PARAMETER_BOUNDARY"
        assert reset_parameter(KEY, paths=WritePaths(user_path=host.config.config_path),
                               origin=ChangeOrigin(actor)).get("code") == "PARAMETER_BOUNDARY"
    assert _command(host, monkeypatch, f"/settings reset {KEY}").ok
    assert getattr(load_config(host.config.config_path), KEY) == ""


def test_non_admin_cannot_change_the_reference(host, monkeypatch):
    host.home_paths.owner_provider, host.home_paths.owner_kind, host.home_paths.owner_id = "feishu", "users", "test-user"
    result = _command(host, monkeypatch, f"/settings set {KEY} some-id")
    assert not result.ok and "管理员" in result.message


def test_settings_show_describes_profile_without_secrets(host, monkeypatch):
    profile_id, _ = _add(host, model_name="emb-profile-model")
    _command(host, monkeypatch, f"/settings set {KEY} {profile_id}")
    show = _command(host, monkeypatch, f"/settings show {KEY}")
    assert profile_id in show.message and "emb-profile-model" in show.message
    assert "fake-emb-credential" not in show.message  # 展示不含凭据
    bad = _command(host, monkeypatch, f"/settings show {KEY}")
    assert "不可用" not in bad.message


def test_semantic_settings_are_global_for_owner_scoped_agents(host):
    """S4 查清：embedding_model_profile 与 memory_semantic_recall 没有 owner 级覆盖——owner 池建作用域 agent 时
    只换 owner 三字段，其余原样继承 Gateway 配置。所以模型自配（manage_models 选向量模型）只对 local/main 开放。"""
    from agent_py_agent.agent.owner_scoped_pool import _config_with_owner

    host.config.embedding_model_profile, host.config.memory_semantic_recall = "admin-embo", True
    owner = SimpleNamespace(provider="feishu", owner_kind="user", owner_id="ou_member")
    scoped = _config_with_owner(host.config, owner)

    assert (scoped.embedding_model_profile, scoped.memory_semantic_recall) == ("admin-embo", True)
    assert scoped.my_agent_owner_id == "ou_member"


def test_ordinary_owner_cannot_resolve_the_admin_profile_and_never_reads_its_key(host, monkeypatch):
    """S4（用户 10-02 拍板）：全局档案编号指向管理员目录里的嵌入档案时，普通 owner 自己的目录里没有它：
    语义通道关闭、只走关键词，并带结构化诊断；全程只读普通 owner 自己的目录，不读管理员目录，也不建嵌入客户端，
    所以绝不会拿管理员的密钥去嵌普通用户的记忆。"""
    admin_profile, _listing = _add(host)  # 管理员（local/main）目录里的嵌入档案，带密钥
    member_home = SimpleNamespace(root=host.home_paths.root, config_dir=host.home_paths.config_dir,
                                  owner_provider="feishu", owner_kind="user", owner_id="ou_member")
    member = _agent(SimpleNamespace(home_paths=member_home, config=host.config), admin_profile)
    reads, clients = [], []
    real_read = model_profiles_module.read_model_profiles
    monkeypatch.setattr(model_profiles_module, "read_model_profiles", lambda path: reads.append(Path(path)) or real_read(path))
    monkeypatch.setattr(embedding_module, "MiniMaxEmbedder", lambda **kwargs: clients.append(kwargs))
    monkeypatch.setattr(embedding_module, "OpenAICompatibleEmbedder", lambda **kwargs: clients.append(kwargs))
    status: dict[str, str] = {}

    assert core._memory_semantic_channel(member, status) == (None, None)

    assert status == {"state": "degraded", "error_code": "MEMORY_EMBEDDING_PROFILE_UNAVAILABLE",
                      "profile_id": admin_profile, "profile_reason": "profile_not_found"}
    assert reads == [model_profiles_path(member_home)], "只读普通 owner 自己的目录"
    assert model_profiles_path(host.home_paths) not in reads and clients == [], "不碰管理员目录，不建客户端"
