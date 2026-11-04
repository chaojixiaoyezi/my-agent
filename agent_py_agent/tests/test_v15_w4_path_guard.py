"""在临时规范Owner布局验证写拒绝、只读放行及路径别名。"""
import sys

import pytest

from agent_py_agent.agent.path_access_policy import PathAccessPolicy


# LLM: 两种模式不得绕过同一个canonical owner存储保护。
# 函数用途: 验证受管资产目录读写有别且workspace不受影响。
@pytest.mark.parametrize('collection,hint', [('scripts', 'scripts(action=update)'), ('workflows', 'workflows(action=save)')])
def test_owner_assets_readable_but_not_file_writable(tmp_path, collection, hint):
    home = tmp_path / 'owners/local/main'
    home.mkdir(parents=True)
    policy = PathAccessPolicy(mode='full', agent_home_root=tmp_path, owner_scope_root=home)
    path = home / collection / 'demo/main.py'
    assert policy.check(path).allowed
    denied = policy.check_write(path)
    assert not denied.allowed and denied.code == 'PATH_HOST_MANAGED_STORE_BLOCKED'
    assert hint in denied.message
    assert policy.check_write(home / 'tasks/project/.agent' / collection / 'demo/main.py').allowed


# LLM: 新侧账本沿既有H2硬门，不另造路径错误码。
# 函数用途: 验证三个宿主侧账本拒绝文件工具访问。
@pytest.mark.parametrize('name', ['asset_evolution', 'source_manifests', 'asset_extractions'])
def test_side_ledgers_are_host_managed(tmp_path, name):
    home = tmp_path / 'owners/local/main'
    policy = PathAccessPolicy(mode='full', agent_home_root=tmp_path, owner_scope_root=home)
    assert not policy.check_write(home / 'data' / name / 'x.json').allowed


# LLM: Darwin实际路径别名测试，不将模拟casefold当跨平台端到端证明。
# 函数用途: 拒绝大小写和firmlink别名下的资产写入。
@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin路径别名')
def test_darwin_case_and_realpath_alias(tmp_path):
    home = tmp_path / 'owners/local/main'
    home.mkdir(parents=True)
    policy = PathAccessPolicy(mode='full', agent_home_root=tmp_path, owner_scope_root=home)
    assert not policy.host_write_decision(home / 'Scripts/demo/main.py').allowed
    alias = tmp_path / 'alias'
    alias.symlink_to(home, target_is_directory=True)
    assert not policy.host_write_decision(alias / 'scripts/demo/main.py').allowed
