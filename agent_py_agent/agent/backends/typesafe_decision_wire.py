# LLM: TypeSafe wire 合同只接受本次题目及候选；供应商返回不控制权限、状态机或业务提交，同步协议测试。
# 模块用途: 校验 Jev 的三类问题和逐题响应，保留实际模型、原用量与局部失败，不依赖外部 SDK。
from __future__ import annotations

import math

from .decision_protocol import (
    MAX_DECISION_RESPONSE_BYTES,
    DecisionAnswer,
    DecisionInputError,
    DecisionRequest,
    DecisionResponse,
    decision_json,
)
from .errors import ProviderResponseError

MAX_QUESTIONS = 64


# LLM: 固定长度是本适配器资源限制，Choice/Score 数量遵循 TypeSafe API；不按名称解释结果含义。
# 函数用途: 检查题号和候选键，拒绝空键、过长键或非字符串。
def _valid_key(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value)


# LLM: instructions/criteria 按公开 API 接受结构化数据，不能仅支持自然语言字符串而丢候选事实。
# 函数用途: 检查一项说明材料的顶层类型，深度和字节由请求快照统一检查。
def _entry(value: object) -> bool:
    return type(value) in (str, dict, list)


# LLM: 发送前验证 native state/questions，未知问题类型明确失败；不得转成普通生成 prompt 或重试猜协议。
# 函数用途: 生成已经校验的 Jev 请求体，不写配置、不发网络，错误不包含用户材料。
def typesafe_payload(request: DecisionRequest, model: str) -> dict:
    payload = request.payload(model)
    questions = payload["questions"]
    if not _entry(payload["state"]) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise DecisionInputError("决策状态或题目数量无效。")
    for key, question in questions.items():
        _validate_question(key, question)
    decision_json(payload)
    return payload


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


# LLM: 分布必须覆盖原题全部候选且和为 1；容差只吸收数值舍入，不进行归一化或补候选。
# 函数用途: 将供应商分布冻结为键值序列，坏数据让该题失效。
def _probabilities(value: object, keys: set[str]) -> tuple[tuple[str, float], ...]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("决策响应分布与候选不一致。")
    pairs = tuple((key, _number(probability)) for key, probability in value.items())
    if not math.isclose(sum(probability for _, probability in pairs), 1.0, abs_tol=1e-4):
        raise ValueError("决策响应分布总量无效。")
    return pairs


# LLM: 只解释协议字段，need_data 等业务含义由宿主候选绑定决定；单题坏响应不可污染其它题。
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
    if not math.isclose(value, sum(int(k) * p for k, p in probabilities), abs_tol=1e-4):
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
