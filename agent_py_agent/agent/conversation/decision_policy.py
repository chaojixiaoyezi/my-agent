# LLM: 这里只保存有界的冷却摘要（含连续失败次数）和在途取消索引，不是 worker/资源池；实际退出仍由 bounded_call 跟踪。
# 模块用途: 隔离决策连接故障并按连续失败逐步加长冷却，同时将同进程设置撤销转交给原精确中断句柄。
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from ..backends.errors import (
    ProviderConfigurationError,
    ProviderQuotaExhaustedError,
    ProviderUsageLimitError,
)
from ..concurrency.interrupt import InterruptHandle
from ..settings.decision_settings_defaults import decision_defaults
from ..settings.model_profiles import model_profiles_path

_LOCK = threading.Lock()
_SALT = secrets.token_bytes(32)
_MAX_FAILURES = 512
_MAX_ACTIVE = 256
# 瞬时失败与超时的首次冷却；同一连接冷却过后再次失败时翻倍，封顶与额度冷却相同。
_BASE_COOLDOWN_SECONDS = 30.0
_MAX_COOLDOWN_SECONDS = 300.0
# 值为 (状态, 设置修订, 冷却截止或 None, 连续失败次数)；冷却过期后保留次数，成功或显式重试才清除。
_FAILURES: OrderedDict[tuple[str, str, str], tuple[str, str, float | None, int]] = OrderedDict()
_ACTIVE: dict[str, ActiveDecision] = {}


# LLM: 只读可信 owner 的原路径并哈希，不接受请求指定 owner，不公开路径或凭据。
# 函数用途: 生成同进程设置通知和冷却使用的稳定归属摘要。
def decision_owner_ref(context: object) -> str:
    return hashlib.sha256(str(model_profiles_path(context.home_paths).resolve()).encode()).hexdigest()


# LLM: 进程随机盐的 HMAC 只供版本对比；缓存不保存 api_key、自定义头或完整连接。
# 函数用途: 对原连接快照生成不可直接反查凭据的进程内版本摘要。
def connection_revision(config: dict) -> str:
    raw = json.dumps(config, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return hmac.new(_SALT, raw, hashlib.sha256).hexdigest()


# LLM: 在途引用只活到 caller 收口，不能据此释放存活 worker 的槽位；上下文不复制到持久文件。
# 类用途: 记录应通知的准确 owner/thread/point 和一次性取消句柄。
@dataclass
class ActiveDecision:
    owner_ref: str
    thread_id: str
    point: str
    handle: InterruptHandle
    context: object = field(repr=False)
    settings: dict = field(repr=False)
    settings_cancelled: bool = False


# LLM: 索引满只拒绝本次可选增强，不等待或建立另一池；caller 退出必须注销，不替 bounded_call 回收资源。
# 函数用途: 发布一个准备发送的取消目标，覆盖关闭发生在 worker 注册前的窗口。
def register_active(key: str, active: ActiveDecision) -> bool:
    with _LOCK:
        if key in _ACTIVE or len(_ACTIVE) >= _MAX_ACTIVE:
            return False
        _ACTIVE[key] = active
        return True


# LLM: 只移除同一个在途通知对象；实际 worker 可能仍存活，不触碰其原保留表。
# 函数用途: 在 caller 的 finally 中清理临时设置通知索引。
def unregister_active(key: str, active: ActiveDecision) -> None:
    with _LOCK:
        if _ACTIVE.get(key) is active:
            del _ACTIVE[key]


# LLM: 显式 retry 清除一个准确连接的冷却及连续失败次数；配置错误在配置修订改变前不热循环重发。
# 冷却过期只放行本次尝试，不删记录：下一次失败要据此加长冷却，成功才由 record_success 清除。
# 函数用途: 返回当前连接的阻塞原因和剩余冷却秒数，未知连接不受其他 owner 故障影响。
def cooldown_state(key: tuple[str, str, str], revision: str, *, retry: bool = False) -> tuple[str, float]:
    with _LOCK:
        item = _FAILURES.get(key)
        if item is None:
            return "", 0.0
        status, old_revision, until, _streak = item
        if retry or until is None and revision != old_revision:
            _FAILURES.pop(key, None)
            return "", 0.0
        remaining = max(0.0, until - time.monotonic()) if until is not None else 0.0
        if until is not None and remaining <= 0:
            return "", 0.0
        _FAILURES.move_to_end(key)
        return status, remaining


# LLM: 只按异常类型分类，不能解析中文/服务商正文；不改变主 LLM 配置或其重试策略。
# 冷却期内返回的并发失败属于同一次故障，不加长阶梯；只有冷却过期后的重试再失败才翻倍。
# 函数用途: 记录一个连接的配置等待或有界冷却：额度 300 秒，其他故障 30 秒起、连续失败翻倍、封顶 300 秒。
def record_failure(key: tuple[str, str, str], revision: str, error: Exception) -> str:
    if isinstance(error, ProviderConfigurationError):
        with _LOCK:
            _store_failure(key, ("configuration_required", revision, None, 0))
        return "configuration_required"
    quota = isinstance(error, (ProviderQuotaExhaustedError, ProviderUsageLimitError))
    with _LOCK:
        streak, until = _next_cooldown(_FAILURES.get(key), time.monotonic(), quota)
        _store_failure(key, ("cooldown", revision, until, streak))
    return "cooldown"


# LLM: 调用方持有 _LOCK；配置等待不计入阶梯，额度固定 300 秒但仍累计连续失败次数。
# 函数用途: 按上一条记录算出本次失败后的连续次数和冷却截止时刻。
def _next_cooldown(previous: tuple | None, now: float, quota: bool) -> tuple[int, float]:
    cooling = previous is not None and previous[0] == "cooldown" and previous[2] is not None
    if cooling and previous[2] > now:
        return previous[3], max(previous[2], now + _MAX_COOLDOWN_SECONDS) if quota else previous[2]
    streak = previous[3] + 1 if cooling else 1
    delay = _MAX_COOLDOWN_SECONDS if quota else _BASE_COOLDOWN_SECONDS * 2 ** min(streak - 1, 4)
    return streak, now + min(delay, _MAX_COOLDOWN_SECONDS)


# LLM: 调用方持有 _LOCK；表有界，满时淘汰最久未用的连接记录，淘汰只会让该连接从 30 秒重新计起。
# 函数用途: 写入一条连接失败记录并维持表的上限。
def _store_failure(key: tuple[str, str, str], item: tuple[str, str, float | None, int]) -> None:
    _FAILURES[key] = item
    _FAILURES.move_to_end(key)
    while len(_FAILURES) > _MAX_FAILURES:
        _FAILURES.popitem(last=False)


# LLM: 连接真实返回过响应即清除该连接的冷却和连续失败次数；响应之后的绑定/期限复核失败不属于连接故障。
# 函数用途: 在决策调用成功返回后复位这个连接的退避阶梯。
def record_success(key: tuple[str, str, str]) -> None:
    with _LOCK:
        _FAILURES.pop(key, None)


# LLM: 比较有效开关/模式/绑定及 Skill/tool 展示策略；时间变化不延长旧请求，也不在通知中重置时钟。
# 函数用途: 从原模块默认与两层覆盖得到一个接入点的实际路由，供精准设置撤销比较。
def routing_signature(context: object, settings: dict, point: str) -> tuple:
    values, _ = decision_defaults(context)
    values.update(settings["overrides"]["owner"])
    values.update(settings["overrides"]["thread"])
    mode = values[f"points.{point}.mode"] if values["enabled"] else "off"
    routing = (mode, values.get(f"points.{point}.profile_id", values["profile_id"]))
    if point == "skill_tool":
        routing += (values["points.skill_tool.context_policy"], tuple(values["points.skill_tool.optional_categories"]))
    return routing


# LLM: 必须在 owner/thread 文件锁外调用；按提交后的覆盖重算有效值，reset 和 thread 遮蔽不能用字段名猜。
# 函数用途: 将实际失效的在途请求标为设置撤销并立即取消精确句柄，不取消其他点或其他用户。
def notify_decision_settings_changed(context: object, result: dict) -> None:
    owner = decision_owner_ref(context)
    scope, thread_id = result["scope"], result["thread_id"]
    with _LOCK:
        targets = tuple(row for row in _ACTIVE.values() if row.owner_ref == owner and (scope == "owner" or row.thread_id == thread_id))
    for active in targets:
        _advance_notification(active, result)


# LLM: 较新通知即使被另一层覆盖也推进快照；CAS重查防逆序通知回滚，取消在索引锁外执行。
# 函数用途: 合并该请求可见的最新覆盖，再判断是否需要取消，修复 owner 变化后 thread reset 的交错。
def _advance_notification(active: ActiveDecision, result: dict) -> None:
    scopes = ("owner", "thread") if result["scope"] == "thread" else ("owner",)
    while True:
        with _LOCK:
            previous = active.settings
            advances = [scope for scope in scopes if result["revision"][scope] > previous["revision"][scope]]
            if not advances:
                return
            updated = {**previous, "revision": dict(previous["revision"]), "overrides": dict(previous["overrides"])}
            for scope in advances:
                updated["revision"][scope] = result["revision"][scope]
                updated["overrides"][scope] = result["overrides"][scope]
        try:
            changed = routing_signature(active.context, updated, active.point) != routing_signature(active.context, previous, active.point)
        except Exception:
            changed = True
        with _LOCK:
            if active.settings is not previous:
                continue
            active.settings = updated
            if changed:
                active.settings_cancelled = True
            break
    if changed:
        active.handle.cancel()
