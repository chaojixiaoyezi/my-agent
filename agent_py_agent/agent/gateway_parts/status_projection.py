# LLM: /status 显示只读取 TUI 同源上下文快照与可信 owner 权限；不读取正文，不生成模型请求或写计量。
# 模块用途: 将同源会话领域事实投影为 Gateway/本地状态页，展示可信角色、权限和上下文，未知不补零。
from __future__ import annotations

from ..conversation.context_usage import context_usage_from_thread
from ..conversation.im_task_status import im_task_status_facts


# LLM: 管理员只认可信 home 的 local/main 规范键（与 OwnerIdentity 的标量 ID 不混用）；未知身份明确展示未知，不把 UI 入口当授权事实。
# 函数用途: 共用会话领域只读任务投影生成状态字段，不在查询时重算历史或写执行状态。
def status_display_facts(agent: object, thread: object = None, *, scope: object = None) -> dict[str, object]:
    if scope is not None:
        try:
            thread = agent.conversation_store.threads.resolve(
                channel=scope.channel, channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id
            )
        except (AttributeError, OSError, TypeError, ValueError):
            thread = None
    home = getattr(agent, "home_paths", None)
    key = tuple(str(getattr(home, name, "") or "") for name in ("owner_provider", "owner_kind", "owner_id"))
    role = "管理员" if key == ("local", "main", "local/main") else "普通用户" if all(key) else "未知"
    config = getattr(agent, "config", None)
    return {"user_role": role,
            "permission_mode": f"文件 {getattr(config, 'path_access_mode', '未知')}；Shell {getattr(config, 'access_mode', '未知')}",
            "context_usage": context_usage_from_thread(thread), "im_task_status": im_task_status_facts(agent, thread, scope)}
