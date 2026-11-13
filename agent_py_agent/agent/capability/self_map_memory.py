# LLM: 只列已绑定组件及规范路径；历史指针使用显式thread_browse模式，不读取正文或遍历账本计数。
# 模块用途: 登记记忆、Persona和学习组件的安全元信息、数量未知原因及原读入口。
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.nofollow_fs import NoFollowPathError, read_bytes_beneath
from ..settings.live_switches import refresh_live_switches
from ..tooling.self_map_registry import SectionDescriptor, SectionProjection, map_item, next_read
from ..user_space.self_map_paths import location_items


# LLM: 记忆领域自行声明静态用途，地图不建记忆副本或读取正文来生成说明。
# 函数用途: 接入记忆分块。
def memory_summary(context):
    """总览摘要读口：仅返回轻量计数与未知原因，不读取正文或膨胀总览字节。"""
    if context.child or context.context_scope in {"task_local", "control_plane", "isolated"}:
        return SectionProjection((), "partial", "not_observed", source="current task memory scope", count_known=False)
    return SectionProjection(
        (),
        source="bound owner memory authorities / tool scope",
        counts={"formal_entries": None},
        count_reasons={"formal_entries": "只列组件元信息，不读取记忆正文计数。"},
        count_labels={"formal_entries": "正式条数"},
    )


def register_memory_section(registry):
    registry.register(
        SectionDescriptor(
            "memory",
            "记忆与人格",
            70,
            memory_projection,
            "记忆组件、正式记忆位置及人格入口",
            summary_reader=memory_summary,
        )
    )


# LLM: 子/隔离不增owner读权；历史next_read明确规范mode，管理员只列正式位置且不读正文计数。
# 函数用途: 展示合法组件、正式数量未知原因和权威位置，不调用写侧或检索。
_MEMORY_TELEMETRY_LABELS = {
    "formal_entries": "正式条数",
    "lessons_active_count": "活跃经验条数",
    "lessons_routed_count": "常驻注入经验数",
    "lessons_archived_count": "已归档经验数(永不自动删)",
    "lesson_archive_days": "经验归档宽限(天)",
}


def _live_lesson_archive_days(agent: object) -> int:
    cfg = getattr(agent, "config", None)
    if cfg is not None:
        try:
            refresh_live_switches(cfg)
        except OSError:
            pass
    raw = getattr(cfg, "memory_lesson_archive_days", 15)
    if not isinstance(raw, bool) and isinstance(raw, int) and raw >= 0:
        return raw
    return 15


def _read_lesson_heat_readonly(agent: object) -> tuple[str, dict[str, Any] | str]:
    lessons_obj = getattr(agent, "memory_lessons", None)
    reader = getattr(lessons_obj, "_read_heat_state", None)
    if callable(reader):
        try:
            return reader()
        except Exception as exc:  # noqa: BLE001 - 只读状态不可用时降级为未知。
            return "error", type(exc).__name__

    from ..memory_store.lessons import _valid_heat_state

    home = getattr(agent, "home_paths", None)
    mem_dir = getattr(home, "owner_memory_dir", None)
    if not mem_dir:
        return "missing", "memory_dir_unavailable"
    mem_root = Path(mem_dir)
    heat_path = mem_root / "lessons" / "lesson_heat.json"
    if not os.path.lexists(heat_path):
        return "missing", "heat_state_absent"
    if os.path.islink(heat_path) or os.path.islink(mem_root / "lessons"):
        return "corrupt", "heat_state_symlink"
    try:
        raw = read_bytes_beneath(mem_root, ("lessons", "lesson_heat.json"))
    except (NoFollowPathError, OSError) as exc:
        return "error", type(exc).__name__
    if raw is None:
        return "missing", "heat_state_absent"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "corrupt", "heat_state_invalid_json"
    if not _valid_heat_state(data):
        return "corrupt", "heat_state_schema_invalid"
    return "ok", data


def _memory_telemetry_and_status(context: object) -> tuple[dict[str, Any], str]:
    """构建 MemoryTelemetry 载荷与底层热态读取状态。"""
    ts = time.time()
    as_of_iso = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    archive_days = _live_lesson_archive_days(context.agent)
    status, payload = _read_lesson_heat_readonly(context.agent)
    count_reasons: dict[str, str] = {}
    if status != "ok" or not isinstance(payload, dict):
        reason = str(payload or status or "lesson_heat_unavailable")
        count_reasons["lessons_active_count"] = f"lesson_heat.json 状态为 {status}（{reason}），未读取活跃经验数量。"
        count_reasons["lessons_routed_count"] = f"lesson_heat.json 状态为 {status}（{reason}），未读取常驻注入数量。"
        count_reasons["lessons_archived_count"] = f"lesson_heat.json 状态为 {status}（{reason}），未确认归档数量。"
        return (
            {
                "lessons_active_count": None,
                "lessons_routed_count": None,
                "lessons_archived_count": None,
                "lesson_archive_days": archive_days,
                "archive_auto_delete": False,
                "coverage": {"status": "unavailable", "reason": reason, "as_of": as_of_iso},
                "count_reasons": count_reasons,
            },
            str(status or "error"),
        )

    rows = payload.get("rows") or {}
    routed_ids = payload.get("routed_ids") or []
    active_count = sum(1 for row in rows.values() if isinstance(row, dict) and row.get("status") == "active")
    routed_count = len(routed_ids)
    raw_archived = payload.get("lessons_archived_count")
    arch_cov = payload.get("archive_count_coverage")
    cov_dict = dict(arch_cov) if isinstance(arch_cov, dict) else {"status": "unavailable", "reason": "archive_coverage_missing"}
    raw_as_of = cov_dict.get("as_of")
    if isinstance(raw_as_of, (int, float)) and not isinstance(raw_as_of, bool):
        cov_dict["as_of"] = datetime.fromtimestamp(float(raw_as_of), timezone.utc).isoformat()
    elif not isinstance(raw_as_of, str) or not raw_as_of:
        cov_dict["as_of"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    cov_status = str(cov_dict.get("status") or "unavailable")
    if cov_status == "complete" and not isinstance(raw_archived, bool) and isinstance(raw_archived, int) and raw_archived >= 0:
        archived_count: int | None = raw_archived
    else:
        archived_count = None
        count_reasons["lessons_archived_count"] = str(cov_dict.get("reason") or "归档统计尚未完成或覆盖不全。")

    return (
        {
            "lessons_active_count": active_count,
            "lessons_routed_count": routed_count,
            "lessons_archived_count": archived_count,
            "lesson_archive_days": archive_days,
            "archive_auto_delete": False,
            "coverage": cov_dict,
            "count_reasons": count_reasons,
        },
        "ok",
    )


def memory_telemetry(context: object) -> dict[str, Any]:
    """返回符合 MemoryTelemetry 契约的经验生命周期与归档真实计数（未知保持 None）。"""
    return _memory_telemetry_and_status(context)[0]


def memory_projection(context):
    if context.child or context.context_scope in {"task_local", "control_plane", "isolated"}:
        return SectionProjection((), "partial", "not_observed", source="current task memory scope", count_known=False)
    items = []
    if "remember" in context.snapshot.available_tool_names:
        items.append(map_item("formal", "正式长期记忆", fields={"count": None, "next_read": next_read("remember", {"action": "list"})}))
    if "update_persona" in context.snapshot.available_tool_names:
        items.extend(map_item(target, name, fields={"count": None, "next_read": next_read("update_persona", {"target": target, "action": "list"})})
                     for target, name in (("user", "用户画像"), ("agents", "工作约定"), ("soul", "人格语气")))
    if "session_search" in context.snapshot.available_tool_names:
        items.append(map_item("archive", "历史与归档", fields={"count": None, "next_read": next_read("session_search", {"mode": "thread_browse"})}))
    items.extend(_source_rule_metadata(context))
    if context.admin:
        tel, heat_status = _memory_telemetry_and_status(context)
        for name, attr in (
            ("candidates", "memory_candidates"),
            ("lessons", "memory_lessons"),
            ("hot", "memory_hot"),
            ("curator", "memory_curator"),
        ):
            fields: dict[str, Any] = {"count": None, "bound": getattr(context.agent, attr, None) is not None}
            if name == "lessons":
                fields["count"] = tel["lessons_active_count"]
                fields["telemetry"] = tel
                fields["archive_auto_delete"] = False
            items.append(map_item(name, name, fields=fields))
        counts = {
            "formal_entries": None,
            "lessons_active_count": tel["lessons_active_count"],
            "lessons_routed_count": tel["lessons_routed_count"],
            "lessons_archived_count": tel["lessons_archived_count"],
            "lesson_archive_days": tel["lesson_archive_days"],
        }
        reasons = {"formal_entries": "只列组件元信息，不读取记忆正文计数。", **tel["count_reasons"]}
        summary = (
            "正式长期记忆存放位置："
            + str(context.agent.home_paths.owner_memory_long_term_jsonl)
            + "；未读取正文计数；"
            + f"经验归档宽限={tel['lesson_archive_days']}天，永不自动删除归档文件（archive_auto_delete=False），"
            + f"覆盖={tel['coverage']['status']}（{tel['coverage'].get('reason', '')}）。"
        )
        cov_status = str(tel["coverage"].get("status") or "")
        completeness = "complete" if cov_status == "complete" else "partial"
        errors = (
            ("LESSON_HEAT_CORRUPT",)
            if heat_status == "corrupt"
            else ("LESSON_HEAT_UNAVAILABLE",)
            if heat_status == "error"
            else ()
        )
        return SectionProjection(
            tuple(items),
            completeness,
            errors=errors,
            source="bound owner memory authorities / tool scope",
            counts=counts,
            count_reasons=reasons,
            locations=_memory_locations(context),
            count_labels=_MEMORY_TELEMETRY_LABELS,
            summary=summary,
        )
    return SectionProjection(
        tuple(items),
        source="bound owner memory authorities / tool scope",
        counts={"formal_entries": None},
        count_reasons={"formal_entries": "只列组件元信息，不读取记忆正文计数。"},
        locations=_memory_locations(context),
        count_labels={"formal_entries": "正式条数"},
        summary="",
    )


# LLM: 先完整管理员判据后取规范位置；不读取正文，也不调用服务构造器或提升文件权限。
# 函数用途: 给正式记忆和三种人格文件的位置及原读写状态。
def _memory_locations(context):
    if not context.admin:
        return ()
    home = context.agent.home_paths
    return location_items(context, {"formal": home.owner_memory_long_term_jsonl, "user": home.owner_user_md,
                                   "agents": home.owner_agents_md, "soul": home.owner_soul_md})


# LLM: 仅管理员本人读取已绑定F4仓库configuration；不构造、load或读正文，不暴露locator，配置字段不代表正文新鲜。
# 函数用途: 标明可控规则来源的开关和修订，显式管理入口只作用户操作说明。
def _source_rule_metadata(context):
    if not context.admin:
        return ()
    enabled = bool(getattr(context.agent.config, "agents_source_enabled", True))
    fields = {"enabled": enabled, "content_observed": "not_observed",
              "user_control": {"command": "/agents show", "explicit_user_required": True}}
    repository = getattr(context.agent, "agents_source_repository", None)
    if not enabled:
        return (map_item("rules_source", "可控工作规则来源", state="disabled", fields=fields),)
    if repository is None:
        return (map_item("rules_source", "可控工作规则来源", state="not_observed", fields=fields),)
    try:
        snapshot = repository.configuration()
        if getattr(snapshot, "owner", None) != context.agent.home_paths.owner_id:
            raise ValueError("owner mismatch")
        fields.update({key: getattr(snapshot, key, "") for key in ("kind", "revision", "status", "failure_code")})
        return (map_item("rules_source", "可控工作规则来源", fields=fields),)
    except (OSError, RuntimeError, TypeError, AttributeError, ValueError):
        return (map_item("rules_source", "可控工作规则来源", state="unavailable", fields=fields),)
