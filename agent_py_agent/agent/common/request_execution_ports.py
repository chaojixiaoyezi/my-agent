# LLM: 只运输原平台短门与精确未结错误；当前canonical cell/interval共同失效，业务层不依赖Gateway，端口出现不授权限，同步平台invocation与copy/fork回归。
# 模块用途: 在真实请求期间给文件事务传入原执行门，默认单进程不增加任何读写。
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

from .executor_registration import managed_request_execution_required


# LLM: 原claim身份仅由宿主producer传入；不能从异常文本建立恢复或重跑许可，consumer仍按原完整tuple确认。
# 类用途: 保存短操作尚未进入但原执行权无法核准的精确请求事实。
class RequestExecutionOutcomeUncertainError(RuntimeError):
    error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"

    # LLM: 字段来自原claimed对象，不查询当前代次；phase仅是产品短操作名。
    # 函数用途: 保留这次未结运输身份，不执行任何业务副作用。
    def __init__(self, request, phase):
        self.request_id = request["id"]
        self.execution_attempt_id = request["execution_attempt_id"]
        self.lease_epoch = request["lease_epoch"]
        self.lease_owner = request["lease_owner"]
        self.operation_entered = False
        self.phase = phase
        super().__init__("原请求执行权未能核实，短操作未进入，持久执行责任待核")


# LLM: 没有任何原claim时只拒绝入口，不能编造request/lease来清账或补权限。
# 类用途: 表示受限执行进程丢失真实请求端口。
class RequestExecutionContextUnavailable(RuntimeError):
    error_code = "EXECUTION_CONTEXT_REQUIRED"


# LLM: canonical区间仅由scope安装，cell构造或浅复制不能发布；纯内存，不是P3/P5许可。
# 类用途: 保存当前原端口的唯一安装指针。
class _OriginalInterval:
    # LLM: 原安装入口捕获同一cell，不读取磁盘或构造claim。
    # 函数用途: 记录这一段同步区间的原对象。
    def __init__(self, cell):
        self.cell, self.origin = cell, self


# LLM: provider保存原平台source，current pointer/origin/PID/active/interval都必须精确；scope退出共同失效，最终许可仍由平台原门核。
# 类用途: 运输真实短事务回调，防止复制或迟到上下文借权。
class _OriginalPorts:
    # LLM: source只保存原平台对象引用，构造不查询运行库、进程或文件。
    # 函数用途: 捕获一次原请求平台的事务端口。
    def __init__(self, source):
        self.source = self._source_origin = source
        self.pid, self.active, self.origin = os.getpid(), True, self
        self.interval = self._interval_origin = None

    # LLM: 拒绝事实由捕获的原source产生，不从新claim或角色补权；平台自身再次检查原execution/lifetime。
    # 函数用途: 确认正在消费当前原安装端口。
    def require_original(self, phase):
        if _source_validator is None:
            raise _missing_context(phase)
        _source_validator(self._source_origin, phase)
        if (type(self) is not _OriginalPorts or self.origin is not self or _PORTS.get() is not self
                or self.source is not self._source_origin or not self.active or self.pid != os.getpid()
                or type(self.interval) is not _OriginalInterval or self.interval is not self._interval_origin
                or self.interval.origin is not self.interval or self.interval.cell is not self):
            raise self._source_origin.rejection(phase)


_PORTS = ContextVar("original_request_transaction_ports", default=None)
_context_rejection = None
_source_validator = None


# LLM: 真实组合根只安装原拒绝producer；None/cell异常绝不默认放行正C/W/独立host，也不签新许可。
# 函数用途: 在丢端口时保留宿主能核实的原请求欠账身份。
def bind_request_context_rejection(provider, *, source_validator):
    global _context_rejection, _source_validator
    _context_rejection, _source_validator = provider, source_validator


# LLM: 无原claim只报缺上下文；有原claimed来源时由宿主精确typed producer拒，不能从错误正文分类。
# 函数用途: 为缺失端口返回安全结构化拒绝。
def _missing_context(phase):
    if _context_rejection is not None:
        return _context_rejection(phase)
    return RequestExecutionContextUnavailable("original_request_transaction_ports_required")


# LLM: 只由实际平台invocation/BG/child组合根安装；None仅原Count0路径，finally使所有copy_context共用的cell失效。
# 函数用途: 在原请求有效期内运输短门，并准确恢复嵌套外层端口。
@contextmanager
def request_execution_ports_scope(source):
    if source is None:
        if managed_request_execution_required():
            raise _missing_context("transaction_scope")
        yield
        return
    if _source_validator is None:
        raise _missing_context("transaction_scope")
    _source_validator(source, "transaction_scope")
    cell = _OriginalPorts(source)
    cell.interval = cell._interval_origin = _OriginalInterval(cell)
    token = _PORTS.set(cell)
    try:
        cell.require_original("transaction_scope")
        yield
    finally:
        cell.active = False
        cell.interval.cell = None
        _PORTS.reset(token)


# LLM: 只读原canonical cell/source，先完整require；None仅原Count0/no source，实际typed拒绝原样传播，不安装、重签或授文件写权。
# 函数用途: 给平台领域适配器核对当前仍在同一个请求来源内，不暴露可重建的授权字段。
def current_request_execution_source(phase):
    cell = _PORTS.get()
    if cell is None:
        if managed_request_execution_required():
            raise _missing_context(phase)
        return None
    if type(cell) is not _OriginalPorts:
        raise _missing_context(phase)
    cell.require_original(phase)
    return cell.source


# LLM: phase是产品声明；先原平台T/P3/P5再业务锁，nested由同一个平台窗口核；Count0无端口仅内存早返。
# 函数用途: 在真实执行短门覆盖下完成文件事务，不持门等待网络或线程。
@contextmanager
def gateway_short_transaction(phase):
    cell = _PORTS.get()
    if cell is None:
        if managed_request_execution_required():
            raise _missing_context(phase)
        yield
        return
    if type(cell) is not _OriginalPorts:
        raise _missing_context(phase)
    cell.require_original(phase)
    with cell.source.short_scope(phase):
        cell.require_original(phase)
        yield


# LLM: 原inner写者已有业务锁，仅核同线程原持T窗口，绝不倒取T；缺端口的正MP拒绝，Count0不增加I/O。
# 函数用途: 在内部落盘前复核外层原短事务仍有效。
@contextmanager
def gateway_transaction_writer(phase):
    cell = _PORTS.get()
    if cell is None:
        if managed_request_execution_required():
            raise _missing_context(phase)
        yield
        return
    if type(cell) is not _OriginalPorts:
        raise _missing_context(phase)
    cell.require_original(phase)
    cell.source.require_writer(phase)
    yield


# LLM: 精确同一typed未结事件贯穿业务best-effort catch；不读取reason/classname或把普通错误当欠账。
# 函数用途: 保留尚未核清的原运输责任，让原请求出口处理。
def raise_if_transport_uncertain(error):
    if isinstance(error, RequestExecutionOutcomeUncertainError):
        raise error


# LLM: callback必须在原平台短门实际区间内执行；ASK复用原T，HCM消费原binding/C写者，Count0无cell直接原调用且零新增I/O。
# 函数用途: 把文件元数据的小段提交交给原执行源，不延长已返回的授权回调。
def gateway_short_operation(phase, operation):
    cell = _PORTS.get()
    if cell is None:
        if managed_request_execution_required():
            raise _missing_context(phase)
        return operation()
    if type(cell) is not _OriginalPorts:
        raise _missing_context(phase)
    cell.require_original(phase)
    return cell.source.short_operation(phase, operation)
