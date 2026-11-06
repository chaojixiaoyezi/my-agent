"""当前群历史工具的默认关闭、纯参数门、注册与假 HTTP 链路。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import core
from agent_py_agent.agent.adapter import feishu_message_history as history
from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.config import load_simple_yaml
from agent_py_agent.agent.tooling.channel_messages_tool import ChannelMessagesTool
from agent_py_agent.agent.tooling.models import EffectResolverPolicy
from agent_py_agent.tests.test_v14_feishu_message_history import environment as environment
from agent_py_agent.tests.test_v14_feishu_message_history import message, page


# LLM: 测试参数工厂；只合并显式覆盖项，让各用例共用相同基线。
# 函数用途: 给工具调用准备固定的时间范围和分页参数。
def params(**overrides):
    return {"action": "list", "start_time": 100, "end_time": 200, **overrides}


# LLM: pytest 默认能力回归；使用真实配置与工具注册，所有状态限于临时 HOME。
# 函数用途: 检查代码、YAML 和运行快照都默认不开放群历史。
def test_defaults_off_in_dataclass_yaml_and_real_registry(tmp_path):
    assert CapabilityConfig().feishu_chat_history_enabled is False
    config = load_simple_yaml(Path(__file__).parents[1] / "config" / "capability_config.yaml")
    assert config["feishu_chat_history_enabled"] is False
    agent = core.SimpleAgent(AgentConfig(enable_tools=True, memory_path=str(tmp_path / "memory.jsonl")), str(tmp_path))
    snapshot = agent.tools.runtime_snapshot()
    assert "channel_messages" not in snapshot.available_tool_names
    assert ("channel_messages", "TOOL_UNAVAILABLE", "disabled by feishu_chat_history_enabled") in snapshot.unavailable_tools


# LLM: pytest 严格 bool 回归；通过真实配置加载拒绝字符串、整数和空值，不能静默放行。
# 函数用途: 防止看起来像 true 的非布尔配置把功能意外打开。
@pytest.mark.parametrize("value", ["true", 1, None])
def test_history_switch_is_strict_config_bool(tmp_path, value):
    path = tmp_path / "capability.yaml"
    # JSON 标量也是合法 YAML，保留字符串引号及整数/null 负例，不依赖 PyYAML。
    path.write_text(f"feishu_chat_history_enabled: {json.dumps(value)}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_capability_config(path)


# LLM: 真布尔值沿真实配置加载；夹具只写单行 YAML，不需要第三方序列化器。
# 函数用途: 检查去掉 PyYAML 后，启用和关闭仍能按严格布尔类型加载。
@pytest.mark.parametrize("value", [False, True])
def test_history_switch_accepts_config_bool(tmp_path, value):
    path = tmp_path / "capability.yaml"
    path.write_text(f"feishu_chat_history_enabled: {json.dumps(value)}\n", encoding="utf-8")
    assert load_capability_config(path).feishu_chat_history_enabled is value


# LLM: pytest 注册到执行链回归；真实 registry 加假 HTTP，保留默认折叠与短摘要合同。
# 函数用途: 检查明确启用后工具确实能发现并调用，而不只是创建了一个类。
def test_enabled_real_registry_deferred_spec_and_handler_fake_http(tmp_path, monkeypatch, environment):
    env = environment
    config = replace(CapabilityConfig(), feishu_chat_history_enabled=True)
    monkeypatch.setattr(core, "capability_config_for_agent", lambda agent: config)
    agent = core.SimpleAgent(AgentConfig(enable_tools=True, memory_path=str(tmp_path / "memory.jsonl")), str(tmp_path))
    agent.home_paths, agent.delivery_service = env.home, env.agent.delivery_service
    tool = agent.tools.tools["channel_messages"]
    assert tool.availability().available
    hints = tool.model_spec.hints
    assert hints.category == "capability" and hints.default_deferred
    assert hints.deferred_summary == "读取当前飞书群的有界历史消息" and len(hints.deferred_summary) <= 60
    assert "chat_id" not in tool.model_spec.input_schema["properties"]
    assert tool.model_spec.input_schema["additionalProperties"] is False
    assert tool.runtime_policy.effect_resolver == EffectResolverPolicy("read_only")
    env.replies.append(page([message()]))
    result = tool.execute(params())
    assert result.ok and result.result_envelope["channel_messages"]["items"][0]["message_id"] == "om_1"
    assert json.loads(result.output)["chat_id"] == "oc_group" and env.calls[0]["container_id"] == ["oc_group"]


# LLM: pytest 输入注入回归；任何模型自带群号、地址、凭据、写操作或布尔 limit 都先拒绝。
# 函数用途: 检查调用者不能把只读当前群工具改造成任意群或任意地址入口。
@pytest.mark.parametrize("injection", [{"chat_id": "oc_other"}, {"url": "https://invalid.example/"},
    {"credential": "not-a-real-secret"}, {"action": "send"}, {"limit": True}])
def test_injected_scope_or_invalid_params_rejected_without_http(environment, injection):
    tool = ChannelMessagesTool(environment.agent)
    result = tool.execute(params(**injection))
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert not environment.calls
    validation = tool.validate_invocation(params(**injection), SimpleNamespace())
    assert validation is not None and not validation.ok and not environment.calls


# LLM: pytest 宿主网络上界回归；owner 开项不能覆盖继承的禁网络策略。
# 函数用途: 检查网络被关掉时不会偷偷请求历史接口。
def test_inherited_network_disable_blocks_even_with_enabled_owner(environment):
    tool = ChannelMessagesTool(environment.agent)
    result = tool.execute(params(__network_access_mode="disabled"))
    assert not result.ok and result.error_code == "NETWORK_DISABLED" and not environment.calls


# LLM: pytest 运行中关项回归；已注册实例每次执行仍要重新检查开关。
# 函数用途: 检查开关关掉后，旧工具对象也不能继续读取。
def test_switch_rechecked_on_already_registered_tool(environment):
    tool = ChannelMessagesTool(environment.agent)
    environment.config.feishu_chat_history_enabled = False
    assert not tool.availability().available
    result = tool.execute(params())
    assert not result.ok and not environment.calls


# LLM: pytest 权威服务失败回归；失败结果不能包含私有消息或未经筛选的平台正文。
# 函数用途: 检查拿不到群证明时安全失败，不泄漏前一页的数据。
def test_unavailable_authority_returns_no_private_items_or_provider_body(environment, monkeypatch):
    from agent_py_agent.agent.adapter.feishu_provider_facts import FeishuProviderFactError

    def unavailable(*args):
        raise FeishuProviderFactError("FEISHU_PROVIDER_SCOPE_REQUIRED", 99991663, stage="members")

    monkeypatch.setattr(history, "group_authority_snapshot", unavailable)
    result = ChannelMessagesTool(environment.agent).execute(params())
    assert not result.ok and result.error_code == "FEISHU_PROVIDER_SCOPE_REQUIRED"
    assert "items" not in result.result_envelope and not environment.calls


# LLM: pytest 接线回归；保留真实群 HOME、部署配置、注册表及 provider facts，仅替代令牌和 HTTP。
# 函数用途: 检查真实本地权限链能连起来，并能在运行中即时撤权。
def test_real_group_home_config_registry_and_provider_facts_over_fake_http(tmp_path, monkeypatch):
    import os
    import sys
    from urllib.parse import parse_qs, urlsplit

    from agent_py_agent.agent.adapter import feishu
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    shared = tmp_path / "home"
    identity = OwnerIdentity(provider="feishu", owner_kind="group", owner_id="oc_group")
    root = resolve_owner_home(shared, identity).home_dir
    for directory in (root, shared):
        config = directory / "config" / "capability_config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("feishu_chat_history_enabled: true\nfeishu_cli_backend_enabled: false\n")
    agent = core.SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(shared), enable_plugins=False,
        enable_subagents=False, my_agent_owner_provider=identity.provider, my_agent_owner_kind=identity.owner_kind,
        my_agent_owner_id=identity.owner_id, memory_path=str(root / "memory.jsonl")), str(root))
    adapter = SimpleNamespace(app_id="fixture-app", _get_tenant_access_token=lambda: "fixture-only")
    # 仅供应商认证材料和 HTTP 为本地夹具；owner/config/注册/参数门/群事实 reader 均走产品代码。
    agent.delivery_service = SimpleNamespace(registry=SimpleNamespace(adapter_for=lambda provider: adapter))
    calls = []

    def transport(method, path, body, credential, *, timeout=10):
        assert method == "GET" and body == {} and credential == "fixture-only" and timeout > 0
        endpoint = urlsplit(path).path
        calls.append(endpoint)
        if endpoint == "/im/v1/messages":
            assert parse_qs(urlsplit(path).query)["container_id"] == ["oc_group"]
            data = page([message()])
        elif endpoint.endswith("/members/is_in_chat"):
            data = {"is_in_chat": True}
        elif endpoint.endswith("/members"):
            data = {"items": [{"member_id_type": "open_id", "member_id": "ou_owner"}], "has_more": False, "member_total": 1}
        else:
            assert endpoint == "/im/v1/chats/oc_group"
            data = {"chat_mode": "group", "owner_id": "ou_owner", "owner_id_type": "open_id", "tenant_key": "fixture-tenant"}
        return {"code": 0, "data": data}

    monkeypatch.setattr(feishu, "_feishu_msg_api", transport)
    tool = agent.tools.tools["channel_messages"]
    directory = "/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"
    before = len(os.listdir(directory))
    result = tool.execute(params())
    assert result.ok, result.output
    assert result.result_envelope["channel_messages"]["items"][0]["message_id"] == "om_1"
    assert calls.count("/im/v1/messages") == 1 and calls.count("/im/v1/chats/oc_group/members") == 2
    assert len(os.listdir(directory)) - before == 0
    (shared / "config" / "capability_config.yaml").write_text("feishu_chat_history_enabled: false\n")
    previous = list(calls)
    disabled = tool.execute(params())
    assert not disabled.ok and calls == previous


# LLM: 直接注入标准解码器可抛的异常，不依赖本机递归阈值；旧真实嵌套输入回归仍保留。
# 函数用途: 验证异常经过工具公开入口转换为安全参数失败。
def test_cursor_decoder_recursion_is_safe_at_tool_boundary(environment, monkeypatch):
    def decode(value):
        raise RecursionError("simulated decoder depth")

    monkeypatch.setattr(history.json, "loads", decode)
    result = ChannelMessagesTool(environment.agent).execute(params(cursor="e30="))
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS" and not environment.calls


# LLM: 空正文也含私有查询事实；读取中撤销权限后不得发布计数、覆盖事实或游标。
# 函数用途: 对无匹配结果验证发表前实时权限门，包含共享开关撤销与机器人离群。
@pytest.mark.parametrize("revocation", ["deployment", "membership"])
@pytest.mark.parametrize("more", [False, True])
def test_empty_result_reauthorizes_before_metadata_publication(environment, monkeypatch, revocation, more):
    from agent_py_agent.agent.adapter import feishu

    env = environment
    env.replies.append(page([message(sender="ou_other")], more=more, token="p2" if more else ""))
    monkeypatch.setattr(history.HistoryLimits, "MAX_PAGES", 1)
    original = feishu._feishu_msg_api

    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        if revocation == "deployment":
            env.config.feishu_chat_history_enabled = False
        else:
            env.proof["bot_is_member"] = False
        return result

    monkeypatch.setattr(feishu, "_feishu_msg_api", revoke)
    result = ChannelMessagesTool(env.agent).execute(params(sender_ids=["ou_a"]))
    assert not result.ok and len(env.calls) == 1
    assert "channel_messages" not in result.result_envelope


# LLM: pytest 权限拒绝回归；第2页平台文档码230027经原 provider facts 映射后，工具整体失败，不泄漏第1页。
# 函数用途: 检查工具公开入口对明确读取权限拒绝只返回安全失败码，不发布已缓冲消息或页信封。
def test_permission_rejection_after_first_page_fails_without_buffered_items(environment, monkeypatch):
    from agent_py_agent.agent.adapter import feishu
    from agent_py_agent.tests.test_v14_feishu_message_history import failing_from_page

    env = environment
    env.replies.extend([page([message(1, text="PRIVATE_FIRST_PAGE")], more=True, token="p2"), page([message(2)])])
    monkeypatch.setattr(feishu, "_feishu_msg_api", failing_from_page(
        env, 2, {"code": 230027, "msg": "PRIVATE_PROVIDER_TEXT"}))
    result = ChannelMessagesTool(env.agent).execute(params())
    assert not result.ok and result.error_code == "FEISHU_PROVIDER_READ_REJECTED"
    assert "channel_messages" not in result.result_envelope and len(env.calls) == 2
    visible = result.output + json.dumps(result.result_envelope, ensure_ascii=False, default=str)
    assert "om_1" not in visible and "PRIVATE_FIRST_PAGE" not in visible and "PRIVATE_PROVIDER_TEXT" not in visible


# LLM: pytest 暂态失败回归；第1页后的传输故障仍是诚实部分结果，工具成功但覆盖标为部分。
# 函数用途: 检查权限拒绝的失败关闭没有把普通可用性故障变成整体失败。
def test_transient_failure_after_first_page_returns_honest_partial_result(environment, monkeypatch):
    from agent_py_agent.agent.adapter import feishu
    from agent_py_agent.tests.test_v14_feishu_message_history import failing_from_page

    env = environment
    env.replies.extend([page([message(1)], more=True, token="p2"), page([message(2)])])
    monkeypatch.setattr(feishu, "_feishu_msg_api", failing_from_page(env, 2, OSError("PRIVATE_TRANSPORT_TEXT")))
    result = ChannelMessagesTool(env.agent).execute(params())
    assert result.ok, result.output
    payload = result.result_envelope["channel_messages"]
    assert [item["message_id"] for item in payload["items"]] == ["om_1"]
    assert payload["coverage"]["status"] == "partial" and payload["has_more"] and payload["truncated"]
    assert "PRIVATE_TRANSPORT_TEXT" not in result.output
