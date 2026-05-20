# LLM: Failure sample capture converts runner failures into replayable refs.
# 模块用途: 把小真实/中真实验收失败项转换成 failure sample library 的结构化记录。

from __future__ import annotations

from typing import Any


# LLM: failure_samples_from_case_results is the bridge from live failures to replay assets.
# 函数用途: 只读取 case 结构化字段，生成合同 fixture、fake trace、replay 和回归测试引用。
def failure_samples_from_case_results(results: tuple[object, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
        sample
        for result in results
        for sample in (_sample_from_case(_case_dict(result)),)
        if sample
    )


# LLM: _sample_from_case returns a complete failure sample or an empty dict for passing cases.
# 函数用途: 将一个 FAILED/BLOCKED case 规整成 failure_sample_library_contract 可验收形状。
def _sample_from_case(case: dict[str, Any]) -> dict[str, Any]:
    if str(case.get("status") or "").strip() not in {"FAILED", "BLOCKED"}:
        return {}
    case_id = str(case.get("case_id") or "unknown_case").strip() or "unknown_case"
    issues = _string_list(case.get("issues")) or ["UNKNOWN_FAILURE"]
    return {
        "case_id": case_id,
        "failure_type": _failure_type(issues),
        "contract_fixture_ref": _ref(case, "contract_fixture_ref", f"contract-fixture://{case_id}.yaml"),
        "fake_tool_trace_ref": _ref(case, "fake_tool_trace_ref", f"trace://{case_id}/tool.jsonl"),
        "fake_llm_trace_ref": _ref(case, "fake_llm_trace_ref", f"trace://{case_id}/llm.jsonl"),
        "replay_spec_ref": _ref(case, "replay_spec_ref", f"replay://{case_id}.json"),
        "expected_error_codes": issues,
        "regression_test_ref": _ref(case, "regression_test_ref", f"pytest://replay/{case_id}"),
        "source": "pre_real_task_validation",
    }


# LLM: _case_dict accepts dataclass-style and dict-style case results.
# 函数用途: 兼容 runner dataclass/to_dict 输出，避免每个 runner 写一套失败转换逻辑。
def _case_dict(result: object) -> dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        return dict(value) if isinstance(value, dict) else {}
    return {
        key: getattr(result, key)
        for key in ("case_id", "status", "issues")
        if hasattr(result, key)
    }


# LLM: _failure_type normalizes issue codes into one stable failure class.
# 函数用途: 从结构化 issue code 里提取粗粒度类型，不解析自然语言描述。
def _failure_type(issues: list[str]) -> str:
    first = issues[0].lower().strip()
    if "artifact" in first:
        return "artifact_contract"
    if "tool" in first:
        return "tool_contract"
    if "timeout" in first:
        return "timeout"
    return "contract"


# LLM: _ref returns an explicit case ref or a deterministic fallback ref.
# 函数用途: 规整失败样本引用字段，保证缺省时仍可形成 replayable 记录。
def _ref(case: dict[str, Any], key: str, fallback: str) -> str:
    value = str(case.get(key) or "").strip()
    return value or fallback


# LLM: _string_list normalizes explicit issue arrays only.
# 函数用途: 从结构化列表中提取非空字符串，不解析正文含义。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


__all__ = ["failure_samples_from_case_results"]
