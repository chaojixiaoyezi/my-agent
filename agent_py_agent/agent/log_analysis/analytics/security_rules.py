# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Metadata for the first log-analysis soft detector pack."""

from dataclasses import asdict, dataclass, field
from typing import Any

RULE_VERSION = "v1"


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 DetectorRule 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DetectorRule 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class DetectorRule:
    detector_id: str
    detector_kind: str = "rule"
    mode: str = "soft"
    version: str = RULE_VERSION
    description: str = ""
    severity_hint: str = "medium"
    min_confidence_for_case: float = 0.6
    lookback_seconds: int = 900
    required_fields: tuple[str, ...] = field(default_factory=tuple)
    tactics: tuple[str, ...] = field(default_factory=tuple)
    techniques: tuple[str, ...] = field(default_factory=tuple)

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SOFT_DETECTOR_RULES: dict[str, DetectorRule] = {
    "waf_attack_success_candidate": DetectorRule(
        detector_id="waf_attack_success_candidate",
        description="WAF or web attack alert followed by server-side success indicators.",
        severity_hint="high",
        lookback_seconds=900,
        required_fields=("event_time", "source_product", "attacker_ip|src_ip", "victim_ip|dst_ip"),
        tactics=("initial-access", "execution"),
        techniques=("T1190",),
    ),
    "web_to_process_anomaly": DetectorRule(
        detector_id="web_to_process_anomaly",
        description="Web service parent process launched an unusual shell, interpreter, or downloader.",
        severity_hint="high",
        min_confidence_for_case=0.62,
        lookback_seconds=600,
        required_fields=("event_time", "process.name", "process.parent_name"),
        tactics=("execution", "persistence"),
        techniques=("T1059", "T1105"),
    ),
    "vpn_new_geo_login": DetectorRule(
        detector_id="vpn_new_geo_login",
        description="Successful VPN login from a new country, ASN, device, or unusual hour.",
        severity_hint="medium",
        lookback_seconds=3600,
        required_fields=("event_time", "user", "src_ip", "event_outcome"),
        tactics=("initial-access", "credential-access"),
        techniques=("T1078",),
    ),
    "bruteforce_then_success": DetectorRule(
        detector_id="bruteforce_then_success",
        description="Repeated authentication failures followed by a successful login.",
        severity_hint="high",
        min_confidence_for_case=0.62,
        lookback_seconds=1800,
        required_fields=("event_time", "user|src_ip", "event_outcome"),
        tactics=("credential-access", "initial-access"),
        techniques=("T1110", "T1078"),
    ),
    "rare_egress_after_alert": DetectorRule(
        detector_id="rare_egress_after_alert",
        description="Alerted asset later reached a rare external destination.",
        severity_hint="high",
        min_confidence_for_case=0.62,
        lookback_seconds=1800,
        required_fields=("event_time", "src_ip|host", "dst_ip|domain"),
        tactics=("command-and-control", "exfiltration"),
        techniques=("T1071", "T1041"),
    ),
    "multi_source_weak_signal": DetectorRule(
        detector_id="multi_source_weak_signal",
        description="Multiple weak signals from different sources overlap on one entity and window.",
        severity_hint="medium",
        min_confidence_for_case=0.58,
        lookback_seconds=900,
        required_fields=("event_time", "source_product"),
        tactics=("discovery", "initial-access"),
    ),
}


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 get_rule 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 get rule 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def get_rule(detector_id: str) -> DetectorRule:
    return SOFT_DETECTOR_RULES[detector_id]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 detector_ids 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 detector ids 的判定结果，避免把推测当作事实写入。
def detector_ids() -> list[str]:
    return list(SOFT_DETECTOR_RULES)


__all__ = [
    "DetectorRule",
    "RULE_VERSION",
    "SOFT_DETECTOR_RULES",
    "detector_ids",
    "get_rule",
]
