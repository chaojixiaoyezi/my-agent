# LLM: ContextVar只传原claim；可选运输定位仅确切BG producer在原T内提供，同claim CAS一次写，投影不授权，Count0无新增字段/I/O。
# 模块用途: 将后台 core 发布的 run/attempt 投影到原会话租约，让插话和停止识别同片中的代次变化。
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from ..gateway_parts.io import update_json_file_atomic


# LLM: 句柄仅由领取租约的宿主构造；不得从客户端 metadata、任务文案或当前展示编号生成。
# 类用途: 固定当前后台片的原 claim 文件和不可复用的领取编号。
@dataclass(frozen=True)
class ClaimExecutionBinding:
    claims: object
    thread_id: str
    claim_id: str
    claim_scope_id: str
    gateway_input_source: object = field(default=None, init=False, repr=False, compare=False)


_CURRENT_CLAIM: ContextVar[ClaimExecutionBinding | None] = ContextVar("conversation_execution_claim", default=None)


# LLM: 嵌套调用退出时还原原上下文；上下文只供发布目标定位，落盘仍须匹配原持久 claim。
# 函数用途: 在已领取的后台片中携带精确租约句柄，不创建新租约或执行权限。
@contextmanager
def bind_claim_execution(claim: ClaimExecutionBinding):
    token = _CURRENT_CLAIM.set(claim)
    try:
        yield
    finally:
        _CURRENT_CLAIM.reset(token)


# LLM: core 在 task guard 内、模型前发布；随后取原 claim JSON 锁，保持 task→claim 顺序。
#   core原task guard不移动，具体BG producer持原T再取claim；运输/core同次CAS，Compact只换core attempt，Count0原写路不变。
# LLM: 原CAS成功后仅通知当前actual BG输入句柄；Count0无句柄则不查询资源或新增写。
# 函数用途: 把本次实际身份保存到原claim并更新本栈输入身份，Compact只允许同run换attempt。
def publish_claim_runtime_authority(binding: dict[str, str], *, gateway_binding=None) -> None:
    claim = _CURRENT_CLAIM.get()
    if claim is None:
        return
    keys = ("invocation_run_id", "task_id", "run_id", "agent_run_id", "attempt_id")
    if any(not isinstance(binding.get(key), str) or not binding[key] for key in keys):
        raise ValueError("后台执行绑定不完整")
    payload = {
        "schema_version": "conversation_claim_execution.v1", "claim_id": claim.claim_id,
        **{key: binding[key] for key in keys},
    }
    transport = None
    if gateway_binding is not None:
        from ..gateway_parts.multiprocess_background_execution import BackgroundRuntimeBinding

        if type(gateway_binding) is not BackgroundRuntimeBinding:
            raise ValueError("后台运输定位必须来自原绑定")
        transport = {**gateway_binding.claim_transport_reference(binding),
                     "claim_id": claim.claim_id, "thread_id": claim.thread_id}

    # LLM: 原子CAS保心跳/状态；运输与core同写，已有transport只容core_attempt_id变化，不能混旧fence/run/claim。
    # 函数用途: 拒绝已结束/换代的后台发布，并阻止同一片偷换任务或主运行链。
    def update(current):
        if (current.get("claim_id") != claim.claim_id or current.get("thread_id") != claim.thread_id
                or current.get("status") != "running"):
            raise InterruptedError("后台执行租约已经结束或切换")
        task_id = current.get("task_id")
        if task_id and task_id != binding["task_id"]:
            raise ValueError("后台执行绑定与租约任务不符")
        previous = current.get("runtime_authority")
        if isinstance(previous, dict) and any(previous.get(key) != payload[key] for key in keys[:-1]):
            raise ValueError("后台执行绑定不能切换任务或运行链")
        if transport is not None:
            previous_transport = current.get("gateway_transport_reference")
            if previous_transport is not None and (
                type(previous_transport) is not dict or set(previous_transport) != set(transport)
                or any(type(previous_transport[key]) is not type(value)
                       or (key != "core_attempt_id" and previous_transport[key] != value)
                       for key, value in transport.items())
            ):
                raise ValueError("后台运输定位不能切换原请求或运行链")
            return {**current, "runtime_authority": payload, "gateway_transport_reference": transport}
        return {**current, "runtime_authority": payload}

    updated = update_json_file_atomic(
        claim.claims.storage.background_claim_path(claim.claim_scope_id or claim.thread_id),
        update, require_existing=True,
    )

    if gateway_binding is not None and claim.gateway_input_source is not None:
        claim.gateway_input_source.runtime_published(gateway_binding, binding, updated)


# LLM: 只读原词法scope中的同一个claim对象；不从JSON重建，不创建许可。
# 函数用途: 为实际BG binder及最终收口取得原领取句柄。
def current_claim_execution():
    return _CURRENT_CLAIM.get()
