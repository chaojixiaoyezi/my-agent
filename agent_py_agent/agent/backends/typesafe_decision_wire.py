# LLM: TypeSafe只接受本次题目及候选；真实API两位小数分布按量化误差核对，不能归一化改写原概率或据此授予权限。
# 经验输入上界只在标定范围内由最终 wire 字节计算并标注 empirical，不能当作 tokenizer 或服务端硬额度。
# 模块用途: 校验三类问题和逐题响应，保留实际模型、原用量及供应商舍入值，并提供实验用的经验输入上界，不依赖外部SDK。
from __future__ import annotations

import math

from ..contracts.model_call_budget import InputTokenBound, ModelCallBudgetError
from ..memory_archive.tokens import estimate_tokens
from .decision_protocol import (
    MAX_DECISION_RESPONSE_BYTES,
    DecisionAnswer,
    DecisionInputError,
    DecisionRequest,
    DecisionResponse,
    decision_json,
)
from .errors import ProviderResponseError
from .gateway_helpers import gateway_request_body

JEV_REQUEST_CONTEXT_TOKENS = 64_000
JEV_STATE_QUESTION_TOKENS = 32_000
# jev_wire_bytes.v1 是版本化的代码方法，不是用户配置：C = ceil(B/2) + 256*Q + 1024。
# 标定依据：4 次真实 64,921–65,063 字节、27 题请求计费 17,352–17,383 输入（C≈40.5k，约 2.3 倍余量）；
# 官方示例 173 字节、1 题计费 296（C=1367）。只在标定范围内使用，超出即拒绝而不外推。
JEV_EMPIRICAL_BOUND_METHOD = "jev_wire_bytes.v1"
_JEV_BOUND_BYTES_PER_TOKEN = 2
_JEV_BOUND_TOKENS_PER_QUESTION = 256
_JEV_BOUND_FIXED_TOKENS = 1024
_JEV_BOUND_MAX_QUESTIONS = 64
_JEV_BOUND_MAX_STATE_BYTES = 4096
_JEV_BOUND_MAX_TOKENS = 57_600  # 0.9 × 64k 整请求窗口
_JEV_BOUND_POINTS = frozenset({"skill_tool"})


# LLM: 输入必须是最终 wire 正文字节及其原载荷；结果只是用户接受的经验上界（kind=empirical），不是供应商保证。
# 超出标定点位/题量/state 字节/总量时抛 input_bound_out_of_calibration，调用方不得预留或发送。
# 函数用途: 按 jev_wire_bytes.v1 计算一次 Jev 决策请求的完整输入 token 经验上界。
def jev_empirical_input_bound(body: bytes, payload: dict, *, point: str) -> InputTokenBound:
    questions = payload.get("questions") if isinstance(payload, dict) else None
    if type(body) is not bytes or type(questions) is not dict or not questions:
        raise ModelCallBudgetError("input_bound_out_of_calibration")
    state_bytes = len(gateway_request_body(payload.get("state")))
    tokens = (-(-len(body) // _JEV_BOUND_BYTES_PER_TOKEN) + _JEV_BOUND_TOKENS_PER_QUESTION * len(questions)
              + _JEV_BOUND_FIXED_TOKENS)
    if (type(point) is not str or point not in _JEV_BOUND_POINTS or len(questions) > _JEV_BOUND_MAX_QUESTIONS
            or state_bytes > _JEV_BOUND_MAX_STATE_BYTES or tokens > _JEV_BOUND_MAX_TOKENS):
        raise ModelCallBudgetError("input_bound_out_of_calibration")
    return InputTokenBound(tokens=tokens, kind="empirical", method=JEV_EMPIRICAL_BOUND_METHOD, body_bytes=len(body),
                           questions=len(questions), state_bytes=state_bytes)


# LLM: 固定长度是本适配器资源限制，Choice/Score 数量遵循 TypeSafe API；不按名称解释结果含义。
# 函数用途: 检查题号和候选键，拒绝空键、过长键或非字符串。
def _valid_key(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value)


# LLM: instructions/criteria 按公开 API 接受结构化数据，不能仅支持自然语言字符串而丢候选事实。
# 函数用途: 检查一项说明材料的顶层类型，深度和字节由请求快照统一检查。
def _entry(value: object) -> bool:
    return type(value) in (str, dict, list)


# LLM: 原JSON节点/字节预算约束整包题量，供应商未声明64题硬限；不得把宿主旧上限当协议事实或转成生成prompt。
# 函数用途: 生成已经校验的 Jev 请求体，不写配置、不发网络，错误不包含用户材料。
def typesafe_payload(request: DecisionRequest, model: str) -> dict:
    payload = request.payload(model)
    questions = payload["questions"]
    if not _entry(payload["state"]) or not questions:
        raise DecisionInputError("决策状态或题目数量无效。")
    for key, question in questions.items():
        _validate_question(key, question)
    decision_json(payload)
    return payload


# LLM: Jev的64k整包和32k共享state加最长题是供应商token窗口；复用原估算器留余量，但它不是供应商tokenizer或硬容量证明。
# 函数用途: 提前筛掉明显超窗的请求，供应商仍负责最终容量判定，拒绝后调用方沿原方案继续。
def validate_typesafe_request_window(payload: dict, configured_window_tokens: int) -> None:
    window = configured_window_tokens if type(configured_window_tokens) is int and configured_window_tokens > 0 else JEV_REQUEST_CONTEXT_TOKENS
    total_limit = min(window, JEV_REQUEST_CONTEXT_TOKENS)
    # 直接拿 UTF-8 字节数当 token 数会误拒合法的批量短题；保留一成服务端包装余量。
    if estimate_tokens(payload) > int(total_limit * 0.9):
        raise DecisionInputError("决策请求超过模型总输入窗口。")
    longest_question = max(
        estimate_tokens({"state": payload["state"], "questions": {key: question}})
        for key, question in payload["questions"].items()
    )
    if longest_question > int(min(window, JEV_STATE_QUESTION_TOKENS) * 0.9):
        raise DecisionInputError("决策状态和最长题目超过模型单题输入窗口。")


# LLM: 校验单题 wire 类型与供应商候选上限，无网络或状态副作用；业务可否跳过仍由宿主决定。
# 函数用途: 检查三类题目的说明与 criteria，不让未知字段被静默忽略或转成生成请求。
def _validate_question(key: str, question: object) -> None:
    if not _valid_key(key) or type(question) is not dict or not _entry(question.get("instructions")):
        raise DecisionInputError("决策题目须有有效编号、类型和说明。")
    if set(question) - {"type", "instructions", "criteria"}:
        raise DecisionInputError("决策题目包含未支持字段。")
    kind, criteria = question.get("type"), question.get("criteria")
    if kind == "choice":
        if (type(criteria) is not dict or not 1 <= len(criteria) <= 255
                or any(not _valid_key(k) or (v is not None and not _entry(v)) for k, v in criteria.items())):
            raise DecisionInputError("选择题须有 1 至 255 个明确候选。")
    elif kind == "score":
        if type(criteria) is not list or not 2 <= len(criteria) <= 10 or not all(_entry(v) for v in criteria):
            raise DecisionInputError("评分题须有 2 至 10 个有序等级。")
    elif kind == "noul":
        if "criteria" in question and (type(criteria) is not dict or set(criteria) - {"true", "false"}
                                       or not all(_entry(v) for v in criteria.values())):
            raise DecisionInputError("是非题说明只接受 true/false 两个字段。")
    else:
        raise DecisionInputError("尚未支持这种决策题目类型。")


# LLM: 数值字符串/bool/NaN 不可伪装成供应商概率，错误不引用原正文。
# 函数用途: 读取给定范围内的有限协议数值。
def _number(value: object, upper: float = 1.0) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= upper:
        raise ValueError("决策响应数值无效。")
    return float(value)


# LLM: 真实API会把各概率舍入到两位小数，和可为0.99/1.01；只允许各项半量化单位累计误差，不归一化或补候选。
# 函数用途: 将供应商分布冻结为键值序列，坏数据让该题失效。
def _probabilities(value: object, keys: set[str]) -> tuple[tuple[str, float], ...]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("决策响应分布与候选不一致。")
    pairs = tuple((key, _number(probability)) for key, probability in value.items())
    tolerance = _probability_rounding_unit(pairs) * len(pairs) + 1e-4
    total = sum(probability for _, probability in pairs)
    if total <= 0 or not math.isclose(total, 1.0, abs_tol=tolerance):
        raise ValueError("决策响应分布总量无效。")
    return pairs


# LLM: 仅已观测的百分位网格允许半单位误差；更高精度分布保持原精度校验，原始浮点值不修改。
# 函数用途: 计算TypeSafe量化概率的单项舍入边界，供总量和评分一致性共用。
def _probability_rounding_unit(pairs: tuple[tuple[str, float], ...]) -> float:
    return 0.005 if all(math.isclose(value, round(value, 2), abs_tol=1e-12, rel_tol=0)
                        for _, value in pairs) else 0.0


# LLM: 只解释协议字段，业务含义归宿主候选；评分按原概率量化误差复核，不能因舍入丢掉整题或归一化概率。
# 函数用途: 校验一个选择、评分或是非答案，保留概率/置信度，绝不将分数变成授权。
def _answer(key: str, question: dict, row: object) -> DecisionAnswer:
    kind = question["type"]
    if type(row) is not dict or row.get("type") != kind:
        raise ValueError("决策响应类型不一致。")
    if kind == "noul":
        return DecisionAnswer(key, kind, _number(row.get("noul")))
    confidence = _number(row.get("confidence"))
    criteria = question["criteria"]
    keys = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
    probabilities = _probabilities(row.get("probabilities"), keys)
    if kind == "choice":
        value = row.get("choice")
        if type(value) is not str or value not in keys:
            raise ValueError("决策选择不在原候选中。")
        if dict(probabilities)[value] + 1e-4 < max(p for _, p in probabilities):
            raise ValueError("决策选择与分布不一致。")
        return DecisionAnswer(key, kind, value, confidence, probabilities)
    value = _number(row.get("score"), len(criteria) - 1)
    unit = _probability_rounding_unit(probabilities)
    tolerance = unit * (1 + sum(int(key) for key, _ in probabilities)) + 1e-4
    if not math.isclose(value, sum(int(k) * p for k, p in probabilities), abs_tol=tolerance):
        raise ValueError("决策评分与分布不一致。")
    legend = row.get("legend")
    if type(legend) is not dict or set(legend) != keys or any(type(v) is not str for v in legend.values()):
        raise ValueError("决策等级说明无效。")
    return DecisionAnswer(key, kind, value, confidence, probabilities, tuple(legend.items()))


# LLM: 缺少用量保持未知，错误用量不补零；已校验 JSON 上限保证原 usage 信封能安全冻结。
# 函数用途: 保存供应商用量对象，已知 token 字段必须是非负整数，额外字段保留原值。
def _usage(raw: dict) -> tuple[bytes, bool]:
    value = raw.get("usage", {})
    if type(value) is not dict:
        raise ValueError("决策用量无效。")
    for key in ("input_tokens", "output_tokens"):
        if key in value and (type(value[key]) is not int or value[key] < 0):
            raise ValueError("决策用量无效。")
    return decision_json(value, limit=MAX_DECISION_RESPONSE_BYTES), "usage" in raw


# LLM: 响应绑定完全取原请求，模型不能注入另一 owner/版本；不合格题独立失效，顶层关联错误整次拒绝。
# 函数用途: 解析完整供应商响应；缺题或坏题保留结构化错误，实际模型与用量交给原账本。
def parse_typesafe_response(request: DecisionRequest, model: str, raw: object) -> DecisionResponse:
    questions = typesafe_payload(request, model)["questions"]
    try:
        decision_json(raw, limit=MAX_DECISION_RESPONSE_BYTES)
        if type(raw) is not dict or type(raw.get("answers")) is not dict:
            raise ValueError("决策响应缺少题目集合。")
        actual_model = raw.get("model")
        if type(actual_model) is not str or not actual_model.strip() or len(actual_model) > 4096:
            raise ValueError("决策响应缺少模型版本。")
        if set(raw["answers"]) - set(questions):
            raise ValueError("决策响应含未请求题目。")
        usage, reported = _usage(raw)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ProviderResponseError("决策服务返回的响应信封无效。") from exc
    answers = []
    for key, question in questions.items():
        if key not in raw["answers"]:
            answers.append(DecisionAnswer(key, question["type"], error_code="missing_answer"))
            continue
        try:
            answers.append(_answer(key, question, raw["answers"][key]))
        except (ValueError, TypeError, OverflowError):
            answers.append(DecisionAnswer(key, question["type"], error_code="invalid_answer"))
    return DecisionResponse(request.binding, request.input_digest, model, actual_model, tuple(answers), usage, reported)
