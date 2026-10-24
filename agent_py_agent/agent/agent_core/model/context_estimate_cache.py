# LLM: 只缓存请求局部纯数字计量；不存授权/压缩裁决，不进入归档state。
# 模块用途: 保存独立于持久状态的上下文估算memo。
from concurrent.futures import Future
from threading import Lock, get_ident

_INITIALIZE_LOCK = Lock()


# LLM: 最新纯数值结果和同键Future受锁保护；计算/等待/通知均在锁外，失败不缓存。
# 类用途: 请求局部single-flight计量holder，不保留历史正文。
class ContextEstimateMemo:
    # LLM: 构造无线程副作用，所有同步对象仅驻留本请求内存。
    # 函数用途: 初始化最近结果与在途计算。
    def __init__(self):
        self._lock = Lock()
        self._last = None
        self._inflight = {}

    # LLM: 同键只有producer计算，等待者异常原样传播；None键保守不缓存，不锁住昂贵工作。
    # 函数用途: 共享一次纯计量并为每个调用返回独立components副本。
    def evaluate(self, key, compute):
        if key is None:
            return _detached(_freeze(compute()))
        with self._lock:
            if self._last is not None and self._last[0] == key:
                return _detached(self._last[1])
            flight = self._inflight.get(key)
            producer = flight is None
            if producer:
                flight = (Future(), get_ident())
                self._inflight[key] = flight
        future, owner = flight
        if not producer:
            if owner == get_ident():
                raise RuntimeError("上下文计量不能同键自等待")
            return _detached(future.result())
        return self._produce(key, future, compute)

    # LLM: 捕获BaseException避免永久挂起waiter；Future回调在锁外，发布的key只用计算前捕获值。
    # 函数用途: 完成producer并在通知后清理inflight。
    def _produce(self, key, future, compute):
        try:
            result = _freeze(compute())
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            with self._lock:
                self._last = key, result
            future.set_result(result)
            return _detached(result)
        finally:
            with self._lock:
                self._inflight.pop(key, None)


# LLM: 浅拷贝的冻结表示不持调用方字典，返回后修改不会破坏缓存。
# 函数用途: 冻结协议、总数和分项计量。
def _freeze(result):
    protocol, current, components, fingerprint = result
    return protocol, current, tuple(components.items()), fingerprint


# LLM: producer、waiter与热命中都走同一副本路径。
# 函数用途: 恢复组件字典供当前调用方使用。
def _detached(result):
    protocol, current, components, fingerprint = result
    return protocol, current, dict(components), fingerprint


# LLM: 真实参数用default_factory；兼容旧替身时仅初始化互斥，不把锁塞入state。
# 函数用途: 取得请求私有holder；不支持赋值的替身保守不缓存。
def estimate_memo_for(params):
    if params is None:
        return None
    with _INITIALIZE_LOCK:
        memo = getattr(params, "_context_estimate_memo", None)
        if memo is None:
            memo = ContextEstimateMemo()
            try:
                params._context_estimate_memo = memo
            except (AttributeError, TypeError):
                return None
        return memo
