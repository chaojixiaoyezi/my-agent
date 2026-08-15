"""my-agent update 自更新命令测试:安装来源探测 + 更新计划/执行(monkeypatch git/pip,不真跑)。

雏鸟学飞:普通用户/agent 一条 `my-agent update` 就能升级。这里钉死核心逻辑:
① git 检出(含 worktree 的 .git 文件)被认出 ② 非 git 安装给明确指引不瞎跑 ③ 无上游时 --check 诚实
不谎报"有更新" ④ pull 失败返回非 0 ⑤ 成功时报 before→after。

注:_git 收参数序列(args: Sequence[str]),不用 *args(架构守卫禁产品代码用可变位置参数),故 fake 也按
(repo, args) 签名,args[0] 仍是 git 子命令。
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from agent_py_agent.cli import update as U


def test_detect_install_source_is_git_in_repo() -> None:
    s = U.detect_install_source()
    assert s.kind == "git" and s.repo is not None and (s.repo / ".git").exists()  # 含 worktree 的 .git 文件


def test_cmd_update_non_git_returns_1(monkeypatch, capsys) -> None:
    monkeypatch.setattr(U, "detect_install_source", lambda: U.InstallSource("pip", None))
    rc = U.cmd_update(Namespace(check=False))
    assert rc == 1
    assert "pip install" in capsys.readouterr().out  # 给明确指引,不瞎跑 git


def test_check_update_no_upstream_is_honest(monkeypatch, capsys) -> None:
    monkeypatch.setattr(U, "detect_install_source", lambda: U.InstallSource("git", Path("/tmp/x")))

    def fake_git(repo, args):
        if args[0] == "fetch":
            return 0, ""
        if args[0] == "rev-parse":
            return 0, "abc123"
        if args[0] == "rev-list":
            return 128, "no upstream"  # 无上游 → 算不出 behind
        return 0, ""

    monkeypatch.setattr(U, "_git", fake_git)
    rc = U.cmd_update(Namespace(check=True))
    out = capsys.readouterr().out
    assert rc == 0 and "无法判断" in out  # ⭐ 诚实:不谎报"有更新可用"


def test_check_update_up_to_date(monkeypatch, capsys) -> None:
    monkeypatch.setattr(U, "detect_install_source", lambda: U.InstallSource("git", Path("/tmp/x")))
    monkeypatch.setattr(U, "_git", lambda repo, args: (0, "0" if args[0] == "rev-list" else "abc123"))
    assert U.cmd_update(Namespace(check=True)) == 0
    assert "已是最新" in capsys.readouterr().out


def test_apply_update_pull_fail_returns_1(monkeypatch, capsys) -> None:
    monkeypatch.setattr(U, "detect_install_source", lambda: U.InstallSource("git", Path("/tmp/x")))

    def fake_git(repo, args):
        if args[0] == "rev-parse":
            return 0, "abc123"
        if args[0] == "pull":
            return 1, "本地有改动"  # pull 失败
        return 0, ""

    monkeypatch.setattr(U, "_git", fake_git)
    rc = U.cmd_update(Namespace(check=False))
    assert rc == 1 and "git pull 失败" in capsys.readouterr().out


def test_apply_update_success_reports_before_after(monkeypatch, capsys) -> None:
    monkeypatch.setattr(U, "detect_install_source", lambda: U.InstallSource("git", Path("/tmp/x")))
    commits = iter(["aaa111", "bbb222"])  # before, after

    def fake_git(repo, args):
        if args[0] == "rev-parse":
            return 0, next(commits)
        if args[0] == "pull":
            return 0, "Updated"
        return 0, ""

    monkeypatch.setattr(U, "_git", fake_git)
    monkeypatch.setattr(U, "_pip_install_editable", lambda repo: (0, "ok"))
    rc = U.cmd_update(Namespace(check=False))
    out = capsys.readouterr().out
    assert rc == 0 and "aaa111 → bbb222" in out and "更新完成" in out


def test_apply_update_pip_fail_returns_1(monkeypatch, capsys) -> None:
    monkeypatch.setattr(U, "detect_install_source", lambda: U.InstallSource("git", Path("/tmp/x")))
    monkeypatch.setattr(U, "_git", lambda repo, args: (0, "abc" if args[0] == "rev-parse" else ""))
    monkeypatch.setattr(U, "_pip_install_editable", lambda repo: (1, "依赖装失败"))
    rc = U.cmd_update(Namespace(check=False))
    assert rc == 1 and "依赖刷新失败" in capsys.readouterr().out  # 代码更新了但依赖失败 → 报警
