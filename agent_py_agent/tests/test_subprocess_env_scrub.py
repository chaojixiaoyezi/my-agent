# LLM: 仅合成环境值，不读取真实凭据；SSH代理能力不能被透传声明重新授予。
# 模块用途: 回归宿主凭据、SSH代理与连接串剥离以及普通变量保留。
"""子进程凭据擦洗(选项1-C):owner-scoped 命令看不到自管密钥,普通变量留着,放行盖不掉自管凭据。"""

from __future__ import annotations

import pytest
from agent.tooling._subprocess_env_scrub import scrub_subprocess_env


def test_scrubs_self_managed_credentials():
    env = {
        "MINIMAX_API_KEY": "sk-secret",
        "AGENT_API_KEY": "agentkey",
        "FEISHU_APP_SECRET": "fs-secret",
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "LANG": "zh_CN.UTF-8",
    }
    out = scrub_subprocess_env(env)
    assert "MINIMAX_API_KEY" not in out
    assert "AGENT_API_KEY" not in out
    assert "FEISHU_APP_SECRET" not in out
    # 干活要的普通变量留着
    assert out["PATH"] == "/usr/bin" and out["HOME"] == "/home/u" and out["LANG"] == "zh_CN.UTF-8"


def test_scrubs_generic_secret_substrings():
    env = {"SOME_API_KEY": "x", "MY_TOKEN": "y", "DB_PASSWORD": "z", "NORMAL_VAR": "keep"}
    out = scrub_subprocess_env(env)
    assert "SOME_API_KEY" not in out and "MY_TOKEN" not in out and "DB_PASSWORD" not in out
    assert out["NORMAL_VAR"] == "keep"


def test_safe_prefix_not_scrubbed_even_if_matches_substring():
    # PATH/HOME 等安全前缀即便恰好含子串也不误剥(这里构造一个安全前缀开头的)
    env = {"PYTHONPATH": "/x", "PATH": "/usr/bin", "USER": "u"}
    out = scrub_subprocess_env(env)
    assert out == env  # 一个都不剥


def test_passthrough_cannot_override_self_managed_credential():
    # GHSA 修复:把自管凭据声明成 passthrough 也无效,照样剥(防恶意 skill/子代理偷 key)
    env = {"MINIMAX_API_KEY": "sk", "TENOR_API_KEY": "third-party"}
    out = scrub_subprocess_env(env, passthrough=frozenset({"MINIMAX_API_KEY", "TENOR_API_KEY"}))
    assert "MINIMAX_API_KEY" not in out  # 自管:放行也删
    assert out.get("TENOR_API_KEY") == "third-party"  # 第三方:放行有效


def test_empty_env():
    assert scrub_subprocess_env({}) == {}


def test_secret_names_cannot_hide_behind_safe_prefixes_or_case():
    secrets = dict.fromkeys(("HOME_API_KEY", "USER_TOKEN", "PYTHON_SECRET", "PIP_PASSWORD", "XDG_PRIVATE_KEY", "agent_api_key", "minimax_api_key"), "test-only")
    assert scrub_subprocess_env(secrets) == {}
    assert scrub_subprocess_env(secrets, passthrough=frozenset({"agent_api_key"})) == {}


def test_host_declared_secret_reference_cannot_be_overridden_by_passthrough():
    env = {'BACKEND_SOURCE': 'synthetic-marker', 'ORDINARY_VAR': 'keep'}
    assert scrub_subprocess_env(env, host_credential_env_names=frozenset({'BACKEND_SOURCE'}),
        passthrough=frozenset({'BACKEND_SOURCE'})) == {'ORDINARY_VAR': 'keep'}


def test_all_shell_modes_scrub_host_declared_environment_sources(monkeypatch):
    from agent_py_agent.agent.tooling.shell import _subprocess_text_env

    monkeypatch.setenv('CHANNEL_SOURCE', 'synthetic-marker')
    monkeypatch.setenv('MINIMAX_API_KEY', 'synthetic-marker')
    for owner in ('', '/tmp/synthetic-owner'):
        env = _subprocess_text_env(owner, sandboxed=True, host_credential_env_names=frozenset({'CHANNEL_SOURCE'}))
        assert 'CHANNEL_SOURCE' not in env and 'MINIMAX_API_KEY' not in env
    assert 'CHANNEL_SOURCE' in __import__('os').environ


# LLM: SSH代理是宿主权限能力，显式passthrough也不能把它带入子进程；只用合成值。
# 函数用途: 验证SSH代理变量大小写及透传覆盖均被拒绝。
@pytest.mark.parametrize('name', ['SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'ssh_auth_sock', 'ssh_agent_pid'])
def test_sec_f_ssh_agent_environment_cannot_be_passed_through(name):
    env = {name: 'synthetic-agent-marker', 'PATH': '/usr/bin', 'LANG': 'C'}
    expected = {'PATH': '/usr/bin', 'LANG': 'C'}
    assert scrub_subprocess_env(env) == expected
    assert scrub_subprocess_env(env, passthrough=frozenset({name})) == expected
    assert env[name] == 'synthetic-agent-marker'


# LLM: 新增模式默认剥离，不破坏非自管第三方凭据的准确名称透传；安全变量和宿主输入保持。
# 函数用途: 验证连接串、访问键及代理套接字秘密识别。
@pytest.mark.parametrize('name', ['AWS_ACCESS_KEY_ID', 'OTHER_AUTH_SOCK', 'MY_DATABASE_URL',
    'MY_CONNECTION_URI', 'MY_CONNECTION_STRING', 'my_database_url'])
def test_sec_f_additional_secret_name_patterns(name):
    env = {name: 'synthetic-private-value', 'NORMAL_VAR': 'keep'}
    assert scrub_subprocess_env(env) == {'NORMAL_VAR': 'keep'}
    assert scrub_subprocess_env(env, passthrough=frozenset({name})) == env
