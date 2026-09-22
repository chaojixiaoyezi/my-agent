# LLM: 只在管理写入口原命令结束后消费；每次严格读回原 operation/replay，查询不调用，旧请求不选择当前激活。
# 模块用途: 将已落账的插件退出证据从原进程账精确移交给原管理结果，消费失败可用同请求重送补做。

from __future__ import annotations

from .plugin_disable_tool import PLUGIN_DISABLE_TOOL
from .runtime_db.host_command_execution import query_host_command
from .tooling.process_session_store import ProcessSessionStore, process_session_store_root


# LLM: 成功回执中必须已保存整份释放证据；UNKNOWN/坏账/未释放不删记录，删除失败也不改写已确认的原操作。
# 函数用途: 在首次管理写入或显式重送后补做幂等收尾，并单列本次消费状态供用户查看。
def consume_plugin_cleanup(owner, repository, request) -> dict:
    if request.command_name != PLUGIN_DISABLE_TOOL:
        return {}
    try:
        settled = query_host_command(repository, request)
        if settled.get("state") != "succeeded" or not settled.get("ok") or settled.get("finalization_pending"):
            return {}
        report = settled["result"][PLUGIN_DISABLE_TOOL]
        if report.get("released") is not True or report.get("cleanup_confirmed") is not True:
            return {}
        release = report.get("release")
        if release is None:
            if report.get("activation_id"):
                raise ValueError("释放结果缺少原资源证明")
            return {}
        references = tuple(release["resources"])
        store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
        with store.transaction() as transaction:
            transaction.consume_cleanup(references)
        return {"state": "consumed", "count": len(references)}
    except Exception as exc:  # noqa: BLE001 收尾错误必须与原管理成功分开，不能重新执行 handler 或丢失可重送结果
        return {"state": "pending", "error_type": type(exc).__name__}
