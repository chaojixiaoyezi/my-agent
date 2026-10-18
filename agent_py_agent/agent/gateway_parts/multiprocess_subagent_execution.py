# LLM: 只消费独立原child request/P3/P5与canonical谱系；未知在原scope内观察既有账，退出后不重绑、不另建run/attempt/queue。
# 模块用途: 将实际child请求绑定到原C/W执行与工具平台门；孤儿业务由agent_core自身消费。
from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .multiprocess_background_execution import (
    background_child_scope,
    background_short_operation,
    current_background_request,
)
from .multiprocess_coordinator_execution import CoordinatorExecution, coordinator_execution_scope
from .multiprocess_execution import (
    WorkerExecution,
    current_worker_execution,
    worker_execution_scope,
)


# LLM: capture只保存当次scope和原子代理身份，原shared lifetime/claim仍末次裁决；不能序列化、fork或给其它run复用。
# 类用途: 将一个实际child运输的原执行事实显式传给原worker和它的设施线程。
@dataclass(frozen=True)
class ChildRequestTransport:
    scope: object
    run_id: str
    root_id: str
    parent_id: str
    owner_id: str
    canonical_ref: str
    creator_pid: int
    native_launcher: object = None
    worker_agent: object = None


# LLM: 原metadata refs已经冻结P5；捕获只接受当前Agent和同一原canonical任务，不能把父请求借给任意新孩子。
# 函数用途: 为当前独立孤儿请求保存准确child身份。
def capture_child_transport(agent, task):
    scope = current_background_request()
    if scope is None:
        return None
    ref = scope.context["request"]["metadata"]["background_source"]
    if ref["kind"] != "orphan" or ref["source_id"] != task.id or scope.execution.agent is not agent:
        raise ValueError("gateway_child_source_required")
    from ..common.executor_registration import capture_executor_launcher

    return ChildRequestTransport(scope, task.id, task.root_id, task.parent_id, task.owner,
        str(Path(task.agent_run_workspace_dir) / "canonical_state.json"), os.getpid(), capture_executor_launcher())


# LLM: 原manager读取canonical而非projection；原run/root/parent/owner和source路径必须精确，状态/attempt准入仍由原生命周期负责。
# 函数用途: 拒绝给其它owner、根任务或子代理安装本次运输身份。
def _require_child(transport, agent):
    if type(transport) is not ChildRequestTransport or transport.creator_pid != os.getpid():
        raise ValueError("gateway_child_transport_required")
    task = agent.subagents.load(transport.run_id)
    actual = (task.id, task.root_id, task.parent_id, task.owner, str(Path(task.agent_run_workspace_dir) / "canonical_state.json"))
    expected = (transport.run_id, transport.root_id, transport.parent_id, transport.owner_id, transport.canonical_ref)
    if actual != expected or agent.home_paths.owner_home_dir != transport.scope.execution.agent.home_paths.owner_home_dir:
        raise ValueError("gateway_child_identity_changed")
    return task


# LLM: 已持原T/P5；可复用门内实读payload，canonical谱系与冻结ref必须一致；返回原任务供dispatch结果对照，不新授child执行权。
# 函数用途: 末次比对原请求冻结的唯一child目标，并保留其实际canonical对象。
def _require_original_child_ref(transport, agent, *, payload=None):
    from dataclasses import asdict

    from ..common.canonical_source_contract import canonical_child_source_contract
    from .io import read_json_file
    from .multiprocess_background_request import source_digest

    task = _require_child(transport, agent)
    if payload is None:
        payload = read_json_file(transport.scope.context["request_path"])
    ref = payload.get("metadata", {}).get("background_source")
    if (type(ref) is not dict or ref.get("kind") != "orphan" or ref.get("source_id") != task.id
            or ref.get("input_revision") != source_digest(canonical_child_source_contract().identity(asdict(task)))
            or str(agent.home_paths.owner_home_dir / ref["source_ref"]) != transport.canonical_ref):
        raise ValueError("gateway_child_frozen_source_changed")
    return task


# LLM: 原child C绑定消费builder实际dispatch receipt及同ref；W binding不变，outer lifetime拒late，未知在关闭本scope前沿原claim观察，模型不持T。
# 函数用途: 为当前实际子代理安装当次原C/W执行和工具登记，退出精确还原。
@contextmanager
def child_worker_scope(transport, agent):
    if transport is None:
        yield
        return
    scope = transport.scope
    context = {**scope.context, "agent": agent}
    with ExitStack() as stack:
        if type(scope.execution) is CoordinatorExecution:
            from .multiprocess_child_execution_binding import capture_child_execution_binding

            child_binding = capture_child_execution_binding(transport, agent)
            stack.enter_context(coordinator_execution_scope(scope.execution.gate, scope.execution.claim, agent,
                                                            child_binding=child_binding))
        elif type(scope.execution) is WorkerExecution:
            from ..common.executor_registration import executor_scope

            _require_child(transport, agent)
            binding = scope.execution.binding
            stack.enter_context(worker_execution_scope(binding, agent))
            stack.enter_context(executor_scope(transport.native_launcher, binding.snapshot.bootstrap.launch.partition))
        else:
            raise ValueError("gateway_child_transport_required")
        stack.enter_context(background_child_scope(context, current_worker_execution(), scope.lifetime))
        background_short_operation("child_frozen_source", lambda: _require_original_child_ref(transport, agent))
        execution = current_worker_execution()
        try:
            yield
        except Exception as error:
            # 原设施捕获当前child execution；必须在ExitStack关闭原lifetime前观察，不能等父层重绑旧Call。
            _observe_original_child_unknown(execution, error)
            raise


# LLM: 路径和tuple只取当前原execution的claim，不读可改context/path作为第二归属；原T/CAS再核Call/心跳/typed来源，失败不替换原异常。
# 函数用途: 在原child来源尚活时附唯一processing unknown，退出后父层只维持原占用。
def _observe_original_child_unknown(execution, error):
    from .io import read_json_file
    from .logging import _report_gateway_side_effect_error
    from .paths import gateway_paths_from_root
    from .request_errors import gateway_request_execution_unknown
    from .request_worker import _record_execution_outcome_uncertain

    claim = execution.claim if type(execution) is CoordinatorExecution else execution.binding.claim
    try:
        payload = read_json_file(claim.path)
        expected = (claim.request_id, claim.execution_attempt_id, claim.lease_epoch)
        if gateway_request_execution_unknown(error, payload, expected):
            _record_execution_outcome_uncertain(gateway_paths_from_root(claim.path.parent.parent.parent),
                                                claim.path, error, expected)
    except Exception as observation_error:
        _report_gateway_side_effect_error("observe_original_child_unknown", claim.request_id, observation_error)
