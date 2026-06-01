# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

"""basic / memory / local-store subcommand registration helpers.

给人看的解释：
这个文件包含基础子命令、记忆子命令和本地事实源子命令的注册函数。
从 parser_subcommands.py 拆分而来，保持原有逻辑不变。
"""

import argparse

from .chat import cmd_chat
from .common import DEFAULT_CAPABILITY_CONFIG, add_resume_context_switches
from .context_bundle_commands import cmd_context_bundle
from .home_runtime_subcommands import add_home_runtime_subcommands
from .local_commands import (
    cmd_local_index_memory,
    cmd_local_search,
    cmd_local_store_status,
    cmd_memory_list,
    cmd_memory_search,
    cmd_remember,
    cmd_run,
    cmd_status,
    cmd_timeline,
)
from .local_repair_commands import cmd_local_doctor, cmd_local_rebuild
from .memory_archive_commands import (
    cmd_memory_archive_list,
    cmd_memory_archive_search,
    cmd_memory_resume,
)
from .memory_artifact_commands import cmd_memory_artifact_read
from .memory_commands import cmd_memory_route
from .memory_compact_commands import cmd_memory_compact
from .memory_doctor import cmd_memory_doctor
from .memory_fact_commands import cmd_memory_fact_write


# LLM: _add_capability_config_arg 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_capability_config_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )


# LLM: add_basic_subcommands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 注册 argparse 参数和子命令，决定用户可见的命令形状。
def add_basic_subcommands(sub: argparse._SubParsersAction) -> None:
    _add_status_timeline_run_commands(sub)
    _add_memory_chat_commands(sub)
    _add_context_bundle_command(sub)


# LLM: _add_context_bundle_command registers refs-only context bundle observability.
# 函数用途: 增加 `context-bundle latest` 命令，不触发模型调用，只读最新主代理任务卡。
def _add_context_bundle_command(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("context-bundle", help="查看主代理 context bundle 任务卡")
    nested = parser.add_subparsers(dest="context_bundle_action")
    latest = nested.add_parser("latest", help="查看最新 main context bundle")
    latest.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    latest.set_defaults(func=cmd_context_bundle)
    parser.set_defaults(func=cmd_context_bundle, context_bundle_action="latest")


# LLM: _add_status_timeline_run_commands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_status_timeline_run_commands(sub: argparse._SubParsersAction) -> None:
    status = sub.add_parser("status", help="查看 my-agent 全局状态")
    status.add_argument("--limit", type=int, default=None, help="最多显示多少条 hot/recent/timeline 项；默认读配置")
    status.add_argument("--recent", action="store_true", help="显示最近子代理列表")
    status.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    status.set_defaults(func=cmd_status)

    timeline = sub.add_parser("timeline", help="查看本地事实源最近事件")
    timeline.add_argument("--limit", type=int, default=None, help="最多显示多少条事件；默认读配置")
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
    run.add_argument("--delivery-contract-file", default="", help="结构化交付合同 JSON 文件")
    add_resume_context_switches(run)
    run.set_defaults(func=cmd_run)


# LLM: _add_memory_chat_commands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_memory_chat_commands(sub: argparse._SubParsersAction) -> None:
    remember = sub.add_parser("remember", help="手动写入一条记忆")
    remember.add_argument("content", help="记忆内容")
    remember.add_argument("--kind", default="note", help="记忆类型，如 note/preference/fact")
    remember.set_defaults(func=cmd_remember)

    memory_list = sub.add_parser("memory-list", help="列出最近记忆")
    memory_list.add_argument("--limit", type=int, default=None, help="最多显示条数；默认读配置")
    memory_list.set_defaults(func=cmd_memory_list)

    memory_search = sub.add_parser("memory-search", help="搜索记忆")
    memory_search.add_argument("query", help="搜索关键词")
    memory_search.add_argument("--limit", type=int, default=None, help="最多显示条数；默认读配置")
    memory_search.set_defaults(func=cmd_memory_search)

    chat = sub.add_parser("chat", help="启动交互循环，反复与智能体交流")
    chat.add_argument("--inject", action="append", help="启动时注入 prompt，可多次传入")
    chat.add_argument("--prompt-file", action="append", help="启动时加载额外 prompt 文件，可多次传入")
    chat.add_argument("--memory-limit", type=int, default=None, help="交互中 /memory 默认显示条数；默认读配置")
    chat.add_argument("--no-save", action="store_true", help="交互对话不自动保存到记忆")
    chat.add_argument("--gateway", action="store_true", help="把普通聊天消息投递给后台 gateway，而不是在当前前台进程里调用模型")
    chat.add_argument("--gateway-timeout", type=float, help="gateway 模式等待单条响应的秒数，默认使用配置 gateway_request_timeout")
    chat.add_argument("--session-id", help="恢复指定会话，不传则创建新会话")
    add_resume_context_switches(chat)
    chat.set_defaults(func=cmd_chat)


# LLM: _add_archive_search_args 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_archive_search_args(parser) -> None:
    parser.add_argument("--layer", choices=["all", "raw", "hook"], default="all", help="搜索哪一层归档")
    parser.add_argument("--date", help="只搜索某一天，格式 YYYY-MM-DD")
    parser.add_argument("--since", help="只看此时间之后的记录，支持 ISO 时间或日期")
    parser.add_argument("--until", help="只看此时间之前的记录，支持 ISO 时间或日期")
    parser.add_argument("--session-id", help="按 session_id 精确过滤")
    parser.add_argument("--request-id", help="按 request_id 精确过滤")
    parser.add_argument("--run-id", help="按 run_id 精确过滤")
    parser.add_argument("--task-id", help="按 task_id 精确过滤")
    parser.add_argument("--speaker", help="按 speaker 精确过滤，如 user/assistant/tool")
    parser.add_argument("--target", help="按 target 精确过滤")
    parser.add_argument("--action", help="按 action 精确过滤，如 message/response/tool_call")
    parser.add_argument("--status", help="按 status 精确过滤，如 ok/failed")
    parser.add_argument("--tool-name", help="按工具名精确过滤")
    parser.add_argument("--source", help="按来源精确过滤，如 run/gateway/subagent")
    parser.add_argument("--limit", type=int, default=None, help="最多显示多少条记录；默认读配置")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")


# LLM: _add_archive_resume_args 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_archive_resume_args(parser) -> None:
    parser.add_argument("--from-compact", dest="from_compact", help="从 memory-compact --apply 的 apply_id 或产物路径恢复")
    parser.add_argument("--compact-resume-mode", choices=["manual", "auto"], default="manual", help="compact 恢复模式；auto 会启用严格 action guard")
    parser.add_argument("--compact-owner-type", default="main_agent", help="compact owner 类型；预留 subagent_run/subagent_session")
    parser.add_argument("--compact-owner-id", default="", help="compact owner 标识；预留给子代理会话压缩")
    parser.add_argument("--layer", choices=["all", "raw", "hook"], default="all", help="从哪一层归档找线索")
    parser.add_argument("--date", help="只看某一天，格式 YYYY-MM-DD")
    parser.add_argument("--since", help="只看此时间之后的归档线索")
    parser.add_argument("--until", help="只看此时间之前的归档线索")
    parser.add_argument("--session-id", help="按 session_id 精确过滤")
    parser.add_argument("--request-id", help="按 request_id 精确过滤")
    parser.add_argument("--run-id", help="按 run_id 精确过滤")
    parser.add_argument("--task-id", help="按 task_id 精确过滤")
    parser.add_argument("--speaker", help="按 speaker 精确过滤")
    parser.add_argument("--target", help="按 target 精确过滤")
    parser.add_argument("--action", help="按 action 精确过滤")
    parser.add_argument("--status", help="按 status 精确过滤")
    parser.add_argument("--tool-name", help="按工具名精确过滤")
    parser.add_argument("--source", help="按来源精确过滤")
    parser.add_argument("--limit", type=int, default=None, help="最多显示多少条线索；默认读配置")
    parser.add_argument("--context-only", action="store_true", help="只输出可交接/注入的恢复上下文块")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")


# LLM: _add_memory_fact_write_args registers the explicit completion-fact write surface.
# 函数用途: 配置 memory-fact-write 参数，让用户手动补齐 compact resume 缺失字段。
def _add_memory_fact_write_args(parser) -> None:
    parser.add_argument("--fact-id", default="", help="事实源目录名；通常使用 request_id/session_id/task_id/run_id")
    parser.add_argument("--from-compact", dest="from_compact", default="", help="可选：从 compact apply 读取目标和下一步")
    parser.add_argument("--compact-owner-type", default="main_agent", help="compact owner 类型；与 memory-resume 保持一致")
    parser.add_argument("--compact-owner-id", default="", help="compact owner 标识；与 memory-resume 保持一致")
    parser.add_argument("--goal", default="", help="可选：当前任务目标；未传时尝试从 --from-compact handoff 读取")
    parser.add_argument("--next-action", action="append", default=[], help="可重复：恢复后的下一步动作")
    parser.add_argument("--acceptance", action="append", default=[], help="可重复：用户确认的验收条件")
    parser.add_argument("--constraint", action="append", default=[], help="可重复：用户确认的约束/禁止事项")
    parser.add_argument("--latest-test", action="append", default=[], help="可重复：最近已跑或必须跑的测试状态")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")


# LLM: _add_memory_compact_args 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _add_memory_compact_args(parser) -> None:
    """参数说明: memory compact 支持只读 dry-run 和非破坏性 apply 产物生成。"""

    parser.add_argument("--dry-run", action="store_true", default=True, help="只生成计划，不修改文件")
    parser.add_argument("--apply", action="store_true", help="生成非破坏性 compact context、自检和 apply ledger")
    parser.add_argument("--layer", choices=["all", "raw", "hook"], default="all", help="扫描哪一层归档")
    parser.add_argument("--date", help="只扫描某一天，格式 YYYY-MM-DD")
    parser.add_argument("--since", help="只看此时间之后的归档线索")
    parser.add_argument("--until", help="只看此时间之前的归档线索")
    parser.add_argument("--session-id", help="按 session_id 精确过滤")
    parser.add_argument("--request-id", help="按 request_id 精确过滤")
    parser.add_argument("--run-id", help="按 run_id 精确过滤")
    parser.add_argument("--task-id", help="按 task_id 精确过滤")
    parser.add_argument("--level", type=int, choices=[0, 1, 2, 3], help="只扫描指定 archive_level")
    parser.add_argument("--limit", type=int, default=None, help="最多纳入多少条归档记录；0 表示不限；默认不截断")
    parser.add_argument("--main-context-bundle-ref", default="", help="显式指定 main context bundle JSON 路径")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")


# LLM: add_memory_subcommands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 注册 argparse 参数和子命令，决定用户可见的命令形状。
def add_memory_subcommands(sub: argparse._SubParsersAction) -> None:
    add_home_runtime_subcommands(sub)
    memory_route = sub.add_parser("memory-route", help="按长期规则索引预览 memory 路由命中")
    memory_route.add_argument("query", nargs="?", default="", help="要路由的查询或用户任务")
    memory_route.add_argument("--index", help="路由索引文件；相对路径按 workspace root 解析")
    memory_route.add_argument("--mode", choices=["off", "soft", "strict"], help="路由模式；默认使用配置")
    memory_route.add_argument("--validate", action="store_true", help="只校验路由索引冲突、死链和重复关键词")
    memory_route.add_argument("--limit", type=int, default=None, help="最多显示多少条命中 route；0 表示不截断；默认读配置")
    memory_route.add_argument("--auto-read-limit", type=int, help="最多升级多少条规则路径；默认使用配置")
    memory_route.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_route.set_defaults(func=cmd_memory_route)

    memory_doctor = sub.add_parser("memory-doctor", help="诊断 memory 配置、路由索引和归档目录")
    memory_doctor.add_argument("--index", help="路由索引文件；相对路径按 workspace root 解析")
    memory_doctor.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_doctor.set_defaults(func=cmd_memory_doctor)

    _add_memory_archive_subcommands(sub)


# LLM: _add_memory_archive_subcommands keeps archive/compact CLI registration grouped and size-safe.
# 函数用途: 注册 archive search/resume/artifact/fact/compact 命令，避免主 memory 注册函数继续膨胀。
def _add_memory_archive_subcommands(sub: argparse._SubParsersAction) -> None:
    memory_archive_list = sub.add_parser("memory-archive-list", help="列出 memory raw/hook 归档记录")
    memory_archive_list.add_argument("--layer", choices=["all", "raw", "hook"], default="all", help="查看哪一层归档")
    memory_archive_list.add_argument("--date", help="只查看某一天，格式 YYYY-MM-DD")
    memory_archive_list.add_argument("--level", type=int, choices=[0, 1, 2, 3], help="只查看指定 archive_level")
    memory_archive_list.add_argument("--limit", type=int, default=None, help="最多显示多少条记录；默认读配置")
    memory_archive_list.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_archive_list.set_defaults(func=cmd_memory_archive_list)

    memory_archive_search = sub.add_parser("memory-archive-search", help="按字段搜索 memory raw/hook 归档")
    memory_archive_search.add_argument("query", nargs="?", default="", help="搜索关键词；可配合字段过滤")
    _add_archive_search_args(memory_archive_search)
    memory_archive_search.set_defaults(func=cmd_memory_archive_search)

    memory_resume = sub.add_parser("memory-resume", help="从归档和事实源生成恢复线索")
    memory_resume.add_argument("query", nargs="?", default="", help="恢复关键词；也可只传 request/run/session 过滤")
    _add_archive_resume_args(memory_resume)
    memory_resume.set_defaults(func=cmd_memory_resume)

    memory_artifact_read = sub.add_parser("memory-artifact-read", help="显式读取已登记 tool-output artifact 正文")
    memory_artifact_read.add_argument("artifact_ref", help="来自恢复包、manifest 或 tool output index 的 artifact path/hash/call_id")
    memory_artifact_read.add_argument("--offset", type=int, default=0, help="从正文第几个字符开始读取")
    memory_artifact_read.add_argument("--max-chars", type=int, default=None, help="最多读取多少字符；0 表示读取全部；默认读配置")
    memory_artifact_read.add_argument("--mode", choices=["slice", "head", "tail", "search"], default="slice", help="读取模式")
    memory_artifact_read.add_argument("--query", default="", help="mode=search 时搜索的关键词")
    memory_artifact_read.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    memory_artifact_read.set_defaults(func=cmd_memory_artifact_read)

    memory_fact_write = sub.add_parser("memory-fact-write", help="写入用户确认的 compact resume 补全事实")
    _add_memory_fact_write_args(memory_fact_write)
    memory_fact_write.set_defaults(func=cmd_memory_fact_write)

    memory_compact = sub.add_parser("memory-compact", help="预演 memory compact 计划")
    _add_memory_compact_args(memory_compact)
    memory_compact.set_defaults(func=cmd_memory_compact)


# LLM: add_local_store_subcommands 属于CLI 命令层；改行为前先对齐调用方和快照/单测。
# 函数用途: 注册 argparse 参数和子命令，决定用户可见的命令形状。
def add_local_store_subcommands(sub: argparse._SubParsersAction) -> None:
    local_store_status = sub.add_parser("local-store-status", help="查看本地事实源状态")
    local_store_status.set_defaults(func=cmd_local_store_status)

    local_search = sub.add_parser("local-search", help="搜索本地事实源 SQLite/FTS5 索引")
    local_search.add_argument("query", help="搜索关键词；为空时可用 local-store-status 看整体状态")
    local_search.add_argument("--limit", type=int, default=None, help="最多显示条数；默认读配置")
    local_search.add_argument("--source-type", help="按来源过滤，如 memory/gateway_request/subagent_run")
    local_search.add_argument("--visibility", help="按可见性过滤，默认不过滤")
    local_search.add_argument("--preview-chars", type=int, default=None, help="每条命中最多打印多少正文字符；-1 表示完整打印；默认读配置")
    local_search.set_defaults(func=cmd_local_search)

    local_index_memory = sub.add_parser("local-index-memory", help="把现有 JSONL 记忆补建到本地事实源")
    local_index_memory.set_defaults(func=cmd_local_index_memory)

    local_doctor = sub.add_parser("local-doctor", help="诊断 LocalStore、gateway 队列和 subagent 文件事实源")
    local_doctor.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    local_doctor.add_argument("--repair", action="store_true", help="处理超时 processing gateway 请求")
    local_doctor.add_argument("--limit", type=int, default=None, help="每类问题最多显示多少条；默认读配置")
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
