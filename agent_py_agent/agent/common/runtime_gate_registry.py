# LLM: 仅弱引用登记当前进程控制器，观察当前线程持有的 M/L；不保存租约状态、不授执行权限、不跨进程恢复。
# 模块用途: 让子代理创建锁在协调者短门内只尝试获取，阻断 CG 与 M/L 的反向等待。
"""当前进程运行时锁的只读持有观察。"""
from __future__ import annotations

import threading
import weakref

_LIVE = weakref.WeakSet()
_LIVE_LOCK = threading.Lock()


# LLM: 仅表示 M/L 持有者首次获取 CG 时的竞争，继承原非阻塞 IO 语义，不代表业务体写入失败。
# 类用途: 通知事务调用方先退出短门，再重新尝试原操作。
class SubagentCreationGuardBusy(BlockingIOError):
    pass


# LLM: 构造器在 M/L 字段赋值后调用；只保存弱引用，不延长控制器或租约寿命。
# 函数用途: 登记一个需要参与当前线程锁序检查的运行时对象。
def register_runtime_gate(owner) -> None:
    with _LIVE_LOCK:
        _LIVE.add(owner)


# LLM: 只观察当前线程，缺属性的对象计为未持锁；快照门不跨运行时锁检查，避免引入第二锁序。
# 函数用途: 判断当前线程是否已持任一登记控制器的互斥锁或租约生命周期锁。
def current_thread_holds_runtime_gate() -> bool:
    with _LIVE_LOCK:
        owners = tuple(_LIVE)
    for owner in owners:
        try:
            if (owner._mutex._is_owned() or (getattr(owner, "_lease", None) is not None
                    and owner._lease._lifecycle_gate._is_owned())):
                return True
        except AttributeError:
            continue
    return False
