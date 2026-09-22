# LLM: 只在管理写入口原命令结束后消费；每次严格读回原 operation/replay，查询不调用，旧请求不选择当前激活。
# 模块用途: 将已落账的退出证据移交给原管理结果，并安全回收卸载包，消费失败可用同请求重送补做。

from __future__ import annotations

from .plugin_disable_tool import PLUGIN_DISABLE_TOOL
from .plugin_install_store import PluginInstallStore
from .plugin_installation import PluginCommitReceipt
from .plugin_removal import PLUGIN_REMOVE_TOOL
from .runtime_db.host_command_execution import query_host_command
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root


# LLM: 成功回执必须已保存释放/卸载证明；UNKNOWN/坏账/未释放不删，重新安装同包时只保留其引用，不改原成功。
# 函数用途: 在首次管理写入或显式重送后补做幂等收尾，并单列本次消费状态供用户查看。
def consume_plugin_cleanup(owner, repository, request) -> dict:
    if request.command_name not in {PLUGIN_DISABLE_TOOL, PLUGIN_REMOVE_TOOL}:
        return {}
    try:
        settled = query_host_command(repository, request)
        if settled.get("state") != "succeeded" or not settled.get("ok") or settled.get("finalization_pending"):
            return {}
        report = settled["result"][request.command_name]
        if report.get("released") is not True or report.get("cleanup_confirmed") is not True:
            return {}
        receipt = _removal_receipt(report, request) if request.command_name == PLUGIN_REMOVE_TOOL else None
        references = _cleanup_references(report)
        if references is None and receipt is None:
            return {}
        if references is not None:
            store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
            with store.transaction() as transaction:
                transaction.consume_cleanup(references)
        result = {"state": "consumed", "count": len(references or ())}
        if receipt is not None:
            result["package"] = PluginInstallStore(owner).consume_removed_package(receipt)
        return result
    except Exception as exc:  # noqa: BLE001 收尾错误必须与原管理成功分开，不能重新执行 handler 或丢失可重送结果
        return {"state": "pending", "error_type": type(exc).__name__}


# LLM: 只接受原操作成功结果中的对应删除回执；缺失确认没有可回收包，坏回执在任何消费前拒绝。
# 函数用途: 固定卸载后包回收所需身份，不凭当前目录或插件名称选择删除对象。
def _removal_receipt(report: dict, request) -> PluginCommitReceipt | None:
    if report.get("removed") is not True:
        raise ValueError("卸载结果尚未确认")
    raw = report.get("receipt")
    if raw is None and report.get("outcome") == "absent" and not report.get("activation_id"):
        return None
    receipt = PluginCommitReceipt(**raw)
    if (receipt.action != "remove" or receipt.operation_id != request.operation_id
            or receipt.plugin_id != report["plugin_id"] or report.get("outcome") != "removed"
            or report.get("commit_state") != "committed"):
        raise ValueError("卸载回执与原请求不匹配")
    return receipt


# LLM: 原激活释放必须有原资源证明；无激活停用/卸载不创建进程账，引用验证仍交原事务。
# 函数用途: 取出已持久保存的精确退出引用，缺证明时拒绝消费。
def _cleanup_references(report: dict) -> tuple | None:
    release = report.get("release")
    if release is None:
        if report.get("activation_id"):
            raise ValueError("释放结果缺少原资源证明")
        return None
    return tuple(release["resources"])
