# LLM: 释放核验只读原 enable 身份、executor 与 ProcessSessionStore；不以取消状态或进程缺失猜清理，也不修改原 UNKNOWN。
# 模块用途: 确认旧准备执行器和同代资源确实收尾，为删除环境与安装表释放提供原生证据。

from __future__ import annotations

import math
from dataclasses import asdict

from .common.strict_json import load_strict_json
from .plugin_activation import PLUGIN_ENABLE_TOOL
from .plugin_installation import PluginInstallationError
from .runtime_db.executor_liveness import executor_exit_reason
from .runtime_db.managed_operation_store import ManagedOperationStore
from .runtime_db.operations import RuntimeConflictError
from .tooling.process_cleanup_evidence import process_cleanup_reference
from .tooling.process_scope import ProcessActivationScope, ProcessExecutionScope
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root


# LLM: 只查计划保存的原 operation；原请求类型和完整 claim 归属须一致，不能按插件名扫描运行或补 current。
# 函数用途: 找回创建此候选环境的首次管理运行，坏账时拒绝代替它清理别的任务。
def preparation_binding(repository, owner_id: str, plan):
    binding = repository.find_host_command_by_operation(owner_id=owner_id, operation_id=plan.operation_id)
    if binding is None or binding.request.command_name != PLUGIN_ENABLE_TOOL:
        raise PluginInstallationError("preparation_owner_unconfirmed", "插件准备操作的归属尚未确认。")
    claim = ManagedOperationStore(repository).get_tool_operation(
        owner_id=owner_id, run_id=binding.run_id, task_id=binding.task_id,
        attempt_id=binding.attempt_id, operation_id=plan.operation_id,
        tool_name=PLUGIN_ENABLE_TOOL, args_hash="sha256:" + binding.request.input_digest,
        resource_scopes=(plan.resource_scope,),
    )
    if claim is None:
        raise RuntimeConflictError("插件准备操作缺少原领取记录")
    return binding


# LLM: find_host_command 已校验首次运行链；出生/退出时间严格拒绝 bool/NaN/文本，原 attempt 缺失身份不可走 runner 回退。
# 函数用途: 核验原准备函数已经返回或原宿主确实死亡，保留可引用的原执行器证据。
def preparation_exit(repository, binding) -> dict:
    attempt = repository.get_attempt(binding.attempt_id)
    if attempt is None or attempt["agent_run_id"] != binding.agent_run_id:
        raise PluginInstallationError("preparation_owner_unconfirmed", "插件准备尝试的归属尚未确认。")
    metadata = load_strict_json(attempt["metadata_json"] or "{}")
    executor = metadata.get("executor")
    keys = {"schema_version", "token", "process_epoch", "pid", "start_time", "status", "started_at"}
    if (not isinstance(executor, dict) or executor.get("schema_version") != "attempt-executor.v1"
            or set(executor) != keys | ({"ended_at"} if executor.get("status") == "exited" else set())
            or not isinstance(executor.get("token"), str) or not executor["token"]
            or not isinstance(executor.get("process_epoch"), str) or not executor["process_epoch"]
            or type(executor.get("pid")) is not int or executor["pid"] <= 0
            or executor.get("status") not in {"running", "exited"}
            or not _valid_time(executor.get("started_at"))
            or (executor.get("start_time") is not None and not _valid_time(executor["start_time"]))
            or (executor.get("status") == "exited" and not _valid_time(executor.get("ended_at")))):
        raise PluginInstallationError("preparation_executor_missing", "插件准备执行器缺少可信退出身份。")
    reason = executor_exit_reason(metadata)
    if not reason:
        raise PluginInstallationError("preparation_executor_unconfirmed", "原准备执行器尚未确认退出，环境暂不删除。")
    return {"attempt_id": binding.attempt_id, "reason": reason, "executor": executor}


# LLM: 原生时间为有限正数；布尔和可 float 的文本不属于出生事实，不能把 NaN 比较不等误当 PID 换代。
# 函数用途: 在破坏性环境删除前严格验证原执行器时间。
def _valid_time(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


# LLM: revocation 已关闭新准入；先证明原 handler 退出，再在资源锁下复读两类完整记录，任一未知或坏记录阻止环境删除。
# 函数用途: 从原账收集可持久化的完整释放证据，不接受调用方声明 cleaned=true。
def plugin_release_evidence(owner, repository, installation) -> dict:
    activation = installation.activation
    if activation is None or activation.phase != "revoked":
        raise PluginInstallationError("activation_unsettled", "原激活尚未撤销。")
    binding = preparation_binding(repository, owner.owner_id, activation.plan)
    exited = preparation_exit(repository, binding)
    execution = asdict(ProcessExecutionScope(str(owner.home_dir), binding.request.thread_id,
                                             binding.task_id, binding.run_id, binding.attempt_id))
    scope = asdict(ProcessActivationScope(owner.owner_id, str(owner.home_dir),
                                         installation.manifest.plugin_id, installation.activation_id))
    store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
    references = []
    with store.transaction() as transaction:
        records, errors = transaction.list_records()
        if errors:
            raise PluginInstallationError("cleanup_unconfirmed", "资源账尚未完整读回，环境暂不删除。")
        for record in records:
            if (record.get("activation_scope") == scope or
                    record.get("activation_scope") is None and record.get("execution_scope") == execution):
                references.append(process_cleanup_reference(record))
    return {"preparation_exit": exited, "resources": references}
