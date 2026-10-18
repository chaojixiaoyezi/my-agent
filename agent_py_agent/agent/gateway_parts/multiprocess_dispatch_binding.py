# LLM: P5 执行检查只运输原 ClaimSnapshot/GatewayClaim；不替代原 lease、执行、历史和规范终态链。
# 模块用途: 为 owner/模型/工具/取消提供同一冻结绑定门，不读最新配置或创建 Agent。
from dataclasses import dataclass

from .lease_service import _lease_payload_matches_request
from .multiprocess_agent_contract import require_agent_home
from .multiprocess_bootstrap import require_worker_bootstrap
from .multiprocess_dispatch import WorkerDispatchError, require_claimable
from .multiprocess_dispatch_claim import ClaimSnapshot
from .multiprocess_records import deserialize_record
from .paths import gateway_paths_from_root
from .queue_service import GatewayClaim


# LLM: 两个嵌套对象均不可变，执行身份唯一来自原 claim，不新造 attempt 或 epoch。
# 类用途: 保存一次已认领回合与其盖章快照的关系，只在当前进程传递。
@dataclass(frozen=True)
class ExecutionBinding:
    snapshot: ClaimSnapshot
    claim: GatewayClaim


# LLM: 原队列认领成功后、任意业务副作用前绑定，任何身份错配必须失败关闭。
# 函数用途: 从原 claim 建立此次执行的不可变检查输入。
def bind_claimed_request(snapshot, claim, request):
    binding = ExecutionBinding(snapshot, claim)
    require_execution_binding(binding, request)
    return binding


# LLM: 四检查点只共用身份门；原 stop 原子写 closing+cancel，不能因此误拒其身份，也不能替代原新执行准入门。
# 函数用途: 返回精确冻结 owner，拒绝身份漂移；输入须当次权威实读，成功不授予新执行权。
def require_execution_binding(binding, request):
    if type(binding) is not ExecutionBinding or type(binding.snapshot) is not ClaimSnapshot:
        raise WorkerDispatchError("execution_binding_invalid", "binding")
    snapshot = binding.snapshot
    require_worker_bootstrap(snapshot.bootstrap, getattr(snapshot.bootstrap, "launch", None))
    _require_claim_fence(snapshot, binding.claim, request)
    return require_claimable(snapshot.bootstrap, request, deserialize_record(snapshot.reservation_bytes))


# LLM: 取消标记本身不是目标身份；检查通过才可调用原中断入口，不在这里发信号或写终态。
# 函数用途: 匹配本次在途请求的持久取消事实。
def require_cancellation(binding, request):
    owner = require_execution_binding(binding, request)
    if request.get("cancel_requested") is not True:
        raise WorkerDispatchError("cancellation_not_requested", "cancel_requested")
    return owner


# LLM: 不用 lease 的旧字段省略语义；保留 stop 的 closing 在途身份，模型/工具仍须原生命周期准入。
# 函数用途: 核原 claim 与当前 processing 请求精确一致，不生成或刷新租约。
def _require_claim_fence(snapshot, claim, request):
    if type(request) is not dict or request.get("status") != "processing" or request.get("turn_phase") not in ("open", "closing"):
        raise WorkerDispatchError("claim_not_processing", "request")
    _require_claim_identity(snapshot, claim, request)


# LLM: 活跃与终态只共用原claim身份核验；此函数不证明活跃准入，不刷新或读取磁盘租约。
# 函数用途: 对照原claim精确检查路径、请求号、attempt、epoch和lease owner。
def _require_claim_identity(snapshot, claim, request):
    _require_complete_claim(claim)
    paths = gateway_paths_from_root(snapshot.bootstrap.launch.gateway_root)
    if claim.path != paths.processing / snapshot.request_path.name or claim.request_id != snapshot.request_path.stem:
        raise WorkerDispatchError("claim_path_mismatch", "claim")
    if type(request) is not dict:
        raise WorkerDispatchError("claim_fence_mismatch", "request")
    if request.get("request_id", claim.request_id) != claim.request_id:
        raise WorkerDispatchError("claim_fence_mismatch", "request_id")
    expected = {"id": claim.request_id, "execution_attempt_id": claim.execution_attempt_id,
                "lease_epoch": claim.lease_epoch, "lease_owner": claim.lease_owner}
    for field, value in expected.items():
        if type(request.get(field)) is not type(value) or request[field] != value:
            raise WorkerDispatchError("claim_fence_mismatch", field)
    if not _lease_payload_matches_request(request, claim.path, claim.request_id, worker_id=claim.lease_owner,
                                          execution_attempt_id=claim.execution_attempt_id, lease_epoch=claim.lease_epoch):
        raise WorkerDispatchError("claim_fence_mismatch", "lease")


# LLM: 精确执行合同不能用空值或 bool/文本 epoch 触发原 lease 的兼容省略路径。
# 函数用途: 拒绝未认领、残缺或类型不合法的 claim。
def _require_complete_claim(claim):
    if type(claim) is not GatewayClaim or type(claim.lease_epoch) is not int or claim.lease_epoch <= 0:
        raise WorkerDispatchError("claim_fence_invalid", "claim")
    for field in ("request_id", "execution_attempt_id", "lease_owner"):
        value = getattr(claim, field)
        if type(value) is not str or not value or value != value.strip():
            raise WorkerDispatchError("claim_fence_invalid", field)


# LLM: 同一原 binding 先验证执行身份，再核所返回 Agent 的原 home_paths；不读取热 config 或造 Agent。
# 函数用途: 给模型、工具和用户存储消费前提供返回对象归属门；不是新执行许可。
def require_execution_agent(binding, request, agent):
    owner = require_execution_binding(binding, request)
    require_agent_home(binding.snapshot.bootstrap, owner, getattr(agent, "home_paths", None))
    return owner


# LLM: 仅终态补交使用；核原bootstrap/claim/预留/route/home，不赋予新回合执行资格，不要求processing文件仍存在。
# 函数用途: 让合法closed终态复用原owner Store，同时拒绝换attempt、owner或启动代次。
def require_terminal_execution_agent(binding, payload, agent):
    if type(binding) is not ExecutionBinding or type(binding.snapshot) is not ClaimSnapshot:
        raise WorkerDispatchError("execution_binding_invalid", "binding")
    if (type(payload) is not dict or payload.get("schema_version") != "gateway_terminal_request.v1"
            or payload.get("turn_phase") != "closed" or payload.get("status") == "processing"):
        raise WorkerDispatchError("terminal_binding_invalid", "terminal")
    response = payload.get("terminal_response")
    if type(response) is not dict or response.get("id") != binding.claim.request_id:
        raise WorkerDispatchError("terminal_binding_invalid", "response")
    snapshot = binding.snapshot
    require_worker_bootstrap(snapshot.bootstrap, getattr(snapshot.bootstrap, "launch", None))
    _require_claim_identity(snapshot, binding.claim, payload)
    owner = require_claimable(snapshot.bootstrap, payload, deserialize_record(snapshot.reservation_bytes))
    require_agent_home(snapshot.bootstrap, owner, getattr(agent, "home_paths", None))
    return owner
