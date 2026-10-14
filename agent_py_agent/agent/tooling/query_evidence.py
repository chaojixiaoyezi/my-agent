# LLM: 查询事实只由 typed tool/source/parser 观察提供；本模块不读取模型正文或 shell stdout，不执行复查、不创建账本。
# 模块用途: 统一查询条件、返回数、总数未知和覆盖边界，提供一次有界的零结果复查软指导。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.log_redaction import redact_sensitive_value


# LLM: source_truth 不从解析器记录数推断；total_count 只有调用者已证完整枚举时填写，未知使用 None。
# 类用途: 汇总一次真实查询观察，供原工具 envelope/归档直接保存。
@dataclass(frozen=True)
class QueryObservation:
    source: str
    conditions: dict[str, Any]
    returned_count: int
    observed_count: int
    coverage: str
    parser_version: str
    total_count: int | None = None
    error_code: str = ''
    count_unit: str = 'records'


# LLM: 数量与 coverage 必须是调用方结构事实；中文、空白、AND 字符均不触发机器路由或查询改写。
# 函数用途: 生成无秘密的统一查询观察，零返回只建议复查，不证明来源不存在。
def query_evidence(observation: QueryObservation) -> dict[str, Any]:
    shown = redact_sensitive_value(observation.conditions)
    failed = bool(observation.error_code)
    zero = observation.returned_count == 0 and observation.observed_count == 0
    status = ('failed' if failed else 'zero_observed' if zero and observation.coverage == 'complete'
              else 'unknown' if zero else 'matches' if observation.returned_count else 'empty_page')
    return {'schema': 'query_evidence.v1', 'source': observation.source, 'conditions': shown,
            'conditions_redacted': shown != observation.conditions,
            'returned_count': observation.returned_count, 'observed_count': observation.observed_count,
            'count_unit': observation.count_unit, 'total_count': observation.total_count,
            'total_known': observation.total_count is not None, 'coverage': observation.coverage,
            'parser_version': observation.parser_version, 'source_truth': 'not_verified', 'status': status,
            'error_code': observation.error_code, 'review_required': zero and not failed,
            'review': {'baseline_budget': 1, 'automatic_request': False, 'authority_change': False}}


# LLM: 这是全 owner/主子共享模型软指导，配置只控制提示；不能用它产生自动网络请求或完成硬门。
# 函数用途: 在 canonical system prompt 中提醒模型先检查零结果的条件和一次有界基线，再作有限结论。
def query_zero_result_guidance(config: object) -> str:
    if not bool(getattr(config, 'query_zero_result_review_enabled', True)):
        return ''
    return ('查询结果纪律：query_evidence 是本次实际条件、返回数和覆盖范围的观察；解析器返回零条不能直接证明来源没有数据。'
            '首次查询或条件改变后出现零结果，先检查准确字段/接口键、来源、时间范围、分页、错误和截断；'
            '在同一 owner、同一来源及原时间范围内，预算和权限允许时最多做一次更宽但有界的基线复查，移除未证可靠的额外 AND/并列过滤。'
            '中文可以是有效查询值，不能按语言或固定词表否定；不要自动扫全量、扩权限或无限重试。'
            '有可靠已执行基线即可复用，不必重复请求；网络不可用、权限不足或无预算时如实说明未知。'
            'shell/自定义脚本/MCP 没有声明查询协议时，不能从 stdout 猜查询条件、记录数或伪造 verified 查询事实。'
            '普通零结果无需单独 IM 告警；最终回复由模型按实际证据表达条件和局限，不靠词句硬门。')
