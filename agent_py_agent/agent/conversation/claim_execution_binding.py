# LLM: ContextVar 只传递本 runner 已领取的 claim 句柄；真实执行身份来自 core，写入时 CAS 复核原 claim。
# 模块用途: 将后台 core 发布的 run/attempt 投影到原会话租约，让插话和停止识别同片中的代次变化。
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ..gateway_parts.io import update_json_file_atomic


# LLM: 句柄仅由领取租约的宿主构造；不得从客户端 metadata、任务文案或当前展示编号生成。
# 类用途: 固定当前后台片的原 claim 文件和不可复用的领取编号。
@dataclass(frozen=True)
class ClaimExecutionBinding:
    claims: object
    thread_id: str
    claim_id: str
    claim_scope_id: str


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
#   无宿主 claim 的直接执行不写盘；错租约和换 task/run 必须拒绝。本投影不续租，过期执行资格由原准入与读取方裁决。
# 函数用途: 把本次实际身份保存到原 claim，Compact 只允许同一 run 更新 attempt。
def publish_claim_runtime_authority(binding: dict[str, str]) -> None:
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

    # LLM: 原子 CAS 不改心跳和状态；这里只写 core 身份投影，不能从当前 DB 最新行猜身份。
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
        return {**current, "runtime_authority": payload}

    update_json_file_atomic(
        claim.claims.storage.background_claim_path(claim.claim_scope_id or claim.thread_id),
        update, require_existing=True,
    )
