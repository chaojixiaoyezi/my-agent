# LLM: 请求私有memo保存最新纯计量/投影和内联预留；不存授权或压缩裁决，锁与正文不进入归档state。
# 模块用途: 为冻结执行参数提供可变的瞬时上下文缓存和原子数字预留。
from concurrent.futures import Future
from threading import Lock, get_ident

_INITIALIZE_LOCK = Lock()


# LLM: 最新纯数值结果和同键Future受锁保护；计算/等待/通知均在锁外，失败不缓存。
# 类用途: 保存单项请求投影、single-flight计量和同批内联数字预留，不持久化。
class ContextEstimateMemo:
    # LLM: 构造无线程副作用，所有同步对象仅驻留本请求内存。
    # 函数用途: 初始化最近结果与在途计算。
    def __init__(self):
        self._lock = Lock()
        self._last = None
        self._inflight = {}
        self._prompt_projection = None
        self._inline_batch = None
        self._inline_pending = {}
        self._prompt_timings = None

    # LLM: 当前线程才可借其准备时长，返回独立数字字典，不从其它worker继承秒表。
    # 函数用途: 取得最近实际prompt构建的分阶段耗时。
    def prompt_timings(self):
        with self._lock:
            value = self._prompt_timings
            return dict(value[1]) if value is not None and value[0] == get_ident() else None

    # LLM: 只发表数字时长和单调起点，不写归档，不改变请求或权限状态。
    # 函数用途: 为本线程记录一次真实prompt构建计时。
    def remember_prompt_timings(self, timings):
        with self._lock:
            self._prompt_timings = get_ident(), dict(timings)

    # LLM: 锁只保护单项引用交换，内容指纹验证在锁外；缓存不是授权或IR权威。
    # 函数用途: 读取最近纯prompt投影，交给调用方重新核对内容键。
    def prompt_projection(self):
        with self._lock:
            return self._prompt_projection

    # LLM: 只保留最新投影；共享holder使frozen参数及同轮replace都能复用，不改归档字典。
    # 函数用途: 原子发表一次通过内容核验的纯投影。
    def remember_prompt_projection(self, projection):
        with self._lock:
            self._prompt_projection = projection

    # LLM: 批次取原模型turn身份，预算变动不丢未提交预留；只按真实IR中已提交的call_id结算，同调用重试不重复扣。
    # 函数用途: 原子结算同批内联占用并预留本条结果，新批次显式重置。
    def reserve_inline_headroom(self, current, ceiling, tokens, *, batch_key=None, call_id=None, committed_ids=()):
        with self._lock:
            if self._inline_batch != batch_key:
                self._inline_batch, self._inline_pending = batch_key, {}
            for committed in committed_ids:
                self._inline_pending.pop(committed, None)
            pending = sum(value for key, value in self._inline_pending.items() if key != call_id)
            remaining = max(0, ceiling - current - pending)
            if tokens < remaining:
                self._inline_pending[call_id if call_id is not None else object()] = tokens
            return remaining

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
