"""可选的只读 git 进展观察：只看已登记、在允许根内的仓库，时间和输出都有上限。"""

# LLM: 默认关闭——只有 capability 配置 orchestration_git_progress_repos 显式登记的仓库才会被观察；模型或请求参数
#   不能指定任意路径。仓库路径不得含符号链接、必须归当前用户、在 agent.workspace_roots 之内，.git 必须是真目录。
#   命令用固定参数列表（不经 shell、不拼接用户输入），清空继承的 GIT_* 环境、关掉系统/全局配置与 fsmonitor，
#   加 --no-optional-locks 不写索引锁；每条命令有超时，输出读到上限即停。任何不满足都返回 error_code，fail closed。
#   ORCH03-R5：git-dir 与工作树用命令行钉死在已校验路径上（压过本地 core.worktree），commondir/alternates 这类
#   仓库结构重定向和本地配置 include 一律拒绝，读外部文件的路径型配置用 -c 改成空设备。
#   ORCH03-R6：子进程独立进程组/会话启动，结束时经共享 terminate_process_tree 清整棵树并核对回执，清理没确认
#   就不算成功；POSIX 在主线程用非阻塞读取，不留后台读线程；HEAD 必须是完整提交号，截断或乱码一律拒绝。
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
# LLM: 从总预算里给最后一条命令的进程树清理留出的时间，保证读取加清理不超过总预算。
# 常量用途: 为 git 子进程树清理预留的时长。
GIT_CLEANUP_RESERVE_SECONDS = 3
_BASELINE = re.compile(r"[0-9a-f]{7,64}")
# SHA-1 / SHA-256 仓库的完整提交号；截断或带其它字符的 HEAD 都不是有效观察。
_FULL_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# git-dir 里能把读取引到别处的结构文件；存在即拒绝，不尝试判断指向哪里。
_GIT_DIR_REDIRECTS = ("commondir", "objects/info/alternates")
# git-dir 里必须是真实文件/目录（不是符号链接）的入口。
_GIT_DIR_NO_SYMLINK = ("config", "HEAD", "objects", "refs")


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
    redirect = _git_dir_redirect(repo / ".git")
    if redirect:
        return redirect
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
    deadline = time.monotonic() + GIT_OBSERVATION_BUDGET_SECONDS - GIT_CLEANUP_RESERVE_SECONDS
    config = _git(repo, ["config", "--null", "--list", "--show-origin", "--no-includes"], deadline)
    if config["error_code"]:
        return _error(str(config["error_code"]))
    if config["truncated"] or not _config_is_local_only(bytes(config["stdout"]), repo):
        return _error("REPO_CONFIG_REDIRECT")
    head = _git(repo, ["rev-parse", "--verify", "HEAD"], deadline)
    if head["error_code"]:
        return _error(str(head["error_code"]))
    head_text = bytes(head["stdout"]).decode("ascii", "replace").strip()
    if head["truncated"] or not _FULL_OBJECT_ID.fullmatch(head_text):
        return _error("GIT_HEAD_INVALID")
    status = _git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=all"],
                  deadline)
    if status["error_code"]:
        return _error(str(status["error_code"]))
    entries = [item for item in bytes(status["stdout"]).split(b"\0") if item]
    result: dict[str, object] = {
        "head": head_text,
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
        elif ahead["truncated"]:
            result["ahead_error_code"] = "GIT_OUTPUT_TRUNCATED"
        else:
            text = bytes(ahead["stdout"]).decode("ascii", "replace").strip()
            result["ahead_of_baseline"] = int(text) if text.isdigit() else None
    return result


# LLM: 固定参数列表 + 清洁环境 + 超时 + 输出上限；git-dir/工作树钉在已校验路径，路径型配置改成空设备。
#   子进程独立进程组启动。POSIX：读到 EOF 后按 shell 前台同一路径用 finish_foreground_process 等根进程退出并
#   清掉残留后代；超时或读满则直接 terminate_process_tree。共享清理入口只能在根进程还没被回收时调用一次，所以
#   读取阶段不 wait。回执没确认就返回 GIT_CLEANUP_UNCONFIRMED，不把这次输出当观察。Windows 根进程正常退出后
#   无法再核对后代，只在超时/读满时用 taskkill /T 清树（已知限制，记在 HANDOFF）。
# 函数用途: 运行一条只读 git 命令并返回有界输出。
def _git(repo: Path, args: list[str], deadline: float) -> dict[str, object]:
    from ...tooling.process_registry import (
        capture_process_birth_token,
        finish_foreground_process,
        terminate_process_tree,
    )

    remaining = min(GIT_COMMAND_TIMEOUT_SECONDS, deadline - time.monotonic())
    if remaining <= 0:
        return {"error_code": "GIT_TIMEOUT", "stdout": b"", "truncated": False}
    command = ["git", "--no-optional-locks", f"--git-dir={repo / '.git'}", f"--work-tree={repo}",
               "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               "-c", f"core.excludesFile={os.devnull}", "-c", f"core.attributesFile={os.devnull}",
               "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *args]
    posix = os.name == "posix"
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=_clean_env(), close_fds=True,
                                   **({"start_new_session": True} if posix else
                                      {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}))
    except OSError:
        return {"error_code": "GIT_UNAVAILABLE", "stdout": b"", "truncated": False}
    birth = capture_process_birth_token(process.pid) if posix else None
    started = time.monotonic()
    output, truncated, timed_out, confirmed = b"", False, True, False
    try:
        output, truncated, timed_out = (_read_bounded_posix if posix else _read_bounded_thread)(process, remaining)
        if not timed_out and not truncated:
            # 根进程按原退出码收尾；超过剩余时间仍不退出就当超时，转去强制清树。
            while True:
                code, receipt = finish_foreground_process(process, expected_birth_token=birth)
                if code is not None:
                    confirmed = receipt.confirmed if receipt is not None else True
                    break
                if time.monotonic() - started >= remaining:
                    timed_out = True
                    break
                time.sleep(0.02)
    finally:
        if process.returncode is None:
            confirmed = terminate_process_tree(process.pid, process, grace_seconds=0,
                                               expected_birth_token=birth).confirmed
        if process.stdout is not None:
            process.stdout.close()
    if not confirmed:
        return {"error_code": "GIT_CLEANUP_UNCONFIRMED", "stdout": b"", "truncated": False}
    if timed_out:
        return {"error_code": "GIT_TIMEOUT", "stdout": b"", "truncated": False}
    if process.returncode != 0 and not truncated:
        return {"error_code": "GIT_FAILED", "stdout": b"", "truncated": False}
    return {"error_code": "", "stdout": output, "truncated": truncated}


# LLM: POSIX 管道可 select：主线程非阻塞读，读到 EOF、上限或超时即返回，不起后台线程、不回收根进程；继承了
#   stdout 的后代一直不关管道也只会等到超时，随后由调用方清整棵树。
# 函数用途: 在时长和字节上限内读取子进程输出（POSIX）。
def _read_bounded_posix(process: subprocess.Popen, timeout: float) -> tuple[bytes, bool, bool]:
    import selectors

    assert process.stdout is not None
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    buffer = bytearray()
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while len(buffer) < MAX_GIT_OUTPUT_BYTES:
            left = deadline - time.monotonic()
            if left <= 0:
                return b"", False, True
            if not selector.select(min(left, 0.2)):
                continue
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                continue
            if not chunk:
                break
            buffer.extend(chunk)
    return bytes(buffer[:MAX_GIT_OUTPUT_BYTES]), len(buffer) >= MAX_GIT_OUTPUT_BYTES, False


# LLM: Windows 管道不能 select，只能放到守护线程里按超时等待；调用方随后用 taskkill /T 清整棵树，管道关闭后
#   读线程随之结束。
# 函数用途: 在时长和字节上限内读取子进程输出（Windows）。
def _read_bounded_thread(process: subprocess.Popen, timeout: float) -> tuple[bytes, bool, bool]:
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

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return b"", False, True
    return bytes(buffer[:MAX_GIT_OUTPUT_BYTES]), len(buffer) >= MAX_GIT_OUTPUT_BYTES, False


# LLM: 只看 git-dir 里的结构事实（lstat），不读文件内容判断指向；存在重定向文件或入口是符号链接都拒绝。
# 函数用途: 检查 git-dir 是否含能把读取引出仓库的结构。
def _git_dir_redirect(git_dir: Path) -> str:
    import stat

    for name in _GIT_DIR_REDIRECTS:
        if os.path.lexists(git_dir / name):
            return "REPO_GIT_DIR_REDIRECT"
    for name in _GIT_DIR_NO_SYMLINK:
        try:
            if stat.S_ISLNK(os.lstat(git_dir / name).st_mode):
                return "REPO_GIT_DIR_REDIRECT"
        except OSError:
            continue
    return ""


# LLM: 读 git 自己解析出的 `config --list --show-origin --no-includes`（NUL 分隔：来源、键\n值）：来源只能是命令行
#   或本仓库 .git 下的配置文件，键不能是 include./includeif. 段；全局/系统配置已经由环境关掉。
# 函数用途: 确认 git 实际生效的配置只来自本仓库，没有 include 把读取引到别处。
def _config_is_local_only(raw: bytes, repo: Path) -> bool:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        return False
    allowed = {os.path.realpath(repo / ".git" / "config"), os.path.realpath(repo / ".git" / "config.worktree")}
    for origin_raw, entry_raw in zip(fields[::2], fields[1::2]):
        origin = origin_raw.decode("utf-8", "replace")
        key = entry_raw.split(b"\n", 1)[0].decode("utf-8", "replace").lower()
        if key.startswith(("include.", "includeif.")):
            return False
        if origin == "command line:":
            continue
        if not origin.startswith("file:"):
            return False
        path = origin[len("file:"):]
        if os.path.realpath(path if os.path.isabs(path) else repo / path) not in allowed:
            return False
    return True


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


__all__ = ["GIT_CLEANUP_RESERVE_SECONDS", "GIT_COMMAND_TIMEOUT_SECONDS", "MAX_GIT_OUTPUT_BYTES", "observe", "observe_registered", "validate_repo"]
