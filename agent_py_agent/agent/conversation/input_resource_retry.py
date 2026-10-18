# LLM: 会话输入仅重试已捕获source的同步安全点；普通资源忙在所有事务退出后等原interrupt，runtime与Gateway共用本实现，绝不重跑模型/工具或把typed失源当忙。
# 模块用途: 将同一deadline贯穿原固定输入步骤/末writer；门外等待，过期保原未结，不能代替source验权。
import time

from ..backends.request_scope import provider_request_timeout
from ..concurrency.interrupt import wait_interruptibly

# 同一次输入安全点的总等待预算，provider剩余时间只能进一步缩短。
INPUT_RESOURCE_RETRY_BUDGET_SECONDS = 5.0
# 事务退出后的资源忙检查间隔，等待仍可被原停止信号立即打断。
INPUT_RESOURCE_RETRY_INTERVAL_SECONDS = 0.05


# LLM: 单次安全点入口只捕获一个覆盖全部refs/retry的deadline，原provider余额只能缩短它；Count0调用方不进入本helper，不重签来源或新建许可。
# 函数用途: 等原输入文件短暂解锁后重做同一个本地动作，停止立即唤醒，源失效与真正错误原样抛出。
def run_input_safe_point(source, agent, action, params=None, *, deadline=None):
    source.require_original(agent)
    deadline = input_resource_deadline() if deadline is None else deadline
    while True:
        if time.monotonic() >= deadline:
            raise source.retry_exhausted(agent)
        try:
            result = _run_input_action(source, agent, action, params, deadline)
            source.require_original(agent)
            if time.monotonic() >= deadline:
                raise source.retry_exhausted(agent)
            return result
        except BlockingIOError as error:
            source.require_retryable_busy(agent, error)
            now = time.monotonic()
            if now >= deadline:
                # LLM: 已执行的append等事实不被否定；只拒绝进入下一次retry，原source/原请求tuple继续持责。
                # 函数用途: 在纯IO等待用尽时保守停止后续动作，不写普通failed或假称已完成。
                raise source.retry_exhausted(agent) from error
            wait_interruptibly(min(INPUT_RESOURCE_RETRY_INTERVAL_SECONDS, deadline - now))


# LLM: 只有既有三种固定source动作，参数/异常同对象原样通过；不接任意callback，不创建执行来源或刷新预算。
# 函数用途: 将同一deadline交给probe/drain/finish，减少外层重试分支嵌套。
def _run_input_action(source, agent, action, params, deadline):
    if action == 'probe':
        return source.probe(agent, params, deadline=deadline)
    if action == 'drain':
        return source.drain(agent, params, deadline=deadline)
    if action == 'finish':
        return source.finish(agent, deadline=deadline)
    raise ValueError('unsupported input safe point')


# LLM: 纯本地预算只在调用入口取一次，原provider绝对余额只能缩短；过期返回当前时刻，由具体原source决定未结，不发模型。
# 函数用途: 为同一次安全点或C发布的全部文件动作生成固定IO等待期限。
def input_resource_deadline():
    started = time.monotonic()
    try:
        remaining = provider_request_timeout(INPUT_RESOURCE_RETRY_BUDGET_SECONDS)
    except TimeoutError:
        remaining = 0.0
    return started + min(INPUT_RESOURCE_RETRY_BUDGET_SECONDS, max(0.0, remaining))
