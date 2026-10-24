# LLM: 前台 Shell 与沙箱同步入口共用的唯一有界管道采集器；保留上限与排空独立，逐流截断与严格UTF-8解码事实供消费者裁决。
# 模块用途: 边读 stdout/stderr 边保留有限字节，避免大输出撑满 Gateway 内存或堵住子进程。
from __future__ import annotations

import os
import threading
import time

from .process_pipe_pump import ProcessPipePump

# 进程输出捕获默认上限 4 MiB：防止后台进程刷屏把内存/磁盘打爆。
DEFAULT_CAPTURE_BYTES = 4 * 1024 * 1024


# LLM: 只严格解码有界保留前缀一次；失败才按Shell原合同替换字符，坏字节事实不靠搜索替换字符推断。
# 函数用途: 返回保留输出的文本和非法UTF-8标志，不抛解码异常，不改变原始字节计数。
def _retained_text(data: bytes | bytearray) -> tuple[str, bool]:
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), True


# LLM: POSIX单pump，Windows保留双reader；共享状态与generation受同一条件锁保护，超限仍排空。
# 类用途: 管理进程有界输出和事实账本，提供不会丢唤醒的变化等待。
class ProcessOutputCapture:
    # LLM: 构造时启动 reader，容量分别限制两条流；不读取 stdin，不创建输出文件或发送模型请求。
    # 函数用途: 开始读取进程管道，默认每条流最多在内存保留 4 MiB。
    def __init__(self, proc, *, max_bytes: int = DEFAULT_CAPTURE_BYTES):
        self._max = max(1, int(max_bytes))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._generation = 0
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self._seen = {"stdout": 0, "stderr": 0}
        self._ended: set[str] = set()
        self._eof: set[str] = set()
        self._errors: dict[str, str] = {}
        self._threads = []
        self._pump = None
        if os.name == "posix":
            self._pump = ProcessPipePump(self, {name: getattr(proc, name) for name in self._buffers})
            thread = threading.Thread(target=self._pump.run, daemon=True)
            self._threads.append(thread)
            try:
                thread.start()
            except BaseException:
                self._pump.close()
                raise
            return
        for name in self._buffers:
            stream = getattr(proc, name)
            thread = threading.Thread(target=self._read, args=(name, stream), daemon=True)
            self._threads.append(thread)
            thread.start()

    # LLM: Windows保留双流读取路径；POSIX不调用此函数，计数与EOF均委托共同账本。
    # 函数用途: 在旧平台持续排空一条输出，不改变原有限前缀语义。
    def _read(self, name: str, stream) -> None:
        try:
            if stream is None:
                self.record_eof(name)
                return
            fd = stream.fileno()
            try:
                os.set_blocking(fd, False)
            except (OSError, ValueError):
                pass
            while not self._stop.is_set():
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    self._stop.wait(0.01)
                    continue
                if not data:
                    self.record_eof(name)
                    break
                self.record_data(name, data)
        except (OSError, ValueError) as exc:
            self.record_error(name, exc)
        finally:
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError) as exc:
                    self.record_error(name, exc)
            self.record_ended(name)

    # LLM: 调用方必须在检查done/取消之前采样，再传给wait_changed，消除检查与等待之间的竞态。
    # 函数用途: 读取当前输出状态代次。
    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    # LLM: 同锁比较代次并进入wait，不把EOF或输出通知当作进程退出；允许显式传入检查前的代次。
    # 函数用途: 等输出或停止状态变化，超时仍返回当前代次。
    def wait_changed(self, timeout: float, *, generation: int | None = None) -> int:
        with self._changed:
            observed = self._generation if generation is None else generation
            self._changed.wait_for(lambda: self._generation != observed, max(0.0, timeout))
            return self._generation

    # LLM: 调用方持有条件锁；只推进观察代次，不改变任何进程/EOF事实。
    # 函数用途: 发布一次采集变化并唤醒等待者。
    def _notify(self) -> None:
        self._generation += 1
        self._changed.notify_all()

    # LLM: 显式取消唤醒同时覆盖父等待和reader；pump在锁下安全写self-pipe。
    # 函数用途: 不停止采集，只打断当前等待。
    def wake(self) -> None:
        with self._changed:
            self._notify()
        if self._pump is not None:
            self._pump.wake()

    # LLM: 停止只作用于本地采集，不杀进程，也不伪造EOF；仍由reader关闭流。
    # 函数用途: 请求采集退出并立即唤醒等待器。
    def stop(self) -> None:
        self._stop.set()
        self.wake()

    # LLM: pump只读取停止标志，不能改动输出账本。
    # 函数用途: 检查采集是否被显式停止。
    def stopping(self) -> bool:
        return self._stop.is_set()

    # LLM: 每流独立限额，超过部分丢弃但持续计数；通知消费者，不持锁做IO。
    # 函数用途: 记下一块真实读取字节。
    def record_data(self, name: str, data: bytes) -> None:
        with self._changed:
            self._seen[name] += len(data)
            remaining = max(0, self._max - len(self._buffers[name]))
            self._buffers[name].extend(data[:remaining])
            self._notify()

    # LLM: 仅真实零字节读取或None流可记EOF，异常/停止不可调用。
    # 函数用途: 记录一条流的真实末尾。
    def record_eof(self, name: str) -> None:
        with self._changed:
            self._eof.add(name)
            self._notify()

    # LLM: 保留首个读/关闭错误；不能把错误覆盖成正常EOF。
    # 函数用途: 记录流失败并唤醒消费者。
    def record_error(self, name: str, exc: Exception) -> None:
        with self._changed:
            self._errors.setdefault(name, type(exc).__name__)
            self._notify()

    # LLM: ended与EOF分离，失败或停止一样结束reader，但不表示完整捕获。
    # 函数用途: 记录reader已经退出。
    def record_ended(self, name: str) -> None:
        with self._changed:
            self._ended.add(name)
            self._notify()

    # LLM: done 仅表示管道结束或读错，不代表命令成功；必须与 result/capture 事实一起使用。
    # 函数用途: 非阻塞检查两个输出读取器是否退出。
    def done(self) -> bool:
        with self._lock:
            return len(self._ended) == 2

    # LLM: 等待两个 reader 共用一个 deadline；超时只停止本地采集，不能声称后代进程已终止。
    # 函数用途: 给已结束进程有限时间排空输出，然后返回是否完整读到管道末尾。
    def finish(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0, timeout)
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        with self._lock:
            drained = len(self._eof) == 2 and not self._errors
        self.stop()
        deadline = time.monotonic() + 0.05
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        return drained

    # LLM: complete 仅指两流EOF、无读错和丢弃；各流严格解码事实独立，检查程序拒绝坏stdout，Shell仍用替换字符展示。
    # 函数用途: 获取有限输出及截断/失败元数据，供工具结果和持久账本使用。
    def result(self) -> tuple[str, str, dict]:
        with self._lock:
            decoded = {name: _retained_text(data) for name, data in self._buffers.items()}
            retained = {name: len(data) for name, data in self._buffers.items()}
            discarded = {name: self._seen[name] - retained[name] for name in retained}
            streams_truncated = {name: discarded[name] > 0 for name in retained}
            truncated = any(streams_truncated.values())
            facts = {
                "complete": len(self._eof) == 2 and not self._errors and not truncated,
                "truncated": truncated,
                "bytes_seen": dict(self._seen),
                "bytes_retained": retained,
                "bytes_discarded": discarded,
                "streams_truncated": streams_truncated,
                "streams_eof": {name: name in self._eof for name in retained},
                "streams_invalid_utf8": {name: value[1] for name, value in decoded.items()},
                "errors": dict(self._errors),
                "limit_bytes_per_stream": self._max,
            }
            return decoded["stdout"][0], decoded["stderr"][0], facts
