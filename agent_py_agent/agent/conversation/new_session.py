# LLM: /new 的领域回执只消费原线程继任事务；不能停止资源、改权限或把旧历史复制到新模型上下文。
# 模块用途: 为CLI、Gateway与Web生成同一份原地新会话结构化结果。
from __future__ import annotations

from .control_commands import ConversationControlResult


# LLM: 调用者先核当前owner及前台空闲；只有原store事务成功才回新线程，后台运行归属保持旧线程不变。
# 函数用途: 创建空白后继并清楚说明历史、目录和后台继续运行，失败返回稳定错误码。
def new_session_result(store, thread_id, *, title="", binding=None):
    try:
        new = store.threads.succeed(thread_id, title=title, binding=binding)
        return ConversationControlResult("new", True,
            f"旧会话 {thread_id} 已归档到历史；新会话 {new.thread_id} 已在当前目录启用。"
            "后台任务、托管进程和监控继续运行，同通道通知将跟随新会话。",
            details={"thread_id": new.thread_id, "predecessor_thread_id": thread_id,
                     "workspace_root": new.workspace_root, "cwd": new.cwd,
                     "background_tasks_continued": True, "view_reset": True})
    except (ValueError, KeyError) as exc:
        return ConversationControlResult("new", False, "新会话未创建，原会话和后台任务均保留。",
                                         error_code=str(exc))
