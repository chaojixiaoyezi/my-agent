"""直属下级未决能力申请的只读收件箱：按已证明的管理身份分页列出，不改动任何状态。"""

# LLM: 收件箱只是现有 SubAgentTask.capability_requests / capability_grants / 续跑记录的有界投影，
#   不新建申请/授权/队列账本，也不唤醒、续跑或自动授权；裁决仍只走 resolve_capability_requests。
# 模块用途: 汇总当前代理（含已证明的定时续查父任务）直属下级的能力申请，并如实标出覆盖范围和未知项。

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from typing import Any

from ...subagents.model_capabilities import (
    CAPABILITY_REQUEST_OPEN_STATUS,
    CAPABILITY_REQUEST_TERMINAL_STATUSES,
    normalize_capability_request_status,
)
from .management_continuity import ManagementPrincipal, management_principals

SCHEMA_VERSION = "pending_inbox.v1"
CURSOR_VERSION = 1
# LLM: 单页硬上限，配置值和模型参数都不能越过它；每页加载的子代理记录数同样受这一页大小约束。
# 常量用途: 限制一次收件箱读取的条目数和子代理记录读取数。
MAX_PAGE_ITEMS_COUNT = 200
RESOLVER_TOOL = "resolve_capability_requests"
EXCLUDED_ITEM_KINDS = ("tool_approval",)
_DENY_DECISION = "deny"
_AUTO_GRANT_MARKER = "capability_auto_grant"


# LLM: 参数错误与游标越权都用结构化错误码返回，调用方按码处理，不解析中文说明。
# 类用途: 收件箱读取被拒时携带稳定错误码，供工具层转成结构化失败。
class PendingInboxError(ValueError):
    # LLM: 只保存错误码和说明，不携带任何申请数据，越权游标不能借错误回显内容。
    # 函数用途: 记录错误码与给人看的说明。
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# LLM: 只读：不 save/mutate/wake/claim；授权来源只取 management_principals，模型参数不能指定父级或子代理。
# 函数用途: 返回一页直属下级能力申请及其决定、执行与覆盖事实。
def pending_inbox_page(agent: Any, *, cursor: str = "", limit: int = 0, include_resolved: bool = False) -> dict[str, object]:
    principals, warnings = management_principals(agent)
    principals = tuple(sorted(principals, key=lambda item: item.run_id))
    page_limit = _page_limit(agent, limit)
    scope = _scope_digests(agent, principals)
    position = _decode_cursor(cursor, scope)
    coverage: dict[str, Any] = {"children_scanned": 0, "child_load_errors": [], "index_errors": [],
                                "runtime_reads_failed": 0, "truncated": False}
    items: list[dict[str, object]] = []
    last: list[str] | None = None
    for principal in principals:
        if position and principal.run_id < position[0]:
            continue
        resume = position if position and principal.run_id == position[0] else None
        stop = _scan_principal(agent, principal, resume, page_limit, include_resolved, items, coverage)
        if stop is not None:
            last = stop
            break
    complete = not (coverage["child_load_errors"] or coverage["index_errors"] or coverage["runtime_reads_failed"])
    return {
        "schema_version": SCHEMA_VERSION,
        "effect": "read_only",
        "items": items,
        "next_cursor": _encode_cursor(scope, last) if last else None,
        "page_counts": _page_counts(items),
        "coverage": {
            "state": "complete" if complete and last is None else "partial",
            "principals": [{"run_id": item.run_id, "kind": item.kind} for item in principals],
            "page_limit": page_limit,
            "excluded_item_kinds": list(EXCLUDED_ITEM_KINDS),
            **coverage,
        },
        "scope_resolution": {"principals": [item.run_id for item in principals], "warnings": list(warnings)},
    }


# LLM: 子代理按 run_id 键集分页、申请按 id 排序；读满一页就把最后位置交给游标，后面的子代理不会被前面的
#   同一批记录永久挡住。每页加载的子代理数与条目数共用 page_limit 上界。
# 函数用途: 扫描一个管理身份的直属下级，追加条目；读满时返回续读位置。
def _scan_principal(agent: Any, principal: ManagementPrincipal, resume: list[str] | None, page_limit: int,
                    include_resolved: bool, items: list[dict[str, object]], coverage: dict[str, Any]) -> list[str] | None:
    after_child, after_request = (resume[1], resume[2]) if resume else ("", "")
    while True:
        budget = page_limit - coverage["children_scanned"]
        if budget <= 0:
            coverage["truncated"] = True
            return [principal.run_id, after_child, ""]
        try:
            child_ids = _child_ids(agent, principal.run_id, after_child, bool(after_request), budget)
        except Exception as exc:  # noqa: BLE001 - 索引不可读只能标成部分覆盖，不能当作没有申请。
            coverage["index_errors"].append({"parent_run_id": principal.run_id, "error_type": type(exc).__name__})
            return None
        if not child_ids:
            return None
        children = _load_children(agent, child_ids, coverage)
        for child_id in child_ids:
            if len(items) >= page_limit:
                coverage["truncated"] = True
                return [principal.run_id, after_child, ""]
            coverage["children_scanned"] += 1
            child = children.get(child_id)
            if child is None or str(getattr(child, "parent_id", "") or "") != principal.run_id:
                after_child, after_request = child_id, ""
                continue
            skip_through = after_request if child_id == after_child else ""
            stop = _append_child_items(agent, principal, child, skip_through, page_limit, include_resolved, items, coverage)
            if stop is not None:
                coverage["truncated"] = True
                return [principal.run_id, child_id, stop]
            after_child, after_request = child_id, ""
        if len(child_ids) < budget:
            return None


# LLM: 断点之后的申请按 id 排序依次追加；一页满时返回本子代理最后列出的 id，下页从同一子代理的下一条继续。
#   调用前已保证页未满，所以本子代理至少列出一条后才可能在这里停下。
# 函数用途: 把一个直属下级的申请转成收件箱条目。
def _append_child_items(agent: Any, principal: ManagementPrincipal, child: Any, skip_through: str, page_limit: int,
                        include_resolved: bool, items: list[dict[str, object]], coverage: dict[str, Any]) -> str | None:
    requests = sorted(getattr(child, "capability_requests", None) or [], key=lambda item: str(getattr(item, "id", "")))
    runtime_cache: dict[str, Any] = {}
    emitted = ""
    for request in requests:
        request_id = str(getattr(request, "id", "") or "")
        if skip_through and request_id <= skip_through:
            continue
        state = _request_state(getattr(request, "status", ""))
        if not include_resolved and state not in {CAPABILITY_REQUEST_OPEN_STATUS, "INVALID"}:
            continue
        if len(items) >= page_limit:
            return emitted or skip_through
        items.append(_item(agent, principal, child, request, state, runtime_cache, coverage))
        emitted = request_id
    return None


# LLM: 决定只取已记录的证据：授权看同 request_id 的 CapabilityGrant，拒绝看原 resolve 写下的续跑记录；
#   单凭 CLOSED 或子代理现状不推断是谁、何时做的决定。执行同理：没有申请到执行轮的因果记录就不报已续跑。
# 函数用途: 组装一条申请的范围、等待、决定与执行事实。
def _item(agent: Any, principal: ManagementPrincipal, child: Any, request: Any, state: str,
          runtime_cache: dict[str, Any], coverage: dict[str, Any]) -> dict[str, object]:
    child_id = str(getattr(child, "id", "") or "")
    request_id = str(getattr(request, "id", "") or "")
    created_at = float(getattr(request, "created_at", 0.0) or 0.0)
    waiting = state in {CAPABILITY_REQUEST_OPEN_STATUS, "INVALID"}
    continuation = _continuation(child, request_id)
    return {
        "item_id": f"cap:{child_id}:{request_id}",
        "kind": "capability_request",
        "request_id": request_id,
        "applicant": {"run_id": child_id, "role": str(getattr(child, "role", "") or ""),
                      "status": str(getattr(child, "status", "") or "")},
        "scope": {key: _plain(getattr(request, key, None)) for key in (
            "capability_type", "requested_tools", "requested_skills", "requested_mcp_tools", "requested_commands",
            "cwd_scope", "path_scope", "network_scope", "risk_level", "output_budget")},
        "reason": {key: str(getattr(request, key, "") or "") for key in ("problem", "needed_capability", "expected_output")},
        "handling_parent": {"run_id": principal.run_id, "principal_kind": principal.kind, "resolver_tool": RESOLVER_TOOL},
        "request_state": state,
        "raw_status": str(getattr(request, "status", "") or ""),
        "wait": {"waiting": waiting, "created_at": created_at or None,
                 "age_seconds": round(max(0.0, time.time() - created_at), 3) if waiting and created_at else None},
        "decision": _decision(child, request, state, continuation),
        "execution": _execution(agent, child_id, state, continuation, runtime_cache, coverage),
    }


# LLM: GAP 是路由没找到能力，不是父级决定；CLOSED 只有原 deny 续跑记录点名本申请时才算拒绝，否则如实写未记录。
# 函数用途: 从授权记录和拒绝续跑记录得出决定事实。
def _decision(child: Any, request: Any, state: str, continuation: dict[str, Any] | None) -> dict[str, object]:
    request_id = str(getattr(request, "id", "") or "")
    if state == "GRANTED":
        grant = next((item for item in getattr(child, "capability_grants", None) or []
                      if str(getattr(item, "request_id", "") or "") == request_id), None)
        if grant is None:
            return {"state": "granted_unrecorded", "evidence": "none", "decided_at": None}
        scope = getattr(grant, "request_scope", None) or {}
        return {"state": "granted", "evidence": "capability_grant", "grant_id": str(getattr(grant, "id", "") or ""),
                "decided_at": float(getattr(grant, "created_at", 0.0) or 0.0) or None,
                "auto_granted": isinstance(scope, dict) and scope.get("resolved_by") == _AUTO_GRANT_MARKER}
    if state == "CLOSED":
        if continuation is not None and continuation.get("decision") == _DENY_DECISION:
            return {"state": "denied", "evidence": "capability_resolution_continuation", "decided_at": None}
        return {"state": "closed_unattributed", "evidence": "none", "decided_at": None}
    if state == "GAP":
        return {"state": "gap", "evidence": "request_status", "decided_at": None}
    return {"state": "none", "evidence": "request_status", "decided_at": None}


# LLM: 现有续跑记录只有 queued_at/request_ids，执行轮没有回指它的字段；因此有了之后的执行轮也只能报 unknown，
#   绝不拿时间先后冒充因果。查看本身不唤醒、不续跑。
# 函数用途: 根据续跑记录和当前执行轮给出排队/未知状态。
def _execution(agent: Any, child_id: str, state: str, continuation: dict[str, Any] | None,
               runtime_cache: dict[str, Any], coverage: dict[str, Any]) -> dict[str, object]:
    if state in {CAPABILITY_REQUEST_OPEN_STATUS, "INVALID", "GAP"}:
        return {"state": "not_applicable"}
    if continuation is None:
        return {"state": "not_recorded"}
    queued_at = float(continuation.get("queued_at") or 0.0)
    base = {"continuation_status": str(continuation.get("status") or ""), "queued_at": queued_at or None}
    if child_id not in runtime_cache:
        runtime_cache[child_id] = _current_attempt(agent, child_id)
    attempt = runtime_cache[child_id]
    if attempt is _UNREADABLE:
        coverage["runtime_reads_failed"] += 1
        return {"state": "unknown", "reason": "RUNTIME_UNREADABLE", **base}
    if attempt is not None and float(attempt["started_at"] or 0.0) > queued_at:
        return {"state": "unknown", "reason": "NO_CAUSAL_LINK", "attempt_id": str(attempt["attempt_id"] or ""), **base}
    return {"state": "queued", **base}


_UNREADABLE = object()


# LLM: 子代理 run_id 先经运行库映射到权威 agent_run_id，再读它的当前执行轮指针；运行库缺失、记录缺失或报错
#   都标成不可读，不能当作“尚未执行”。
# 函数用途: 读取子代理当前执行轮。
def _current_attempt(agent: Any, child_id: str) -> Any:
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    if repo is None:
        return _UNREADABLE
    try:
        agent_run = repo.agent_run_for_run_id(child_id)
        if agent_run is None:
            return _UNREADABLE
        return repo.current_attempt(str(agent_run["agent_run_id"] or ""))
    except Exception:  # noqa: BLE001 - 读失败如实计入覆盖缺口。
        return _UNREADABLE


# LLM: 续跑记录每次裁决会覆盖成最近一次，request_ids 不含本申请时它描述的是别的申请，不能借用。
# 函数用途: 取出点名本申请的续跑记录。
def _continuation(child: Any, request_id: str) -> dict[str, Any] | None:
    record = (getattr(child, "attributes", None) or {}).get("capability_resolution_continuation")
    if not isinstance(record, dict) or request_id not in [str(item) for item in record.get("request_ids") or []]:
        return None
    return record


# LLM: 读运行索引的父子投影只用于定位，随后必须加载规范记录并复核 parent_id；索引不完整时抛错交给覆盖统计。
# 函数用途: 按 run_id 键集读取一个父级的下一批直属下级编号。
def _child_ids(agent: Any, parent_id: str, after: str, inclusive: bool, limit: int) -> list[str]:
    from ...subagents.manager import creation_run_index

    manager = getattr(agent, "subagents", None)
    index = creation_run_index(manager)
    if index is None:
        raise LookupError("subagent run index unavailable")
    identity = manager.persistence._run_index_identity()
    with index.connect() as db:
        db.execute("BEGIN")
        index._require_complete(db)
        rows = db.execute(
            f"SELECT run_id FROM run_lookup WHERE parent_id=? AND run_id {'>=' if inclusive else '>'} ? "
            "ORDER BY run_id LIMIT ?", (parent_id, after, limit)).fetchall()
    manager.persistence._require_run_index_identity(identity)
    return [str(row[0]) for row in rows]


# LLM: 一次批量读取本页子代理，单条读失败记入 child_load_errors，其余照常列出。
# 函数用途: 按编号加载规范子代理记录。
def _load_children(agent: Any, child_ids: list[str], coverage: dict[str, Any]) -> dict[str, Any]:
    report = agent.subagents.list_runs_by_ids_report(child_ids)
    for error in report.load_errors:
        coverage["child_load_errors"].append({"run_id": str(error.get("run_id") or ""),
                                              "error_type": str(error.get("error_type") or "")})
    return {str(task.id): task for task in report.runs}


# LLM: 非当前协议状态统一记 INVALID，仍需父级处理；不把旧标签或别名自动映射成当前状态。
# 函数用途: 把申请原始状态归成收件箱状态。
def _request_state(status: object) -> str:
    normalized = normalize_capability_request_status(status)
    if normalized == CAPABILITY_REQUEST_OPEN_STATUS or normalized in CAPABILITY_REQUEST_TERMINAL_STATUSES:
        return normalized
    return "INVALID"


# LLM: 计数只描述本页，不是 owner 全量统计。
# 函数用途: 统计本页各状态条目数。
def _page_counts(items: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item["request_state"]).lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


# LLM: 模型参数 1..200；省略或 0 时用配置；配置 0 表示不另设限制，仍受硬上限。
# 函数用途: 计算本页条目上限。
def _page_limit(agent: Any, limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0 or limit > MAX_PAGE_ITEMS_COUNT:
        raise PendingInboxError("TOOL_INVALID_ARGUMENTS", f"limit 必须是 1 到 {MAX_PAGE_ITEMS_COUNT} 的整数。")
    if limit:
        return limit
    from ...capability.config import CapabilityConfig
    from ...capability.runtime_config_reload import capability_config_for_agent

    config = capability_config_for_agent(agent) or CapabilityConfig()
    configured = int(getattr(config, "orchestration_pending_inbox_page_limit", 0) or 0)
    return min(configured, MAX_PAGE_ITEMS_COUNT) if configured > 0 else MAX_PAGE_ITEMS_COUNT


# LLM: 游标绑定 owner 与管理身份集合的摘要，换了身份或 owner 的游标不能续读别人的条目。
# 函数用途: 计算当前读取范围的摘要。
def _scope_digests(agent: Any, principals: tuple[ManagementPrincipal, ...]) -> dict[str, str]:
    owner = str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")
    ids = "|".join(item.run_id for item in principals)
    return {"o": hashlib.sha256(owner.encode()).hexdigest()[:16], "p": hashlib.sha256(ids.encode()).hexdigest()[:16]}


# LLM: 游标只含版本、范围摘要和位置，不含申请正文。
# 函数用途: 编码续读游标。
def _encode_cursor(scope: dict[str, str], position: list[str]) -> str:
    raw = json.dumps({"v": CURSOR_VERSION, **scope, "at": position}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


# LLM: 格式坏返回参数错误；范围摘要不符返回 CURSOR_SCOPE_MISMATCH 且不给任何数据。
# 函数用途: 解码并校验续读游标。
def _decode_cursor(cursor: object, scope: dict[str, str]) -> list[str] | None:
    if not cursor:
        return None
    try:
        text = str(cursor)
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        position = [str(item) for item in data["at"]]
        valid = data.get("v") == CURSOR_VERSION and len(position) == 3 and position[0]
    except (binascii.Error, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        raise PendingInboxError("TOOL_INVALID_ARGUMENTS", "cursor 格式无效，请从第一页重新读取。")
    if data.get("o") != scope["o"] or data.get("p") != scope["p"]:
        raise PendingInboxError("CURSOR_SCOPE_MISMATCH", "cursor 不属于当前管理身份，请从第一页重新读取。")
    return position


# LLM: 只复制结构化字段的普通值，避免把对象引用交给序列化层。
# 函数用途: 把列表/字典/标量转成可 JSON 序列化的副本。
def _plain(value: object) -> object:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


__all__ = ["MAX_PAGE_ITEMS_COUNT", "PendingInboxError", "SCHEMA_VERSION", "pending_inbox_page"]
