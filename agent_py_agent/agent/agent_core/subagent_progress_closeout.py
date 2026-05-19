# LLM: Subagent progress closeout converts task-local write progress into a structured runner result.
# 模块用途: 当子代理已经写出可验收产物但还没写 output.json 时，用机器进度包生成 SUBAGENT_RESULT。

from __future__ import annotations

import json
from pathlib import Path

from ..backends import ModelResponse
from ..subagents.utils import _read_json_object


# LLM: subagent_progress_closeout_response turns ready task-local write progress into a final runner envelope.
# 函数用途: 真实 runner 已写出完整产物但未再写 output.json 时，用 latest_tool_progress 的机器字段收口，避免最后一步靠模型自觉。
def subagent_progress_closeout_response(agent, fallback: ModelResponse) -> ModelResponse | None:
    task = _current_subagent_task(agent)
    if task is None:
        return None
    progress = _latest_progress_payload(task)
    if not _progress_ready_for_closeout(progress, task):
        return None
    return _closeout_response(_progress_closeout_payload(progress, task), fallback)


# LLM: subagent_progress_timeout_closeout_response salvages timed-out runners from machine progress facts.
# 函数用途: runner 超时前已写产物时，转成等待验收或精准修复合同，避免只留下泛化 TIMEOUT。
def subagent_progress_timeout_closeout_response(agent, run_id: str, fallback: ModelResponse) -> ModelResponse | None:
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return None
    progress = _latest_progress_payload(task)
    if _progress_ready_for_closeout(progress, task):
        return _closeout_response(_progress_closeout_payload(progress, task), fallback)
    repair_payload = _progress_repair_payload(progress, task)
    if repair_payload:
        return _closeout_response(repair_payload, fallback)
    return None


# LLM: _current_subagent_task loads only the active runner task for progress closeout.
# 函数用途: 只在子代理 runner 上下文里工作；主代理普通工具轮不会触发自动收口。
def _current_subagent_task(agent) -> object | None:
    run_id = str(getattr(agent, "_current_subagent_run_id", "") or "")
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
# 函数用途: 只有存在真实产物路径、HTML 结构检查无 blocker/warning、且不是内部 output.json 时才自动收口。
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
    if not isinstance(integrity, dict) or integrity.get("kind") != "html":
        return False
    if integrity.get("ok") is not True:
        return False
    return not (integrity.get("blocker_codes") or integrity.get("warning_codes"))


# LLM: _progress_repair_payload turns known artifact integrity issues into a repairable runner result.
# 函数用途: 超时发生时保留最新产物 ref 和机器 issue codes，供父级创建精准 repair worker。
def _progress_repair_payload(progress: dict[str, object], task) -> dict[str, object]:
    artifact_ref = str(progress.get("latest_written_path") or "").strip()
    if not artifact_ref or _same_path(artifact_ref, getattr(task, "output_json", "")):
        return {}
    if not Path(artifact_ref).is_file():
        return {}
    integrity = progress.get("artifact_integrity")
    if not isinstance(integrity, dict) or integrity.get("kind") != "html":
        return {}
    codes = _progress_repair_codes(integrity)
    if not codes:
        return {}
    progress_ref = str(progress.get("latest_tool_progress_ref") or "").strip()
    blocker = f"artifact_integrity_failed:{artifact_ref}:{','.join(codes[:6])}"
    return {
        "status": "BLOCKED",
        "summary": "task-local progress found a product artifact that needs bounded artifact repair after runner timeout.",
        "used_tools": [],
        "used_skills": [],
        "evidence": _progress_evidence(progress_ref),
        "evidence_packets": _progress_evidence_packets(artifact_ref, [ref for ref in [progress_ref] if ref], task),
        "coverage_records": [],
        "capability_requests": [],
        "artifacts": _progress_artifacts(artifact_ref),
        "tests": [
            {
                "name": "artifact integrity",
                "validation_method": "artifact_integrity",
                "ok": False,
                "summary": ",".join(codes[:6]),
            }
        ],
        "patches": [],
        "lessons": [],
        "next_actions": ["repair_artifacts", "rerun_artifact_integrity_check"],
        "blocked_reason": blocker,
        "failure_type": "artifact_integrity_failed",
    }


# LLM: _progress_repair_codes is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _progress_repair_codes(integrity: dict[str, object]) -> list[str]:
    codes = [str(code) for code in integrity.get("blocker_codes") or [] if str(code or "").strip()]
    for code in integrity.get("warning_codes") or []:
        text = str(code or "").strip()
        if text in {"placeholder_hash_link", "missing_hash_target"} and text not in codes:
            codes.append(text)
    return codes


# LLM: _progress_closeout_payload is the synthetic SUBAGENT_RESULT contract for ready artifacts.
# 函数用途: 把 latest_tool_progress 的 refs 转成 artifacts/evidence_packets/tests，交后续验收链路继续判断业务质量。
def _progress_closeout_payload(progress: dict[str, object], task) -> dict[str, object]:
    artifact_ref = str(progress.get("latest_written_path") or "").strip()
    progress_ref = str(progress.get("latest_tool_progress_ref") or "").strip()
    evidence_refs = [ref for ref in [progress_ref] if ref]
    return {
        "status": "AWAITING_ACCEPTANCE",
        "summary": "task-local progress shows a structurally complete product artifact awaiting parent acceptance.",
        "used_tools": [],
        "used_skills": [],
        "evidence": _progress_evidence(progress_ref),
        "evidence_packets": _progress_evidence_packets(artifact_ref, evidence_refs, task),
        "coverage_records": [],
        "capability_requests": [],
        "artifacts": _progress_artifacts(artifact_ref),
        "tests": _progress_tests(),
        "patches": [],
        "lessons": [],
        "next_actions": ["parent_acceptance"],
        "blocked_reason": "",
        "failure_type": "",
    }


# LLM: _progress_evidence keeps the closeout evidence shape compact and stable.
# 函数用途: 生成指向 progress 控制包的结构化证据，不读取产物正文。
def _progress_evidence(progress_ref: str) -> list[dict[str, object]]:
    return [
        {
            "kind": "artifact_integrity",
            "summary": "latest product artifact has no blocker or warning codes",
            "path": progress_ref,
            "url": "",
            "ok": True,
        }
    ]


# LLM: _progress_evidence_packets links the product artifact and progress packet for parent acceptance.
# 函数用途: 生成最小 evidence packet，后续验收器可追踪到产物和结构检查记录。
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
# 函数用途: 把 latest_written_path 转成父级验收能读取的 artifact 引用。
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
def _progress_tests() -> list[dict[str, object]]:
    return [
        {
            "name": "artifact integrity",
            "validation_method": "artifact_integrity",
            "ok": True,
            "summary": "no blocker_codes or warning_codes",
        }
    ]


# LLM: _closeout_response wraps a progress payload in the standard subagent result block.
# 函数用途: 保持 progress 收口和 output.json 收口使用相同的父级读取格式。
def _closeout_response(payload: dict[str, object], fallback: ModelResponse) -> ModelResponse:
    text = (
        "[SUBAGENT_RESULT]\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "[/SUBAGENT_RESULT]\n\n"
        "系统检测到 task-local progress 已形成可验收产物引用，已结束工具循环并等待父级验收。"
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
