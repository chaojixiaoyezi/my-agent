
# LLM: 归属仍读process_scope；只读探针合并ps与微秒birth投影，终止选取强制新鲜关系，未知不冒充死亡或授权。
# 前台自然退出按启动身份清理原组；旧locale仅缓存同PID/原token/当前token的原生复核成功等价，不缓存存活或改写原账。
#   只reap本Registry持有的外层host Popen；终态先写后退的窗口保留待回收句柄，不发信号也不替代整树终止证明。
# 模块用途: 查询或精确停止受管进程，观测有界日志和读取故障；进程身份、退出与清理事实仍由原生命周期入口决定。
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ..common.cancellation import current_cancellation_token, raise_if_cancelled
from ..common.json_io import read_json_object
from .process_scope import ProcessAccessScope
from .process_session_records import (
    LEGACY_PROCESS_SESSION_SCHEMA,
    MANAGED_PROCESS_SESSION_SCHEMAS,
    PROCESS_TERMINAL_STATUSES,
)
from .process_session_store import (
    ProcessSessionStore,
    process_session_error_report,
)

HOST_STATE_SCHEMA = "background_process_host.v1"  # 仅供已发布 v1 记录读取旧终态文件。
_IS_WINDOWS = os.name == "nt"
_DEFAULT_SUBPROCESS_RUN = subprocess.run

# LLM: 只存原生复核成功的精确三元组；锁内合并并发探针，FIFO最多256项，不写磁盘也不代表实例仍存活。
# 缓存用途: 让升级前旧locale指纹与新观察复用已验证的等价关系，未知/不匹配不入缓存。
_LEGACY_BIRTH_CACHE_MAX_COUNT = 256
_LEGACY_BIRTH_EQUIVALENTS: dict[tuple[int, str, str], None] = {}
_LEGACY_BIRTH_CACHE_LOCK = threading.Lock()

# 只缓存内核微秒出生身份到旧host文本的投影，不缓存存活；与旧locale等价证明共用锁合并miss。
_PID_BIRTH_TOKEN_CACHE: OrderedDict[tuple[int, str], str] = OrderedDict()
# 出生身份投影缓存最多保留2048项，不代表实例仍存活。
_PID_BIRTH_TOKEN_CACHE_MAX_COUNT = 2048
_PS_RELATION_CACHE: dict[str, tuple[float, dict[int, int], bool]] = {}
_PS_RELATION_CACHE_LOCK = threading.Lock()
# 只读ps关系表最多复用0.25秒；终止选取仍强制新鲜观察。
_PS_RELATION_CACHE_SECONDS = 0.25
_FRESH_RELATION_MAPS = threading.local()

# 杀进程时 SIGTERM 到 SIGKILL 的宽限秒数。先礼(SIGTERM 让进程自己清理)后兵
# (还活着就 SIGKILL 硬杀整组),避免留下孤儿子进程。
_KILL_GRACE_SECONDS = 3.0
# 没有 Popen 句柄的停止方（如从别的进程停插件宿主）拿不到 wait()；SIGKILL 后只留 0.5 秒核对在慢主机上会漏掉刚死的实例，
# 把它记成未确认（2026-09-25 CI 3.10 复现）。有句柄时 proc.wait(2) 已经等过，这里只需短核对。
_FINAL_CONFIRM_WITH_HANDLE_SECONDS = 0.5
# 无 Popen 句柄的停止方 SIGKILL 后核对 2 秒：慢主机上防漏判刚死的实例（2026-09-25 CI 3.10 复现）。
_FINAL_CONFIRM_WITHOUT_HANDLE_SECONDS = 2.0


# LLM: 该回执只证明本次观察到的进程树是否已终止，不证明命令成功或外部系统已回滚。
# 类用途: 把终止方式、退出码和无法确认的进程带回 shell 与后台进程控制调用方。
@dataclass(frozen=True)
class ProcessTerminationReceipt:
    method: str
    confirmed: bool
    return_code: int | None
    observed_processes: int
    unresolved_pids: tuple[int, ...] = ()
# 滚动输出缓冲:查询时从日志文件尾部最多读这么多字符,够看进度/结论又不撑爆 prompt。
_OUTPUT_TAIL_CHARS = 4000
# 最多保留多少条已结束的进程记录,超了按启动时间淘汰最老的(防内存无界增长)。
_MAX_FINISHED_COUNT = 128


# LLM: 仅观察当前显式wait的单调时长；调用方仍受剩余deadline和原取消令牌约束，不缓存终态。
# 函数用途: 前半秒50ms、三秒内100ms、之后250ms核对一次原会话。
def _process_wait_interval(elapsed_s: float) -> float:
    return 0.05 if elapsed_s < 0.5 else 0.1 if elapsed_s < 3.0 else 0.25


# LLM: 只投影原受管记录是否绑定宿主收件目标，不暴露目标地址，也不声称完成事件已经送达。
# 函数用途: 给启动、状态与等待入口同一份完成唤醒能力事实。
def completion_wake_facts(record: object) -> dict[str, object]:
    enabled = bool(getattr(record, "completion_target", None))
    return {"enabled": enabled, "reason": "managed_process_exited" if enabled else "", "yield_supported": enabled}


# LLM: 结构化错误区别权威损坏和记录不存在，不展开其它会话的记录内容。
# 类用途: 阻止查询或停止在存储故障后继续使用旧缓存。
class ProcessSessionAuthorityError(RuntimeError):
    # LLM: 保留 Store 的脱敏分类供工具返回，异常消息不包含命令或身份正文。
    # 函数用途: 向调用方报告原权威不可读取。
    def __init__(self, report: dict[str, object]) -> None:
        super().__init__("managed process authority unavailable")
        self.report = report


# LLM: 记录既保存进程生命周期事实，也保存启动时的不可变访问范围；后续查询不得
# 用当前工作目录猜归属。新增生命周期字段时同步 summary 投影和进程工具测试。
# 类用途: 保存一条后台命令的进程、日志、状态和所属用户会话。
@dataclass
class BackgroundProcess:
    """一个被登记的后台进程。对标 长期助手 ProcessSession,裁剪到 my-agent 够用的字段。"""

    session_id: str
    command: str
    pid: int
    started_at: float
    access_scope: ProcessAccessScope = field(default_factory=ProcessAccessScope)
    cwd: str = ""
    output_file: str = ""
    process: subprocess.Popen | None = None  # 父进程 Popen 句柄(同进程内才有)
    child_pid: int = 0
    pid_birth_token: str = ""
    host_state_file: str = ""
    store_root: str = ""
    status: str = "running"
    exit_code: int | None = None
    finished_at: float | None = None
    completion_target: dict[str, str] = field(default_factory=dict)
    completion_notice_id: str = ""
    persisted_snapshot: dict[str, object] = field(default_factory=dict, repr=False)

    # LLM: Terminal is a typed host fact; model text and host-state prose never
    # participate in this transition.
    # 函数用途: 判断业务是否已退出、被终止或确定未启动；此标志不替代 host 资源清理回执。
    def is_terminal(self) -> bool:
        return self.status in PROCESS_TERMINAL_STATUSES

    # LLM: 摘要保留原控制标记；运行态只公开唤醒能力而非收件身份，日志错误不改进程终态。
    # 函数用途: 生成后台进程的状态和日志观测，耗时与输出增长分开，宿主 PID 保持内部使用。
    def to_summary(self, *, include_output: bool = False, output_tail_chars: int = _OUTPUT_TAIL_CHARS) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "session_id": self.session_id,
            "command": self.command[:200],
            "status": self.status,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)) if self.started_at else None,
            "uptime_seconds": max(0, int((self.finished_at or time.time()) - self.started_at)) if self.started_at else 0,
            "output_file": self.output_file,
        }
        if self.persisted_snapshot.get("schema") in MANAGED_PROCESS_SESSION_SCHEMAS:
            summary.update(stop_requested=self.persisted_snapshot["stop_requested"],
                           handoff_confirmed=self.persisted_snapshot["handoff_confirmed"])
        if self.exit_code is not None:
            summary["exit_code"] = self.exit_code
        if self.status == "running":
            summary["completion_wake"] = completion_wake_facts(self)
        # 监听范围声明、host 停止原因与越界证据都是记录里的结构化事实，原样投影给 status/list。
        for key in ("reason", "listen_scope", "listener_violation", "listener_warning"):
            if self.persisted_snapshot.get(key):
                summary[key] = self.persisted_snapshot[key]
        if include_output:
            tail = _read_log_tail(self.output_file, output_tail_chars)
            summary["output_tail"] = tail.text
            summary["output_bytes"] = tail.output_bytes
            summary["output_tail_capture"] = tail.to_fact()
        return summary

    # LLM: 保留已验证记录的全部版本字段；旧版本不升级，新版本不丢执行身份、revision 或控制事实。
    # 函数用途: 返回当前缓存快照的持久字段，Popen 永不序列化。
    def to_record(self) -> dict[str, object]:
        return {
            **self.persisted_snapshot,
            "schema": self.persisted_snapshot["schema"],
            "session_id": self.session_id,
            "command": self.command,
            "pid": self.pid,
            "child_pid": self.child_pid,
            "pid_birth_token": self.pid_birth_token,
            "started_at": self.started_at,
            "access_scope": {
                "owner_id": self.access_scope.owner_id,
                "conversation_id": self.access_scope.conversation_id,
                "owner_home": self.access_scope.owner_home,
            },
            "cwd": self.cwd,
            "output_file": self.output_file,
            "host_state_file": self.host_state_file,
            "status": self.status,
            "exit_code": self.exit_code,
            "finished_at": self.finished_at,
            "completion_target": self.completion_target,
            "completion_notice_id": self.completion_notice_id,
        }

    # LLM: 只接受 Store 验证的记录，完整保留 v1/v2/v3 字段；本地句柄只在同目录同出生实例核对后附加。
    # 函数用途: 恢复另一进程写下的当前状态、执行归属和控制事实，不从访问范围猜任务身份。
    @classmethod
    def from_record(
        cls,
        payload: dict[str, object],
        *,
        store_root: Path,
    ) -> BackgroundProcess:
        scope = dict(payload.get("access_scope") or {})
        exit_code = payload.get("exit_code")
        finished_at = payload.get("finished_at")
        return cls(
            session_id=str(payload["session_id"]),
            command=str(payload.get("command") or ""),
            pid=int(payload["pid"]),
            started_at=float(payload["started_at"]),
            access_scope=ProcessAccessScope(
                owner_id=str(scope.get("owner_id") or ""),
                conversation_id=str(scope.get("conversation_id") or ""),
                owner_home=str(scope.get("owner_home") or ""),
            ),
            cwd=str(payload.get("cwd") or ""),
            output_file=str(payload.get("output_file") or ""),
            child_pid=int(payload.get("child_pid") or 0),
            pid_birth_token=str(payload.get("pid_birth_token") or ""),
            host_state_file=str(payload.get("host_state_file") or ""),
            store_root=str(store_root),
            status=str(payload.get("status") or "running"),
            exit_code=int(exit_code) if exit_code is not None else None,
            finished_at=float(finished_at) if finished_at is not None else None,
            completion_target=dict(payload.get("completion_target") or {}),
            completion_notice_id=str(payload.get("completion_notice_id") or ""),
            persisted_snapshot=dict(payload),
        )


_TEST_PROCESS_SPAWN_HOOK: Callable[[BackgroundProcess], None] | None = None


# LLM: 仅供 pytest 注入创建观察者；默认关闭，不改变生产启动与出生探针。
# 函数用途: 安装或撤销测试派生进程登记回调。
def set_test_process_spawn_hook(hook: Callable[[BackgroundProcess], None] | None) -> None:
    global _TEST_PROCESS_SPAWN_HOOK
    _TEST_PROCESS_SPAWN_HOOK = hook


# LLM: 只在显式接管创建的进程后通知，不在探针、读取或缓存恢复时触发。
# 函数用途: 向当前测试发送新受管进程的独立快照。
def _notify_test_process_spawn(proc: BackgroundProcess) -> None:
    hook = _TEST_PROCESS_SPAWN_HOOK
    if hook is not None:
        hook(replace(proc, completion_target=dict(proc.completion_target),
                     persisted_snapshot=deepcopy(proc.persisted_snapshot)))


# LLM: host/child 各自校验创建出生身份；Store 丢失或 host 退出不丢 child，返回未确认事实供 pytest 诊断。
# 函数用途: 先尝试原受管停止，再依次精确回收创建时冻结的 host 与 child，绝不盲发旧 PGID 信号。
def reap_test_registered_process(entry: dict[str, Any]) -> dict[str, Any]:
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError, OverflowError):
        return {"confirmed": False, "reason": "invalid_pid"}
    birth = str(entry.get("pid_birth_token") or "")
    if pid <= 1 or pid == os.getpid():
        return {"confirmed": False, "reason": "protected_pid"}
    managed, errors, instances = False, [], []
    try:
        if birth and _test_process_target_allowed(pid):
            managed = _stop_test_registered_session(entry, pid, birth)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        errors.append({"stage": "managed_stop", "error_type": type(exc).__name__})
    targets = [(pid, birth, entry.get("popen"))]
    child = int(entry.get("child_pid") or 0)
    if child > 0 and child != pid:
        targets.append((child, str(entry.get("child_pid_birth_token") or ""), None))
    for target, token, handle in targets:
        try:
            instances.append(_reap_test_process_instance(target, token, handle))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            instances.append({"pid": target, "confirmed": False, "error_type": type(exc).__name__})
    return {"confirmed": bool(managed or instances and all(row["confirmed"] for row in instances)),
            "managed_confirmed": bool(managed), "instances": instances, "errors": errors}


# LLM: 独立拒绝保护 PID/原组/缺失出生；匹配 birth 的僵尸组长须先走树快照，不能先 poll 丢失同组后代身份。
# 函数用途: 先精确清理仍有原身份的进程树，再确认已消失或复用的旧实例，真实回执原样返回。
def _reap_test_process_instance(pid: int, birth: str, proc) -> dict[str, Any]:
    if not birth or not _test_process_target_allowed(pid):
        return {"pid": pid, "confirmed": False, "reason": "protected_or_unidentified"}
    if proc is not None and getattr(proc, "pid", None) != pid:
        proc = None
    if _same_process(pid, birth):
        return {"pid": pid, **asdict(terminate_process_tree(pid, proc, expected_birth_token=birth))}
    if _process_instance_terminated(pid, birth):
        if proc is not None:
            proc.poll()
        return {"pid": pid, "confirmed": True, "method": "already_gone"}
    return {"pid": pid, "confirmed": False, "reason": "identity_unavailable"}


# LLM: 只核对明确 PID 的进程组；不存在允许后续受管入口清理原 child，未知组拒绝，不做 PID 差集。
# 函数用途: 排除 init、pytest 自身和 pytest 进程组，跨平台保留保守边界。
def _test_process_target_allowed(pid: int) -> bool:
    if pid <= 1 or pid == os.getpid():
        return False
    if _IS_WINDOWS:
        return True
    try:
        return os.getpgid(pid) != os.getpgrp()
    except ProcessLookupError:
        return True
    except OSError:
        return False


# LLM: Store 必须匹配登记的 host/child、owner/task 身份；原停止事务 CAS 拒绝同名替换，不重选新资源。
# 函数用途: 优先清理原受管绑定并返回真实确认，失败后仍可沿原创建快照精确回收。
def _stop_test_registered_session(entry: dict[str, Any], pid: int, birth: str) -> bool:
    from .process_session_cleanup import stop_process_session

    session, root = entry.get("session_id"), entry.get("store_root")
    if not session or not root:
        return False
    store = ProcessSessionStore(root)
    report = store.load(str(session))
    payload = report.record
    if (report.load_error or payload.get("schema") not in MANAGED_PROCESS_SESSION_SCHEMAS
            or payload.get("pid") != pid or payload.get("pid_birth_token") != birth
            or not _test_session_identity_matches(entry, payload)):
        return False
    for key in ("pid", "child_pid"):
        target = int(payload.get(key) or 0)
        if target and not _test_process_target_allowed(target):
            return False
    proc = entry.get("popen")
    if proc is not None and getattr(proc, "pid", None) != pid:
        proc = None
    return stop_process_session(store, payload, host_process=proc).confirmed


# LLM: 创建期完整 child 对必须与 Store 相同（含空值），未知不能充当当前 child 的通配符；执行身份不猜补。
# 函数用途: 阻止同名 Store 或后续补写 child 将测试的停止权限转移到另一出生实例。
def _test_session_identity_matches(entry, payload) -> bool:
    expected_child = (int(entry.get("child_pid") or 0), str(entry.get("child_pid_birth_token") or ""))
    stored_child = (int(payload.get("child_pid") or 0), str(payload.get("child_pid_birth_token") or ""))
    if expected_child != stored_child:
        return False
    return (not entry.get("owner_id") or entry["owner_id"] == (payload.get("access_scope") or {}).get("owner_id")) and (
        not entry.get("task_id") or entry["task_id"] == (payload.get("execution_scope") or {}).get("root_task_id"))


# LLM: 只移除指定规范目录和 session 缓存；无目录时只接受唯一匹配，保留原 host 句柄回收责任，不写 Store 或发信号。
# 函数用途: 让测试仅清掉已到期会话，不破坏其它 owner 或仍有效的共享 fixture 查找索引。
def _forget_cached_process_session(registry, session_id: str, store_root) -> None:
    root = str(Path(store_root).expanduser().resolve(strict=False)) if store_root else None
    with registry._lock:
        keys = [key for key in registry._processes if key[1] == session_id and (root is None or key[0] == root)]
        if root is None and len(keys) != 1:
            return
        registry._drain_pending_reap_locked()
        for key in keys:
            registry._discard_cached_locked(key)


# LLM: 日志观测只携读取事实，不能决定进程状态或扩大路径权限；正文与元数据分开，避免再造输出归档。
# 类用途: 保存一次限量日志尾读的文本、同fd文件长度、截断、编码和读取失败事实。
@dataclass(frozen=True)
class ProcessLogTail:
    text: str = ""
    output_bytes: int | None = None
    bytes_read: int = 0
    limit_bytes: int = 0
    truncated: bool = False
    invalid_utf8: bool = False
    error_type: str = ""

    # LLM: 字段来自本次读取，不从展示截断提示推断；没有正文，不修改原进程记录。
    # 函数用途: 为现有进程工具的 JSON 输出投影机器可判断的日志采集事实。
    def to_fact(self) -> dict[str, object]:
        return {"bytes_read": self.bytes_read, "limit_bytes": self.limit_bytes,
                "truncated": self.truncated, "invalid_utf8": self.invalid_utf8,
                "error_type": self.error_type}


# LLM: 先打开再fstat同一fd，read最多取观察长度与预算的小者；并发追加不纳入本次观察，短读/坏UTF8均结构化。
#   原max_chars接口实际一直按字节预算；读失败只影响观测，不能改变进程状态、权限或停止目标。
# 函数用途: 有界读取已授权日志文件尾部，返回文本及同一次读取的截断/失败事实，不吞成假空成功。
def _read_log_tail(output_file: str, max_chars: int) -> ProcessLogTail:
    limit = max(0, int(max_chars))
    if not output_file or not limit:
        return ProcessLogTail(limit_bytes=limit)
    size = None
    try:
        with Path(output_file).open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - limit))
            raw = handle.read(min(size, limit))
    except OSError as exc:
        return ProcessLogTail(output_bytes=size, limit_bytes=limit, error_type=type(exc).__name__)
    return _log_tail_result(raw, size=size, limit=limit)


# LLM: 严格解码失败时保留原replacement展示但单列坏编码；读取预算不足和观察后截断都不能声称完整。
# 函数用途: 将有限日志字节转成原尾部展示与采集事实，日志截断提示只供阅读。
def _log_tail_result(raw: bytes, *, size: int, limit: int) -> ProcessLogTail:
    try:
        text, invalid = raw.decode("utf-8"), False
    except UnicodeDecodeError:
        text, invalid = raw.decode("utf-8", errors="replace"), True
    short_read = len(raw) < min(size, limit)
    if size > limit:
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1:]
        text = f"...[日志已截断,只显示尾部 {len(text)} 字符]...\n{text}"
    return ProcessLogTail(text, size, len(raw), limit, size > limit or short_read, invalid,
                          "short_read" if short_read else "")


# LLM: 只缓存原Store记录和本进程句柄；指纹比较共用旧locale复核门，观察未知不复用句柄，锁仍registry到Store。
#   移除缓存不放弃host回收责任；最多64个未退出Popen等后续操作非阻塞poll，不改变业务终态或发送信号。
# 类用途: 按精确范围查询、等待和停止后台会话，兼容旧出生指纹但不新建权威或改写持久记录。
class ProcessRegistry:
    # LLM: key 同时包含规范目录和 session ID；不同 owner 的同名句柄不会复用进程对象。
    # 函数用途: 初始化本进程缓存、互斥和仅保存外层host句柄的待回收列表。
    def __init__(self) -> None:
        self._processes: dict[tuple[str, str], BackgroundProcess] = {}
        self._lock = threading.Lock()
        self._pending_reap: list[subprocess.Popen[str]] = []

    # LLM: 必须持registry锁；只poll已有本地Popen，最多64项，不按PID重建句柄或操作业务child组长。
    # 函数用途: 顺带回收已经物理退出的host，仍活着的句柄继续留在内存列表。
    def _drain_pending_reap_locked(self) -> None:
        self._pending_reap[:] = [proc for proc in self._pending_reap if proc.poll() is None]

    # LLM: 只reap本Registry持有的外层host；终态最多等50ms，移除只poll；去重上限64，满额明确报错由调用方保留原记录，不发信号。
    # 函数用途: 等待host在写终态后真正退出，暂未退出时把回收责任留给下次Registry操作。
    def _reap_popen_or_retain_locked(self, proc: subprocess.Popen[str] | None, *, wait_seconds: float = 0.05) -> None:
        if proc is None:
            return
        if proc.poll() is None and wait_seconds > 0:
            with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
                proc.wait(timeout=wait_seconds)
        if proc.poll() is not None:
            self._pending_reap[:] = [pending for pending in self._pending_reap if pending is not proc]
        elif all(pending is not proc for pending in self._pending_reap):
            self._drain_pending_reap_locked()
            if len(self._pending_reap) >= 64:
                raise RuntimeError("managed host reap capacity exhausted")
            self._pending_reap.append(proc)

    # LLM: 必须持registry锁；先移交本地host句柄再撤缓存，满额异常时原记录不动；未终态也仅poll，不更新存储或发信号。
    # 函数用途: 安全移除一条内存记录，避免权威缺失、裁剪或枚举刷新时漏掉host回收。
    def _discard_cached_locked(self, key: tuple[str, str]) -> BackgroundProcess | None:
        old = self._processes.get(key)
        if old is not None:
            self._reap_popen_or_retain_locked(old.process, wait_seconds=0.0)
            self._processes.pop(key, None)
        return old

    # LLM: 原记录必须已经提交；附加 Popen 不新建 ID 或重写生命周期，实例不符明确拒绝。
    # 函数用途: 接管已交接的同一会话并通知可选测试观察者，保留可回收的本地 host 句柄。
    def attach(self, payload: dict[str, object], process: subprocess.Popen, store_root: Path) -> BackgroundProcess:
        from .process_session_records import merge_process_record

        with self._lock:
            record = self._visible_record_locked(str(payload["session_id"]), None, store_root)
            if record is None:
                raise ProcessSessionAuthorityError({"error_type": "authority_missing"})
            merge_process_record(payload, record.to_record())
            if process.pid != record.pid:
                raise ValueError("managed process handle conflict")
            record.process = process
            self._prune_finished_locked(store_root)
            _notify_test_process_spawn(record)
            return record

    # LLM: 越界和缺失统一为空，损坏权威必须报错而不是拿缓存补成功；日志只能在范围校验后读取。
    # 函数用途: 获取当前有效会话，并检查托管进程是否失去控制。
    def get(self, session_id: str, access_scope: ProcessAccessScope | None = None,
            store_root: str | Path | None = None) -> BackgroundProcess | None:
        with self._lock:
            record = self._visible_record_locked(session_id, access_scope, store_root)
            if record is not None:
                self._refresh_locked(record)
            return record

    # LLM: v2/v3 只信原 Store 终态，host 丢失不是 child 退出；v1 保留原终态读取，旧 v2 不得误走该分支。
    #   业务终态只对持有的外层host做有界reap；未退出则保留待回收，不改WNOWAIT或业务树清理顺序。
    # 函数用途: 更新失去托管的未知状态，或读取旧版本已经退出的命令结果。
    def _refresh_locked(self, record: BackgroundProcess) -> None:
        self._drain_pending_reap_locked()
        if record.is_terminal():
            self._reap_popen_or_retain_locked(record.process, wait_seconds=0.05)
            return
        if record.persisted_snapshot.get("schema") in MANAGED_PROCESS_SESSION_SCHEMAS:
            pid = record.pid or record.persisted_snapshot["launcher_pid"]
            birth = record.pid_birth_token or record.persisted_snapshot["launcher_birth_token"]
            if not _process_instance_terminated(pid, birth) or record.status == "unknown":
                return
            try:
                with ProcessSessionStore(record.store_root).transaction() as transaction:
                    current = transaction.load(record.session_id)
                    if current is None:
                        raise ProcessSessionAuthorityError({"error_type": "authority_missing"})
                    # 刚读取的较新终态或实例绑定优先，不能把旧观察写给不同的启动阶段。
                    if current == record.persisted_snapshot:
                        current = transaction.write({**current, "status": "unknown"})
                    self._cache_payload(current, Path(record.store_root))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                self._discard_cached_locked((record.store_root, record.session_id))
                raise ProcessSessionAuthorityError(process_session_error_report(exc, "process_registry.refresh")) from exc
            return
        return_code = record.process.poll() if record.process is not None else None
        if (record.process is not None and return_code is None) or (record.process is None and _same_process(record.pid, record.pid_birth_token)):
            return
        host_state = _host_terminal_state(record.host_state_file)
        record.status = "exited"
        record.exit_code = int(host_state["exit_code"]) if host_state.get("exit_code") is not None else return_code
        record.finished_at = float(host_state.get("finished_at") or time.time())
        self._persist_locked(record)

    # LLM: 状态与日志来自同一个范围内的当前记录；PID 不进入模型摘要。
    # 函数用途: 查询后台进度和最近输出。
    def status(self, session_id: str, access_scope: ProcessAccessScope | None = None,
               store_root: str | Path | None = None) -> dict[str, Any] | None:
        record = self.get(session_id, access_scope, store_root)
        return record.to_summary(include_output=True) if record is not None else None

    # LLM: 简单内部调用沿结构化报告入口，模型工具需直接消费 list_report 中的错误。
    # 函数用途: 返回当前可见的进程摘要列表。
    def list(self, access_scope: ProcessAccessScope | None = None, store_root: str | Path | None = None) -> list[dict[str, Any]]:
        rows, _errors = self.list_report(access_scope, store_root)
        return rows

    # LLM: 枚举先刷新权威；业务范围显式排除共享激活，redo 故障不能回退到旧成功视图。
    #   每次枚举顺带非阻塞reap待退出host，即使原会话已经被移除或已发完成通知。
    # 函数用途: 列出当前范围的后台会话，并单列损坏或恢复错误。
    def list_report(self, access_scope: ProcessAccessScope | None = None,
                    store_root: str | Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
        with self._lock:
            self._drain_pending_reap_locked()
            roots = {str(Path(store_root).resolve())} if store_root else {rec.store_root for rec in self._processes.values()}
            errors = []
            for root in roots:
                errors.extend(self._hydrate_store_locked(root))
            records = [rec for rec in self._processes.values() if (access_scope is None or
                       rec.persisted_snapshot.get("activation_scope") is None and rec.access_scope == access_scope)
                       and _record_matches_store(rec, store_root)]
            for record in records:
                self._refresh_locked(record)
            records.sort(key=lambda item: (item.is_terminal(), -item.started_at))
            return [record.to_summary() for record in records], errors

    # LLM: v2/v3 精确冻结一个 ID 再锁外清理；业务范围不得访问共享激活，launcher 绝不参与终止。
    # 函数用途: 停止显式指定的会话；只有可信树退出回执才报告已停止。
    def kill(self, session_id: str, access_scope: ProcessAccessScope | None = None,
             store_root: str | Path | None = None) -> dict[str, Any] | None:
        with self._lock:
            record = self._visible_record_locked(session_id, access_scope, store_root)
            if record is None:
                return None
            payload, process = record.to_record(), record.process
            root = Path(record.store_root)
        if payload["schema"] in MANAGED_PROCESS_SESSION_SCHEMAS:
            from .process_session_cleanup import stop_process_session

            cleanup = stop_process_session(ProcessSessionStore(root), payload, host_process=process)
            if cleanup.authority_missing:
                # 与旧版路径同一口径：权威记录不见了就是权威读不出，不是“停止未确认”（这时一个信号都没发）。
                raise ProcessSessionAuthorityError({"error_type": "authority_missing"})
            with self._lock:
                current = self._cache_payload(cleanup.record, root)
                summary = current.to_summary()
            summary["termination"] = {"confirmed": cleanup.confirmed, "instances": [asdict(r) for r in cleanup.terminations]}
            return summary
        return self._kill_legacy(record, access_scope)

    # LLM: 已发布 v1 仅按原 session 管理，不推导 task 身份；原出生标识必须传入终止快照边界。
    #   发信号之后的失败（第二次读记录出错，或记录已经不在了）一律改抛 ProcessSessionCleanupError：
    #   那时信号已经发出，可能已有副作用，不能让它退化成「发信号前失败」的 AuthorityError。
    # 函数用途: 保留旧记录的显式句柄停止，确认失败时不登记 killed。
    def _kill_legacy(self, record: BackgroundProcess, access_scope: ProcessAccessScope | None) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked(record)
            if record.is_terminal():
                return {"session_id": record.session_id, "status": "already_exited" if record.status == "exited" else record.status,
                        "exit_code": record.exit_code, "message": "进程已经结束，无需终止。"}
        receipt = terminate_process_tree(record.pid, record.process, expected_birth_token=record.pid_birth_token)
        # 信号已经发出：此后无论读记录还是持久化失败，都属于「清理结果未定」。捕获范围与托管路径 stop_process_session
        # 一致：旧格式写盘失败会直接抛 OSError / ValueError（write_json_file_atomic_unlocked、validate_process_record），
        # 不能逃出工具变成 TOOL_ERROR；ProcessSessionAuthorityError 本身属于 RuntimeError。
        with self._lock:
            try:
                current = self._visible_record_locked(record.session_id, access_scope, record.store_root)
                if current is None:
                    raise ProcessSessionAuthorityError({"error_type": "authority_missing"})
                if receipt.confirmed:
                    current.status, current.finished_at, current.exit_code = "killed", time.time(), receipt.return_code
                    self._persist_locked(current)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise self._legacy_cleanup_unresolved(record, receipt, exc) from exc
            summary = current.to_summary()
            summary.update(signal=receipt.method, termination=asdict(receipt))
            return summary

    # LLM: 只把「已发信号之后」的权威故障翻译成 cleanup 语义，回执原样带上供人工核对；
    #   已确认的终止不会被降级成未发生，也不触发自动重做。
    # 函数用途: 构造 legacy 停止在发信号后失败时的清理未定异常。
    @staticmethod
    def _legacy_cleanup_unresolved(
        record: BackgroundProcess,
        receipt: ProcessTerminationReceipt,
        cause: Exception,
    ) -> Any:
        from .process_session_cleanup import ProcessSessionCleanupError

        return ProcessSessionCleanupError(
            cause,
            record.to_record(),
            (receipt,),
            True,
        )

    # LLM: 每轮观察原 Store 的阶段变化，单调时长退避且截断到剩余预算；取消仅释放等待，不能推断child已结束。
    # 函数用途: 在宿主取消和超时范围内等待同一个 session 的可信终态。
    def wait(self, session_id: str, timeout_seconds: float, access_scope: ProcessAccessScope | None = None,
             store_root: str | Path | None = None) -> dict[str, Any] | None:
        started_monotonic = time.monotonic()
        deadline = started_monotonic + max(0.0, float(timeout_seconds or 0.0))
        token = current_cancellation_token()
        while True:
            raise_if_cancelled()
            record = self.get(session_id, access_scope, store_root)
            if record is None:
                return None
            remaining = deadline - time.monotonic()
            if record.is_terminal() or remaining <= 0:
                summary = record.to_summary(include_output=True)
                summary["wait_timed_out"] = not record.is_terminal()
                return summary
            interval = min(_process_wait_interval(time.monotonic() - started_monotonic), remaining)
            token.wait(interval) if token is not None else time.sleep(interval)

    # LLM: 无 root 内部查询只允许缓存唯一匹配；业务 scope 显式拒绝共享激活，不能凭已知句柄越过归属。
    #   正常容量下权威损坏/缺失沿原错误；移交host回收责任满额时先报容量异常并保留缓存，不授权发信号。
    # 函数用途: 从权威目录读当前记录，再做访问身份比较。
    def _visible_record_locked(self, session_id: str, access_scope: ProcessAccessScope | None,
                               store_root: str | Path | None) -> BackgroundProcess | None:
        if not store_root:
            matches = [rec for rec in self._processes.values() if rec.session_id == session_id]
            if len(matches) != 1:
                return None
            store_root = matches[0].store_root
        root = Path(store_root).expanduser().resolve(strict=False)
        report = ProcessSessionStore(root).load(session_id)
        if report.load_error:
            self._discard_cached_locked((str(root), session_id))
            raise ProcessSessionAuthorityError(report.load_error)
        if not report.record:
            cached = self._discard_cached_locked((str(root), session_id))
            if cached is not None and not cached.is_terminal():
                raise ProcessSessionAuthorityError({"error_type": "active_authority_missing"})
            return None
        record = self._cache_payload(report.record, root)
        return record if access_scope is None or (
            record.persisted_snapshot.get("activation_scope") is None and record.access_scope == access_scope
        ) else None

    # LLM: 同地址/PID的不同文本须将缓存和存储指纹同时绑定新内核观察；不能拿缓存token冒充当前活体，未知不复用句柄。
    #   旧句柄无法复用时仅poll并留待回收，绝不将它附给不同实例或以退出码推断业务终态。
    # 函数用途: 刷新可信快照并兼容旧locale，阻止把旧Popen附给复用PID的新实例，不改写存储token。
    def _cache_payload(self, payload: dict[str, object], root: Path) -> BackgroundProcess:
        key = (str(root), str(payload["session_id"]))
        cached = self._processes.get(key)
        fresh = BackgroundProcess.from_record(payload, store_root=root)
        if cached is not None:
            try:
                current = fresh.pid_birth_token
                if cached.pid == fresh.pid and cached.pid_birth_token != current:
                    current = capture_process_birth_token(fresh.pid)
                same = (cached.pid == fresh.pid
                        and _birth_token_matches(fresh.pid, cached.pid_birth_token, current)
                        and _birth_token_matches(fresh.pid, fresh.pid_birth_token, current))
            except (OSError, subprocess.TimeoutExpired, ValueError):
                same = False
            if same:
                fresh.process = cached.process
            else:
                self._reap_popen_or_retain_locked(cached.process, wait_seconds=0.0)
            cached.__dict__.update(fresh.__dict__)
            return cached
        self._processes[key] = fresh
        return fresh

    # LLM: 读取错误的记录从缓存撤下，健康子集来自同一次枚举；不可拿原缓存绕过恢复错误。
    #   枚举中消失的记录仅非阻塞移交本地host句柄，不因记录缺失就终止仍活着的进程。
    # 函数用途: 刷新一个规范目录的完整可读记录，并返回独立错误。
    def _hydrate_store_locked(self, store_root: str | Path) -> list[dict[str, object]]:
        root = Path(store_root).expanduser().resolve(strict=False)
        records, errors = ProcessSessionStore(root).list_records()
        present = {str(payload["session_id"]) for payload in records}
        for key in list(self._processes):
            if key[0] == str(root) and key[1] not in present:
                self._discard_cached_locked(key)
        for payload in records:
            self._cache_payload(payload, root)
        return errors

    # LLM: 此入口只写已发布 v1 的观察更新；v2/v3 必须在原事务重读，不能强换 revision 写旧缓存。
    # 函数用途: 保存旧会话终态并吸收实际提交结果。
    def _persist_locked(self, record: BackgroundProcess) -> None:
        if record.persisted_snapshot.get("schema") != LEGACY_PROCESS_SESSION_SCHEMA:
            raise ValueError("v2 updates require a current store transaction")
        effective = ProcessSessionStore(record.store_root).write(record.to_record())
        self._cache_payload(effective, Path(record.store_root))

    # LLM: 只裁剪确认终态；磁盘仅操作当前调用目录，不能因其它 owner 的损坏影响本次交接。
    #   先poll原待回收句柄，裁剪记录前将未退出host留待回收；不能改变业务终态或发信号。
    # 函数用途: 限制已完成缓存数量，并在本次启动所属的原目录裁剪历史。
    def _prune_finished_locked(self, store_root: Path) -> None:
        self._drain_pending_reap_locked()
        finished = [(key, rec) for key, rec in self._processes.items() if rec.is_terminal()]
        finished.sort(key=lambda item: item[1].finished_at or item[1].started_at)
        for key, _record in finished[:max(0, len(finished) - _MAX_FINISHED_COUNT)]:
            self._discard_cached_locked(key)
        ProcessSessionStore(store_root).prune_finished(_MAX_FINISHED_COUNT)

    # LLM: 精确 session/目录或唯一同名缓存才可撤下；沿原有界句柄回收，不写权威或发信号。
    # 函数用途: 忘记已到期测试会话，保留其它共享资源的无 root 查询能力。
    def forget(self, session_id: str, store_root: str | Path | None = None) -> None:
        _forget_cached_process_session(self, session_id, store_root)

    # LLM: 只丢本进程记录缓存；持有的host句柄只非阻塞poll并留待回收，不改持久记录或发信号。
    # 函数用途: 清除会话缓存，仍保留host的有界回收责任；测试负责显式结束自己创建的活资源。
    def clear(self) -> None:
        with self._lock:
            self._drain_pending_reap_locked()
            for record in self._processes.values():
                self._reap_popen_or_retain_locked(record.process, wait_seconds=0.0)
            self._processes.clear()


# LLM: Host state is never used while the recorded PID identity is alive. Once
# the host is gone it may contribute only exit_code/finished_at, never scope or PID.
# 函数用途: 读取托管进程留下的真实命令退出码；损坏或未结束时返回空事实。
def _host_terminal_state(state_file: str) -> dict[str, object]:
    if not str(state_file or "").strip():
        return {}
    payload = read_json_object(Path(state_file))
    if (
        str(payload.get("schema") or "") != HOST_STATE_SCHEMA
        or str(payload.get("status") or "") != "exited"
    ):
        return {}
    result: dict[str, object] = {}
    try:
        if payload.get("exit_code") is not None:
            result["exit_code"] = int(payload["exit_code"])
        if payload.get("finished_at") is not None:
            result["finished_at"] = float(payload["finished_at"])
    except (TypeError, ValueError):
        return {}
    return result


def _pid_alive(pid: int) -> bool:
    """跨平台进程存活探测。pid<=0 视为无效。"""
    if not pid or pid <= 0:
        return False
    if _IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限发信号,仍算存活
    except OSError:
        return False
    return True


# LLM: 仅前台采集完成后调用；有启动身份先清树后poll；缺启动身份先由原写门拒绝清树，再仅取原句柄父退出码。
# Windows 保留原 Popen 退出路径；本入口不选择后台/PTY，不持久化另一套资源账，联测 shell foreground cleanup。
# 函数用途: 分开返回真实父退出码与进程树回执；身份不可核验始终不发信号、不确认整树退出。
def finish_foreground_process(
    proc: subprocess.Popen,
    *,
    expected_birth_token: str | None,
) -> tuple[int | None, ProcessTerminationReceipt | None]:
    if _IS_WINDOWS:
        return proc.poll(), None
    if not expected_birth_token:
        termination = terminate_process_tree(proc.pid, proc, expected_birth_token="")
        return proc.poll(), termination
    if proc.returncode is None and not _process_instance_terminated(proc.pid, expected_birth_token or ""):
        return None, None
    termination = terminate_process_tree(proc.pid, proc, expected_birth_token=expected_birth_token or "")
    return proc.poll(), termination


# LLM: 原唯一终止入口强制本次新鲜关系再核启动指纹；不消费只读TTL缓存选信号目标，未知/不匹配均不能发信号。
# 函数用途: 确认同一实例后有界发送TERM/KILL并回收，出生观察失败返回identity_unavailable而非假冒换PID。
def terminate_process_tree(
    pid: int,
    proc: subprocess.Popen | None,
    *,
    grace_seconds: float | None = None,
    expected_birth_token: str | None = None,
) -> ProcessTerminationReceipt:
    """终止观察到的后代进程树，并返回退出核对结果。

    POSIX:先快照 root 的全部后代,对每个进程组和 pid 发 SIGTERM,宽限后再对
      仍为同一进程实例的存活者发 SIGKILL。不能只 killpg(root):bwrap
      ``--new-session`` 会在里面再建 session/process-group,否则 npm/test 等后代
      能逃逸并继续持有 stdout/stderr pipe。
    核对回执只认“进程实例”:某个 PID 一旦被证明消失(不存在/僵尸/出生标识已变),
      后续同号 PID 被别的进程复用也不能把它记回 unresolved(2026-09-25 CI 复现)。
    Windows:taskkill /T /F 杀整棵进程树(/T 含子进程,/F 强制)。
    """
    if pid <= 0:
        return ProcessTerminationReceipt("noop", False, None, 0)
    if expected_birth_token is not None and not expected_birth_token:
        return ProcessTerminationReceipt("identity_unavailable", False, None, 0, (pid,))
    if _IS_WINDOWS:
        if expected_birth_token is not None and not _same_process(pid, expected_birth_token):
            return ProcessTerminationReceipt("identity_changed", False, None, 0, (pid,))
        return _terminate_windows_tree(pid, proc)
    snapshot, complete = _fresh_process_tree_snapshot(pid)
    if expected_birth_token is not None:
        if not snapshot.get(pid):
            return ProcessTerminationReceipt("identity_unavailable", False, None, len(snapshot), (pid,))
        try:
            same = _birth_token_matches(pid, expected_birth_token, snapshot[pid])
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return ProcessTerminationReceipt("identity_unavailable", False, None, len(snapshot), (pid,))
        if not same:
            return ProcessTerminationReceipt("identity_changed", False, None, len(snapshot), (pid,))
    method = "already_gone"
    # 已证明消失的进程实例不再重查：PID 在宽限期后被新进程复用时，os.kill 探测会重新成功，
    # 但那已经是另一个实例；只有快照里同 PID 换了出生标识（根的新后代）才重新纳入核对。
    terminated: set[int] = set()
    signalled = _signal_process_snapshot(snapshot, signal.SIGTERM)
    grace = _KILL_GRACE_SECONDS if grace_seconds is None else max(0.0, grace_seconds)
    if signalled:
        method = "SIGTERM"
        if not _wait_process_snapshot_gone(snapshot, proc, grace, terminated):
            # 根进程已回收时 PID 可能复用；只能对仍是原实例的根补充后代，不能杀到另一棵新树。
            if snapshot.get(pid) and _same_process(pid, snapshot[pid]):
                current, current_complete = _fresh_process_tree_snapshot(pid)
                _merge_tree_snapshot(snapshot, current, terminated)
                complete = complete and current_complete
            _signal_process_snapshot(snapshot, getattr(signal, "SIGKILL", signal.SIGTERM))
            method = "SIGTERM->SIGKILL"
    if proc is not None:
        try:
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
    _wait_process_snapshot_gone(
        snapshot, proc,
        _FINAL_CONFIRM_WITH_HANDLE_SECONDS if proc is not None else _FINAL_CONFIRM_WITHOUT_HANDLE_SECONDS,
        terminated,
    )
    unresolved = tuple(member for member, token in snapshot.items()
                       if member not in terminated and not _process_instance_terminated(member, token))
    return_code = proc.poll() if proc is not None else None
    confirmed = complete and not unresolved and (proc is None or return_code is not None)
    return ProcessTerminationReceipt(method, confirmed, return_code, len(snapshot), unresolved)


# LLM: Windows 仅在 taskkill /T /F 成功且直接子进程已回收时确认；单独 Popen.kill 不能证明后代退出。
# 函数用途: 调用系统进程树终止工具并核对返回码，失败时尽力终止直接子进程但保留未确认结果。
def _terminate_windows_tree(pid: int, proc: subprocess.Popen | None) -> ProcessTerminationReceipt:
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc is not None:
            proc.wait(timeout=2)
        code = proc.poll() if proc is not None else None
        confirmed = result.returncode == 0 and proc is not None and code is not None
        return ProcessTerminationReceipt("taskkill/T/F", confirmed, code, 1, () if confirmed else (pid,))
    except (OSError, subprocess.SubprocessError):
        _safe_popen_kill(proc)
        return ProcessTerminationReceipt("popen.kill", False, None, 1, (pid,))


def _safe_popen_kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass


# LLM: Linux默认单遍proc；普通POSIX观察可共享缓存，终止须经fresh包装；map替身调用形状与完整性不变。
# 函数用途: 只读采集原进程树及已换父进程的同组后代出生标识，供本次终止和退出核对共用。
def _process_tree_snapshot(root_pid: int) -> tuple[dict[int, str], bool]:
    if root_pid <= 0:
        return {}, False
    children: dict[int, list[int]] = {}
    if (Path("/proc").is_dir() and _process_parent_map is _DEFAULT_PROCESS_PARENT_MAP
            and _process_group_map is _DEFAULT_PROCESS_GROUP_MAP):
        birth = capture_process_birth_token(root_pid)
        parents, groups = _linux_proc_parent_and_group_maps()
        complete = bool(parents) and getattr(parents, "complete", True)
        group_members, group_complete = _exclusive_group_members(root_pid, groups=groups)
        # 预取表也必须被扫描前的同一组长身份约束，不能把旧组成员套给复用后的PID。
        if group_members and (not birth or not _same_process(root_pid, birth)):
            group_members, group_complete = [], False
    else:
        parents, complete = _process_parent_map()
        group_members, group_complete = _exclusive_group_members(root_pid)
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)
    complete = complete and group_complete
    ordered: list[int] = []
    pending = [root_pid, *group_members]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current <= 0 or current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        pending.extend(children.get(current, ()))
    snapshot = {current: capture_process_birth_token(current) for current in ordered}
    complete = complete and all(token or _process_instance_terminated(pid, "") for pid, token in snapshot.items())
    return snapshot, complete


# LLM: 仅此线程临时禁用关系TTL缓存，finally恢复；仍调用原snapshot以保替身/身份守卫，不改外部观察者的默认缓存。
# 函数用途: 终止及升级KILL前重新采集关系，避免旧关系配上复用PID的新出生身份。
def _fresh_process_tree_snapshot(root_pid: int) -> tuple[dict[int, str], bool]:
    previous = getattr(_FRESH_RELATION_MAPS, "required", False)
    _FRESH_RELATION_MAPS.required = True
    try:
        return _process_tree_snapshot(root_pid)
    finally:
        _FRESH_RELATION_MAPS.required = previous


# LLM: 可共享同次扫描的groups，但仍须核对组长出生身份、独立组与宿主组；不得用不完整映射证明清理完成。
# 函数用途: Linux用procfs、其它平台用关闭stdin的ps找原组成员，仍核对组长出生身份与宿主组边界。
def _exclusive_group_members(root_pid: int, *, groups: dict[int, int] | None = None) -> tuple[list[int], bool]:
    birth = capture_process_birth_token(root_pid)
    if not birth:
        return [], _process_instance_terminated(root_pid, "")
    if groups is None:
        groups, complete = _process_group_map()
    else:
        complete = bool(groups) and getattr(groups, "complete", True)
    root_group = groups.get(root_pid)
    if not _same_process(root_pid, birth):
        return [], False
    if root_group is None:
        return [], False
    if root_group != root_pid or root_group == os.getpgrp():
        return [], complete
    return [pid for pid, pgid in groups.items() if pgid == root_pid], complete


# LLM: Linux仍读proc；无proc原生POSIX共享250ms的ps观察，替换run时不缓存；完整性不得被命中升级。
# 函数用途: 从proc或有界ps缓存获取进程组表副本，供精确组长身份核对后使用。
def _process_group_map() -> tuple[dict[int, int], bool]:
    proc_root = Path('/proc')
    groups: dict[int, int] = {}
    complete = True
    if proc_root.is_dir():
        try:
            entries = tuple(proc_root.iterdir())
        except OSError:
            return {}, False
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / 'stat').read_text(encoding='utf-8', errors='replace')
                groups[int(entry.name)] = int(stat.rsplit(')', 1)[1].split()[2])
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (OSError, IndexError, TypeError, ValueError):
                complete = False
        return groups, complete and bool(groups)
    return _ps_relation_map("pgid")


# LLM: 仅默认run、无proc、非Windows启用新缓存，测试替换run时保留原ps可见调用。
# 函数用途: 统一限定原生POSIX优化范围，不改变Linux与Windows的探测入口。
def _native_posix_probe_enabled() -> bool:
    return subprocess.run is _DEFAULT_SUBPROCESS_RUN and not _IS_WINDOWS and not Path("/proc").is_dir()


# LLM: 锁覆盖检查和ps采集以合并并发miss；终止线程强制原采集，普通观察保留False缺口、完成起算TTL和副本。
# 函数用途: 在原生无proc平台为父/组表分别共享250ms只读观察，其它入口不使用新缓存。
def _ps_relation_map(relation: str) -> tuple[dict[int, int], bool]:
    if getattr(_FRESH_RELATION_MAPS, "required", False) or not _native_posix_probe_enabled():
        return _read_ps_relation_map(relation)
    with _PS_RELATION_CACHE_LOCK:
        now = time.monotonic()
        cached = _PS_RELATION_CACHE.get(relation)
        if cached is not None and 0 <= now - cached[0] < _PS_RELATION_CACHE_SECONDS:
            return dict(cached[1]), cached[2]
        result, complete = _read_ps_relation_map(relation)
        _PS_RELATION_CACHE[relation] = (time.monotonic(), dict(result), complete)
        return dict(result), complete


# LLM: 采集失败与坏行保留不完整，不把旧缓存补成成功；关闭stdin并仅为子进程固定C环境。
# 函数用途: 保留原ps父/组表命令和各自空表完整性合同，集中解析一次观察。
def _read_ps_relation_map(relation: str) -> tuple[dict[int, int], bool]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", f"pid=,{relation}="], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=1, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}, False
    result: dict[int, int] = {}
    complete = completed.returncode == 0
    for line in completed.stdout.splitlines():
        try:
            pid, related = map(int, line.split())
            result[pid] = related
        except (TypeError, ValueError):
            complete = False
    return result, complete and (bool(result) if relation == "ppid" else True)


# LLM: dict内容仍是PID到父/组号；complete只承载原枚举缺口，不是另一份进程权威或跨调用缓存。
# 类用途: 在保持双dict返回合同的同时携带父表与组表各自的完整性事实。
class _ProcRelationMap(dict[int, int]):
    complete: bool = True


# LLM: 只枚举Linux procfs一次，stat失败回退status只恢复父表；读不到组号不得冒充完整组表。
# 函数用途: 一次枚举收集父进程与组号，供进程树快照共享。
def _linux_proc_parent_and_group_maps(proc_root: Path | None = None) -> tuple[dict[int, int], dict[int, int]]:
    root = proc_root if proc_root is not None else Path("/proc")
    parents, groups = _ProcRelationMap(), _ProcRelationMap()
    try:
        entries = tuple(root.iterdir())
    except OSError:
        parents.complete = groups.complete = False
        return parents, groups
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            parents.complete = groups.complete = False
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            fields = stat.rsplit(")", 1)[1].split()
            parent, group = int(fields[1]), int(fields[2])
        except (FileNotFoundError, ProcessLookupError):
            pass
        except (OSError, IndexError, TypeError, ValueError):
            groups.complete = False
        else:
            parents[pid], groups[pid] = parent, group
            continue
        parent, parent_complete = _linux_proc_status_parent(entry)
        parents.complete = parents.complete and parent_complete
        if parent is not None:
            parents[pid] = parent
        if parent is not None or not parent_complete:
            groups.complete = False
    return parents, groups


# LLM: 消失项不算拒读，status坏文本/权限错误保留父表缺口；不得从父表推断进程组。
# 函数用途: 为仅有status的旧测试或竞态中的进程读取PPid回退字段。
def _linux_proc_status_parent(entry: Path) -> tuple[int | None, bool]:
    try:
        status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        line = next(line for line in status.splitlines() if line.startswith("PPid:"))
        return int(line.split(":", 1)[1].strip()), True
    except (FileNotFoundError, ProcessLookupError):
        return None, True
    except (OSError, StopIteration, TypeError, ValueError):
        return None, False


# LLM: Linux仍单遍proc扫描；无proc默认POSIX共享250ms观察，monkeypatch绕过缓存且不丢完整性缺口。
# 函数用途: 读取父子关系独立副本，保持关闭stdin的ps回退与原完整性合同。
def _process_parent_map() -> tuple[dict[int, int], bool]:
    proc_root = Path("/proc")
    if proc_root.is_dir():
        result, _groups = _linux_proc_parent_and_group_maps(proc_root)
        return result, bool(result) and getattr(result, "complete", True)
    return _ps_relation_map("ppid")


# 函数身份引用仅用于保留既有monkeypatch边界，不缓存任何进程或出生信息。
_DEFAULT_PROCESS_PARENT_MAP = _process_parent_map
_DEFAULT_PROCESS_GROUP_MAP = _process_group_map


# LLM: 只认消失/僵尸/明确复用；旧本地化指纹须同PID原生locale复核，任何观察错误均不等于死亡，不调用waitpid。
# 函数用途: 只读核对原进程实例确已终止，避免升级后的日期语言差异或拒读伪造完成回执。
def _process_instance_terminated(pid: int, birth_token: str) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if _process_is_zombie(pid):
        return True
    current = capture_process_birth_token(pid) if birth_token else ""
    try:
        return bool(birth_token and current and not _birth_token_matches(pid, birth_token, current))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False


# LLM: Linux仍读proc；原生Darwin先核内核观察，False须确认非错误折叠；未知/异常/不支持回退ps，替身绕过。
# 函数用途: 不回收进程地观察终止状态，保留LC_ALL=C、关闭stdin和未知不等于死亡的安全门。
def _process_is_zombie(pid: int) -> bool:
    if _IS_WINDOWS:
        return False
    if Path("/proc").is_dir():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            return stat.rsplit(")", 1)[1].split()[0] in {"Z", "X"}
        except (OSError, IndexError):
            return False
    if _native_posix_probe_enabled():
        try:
            terminated = _native_kernel_terminated(int(pid))
            if isinstance(terminated, bool):
                return terminated
        except (ImportError, OSError, TypeError, ValueError, AttributeError):
            pass
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=1, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().startswith(("Z", "X"))


# LLM: 此处只回答僵尸状态，不把kernel_terminated的ESRCH当作Z；消失由调用方kill(0)单独确认，未知仍走ps。
# 函数用途: 用一次Darwin内核状态读取判断僵尸，不回收进程，也不把身份缺失冒充终止。
def _native_kernel_terminated(pid: int) -> bool | None:
    from ..gateway_parts import multiprocess_native_host_observation as native

    if native.sys.platform != "darwin":
        return None
    terminated = native.kernel_terminated(pid)
    if terminated is False or (
        terminated is True and getattr(native.kernel_terminated, "__name__", "") == "kernel_terminated"
    ):
        info, error = native._darwin_info(pid)
        if info is None or error:
            return None
        return info.prefix[1] == 5
    return terminated if isinstance(terminated, bool) else None


# LLM: Linux/Windows保留原入口；默认无proc路径将完整内核birth贯穿缓存键和host后缀，替换run绕过。
# 函数用途: 读取出生指纹，同秒复用也返回不同身份；ps不可用时仅权威内核观察可补全身份。
def capture_process_birth_token(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return f"proc:{stat.rsplit(')', 1)[1].split()[19]}"
    except (IndexError, OSError):
        pass
    if _native_posix_probe_enabled():
        try:
            from ..gateway_parts.multiprocess_native_host_observation import process_birth

            # store_birth在Darwin只投影到秒；必须保留原pbi_start_tvsec:pbi_start_tvusec作为键。
            kernel_birth = process_birth(int(pid))
        except (ImportError, OSError, TypeError, ValueError, AttributeError):
            kernel_birth = None
        if kernel_birth is not None and str(kernel_birth):
            return _cached_host_birth_token(pid, str(kernel_birth))
    return _read_host_birth_token(pid)


# LLM: 锁覆盖命中与ps采集；完整内核birth同时进入键和返回值，只缓存成功身份，不缓存存活判断。
# 函数用途: 合并相同PID出生实例的并发采集，保留微秒身份供最终信号资格比对。
def _cached_host_birth_token(pid: int, kernel_birth: str) -> str:
    key = (int(pid), kernel_birth)
    with _LEGACY_BIRTH_CACHE_LOCK:
        cached = _PID_BIRTH_TOKEN_CACHE.get(key)
        if cached is not None:
            return cached
        value = _read_host_birth_token(pid, kernel_birth=kernel_birth)
        if not value:
            return value
        _PID_BIRTH_TOKEN_CACHE[key] = value
        if len(_PID_BIRTH_TOKEN_CACHE) > _PID_BIRTH_TOKEN_CACHE_MAX_COUNT:
            _PID_BIRTH_TOKEN_CACHE.popitem(last=False)
        return value


# LLM: ps抛OSError或超时/空响应/失败退出时一律保守返回空串，不污染缓存，不制造第二种token格式。
# 函数用途: 保留原Windows及ps命令和子进程环境；已知内核身份附加微秒后缀，不改父环境。
def _read_host_birth_token(pid: int, *, kernel_birth: str | None = None) -> str:
    if _IS_WINDOWS:
        command = (
            f"(Get-Process -Id {int(pid)} -ErrorAction Stop)."
            "StartTime.ToUniversalTime().Ticks"
        )
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["ps", "-o", "lstart=", "-p", str(int(pid))]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
            env=None if _IS_WINDOWS else {**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        return ""
    return f"host:{value}" + (f"#kb={kernel_birth}" if kernel_birth else "")


# LLM: 纯比较不探测进程；双内核后缀必须整串相等，只有一方无后缀时兼容原基础token，空身份拒绝。
# 函数用途: 区分同秒内不同微秒的进程，同时让既有落盘身份可平滑升级。
def _birth_tokens_match(expected: str | None, current: str | None) -> bool:
    if not expected or not current or not expected.strip() or not current.strip():
        return False
    if "#kb=" in expected and "#kb=" in current:
        return expected.strip() == current.strip()
    return expected.split("#kb=", 1)[0].strip() == current.split("#kb=", 1)[0].strip()


# LLM: 双新格式不等不得落入旧locale；旧投影须重采完整身份，丢失微秒时抛观察错误，不能以False伪造死亡。
# 函数用途: 为实例/信号资格和旧投影调用方共用身份匹配；未知由原调用方保守处理，旧locale复核保持。
def _birth_token_matches(pid: int, expected_birth_token: str, current_token: str | None = None) -> bool:
    current = current_token if current_token is not None else capture_process_birth_token(pid)
    if not expected_birth_token or not current:
        return False
    if "#kb=" in expected_birth_token and "#kb=" not in current and _native_posix_probe_enabled():
        # Store投影可能丢微秒或使用不同基础文本；不能让它掩盖同秒PID复用。
        observed = capture_process_birth_token(pid)
        if not observed or "#kb=" not in observed:
            if observed and _process_is_zombie(pid):
                return _birth_tokens_match(expected_birth_token, observed)
            raise OSError("native process birth observation unavailable")
        return _birth_tokens_match(expected_birth_token, observed)
    if _birth_tokens_match(expected_birth_token, current):
        return True
    if "#kb=" in expected_birth_token and "#kb=" in current:
        return False
    if not _IS_WINDOWS and expected_birth_token.startswith("host:") and current.startswith("host:"):
        try:
            fields = expected_birth_token[5:].split()
            if len(fields) != 5 or fields[0].lower() not in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
                raise ValueError("not C locale weekday/date fields")
            months = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
            month = months.index(fields[1].lower()) + 1
            time.strptime(f"{month} {fields[2]} {fields[3]} {fields[4]}", "%m %d %H:%M:%S %Y")
        except ValueError:
            key = (pid, expected_birth_token, current)
            with _LEGACY_BIRTH_CACHE_LOCK:
                if key in _LEGACY_BIRTH_EQUIVALENTS:
                    return True
                native = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=2, check=False)
                value = " ".join(native.stdout.split())
                if native.returncode != 0 or not value:
                    raise OSError("native process birth observation unavailable") from None
                if value != " ".join(expected_birth_token[5:].split()):
                    return False
                _LEGACY_BIRTH_EQUIVALENTS[key] = None
                if len(_LEGACY_BIRTH_EQUIVALENTS) > _LEGACY_BIRTH_CACHE_MAX_COUNT:
                    _LEGACY_BIRTH_EQUIVALENTS.pop(next(iter(_LEGACY_BIRTH_EQUIVALENTS)))
                return True
    return False


# LLM: 指纹仍决定发送信号资格；只允许精确或经原生locale复核匹配，未知不能借PID存活升级为同一实例。
# 函数用途: 判断是否仍为原进程；只有未记录出生身份的旧入口继续沿原PID存活检查。
def _same_process(pid: int, birth_token: str) -> bool:
    if pid <= 0:
        return False
    if birth_token:
        try:
            return _birth_token_matches(pid, birth_token)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False
    return _pid_alive(pid)


# LLM: A cached record may be reused only for its canonical authority directory;
# equal owner/thread scope never aliases two independently configured stores.
# 函数用途: 核对缓存会话是否属于本次工具绑定的同一个持久目录。
def _record_matches_store(
    record: BackgroundProcess,
    store_root: str | Path | None,
) -> bool:
    if not store_root:
        return True
    if not record.store_root:
        return False
    expected = Path(store_root).expanduser().resolve(strict=False)
    actual = Path(record.store_root).expanduser().resolve(strict=False)
    return actual == expected


# LLM: 仅同出生活进程可授组信号范围，跳过僵尸避免虚报sent及空等宽限；调用者自身及原组边界不变。
# 函数用途: 向仍存活的快照后代发送组和单PID信号，不给已退出的僵尸进程发信号。
def _signal_process_snapshot(snapshot: dict[int, str], signum: int) -> bool:
    """先信号各独立进程组，再逐个信号后代；绝不碰调用者自己的进程组。"""
    own_pid = os.getpid()
    try:
        own_pgid = os.getpgrp()
    except OSError:
        own_pgid = -1
    live = [(pid, token) for pid, token in snapshot.items()
            if pid != own_pid and _same_process(pid, token) and not _process_is_zombie(pid)]
    groups: set[int] = set()
    for pid, _token in live:
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
        if pgid > 0 and pgid != own_pgid:
            groups.add(pgid)
    sent = False
    for pgid in groups:
        try:
            os.killpg(pgid, signum)
            sent = True
        except (ProcessLookupError, PermissionError, OSError):
            continue
    for pid, token in reversed(live):
        if not _same_process(pid, token):
            continue
        try:
            os.kill(pid, signum)
            sent = True
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return sent


# LLM: 合并先按共同匹配入口排除单纯locale变化；明确变代或观察未知都重新核对，不能保留不适用的旧退出证明。
# 函数用途: 合并补充后代快照，兼容旧语言指纹而不把未知实例误记成已经终止。
def _merge_tree_snapshot(snapshot: dict[int, str], current: dict[int, str], terminated: set[int]) -> None:
    for member, token in current.items():
        try:
            same = _birth_token_matches(member, snapshot.get(member, ""), token)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            same = False
        if not same:
            terminated.discard(member)
        snapshot[member] = token


# LLM: terminated 集合跨多次等待累积“已证明消失”的 PID，避免宽限后 PID 复用把死进程记回未解决；
#   调用方在快照里同 PID 换出生标识时负责把它移出集合。
# 函数用途: 在宽限期内轮询快照成员是否都已消失，并把证明过的 PID 记进 terminated。
def _wait_process_snapshot_gone(
    snapshot: dict[int, str],
    proc: subprocess.Popen | None,
    grace_seconds: float,
    terminated: set[int] | None = None,
) -> bool:
    proven = set() if terminated is None else terminated
    deadline = time.monotonic() + grace_seconds
    while True:
        if proc is not None:
            proc.poll()
        for member, token in snapshot.items():
            if member not in proven and _process_instance_terminated(member, token):
                proven.add(member)
        if len(proven) >= len(snapshot):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


# 当前进程的共享缓存；跨主/子代理的权威事实由 ProcessSessionStore 提供。
process_registry = ProcessRegistry()


__all__ = [
    "BackgroundProcess",
    "ProcessRegistry",
    "ProcessTerminationReceipt",
    "capture_process_birth_token",
    "completion_wake_facts",
    "finish_foreground_process",
    "process_registry",
    "reap_test_registered_process",
    "set_test_process_spawn_hook",
    "terminate_process_tree",
]
