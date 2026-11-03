"""管理员只读聚合：消费原生结构化来源，失源保留未知，不建立第二事实库。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ..plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers


# LLM: 未观察到时间时保留null，不把缺值换成当前时间。
# 函数用途: 转为标准可空时间。
def timestamp(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value > 0 else None


# LLM: 原生来源异常只披露稳定错误码，异常正文可能含秘密。
# 类用途: 收集一次只读模块的数据覆盖。
class ModuleRead:
    # LLM: 仅初始化内存观察账。
    # 函数用途: 绑定模块名和失源列表。
    def __init__(self, module):
        self.module = module
        self.unavailable = []

    # LLM: 空列表只有携带unavailable时才表示失源，不伪报全量成功。
    # 函数用途: 独立读取一个来源并保留故障。
    def read(self, source, operation, missing=None):
        try:
            return operation()
        except Exception:
            self.unavailable.append({"module": self.module, "source": source, "reason_code": "HOST_NOT_READY"})
            return missing

    # LLM: 缺宿主属性是未知，不使用猜测的默认数值。
    # 函数用途: 记录不可用的属性。
    def value(self, source, value):
        if value is None:
            self.unavailable.append({"module": self.module, "source": source, "reason_code": "HOST_NOT_READY"})
        return value

    # LLM: 覆盖账与数据一并返回。
    # 函数用途: 组成模块结果。
    def result(self, data):
        return {"data": data, "coverage": "partial" if self.unavailable else "complete", "unavailable": self.unavailable}


# LLM: home只来自宿主，不接收请求中的路径。
# 函数用途: 解析规范主owner路径。
def main_home_paths(server):
    from ..user_space.home_layout import home_paths
    from ..user_space.owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home
    root = server.agent.home_paths.root
    return home_paths_with_owner(home_paths(root), resolve_owner_home(root, OwnerIdentity.local_main()))


# LLM: 只读查询不得初始化缺失的运行库。
# 函数用途: 打开已有RuntimeRepository。
def runtime_repository(home, *, read_only=True):
    from ..runtime_db.repository import RuntimeRepository
    from ..runtime_db.schema import runtime_db_path
    path = runtime_db_path(Path(home))
    if not path.is_file():
        raise FileNotFoundError("runtime unavailable")
    return RuntimeRepository(path, read_only=read_only, existing_only=not read_only)


# LLM: 报告包含源错误时不能把部分列表当完整。
# 函数用途: 检查原生数据报告。
def report_value(report):
    value, errors = report
    if errors:
        raise ValueError("source partial")
    return value


# LLM: 只调用已脱敏的公共模型读口。
# 函数用途: 分页读取模型和服务商目录。
def model_state(server):
    reader = ModelSettingsV2Handlers(main_home_paths(server))
    profiles, cursor = [], None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        page = reader.business_profiles(params)
        profiles.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor or len(profiles) >= 200:
            break
    providers = reader.list_providers({"capability_filter": "all"})
    truncated = bool(cursor) or len(providers["providers"]) > 100 or any(len(row["models"]) > 200 for row in providers["providers"])
    return profiles[:200], providers, truncated


# LLM: 只读宿主与原生查询；近期活动先白名单投影再统一脱敏，不让任务goal或未来来源字段泄密。
# 函数用途: 聚合系统状态。
def module_system(server):
    from ..common.log_redaction import redact_sensitive_value
    read = ModuleRead("system")
    query = server.admin_query
    sessions = read.read("admin_query.sessions", lambda: query.get_all_sessions(query.ADMIN_USER))
    tasks = read.read("admin_query.tasks", lambda: report_value(query.get_all_tasks_report(query.ADMIN_USER)))
    if tasks is not None and len(tasks) >= 100:
        tasks = read.value("admin_query.tasks.truncated", None)
    activity = read.read("admin_query.activity", lambda: report_value(query.get_recent_activity_report(query.ADMIN_USER)), [])
    activity = redact_sensitive_value([{key: row[key] for key in (
        "type", "session_id", "channel", "active", "timestamp", "task_id", "status", "goal") if key in row}
        for row in activity[:20]])
    models = read.read("model_profiles", lambda: model_state(server))
    if models and models[2]:
        read.value("model_profiles.truncated", None)
    gateway = {"pid": os.getpid(), "started_at": timestamp(read.value("gateway.started_at", getattr(server, "started_at", None))),
        "multiprocess": bool(server.control_plane is not None and server.control_plane.worker_count > 0),
        "queue_depth": read.value("gateway.queue_depth", getattr(server, "queue_depth", None))}
    return read.result({"gateway": gateway, "sessions_total": len(sessions) if sessions is not None else None,
        "tasks": {name: sum(str(row.get("status", "")).lower() == name for row in tasks) if tasks is not None else None
                  for name in ("running", "queued")}, "recent_activity": activity,
        "business_spaces_enabled": bool(getattr(server.agent.config, "business_spaces_enabled", False)),
        "model_configured": bool(models and models[0]),
        "default_model_profile_id": models[1]["selected_profile_id"] if models else None})


# LLM: URL仅输出hostname，凭据、头值和查询串都不输出。
# 函数用途: 投影模型与安全路由配置。
def module_models(server):
    read = ModuleRead("models")
    state = read.read("model_profiles", lambda: model_state(server))
    profiles, providers, truncated = state if state else ([], {"providers": [], "selected_profile_id": None}, False)
    if truncated:
        read.value("model_profiles.truncated", None)
    routing = {key: getattr(server.agent.config, key) for key in ("model_routing_enabled", "default_model_profile_id", "decision_model_profile_id", "embedding_model_profile_id")
               if hasattr(server.agent.config, key)}
    return read.result({"profiles": profiles, "providers": [{"provider_id": row["provider_id"],
        "kind": next(iter(kinds)) if len(kinds := {model["model_backend"] for model in row["models"]}) == 1 else "mixed_or_unknown",
        "base_url_host": urlsplit(row["api_base"]).hostname,
        "has_secret": row["has_api_key"], "models": [model["model_name"] for model in row["models"]][:200]}
        for row in providers["providers"][:100]], "default_model_profile_id": providers["selected_profile_id"], "routing": routing})


# LLM: effect只读canonical resolver默认值；动态策略显式标注default而非假装一次调用的已解析effect。
# 函数用途: 投影单工具元数据，缺列逐项保留unavailable。
def tool_metadata(row, read):
    spec = row.model_spec
    prefix = "agent.tools." + spec.name + "."
    hints = getattr(spec, "hints", None)
    resolver = getattr(row.runtime_policy, "effect_resolver", None)
    effect = read.value(prefix + "effect", getattr(resolver, "default_effect", None))
    if effect is not None and (resolver.strategy == "command" or resolver.by_parameter or resolver.by_parameter_combinations):
        effect = "dynamic(default=" + effect + ")"
    return {"name": spec.name, "category": read.value(prefix + "category", getattr(hints, "category", None)),
        "effect": effect, "default_deferred": read.value(prefix + "default_deferred", getattr(hints, "default_deferred", None))}


# LLM: 工具来自原注册表，不以推荐卡片替代目录；审批超过公开上限才截断并披露覆盖缺口。
# 函数用途: 聚合工具元数据和当前scope审批。
def module_tools(server, *, handler=None):
    from ..auth.middleware import extract_user_from_request
    from ..user_space.approval_mode import read_permission_mode
    from .admin_control_service import pending_tool_approvals
    from .control_service import GatewayControlScope
    read = ModuleRead("tools")
    rows = read.read("agent.tools", lambda: list(server.agent.tools.tools.values()), [])
    if len(rows) > 1000:
        read.value("agent.tools.truncated", None)
    user, channel = extract_user_from_request(handler) if handler is not None else ("admin", "chat")
    scope = GatewayControlScope(user_id=user, channel=channel, conversation_id="", all_user_access=True)
    pending = read.read("pending_tool_approvals", lambda: pending_tool_approvals(server.agent, server.paths, scope), [])
    if len(pending) > 100:
        read.value("pending_tool_approvals.truncated", None)
    return read.result({"tools": [tool_metadata(row, read) for row in rows[:1000]],
        "approval_mode": read.read("approval_mode", lambda: read_permission_mode(main_home_paths(server))),
        "pending_approvals": [{"request_id": row.request_id, "tool_name": row.request.tool_name} for row in pending[:100]]})


# LLM: 只核当前attempt的退出证据，心跳年龄不能授予回收资格。
# 函数用途: 读取规范子代理快照，读写共用资格判断。
def runtime_run_rows(server):
    from ..runtime_db.executor_liveness import executor_exit_reason
    repo = runtime_repository(main_home_paths(server).owner_home_dir)
    with repo._runtime_connection() as conn:
        rows = conn.execute("SELECT ar.*, aa.status AS attempt_status, aa.started_at, aa.metadata_json, tr.task_id "
            "FROM agent_runs ar JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id "
            "JOIN task_runs tr ON tr.task_run_id=ar.task_run_id WHERE ar.parent_agent_run_id!='' "
            "AND tr.closed_at=0 AND aa.status='running' ORDER BY ar.created_at DESC LIMIT 501").fetchall()
    if len(rows) > 500:
        raise ValueError("runtime snapshot exceeds public bound")
    result = []
    for raw in rows:
        row = dict(raw)
        metadata = json.loads(row["metadata_json"])
        row["reason"] = executor_exit_reason(metadata) or None
        row["last_heartbeat_at"] = timestamp(metadata.get("last_heartbeat_at"))
        result.append(row)
    return result


# LLM: 读取原指标对象，不解析渲染文本，也不启动执行器。
# 函数用途: 获取并发指标快照。
def concurrency_snapshot():
    from ..observability.concurrency_metrics import _metrics, ensure_concurrency_metrics_registered
    ensure_concurrency_metrics_registered()
    metrics = _metrics()
    result = {}
    for key in ("llm_inflight", "gateway_workers_busy", "subagent_runners_inflight"):
        metric = getattr(metrics, key)
        with metric._lock:
            result[key] = [{"labels": dict(labels), "value": value} for labels, value in metric._vals.items()]
    return result


# LLM: 可回收列表与写口共用规范快照，不读子代理正文。
# 函数用途: 投影运行子代理和并发占用。
def module_subagents(server):
    read = ModuleRead("subagents")
    rows = read.read("runtime_db", lambda: runtime_run_rows(server), [])
    public = [{"run_id": row["run_id"], "agent_run_id": row["agent_run_id"], "task_id": row["task_id"],
        "status": row["attempt_status"], "started_at": timestamp(row["started_at"]),
        "last_heartbeat_at": row["last_heartbeat_at"], "reason": row["reason"]} for row in rows]
    return read.result({"running": public, "reclaimable": [row for row in public if row["reason"]],
        "concurrency": read.read("concurrency_metrics", concurrency_snapshot, {})})


# LLM: 列表是原候选库的结构化状态，不解析展示文案，不触发preview/confirm。
# 函数用途: 分页统计仍待审核的记忆候选。
def memory_pending(server):
    from types import SimpleNamespace

    from .memory_candidate_review import run_memory_candidate_control
    offset, pending = 0, 0
    while True:
        command = SimpleNamespace(operation="list", target_id=str(offset))
        result = run_memory_candidate_control(server.agent, server.paths, command, None, "")
        if not result.ok:
            raise ValueError("memory unavailable")
        details = result.details
        pending += sum(row["status"] in {"observed", "pending_review", "approved"} for row in details["candidates"])
        offset += len(details["candidates"])
        if offset >= details["count"]:
            return pending
        if not details["candidates"]:
            raise ValueError("memory incomplete")


# LLM: 自学习读口必须保持refill=False，不能因查看页面补写请求队列。
# 函数用途: 聚合记忆候选、自学习积压与使用量。
def module_memory(server):
    from ..capability.skill_learning_store import SkillLearningStore
    read = ModuleRead("memory")
    learning = SkillLearningStore.for_home(main_home_paths(server))
    return read.result({"memory_candidates_pending": read.read("memory_candidates", lambda: memory_pending(server)),
        "learning_requests_pending": read.read("learning.requests", lambda: len(learning.pending_requests(refill=False))),
        "learning_overflow": read.read("learning.overflow", lambda: len(learning.overflow_requests())),
        "learning_usage": read.read("learning.usage", learning.usage_summary)})


# LLM: 目录只按原生root清单作一层枚举，不能无界rglob或跟随符号链接。
# 函数用途: 统计原生Skill根中可见的直接Skill文件。
def skill_layer_count(roots):
    count = 0
    for root in dict.fromkeys(roots):
        if root.is_symlink():
            raise ValueError("skill root unavailable")
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink() and (child / "SKILL.md").is_file():
                count += 1
    return count


# LLM: learned所有权只从原登记取得，不能根据目录名猜自学身份。
# 函数用途: 聚合四层Skill和自学登记。
def module_skills(server):
    from ..capability.skill_learning_store import SkillLearningStore
    from ..capability.skills import skill_catalog_roots
    read = ModuleRead("skills")
    home = main_home_paths(server)
    builtin = Path(__file__).parents[2] / "skills"
    roots = skill_catalog_roots(home.owner_home_dir, extra_roots=((builtin, "builtin"),))
    learned_root = Path(home.owner_home_dir) / "skills" / "learned"
    groups = {"workspace": [], "user": [], "builtin": [], "learned": [learned_root]}
    for root, source in roots:
        if root == learned_root:
            continue
        layer = "user" if source == "owner" else source
        if layer in groups:
            groups[layer].append(root)
    layers = [{"layer": layer, "root_ref": str(paths[0]) if paths else layer,
        "skill_count": read.read("skills." + layer, lambda: skill_layer_count(paths))} for layer, paths in groups.items()]
    registry = read.read("learning.registry", lambda: SkillLearningStore.for_home(home).load_registry())
    learned = [{"name": row.name, "version": row.version, "revision": row.revision,
                "sha256": row.sha256, "updated_at": row.updated_at} for row in registry.skills.values()] if registry else []
    if len(learned) > 500:
        read.value("learning.registry.truncated", None)
    return read.result({"layers": layers, "learned": learned[:500]})


# LLM: 仅读已绑定的原仓储，不能为一个只读页面构造会建目录的SchedulerRepository。
# 函数用途: 获取调度公共只读列表。
def schedule_jobs(server):
    return report_value(server.agent.scheduler_repository.list_jobs())


# LLM: 缺源时计数是null，而不是误导门户的零。
# 函数用途: 聚合持久计划及状态计数。
def module_schedules(server):
    from ..common.log_redaction import redact_sensitive_value
    read = ModuleRead("schedules")
    jobs = read.read("scheduler_repository", lambda: schedule_jobs(server))
    if jobs is not None and len(jobs) > 500:
        read.value("scheduler_repository.truncated", None)
    counts = {"active": None, "paused": None, "failed_last_run": None}
    if jobs is not None:
        counts = {"active": sum(row.get("status") == "active" for row in jobs),
            "paused": sum(row.get("status") == "paused" for row in jobs),
            "failed_last_run": sum(row.get("last_run_status") == "failed" for row in jobs)}
    return read.result({"jobs": redact_sensitive_value((jobs or [])[:500]), "counts": counts})


# LLM: 适配器只使用宿主当前绑定的公共对象，不创建新客户端或读取token。
# 函数用途: 找到原生飞书适配器。
def feishu_adapter(server):
    return server.agent.channel_registry.adapter_for("feishu")


# LLM: 原通道摘要可能带近期消息，管理口仅返回计数和掩码应用ID。
# 函数用途: 聚合通道配置与活跃计数。
def module_channels(server):
    read = ModuleRead("channels")
    config = server.agent.config
    configured = bool(getattr(config, "feishu_app_id", "") and getattr(config, "feishu_app_secret", ""))
    rows = []
    for channel in ("chat", "feishu", "qq", "web"):
        summary = read.read("admin_query." + channel,
            lambda: server.admin_query.get_channel_summary(server.admin_query.ADMIN_USER, channel))
        if summary is not None and summary.get("load_errors"):
            read.value("admin_query." + channel, None)
            summary = None
        public = {key: summary[key] for key in ("session_count", "active_task_count")} if summary is not None else None
        if channel == "feishu" and configured:
            app_id = str(getattr(config, "feishu_app_id", ""))
            public = {"app_id_masked": (app_id[:3] + "***") if app_id else None, **(public or {})}
        rows.append({"channel": channel, "configured": configured if channel == "feishu" else summary is not None, "summary": public})
    return read.result({"channels": rows})


# LLM: 账号只从LocalUserStore获取，主owner另用规范身份；绝不从请求拼路径。
# 函数用途: 列出可管理的规范owner根。
def owner_homes(server):
    from ..user_space.local_user_store import LocalUserStore
    from ..user_space.owner_resolver import resolve_owner_home
    home = main_home_paths(server)
    return [("local/main", Path(home.owner_home_dir)),
        *[(resolve_owner_home(home.root, user.owner).owner_id, resolve_owner_home(home.root, user.owner).home_dir) for user in LocalUserStore(home.root).list_users()]]


# LLM: 配额与workspace计数保留逐字段失源；未知limit不能写成无限或零。
# 函数用途: 读取单个owner配额与规范binding数量。
def owner_quota_view(server, owner_id, owner_home, read):
    from ..user_space.owner_quota import (
        owner_logical_usage_bytes,
        owner_quota_enabled,
        owner_quota_reconcile_pending,
        read_local_user_quota,
    )
    prefix = "quota." + owner_id
    limits = read.read(prefix + ".limits", lambda: read_local_user_quota(main_home_paths(server).root, owner_home))
    # LLM: 查询纯SQL计数，不建立workspace索引或缓存。
    # 函数用途: 取得当前owner的已有binding数量。
    def binding_count():
        repo = runtime_repository(owner_home)
        with repo._runtime_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM workspace_bindings WHERE owner_id=?", (owner_id,)).fetchone()[0]
    return {"owner_id": owner_id,
        "quota_enabled": read.read(prefix + ".enabled", lambda: owner_quota_enabled(owner_home)),
        "used_bytes": read.read(prefix + ".usage", lambda: owner_logical_usage_bytes(owner_home)),
        "limit_bytes": limits[0].max_disk_mb * 1024 * 1024 if limits else None,
        "reconcile_pending": read.read(prefix + ".pending", lambda: owner_quota_reconcile_pending(owner_home)),
        "workspace_bindings": read.read(prefix + ".bindings", binding_count)}


# LLM: owner列表失源也要披露，不能返回空完整状态。
# 函数用途: 聚合工作区及配额。
def module_workspace(server):
    read = ModuleRead("workspace")
    owners = read.read("local_users", lambda: owner_homes(server), [])
    if len(owners) > 500:
        read.value("local_users.truncated", None)
    return read.result({"owners": [owner_quota_view(server, owner_id, home, read) for owner_id, home in owners[:500]]})


# LLM: 审计与业务audit.list共用唯一聚合读口。
# 函数用途: 查询host审计并保持原coverage和unavailable。
def audit_page(server, *, handler=None, since=None, actor=None, cursor=None, limit=50):
    from ..business_spaces.v13_audit import audit_records
    home = main_home_paths(server)
    page = audit_records(home.owner_home_dir, "local/main", scope="host", since=since, until=None,
                         actor=actor, cursor=cursor, limit=limit, admin_context=handler, host=server.agent)
    return {"data": {key: page[key] for key in ("records", "next_cursor", "coverage")},
            "coverage": page["coverage"], "unavailable": page["unavailable"]}


# LLM: 缺源通过ModuleRead明示，游标错误由查询专用入口原样映射。
# 函数用途: 提供审计模块首页。
def module_audit(server, *, handler=None):
    read = ModuleRead("audit")
    page = read.read("audit_records", lambda: audit_page(server, handler=handler))
    return page if page is not None else read.result({"records": [], "next_cursor": None, "coverage": "unknown"})


MODULE_READERS = {"system": module_system, "models": module_models, "tools": module_tools,
    "subagents": module_subagents, "memory": module_memory, "skills": module_skills,
    "schedules": module_schedules, "channels": module_channels, "workspace": module_workspace, "audit": module_audit}
