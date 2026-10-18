# LLM: 纯内存同源scope限制，原current_execution仍唯一包装；token不授P3/P5许可、不做进程/磁盘I/O，不用threadID阻止原模型设施线程。
# 模块用途: 原C/W作用域共享同一活性对象，退出/fork/复制对象后不能借未结processing重新执行。
from __future__ import annotations

import os


# LLM: origin/self和exact execution只用于拒绝重绑/复制；实际claim、source、lease、peer仍由原C/W门裁决，不能序列化成持久权威。
# 类用途: 保留原执行包装在本进程的准确scope生命周期。
class ExecutionScopeLifetime:
    # LLM: 仅原scope组合根创建，本身无许可；绑定之前任何执行核验都失败，Count0不构造此对象。
    # 函数用途: 创建尚未绑定的原作用域限制。
    def __init__(self):
        self.creator_pid = os.getpid()
        self.active = True
        self._origin = self
        self._execution = None

    # LLM: 同一token只接受原producer当次包装一次，PID/origin/active不可由绑定恢复；没有任何写账或新准入。
    # 函数用途: 把scope生命周期绑定到唯一实际执行对象。
    def bind_original(self, execution):
        if (self._origin is not self or self.creator_pid != os.getpid() or self.active is not True
                or self._execution is not None):
            raise ValueError("execution_scope_binding_invalid")
        self._execution = execution

    # LLM: 核同对象+真实PID+活共享token，copy_context保同token因此退出即失效；允许仍活原worker跨线程调用，末次P5仍必需。
    # 函数用途: 拒绝退出、fork或重新包装的执行上下文。
    def require_original(self, execution):
        if (self._origin is not self or self.creator_pid != os.getpid() or self.active is not True
                or self._execution is not execution):
            raise ValueError("execution_scope_unavailable")

    # LLM: 失效只缩小许可，不能再打开或释放任何持久P3/P1；实际物理收尾保持原专用source合同。
    # 函数用途: 在原scope finally关闭共享活性，使所有复制上下文同时失效。
    def close(self):
        self.active = False
