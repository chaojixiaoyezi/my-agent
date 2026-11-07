
from __future__ import annotations

"""my-agent 自更新命令(像 通道运行时 update):拉取最新代码 + 刷新依赖,普通用户一条命令就能升级。

设计取舍:把"探测安装来源 + 算更新计划"(纯函数,可测)和"真跑 git/pip"(子进程)分开。
editable(git 检出)安装是主路径——install.sh 就是 git clone + pip install -e,故 update = git pull + pip 刷依赖。
非 git 检出(pip 包)给出明确指引,不瞎跑。
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
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
    # 离线标记沿解释器前缀核身份，不能把数据 HOME 或任意 .run 文件当安装来源。
    for prefix in Path(sys.executable).resolve().parents:
        marker = prefix / '.my-agent-private-install.json'
        if marker.is_file():
            try:
                row = json.loads(marker.read_text(encoding='utf-8'))
                valid = (type(row) is dict and row.get('schema') == 'my-agent.private-release.v1'
                    and row.get('owner_uid') == getattr(os, 'getuid', lambda: None)()
                    and row.get('prefix') == str(prefix))
            except (OSError, ValueError, UnicodeError):
                valid = False
            return InstallSource('offline' if valid else 'unknown', None)
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
    parser = sub.add_parser("update", help="自更新:拉取最新代码并刷新依赖")
    parser.add_argument("--check", action="store_true", help="只检查有没有更新,不实际更新")
    parser.add_argument("--package", type=Path, help="离线更新包 .run；保留当前安装目录、配置和数据")
    parser.set_defaults(func=cmd_update)


def cmd_update(args) -> int:
    if package := getattr(args, "package", None):
        return _offline_update(package)
    source = detect_install_source()
    if source.kind != "git" or source.repo is None:
        print("⚠️ my-agent 不是从 git 检出安装的(可能是 pip 包)。请用 `pip install --upgrade ...` 或重跑安装脚本更新。")
        return 1
    if bool(getattr(args, "check", False)):
        return _check_update(source.repo)
    return _apply_update(source.repo)


def _check_update(repo: Path) -> int:
    # CLI 保留原来的 git 调用顺序；远程诊断的额外探测不能改变旧 fake-git 契约。
    return update_status(repo, inspect_worktree=False, on_step=_print_step)["returncode"]


def _apply_update(repo: Path) -> int:
    return apply_update_steps(repo, on_step=_print_step)["returncode"]


def _print_step(step: dict) -> None:
    if "cli" in step:
        print(step["cli"])


def _step(result: dict, callback: Callable[[dict], None] | None, name: str,
          *, code: int = 0, text: str = "", cli: str | None = None) -> None:
    """结构化步骤和展示分开，远程作业可逐步落盘而不用截获 stdout。"""
    record = {"step": name, "returncode": code, "text": text,
              "stream": "stderr" if code else "status"}
    if cli is not None:
        record["cli"] = cli
    result["steps"].append(record)
    if callback is not None:
        callback(dict(record))


def update_status(repo: Path | None = None, *, inspect_worktree: bool = True,
                  on_step: Callable[[dict], None] | None = None) -> dict:
    """诊断真实安装来源；失败保留诊断，不把 fetch/上游失败说成已是最新。"""
    source = detect_install_source() if repo is None else InstallSource("git", Path(repo))
    result = {"source": source.kind, "current": "unknown", "latest": "",
              "update_available": False, "dirty": False, "notes": [],
              "returncode": 0, "steps": []}
    if source.kind != "git" or source.repo is None:
        result["notes"].append("当前安装不是 git 检出，不能远程拉取更新。")
        return result
    repo = source.repo
    now = result["current"] = _current_commit(repo)
    code, text = _git(repo, ["fetch", "--quiet"])
    if code != 0:
        message = f"⚠️ 无法 fetch 远端(检查网络/远端配置)。当前: {now}"
        result.update(returncode=1, notes=[message])
        _step(result, on_step, "fetch", code=code, text=text, cli=message)
    else:
        _step(result, on_step, "fetch", text=text)
        behind_code, behind = _git(repo, ["rev-list", "--count", "HEAD..@{u}"])
        if behind_code != 0 or not behind.isdigit():
            message = f"⚠️ 无法判断更新:当前分支可能未设上游/远端无此分支。当前: {now}"
            result["notes"].append(message)
        elif behind == "0":
            message = f"✅ 已是最新({now}),无需更新。"
        else:
            result["update_available"] = True
            message = f"有更新可用:落后远端 {behind} 个提交(当前 {now})。跑 `my-agent update` 升级。"
        _step(result, on_step, "availability", code=behind_code, text=behind, cli=message)
        if inspect_worktree and behind_code == 0 and behind.isdigit():
            latest_code, latest = _git(repo, ["rev-parse", "--short", "@{u}"])
            if latest_code == 0:
                result["latest"] = latest[:64]
    if inspect_worktree:
        dirty_code, dirty = _git(repo, ["status", "--porcelain"])
        result["dirty"] = dirty_code != 0 or bool(dirty)
        if dirty_code != 0:
            result["notes"].append("无法核验工作区状态，远程更新将拒绝执行。")
    return result


def _clean_worktree(repo: Path, result: dict, callback) -> bool:
    code, text = _git(repo, ["status", "--porcelain"])
    _step(result, callback, "worktree", code=code, text=text)
    if code != 0 or text:
        result.update(returncode=1, reason="worktree_dirty" if code == 0 else "worktree_status_unavailable")
        return False
    return True


def apply_update_steps(repo: Path, *, on_step: Callable[[dict], None] | None = None,
                       refuse_dirty: bool = False, pre_commit: str | None = None) -> dict:
    """共享 git/pip 主链路；CLI 不新增 dirty 拒绝，远程入口显式选择保守检查。"""
    repo = Path(repo)
    result = {"returncode": 0, "steps": [], "pre_commit": "", "post_commit": ""}
    if refuse_dirty and not _clean_worktree(repo, result, on_step):
        return result
    before = result["pre_commit"] = pre_commit or _current_commit(repo)
    _step(result, on_step, "start", cli=f"当前: {repo} @ {before}\n拉取最新代码…")
    code, text = _git(repo, ["pull", "--ff-only"])
    if code != 0:
        result["returncode"] = 1
        _step(result, on_step, "pull", code=code, text=text,
              cli=f"❌ git pull 失败(本地可能有改动/分叉):\n{text}\n请手动处理后重试。")
        return result
    _step(result, on_step, "pull", text=text, cli="刷新依赖…")
    pip_code, pip_text = _pip_install_editable(repo)
    if pip_code != 0:
        result["returncode"] = 1
        _step(result, on_step, "install", code=pip_code, text=pip_text,
              cli=f"⚠️ 代码已更新但依赖刷新失败:\n{pip_text}\n手动跑 `pip install -e {repo}`。")
        return result
    _step(result, on_step, "install", text=pip_text)
    after = result["post_commit"] = _current_commit(repo)
    message = (f"✅ 已是最新({after}),无变化。" if before == after
               else f"✅ 更新完成:{before} → {after}。")
    _step(result, on_step, "complete", cli=message)
    return result


def rollback_update_steps(repo: Path, pre_commit: str, *,
                          on_step: Callable[[dict], None] | None = None) -> dict:
    """只回退准确提交并复用 editable 安装；拒绝脏工作区，绝不 reset --hard。"""
    repo = Path(repo)
    result = {"returncode": 0, "steps": [], "pre_commit": pre_commit, "post_commit": ""}
    if type(pre_commit) is not str or re.fullmatch(r"[0-9a-fA-F]{7,64}", pre_commit) is None:
        result.update(returncode=1, reason="invalid_pre_commit")
        return result
    if not _clean_worktree(repo, result, on_step):
        return result
    code, text = _git(repo, ["reset", "--keep", pre_commit])
    _step(result, on_step, "reset", code=code, text=text)
    if code != 0:
        result["returncode"] = 1
        return result
    pip_code, pip_text = _pip_install_editable(repo)
    _step(result, on_step, "install", code=pip_code, text=pip_text)
    result["returncode"] = 1 if pip_code else 0
    if not pip_code:
        result["post_commit"] = _current_commit(repo)
        _step(result, on_step, "complete", cli=f"✅ 已回退到 {pre_commit}。")
    return result


__all__ = ["InstallSource", "add_update_subcommand", "cmd_update", "detect_install_source",
           "update_status", "apply_update_steps", "rollback_update_steps"]


# LLM: 从当前安装 runtime 解析原标记，数据目录或 HOME 不是安装身份；exec 替换 updater，避免它自身阻挡切换。
# 函数用途: 校验离线更新入口并交给包内原安装器，联网和源码更新仍走原 git 路径。
def _offline_update(package: Path) -> int:
    import json
    package = package.expanduser().resolve()
    if not package.is_file() or package.suffix != '.run':
        print('请指定存在的离线 .run 更新包。')
        return 1
    executable = Path(sys.executable).resolve()
    for prefix in executable.parents:
        marker = prefix / '.my-agent-private-install.json'
        if marker.is_file():
            row = json.loads(marker.read_text(encoding='utf-8'))
            if row.get('schema') != 'my-agent.private-release.v1' or row.get('owner_uid') != os.getuid() or row.get('prefix') != str(prefix):
                print('当前离线安装标记不一致，未更新。')
                return 1
            os.execvp('bash', ['bash', str(package), '--update', '--prefix', str(prefix)])
    print('当前程序不是离线安装实例，请用 bash 新包.run --update 识别已有实例。')
    return 1
