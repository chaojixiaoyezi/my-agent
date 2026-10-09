# LLM: 续租只核对规范 state 的精确 lease_id；不另建执行权，不改变提取预算或游标，不续租他人租约。
# 模块用途: 为 Curator 整轮（包括提交后晋升）提供可停止心跳，并在晋升结束后单独释放租约。
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

from ..common.json_io import locked_json_path
from . import curator_state


# LLM: 调用方只提供已持有租约的 ID；锁内再核对，不因进程相同或 run 相似接受错误身份。
# 函数用途: 在 state 锁内延长当前 Curator 租约的期限并留下心跳时间，不改计数和游标。
def renew_curator_lease(store, lease_id: str, seconds: int) -> None:
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
def retire_curator_lease(store, lease_id: str) -> None:
    with locked_json_path(store.path):
        state = store._load_unlocked()
        if str(state.active_lease.get("lease_id") or "") != lease_id:
            return
        store._write_unlocked(replace(state, active_lease={**state.active_lease, "phase": "interrupted"}))


# LLM: 守卫包住主体与失败收尾；同级 except 捕不到失败审计内部中断，必须有独立外层。
# 函数用途: 任意异常逃出整轮后取消执行保护，清理失败只补诊断而不盖掉原异常。
@contextmanager
def curator_lease_interruption_guard(store, lease_id: str):
    try:
        yield
    except BaseException as exc:
        try:
            retire_curator_lease(store, lease_id)
        except Exception as cleanup_error:
            exc.add_note(f"curator lease cleanup failed: {type(cleanup_error).__name__}")
        raise


# LLM: 释放不重放 success commit，否则会重复累计消息/候选；已经失去 ID 时不动新租约。
# 函数用途: 晋升尾段结束后只清空本运行的租约，保留已提交游标、计数与新触发代次。
def release_curator_lease(store, lease_id: str) -> None:
    with locked_json_path(store.path):
        state = store._load_unlocked()
        if str(state.active_lease.get("lease_id") or "") != lease_id:
            return
        store._write_unlocked(replace(state, active_lease={}))


# LLM: 心跳线程只持短 state 锁；首次同步续租，循环只到上下文退出，遇错不重新领取执行权。
# 类用途: 续租一个正在执行的 Curator，退出时停止并等待该线程，不遗留守护写入。
class _LeaseHeartbeat:
    # LLM: 这里只绑定依赖和停止事件；线程由上下文显式启动，便于异常路径回收。
    # 函数用途: 初始化某个规范租约的心跳状态，不写文件。
    def __init__(self, store, lease_id: str, seconds: int):
        self.store, self.lease_id, self.seconds = store, lease_id, seconds
        self.stop = threading.Event()
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self.run, name="curator-lease-heartbeat", daemon=True)

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
@contextmanager
def curator_lease_heartbeat(store, lease_id: str, seconds: int):
    renew_curator_lease(store, lease_id, seconds)
    heartbeat = _LeaseHeartbeat(store, lease_id, seconds)
    heartbeat.thread.start()
    try:
        yield
    finally:
        heartbeat.stop.set()
        heartbeat.thread.join()
    # 必须等在途续租真正结束才看错误；主体抛错时不会走到这里，保留主体原始异常。
    if heartbeat.error is not None:
        raise heartbeat.error
