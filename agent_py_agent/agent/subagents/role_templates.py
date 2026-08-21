"""Role template loading, snapshots, and runner capability decisions."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.value_parsing import StringListOptions, string_list

_ROLE_TEMPLATE_LIST_OPTIONS = StringListOptions(split_commas=True)
ROLE_TEMPLATE_ATTRIBUTE_KEY = "role_template"
WEB_TOOLS = ["web_search", "web_fetch"]
READ_ONLY_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
WORKER_READ_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
ARTIFACT_BUILDER_TOOLS: list[str] = []
WORKER_WRITE_TOOLS = ["write_file", "apply_patch"]
REPORT_WRITE_TOOLS = ["write_file", "apply_patch"]
SHELL_TOOL = "run_command"
CAPABILITY_REQUEST_TOOL = "capability_request"
MAIN_EVENT_TOOLS = ["raise_event"]
COLLABORATION_TOOLS: list[str] = []
RETIRED_MODEL_SUBAGENT_CONTROL_TOOLS = frozenset(
    {"inspect_agent_tree", "dispatch_subagents", "schedule_child_subagents"}
)
# LLM: Role snapshots expose the same recursive edge-local control at every
# depth. Do not add polling, manual dispatch, or ancestor-wide controls here.
# 配置用途: worker 保留通用执行能力；coordinator 额外获得创建、直属插话、取消和权限裁决。
ROLE_BASE_TOOLS = [
    *READ_ONLY_TOOLS,
    *REPORT_WRITE_TOOLS,
    SHELL_TOOL,
    *MAIN_EVENT_TOOLS,
    *COLLABORATION_TOOLS,
    CAPABILITY_REQUEST_TOOL,
]
COORDINATOR_TOOLS = [
    "create_subagents",
    "send_guidance",
    "cancel_subagents",
    "resolve_capability_requests",
    *ROLE_BASE_TOOLS,
]


# LLM: Every model-facing tool snapshot, including persisted legacy grants,
# must pass this single retirement filter before prompt or execution exposure.
# 函数用途: 从工具名列表中剔除已删除的子代理巡检和手动推动入口，并保持原顺序去重。
def active_model_subagent_tools(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [
        tool
        for tool in dict.fromkeys(
            str(item or "").strip()
            for item in value
            if str(item or "").strip()
        )
        if tool not in RETIRED_MODEL_SUBAGENT_CONTROL_TOOLS
    ]


@dataclass(frozen=True)
class RoleTemplate:
    id: str
    name: str
    name_zh: str
    summary: str
    summary_zh: str
    scope: str
    handles_multiple_targets: bool
    use_when_zh: list[str]
    do_not_use_when_zh: list[str] = field(default_factory=list)
    default_tools: list[str] = field(default_factory=list)
    can_write: bool = False
    can_accept: bool = False
    can_run_tests: bool = False
    can_spawn_children: bool = False
    depends_on_outputs: bool = False
    output_contract: dict[str, Any] = field(default_factory=dict)
    output_contract_zh: str = ""
    prompt_zh: str = ""
    source: str = "builtin"
    source_path: str = ""


@dataclass(frozen=True)
class RoleTemplateIssue:
    template_id: str
    source_path: str
    message: str


@dataclass(frozen=True)
class RoleTemplateRegistration:
    source: str
    source_path: str


@dataclass(frozen=True)
class RoleTemplateStore:
    templates: dict[str, RoleTemplate]
    issues: list[RoleTemplateIssue] = field(default_factory=list)

    def get(self, template_id: str) -> RoleTemplate | None:
        return self.templates.get(_clean_id(template_id))

    def all(self) -> list[RoleTemplate]:
        return [self.templates[key] for key in sorted(self.templates)]


def load_role_template_store(
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
) -> RoleTemplateStore:
    templates: dict[str, RoleTemplate] = {}
    issues: list[RoleTemplateIssue] = []
    for path in _builtin_template_paths():
        _load_template_file(path, templates, issues, source="builtin")
    for directory in _iter_user_template_dirs(user_template_dir):
        _load_user_templates(directory, templates, issues)
    return RoleTemplateStore(templates=templates, issues=issues)


def template_for_role(
    role: str,
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
) -> RoleTemplate | None:
    return load_role_template_store(user_template_dir=user_template_dir).get(role)


def template_for_role_identity(
    role: str,
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
) -> RoleTemplate | None:
    template_id = role_template_id_for_role(role, user_template_dir, default_id="")
    return template_for_role(template_id, user_template_dir) if template_id else None


def role_template_snapshot(template: RoleTemplate | None) -> dict[str, object]:
    if template is None:
        return {}
    return {
        "id": template.id,
        "source": template.source,
        "source_path": template.source_path,
        "can_write": template.can_write,
        "can_accept": template.can_accept,
        "can_run_tests": template.can_run_tests,
        "can_spawn_children": template.can_spawn_children,
        "depends_on_outputs": template.depends_on_outputs,
        "output_contract": dict(template.output_contract or {}),
    }


def role_template_snapshot_for_role(
    role: str,
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
) -> dict[str, object]:
    return role_template_snapshot(template_for_role_identity(role, user_template_dir))


def role_template_snapshot_for_task(task: object) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    if isinstance(attrs, dict):
        snapshot = attrs.get(ROLE_TEMPLATE_ATTRIBUTE_KEY)
        if isinstance(snapshot, dict) and snapshot.get("id"):
            return dict(snapshot)
    return role_template_snapshot_for_role(str(getattr(task, "role", "") or ""))


def is_self_authorized_root_task(task: object) -> bool:
    parent_id = str(getattr(task, "parent_id", "") or "").strip()
    if parent_id:
        return False
    attrs = getattr(task, "attributes", {}) or {}
    if isinstance(attrs, dict) and bool(attrs.get("self_authorized_root")):
        return True
    return bool(role_template_snapshot_for_task(task).get("can_spawn_children"))


def role_template_id_for_role(
    role: str,
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
    *,
    default_id: str | None = None,
) -> str:
    store = load_role_template_store(user_template_dir=user_template_dir)
    return resolve_role_template_id(store, role, default_id=default_id)


def resolve_role_template_id(store: Any, role: str, *, default_id: str | None = None) -> str:
    cleaned = _clean_id(role)
    if not cleaned:
        return _valid_template_default(store, default_id)
    exact = store.get(cleaned)
    if exact is not None:
        return exact.id
    return _valid_template_default(store, default_id)


def _valid_template_default(store: Any, default_id: str | None) -> str:
    cleaned = _clean_id(default_id)
    if cleaned and store.get(cleaned):
        return cleaned
    return ""


def role_template_index_text(
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
    *,
    limit: int = 12,
    include_source_path: bool = False,
) -> str:
    store = load_role_template_store(user_template_dir=user_template_dir)
    lines = [_index_line(item, include_source_path=include_source_path) for item in store.all()[:limit]]
    return "\n".join(lines)


def role_template_detail_text(
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
    *,
    roles: list[str] | tuple[str, ...] | None = None,
    limit: int = 12,
) -> str:
    store = load_role_template_store(user_template_dir=user_template_dir)
    templates = _selected_templates(store, roles=roles, limit=limit)
    return "\n".join(_detail_lines(item) for item in templates)


def role_template_guide_text(
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
    *,
    limit: int = 12,
) -> str:
    return role_template_index_text(user_template_dir=user_template_dir, limit=limit)


def _selected_templates(
    store: RoleTemplateStore,
    *,
    roles: list[str] | tuple[str, ...] | None,
    limit: int,
) -> list[RoleTemplate]:
    if not roles:
        return store.all()[:limit]
    selected: list[RoleTemplate] = []
    for role in roles:
        template_id = resolve_role_template_id(store, role)
        template = store.get(template_id)
        if template is not None:
            selected.append(template)
    return selected[:limit]


def _capability_tags(item: RoleTemplate) -> str:
    tags = []
    if item.can_spawn_children:
        tags.append("spawn_children")
    if item.can_write:
        tags.append("write")
    if item.can_run_tests:
        tags.append("test")
    if item.depends_on_outputs:
        tags.append("depends_on_outputs")
    if item.can_accept:
        tags.append("accept")
    return ",".join(tags) or "read_only"


def _index_line(item: RoleTemplate, *, include_source_path: bool) -> str:
    line = (
        f"- {item.id}: {item.name_zh}；{item.summary_zh}；"
        f"适用={_compact_role_rules(item.use_when_zh)}；"
        f"不适用={_compact_role_rules(item.do_not_use_when_zh, max_chars=48)}；"
        f"能力={_capability_tags(item)}"
    )
    if include_source_path:
        line += f"；模板位置={item.source_path}"
    return line


def _compact_role_rules(items: list[str], *, max_chars: int = 80) -> str:
    text = "；".join(item.strip() for item in items if item.strip()) or "未设置"
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "..."


def _detail_lines(item: RoleTemplate) -> str:
    return "\n".join([
        f"- {item.id}: {item.name_zh}",
        f"  用途: {item.summary_zh}",
        f"  适用: {'；'.join(item.use_when_zh) or '未设置'}",
        f"  不适用: {'；'.join(item.do_not_use_when_zh) or '未设置'}",
        f"  默认工具: {', '.join(item.default_tools) or 'none'}",
        f"  输出合同: {item.output_contract_zh or '未设置'}",
        f"  系统提示片段: {item.prompt_zh or '未设置'}",
    ])


def _iter_user_template_dirs(value: str | Path | Iterable[str | Path] | None) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [Path(value)]
    return [Path(item) for item in value if str(item).strip()]


def _load_user_templates(
    directory: Path,
    templates: dict[str, RoleTemplate],
    issues: list[RoleTemplateIssue],
) -> None:
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("."):
            continue  # 跳过隐藏文件(如 macOS 打包产生的 ._ AppleDouble 元数据,二进制非 JSON)
        _load_template_file(path, templates, issues, source="user")


def _builtin_template_paths() -> list[Path]:
    directory = Path(__file__).with_name("role_template_catalog").joinpath("builtin")
    return sorted(p for p in directory.glob("*.json") if not p.name.startswith("."))


def _load_template_file(
    path: Path,
    templates: dict[str, RoleTemplate],
    issues: list[RoleTemplateIssue],
    *,
    source: str,
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        issues.append(RoleTemplateIssue("", str(path), f"invalid JSON: {exc}"))
        return
    for item in _iter_template_payloads(payload):
        _register_template(
            templates,
            issues,
            item,
            registration=RoleTemplateRegistration(source=source, source_path=str(path)),
        )


def _iter_template_payloads(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, dict) and isinstance(payload.get("templates"), list):
        return [item for item in payload["templates"] if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _register_template(
    templates: dict[str, RoleTemplate],
    issues: list[RoleTemplateIssue],
    payload: dict[str, object],
    *,
    registration: RoleTemplateRegistration,
) -> None:
    template_id = _clean_id(payload.get("id"))
    issue = _validate_payload(template_id, payload, registration.source_path)
    if issue:
        issues.append(issue)
        return
    template = _template_from_payload(
        payload,
        source=registration.source,
        source_path=registration.source_path,
    )
    templates[template.id] = template


def _validate_payload(
    template_id: str,
    payload: dict[str, object],
    source_path: str,
) -> RoleTemplateIssue | None:
    if not template_id:
        return RoleTemplateIssue("", source_path, "missing template id")
    if str(payload.get("scope") or "").strip().lower() != "role":
        return RoleTemplateIssue(template_id, source_path, "role template must describe a broad role")
    if payload.get("handles_multiple_targets") is not True:
        return RoleTemplateIssue(template_id, source_path, "role template must describe a broad role")
    for field_name in ("name_zh", "summary_zh", "output_contract_zh"):
        if not str(payload.get(field_name) or "").strip():
            return RoleTemplateIssue(template_id, source_path, f"missing {field_name}")
    if not _role_template_list(payload.get("use_when_zh")):
        return RoleTemplateIssue(template_id, source_path, "missing use_when_zh")
    return None


def _template_from_payload(payload: dict[str, object], *, source: str, source_path: str) -> RoleTemplate:
    return RoleTemplate(
        id=_clean_id(payload.get("id")),
        name=str(payload.get("name") or "").strip(),
        name_zh=str(payload.get("name_zh") or "").strip(),
        summary=str(payload.get("summary") or "").strip(),
        summary_zh=str(payload.get("summary_zh") or "").strip(),
        scope=str(payload.get("scope") or "").strip().lower(),
        handles_multiple_targets=payload.get("handles_multiple_targets") is True,
        use_when_zh=_role_template_list(payload.get("use_when_zh")),
        do_not_use_when_zh=_role_template_list(payload.get("do_not_use_when_zh")),
        default_tools=_role_template_list(payload.get("default_tools")),
        can_write=bool(payload.get("can_write", False)),
        can_accept=bool(payload.get("can_accept", False)),
        can_run_tests=bool(payload.get("can_run_tests", False)),
        can_spawn_children=bool(payload.get("can_spawn_children", False)),
        depends_on_outputs=bool(payload.get("depends_on_outputs", False)),
        output_contract=dict(payload.get("output_contract") or {}),
        output_contract_zh=str(payload.get("output_contract_zh") or "").strip(),
        prompt_zh=str(payload.get("prompt_zh") or "").strip(),
        source=source,
        source_path=source_path,
    )


def _role_template_list(value: object) -> list[str]:
    return string_list(value, _ROLE_TEMPLATE_LIST_OPTIONS)


def _clean_id(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")
