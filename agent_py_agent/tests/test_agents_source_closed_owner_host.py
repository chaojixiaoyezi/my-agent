# LLM: 真实SimpleAgent可在坏权限下恢复为closed宿主；来源实际读取仍走活provider，不能靠构造锚点放行或消费旧缓存。
# 模块用途: 用echo、临时owner和本地规则验证来源装配、拒绝、修复及再次撤销，全程不抓取URL或调用外部服务。
import json

import pytest

from agent_py_agent.agent.capability._agents_contract_fields import AgentsContractError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_agents_source_repository import choose


# LLM: helper只在pytest临时目录构造正式宿主，完整owner配置不依赖scope替身，不修改原权限文件的解析或裁决。
# 函数用途: 分别准备本机管理员与普通提供方用户的来源宿主。
def _agent(tmp_path, owner):
    provider, kind, identity = owner
    config = AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"),
        my_agent_owner_provider=provider, my_agent_owner_kind=kind, my_agent_owner_id=identity,
        enable_tools=False, enable_plugins=False, enable_self_learning=False, prompt_files=[],
        agents_source_network_enabled=True,
    )
    return SimpleAgent(config, tmp_path / "workspace")


@pytest.mark.parametrize("owner", [("local", "main", "main"), ("feishu", "user", "source-closed")])
def test_closed_owner_constructs_but_source_requires_live_permission_after_restore(tmp_path, owner):
    original = _agent(tmp_path, owner)
    source = original.agents_source_repository
    target = source.owner_home / "working-rules.arbitrary"
    content = "# 原来源\n- CLOSED_OWNER_SOURCE_MARKER\n"
    target.write_text(content)
    choose(source, target)
    assert source.load().content == content
    configuration = source.configuration().configuration_cas
    permissions = original.home_paths.owner_permissions_json
    good = permissions.read_bytes()
    damaged = "{broken-permissions"
    permissions.write_text(damaged)

    restored = _agent(tmp_path, owner)
    restored_source = restored.agents_source_repository
    assert restored_source is restored.persona_repository.agents_source
    assert restored_source.owner == original.home_paths.owner_id
    assert restored_source.owner_home == original.home_paths.owner_home_dir
    assert restored_source.configuration().configuration_cas == configuration
    assert not restored.owner_policy.network_enabled
    assert permissions.read_text() == damaged
    assert any(row["context"] == "owner_policy.permissions" for row in restored.owner_policy.load_errors)

    # URL回调只是本地布尔裁决；不执行验证、DNS、抓取或授权签发。
    assert not restored_source._url_allowed("https://example.invalid/rules")
    for current in (source, restored_source):
        with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_PERMISSION_REVOKED"):
            _ = current.path_policy
        rejected = current.load()
        assert rejected.content is None
        assert rejected.snapshot.status == "unavailable"
        assert rejected.snapshot.failure_code == "AGENTS_SOURCE_PERMISSION_REVOKED"
        assert rejected.snapshot.used_cache is False

    permissions.write_bytes(good)
    assert restored_source.load().content == content
    assert restored_source.configuration().configuration_cas == configuration
    valid = json.loads(good)
    valid["network"]["enabled"] = False
    permissions.write_text(json.dumps(valid))
    assert not restored_source._url_allowed("https://example.invalid/rules")
    # 明确禁网不等于禁止已允许的本地来源；原本地路径门保持独立。
    assert restored_source.load().content == content
    permissions.write_text(damaged)
    rejected_again = restored_source.load()
    assert rejected_again.content is None
    assert rejected_again.snapshot.failure_code == "AGENTS_SOURCE_PERMISSION_REVOKED"
    assert permissions.read_text() == damaged
