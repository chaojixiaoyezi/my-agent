# LLM: 实验对照记录唯一权威是 Gateway 请求记录的 experiment_records 块（与能力观测、experiment_grant 同一文件）；
#   没有 token 表、旁路恢复文件或第二本账。写入沿精确回合转换锁 + 原 JSON 原子更新，并同步内存 request。
#   样本在决策调用后写一次（record_id=原调用编号去重）；实际用量只在回合正常收尾时按结构化工具账补写，停止/关闭的回合不补写。
#   证据链只沿 experiment_grant.previous_request_id 回读原请求记录，条目身份由评估器按 owner/thread 逐条核对。
#   普通请求（无实验条目、无 apply 授权）在任何入口都零 I/O、不写任何键。调用方：request_execution、request_binding、晋升模块。
# 模块用途: 把只观察实验的对照记录、回合结束的实际工具用量和跨请求证据读取接到原请求记录上。
from __future__ import annotations

import logging
import re

from ..common.cancellation import ToolCancelled
from ..conversation.decision_experiment_evaluation import evaluate_skill_tool_samples
from ..conversation.decision_policy import decision_owner_ref
from ..turn_end import result_turn_end_reason
from .io import read_json_file_report, update_json_file_atomic
from .paths import gateway_paths_from_root
from .request_binding import GatewayActiveTurnTransition, record_capability_presentation_observation
from .request_experiment import EXPERIMENT_GRANT_KEY

EXPERIMENT_RECORDS_KEY = "experiment_records"
_RECORDS_SCHEMA = "gateway_decision_experiment_records.v1"
# 与能力观测同口径：一条请求最多保留最近 8 条实验记录（正常每轮只有一次实验调用）。
_RECORD_LIMIT = 8
# 实际调用的不同工具名截到 64 个；超过时显式标记截断，评估器不据此计算召回。
_REALIZED_NAME_LIMIT = 64
# 证据链最多回读 16 条原请求记录，读路径与回合收尾都保持有界 I/O。
_CHAIN_REQUESTS = 16
# 请求编号只作文件名使用，拒绝路径分隔符、冒号（Windows 盘符相对路径）等异常字符，防止证据链指针越出请求目录。
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_LOGGER = logging.getLogger(__name__)


# LLM: 非字典或结构不符的旧值按空块读取，只保留字典条目与可选 promotion 回执，不修复或解释其它字段。
# 函数用途: 从一份请求记录中取出规范化的实验记录块。
def _records_block(payload: object) -> dict:
    block = payload.get(EXPERIMENT_RECORDS_KEY) if isinstance(payload, dict) else None
    block = block if isinstance(block, dict) else {}
    entries = [item for item in block.get("entries") or () if isinstance(item, dict)]
    normalized = {"schema": _RECORDS_SCHEMA, "entries": entries}
    if isinstance(block.get("promotion"), dict):
        normalized["promotion"] = block["promotion"]
    return normalized


# LLM: 调用方必须已持有本回合转换锁（run_in_turn 内）；change 返回 None 表示不改。写后同步内存 request，保证整份回写不丢键。
# 函数用途: 在原 JSON 原子更新内修改本请求的实验记录块，返回保存后的块。
def update_experiment_records(context: object, change) -> dict:
    # LLM: 只读写本键，其它队列字段原样保留；change 只接收规范化块。
    # 函数用途: 在 JSON 锁内应用一次实验记录块变更。
    def update(current: dict) -> dict:
        changed = change(_records_block(current))
        return current if changed is None else {**current, EXPERIMENT_RECORDS_KEY: changed}

    saved = update_json_file_atomic(context.request_path, update, require_existing=True)
    if EXPERIMENT_RECORDS_KEY in saved:
        context.request[EXPERIMENT_RECORDS_KEY] = saved[EXPERIMENT_RECORDS_KEY]
    return _records_block(saved)


# LLM: phase 不在原转换的 closing 放行集合内，回合关闭、停止或换执行代次都抛 InterruptedError，不写任何内容。
# 函数用途: 在本请求精确回合转换锁内执行一次实验记录操作。
def run_in_turn(context: object, phase: str, operation):
    transition = GatewayActiveTurnTransition(context.request_path, context.request_id,
                                             context.request["execution_attempt_id"])
    return transition(phase, operation)


# LLM: 普通观测原样交给原写入器（键集合、条数上限与语义不变）；只有实验路径附带的 experiment_record 被拆出另写。
# 函数用途: 作为 Gateway 的能力推荐观察出口，分别写能力观测与实验对照记录。
def observe_capability_presentation(context: object, observation: dict) -> None:
    entry = dict(observation)
    record = entry.pop("experiment_record", None)
    record_capability_presentation_observation(context, entry)
    if isinstance(record, dict):
        record_decision_experiment_sample(context, record)


# LLM: 同一 record_id 只落一条；条目加盖 Gateway 执行代次，回合收尾只补写同代次条目。回合已关闭/停止时抛中断且不写；
#   其它写盘失败只放弃这一条（与能力观测写入器同一模式），不影响业务回合。
# 函数用途: 把一次实验决策调用的对照记录追加进本请求记录。
def record_decision_experiment_sample(context: object, record: dict) -> None:
    entry = {**record, "execution_attempt_id": str(context.request.get("execution_attempt_id") or "")}

    # LLM: 已有同编号条目即不改；超过上限时丢最旧条目，promotion 回执原样保留。
    # 函数用途: 生成追加本条记录后的实验记录块。
    def change(block: dict) -> dict | None:
        if any(item.get("record_id") == entry["record_id"] for item in block["entries"]):
            return None
        return {**block, "entries": [*block["entries"], entry][-_RECORD_LIMIT:]}

    try:
        run_in_turn(context, "experiment_record", lambda: update_experiment_records(context, change))
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:  # noqa: BLE001 对照记录只是观察事实，写盘失败不能阻断业务
        _LOGGER.warning("决策实验对照记录未能写入请求记录；本轮业务继续。")


# LLM: 工具名只取原工具账 archive_tool_calls 的结构化 tool 字段；任何一条缺名或结构不符就整体视为未知，不从正文补猜。
# 函数用途: 从一轮结果的工具账中取出实际调用过的工具名集合。
def _archive_tool_names(records: object) -> set[str] | None:
    if not isinstance(records, list) or not all(
            isinstance(record, dict) and type(record.get("tool")) is str and record["tool"].strip() for record in records):
        return None
    return {record["tool"].strip() for record in records}


# LLM: 只有宿主协议结束原因为 completed 且工具账完整可读时才算已知；其它情况记 known=false 与结构化原因，不猜。
# 函数用途: 生成回合结束时要补写进实验记录的实际工具用量。
def realized_tool_usage(result: object) -> dict:
    reason = result_turn_end_reason(result)
    base = {"source": "archive_tool_calls", "turn_end_reason": reason}
    if reason != "completed":
        return {**base, "known": False, "reason": "turn_not_completed"}
    records = getattr(result, "archive_tool_calls", None)
    names = _archive_tool_names(records)
    if names is None:
        return {**base, "known": False, "reason": "archive_unavailable"}
    listed = sorted(names)
    return {**base, "known": True, "reason": "", "tool_names": listed[:_REALIZED_NAME_LIMIT],
            "tool_count": len(listed), "names_truncated": len(listed) > _REALIZED_NAME_LIMIT, "call_count": len(records)}


# LLM: 只认本请求当前执行代次写下且尚未补写的条目；崩溃恢复后的新代次不接管旧代次的候选。
# 函数用途: 判断一条实验记录是否等待本轮补写实际用量。
def _pending(entry: dict, attempt: str) -> bool:
    return entry.get("status") == "observed" and entry.get("execution_attempt_id") == attempt


# LLM: 只读内存 request（与文件同步），无实验条目时不做任何 I/O。
# 函数用途: 判断本请求是否有待补写实际用量的实验记录。
def _has_pending(request: dict) -> bool:
    attempt = str(request.get("execution_attempt_id") or "")
    return any(_pending(entry, attempt) for entry in _records_block(request)["entries"])


# LLM: 只认本请求 v2 授权回执里宿主写下的 granted 状态与 apply 动作；模型或 Jev 回答无法写入或伪造这个回执。
# 函数用途: 判断本请求的用户显式授权是否包含自动晋升。
def _apply_granted(request: dict) -> bool:
    grant = request.get(EXPERIMENT_GRANT_KEY)
    return isinstance(grant, dict) and grant.get("status") == "granted" and "apply" in (grant.get("operations") or ())


# LLM: 回合已正常返回后调用；先补写实际用量，再在 apply 授权内尝试晋升。停止/关闭（转换锁抛中断）只跳过，
#   不把可选实验收尾变成另一种请求结果；其它异常只记日志。普通请求直接返回，零 I/O。
# 函数用途: 在 Gateway 回合结束时补写实验记录的实际用量，并触发授权内的自动晋升检查。
def finish_decision_experiment_turn(context: object, result: object) -> None:
    request = context.request
    pending, apply = _has_pending(request), _apply_granted(request)
    if not pending and not apply:
        return
    try:
        if pending:
            _complete_records(context, realized_tool_usage(result))
        if apply:
            from .request_experiment_promotion import promote_skill_tool_if_ready

            promote_skill_tool_if_ready(context)
    except (InterruptedError, ToolCancelled):
        _LOGGER.info("决策实验收尾时本回合已停止或关闭；不补写实际用量、不晋升设置。")
    except Exception:  # noqa: BLE001 实验收尾失败不能改变已完成的业务回合
        _LOGGER.warning("决策实验收尾未完成；本轮业务结果不受影响。")


# LLM: 在转换锁内把本代次待补写条目标为 completed 并写入同一份实际用量；已完成或其它代次条目不动。
# 函数用途: 把回合结束时的实际工具用量补写到本请求的实验记录。
def _complete_records(context: object, realized: dict) -> None:
    attempt = str(context.request.get("execution_attempt_id") or "")

    # LLM: 纯变换，不读其它请求或设置。
    # 函数用途: 生成补写实际用量后的实验记录块。
    def change(block: dict) -> dict:
        entries = [{**entry, "status": "completed", "realized": dict(realized)} if _pending(entry, attempt) else entry
                   for entry in block["entries"]]
        return {**block, "entries": entries}

    run_in_turn(context, "experiment_record", lambda: update_experiment_records(context, change))


# LLM: 编号先按文件名规则校验；依次读终态、处理中、done、failed 投影，只接受 id 与编号一致且可读的记录。
# 函数用途: 按请求编号读取一份原请求记录，找不到或损坏时返回 None。
def load_gateway_request_record(paths: object, request_id: str) -> dict | None:
    if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
        return None
    for folder in (paths.terminal, paths.processing, paths.done, paths.failed):
        report = read_json_file_report(folder / f"{request_id}.json", context="gateway.decision_experiment.read")
        if report.load_error is None and str(report.payload.get("id") or "") == request_id:
            return report.payload
    return None


# LLM: 只认宿主写下的 v2 granted 回执且其 thread 与当前会话一致；旧 v1 回执或跨会话记录都不属于本会话证据链。
# 函数用途: 判断一份请求记录是否是本会话一次已授权的实验请求。
def _granted_for(request: dict, thread_id: str) -> bool:
    grant = request.get(EXPERIMENT_GRANT_KEY)
    return isinstance(grant, dict) and grant.get("status") == "granted" and grant.get("thread_id") == thread_id


# LLM: 指针只按文件名规则校验，缺失或非法即终止链，不猜测其它请求。
# 函数用途: 取得一份请求记录指向的上一份实验授权来源请求编号。
def _previous_request_id(request: dict) -> str:
    previous = request[EXPERIMENT_GRANT_KEY].get("previous_request_id")
    return previous if type(previous) is str and _REQUEST_ID.fullmatch(previous) else ""


# LLM: 从 head（最新请求）起沿授权指针回读，最多 16 条原请求记录；每条都须是本会话 granted 回执，
#   否则（跨会话、旧版回执、缺失、成环）立即终止。条目按最新在前返回，身份再由评估器逐条核对。
# 函数用途: 收集当前会话最近若干次实验请求记录中的对照条目。
def experiment_chain_entries(paths: object, head: dict | None, *, thread_id: str) -> list[dict]:
    entries: list[dict] = []
    request, seen = head, set()
    for _ in range(_CHAIN_REQUESTS):
        request_id = str(request.get("id") or "") if isinstance(request, dict) else ""
        if not request_id or request_id in seen or not _granted_for(request, thread_id):
            break
        seen.add(request_id)
        entries.extend(reversed(_records_block(request)["entries"]))
        previous = _previous_request_id(request)
        request = load_gateway_request_record(paths, previous) if previous else None
    return entries


# LLM: 请求记录路径固定为 <root>/requests/<folder>/<id>.json，与原回合转换锁同一推导；不信任 agent 自身的 Gateway 配置。
# 函数用途: 由本请求记录路径得到原 Gateway 队列目录。
def _request_paths(request_path) -> object:
    return gateway_paths_from_root(request_path.parent.parent.parent)


# LLM: 供晋升前复核：从本请求（内存，与文件同步）起读证据链，owner 取宿主 Agent，thread 取授权回执；只读。
# 函数用途: 评估当前会话最近的 skill_tool 实验样本。
def request_experiment_evaluation(context: object, *, thread_id: str) -> dict:
    entries = experiment_chain_entries(_request_paths(context.request_path), context.request, thread_id=thread_id)
    return evaluate_skill_tool_samples(entries, owner_ref=decision_owner_ref(context.agent), thread_id=thread_id)


# LLM: 读路径只在已有授权信封时由 user_config decision_read 调用；head 为信封来源请求，本请求则用内存副本。
#   返回只读评估、授权动作与该请求上的晋升回执；不写任何文件，也不因模型读取而触发晋升。
# 函数用途: 为设置读取附上当前会话实验证据的评估结果。
def thread_experiment_evaluation(writer: object, agent: object, *, thread_id: str, authorization: dict) -> dict:
    paths = _request_paths(writer.request_path)
    head_id = authorization["source"]["request_id"]
    head = writer.request if head_id == writer.request_id and isinstance(writer.request, dict) else (
        load_gateway_request_record(paths, head_id))
    entries = experiment_chain_entries(paths, head, thread_id=thread_id)
    evaluation = evaluate_skill_tool_samples(entries, owner_ref=decision_owner_ref(agent), thread_id=thread_id)
    return {**evaluation, "authorization_id": authorization["authorization_id"],
            "authorized_operations": list(authorization["operations"]),
            "latest_promotion": _records_block(head).get("promotion") if isinstance(head, dict) else None}


__all__ = ["EXPERIMENT_RECORDS_KEY", "experiment_chain_entries", "finish_decision_experiment_turn",
           "load_gateway_request_record", "observe_capability_presentation", "realized_tool_usage",
           "record_decision_experiment_sample", "request_experiment_evaluation", "run_in_turn",
           "thread_experiment_evaluation", "update_experiment_records"]
