# LLM: 当前owner的Home与Memory只读诊断由DoctorService统一；memory工厂由CLI显式注入，不反向依赖CLI。
# 模块用途: 聚合Home健康与两个记忆诊断facet，不修复、不迁移、不调用模型。

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..memory_archive import compression_snapshot_dir, raw_event_path_for, snapshot_path_for
from .capability_requests import list_capability_requests_report
from .home_backup import latest_home_backup_snapshots_report
from .home_indexes import dangling_index_refs
from .home_layout import MyAgentHomePaths, resolve_route_index_target
from .home_retention import plan_owner_retention
from .home_runtime_query import home_runtime_status
from .owner_policy import read_owner_policy_bundle_report
from .temporary_grants import list_temporary_grants


def build_home_doctor_report(home: MyAgentHomePaths) -> dict[str, Any]:
    sections = _collect_doctor_sections(home)
    findings = build_doctor_findings(sections)
    return _doctor_report(home, sections, findings)


def _collect_doctor_sections(home: MyAgentHomePaths) -> dict[str, Any]:
    home_status = home_runtime_status(home)
    dangling = dangling_index_refs(home)
    retention = plan_owner_retention(home)
    schema = _schema_payload(home.system_schema_version_json)
    backup = _backup_payload(home)
    owner_policy = _owner_policy_payload(home)
    capability_requests = _capability_requests_payload(home)
    temporary_grants = _temporary_grants_payload(home)
    return {
        "home": home_status,
        "dangling": dangling,
        "retention": retention,
        "schema": schema,
        "backup": backup,
        "owner_policy": owner_policy,
        "capability_requests": capability_requests,
        "temporary_grants": temporary_grants,
    }


def _doctor_report(home: MyAgentHomePaths, sections: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "home": sections["home"],
        "schema": sections["schema"],
        "backup": sections["backup"],
        "owner_policy": sections["owner_policy"],
        "capability_requests": sections["capability_requests"],
        "temporary_grants": sections["temporary_grants"],
        "indexes": {
            "dangling_count": len(sections["dangling"]),
            "dangling_refs": sections["dangling"],
        },
        "retention": {
            "planned_count": len(sections["retention"].actions),
            "actions": [action.to_dict() for action in sections["retention"].actions],
        },
        "repair_plan": repair_plan(findings),
        "findings": findings,
    }


def build_doctor_findings(sections: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *_owner_lifecycle_findings(sections["home"]),
        *_dangling_findings(sections["dangling"]),
        *_retention_findings(sections["retention"].actions),
        *_schema_findings(sections["schema"]),
        *_backup_findings(sections["backup"]),
        *_owner_policy_findings(sections["owner_policy"]),
        *_capability_request_findings(sections["capability_requests"]),
        *_temporary_grant_findings(sections["temporary_grants"]),
    ]


def repair_plan(findings: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {"auto_repair": [], "warn": [], "manual": []}
    for finding in findings:
        resolution = finding.get("resolution") if isinstance(finding.get("resolution"), dict) else {}
        action_class = str(resolution.get("action_class") or "manual")
        if action_class not in grouped:
            action_class = "manual"
        grouped[action_class].append(
            {
                "kind": str(finding.get("kind") or ""),
                "message": str(finding.get("message") or ""),
                "command": str(resolution.get("command") or ""),
                "note": str(resolution.get("note") or ""),
            }
        )
    return {
        "auto_repair_count": len(grouped["auto_repair"]),
        "warn_count": len(grouped["warn"]),
        "manual_count": len(grouped["manual"]),
        "auto_repair": grouped["auto_repair"],
        "warn": grouped["warn"],
        "manual": grouped["manual"],
    }


def _owner_lifecycle_findings(home_status: dict[str, Any]) -> list[dict[str, Any]]:
    lifecycle = home_status.get("owner", {}).get("lifecycle", {})
    load_error = lifecycle.get("load_error") if isinstance(lifecycle, dict) else None
    if not isinstance(load_error, dict):
        return []
    return [
        {
            "kind": "owner_lifecycle_load_error",
            "path": load_error.get("path") or lifecycle.get("path", ""),
            "status": "UNKNOWN",
            "note": "owner lifecycle status is unreadable; do not treat this owner as confirmed active",
            "load_error": load_error,
        }
    ]


# LLM: 原调用方只消费建议，保持权限边界，不增加自动副作用。
# 函数用途: 生成悬空索引的规范重建建议，不执行重建或清理。
def _dangling_findings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "dangling_index",
            "severity": "warning",
            "message": "global index points to a missing path",
            "missing_path": str(record.get("missing_path") or ""),
            "resolution": _resolution(
                "auto_repair",
                command="my-agent home index rebuild --apply",
                note="rebuild owner/task/run/agent indexes from existing owner homes",
            ),
        }
        for record in records
    ]


# LLM: 原调用方只消费建议，保持权限边界，不增加自动副作用。
# 函数用途: 将保留候选格式化为规范命令建议，不执行删除。
def _retention_findings(actions: tuple[Any, ...]) -> list[dict[str, Any]]:
    if not actions:
        return []
    return [
        {
            "kind": "retention_candidate",
            "severity": "info",
            "message": "retention policy has expired files to clean",
            "count": len(actions),
            "resolution": _resolution(
                "auto_repair",
                command="my-agent home retention --apply",
                note="delete only files selected by the explicit owner retention policy",
            ),
        }
    ]


def _schema_findings(schema: dict[str, Any]) -> list[dict[str, Any]]:
    if schema.get("ok"):
        return []
    return [
        {
            "kind": "schema_version_unreadable",
            "severity": "warning",
            "message": str(schema.get("error") or "schema_version missing"),
            "path": str(schema.get("path") or ""),
            "resolution": _resolution(
                "manual",
                note="inspect schema_version.json or rerun home bootstrap before trusting schema status",
            ),
        }
    ]


def _backup_findings(backup: dict[str, Any]) -> list[dict[str, Any]]:
    if int(backup.get("snapshot_count") or 0) > 0:
        return []
    return [
        {
            "kind": "backup_snapshot_missing",
            "severity": "info",
            "message": "no owner-home snapshot backup has been recorded yet",
            "resolution": _resolution(
                "warn",
                note="create a snapshot before destructive maintenance or schema changes",
            ),
        }
    ]


def _owner_policy_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors = payload.get("load_errors") if isinstance(payload.get("load_errors"), list) else []
    return [
        {
            "kind": "owner_policy_load_error",
            "severity": "warning",
            "message": "owner policy file could not be read; defaults may be incomplete",
            "context": str(error.get("context") or ""),
            "path": str(error.get("path") or ""),
            "resolution": {
                "action_class": "manual",
                "note": "inspect the owner policy file before trusting permissions, quota, retention, skills or tools",
            },
        }
        for error in errors
        if isinstance(error, dict)
    ]


def _capability_request_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    count = int(payload.get("open_count") or 0)
    if count <= 0:
        return []
    return [
        {
            "kind": "capability_request_open",
            "severity": "info",
            "message": "owner has open capability requests",
            "count": count,
            "resolution": {
                "action_class": "manual",
                "note": "review or expire pending capability requests; do not auto-approve them",
            },
        }
    ]


def _temporary_grant_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    count = int(payload.get("active_count") or 0)
    if count <= 0:
        return []
    return [
        {
            "kind": "temporary_grant_active",
            "severity": "info",
            "message": "owner has active temporary grants",
            "count": count,
            "resolution": _resolution(
                "warn",
                note="verify active grants are still intended; expired grants are handled by retention/doctor lifecycle",
            ),
        }
    ]


def _resolution(action_class: str, *, command: str = "", note: str = "") -> dict[str, str]:
    payload = {"action_class": action_class, "note": note}
    if command:
        payload["command"] = command
    return payload


def _schema_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(path), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "path": str(path),
        "ok": isinstance(payload, dict) and bool(payload.get("schema_version")),
        "payload": payload if isinstance(payload, dict) else {},
    }


def _backup_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    report = latest_home_backup_snapshots_report(home, limit=5)
    snapshots = report.snapshots
    return {
        "snapshot_count": len(snapshots),
        "latest": snapshots[0] if snapshots else {},
        "snapshots": snapshots,
        "load_errors": report.load_errors,
    }


def _owner_policy_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    report = read_owner_policy_bundle_report(home)
    return {
        "ok": not report.load_errors,
        "load_errors": list(report.load_errors),
    }


def _capability_requests_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    report = list_capability_requests_report(home)
    rows = report.requests
    open_rows = [row for row in rows if row.status == "open"]
    return {
        "total_count": len(rows),
        "open_count": len(open_rows),
        "load_errors": report.load_errors,
        "open": [
            {
                "request_id": row.request_id,
                "capability": row.capability,
                "requested_by": row.requested_by,
                "task_id": row.task_id,
                "expires_at": row.expires_at,
            }
            for row in open_rows
        ],
    }


def _temporary_grants_payload(home: MyAgentHomePaths) -> dict[str, Any]:
    rows = list_temporary_grants(home)
    active_rows = [row for row in rows if row.status == "active"]
    return {
        "total_count": len(rows),
        "active_count": len(active_rows),
        "active": [
            {
                "grant_id": row.grant_id,
                "capability": row.capability,
                "granted_to": row.granted_to,
                "path_prefix": row.path_prefix,
                "expires_at": row.expires_at,
            }
            for row in active_rows
        ],
    }


__all__ = ["build_home_doctor_report", "DoctorService"]


# 记忆体检抽查近期归档时最多读取的归档文件数量。
MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_COUNT = 5

# LLM: 接受字典、带 to_dict 的对象、数据类或普通值；只转换展示形状，不更改警告语义或执行修复。
# 函数用途: 将一种配置警告转换为报告字典。
def _normalize_warning_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if hasattr(item, "__dataclass_fields__"):
        return asdict(item)
    return {"message": str(item)}

# LLM: 仅消费加载器已记录的 memory_config_warnings，缺失按空列表处理；不重新校验或修改配置。
# 函数用途: 汇集配置对象中的记忆警告并统一其展示格式。
def _config_warnings(config: object) -> list[dict[str, Any]]:
    warnings = getattr(config, "memory_config_warnings", []) or []
    return [_normalize_warning_item(item) for item in warnings]

# LLM: 只投影四项记忆诊断配置；缺属性仍抛错，不伪造默认值，也不包含通道凭据。
# 函数用途: 提取归档、钩子、路由模式和自动读取限额。
def _memory_config_payload(config: object) -> dict[str, Any]:
    fields = [
        "memory_archive_level",
        "memory_hook_enabled",
        "memory_rule_routing_mode",
        "memory_rule_auto_read_limit",
    ]
    return {field: getattr(config, field) for field in fields}

# LLM: 从显式索引只读加载路由并按传入根验证；加载失败保留 load_error，extended=False 省去旧视图没有的两项字段。
# 函数用途: 生成路由索引状态、路由详情及校验警告。
def _build_routing_doctor(root: Path, index_path: Path, *, extended: bool = True) -> dict[str, Any]:
    from ..memory_routing import load_routes, validate_routes

    payload: dict[str, Any] = {
        "index": _index_payload(index_path),
        "routes": [],
        "route_count": 0,
        "validation_warnings": [],
        "load_error": "",
    }
    if not index_path.exists():
        payload["load_error"] = f"memory route index not found: {index_path}"
        return payload
    try:
        routes = load_routes(index_path)
    except Exception as exc:
        payload["load_error"] = f"{type(exc).__name__}: {exc}"
        return payload
    payload["routes"] = [_route_payload(route) for route in routes]
    if not extended:
        for item in payload["routes"]:
            item.pop("source_file", None)
            item.pop("inject_mode", None)
    payload["route_count"] = len(routes)
    payload["validation_warnings"] = validate_routes(routes, root)
    return payload

# LLM: 仅检查指定根的 hook/raw 与可选快照；include_snapshots=False 保持旧 runtime 字段，不创建目录或归档。
# 函数用途: 聚合归档保留配置、文件统计和快照一致性警告。
def _build_archive_doctor(root: Path, config: object, *, include_snapshots: bool = True) -> dict[str, Any]:
    hook_today_path = snapshot_path_for(root)
    raw_today_path = raw_event_path_for(root)
    result = {
        # 参数减量第 1 批（2026-09-27）：memory_hook_retention_days 没有清理器读取，已从配置删除，不再回显。
        "retention": {
            "memory_hook_enabled": bool(getattr(config, "memory_hook_enabled", True)),
            "memory_archive_level": int(getattr(config, "memory_archive_level", 3)),
        },
        "hook": _archive_dir_payload(hook_today_path.parent, hook_today_path, config=config),
        "raw": _archive_dir_payload(raw_today_path.parent, raw_today_path, config=config),
    }
    if include_snapshots:
        snapshot_dir = compression_snapshot_dir(root)
        result["snapshots"] = _snapshot_dir_payload(snapshot_dir, config=config)
        result["consistency_warnings"] = _archive_consistency_warnings(hook_today_path.parent, snapshot_dir)
    return result

# LLM: 只统计目录内普通 JSONL 文件，按修改时间和名称倒序保留最近五项；不读正文，缺目录不创建。
# 函数用途: 返回归档目录存在性、文件总数、当日路径与近期文件元数据。
def _archive_dir_payload(directory: Path, today_path: Path, *, config: object | None = None) -> dict[str, Any]:
    files = sorted(
        [path for path in directory.glob("*.jsonl") if path.is_file()] if directory.exists() else [],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return {
        "dir": str(directory),
        "exists": directory.exists(),
        "file_count": len(files),
        "today_path": str(today_path),
        "recent_files": [
            _archive_file_payload(path)
            for path in files[:MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_COUNT]
        ],
    }

# LLM: 对已选文件只做 stat，不读记忆正文；文件消失或不可读的异常沿原调用链暴露。
# 函数用途: 投影归档文件的名称、路径、大小与修改时间。
def _archive_file_payload(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }

# LLM: 检查普通 JSON 快照是否可读且顶层为对象；坏文件单列路径，不修复或输出正文，最近五项仅带元数据。
# 函数用途: 汇总快照目录文件数、无效文件及近期快照。
def _snapshot_dir_payload(directory: Path, *, config: object) -> dict[str, Any]:
    files = sorted(
        [path for path in directory.glob("*.json") if path.is_file()] if directory.exists() else [],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    invalid_json_files: list[str] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid_json_files.append(str(path))
            continue
        if not isinstance(payload, dict):
            invalid_json_files.append(str(path))
    return {
        "dir": str(directory),
        "exists": directory.exists(),
        "file_count": len(files),
        "invalid_json_files": invalid_json_files,
        "recent_files": [
            _archive_file_payload(path)
            for path in files[:MEMORY_DOCTOR_RECENT_ARCHIVE_FILE_COUNT]
        ],
    }

# LLM: 仅比较 hook JSONL 与快照 JSON 两层是否各有文件；这是存在性提示，不证明内容一致，不执行补齐。
# 函数用途: 报告钩子记录和压缩快照单边缺失的情况。
def _archive_consistency_warnings(hook_dir: Path, snapshot_dir: Path) -> list[str]:
    warnings: list[str] = []
    hook_exists = hook_dir.exists() and any(hook_dir.glob("*.jsonl"))
    snapshot_exists = snapshot_dir.exists() and any(snapshot_dir.glob("*.json"))
    if hook_exists and not snapshot_exists:
        warnings.append("hook layer has records but memory_archive/snapshots is empty")
    if snapshot_exists and not hook_exists:
        warnings.append("snapshot JSON files exist but hook JSONL layer is empty")
    return warnings

# LLM: 对传入的索引路径只查询 exists/is_file，不读取内容，也不把存在性等同于有效路由。
# 函数用途: 生成索引路径及文件类型的诊断字段。
def _index_payload(index_path: Path) -> dict[str, Any]:
    return {
        "path": str(index_path),
        "exists": index_path.exists(),
        "is_file": index_path.is_file(),
    }

# LLM: 原样投影已加载 Route 的元数据，不解析目标正文或改变读取策略；旧 runtime 字段裁剪由调用方负责。
# 函数用途: 序列化一条路由的触发、来源、范围和时效字段。
def _route_payload(route) -> dict[str, Any]:
    return {
        "route_id": route.route_id,
        "topic": route.topic,
        "trigger_keywords": route.trigger_keywords,
        "related_terms": route.related_terms,
        "when_to_read": route.when_to_read,
        "authority_path": route.authority_path,
        "source_file": route.source_file,
        "inject_mode": route.inject_mode,
        "scope": route.scope,
        "priority": route.priority,
        "stale_check": route.stale_check,
        "last_verified_at": route.last_verified_at,
        "source_path": route.source_path,
    }

# LLM: 当前 Agent 决定 owner 边界，迁移和保留期工厂由 CLI 注入；请求内缓存只读事实，不触发 apply、模型或修复。
# 类用途: 统一 runtime/memory 诊断投影并隔离公开报告与内部缓存。
class DoctorService:
    # LLM: 保存显式当前Agent与请求级缓存，不初始化任何持久服务或目录。
    # 函数用途: 建立一次只读诊断请求。
    def __init__(self, agent, *, migration_factory=None, retention_factory=None):
        self.agent = agent
        self.migration_factory = migration_factory
        self.retention_factory = retention_factory
        self._common_cache = {}

    # LLM: 公开报告必须与请求内缓存隔离；外部修改返回字典不能改变后续健康判据。
    # 函数用途: 返回一次诊断的独立快照，不暴露内部可变引用。
    def report(self, args, *, scope="memory", legacy=False):
        return deepcopy(self._report(args, scope=scope, legacy=legacy))

    # LLM: scope 与 v1 只接受封闭值；all 公共字段始终用 runtime/memory 双键，即使值相等也不缩形；v1 不调用记忆维护服务。
    # 函数用途: 选择原视图或稳定的双视图聚合，不改变默认 memory 字段与成功判据。
    def _report(self, args, *, scope="memory", legacy=False):
        if scope not in {"runtime", "memory", "all"}:
            raise ValueError("未知诊断范围")
        if legacy:
            return self._runtime(args)
        if scope == "memory":
            return self._memory(args)
        if scope == "runtime":
            facet = self._runtime(args)
            return {"ok": facet["ok"], "scope": "runtime", "runtime": facet}
        runtime, memory = self._runtime(args), self._memory(args)
        common = {}
        for key in ("config", "routing", "archive"):
            runtime_value, memory_value = runtime.pop(key), memory.pop(key)
            common[key] = {"runtime": runtime_value, "memory": memory_value}
        runtime_warnings, memory_warnings = runtime.pop("warnings"), memory.pop("config_warnings")
        common["warnings"] = {"runtime": runtime_warnings, "memory": memory_warnings}
        return {"ok": runtime["ok"] and memory["ok"], "scope": "all", "common": common,
                "runtime": runtime, "memory": memory}

    # LLM: validation_root和archive_scope分别沿旧两条入口的事实，不为去重而扩大读取范围。
    # 函数用途: 每个精确范围只生成一份公共配置、路由和归档结果。
    def _common(self, args, *, runtime):
        agent = self.agent
        target = resolve_route_index_target(agent.root, getattr(args, "index", None), home_paths=getattr(agent, "home_paths", None))
        root = target.authority_root if runtime else agent.root
        key = (str(getattr(agent.home_paths, "owner_id", "")), str(root), str(target.path), runtime)
        if key not in self._common_cache:
            self._common_cache[key] = {
                "config": _memory_config_payload(agent.config),
                "warnings": _config_warnings(agent.config),
                "routing": _build_routing_doctor(root, target.path, extended=not runtime),
                "archive": _build_archive_doctor(agent.root, agent.config, include_snapshots=not runtime),
            }
        return self._common_cache[key]

    # LLM: runtime只使用原home诊断，ok仍只取路由加载状态；不初始化候选、策展或迁移。
    # 函数用途: 生成原平铺doctor的只读运行范围报告。
    def _runtime(self, args):
        common = self._common(args, runtime=True)
        home = build_home_doctor_report(self.agent.home_paths)
        return {"ok": not common["routing"]["load_error"], "workspace_root": str(self.agent.root),
                **common, "home": home["home"], "home_doctor": home}

    # LLM: memory只调用原服务的stats/status/plan，严禁apply、curator run、正式晋升或模型请求。
    # 函数用途: 生成现有memory doctor合同的完整只读报告。
    def _memory(self, args):
        agent = self.agent
        common = self._common(args, runtime=False)
        if self.migration_factory is None or self.retention_factory is None:
            raise ValueError("memory诊断依赖未装配，结果未知")
        migration = self.migration_factory(agent).plan()
        retention = self.retention_factory(agent).plan()
        return {
            "ok": not common["routing"]["load_error"] and migration.ok and retention.ok,
            "command": "memory doctor", "owner_id": str(getattr(agent.home_paths, "owner_id", "") or "local/main"),
            "workspace_root": str(agent.root), "config": common["config"], "config_warnings": common["warnings"],
            "candidate": agent.memory_candidates.stats(), "curator": agent.memory_curator.status(),
            "routing": common["routing"], "archive": common["archive"],
            "migration": migration.to_dict(), "retention": retention.to_dict(),
        }



# LLM: 显式 raw_index 与 owner 默认索引的优先级全部委托统一解析器；本函数不判断内容有效性或创建文件。
# 函数用途: 将工作根、可选索引和 Home 路径解析为诊断读取目标。
def _resolve_index_path(root: Path, raw_index: str | None, *, home_paths=None) -> Path:
    return resolve_route_index_target(root, raw_index, home_paths=home_paths).path
