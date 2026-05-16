# LLM: Output ref rebinding keeps newly-created subagents from inheriting stale sibling run ids.
# 模块用途: 子代理 run_id 生成后，把“当前任务自己要写”的 data/subagents/<旧id>/ 文件绑定到真实 run_id。

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import SubAgentTask

_FILE_REF_RE = re.compile(
    r"(?<![\w.-])/?(?:[\w.-]+/)*[\w.-]+\."
    r"(?:json|md|csv|txt|xlsx|xls|pdf|html|htm|py|yaml|yml)\b",
    re.IGNORECASE,
)
_DIRECT_WRITE_MARKERS = (
    "写到",
    "写入",
    "保存到",
    "保存为",
    "生成",
    "创建",
    "输出到",
    "输出为",
    "产出到",
    "导出",
    "write",
    "output to",
    "create",
    "generate",
    "save",
    "export",
)
_READ_MARKERS = ("读取", "读", "接收", "基于", "根据", "依赖", "输入", "引用", "参考", "read", "from", "input")


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


# LLM: rebind_task_output_refs_to_run mutates only current-task output refs, never input refs.
# 函数用途: 创建 run 后修正 goal/thought/plan/acceptance_checks 里的自写 data/subagents/<旧id>/ 路径。
def rebind_task_output_refs_to_run(task: SubAgentTask) -> list[OutputRefRebinding]:
    rewrites: list[OutputRefRebinding] = []
    task.goal = _rewrite_field("goal", task.goal, task.id, rewrites)
    task.thought = _rewrite_field("thought", task.thought, task.id, rewrites)
    task.plan = _rewrite_list_field("plan", task.plan, task.id, rewrites)
    task.acceptance_checks = _rewrite_list_field("acceptance_checks", task.acceptance_checks, task.id, rewrites)
    if rewrites:
        _store_rebindings(task, rewrites)
    return rewrites


# LLM: _rewrite_list_field preserves list order while rebinding each text item independently.
# 函数用途: 处理 plan 和 acceptance_checks 这类字符串列表字段。
def _rewrite_list_field(
    field: str,
    values: list[str],
    run_id: str,
    rewrites: list[OutputRefRebinding],
) -> list[str]:
    return [_rewrite_field(field, str(item), run_id, rewrites) for item in values]


# LLM: _rewrite_field replaces only refs whose local grammar says "this task writes here".
# 函数用途: 遍历文本中的文件路径；只有写入语义且路径含旧 subagent run id 时才替换。
def _rewrite_field(field: str, text: str, run_id: str, rewrites: list[OutputRefRebinding]) -> str:
    source = str(text or "")
    result = source
    for ref, start in _file_refs(source):
        if not _path_ref_is_output(source, start):
            continue
        rebound = _rebound_subagent_ref(ref, run_id)
        if rebound and rebound != ref:
            result = result.replace(ref, rebound)
            rewrites.append(OutputRefRebinding(field, ref, rebound))
    return result


# LLM: _file_refs returns lightweight path candidates and their offsets in current text.
# 函数用途: 找出可能的文件路径，供写入语义和 run_id 重绑定继续判断。
def _file_refs(text: str):
    for match in _FILE_REF_RE.finditer(str(text or "")):
        yield match.group(), match.start()


# LLM: _path_ref_is_output intentionally mirrors runner dependency parsing without importing agent_core.
# 函数用途: 判断路径前缀是否表达当前任务写出产物，避免把“读取旧 run 文件”误改掉。
def _path_ref_is_output(text: str, start: int) -> bool:
    prefix = str(text[max(0, start - 96):start]).lower()
    direct = prefix[-24:]
    if any(marker in direct for marker in _DIRECT_WRITE_MARKERS):
        return True
    return ("输出" in direct or "output" in direct) and not any(marker in direct for marker in _READ_MARKERS)


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
