# LLM: 审计只读 Gateway 请求记录里宿主写下的结构化事实：决策观察块（model_selection_observation、capability_presentation_observation）
#   与请求结果（状态、错误码、渠道、私聊/群聊、耗时）。决策观察按 conversation_claim.thread_id 归属 owner；请求结果优先用
#   宿主写进终态响应的 owner_id，旧记录再退回线程归属。字段白名单投影；不读 prompt/goal/正文/附件/用户可见文案，也不读日志。
#   扫描有界：只看修改时间落在窗口内的记录，按新到旧最多读 _SCAN_LIMIT 份，超出如实标 truncated。只读，不写任何文件。
#   调用方：GatewayTaskBindingWriter.decision_audit_observations / request_audit_outcomes（audit_records 工具经宿主运行参数调用）。
# 模块用途: 为审计工具提供 Gateway 请求记录里的决策观察和请求结果（谁的请求、成败、错误码与处理建议），不含任何对话内容。
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .io import read_json_file_report
from .request_binding import CAPABILITY_OBSERVATION_KEY, MODEL_OBSERVATION_KEY

# 一次审计最多读这么多份请求记录（按修改时间新到旧），保证工具调用有界
_SCAN_LIMIT = 300
# 选模型观察只投影这些宿主写下的结构化字段
_MODEL_FIELDS = ("status", "reason", "requested_mode", "choice", "adopted", "adoption_eligibility", "frozen_profile_id")
# 能力推荐观察只投影这些字段（不含工具名清单等细节）
_CAPABILITY_FIELDS = ("point", "mode", "status", "reason", "adopted", "retain_reason")


# LLM: 只取标量字段；非标量（列表、字典）一律丢弃，防止把大块内容带进审计结果。
# 函数用途: 按白名单从一个观察块里取出可展示的字段。
def _pick(block: dict, fields: tuple[str, ...]) -> dict:
    return {key: block[key] for key in fields if isinstance(block.get(key), (str, int, float, bool))}


# LLM: 一份请求记录可同时有选模型观察（至多一条）和能力推荐观察（至多 8 条）；结构不符的块按不存在处理。
# 函数用途: 把一份请求记录里的决策观察转成审计条目列表。
def _observations(payload: dict) -> list[dict]:
    result = []
    model = payload.get(MODEL_OBSERVATION_KEY)
    if isinstance(model, dict):
        result.append({"kind": "model_selection", "point": "model_selection", **_pick(model, _MODEL_FIELDS)})
    capability = payload.get(CAPABILITY_OBSERVATION_KEY)
    entries = capability.get("entries") if isinstance(capability, dict) else None
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict):
            result.append({"kind": "capability_presentation", **_pick(entry, _CAPABILITY_FIELDS)})
    return result


# LLM: 只列目录项与 stat，不读内容；缺目录按空处理。
# 函数用途: 列出一个队列目录里修改时间在窗口内的请求记录文件（修改时间、路径）。
def _folder_candidates(folder: Path, since: float) -> list[tuple[float, Path]]:
    try:
        with os.scandir(folder) as iterator:
            stamped = [(entry.stat().st_mtime, Path(entry.path)) for entry in iterator
                       if entry.name.endswith(".json") and entry.is_file()]
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [item for item in stamped if item[0] >= since]


# LLM: 同一请求可能短暂同时出现在两个目录（终态投影与 done），交给调用方按编号去重；按修改时间新到旧排序。
# 函数用途: 收集窗口内全部队列目录的请求记录文件。
def _candidates(paths: object, since: float) -> list[tuple[float, Path]]:
    found = [item for folder in (paths.terminal, paths.processing, paths.done, paths.failed)
             for item in _folder_candidates(folder, since)]
    found.sort(key=lambda item: item[0], reverse=True)
    return found


# LLM: thread_owners 把线程编号映射到 owner 编号，由调用方按已认证 owner（或管理员许可的全部 owner）解析得到；
#   不在映射里的请求一律不返回。坏文件只计数。结果最新在前，至多 limit 条。
# 函数用途: 读取窗口内属于这些会话的决策观察，并报告扫描范围与是否截断。
def decision_observation_records(paths: object, *, thread_owners: dict[str, str], since: float, limit: int) -> dict:
    candidates = _candidates(paths, since)
    entries: list[dict] = []
    seen: set[str] = set()
    unreadable = 0
    for mtime, path in candidates[:_SCAN_LIMIT]:
        report = read_json_file_report(path, context="gateway.audit_records.read")
        if report.load_error is not None:
            unreadable += 1
            continue
        payload = report.payload
        request_id = str(payload.get("id") or path.stem)
        claim = payload.get("conversation_claim")
        thread_id = str(claim.get("thread_id") or "") if isinstance(claim, dict) else ""
        if request_id in seen or thread_id not in thread_owners:
            continue
        seen.add(request_id)
        for item in _observations(payload):
            entries.append({**item, "request_id": request_id, "thread_id": thread_id,
                            "owner_id": thread_owners[thread_id], "recorded_at": round(mtime, 3)})
    return {"available": True, "entries": entries[:max(0, limit)], "matched": len(entries),
            "scanned": min(len(candidates), _SCAN_LIMIT), "in_window": len(candidates),
            "truncated": len(candidates) > _SCAN_LIMIT or len(entries) > limit, "unreadable": unreadable}


UNATTRIBUTED_OWNER = "unattributed"


# LLM: 归属只认两种结构化事实：宿主写进终态响应的 owner_id（权威），或旧记录的 conversation_claim.thread_id 落在调用方
#   给出的线程映射里；都没有就返回空串（未归属），不按 user_id 或渠道猜。
# 函数用途: 判断一份请求记录属于哪个 owner。
def _record_owner(payload: dict, thread_owners: dict[str, str]) -> str:
    terminal = payload.get("terminal_response") if isinstance(payload.get("terminal_response"), dict) else {}
    owner_id = str(terminal.get("owner_id") or payload.get("owner_id") or "").strip()
    if owner_id:
        return owner_id
    claim = payload.get("conversation_claim") if isinstance(payload.get("conversation_claim"), dict) else {}
    return thread_owners.get(str(claim.get("thread_id") or ""), "")


# LLM: 错误码的处理建议取自唯一错误分类表（类别、可否重试、建议动作、恢复提示），不读异常原文或用户可见文案。
# 函数用途: 把一个错误码变成可展示的处理建议。
def _error_summary(code: str) -> dict:
    from ..contracts.error_taxonomy import error_contract

    contract = error_contract(code)
    return {"category": contract.category, "retryable": contract.retryable,
            "recommended_action": contract.recommended_action, "recovery_hint": contract.recovery_hint}


# LLM: 只投影标量白名单字段；终态字段优先取 terminal_response，再退回记录顶层。
# 函数用途: 把一份请求记录变成一条请求结果审计条目。
def _outcome_entry(payload: dict, mtime: float, owner_id: str) -> dict:
    terminal = payload.get("terminal_response") if isinstance(payload.get("terminal_response"), dict) else {}
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    code = str(payload.get("error_code") or terminal.get("error_code") or "").strip()
    entry = {"request_id": str(payload.get("id") or payload.get("request_id") or ""), "recorded_at": round(mtime, 3),
             "owner_id": owner_id or UNATTRIBUTED_OWNER, "channel": str(meta.get("channel") or conversation.get("channel") or ""),
             "chat_type": str(meta.get("channel_chat_type") or ""), "status": str(payload.get("status") or terminal.get("status") or ""),
             "error_code": code}
    entry.update(_pick(terminal, ("duration_seconds", "tool_rounds")))
    if code:
        entry["error"] = _error_summary(code)
    return entry


# LLM: allowed_owners 为 None 只能由已通过跨用户审计许可的调用方传入（全部 owner，含未归属）；thread_id 非空时只看这个会话
#   （按 conversation_claim.thread_id）；thread_owners 只用于给没有 owner_id 的旧记录归属。since/limit 为时间窗与条数上限。
# 类用途: 一次请求结果审计的过滤条件。
@dataclass(frozen=True)
class OutcomeQuery:
    allowed_owners: frozenset | None
    thread_owners: dict
    thread_id: str
    since: float
    limit: int


# 函数用途: 判断一份请求记录是否落在本次查询的 owner 与会话范围内。
def _outcome_in_scope(payload: dict, owner_id: str, query: OutcomeQuery) -> bool:
    if query.allowed_owners is not None and owner_id not in query.allowed_owners:
        return False
    if not query.thread_id:
        return True
    claim = payload.get("conversation_claim") if isinstance(payload.get("conversation_claim"), dict) else {}
    return str(claim.get("thread_id") or "") == query.thread_id


# LLM: 同一请求在 done/failed 与 terminal 各有一份，按请求编号去重；坏文件只计数。结果最新在前，至多 limit 条，
#   另附按状态、错误码、渠道的计数。只读，不写任何文件。
# 函数用途: 读取窗口内的请求结果，回答“哪些请求失败了、为什么、属于谁”。
def request_outcome_records(paths: object, query: OutcomeQuery) -> dict:
    candidates = _candidates(paths, query.since)
    entries: list[dict] = []
    seen: set[str] = set()
    unreadable = 0
    for mtime, path in candidates[:_SCAN_LIMIT]:
        report = read_json_file_report(path, context="gateway.audit_records.outcomes.read")
        if report.load_error is not None:
            unreadable += 1
            continue
        request_id = str(report.payload.get("id") or path.stem)
        owner_id = _record_owner(report.payload, query.thread_owners)
        if request_id in seen or not _outcome_in_scope(report.payload, owner_id, query):
            continue
        seen.add(request_id)
        entries.append(_outcome_entry(report.payload, mtime, owner_id))
    return {"available": True, "entries": entries[:max(0, query.limit)], "counts": _outcome_counts(entries),
            "matched": len(entries), "scanned": min(len(candidates), _SCAN_LIMIT), "in_window": len(candidates),
            "truncated": len(candidates) > _SCAN_LIMIT or len(entries) > query.limit, "unreadable": unreadable}


# 函数用途: 按状态、错误码、渠道汇总请求结果条数。
def _outcome_counts(entries: list[dict]) -> dict:
    counts: dict[str, dict[str, int]] = {"status": {}, "error_code": {}, "channel": {}}
    for entry in entries:
        for field in counts:
            key = str(entry.get(field) or "none")
            counts[field][key] = counts[field].get(key, 0) + 1
    return counts


__all__ = ["UNATTRIBUTED_OWNER", "OutcomeQuery", "decision_observation_records", "request_outcome_records"]
