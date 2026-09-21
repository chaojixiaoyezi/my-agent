# LLM: 本模块只持有一次 direct 调用的控制句柄；执行权仍由 RuntimeDB 决定，不能用消息编号推导 run/attempt。
# 模块用途: 在本地 worker 与控制端之间传递模型启动前发布的真实身份，并阻止停止后的迟到发布。
from __future__ import annotations

import threading

from ..concurrency.interrupt import interrupt_by_name, is_interrupted
from .control_commands import conversation_request_interrupt_name


# LLM: 每个 worker job 创建独立实例，Compact 复用它；不跨 job 复用，不持久化，不作为任务状态事实源。
# 类用途: 保存这一条本地消息实际绑定的运行身份，协调启动前中断、身份发布与回合结束。
class LocalRunControl:
    # LLM: request_id 来自宿主队列，只标识本次调用；真正执行身份由 bind_runtime_authority 填入。
    # 函数用途: 为一条本地消息建立独立控制句柄，避免旧回调覆盖下一条消息。
    def __init__(self, request_id: str) -> None:
        if not request_id:
            raise ValueError("本地回合缺少消息身份")
        self.request_id = request_id
        self._lock = threading.Lock()
        self._binding: dict[str, str] = {}
        self._interrupted = False
        self._finished = False

    # LLM: core 在 task guard 内、模型和工具之前调用；只允许同一次调用的原 task/run 轮换 attempt。
    # 函数用途: 发布实际执行身份；停止或结束后拒绝迟到发布，让 core 收口尚未启动的 attempt。
    def bind_runtime_authority(self, binding: dict[str, str]) -> bool:
        fields = ("task_id", "run_id", "agent_run_id", "attempt_id")
        if (binding.get("invocation_run_id") != self.request_id
                or any(not isinstance(binding.get(key), str) or not binding[key].strip() for key in fields)):
            raise ValueError("本地运行绑定与消息身份不符")
        with self._lock:
            if self._interrupted or self._finished:
                raise InterruptedError("本地回合已经关闭身份发布")
            if self._binding and any(self._binding[key] != binding[key] for key in fields[:-1]):
                raise ValueError("本地运行绑定不能切换任务或运行链")
            self._binding = {key: binding[key] for key in fields}
        return True

    # LLM: 复用宿主的 task-link 确认接口；原 Store 已拥有链接，确认不能反写或替代 RuntimeDB 身份。
    # 函数用途: 允许仍开放的本地回合完成既有任务晋升；未管理模式也不因此多出一份持久投影。
    def __call__(self, _link: object) -> bool:
        with self._lock:
            return not self._interrupted and not self._finished

    # LLM: 只返回副本，不在句柄锁内调用 DB 或任务锁；停止方须在 task guard 内再次读取。
    # 函数用途: 读取本次调用已正式发布的身份，空值表示尚未发布。
    def runtime_authority(self) -> dict[str, str]:
        with self._lock:
            return dict(self._binding)

    # LLM: 先关发布再调用现有有界命名中断；覆盖 worker 已显示运行但尚未注册线程的启动窗口。
    # 函数用途: 中断这一条消息的执行链，不触碰独立后台资源、子代理或插话。
    def request_interrupt(self) -> bool:
        with self._lock:
            if self._finished:
                return False
            self._interrupted = True
        interrupt_by_name(conversation_request_interrupt_name(self.request_id))
        return True

    # LLM: 注册命名中断后、agent.run 前调用；未发布身份的停止不能被后来的线程注册清空。
    # 函数用途: 拦住启动前已经被中断的本地回合。
    def check_admission(self) -> None:
        with self._lock:
            closed = self._interrupted or self._finished
        if closed or is_interrupted():
            raise InterruptedError("本地回合已中断")

    # LLM: 仅关闭本句柄的发布门；不会结算 RuntimeDB、清理资源或改变后来 job 的句柄。
    # 函数用途: worker 结束时拒绝旧回合的迟到回调，保留已发布身份供在途控制核对。
    def finish(self) -> None:
        with self._lock:
            self._finished = True


# LLM: 已发布身份按 task→句柄锁顺序重新读取；与 Compact 创建/发布共用原 task guard，慢清理留在锁外。
# 函数用途: 关闭本地主执行权并冻结原资源；未发布时只关闭发布门，无法管理的执行不能假称已清理。
def prepare_local_task_stop(agent: object, control: LocalRunControl):
    from .task_resources import close_main_task_authority, freeze_task_resources

    binding = control.runtime_authority()
    if not binding:
        control.request_interrupt()
        # 发布可能先于中断拿到句柄锁；此时必须按刚发布的真实身份继续停止。
        binding = control.runtime_authority()
        if not binding:
            if getattr(getattr(agent, "subagents", None), "runtime_db", None) is None:
                raise ValueError("未管理的本地回合无法确认资源归属")
            return None
    with agent.conversation_store.tasks.transition_guard(binding["task_id"]), agent.subagents.creation_guard():
        control.request_interrupt()
        binding = control.runtime_authority()
        scope = close_main_task_authority(
            getattr(getattr(agent, "subagents", None), "runtime_db", None),
            owner_home=getattr(getattr(agent, "home_paths", None), "owner_home_dir", ""),
            task_id=binding["task_id"], thread_id="", binding=binding,
        )
        if scope is None:
            raise ValueError("本地主执行链无法确认")
        return freeze_task_resources(
            agent, scope, control.request_id, related_request_ids=(binding["task_id"],),
        )
