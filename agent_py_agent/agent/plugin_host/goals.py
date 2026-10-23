# LLM: Goal只控制后续调度，不拥有当前任务资源；全部写入复用原GoalStore转换锁和回执。
# 模块用途: 适配插件Goal列表、详情和有限控制动作。
from __future__ import annotations

from ..gateway_parts.goal_control_service import (
    PluginGoalControlRequest,
    execute_plugin_goal_control,
)
from .contracts import DomainResult
from .errors import HostAPIError
from .tasks import checked, context_for, finish, require_task, require_thread, resource_id


# LLM: 公开字段只来自原记录，不导出metadata、路径或调度身份；不刷新clock基线。
# 函数用途: 投影原Goal的内容版本和用量。
def project_goal(goal):
    return {"goal_id": goal.goal_id, "task_id": goal.task_id, "thread_id": goal.thread_id,
            "name": goal.name, "objective": goal.objective, "status": goal.status, "revision": goal.revision,
            "token_budget": goal.token_budget, "tokens_used": goal.tokens_used,
            "duration_seconds": goal.duration_seconds, "time_used_seconds": goal.time_used_seconds}


# LLM: 生命周期回调由受限宿主提供，不得使用main invoker代替。
# 类用途: 对外提供七个Goal动作。
class GoalAdapter:
    # LLM: 只保存宿主依赖，构造不创建owner或Goal。
    # 函数用途: 接入当前授权和原Goal服务。
    def __init__(self, services, lifecycle):
        self.services, self.lifecycle = services, lifecycle

    # LLM: 读用list_report而非会启动时钟的load/list；写用原receipt和原Goal服务，重放也复核当前授权。
    # 函数用途: 读取目标或受理一次有限控制。
    def call(self, authorized):
        payload = checked(self.services, authorized, "goals.")
        context = context_for(self.services, authorized)
        require_thread(context, authorized, payload["thread_id"])
        if "goal_id" in payload:
            resource_id(payload["goal_id"])
        if authorized.call.action in {"goals.list", "goals.get"}:
            return self._read(authorized, context, payload)
        result = self.services.once(authorized, lambda: self._write(authorized, context, payload))
        return finish(self.services, authorized, result.http_status, result.data, outcome="committed")

    # LLM: thread范围不隐含已有Goal所属task范围，坏账返回结构化不可用而不是空结果。
    # 函数用途: 返回精确目标或安全分页列表。
    def _read(self, authorized, context, payload):
        goals, error = context.store.goals.list_report(payload["thread_id"])
        if error is not None:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        visible = []
        for goal in goals:
            if goal.thread_id != payload["thread_id"] or not context.allows(authorized, "task", goal.task_id):
                continue
            task = require_task(context, authorized, goal.task_id)
            if task.thread_id == goal.thread_id:
                visible.append(project_goal(goal))
        if authorized.call.action == "goals.list":
            return finish(self.services, authorized, 200, self.services.page(authorized, visible))
        selected = next((row for row in visible if row["goal_id"] == payload["goal_id"]), None)
        if selected is None:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        return finish(self.services, authorized, 200, selected)

    # LLM: 在原Goal锁内重新核实时权限及精确task/thread，不允许新绑定或旧grant混入提交。
    # 函数用途: 把授权回调和结构化参数交给原Goal控制服务。
    def _write(self, authorized, context, payload):
        # LLM: 该闭包只由宿主原状态机调用，不从插件参数接受任意选择器。
        # 函数用途: 末核当前线程和选中目标的任务归属。
        def revalidate(goal):
            checked(self.services, authorized, "goals.")
            require_thread(context, authorized, payload["thread_id"])
            if goal is not None:
                task = require_task(context, authorized, goal.task_id)
                if task.thread_id != payload["thread_id"] or goal.thread_id != task.thread_id:
                    raise HostAPIError("RESOURCE_NOT_FOUND", 404)

        result = execute_plugin_goal_control(PluginGoalControlRequest(
            context.store, authorized, payload, revalidate, self.lifecycle))
        status = 201 if authorized.call.action == "goals.create" else 200
        data = result if authorized.call.action == "goals.clear" else project_goal(result)
        return DomainResult(status, data)
