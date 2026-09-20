# LLM: ConversationThread.model_profile_id 是会话选择唯一权威；这里不保存模型连接、密钥或第二份会话索引。
# 模块用途: 将用户默认模型一次性绑定到会话，校验 owner 后在工作片边界解析私有配置。

from __future__ import annotations

from dataclasses import replace

from ..backends.errors import ModelNotConfiguredError
from .model_profiles import (
    ModelProfileError,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)


# LLM: 默认引用只用于新会话初始化或旧记录首次显式迁移；读取没有网络副作用。
# 函数用途: 取得当前用户为未来新会话选择的模型编号。
def default_model_profile_id(agent: object) -> str:
    return read_model_profiles(model_profiles_path(agent.home_paths))["selected"]


# LLM: 本地 TUI 复用 chat/local-agent 的正式通道身份；必须有真实 store，不能退回 owner 全局选择。
# 函数用途: 用本地 session_id 进入与 Gateway 相同的会话模型管理逻辑。
def execute_local_model_operation(agent: object, session_id: str, operation: str, payload: dict) -> dict:
    from ..conversation.channels import LOCAL_AGENT_USER_ID, LOCAL_CHAT_CHANNEL
    from .model_profiles import execute_model_profile_operation

    store = getattr(agent, "conversation_store", None)
    if store is None or not str(session_id).strip():
        raise ModelProfileError("模型选择需要可用的本地会话存储与会话编号。")
    home = agent.home_paths
    thread = store.get_or_create_thread({
        "canonical_user_id": LOCAL_AGENT_USER_ID, "channel": LOCAL_CHAT_CHANNEL,
        "channel_conversation_id": session_id, "channel_user_id": LOCAL_AGENT_USER_ID,
        "owner_id": getattr(home, "owner_id", ""),
        "owner_home": str(getattr(home, "owner_home_dir", "")), "title": "会话设置",
    })
    return execute_model_profile_operation(agent, operation, payload, thread_id=thread.thread_id)


# LLM: 调用方必须先用认证 channel binding 解析 thread；再次校验宿主 owner 防止误传其他用户的 store/thread。
# 函数用途: 拒绝跨用户会话引用；旧记录缺 owner 字段保持由已认证存储边界约束。
def _require_owner(agent: object, thread: object) -> None:
    home = agent.home_paths
    for field, expected in (("owner_id", getattr(home, "owner_id", "")),
                            ("owner_home", str(getattr(home, "owner_home_dir", "")))):
        actual = str(getattr(thread, field, "") or "")
        if actual and expected and actual != str(expected):
            raise ModelProfileError("当前模型选择不能访问其他用户的会话。")


# LLM: 所有更新走 canonical thread 文件原子合并；不会覆盖并发 Compact/history 状态，也不热改运行中的配置快照。
# 函数用途: 读取已固定模型；首次迁移只填空，显式选择才替换当前会话并清除旧模型展示校准。
def thread_model_profile_id(agent: object, thread_id: str, *, select: str | None = None) -> str:
    store = agent.conversation_store
    thread = store.load_thread(thread_id)
    if thread is None:
        raise ModelProfileError("当前会话不存在，请重新打开会话。")
    _require_owner(agent, thread)
    if select is None and thread.model_profile_id:
        return thread.model_profile_id
    selected = default_model_profile_id(agent) if select is None else select
    # 显式选择必须先验证；旧记录只迁移编号，原共享被撤销时仍需能打开菜单另选，而实际执行会拒绝。
    if select is not None:
        selected_model_config(agent, profile_id=selected)

    # LLM: 更新函数在最新 thread 锁内运行；旧记录初始化不得覆盖另一个窗口刚提交的显式选择。
    # 函数用途: 原子保存选择，保留压缩与消息游标；只在实际换模型时清理失效的数值投影。
    def update(latest):
        _require_owner(agent, latest)
        if select is None and latest.model_profile_id:
            return latest
        if latest.model_profile_id == selected:
            return latest
        return replace(latest, model_profile_id=selected, provider_context_observation={}, model_context_usage={})

    return store._update_thread_atomic(thread_id, update).model_profile_id


# LLM: 每工作片只解析一次配置引用，后续 scope 绑定不可变对象；不临时修改 owner selected 来模拟会话切换。
# 函数用途: 为前台、后台、恢复和手动 Compact 取得同一个会话选定的模型配置。
def thread_model_config(agent: object, thread_id: str):
    return selected_model_config(agent, profile_id=thread_model_profile_id(agent, thread_id))


# LLM: 仅 typed 本地配置/引用异常需要等待配置；远端 4xx、额度和任务阻塞不能由此解除或吞掉。
# 函数用途: 让 Goal 错误处理和 Gateway 重试统一识别缺模型，不用异常正文猜测恢复条件。
def is_model_configuration_unavailable(error: BaseException) -> bool:
    return isinstance(error, (ModelNotConfiguredError, ModelProfileError))


# LLM: 仅供已因模型配置失败的后台车道复查；复用 owner 验证及模型引用，旧空引用沿原入口一次迁移。
# 函数用途: 用户修改模型后判断是否可恢复后台工作；不读其它会话选择，不构造后端或发送网络请求。
def thread_model_is_configured(agent: object, thread_id: str) -> bool:
    from ..backends.factory import model_configuration_missing

    try:
        config = thread_model_config(agent, thread_id)
    except ModelProfileError:
        return False
    return not model_configuration_missing(config.model_backend, config)
