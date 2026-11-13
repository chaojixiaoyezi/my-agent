# LLM: 只读当轮快照及服务规范根声明，不发现、刷新或读取正文；子代理沿精确restricted子集。
# 模块用途: 登记普通方法、数量和管理员权威位置，保留stable_id及合法正文入口。
import os
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from ..settings.live_switches import refresh_live_switches
from ..tooling.self_map_registry import SectionDescriptor, SectionProjection, map_item, next_read
from ..user_space.self_map_paths import location_items
from .learned_metadata_projection import metadata_from_registry
from .skill_learning_maintenance import MAX_STRIKE_COUNT
from .skill_learning_store import SkillLearningStore, SkillLearningStoreError
from .skill_learning_versions import frozen_learned_metadata_batch, learned_store


# LLM: 普通方法领域装配固定用途，schema和工具说明不维护第二份skills编号。
# 函数用途: 登记普通方法分块。
def register_skill_section(registry):
    registry.register(SectionDescriptor("skills", "方法", 30, skill_projection, "普通Skill方法清单、短用途与数量",
                                        summary_reader=skill_summary))


# LLM: 只数原owner匹配且完整的冻结快照；不补写生命周期或readable结论，不逐条读取登记表。
# 函数用途: 提供普通方法总览数量和类别，缺绑定或损坏保持未知。
def skill_summary(context):
    snapshot = getattr(context.agent, "_current_skill_snapshot", None)
    if snapshot is None:
        return SectionProjection((), "partial", "not_observed", source="SkillSnapshot", count_known=False)
    if snapshot.owner_id != context.agent.home_paths.owner_id:
        raise ValueError("snapshot owner mismatch")
    entries = snapshot.enabled_entries()
    errors = tuple(sorted({error.code for error in snapshot.errors}))
    return SectionProjection((), "partial" if errors else "complete", errors=errors, source="SkillSnapshot.enabled_entries",
                             summary_count=None if errors else len(entries),
                             summary_families=None if errors else dict(sorted(Counter(entry.category or 'uncategorized' for entry in entries).items())))


# LLM: 未绑定是未观测，错误仅安全码；普通方法数不混入内容包，位置不从条目私有路径推导。
# 函数用途: 列当前授权方法数量/用途及管理员位置，不执行发现或reload。
_SKILL_TELEMETRY_LABELS = {
    "ordinary_skills": "可用方法总数",
    "live_learned_count": "在役自学习数",
    "max_learned_skills": "自学习数量上限",
    "user_owned_count": "用户自有数(不占上限)",
    "shared_count": "共享技能数(不占上限)",
    "cooled_count": "冷藏中数",
    "archived_count": "已归档数",
    "cool_seconds": "冷藏阈值(秒)",
    "archive_seconds": "归档阈值(秒)",
    "evict_seconds": "归档清理保留期(秒)",
    "merge_similarity_percent": "合并相似度阈值(%)",
    "recommended_limit": "默认推荐上限",
    "recommended_explore_percent": "探索比例(%)",
    "retire_at_hurt_count": "退场负反馈次数",
}


def _cfg_int(cfg: object, key: str, default: int) -> int:
    raw = getattr(cfg, key, default)
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int) and raw >= 0:
        return raw
    return default


def _is_restricted_context(context: object) -> bool:
    return bool(
        getattr(context, "child", False)
        or str(getattr(context, "context_scope", "") or "") in {"task_local", "control_plane", "isolated"}
        or not getattr(context, "admin", False)
    )


def _refresh_config(agent: object) -> object:
    cfg = getattr(agent, "config", None)
    if cfg is not None:
        try:
            refresh_live_switches(cfg)
        except OSError:
            pass
    return cfg


def _load_owner_registry_once(context: object, entries: tuple[Any, ...]):
    home_paths = getattr(context.agent, "home_paths", None)
    if _is_restricted_context(context) or not getattr(home_paths, "owner_skill_learning_dir", None):
        return None, None, {}, "", False, frozen_learned_metadata_batch(entries)

    owner_store = SkillLearningStore.for_home(home_paths)
    owner_entries: list[Any] = []
    other_entries: list[Any] = []
    for entry in entries:
        if not getattr(entry, "frozen_path", ""):
            continue
        try:
            st = learned_store(entry)
        except (OSError, ValueError, IndexError):
            other_entries.append(entry)
            continue
        if st.directory == owner_store.directory:
            owner_entries.append(entry)
        else:
            other_entries.append(entry)

    metadata: dict[str, Any] = {}
    if other_entries:
        metadata.update(frozen_learned_metadata_batch(other_entries))

    store_symlink = any(
        os.path.islink(path)
        for path in (owner_store.directory, owner_store.registry_path, owner_store.learned_root)
    )
    if store_symlink:
        for entry in owner_entries:
            metadata[entry.stable_id] = None
        return owner_store, None, {}, "skill_learning_symlink_detected", False, metadata

    has_disk_state = (
        bool(owner_entries)
        or os.path.lexists(owner_store.registry_path)
        or os.path.lexists(owner_store.learned_root)
    )
    if not has_disk_state:
        return owner_store, None, {}, "", True, metadata

    try:
        registry = owner_store.load_registry()
    except (OSError, SkillLearningStoreError, ValueError) as exc:
        for entry in owner_entries:
            metadata[entry.stable_id] = None
        reason = getattr(exc, "code", "") or f"registry_unreadable:{type(exc).__name__}"
        return owner_store, None, {}, str(reason), False, metadata

    for entry in owner_entries:
        try:
            metadata[entry.stable_id] = metadata_from_registry(entry, owner_store, registry)
        except (OSError, SkillLearningStoreError, ValueError):
            metadata[entry.stable_id] = None
    return owner_store, registry, None, "", False, metadata


def skill_telemetry(
    context: object,
    *,
    snapshot: object | None = None,
    owner_store: SkillLearningStore | None = None,
    owner_registry: Any | None = None,
    registry_error: str = "",
    owner_absent: bool = False,
) -> dict[str, Any]:
    """返回符合 SkillTelemetry 契约的实时自学习策略与真实计数（未知保持 None）。"""
    ts = time.time()
    cfg = _refresh_config(context.agent)
    mode = str(getattr(cfg, "self_learning_maintenance_mode", "autonomous") or "autonomous").strip().lower()
    enabled = bool(getattr(cfg, "enable_self_learning", False))
    max_skills = _cfg_int(cfg, "self_learning_max_skills", 50)
    cool_seconds = _cfg_int(cfg, "self_learning_cool_seconds", 604800)
    archive_seconds = _cfg_int(cfg, "self_learning_archive_seconds", 1296000)
    evict_seconds = _cfg_int(cfg, "self_learning_evict_seconds", 1296000)
    merge_pct = _cfg_int(cfg, "self_learning_merge_similarity_percent", 70)
    rec_limit = _cfg_int(cfg, "self_learning_recommended_limit", 50)
    rec_explore = _cfg_int(cfg, "self_learning_recommended_explore_percent", 20)

    count_reasons: dict[str, str] = {}
    partial_reasons: list[str] = []

    live_learned_count: int | None = None
    cooled_count: int | None = None
    archived_count: int | None = None

    if registry_error:
        partial_reasons.append(registry_error)
        count_reasons["live_learned_count"] = f"登记表或目录状态不可核验（{registry_error}），不能猜测数量。"
        count_reasons["cooled_count"] = f"登记表不可核验（{registry_error}）。"
        count_reasons["archived_count"] = f"登记表不可核验（{registry_error}）。"
    elif owner_absent:
        live_learned_count = 0
        cooled_count = 0
        archived_count = 0
    elif owner_store is not None and owner_registry is not None:
        cooled_count = sum(
            1
            for row in owner_registry.skills.values()
            if row.layout != "owner" and row.maintenance.get("state") == "cooled"
        )
        archived_count = sum(
            1
            for row in owner_registry.skills.values()
            if row.layout != "owner" and row.maintenance.get("state") == "archived"
        )
        try:
            managed = owner_store.managed_records(owner_registry, include_archived=False)
            live_learned_count = owner_registry.live_count(managed_names=managed.keys())
        except (SkillLearningStoreError, OSError) as exc:
            err = getattr(exc, "code", "") or type(exc).__name__
            partial_reasons.append(f"managed_records:{err}")
            count_reasons["live_learned_count"] = f"托管目录所有权核验未完全通过（{err}），保持未知。"
    else:
        partial_reasons.append("store_unavailable")
        count_reasons["live_learned_count"] = "当前上下文缺少可校验的学习存储目录。"
        count_reasons["cooled_count"] = "当前上下文缺少可校验的学习存储目录。"
        count_reasons["archived_count"] = "当前上下文缺少可校验的学习存储目录。"

    user_owned_count: int | None = None
    shared_count: int | None = None
    if snapshot is None:
        partial_reasons.append("snapshot_unbound")
        count_reasons["user_owned_count"] = "本轮方法快照未绑定，无法确认用户自有与工作区方法数。"
        count_reasons["shared_count"] = "本轮方法快照未绑定，无法确认共享方法数。"
    elif getattr(snapshot, "errors", ()):
        partial_reasons.append("snapshot_errors")
        count_reasons["user_owned_count"] = "方法快照存在错误，用户自有数量未确认。"
        count_reasons["shared_count"] = "方法快照存在错误，共享方法数量未确认。"
    else:
        entries = snapshot.enabled_entries()
        shared_count = sum(1 for entry in entries if entry.source == "shared")
        if registry_error:
            count_reasons["user_owned_count"] = f"登记表不可核验（{registry_error}），不能区分用户自有与学习托管条目。"
        else:
            skills_map = owner_registry.skills if owner_registry is not None else {}
            user_owned = 0
            for entry in entries:
                if entry.source == "workspace":
                    user_owned += 1
                elif entry.source == "owner":
                    rec = skills_map.get(entry.name)
                    if (
                        rec is None
                        or rec.layout == "owner"
                        or rec.maintenance.get("ownership") in {"owner_layout", "user_modified"}
                    ):
                        user_owned += 1
            user_owned_count = user_owned

    cov_status = "complete"
    cov_reason = "registry_and_snapshot_complete"
    if registry_error and (snapshot is None or getattr(snapshot, "errors", ())):
        cov_status = "unavailable"
        cov_reason = ",".join(partial_reasons)
    elif partial_reasons:
        cov_status = "partial"
        cov_reason = ",".join(partial_reasons)

    return {
        "maintenance_mode": mode,
        "self_learning_enabled": enabled,
        "live_learned_count": live_learned_count,
        "max_learned_skills": max_skills,
        "user_owned_count": user_owned_count,
        "shared_count": shared_count,
        "user_owned_exempt_from_cap": True,
        "shared_exempt_from_cap": True,
        "cooled_count": cooled_count,
        "archived_count": archived_count,
        "cool_seconds": cool_seconds,
        "archive_seconds": archive_seconds,
        "evict_seconds": evict_seconds,
        "merge_similarity_percent": merge_pct,
        "recommended_limit": rec_limit,
        "recommended_explore_percent": rec_explore,
        "required_uses_formula": "3 + hurt_count",
        "retire_at_hurt_count": MAX_STRIKE_COUNT,
        "coverage": {
            "status": cov_status,
            "reason": cov_reason,
            "as_of": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
        },
        "count_reasons": count_reasons,
    }


def _telemetry_summary_text(telemetry: dict[str, Any]) -> str:
    return (
        f"自主学习 telemetry：enable_self_learning={telemetry['self_learning_enabled']}，"
        f"maintenance_mode={telemetry['maintenance_mode']}，"
        f"cap={telemetry['max_learned_skills']}（user_owned 与 shared 豁免上限），"
        f"晋升公式={telemetry['required_uses_formula']}，retire_at_hurt_count={telemetry['retire_at_hurt_count']}，"
        f"冷却/归档/清理秒数={telemetry['cool_seconds']}/{telemetry['archive_seconds']}/{telemetry['evict_seconds']}，"
        f"覆盖={telemetry['coverage']['status']}（{telemetry['coverage']['reason']}）。"
    )


def skill_projection(context):
    snapshot = getattr(context.agent, "_current_skill_snapshot", None)
    locations = _skill_locations(context)
    restricted = _is_restricted_context(context)
    if snapshot is None:
        if restricted:
            return SectionProjection(
                (),
                "partial",
                "not_observed",
                source="SkillSnapshot",
                count_known=False,
                counts={"ordinary_skills": None},
                count_reasons={"ordinary_skills": "本轮方法快照未绑定。"},
                locations=locations,
            )
        owner_store, owner_registry, _, reg_err, owner_absent, _ = _load_owner_registry_once(context, ())
        tel = skill_telemetry(
            context,
            snapshot=None,
            owner_store=owner_store,
            owner_registry=owner_registry,
            registry_error=reg_err,
            owner_absent=owner_absent,
        )
        counts = {
            "ordinary_skills": None,
            "live_learned_count": tel["live_learned_count"],
            "max_learned_skills": tel["max_learned_skills"],
            "user_owned_count": tel["user_owned_count"],
            "shared_count": tel["shared_count"],
            "cooled_count": tel["cooled_count"],
            "archived_count": tel["archived_count"],
            "cool_seconds": tel["cool_seconds"],
            "archive_seconds": tel["archive_seconds"],
            "evict_seconds": tel["evict_seconds"],
            "merge_similarity_percent": tel["merge_similarity_percent"],
            "recommended_limit": tel["recommended_limit"],
            "recommended_explore_percent": tel["recommended_explore_percent"],
            "retire_at_hurt_count": tel["retire_at_hurt_count"],
        }
        reasons = {"ordinary_skills": "本轮方法快照未绑定。", **tel["count_reasons"]}
        return SectionProjection(
            (),
            "partial",
            "not_observed",
            source="SkillSnapshot",
            count_known=False,
            counts=counts,
            count_labels=_SKILL_TELEMETRY_LABELS,
            count_reasons=reasons,
            summary=_telemetry_summary_text(tel),
            locations=locations,
        )
    if snapshot.owner_id != context.agent.home_paths.owner_id:
        raise ValueError("snapshot owner mismatch")
    entries = snapshot.enabled_entries()
    errors = tuple(sorted({error.code for error in snapshot.errors}))
    if restricted:
        metadata = frozen_learned_metadata_batch(entries)
        items = tuple(_skill_item(entry, metadata.get(entry.stable_id)) for entry in entries)
        return SectionProjection(
            items,
            "partial" if errors else "complete",
            errors=errors,
            source="SkillSnapshot.enabled_entries",
            counts={"ordinary_skills": None if errors else len(items)},
            count_reasons={"ordinary_skills": "快照来源不完整，不能确认完整方法数量。"} if errors else {},
            locations=locations,
        )

    owner_store, owner_registry, _, reg_err, owner_absent, metadata = _load_owner_registry_once(context, entries)
    items = tuple(_skill_item(entry, metadata.get(entry.stable_id)) for entry in entries)
    tel = skill_telemetry(
        context,
        snapshot=snapshot,
        owner_store=owner_store,
        owner_registry=owner_registry,
        registry_error=reg_err,
        owner_absent=owner_absent,
    )
    counts = {
        "ordinary_skills": None if errors else len(items),
        "live_learned_count": tel["live_learned_count"],
        "max_learned_skills": tel["max_learned_skills"],
        "user_owned_count": tel["user_owned_count"],
        "shared_count": tel["shared_count"],
        "cooled_count": tel["cooled_count"],
        "archived_count": tel["archived_count"],
        "cool_seconds": tel["cool_seconds"],
        "archive_seconds": tel["archive_seconds"],
        "evict_seconds": tel["evict_seconds"],
        "merge_similarity_percent": tel["merge_similarity_percent"],
        "recommended_limit": tel["recommended_limit"],
        "recommended_explore_percent": tel["recommended_explore_percent"],
        "retire_at_hurt_count": tel["retire_at_hurt_count"],
    }
    reasons = dict(tel["count_reasons"])
    if errors:
        reasons["ordinary_skills"] = "快照来源不完整，不能确认完整方法数量。"
    return SectionProjection(
        items,
        "partial" if (errors or tel["coverage"]["status"] != "complete") else "complete",
        errors=errors,
        source="SkillSnapshot.enabled_entries",
        counts=counts,
        count_labels=_SKILL_TELEMETRY_LABELS,
        count_reasons=reasons,
        summary=_telemetry_summary_text(tel),
        locations=locations,
    )


# LLM: 完整管理员判据之后，只取已绑定服务的base_roots；不调用插件根、正文发现或快照刷新。
# 函数用途: 给方法根的原读写裁决，重复来源根保持分别可见。
def _skill_locations(context):
    if not context.admin:
        return ()
    service = context.agent.skills_service
    roots = service.base_roots(context.agent.tools.workspace_root)
    extras = set(getattr(service, "_extra_roots", ()) or ())
    paths = {}
    for index, root in enumerate(roots):
        if root.source in paths and root.path not in extras and not root.path.exists():
            continue
        identity = root.source if root.source not in paths else f"{root.source}:{index}"
        paths[identity] = root.path
    return location_items(context, paths)


# LLM: 普通快照只给读取建议，不验证正文；托管自学Skill沿原生命周期公开读口复核撤回，不reload或读取版本正文。
# 函数用途: 保留方法用途和稳定引用，同时准确标注停用、删除或来源不可用。
def _skill_item(entry, metadata=None):
    fields = {"source_layer": entry.source, "summary": entry.description, "category": entry.category,
              "recommended": entry.recommended, "body_observed": "not_observed", "readable": None}
    state = "known"
    if entry.frozen_path:
        if metadata is not None:
            fields.update(lifecycle=metadata, readable=metadata["authorized"])
            state = "known" if metadata["authorized"] else "revoked"
        else:
            state = "unavailable"
    if state == "known":
        fields["next_read"] = next_read("skill_search", {"action": "get", "skill_id": entry.stable_id})
    return map_item(entry.stable_id, entry.name, state=state, fields=fields)
