# LLM: Dispatch ref helpers keep model-facing deliverable refs separate from tool execution.
# 模块用途: 汇总本轮 dispatch 触碰的 artifact/evidence refs，避免主工具文件继续变胖。

from __future__ import annotations

import json
from pathlib import Path


# LLM: related_task_refs exposes product refs from the runs touched by this dispatch report.
# 函数用途: 收集本轮 run 和 runner 新建 child 的 artifact/evidence refs，不读取文件正文。
def related_task_refs(agent: object, report: object, attr: str, *, limit: int = 20) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for run_id in _related_run_ids(report):
        task = _safe_load_task(agent, run_id)
        if _extend_unique_refs(refs, seen, _string_refs(getattr(task, attr, []), limit=limit), limit):
            return refs
    return refs


# LLM: related_task_result_refs gives parent agents a compact per-run result index instead of a long flat path list.
# 函数用途: 按本轮触碰的 run 和 runner 新建 child 汇总状态、摘要和 refs，父级优先读这里，避免猜子代理文件名。
def related_task_result_refs(agent: object, report: object, *, per_run_artifact_limit: int = 3) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for run_id in _related_run_ids(report) or _visible_run_ids(agent):
        task = _safe_load_task(agent, run_id)
        if not _has_task_identity(task, run_id):
            continue
        row = _task_result_ref_row(task, per_run_artifact_limit=per_run_artifact_limit)
        if row:
            rows.append(row)
    return rows


# LLM: _related_run_ids includes explicit runner targets without flattening entire descendant trees.
# 函数用途: 调度索引只列本轮直接相关 run；孙代理通过对应父代理 output/tree 追踪，避免大型代理树撑爆上下文。
def _related_run_ids(report: object) -> list[str]:
    ids: list[str] = []
    for record in getattr(report, "records", []) or []:
        _extend_unique_refs(ids, set(ids), _record_related_run_ids(record), 100)
    return ids


# LLM: _visible_run_ids is the status-query fallback when dispatch did not touch a concrete run.
# 函数用途: 顶层模型把 dispatch_subagents 当“查一下子代理状态”使用时，返回当前子代理索引而不是空摘要。
def _visible_run_ids(agent: object, *, limit: int = 20) -> list[str]:
    try:
        tasks = list(agent.subagents.list_runs())
    except Exception:
        return []
    ids: list[str] = []
    for task in sorted(tasks, key=_task_sort_key):
        run_id = str(getattr(task, "id", "") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
        if len(ids) >= limit:
            break
    return ids


def _task_sort_key(task: object) -> tuple[int, float, str]:
    try:
        depth = int(getattr(task, "depth", 0) or 0)
    except (TypeError, ValueError):
        depth = 0
    try:
        created = float(getattr(task, "created_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        created = 0.0
    return depth, created, str(getattr(task, "id", "") or "")


# LLM: _record_related_run_ids extracts the run touched by one dispatch record.
# 函数用途: 只返回当前 record 的 run_id；不自动展开 runner_created_child_ids，避免跨层级抢占父级汇总入口。
def _record_related_run_ids(record: object) -> list[str]:
    run_id = str(getattr(record, "run_id", "") or "").strip()
    return [run_id] if run_id else []


# LLM: _extend_unique_refs is the shared bounded append helper for run ids and artifact refs.
# 函数用途: 按首次出现顺序追加非空唯一 ref；达到 limit 时返回 True 提示调用方停止。
def _extend_unique_refs(target: list[str], seen: set[str], refs: list[str], limit: int) -> bool:
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        target.append(ref)
        if len(target) >= limit:
            return True
    return False


# LLM: _safe_load_task keeps advisory payload generation from breaking dispatch_subagents.
# 函数用途: 读取任务失败时返回空对象，保证 refs 汇总只增强输出、不影响调度主流程。
def _safe_load_task(agent: object, run_id: str) -> object:
    try:
        return agent.subagents.load(run_id)
    except Exception:
        return object()


# LLM: _has_task_identity filters the empty object returned by safe-load failures.
# 函数用途: 确认加载到的 task 确实对应目标 run，避免错误对象进入父级结果索引。
def _has_task_identity(task: object, run_id: str) -> bool:
    return str(getattr(task, "id", "") or "").strip() == run_id


# LLM: _task_result_ref_row keeps one child handoff self-contained and refs-only.
# 函数用途: 给父级返回每个子代理的权威状态、摘要、output/run refs 和少量主产物路径。
def _task_result_ref_row(task: object, *, per_run_artifact_limit: int) -> dict[str, object]:
    output_payload = _read_output_payload(task)
    artifacts = _string_refs(getattr(task, "artifact_refs", []), limit=per_run_artifact_limit)
    if not artifacts:
        artifacts = _output_artifact_refs(output_payload, limit=per_run_artifact_limit)
    evidence = _string_refs(getattr(task, "evidence_refs", []), limit=2)
    summary = _task_summary(task, output_payload)
    return {
        "run_id": str(getattr(task, "id", "") or ""),
        "agent_name": str(getattr(task, "agent_name", "") or ""),
        "role": str(getattr(task, "role", "") or ""),
        "status": str(getattr(task, "status", "") or ""),
        "verification_status": str(getattr(task, "verification_status", "") or ""),
        "summary": summary,
        "primary_artifact_refs": artifacts,
        "primary_artifact_summaries": _output_artifact_summaries(output_payload, limit=per_run_artifact_limit),
        "evidence_refs": evidence,
        "output_json": str(getattr(task, "output_json", "") or ""),
        "runner_result_json": str(getattr(task, "runner_result_json", "") or ""),
    }


# LLM: _read_output_payload reads only the small closeout json so parents get summaries without artifact bodies.
# 函数用途: 从 output.json 读取状态和产物摘要；失败返回空对象，不能影响 dispatch 主流程。
def _read_output_payload(task: object) -> dict[str, object]:
    output_json = str(getattr(task, "output_json", "") or "")
    if not output_json:
        return {}
    try:
        payload = json.loads(Path(output_json).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _task_summary prefers canonical task summary and falls back to output closeout facts.
# 函数用途: 给父级一行可读交接摘要，减少它为了弄懂子代理产物而读取整篇报告。
def _task_summary(task: object, output_payload: dict[str, object]) -> str:
    candidates = (
        getattr(task, "latest_summary", ""),
        getattr(task, "result", ""),
        output_payload.get("summary", ""),
        output_payload.get("message", ""),
    )
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text[:500]
    return _first_packet_claim(output_payload.get("evidence_packets"))[:500]


# LLM: _first_packet_claim gives parents a compact fallback without reading artifact bodies.
# 函数用途: 从 evidence_packets 中取第一条 claim 作为摘要兜底；无有效 claim 时返回空字符串。
def _first_packet_claim(value: object) -> str:
    if not isinstance(value, list):
        return ""
    for packet in value:
        claim = _packet_claim(packet)
        if claim:
            return claim
    return ""


# LLM: _packet_claim isolates one evidence-packet shape check for flat summary fallback logic.
# 函数用途: 非 dict 或空 claim 返回空字符串；有效 claim 返回清洗后的文本。
def _packet_claim(packet: object) -> str:
    if not isinstance(packet, dict):
        return ""
    return str(packet.get("claim") or "").strip()


# LLM: _output_artifact_refs recovers refs from structured output.json artifact entries.
# 函数用途: task.artifact_refs 为空时，从 output.json artifacts/evidence_packets 中恢复主产物路径。
def _output_artifact_refs(payload: dict[str, object], *, limit: int) -> list[str]:
    refs = _artifact_entry_paths(payload.get("artifacts"))
    refs.extend(_packet_artifact_refs(payload.get("evidence_packets")))
    return _unique_strings(refs)[:limit]


# LLM: _output_artifact_summaries keeps artifact descriptions beside refs without reading artifact bodies.
# 函数用途: 把 output.json artifacts 的 path/summary/kind 压成短列表，供父级按需选择是否读取正文。
def _output_artifact_summaries(payload: dict[str, object], *, limit: int) -> list[dict[str, str]]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    rows: list[dict[str, str]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        rows.append({
            "path": path,
            "kind": str(item.get("kind") or "").strip(),
            "summary": str(item.get("summary") or "").strip()[:220],
        })
        if len(rows) >= limit:
            break
    return rows


# LLM: _artifact_entry_paths extracts paths from output.json artifacts without interpreting summaries.
# 函数用途: 支持 output.json 中 artifacts=[{path: ...}] 的标准 closeout 格式。
def _artifact_entry_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        if isinstance(item, dict):
            refs.append(str(item.get("path") or "").strip())
    return [ref for ref in refs if ref]


# LLM: _packet_artifact_refs extracts evidence packet artifact paths.
# 函数用途: 兼容子代理只在 evidence_packets[].artifact_refs 里登记产物路径的情况。
def _packet_artifact_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        if isinstance(item, dict):
            refs.extend(str(ref or "").strip() for ref in item.get("artifact_refs") or [])
    return [ref for ref in refs if ref]


# LLM: _string_refs bounds persisted task refs before model-facing payloads.
# 函数用途: 清洗 artifact/evidence refs，避免 dispatch 输出被大量产物路径撑大。
def _string_refs(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return _unique_strings([str(item or "").strip() for item in value if str(item or "").strip()])[:limit]


# LLM: _unique_strings preserves first occurrence order for small model-facing lists.
# 函数用途: 给 run ids 和 refs 去重，保持调度输出稳定。
def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
