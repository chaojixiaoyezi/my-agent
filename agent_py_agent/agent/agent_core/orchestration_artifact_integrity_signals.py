# LLM: Artifact integrity signal extraction reads small refs without opening deliverable bodies.
# 模块用途: 从 task、dispatch record 和 output.json 中提取产物结构失败线索，供修复建议层复用。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FAILURE_TYPE = "artifact_integrity_failed"


# LLM: ArtifactIntegritySignalInput is the internal bundle for stable signal dictionaries.
# 类用途: 收拢 run refs、blocker、artifact 和授权根，避免信号构造函数散参数膨胀。
@dataclass(frozen=True)
class ArtifactIntegritySignalInput:
    run_id: str
    task_ref: str
    output_ref: str
    run_ref: str
    blockers: list[str]
    artifact_refs: list[str]
    roots: list[str]


# LLM: artifact_integrity_blocked identifies the explicit lifecycle lane for broken artifacts.
# 函数用途: 判断 task/output 是否是产物结构检查失败，而不是泛化成普通 BLOCKED 接管。
def artifact_integrity_blocked(item: Any) -> bool:
    if str(getattr(item, "failure_type", "") or "").lower() == _FAILURE_TYPE:
        return True
    if any(_FAILURE_TYPE in text for text in _item_blockers(item)):
        return True
    payload = _read_json_object(_output_ref_for_item(item))
    return _payload_has_artifact_integrity_failure(payload)


# LLM: artifact_integrity_signals_from_tasks extracts repair signals from direct child tasks.
# 函数用途: 给 runner-context progress payload 使用，只读取 output/run 小 JSON 和 task 字段。
def artifact_integrity_signals_from_tasks(children: list[Any]) -> list[dict[str, object]]:
    return [signal for item in children if (signal := _signal_from_task(item))]


# LLM: artifact_integrity_signals_from_records extracts repair signals from dispatch records.
# 函数用途: 顶层 root 只有 classify_blocker 记录时，回推 task_dir 并读取 output/run refs。
def artifact_integrity_signals_from_records(records: list[Any]) -> list[dict[str, object]]:
    return [signal for item in records if (signal := _signal_from_record(item))]


# LLM: _signal_from_task reads refs and blockers from an in-memory SubAgentTask-like object.
# 函数用途: 生成直接 child 修复信号，只读取小 JSON 和路径字段，不打开业务产物正文。
def _signal_from_task(item: Any) -> dict[str, object]:
    if not artifact_integrity_blocked(item):
        return {}
    task_dir = _task_dir_for_item(item)
    output_ref = _output_ref_for_item(item)
    run_ref = _run_ref_for_task_dir(task_dir)
    payload = _read_json_object(output_ref)
    blockers = _artifact_blockers(item, payload)
    return _signal(ArtifactIntegritySignalInput(
        run_id=str(getattr(item, "id", "") or ""),
        task_ref=str(task_dir) if task_dir else "",
        output_ref=str(output_ref) if output_ref else "",
        run_ref=str(run_ref) if run_ref else "",
        blockers=blockers,
        artifact_refs=_artifact_refs(payload, blockers),
        roots=_product_roots(item, run_ref),
    ))


# LLM: _signal_from_record reconstructs a run directory from dispatch evidence refs.
# 函数用途: dispatch record 只有 WORK_LOG 或 task dir 时，回推 output/run refs 并形成修复信号。
def _signal_from_record(item: Any) -> dict[str, object]:
    if not _record_can_hold_artifact_blocker(item):
        return {}
    task_dir = _task_dir_from_record(item)
    if task_dir is None:
        return {}
    output_ref = task_dir / "output.json"
    payload = _read_json_object(output_ref)
    if not _payload_has_artifact_integrity_failure(payload):
        return {}
    run_ref = task_dir / "run.json"
    blockers = _artifact_blockers(item, payload)
    return _signal(ArtifactIntegritySignalInput(
        run_id=str(getattr(item, "run_id", "") or ""),
        task_ref=str(task_dir),
        output_ref=str(output_ref),
        run_ref=str(run_ref) if run_ref.exists() else "",
        blockers=blockers,
        artifact_refs=_artifact_refs(payload, blockers),
        roots=_product_roots(item, run_ref),
    ))


# LLM: _signal keeps signal dictionaries stable for tests, summaries, and future UIs.
# 函数用途: 组装一个失败 child 的小型机器线索，过滤空值和重复路径。
def _signal(data: ArtifactIntegritySignalInput) -> dict[str, object]:
    return {
        "run_id": data.run_id,
        "task_ref": data.task_ref,
        "output_ref": data.output_ref,
        "run_ref": data.run_ref,
        "blockers": _unique_text(data.blockers),
        "artifact_refs": _unique_text(data.artifact_refs),
        "allowed_write_roots": _unique_text(data.roots),
    }


# LLM: _record_can_hold_artifact_blocker limits record scanning to blocker audit rows.
# 函数用途: 避免每条 dispatch record 都回读磁盘，只处理 BLOCKED/classify 这类候选。
def _record_can_hold_artifact_blocker(item: Any) -> bool:
    if str(getattr(item, "action", "") or "") != "classify_blocker":
        return False
    status = str(getattr(item, "after_status", "") or getattr(item, "before_status", "") or "").upper()
    return status == "BLOCKED"


# LLM: _task_dir_from_record resolves WORK_LOG.md/output.json/task-dir evidence into a task directory.
# 函数用途: 兼容 dispatch evidence_paths 只给 WORK_LOG.md 的真实输出形态。
def _task_dir_from_record(item: Any) -> Path | None:
    for raw in getattr(item, "evidence_paths", []) or []:
        path = Path(str(raw or ""))
        if path.name in {"WORK_LOG.md", "output.json", "run.json"}:
            return path.parent
        if (path / "output.json").exists() or (path / "run.json").exists():
            return path
    return None


# LLM: _artifact_blockers combines task blockers and output payload failure fields.
# 函数用途: 从多个小字段收集 artifact_integrity_failed 线索，不读取产物正文。
def _artifact_blockers(item: Any, payload: dict[str, Any]) -> list[str]:
    values = [*_item_blockers(item), *_payload_blocker_texts(payload)]
    return _unique_text([text for text in values if _FAILURE_TYPE in text or "missing_" in text])


# LLM: _payload_blocker_texts extracts artifact failure text from structured output and tests.
# 函数用途: 只读取 output.json 小字段，避免将产物正文拉入父级上下文。
def _payload_blocker_texts(payload: dict[str, Any]) -> list[str]:
    structured = payload.get("structured_output") if isinstance(payload.get("structured_output"), dict) else {}
    values = [str(structured.get(key) or "") for key in ("blocked_reason", "summary")]
    values.extend(str(payload.get(key) or "") for key in ("blocked_reason", "summary"))
    values.extend(_test_summary_texts(payload.get("tests") or []))
    return values


# LLM: _test_summary_texts keeps artifact_integrity test summaries separate from generic tests.
# 函数用途: 提取 validation_method=artifact_integrity 的失败摘要，供修复目标引用。
def _test_summary_texts(tests: list[Any]) -> list[str]:
    summaries: list[str] = []
    for test in tests:
        if isinstance(test, dict) and str(test.get("validation_method") or "") == "artifact_integrity":
            summaries.append(str(test.get("summary") or ""))
    return summaries


# LLM: _artifact_refs extracts artifact refs from output.json plus blocker paths.
# 函数用途: 让 repair worker 直接知道要修哪个文件，而不是从长摘要里猜。
def _artifact_refs(payload: dict[str, Any], blockers: list[str]) -> list[str]:
    refs = [
        *_artifact_item_refs(payload.get("artifacts") or []),
        *_packet_artifact_refs(payload.get("evidence_packets") or []),
        *[_path_from_blocker(text) for text in blockers],
    ]
    return _unique_text(refs)


# LLM: _artifact_item_refs normalizes artifacts list entries into path strings.
# 函数用途: 支持 dict 和纯字符串两类 artifact ref 形态。
def _artifact_item_refs(items: list[Any]) -> list[str]:
    refs: list[str] = []
    for item in items:
        refs.append(_artifact_item_ref(item))
    return refs


# LLM: _artifact_item_ref converts one artifact entry into a path-like ref.
# 函数用途: 拆出单项解析，降低 artifact refs 收集的嵌套复杂度。
def _artifact_item_ref(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("path") or item.get("file_path") or item.get("artifact_path") or item.get("ref") or "")
    return str(item) if item else ""


# LLM: _packet_artifact_refs reads evidence packet artifact_refs without expanding packets.
# 函数用途: 兼容子代理通过 evidence_packets 报告产物路径的输出格式。
def _packet_artifact_refs(packets: list[Any]) -> list[str]:
    refs: list[str] = []
    for packet in packets:
        if isinstance(packet, dict):
            refs.extend(str(ref) for ref in packet.get("artifact_refs") or [])
    return refs


# LLM: _path_from_blocker parses artifact_integrity_failed:<path>:<codes> blockers safely enough for refs.
# 函数用途: 从 blocker 字符串提取本地文件路径；无法解析时返回空，由 output artifacts 兜底。
def _path_from_blocker(text: str) -> str:
    if _FAILURE_TYPE not in text:
        return ""
    tail = text.split(f"{_FAILURE_TYPE}:", 1)[-1]
    return tail.rsplit(":", 1)[0] if ":" in tail else tail


# LLM: _payload_has_artifact_integrity_failure recognizes persisted output.json failure shapes.
# 函数用途: 支持 failure_type、blockers、structured_output、tests 四种来源。
def _payload_has_artifact_integrity_failure(payload: dict[str, Any]) -> bool:
    if str(payload.get("failure_type") or "").lower() == _FAILURE_TYPE:
        return True
    text = json.dumps(_artifact_failure_probe(payload), ensure_ascii=False).lower()
    return _FAILURE_TYPE in text


# LLM: _artifact_failure_probe narrows failure scans to small machine fields.
# 函数用途: 避免整份 output 参与关键词扫描，降低误判和 prompt 风险。
def _artifact_failure_probe(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": payload.get("status", ""),
        "blockers": payload.get("blockers") or [],
        "structured_output": payload.get("structured_output") or {},
        "tests": payload.get("tests") or [],
    }


# LLM: _product_roots merges task and run.json write roots while excluding the internal task dir.
# 函数用途: 修复 child 只能拿到真实产物根，避免把整个 runtime 子代理目录当成业务根。
def _product_roots(item: Any, run_ref: Path | str | None) -> list[str]:
    task_dir = str(_task_dir_for_item(item) or "")
    roots = [str(root) for root in getattr(item, "allowed_write_roots", []) or []]
    payload = _read_json_object(Path(str(run_ref))) if run_ref else {}
    task_dir = task_dir or str(payload.get("task_dir") or "")
    roots.extend(str(root) for root in payload.get("allowed_write_roots") or [])
    return _unique_text([root for root in roots if root and root != task_dir])


# LLM: _task_dir_for_item finds the canonical per-run workspace for task-like objects.
# 函数用途: 从 task_dir/output_json 推导任务目录，供 output/run refs 和 root 过滤使用。
def _task_dir_for_item(item: Any) -> Path | None:
    raw = str(getattr(item, "task_dir", "") or "")
    if raw:
        return Path(raw)
    output_ref = _output_ref_for_item(item)
    return output_ref.parent if output_ref else None


# LLM: _output_ref_for_item returns the persisted output.json location when available.
# 函数用途: 支持 task.output_json 和 task_dir/output.json 两种已存在布局。
def _output_ref_for_item(item: Any) -> Path | None:
    explicit = str(getattr(item, "output_json", "") or "")
    if explicit:
        return Path(explicit)
    task_dir = str(getattr(item, "task_dir", "") or "")
    return Path(task_dir) / "output.json" if task_dir else None


# LLM: _run_ref_for_task_dir returns run.json without assuming it exists.
# 函数用途: 统一 task_dir 到 run.json 的推导，读取前由 helper 自己容错。
def _run_ref_for_task_dir(task_dir: Path | None) -> Path | None:
    return task_dir / "run.json" if task_dir else None


# LLM: _read_json_object keeps malformed machine refs advisory instead of fatal.
# 函数用途: 安全读取 output/run 小 JSON；缺失或损坏返回空对象。
def _read_json_object(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _item_blockers normalizes task blocker lists and single-string fallback fields.
# 函数用途: 收集 task.blockers/message/result 里的短 blocker 文本。
def _item_blockers(item: Any) -> list[str]:
    values = [str(value) for value in getattr(item, "blockers", []) or []]
    values.extend(str(getattr(item, field, "") or "") for field in ("message", "result"))
    return _unique_text(values)


# LLM: _unique_text filters empty strings and preserves first-seen order.
# 函数用途: 稳定模型可见列表，避免重复路径放大 prompt。
def _unique_text(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in unique:
            unique.append(text)
    return unique


__all__ = [
    "artifact_integrity_blocked",
    "artifact_integrity_signals_from_records",
    "artifact_integrity_signals_from_tasks",
]
