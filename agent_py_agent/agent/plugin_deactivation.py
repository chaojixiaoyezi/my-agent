# LLM: 撤销先关闭唯一安装表，再关闭原准备 attempt，冻结两种精确资源；不新建执行账或删除退出证据，联测管理与启动竞态。
# 模块用途: 停用一个固定插件代次，保留未知清理结果，避免卡住的插件影响核心及其他用户任务。

from __future__ import annotations

from dataclasses import asdict, replace

from .plugin_activation import PLUGIN_ENABLE_TOOL, PluginActivationRequest
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginInstallation, PluginInstallationError
from .runtime_db.managed_operation_store import ManagedOperationStore
from .runtime_db.operations import RuntimeConflictError
from .runtime_db.run_cancellation import RuntimeCancellationTarget, cancel_runtime_run
from .tooling.process_scope import ProcessActivationScope, ProcessExecutionScope
from .tooling.process_session_cleanup import ProcessSessionCleanupError, stop_process_session
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root


# LLM: installation 来自与已见目录同一次宿主读取；提交后错误不能猜未发生，原记录及清理证据保留到后续显式释放。
# 函数用途: 撤销当前代并分别清理准备任务和共享连接，不终止 Gateway、业务会话或其他代次。
def deactivate_plugin(owner, repository, installation: PluginInstallation, operation_id: str) -> dict:
    activation = installation.activation
    if activation is None:
        PluginInstallStore(owner).confirm_inactive(installation)
        return {"plugin_id": installation.manifest.plugin_id, "enabled": False,
                "revision": installation.revision, "authority_revoked": True,
                "cleanup_confirmed": True, "sessions": [], "errors": []}
    stopped = PluginInstallStore(owner).change_activation(PluginActivationRequest(
        operation_id, installation.revision, replace(activation, phase="revoked"),
    )).installation
    scope = ProcessActivationScope(owner.owner_id, str(owner.home_dir),
                                   stopped.manifest.plugin_id, stopped.activation_id)
    store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
    report = {"plugin_id": scope.plugin_id, "enabled": False, "revision": stopped.revision,
              "activation_id": scope.activation_id, "authority_revoked": True,
              "cleanup_confirmed": False, "sessions": [], "errors": []}
    try:
        binding = _preparation_binding(repository, owner.owner_id, activation.plan)
        closed = cancel_runtime_run(repository, RuntimeCancellationTarget(
            binding.task_id, binding.run_id, binding.agent_run_id, binding.attempt_id,
        ), reason="plugin_revoked", source="plugin_management")
        report["preparation"] = closed
        preparation_scope = ProcessExecutionScope(str(owner.home_dir), binding.request.thread_id,
                                                  binding.task_id, binding.run_id, binding.attempt_id)
        selected = store.request_stop(preparation_scope, include_terminal=True)
        _clean_selected(store, selected.records, "preparation", report)
    except Exception as exc:  # noqa: BLE001 缺失准备归属不能扩大停止范围，仍清理已经撤销的准确共享资源
        report["errors"].append({"stage": "preparation", "error_type": type(exc).__name__})
    try:
        selected = store.request_stop_activation(scope)
        _clean_selected(store, selected.records, "activation", report)
    except Exception as exc:  # noqa: BLE001 已撤销不代表资源消失，结构化错误留下原 UNKNOWN
        report["errors"].append({"stage": "activation", "error_type": type(exc).__name__})
    report["cleanup_confirmed"] = not report["errors"] and all(row["confirmed"] for row in report["sessions"])
    return report


# LLM: 只查计划保存的原 operation；原请求类型和完整 claim 归属须一致，不能按插件名扫描运行或补 current。
# 函数用途: 找回创建此候选环境的首次管理运行，坏账时拒绝代替它清理别的任务。
def _preparation_binding(repository, owner_id: str, plan):
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


# LLM: 输入已在原锁内固定，逐一原生清理并保留每个结果；一个坏资源不能跳过余下资源，也不能凭命令退出代替 host 退出。
# 函数用途: 在锁外停止本次选中的进程树，把可查询证据交回唯一管理操作。
def _clean_selected(store, records, kind: str, report: dict) -> None:
    for record in records:
        try:
            cleanup = stop_process_session(store, record)
            report["sessions"].append({
                "session_id": record["session_id"], "kind": kind, "confirmed": cleanup.confirmed,
                "status": cleanup.record["status"], "terminations": [asdict(item) for item in cleanup.terminations],
            })
        except ProcessSessionCleanupError as exc:
            report["sessions"].append({"session_id": record["session_id"], "kind": kind,
                                       "confirmed": False, "error": exc.report})
