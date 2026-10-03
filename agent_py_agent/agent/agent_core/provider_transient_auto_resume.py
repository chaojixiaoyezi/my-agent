# LLM: 模型采样退避只放行 typed 瞬时错或无类型异常的分类结果；配置/请求拒绝不能被文本放大恢复范围。
# 模块用途: 在可恢复网络故障中等待并重试当前模型调用，保留中断和既有次数预算，不重放工具副作用。
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from ..backends import (
    is_provider_recoverable_error,
    is_provider_stream_timeout_error,
    is_provider_transient_error,
)
from ..backends.errors import ProviderConfigurationError
from ..concurrency.interrupt import is_interrupted, wait_interruptibly
from ..concurrency.retry import apply_retry_jitter
from ..settings.runtime_guard_config import RuntimeGuardPolicy, runtime_guard_data

_T = TypeVar("_T")

DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS = (10.0, 25.0, 45.0, 100.0, 180.0)
# 一整个模型调用的自动重试总时长上限；0 表示不限（只受上面的次数阶梯约束）。
DEFAULT_PROVIDER_TRANSIENT_TOTAL_BUDGET_SECONDS = 1800.0
# 到总时长上限时的结构化错误码；上层按码识别，不读报错文字。
PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED = "PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED"


@dataclass(frozen=True)
class _RetryNotice:
    attempt: int
    total: int
    delay: float
    error: BaseException
    # 到总时长上限的收口提示：不再有下一次等待，文案与普通重试进度不同。
    final: bool = False


def provider_transient_retry_delays(policy: RuntimeGuardPolicy | None = None) -> tuple[float, ...]:
    value = runtime_guard_data(policy=policy).get(
        "provider_transient_auto_resume_delays_seconds",
        DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS,
    )
    if not isinstance(value, list | tuple):
        value = DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS
    return tuple(value for value in _parsed_delays(value) if value > 0)


def _parsed_delays(value: list | tuple) -> tuple[float, ...]:
    parsed: list[float] = []
    for item in value:
        try:
            parsed.append(float(item))
        except (TypeError, ValueError):
            continue
    return tuple(parsed)


# LLM: 只读运行护栏配置里的总时长上限；坏值、NaN、负数都回落默认 1800 秒，只有显式 0 表示不限
#   （负数静默关掉护栏比回落默认更危险，注释与行为按此对齐）。缺键也回落默认。
#   计时只看结构化事实（时刻与尝试次数），不读任何报错文字。
# 函数用途: 返回本次逻辑模型调用的自动重试总时长上限（秒），0 表示不限。
def provider_transient_total_budget_seconds(policy: RuntimeGuardPolicy | None = None) -> float:
    value = runtime_guard_data(policy=policy).get(
        "provider_transient_auto_resume_total_budget_seconds",
        DEFAULT_PROVIDER_TRANSIENT_TOTAL_BUDGET_SECONDS,
    )
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PROVIDER_TRANSIENT_TOTAL_BUDGET_SECONDS
    if not math.isfinite(parsed) or parsed < 0:  # NaN/inf/负数：按坏值回落默认，防止静默关掉护栏
        return DEFAULT_PROVIDER_TRANSIENT_TOTAL_BUDGET_SECONDS
    return parsed


def run_with_provider_transient_auto_resume(
    operation: Callable[[], _T],
    *,
    on_chunk: Callable[[str], object] | None = None,
    policy: RuntimeGuardPolicy | None = None,
    retry_guard: Callable[[], bool] | None = None,
) -> _T:
    delays = provider_transient_retry_delays(policy)
    # 预算从第一次尝试开始计时：尝试耗时、退避等待和它的抖动都算在里面。
    budget = provider_transient_total_budget_seconds(policy)
    started_at = time.monotonic()
    for attempt, delay in enumerate(delays, start=1):
        _raise_if_interrupted()
        try:
            return operation()
        except InterruptedError:
            raise  # 用户/任务中断绝不重试: 中断标记必须被消费, 不能进入退避重连
        except Exception as exc:
            _raise_unless_provider_transient(exc)
            if callable(retry_guard) and not retry_guard():
                raise
            # 配置阶梯+随机抖动(批3):多实例同撞限流时错峰重试,防共振雪崩。
            wait = apply_retry_jitter(delay)
            # 再等这一次就会超预算时不再重试：先发收口提示（与重试进度同一 on_chunk 通道），
            # 给原异常补结构化事实后原样重新抛出——不换类型，上层收尾与跑满阶梯完全同口径。
            if _budget_exhausted(budget, started_at, wait):
                _emit_retry_notice(on_chunk, _RetryNotice(attempt, len(delays), wait, exc, final=True))
                _mark_budget_exceeded(exc, budget)
                raise
            _wait_before_retry(on_chunk, _RetryNotice(attempt, len(delays), wait, exc))
            _raise_if_interrupted()
    _raise_if_interrupted()
    if callable(retry_guard) and not retry_guard():
        raise RuntimeError("provider retry blocked by durable request state")
    return operation()


# LLM: 只有带上限（>0）才判断；判断用“已用 + 下一次等待”，即再试一次必定越界就停。
#   不预判下一次尝试本身的耗时（无法预知），到那时由下一个循环的等待判断收口。
# 函数用途: 判断再一次退避重试是否会超过总时长上限。
def _budget_exhausted(budget: float, started_at: float, wait: float) -> bool:
    if budget <= 0:
        return False
    return (time.monotonic() - started_at) + wait > budget


# LLM: 到总时长上限不换异常类型：给触发本次判断的异常就地补两个结构化事实
#   （error_code / retry_budget_seconds），随后由调用方裸 raise 原样重新抛出。后台 claim 结算、
#   子代理失败类型、Goal 的 usage_limited 判定都按异常类型分路，保持类型才能让「到上限」与
#   「跑满阶梯」对上层完全同口径；能进入本循环的异常不带 error_code（带码的 recoverable
#   直接上抛），不会覆盖已有的码。
# 函数用途: 给触发上限的异常补上结构化事实（就地修改，不新建异常、不改消息）。
def _mark_budget_exceeded(exc: Exception, budget: float) -> None:
    exc.error_code = PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED
    exc.retry_budget_seconds = budget


# LLM: 重试/退避循环必须检查任务中断(2026-08-14 真机, gateway 后台接管轮挂起):
#   watchdog interrupt_by_name 只会标记已登记的线程, 而 guard worker 由
#   _interrupt_generation_worker 显式 set_interrupt——本循环每轮开跑前/退避后
#   都检查 is_interrupted(), 让中断标记被消费而不是在断流→退避→重连里空转。
def _raise_if_interrupted() -> None:
    if is_interrupted():
        raise InterruptedError("provider 重试循环已收到任务中断, 停止重试")


# LLM: typed 配置/请求拒绝先上抛；typed transient 之外,经分类器
#   (contracts/provider_error_classifier,长期助手 蓝本)判为 rate_limit/
#   overloaded/server_error/timeout 的裸异常同样进入重试;auth/billing/format/
#   context_overflow/unknown 照旧上抛(context_overflow 由上层 ptl_retry 链
#   接手压缩,unknown 保守快速浮出)。模型全程无感。
# 函数用途: 这个错值不值得原地重试?值得就放行去等待,不值得立刻抛给上层。
def _raise_unless_provider_transient(exc: Exception) -> None:
    if isinstance(exc, ProviderConfigurationError):
        raise exc
    # 会话运行时 对 dropped/idle response stream 重发同一 sampling request，并在 UI
    # 展示 reconnect 进度。这里仅放行结构化 first_event/stream_idle；工具尚未
    # 执行，重放的是模型采样而不是副作用。wall_clock/provider_declared/legacy
    # 仍由生成层的一次有界重试收口，避免慢模型永久空转。
    if is_provider_transient_error(exc) or is_provider_stream_timeout_error(exc):
        return
    # typed provider 错误语义明确,只信上面的结构化判定,不再用文本
    # 分类器二次放大重试面(CI 回归实锤:ProviderTimeoutError 的 str 含 "timeout",
    # 会把 wall_clock/provider_declared 误判 TIMEOUT/retryable 进入重试循环)。
    # 文本分类器只兜"没有 typed 形态的裸异常"(如裸 RuntimeError("429"))。
    if is_provider_recoverable_error(exc):
        raise exc
    from ..contracts.provider_error_classifier import classify_provider_error

    if classify_provider_error(exc).retryable:
        return
    raise exc


def _wait_before_retry(on_chunk: Callable[[str], object] | None, notice: _RetryNotice) -> None:
    _emit_retry_notice(on_chunk, notice)
    wait_interruptibly(notice.delay)


# LLM: 支持 typed retry sink 时只发送结构化序号/等待值；旧 callback 继续接收兼容文本且不参与重试裁决。
#   收口提示（final）也先走 typed sink，并加 final/error_code 结构化字段，富客户端据此显示
#   「已停止重试」；不认识的旧 sink 会因未知参数抛 TypeError、或抛异常/返回 False，
#   本模块吞掉后照旧退回文本回调，绝不因显示层故障改变重试裁决。
# 函数用途: 在模型回合级退避开始前把重连进度交给当前客户端显示；到总时长上限时改发收口提示。
def _emit_retry_notice(on_chunk: Callable[[str], object] | None, notice: _RetryNotice) -> None:
    if not callable(on_chunk):
        return
    if _publish_typed_retry_notice(on_chunk, notice):
        return
    if notice.final:
        on_chunk(
            "\n"
            f"[provider_transient_auto_resume attempt={notice.attempt}/{notice.total}; budget_exhausted]\n"
            f"模型接口持续不可用，已到自动重试的总时长上限，本轮不再重试。\n"
            f"error={notice.error}\n"
        )
        return
    delay = _format_delay(notice.delay)
    on_chunk(
        "\n"
        f"[provider_transient_auto_resume attempt={notice.attempt}/{notice.total}; wait_seconds={delay}]\n"
        f"模型接口临时不可用或被限流，等待 {delay} 秒后自动重试当前模型回合。\n"
        f"error={notice.error}\n"
    )


# LLM: typed sink 是可选显示能力，异常或明确拒绝时退回既有 callback；绝不能让 UI 通知破坏真实重试。
#   收口事件（final）额外携带 final=True 与 error_code 两个结构化字段；普通重试一个字段都不加，
#   保持旧客户端与既有精确断言兼容。判断只看结构化字段，不解析文案。
#   协议约定：实现方必须显式声明自己支持的 params 参数（新增字段都走 params），不得用 **kwargs
#   静默收下却不处理——那样收口提示既不显示也不回退，用户无感；回归守卫见 test_provider_retry_final_sink.py。
# 函数用途: 尝试向富客户端发布一次模型回合级重连或收口事件。
def _publish_typed_retry_notice(
    on_chunk: Callable[[str], object],
    notice: _RetryNotice,
) -> bool:
    sink = getattr(on_chunk, "write_provider_retry", None)
    if not callable(sink):
        return False
    payload: dict[str, object] = {
        "scope": "model_turn",
        "attempt": notice.attempt,
        "total": notice.total,
        "delay_seconds": notice.delay,
        "error_type": type(notice.error).__name__,
    }
    if notice.final:
        # 收口事实打包成一个结构化参数（final + error_code），显示层只读字段、不解析文案。
        payload["params"] = {
            "final": True,
            "error_code": PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED,
        }
    try:
        return bool(sink(**payload))
    except Exception:
        return False


def _format_delay(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".rstrip("0").rstrip(".")


__all__ = [
    "DEFAULT_PROVIDER_TRANSIENT_RETRY_DELAYS_SECONDS",
    "DEFAULT_PROVIDER_TRANSIENT_TOTAL_BUDGET_SECONDS",
    "PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED",
    "provider_transient_retry_delays",
    "provider_transient_total_budget_seconds",
    "run_with_provider_transient_auto_resume",
]
