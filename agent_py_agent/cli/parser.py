from __future__ import annotations

"""LLM: builds the argparse command tree and owns the process main entrypoint.

给人看的解释：
这个文件只做命令注册：有哪些命令、每个命令有哪些参数、最终绑定到哪个处理函数。
具体命令逻辑都在对应 cli 模块里。
"""

import argparse

from .adapter import cmd_adapter, cmd_adapter_file
from .chat import cmd_chat
from .common import DEFAULT_CAPABILITY_CONFIG, DEFAULT_CONFIG, add_resume_context_switches, configure_stdio
from .daemon import cmd_daemon
from .gateway_client import cmd_default, cmd_gateway, cmd_gateway_ask, cmd_gateway_result
from .gateway_process import (
    cmd_gateway_logs,
    cmd_gateway_restart,
    cmd_gateway_run,
    cmd_gateway_start,
    cmd_gateway_status,
    cmd_gateway_stop,
)
from .learning import cmd_learn_accept, cmd_learn_list, cmd_learn_reject, cmd_learn_stats
from .local_commands import (
    cmd_local_doctor,
    cmd_local_index_memory,
    cmd_local_rebuild,
    cmd_local_search,
    cmd_local_store_status,
    cmd_memory_list,
    cmd_memory_search,
    cmd_remember,
    cmd_run,
    cmd_status,
    cmd_timeline,
)
from .logs import (
    cmd_logs,
    cmd_logs_hunt_ip,
    cmd_logs_ingest,
    cmd_logs_query,
    cmd_logs_status,
    cmd_logs_trace_case,
)
from .memory_archive_commands import (
    cmd_memory_archive_list,
    cmd_memory_archive_search,
    cmd_memory_resume,
)
from .memory_commands import cmd_memory_doctor, cmd_memory_route
from .scenario import cmd_scenario_test
from .subagents import (
    cmd_spawn,
    cmd_subagent_context,
    cmd_subagent_detail,
    cmd_subagent_run,
    cmd_subagents,
    cmd_subagents_acceptance,
    cmd_subagents_apply_actions,
    cmd_subagents_dispatch,
    cmd_subagents_due_check,
    cmd_subagents_patches,
    cmd_subagents_plan_actions,
    cmd_subagents_probe,
    cmd_subagents_route_capabilities,
    cmd_subagents_workflow_plan,
)


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="my-agent",
        description="Simple Python3 CLI Agent with memory, dynamic prompt and subagents.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help="配置文件路径，默认使用 config/agent_config.yaml",
    )
    sub = parser.add_subparsers(dest="command")
    parser.set_defaults(func=cmd_default)

    status = sub.add_parser("status", help="查看 my-agent 全局状态")
    status.add_argument("--limit", type=int, default=5, help="最多显示多少条 hot/recent/timeline 项")
    status.add_argument("--recent", action="store_true", help="显示最近子代理列表")
    status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    status.set_defaults(func=cmd_status)

    timeline = sub.add_parser("timeline", help="查看本地事实源最近事件")
    timeline.add_argument("--limit", type=int, default=20, help="最多显示多少条事件")
    timeline.add_argument("--source-type", help="按来源过滤，如 gateway_request/subagent_run")
    timeline.add_argument("--event-type", help="按事件类型过滤，如 gateway_request_completed")
    timeline.add_argument("--details", action="store_true", help="显示事件 payload 摘要")
    timeline.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    timeline.set_defaults(func=cmd_timeline)

    run = sub.add_parser("run", help="运行一次智能体对话")
    run.add_argument("prompt", help="用户任务 / prompt")
    run.add_argument("--inject", action="append", help="动态注入 prompt，可多次传入")
    run.add_argument("--prompt-file", action="append", help="额外动态 prompt 文件，可多次传入")
    run.add_argument("--save", action="store_true", default=None, help="保存本次对话到记忆")
    run.add_argument("--no-save", action="store_false", dest="save", help="不保存本次对话到记忆")
    run.add_argument("--show-prompt", action="store_true", help="打印最终拼装后的 prompt")
    add_resume_context_switches(run)
    run.set_defaults(func=cmd_run)

    remember = sub.add_parser("remember", help="手动写入一条记忆")
    remember.add_argument("content", help="记忆内容")
    remember.add_argument("--kind", default="note", help="记忆类型，如 note/preference/fact")
    remember.set_defaults(func=cmd_remember)

    memory_list = sub.add_parser("memory-list", help="列出最近记忆")
    memory_list.add_argument("--limit", type=int, default=20, help="最多显示条数")
    memory_list.set_defaults(func=cmd_memory_list)

    memory_search = sub.add_parser("memory-search", help="搜索记忆")
    memory_search.add_argument("query", help="搜索关键词")
    memory_search.add_argument("--limit", type=int, default=5, help="最多显示条数")
    memory_search.set_defaults(func=cmd_memory_search)

    memory_route = sub.add_parser("memory-route", help="按长期规则索引预览 memory 路由命中")
    memory_route.add_argument("query", help="要路由的查询或用户任务")
    memory_route.add_argument("--index", help="路由索引文件；相对路径按 workspace root 解析")
    memory_route.add_argument("--mode", choices=["off", "soft", "strict"], help="路由模式；默认使用配置")
    memory_route.add_argument("--limit", type=int, default=5, help="最多显示多少条命中 route；0 表示不截断")
    memory_route.add_argument("--auto-read-limit", type=int, help="最多升级多少条规则路径；默认使用配置")
    memory_route.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_route.set_defaults(func=cmd_memory_route)

    memory_doctor = sub.add_parser("memory-doctor", help="诊断 memory 配置、路由索引和归档目录")
    memory_doctor.add_argument("--index", help="路由索引文件；相对路径按 workspace root 解析")
    memory_doctor.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_doctor.set_defaults(func=cmd_memory_doctor)

    memory_archive_list = sub.add_parser("memory-archive-list", help="列出 memory raw/hook 归档记录")
    memory_archive_list.add_argument("--layer", choices=["all", "raw", "hook"], default="all", help="查看哪一层归档")
    memory_archive_list.add_argument("--date", help="只查看某一天，格式 YYYY-MM-DD")
    memory_archive_list.add_argument("--limit", type=int, default=20, help="最多显示多少条记录")
    memory_archive_list.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_archive_list.set_defaults(func=cmd_memory_archive_list)

    memory_archive_search = sub.add_parser("memory-archive-search", help="按字段搜索 memory raw/hook 归档")
    memory_archive_search.add_argument("query", nargs="?", default="", help="搜索关键词；可配合字段过滤")
    memory_archive_search.add_argument("--layer", choices=["all", "raw", "hook"], default="all", help="搜索哪一层归档")
    memory_archive_search.add_argument("--date", help="只搜索某一天，格式 YYYY-MM-DD")
    memory_archive_search.add_argument("--since", help="只看此时间之后的记录，支持 ISO 时间或日期")
    memory_archive_search.add_argument("--until", help="只看此时间之前的记录，支持 ISO 时间或日期")
    memory_archive_search.add_argument("--session-id", help="按 session_id 精确过滤")
    memory_archive_search.add_argument("--request-id", help="按 request_id 精确过滤")
    memory_archive_search.add_argument("--run-id", help="按 run_id 精确过滤")
    memory_archive_search.add_argument("--task-id", help="按 task_id 精确过滤")
    memory_archive_search.add_argument("--speaker", help="按 speaker 精确过滤，如 user/assistant/tool")
    memory_archive_search.add_argument("--target", help="按 target 精确过滤")
    memory_archive_search.add_argument("--action", help="按 action 精确过滤，如 message/response/tool_call")
    memory_archive_search.add_argument("--status", help="按 status 精确过滤，如 ok/failed")
    memory_archive_search.add_argument("--tool-name", help="按工具名精确过滤")
    memory_archive_search.add_argument("--source", help="按来源精确过滤，如 run/gateway/subagent")
    memory_archive_search.add_argument("--limit", type=int, default=20, help="最多显示多少条记录")
    memory_archive_search.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_archive_search.set_defaults(func=cmd_memory_archive_search)

    memory_resume = sub.add_parser("memory-resume", help="从归档和事实源生成恢复线索")
    memory_resume.add_argument("query", nargs="?", default="", help="恢复关键词；也可只传 request/run/session 过滤")
    memory_resume.add_argument("--layer", choices=["all", "raw", "hook"], default="all", help="从哪一层归档找线索")
    memory_resume.add_argument("--date", help="只看某一天，格式 YYYY-MM-DD")
    memory_resume.add_argument("--since", help="只看此时间之后的归档线索")
    memory_resume.add_argument("--until", help="只看此时间之前的归档线索")
    memory_resume.add_argument("--session-id", help="按 session_id 精确过滤")
    memory_resume.add_argument("--request-id", help="按 request_id 精确过滤")
    memory_resume.add_argument("--run-id", help="按 run_id 精确过滤")
    memory_resume.add_argument("--task-id", help="按 task_id 精确过滤")
    memory_resume.add_argument("--speaker", help="按 speaker 精确过滤")
    memory_resume.add_argument("--target", help="按 target 精确过滤")
    memory_resume.add_argument("--action", help="按 action 精确过滤")
    memory_resume.add_argument("--status", help="按 status 精确过滤")
    memory_resume.add_argument("--tool-name", help="按工具名精确过滤")
    memory_resume.add_argument("--source", help="按来源精确过滤")
    memory_resume.add_argument("--limit", type=int, default=20, help="最多显示多少条线索")
    memory_resume.add_argument("--context-only", action="store_true", help="只输出可交接/注入的恢复上下文块")
    memory_resume.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_resume.set_defaults(func=cmd_memory_resume)

    local_store_status = sub.add_parser("local-store-status", help="查看本地事实源状态")
    local_store_status.set_defaults(func=cmd_local_store_status)

    local_search = sub.add_parser("local-search", help="搜索本地事实源 SQLite/FTS5 索引")
    local_search.add_argument("query", help="搜索关键词；为空时可用 local-store-status 看整体状态")
    local_search.add_argument("--limit", type=int, default=5, help="最多显示条数")
    local_search.add_argument("--source-type", help="按来源过滤，如 memory/gateway_request/subagent_run")
    local_search.add_argument("--visibility", help="按可见性过滤，默认不过滤")
    local_search.add_argument("--preview-chars", type=int, default=500, help="每条命中最多打印多少正文字符；-1 表示完整打印")
    local_search.set_defaults(func=cmd_local_search)

    local_index_memory = sub.add_parser("local-index-memory", help="把现有 JSONL 记忆补建到本地事实源")
    local_index_memory.set_defaults(func=cmd_local_index_memory)

    local_doctor = sub.add_parser("local-doctor", help="诊断 LocalStore、gateway 队列和 subagent 文件事实源")
    local_doctor.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    local_doctor.add_argument("--repair", action="store_true", help="处理超时 processing gateway 请求")
    local_doctor.add_argument("--limit", type=int, default=20, help="每类问题最多显示多少条")
    local_doctor.set_defaults(func=cmd_local_doctor)

    local_rebuild = sub.add_parser("local-rebuild", help="从 memory/gateway/subagent 文件事实源重建 LocalStore")
    local_rebuild.add_argument(
        "--source",
        action="append",
        choices=["all", "memory", "gateway", "subagent", "fts"],
        help="只重建指定来源，可多次传入；默认 all",
    )
    local_rebuild.add_argument("--reset", action="store_true", help="先清空 LocalStore records/events/FTS 再重建")
    local_rebuild.set_defaults(func=cmd_local_rebuild)

    logs = sub.add_parser("logs", help="Log analysis status, ingest and query commands")
    logs_sub = logs.add_subparsers(dest="logs_command")
    logs.set_defaults(func=cmd_logs)

    logs_status = logs_sub.add_parser("status", help="Show log analysis module status without starting workers")
    logs_status.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    logs_status.set_defaults(func=cmd_logs_status)

    logs_ingest = logs_sub.add_parser("ingest", help="Ingest a local security log file")
    logs_ingest.add_argument("file", help="File to ingest")
    logs_ingest.add_argument("--root", help="Override log-analysis data directory")
    logs_ingest.add_argument("--source-id", help="Source identifier for checkpoints and manifests")
    logs_ingest.add_argument("--format", choices=["jsonl", "json", "csv", "log"], help="Input file format")
    logs_ingest.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    logs_ingest.set_defaults(func=cmd_logs_ingest)

    logs_query = logs_sub.add_parser("query", help="Query ingested security events")
    logs_query.add_argument("--root", help="Override log-analysis data directory")
    logs_query.add_argument("--start-time", help="Inclusive ISO-8601 start time")
    logs_query.add_argument("--end-time", help="Inclusive ISO-8601 end time")
    logs_query.add_argument("--attacker-ip", help="Filter by attacker/source IP")
    logs_query.add_argument("--victim-ip", help="Filter by victim/destination IP")
    logs_query.add_argument("--domain", help="Filter by domain/host/SNI/DNS query")
    logs_query.add_argument("--uri", help="Filter by URI/URL/path/API")
    logs_query.add_argument("--alert-type", help="Filter by alert type")
    logs_query.add_argument("--limit", type=int, help="Maximum rows to return")
    logs_query.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    logs_query.set_defaults(func=cmd_logs_query)

    logs_hunt_ip = logs_sub.add_parser("hunt-ip", help="Run attacker/victim IP hunt queries")
    logs_hunt_ip.add_argument("ip", help="IP address to hunt")
    logs_hunt_ip.add_argument("--root", help="Override log-analysis data directory")
    logs_hunt_ip.add_argument("--role", choices=["any", "attacker", "victim"], default="any", help="IP role to query")
    logs_hunt_ip.add_argument("--start-time", help="Inclusive ISO-8601 start time")
    logs_hunt_ip.add_argument("--end-time", help="Inclusive ISO-8601 end time")
    logs_hunt_ip.add_argument("--limit", type=int, help="Maximum rows to return")
    logs_hunt_ip.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    logs_hunt_ip.set_defaults(func=cmd_logs_hunt_ip)

    logs_trace_case = logs_sub.add_parser("trace-case", help="Trace a case through stored query seeds")
    logs_trace_case.add_argument("case_id", help="Case identifier")
    logs_trace_case.add_argument("--root", help="Override log-analysis data directory")
    logs_trace_case.add_argument("--start-time", help="Inclusive ISO-8601 start time")
    logs_trace_case.add_argument("--end-time", help="Inclusive ISO-8601 end time")
    logs_trace_case.add_argument("--limit", type=int, help="Maximum rows to return")
    logs_trace_case.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    logs_trace_case.set_defaults(func=cmd_logs_trace_case)

    chat = sub.add_parser("chat", help="启动交互循环，反复与智能体交流")
    chat.add_argument("--inject", action="append", help="启动时注入 prompt，可多次传入")
    chat.add_argument("--prompt-file", action="append", help="启动时加载额外 prompt 文件，可多次传入")
    chat.add_argument("--memory-limit", type=int, default=5, help="交互中 /memory 默认显示条数")
    chat.add_argument("--no-save", action="store_true", help="交互对话不自动保存到记忆")
    chat.add_argument("--gateway", action="store_true", help="把普通聊天消息投递给后台 gateway，而不是在当前前台进程里调用模型")
    chat.add_argument("--gateway-timeout", type=float, help="gateway 模式等待单条响应的秒数，默认使用配置 gateway_request_timeout")
    add_resume_context_switches(chat)
    chat.set_defaults(func=cmd_chat)

    learn = sub.add_parser("learn", help="管理自动生成的 learning draft 候选")
    learn_sub = learn.add_subparsers(dest="learn_command", required=True)

    learn_list = learn_sub.add_parser("list", help="列出当前 learning draft 候选")
    learn_list.set_defaults(func=cmd_learn_list)

    learn_accept = learn_sub.add_parser("accept", help="确认一个 learning draft")
    learn_accept.add_argument("candidate_id", help="learning draft ID")
    learn_accept.set_defaults(func=cmd_learn_accept)

    learn_reject = learn_sub.add_parser("reject", help="拒绝一个 learning draft")
    learn_reject.add_argument("candidate_id", help="learning draft ID")
    learn_reject.set_defaults(func=cmd_learn_reject)

    learn_stats = learn_sub.add_parser("stats", help="查看 learning draft 汇总统计")
    learn_stats.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    learn_stats.set_defaults(func=cmd_learn_stats)

    spawn = sub.add_parser("spawn-subagents", help="拆分并创建 subagent 任务记录")
    spawn.add_argument("goal", help="要拆分的目标")
    spawn.add_argument("--count", type=int, default=3, help="子代理数量")
    spawn.set_defaults(func=cmd_spawn)

    subagents = sub.add_parser("subagents", help="查看 subagent 红绿灯看板")
    subagents.add_argument("--all", action="store_true", help="显示全部记录，而不是默认的红灯/最近记录")
    subagents.add_argument("--status", help="按状态过滤，如 BLOCKED/DONE/TAKEN_OVER")
    subagents.add_argument("--owner", help="按 owner/supervisor/final_owner 过滤")
    subagents.add_argument("--root-id", help="按根任务 ID 过滤")
    subagents.add_argument("--limit", type=int, default=20, help="最多显示多少条")
    subagents.set_defaults(func=cmd_subagents)

    workflow_plan = sub.add_parser("subagents-workflow-plan", help="Preview automatic subagent workflow routing")
    workflow_plan.add_argument("goal", help="Parent goal to route into a workflow")
    workflow_plan.add_argument("--template-id", help="Force a workflow template id for the preview")
    workflow_plan.add_argument("--output-dir", help="Write JSON and Markdown dry-run previews to this directory")
    workflow_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    workflow_plan.set_defaults(func=cmd_subagents_workflow_plan)

    due_check = sub.add_parser("subagents-due-check", help="巡检 subagent 并输出父代理待处理项")
    due_check.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    due_check.add_argument("--all", action="store_true", help="显示全部问题，而不是按 limit 截断")
    due_check.add_argument("--limit", type=int, default=20, help="最多显示多少条问题")
    due_check.set_defaults(func=cmd_subagents_due_check)

    probe = sub.add_parser("subagents-probe", help="检查 subagent 通道健康状态")
    probe.add_argument("run_id", nargs="*", help="子代理运行 ID；不传则检查最近记录")
    probe.add_argument("--limit", type=int, default=20, help="不指定 run_id 时最多检查多少条")
    probe.set_defaults(func=cmd_subagents_probe)

    action_plan = sub.add_parser("subagents-plan-actions", help="根据 due-check 生成 dry-run 动作计划")
    action_plan.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    action_plan.add_argument("--all", action="store_true", help="显示全部动作，而不是按 limit 截断")
    action_plan.add_argument("--limit", type=int, default=20, help="最多显示多少条动作")
    action_plan.set_defaults(func=cmd_subagents_plan_actions)

    apply_actions = sub.add_parser("subagents-apply-actions", help="执行或 dry-run 执行 action plan")
    apply_actions.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    apply_actions.add_argument("--dry-run", action="store_false", dest="apply", help="只预览动作，不修改记录")
    apply_actions.add_argument("--apply", action="store_true", help="真正执行低风险动作")
    apply_actions.add_argument("--action", help="只处理指定动作，如 reopen_for_evidence")
    apply_actions.add_argument("--run-id", help="只处理指定子代理运行 ID")
    apply_actions.add_argument("--limit", type=int, default=20, help="最多处理多少条动作")
    apply_actions.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    apply_actions.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    apply_actions.set_defaults(func=cmd_subagents_apply_actions, apply=False)

    route = sub.add_parser("subagents-route-capabilities", help="路由 OPEN capability request")
    route.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    route.add_argument("--dry-run", action="store_false", dest="apply", help="只预览路由，不生成 grant/gap")
    route.add_argument("--apply", action="store_true", help="真正生成 capability grant 或 gap")
    route.add_argument("--run-id", nargs="*", help="只处理指定子代理运行 ID")
    route.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    route.add_argument("--limit", type=int, default=20, help="最多处理多少条 request")
    route.set_defaults(func=cmd_subagents_route_capabilities, apply=False)

    acceptance = sub.add_parser("subagents-acceptance", help="验收等待验收的 subagent")
    acceptance.add_argument("--dry-run", action="store_false", dest="apply", help="只生成验收报告，不修改记录")
    acceptance.add_argument("--apply", action="store_true", help="验收通过时标记 DONE/VERIFIED，失败时标记 BLOCKED/FAILED")
    acceptance.add_argument("--run-id", nargs="*", help="只验收指定子代理运行 ID")
    acceptance.add_argument("--limit", type=int, default=20, help="最多处理多少条记录")
    acceptance.add_argument("--reviewer", default="parent", help="验收者标识")
    acceptance.add_argument("--note", help="写入验收记录的备注")
    acceptance.set_defaults(func=cmd_subagents_acceptance, apply=False)

    patches = sub.add_parser("subagents-patches", help="审核 runner 输出里的 patch 记录")
    patch_action = patches.add_mutually_exclusive_group()
    patch_action.add_argument("--dry-run", action="store_const", const="review_dry_run", dest="patch_action", help="只生成 patch 审核报告，不修改记录")
    patch_action.add_argument("--review-apply", action="store_const", const="review_apply", dest="patch_action", help="写回 patch 审核状态，但不真正 apply 文件")
    patch_action.add_argument("--apply-dry-run", action="store_const", const="apply_dry_run", dest="patch_action", help="展示将要 apply 的 diff，不真正写文件")
    patch_action.add_argument("--apply", action="store_const", const="apply", dest="patch_action", help="真正 apply patch、跑 allowlist 测试并记录审计日志")
    patches.add_argument("--run-id", nargs="*", help="只审核指定子代理运行 ID")
    patches.add_argument("--limit", type=int, default=20, help="最多处理多少条记录")
    patches.add_argument("--reviewer", default="parent", help="审核者标识")
    patches.add_argument("--note", help="写入 patch 审核记录的备注")
    patches.set_defaults(func=cmd_subagents_patches, patch_action="review_dry_run")

    dispatch = sub.add_parser("subagents-dispatch", help="执行一轮父代理调度，默认 dry-run")
    dispatch.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    dispatch.add_argument("--dry-run", action="store_false", dest="apply", help="只生成调度报告，不修改记录")
    dispatch.add_argument("--apply", action="store_true", help="执行低风险调度动作并写审计日志")
    dispatch.add_argument("--execute-runners", action="store_true", help="配合 --apply 调用真实模型执行 runner")
    dispatch.add_argument("--planner", action="store_true", help="有待处理事项时调用父代理 LLM planner，禁止空心 HEARTBEAT_OK")
    dispatch.add_argument("--workflow-mode", choices=["off", "plan", "auto"], default="off", help="dispatch 前对父任务执行 workflow 规划；plan 只写计划，auto 还会自动派工")
    dispatch.add_argument("--max-runners", type=int, default=1, help="本轮最多推进多少个 runner，0 表示不执行 runner")
    dispatch.add_argument("--limit", type=int, default=20, help="每个阶段最多处理多少条记录，0 表示不限制")
    dispatch.add_argument("--watch", action="store_true", help="持续循环执行 dispatch")
    dispatch.add_argument("--interval", type=float, default=30.0, help="watch 模式每轮间隔秒数，0 表示不等待")
    dispatch.add_argument("--max-cycles", type=int, default=0, help="watch 模式最多循环次数，0 表示持续运行")
    dispatch.add_argument("--force-lock", action="store_true", help="强制覆盖已有 watch lock")
    dispatch.add_argument("--reviewer", default="parent-dispatch", help="patch/acceptance 审核者标识")
    dispatch.add_argument("--note", help="写入调度关联审核记录的备注")
    dispatch.add_argument("--instruction", help="给本轮 runner 的额外指令")
    dispatch.add_argument("--max-cards", type=int, default=0, help="runner 最多注入多少张能力卡，0 表示不限制")
    dispatch.add_argument("--no-probe", action="store_true", help="执行 runner 前不做通道健康检查")
    dispatch.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    dispatch.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    dispatch.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    dispatch.set_defaults(func=cmd_subagents_dispatch, apply=False)

    daemon = sub.add_parser("daemon", help="按配置启动前台常驻调度")
    daemon.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    daemon.add_argument("--dry-run", action="store_false", dest="apply", default=None, help="覆盖配置：只生成报告，不写回")
    daemon.add_argument("--apply", action="store_true", default=None, help="覆盖配置：写回低风险动作和审计日志")
    daemon.add_argument("--execute-runners", action="store_true", dest="execute_runners", default=None, help="覆盖配置：配合 apply 调用真实模型执行 runner")
    daemon.add_argument("--no-execute-runners", action="store_false", dest="execute_runners", help="覆盖配置：不调用真实模型执行 runner")
    daemon.add_argument("--planner", action="store_true", dest="planner", default=None, help="覆盖配置：启用父代理 LLM planner")
    daemon.add_argument("--no-planner", action="store_false", dest="planner", help="覆盖配置：关闭父代理 LLM planner")
    daemon.add_argument("--interval", type=float, help="覆盖配置：每轮间隔秒数，0 表示不等待")
    daemon.add_argument("--max-runners", help="覆盖配置：每轮最多推进多少个 runner；auto 表示保守自适应，0 表示不执行 runner")
    daemon.add_argument("--limit", type=int, help="覆盖配置：每个阶段最多处理多少条记录，0 表示不限制")
    daemon.add_argument("--max-cycles", type=int, help="覆盖配置：最多循环次数，0 表示持续运行")
    daemon.add_argument("--force-lock", action="store_true", help="强制覆盖已有 watch lock")
    daemon.add_argument("--reviewer", help="覆盖配置：patch/acceptance 审核者标识")
    daemon.add_argument("--note", help="写入调度关联审核记录的备注")
    daemon.add_argument("--instruction", help="覆盖配置：给 runner 的额外指令")
    daemon.add_argument("--max-cards", type=int, help="覆盖配置：runner 最多注入多少张能力卡，0 表示不限制")
    daemon.add_argument("--no-probe", action="store_true", help="覆盖配置：执行 runner 前不做通道健康检查")
    daemon.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    daemon.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    daemon.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    daemon.set_defaults(func=cmd_daemon)

    scenario = sub.add_parser("scenario-test", help="跑一轮隔离的真实全流程任务测试")
    scenario.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    scenario.add_argument(
        "--case",
        choices=[
            "happy",
            "verification",
            "gateway-restart",
            "gateway-cross-day-resume",
            "gateway-delayed-response",
            "gateway-multi-worker",
            "gateway-stale-lease",
            "parent-subagent-cross-day-resume",
            "real-model-recovery",
            "structured-repair",
            "runner-retry",
            "all",
        ],
        default="happy",
        help="场景类型：happy 跑真实全流程；verification 测验收防作弊；gateway-restart 测重启恢复；gateway-cross-day-resume 测真实 gateway 请求跨天恢复；gateway-delayed-response 测响应先到后请求副本归档；gateway-multi-worker 测多 request worker 并发抢占；gateway-stale-lease 测 processing stale lease 重排恢复；parent-subagent-cross-day-resume 测真实 runner 写回后的跨天恢复；real-model-recovery 测真实模型 API 的 parent/subagent 跨天恢复；structured-repair 测坏结构化输出修复；runner-retry 测 runner 失败重试；all 连续运行",
    )
    scenario.add_argument("--workspace", help="保存场景测试结果的父目录；不传则使用系统临时目录")
    scenario.add_argument("--count", type=int, default=2, help="本场景创建多少个子代理")
    scenario.add_argument("--max-runners", type=int, default=2, help="每轮最多推进多少个 runner")
    scenario.add_argument("--max-cycles", type=int, default=3, help="最多执行多少轮 dispatch")
    scenario.add_argument("--timeout", type=float, default=300.0, help="gateway ask 等待响应的秒数")
    scenario.add_argument("--dry-run", action="store_true", help="只调度不执行真实 runner API")
    scenario.add_argument("--planner", action="store_true", help="dispatch 时启用父代理 planner")
    scenario.add_argument("--direct", action="store_true", help="不经过 gateway，直接用当前进程跑主代理派工")
    scenario.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    scenario.set_defaults(func=cmd_scenario_test)

    gateway = sub.add_parser("gateway", help="管理后台 gateway 进程")
    gateway_sub = gateway.add_subparsers(dest="gateway_command")
    gateway.set_defaults(func=cmd_gateway)

    gateway_start = gateway_sub.add_parser("start", help="启动后台 gateway")
    gateway_start.add_argument("--force", action="store_true", help="已有 gateway 运行时先尝试停止再启动")
    gateway_start.add_argument("--force-lock", action="store_true", help="传给内部 daemon，强制覆盖已有 dispatch watch lock")
    gateway_start.set_defaults(func=cmd_gateway_start)

    gateway_run = gateway_sub.add_parser("run", help="内部命令：前台运行 gateway 循环")
    gateway_run.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )
    gateway_run.add_argument("--dry-run", action="store_false", dest="apply", default=None, help="覆盖配置：只生成报告，不写回")
    gateway_run.add_argument("--apply", action="store_true", default=None, help="覆盖配置：写回低风险动作和审计日志")
    gateway_run.add_argument("--execute-runners", action="store_true", dest="execute_runners", default=None, help="覆盖配置：配合 apply 调用真实模型执行 runner")
    gateway_run.add_argument("--no-execute-runners", action="store_false", dest="execute_runners", help="覆盖配置：不调用真实模型执行 runner")
    gateway_run.add_argument("--planner", action="store_true", dest="planner", default=None, help="覆盖配置：启用父代理 LLM planner")
    gateway_run.add_argument("--no-planner", action="store_false", dest="planner", help="覆盖配置：关闭父代理 LLM planner")
    gateway_run.add_argument("--interval", type=float, help="覆盖配置：每轮间隔秒数，0 表示不等待")
    gateway_run.add_argument("--max-runners", help="覆盖配置：每轮最多推进多少个 runner；auto 表示保守自适应，0 表示不执行 runner")
    gateway_run.add_argument("--limit", type=int, help="覆盖配置：每个阶段最多处理多少条记录，0 表示不限制")
    gateway_run.add_argument("--max-cycles", type=int, help="覆盖配置：最多循环次数，0 表示持续运行")
    gateway_run.add_argument("--force-lock", action="store_true", help="强制覆盖已有 watch lock")
    gateway_run.add_argument("--reviewer", help="覆盖配置：patch/acceptance 审核者标识")
    gateway_run.add_argument("--note", help="写入调度关联审核记录的备注")
    gateway_run.add_argument("--instruction", help="覆盖配置：给 runner 的额外指令")
    gateway_run.add_argument("--max-cards", type=int, help="覆盖配置：runner 最多注入多少张能力卡，0 表示不限制")
    gateway_run.add_argument("--no-probe", action="store_true", help="覆盖配置：执行 runner 前不做通道健康检查")
    gateway_run.add_argument("--take-over-by", help="接管动作的接管者，apply takeover 时必填")
    gateway_run.add_argument("--locked-file", action="append", help="接管时锁定的文件，可多次传入")
    gateway_run.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    gateway_run.set_defaults(func=cmd_gateway_run)

    gateway_status = gateway_sub.add_parser("status", help="查看 gateway 状态")
    gateway_status.set_defaults(func=cmd_gateway_status)

    gateway_stop = gateway_sub.add_parser("stop", help="请求 gateway 停止")
    gateway_stop.add_argument("--timeout", type=float, help="等待正常停止的秒数，默认使用配置")
    gateway_stop.add_argument("--kill", action="store_true", help="超时后强制终止进程")
    gateway_stop.add_argument("--reason", help="写入 stop request 的原因")
    gateway_stop.set_defaults(func=cmd_gateway_stop)

    gateway_restart = gateway_sub.add_parser("restart", help="重启 gateway")
    gateway_restart.add_argument("--timeout", type=float, help="等待正常停止的秒数，默认使用配置")
    gateway_restart.add_argument("--force", action="store_true", help="停止超时后强制终止旧进程")
    gateway_restart.add_argument("--force-lock", action="store_true", help="传给内部 daemon，强制覆盖已有 dispatch watch lock")
    gateway_restart.set_defaults(func=cmd_gateway_restart)

    gateway_logs = gateway_sub.add_parser("logs", help="显示 gateway 日志尾部")
    gateway_logs.add_argument("--lines", type=int, default=80, help="显示最后多少行日志，0 表示全部")
    gateway_logs.set_defaults(func=cmd_gateway_logs)

    gateway_ask = gateway_sub.add_parser(
        "ask",
        help="向后台 gateway 投递一条聊天请求；未来聊天工具/TUI 会复用这条通道",
    )
    gateway_ask.add_argument("prompt", help="用户任务 / prompt")
    gateway_ask.add_argument("--inject", action="append", help="动态注入 prompt，可多次传入")
    gateway_ask.add_argument("--prompt-file", action="append", help="额外动态 prompt 文件，可多次传入")
    gateway_ask.add_argument("--no-save", action="store_true", help="不保存本次对话到记忆")
    gateway_ask.add_argument("--show-prompt", action="store_true", help="响应返回时打印最终 prompt")
    gateway_ask.add_argument("--timeout", type=float, help="等待 gateway 响应的秒数，默认使用配置")
    gateway_ask.add_argument("--no-wait", action="store_true", help="只投递请求并立即返回 request_id，适合长任务")
    gateway_ask.add_argument("--json", action="store_true", help="输出完整响应 JSON，方便脚本或聊天适配器读取")
    add_resume_context_switches(gateway_ask)
    gateway_ask.set_defaults(func=cmd_gateway_ask)

    gateway_result = gateway_sub.add_parser("result", help="读取某个 gateway 请求结果，通常配合 ask --no-wait 使用")
    gateway_result.add_argument("request_id", help="gateway 请求 ID")
    gateway_result.add_argument("--show-prompt", action="store_true", help="打印响应中保存的最终 prompt")
    gateway_result.add_argument("--json", action="store_true", help="输出完整响应 JSON，方便脚本或聊天适配器读取")
    gateway_result.set_defaults(func=cmd_gateway_result)

    adapter = sub.add_parser("adapter", help="外部聊天工具 / TUI 适配器")
    adapter_sub = adapter.add_subparsers(dest="adapter_command")
    adapter.set_defaults(func=cmd_adapter)

    adapter_file = adapter_sub.add_parser("file", help="文件协议适配器：inbox JSON -> gateway -> outbox JSON")
    adapter_file.add_argument("--root", help="适配器根目录；默认使用配置 adapter_workspace")
    adapter_file.add_argument("--inbox", help="覆盖 inbox 目录")
    adapter_file.add_argument("--outbox", help="覆盖 outbox 目录")
    adapter_file.add_argument("--watch", action="store_true", help="持续轮询 inbox")
    adapter_file.add_argument("--once", action="store_true", help="只处理当前已有消息后退出")
    adapter_file.add_argument("--poll-interval", type=float, default=1.0, help="watch 模式轮询间隔秒数")
    adapter_file.add_argument("--limit", type=int, default=20, help="每轮最多处理多少条消息，0 表示不限制")
    adapter_file.add_argument("--timeout", type=float, help="等待 gateway 响应的秒数，默认使用配置 gateway_request_timeout")
    adapter_file.add_argument("--no-start-gateway", action="store_true", help="不自动启动 gateway；未运行时直接失败")
    adapter_file.set_defaults(func=cmd_adapter_file)

    subagent_context = sub.add_parser("subagent-context", help="生成单个 subagent 执行上下文包")
    subagent_context.add_argument("run_id", help="子代理运行 ID")
    subagent_context.add_argument("--max-cards", type=int, default=0, help="最多注入多少张能力卡，0 表示不限制")
    subagent_context.set_defaults(func=cmd_subagent_context)

    subagent_run = sub.add_parser("subagent-run", help="按执行上下文运行一个 subagent，默认 dry-run")
    subagent_run.add_argument("run_id", help="子代理运行 ID")
    subagent_run.add_argument("--dry-run", action="store_false", dest="execute", help="只生成 prompt 和报告，不调用模型")
    subagent_run.add_argument("--execute", action="store_true", help="真正调用模型执行，可能消耗 API")
    subagent_run.add_argument("--instruction", help="给本次 runner 的额外指令")
    subagent_run.add_argument("--max-cards", type=int, default=0, help="最多注入多少张能力卡，0 表示不限制")
    subagent_run.add_argument("--no-probe", action="store_true", help="执行前不做通道健康检查")
    subagent_run.set_defaults(func=cmd_subagent_run, execute=False)

    subagent = sub.add_parser("subagent", help="查看单个 subagent 运行详情")
    subagent.add_argument("run_id", help="子代理运行 ID")
    subagent.set_defaults(func=cmd_subagent_detail)
    return parser


def main() -> int:
    """程序入口。"""

    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)
