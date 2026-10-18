# LLM: 只捕获原dispatch返回对象和本次独立child请求；原T/P3/P5/RuntimeDB仍唯一授权，纯内存receipt不建账或补权，同步child/native源测试。
# 模块用途: 把实际子代理与原认领的协调者来源绑定，让同一child工具登记保留原C、claim和canonical谱系。
from __future__ import annotations

import os


# LLM: 默认构造没有来源；只有原builder在真实T内捕获后挂到准确worker，origin/self/PID/同worker指针拒copy/ctor/fork。
# 类用途: 保留原dispatch授权门已经返回的实际任务与智能体，不重新授权或写任何状态。
class ChildDispatchReceipt:
    # LLM: 构造纯内存且未捕获，单独对象不能被当作dispatch结果。
    # 函数用途: 创建尚无原来源的receipt外壳。
    def __init__(self):
        self._origin, self._creator_pid, self._facts = self, os.getpid(), None

    # LLM: 只核自产引用/字段，不要求当前attempt或scope活；终态物理read/finish必须仍可辨认原责任。
    # 函数用途: 拒绝复制receipt、不同worker、不同原来源和home漂移。
    def require_identity(self, scope, agent):
        if (type(self) is not ChildDispatchReceipt or self._origin is not self
                or self._creator_pid != os.getpid() or self._facts is None):
            raise ValueError("original_child_dispatch_receipt_required")
        original_scope, original_agent, _task, _identity, home, location = self._facts
        if (scope is not original_scope or agent is not original_agent
                or getattr(agent, "_gateway_child_dispatch", None) is not self or agent.home_paths is not home
                or (str(home.root), str(home.owner_home_dir), home.owner_id) != location):
            raise ValueError("original_child_dispatch_receipt_changed")


# LLM: 默认没有来源/绑定；原组合根只在原T验证receipt/ref后捕获并一次绑定actualexecution，copy/子类/同字段包装均拒。
# 类用途: 保存本次实际child执行包装的原C来源，静态物理身份与活跃提交准入分别核验。
class ChildExecutionBinding:
    # LLM: 未捕获外壳不授任何权限，不构造claim或lifetime。
    # 函数用途: 准备一个尚不可消费的组合绑定。
    def __init__(self):
        self._origin, self._creator_pid = self, os.getpid()
        self._facts, self._execution = None, None

    # LLM: 只接受原scopeproducer建立的actualexecution一次；捕获来源未就绪或不同Agent/gate/claim不能重绑。
    # 函数用途: 将准确child来源固定到这次原C执行包装。
    def bind_original(self, execution):
        from .multiprocess_coordinator_execution import CoordinatorExecution

        if (type(self) is not ChildExecutionBinding or self._origin is not self
                or self._creator_pid != os.getpid() or self._facts is None or self._execution is not None):
            raise ValueError("original_child_execution_binding_required")
        _receipt, _transport, agent, parent, _base = self._facts
        if (type(execution) is not CoordinatorExecution or execution.gate is not parent.gate
                or execution.claim is not parent.claim or execution.agent is not agent
                or execution.child_binding is not self):
            raise ValueError("original_child_execution_binding_changed")
        self._execution = execution
        self.require_identity(execution)

    # LLM: 无I/O，只核自产原对象/creator/谱系引用，既有P1责任不因scope或AgentAttempt结束消失；同步原source read/finish合同。
    # 函数用途: 确认本来源仍是原实际child，不将物理清理误当新业务执行。
    def require_identity(self, execution):
        from .multiprocess_coordinator_execution import CoordinatorExecution

        if (type(self) is not ChildExecutionBinding or self._origin is not self
                or self._creator_pid != os.getpid() or self._facts is None or self._execution is not execution):
            raise ValueError("original_child_execution_binding_required")
        receipt, transport, agent, parent, base = self._facts
        receipt.require_identity(transport.scope, agent)
        if (type(parent) is not CoordinatorExecution or parent.gate.agent is not base or parent.agent is not base
                or transport.scope.execution is not parent or transport.worker_agent is not agent
                or execution.gate is not parent.gate or execution.claim is not parent.claim
                or execution.agent is not agent or execution.child_binding is not self
                or execution.creator_pid != self._creator_pid):
            raise ValueError("original_child_execution_binding_changed")
        _require_receipt_transport(receipt, transport)

    # LLM: 原T已经持有；同actualexecution和outer lifetime必须活，再核原processing ref及canonical身份，不能以receipt代替当次P3/P5。
    # 函数用途: 在新增模型/工具/短写之前核本次child来源仍可消费。
    def require_claim(self, execution, payload):
        from .multiprocess_background_execution import _original_background_lifetime_active
        from .multiprocess_subagent_execution import _require_original_child_ref

        self.require_identity(execution)
        _receipt, transport, agent, parent, _base = self._facts
        if not _original_background_lifetime_active(transport.scope.lifetime):
            raise ValueError("original_child_scope_unavailable")
        parent.lifetime.require_original(parent)
        execution.lifetime.require_original(execution)
        _require_original_child_ref(transport, agent, payload=payload)


# LLM: 仅结构化canonical身份，不读取goal/summary/status文字；原task/run/root/parent及路径必须逐字保持。
# 函数用途: 对比原dispatch任务和唯一来源ref运输的谱系。
def _child_identity(task):
    from pathlib import Path

    return (task.id, task.root_id, task.parent_id, task.owner,
            str(Path(task.agent_run_workspace_dir) / "canonical_state.json"), str(task.task_workspace_dir))


# LLM: 原receipt task已通过唯一dispatch门；这里只比同source的捕获谱系，不刷新权限/attempt，不从文件重造授权。
# 函数用途: 防止把已验证worker借给其它run或路径。
def _require_receipt_transport(receipt, transport):
    identity = receipt._facts[3]
    if identity[:5] != (transport.run_id, transport.root_id, transport.parent_id,
                        transport.owner_id, transport.canonical_ref):
        raise ValueError("original_child_dispatch_identity_changed")


# LLM: builder传入原authorize_operation的实际返回task；真实T内只核同canonical来源并捕获，不再授权/写账，Count0不调用。
# 函数用途: 将第一次原dispatch结果保存到它实际构造的worker。
def capture_original_child_dispatch(transport, agent, task):
    from .multiprocess_background_execution import background_short_operation
    from .multiprocess_subagent_execution import _require_original_child_ref

    # LLM: 原T已核当前claim；task必须与原canonical身份一致后才能发布唯一receipt指针，只有内存副作用。
    # 函数用途: 捕获本次builder已经确认的原dispatch结果。
    def capture():
        current = _require_original_child_ref(transport, agent)
        if _child_identity(current) != _child_identity(task):
            raise ValueError("original_child_dispatch_identity_changed")
        receipt = ChildDispatchReceipt()
        home = agent.home_paths
        receipt._facts = (transport.scope, agent, task, _child_identity(task), home,
                          (str(home.root), str(home.owner_home_dir), home.owner_id))
        agent._gateway_child_dispatch = receipt
        return receipt

    return background_short_operation("child_dispatch_capture", capture, transition=transport.scope.transition)


# LLM: 原outer transition捕获真实C/claim，设施线程无ContextVar时仍核同原活来源；只有短准入读事实，失败准确typed未结，不建账。
# 函数用途: 为同一实际child进入原C scope准备不可重绑的本次绑定。
def capture_child_execution_binding(transport, agent):
    from .multiprocess_background_execution import (
        _original_background_lifetime_active,
        _uncertain_execution,
    )
    from .multiprocess_coordinator_execution import CoordinatorExecution
    from .multiprocess_subagent_execution import _require_original_child_ref

    scope = transport.scope
    try:
        with scope.transition.scope("child_scope_admission"):
            if type(scope.execution) is not CoordinatorExecution or not _original_background_lifetime_active(scope.lifetime):
                raise ValueError("original_child_scope_unavailable")
            receipt = getattr(agent, "_gateway_child_dispatch", None)
            if type(receipt) is not ChildDispatchReceipt or transport.worker_agent is not agent:
                raise ValueError("original_child_dispatch_receipt_required")
            receipt.require_identity(scope, agent)
            _require_receipt_transport(receipt, transport)
            _require_original_child_ref(transport, agent)
            binding = ChildExecutionBinding()
            binding._facts = (receipt, transport, agent, scope.execution, scope.execution.gate.agent)
            return binding
    except Exception as error:
        raise _uncertain_execution(scope.transition, "child_scope_admission") from error
