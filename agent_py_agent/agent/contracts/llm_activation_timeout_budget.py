# LLM: LLM activation timeout budget builds model-call timing evidence from structured probes.
# 模块用途: 用模型调用账本的 5K/10K probe 样本生成首 token 动态超时预算。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..agent_core.model_call_monitor import (
    FirstTokenTimeoutContext,
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
)
from .model_call_ledger import (
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallStartedParams,
)


# LLM: ProbeSample bundles model probe timing fields to keep helpers narrow.
# 类用途: 描述一个 probe 调用 id、输入 token 数和首 token 延迟。
@dataclass(frozen=True)
class ProbeSample:
    call_id: str
    input_tokens: int
    first_token_latency_seconds: float


# LLM: _FakeClock provides deterministic ledger timings for readiness evidence.
# 类用途: 生成可预测的模型调用 started/first_token/finished 时间，不睡眠、不调用真实模型。
@dataclass
class _FakeClock:
    now_seconds: float = 0.0

    # LLM: now mirrors monotonic clock access for ModelCallLedger.
    # 函数用途: 返回测试时钟当前秒数。
    def now(self) -> float:
        return self.now_seconds

    # LLM: advance moves the fake clock forward by explicit seconds.
    # 函数用途: 推进账本时间，让 probe latency 可复现。
    def advance(self, seconds: float) -> None:
        self.now_seconds += float(seconds)


# LLM: build_model_timeout_budget produces first-token timeout evidence from probe ledger facts.
# 函数用途: 用 5K/10K 结构化 probe 样本估算较大输入的首 token 超时预算，并写账本文件。
def build_model_timeout_budget(workspace: Path) -> dict[str, object]:
    root = Path(workspace).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    clock = _FakeClock()
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=clock.now))
    _record_probe(ledger, clock, ProbeSample("probe-5k", 5000, 13.0))
    _record_probe(ledger, clock, ProbeSample("probe-10k", 10000, 23.0))
    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=15000,
            ledger=ledger,
            options=FirstTokenTimeoutOptions(
                safety_margin=1.5,
                min_timeout_seconds=1.0,
                max_timeout_seconds=180.0,
            ),
            context=FirstTokenTimeoutContext(required_probe_tokens=(5000, 10000)),
        )
    )
    ledger_ref = "model_call_ledger.json"
    payload = {
        "ledger_ref": ledger_ref,
        "estimate": estimate.to_dict(),
        "probe_tokens": [5000, 10000],
        "target_input_tokens": 15000,
    }
    _write_json(root / ledger_ref, [record.to_dict() for record in ledger.records()])
    _write_json(root / "timeout_budget.json", payload)
    return payload


# LLM: timeout_budget_issues checks model timeout evidence shape.
# 函数用途: 校验 timeout 估算来源、数值和 cache_suspected 标记。
def timeout_budget_issues(budget: dict[str, object]) -> list[str]:
    estimate = budget.get("estimate") if isinstance(budget.get("estimate"), dict) else {}
    issues: list[str] = []
    if estimate.get("source") != "probe_5k_10k":
        issues.append("MODEL_TIMEOUT_PROBE_SOURCE_MISSING")
    if not isinstance(estimate.get("timeout_seconds"), (int, float)) or float(estimate["timeout_seconds"]) <= 0:
        issues.append("MODEL_TIMEOUT_SECONDS_INVALID")
    if estimate.get("cache_suspected") is not False:
        issues.append("MODEL_TIMEOUT_CACHE_SAMPLE_USED")
    if not str(budget.get("ledger_ref") or ""):
        issues.append("MODEL_TIMEOUT_LEDGER_REF_MISSING")
    return issues


# LLM: _record_probe appends one finished probe sample to the model call ledger.
# 函数用途: 用公共账本 API 写入 started、first_token 和 finished 事件。
def _record_probe(
    ledger: ModelCallLedger,
    clock: _FakeClock,
    sample: ProbeSample,
) -> None:
    ledger.started(
        ModelCallStartedParams(
            call_id=sample.call_id,
            backend="test-backend",
            model="test-model",
            input_tokens=sample.input_tokens,
            output_tokens_estimate=1,
            request_id=sample.call_id,
            run_id="activation-probe",
            is_probe=True,
        )
    )
    clock.advance(sample.first_token_latency_seconds)
    ledger.first_token(ModelCallFirstTokenParams(call_id=sample.call_id))
    ledger.finished(ModelCallFinishParams(call_id=sample.call_id, output_tokens=1))


# LLM: _write_json persists timeout evidence in deterministic JSON form.
# 函数用途: 写入 JSON 文件，供后续 replay、审计或 CI gate 读取。
def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


__all__ = ["ProbeSample", "build_model_timeout_budget", "timeout_budget_issues"]
