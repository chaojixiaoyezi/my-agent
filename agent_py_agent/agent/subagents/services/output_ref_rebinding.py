# LLM: Output ref rebinding keeps newly-created subagents from inheriting stale sibling run ids.
# 模块用途: 子代理 run_id 生成后，把结构化 output_refs/output_files 里的 data/subagents/<旧id>/ 绑定到真实 run_id。

from __future__ import annotations

from dataclasses import dataclass

from ..models import SubAgentTask


# LLM: OutputRefRebinding records one machine-auditable replacement for later E2E diagnosis.
# 类用途: 保存字段、旧路径、新路径，说明某个自写产物路径被重新绑定到真实子代理 run_id。
@dataclass(frozen=True)
class OutputRefRebinding:
    field: str
    from_ref: str
    to_ref: str

    # LLM: to_dict keeps persisted task.attributes JSON-friendly.
    # 函数用途: 转成稳定字典，避免 dataclass 对象进入 task.json。
    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "from": self.from_ref, "to": self.to_ref}


# LLM: rebind_task_output_refs_to_run mutates only structured output attributes, never prose.
# 函数用途: 创建 run 后修正 attributes 里的 output_refs/output_files/artifact_refs 自写路径。
def rebind_task_output_refs_to_run(task: SubAgentTask) -> list[OutputRefRebinding]:
    rewrites: list[OutputRefRebinding] = []
    task.attributes = _rewrite_attribute_output_refs(task.attributes, task.id, rewrites)
    if rewrites:
        _store_rebindings(task, rewrites)
    return rewrites


# LLM: _rewrite_attribute_output_refs walks only known structured output ref fields.
# 函数用途: 替换 task.attributes.output_files/output_refs/artifact_refs 中猜错的 subagent run id。
def _rewrite_attribute_output_refs(
    attributes: dict[str, object],
    run_id: str,
    rewrites: list[OutputRefRebinding],
) -> dict[str, object]:
    attrs = dict(attributes or {})
    for field in ("output_refs", "output_files", "artifact_refs"):
        if field in attrs:
            attrs[field] = _rewrite_attribute_value(field, attrs.get(field), run_id, rewrites)
    return attrs


# LLM: _rewrite_attribute_value preserves the caller's shallow value shape.
# 函数用途: 支持字符串和列表形式的 output ref 字段，非字符串值保持原样。
def _rewrite_attribute_value(
    field: str,
    value: object,
    run_id: str,
    rewrites: list[OutputRefRebinding],
) -> object:
    if isinstance(value, list):
        return [_rewrite_attribute_value(field, item, run_id, rewrites) for item in value]
    if not isinstance(value, str):
        return value
    rebound = _rebound_subagent_ref(value, run_id)
    if rebound and rebound != value:
        rewrites.append(OutputRefRebinding(field, value, rebound))
        return rebound
    return value


# LLM: _rebound_subagent_ref swaps the segment after data/subagents when it is a concrete run id.
# 函数用途: `data/subagents/subagent-old/a.md` -> `data/subagents/<current-run>/a.md`。
def _rebound_subagent_ref(ref: str, run_id: str) -> str:
    parts = str(ref or "").split("/")
    for index, part in enumerate(parts[:-1]):
        if part != "subagents":
            continue
        next_index = index + 1
        if next_index >= len(parts) or not parts[next_index].startswith("subagent-"):
            continue
        if parts[next_index] == run_id:
            return ""
        parts[next_index] = run_id
        return "/".join(parts)
    return ""


# LLM: _store_rebindings appends audit data without overwriting unrelated attributes.
# 函数用途: 把路径修正记录写进 task.attributes.output_ref_rebindings，便于后续真实 E2E 复盘。
def _store_rebindings(task: SubAgentTask, rewrites: list[OutputRefRebinding]) -> None:
    attrs = dict(getattr(task, "attributes", {}) or {})
    existing = attrs.get("output_ref_rebindings")
    records = list(existing) if isinstance(existing, list) else []
    records.extend(item.to_dict() for item in rewrites)
    attrs["output_ref_rebindings"] = records
    task.attributes = attrs
