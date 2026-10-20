# LLM: Count0与MP owner W共用原RuntimeDB单行消费；只发布既有文件Wake，不执行模型，来源认领/complete由调用方原CAS负责。
# 模块用途: 把原任务期限或定时字条转换成规范会话唤醒，不新建队列。
from __future__ import annotations


# LLM: quota→SQL claim窗口覆盖原文件效果；新重排后旧claim不能expire/publish，complete/release另CAS。task_link_status保原缺失/终态退休和unknown保留信号。
# 函数用途: 消费一条已由调用方认领的原字条，成功后仅完成这一条责任。
def consume_runtime_wake(inputs, wake, *, now, complete, task_link_status):
    task_id = wake['root_task_id']

    # LLM: 先取原quota，再持有SQL claim窗口；admission传入原输入mutate避免重入非重入配额锁。
    # 函数用途: 在准确claim仍有效时提交本行领域效果。
    def effect():
        with inputs.quota.admission(blocking=inputs.blocking) as admission:
            # LLM: 仅当前claim的同步事务回调，禁止模型/网络及RuntimeDB嵌套写；deadline重排在complete后。
            # 函数用途: 复用原输入对账或原业务Wake发布。
            def publish():
                if wake['kind'] == 'task_input_deadline':
                    inputs.reconcile(task_id,now=now,schedule=False,admission=admission)
                    return
                thread = inputs.store.tasks.thread_for(task_id)
                if thread is not None and task_link_status(thread.thread_id,task_id) not in {'terminal','missing'}:
                    inputs.store.wakes.raise_signal({'thread_id':thread.thread_id,'urgency':'normal',
                        'reason':'wake_queue_due','root_task_id':task_id,'source_agent_id':'wake-queue',
                        'summary':task_id,'dedupe_key':f'wake-queue:{task_id}'},blocking=inputs.blocking)
            return inputs.runtime_db.perform_runtime_wake(wake,publish)
    if not inputs.perform(effect):
        return False
    if complete() and wake['kind'] == 'task_input_deadline':
        link,error = inputs.store.tasks.load_report(task_id)
        if error:
            raise ValueError('输入任务记录不可读')
        if link is not None:
            inputs.schedule(task_id)
    return True
