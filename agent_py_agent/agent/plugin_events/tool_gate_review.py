# LLM: B5 征询消费唯一安装事实与 B2 注入的共用池；不自行建连接、授权或改参数。执行器和配置测试共用此入口。
# 模块用途: 在一次工具调用预算内并行询问匹配的插件，复核停用与换代，只返回收紧事实。
from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace

from ..plugin_channel import ChannelCall, PluginChannelRevoked, PluginChannelTimeout
from .tool_gate import GateCall, GateReply, GateReview, GateTarget, PluginToolGate, failure_review


# LLM: 外部依赖显式成组，pool 只能是 B2 池的宿主提供方；installations 测试可用假激活。
# 类用途: 保存征询的 owner、配置、池和安装读取入口。
@dataclass(frozen=True)
class GateWiring:
    owner: object
    config: object
    pool: Callable[[], object]
    installations: Callable[[object], tuple]


# LLM: 任务保留原调用与本轮总 deadline，排队/启动/换代都不能重新给满预算。
# 类用途: 保存一次单门征询的固定激活与剩余预算。
@dataclass(frozen=True)
class _ReviewJob:
    row: object
    target: GateTarget
    call: GateCall
    started: float
    deadline: float


# LLM: 不创建连接池，不缓存授权或结果；每次调用都从安装表读激活，B2 池复核发送前后同代。
# 类用途: 用宿主提供的唯一共用池并行征询，并在剩余预算内追随新激活。
class PluginGateReviewer:
    # LLM: 构造仅存依赖，不启动插件。
    # 函数用途: 保存本次宿主接线。
    def __init__(self, wiring: GateWiring) -> None:
        self.wiring = wiring

    # LLM: 当前安装表每轮复读；同调用已批准门只投影宿主事实、不发协议，其它门及换代仍用原deadline征询，读失败宁严。
    # 函数用途: 返回本次所有征询事实，包括作废旧代和预算不足的新代。
    def review(self, call: GateCall) -> tuple[GateReview, ...]:
        started = time.monotonic()
        deadline = started + plugin_gate_timeout_seconds(self.wiring.config)
        reviews: list[GateReview] = []
        targets = _targets(self.wiring.installations(self.wiring.owner), call)
        while targets:
            seen = {_identity(item.target) for item in reviews}
            pending = [(row, target) for row, target in targets if _identity(target) not in seen]
            if not pending:
                break
            jobs = tuple(_ReviewJob(row, target, call, started, deadline) for row, target in pending)
            reviews.extend(self._round(jobs))
            targets = _targets(self.wiring.installations(self.wiring.owner), call)
            current = {_identity(target) for _row, target in targets}
            reviews = [_current_review(item, current) for item in reviews]
            if time.monotonic() >= deadline:
                seen = {_identity(item.target) for item in reviews}
                reviews.extend(_failure(target, "timeout", started) for _row, target in targets
                               if _identity(target) not in seen)
                break
        return tuple(reviews)

    # LLM: 已批准门无需线程或协议请求；approval_applied区分宿主事实，不当作插件回答记账。其它门复用B2池和同一总deadline。
    # 函数用途: 并行征询当前匹配集合，将未及时完成的项记为要求确认。
    def _round(self, jobs: tuple[_ReviewJob, ...]) -> tuple[GateReview, ...]:
        approved = tuple(GateReview(job.target, GateReply("allow_as_is", "PLUGIN_GATE_APPROVED"), approval_applied=True)
                         for job in jobs if PluginToolGate.approved(job.target, job.call))
        approved_targets = {_identity(item.target) for item in approved}
        jobs = tuple(job for job in jobs if _identity(job.target) not in approved_targets)
        if not jobs:
            return approved
        if time.monotonic() >= jobs[0].deadline:
            return (*approved, *(_failure(job.target, "timeout", job.started) for job in jobs))
        executor = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="plugin-gate")
        futures = {executor.submit(self._exchange, job): job for job in jobs}
        done, _pending = wait(futures, timeout=max(0, jobs[0].deadline - time.monotonic()))
        results = tuple(future.result() if future in done else _failure(job.target, "timeout", job.started)
                        for future, job in futures.items())
        executor.shutdown(wait=False, cancel_futures=True)
        return (*approved, *results)

    # LLM: 每次请求先 acquire 刷新 last_used，再传剩余 timeout；握手在发送前校验，不采用额外回复字段。
    # 函数用途: 经 B2 完成一个固定代次征询，将结构化故障转换为 ask，撤销结果不参与收紧。
    def _exchange(self, job: _ReviewJob) -> GateReview:
        try:
            remaining = job.deadline - time.monotonic()
            if remaining <= 0:
                return _failure(job.target, "timeout", job.started)
            pool = self.wiring.pool()
            if pool is None:
                return _failure(job.target, "unavailable", job.started)
            connection = pool.acquire(str(self.wiring.owner.home_dir), job.row, time.monotonic())
            result = pool.request(connection, ChannelCall(
                owner=self.wiring.owner, method="my-agent/tool-gate.review",
                params=PluginToolGate.request_payload(job.target, job.call),
                timeout=max(0, job.deadline - time.monotonic()), before_send=_require_capability))
            return replace(PluginToolGate.decode_reply(job.target, result), latency_ms=_latency(job.started))
        except PluginChannelRevoked:
            return _failure(job.target, "revoked", job.started)
        except _GateUnavailable:
            return _failure(job.target, "unavailable", job.started)
        except (PluginChannelTimeout, TimeoutError):
            return _failure(job.target, "timeout", job.started)
        except Exception as exc:  # noqa: BLE001 插件错误不得使工具放行，异常正文不进入回复或账本
            outcome = "timeout" if getattr(exc, "code", "") == "MCP_TIMEOUT" else "error"
            return _failure(job.target, outcome, job.started)


# LLM: 真实消费 AgentConfig 字段；旧配置缺项/非法类型回退默认，不把 bool 当整数，也不放大到范围外。
# 函数用途: 将配置的 200–10000 毫秒转换为一次调用总预算秒数。
def plugin_gate_timeout_seconds(config: object) -> float:
    value = getattr(config, "plugin_tool_gate_timeout_ms", 2000)
    return (value if type(value) is int and 200 <= value <= 10000 else 2000) / 1000


# LLM: 未声明版本 1 是请求不可用，不是连接错误；不得发出征询或让其它调用方进入连接退避。
# 类用途: 表示插件握手不支持一期收紧协议。
class _GateUnavailable(ValueError):
    pass


# LLM: 只读 initialize 的 experimental 声明，版本为确切字符串 1；不根据自然语言描述猜能力。
# 函数用途: 在 B2 实际发送前核对插件收紧握手。
def _require_capability(client: object) -> None:
    capabilities = getattr(client, "capabilities", None)
    experimental = capabilities.get("experimental") if isinstance(capabilities, dict) else None
    declared = experimental.get("my-agent/tool-gate") if isinstance(experimental, dict) else None
    versions = declared.get("versions") if isinstance(declared, dict) else None
    if not isinstance(versions, list) or "1" not in versions:
        raise _GateUnavailable()


# LLM: 安装 Store 是唯一权威，不扫描目录；仅接受当前发布激活，失败不得当作没有插件。
# 函数用途: 为生产征询读取 owner 的安装快照。
def enabled_gate_installations(owner: object) -> tuple:
    from ..plugin_install_store import PluginInstallStore

    return tuple(row for row in PluginInstallStore(owner).snapshot() if row.enabled and row.activation is not None)


# LLM: None 表示读表失败，必须上抛宁严；此投影不授予激活、批准或延長原代。
# 函数用途: 从当前启用记录中投影本次匹配的单门目标。
def _targets(rows: tuple, call: GateCall) -> tuple:
    if rows is None:
        raise _GateUnavailable()
    return tuple(item for row in rows if row.enabled and row.activation is not None for item in _row_targets(row, call))


# LLM: 声明由 B1 严格清单构造，匹配只看精确工具和宿主效果，不读模型参数中的任何控制字段。
# 函数用途: 将一个激活匹配到的声明绑定到固定插件身份。
def _row_targets(row: object, call: GateCall) -> tuple:
    manifest = row.manifest
    return tuple((row, GateTarget(manifest.plugin_id, manifest.version, row.activation.activation_id, declaration))
                 for declaration in PluginToolGate.matching(getattr(manifest, "tool_gates", ()), call))


# LLM: 本调用去重/撤销身份包含包版本；批准跳门后复读发生版本变化也撤销，不能漏掉同激活下声明更新，不保存授权。
# 函数用途: 取得一条征询的固定插件/激活/门身份。
def _identity(target: GateTarget) -> tuple:
    return target.plugin_id, target.version, target.activation_id, target.declaration.id


# LLM: 失败统一交纯合同 failure_review，不在征询分支另拼 verdict；message 为空，撤销由合并剔除。
# 函数用途: 补上本次延迟后生成同源故障事实。
def _failure(target: GateTarget, outcome: str, started: float) -> GateReview:
    return failure_review(target, outcome, _latency(started))


# LLM: 只有安装快照证明失效才作废；校准也走统一故障构造，池关闭仍启用时按 unavailable 防止误放行。
# 函数用途: 保留本次延迟，依据安装事实生成撤销或不可用结果。
def _current_review(review: GateReview, current: set[tuple]) -> GateReview:
    if _identity(review.target) not in current:
        return failure_review(review.target, "revoked", review.latency_ms)
    if review.outcome == "revoked":
        # 池关闭/代次引用读失败也会报 revoked，但安装表仍启用，不能据此当作停用而放行。
        return failure_review(review.target, "unavailable", review.latency_ms)
    return review


# LLM: 计时用单调时钟，排队等待算本次延迟；不记录插件消息正文。
# 函数用途: 计算征询从本次开始到当前的毫秒数。
def _latency(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
