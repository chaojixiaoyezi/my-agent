"""可选的只读 git 进展观察：只看已登记、在允许根内的仓库，时间和输出都有上限。"""

# LLM: 默认关闭——只有 capability 配置 orchestration_git_progress_repos 显式登记的仓库才会被观察；模型或请求参数
#   不能指定任意路径。仓库路径不得含符号链接、必须归当前用户、在 agent.workspace_roots 之内，.git 必须是真目录。
#   命令用固定参数列表（不经 shell、不拼接用户输入），清空继承的 GIT_* 环境、关掉系统/全局配置与 fsmonitor，
#   加 --no-optional-locks 不写索引锁；每条命令有超时，输出读到上限即停。任何不满足都返回 error_code，fail closed。
# 模块用途: 给进展分类提供一份可核验的 HEAD/改动数观察，不代表进展本身。

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

# LLM: 单条 git 命令的超时上限；一次观察最多三条命令，整体再受总预算约束。
# 常量用途: 限制单条 git 命令的运行时长。
GIT_COMMAND_TIMEOUT_SECONDS = 5
# LLM: 一次观察（全部命令）的总时长预算，后面的命令只能用剩余时间。
# 常量用途: 限制一次 git 观察的总时长。
GIT_OBSERVATION_BUDGET_SECONDS = 10
# LLM: 每条命令最多读取这么多输出字节，超出即停止进程，改动数标成下限。
# 常量用途: 限制一次 git 命令读取的输出量。
MAX_GIT_OUTPUT_BYTES = 64 * 1024
_BASELINE = re.compile(r"[0-9a-f]{7,64}")


# LLM: 只认配置里的登记列表；登记项本身也要满足同样的路径校验，不能借登记绕过允许根。
# 函数用途: 从 agent 的能力配置与工作区根观察一个已登记仓库。
def observe_registered(agent: Any, repo: str, *, baseline: str = "") -> dict[str, object]:
    from ...capability import runtime_config_reload

    config = runtime_config_reload.capability_config_for_agent(agent)
    registered = getattr(config, "orchestration_git_progress_repos", None) if config is not None else None
    if not isinstance(registered, list) or not registered:
        return _error("GIT_ADAPTER_DISABLED")
    if not isinstance(repo, str) or repo not in [item for item in registered if isinstance(item, str)]:
        return _error("REPO_NOT_REGISTERED")
    roots = [Path(item) for item in getattr(agent, "workspace_roots", None) or []]
    checked = validate_repo(Path(repo), roots)
    if isinstance(checked, str):
        return _error(checked)
    return observe(checked, baseline=baseline)


# LLM: 路径必须是绝对路径且与其 realpath 完全相同（没有任何符号链接分量），归当前用户，位于某个允许根之内；
#   .git 必须是真目录（gitfile 形式的工作树一律拒绝，避免被指到别处）。
# 函数用途: 校验仓库路径，返回规范路径或拒绝原因码。
def validate_repo(repo: Path, allowed_roots: list[Path]) -> Path | str:
    if not repo.is_absolute():
        return "REPO_NOT_ABSOLUTE"
    try:
        real = Path(os.path.realpath(repo))
        if real != repo:
            return "REPO_SYMLINK"
        info = os.lstat(repo)
        git_dir = os.lstat(repo / ".git")
    except OSError:
        return "REPO_NOT_A_GIT_DIR"
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return "REPO_OWNER_MISMATCH"
    import stat

    if not stat.S_ISDIR(info.st_mode) or not stat.S_ISDIR(git_dir.st_mode):
        return "REPO_NOT_A_GIT_DIR"
    for root in allowed_roots:
        try:
            resolved_root = Path(os.path.realpath(root))
        except OSError:
            continue
        if real == resolved_root or resolved_root in real.parents:
            return real
    return "REPO_OUTSIDE_ALLOWED_ROOTS"


# LLM: 已校验路径上的只读观察；baseline 只接受十六进制提交号，不接受分支名或任何选项样式输入。
# 函数用途: 读取 HEAD、相对基线的提交数与改动文件数。
def observe(repo: Path, *, baseline: str = "") -> dict[str, object]:
    if baseline and not _BASELINE.fullmatch(baseline):
        return _error("BASELINE_INVALID")
    deadline = time.monotonic() + GIT_OBSERVATION_BUDGET_SECONDS
    head = _git(repo, ["rev-parse", "--verify", "HEAD"], deadline)
    if head["error_code"]:
        return _error(str(head["error_code"]))
    status = _git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=normal"], deadline)
    if status["error_code"]:
        return _error(str(status["error_code"]))
    entries = [item for item in bytes(status["stdout"]).split(b"\0") if item]
    result: dict[str, object] = {
        "head": bytes(head["stdout"]).decode("ascii", "replace").strip(),
        "dirty_paths_count": len(entries),
        "dirty_count_is_lower_bound": bool(status["truncated"]),
        "ahead_of_baseline": None,
        "observed_at": round(time.time(), 3),
        "error_code": "",
    }
    if baseline:
        ahead = _git(repo, ["rev-list", "--count", f"{baseline}..HEAD"], deadline)
        if ahead["error_code"]:
            result["ahead_error_code"] = ahead["error_code"]
        else:
            text = bytes(ahead["stdout"]).decode("ascii", "replace").strip()
            result["ahead_of_baseline"] = int(text) if text.isdigit() else None
    return result


# LLM: 固定参数列表 + 清洁环境 + 超时 + 输出上限；超时或超量都会结束子进程，不留后台进程。
# 函数用途: 运行一条只读 git 命令并返回有界输出。
def _git(repo: Path, args: list[str], deadline: float) -> dict[str, object]:
    remaining = min(GIT_COMMAND_TIMEOUT_SECONDS, deadline - time.monotonic())
    if remaining <= 0:
        return {"error_code": "GIT_TIMEOUT", "stdout": b"", "truncated": False}
    command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               "-C", str(repo), *args]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=_clean_env(), close_fds=True)
    except OSError:
        return {"error_code": "GIT_UNAVAILABLE", "stdout": b"", "truncated": False}
    try:
        output, truncated = _read_bounded(process, remaining)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return {"error_code": "GIT_TIMEOUT", "stdout": b"", "truncated": False}
    if process.returncode != 0 and not truncated:
        return {"error_code": "GIT_FAILED", "stdout": b"", "truncated": False}
    return {"error_code": "", "stdout": output, "truncated": truncated}


# LLM: 读取放在守护线程里，主线程按超时等待；超时或读满上限都结束子进程，挂住不出输出的 git 也不会卡住调用方。
#   跨平台（不依赖对管道 select）。
# 函数用途: 在时长和字节上限内读取子进程输出。
def _read_bounded(process: subprocess.Popen, timeout: float) -> tuple[bytes, bool]:
    assert process.stdout is not None
    buffer = bytearray()

    # LLM: 只往本地缓冲追加，读到上限或管道关闭即退出。
    # 函数用途: 在后台线程里有界读取输出。
    def reader() -> None:
        while len(buffer) < MAX_GIT_OUTPUT_BYTES:
            chunk = process.stdout.read1(8192)
            if not chunk:
                return
            buffer.extend(chunk)

    started = time.monotonic()
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        process.kill()
        process.wait()
        thread.join(1)
        raise subprocess.TimeoutExpired(process.args, timeout)
    truncated = len(buffer) >= MAX_GIT_OUTPUT_BYTES
    if truncated:
        process.kill()
    try:
        process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    return bytes(buffer[:MAX_GIT_OUTPUT_BYTES]), truncated


# LLM: 只保留定位 git 所需的 PATH 和语言设置，屏蔽系统/全局配置与交互提示，不继承任何 GIT_* 变量。
# 函数用途: 构造运行 git 的最小环境。
def _clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "SYSTEMROOT"}}
    env.update(LC_ALL="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               HOME=os.devnull)
    return env


# LLM: 失败结果也带观察时间，供父级区分“git 不可用”与“有进展”。
# 函数用途: 组装拒绝或失败的观察结果。
def _error(code: str) -> dict[str, object]:
    return {"head": "", "dirty_paths_count": None, "ahead_of_baseline": None,
            "observed_at": round(time.time(), 3), "error_code": code}


__all__ = ["GIT_COMMAND_TIMEOUT_SECONDS", "MAX_GIT_OUTPUT_BYTES", "observe", "observe_registered", "validate_repo"]
