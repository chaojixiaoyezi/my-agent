# LLM: 该模块只消费已脱敏的 Gateway typed chunk，发布到前后台共用的 owner/thread main 标量。
# request/thread/task 绑定来自持锁请求对象，等待审批只跟踪显式 permission ID；不触碰正文、模型缓存或执行权。
# 模块用途: 把前台真实工作阶段和上下文数字同步给同会话的其他窗口，避免显示旧后台等待状态。

from __future__ import annotations

import time
from uuid import uuid4

from ..conversation.agent_activity import MainActivitySource, publish_main_activity


# LLM: 一个实例只属于一次已获会话车道的请求；绑定冲突时不发布，不猜另一个 task/thread。
# 类用途: 接收前台公开事件，把数值、工作阶段和审批等待送到已有 main 状态表。
class GatewayMainActivitySink:
    # LLM: request 是宿主 task-binding writer 更新的同一个字典，不能复制后冻结旧 task。
    # 函数用途: 绑定真实 owner、会话和请求，初始化本工作片的审批显示集合并发布准备阶段。
    def __init__(self, agent: object, *, thread_id: str, request_id: str, request: dict, task_id: str = "") -> None:
        self.agent = agent
        self.thread_id = thread_id
        self.request_id = request_id
        self.request = request
        self.task_id = task_id
        self.projection_id = f"gateway:{request_id}:{uuid4().hex}"
        self.started_at = time.time()
        self.closed = False
        self._pending_permissions: set[str] = set()
        self._publish("running", "整理任务上下文", begin=True)

    # LLM: 只读显式事件 kind/结构化工具阶段；任意模型正文与错误字符串均不能成为状态依据。
    # 函数用途: 在原 chunk 已发出的边界同步工作及审批等待；无关审批回执不能清掉当前等待。
    def __call__(self, event: dict[str, object]) -> None:
        if self.closed:
            return
        kind = event.get("kind")
        if kind in {"permission_requested", "permission_resolved"}:
            value = event.get("permission") if kind == "permission_requested" else event
            permission_id = value.get("permission_id") if isinstance(value, dict) else None
            if not isinstance(permission_id, str) or not permission_id.strip():
                return
            if kind == "permission_requested":
                self._pending_permissions.add(permission_id)
            elif permission_id in self._pending_permissions:
                self._pending_permissions.remove(permission_id)
            else:
                return
            self._publish("running", "继续当前任务")
            return
        if kind == "context_usage_updated":
            self._publish(context_usage=event.get("context_usage"))
            return
        if kind in {"thinking_delta", "assistant_thinking", "tool_input_progress"}:
            self._publish("thinking", "思考中" if kind != "tool_input_progress" else "正在准备工具参数")
            return
        if kind in {"model_delta", "assistant_commentary"}:
            self._publish("responding", "正在生成回复")
            return
        if kind == "tool_progress" and isinstance(event.get("progress"), dict):
            progress = event["progress"]
            tool = str(progress.get("tool") or "工具")[:80]
            phase = str(progress.get("phase") or "")
            terminal = phase in {"finished", "completed", "succeeded", "failed"}
            self._publish("tool", f"{'已完成' if terminal else '正在使用'} {tool}")
            return
        if kind == "runtime_progress" and isinstance(event.get("retry"), dict):
            self._publish("retrying", "正在重连模型服务")
            return
        if kind in {"context_window_compacted", "conversation_compacted"}:
            self._publish("running", "继续当前任务")
            return
        if kind == "conversation_compaction_progress" and isinstance(event.get("compact_progress"), dict):
            phase = event["compact_progress"].get("phase")
            if phase in {"completed", "superseded", "failed"}:
                self._publish("running", "继续当前任务")
            else:
                self._publish("compacting", "正在压缩会话上下文")

    # LLM: close 只表示该请求不再有前台流；真正成功/失败及子代理等待仍从耐久账本读取。
    # 函数用途: 前台退出时释放审批显示集合并停止工作动画，不修改真正审批或任务终态。
    def close(self) -> None:
        if not self.closed:
            self._pending_permissions.clear()
            self._publish("waiting", "等待后续事件")
            self.closed = True

    # LLM: conversation_runtime 是唯一展示 task 绑定，不能用 RuntimeDB 执行 task 替代它；冲突拒绝投影。
    # 函数用途: 跟随真实任务晋升写共用状态；审批未结束时继续同步数值但不被工具/模型事件覆盖等待。
    def _publish(self, phase: str = "", activity: str = "", *, context_usage: object = None, begin: bool = False) -> None:
        binding = self.request.get("conversation_runtime")
        if binding is not None:
            if not isinstance(binding, dict) or binding.get("thread_id") != self.thread_id or binding.get("request_id") != self.request_id:
                return
            self.task_id = str(binding.get("task_id") or "")
        if self._pending_permissions:
            phase, activity = "waiting_permission", "等待工具审批"
        publish_main_activity(
            self.agent, MainActivitySource(self.thread_id, self.task_id, self.started_at, self.projection_id),
            phase=phase, activity=activity, context_usage=context_usage, begin=begin,
        )
