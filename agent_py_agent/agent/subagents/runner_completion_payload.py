# LLM: 子代理完成信封只投影已有结果和精确引用；状态、权限、父级通知和恢复仍由各自权威模块处理。
# 模块用途: 给直属父级和活动回合生成同一份有界完成内容，不发送通知或改任务状态。
from __future__ import annotations

from typing import Any

from ..contracts.subagent_completion import SUBAGENT_COMPLETION_SCHEMA_VERSION
from ..memory_archive.tokens import estimate_tokens
from ..model_visible_refs import current_model_ref, current_model_ref_list
from .context_bundle_contracts import declared_output_refs

_COMPLETION_MESSAGE_MAX_TOKENS = 1_000
_COMPLETION_EVIDENCE_REF_LIMIT = 20


# LLM: 根父级 wake 与递归父级必须读取同一交接 schema；内容有界且只含当前结果和规范 refs，不授予完成权。
# 函数用途: 生成子代理最终回复与产物位置的统一交接包，长正文保留报告引用。
def completion_handoff_payload(
    task: Any,
    result: Any | None = None,
    output_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    selected_result = result if result is not None else task
    selected_output = output_payload if isinstance(output_payload, dict) else {}
    completion_message, completion_truncated, completion_tokens = _completion_message(
        task,
        selected_result,
        selected_output,
    )
    selected_artifacts = selected_output.get("artifacts")
    artifact_refs: list[str] = []
    if isinstance(selected_artifacts, list):
        for item in selected_artifacts:
            ref = current_model_ref(item.get("path") if isinstance(item, dict) else item)
            if ref and ref not in artifact_refs:
                artifact_refs.append(ref)
    else:
        artifact_refs = current_model_ref_list(
            getattr(task, "artifact_refs", []) or [],
            limit=_COMPLETION_EVIDENCE_REF_LIMIT,
        )
    payload: dict[str, object] = {
        "completion_schema_version": SUBAGENT_COMPLETION_SCHEMA_VERSION,
        "completion_message": completion_message,
        "final_report_ref": current_model_ref(
            getattr(task, "agent_run_final_report_md", "")
            or getattr(task, "debrief_file", "")
        ),
        "declared_output_refs": declared_output_refs(task)[:_COMPLETION_EVIDENCE_REF_LIMIT],
        "artifact_refs": artifact_refs[:_COMPLETION_EVIDENCE_REF_LIMIT],
    }
    if completion_truncated:
        payload["completion_message_truncated"] = True
        payload["completion_message_original_tokens"] = completion_tokens
    return payload


# LLM: 宿主状态仍是完成权威；这里只限制交给父级的模型正文体积，不据正文推断任务终态。
# 函数用途: 从当前结果提取最终回复，超过预算时保留开头、结尾和完整报告引用提示。
def _completion_message(
    task: Any,
    result: Any,
    output_payload: dict[str, object],
) -> tuple[str, bool, int]:
    candidates = (
        getattr(task, "result", ""),
        output_payload.get("response", ""),
        output_payload.get("summary", ""),
        getattr(result, "message", ""),
        getattr(task, "latest_summary", ""),
    )
    text = next((str(value).strip() for value in candidates if str(value or "").strip()), "")
    original_tokens = estimate_tokens(text) if text else 0
    if original_tokens <= _COMPLETION_MESSAGE_MAX_TOKENS:
        return text, False, original_tokens

    marker = "\n...[完成消息已截断；完整内容见 final_report_ref]...\n"
    low = 0
    high = len(text)
    best = marker.strip()
    while low <= high:
        keep = (low + high) // 2
        head_chars = (keep * 3) // 4
        tail_chars = keep - head_chars
        tail = text[-tail_chars:] if tail_chars else ""
        candidate = text[:head_chars] + marker + tail
        if estimate_tokens(candidate) <= _COMPLETION_MESSAGE_MAX_TOKENS:
            best = candidate
            low = keep + 1
        else:
            high = keep - 1
    return best, True, original_tokens


# LLM: 完成引用只是只读交付索引，不能决定状态、验收、权限、重试或根任务收口。
# 函数用途: 从最终报告和已登记产物中生成去重后的规范引用，供原父级通知直接携带。
def completion_evidence_refs(
    task: Any,
    output_payload: dict[str, object],
    metadata: dict[str, object],
) -> list[str]:
    refs = [current_model_ref(metadata.get("final_report_ref"))]
    refs.extend(current_model_ref_list(getattr(task, "artifact_refs", []) or []))
    artifacts = output_payload.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, dict):
                refs.append(current_model_ref(item.get("path")))
            else:
                refs.append(current_model_ref(item))
    return list(dict.fromkeys(ref for ref in refs if ref))[:_COMPLETION_EVIDENCE_REF_LIMIT]
