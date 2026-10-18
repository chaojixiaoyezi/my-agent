# LLM: 原HTTP设施出生和实际finish_request区间各守原对象；通用upper/lower只读观察与领域wrapper共用同对象校验，不授业务/owner/P3权，Count0不构造新来源。
# 模块用途: 核协调者HTTP设施与本次真实请求的同源生命周期，阻止晚回调借旧handler写元数据。
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from ..common.global_write_guard import require_global_writer

_CURRENT_HTTP_WORK = ContextVar('gateway_current_http_work', default=None)


# LLM: Count0只读既有内存事实；已持C或复制晚C缺Runtime拒，upper的paths/control必须准确同Root，lower仍核原group，原lease未撤且未停出生；不授handler权。
# 函数用途: 拒绝受限执行器和不属于原C的HTTP设施启动。
def require_http_transport(runtime, *, control=None, paths=None):
    from .multiprocess_coordinator import coordinator_executor_required
    from .multiprocess_coordinator_execution import current_coordinator_execution
    from .multiprocess_drain_runtime import GatewayDrainRuntime

    require_global_writer('gateway_http_transport')
    if runtime is None:
        if control is not None or coordinator_executor_required() or current_coordinator_execution() is not None:
            raise ValueError('http_original_runtime_required')
        return
    if type(runtime) is not GatewayDrainRuntime:
        raise ValueError('http_original_runtime_required')
    runtime.require_original()
    if (runtime._closing or control is not None and control is not runtime.control
            or paths is not None and (paths is not runtime.control.paths or control is not runtime.control)):
        raise ValueError('http_original_control_required')


# LLM: lower ctor的callback只观察，不成为authority；两个bound methods须准确同原已登记control group，默认None组不能带观察者，W/host先由公共原拒事实挡。
# 函数用途: 在实际池构造之前核对出生观察者来自同一原资源。
def require_http_worker_source(runtime, group, initializer, failure):
    from .multiprocess_drain_resources import DrainResource

    require_http_transport(runtime)
    if runtime is None:
        if group is not None or initializer is not None or failure is not None:
            raise ValueError('http_original_worker_observer_required')
        return
    if (type(group) is not DrainResource or runtime._resources.get('http/requests') is not group
            or group.kind != 'control' or group.resource_ref != 'http/requests'
            or getattr(initializer, '__self__', None) is not group
            or getattr(initializer, '__func__', None) is not DrainResource.remember_current_thread
            or getattr(failure, '__self__', None) is not group
            or getattr(failure, '__func__', None) is not DrainResource.retain_fault
            or group._origin is not group or group._creator_pid != os.getpid()
            or group.owner is not None or group._owner_origin is not None):
        raise ValueError('http_original_worker_observer_required')


# LLM: 仅socket/pool/listener设施出生的同步短门，原controller/lease/root再次末核；不等待业务、模型、join或取消，Count0无新锁/文件/authority。
# 函数用途: 使本C撤权与原HTTP设施出生互斥，出生对象仍由原Runtime保留。
@contextmanager
def original_http_birth(runtime, *, control=None, paths=None):
    require_http_transport(runtime, control=control, paths=paths)
    if runtime is None:
        yield
        return
    with runtime.controller._mutex, runtime._lease_origin._lifecycle_gate, runtime._gate:
        require_http_transport(runtime, control=control, paths=paths)
        yield


# LLM: 原pool只在finish_request同步区间构造一次；字段不持久化、不授权限，origin/当前pointer/寿命共同拒绝复制和退出复用。
# 类用途: 保存这一次实际HTTP工作片的原设施引用，供固定元数据源末写时只读核对。
@dataclass(frozen=True, eq=False, repr=False)
class _HTTPRequestWork:
    server: object
    runtime: object
    control: object
    paths: object
    executor: object
    request: object
    thread: object
    controller: object
    lease: object
    creator_pid: int
    _origin: object = field(init=False, repr=False)
    _active: bool = field(default=True, init=False, repr=False)
    _handler: object = field(default=None, init=False, repr=False)

    # LLM: 不接受外部origin；原producer构造时锚定自身，dataclass浅复制仍保旧origin因此不能续权。
    # 函数用途: 记录原工作对象身份，不访问磁盘或启动资源。
    def __post_init__(self):
        object.__setattr__(self, '_origin', self)


# LLM: 这里只读当前pointer，不验证写权或创建来源；需要提交的调用方必须capture并每次require同一原work。
# 函数用途: 观察当前是否处在原HTTP工作区间，用于来源接线与测试。
def current_http_request_work():
    return _CURRENT_HTTP_WORK.get()


# LLM: 只消费原server ctor保存的同一executor/runtime与Root登记线程，born表原实现只追加，用exact get不等group锁；不等待线程或创建owner。
# 函数用途: 核当前池工作仍属于原设施与原C，缺失或复制均明确拒绝。
def require_http_request_work(work, *, handler=None, server=None):
    if (type(work) is not _HTTPRequestWork or work._origin is not work or not work._active
            or _CURRENT_HTTP_WORK.get() is not work or work.creator_pid != os.getpid()
            or threading.current_thread() is not work.thread
            or server is not None and server is not work.server):
        raise ValueError('http_original_work_required')
    selected = work.server
    if (getattr(selected, '_request_executor', None) is not work.executor
            or getattr(selected, '_request_executor_origin', None) is not work.executor
            or getattr(selected, '_request_drain_runtime', None) is not work.runtime
            or getattr(selected, '_request_drain_runtime_origin', None) is not work.runtime
            or work.runtime.controller is not work.controller
            or work.runtime._lease_origin is not work.lease
            or work.runtime.control is not work.control or work.control.paths is not work.paths):
        raise ValueError('http_original_work_required')
    require_http_transport(work.runtime, control=work.control, paths=work.paths)
    group = work.runtime._resources.get('http/requests')
    if group is None:
        raise ValueError('http_original_work_required')
    group._require_original()
    if group.owner is not work.executor or group._born_threads.get(work.thread) is not work.thread:
        raise ValueError('http_original_work_required')
    if handler is not None and (work._handler is not handler
            or getattr(handler, 'server', None) is not selected
            or getattr(handler, 'request', None) is not work.request
            or getattr(handler, 'connection', None) is not work.request):
        raise ValueError('http_original_handler_required')
    return work


# LLM: 首次capture只能来自当前finish_request的同server/原socket handler；只绑定一次真实对象，不能用新壳或header重签。
# 函数用途: 给本次认证handler捕获原工作寿命，后续短写仍须require原对象。
def capture_http_request_work(handler):
    work = current_http_request_work()
    require_http_request_work(work, server=getattr(handler, 'server', None))
    if (getattr(handler, 'server', None) is not work.server
            or getattr(handler, 'request', None) is not work.request
            or getattr(handler, 'connection', None) is not work.request):
        raise ValueError('http_original_handler_required')
    if work._handler is None:
        object.__setattr__(work, '_handler', handler)
    return require_http_request_work(work, handler=handler)


# LLM: 仅区分原upper设施引用变化与底层work不可用，保留领域wrapper原错误映射；不是writer或恢复许可。
# 类用途: 表示当前handler虽然仍属原工作片，但上层HTTP服务已经换成另一组设施。
class HTTPHandlerWorkChanged(ValueError):
    pass


# LLM: 只复用原work来源核验再比较upper/lower同对象；没有文件写、Agent创建、领域授权或fresh capture。
# 函数用途: 在只读投影前确认服务、控制器和路径仍属于最初的同一次HTTP处理。
def require_http_handler_work(work, handler, server):
    require_http_request_work(work, handler=handler)
    if (server.server is not work.server or server.drain_runtime is not work.runtime
            or server.control_plane is not work.control or server.paths is not work.paths
            or server.control_plane.controller() is not work.controller):
        raise HTTPHandlerWorkChanged('http_original_upper_work_changed')
    return work


# LLM: 只在真实finish_request区间捕获一次，随后必须require原对象；只读生命周期观察不授Root写权或owner权限。
# 函数用途: 为通用控制回执读取固定当前handler的原设施，不创建冷Agent或后台工作。
def capture_http_handler_work(handler, server):
    work = capture_http_request_work(handler)
    return require_http_handler_work(work, handler, server)


# LLM: 仅原bounded worker在finish_request前生产/退出；entry核原pool已登记current Thread，不持Root门跨handler，不签工具或owner权限。
# 函数用途: 令HTTP固定元数据提交只能发生在这一次实际工作片内，finally使所有复制Context的晚引用失效。
@contextmanager
def http_request_work_scope(server, request):
    runtime = getattr(server, '_request_drain_runtime', None)
    if runtime is None or runtime is not getattr(server, '_request_drain_runtime_origin', None):
        raise ValueError('http_original_work_required')
    if current_http_request_work() is not None:
        raise ValueError('http_original_work_nested')
    require_http_transport(runtime, control=runtime.control, paths=runtime.control.paths)
    work = _HTTPRequestWork(server, runtime, runtime.control, runtime.control.paths,
        server._request_executor, request, threading.current_thread(), runtime.controller,
        runtime._lease_origin, os.getpid())
    token = _CURRENT_HTTP_WORK.set(work)
    try:
        require_http_request_work(work, server=server)
        yield work
    finally:
        object.__setattr__(work, '_active', False)
        _CURRENT_HTTP_WORK.reset(token)
