# LLM: 原心跳运输同一source并逐次fresh T；原生异常仅内存保同observer/Thread/Context/租约出处，公开诊断只白名单标量，不签权或证明退出。
# 模块用途: 在原请求短门内续租或收尾策展租约，撤权和退出后保留责任。
# LLM: 续租仅核规范 state 精确 lease_id；原运输和物理退出未结不得退役原执行责任，不能据异常猜已退出或另建权限。
# 模块用途: 维持原策展租约和心跳，普通中断保期限，未结运输继续保留原租约。
from __future__ import annotations

import contextvars
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.json_io import locked_json_path
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
    gateway_short_transaction,
)
from . import curator_state


# LLM: 调用方只提供已持有租约的 ID；锁内再核对，不因进程相同或 run 相似接受错误身份。
# 函数用途: 在 state 锁内延长当前 Curator 租约的期限并留下心跳时间，不改计数和游标。
# LLM: 每次先原T/P3/P5短核再state锁；仅本lease精确ID，失效原typed不得写或清租约，Count0不增加I/O。
# 函数用途: 在原执行仍有效时续租或收尾同一租约，撤权后保留原责任。
def renew_curator_lease(store, lease_id: str, seconds: int) -> None:
    with gateway_short_transaction("renew_curator_lease"):
        with locked_json_path(store.path):
            state = store._load_unlocked()
            curator_state._require_lease(state, lease_id)
            now = curator_state.datetime.now(curator_state.timezone.utc)
            expires = max(curator_state.datetime.fromisoformat(str(state.active_lease["expires_at"])),
                          now + timedelta(seconds=max(30, seconds)))
            lease = {**state.active_lease, "heartbeat_at": now.isoformat(),
                     "phase": state.active_lease.get("phase") or "executing", "expires_at": expires.isoformat()}
            store._write_unlocked(replace(state, active_lease=lease))


# LLM: 捕获进程级中断说明主体已退出；保留原崩溃恢复短期租约，但不能仍宣称正在执行。
# 函数用途: 取消执行保护，保留原期限和事务恢复身份供既有恢复入口使用。
# LLM: 每次先原T/P3/P5短核再state锁；仅本lease精确ID，失效原typed不得写或清租约，Count0不增加I/O。
# 函数用途: 在原执行仍有效时续租或收尾同一租约，撤权后保留原责任。
def retire_curator_lease(store, lease_id: str) -> None:
    with gateway_short_transaction("retire_curator_lease"):
        with locked_json_path(store.path):
            state = store._load_unlocked()
            if str(state.active_lease.get("lease_id") or "") != lease_id:
                return
            store._write_unlocked(replace(state, active_lease={**state.active_lease, "phase": "interrupted"}))


# LLM: 原端口/运输和物理退出未结必须原样越过退役；心跳停止事实仍读原异常字典，不能用普通异常清理洗掉责任。
# 函数用途: 只为普通中断取消执行保护，未结时保持 executing、期限与原异常不变。
@contextmanager
def curator_lease_interruption_guard(store, lease_id: str):
    try:
        yield
    except (RequestExecutionContextUnavailable, RequestExecutionOutcomeUncertainError, BoundedCallExitUnconfirmedError):
        raise
    except BaseException as exc:
        if lease_exception_metadata(exc).get("lease_heartbeat_stop_error_class"):
            raise
        try:
            retire_curator_lease(store, lease_id)
        except Exception as cleanup_error:
            exc.add_note(f"curator lease cleanup failed: {type(cleanup_error).__name__}")
        raise


# LLM: 释放不重放 success commit，否则会重复累计消息/候选；已经失去 ID 时不动新租约。
# 函数用途: 晋升尾段结束后只清空本运行的租约，保留已提交游标、计数与新触发代次。
# LLM: 每次先原T/P3/P5短核再state锁；仅本lease精确ID，失效原typed不得写或清租约，Count0不增加I/O。
# 函数用途: 在原执行仍有效时续租或收尾同一租约，撤权后保留原责任。
def release_curator_lease(store, lease_id: str) -> None:
    with gateway_short_transaction("release_curator_lease"):
        with locked_json_path(store.path):
            state = store._load_unlocked()
            if str(state.active_lease.get("lease_id") or "") != lease_id:
                return
            store._write_unlocked(replace(state, active_lease={}))


# LLM: 出生前固定原心跳/Thread/Context/租约/PID；原对象内留一次错误出处，不建表、不序列化或运行Context，复制观察无效。
# 类用途: 保存同一次心跳的只读原引用，供请求出口核对停止异常来自哪次执行。
@dataclass(frozen=True, eq=False, slots=True, repr=False)
class _LeaseHeartbeatOrigin:
    heartbeat: object
    thread: threading.Thread
    started: threading.Event
    context: contextvars.Context
    lease_id: str
    pid: int
    error: BaseException | None = field(default=None, init=False)
    origin: object = field(init=False)

    # LLM: 仅固定自身份，不读磁盘、不启动线程；复制出的origin仍指原对象，因此不能代替原观察。
    # 函数用途: 标记这份观察自身，供后续同对象校验。
    def __post_init__(self):
        object.__setattr__(self, "origin", self)


# LLM: 心跳线程只持短 state 锁；首次同步续租，循环只到上下文退出，遇错不重新领取执行权。
# 类用途: 续租一个正在执行的 Curator，退出时停止并等待该线程，不遗留守护写入。
class _LeaseHeartbeat:
    # LLM: 这里只绑定依赖和停止事件；线程由上下文显式启动，便于异常路径回收。
    # 函数用途: 初始化某个规范租约的心跳状态，不写文件。
    # LLM: copy_context只运输同一source，原线程每次renew重取fresh T；出生前同批固定只读出处，不由异常或新租约重新绑定身份。
    # 函数用途: 给同一心跳线程保留原执行来源，构造不启动线程、不续租或写文件。
    def __init__(self, store, lease_id: str, seconds: int):
        self.store, self.lease_id, self.seconds = store, lease_id, seconds
        self.stop = threading.Event()
        self.error: Exception | None = None
        self.context = contextvars.copy_context()
        self.thread = threading.Thread(target=lambda: self.context.run(self.run), name="curator-lease-heartbeat", daemon=True)
        self._origin = _LeaseHeartbeatOrigin(self, self.thread, self.thread._started, self.context, lease_id, os.getpid())

    # LLM: 原观察引用出生前只绑定一次；可见Thread/Context/租约改动由查询拒绝，不能换观察洗成新来源。
    # 函数用途: 防止普通字段赋值替换原心跳出处，不增加全局登记表。
    def __setattr__(self, name, value):
        if name == "_origin" and "_origin" in self.__dict__ and value is not self.__dict__[name]:
            raise AttributeError("原心跳观察不能替换")
        object.__setattr__(self, name, value)

    # LLM: 间隔使用期限的三分之一；晋升慢于原提取期限时仍续租，确定死亡回收仍由原 E11d 判据裁决。
    # 函数用途: 周期更新同一租约，失败后停止并把原始异常交回执行线程。
    def run(self):
        while not self.stop.wait(max(0.01, self.seconds / 3)):
            try:
                renew_curator_lease(self.store, self.lease_id, self.seconds)
            except Exception as exc:
                self.error = exc
                return


# LLM: 持租约执行的生命周期使用；心跳不会吞执行异常，正常退出时续租错误原样暴露。
# 函数用途: 在模型提取、事务提交和晋升期间持续续租，所有退出路径停止心跳线程。
# LLM: 原Thread已被heartbeat持有后才start；ACK异常也进入同一stop/join，未知保原生停止标记与租约，不制造BoundedCall身份或门前未进入事实。
# 函数用途: 将启动确认异常纳入原心跳收尾，真实join确认后才允许普通失败处理。
@contextmanager
def curator_lease_heartbeat(store, lease_id: str, seconds: int):
    renew_curator_lease(store, lease_id, seconds)
    heartbeat = _LeaseHeartbeat(store, lease_id, seconds)
    primary = None
    try:
        heartbeat.thread.start()
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _stop_heartbeat(heartbeat, primary)
    # 必须等在途续租真正结束才看错误；主体抛错时不会走到这里，保留主体原始异常。
    if heartbeat.error is not None:
        raise heartbeat.error


# LLM: stop/join中断不证明已停；原异常原生字典保标量诊断及原观察一次出处，旧异常不能重绑另一心跳，未知不清责任。
# 函数用途: 请求停止并等待心跳，两步均在同一保护范围内，失败保留原异常对象。
def _stop_heartbeat(heartbeat: _LeaseHeartbeat, primary: BaseException | None) -> None:
    try:
        heartbeat.stop.set()
        heartbeat.thread.join()
    except BaseException as exc:
        target = exc if primary is None else primary
        metadata = lease_exception_metadata(target)
        metadata["lease_heartbeat_stop_error_class"] = type(exc).__name__
        if "_lease_heartbeat_origin" not in metadata:
            origin = getattr(heartbeat, "_origin", None)
            if type(origin) is _LeaseHeartbeatOrigin:
                if origin.error is None:
                    object.__setattr__(origin, "error", target)
                if origin.error is target:
                    metadata["_lease_heartbeat_origin"] = origin
        if primary is None:
            raise


# LLM: 异常子类可能伪装 __dict__，用 BaseException 原生描述符取真正实例元数据；停止标记读写同源，不能由临时/只读映射屏蔽。
# 函数用途: 取得原异常真实字典，保留对象身份且绕过子类属性钩子。
def lease_exception_metadata(error: BaseException) -> dict:
    return BaseException.__dict__["__dict__"].__get__(error)


# LLM: 只核原生异常/observer/心跳/Thread/Context/PID与构造租约同源；不读state、不运行Context、不判死或授新权，消费者仍核原claim。
# 函数用途: 返回原停止异常的只读上下文与租约ID，伪造标记、复制或引用漂移均无出处。
def lease_heartbeat_error_origin(error: BaseException) -> tuple[contextvars.Context, str] | None:
    if not isinstance(error, BaseException):
        return None
    origin = lease_exception_metadata(error).get("_lease_heartbeat_origin")
    if (type(origin) is not _LeaseHeartbeatOrigin or origin.origin is not origin
            or origin.error is not error or origin.pid != os.getpid()):
        return None
    heartbeat = origin.heartbeat
    if (getattr(heartbeat, "_origin", None) is not origin or getattr(heartbeat, "thread", None) is not origin.thread
            or getattr(origin.thread, "_started", None) is not origin.started or getattr(heartbeat, "context", None) is not origin.context
            or type(origin.context) is not contextvars.Context or type(origin.lease_id) is not str
            or not origin.lease_id or getattr(heartbeat, "lease_id", None) != origin.lease_id):
        return None
    return origin.context, origin.lease_id
