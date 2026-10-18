# LLM: 只运输组合根工厂和唯一工具执行器的实际request/claim原引用；无Gateway反向依赖、无磁盘/角色授权，终态副本共同失效。
# 模块用途: 让实际工具叶子延迟进入原同步短写门，长handler和模型等待不持有写门。
import os
from contextlib import contextmanager
from contextvars import ContextVar


# LLM: 错误表示原工具同步来源不可再消费，不自动重试或补当前claim。
# 类用途: 拒绝无领取、复制及已退出的工具全局写来源。
class ToolWriteContextUnavailable(RuntimeError):
    error_code = 'GLOBAL_WRITE_REJECTED'


# LLM: interval只在原contextmanager内安装，canonical cell指针不由cell构造器发布；finally共同失效，替身不能借原活区间。
# 类用途: 保存原安装区间的唯一运输对象。
class _OriginalInterval:
    # LLM: 仅原scope进入时调用，保存实际cell；不访问外部状态。
    # 函数用途: 发布当前原区间的canonical运输指针。
    def __init__(self, cell):
        self.cell, self.origin = cell, self


# LLM: 原cell只保存内存引用；PID/origin/active与当前ContextVar对象共同核，允许原handler线程显式copy_context但不延寿。
# 类用途: 保存原工厂或原领取的一段共同寿命。
class _OriginalCell:
    # LLM: 构造不读取运行库、不创建claim或线程；保存原对象印记和原不可重绑value引用。
    # 函数用途: 捕获一次当前进程的原引用。
    def __init__(self, value):
        self.value, self.pid, self.active, self.origin = value, os.getpid(), True, self
        self._value_origin = value
        self.interval = self._interval_origin = None

    # LLM: 替身/copy/fork及终态不能借尚活新来源；指针必须仍为当前安装的原cell。
    # 函数用途: 核实正在消费同一原同步区间。
    def require_original(self, current):
        if (self.origin is not self or current is not self or self.value is not self._value_origin
                or not self.active or self.pid != os.getpid()
                or type(self.interval) is not _OriginalInterval or self.interval is not self._interval_origin
                or self.interval.origin is not self.interval or self.interval.cell is not self):
            raise ToolWriteContextUnavailable('original_tool_write_context_unavailable')


_FACTORY = ContextVar('original_tool_write_factory', default=None)
_CLAIM = ContextVar('original_tool_write_claim', default=None)


# LLM: 只由原C组合根安装受信工厂；None是Count0纯内存无新增执行者查询，退出失效所有上下文副本。
# 函数用途: 在实际协调者请求范围内运输工具最后写门的构造器。
@contextmanager
def tool_write_factory_scope(factory):
    if factory is None:
        yield
        return
    if not callable(factory):
        raise ToolWriteContextUnavailable('original_tool_write_factory_required')
    cell = _OriginalCell(factory)
    cell.interval = cell._interval_origin = _OriginalInterval(cell)
    token = _FACTORY.set(cell)
    try:
        yield
    finally:
        cell.active = False
        cell.interval.cell = None
        _FACTORY.reset(token)


# LLM: 唯一工具执行器在真实execute领取后、handler前调用；默认无工厂不查询PID/线程，不触发新账本。
# 函数用途: 把同一次实际request和claim带到其原handler，返回后立即撤销。
@contextmanager
def tool_write_claim_scope(request, claim):
    factory = _FACTORY.get()
    if factory is None:
        yield
        return
    if type(factory) is not _OriginalCell:
        raise ToolWriteContextUnavailable('original_tool_write_factory_required')
    factory.require_original(_FACTORY.get())
    cell = _OriginalCell((request, claim, factory))
    cell.interval = cell._interval_origin = _OriginalInterval(cell)
    token = _CLAIM.set(cell)
    try:
        yield
    finally:
        cell.active = False
        cell.interval.cell = None
        _CLAIM.reset(token)


# LLM: 默认只读ContextVar无I/O；正C工厂存在时无真实claim/退出/替身一律拒；最终权限由工厂返回的原source每次重核。
# 函数用途: 在真正叶子当前线程构造原短写来源，避免长handler提前持门。
def original_tool_write_commit():
    factory = _FACTORY.get()
    if factory is None:
        return None
    if type(factory) is not _OriginalCell:
        raise ToolWriteContextUnavailable('original_tool_write_factory_required')
    factory.require_original(_FACTORY.get())
    cell = _CLAIM.get()
    if type(cell) is not _OriginalCell:
        raise ToolWriteContextUnavailable('original_tool_write_claim_required')

    # LLM: 此guard带原共享cell，不从当前值替换来源；原最后门再次调用它，迟到handler/copy_context不能延寿。
    # 函数用途: 在争门后核实仍处于原工厂及原领取同步区间。
    def guard():
        factory.require_original(_FACTORY.get())
        cell.require_original(_CLAIM.get())
        if cell.value[2] is not factory:
            raise ToolWriteContextUnavailable('original_tool_write_factory_changed')

    guard()
    request, claim, _original_factory = cell.value
    return factory.value(request, claim, guard)
