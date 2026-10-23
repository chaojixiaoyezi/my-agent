# LLM: Domain projections consume only the current authorized result; preserve original orchestration summaries for other tools.
# 模块用途: 为外置命令和代理树保留关键字段及有界头尾，归档只暴露逻辑读取引用。
from __future__ import annotations

import json
import re
from types import SimpleNamespace

from ...tooling.output_projection import project_tool_output_body
from ..agent_tree.model_view import agent_tree_model_preview
from ..orchestration.context.live_summary import orchestration_live_summary as _original_summary

# 工具实时摘要中单段摘录保留的最大字符数。
_EXCERPT_CHARS = 1200
# 子代理工具实时摘要保留的最大字符数。
_AGENT_SUMMARY_CHARS = 8000
# 进程工具实时摘要保留的最大字符数。
_PROCESS_SUMMARY_CHARS = 8000
DOMAIN_TOOLS = frozenset({"run_command", "process_session", "list_agents"})


# LLM: Physical archive paths remain host-only; never invent a readable ref from a path or filename.
# 函数用途: 从本次归档元数据取得可恢复的逻辑引用。
def archive_output_ref(record: dict[str, object]) -> str:
    ref = str(record.get("scoped_call_id") or record.get("id") or record.get("call_id")
              or record.get("artifact_ref") or "")
    return "" if ref.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", ref) else ref


# LLM: This is one host-owned line outside the untrusted body; escape control line breaks and keep the old reader call contract.
# 函数用途: 将逻辑引用、哈希与续读提示收成单行，不展示物理归档路径。
def compact_archive_pointer(record: dict[str, object]) -> str:
    ref = archive_output_ref(record)
    digest = str(record.get("output_hash") or record.get("sha256") or "")
    hint = json.dumps({"tool": "read_artifact", "artifact_ref": ref, "offset": 0,
                       "max_chars": 4000}, ensure_ascii=False) if ref else "unavailable"
    text = f"[Archive Ref: artifact_ref={ref} path=<host-only> sha256={digest} read_artifact_hint={hint}]"
    return text.replace("\r", "\\r").replace("\n", "\\n")


# LLM: Only presentation changes; callers still apply the original trust/redaction boundary to returned body text.
# 函数用途: 扩展三类领域摘要，其余工具沿用原编排摘要。
def orchestration_live_summary(result: object, archive_record: dict[str, object]) -> str:
    tool = getattr(result, "tool_name", "")
    if tool in {"run_command", "process_session"}:
        return _process_summary(result, archive_record)
    if tool == "list_agents":
        return _agents_summary(result, archive_record)
    return _original_summary(result, archive_record)


# LLM: 只对进程JSON在首次截断前做有界投影；不改原文、哈希或信任级别，调用方仍须统一脱敏封装。
# 函数用途: 从完整回执生成保留真实流尾部的外置预览，其它工具沿原预览。
def archive_output_preview(tool: str, output: str, record: dict[str, object], handler_details: object) -> str:
    if tool not in {'run_command', 'process_session'}:
        return str(record.get('output_preview') or '')
    result = SimpleNamespace(tool_name=tool, output=output, output_trust='runtime',
                             metadata={'handler_details': handler_details})
    return _process_summary(result, record)


# LLM: Decode only an exact host-generated external frame (including canonical escaped delimiters).
#   This extracts data, not authority; callers must reapply the result's original trust/redaction policy.
# 函数用途: 解析原JSON或宿主外部数据框中的JSON，不能从任意日志寻找状态字段。
def _payload(result: object) -> dict:
    text = str(getattr(result, "output", ""))
    if getattr(result, "output_trust", "") == "external_data":
        frame = project_tool_output_body(tool=getattr(result, "tool_name", ""), output="",
                                         trust="external_data", redaction="default")
        for variant in (frame, frame.replace("untrusted_tool_result", "untrusted-tool-result")):
            prefix, _, suffix = variant.partition("\n\n")
            prefix += "\n\n"
            if text.startswith(prefix) and text.endswith(suffix):
                text = text[len(prefix):-len(suffix)]
                break
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: Bound the final JSON encoding, including escapes and omission marker, so logs cannot crowd out network evidence.
#   Only excerpts are shortened; identities and references never use this function. No file IO or subprocesses.
# 函数用途: 按编码后的长度为日志保留两端，避免控制字符转义挤掉网络未验证标记和证据边界。
def _head_tail(value: object) -> str:
    text = str(value or "")
    if _json_size(text) <= _EXCERPT_CHARS:
        return text
    low, high, selected = 0, min(len(text) // 2, _EXCERPT_CHARS // 2), ""
    while low <= high:
        half = (low + high) // 2
        marker = f"\n...[omitted {len(text) - 2 * half} chars]...\n"
        candidate = text[:half] + marker + (text[-half:] if half else "")
        if _json_size(candidate) <= _EXCERPT_CHARS:
            selected, low = candidate, half + 1
        else:
            high = half - 1
    return selected


# LLM: Count the same compact JSON characters emitted by the domain renderer, not Python repr sizes.
# 函数用途: 按实际摘要编码计算剩余字符预算。
def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


# LLM: Keep scalar conclusions before collections, then dictionary facts before lists. Retained values/rows stay exact.
#   The omission count covers removed dictionary fields or array items; never clip refs or reinterpret partial observations.
# 函数用途: 在有限预算内投影网络和加载诊断，保留结论语义并明确统计省略的字段或数组项。
def _bounded_detail(value: object, limit: int) -> tuple[object, int]:
    if _json_size(value) <= limit:
        return value, 0
    if isinstance(value, list):
        selected = []
        for row in value:
            if _json_size([*selected, row]) > limit:
                break
            selected.append(row)
        return selected, len(value) - len(selected)
    if not isinstance(value, dict):
        return None, 1
    selected, omitted = {}, 0
    # Protect short observation statuses and evidence boundaries from earlier, arbitrarily large collections.
    ordered = sorted(value.items(), key=lambda item: 2 if isinstance(item[1], list) else int(isinstance(item[1], dict)))
    for key, item in ordered:
        available = limit - _json_size(selected) - _json_size(key) - 3
        if available < 4:
            omitted += 1
            continue
        budget = min(available, max(16, limit // 2)) if isinstance(item, (dict, list)) else available
        projected, lost = _bounded_detail(item, budget)
        if projected is None and lost:
            omitted += lost
            continue
        selected[key] = projected
        omitted += lost
    return selected, omitted


# LLM: Canonical metadata outranks body claims; preserve bounded archive previews, counts and omissions on a second projection.
#   Nested network/list shapes retain existing facts; absent process facts are never invented as null.
# 函数用途: 归约真实命令和后台各动作，分别保留流尾部及已有网络观察。
def _process_summary(result: object, record: dict[str, object]) -> str:
    details = getattr(result, "metadata", {}).get("handler_details", {})
    details = details if isinstance(details, dict) else {}
    payload = _payload(result)
    if isinstance(payload.get("processes"), list):
        rows = [_process_projection(row) for row in payload["processes"] if isinstance(row, dict)]
        count = payload.get('count')
        summary = {"count": count if type(count) is int and count >= len(rows) else len(rows), "processes": []}
        for row in rows:
            candidate = {**summary, "processes": [*summary["processes"], row]}
            if _json_size(candidate) > _PROCESS_SUMMARY_CHARS - 1024:
                break
            summary = candidate
        prior_omitted = payload.get('omitted_processes')
        omitted = (prior_omitted if type(prior_omitted) is int and prior_omitted > 0 else 0) + len(rows) - len(summary['processes'])
        if omitted:
            summary['omitted_processes'] = omitted
    else:
        process = payload.get("process")
        data = {**payload, **(process if isinstance(process, dict) else {})}
        metadata = details.get("process")
        data.update(metadata if isinstance(metadata, dict) else {})
        display = details.get("display")
        if isinstance(display, dict):
            data.update({key: display[key] for key in ("stdout", "stderr", "capture_complete") if key in display})
        # A parsed receipt is not a log. In particular, stop receipts may contain private cleanup identities.
        summary = _process_projection(data, fallback="" if payload else getattr(result, "output", ""))
    prior = payload.get('detail_omissions')
    omissions = {key: value for key, value in prior.items() if key in {'network', 'load_errors'}
                 and type(value) is int and value > 0} if isinstance(prior, dict) else {}
    for key in ("network", "load_errors"):
        if key in payload:
            budget = max(0, min(2400, _PROCESS_SUMMARY_CHARS - _json_size(summary) - 512))
            summary[key], omitted = _bounded_detail(payload[key], budget)
            if omitted:
                omissions[key] = omissions.get(key, 0) + omitted
    if omissions:
        summary["detail_omissions"] = omissions
    summary["output_artifact_ref"] = archive_output_ref(record)
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


# LLM: Keep status/timeout distinctions verbatim; preserve output_head_tail from a prior projection as external data.
# 函数用途: 为一个已有进程对象保留确定字段和独立的标准/错误输出头尾。
def _process_projection(data: dict, *, fallback: object = "") -> dict:
    summary = {key: data[key] for key in ("status", "session_id", "timed_out", "wait_timed_out",
               "wait_budget", "capture_complete") if key in data}
    if "exit_code" in data or "return_code" in data:
        summary["exit_code"] = data.get("exit_code", data.get("return_code"))
    streams = {key: _head_tail(data[key]) for key in ("stdout", "stderr") if key in data}
    if not streams:
        streams = {"output_head_tail": _head_tail(data.get("output_tail") or data.get('output_head_tail') or data.get("output")
                                                or fallback)}
    summary.update(streams)
    return summary


# LLM: Only replace the recognizable derived canonical tree preview, never an explicit custom live override.
#   Consume the same already-authorized model payload; do not query the tree or recover host/private paths.
# 函数用途: 为真实list_agents的默认派生预览补上领域摘要，不修改生产者、原结果或显式自定义摘要。
def agents_live_preview(result: object, live_output: str, record: dict[str, object]) -> str:
    payload = _payload(result)
    if (getattr(result, "tool_name", "") == "list_agents"
            and payload.get("schema_version") == "agent_tree_model_status.v1"
            and live_output == agent_tree_model_preview(payload)):
        return _agents_summary(result, record) or live_output
    return live_output


# LLM: Preserve existing identities/refs verbatim; never reconstruct internal final-report filenames or truncate a ref into a fake path.
# 函数用途: 从真实代理节点生成状态列表，超预算时明确报告省略数量。
def _agents_summary(result: object, record: dict[str, object]) -> str:
    payload = _payload(result)
    rows = payload.get("nodes", payload.get("agents"))
    if not isinstance(rows, list):
        return ""
    rows = [row for row in rows if isinstance(row, dict)]
    count = payload.get("count", payload.get("total_nodes", len(rows)))
    summary = {"count": count, "agents": [], "output_artifact_ref": archive_output_ref(record)}
    for row in rows:
        agent = {"id": row.get("id", row.get("run_id", "")), "role": row.get("role", ""),
                 "status": row.get("status", ""), "final_report_ref": row.get("final_report_ref", "")}
        if row.get("read_order"):
            agent["read_order"] = row["read_order"]
        candidate = {**summary, "agents": [*summary["agents"], agent]}
        if len(json.dumps(candidate, ensure_ascii=False)) > _AGENT_SUMMARY_CHARS:
            break
        summary = candidate
    if len(summary["agents"]) < len(rows):
        summary["omitted_agents"] = len(rows) - len(summary["agents"])
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
