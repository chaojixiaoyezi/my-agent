"""子进程凭据擦洗(选项1-C):owner-scoped 命令看不到自管密钥,普通变量留着,放行盖不掉自管凭据。"""

from __future__ import annotations

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
