# LLM: 普通与可选模型请求使用同一个进程准入器；optional 不排队且保留一个普通名额，同步 hot_path/admission 测试。
# 模块用途: 把原并发上限接入实际模型调用；开关与容量仍归原环境配置，不另建决策并发池。
"""把 llm_scale 的全局并发闸接进真实 LLM 热路径。

架构勘查结论:llm_scale 的 admission/限流/公平栈功能齐全但没接线,真实文件网关路径的
`_invoke_backend_generate` 只有 gauge、无任何全局并发闸——子代理扇出(每层×8、总50)
会把在飞 LLM 调用推到远超 provider RPM/TPM → 429。本模块把 ConcurrencyLimiter(有界
信号量)接上,给"全局同时在飞的模型调用数"一个硬顶。

【默认关】env `LLM_MAX_INFLIGHT` 未设或 ≤0 → 返回空上下文=零行为变化(当前默认路径不变);
设了才封顶,槽满等 `LLM_ADMISSION_WAIT_SECONDS`(默认30s)拿不到抛 ConcurrencyTimeout,
由上层 provider_transient_auto_resume 当瞬时错误退避重试(而非闷等/崩)。命名沿用 llm_scale
既有 env(worker_handler 同名),将来接全量栈同一个旋钮。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext

from .concurrency import ConcurrencyLimiter, ConcurrencyTimeout

_LOCK = threading.Lock()
_LIMITER: ConcurrencyLimiter | None = None
_RESOLVED = False


# LLM: 环境解析保持原缺失/无效值回退语义，不从模型文本或请求正文读取容量。
# 函数用途: 读取原准入环境设置，未配置时保持普通调用默认行为。
def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


# LLM: 所有请求复用同一个惰性实例；此容量是单进程配置，不宣称跨机器限额。
# 函数用途: 首次调用时创建原并发限制器，未配置上限则不启用该限制。
def _limiter() -> ConcurrencyLimiter | None:
    global _LIMITER, _RESOLVED
    with _LOCK:
        if not _RESOLVED:
            cap = _env_int("LLM_MAX_INFLIGHT", 0)
            _LIMITER = ConcurrencyLimiter(cap) if cap > 0 else None
            _RESOLVED = True
        return _LIMITER


# LLM: optional 必须在实际 worker 内持有，不能随外层超时释放；只改领取策略，不改主模型等待或异常类型。
# 函数用途: 领取原全局模型名额，可选调用立即尝试并给普通调用留一个名额，避免短判断排队阻塞主流程。
@contextmanager
def global_llm_admission_slot(*, optional: bool = False) -> Iterator[None]:
    """全局在飞 LLM 并发槽;未配上限时是零成本 nullcontext(默认路径不变)。

    槽满等 LLM_ADMISSION_WAIT_SECONDS 拿不到 → 抛 ProviderTransientError(过载/限流类,
    tool 循环的 run_with_provider_transient_auto_resume 按退避重试=背压,而非闷等/崩;
    不用 ProviderTimeoutError——那是墙钟超时,设计成快速失败不重试)。"""
    limiter = _limiter()
    if limiter is None:
        with nullcontext():
            yield
        return
    try:
        timeout = 0.0 if optional else float(_env_int("LLM_ADMISSION_WAIT_SECONDS", 30))
        with limiter.slot(timeout=timeout, reserve=1 if optional else 0):
            yield
    except ConcurrencyTimeout as exc:
        from ..backends.errors import ProviderTransientError

        raise ProviderTransientError(f"全局 LLM 并发闸背压: {exc}") from exc


# LLM: 仅供没有在途请求的测试重置实例，生产配置切换不得借此丢失真实占用。
# 函数用途: 清理测试环境缓存，避免上一项测试的容量影响下一项。
def reset_hot_path_admission_for_test() -> None:
    global _LIMITER, _RESOLVED
    with _LOCK:
        _LIMITER = None
        _RESOLVED = False


__all__ = ["global_llm_admission_slot", "reset_hot_path_admission_for_test"]
