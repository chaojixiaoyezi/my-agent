# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: SubAgentIndexingMixin - thin facade delegating indexing service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/indexing.py。
"""

from .services.indexing import SubAgentIndexingService
from .services.indexing_params import (
    DataclassRecordIndexParams,
    IndexReportParams,
    LocalRecordParams,
)


# LLM: SubAgentIndexingMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent索引混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentIndexingMixin:
    """Thin facade delegating indexing and event logging to SubAgentIndexingService."""

    # LLM: _indexing_service 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理索引服务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    @property
    def _indexing_service(self):
        if "_indexing_service" not in self.__dict__:
            self.__dict__["_indexing_service"] = SubAgentIndexingService(self)
        return self.__dict__["_indexing_service"]

    # LLM: _log_local_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入local记录的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _log_local_record(
        self,
        *,
        params: LocalRecordParams | None = None,
        source_type: str = "",
        source_id: str = "",
        title: str = "",
        content: str = "",
        event_type: str = "",
        metadata: dict[str, object] | None = None,
    ):
        params = params or LocalRecordParams(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            event_type=event_type,
            metadata=metadata,
        )
        return self._indexing_service.log_local_record(params=params)

    # LLM: _index_task 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index任务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _index_task(self, task):
        return self._indexing_service.index_task(task)

    # LLM: _index_report 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index报告相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _index_report(self, params: IndexReportParams):
        return self._indexing_service.index_report(params)

    # LLM: _index_action_apply 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index动作应用相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
    def _index_action_apply(self, record):
        return self._indexing_service.index_action_apply(record)

    # LLM: _index_capability_route 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index能力route相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _index_capability_route(self, record):
        return self._indexing_service.index_capability_route(record)

    # LLM: _index_acceptance_review 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index验收审查相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _index_acceptance_review(self, record):
        return self._indexing_service.index_acceptance_review(record)

    # LLM: _index_patch_review 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index补丁审查相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _index_patch_review(self, record):
        return self._indexing_service.index_patch_review(record)

    # LLM: _index_dispatch_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index调度记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _index_dispatch_record(self, record):
        return self._indexing_service.index_dispatch_record(record)

    # LLM: _index_dispatch_watch_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index调度监控记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _index_dispatch_watch_record(self, record):
        return self._indexing_service.index_dispatch_watch_record(record)

    # LLM: _index_parent_planner_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index父级规划器记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _index_parent_planner_record(self, record):
        return self._indexing_service.index_parent_planner_record(record)

    # LLM: _index_execution_context 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理indexexecution上下文相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _index_execution_context(self, context):
        return self._indexing_service.index_execution_context(context)

    # LLM: _index_runner_result 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index执行器结果相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def _index_runner_result(self, result, output_payload):
        return self._indexing_service.index_runner_result(result, output_payload)

    # LLM: _index_channel_probe 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index通道probe相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _index_channel_probe(self, result):
        return self._indexing_service.index_channel_probe(result)

    # LLM: _index_dataclass_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理indexdataclass记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _index_dataclass_record(self, params: DataclassRecordIndexParams):
        return self._indexing_service._index_dataclass_record(params)

    # LLM: _select_runs 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询runs需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def _select_runs(self, run_ids):
        return self._indexing_service.select_runs(run_ids)

    # LLM: select_runs 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 读取或查询runs需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
    def select_runs(self, run_ids):
        return self._indexing_service.select_runs(run_ids)

    # LLM: index_task 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index任务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def index_task(self, task):
        return self._indexing_service.index_task(task)

    # LLM: index_dispatch_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index调度记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def index_dispatch_record(self, record):
        return self._indexing_service.index_dispatch_record(record)

    # LLM: index_dispatch_watch_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index调度监控记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def index_dispatch_watch_record(self, record):
        return self._indexing_service.index_dispatch_watch_record(record)

    # LLM: index_parent_planner_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index父级规划器记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def index_parent_planner_record(self, record):
        return self._indexing_service.index_parent_planner_record(record)

    # LLM: index_execution_context 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理indexexecution上下文相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def index_execution_context(self, context):
        return self._indexing_service.index_execution_context(context)

    # LLM: index_report 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理index报告相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def index_report(self, params: IndexReportParams):
        return self._indexing_service.index_report(params)

    # LLM: log_local_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入local记录的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def log_local_record(
        self,
        *,
        params: LocalRecordParams | None = None,
        source_type: str = "",
        source_id: str = "",
        title: str = "",
        content: str = "",
        event_type: str = "",
        metadata: dict[str, object] | None = None,
    ):
        params = params or LocalRecordParams(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            event_type=event_type,
            metadata=metadata,
        )
        return self._indexing_service.log_local_record(params=params)
