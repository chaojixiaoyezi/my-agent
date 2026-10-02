# LLM: 只读原 canonical extract 回执；可选决策只给阅读顺序，不改工具结果、权限、归档或引用，不补读来源。
# 模块用途: 给已抓取并归档的页面追加有界参考提示；关闭、失效或无法安全投影时保留原展示。
from __future__ import annotations

import hashlib
import re
import time

from ...backends.decision_protocol import DecisionInputError, DecisionPrivacySkip, decision_json
from ...common.cancellation import ToolCancelled, bind_cancellation_token, raise_if_cancelled
from ...concurrency.interrupt import is_interrupted
from ...conversation import decision_point_limits as limits
from ...conversation.decision_outcome_log import (
    DROP_ADOPTION_DEADLINE,
    DROP_SOURCES_CHANGED,
    drop_and_return,
    material_or_skip,
)
from ...conversation.decision_reach_counts import CALLED, note_decision_reach, stage_miss_reason
from ...conversation.decision_service import (
    begin_decision_stage,
    decide,
    decision_outcome_is_current,
)
from ...settings.decision_settings_schema import POINT_RUNTIME_SCOPES
from ...tooling.output_projection import project_tool_output_body
from ...tooling.runtime_contracts import ToolCall, ToolResult

_POINT = "external_material_order"
# 决策点提示文字的最大字符数，控制上下文占用。
_MAX_HINT_CHARS = 1024
_URL_WITH_QUERY = re.compile(r"(?:\b[A-Za-z][A-Za-z0-9+.-]*:)?//[^\s<>\"']*\?")
_RANKS = {"first": 0, "normal": 1, "later": 2}
# 逐页题的候选说明（照 J12b，2026-10-02）：旧措辞 not_needed=“无需额外建议”、no_match=“无法匹配优先级”被决策模型读成
# “这一页用不上”，对无关页答 not_needed/no_match；任何非排序回答都会让整次不给提示（基准 0.61，14 道错题全是这种）。
# 现在每档都写明对“这一页”意味着什么，无关页明确指向 later；候选键与题目结构不变，非排序回答的处理也不变。
_CHOICES = {"first": "这一页与当前问题直接相关：优先阅读",
            "normal": "这一页与当前问题有一些关系：按原顺序阅读",
            "later": "这一页与当前问题无关或关系很弱：放到后面读（原页仍保留在结果里，不会被删除）"}
_NON_SELECTIONS = {
    "not_needed": "不打算给这一页排阅读优先级；选它会让本次不给任何阅读建议。只是与问题无关请选 later",
    "no_match": "first/normal/later 三档都不适合这一页；选它会让本次不给任何阅读建议。只是与问题无关请选 later",
    "abstain": "无法可靠判断这一页，明确弃权；选它会让本次不给任何阅读建议",
}


# LLM: 只在独立点注册且启用后准备材料；原阶段期限覆盖准备/发送/消费，真实停止传播，普通故障不得影响已执行结果。
#   每次到达都在 decision_reach_counts 记一次结果（没调用的原因码，或真正调用前记 called）。
# 函数用途: 调用原决策服务并返回临时阅读提示；不保存状态（只记到达诊断计数），不发起工具或读取 artifact，调用用量沿原账本登记。
def external_material_order_hint(agent: object, record: object, archive_record: dict) -> str:
    if _POINT not in POINT_RUNTIME_SCOPES:
        return ""
    try:
        reason = _miss_reason(record, archive_record)
        params = record.params
        with bind_cancellation_token(getattr(params, "cancellation_token", None)):
            stage = None if reason else begin_decision_stage(agent, params, operation_id=_operation_id(record))
            reason = reason or stage_miss_reason(stage, _POINT, record.call.run_id)
            if reason:
                note_decision_reach(agent, _POINT, reason)
                return ""
            _check_interrupted()
            material = material_or_skip(agent, stage, _POINT, lambda: _material(record, archive_record))
            if material is None:
                return ""
            state, questions, revision = material
            context_revision = _context_revision(params)
            note_decision_reach(agent, _POINT, CALLED)
            outcome = decide(agent, params, stage, point=_POINT, state=state, questions=questions,
                             candidates_revision=revision, source_refs=(archive_record["scoped_call_id"],))
            _check_interrupted()
            if not outcome.may_apply or outcome.response is None:
                return ""
            deadline = min(stage.deadline, outcome.deadline)
            if stale := _material_stale(agent, record, archive_record, (params, outcome, revision, context_revision, deadline)):
                return drop_and_return(agent, (stage, outcome), stale, "")
            order = _reading_order(outcome.response, questions)
            if not (hint := _render_hint(order)):
                return ""
            _check_interrupted()
            if not decision_outcome_is_current(agent, params, stage, outcome):
                # 强制门不通过时它自己已登记一行 dropped（带真实原因码），这里直接放弃，不再重复登记。
                return ""
            if stale := _material_stale(agent, record, archive_record, (params, outcome, revision, context_revision, deadline)):
                return drop_and_return(agent, (stage, outcome), stale, "")
            return hint
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:
        with bind_cancellation_token(getattr(record.params, "cancellation_token", None)):
            _check_interrupted()
        return ""


# LLM: 两段出口共用同一套复核：到期算 adoption_deadline，候选 revision/宿主来源/上下文 revision 任一变化算 sources_changed；
#   唯一强制门（decision_outcome_is_current）由调用方直接调用：门不通过时门自己已登记一行 dropped（真实原因码），
#   调用方直接返回空串，本函数不再返回门的原因码，避免同一条建议被登记两行。判定顺序与原先两个 if 逐字对应，
#   不新增语义；ctx 是 (params, outcome, revision, context_revision, deadline) 五元组。
# 函数用途: 复核外部材料阅读顺序建议是否仍成立，返回原因码（空串表示可采用）。
def _material_stale(agent, record, archive_record, ctx) -> str:
    params, outcome, revision, context_revision, deadline = ctx
    if time.monotonic() >= deadline:
        return DROP_ADOPTION_DEADLINE
    if (outcome.response.binding.candidates_revision != revision or _miss_reason(record, archive_record)
            or _context_revision(params) != context_revision or _material(record, archive_record)[2] != revision):
        return DROP_SOURCES_CHANGED
    return ""


# LLM: 只认原调用配对、成功执行、external_data/default 投影和已有归档摘要；任意 JSON 正文不是候选事实。
#   返回宿主原因码，空串表示满足；各条件与原先的整体判断一一对应，不改变触发结果。网页数下限读
#   decision_point_limits.MATERIAL_PAGES_MIN_COUNT（与诊断大白话共用，调用时现读）。
# 函数用途: 在不编码正文的前提下判断回执能否进入独立增强，不能时给出原因码；单页、失败和重放保持原路径。
def _miss_reason(record: object, archive: dict) -> str:
    call, result = record.call, record.result
    if not isinstance(call, ToolCall) or call.tool_name != "web_fetch":
        return "not_web_fetch"
    if (not isinstance(result, ToolResult) or result.tool_name != call.tool_name
            or result.call_id != call.call_id or not result.ok or not result.handler_executed):
        return "fetch_failed"
    if result.output_trust != "external_data" or result.output_redaction != "default":
        return "not_external_page"
    details = result.metadata.get("handler_details")
    pages = details.get("pages") if type(details) is dict and details.get("mode") == "extract" else None
    if type(pages) is not list:
        return "not_extract"
    if len(pages) < limits.MATERIAL_PAGES_MIN_COUNT:
        return "single_page"
    matches = (archive.get("tool") == call.tool_name and archive.get("id") == call.call_id
               and archive.get("run_id") == call.run_id and bool(archive.get("scoped_call_id"))
               and archive.get("task_id") == getattr(record.params, "task_id", "")
               and bool(archive.get("output_hash"))
               and archive.get("output_hash") == result.metadata.get("raw_output_sha256"))
    return "" if matches else "record_mismatch"


# LLM: 原 call 身份决定一次建议的操作编号；不从正文或模型选项推断 owner/run/task，也不创建持久操作。
# 函数用途: 将现有调用编号绑定到原决策账本，防止同名工具不同回合混用建议。
def _operation_id(record: object) -> str:
    call = record.call
    return _POINT + ":" + _digest({"call_id": call.call_id, "run_id": call.run_id,
                                   "turn_id": call.turn_id, "attempt_id": call.attempt_id})


# LLM: 摘要共用原有界 JSON；只用来比较本轮内存快照，不保存原权限属性或未脱敏来源。
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


# LLM: 页面必须携带原 artifact 与内容 hash；这些来源只参与本地版本绑定，URL/路径/调用参数不进入外部材料。
# 函数用途: 校验已归档页面的结构，并取出完整原值供等待前后比较，不访问任何文件。
def _page_sources(record: object) -> list[dict]:
    pages = record.result.metadata["handler_details"]["pages"]
    decision_json(pages)
    for page in pages:
        if (type(page) is not dict or any(type(page.get(key)) is not str for key in
                                        ("title", "preview", "artifact_ref", "content_hash"))
                or not page["artifact_ref"] or not re.fullmatch(r"[0-9a-f]{64}", page["content_hash"])):
            raise DecisionInputError("外部材料缺少已归档来源。")
    return pages


# LLM: 复用原 external_data/default 脱敏和指令边界；URL 字段不进入输入，摘录内含完整或协议相对 URL 查询串时也放弃，
#   抛 DecisionPrivacySkip（原因码 privacy_url），由 material_or_skip 留一条 skipped 审计记录。
# 函数用途: 将页面摘录或当前问题投影成有界安全副本，无法确认安全或编码时交调用方保留原结果。
def _safe_excerpt(value: dict) -> str:
    text = decision_json(value).decode("utf-8")
    if _URL_WITH_QUERY.search(text):
        raise DecisionPrivacySkip("摘录含有不应转发的 URL 查询串。")
    return project_tool_output_body(tool="external_material_order", output=text,
                                    trust="external_data", redaction="default")


# LLM: 题目只建议完整原页的阅读优先级；输入不含 URL、headers/body、调用参数或未读 artifact 正文。
# 函数用途: 冻结原页序、摘要、当前问题与逐页选项，来源全文和失败/未完成项继续留在原工具回执。
def _material(record: object, archive: dict) -> tuple[dict, dict, str]:
    pages = _page_sources(record)
    rows, questions = [], {}
    for index, page in enumerate(pages):
        key = f"page_{index + 1}"
        rows.append({"id": key, "content_hash": page["content_hash"],
                     "excerpt": _safe_excerpt({"title": page["title"], "preview": page["preview"]})})
        questions[key] = {"type": "choice", "instructions": {
            "page_id": key, "question": "给这一页选阅读优先级：与当前问题直接相关选 first，有些关系选 normal，"
                                        "无关或很弱选 later；这是展示建议，不删除或改写来源。"},
            "criteria": {**_CHOICES, **_NON_SELECTIONS, "need_data": {
                "meaning": "现有摘录不足；本增强不补读，保留原结果",
                "required_refs": [{"kind": "existing_page", "ref": key}],
            }}}
    state = {"task_context": _safe_excerpt({"user_prompt": getattr(record.params, "user_prompt", "")}),
             "pages": rows}
    # 完整来源只进本地版本，DecisionRequest 的外部 payload 仍只含 state/questions。
    revision = _digest({"state": state, "questions": questions, "sources": pages,
                        "archive_hash": archive["output_hash"], "archive_ref": archive["scoped_call_id"]})
    return state, questions, revision


# LLM: 任何缺题、重复题、逐题错误或非选择都放弃整次提示；不把置信度、not_needed 或缺数据变成删除权。
# 函数用途: 生成覆盖全部原页的稳定顺序，同级保持原序，排序未变化时无需重复提示。
def _reading_order(response: object, questions: dict) -> tuple[int, ...]:
    answers = response.answers
    if len(answers) != len(questions) or {answer.question_id for answer in answers} != set(questions):
        return ()
    ranks = {}
    for answer in answers:
        if answer.kind != "choice" or answer.error_code or answer.value not in _RANKS:
            return ()
        ranks[answer.question_id] = _RANKS[answer.value]
    order = tuple(sorted(range(1, len(questions) + 1), key=lambda index: ranks[f"page_{index}"]))
    return order if order != tuple(range(1, len(questions) + 1)) else ()


# LLM: 提示只渲染宿主验证后的页序数字，不复制模型文案或来源文本，也不改变原结果、refs 或恢复锚点。
# 函数用途: 为当前工具回执追加简短可忽略的阅读建议，超过展示预算则完全不追加。
def _render_hint(order: tuple[int, ...]) -> str:
    if not order:
        return ""
    hint = ("[external-material-reading-order]\n"
            "阅读优先级建议（仅供参考，页码对应本次 pages 原顺序，从 1 开始）："
            + " → ".join(map(str, order)) + "。原页面、失败项及恢复引用仍以原工具结果为准。")
    return hint if len(hint) <= _MAX_HINT_CHARS else ""


# LLM: 用户取消在发送和消费边界传播，不能降级成一次可忽略的增强失败。
# 函数用途: 沿原工具 token 与运行中断事实终止等待或采用。
def _check_interrupted() -> None:
    raise_if_cancelled()
    if is_interrupted():
        raise InterruptedError("外部材料阅读建议随当前运行停止。")
