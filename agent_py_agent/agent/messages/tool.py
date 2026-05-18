from __future__ import annotations

# LLM: MessageTool is the high-level internal messaging facade used by runtime code.
# 模块用途: 封装内部消息发送、读取、确认、进度通知和用户输入请求。
from .models import MessageCard, MessageTarget
from .store import MessageStore


 # LLM: MessageTool exposes structured messaging operations without leaking store paths.
 # 类用途: 给 runtime 和桥接层提供内部消息 API。
class MessageTool:
    # LLM: MessageTool.__init__ stores the underlying MessageStore.
    # 函数用途: 初始化消息工具依赖。
    def __init__(self, store: MessageStore):
        self.store = store

    # LLM: MessageTool.send_message sends a structured internal message.
    # 函数用途: 发送普通内部消息，可关联任务和幂等 key。
    def send_message(
        self,
        *,
        sender: MessageTarget,
        target: MessageTarget,
        content: str,
        task_id: str | None = None,
        message_type: str = "text",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> MessageCard:
        return self.store.send(
            sender=sender,
            target=target,
            content=content,
            task_id=task_id,
            message_type=message_type,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    # LLM: MessageTool.read_inbox reads messages for a structured target.
    # 函数用途: 读取 session/user/task 等目标的 inbox。
    def read_inbox(self, target: MessageTarget, *, unread_only: bool = True) -> list[MessageCard]:
        return self.store.read_inbox(target, unread_only=unread_only)

    # LLM: MessageTool.ack_message marks one inbox delivery as read.
    # 函数用途: 确认指定目标已读某条消息。
    def ack_message(self, target: MessageTarget, message_id: str) -> bool:
        return self.store.ack(target, message_id)

    # LLM: MessageTool.broadcast_progress creates a progress message for a task route.
    # 函数用途: 向任务通知路线发送结构化进度消息。
    def broadcast_progress(self, *, task_id: str, route_target: MessageTarget, content: str) -> MessageCard:
        return self.send_message(
            sender=MessageTarget(kind="task", identifier=task_id),
            target=route_target,
            content=content,
            task_id=task_id,
            message_type="progress",
            idempotency_key=f"{task_id}:progress:{content}",
            metadata={"task_id": task_id},
        )

    # LLM: MessageTool.send_need_user_input routes backend blockers to the frontstage session.
    # 函数用途: 让后台任务通过前台 session 向用户请求确认。
    def send_need_user_input(self, *, task_id: str, session_id: str, prompt: str) -> MessageCard:
        return self.send_message(
            sender=MessageTarget(kind="task", identifier=task_id),
            target=MessageTarget(kind="session", identifier=session_id),
            content=prompt,
            task_id=task_id,
            message_type="need_user_input",
            idempotency_key=f"{task_id}:need_user_input:{prompt}",
            metadata={"task_id": task_id, "requires_frontstage_agent": True},
        )
