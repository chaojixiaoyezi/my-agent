# LLM: 插件 MCP 只能使用原 BackgroundLaunch/ProcessSessionStore；这里绑定一次资源，不执行工具或持有第二份激活权威。
# 模块用途: 将固定激活的 MCP 管道接到原托管进程，并保留原生清理回执及提交异常。

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from ..plugin_activation_ref import PluginActivationRef
from .background_process_launch import (
    BackgroundLaunchRequest,
    HostedBackgroundProcess,
    start_background_process,
)
from .mcp_protocol import MCPError
from .process_scope import ProcessAccessScope, ProcessExecutionScope
from .process_session_cleanup import (
    ProcessSessionCleanup,
    ProcessSessionCleanupError,
    stop_process_session,
)
from .process_session_records import merge_process_record
from .process_session_store import (
    ProcessSessionStore,
    ProcessSessionTransaction,
    process_session_store_root,
)


# LLM: hosted 与 activation 一次绑定；原句柄不能随 client.current 换代，清理不按任务或进程名重新搜索。
# 类用途: 保存一条插件连接的实际 host/child 和固定激活引用。
@dataclass(frozen=True)
class ManagedMCPProcess:
    hosted: HostedBackgroundProcess
    activation: PluginActivationRef

    # LLM: argv/cwd/env 来自已授权宿主；只用原托管入口，失败不能回退裸 Popen，stdio 与 launcher 同寿命。
    # 函数用途: 为插件启动带原资源登记的进程，将输出管道交给唯一的 UTF-8 文本读取端。
    @classmethod
    def launch(cls, activation: PluginActivationRef, *, argv: list[str], cwd: Path,
               environment: dict[str, str], timeout: float) -> ManagedMCPProcess:
        owner = activation.owner()
        if not cwd.is_absolute():
            raise ValueError("插件 MCP 工作目录必须由宿主固定为绝对路径")
        hosted = start_background_process(BackgroundLaunchRequest(
            argv=argv, command="插件 MCP 服务", cwd=cwd, log_path=None, env=environment, max_log_bytes=0,
            store_root=process_session_store_root(owner.home_dir, owner.home_dir),
            access_scope=ProcessAccessScope(owner.owner_id, "", str(owner.home_dir)),
            execution_scope=ProcessExecutionScope(owner_home=str(owner.home_dir)),
            io_mode="stdio", stop_on_launcher_exit=True, activation=activation,
        ), startup_timeout_seconds=timeout)
        result = cls(hosted, activation)
        try:
            for name in ("stdout", "stderr"):
                stream = getattr(hosted.process, name)
                if stream is None:
                    raise ValueError("托管 MCP 输出管道缺失")
                setattr(hosted.process, name, io.TextIOWrapper(stream, encoding="utf-8", errors="replace"))
            return result
        except BaseException:
            result.close_unclaimed()
            raise

    # LLM: 与预留、host 创建和撤销冻结使用原锁；等待可取消，yield 内不得等协议响应或取得安装写锁。
    # 函数用途: 在发送前取得原资源临界区，连接关闭时允许退出排队。
    def transaction(self, wait_check):
        return ProcessSessionStore(self.hosted.store_root).transaction(wait_check=wait_check)

    # LLM: 调用方持原资源锁；先核对原激活再看进程记录——停用会先撤销激活再请求停止，所以因停用而关闭的调用报 PLUGIN_ACTIVATION_UNAVAILABLE（→TOOL_UNAVAILABLE，不建议重试），
    # 只有激活仍有效而进程缺失/已停时才报 MCP_CONNECTION_CLOSED；撤销后冻结会包含所有此前准入的实例。
    # 函数用途: 拒绝缺失、停止、换代或尚未发布的业务调用，不关闭别的调用共用的连接。
    def require(self, transaction: ProcessSessionTransaction, *, allow_preparing: bool) -> None:
        original = self.hosted.record
        current = transaction.load(str(original["session_id"]))
        if current is None:
            raise MCPError("插件原进程记录缺失", code="MCP_CONNECTION_CLOSED")
        merge_process_record(original, current)
        self.activation.require(allow_preparing=allow_preparing)
        if current["stop_requested"] or current["status"] != "running" or not current["handoff_confirmed"]:
            raise MCPError("插件原进程已停止或不可用", code="MCP_CONNECTION_CLOSED")

    # LLM: 返回原 ProcessSessionCleanup，异常继续携带原提交/redo/终止事实，不能压成裸 host 的树清理成功。
    # 函数用途: 按启动时固定的 host/child 身份收回本连接资源，未知结果保持未知。
    def terminate(self) -> ProcessSessionCleanup:
        return stop_process_session(ProcessSessionStore(self.hosted.store_root), self.hosted.record,
                                    host_process=self.hosted.process)

    # LLM: 仅在尚无 reader/writer 接手时调用，清理失败仍释放自己持有的管道；不能用于关闭活跃读线程的缓冲流。
    # 函数用途: 回收启动到 Transport 接管之间失败的精确资源和管道。
    def close_unclaimed(self) -> None:
        try:
            receipt = self.terminate()
            if not receipt.confirmed:
                raise ProcessSessionCleanupError(RuntimeError("unclaimed MCP cleanup unresolved"),
                    receipt.record, receipt.terminations, bool(receipt.record.get("stop_requested")))
        finally:
            for stream in (self.hosted.process.stdin, self.hosted.process.stdout, self.hosted.process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
