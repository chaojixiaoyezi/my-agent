from __future__ import annotations

"""path_access_policy: my-agent 自己 home 豁免 dangerous_roots —— 修 root 用户场景误伤产物目录。

真机 dogfooding(testbox root 用户):dangerous_roots 含 /root(本意保护 /root/.ssh 等),但 my-agent
数据目录在 /root/.my-agent,整个 /root 被列危险目录,导致 agent 写自己的 output 产物失败(UNKNOWN_ERROR),
只能自适应改写 /tmp。修复:agent 写自己 home 子树(MY_AGENT_HOME,默认 ~/.my-agent)豁免;/root 下其他
敏感目录(.ssh 等)仍拦——口子精确不扩大,resolve 已展开 .. 防逃逸。
"""

from pathlib import Path

from agent_py_agent.agent.path_access_policy import PathAccessPolicy


def test_my_agent_home_exempt_from_dangerous_root(monkeypatch, tmp_path) -> None:
    """根因场景:home 的父目录在 dangerous_roots,但 home 子树内的产物目录放行。"""
    home = tmp_path / "myagent_home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    # tmp_path(即 home 的父)整个被列危险目录,模拟 /root 含 /root/.my-agent
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[str(tmp_path)])
    decision = policy.check(str(home / "owners/local/main/tasks/x/output/bot.py"))
    assert decision.allowed  # agent 写自己产物目录放行


def test_dangerous_siblings_still_blocked(monkeypatch, tmp_path) -> None:
    """豁免精确:home 之外、dangerous_root 下的文件仍被拦(不因豁免开口子)。"""
    home = tmp_path / "myagent_home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[str(tmp_path)])
    decision = policy.check(str(tmp_path / "secret.txt"))  # 在 tmp_path 下但不在 home 子树
    assert not decision.allowed
    assert decision.code == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_home_escape_via_dotdot_still_blocked(monkeypatch, tmp_path) -> None:
    """逃逸防护:home/../secret 经 resolve 展开后不在 home 下,仍被拦。"""
    home = tmp_path / "myagent_home"
    home.mkdir()
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[str(tmp_path / "secret")])
    decision = policy.check(str(home / ".." / "secret" / "key"))  # → tmp_path/secret/key,不在 home 下
    assert not decision.allowed


def test_default_home_when_env_unset(monkeypatch) -> None:
    """MY_AGENT_HOME 未设时默认 ~/.my-agent 豁免;但 ~ 下其他文件仍拦。"""
    monkeypatch.delenv("MY_AGENT_HOME", raising=False)
    home = Path("~/.my-agent").expanduser().resolve()
    policy = PathAccessPolicy.from_values(mode="normal", dangerous_roots=[str(home.parent)])
    assert policy.check(str(home / "output/f.py")).allowed  # ~/.my-agent 子树放行
    assert not policy.check(str(home.parent / "other_secret.txt")).allowed  # ~ 下非 home 文件仍拦


def test_full_mode_unaffected() -> None:
    """full 模式本就全放行,豁免逻辑不影响它。"""
    assert PathAccessPolicy.from_values(mode="full", dangerous_roots=["/etc"]).check("/etc/passwd").allowed
