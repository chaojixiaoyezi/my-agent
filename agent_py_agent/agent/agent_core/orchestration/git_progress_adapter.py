"""可选的只读 git 进展观察：只看已登记、在允许根内的仓库，时间和输出都有上限。"""

# LLM: 默认关闭——只有 capability 配置 orchestration_git_progress_repos 显式登记的仓库才会被观察；模型或请求参数
#   不能指定任意路径。仓库路径不得含符号链接、必须归当前用户、在 agent.workspace_roots 之内，.git 必须是真目录。
#   命令用固定参数列表（不经 shell、不拼接用户输入），清空继承的 GIT_* 环境、关掉系统/全局配置与 fsmonitor，
#   加 --no-optional-locks 不写索引锁；每条命令有超时，输出读到上限即停。任何不满足都返回 error_code，fail closed。
#   ORCH03-R5：git-dir 与工作树用命令行钉死在已校验路径上（压过本地 core.worktree），commondir/alternates 这类
#   仓库结构重定向和本地配置 include 一律拒绝，读外部文件的路径型配置用 -c 改成空设备。
#   ORCH03-R6（PRO-MSG-022/023/027/032 修正）：git 以新会话启动，进程组号等于根 pid。任何破坏性信号都只在“钉住”
#   被当场证明时发：根进程仍是本进程未回收的子进程（Linux waitid WNOWAIT 成功；macOS libproc 按父进程列出的子进程
#   里仍有它），且 SIGCHLD 没被设成忽略（否则内核自动回收，钉不住，启动前就拒绝）。钉住分三态：RUNNING_PINNED /
#   EXITED_UNREAPED / 身份丢失或不明；后者一律不发破坏性信号，只做无破坏回收与探测并判 GIT_CLEANUP_UNCONFIRMED。
#   正常结束时先不回收地等根进程退出，趁它还是僵尸查组里活成员（Linux 流式扫 /proc、受同一截止约束；macOS libproc
#   按组列成员并逐个读状态；EPERM 本身不算“只剩僵尸”的证明），有活成员就当场对组 SIGKILL 并判不健康，查不清也判
#   未确认。之后才回收根进程拿真实退出码，回收后只做 killpg(组, 0) 探测直到 ESRCH，绝不再发信号。不依赖 ps。识别、
#   读取、清理、确认共用同一个绝对截止时间，等待都按剩余时间截断。stdout 在清理前单独关闭，清理异常也不漏关。
#   主线程非阻塞读取，不起读线程；HEAD 必须是完整提交号。没有这些原语的平台在启动任何进程前拒绝。
#   残余前提：同一进程里若有别的代码用 waitpid(-1)/os.wait() 抢先回收，只能在下一次钉住检查时发现（身份丢失）。
#   已知限制：组为空只证明“本组”没有成员；自己另开会话/进程组逃出去的后代不在组里，看不到也杀不到。
# 模块用途: 给进展分类提供一份可核验的 HEAD/改动数观察，不代表进展本身。

from __future__ import annotations

import ctypes
import errno
import os
import re
import select
import signal
import subprocess
import sys
import time
from functools import lru_cache
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
# 需要两样原语：不回收地等根进程退出（waitid WNOWAIT 或 kqueue NOTE_EXIT），以及在根进程未回收时查组里的活成员
# （Linux /proc 或 macOS 纯僵尸组的 EPERM 语义）。缺一样就在启动任何进程前拒绝。
_IS_DARWIN = sys.platform == "darwin"
_HAS_PROC = Path("/proc/self/stat").exists()
_PLATFORM_SUPPORTED = os.name == "posix" and (
    (hasattr(os, "waitid") and hasattr(os, "WNOWAIT") and _HAS_PROC)
    or (_IS_DARWIN and hasattr(select, "kqueue"))
)
# 根进程钉住状态：只有前两种允许发破坏性组信号。
_RUNNING_PINNED = "running_pinned"
_EXITED_UNREAPED = "exited_unreaped"
_IDENTITY_LOST = "identity_lost_or_unknown"
# Linux 上查组成员时最多扫这么多个 /proc 条目，超出就算“不知道”，观察判为清理未确认。
_MAX_PROC_SCAN_ENTRIES = 65536
# 常量用途: 单个 /proc/<pid>/stat 最多读的字节数；读满视为解析不了（查不清），不当作不存在。
_MAX_PROC_STAT_BYTES = 4096
# 常量用途: macOS libproc 一次列 pid 的缓冲条数；写满视为列不全。
_DARWIN_LIST_CAPACITY_COUNT = 4096
_HAS_WAITID = hasattr(os, "waitid") and hasattr(os, "WNOWAIT")
# libproc.h 的接口编号（协议值，不是可调参数）：按进程组 / 按父进程列 pid，以及 BSD 信息查询号。
_LIBPROC_PROTOCOL_PGRP_ONLY = 2
_LIBPROC_PROTOCOL_PPID_ONLY = 6
_LIBPROC_PROTOCOL_PIDTBSDINFO = 3
# 组清理确认的探测间隔。
_GROUP_PROBE_INTERVAL_SECONDS = 0.02


# LLM: 只认配置里的登记列表；登记项本身也要满足同样的路径校验，不能借登记绕过允许根。
# 函数用途: 从 agent 的能力配置与工作区根观察一个已登记仓库。
def observe_registered(agent: Any, repo: str, *, baseline: str = "") -> dict[str, object]:
    from ...capability import runtime_config_reload

    config = runtime_config_reload.capability_config_for_agent(agent)
    # ORCH03-R3：总开关必须严格为 True；关着时即使登记了仓库也不校验、不启动 git。
    if getattr(config, "orchestration_git_progress_enabled", None) is not True:
        return _error("GIT_ADAPTER_DISABLED")
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


# LLM: ORCH03-R2：关联只来自子代理任务记录里的 allowed_write_roots（权威写入范围），不看 workspace_refs 投影或模型
#   输入。每个写入根取包含它的最深已校验仓库（嵌套时取更具体的）；不在任何已登记仓库内的写入根（例如宿主分给每个
#   子代理的私有工作目录）与仓库信号无关，不参与关联。规则：落到恰好一个仓库 → exact；落到多个不同仓库 → ambiguous；
#   一个都没有 → none；同一仓库 exact 关联到多个子代理 → shared（谁的改动说不清，一律不提升）。
#   返回 run_id -> {"association": exact|shared|ambiguous|none, "repo": 路径或 ""}。
# 函数用途: 把已校验仓库关联到子代理。
def associate_repos(write_roots_by_run: dict[str, list[str]], repos: list[Path]) -> dict[str, dict[str, str]]:
    ordered = sorted(repos, key=lambda item: len(item.parts), reverse=True)
    result: dict[str, dict[str, str]] = {}
    for run_id, roots in write_roots_by_run.items():
        owners = set()
        for raw in roots:
            if not isinstance(raw, str) or not os.path.isabs(raw):
                continue
            try:
                real = Path(os.path.realpath(raw))
            except OSError:
                continue
            match = next((repo for repo in ordered if real == repo or repo in real.parents), None)
            if match is not None:
                owners.add(match)
        if not owners:
            result[run_id] = {"association": "none", "repo": ""}
        elif len(owners) > 1:
            result[run_id] = {"association": "ambiguous", "repo": ""}
        else:
            result[run_id] = {"association": "exact", "repo": str(next(iter(owners)))}
    counts: dict[str, int] = {}
    for item in result.values():
        if item["association"] == "exact":
            counts[item["repo"]] = counts.get(item["repo"], 0) + 1
    for item in result.values():
        if item["association"] == "exact" and counts[item["repo"]] > 1:
            item["association"] = "shared"
    return result


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
def observe(repo: Path, *, baseline: str = "", deadline: float | None = None) -> dict[str, object]:
    if baseline and not _BASELINE.fullmatch(baseline):
        return _error("BASELINE_INVALID")
    if not _PLATFORM_SUPPORTED or (_IS_DARWIN and _libproc() is None):
        # 没有经过验证的进程树清理合同，不启动任何 git 进程，也不编造清理确认。
        return _error("GIT_PLATFORM_UNSUPPORTED")
    if not _reaper_exclusive():
        # SIGCHLD 被忽略时内核会自动回收子进程，根进程钉不住，任何组信号都不安全。
        return _error("GIT_REAPER_UNAVAILABLE")
    # 调用方（list_agents）可给整次调用共用的绝对截止；本次观察取两者中更早的那个。
    own = time.monotonic() + GIT_OBSERVATION_BUDGET_SECONDS
    deadline = own if deadline is None else min(own, deadline)
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
        try:
            if process.stdout is not None:
                process.stdout.close()
        finally:
            settled = _settle_group(process, group, deadline, kill=timed_out or truncated)
    if settled == "unconfirmed":
        return {"error_code": "GIT_CLEANUP_UNCONFIRMED", "stdout": b"", "truncated": False}
    if timed_out:
        return {"error_code": "GIT_TIMEOUT", "stdout": b"", "truncated": False}
    if settled == "descendants":
        # 正常 EOF 但 git 留下了还活着的后代：已在根进程被占着时清掉，这次输出不当作健康观察。
        return {"error_code": "GIT_DESCENDANTS_LEFT", "stdout": b"", "truncated": False}
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


# LLM: 两阶段，破坏性信号只出现在第一阶段且每次发之前都当场重证钉住（_signal_pinned）。kill=True（读超时/超量）
#   直接对组 SIGKILL；否则先不回收地等根进程退出（到读截止仍在运行就转强杀），成了僵尸后查组里活成员，有或查不清
#   都对组 SIGKILL（钉住时组号不会被复用，安全），查不清另判未确认。身份丢失或不明：不发任何破坏性信号，只做
#   无破坏回收，判未确认。第二阶段回收根进程拿真实退出码，之后只用 killpg(组, 0) 探测到 ESRCH。内部 OSError 一律
#   按未确认收尾，不向上抛。
# 函数用途: 清掉 git 进程组并返回 clean / descendants / unconfirmed。
def _settle_group(process: subprocess.Popen, group: int, deadline: float, *, kill: bool) -> str:
    try:
        return _settle(process, group, deadline, kill)
    except OSError:
        _reap(process, deadline)
        return "unconfirmed"


def _settle(process: subprocess.Popen, group: int, deadline: float, kill: bool) -> str:
    descendants = False
    if not kill:
        state = _wait_root_exit_unreaped(process, deadline - GIT_CLEANUP_RESERVE_SECONDS)
        if state == _IDENTITY_LOST:
            _reap(process, deadline)
            return "unconfirmed"
        kill = state == _RUNNING_PINNED
    if kill:
        if not _signal_pinned(process, group):
            _reap(process, deadline)
            return "unconfirmed"
    else:
        live = _group_has_live_members(group, process.pid, deadline)
        if live is not False:
            descendants = True
            if not _signal_pinned(process, group) or live is None:
                # 查不清组成员：即便已在钉住时强杀，也不能说成干净。
                _reap(process, deadline)
                return "unconfirmed"
    if not _reap(process, deadline):
        return "unconfirmed"
    while True:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            return "descendants" if descendants else "clean"
        except PermissionError:
            pass
        left = deadline - time.monotonic()
        if left <= 0:
            return "unconfirmed"
        time.sleep(min(_GROUP_PROBE_INTERVAL_SECONDS, left))


# LLM: 第二阶段入口；回收之后组号不再被根进程占着，调用方不得再发破坏性信号。身份丢失时 Popen 也只做无破坏等待。
# 函数用途: 在截止前回收根进程。
def _reap(process: subprocess.Popen, deadline: float) -> bool:
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return True


# LLM: 唯一的破坏性信号出口：发之前当场重证钉住，只有 RUNNING_PINNED / EXITED_UNREAPED 才发 SIGKILL。EPERM（组里
#   没有可杀的活成员，或成员不归我们）不当错误，留给回收后的探测去证明；钉住期间出现 ESRCH 与其它错误都算查不清。
# 函数用途: 在钉住被证明时向 git 进程组发 SIGKILL。
def _signal_pinned(process: subprocess.Popen, group: int) -> bool:
    if _root_state(process) == _IDENTITY_LOST:
        return False
    try:
        os.killpg(group, signal.SIGKILL)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# LLM: 内核事实而非缓存：returncode 已有值、SIGCHLD 被忽略、waitid 报不是子进程或出错、libproc 父子关系列不全或
#   不含它，都算身份丢失或不明。Linux 用 waitid WNOWAIT（成功即证明仍是未回收的子进程）；macOS 用 libproc 按本进程
#   列子进程（含僵尸，回收后消失），再读状态区分运行中与已退出（僵尸读不到 BSD 信息，返回 ESRCH）。
# 函数用途: 当场判定根进程的钉住状态。
def _root_state(process: subprocess.Popen) -> str:
    if process.returncode is not None or not _reaper_exclusive():
        return _IDENTITY_LOST
    pid = process.pid
    if _HAS_WAITID:
        try:
            result = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except OSError:
            return _IDENTITY_LOST
        if result is None:
            return _RUNNING_PINNED
        return _EXITED_UNREAPED if result.si_pid == pid else _IDENTITY_LOST
    running = _darwin_running(pid)
    children = _darwin_list(_LIBPROC_PROTOCOL_PPID_ONLY, os.getpid())
    if running is None or children is None or pid not in children:
        return _IDENTITY_LOST
    return _RUNNING_PINNED if running else _EXITED_UNREAPED


# LLM: 不回收地等根进程退出；Linux 按剩余时间截断的短轮询，macOS kqueue NOTE_EXIT（对已是僵尸的进程立即触发），
#   每次醒来都用 _root_state 重新取内核事实。到 until 仍在运行返回 RUNNING_PINNED。
# 函数用途: 等根进程退出但不回收它，返回三态之一。
def _wait_root_exit_unreaped(process: subprocess.Popen, until: float) -> str:
    while True:
        state = _root_state(process)
        left = until - time.monotonic()
        if state != _RUNNING_PINNED or left <= 0:
            return state
        if _HAS_WAITID:
            time.sleep(min(_GROUP_PROBE_INTERVAL_SECONDS, left))
            continue
        queue = select.kqueue()
        try:
            event = select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)
            queue.control([event], 1, left)
        except OSError:
            # 注册失败（进程已不在等）交给下一轮 _root_state 判定，不在这里猜。
            time.sleep(min(_GROUP_PROBE_INTERVAL_SECONDS, max(0.0, until - time.monotonic())))
        finally:
            queue.close()


# LLM: 只在根进程钉住（未回收）时调用。返回 True（有活成员）/ False（证明没有）/ None（查不清）。Linux 流式扫 /proc：
#   条目数、单文件字节数和截止时间都有上限，进程在扫描中消失算不存在，其它读失败或 stat 解析不了算查不清。macOS 用
#   libproc 按组列成员，逐个读 BSD 信息：读得到且组号相符是活成员，ESRCH 是僵尸或已消失，其它错误查不清；不拿
#   killpg 的 EPERM 当证明。
# 函数用途: 判断 git 进程组里除根进程外是否还有活着的成员。
def _group_has_live_members(group: int, root_pid: int, deadline: float, proc_root: str = "/proc") -> bool | None:
    if _HAS_PROC:
        return _linux_group_has_live_members(group, root_pid, deadline, proc_root)
    if _IS_DARWIN:
        members = _darwin_list(_LIBPROC_PROTOCOL_PGRP_ONLY, group)
        if members is None:
            return None
        for pid in members:
            if pid == root_pid:
                continue
            info, error = _darwin_info(pid)
            if info is not None and info.scope[1] == group:
                return True
            if info is None and error != errno.ESRCH:
                return None
        return False
    return None


def _linux_group_has_live_members(group: int, root_pid: int, deadline: float, proc_root: str) -> bool | None:
    scanned = 0
    try:
        with os.scandir(proc_root) as entries:
            for entry in entries:
                scanned += 1
                if scanned > _MAX_PROC_SCAN_ENTRIES or time.monotonic() >= deadline:
                    return None
                if not entry.name.isdigit() or int(entry.name) == root_pid:
                    continue
                try:
                    fd = os.open(os.path.join(proc_root, entry.name, "stat"), os.O_RDONLY)
                    try:
                        raw = os.read(fd, _MAX_PROC_STAT_BYTES)
                    finally:
                        os.close(fd)
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except OSError:
                    return None
                fields = raw.decode("ascii", errors="replace").rsplit(")", 1)
                parts = fields[1].split() if len(fields) == 2 and len(raw) < _MAX_PROC_STAT_BYTES else []
                if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
                    return None
                if int(parts[2]) == group and parts[0] != "Z":
                    return True
    except OSError:
        return None
    return False


# LLM: SIGCHLD 被设成忽略时内核自动回收子进程，pid 钉不住；此外不能证明别处没有抢先回收的代码（见模块说明）。
# 函数用途: 检查本进程是否保留了回收子进程的权利。
def _reaper_exclusive() -> bool:
    try:
        return signal.getsignal(signal.SIGCHLD) is not signal.SIG_IGN
    except (AttributeError, ValueError):
        return False


# LLM: Darwin libproc.h 的 proc_bsdinfo 固定 136 字节布局（与 gateway_parts 的出生身份读取同一 ABI）：prefix[1] 状态、
#   scope[1] 组号。这里只读本进程的子进程与本组成员，不枚举其它用户进程；加载失败返回 None，观察在启动前拒绝。
# 类用途: 接收内核的进程 BSD 信息。
class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [("prefix", ctypes.c_uint32 * 12), ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32),
                ("scope", ctypes.c_uint32 * 6), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


@lru_cache(maxsize=1)
def _libproc():
    if not _IS_DARWIN:
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        listpids, pidinfo = library.proc_listpids, library.proc_pidinfo
    except (OSError, AttributeError):
        return None
    listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    listpids.restype = ctypes.c_int
    pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    pidinfo.restype = ctypes.c_int
    return listpids, pidinfo


# LLM: 有界缓冲；返回值为负、置了 errno、写满缓冲或长度不整都算列不全（None），不拿半张表当完整。
# 函数用途: 按父进程或进程组列出 pid。
def _darwin_list(kind: int, identifier: int) -> frozenset[int] | None:
    functions = _libproc()
    if functions is None:
        return None
    buffer = (ctypes.c_int * _DARWIN_LIST_CAPACITY_COUNT)()
    ctypes.set_errno(0)
    size = functions[0](kind, identifier, buffer, ctypes.sizeof(buffer))
    width = ctypes.sizeof(ctypes.c_int)
    if size < 0 or ctypes.get_errno() or size >= ctypes.sizeof(buffer) or size % width:
        return None
    return frozenset(pid for pid in buffer[:size // width] if pid > 0)


# 函数用途: 读一个 pid 的 BSD 信息，返回 (信息或 None, errno)。
def _darwin_info(pid: int) -> tuple[_ProcBSDInfo | None, int]:
    functions = _libproc()
    if functions is None:
        return None, errno.ENOSYS
    info = _ProcBSDInfo()
    ctypes.set_errno(0)
    size = functions[1](pid, _LIBPROC_PROTOCOL_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    return (info, 0) if size == ctypes.sizeof(info) else (None, ctypes.get_errno() or errno.EIO)


# 函数用途: 根进程是否在运行：读得到 BSD 信息为运行中，ESRCH 为已退出（僵尸），其它为不明（None）。
def _darwin_running(pid: int) -> bool | None:
    info, error = _darwin_info(pid)
    if info is not None:
        return True
    return False if error == errno.ESRCH else None


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


__all__ = ["GIT_CLEANUP_RESERVE_SECONDS", "associate_repos", "GIT_COMMAND_TIMEOUT_SECONDS", "MAX_GIT_OUTPUT_BYTES", "observe", "observe_registered", "validate_repo"]
