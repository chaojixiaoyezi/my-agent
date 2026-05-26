# LLM: QA ready refs give tester agents concrete worker outputs without reading bodies.
# 模块用途: 扫描父任务子树里的可验收实现节点，输出 refs-only 的 QA 输入清单。

from __future__ import annotations

from pathlib import Path
from typing import Any

_READY_SCAN_MAX_NODES = 64


# LLM: ready_implementation_refs is shared by schedule and dispatch quality advice.
# 函数用途: 返回 worker/writer/leaf/coder 已可验收节点的 run/status/artifact refs，供 QA 波次使用。
def ready_implementation_refs(manager: Any, parent: object, *, max_nodes: int = _READY_SCAN_MAX_NODES) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    queue = [str(item) for item in _list_attr(parent, "child_ids") if str(item or "").strip()]
    seen: set[str] = set()
    scanned = 0
    while queue and scanned < max_nodes:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        scanned += 1
        child = _load_child(manager, run_id)
        if child is None:
            continue
        if _is_ready_implementation_child(child):
            refs.append(_ready_ref_payload(child))
        queue.extend(child_id for child_id in _list_attr(child, "child_ids") if child_id not in seen)
    return refs


# LLM: has_ready_implementation_child keeps the old boolean phase gate backed by the same refs source.
# 函数用途: 判断父任务是否已有可进入 QA 的实现节点。
def has_ready_implementation_child(manager: Any, parent: object) -> bool:
    return bool(ready_implementation_refs(manager, parent, max_nodes=_READY_SCAN_MAX_NODES))


# LLM: flatten_ready_ref_values gathers artifact/output refs without expanding referenced file bodies.
# 函数用途: 从 ready_work_refs 中抽取指定 ref 列表并去重，供 QA 子任务作为输入合同。
def flatten_ready_ref_values(ready_refs: list[dict[str, object]], key: str, *, max_refs: int = 20) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for text in _iter_ready_ref_values(ready_refs, key):
        if text in seen:
            continue
        seen.add(text)
        refs.append(text)
    return refs[:max_refs]


# LLM: _ready_ref_payload keeps QA inputs bounded and machine-readable.
# 函数用途: 把实现节点转成小型 refs 包，不读取 HTML、代码或报告正文。
def _ready_ref_payload(child: object) -> dict[str, object]:
    return {
        "run_id": _text_attr(child, "id"),
        "role": _text_attr(child, "role"),
        "agent_name": _text_attr(child, "agent_name"),
        "status": _text_attr(child, "status"),
        "verification_status": _text_attr(child, "verification_status"),
        "artifact_refs": _existing_refs(_list_attr(child, "artifact_refs")),
        "output_refs": _existing_refs(
            [
                _text_attr(child, "output_json"),
                _text_attr(child, "final_report_md"),
                _text_attr(child, "runner_result_json"),
            ]
        ),
    }


# LLM: _is_ready_implementation_child classifies implementation roles and completion states.
# 函数用途: worker/writer/coder/leaf 至少等待收口或已验证完成，才暴露给 QA。
def _is_ready_implementation_child(child: object) -> bool:
    identity = f"{_text_attr(child, 'role')} {_text_attr(child, 'agent_name')}".lower().replace("-", "_")
    if not any(token in identity for token in {"worker", "writer", "coder", "leaf"}):
        return False
    status = _text_attr(child, "status").upper()
    verification = _text_attr(child, "verification_status").upper()
    return status in {"DONE"} or verification in {"VERIFIED"}


# LLM: _load_child isolates manager compatibility differences and stale refs.
# 函数用途: 读取一个 child run；失败时返回 None，保持 QA 建议保守。
def _load_child(manager: Any, run_id: str) -> object | None:
    try:
        return manager.load(run_id)
    except (FileNotFoundError, OSError, ValueError, TypeError, AttributeError):
        return None


# LLM: _existing_refs keeps refs-only payloads small and avoids stale path noise where possible.
# 函数用途: 保留存在路径或非路径引用，去重并过滤空值。
def _existing_refs(values: list[object]) -> list[str]:
    refs: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in refs:
            continue
        if _looks_like_path(text) and not Path(text).exists():
            continue
        refs.append(text)
    return refs[:12]


# LLM: _iter_ready_ref_values streams scalar refs from a selected ready refs field.
# 函数用途: 只展开一层 ref 列表，过滤空值，不做路径读取。
def _iter_ready_ref_values(ready_refs: list[dict[str, object]], key: str):
    for item in ready_refs:
        values = item.get(key)
        if isinstance(values, list):
            yield from (str(ref or "").strip() for ref in values if str(ref or "").strip())


# LLM: _looks_like_path avoids dropping future non-path artifact identifiers.
# 函数用途: 粗略判断字符串是否像本地路径；非路径 ref 不做存在性过滤。
def _looks_like_path(text: str) -> bool:
    return text.startswith(("/", "~", ".")) or "/" in text or "\\" in text


# LLM: _list_attr normalizes optional list fields from real tasks and test namespaces.
# 函数用途: 安全读取 list 字段，字符串和缺失值都按空列表处理。
def _list_attr(item: object, name: str) -> list[object]:
    value = getattr(item, name, [])
    return value if isinstance(value, list) else []


# LLM: _text_attr reads scalar task fields without leaking MagicMock placeholders into JSON.
# 函数用途: 安全读取字符串字段，非基础标量返回空字符串。
def _text_attr(item: object, name: str) -> str:
    value = getattr(item, name, "")
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return ""


__all__ = ["flatten_ready_ref_values", "has_ready_implementation_child", "ready_implementation_refs"]
