"""可选的只读 git 进展观察：只看已登记、在允许根内的仓库，时间和输出都有上限。"""

# LLM: 默认关闭——只有 capability 配置 orchestration_git_progress_repos 显式登记的仓库才会被观察；模型或请求参数
#   不能指定任意路径。仓库路径不得含符号链接、必须归当前用户、在 agent.workspace_roots 之内，.git 必须是真目录。
#   命令用固定参数列表（不经 shell、不拼接用户输入），清空继承的 GIT_* 环境、关掉系统/全局配置与 fsmonitor，
#   加 --no-optional-locks 不写索引锁；每条命令有超时，输出读到上限即停。任何不满足都返回 error_code，fail closed。
#   ORCH03-R5：git-dir 与工作树用命令行钉死在已校验路径上（压过本地 core.worktree），commondir/alternates 这类
#   仓库结构重定向和本地配置 include 一律拒绝，读外部文件的路径型配置用 -c 改成空设备。
#   ORCH03-R6（PRO-MSG-022/023 修正）：只支持 POSIX，其它平台在启动任何进程前拒绝。git 以新会话启动，进程组号
#   等于根 pid；清理只针对这个自己创建的组：超时/读满时趁根进程还没被回收（pid 被占着、不可能被复用）对组发
#   SIGKILL，正常结束则先回收根进程拿真实退出码；之后用 killpg(组, 0) 探测，直到 ESRCH 证明组里一个成员都不剩，
#   截止前证明不了就是 GIT_CLEANUP_UNCONFIRMED。不依赖 ps 采集启动身份。识别、读取、清理、确认共用同一个绝对
#   截止时间。主线程非阻塞读取，不起读线程；HEAD 必须是完整提交号，截断或乱码一律拒绝。
#   已知限制：自己另开会话/进程组逃出去的后代不在组里，看不到也杀不到（不会卡住观察）。
# 模块用途: 给进展分类提供一份可核验的 HEAD/改动数观察，不代表进展本身。

from __future__ import annotations

import os
import re
import signal
import subprocess
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
# LLM: 读取最晚在总截止前这么多秒结束，留给最后一条命令的组清理与确认；清理本身也受同一绝对截止约束。
# 常量用途: 为 git 进程组清理与确认预留的时长。
GIT_CLEANUP_RESERVE_SECONDS = 2
_BASELINE = re.compile(r"[0-9a-f]{7,64}")
# SHA-1 / SHA-256 仓库的完整提交号；截断或带其它字符的 HEAD 都不是有效观察。
_FULL_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# git-dir 里能把读取引到别处的结构文件；存在即拒绝，不尝试判断指向哪里。
_GIT_DIR_REDIRECTS = ("commondir", "objects/info/alternates")
# git-dir 里必须是真实文件/目录（不是符号链接）的入口。
_GIT_DIR_NO_SYMLINK = ("config", "config.worktree", "HEAD", "objects", "refs")
# 只有 POSIX 有经过验证的会话组清理合同；其它平台在启动任何进程前拒绝。
_PLATFORM_SUPPORTED = os.name == "posix"
# 组清理确认的探测间隔。
_GROUP_PROBE_INTERVAL_SECONDS = 0.02


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
    if not _PLATFORM_SUPPORTED:
        # 没有经过验证的进程树清理合同，不启动任何 git 进程，也不编造清理确认。
        return _error("GIT_PLATFORM_UNSUPPORTED")
    deadline = time.monotonic() + GIT_OBSERVATION_BUDGET_SECONDS
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


# LLM: 固定参数列表 + 清洁环境 + 输出上限；git-dir/工作树钉在已校验路径，路径型配置改成空设备。读取最晚在
#   deadline − GIT_CLEANUP_RESERVE_SECONDS 结束，组清理与确认最晚到 deadline；清理没确认就不交出输出。
# 函数用途: 运行一条只读 git 命令并返回有界输出。
def _git(repo: Path, args: list[str], deadline: float) -> dict[str, object]:
    read_until = min(time.monotonic() + GIT_COMMAND_TIMEOUT_SECONDS, deadline - GIT_CLEANUP_RESERVE_SECONDS)
    if read_until <= time.monotonic():
        return {"error_code": "GIT_TIMEOUT", "stdout": b"", "truncated": False}
    command = ["git", "--no-optional-locks", f"--git-dir={repo / '.git'}", f"--work-tree={repo}",
               "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
               "-c", f"core.excludesFile={os.devnull}", "-c", f"core.attributesFile={os.devnull}",
               "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *args]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=_clean_env(), close_fds=True,
                                   start_new_session=True)
    except OSError:
        return {"error_code": "GIT_UNAVAILABLE", "stdout": b"", "truncated": False}
    group = process.pid
    output, truncated, timed_out = b"", False, True
    try:
        output, truncated, timed_out = _read_bounded(process, read_until)
    finally:
        confirmed = _settle_group(process, group, deadline, kill=timed_out or truncated)
        if process.stdout is not None:
            process.stdout.close()
    if not confirmed:
        return {"error_code": "GIT_CLEANUP_UNCONFIRMED", "stdout": b"", "truncated": False}
    if timed_out:
        return {"error_code": "GIT_TIMEOUT", "stdout": b"", "truncated": False}
    if process.returncode != 0 and not truncated:
        return {"error_code": "GIT_FAILED", "stdout": b"", "truncated": False}
    return {"error_code": "", "stdout": output, "truncated": truncated}


# LLM: 主线程非阻塞读，读到 EOF、上限或截止即返回，不起后台线程、不回收根进程；继承了 stdout 的后代一直不关
#   管道也只会等到截止，随后由调用方清组。
# 函数用途: 在截止时间和字节上限内读取子进程输出。
def _read_bounded(process: subprocess.Popen, until: float) -> tuple[bytes, bool, bool]:
    import selectors

    assert process.stdout is not None
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    buffer = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while len(buffer) < MAX_GIT_OUTPUT_BYTES:
            left = until - time.monotonic()
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


# LLM: 只对自己创建的会话组发信号。kill=True 时趁根进程未回收先对组发 SIGKILL（组号就是被占着的根 pid）；
#   否则先等根进程自己退出拿真实退出码，到截止仍不退出就转为强杀。回收根进程后用 killpg(组, 0) 探测：ESRCH
#   证明组里不剩任何成员；还有成员（含僵尸，macOS 对纯僵尸组回 EPERM）就再发一次 SIGKILL 并继续等，截止前没
#   等到 ESRCH 就返回 False。组里还有成员时组号不会被分配给新进程，所以补发的信号不会落到无关进程上。
# 函数用途: 清掉 git 进程组并证明组已为空。
def _settle_group(process: subprocess.Popen, group: int, deadline: float, *, kill: bool) -> bool:
    if not kill:
        try:
            process.wait(timeout=max(0.0, deadline - GIT_CLEANUP_RESERVE_SECONDS - time.monotonic()))
        except subprocess.TimeoutExpired:
            kill = True
    if kill and process.returncode is None:
        _signal_group(group, signal.SIGKILL)
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
    resent = False
    while True:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if not resent:
            _signal_group(group, signal.SIGKILL)
            resent = True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_GROUP_PROBE_INTERVAL_SECONDS)


# LLM: 组已经没了（ESRCH）或只剩僵尸（macOS EPERM）都不是错误，确认交给 killpg(组, 0) 探测。
# 函数用途: 向 git 进程组发一个信号。
def _signal_group(group: int, sig: int) -> None:
    try:
        os.killpg(group, sig)
    except (ProcessLookupError, PermissionError):
        pass


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
    # 不用 realpath：符号链接已在启动前拒绝，这里只认字面上就在本仓库 .git 下的两个文件，不把外部目标算成本地。
    allowed = {os.path.normpath(repo / ".git" / "config"), os.path.normpath(repo / ".git" / "config.worktree")}
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
        if os.path.normpath(path if os.path.isabs(path) else repo / path) not in allowed:
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
