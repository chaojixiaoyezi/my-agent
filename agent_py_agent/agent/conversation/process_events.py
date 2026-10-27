# LLM: 任务进程终态由原 ProcessSessionStore 核实，共享激活不参与业务通知；通知只进入既有 ConversationStore wake。
# 不重放命令、不新建任务、不靠正文判完成；同 session 的通知发布与重启补发共用去重键。
# 便宜检查只影响初次目录内容读取，正式通知前仍核原始状态、归属与去重回执。
# 持久unknown不再刷新；存活旧locale实例共用registry有界等价缓存，未知回原get复核，不改磁盘token或通知去重。
# 模块用途: 让后台命令结束后主动叫醒所属主会话，或在活动主回合安全点交接，省掉模型短轮询。
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

from ..gateway_parts.multiprocess_executor_identity import process_birth
from ..gateway_parts.multiprocess_native_host_observation import kernel_terminated, store_birth
from ..tooling import process_registry as _registry_module
from ..tooling.process_registry import (
    ProcessRegistry,
    ProcessSessionAuthorityError,
    process_registry,
)
from ..tooling.process_session_records import (
    MANAGED_PROCESS_SESSION_SCHEMAS,
    PROCESS_TERMINAL_STATUSES,
)
from ..tooling.process_session_store import (
    ProcessSessionListReport,
    ProcessSessionStore,
    process_session_store_root,
)

PROCESS_COMPLETION_REASON = "managed_process_exited"
_LOG = logging.getLogger(__name__)
_PROCESS_REGISTRY_STATUS = ProcessRegistry.status
# 每个会话存储根执行历史清理的最短间隔秒数。
_PROCESS_COMPLETION_PRUNE_INTERVAL_SECONDS = 300.0
# 已结清通知义务的终态记录保留秒数。
_PROCESS_COMPLETION_RETENTION_SECONDS = 86400.0
# 历史清理节流最多记住的会话存储根数量。
_PROCESS_COMPLETION_PRUNE_MAX_ROOTS_COUNT = 256
_PROCESS_COMPLETION_PRUNE_TIMES: OrderedDict[str, float] = OrderedDict()
_PROCESS_COMPLETION_PRUNE_LOCK = threading.Lock()


# LLM: 固定report读口存在时直接调用，内部异常原样传播；旧替身沿原二元组接口，不靠TypeError猜版本。
# 函数用途: 为协调循环读取候选副本和完整错误，兼容旧authority替身。
def _pending_completion_report(authority, *, cheap_checks: bool = False, task_id: str = "") -> ProcessSessionListReport:
    query = getattr(authority, "list_pending_completion_records_report", None)
    if callable(query):
        return query(cheap_checks=cheap_checks, task_id=task_id)
    records, errors = authority.list_records(cheap_checks=cheap_checks)
    return ProcessSessionListReport(records, errors)


# LLM: owner隔离由原权威目录承担，当前target无owner_key；task过滤保留原store_root复核，空cheap结果必须全读再裁定。
# 函数用途: 为待通知发现提供便宜检查后严格复核，旧替身维持无参数调用。
def _pending_completion_reports(authority, *, task_id: str = ""):
    query = getattr(authority, "list_pending_completion_records_report", None)
    if not callable(query):
        records, errors = authority.list_records()
        yield ProcessSessionListReport(records, errors)
        return
    for cheap in (True, False):
        yield query(cheap_checks=cheap, task_id=task_id)


# LLM: 每个conversation根最多五分钟一次，锁只保护有界节流表；磁盘清理仍在原Store事务中，pending与未知读取均保留。
# 函数用途: 无新attach也周期回收超龄已消费记录，不删除仍欠投递的wake依赖。
def _prune_consumed_process_records(store, authority) -> None:
    prune = getattr(authority, "prune_finished", None)
    if not callable(prune):
        return
    key, now = str(store.storage.root), time.monotonic()
    with _PROCESS_COMPLETION_PRUNE_LOCK:
        previous = _PROCESS_COMPLETION_PRUNE_TIMES.get(key)
        if previous is not None and now - previous < _PROCESS_COMPLETION_PRUNE_INTERVAL_SECONDS:
            return
        _PROCESS_COMPLETION_PRUNE_TIMES[key] = now
        _PROCESS_COMPLETION_PRUNE_TIMES.move_to_end(key)
        while len(_PROCESS_COMPLETION_PRUNE_TIMES) > _PROCESS_COMPLETION_PRUNE_MAX_ROOTS_COUNT:
            _PROCESS_COMPLETION_PRUNE_TIMES.popitem(last=False)

    current_root = Path(store.storage.root).resolve()

    # LLM: 优先真实pending_one；先按completion_target.store_root核验目标会话库，不跨库误判已消费。
    # 函数用途: 允许清理明确在本会话库或目标会话库不再pending的通知依赖。
    def consumed(notice_id: str, record: dict[str, object] | None = None) -> bool:
        target = record.get("completion_target") if isinstance(record, dict) else None
        target_root = str(target.get("store_root") or "").strip() if isinstance(target, dict) else ""
        wakes = store.wakes
        if target_root and Path(target_root).resolve() != current_root:
            target_path = Path(target_root)
            if not target_path.is_dir():
                return False
            from .store import ConversationStore

            wakes = ConversationStore(target_path).wakes
        query = getattr(wakes, "pending_one", None)
        if not callable(query):
            query = getattr(wakes, "get", None)
        if not callable(query):
            raise AttributeError("wake_pending_query_unavailable")
        wake = query(notice_id)
        return wake is None or wake.status != "pending"

    consumed._accepts_record = True  # type: ignore[attr-defined]

    try:
        prune(max_age_seconds=_PROCESS_COMPLETION_RETENTION_SECONDS, is_notice_consumed=consumed)
    except Exception:
        _LOG.exception("PROCESS_COMPLETION_PRUNE_RETRY")


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
    return {"store_root": str(store.storage.root), "thread_id": thread_id, "task_id": task_id,
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


# LLM: Count0保原循环/锁顺序，MP仅共用单会话发表叶；宿主周期tick与前台安全点共用；配置仅控制初步目录内容读取，发通知前仍核原status/target并持久去重，不缓存通知结论。
# 仅任务的自然exited可唤醒；持久unknown不可再转终态，直接跳过；其余轻量刷新后重读原ID与收件身份。
# 查询仅拷贝候选，末尾有界TTL不影响仍pending的wake读取；通知关闭时仍可做安全历史维护。
# 函数用途: 不读日志地收割后台命令并通知所属主会话；保留坏记录重试与通知去重，不反复查询历史未知项。
def reconcile_process_completions(agent: object) -> int:
    store = getattr(agent, "conversation_store", None)
    root = _agent_process_root(agent)
    if store is None or root is None:
        return 0
    authority = ProcessSessionStore(root)
    if not getattr(getattr(agent, "config", None), "background_process_notifications", True):
        _prune_consumed_process_records(store, authority)
        return 0
    cheap_checks = bool(
        getattr(getattr(agent, "config", None), "background_tick_cheap_checks", False)
    )
    report = _pending_completion_report(authority, cheap_checks=cheap_checks)
    records, errors = report.records, report.load_errors
    if errors:
        _LOG.warning("PROCESS_COMPLETION_STORE_ERRORS count=%s", len(errors))
    sent = 0
    for payload in records:
        if payload.get("activation_scope") is not None or payload.get("status") == "unknown":
            continue
        target = payload.get("completion_target") or {}
        if not target or payload.get("completion_notice_id"):
            continue
        if str(target.get("store_root") or "") != str(store.storage.root):
            continue
        scope = payload["access_scope"]
        if target.get("thread_id") != scope.get("conversation_id"):
            _LOG.error("PROCESS_COMPLETION_SCOPE_CONFLICT session=%s", payload["session_id"])
            continue
        try:
            summary = _process_completion_status(str(payload["session_id"]), root, payload=payload)
            if not summary or summary["status"] not in PROCESS_TERMINAL_STATUSES:
                continue
            report = authority.load(str(payload["session_id"]))
            if report.load_error:
                raise ProcessSessionAuthorityError(report.load_error)
            current = report.record
            if not current or current.get("completion_notice_id") or current["status"] not in PROCESS_TERMINAL_STATUSES:
                continue
            notice_id = publish_process_completion(store, current)
            sent += int(current["status"] == "exited" and not current.get("stop_requested"))
            _record_completion_notice(authority, current, notice_id)
        except Exception:
            _LOG.exception("PROCESS_COMPLETION_RETRY session=%s", payload["session_id"])
    _prune_consumed_process_records(store, authority)
    return sent


# LLM: 仅原类、原status身份且函数确属registry模块命名空间才轻量get；导入前后替换和外部替身仍走status。
#   对尚未终态的受管 v2/v3/v4 记录，仅在内核确认原 PID/launcher_pid 存活、非僵尸且出生标识一致时跳过每拍重读与 ps；
#   已终态记录与失联/PID 复用记录均走 process_registry.get 完成本地句柄 poll 回收、缓存刷新或 unknown 转换。
# 函数用途: 取得通知所需的最新状态，不为后台巡检打开日志或对存活受管进程高频拉起 ps；归属校验和最终回执仍由原收割循环负责。
def _process_completion_status(
    session_id: str,
    root: Path,
    *,
    payload: dict[str, object] | None = None,
) -> dict[str, object] | None:
    status = process_registry.status
    if (type(process_registry) is ProcessRegistry
            and getattr(status, "__func__", None) is _PROCESS_REGISTRY_STATUS
            and getattr(_PROCESS_REGISTRY_STATUS, "__globals__", None) is vars(_registry_module)):
        if (
            isinstance(payload, dict)
            and payload.get("schema") in MANAGED_PROCESS_SESSION_SCHEMAS
            and "get" not in process_registry.__dict__
            and getattr(process_registry.get, "__func__", None) is ProcessRegistry.get
        ):
            recorded_status = str(payload.get("status") or "running")
            if recorded_status not in PROCESS_TERMINAL_STATUSES and _managed_instance_still_live(
                session_id, root, payload
            ):
                return {"status": recorded_status}
        record = process_registry.get(session_id, store_root=root)
        return {"status": record.status} if record is not None else None
    return status(session_id, store_root=root)


# LLM: 每拍仍核PID/非僵尸/出生观察与句柄poll；旧locale仅首次原生复核，后续三元组命中不拉ps；未知返原get。
# 函数用途: 判断原受管实例仍存活，复用已核出生等价但不把缓存当当前存活证据。
def _managed_instance_still_live(
    session_id: str,
    root: Path,
    payload: dict[str, object],
) -> bool:
    normalized_root = str(root.expanduser().resolve(strict=False))
    with process_registry._lock:
        cached = process_registry._processes.get((normalized_root, session_id))
        local_process = cached.process if cached is not None else None
    if local_process is not None and local_process.poll() is not None:
        return False
    pid = int(payload.get("pid") or payload.get("launcher_pid") or 0)
    birth = str(payload.get("pid_birth_token") or payload.get("launcher_birth_token") or "")
    if pid <= 0 or not _registry_module._pid_alive(pid) or kernel_terminated(pid):
        return False
    if birth:
        observed = store_birth(process_birth(pid))
        if not observed:
            return False
        if observed != birth:
            try:
                return _registry_module._birth_token_matches(pid, birth, observed)
            except (OSError, subprocess.TimeoutExpired, ValueError):
                return False
    return True


# LLM: 单条已核终态共用原Wake reason/facts/dedupe；调用者负责原owner/target/状态与写门，不能据本函数授执行权。
# 函数用途: 为自然结束的原会话发布一次去重完成信号，明确停止只返回原收口标记。
def publish_process_completion(store, current):
    if current["status"] != "exited" or current.get("stop_requested"):
        return "explicit_stop"
    target = current["completion_target"]
    facts = {key: current[key] for key in ("session_id", "status", "exit_code", "output_file") if key in current}
    signal = store.wakes.raise_signal({
        "thread_id": target["thread_id"], "root_task_id": target["task_id"],
        "source_agent_id": target["run_id"], "reason": PROCESS_COMPLETION_REASON,
        "urgency": "normal", "dedupe_key": f"process-exit:{current['session_id']}",
        "summary": "受管后台命令已结束，请按退出状态核对产物并继续相关工作。",
        "metadata": {"process_completion": facts},
    })
    return signal.wake_signal_id


# LLM: 发布后只重读同一 session，终态或停止改变不重发 wake；不强换旧 revision 覆盖新记录。
# 函数用途: 保存原完成通知的去重回执，显式停止优先结算为不再唤醒。
def _record_completion_notice(authority: ProcessSessionStore, current: dict[str, object], notice_id: str) -> None:
    with authority.transaction() as transaction:
        latest = transaction.load(str(current["session_id"]))
        if latest is None or latest.get("completion_notice_id"):
            return
        if latest.get("completion_target") != current["completion_target"] or latest["status"] not in PROCESS_TERMINAL_STATUSES:
            raise ValueError("process completion authority changed")
        transaction.write({**latest, "completion_notice_id": "explicit_stop" if latest.get("stop_requested") else notice_id})


# LLM: 发现层只判断尚欠通知的结构化记录；持久 unknown 与共享激活永不产生会话完成通知，不纳入待处理事实；
#   权威读取失败保留待检查，不能当成没有义务。
#   有固定候选读口时先cheap再full，原owner目录是隔离边界，不从缺失的owner_key补造身份。
# 函数用途: Gateway 重启或 owner 热缓存淘汰后，仍能发现后台命令，不让完成通知无人处理。
def owner_has_pending_process_completions(owner_home: Path) -> bool:
    root = process_session_store_root(owner_home, owner_home)
    errors = []
    for report in _pending_completion_reports(ProcessSessionStore(root)):
        errors = report.load_errors
        if any(record.get("activation_scope") is None and record.get("status") != "unknown"
               and record.get("completion_target") and not record.get("completion_notice_id") for record in report.records):
            return True
    return bool(errors)


# LLM: 只按记录里的结构化 completion_target（store_root + task_id）判断这个任务还欠不欠一次完成通知；
#   持久 unknown 与共享激活不会再转终态或发完成通知，不计入待等待事实；
#   权威读取出错时抛 ProcessSessionAuthorityError，调用方必须按"读不到"处理，不能当成没有后台命令。
#   读到的记录里已有本任务欠通知的就直接返回 True（同类坏记录不遮住它）；否则读错误先按记录归属限定：
#   坏记录能解析出不属于本任务（见 _record_owned_elsewhere）就不计入，
#   避免别的会话一条坏记录让所有定时收口都读不到。
#   固定查询仅复制task候选，cheap未命中后仍全读；错误归属裁决与store_root匹配保持原义。
# 函数用途: 判断某个会话任务是否还有尚未发出完成通知的受管后台命令（命令仍在跑或刚结束待通知）。
def task_has_pending_process_completion(agent: object, task_id: str) -> bool:
    store = getattr(agent, "conversation_store", None)
    root = _agent_process_root(agent)
    selected = str(task_id or "").strip()
    if store is None or root is None or not selected:
        return False
    store_root = str(store.storage.root)
    errors = []
    for report in _pending_completion_reports(ProcessSessionStore(root), task_id=selected):
        errors = report.load_errors
        if any(record.get("activation_scope") is None and record.get("status") != "unknown"
               and isinstance(record.get("completion_target"), dict)
               and str(record["completion_target"].get("store_root") or "") == store_root
               and str(record["completion_target"].get("task_id") or "") == selected
               and not record.get("completion_notice_id") for record in report.records):
            return True
    unscoped = [error for error in errors if not _record_owned_elsewhere(error, selected, store_root)]
    if unscoped:
        raise ProcessSessionAuthorityError(unscoped[0])
    return False


# LLM: 只读错误报告里的 path 指向的那一个记录文件并解析 JSON，不改任何文件。记录里没有完成通知目标（缺键、None，
#   或盘上的规范空形状 {}：v2 校验会把缺失目标归一成 {}，共享插件激活、子代理作用域、无会话线程或通知关闭时也是 {}，
#   合法路径下 {} 永远不欠任何任务的通知）、或 completion_target 写明了别的任务 / 别的会话存储，才算"不属于本任务"；
#   文件读不出、不是 JSON 对象、completion_target 形状不对，都按可能属于本任务处理（计入读取错误）。
# 函数用途: 判断一条后台命令记录的读取错误是否确定与本任务无关。
def _record_owned_elsewhere(error: object, task_id: str, store_root: str) -> bool:
    path = error.get("path") if isinstance(error, dict) else None
    try:
        payload = json.loads(Path(str(path)).read_text(encoding="utf-8")) if path else None
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    target = payload.get("completion_target")
    if target is None or target == {}:
        return True
    if not isinstance(target, dict):
        return False
    return any(isinstance(target.get(key), str) and target[key] != expected
               for key, expected in (("task_id", task_id), ("store_root", store_root)))


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
    report = ProcessSessionStore(root).load(str(facts["session_id"]))
    if report.load_error:
        return "receipt_pending"
    record = report.record
    if not record or record.get("status") != "exited" or record.get("stop_requested"):
        return ""
    target = record.get("completion_target") or {}
    if target != {"store_root": str(store.storage.root), "thread_id": signal.thread_id,
                  "task_id": signal.root_task_id, "run_id": signal.source_agent_id}:
        return ""
    link = store.tasks.load(signal.root_task_id)
    if link is None or link.thread_id != signal.thread_id or link.status != "completed":
        return ""
    if any(goal.task_id == signal.root_task_id and goal.status != "complete"
           for goal in store.goals.list(signal.thread_id)):
        return ""
    notice_id = record.get("completion_notice_id")
    if not notice_id:
        return "receipt_pending"
    return "ready" if notice_id == signal.wake_signal_id else ""
