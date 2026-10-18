# LLM: P6仅保留本次trusted组合根已经创建的原Thread/Executor；不按线程名称、全局枚举或future.done推退出，不建权限/持久状态，同步实际held资源及Count0测试。
# 模块用途: 保留实际线程池与线程，停止出生后用原对象判断物理收尾是否完成。
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor


# LLM: 快照仍引用实际Thread，不复制其内部状态；缺原锁、未知线程类型或未确认出生都须拒绝，不返回成功空集合。
# 函数用途: 在原executor自身的创建/关闭锁下冻结真实线程引用。
def executor_threads(executor):
    if not isinstance(executor, ThreadPoolExecutor):
        raise ValueError("drain_executor_required")
    with executor._shutdown_lock:
        threads = tuple(executor._threads)
    if any(type(thread) is not threading.Thread for thread in threads):
        raise ValueError("drain_original_thread_required")
    return threads


# LLM: 等待仅门外使用原Thread，未start ACK不能当不存在；期限只是等待预算，不是退出证据，无原对象的未知必须保留。
# 函数用途: 有界join原线程，返回仍活或出生状态不确定的实际对象。
def join_original_threads(threads, deadline):
    pending = []
    for thread in threads:
        if type(thread) is not threading.Thread or thread.ident is None:
            pending.append(thread)
            continue
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            pending.append(thread)
    return tuple(pending)


# LLM: 原组合根交来的单一资源对象/来源ref不授业务许可；birth失败只追加限制性fault，既有源不能替换或被清表冒充死亡。
# 类用途: 保留一个实际执行池或设施线程及其明确来源分类。
class DrainResource:
    # LLM: 构造纯内存；kind/ref由可信producer给出，owner只是原资源定位，必须保留准确同一对象。
    # 函数用途: 记录一组来源的实际Thread或Executor，尚未停止出生时不能核准为空。
    def __init__(self, kind, resource_ref, owner=None):
        self.kind, self.resource_ref = kind, resource_ref
        self.owner = self._owner_origin = owner
        self._origin, self._creator_pid = self, os.getpid()
        self._gate = threading.RLock()
        self._born_threads = {}
        self._stopped = False
        self._fault = None

    # LLM: 只有原producer在第一次submit前绑定一次准确池；initializer由同一个对象供给，不从后来的空_threads重建出生历史。
    # 函数用途: 保存实际构造出的原池，让其worker在进入业务前登记真实Thread。
    def bind_executor(self, owner):
        with self._gate:
            if self._origin is not self or self._creator_pid != os.getpid() or self.owner is not None:
                raise ValueError("drain_resource_rebind")
            if not isinstance(owner, ThreadPoolExecutor):
                raise ValueError("drain_executor_required")
            self.owner = self._owner_origin = owner

    # LLM: 真实executor initializer在业务前调用；只记录actual current_thread，不赋actor/owner许可。复制、错进程、未绑定池只保unknown。
    # 函数用途: 在准确worker出生时保留原线程，避免关闭或清空标准库线程表抹去实际责任。
    def remember_current_thread(self):
        self._require_original()
        thread = threading.current_thread()
        with self._gate:
            if type(thread) is not threading.Thread or not isinstance(self.owner, ThreadPoolExecutor):
                self.retain_fault(ValueError("drain_original_worker_required"))
                raise ValueError("drain_original_worker_required")
            self._born_threads[thread] = thread

    # LLM: 不接收可变替身；本表只限制/观察，不签发权限或按异常正文裁决。
    # 函数用途: 在一次真实启动/提交失败后保留无法确认出生的资源缺口。
    def retain_fault(self, error):
        with self._gate:
            if self._fault is None:
                self._fault = type(error).__name__

    # LLM: Thread仅原producer通知；Executor先关闭submit但保所有已原claim的排队工作，不在门内join；HTTP传输未执行连接取消仍由原server_close负责，不统一取消业务。
    # 函数用途: 关闭已捕获池的新出生入口，实际join由后续门外阶段承担。
    def stop(self):
        self._require_original()
        if isinstance(self.owner, ThreadPoolExecutor):
            self.owner.shutdown(wait=False, cancel_futures=False)
        elif type(self.owner) is not threading.Thread:
            raise ValueError("drain_resource_required")
        self._stopped = True

    # LLM: 已停止submit也不能从future/字段清空推物理退出，出生未知或fault永久保留；同原Thread真实join之后才settled。
    # 函数用途: 观察准确资源，返回settled/running/unknown而不写任何业务账。
    def observe(self, deadline):
        try:
            self._require_original()
        except ValueError:
            return 'unknown'
        if not self._stopped or self._fault is not None:
            return "unknown"
        if isinstance(self.owner, ThreadPoolExecutor):
            current = executor_threads(self.owner)
            with self._gate:
                threads = tuple(self._born_threads)
                if any(self._born_threads[thread] is not thread for thread in threads) or any(
                        thread not in self._born_threads for thread in current):
                    return "unknown"
        else:
            threads = (self.owner,)
        pending = join_original_threads(threads, deadline)
        if any(type(thread) is not threading.Thread or thread.ident is None for thread in pending):
            return "unknown"
        return "running" if pending else "settled"

    # LLM: 复制外壳/资源字段替换不能改变原引用；校验失败不消费资源或关闭陌生对象。
    # 函数用途: 保持这条观察仍绑定出生时的准确资源。
    def _require_original(self):
        if (type(self) is not DrainResource or self._origin is not self or self._creator_pid != os.getpid()
                or self.owner is None or self.owner is not self._owner_origin):
            raise ValueError("drain_original_resource_required")
