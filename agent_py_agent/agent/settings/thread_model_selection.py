# LLM: ConversationThread.model_profile_id 是有效模型唯一权威；显式选择连同单调版本和 pending 终态原子写入，不授予自动采用权限。
# 模块用途: 管理会话模型与覆盖事实，同值选择也记录新版本；不保存连接、永久 pin 或第二份选择索引。

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from ..backends.errors import ModelNotConfiguredError
from .model_profiles import (
    ModelProfileError,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from .model_provider_schema import ModelProfileGeneration

SUBAGENT_MODEL_ADVICE_KEY = "host_subagent_model_advice.v1"


# LLM: 仅宿主创建准备载体可传递；无响应正文、时钟、连接摘要或授权。序列化只初始化新 thread，不能从 task attributes 反序列化。
# 类用途: 保存一条待首轮验证的模型建议及稳定来源，任何字段都不代表窗口或工具能力已通过。
@dataclass(frozen=True)
class PendingSubagentModelAdvice:
    profile_id: str
    operation_id: str
    source_owner_ref: str
    source_thread_id: str
    source_run_id: str
    source_task_id: str
    source_owner_revision: int
    source_thread_revision: int
    child_run_id: str
    child_thread_id: str
    source_model_generation: ModelProfileGeneration | None = None

    # LLM: 精确 child 身份必须与物化请求一致；只输出稳定公开字段，schema/status 由宿主固定，不接受调用方覆盖。
    # 函数用途: 将内存建议转换成新线程的 pending 元数据；错误身份在任何建议写入前拒绝。
    def pending_metadata(self, *, run_id: str, thread_id: str) -> dict:
        from .decision_settings_schema import profile_reference

        values = asdict(self)
        revisions = ("source_owner_revision", "source_thread_revision")
        if any(type(values[key]) is not int or values[key] < 0 for key in revisions):
            raise ModelProfileError("子代理模型建议的设置版本无效。")
        if any(type(value) is not str for key, value in values.items() if key not in (*revisions, "source_model_generation")):
            raise ModelProfileError("子代理模型建议的来源身份无效。")
        if (not self.profile_id or not self.operation_id or not self.source_owner_ref or not self.source_thread_id
                or not run_id or not thread_id or (self.child_run_id, self.child_thread_id) != (run_id, thread_id)):
            raise ModelProfileError("子代理模型建议不能绑定其他运行或线程。")
        profile_reference(self.profile_id)
        generation = self.source_model_generation
        if generation is not None and (not isinstance(generation, ModelProfileGeneration) or generation.profile_id != self.profile_id):
            raise ModelProfileError("子代理模型建议的目录版本不匹配。")
        values["source_model_generation"] = generation.to_dict() if generation is not None else None
        return {"schema": SUBAGENT_MODEL_ADVICE_KEY, "status": "pending", **values}


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
    thread = store.threads.get_or_create({
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


# LLM: 显式同值选择也在 canonical thread 原子前进版本并终结 pending；旧空引用初始化不是旧手动事件，不热改运行片。
# 函数用途: 读取或显式设置模型与覆盖事实；只在实际换模型时清除校准，不增加自动采用或确认流程。
def thread_model_profile_id(agent: object, thread_id: str, *, select: str | None = None) -> str:
    store = agent.conversation_store
    thread = store.threads.load(thread_id)
    if thread is None:
        raise ModelProfileError("当前会话不存在，请重新打开会话。")
    _require_owner(agent, thread)
    if select is None and thread.model_profile_id:
        return thread.model_profile_id
    selected = default_model_profile_id(agent) if select is None else select
    # 显式选择必须先验证；旧记录只迁移编号，原共享被撤销时仍需能打开菜单另选，而实际执行会拒绝。
    if select is not None:
        selected_model_config(agent, profile_id=selected)

    # LLM: 原 thread 锁内从最新版本递增，与 pending 终结同次提交；并发旧读不能吞掉同值显式事件或覆盖其它状态。
    # 函数用途: 原子保存选择版本和建议失效原因，保留压缩、消息游标以及同模型校准。
    def update(latest):
        _require_owner(agent, latest)
        if select is None and latest.model_profile_id:
            return latest
        advice = latest.metadata.get(SUBAGENT_MODEL_ADVICE_KEY)
        if select is not None and isinstance(advice, dict) and advice.get("status") == "pending":
            latest = replace(latest, metadata={**latest.metadata, SUBAGENT_MODEL_ADVICE_KEY: {
                **advice, "status": "retained", "reason": "explicit_model_selection",
            }})
        revision = latest.model_selection_revision + 1
        changed = latest.model_profile_id != selected
        return replace(
            latest, model_profile_id=selected, model_selection_revision=revision,
            model_selection_source="explicit" if select is not None else "default",
            model_selection_last_explicit_revision=(revision if select is not None else latest.model_selection_last_explicit_revision),
            provider_context_observation={} if changed else latest.provider_context_observation,
            model_context_usage={} if changed else latest.model_context_usage,
        )

    return store.threads.update_atomic(thread_id, update).model_profile_id


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
