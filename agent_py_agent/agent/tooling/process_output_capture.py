# LLM: 前台 Shell 的唯一有界管道采集器；保留上限与排空独立，截断必须以结构化事实暴露。
# 模块用途: 边读 stdout/stderr 边保留有限字节，避免大输出撑满 Gateway 内存或堵住子进程。
from __future__ import annotations

import os
import threading
import time

DEFAULT_CAPTURE_BYTES = 4 * 1024 * 1024


# LLM: 每个 reader 只拥有自己的流；共享状态受锁保护，超出保留上限仍排空，不把丢弃字节称为完整归档。
# 类用途: 管理一个进程的两个输出读取线程和限额计数；取消或回收时可停止非阻塞读取。
class ProcessOutputCapture:
    # LLM: 构造时启动 reader，容量分别限制两条流；不读取 stdin，不创建输出文件或发送模型请求。
    # 函数用途: 开始读取进程管道，默认每条流最多在内存保留 4 MiB。
    def __init__(self, proc, *, max_bytes: int = DEFAULT_CAPTURE_BYTES):
        self._max = max(1, int(max_bytes))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self._seen = {"stdout": 0, "stderr": 0}
        self._ended: set[str] = set()
        self._eof: set[str] = set()
        self._errors: dict[str, str] = {}
        self._threads = []
        for name in self._buffers:
            stream = getattr(proc, name)
            thread = threading.Thread(target=self._read, args=(name, stream), daemon=True)
            self._threads.append(thread)
            thread.start()

    # LLM: 读取块固定 64 KiB；POSIX 与支持 nonblocking pipe 的 Windows 可主动取消，旧 Windows 管道回收依赖进程树终止。
    # 函数用途: 持续排空一条管道，保留前缀、累计真实字节数，并报告非正常关闭。
    def _read(self, name: str, stream) -> None:
        try:
            if stream is None:
                with self._lock:
                    self._eof.add(name)
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
                    with self._lock:
                        self._eof.add(name)
                    break
                with self._lock:
                    self._seen[name] += len(data)
                    remaining = max(0, self._max - len(self._buffers[name]))
                    self._buffers[name].extend(data[:remaining])
        except OSError as exc:
            with self._lock:
                self._errors[name] = type(exc).__name__
        finally:
            if stream is not None:
                stream.close()
            with self._lock:
                self._ended.add(name)

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
        self._stop.set()
        for thread in self._threads:
            thread.join(0.05)
        return drained

    # LLM: complete 同时要求读到 EOF、没有读错且没有丢弃字节；UTF-8 截断可产生 replacement character，原始字节计数仍准确。
    # 函数用途: 获取有限输出及截断/失败元数据，供工具结果和持久账本使用。
    def result(self) -> tuple[str, str, dict]:
        with self._lock:
            retained = {name: len(data) for name, data in self._buffers.items()}
            truncated = any(self._seen[name] > retained[name] for name in retained)
            facts = {
                "complete": len(self._eof) == 2 and not self._errors and not truncated,
                "truncated": truncated,
                "bytes_seen": dict(self._seen),
                "bytes_retained": retained,
                "errors": dict(self._errors),
                "limit_bytes_per_stream": self._max,
            }
            return (self._buffers["stdout"].decode("utf-8", errors="replace"),
                    self._buffers["stderr"].decode("utf-8", errors="replace"), facts)
