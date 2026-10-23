# LLM: 此队列只把设置交给原 durable enqueue，不直接发送请求、不产生第二份落账；IO 失败保留原 ID。
# 模块用途: 让按键线程避开控制 outbox 的磁盘锁，使用一个有界持久化线程。
from __future__ import annotations

import threading
from concurrent.futures import Future
from queue import Empty, Queue


# LLM: 连续失败暂停并保留首项；只由用户 F8 恢复预算，不因新命令偷偷重试旧输入。
# 类用途: 顺序持久化模型和档位命令，忙时不谎报服务器已保存。
class SettingEnqueuer:
    # LLM: 构造不读磁盘，不接触原 outbox 锁；保存回调由原对账器提供。
    # 函数用途: 初始化有界队列、独立线程锁和待重试身份。
    def __init__(self, reconciler):
        self.owner = reconciler
        self.queue = Queue(maxsize=32)
        self.lock = threading.Lock()
        self.worker = None
        self.pending = None
        self.retry = threading.Event()
        self.paused = threading.Event()
        self.results = SettingResults()

    # LLM: put_nowait 不等待磁盘，队列满显式报错；同一 worker 保证提交顺序，不为每次点击新建线程。
    # 函数用途: 接受一个尚未持久化的设置并唤醒后台保存。
    def submit(self, entry, callback=None):
        with self.lock:
            self.queue.put_nowait((entry, callback))
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._run, name="tui-setting-persist", daemon=True)
                self.worker.start()

    # LLM: 只能由显式恢复动作重置自动尝试预算；不换输入身份，不由新命令偷偷重试旧操作。
    # 函数用途: 恢复已暂停的原输入落盘。
    def retry_pending(self):
        if not self.paused.is_set():
            return False
        self.retry.set()
        return True

    # LLM: 失败原行保持在内存 pending，最多三次原 ID 保存尝试；成功后才能交原 dispatcher。
    # 函数用途: 后台处理磁盘转换，持续错误通过原 on_error 告知，不丢弃待保存命令。
    def _run(self):
        failures = 0
        while not self.owner.stop_event.is_set():
            if self.pending is None:
                try:
                    self.pending = self.queue.get(timeout=0.1)
                except Empty:
                    continue
            try:
                entry, callback = self.pending
                self.owner.enqueue(entry, on_persisted_before_dispatch=callback)
            except Exception as error:
                failures += 1
                self.owner.on_error(error)
                if failures >= 3:
                    self.paused.set()
                    self.owner.on_error(SettingPersistencePaused("控制保存已暂停；Esc 返回聊天后按 F8 重试原消息。"))
                    while not self.owner.stop_event.is_set() and not self.retry.wait(0.1):
                        pass
                    self.retry.clear()
                    self.paused.clear()
                    failures = 0
                self.owner.stop_event.wait(min(self.owner.maximum_delay, failures * self.owner.initial_delay))
                continue
            self.pending = None
            failures = 0
            self.queue.task_done()


# LLM: 该异常只表示有限落盘尝试耗尽，不代表 Gateway 执行失败或消息已被丢弃。
# 类用途: 向界面提供可见且可恢复的持久化暂停状态。
class SettingPersistencePaused(RuntimeError):
    pass


# LLM: 内存 Future 仅观察原 outbox 终态，不是第二本收据；锁不保护 IO，后台结果用 asyncio.wrap_future 接回 UI。
# 类用途: 让菜单等待同一设置投递通道，而不是另发一次写请求。
class SettingResults:
    # LLM: 只创建显示观察表，真实提交仍由 reconciler 处理。
    # 函数用途: 初始化短锁和未完成观察者。
    def __init__(self):
        self.lock = threading.Lock()
        self.waiters = {}

    # LLM: 一项只允许一个观察 Future；身份由原 Entry.message_id 提供。
    # 函数用途: 登记菜单对原操作的观察。
    def watch(self, message_id):
        with self.lock:
            return self.waiters.setdefault(message_id, Future())

    # LLM: 仅在入队明确失败时撤去观察者；未知持久结果不能调用这里丢掉原身份。
    # 函数用途: 清理从未被队列接纳的菜单观察 Future。
    def abandon(self, message_id):
        with self.lock:
            self.waiters.pop(message_id, None)

    # LLM: 原终态才结算；完成数据直接来自同一 ConversationControlResult，不解析文案。
    # 函数用途: 将原收据交给等待菜单并移除本地观察者。
    def complete(self, entry, result):
        with self.lock:
            future = self.waiters.pop(entry.message_id, None)
        if future is not None and not future.done():
            future.set_result({**(result.details or {}), "ok": result.ok, "message": result.message,
                               "control_state": result.control_state})
