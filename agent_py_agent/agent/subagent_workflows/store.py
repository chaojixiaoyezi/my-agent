# LLM: Subagent workflow planner module; keep route, compile, and acceptance bundle shapes stable.
# 模块用途: 拆分子代理工作流的规划、编译、验收或存储逻辑。

from __future__ import annotations

"""Load built-in and user-defined subagent workflow templates."""

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .models import WorkflowLoadIssue, WorkflowPhase, WorkflowTemplate

BUILTIN_PACKAGE = "agent_py_agent.agent.subagent_workflows.builtin"
REQUIRED_TEMPLATE_FIELDS = ("id", "name", "solves", "fit_for", "phases")
REQUIRED_PHASE_FIELDS = ("id", "kind", "task")


# LLM: WorkflowTemplateStore 属于子代理工作流编排的类边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 类用途: 集中保存工作流模板存储字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发模板选择、步骤编译和验收策略相关副作用，需保持公开契约稳定。
@dataclass
class WorkflowTemplateStore:
    """In-memory workflow template catalog with accumulated load issues."""

    templates: dict[str, WorkflowTemplate] = field(default_factory=dict)
    issues: list[WorkflowLoadIssue] = field(default_factory=list)

    # LLM: all 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
    # 函数用途: 处理all相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
    def all(self) -> list[WorkflowTemplate]:
        return [self.templates[key] for key in sorted(self.templates)]

    # LLM: get 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
    # 函数用途: 读取或查询get需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def get(self, template_id: str) -> WorkflowTemplate | None:
        return self.templates.get(template_id)


# LLM: load_template_store 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 读取或查询模板存储需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def load_template_store(user_template_dir: str | Path | None = None) -> WorkflowTemplateStore:
    """Load built-in templates, then overlay valid user templates with matching ids."""

    store = WorkflowTemplateStore()
    _merge_templates(store, _load_builtin_templates())
    if user_template_dir is not None:
        _merge_templates(store, _load_user_templates(Path(user_template_dir)))
    return store


# LLM: load_workflow_templates 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 读取或查询工作流模板需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def load_workflow_templates(
    user_template_dir: str | Path | None = None,
) -> tuple[list[WorkflowTemplate], list[WorkflowLoadIssue]]:
    """Compatibility helper returning sorted templates and issues."""

    store = load_template_store(user_template_dir)
    return store.all(), store.issues


# LLM: validate_template_data 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 校验模板data需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def validate_template_data(
    data: Any,
    *,
    source_path: str = "",
) -> list[WorkflowLoadIssue]:
    """Validate raw template JSON data without constructing dataclasses."""
    if not isinstance(data, dict):
        return [WorkflowLoadIssue(message="workflow template must be an object", source_path=source_path)]
    template_id = _clean_str(data.get("id"))
    issues: list[WorkflowLoadIssue] = _check_required_fields(data, template_id, source_path)
    issues.extend(_validate_phases(data.get("phases"), template_id, source_path))
    return issues


# LLM: _check_required_fields 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 校验required字段需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _check_required_fields(data: dict, template_id: str, source_path: str) -> list[WorkflowLoadIssue]:
    """Check that all required top-level fields are present."""
    issues: list[WorkflowLoadIssue] = []
    for field_name in REQUIRED_TEMPLATE_FIELDS:
        if _is_missing(data.get(field_name)):
            issues.append(WorkflowLoadIssue(
                message=f"missing required field: {field_name}",
                source_path=source_path,
                template_id=template_id,
                field=field_name,
            ))
    return issues


# LLM: _validate_phases 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 校验phases需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _validate_phases(phases: Any, template_id: str, source_path: str) -> list[WorkflowLoadIssue]:
    """Validate phases field and its contents."""
    issues: list[WorkflowLoadIssue] = []
    if _is_missing(phases):
        return issues
    if not isinstance(phases, list):
        issues.append(WorkflowLoadIssue(
            message="phases must be a list",
            source_path=source_path,
            template_id=template_id,
            field="phases",
        ))
        return issues
    for idx, phase in enumerate(phases):
        issues.extend(_validate_single_phase(phase, idx, template_id, source_path))
    return issues


# LLM: _validate_single_phase 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 校验单个phase需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _validate_single_phase(phase: Any, idx: int, template_id: str, source_path: str) -> list[WorkflowLoadIssue]:
    """Validate one phase entry."""
    issues: list[WorkflowLoadIssue] = []
    field_prefix = f"phases[{idx}]"
    if not isinstance(phase, dict):
        issues.append(WorkflowLoadIssue(
            message="phase must be an object",
            source_path=source_path,
            template_id=template_id,
            field=field_prefix,
        ))
        return issues
    for field_name in REQUIRED_PHASE_FIELDS:
        if _is_missing(phase.get(field_name)):
            issues.append(WorkflowLoadIssue(
                message=f"phase missing required field: {field_name}",
                source_path=source_path,
                template_id=template_id,
                field=f"{field_prefix}.{field_name}",
            ))
    return issues


# LLM: _load_builtin_templates 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 读取或查询builtin模板需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _load_builtin_templates() -> WorkflowTemplateStore:
    store = WorkflowTemplateStore()
    try:
        package_files = resources.files(BUILTIN_PACKAGE)
    except ModuleNotFoundError as exc:
        store.issues.append(WorkflowLoadIssue(message=f"cannot load built-in templates: {exc}"))
        return store

    for entry in sorted(package_files.iterdir(), key=lambda item: item.name):
        if Path(entry.name).suffix.lower() != ".json":
            continue
        text = entry.read_text(encoding="utf-8")
        _load_json_text_into_store(store, text, source="builtin", source_path=str(entry))
    return store


# LLM: _load_user_templates 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 读取或查询user模板需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _load_user_templates(user_template_dir: Path) -> WorkflowTemplateStore:
    store = WorkflowTemplateStore()
    if not user_template_dir.exists():
        store.issues.append(
            WorkflowLoadIssue(
                message="user template directory does not exist",
                source_path=str(user_template_dir),
            )
        )
        return store
    if not user_template_dir.is_dir():
        store.issues.append(
            WorkflowLoadIssue(
                message="user template path is not a directory",
                source_path=str(user_template_dir),
            )
        )
        return store

    for path in sorted(user_template_dir.glob("*.json")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            store.issues.append(
                WorkflowLoadIssue(message=f"cannot read workflow template: {exc}", source_path=str(path))
            )
            continue
        _load_json_text_into_store(store, text, source="user", source_path=str(path))
    return store


# LLM: _load_json_text_into_store 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 读取或查询JSON文本into存储需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _load_json_text_into_store(
    store: WorkflowTemplateStore,
    text: str,
    *,
    source: str,
    source_path: str,
) -> None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        store.issues.append(
            WorkflowLoadIssue(message=f"invalid JSON workflow template: {exc}", source_path=source_path)
        )
        return

    for item in _template_items(data, source_path=source_path, issues=store.issues):
        issues = validate_template_data(item, source_path=source_path)
        if issues:
            store.issues.extend(issues)
            continue
        template = _template_from_mapping(item, source=source, source_path=source_path)
        store.templates[template.id] = template


# LLM: _template_items 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理模板条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _template_items(
    data: Any,
    *,
    source_path: str,
    issues: list[WorkflowLoadIssue],
) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("templates"), list):
        raw_items = data["templates"]
    elif isinstance(data, list):
        raw_items = data
    else:
        raw_items = [data]

    items: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_items):
        if isinstance(item, dict):
            items.append(item)
            continue
        issues.append(
            WorkflowLoadIssue(
                message="workflow template must be an object",
                source_path=source_path,
                field=f"templates[{idx}]",
            )
        )
    return items


# LLM: _template_from_mapping 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理来自模板mapping相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _template_from_mapping(
    data: dict[str, Any],
    *,
    source: str,
    source_path: str,
) -> WorkflowTemplate:
    return WorkflowTemplate(
        id=_clean_str(data.get("id")),
        name=_clean_str(data.get("name")),
        solves=_as_list(data.get("solves")),
        fit_for=_as_list(data.get("fit_for")),
        phases=[_phase_from_mapping(item) for item in data.get("phases", [])],
        final_checks=_as_list(data.get("final_checks") or data.get("checks")),
        source=source,
        source_path=source_path,
    )


# LLM: _phase_from_mapping 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理来自phasemapping相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _phase_from_mapping(data: dict[str, Any]) -> WorkflowPhase:
    return WorkflowPhase(
        id=_clean_str(data.get("id")),
        kind=_clean_str(data.get("kind")),
        task=_clean_str(data.get("task")),
        acceptance=_as_list(data.get("acceptance")),
        depends_on=_as_list(data.get("depends_on")),
    )


# LLM: _merge_templates 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 更新模板对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新模板选择、步骤编译和验收策略，需避免破坏既有状态机约定。
def _merge_templates(target: WorkflowTemplateStore, source: WorkflowTemplateStore) -> None:
    target.templates.update(source.templates)
    target.issues.extend(source.issues)


# LLM: _clean_str 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 处理cleanstr相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持模板选择、步骤编译和验收策略上的返回值和副作用边界稳定。
def _clean_str(value: Any) -> str:
    return str(value or "").strip()


# LLM: _is_missing 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 判断missing条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == []


# LLM: _as_list 属于子代理工作流编排的函数边界；调整时先确认模板选择、步骤编译和验收策略仍按原契约工作。
# 函数用途: 转换aslist的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    cleaned = str(value).strip()
    return [cleaned] if cleaned else []
