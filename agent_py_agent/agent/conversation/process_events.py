# LLM: 后台命令终态由受保护的 ProcessSessionStore 核实，通知只进入既有 ConversationStore wake。
# 不重放命令、不新建任务、不靠正文判完成；同 session 的通知发布与重启补发共用去重键。
# 模块用途: 让后台命令结束后主动叫醒所属主会话，或在活动主回合安全点交接，省掉模型短轮询。
from __future__ import annotations

import logging
from pathlib import Path

from ..tooling.process_registry import process_registry
from ..tooling.process_session_store import ProcessSessionStore, process_session_store_root

PROCESS_COMPLETION_REASON = "managed_process_exited"
_LOG = logging.getLogger(__name__)


# LLM: 目标由宿主当前主会话身份冻结，子代理生命周期仍由直属父级协议负责，不额外启动影子主代理。
# 函数用途: 在工具执行前生成可信收件地址；模型参数不能指定或更改这个地址。
def process_completion_target(agent: object, params: object) -> dict[str, str]:
    from ..agent_core.runtime.task_identity import durable_task_id

    if not getattr(getattr(agent, "config", None), "background_process_notifications", True):
        return {}
    if str(getattr(params, "context_scope", "default") or "default") not in {"default", "conversation"}:
        return {}
    attrs = getattr(params, "task_attributes", None)
    thread_id = str(attrs.get("conversation_thread_id") or "") if isinstance(attrs, dict) else ""
    store = getattr(agent, "conversation_store", None)
    task_id = durable_task_id(params)
    if store is None or not thread_id or not task_id:
        return {}
    return {"store_root": str(store.root), "thread_id": thread_id, "task_id": task_id,
            "run_id": str(getattr(params, "run_id", "") or "")}


# LLM: 只用宿主路径定位本 owner 的权威记录，不遍历用户任务文件，也不把模型路径当存储根。
# 函数用途: 找到当前 agent 的后台进程记录目录；未绑定的轻量测试对象返回空。
def _agent_process_root(agent: object) -> Path | None:
    paths = getattr(agent, "home_paths", None)
    owner = str(getattr(paths, "owner_home_dir", "") or "")
    workspace = str(getattr(agent, "effective_workspace_root", "") or "")
    if not owner and not workspace:
        return None
    return process_session_store_root(workspace or owner, owner)


# LLM: 宿主周期 tick 与前台安全点共用此入口；先持久去重入队，再标记已发布，崩溃重入不丢不重。
# killed 只登记已观察，不重新唤醒用户明确停止的命令；日志全文不复制进上下文，只有原文件引用。
# 函数用途: 自动收割已结束后台命令并通知所属主会话；一个损坏记录不会阻塞其它会话。
def reconcile_process_completions(agent: object) -> int:
    store = getattr(agent, "conversation_store", None)
    root = _agent_process_root(agent)
    if store is None or root is None:
        return 0
    if not getattr(getattr(agent, "config", None), "background_process_notifications", True):
        return 0
    authority = ProcessSessionStore(root)
    records, errors = authority.list_records()
    if errors:
        _LOG.warning("PROCESS_COMPLETION_STORE_ERRORS count=%s", len(errors))
    sent = 0
    for payload in records:
        target = payload.get("completion_target") or {}
        if not target or payload.get("completion_notice_id"):
            continue
        if str(target.get("store_root") or "") != str(store.root):
            continue
        scope = payload["access_scope"]
        if target.get("thread_id") != scope.get("conversation_id"):
            _LOG.error("PROCESS_COMPLETION_SCOPE_CONFLICT session=%s", payload["session_id"])
            continue
        try:
            summary = process_registry.status(str(payload["session_id"]), store_root=root)
            if not summary or summary["status"] == "running":
                continue
            current = authority.load(str(payload["session_id"])).record
            if not current or current.get("completion_notice_id"):
                continue
            notice_id = "explicit_stop"
            if summary["status"] != "killed":
                facts = {key: summary[key] for key in ("session_id", "status", "exit_code", "output_file") if key in summary}
                signal = store.raise_wake_signal({
                    "thread_id": target["thread_id"], "root_task_id": target["task_id"],
                    "source_agent_id": target["run_id"], "reason": PROCESS_COMPLETION_REASON,
                    "urgency": "normal", "dedupe_key": f"process-exit:{payload['session_id']}",
                    "summary": "受管后台命令已结束，请按退出状态核对产物并继续相关工作。",
                    "metadata": {"process_completion": facts},
                })
                notice_id = signal.wake_signal_id
                sent += 1
            authority.write({**current, "completion_notice_id": notice_id})
        except Exception:
            _LOG.exception("PROCESS_COMPLETION_RETRY session=%s", payload["session_id"])
    return sent


# LLM: 发现层只判断尚欠通知的结构化记录存在，真实是否退出由 registry PID 出生标识核对。
# 函数用途: Gateway 重启或 owner 热缓存淘汰后，仍能发现后台命令，不让完成通知无人处理。
def owner_has_pending_process_completions(owner_home: Path) -> bool:
    root = process_session_store_root(owner_home, owner_home)
    records, _errors = ProcessSessionStore(root).list_records()
    return any(record.get("completion_target") and not record.get("completion_notice_id") for record in records)


# LLM: 终态和收件身份匹配后分辨 ready/receipt_pending；发布间隙或回执写失败须等重投，不能误消费。
# 函数用途: 核对自然收尾后的欠报通知；返回空表示无此义务，不重新运行命令或恢复 Goal。
def process_completion_delivery_state(agent: object, signal: object) -> str:
    if getattr(signal, "reason", "") != PROCESS_COMPLETION_REASON:
        return ""
    store = getattr(agent, "conversation_store", None)
    root = _agent_process_root(agent)
    if store is None or root is None:
        return ""
    facts = (getattr(signal, "metadata", None) or {}).get("process_completion")
    if not isinstance(facts, dict) or not facts.get("session_id"):
        return ""
    record = ProcessSessionStore(root).load(str(facts["session_id"])).record
    if not record or record.get("status") != "exited":
        return ""
    target = record.get("completion_target") or {}
    if target != {"store_root": str(store.root), "thread_id": signal.thread_id,
                  "task_id": signal.root_task_id, "run_id": signal.source_agent_id}:
        return ""
    link = store.load_task_link(signal.root_task_id)
    if link is None or link.thread_id != signal.thread_id or link.status != "completed":
        return ""
    if any(goal.task_id == signal.root_task_id and goal.status != "complete"
           for goal in store.load_goals(signal.thread_id)):
        return ""
    notice_id = record.get("completion_notice_id")
    if not notice_id:
        return "receipt_pending"
    return "ready" if notice_id == signal.wake_signal_id else ""
