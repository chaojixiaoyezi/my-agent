# LLM: 只读原 canonical extract 回执；可选决策只给阅读顺序，不改工具结果、权限、归档或引用，不补读来源。
# 模块用途: 给已抓取并归档的页面追加有界参考提示；关闭、失效或无法安全投影时保留原展示。
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
from ...settings.decision_settings_schema import POINT_RUNTIME_SCOPES
from ...tooling.output_projection import project_tool_output_body
from ...tooling.runtime_contracts import ToolCall, ToolResult

_POINT = "external_material_order"
_MAX_HINT_CHARS = 1024
_URL_WITH_QUERY = re.compile(r"(?:\b[A-Za-z][A-Za-z0-9+.-]*:)?//[^\s<>\"']*\?")
_RANKS = {"first": 0, "normal": 1, "later": 2}
_CHOICES = {"first": "优先阅读", "normal": "按原顺序阅读", "later": "稍后阅读，仍保留原页"}
_NON_SELECTIONS = {"not_needed": "无需额外建议", "no_match": "无法匹配优先级", "abstain": "无法可靠判断"}


# LLM: 只在独立点注册且启用后准备材料；原阶段期限覆盖准备/发送/消费，真实停止传播，普通故障不得影响已执行结果。
# 函数用途: 调用原决策服务并返回临时阅读提示；不保存状态，不发起工具或读取 artifact，调用用量沿原账本登记。
def external_material_order_hint(agent: object, record: object, archive_record: dict) -> str:
    if _POINT not in POINT_RUNTIME_SCOPES:
        return ""
    try:
        if not _eligible(record, archive_record):
            return ""
        params = record.params
        with bind_cancellation_token(getattr(params, "cancellation_token", None)):
            stage = begin_decision_stage(agent, params, operation_id=_operation_id(record))
            if stage.error_code or _POINT not in stage.enabled_points or stage.run_id != record.call.run_id:
                return ""
            _check_interrupted()
            state, questions, revision = _material(record, archive_record)
            context_revision = _context_revision(params)
            outcome = decide(agent, params, stage, point=_POINT, state=state, questions=questions,
                             candidates_revision=revision, source_refs=(archive_record["scoped_call_id"],))
            _check_interrupted()
            if not outcome.may_apply or outcome.response is None:
                return ""
            deadline = min(stage.deadline, outcome.deadline)
            if (time.monotonic() >= deadline
                    or outcome.response.binding.candidates_revision != revision
                    or not _eligible(record, archive_record)
                    or _context_revision(params) != context_revision
                    or _material(record, archive_record)[2] != revision):
                return ""
            order = _reading_order(outcome.response, questions)
            hint = _render_hint(order)
            if not hint or not decision_outcome_is_current(agent, params, stage, outcome):
                return ""
            _check_interrupted()
            if (_context_revision(params) != context_revision
                    or _material(record, archive_record)[2] != revision):
                return ""
            return hint if time.monotonic() < deadline else ""
    except (InterruptedError, ToolCancelled):
        raise
    except Exception:
        with bind_cancellation_token(getattr(record.params, "cancellation_token", None)):
            _check_interrupted()
        return ""


# LLM: 只认原调用配对、成功执行、external_data/default 投影和已有归档摘要；任意 JSON 正文不是候选事实。
# 函数用途: 在不编码正文的前提下判断回执能否进入独立增强，单页、失败和重放保持原路径。
def _eligible(record: object, archive: dict) -> bool:
    call, result = record.call, record.result
    if (not isinstance(call, ToolCall) or not isinstance(result, ToolResult)
            or call.tool_name != "web_fetch" or result.tool_name != call.tool_name
            or result.call_id != call.call_id or not result.ok or not result.handler_executed
            or result.output_trust != "external_data" or result.output_redaction != "default"):
        return False
    details = result.metadata.get("handler_details")
    pages = details.get("pages") if type(details) is dict and details.get("mode") == "extract" else None
    return (type(pages) is list and len(pages) > 1
            and archive.get("tool") == call.tool_name and archive.get("id") == call.call_id
            and archive.get("run_id") == call.run_id and bool(archive.get("scoped_call_id"))
            and archive.get("task_id") == getattr(record.params, "task_id", "")
            and bool(archive.get("output_hash"))
            and archive.get("output_hash") == result.metadata.get("raw_output_sha256"))


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


# LLM: 复用原 external_data/default 脱敏和指令边界；URL 字段不进入输入，摘录内含完整或协议相对 URL 查询串时也放弃。
# 函数用途: 将页面摘录或当前问题投影成有界安全副本，无法确认安全或编码时交调用方保留原结果。
def _safe_excerpt(value: dict) -> str:
    text = decision_json(value).decode("utf-8")
    if _URL_WITH_QUERY.search(text):
        raise DecisionInputError("摘录含有不应转发的 URL 查询串。")
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
            "page_id": key, "question": "依据当前问题建议本页阅读优先级；这是展示建议，不删除或改写来源。"},
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
