# LLM: 智能程度自动检测的判定规则，纯函数：不发网络、不读写文件、不看回复正文，只读 usage 里的结构化 token 字段。
#   阈值按 2026-09-26 DeepSeek 官方接口实测标定（docs/design/REASONING_EFFORT.md 第 2、8 节），宁可判“不支持”也不误判“支持”。
#   改阈值或计量口径须同步 test_reasoning_probe.py 的回放用例与变异验证，以及 reasoning_probe.py 的回执文案。
# 模块用途: 从一次检测的 low / max / 不带字段三组请求结果，判断模型是否真的按推理强度档位调节思考。
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

# 三组请求的档位；空串表示不带任何推理字段（服务商默认）。每组发 PROBE_ROUNDS 次，按轮交替发送，减少时段漂移的影响。
PROBE_LEVELS = ("low", "max", "")
PROBE_ROUNDS = 3
VERDICT_SUPPORTED = "supported"
VERDICT_UNSUPPORTED = "unsupported"
VERDICT_INCONCLUSIVE = "inconclusive"
MEASURE_REASONING = "reasoning_tokens"
MEASURE_OUTPUT = "output_tokens"
# “支持”必须同时满足：max 组中位数至少是 low 组的 1.5 倍、至少多 200 个 token，并且两组完全不重叠
# （max 组最少的一次也比 low 组最多的一次多）。同分布下三对三完全不重叠的概率只有 1/20，噪声很难凑出“支持”。
_MIN_RATIO = 1.5
_MIN_GAP_TOKENS = 200
# 服务商认为请求参数不合法时的状态码；带字段的两组全被这样拒绝、不带字段的一组全部成功，才算“拒绝了这个字段”。
_REJECT_STATUS = frozenset({400, 422})
# usage 里推理 token 与输出 token 的已知位置（OpenAI Chat、Responses、Anthropic）；不在表里就当没有，只会得到“无法判定”。
_REASONING_PATHS = (("completion_tokens_details", "reasoning_tokens"), ("output_tokens_details", "reasoning_tokens"))
_OUTPUT_KEYS = ("completion_tokens", "output_tokens")


# LLM: 一次请求的结构化结果；error_type 非空表示请求失败（status_code 为上游 HTTP 状态，拿不到时为 0），不保存任何响应正文。
# 类用途: 检测里一次请求的档位、推理 token、输出 token 与失败信息。
@dataclass(frozen=True)
class ProbeSample:
    level: str
    reasoning_tokens: int | None = None
    output_tokens: int | None = None
    status_code: int = 0
    error_type: str = ""

    # 函数用途: 这次请求是否成功返回。
    @property
    def ok(self) -> bool:
        return not self.error_type


# LLM: verdict 取 VERDICT_*；reason 是稳定原因码（max_above_low / no_difference / no_reasoning / field_rejected /
#   incomplete / no_usage），回执文案只按它生成；measure 说明比较的是推理 token 还是输出 token。
# 类用途: 一次检测的结论和三组中位数。
@dataclass(frozen=True)
class ProbeVerdict:
    verdict: str
    reason: str
    measure: str = ""
    low_median: int | None = None
    max_median: int | None = None
    default_median: int | None = None


# LLM: 只认非负整数（布尔不算）；按已知位置依次找，找不到返回 None，由判定得出“无法判定”而不是猜。
# 函数用途: 从 usage 取出推理 token 数。
def reasoning_token_count(usage: object) -> int | None:
    for outer, inner in _REASONING_PATHS:
        details = usage.get(outer) if isinstance(usage, dict) else None
        value = details.get(inner) if isinstance(details, dict) else None
        if _is_count(value):
            return value
    return None


# 函数用途: 从 usage 取出输出 token 数（含推理），找不到返回 None。
def output_token_count(usage: object) -> int | None:
    values = [usage.get(key) for key in _OUTPUT_KEYS] if isinstance(usage, dict) else []
    return next((value for value in values if _is_count(value)), None)


# 函数用途: 判断一个值是不是合法的 token 数（非负整数，布尔不算）。
def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# LLM: 规则按顺序：带字段两组全被 400/422 拒绝而默认组全部成功 → 不支持（拒绝字段）；任何一组成功样本不足 → 无法判定；
#   全部为 0 → 不支持（模型不产生推理 token）；否则按中位数倍数、差值与不重叠三条同时满足才算支持。
#   计量优先推理 token；只有所有成功样本都没有推理 token、但都有输出 token 时才退回比较输出 token（中转接口常不单独报告推理）。
# 函数用途: 由三组样本得出检测结论。
def judge_reasoning_samples(samples: list[ProbeSample]) -> ProbeVerdict:
    groups = {level: [sample for sample in samples if sample.level == level] for level in PROBE_LEVELS}
    if _field_rejected(groups):
        return ProbeVerdict(VERDICT_UNSUPPORTED, "field_rejected")
    measure, counts = _measured_counts(groups)
    if any(len(values) < PROBE_ROUNDS for values in counts.values()):
        return ProbeVerdict(VERDICT_INCONCLUSIVE, "incomplete" if any(not sample.ok for sample in samples) else "no_usage")
    low, high, default = (int(median(counts[level])) for level in PROBE_LEVELS)
    medians = {"measure": measure, "low_median": low, "max_median": high, "default_median": default}
    if not any(value for values in counts.values() for value in values):
        return ProbeVerdict(VERDICT_UNSUPPORTED, "no_reasoning", **medians)
    separated = min(counts["max"]) > max(counts["low"])
    if separated and high >= _MIN_RATIO * low and high - low >= _MIN_GAP_TOKENS:
        return ProbeVerdict(VERDICT_SUPPORTED, "max_above_low", **medians)
    return ProbeVerdict(VERDICT_UNSUPPORTED, "no_difference", **medians)


# 函数用途: 判断服务商是否拒绝了推理强度字段（带字段的请求全被 400/422 拒绝，不带字段的全部成功）。
def _field_rejected(groups: dict[str, list[ProbeSample]]) -> bool:
    fielded = groups["low"] + groups["max"]
    baseline = groups[""]
    return bool(fielded and baseline) and all(sample.status_code in _REJECT_STATUS for sample in fielded) \
        and all(sample.ok for sample in baseline)


# LLM: 返回（计量口径, 每组成功样本的计数）；推理 token 在所有成功样本里都有才用它，否则所有成功样本都有输出 token 才退回输出 token，
#   两者都不齐时返回空计数（判定为无法判定）。
# 函数用途: 选定这次检测用哪种 token 计量，并取出各组的数值。
def _measured_counts(groups: dict[str, list[ProbeSample]]) -> tuple[str, dict[str, list[int]]]:
    succeeded = [sample for rows in groups.values() for sample in rows if sample.ok]
    for measure, field in ((MEASURE_REASONING, "reasoning_tokens"), (MEASURE_OUTPUT, "output_tokens")):
        if succeeded and all(getattr(sample, field) is not None for sample in succeeded):
            return measure, {level: [getattr(sample, field) for sample in rows if sample.ok] for level, rows in groups.items()}
    return "", {level: [] for level in groups}


__all__ = [
    "MEASURE_OUTPUT",
    "MEASURE_REASONING",
    "PROBE_LEVELS",
    "PROBE_ROUNDS",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_SUPPORTED",
    "VERDICT_UNSUPPORTED",
    "ProbeSample",
    "ProbeVerdict",
    "judge_reasoning_samples",
    "output_token_count",
    "reasoning_token_count",
]
