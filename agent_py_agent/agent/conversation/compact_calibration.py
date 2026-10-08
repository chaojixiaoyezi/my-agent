# LLM: 本模块是 Compact 候选/压缩前计量的唯一校准口径（纯函数，不读宿主、不读线程、不写状态）。观测由宿主边界按
#   fingerprint 与代次冻结后传入（context_pressure.frozen_compact_request_calibration）；这里只做数字换算。
#   与预检 _provider_calibrated_context_tokens 的关系：请求不小于观测请求时两者完全一致（追加口径）；请求比观测请求小
#   时预检的本轮口径原样返回原始值，而候选门必须按观测比例折算（下限 50%），否则压小后的候选永远改变不了接受结果。
#   改这里的公式要同步 test_compact_calibrated_candidate_gate 与 context_pressure 的注释。
# 模块用途: 把“本地估算 / 供应商实际”这一对观测应用到压缩候选与压缩前的计量上，让接受门和预检说同一种数。
from __future__ import annotations

from dataclasses import dataclass

CALIBRATION_SCOPE_CURRENT_RUN = "current_run"
CALIBRATION_SCOPE_DURABLE_THREAD = "durable_thread"
# owner 级按分词身份的比值（别的会话或别的请求表面）：只按比例折算，不做增量口径（两边原始量不在同一基线上）。
CALIBRATION_SCOPE_OWNER_RATIO = "owner_ratio"


# LLM: 冻结后的观测事实；字段直接来自 provider_context_observation.v3 加上冻结时的作用域与代次，不含任何正文。
#   raw_estimated_tokens/provider_input_tokens 任一不为正数即视为不可用（calibrated_compact_request_tokens 原样返回）。
# 类用途: 表示“某个请求表面在某个压缩代次下，本地估算多少、供应商实际多少”，供候选门和恢复宿主复用。
@dataclass(frozen=True)
class CompactRequestCalibration:
    raw_estimated_tokens: int
    provider_input_tokens: int
    context_surface_fingerprint: str = ""
    compact_generation: int = 0
    calibration_scope: str = CALIBRATION_SCOPE_CURRENT_RUN


# LLM: 纯换算：无观测/坏观测返回原始值；请求比观测小按 ceil(raw × max(provider, ceil(observed/2)) / observed) 折算；
#   请求不小于观测时本轮作用域按 provider + 增量，耐久作用域取折算与增量口径的较大者——与预检对同类输入的结果相同；
#   owner_ratio 作用域（别的会话按分词身份的比值）一律只按比例折算。
#   不能在这里猜系数：没有观测就是没有校准。
# 函数用途: 把一个请求的本地估算换成“按上次供应商观测校准后”的 token 数。
def calibrated_compact_request_tokens(raw_tokens: int, calibration: CompactRequestCalibration | None) -> int:
    raw = max(0, int(raw_tokens or 0))
    if calibration is None:
        return raw
    observed_raw = int(calibration.raw_estimated_tokens or 0)
    provider_input = int(calibration.provider_input_tokens or 0)
    if observed_raw <= 0 or provider_input <= 0:
        return raw
    conservative_input = max(provider_input, (observed_raw + 1) // 2)
    scaled = (raw * conservative_input + observed_raw - 1) // observed_raw
    if raw < observed_raw or calibration.calibration_scope == CALIBRATION_SCOPE_OWNER_RATIO:
        return scaled
    grown = provider_input + (raw - observed_raw)
    if calibration.calibration_scope == CALIBRATION_SCOPE_DURABLE_THREAD:
        return max(scaled, grown)
    return grown


# LLM: 瘦身（compact_message_source.shrink_tool_outputs）按原始估算累计“可省 token”，而预算比较按校准口径；超出量必须换回
#   原始口径再交给瘦身，否则校准比例越小瘦得越少、再量一次又超预算。校准值不大于 0 或不超预算时返回 0。
# 函数用途: 把“校准口径超出预算多少”换算成“原始估算口径要省多少”，向上取整。
def raw_token_excess(raw_tokens: int, calibrated_tokens: int, budget_tokens: int) -> int:
    raw, calibrated = max(0, int(raw_tokens or 0)), max(0, int(calibrated_tokens or 0))
    excess = calibrated - max(0, int(budget_tokens or 0))
    if excess <= 0 or calibrated <= 0:
        return 0
    return -(-excess * max(raw, calibrated) // calibrated)


__all__ = [
    "CALIBRATION_SCOPE_CURRENT_RUN",
    "CALIBRATION_SCOPE_DURABLE_THREAD",
    "CALIBRATION_SCOPE_OWNER_RATIO",
    "CompactRequestCalibration",
    "calibrated_compact_request_tokens",
    "raw_token_excess",
]
