
from __future__ import annotations

"""my-agent 自更新命令(像 通道运行时 update):拉取最新代码 + 刷新依赖,普通用户一条命令就能升级。

设计取舍:把"探测安装来源 + 算更新计划"(纯函数,可测)和"真跑 git/pip"(子进程)分开。
editable(git 检出)安装是主路径——install.sh 就是 git clone + pip install -e,故 update = git pull + pip 刷依赖。
非 git 检出(pip 包)给出明确指引,不瞎跑。
"""

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstallSource:
    """安装来源:kind=git(可 git pull 自更新)/ pip(需 pip --upgrade)/ unknown。"""

    kind: str
    repo: Path | None


def detect_install_source() -> InstallSource:
    """从 agent_py_agent 包目录向上找 .git → git 检出(可自更新);找不到 → pip 包。

    .git 用 exists() 不用 is_dir():worktree/submodule 下 .git 是个指向真 gitdir 的**文件**,
    git 命令照常工作。向上走兼容任意嵌套。
    """
    import agent_py_agent

    start = Path(agent_py_agent.__file__).resolve().parent
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return InstallSource("git", candidate)
    return InstallSource("pip", None)


def _git(repo: Path, args: Sequence[str]) -> tuple[int, str]:
    """跑一条 git 子命令(args 为参数序列),返回 (returncode, stdout+stderr 文本)。git 缺失/异常 → (127, 说明)。"""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=180
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"git 执行失败: {type(exc).__name__}"
    return out.returncode, (out.stdout + out.stderr).strip()


def _current_commit(repo: Path) -> str:
    code, text = _git(repo, ["rev-parse", "--short", "HEAD"])
    return text if code == 0 else "unknown"


def _pip_install_editable(repo: Path) -> tuple[int, str]:
    """用当前解释器的 pip 重装 editable(刷新依赖;代码本身 editable 已随 git pull 生效)。"""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(repo), "--quiet"],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, f"pip 执行失败: {type(exc).__name__}"
    return out.returncode, (out.stdout + out.stderr).strip()


def add_update_subcommand(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("update", help="自更新:拉取最新代码并刷新依赖(像 openclaw update)")
    parser.add_argument("--check", action="store_true", help="只检查有没有更新,不实际更新")
    parser.set_defaults(func=cmd_update)


def cmd_update(args) -> int:
    source = detect_install_source()
    if source.kind != "git" or source.repo is None:
        print("⚠️ my-agent 不是从 git 检出安装的(可能是 pip 包)。请用 `pip install --upgrade ...` 或重跑安装脚本更新。")
        return 1
    if bool(getattr(args, "check", False)):
        return _check_update(source.repo)
    return _apply_update(source.repo)


def _check_update(repo: Path) -> int:
    now = _current_commit(repo)
    code, _text = _git(repo, ["fetch", "--quiet"])
    if code != 0:
        print(f"⚠️ 无法 fetch 远端(检查网络/远端配置)。当前: {now}")
        return 1
    behind_code, behind = _git(repo, ["rev-list", "--count", "HEAD..@{u}"])
    if behind_code != 0 or not behind.isdigit():
        print(f"⚠️ 无法判断更新:当前分支可能未设上游/远端无此分支。当前: {now}")
        return 0
    if behind == "0":
        print(f"✅ 已是最新({now}),无需更新。")
        return 0
    print(f"有更新可用:落后远端 {behind} 个提交(当前 {now})。跑 `my-agent update` 升级。")
    return 0


def _apply_update(repo: Path) -> int:
    before = _current_commit(repo)
    print(f"当前: {repo} @ {before}\n拉取最新代码…")
    code, text = _git(repo, ["pull", "--ff-only"])
    if code != 0:
        print(f"❌ git pull 失败(本地可能有改动/分叉):\n{text}\n请手动处理后重试。")
        return 1
    print("刷新依赖…")
    pip_code, pip_text = _pip_install_editable(repo)
    if pip_code != 0:
        print(f"⚠️ 代码已更新但依赖刷新失败:\n{pip_text}\n手动跑 `pip install -e {repo}`。")
        return 1
    after = _current_commit(repo)
    if before == after:
        print(f"✅ 已是最新({after}),无变化。")
    else:
        print(f"✅ 更新完成:{before} → {after}。")
    return 0


__all__ = ["InstallSource", "add_update_subcommand", "cmd_update", "detect_install_source"]
