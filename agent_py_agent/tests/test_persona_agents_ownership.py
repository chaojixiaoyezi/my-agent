# LLM: H05只使用临时登记用户及原来源切换/人格服务；不触碰真实owner规则或网络。
# 模块用途: 回归管理员锁定AGENTS后对旧用户来源及冻结来源的覆盖。
from __future__ import annotations

import pytest

from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.managed_user_governance import ManagedUserGovernanceService
from agent_py_agent.tests.test_agents_source_repository import choose


# LLM: 先在自治模式通过原来源授权切到自定义文件，再由真实管理员锁定canonical原件。
# 函数用途: 建立复现旧自定义来源遗留的H05环境。
@pytest.fixture
def locked_agents(tmp_path):
    root = tmp_path / "host"
    LocalUserLifecycle(LocalUserStore(root)).create("alice", idempotency_key="holistic-agents-alice")
    home = LocalUserStore(root).owner_home("alice")
    source = AgentsSourceRepository(owner=home.owner_id, owner_home=home.home_dir,
        path_policy=PathAccessPolicy.from_values(mode="full_access", agent_home_root=root))
    custom = home.home_dir / "custom-rules.md"
    custom.write_text("USER CUSTOM RULES\n")
    choose(source, custom)
    loaded = source.load()
    active = source.activation_metadata(loaded.snapshot)
    config_before = source.config_path.read_bytes()
    admin = ManagedUserGovernanceService(root)
    admin.set_persona("alice", agents_md="ADMIN CANONICAL RULES\n", persona_mode="admin_locked")
    return root, home, source, custom, active, config_before, admin


# LLM: 同一已构造仓库需跟随策略变化；canonical加载不能破坏用户旧来源配置。
# 函数用途: 管理员锁定后来源配置及加载都只指向owner原件。
def test_h05_locked_source_ignores_preexisting_custom_configuration(locked_agents):
    _root, home, source, _custom, _active, config_before, _admin = locked_agents
    assert source.configuration().kind == "owner_default"
    result = source.load()
    assert result.content == "ADMIN CANONICAL RULES\n"
    assert result.snapshot.locator == str(home.agents_md)
    assert source.config_path.read_bytes() == config_before
    source.require_active_permission(source.activation_metadata(result.snapshot))


# LLM: 覆盖带来源仓库及旧独立Persona构造；旧agents_path参数也不能替代canonical。
# 函数用途: 两种Persona装配都不能在admin_locked下加载旧自定义规则。
@pytest.mark.parametrize("attached", [False, True])
def test_h05_persona_always_loads_admin_canonical_agents(locked_agents, attached):
    _root, home, source, custom, _active, _config, _admin = locked_agents
    repository = PersonaRepository(owner_home=home.home_dir, soul_path=home.soul_md,
                                   user_path=home.user_md, agents_path=custom)
    if attached:
        repository.agents_source = source
    result = repository.load("agents")
    assert result.content == "ADMIN CANONICAL RULES\n"
    assert result.diagnostic.state == "ok"


# LLM: 已冻结旧CAS不得继续注入，权限核验不能因历史加载成功而延寿。
# 函数用途: 锁定后旧自定义active来源立即失去注入资格。
def test_h05_locked_source_rejects_old_active_custom_metadata(locked_agents):
    _root, _home, source, _custom, active, _config, _admin = locked_agents
    with pytest.raises(AgentsContractError):
        source.require_active_permission(active)


# LLM: 锁定忽略整个旧配置，不因旧文件坏了退回用户路径或读取错误来源。
# 函数用途: 坏旧配置和缺失自定义文件均不影响管理员原件。
def test_h05_locked_source_does_not_parse_broken_old_override(locked_agents):
    _root, _home, source, custom, _active, _config, _admin = locked_agents
    source.config_path.write_text("{broken old override")
    custom.unlink()
    assert source.load().content == "ADMIN CANONICAL RULES\n"


# LLM: 取消管理员锁后只恢复原有来源合同，不永久删除或重写用户配置。
# 函数用途: 自治模式仍能正常使用预先选择的自定义AGENTS。
def test_h05_unlock_restores_original_custom_source(locked_agents):
    _root, _home, source, _custom, _active, config_before, admin = locked_agents
    admin.set_policy("alice", {"persona_mode": "user_autonomous"})
    assert source.load().content == "USER CUSTOM RULES\n"
    assert source.configuration().kind == "local_path"
    assert source.config_path.read_bytes() == config_before
