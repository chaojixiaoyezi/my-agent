# LLM: reusable subagent role template catalog; keep entries broad, bilingual, and tool-policy aware.
# 模块用途: 提供内置/用户自定义子代理角色模板，让父代理按“找茬/测试/验收/协调/写作”等广义职责派工。

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .role_template_resolution import resolve_role_template_id

# LLM: WEB_TOOLS keeps broad role templates able to gather web evidence without role-specific tool gaps.
# 函数用途: 给研究、写作、测试、验收等模板统一补齐网页读取能力，避免自然语言派工后落成“没法查资料”的空角色。
WEB_TOOLS = ["fetch_url", "http_request"]
READ_ONLY_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
WORKER_READ_TOOLS = ["list_files", "read_file", "search_text", "read_artifact", *WEB_TOOLS]
ARTIFACT_BUILDER_TOOLS = ["file_write_session", "write_structured_json", "data_to_workbook", "markdown_to_pdf"]
WORKER_WRITE_TOOLS = ["write_file", "append_file", "replace_in_file", *ARTIFACT_BUILDER_TOOLS]
REPORT_WRITE_TOOLS = ["write_file", "append_file", "replace_in_file", *ARTIFACT_BUILDER_TOOLS]
CAPABILITY_REQUEST_TOOL = "capability_request"
ROLE_BASE_TOOLS = [*READ_ONLY_TOOLS, *REPORT_WRITE_TOOLS, CAPABILITY_REQUEST_TOOL]
COORDINATOR_TOOLS = [
    "schedule_child_subagents",
    "dispatch_subagents",
    "subagent_board",
    "subagent_message",
    *ROLE_BASE_TOOLS,
]


# LLM: RoleTemplate is the stable bundle for one reusable subagent persona.
# 类用途: 保存一个广义子代理角色的中文说明、默认工具、验收输出和权限边界。
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
    output_contract: dict[str, Any] = field(default_factory=dict)
    output_contract_zh: str = ""
    prompt_zh: str = ""
    source: str = "builtin"
    source_path: str = ""


# LLM: RoleTemplateIssue records rejected user templates without breaking startup.
# 类用途: 保存模板加载问题，方便用户修正自定义 JSON，而不影响内置模板继续工作。
@dataclass(frozen=True)
class RoleTemplateIssue:
    template_id: str
    source_path: str
    message: str


# LLM: RoleTemplateRegistration bundles source metadata for size-guard-friendly helper calls.
# 类用途: 保存模板来源和路径，避免注册函数继续增加散参数。
@dataclass(frozen=True)
class RoleTemplateRegistration:
    source: str
    source_path: str


# LLM: RoleTemplateStore is the read API for built-in and user role templates.
# 类用途: 提供按 id 查询和列出模板的稳定入口，同时带上加载告警。
@dataclass(frozen=True)
class RoleTemplateStore:
    templates: dict[str, RoleTemplate]
    issues: list[RoleTemplateIssue] = field(default_factory=list)

    # LLM: get normalizes role ids so aliases can be handled by callers before lookup.
    # 函数用途: 按模板 id 查找角色模板，找不到返回 None。
    def get(self, template_id: str) -> RoleTemplate | None:
        return self.templates.get(_clean_id(template_id))

    # LLM: all returns templates in deterministic id order for docs and tests.
    # 函数用途: 列出所有可用模板，顺序稳定便于生成文档和调试。
    def all(self) -> list[RoleTemplate]:
        return [self.templates[key] for key in sorted(self.templates)]


# LLM: load_role_template_store merges built-ins with optional user JSON files.
# 函数用途: 加载内置角色模板，并从用户目录读取额外模板；坏模板只记录 issue，不让系统启动失败。
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


# LLM: template_for_role is a tiny helper for runtime role contracts.
# 函数用途: 根据标准化 role id 返回模板；用户目录由上层传入。
def template_for_role(
    role: str,
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
) -> RoleTemplate | None:
    return load_role_template_store(user_template_dir=user_template_dir).get(role)


# LLM: role_template_id_for_role maps natural runtime role names onto external template ids.
# 函数用途: 从模板目录中查找最匹配的角色模板；child_coordinator、qa_tester、用户自定义前后缀角色都不能落成空模板。
def role_template_id_for_role(
    role: str,
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
    *,
    fallback: str | None = None,
) -> str:
    store = load_role_template_store(user_template_dir=user_template_dir)
    return resolve_role_template_id(store, role, fallback=fallback)


# LLM: role_template_index_text renders the lightweight catalog main agents can keep in prompt.
# 函数用途: 输出角色模板索引，只包含 id、中文名、摘要、能力标签和模板位置；不展开完整 prompt 细节。
def role_template_index_text(
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
    *,
    limit: int = 12,
) -> str:
    store = load_role_template_store(user_template_dir=user_template_dir)
    lines = [
        (
            f"- {item.id}: {item.name_zh}；{item.summary_zh}；"
            f"适用={_compact_role_rules(item.use_when_zh)}；"
            f"不适用={_compact_role_rules(item.do_not_use_when_zh)}；"
            f"能力={_capability_tags(item)}；模板位置={item.source_path}"
        )
        for item in store.all()[:limit]
    ]
    return "\n".join(lines)


# LLM: role_template_detail_text loads full role prompt contracts only when dispatching/delegating.
# 函数用途: 按需输出选定角色的详细中文提示、默认工具和输出合同；用于派工 prompt，不用于常驻索引。
def role_template_detail_text(
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
    *,
    roles: list[str] | tuple[str, ...] | None = None,
    limit: int = 12,
) -> str:
    store = load_role_template_store(user_template_dir=user_template_dir)
    templates = _selected_templates(store, roles=roles, limit=limit)
    return "\n".join(_detail_lines(item) for item in templates)


# LLM: role_template_guide_text keeps older callers compatible while preferring the lightweight index.
# 函数用途: 兼容旧的 guide 调用；新代码应按场景选择 index 或 detail。
def role_template_guide_text(
    user_template_dir: str | Path | Iterable[str | Path] | None = None,
    *,
    limit: int = 12,
) -> str:
    return role_template_index_text(user_template_dir=user_template_dir, limit=limit)


# LLM: _selected_templates keeps detail loading scoped to requested roles when possible.
# 函数用途: 根据 roles 选择模板详情；未指定时最多返回前 limit 个模板，避免 prompt 无限增长。
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


# LLM: _capability_tags compresses role permissions for the always-on template catalog.
# 函数用途: 把写入、验收、测试、派生子节点等能力压成短标签，减少主 prompt 负担。
def _capability_tags(item: RoleTemplate) -> str:
    tags = []
    if item.can_spawn_children:
        tags.append("spawn_children")
    if item.can_write:
        tags.append("write")
    if item.can_run_tests:
        tags.append("test")
    if item.can_accept:
        tags.append("accept")
    return ",".join(tags) or "read_only"


# LLM: _compact_role_rules keeps role-selection hints useful without loading full prompts.
# 函数用途: 将角色适用/不适用场景压成一行，给主代理和派工类角色做轻量选择依据。
def _compact_role_rules(items: list[str], *, max_chars: int = 80) -> str:
    text = "；".join(item.strip() for item in items if item.strip()) or "未设置"
    return text if len(text) <= max_chars else text[:max_chars].rstrip() + "..."


# LLM: _detail_lines renders one full role template for active delegation prompts.
# 函数用途: 输出一个模板的完整细节，包括适用/不适用、默认工具、输出合同和中文系统提示片段。
def _detail_lines(item: RoleTemplate) -> str:
    return "\n".join([
        f"- {item.id}: {item.name_zh}",
        f"  用途: {item.summary_zh}",
        f"  适用: {'；'.join(item.use_when_zh) or '未设置'}",
        f"  不适用: {'；'.join(item.do_not_use_when_zh) or '未设置'}",
        f"  默认工具: {', '.join(item.default_tools) or 'none'}",
        f"  输出合同: {item.output_contract_zh or '未设置'}",
        f"  系统提示片段: {item.prompt_zh or '未设置'}",
        f"  模板位置: {item.source_path}",
    ])


# LLM: _iter_user_template_dirs protects strings from being treated as char iterables.
# 函数用途: 把 None、单目录和目录列表统一成 Path 列表，供模板加载循环使用。
def _iter_user_template_dirs(value: str | Path | Iterable[str | Path] | None) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [Path(value)]
    return [Path(item) for item in value if str(item).strip()]


# LLM: _load_user_templates scans only JSON files in one configured directory.
# 函数用途: 读取用户自定义角色模板目录，支持单文件单模板或 {"templates": [...]} 格式。
def _load_user_templates(
    directory: Path,
    templates: dict[str, RoleTemplate],
    issues: list[RoleTemplateIssue],
) -> None:
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.json")):
        _load_template_file(path, templates, issues, source="user")


# LLM: _builtin_template_paths keeps built-in roles editable as JSON assets.
# 函数用途: 返回仓库内置角色模板文件路径；代码只加载模板，不把模板正文硬编码进 Python。
def _builtin_template_paths() -> list[Path]:
    directory = Path(__file__).with_name("role_template_catalog").joinpath("builtin")
    return sorted(directory.glob("*.json"))


# LLM: _load_template_file shares JSON parsing between built-in and user template directories.
# 函数用途: 读取一个模板 JSON 文件，支持单模板或 templates 数组，错误转成 issue。
def _load_template_file(
    path: Path,
    templates: dict[str, RoleTemplate],
    issues: list[RoleTemplateIssue],
    *,
    source: str,
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(RoleTemplateIssue("", str(path), f"invalid JSON: {exc}"))
        return
    for item in _iter_template_payloads(payload):
        _register_template(
            templates,
            issues,
            item,
            registration=RoleTemplateRegistration(source=source, source_path=str(path)),
        )


# LLM: _iter_template_payloads accepts both single-template and list-wrapper JSON shapes.
# 函数用途: 兼容用户模板文件的两种常见写法，减少格式负担。
def _iter_template_payloads(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, dict) and isinstance(payload.get("templates"), list):
        return [item for item in payload["templates"] if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


# LLM: _register_template validates before exposing a template to dispatch code.
# 函数用途: 校验模板必须是广义角色、必须有中文说明；通过后写入模板表。
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


# LLM: _validate_payload keeps role templates broad and readable for Chinese users.
# 函数用途: 拒绝小动作模板和缺少中文说明的模板，避免模板库碎片化。
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
    if not _string_list(payload.get("use_when_zh")):
        return RoleTemplateIssue(template_id, source_path, "missing use_when_zh")
    return None


# LLM: _template_from_payload performs narrow coercion after validation has passed.
# 函数用途: 把 JSON/dict 输入转换成 RoleTemplate，保证字段类型稳定。
def _template_from_payload(payload: dict[str, object], *, source: str, source_path: str) -> RoleTemplate:
    return RoleTemplate(
        id=_clean_id(payload.get("id")),
        name=str(payload.get("name") or "").strip(),
        name_zh=str(payload.get("name_zh") or "").strip(),
        summary=str(payload.get("summary") or "").strip(),
        summary_zh=str(payload.get("summary_zh") or "").strip(),
        scope=str(payload.get("scope") or "").strip().lower(),
        handles_multiple_targets=payload.get("handles_multiple_targets") is True,
        use_when_zh=_string_list(payload.get("use_when_zh")),
        do_not_use_when_zh=_string_list(payload.get("do_not_use_when_zh")),
        default_tools=_string_list(payload.get("default_tools")),
        can_write=bool(payload.get("can_write", False)),
        can_accept=bool(payload.get("can_accept", False)),
        can_run_tests=bool(payload.get("can_run_tests", False)),
        can_spawn_children=bool(payload.get("can_spawn_children", False)),
        output_contract=dict(payload.get("output_contract") or {}),
        output_contract_zh=str(payload.get("output_contract_zh") or "").strip(),
        prompt_zh=str(payload.get("prompt_zh") or "").strip(),
        source=source,
        source_path=source_path,
    )


# LLM: _string_list normalizes loose config JSON values into compact string lists.
# 函数用途: 把字符串、列表、空值统一为去空白后的字符串列表。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = [value]
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


# LLM: _clean_id makes template ids stable across user JSON and runtime roles.
# 函数用途: 统一模板 id 的大小写和连接符，便于 role 别名映射。
def _clean_id(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")
