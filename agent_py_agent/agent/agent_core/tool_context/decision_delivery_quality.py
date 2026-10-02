# LLM: 只读同 run/task 原归档信封中的验证事件及成功写入的 stale/last_verification_id；只选已有复核焦点，不跑测试、不补读，
# 不改 ToolResult、归档、验证账、Goal、Todo 或收口。路径、原命令和输出只进本地版本摘要，绝不发送给决策模型。
# 模块用途: 新验证或文件改后未复核时，可选地提示主模型先复核哪个已有焦点；单个 stale 无需选择，关闭或失败保留原展示。
from __future__ import annotations

import hashlib
import re
import time

from ...backends.decision_protocol import (
    DecisionInputError,
    DecisionPrivacySkip,
    decision_json,
    decision_request_excerpt,
)
from ...common.cancellation import ToolCancelled, bind_cancellation_token, raise_if_cancelled
from ...concurrency.interrupt import is_interrupted
from ...conversation import decision_point_limits as limits
from ...conversation.decision_outcome_log import (
    DROP_ADOPTION_DEADLINE,
    DROP_SOURCES_CHANGED,
    material_or_skip,
    record_decision_dropped,
)
from ...conversation.decision_reach_counts import CALLED, note_decision_reach, stage_miss_reason
from ...conversation.decision_service import (
    begin_decision_stage,
    decide,
    decision_outcome_is_current,
)
from ...runtime_context import current_subagent_run_id
from ...settings.decision_settings_schema import POINT_RUNTIME_SCOPES
from ...settings.defaults import decision_request_max_chars
from ...tooling.output_projection import project_tool_output_body
from ...tooling.runtime_contracts import ToolCall, ToolResult
from ...tooling.write_boundary import WRITE_TOOL_NAMES

# URL 查询串拒绝沿用外部材料首片的同一策略，不在本点另立第二份规则。
from .external_material_order import _URL_WITH_QUERY

_POINT = "delivery_quality"
_TOOL = "run_command"
_QUESTION = "review_focus"
# 决策点提示文字的最大字符数，控制上下文占用。
_MAX_HINT_CHARS = 512
_HINT_TAG = "[delivery-review-focus]"
# 宿主分类值只按短标识校验，不是封闭枚举；形态异常时放弃增强，不猜测含义。
_FACT_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_FACT_KEYS = ("kind", "scope", "status")
_LOCAL_KEYS = ("root", "canonical_command", "created_at")
# 非选择候选与题面的措辞（2026-10-02，照 J12b）：旧题面只写“选一个最值得复核的焦点”，not_needed 只写“无需额外复核建议”，
# 没说什么情况该选它；基准里“全部通过且之后没改动”的用例两遍都选了最新焦点（16/20）。现在题面写明哪些焦点值得先复核、
# 全部通过且没改动时选 not_needed；候选键、题目结构和非选择回答的处理都不变。
_NON_SELECTIONS = {
    "not_needed": "所有焦点的验证都通过了，且验证之后都没再改过文件（edited_after 都是 false）：不需要复核建议，选它本次不给建议",
    "no_match": "现有焦点都不适合优先复核：选它本次不给建议",
    "abstain": "无法可靠判断，明确弃权：选它本次不给建议",
    "need_data": "现有事实不足以判断；本增强不补读材料、不运行测试，保留原结果",
}
_INSTRUCTIONS = ("依据当前请求，从本轮已有验证焦点中选一个交付前最值得主模型先复核的焦点：验证没通过的，"
                 "或验证之后同一项目又改过文件（edited_after 为 true）的，值得先复核；所有焦点都通过且之后没再改过文件时，"
                 "不要硬选一个，选 not_needed。order 越大越新；只能选择已有焦点，"
                 "不能要求运行测试、补读材料或判定任务完成。current_request 可能是首尾节选（见 current_request_completeness），信息不足时选 need_data。")


# LLM: 仅在独立点注册后进入；同一阶段期限覆盖准备/发送/采用，真实停止传播，普通故障只返回空串且不影响已执行结果。
# 函数用途: 返回可忽略的复核提示；仅保存本轮请求去重与原诊断/用量，不执行工具，改动需核对共享 text/native 测试。
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


# LLM: 资格与材料只读结构化事实；发送前领取本轮请求标记，写入与 run_command 共用精确调用键，非选择/失败也不重试。
#   采用前在配置复核之后再比对原来源/参数版本与同一绝对期限，任一变化都不采用，并按结构化原因码留下丢弃记录。
# 函数用途: 完成一次“资格→阶段→材料→领取→决策→复核→渲染”，只改临时去重与原诊断/用量账，不改工具或收口事实。
def _advise(agent: object, record: object, archive: dict) -> str:
    reason = _miss_reason(agent, record, archive)
    stage = None if reason else begin_decision_stage(agent, record.params, operation_id=_operation_id(record))
    reason = reason or stage_miss_reason(stage, _POINT, record.call.run_id)
    if reason:
        note_decision_reach(agent, _POINT, reason)
        return ""
    params = record.params
    # 资格检查已保证当前请求非空；长请求在材料里取首尾节选并标注，不再按长度整点跳过。
    limit = decision_request_max_chars(getattr(agent, "config", None))
    _check_interrupted()
    material = material_or_skip(agent, stage, _POINT, lambda: _material(record, archive, limit))
    if material is None:
        return ""
    state, questions, revision = material
    frozen = (_context_revision(params), revision)
    if not _claim_review_request(record):
        note_decision_reach(agent, _POINT, "already_requested")
        return ""
    note_decision_reach(agent, _POINT, CALLED)
    outcome = decide(agent, params, stage, point=_POINT, state=state, questions=questions,
                     candidates_revision=revision, source_refs=(archive["scoped_call_id"],))
    _check_interrupted()
    if not outcome.may_apply or outcome.response is None:
        return ""
    if outcome.response.binding.candidates_revision != revision:
        record_decision_dropped(agent, stage, outcome, DROP_SOURCES_CHANGED)
        return ""
    # 渲染可能读到等待期间变化后的来源；最后的来源版本比对会整体丢弃这种建议。
    hint = _render_hint(_selected_focus(outcome.response, _review_focuses(record, archive)))
    if not hint:
        return ""
    if not decision_outcome_is_current(agent, params, stage, outcome):
        return ""
    _check_interrupted()
    if _current_sources(agent, record, archive, limit) != frozen:
        record_decision_dropped(agent, stage, outcome, DROP_SOURCES_CHANGED)
        return ""
    if time.monotonic() >= min(stage.deadline, outcome.deadline):
        record_decision_dropped(agent, stage, outcome, DROP_ADOPTION_DEADLINE)
        return ""
    return hint


# LLM: 新验证或成功写入 stale 引用须与原归档配对；主代理、无收口、同 run/task 焦点数沿原界限。
# 写入另须关联已有事件且至少两个 stale 焦点，不读输出或自然语言；返回宿主原因码，空串表示满足。
#   验证来源形状不合规（DecisionInputError）记 bad_verification；当前归档不在本轮列表里记 record_mismatch，
#   两者都与原先放弃的结果一致。
# 函数用途: 判断新验证或改后未复核回执能否进入建议；保留原总焦点门槛，单个 stale 不打开决策阶段。
def _miss_reason(agent: object, record: object, archive: dict) -> str:
    reason = _record_miss_reason(agent, record, archive)
    if reason:
        return reason
    try:
        focuses = _focuses(record, archive)
    except DecisionInputError:
        return "bad_verification"
    if not limits.DELIVERY_FOCUSES_MIN_COUNT <= len(focuses) <= limits.DELIVERY_FOCUSES_MAX_COUNT:
        return "focus_count"
    if record.call.tool_name in WRITE_TOOL_NAMES:
        return _write_miss_reason(record, archive, focuses)
    if not any(focus["current"] for focus in focuses):
        return "record_mismatch"
    if not any(focus["status"] == "failed" or focus["edited_after"] for focus in focuses):
        return "nothing_to_review"
    return ""


# LLM: 原 ToolCall/ToolResult 与归档须在 tool/id/run/task/scoped_call_id 及验证事实上匹配；文件工具只接受成功执行。
# 本轮同对象成员关系由焦点/写入资格核对；其他工具、子代理、重复失败/未知副作用收口、
# 重放或空请求都直接放弃，不读取正文推断。长请求不再按长度放弃，由 _material 取首尾节选并标注。
# 函数用途: 核对当前验证或写入回执的身份、执行和信封配对；不符时在扫描焦点前放弃。
def _record_miss_reason(agent: object, record: object, archive: dict) -> str:
    call, result, params = getattr(record, "call", None), getattr(record, "result", None), getattr(record, "params", None)
    if not isinstance(call, ToolCall) or call.tool_name not in WRITE_TOOL_NAMES | {_TOOL}:
        return "not_test_command"
    if (not isinstance(result, ToolResult) or result.tool_name != call.tool_name or result.call_id != call.call_id
            or result.handler_executed is not True):
        return "not_executed"
    if call.tool_name in WRITE_TOOL_NAMES and result.ok is not True:
        return "failed_call"
    reason = _run_state_miss_reason(agent, params)
    if reason:
        return reason
    if not _verification_matches(record, archive):
        return "not_verification"
    matches = (archive.get("tool") == call.tool_name and archive.get("id") == call.call_id
               and archive.get("run_id") == call.run_id and archive.get("task_id") == getattr(params, "task_id", "")
               and type(archive.get("scoped_call_id")) is str and bool(archive["scoped_call_id"]))
    return "" if matches else "record_mismatch"


# LLM: 只读 canonical handler_details 与归档信封；写入须带非空列表，新验证须带对象，不从正文补事实。
# 函数用途: 校验当前验证事实是否完整复制到归档，字段选择只取规范工具身份。
def _verification_matches(record: object, archive: dict) -> bool:
    key = "verification_state" if record.call.tool_name in WRITE_TOOL_NAMES else "verification_evidence"
    details = record.result.metadata.get("handler_details")
    value = details.get(key) if type(details) is dict else None
    envelope = archive.get("tool_result_envelope") if type(archive) is dict else None
    expected = list if record.call.tool_name in WRITE_TOOL_NAMES else dict
    return type(value) is expected and bool(value) and type(envelope) is dict and envelope.get(key) == value


# LLM: 只认当前写入的 stale/正整数 last_verification_id；引用须匹配同 root 较早焦点，不猜外部或退休事件。
# 当前归档须在本轮唯一出现；只有多个过期焦点才需要选择，单个不请求也不另加提示。
# 函数用途: 判断文件写入是否确实关联已有验证，并且有多个过期焦点值得挑选。
def _write_miss_reason(record: object, archive: dict, focuses: list[dict]) -> str:
    positions = [index for index, row in enumerate(record.params.archive_tool_calls) if row is archive]
    if len(positions) != 1:
        return "record_mismatch"
    states = archive["tool_result_envelope"]["verification_state"]
    stale = [row for row in states if row.get("status") == "stale"]
    if not stale or any(type(row.get("last_verification_id")) is not int or row["last_verification_id"] <= 0 for row in stale):
        return "not_verification"
    earlier = {(focus["root"], focus["id"]) for focus in focuses if focus["position"] < positions[0]}
    if any((row["root"], row["last_verification_id"]) not in earlier for row in stale):
        return "record_mismatch"
    return "" if sum(focus["edited_after"] for focus in focuses) > 1 else "few_stale_focuses"


# LLM: 只读主/子身份与 params 结构化收口标记，包含同参重复失败；不从错误正文猜收口。
# 函数用途: 子代理、重复失败、未知副作用收口或空请求不进入复核，保持原业务收口行为。
def _run_state_miss_reason(agent: object, params: object) -> str:
    if current_subagent_run_id(agent):
        return "subagent"
    if any(getattr(params, key, None) is not None for key in (
            "repeated_failure_halt", "identical_failure_halt", "unknown_outcome_halt")):
        return "halted"
    return "" if _has_request(params) else "no_request"


# LLM: 当前请求取原 params.user_prompt；超出预算（调用方从配置 decision_request_max_chars 读出后传入，0 表示不截取）时
#   取首尾节选并附完整性标注，决策模型据标注知道是节选；空请求返回空串（资格检查已先排除）。
# 函数用途: 返回当前请求（必要时为首尾节选）与完整性标注。
def _request_excerpt(params: object, limit: int) -> tuple[str, dict[str, object]]:
    value = getattr(params, "user_prompt", "")
    if type(value) is not str or not value:
        return "", {}
    return decision_request_excerpt(value, limit)


# LLM: 空请求（无当前用户原话）不是本点场景，在扫描任何归档之前放弃、不留记录；长请求取首尾节选，不按长度放弃。
# 函数用途: 判断当前请求是否存在（非空字符串）。
def _has_request(params: object) -> bool:
    value = getattr(params, "user_prompt", "")
    return type(value) is str and bool(value)


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


# LLM: 写入没有“当前验证事件”；只在已过期焦点里选择，run_command 仍使用全部焦点，不另开触发通道。
# 函数用途: 给材料与选项消费提供同一候选顺序，避免文件写入选到未修改项目；同步检查两种触发测试。
def _review_focuses(record: object, archive: dict) -> list[dict]:
    focuses = _focuses(record, archive)
    return [focus for focus in focuses if focus["edited_after"]] if record.call.tool_name in WRITE_TOOL_NAMES else focuses


# LLM: 只读 params.archive_tool_calls 中同 run/task 的归档信封；run_command 的 verification_evidence（&& 串联整体通过时
# 为 verification_evidence_chain 的全部事件，同一位置按原顺序）是焦点，任意工具的 verification_state stale 行是修改事实；
# 不访问文件、验证账或工具输出。
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
            events.extend({**fact, "position": position, "current": item is archive,
                           "scoped_call_id": item.get("scoped_call_id")} for fact in _envelope_evidence(envelope))
    return events, edits


# LLM: 串联整体通过时信封另带完整有序的 verification_evidence_chain，末项必须等于 verification_evidence；
# 形状不符时整点放弃，不猜哪一段可信。
# 函数用途: 取出一条归档信封里的全部验证事件（单条或串联）。
def _envelope_evidence(envelope: dict) -> list[dict]:
    chain = envelope.get("verification_evidence_chain")
    if chain is None:
        return [_evidence_fact(envelope["verification_evidence"])]
    if type(chain) is not list or not chain or chain[-1] != envelope["verification_evidence"]:
        raise DecisionInputError("串联验证事件与末项证据不一致。")
    return [_evidence_fact(row) for row in chain]


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
# 路径、原命令、写入事实、原参数与时间只进本地版本摘要。写入只选 stale 焦点，run_command 保持原候选。
# 当前请求可能是首尾节选，current_request_completeness 如实标注并进入版本摘要。
# 函数用途: 冻结同一单选材料与本地来源版本；写入状态或参数变化时丢弃建议，须与消费端使用同一焦点顺序。
def _material(record: object, archive: dict, limit: int) -> tuple[dict, dict, str]:
    focuses = _review_focuses(record, archive)
    projects: dict[str, str] = {}
    rows = []
    for order, focus in enumerate(focuses, 1):
        project = projects.setdefault(focus["root"], f"project_{len(projects) + 1}")
        rows.append({"candidate": f"focus_{order}", "project": project, "order": order,
                     "edited_after": focus["edited_after"], "exit_code": focus["exit_code"],
                     **{key: focus[key] for key in _FACT_KEYS}})
    request, completeness = _request_excerpt(record.params, limit)
    state = {"current_request": _safe_request(request), "current_request_completeness": completeness, "focuses": rows}
    criteria = {row["candidate"]: {key: value for key, value in row.items() if key != "candidate"} for row in rows}
    questions = {_QUESTION: {"type": "choice", "instructions": _INSTRUCTIONS,
                             "criteria": {**criteria, **_NON_SELECTIONS}}}
    revision = _digest({"state": state, "questions": questions, "sources": focuses,
                        "call": record.call.to_dict(),
                        "verification_state": archive["tool_result_envelope"].get("verification_state"),
                        "archive_ref": archive["scoped_call_id"], "archive_hash": archive.get("output_hash", "")})
    return state, questions, revision


# LLM: 复用外部材料首片的 external_data/default 脱敏和指令边界；含完整或协议相对 URL 查询串时整点放弃，
#   抛 DecisionPrivacySkip（原因码 privacy_url），由 _advise 经 material_or_skip 留一条 skipped 审计记录。
# 函数用途: 把当前请求投影成可发送的安全副本，无法确认安全时交调用方保留原结果。
def _safe_request(text: str) -> str:
    encoded = decision_json({"current_request": text}).decode("utf-8")
    if _URL_WITH_QUERY.search(encoded):
        raise DecisionPrivacySkip("当前请求含有不应转发的 URL 查询串。")
    return project_tool_output_body(tool=_POINT, output=encoded, trust="external_data", redaction="default")


# LLM: 等待前后都用同一函数取来源快照；不合格时返回空版本，调用方据此放弃采用。
# 函数用途: 返回当前参数版本与材料版本，用于比较决策等待期间来源是否变化。
def _current_sources(agent: object, record: object, archive: dict, limit: int) -> tuple[str, str]:
    if _miss_reason(agent, record, archive):
        return "", ""
    return _context_revision(record.params), _material(record, archive, limit)[2]


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


# LLM: 领取紧靠原 decide 调用前；写入/命令共用同一记录身份，失败、超时和非选择也消耗一次，隐私/关闭未发送不消耗。
# 函数用途: 本轮内标记一条记录已请求复核；不持久化、不参与工具幂等或任务完成，须保留重复请求回归。
def _claim_review_request(record: object) -> bool:
    claimed = record.params.delivery_review_requested_records
    key = _operation_id(record)
    if key in claimed:
        return False
    claimed.add(key)
    return True


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
