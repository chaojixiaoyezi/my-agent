# LLM: 原任务、Goal和卡片仍是唯一权威；执行取消只请求，不伪造进程停止。
# 模块用途: 为生产D1组合提供原进度、调度及真人交互端口。
from __future__ import annotations

from pathlib import Path

from .contracts import resolve_read_context
from .errors import HostAPIError
from .gateway_runtime import runtime_rows
from .schemas import strict_json


# LLM: 只有可信宿主UI能提供人工证明；插件token或任意字典不是证明，不创建自动批准路径。
# 类用途: 桥接原D1所需的有限副作用与原卡元数据读取。
class GatewayOperations:
    # LLM: 保存原资源，不构造Agent、模型或执行器。
    # 函数用途: 绑定同一个真实授权服务。
    def __init__(self, services):
        self.services = services

    # LLM: 只读TaskLink提供的规范业务路径；未保存清单为真实空清单，坏JSON明确失败。
    # 函数用途: 从原任务进度文件读取已记录待办。
    def progress(self, authorized, task):
        from .memory import _owner_bytes
        context = resolve_read_context(authorized)
        if not task.task_path:
            return []
        path = Path(task.task_path) / "task_progress.json"
        if not path.is_absolute() or not path.is_relative_to(context.owner_home.home_dir):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        raw = _owner_bytes(context, path)
        return strict_json(raw).get("items", []) if raw else []

    # LLM: 只消费原后代关系，不把同线程所有任务误当子任务；本代原TaskLink无parent字段时没有可证明后代。
    # 函数用途: 返回原任务记录能证明的后代ID。
    def descendants(self, authorized, task):
        context = self.services.resolve(authorized)
        rows, errors = context.store.tasks.list_report(task.thread_id)
        if errors:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        return [row.task_id for row in rows if getattr(row, "parent_task_id", None) == task.task_id]

    # LLM: 不改任务终态或清Goal；原claim/运行过程自行观察取消，返回资源停止未知。
    # 函数用途: 在原任务短锁内受理精确取消请求。
    def cancel(self, authorized, tasks, reason):
        from ..gateway_parts.io import update_json_file_atomic
        context = self.services.resolve(authorized)
        read = resolve_read_context(authorized)
        paths = self.services.resources.paths_for(read)
        for task in tasks:
            self.services.revalidate(authorized)
            path = context.store.storage.background_claim_path(task.thread_id)
            # 取消标记留在原claim，不以active TaskLink推断有可杀进程。
            if path.exists():
                update_json_file_atomic(path, lambda row: {**row, "cancel_requested": True,
                    "cancel_reason": reason, "cancel_task_id": task.task_id})
            _cancel_task_gateway_requests(read, paths, task)
        return {"cancel_requested": True, "resources_stopped": None}

    # LLM: 调度准入只允许原零工具Goal合同，不能用main的工具目录执行；当前来源仍经A末核。
    # 函数用途: 核验Goal创建或恢复资格。
    def check(self, authorized, current, operation):
        self.services.revalidate(authorized)
        if current is not None and current.metadata.get("plugin_origin", {}).get("installation_ref") != authorized.binding.installation_ref:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)

    # LLM: 原Goal已提交，绑定其唯一任务；不复制目标记录或扩大目标权限。
    # 函数用途: 为新插件Goal建立原任务关联。
    def created(self, authorized, goal):
        context = self.services.resolve(authorized)
        context.store.tasks.bind({"thread_id": goal.thread_id, "task_id": goal.task_id, "goal": goal.objective,
            "work_kind": "goal", "work_name": goal.name})
        self.resumed(authorized, goal)

    # LLM: 只更新同一TaskLink目标，不重置运行状态、用量或预算。
    # 函数用途: 同步原Goal变更到原任务。
    def updated(self, authorized, goal):
        self.services.resolve(authorized).store.tasks.update_goal({"task_id": goal.task_id, "goal": goal.objective})

    # LLM: 唤醒携原Goal和插件来源，不能把暂停/清除转换为资源取消。
    # 函数用途: 向原唤醒队列提交准确的后续调度信号。
    def resumed(self, authorized, goal):
        context = self.services.resolve(authorized)
        context.store.wakes.raise_signal({"thread_id": goal.thread_id, "task_id": goal.task_id,
            "kind": "goal_continue", "source": "plugin_control", "dedupe_key": f"{goal.goal_id}:{goal.revision}",
            "metadata": {"plugin_origin": goal.metadata["plugin_origin"], "tool_policy": "none"}})

    # LLM: 不把插件传入字符串、dict或自己的token当真人；UI签票只走另一个宿主认证端口。
    # 函数用途: 拒绝未由宿主真人入口认证的票据签发。
    def verify_human(self, proof, authorized, interaction, decision_payload):
        raise HostAPIError("HUMAN_TICKET_REQUIRED", 403)

    # LLM: 公开列表只读原卡；每张卡仍由原服务核有效期/claim，不续租或导出秘密。
    # 函数用途: 读取当前线程可见的原人工待办。
    def pending(self, authorized, thread_id):
        from .gateway_pending import pending_interactions
        return pending_interactions(self.services, authorized, thread_id)

    # LLM: 此方法仅接内部HumanResponse，原人工票据不含决定时不能自行补同意。
    # 函数用途: 将宿主签出的准确决定提交原卡服务。
    def respond(self, authorized, ticket):
        from .gateway_pending import respond_interaction
        return respond_interaction(self.services, authorized, ticket)


# LLM: 仅扫描同owner原inbox/processing目录，匹配目标任务或线程时进入单请求转换锁标记取消。
# 函数用途: 将任务取消请求同步到当前关联的Gateway请求记录。
def _cancel_task_gateway_requests(read, paths, task) -> None:
    for folder in (paths.inbox, paths.processing):
        for row in runtime_rows(read.owner_home, folder):
            _mark_request_cancelled(paths, folder, row, task)


# LLM: 在gateway_input_transition锁内原子写入cancel_requested=True，不伪造进程终止。
# 函数用途: 当请求属于目标线程或任务时写入取消请求标记。
def _mark_request_cancelled(paths, folder: Path, row: dict, task) -> None:
    from ..gateway_parts import input_delivery_service as inputs
    from ..gateway_parts.io import update_json_file_atomic

    rid = row.get("id")
    if not isinstance(rid, str):
        return
    tid = (row.get("conversation") or {}).get("thread_id") or (
        ((row.get("metadata") or {}).get("plugin_provenance") or {}).get("thread_id")
    )
    if tid != task.thread_id and f"gateway:{rid}" != task.task_id:
        return
    with inputs.gateway_input_transition(paths, rid):
        req_path = folder / f"{rid}.json"
        if req_path.exists():
            update_json_file_atomic(req_path, lambda r: {**r, "cancel_requested": True})
