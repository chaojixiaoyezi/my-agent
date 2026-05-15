# LLM: Dispatch ref helpers keep model-facing deliverable refs separate from tool execution.
# 模块用途: 汇总本轮 dispatch 触碰的 artifact/evidence refs，避免主工具文件继续变胖。

from __future__ import annotations


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


# LLM: _related_run_ids includes explicit runner targets and nested child ids created during dispatch.
# 函数用途: 顶层 refs 汇总要看到 runner 内创建的孙代理，不能只看父 run。
def _related_run_ids(report: object) -> list[str]:
    ids: list[str] = []
    for record in getattr(report, "records", []) or []:
        _extend_unique_refs(ids, set(ids), _record_related_run_ids(record), 100)
    return ids


# LLM: _record_related_run_ids flattens one dispatch record into candidate run refs.
# 函数用途: 从一条 dispatch record 中取当前 run 和 runner 新建 child id，供顶层 refs 汇总去重。
def _record_related_run_ids(record: object) -> list[str]:
    ids = [str(getattr(record, "run_id", "") or "").strip()]
    ids.extend(str(item or "").strip() for item in getattr(record, "runner_created_child_ids", []) or [])
    return [item for item in ids if item]


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
