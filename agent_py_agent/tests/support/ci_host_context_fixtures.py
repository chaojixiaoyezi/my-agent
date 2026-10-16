# LLM: 仅供离线合同测试显式声明本地父运行；不替代产品身份解析、owner 路由或授权门。
# 模块用途: 防止宽 MagicMock 自动生成的属性被误当成宿主签发的当前身份。
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams


# LLM: 无 IM 来源的参数合同测试使用真正 RunParams 的空身份；外部 actor 用例必须另外建立真实来源记录。
# 函数用途: 保留调用方原有服务替身，固定本用例是本地无通道身份的父运行。
def local_mock_agent():
    agent = MagicMock()
    agent._current_run_params = RunParams(task_attributes={}, host_channel_actor=None)
    return agent


# LLM: 后台恢复从 canonical TaskLink 读取身份；此入口只给已存在的本地线程登记原任务，不提供外部身份。
# 函数用途: 在临时 ConversationStore 里补齐后台测试应有的精确 thread/task 关联。
def bind_local_task(agent, thread_id, task_id):
    return agent.conversation_store.tasks.bind({"thread_id": thread_id, "task_id": task_id, "goal": "离线后台合同验证"})


# LLM: 原 IM actor 只能由准确 owner、真实线程绑定与 typed 宿主入口签发；不授予群能力，也不替换权限门。
# 函数用途: 为隔离群资料夹具建立原请求及当前运行，调用方仍须走产品配置事务取得短租约。
def bind_im_task(agent, *, actor_id, conversation_id, task_id="ci-host-request"):
    from agent_py_agent.agent.conversation.host_actor import actor_from_scope

    store = agent.conversation_store
    thread = store.threads.get_or_create({"channel": agent.home_paths.owner_provider,
        "channel_conversation_id": conversation_id, "channel_user_id": actor_id,
        "canonical_user_id": actor_id, "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir)})
    scope = SimpleNamespace(channel=agent.home_paths.owner_provider,
        channel_user_id=actor_id, channel_conversation_id=conversation_id)
    actor = actor_from_scope(agent, scope, thread.thread_id, task_id)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": "隔离宿主资料验证"},
                     host_channel_actor=actor)
    agent._current_run_params = RunParams(request_id=task_id, task_id=task_id, run_id=task_id,
        task_attributes={"conversation_thread_id": thread.thread_id}, host_channel_actor=actor)
    return thread, actor
