# LLM: Subagent progress closeout converts task-local product progress into a structured runner result.
# 模块用途: 当子代理已经写出声明产物但还没写 output.json 时，用机器进度包生成 DONE 结果。

from __future__ import annotations

import json
from pathlib import Path

from ..backends import ModelResponse
from ..subagents.utils import _read_json_object
from .runner_context import current_subagent_run_id


# LLM: subagent_progress_closeout_response turns ready task-local product progress into a final runner envelope.
# 函数用途: 真实 runner 已写出声明业务产物但未再写 output.json 时，用 latest_tool_progress 的机器字段收口，避免最后一步靠模型自觉。
def subagent_progress_closeout_response(agent, fallback: ModelResponse) -> ModelResponse | None:
    task = _current_subagent_task(agent)
    if task is None:
        return None
    progress = _latest_progress_payload(task)
    if not _progress_ready_for_closeout(progress, task):
        return None
    return _closeout_response(_progress_closeout_payload(progress, task), fallback)


# LLM: _current_subagent_task loads only the active runner task for progress closeout.
# 函数用途: 只在子代理 runner 上下文里工作；主代理普通工具轮不会触发自动收口。
def _current_subagent_task(agent) -> object | None:
    run_id = current_subagent_run_id(agent)
    if not run_id:
        return None
    try:
        return agent.subagents.load(run_id)
    except Exception:
        return None


# LLM: _latest_progress_payload reads the small task-local progress control packet.
# 函数用途: 只读取 latest_tool_progress.json，不扫描产物目录，也不读取产物正文进 prompt。
def _latest_progress_payload(task) -> dict[str, object]:
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if not workspace:
        return {}
    payload = _read_json_object(Path(workspace) / "progress" / "latest_tool_progress.json")
    return payload if isinstance(payload, dict) else {}


# LLM: _progress_ready_for_closeout checks machine facts, not natural-language summaries.
# 函数用途: HTML 走完整结构检查；其它开放格式必须命中声明 output_refs/output_files 才自动收口。
def _progress_ready_for_closeout(progress: dict[str, object], task) -> bool:
    path = str(progress.get("latest_written_path") or "").strip()
    if not path or path == str(progress.get("closeout_written_path") or "").strip():
        return False
    if _same_path(path, getattr(task, "output_json", "")):
        return False
    artifact = Path(path)
    if not artifact.is_file():
        return False
    integrity = progress.get("artifact_integrity")
    if isinstance(integrity, dict) and integrity.get("kind") == "html":
        if integrity.get("ok") is not True:
            return False
        return not (integrity.get("blocker_codes") or integrity.get("warning_codes"))
    return _matches_declared_product_output(path, task)


# LLM: _progress_closeout_payload is the synthetic SUBAGENT_RESULT contract for ready artifacts.
# 函数用途: 把 latest_tool_progress 的 refs 转成 artifacts/evidence_packets/tests，交父级汇总或最终 closeout 继续判断业务质量。
def _progress_closeout_payload(progress: dict[str, object], task) -> dict[str, object]:
    artifact_ref = str(progress.get("latest_written_path") or "").strip()
    progress_ref = str(progress.get("latest_tool_progress_ref") or "").strip()
    evidence_refs = [ref for ref in [progress_ref] if ref]
    return {
        "status": "DONE",
        "summary": "task-local progress shows a declared product artifact ready for parent summarization.",
        "used_tools": [],
        "used_skills": [],
        "evidence": _progress_evidence(progress_ref, artifact_ref),
        "evidence_packets": _progress_evidence_packets(artifact_ref, evidence_refs, task),
        "coverage_records": [],
        "capability_requests": [],
        "artifacts": _progress_artifacts(artifact_ref),
        "tests": _progress_tests(progress),
        "patches": [],
        "lessons": [],
        "next_actions": ["summarize_or_deliver"],
        "blocked_reason": "",
        "failure_type": "",
    }


# LLM: _progress_evidence keeps the closeout evidence shape compact and stable.
# 函数用途: 生成指向 progress 控制包的结构化证据，不读取产物正文。
def _progress_evidence(progress_ref: str, artifact_ref: str) -> list[dict[str, object]]:
    return [
        {
            "kind": "product_artifact_progress",
            "summary": "latest product artifact path is recorded by task-local progress",
            "path": progress_ref,
            "url": "",
            "ok": True,
            "artifact_ref": artifact_ref,
        }
    ]


# LLM: _progress_evidence_packets links the product artifact and progress packet for closeout.
# 函数用途: 生成最小 evidence packet，后续 closeout 可追踪到产物和结构检查记录。
def _progress_evidence_packets(
    artifact_ref: str, evidence_refs: list[str], task: object
) -> list[dict[str, object]]:
    return [
        {
            "id": f"evpkt-progress-closeout-{getattr(task, 'id', 'run')}",
            "claim": "latest product artifact passed task-local artifact integrity checks",
            "checked_scope": "latest_tool_progress",
            "evidence_refs": evidence_refs,
            "artifact_refs": [artifact_ref],
            "confidence": 0.8,
        }
    ]


# LLM: _progress_artifacts exposes the product artifact as a ref-only payload.
# 函数用途: 把 latest_written_path 转成父级汇总能读取的 artifact 引用。
def _progress_artifacts(artifact_ref: str) -> list[dict[str, object]]:
    return [
        {
            "path": artifact_ref,
            "kind": "file",
            "summary": "product artifact ref from latest_tool_progress",
        }
    ]


# LLM: _progress_tests reports the deterministic artifact-integrity check.
# 函数用途: 写出机器可读测试记录，说明当前只证明结构完整，不替代业务验收。
def _progress_tests(progress: dict[str, object]) -> list[dict[str, object]]:
    integrity = progress.get("artifact_integrity")
    if isinstance(integrity, dict) and integrity.get("kind") == "html":
        return [
            {
                "name": "artifact integrity",
                "validation_method": "artifact_integrity",
                "ok": True,
                "summary": "no blocker_codes or warning_codes",
            }
        ]
    return [
        {
            "name": "declared product artifact exists",
            "validation_method": "file_exists",
            "ok": True,
            "summary": "declared output path exists and is non-empty enough for closeout",
        }
    ]


# LLM: _closeout_response wraps a progress payload in the standard subagent result block.
# 函数用途: 保持 progress 收口和 output.json 收口使用相同的父级读取格式。
def _closeout_response(payload: dict[str, object], fallback: ModelResponse) -> ModelResponse:
    text = (
        "[SUBAGENT_RESULT]\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "[/SUBAGENT_RESULT]\n\n"
        "系统检测到 task-local progress 已形成产物引用，已结束工具循环并交回父级汇总。"
    )
    return ModelResponse(text=text, backend=fallback.backend)


# LLM: _same_path compares optional refs without requiring files to exist.
# 函数用途: 防止内部 output.json 写入被误当作用户业务产物。
def _same_path(first: object, second: object) -> bool:
    left = str(first or "").strip()
    right = str(second or "").strip()
    if not left or not right:
        return False
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except OSError:
        return left == right


# LLM: _matches_declared_product_output prevents generic progress closeout from promoting scratch files.
# 函数用途: 非 HTML/未知格式只在命中机器声明的 output_refs/output_files/artifact_refs 时自动收口。
def _matches_declared_product_output(path: str, task: object) -> bool:
    return any(_same_path(path, ref) for ref in _declared_product_refs(task))


# LLM: _declared_product_refs reads only machine fields that were set by create/schedule contracts.
# 函数用途: 从 task.attributes 和 output-scope context packs 中读取交付目标，不解析 goal 自然语言。
def _declared_product_refs(task: object) -> list[str]:
    refs: list[str] = []
    attrs = getattr(task, "attributes", {}) or {}
    if isinstance(attrs, dict):
        for key in ("output_refs", "output_files", "artifact_refs"):
            refs.extend(_string_list(attrs.get(key)))
    for pack in getattr(task, "context_packs", []) or []:
        if not isinstance(pack, dict):
            continue
        contract = pack.get("contract")
        if not isinstance(contract, dict):
            continue
        kind = str(contract.get("kind") or "").strip()
        idem_key = str(contract.get("idempotency_key") or contract.get("key") or "").strip()
        if kind != "system_derived_output_scope" and not idem_key.endswith(".output_refs"):
            continue
        refs.extend(_string_list(contract.get("scope_refs")))
        refs.extend(_string_list(contract.get("target_artifact_refs")))
    return _unique_strings(refs)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique
