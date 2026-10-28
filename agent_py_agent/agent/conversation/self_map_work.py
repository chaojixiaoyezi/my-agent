# LLM: 工作及专项目录只读原会话/调度和公开节点元信息；不执行工具，不另造代理树、凭据或调度策略账。
# 模块用途: 登记本 owner 与当前代理的工作元信息，正文和唤醒/维护副作用不进入目录。
from ..tooling.self_map_registry import SectionDescriptor, SectionProjection, map_item, next_read
from ..tooling.self_map_remote_peers import remote_peers_projection, restricted_owner_scope
from .goal_binding import goal_binding


# LLM: work保留原总览，节点和调度专项只进完整登记，避免无参调用额外读取专项来源。
# 函数用途: 接入工作状态及两个按需专项分块。
def register_work_section(registry):
    registry.register(SectionDescriptor("work", "工作状态", 100, work_projection, "项目工作位置、会话与受管任务状态",
                                        summary_reader=work_summary))
    registry.register(SectionDescriptor("remote_peers", "远端推送节点", 130, remote_peers_projection,
                                        "本Owner可用的单向推送节点别名，不读取凭据或联机", include_in_overview=False))
    registry.register(SectionDescriptor("schedules", "监控与定时任务", 150, schedules_projection,
                                        "监控绑定线程、同通道顺延及多通道告警摘要", include_in_overview=False))


# LLM: 仅消费已绑定当前线程/任务位置，不加载历史、Goal、调度或进程；未枚举维度不报零。
# 函数用途: 为总览提供轻量工作位置与原详情导航。
def work_summary(context):
    fields = {"kind": "current_binding", "thread_id": context.thread_id,
              "workspace": str(context.agent.tools.workspace_root),
              "tasks": str(context.agent.home_paths.owner_tasks_dir),
              "next_read": next_read("self_map", {"section": "work"})}
    return SectionProjection((map_item("current", "当前工作位置", fields=fields),), "partial",
                             source="bound current workspace / thread", count_known=False)


# LLM: 复用原读者及准入后的启用节点摘要；各来源失败保留安全码，不扫描项目或把元数据入口当业务数量。
# 函数用途: 组合现有合法读侧的元信息，不改变生命周期。
def work_projection(context):
    items, errors = [], []
    for reader in (_sessions, _goals, _schedules, _processes, _terminals, _remote_peers):
        try:
            rows, problems = reader(context)
            items.extend(rows)
            errors.extend(problems)
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            errors.append("SELF_MAP_WORK_SOURCE_UNAVAILABLE")
    items.append(map_item("provider_queue", "供应商排队", state="not_observed", fields={
        "kind": "provider_queue", "count": None, "scope": "current_owner_and_run",
        "summary": "原排队事件仅作当前运行展示；未提供可按当前身份读取的持久队列清单。"}))
    items.extend(map_item(name, title, fields={"kind": name, "count": None, "next_read": next_read(name, arguments)})
                 for name, title, arguments in (("list_agents", "代理树", {}), ("audit_records", "运行审计", {"topic": "requests"}))
                 if name in context.snapshot.available_tool_names)
    return SectionProjection(tuple(items), "partial" if errors else "complete", errors=tuple(sorted(set(errors))),
                             source="bound ConversationStore / SchedulerRepository / process and PTY registries", count_known=False,
                              summary="项目工作目录：" + str(context.agent.tools.workspace_root) + "；业务任务目录：" + str(context.agent.home_paths.owner_tasks_dir) + "。")


# LLM: 管理员列本owner全会话，普通人/子仅exact当前线程；复用原内部线程/归属过滤，不显示标题或正文。
# 函数用途: 读取会话清单，损坏记录不冒充空集。
def _sessions(context):
    from ..agent_core.orchestration.tools.list_owner_sessions import (
        _listable,
        _owner_home,
        _thread_channel,
    )

    threads = context.agent.conversation_store.threads
    rows, errors = threads.list_report(limit=0) if context.admin else ([], [])
    if not context.admin and context.thread_id:
        thread, error = threads.load_report(context.thread_id)
        rows, errors = ([thread] if thread else []), ([error] if error else [])
    items = [map_item("thread:" + thread.thread_id, thread.thread_id, state=thread.status,
             fields={"kind": "conversation", "channel": _thread_channel(thread), "current": thread.thread_id == context.thread_id,
                     "last_activity_at": thread.updated_at, "next_read": next_read("list_owner_sessions", {})})
             for thread in rows if _listable(thread, _owner_home(context.agent))]
    return items, ["SELF_MAP_SESSION_RECORD_UNREADABLE"] if errors else []


# LLM: Goal 绑定沿原精确解析，不用名字/父任务猜目标；只给状态、预算与原get入口，目标正文不自动展开。
# 函数用途: 读取当前代理的权威 Goal 元数据。
def _goals(context):
    if "get_goal" not in context.snapshot.available_tool_names:
        return [], []
    thread, task, goal_id, _ = goal_binding(context.agent)
    if not thread:
        return [map_item("goal:current", "当前 Goal", state="not_observed", fields={"kind": "goal"})], []
    goal = context.agent.conversation_store.goals.load(thread, goal_id=goal_id, task_id=task)
    return [map_item("goal:current", "当前 Goal", state=getattr(goal, "status", "empty"),
                     fields={"kind": "goal", "token_budget": getattr(goal, "token_budget", None),
                             "tokens_used": getattr(goal, "tokens_used", None), "next_read": next_read("get_goal", {})})], []


# LLM: 仅读已绑定owner仓库，群须先复核宿主actor；受限scope先拒绝，策略仅从原delivery映射，不输出收件人或prompt。
# 函数用途: 列定时任务绑定及投递通道元数据，不修改原job或通知绑定。
def _schedules(context):
    if restricted_owner_scope(context) or "schedule" not in context.snapshot.available_tool_names:
        return [], []
    if getattr(context.agent.home_paths, "owner_kind", "") == "group":
        from .host_actor import resolve_host_actor
        if not context.thread_id or resolve_host_actor(context.agent, context.thread_id) is None:
            raise ValueError("group actor unavailable")
    jobs, errors = context.agent.scheduler_repository.list_jobs()
    items, problems = [], ["SELF_MAP_SCHEDULE_RECORD_UNREADABLE"] if errors else []
    for job in jobs:
        policy = _delivery_summary(context, job)
        if policy["channels"] is None:
            problems.append("SELF_MAP_SCHEDULE_CHANNELS_UNAVAILABLE")
        items.append(map_item("schedule:" + job["job_id"], job["job_id"], state=job["status"], fields={
            "kind": "schedule", "next_run_at": job.get("next_run_at"),
            "bound_thread_id": job.get("source_thread_id") or job.get("thread_id"), "delivery_policy": policy,
            "channels_source": "explicit policy targets; origin uses bound channel",
            "next_read": next_read("schedule", {"action": "get", "job_id": job["job_id"]})}))
    return items, sorted(set(problems))


# LLM: 只用原_schedules的权限和错误合同，空授权面不回退父或全局仓库；没有副作用工具调用。
# 函数用途: 让监控专项与work使用相同的原仓库投影。
def schedules_projection(context):
    rows, errors = _schedules(context)
    return SectionProjection(tuple(rows), "partial" if errors else "complete", errors=tuple(errors),
                             source="current owner SchedulerRepository / original delivery policy")


# LLM: 原delivery是唯一权威；仅需binding_id补channel时读取原认证目录，不输出引用或地址，失效绑定保持未知。
# 函数用途: 映射同通道跟随及显式多线通道标签，不把配置当实际投递或外部可达证据。
def _delivery_summary(context, job):
    policy = job.get("delivery") or {"mode": "origin"}
    channels, unknown = set(), False
    for target in policy.get("targets", ()):
        if target.get("target_kind") == "local":
            channels.add("local")
        elif target.get("target_kind") == "channel_binding":
            channel = target.get("channel")
            if not channel:
                from ..scheduler.delivery_policy import available_delivery_targets
                key = "schedule_public_channels"
                if key not in context.memo:
                    context.memo[key] = {row["binding_id"]: row["channel"] for row in available_delivery_targets(context.agent)}
                channel = context.memo[key].get(target.get("binding_id"))
            if channel:
                channels.add(channel)
            else:
                unknown = True
    return {"mode": policy["mode"], "follow_active_thread": policy.get("follow_latest_channel_session", True),
            "channels": None if unknown else sorted(channels)}


# LLM: 节点读者先做child/隔离范围拒绝，再读公开字段；work只挂启用节点，不读第二份库存。
# 函数用途: 为原工作入口复用远端别名及其安全错误码。
def _remote_peers(context):
    projection = remote_peers_projection(context)
    return [row for row in projection.items if row["enabled"]], list(projection.errors)


# LLM: 当前线程范围不能扩成owner全部进程；没有绑定保持未观测，不读取系统PID或任务账本。
# 函数用途: 构造与原进程工具同形的可信范围。
def _run_scope(context):
    params = getattr(context.agent, "_current_run_params", None)
    attrs = getattr(params, "task_attributes", None) or {}
    return {"owner_id": context.agent.home_paths.owner_id, "owner_home": str(context.agent.home_paths.owner_home_dir),
            "session_id": context.thread_id, "root_task_id": attrs.get("conversation_task_id", ""),
            "run_id": getattr(params, "run_id", "")}


# LLM: 原list_report会刷新持久记录；目录不能调用它或直接越过scope读盘，也不将旧status当本次活性。
# 函数用途: 明确后台进程未观测，并给原生命周期工具的读取入口。
def _processes(context):
    if "process_session" not in context.snapshot.available_tool_names:
        return [], []
    return [map_item("process:current", "后台进程", state="not_observed", fields={"kind": "process", "count": None,
            "next_read": next_read("process_session", {"action": "list"})})], ["SELF_MAP_PROCESS_LIVENESS_NOT_OBSERVED"]


# LLM: 原终端工具才能构造完整 PTY 沙箱键；地图没有观测就给 null，并指向现有 list 入口而不伪称空集。
# 函数用途: 说明终端需要一次原查询口复核，不启动终端或读取输出正文。
def _terminals(context):
    if "terminal_session" not in context.snapshot.available_tool_names or not context.thread_id:
        return [], []
    # 当前 invocation 不含 PTY 完整沙箱保护键；不能拿 None 查询不同范围后声称终端为空。
    return [map_item("terminal:current", "交互终端", state="not_observed", fields={"kind": "terminal", "count": None,
            "next_read": next_read("terminal_session", {"action": "list"})})], ["SELF_MAP_PTY_SCOPE_NOT_OBSERVED"]
