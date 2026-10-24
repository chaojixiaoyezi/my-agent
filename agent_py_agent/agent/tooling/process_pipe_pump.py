# LLM: POSIX唯一管道reader；capture拥有字节/EOF账本，本模块只负责selector与描述符生命周期。
# 模块用途: 一条线程同时排空stdout/stderr，用自唤醒管道打断无超时等待。
from __future__ import annotations

import os
import selectors
import threading


# LLM: 同一reader独占流的读取与关闭；wake写入/关闭用锁防止FD复用竞争，不记录第二份输出状态。
# 类用途: 管理两条输出和一条唤醒管道的POSIX事件循环。
class ProcessPipePump:
    # LLM: 初始化仅创建自管道；业务管道由run接管，初始化失败不关闭调用方描述符。
    # 函数用途: 保存采集器和流并建立非阻塞唤醒通道。
    def __init__(self, capture, streams: dict):
        self.capture = capture
        self.streams = dict(streams)
        self._lock = threading.Lock()
        self._wake_read, self._wake_write = os.pipe()
        try:
            os.set_blocking(self._wake_read, False)
            os.set_blocking(self._wake_write, False)
        except BaseException:
            self.close()
            raise

    # LLM: 与wake共锁防止FD复用；初始化或线程启动失败也可调用，不关闭业务流，不覆盖已有异常。
    # 函数用途: 幂等释放自管道的两个描述符。
    def close(self) -> None:
        with self._lock:
            for attribute in ("_wake_read", "_wake_write"):
                descriptor = getattr(self, attribute)
                if descriptor is None:
                    continue
                setattr(self, attribute, None)
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    # LLM: 满管道已有未消费唤醒，不需要再写；关闭后禁止向被复用FD写入。
    # 函数用途: 让阻塞selector立即复核停止标志。
    def wake(self) -> None:
        with self._lock:
            if self._wake_write is not None:
                try:
                    os.write(self._wake_write, b"w")
                except BlockingIOError:
                    pass

    # LLM: select无固定超时；每事件只读一块保证两流和WAKE公平，结束/错误关闭仍由同一reader执行。
    # 函数用途: 同时监听输出与停止通知，退出时释放所有注册和描述符。
    def run(self) -> None:
        active = {}
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self._wake_read, selectors.EVENT_READ, None)
                self._register_streams(selector, active)
                self._pump_events(selector, active)
        except (OSError, ValueError) as exc:
            for name in active.values():
                self.capture.record_error(name, exc)
        finally:
            self._close_streams()
            self.close()

    # LLM: 无输出时无限阻塞selector，由wake或EOF唤醒；每事件一块，避免饱和stdout饿死stderr/stop。
    # 函数用途: 在同一selector中公平调度所有输出事件。
    def _pump_events(self, selector, active: dict) -> None:
        while active and not self.capture.stopping():
            for key, _ in selector.select():
                if key.data is None:
                    self._drain_wake()
                elif key.fd in active:
                    self._read_ready(selector, active, key.fd)
                if self.capture.stopping():
                    break

    # LLM: 每流注册失败单独记账，其它流继续排空；None流等价原EOF，不声称进程退出。
    # 函数用途: 给两条流设置非阻塞并加入同一selector。
    def _register_streams(self, selector, active: dict) -> None:
        for name, stream in self.streams.items():
            if stream is None:
                self.capture.record_eof(name)
                continue
            try:
                fd = stream.fileno()
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ, name)
                active[fd] = name
            except (OSError, ValueError) as exc:
                self.capture.record_error(name, exc)

    # LLM: EAGAIN不是EOF，部分读取也不是EOF；读错只终止该流并保留错误，超限数据交capture继续计数。
    # 函数用途: 处理一条可读输出，EOF或失败才注销它。
    def _read_ready(self, selector, active: dict, fd: int) -> None:
        name = active[fd]
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        except (OSError, ValueError) as exc:
            self.capture.record_error(name, exc)
        else:
            if data:
                self.capture.record_data(name, data)
                return
            self.capture.record_eof(name)
        selector.unregister(fd)
        del active[fd]

    # LLM: 自管道只含唤醒字节，不进入业务输出；非阻塞排空直到EAGAIN。
    # 函数用途: 清掉当前积累的唤醒通知。
    def _drain_wake(self) -> None:
        try:
            while os.read(self._wake_read, 65536):
                pass
        except BlockingIOError:
            pass

    # LLM: 只关闭本次交给pump的流；关闭错误也记capture，ended与EOF保持独立。
    # 函数用途: 关闭两条流并通知采集器reader已退出。
    def _close_streams(self) -> None:
        for name, stream in self.streams.items():
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError) as exc:
                self.capture.record_error(name, exc)
            self.capture.record_ended(name)
