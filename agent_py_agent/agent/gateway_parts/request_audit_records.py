# LLM: 审计只读 Gateway 请求记录里宿主写下的结构化决策观察块（model_selection_observation、capability_presentation_observation），
#   按 conversation_claim.thread_id 是否属于调用方给出的线程集合归属 owner，字段白名单投影；不读 prompt/goal/正文/附件，也不读日志。
#   扫描有界：只看修改时间落在窗口内的记录，按新到旧最多读 _SCAN_LIMIT 份，超出如实标 truncated。只读，不写任何文件。
#   调用方：GatewayTaskBindingWriter.decision_audit_observations（audit_records 工具经宿主运行参数调用）。
# 模块用途: 为审计工具提供某些会话在 Gateway 请求记录里的决策观察（选模型建议、能力推荐），不含任何对话内容。
from __future__ import annotations

import os
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


__all__ = ["decision_observation_records"]
