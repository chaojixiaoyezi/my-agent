# LLM: 默认唯一归 typed RuntimeGuardPolicy；软提示可覆盖，硬门只允许任务收紧，不能关闭或放大宿主限制。
# 模块用途: 为真实工具门解析冻结部署快照及原合法任务提示属性。
from __future__ import annotations

from ...settings.runtime_guard_config import RuntimeGuardPolicy, _effective_policy

# 旧公开符号只作代码默认的投影，不能再维护另一份数字。
DEFAULT_REPEAT_FAIL_THRESHOLD_COUNT = RuntimeGuardPolicy().repeat_fail_threshold
DEFAULT_READONLY_NO_PROGRESS_THRESHOLD_COUNT = RuntimeGuardPolicy().readonly_no_progress_threshold
DEFAULT_REPEATED_SUCCESS_HINT_THRESHOLD_COUNT = RuntimeGuardPolicy().repeated_success_hint_threshold
DEFAULT_REPEATED_FAILURE_HALT_THRESHOLD_COUNT = RuntimeGuardPolicy().repeated_failure_halt_threshold
DEFAULT_HARD_FAILURE_HALT_THRESHOLD_COUNT = RuntimeGuardPolicy().hard_failure_halt_threshold


# LLM: 只有真实 typed 冻结快照或正式宿主加载器能提供策略，错误不吞成默认。
# 函数用途: 取得当前工作片的守卫策略。
def _policy(params: object) -> RuntimeGuardPolicy:
    return _effective_policy(policy=getattr(params, "runtime_guard_policy", None))


# LLM: 整数不得用 bool 冒充；不校正字符串、负值或任意动作正文。
# 函数用途: 从原任务属性读取合法非负整数。
def _task_int(params: object, key: str) -> int | None:
    attrs = getattr(params, "task_attributes", None)
    value = attrs.get(key) if isinstance(attrs, dict) else None
    return value if type(value) is int and value >= 0 else None


# LLM: 此三项只产生策略/进展软提示，不授执行权限或关闭硬门；保留任务的显式0关闭提示。
# 函数用途: 读取可以按任务覆盖的软提示阈值。
def _soft_threshold(params: object, key: str) -> int:
    candidate = _task_int(params, key)
    return getattr(_policy(params), key) if candidate is None else candidate


# LLM: 成功重复只是软提醒，与只读或失败硬门无关。
# 函数用途: 读取成功重复提醒阈值。
def repeated_success_hint_threshold(params: object) -> int:
    return _soft_threshold(params, "repeated_success_hint_threshold")


# LLM: 失败策略提示允许任务调整，但不改变独立硬失败上限。
# 函数用途: 读取重复失败提示阈值。
def repeat_fail_threshold(params: object) -> int:
    return _soft_threshold(params, "repeat_fail_threshold")


# LLM: 只读无进展提示允许原任务覆盖，不放大任何写入权限。
# 函数用途: 读取只读无进展提示阈值。
def readonly_no_progress_threshold(params: object) -> int:
    return _soft_threshold(params, "readonly_no_progress_threshold")


# LLM: 任务只能启用硬保护，false/字符串/数字均不能撤销宿主true；来源优先级不是授权。
# 函数用途: 合并终端或硬失败保护开关。
def _hard_enabled(params: object, key: str) -> bool:
    attrs = getattr(params, "task_attributes", None)
    requested = isinstance(attrs, dict) and attrs.get(key) is True
    return getattr(_policy(params), key) or requested


# LLM: 宿主正阈值下任务0或更大值不能放宽；宿主0未设此独立保护时任务可主动收紧为正数。
# 函数用途: 取硬失败限制和合法任务收紧值的交集。
def _hard_threshold(params: object, key: str) -> int:
    limit, requested = getattr(_policy(params), key), _task_int(params, key)
    if requested is None or requested == 0:
        return limit
    return min(limit, requested) if limit > 0 else requested


# LLM: 不允许 task=false 解除宿主终端阻断。
# 函数用途: 读取终端硬保护开关。
def terminal_block_enabled(params: object) -> bool:
    return _hard_enabled(params, "terminal_block_enabled")


# LLM: 此值也用于同调用失败上限及子代理授权阻塞收口，不能当纯提示放大。
# 函数用途: 读取重复失败收口上限。
def repeated_failure_halt_threshold(params: object) -> int:
    return _hard_threshold(params, "repeated_failure_halt_threshold")


# LLM: 宿主启用后任务不能关闭此独立硬门。
# 函数用途: 读取硬失败门开关。
def hard_failure_halt_enabled(params: object) -> bool:
    return _hard_enabled(params, "hard_failure_halt_enabled")


# LLM: 数值越小越严格；显式任务0不能取消宿主上限。
# 函数用途: 读取硬失败门的收口阈值。
def hard_failure_halt_threshold(params: object) -> int:
    return _hard_threshold(params, "hard_failure_halt_threshold")
