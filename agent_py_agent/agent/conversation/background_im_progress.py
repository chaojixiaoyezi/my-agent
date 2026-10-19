# LLM: 只观察原委派任务的真实 claim/attempt，时间从执行进程产生；原 DeliveryService 编辑同条消息，线程元数据只保存运输 ID。
# 模块用途: 原调度进度复用同条窗口并在终态停更；实际委派后台不再独立向IM发进度。
from __future__ import annotations

import threading
from dataclasses import replace

from ..common.agent_time import now as display_now
from ..delivery import DeliveryContext, ReplyEnvelope
from ..scheduler.delivery_policy import normalize_delivery
from .execution_threads import EXECUTION_ORIGIN_KEY, IM_TASK_EXECUTION_KEY, execution_origin_thread
from .tool_approval_scope import tool_approval_scope, tool_approval_scope_is_current

_KEY='host_progress_delivery.v1'


# LLM: 进度外发只读精确根任务的活动调度run冻结策略；不读当前job或正文，错ID/线程不能冒认普通委派。
# 函数用途: 在原观察者启动前守住静默监控的通知范围，保留普通委派与旧逐轮回执的进度。
def _scheduled_progress_allowed(agent, root_task_id, thread_id):
    repository = getattr(agent, 'scheduler_repository', None)
    run = repository.get_active_run(root_task_id) if repository is not None else None
    if run is None:
        return True
    if run.get('run_id') != root_task_id or run.get('thread_id') != thread_id:
        raise ValueError('定时进度运行与原根任务或执行线程不匹配')
    delivery = normalize_delivery(run.get('delivery'))
    return delivery['mode'] == 'origin' and delivery.get('reply_policy', 'always') == 'always'


# LLM: 一个观察者只绑定一个实际工作片；启动、发送失败不改变任务，关闭必须在原 final 投递前调用。
# 类用途: 周期展示结构化等待阶段，后台聊天任务与执行互不占用车道。
class BackgroundIMProgress:
    # LLM: 有原SessionTask的委派仅回主sender，不开外部进度；其它原进度仍核scope及调度run冻结策略，配置0禁用。
    # 函数用途: 冻结原身份和回送绑定，启动可停止的轻量观察线程。
    def __init__(self,agent,task_id):
        self.agent=agent
        self.stop_event=threading.Event()
        self.phase='执行中'
        self.thread=None
        self.scope=tool_approval_scope(agent,task_id)
        link=agent.conversation_store.tasks.load(self.scope.root_task_id)
        if link is None:
            raise ValueError("原任务不存在")
        self.execution=agent.conversation_store.threads.require(link.thread_id)
        if not self.scope.active or IM_TASK_EXECUTION_KEY not in self.execution.metadata:
            return
        from .im_delegation_delivery import delegated_delivery_task

        if not _scheduled_progress_allowed(agent, self.scope.root_task_id, self.execution.thread_id):
            return
        if delegated_delivery_task(agent, self.execution.thread_id, task_id=self.scope.root_task_id) is not None:
            return
        interval=float(agent.config.im_stream_heartbeat_seconds)
        if interval<=0:
            return
        self.origin=execution_origin_thread(agent.conversation_store.threads.require,self.execution)
        binding=self.execution.metadata[EXECUTION_ORIGIN_KEY]['binding']
        from .channels import binding_delivery_target

        target = binding_delivery_target(binding['channel'], binding['actor_id'], binding['conversation_id'],
            group='/groups/' in str(self.origin.owner_id or ''))
        self.context=DeliveryContext(channel=binding['channel'],target=target,
            thread_id=self.origin.thread_id,task_id=self.scope.root_task_id,mode='proactive',idempotency_key='progress:'+self.scope.root_task_id)
        self.interval=min(120,max(1,interval))
        self.thread=threading.Thread(target=self._loop,name='im-task-progress',daemon=True)
        self.thread.start()

    # LLM: phase 只由原活动事件传入，不能接受自由模型正文作为健康状态；最终显示上限与 stream 共用配置。
    # 函数用途: 更新下次可见的真实阶段，不立即发请求，不在每个字符产生网络开销。
    def update(self,phase):
        self.phase=phase[:80]

    # LLM: 真实执行代次失效时停止；无新的执行证据不更新时间，发送错误不导致 LLM 重跑或生成假进展。
    # 函数用途: 以 120 秒为默认间隔更新原消息的短状态和时间。
    def _loop(self):
        while not self.stop_event.wait(self.interval):
            if not tool_approval_scope_is_current(self.agent,self.scope):
                break
            try:
                self._deliver()
            except (OSError,ValueError,RuntimeError,AttributeError):
                continue

    # LLM: 运输句柄只写原线程元数据，原 TaskLink/SessionTask 是生命周期权威；不依据消息正文判断完成。
    # 函数用途: 沿原进度接口创建一次消息，后续使用已确认 ID 编辑。
    def _deliver(self, *, finished=False):
        current=self.agent.conversation_store.threads.require(self.execution.thread_id)
        handles=current.metadata.get(_KEY) or {}
        handle=handles.get(self.scope.root_task_id) or {}
        if finished and not handle.get('message_id'):
            return
        stamp=display_now(self.agent.config.timezone).strftime('%H:%M:%S')
        limit=max(1,int(self.agent.config.im_stream_preview_max_chars))
        footer='更新于 '+stamp
        phase='本轮已结束' if finished else self.phase
        text=phase[:max(0,limit-len(footer)-1)]+'\n'+footer
        receipt=self.agent.delivery_service.deliver_progress(self.context,ReplyEnvelope(content=text[:limit]),message_id=handle.get('message_id',''))
        if not receipt.ok or not receipt.message_id:
            return
        def persist(thread):
            existing=dict(thread.metadata.get(_KEY) or {})
            existing[self.scope.root_task_id]={'message_id':receipt.message_id,'updated_at':stamp}
            return replace(thread,metadata={**thread.metadata,_KEY:existing})
        self.agent.conversation_store.threads.update_atomic(current.thread_id,persist)

    # LLM: finally/终态前调用，必须收回已发出的有界 provider 请求；不能用短于原网络超时的 join 让编辑落到 final 之后。
    # 函数用途: 停止观察线程，后台任务的原最终投递继续独立发送。
    def close(self):
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join()
        if self.thread is not None and not self.thread.is_alive() and tool_approval_scope_is_current(self.agent,self.scope):
            try:
                self._deliver(finished=True)
            except (OSError,ValueError,RuntimeError,AttributeError):
                pass


# LLM: 展示能力不可用只关闭观察，不拒绝原任务；不把普通后台巡检改成外部通知。
# 函数用途: 创建可选委派任务观察者，允许缺少 IM 来源的正常本地任务继续。
def make_background_progress(agent,task_id):
    try:
        return BackgroundIMProgress(agent,task_id)
    except (AttributeError,OSError,KeyError,ValueError,RuntimeError):
        return None


# LLM: 观察者可选，生命周期收回与原 final 顺序保持；不存在的观察者不能影响本地任务。
# 函数用途: 收回后台 IM 心跳线程，供原展示流收口调用。
def close_background_progress(observer):
    if observer is not None:
        observer.close()


# LLM: 阶段只取 typed tool/phase；自由工具输出不会成为存活证据或广播正文。
# 函数用途: 为后台工具执行更新可选的短窗口阶段。
def update_background_progress(pulse,progress):
    if pulse is not None:
        tool=str(progress.get('tool') or '工具')[:60]
        pulse.update('执行命令：'+tool if progress.get('phase') in {'started','running'} else '正在处理结果')
