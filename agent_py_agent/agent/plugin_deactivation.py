# LLM: 撤销先关闭唯一安装表，再关闭原准备 attempt 并清理精确资源；executor 退出后才释放，退出证据须留到原结果持久化。
# 模块用途: 停用一个固定插件代次，保留未知清理结果，避免卡住的插件影响核心及其他用户任务。

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from .plugin_activation import PluginActivationRequest
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginInstallation, PluginInstallationError
from .plugin_release import preparation_binding
from .runtime_db.run_cancellation import RuntimeCancellationTarget, cancel_runtime_run
from .tooling.process_scope import ProcessActivationScope, ProcessExecutionScope
from .tooling.process_session_cleanup import ProcessSessionCleanupError, stop_process_session
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root


# LLM: 返回原 CAS 得到的完整安装，卸载不能重读插件名挑新目标；报告继续交同一个原操作保存。
# 类用途: 将准确停用或释放后的安装记录与可展示的清理结果一并交给管理工具。
@dataclass(frozen=True)
class PluginDeactivationResult:
    installation: PluginInstallation
    report: dict


# LLM: installation 来自已见目录；返回准确停用/释放记录，提交后错误不猜未发生，资源证据在这里不消费。
# 函数用途: 停用固定插件代次，能确认原准备和资源都结束时删除环境并允许再次启用。
def deactivate_plugin(owner, repository, installation: PluginInstallation, operation_id: str) -> PluginDeactivationResult:
    activation = installation.activation
    if activation is None:
        PluginInstallStore(owner).confirm_inactive(installation)
        return PluginDeactivationResult(installation, {"plugin_id": installation.manifest.plugin_id, "enabled": False,
                "revision": installation.revision, "authority_revoked": True,
                "cleanup_confirmed": True, "released": True, "sessions": [], "errors": []})
    stopped = PluginInstallStore(owner).change_activation(PluginActivationRequest(
        operation_id, installation.revision, replace(activation, phase="revoked"),
    )).installation
    scope = ProcessActivationScope(owner.owner_id, str(owner.home_dir),
                                   stopped.manifest.plugin_id, stopped.activation_id)
    store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
    report = {"plugin_id": scope.plugin_id, "enabled": False, "revision": stopped.revision,
              "activation_id": scope.activation_id, "authority_revoked": True,
              "cleanup_confirmed": False, "released": False, "sessions": [], "errors": []}
    try:
        binding = preparation_binding(repository, owner.owner_id, activation.plan)
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
    if report["cleanup_confirmed"]:
        stopped = _release_if_settled(owner, repository, stopped, operation_id, report)
    return PluginDeactivationResult(stopped, report)


# LLM: 已选资源退出不代表原 handler 退出；原记录不重读换绑，返回释放 CAS 结果或原 revoked，错误仍留原账。
# 函数用途: 尝试完成已停用插件的环境释放，并把执行器/资源证明交给同一个管理结果。
def _release_if_settled(owner, repository, stopped, operation_id: str, report: dict) -> PluginInstallation:
    try:
        released, evidence = PluginInstallStore(owner).release_activation(owner, repository, stopped, operation_id)
        report.update(released=True, revision=released.installation.revision, release=evidence)
        return released.installation
    except PluginInstallationError as exc:
        if exc.reason == "preparation_executor_unconfirmed":
            report["release_pending"] = exc.reason
        else:
            report["cleanup_confirmed"] = False
            report["errors"].append({"stage": "release", "reason": exc.reason, "commit_state": exc.commit_state})
    except Exception as exc:  # noqa: BLE001 证明损坏不能用已选集合的局部成功洗成全部收尾
        report["cleanup_confirmed"] = False
        report["errors"].append({"stage": "release", "error_type": type(exc).__name__})
    return stopped


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
