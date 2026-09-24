# LLM: 只读同 run/task 原 run_command 归档信封中的验证事实；可选决策只能选一个已有复核焦点，不跑测试、不补读，
# 不改 ToolResult、归档、验证账、Goal、Todo 或收口。路径、原命令和输出只进本地版本摘要，绝不发送给决策模型。
# 模块用途: run_command 产生新验证事件后，可选地提示主模型交付前先复核哪个已有验证焦点；关闭、失败或无法安全投影时保留原展示。
from __future__ import annotations

import hashlib
import re
import time

from ...backends.decision_protocol import DecisionInputError, decision_json
from ...common.cancellation import ToolCancelled, bind_cancellation_token, raise_if_cancelled
from ...concurrency.interrupt import is_interrupted
from ...conversation.decision_service import (
    begin_decision_stage,
    decide,
    decision_outcome_is_current,
)
from ...runtime_context import current_subagent_run_id
from ...settings.decision_settings_schema import POINT_RUNTIME_SCOPES
from ...tooling.output_projection import project_tool_output_body
from ...tooling.runtime_contracts import ToolCall, ToolResult

# URL 查询串拒绝沿用外部材料首片的同一策略，不在本点另立第二份规则。
from .external_material_order import _URL_WITH_QUERY

_POINT = "delivery_quality"
_TOOL = "run_command"
_QUESTION = "review_focus"
_MIN_FOCUSES = 2
_MAX_FOCUSES = 12
_MAX_REQUEST_CHARS = 1024
_MAX_HINT_CHARS = 512
_HINT_TAG = "[delivery-review-focus]"
# 宿主分类值只按短标识校验，不是封闭枚举；形态异常时放弃增强，不猜测含义。
_FACT_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_FACT_KEYS = ("kind", "scope", "status")
_LOCAL_KEYS = ("root", "canonical_command", "created_at")
_NON_SELECTIONS = {
    "not_needed": "无需额外复核建议，保留原结果",
    "no_match": "现有焦点均不适合优先复核",
    "abstain": "无法可靠判断",
    "need_data": "现有事实不足以判断；本增强不补读材料、不运行测试，保留原结果",
}
_INSTRUCTIONS = ("依据当前请求，从本轮已有验证焦点中选一个交付前最值得主模型先复核的焦点。order 越大越新；"
                 "edited_after 表示该验证之后同一项目又有文件修改。只能选择已有焦点，"
                 "不能要求运行测试、补读材料或判定任务完成。")


# LLM: 仅在独立点注册后进入；同一阶段期限覆盖准备/发送/采用，真实停止传播，普通故障只返回空串且不影响已执行结果。
# 函数用途: 调用原决策服务并返回临时复核提示；不保存状态、不执行工具，调用用量沿原账本登记。
def delivery_quality_hint(agent: object, record: object, archive_record: dict) -> str:
    if _POINT not in POINT_RUNTIME_SCOPES:
        return ""
    token = getattr(getattr(record, "params", None), "cancellation_token", None)
    try:
        with bind_cancellation_token(token):
            return _advise(agent, record, archive_record)
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:
        with bind_cancellation_token(token):
            _check_interrupted()
        return ""


# LLM: 资格与材料都只读结构化事实；采用前在配置复核之后再比对原来源/参数版本与同一绝对期限，任一变化都不采用。
# 函数用途: 完成一次“资格→阶段→材料→决策→复核→渲染”，调用方负责取消绑定和普通故障回退。
def _advise(agent: object, record: object, archive: dict) -> str:
    if not _eligible(agent, record, archive):
        return ""
    params = record.params
    stage = begin_decision_stage(agent, params, operation_id=_operation_id(record))
    if stage.error_code or _POINT not in stage.enabled_points or stage.run_id != record.call.run_id:
        return ""
    _check_interrupted()
    state, questions, revision = _material(record, archive)
    frozen = (_context_revision(params), revision)
    outcome = decide(agent, params, stage, point=_POINT, state=state, questions=questions,
                     candidates_revision=revision, source_refs=(archive["scoped_call_id"],))
    _check_interrupted()
    if (not outcome.may_apply or outcome.response is None
            or outcome.response.binding.candidates_revision != revision):
        return ""
    # 渲染可能读到等待期间变化后的来源；最后的来源版本比对会整体丢弃这种建议。
    hint = _render_hint(_selected_focus(outcome.response, _focuses(record, archive)))
    if not hint or not decision_outcome_is_current(agent, params, stage, outcome):
        return ""
    _check_interrupted()
    if _current_sources(agent, record, archive) != frozen:
        return ""
    return hint if time.monotonic() < min(stage.deadline, outcome.deadline) else ""


# LLM: 触发只看结构化事实：当前 run_command 的新验证事件、原调用/归档配对、主代理、无收口标记，
# 同 run/task 的 2—12 个焦点且至少一个 failed 或其后有修改；不读输出正文或自然语言。
# 函数用途: 判断这条工具回执能否进入可选复核建议；不满足时原展示不变且不发请求。
def _eligible(agent: object, record: object, archive: dict) -> bool:
    if not _current_record_matches(agent, record, archive):
        return False
    focuses = _focuses(record, archive)
    return (_MIN_FOCUSES <= len(focuses) <= _MAX_FOCUSES and any(focus["current"] for focus in focuses)
            and any(focus["status"] == "failed" or focus["edited_after"] for focus in focuses))


# LLM: 原 ToolCall/ToolResult 与归档须在 tool/id/run/task/scoped_call_id 和验证事件上一致；归档须以同一对象出现在
# 本 run 列表中（由 _eligible 的 current 焦点保证）。非 run_command 在扫描归档前即返回；子代理、重复失败/未知副作用收口、
# 重放或超长/空请求都直接放弃，不读取正文推断。
# 函数用途: 核对当前回执的身份、执行事实与归档配对，给焦点扫描提供可信的“当前事件”。
def _current_record_matches(agent: object, record: object, archive: dict) -> bool:
    call, result, params = getattr(record, "call", None), getattr(record, "result", None), getattr(record, "params", None)
    if (not isinstance(call, ToolCall) or not isinstance(result, ToolResult) or call.tool_name != _TOOL
            or result.tool_name != call.tool_name or result.call_id != call.call_id or result.handler_executed is not True
            or current_subagent_run_id(agent) or getattr(params, "repeated_failure_halt", None) is not None
            or getattr(params, "unknown_outcome_halt", None) is not None or not _request_text(params)):
        return False
    details = result.metadata.get("handler_details")
    evidence = details.get("verification_evidence") if type(details) is dict else None
    envelope = archive.get("tool_result_envelope") if type(archive) is dict else None
    return (type(evidence) is dict and type(envelope) is dict and envelope.get("verification_evidence") == evidence
            and archive.get("tool") == call.tool_name and archive.get("id") == call.call_id
            and archive.get("run_id") == call.run_id and archive.get("task_id") == getattr(params, "task_id", "")
            and type(archive.get("scoped_call_id")) is str and bool(archive["scoped_call_id"]))


# LLM: 当前请求只取原 params.user_prompt 完整原文；超过上限或为空时整点跳过（空串本身为假），绝不发送截断片段。
# 函数用途: 返回可用的有界当前请求，不合格时返回空串。
def _request_text(params: object) -> str:
    value = getattr(params, "user_prompt", "")
    return value if type(value) is str and len(value) <= _MAX_REQUEST_CHARS else ""


# LLM: 每个 (root, kind, scope) 只保留最新事件；edited_after 只认同 run/task、其后同 root 的 stale 状态。
# 同一事件编号重复出现视为来源不可信并放弃；root/原命令等本地事实只供本地版本比较与宿主渲染。
# 函数用途: 按归档时间顺序列出本轮可复核的验证焦点，标出当前事件和其后是否有文件修改。
def _focuses(record: object, archive: dict) -> list[dict]:
    events, edits = _scan(record, archive)
    ids = [event["id"] for event in events]
    if len(set(ids)) != len(ids):
        raise DecisionInputError("同一验证事件在本轮归档中重复出现。")
    latest = {(event["root"], event["kind"], event["scope"]): event for event in events}
    ordered = sorted(latest.values(), key=lambda event: event["position"])
    return [{**event, "edited_after": any(position > event["position"] and root == event["root"]
                                          for position, root in edits)} for event in ordered]


# LLM: 只读 params.archive_tool_calls 中同 run/task 的归档信封；run_command 的 verification_evidence 是焦点，
# 任意工具的 verification_state stale 行是修改事实；不访问文件、验证账或工具输出。
# 函数用途: 顺序扫描本轮归档，交回带位置的验证事件和 (位置, 项目根) 修改记录。
def _scan(record: object, archive: dict) -> tuple[list[dict], list[tuple[int, str]]]:
    call, params = record.call, record.params
    events, edits = [], []
    for position, item in enumerate(params.archive_tool_calls):
        envelope = item.get("tool_result_envelope") if type(item) is dict else None
        if type(envelope) is not dict or item.get("run_id") != call.run_id or item.get("task_id") != params.task_id:
            continue
        edits.extend((position, root) for root in _stale_roots(envelope.get("verification_state")))
        if item.get("tool") == _TOOL and "verification_evidence" in envelope:
            events.append({**_evidence_fact(envelope["verification_evidence"]), "position": position,
                           "current": item is archive, "scoped_call_id": item.get("scoped_call_id")})
    return events, edits


# LLM: 事件须带正整数 id、整数退出码、短标识 kind/scope/status 及非空本地 root/命令/时间；缺项整点放弃，不补默认值。
# 函数用途: 校验并复制一条原验证事件的宿主事实。
def _evidence_fact(value: object) -> dict:
    if (type(value) is not dict or type(value.get("id")) is not int or value["id"] <= 0
            or type(value.get("exit_code")) is not int or value["exit_code"].bit_length() > 64
            or any(type(value.get(key)) is not str or not _FACT_TOKEN.fullmatch(value[key]) for key in _FACT_KEYS)
            or any(type(value.get(key)) is not str or not value[key] for key in _LOCAL_KEYS)):
        raise DecisionInputError("验证事件缺少宿主结构化事实。")
    return {key: value[key] for key in ("id", "exit_code", *_FACT_KEYS, *_LOCAL_KEYS)}


# LLM: verification_state 须是原写入工具生成的对象列表；只有 status=stale 的行算“其后有修改”，缺项目根放弃。
# 函数用途: 取出一次文件写入后被标为过期的项目根。
def _stale_roots(value: object) -> list[str]:
    if value is None:
        return []
    if type(value) is not list or any(type(row) is not dict for row in value):
        raise DecisionInputError("文件修改后的验证状态格式无效。")
    roots = [row.get("root") for row in value if row.get("status") == "stale"]
    if any(type(root) is not str or not root for root in roots):
        raise DecisionInputError("过期验证状态缺少项目根。")
    return roots


# LLM: 外部 payload 只有脱敏当前请求和焦点的 candidate/project 别名/kind/scope/status/exit_code/edited_after/order；
# 路径、原命令、输出、改动路径和时间只进本地版本摘要。题目唯一，候选之外只有保留原结果的非选择项。
# 函数用途: 冻结一次交付复核材料和单选题，并给等待后的来源复核生成版本值。
def _material(record: object, archive: dict) -> tuple[dict, dict, str]:
    focuses = _focuses(record, archive)
    projects: dict[str, str] = {}
    rows = []
    for order, focus in enumerate(focuses, 1):
        project = projects.setdefault(focus["root"], f"project_{len(projects) + 1}")
        rows.append({"candidate": f"focus_{order}", "project": project, "order": order,
                     "edited_after": focus["edited_after"], "exit_code": focus["exit_code"],
                     **{key: focus[key] for key in _FACT_KEYS}})
    state = {"current_request": _safe_request(_request_text(record.params)), "focuses": rows}
    criteria = {row["candidate"]: {key: value for key, value in row.items() if key != "candidate"} for row in rows}
    questions = {_QUESTION: {"type": "choice", "instructions": _INSTRUCTIONS,
                             "criteria": {**criteria, **_NON_SELECTIONS}}}
    revision = _digest({"state": state, "questions": questions, "sources": focuses,
                        "archive_ref": archive["scoped_call_id"], "archive_hash": archive.get("output_hash", "")})
    return state, questions, revision


# LLM: 复用外部材料首片的 external_data/default 脱敏和指令边界；含完整或协议相对 URL 查询串时整点放弃。
# 函数用途: 把当前请求投影成可发送的安全副本，无法确认安全时交调用方保留原结果。
def _safe_request(text: str) -> str:
    encoded = decision_json({"current_request": text}).decode("utf-8")
    if _URL_WITH_QUERY.search(encoded):
        raise DecisionInputError("当前请求含有不应转发的 URL 查询串。")
    return project_tool_output_body(tool=_POINT, output=encoded, trust="external_data", redaction="default")


# LLM: 等待前后都用同一函数取来源快照；不合格时返回空版本，调用方据此放弃采用。
# 函数用途: 返回当前参数版本与材料版本，用于比较决策等待期间来源是否变化。
def _current_sources(agent: object, record: object, archive: dict) -> tuple[str, str]:
    if not _eligible(agent, record, archive):
        return "", ""
    return _context_revision(record.params), _material(record, archive)[2]


# LLM: 只接受唯一题目的一次 choice 回答，且值必须是本次宿主生成的 focus 编号；缺项、多答、逐题错误或非选择都返回 None。
# 函数用途: 把合法选择映射回宿主焦点事实，其他结果一律保留原展示。
def _selected_focus(response: object, focuses: list[dict]) -> dict | None:
    answers = getattr(response, "answers", ())
    if len(answers) != 1:
        return None
    answer = answers[0]
    if answer.question_id != _QUESTION or answer.kind != "choice" or answer.error_code:
        return None
    return {f"focus_{order}": focus for order, focus in enumerate(focuses, 1)}.get(answer.value)


# LLM: 提示只渲染宿主验证过的事件编号/kind/scope/status/其后修改，不复制模型文案；选中当前事件时无需追加。
# 函数用途: 为当前工具回执生成一段可忽略的复核建议，超过展示预算则完全不追加。
def _render_hint(focus: dict | None) -> str:
    if focus is None or focus["current"]:
        return ""
    facts = f"{focus['kind']}/{focus['scope']}，{focus['status']}" + ("，其后有修改" if focus["edited_after"] else "")
    scope_note = "，targeted 不代表全量" if focus["scope"] == "targeted" else ""
    hint = (f"{_HINT_TAG}\n可选复核建议：交付前可先复核本轮验证事件 #{focus['id']}（{facts}）；"
            f"范围与结果以原事实为准{scope_note}。")
    return hint if len(hint) <= _MAX_HINT_CHARS else ""


# LLM: 原 call 身份决定一次建议的操作编号；不从正文或模型选项推断 owner/run/task，也不创建持久操作。
# 函数用途: 将现有调用编号绑定到原决策账本，防止不同回合混用建议。
def _operation_id(record: object) -> str:
    call = record.call
    return _POINT + ":" + _digest({"call_id": call.call_id, "run_id": call.run_id,
                                   "turn_id": call.turn_id, "attempt_id": call.attempt_id})


# LLM: 摘要共用原有界 JSON；只比较本轮内存快照，不保存或上传本地路径等来源。
# 函数用途: 生成短版本值，坏类型或超出原输入上限会放弃增强。
def _digest(value: object) -> str:
    return hashlib.sha256(decision_json(value)).hexdigest()


# LLM: 参数/权限版本只在宿主内比较，绝不上传 task_attributes、write_boundary 或原工具参数。
# 函数用途: 判断等待期间当前任务、问题、权限和工具快照是否变化。
def _context_revision(params: object) -> str:
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    return _digest({
        "request_id": getattr(params, "request_id", ""), "run_id": getattr(params, "run_id", ""),
        "task_id": getattr(params, "task_id", ""), "user_prompt": getattr(params, "user_prompt", ""),
        "context_scope": getattr(params, "context_scope", ""),
        "task_attributes": getattr(params, "task_attributes", None),
        "allowed_tools": getattr(params, "allowed_tools", None),
        "write_boundary": getattr(params, "write_boundary", None),
        "snapshot_hash": getattr(snapshot, "snapshot_hash", ""),
        "available_tools": sorted(getattr(snapshot, "available_tool_names", ())),
    })


# LLM: 用户取消在发送和消费边界传播，不能降级成一次可忽略的增强失败。
# 函数用途: 沿原工具 token 与运行中断事实终止等待或采用。
def _check_interrupted() -> None:
    raise_if_cancelled()
    if is_interrupted():
        raise InterruptedError("交付复核建议随当前运行停止。")
