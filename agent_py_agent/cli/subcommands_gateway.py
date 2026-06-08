
from __future__ import annotations

"""logs / gateway / adapter / daemon / scenario subcommand registration helpers.

给人看的解释：
这个文件包含日志、gateway、适配器、守护进程和场景测试子命令的注册函数。
gateway、adapter、daemon、logs 和 scenario 命令注册逻辑集中在这里。
"""

import argparse

from .adapter import (
    cmd_adapter,
    cmd_adapter_file,
    cmd_adapter_start,
    cmd_adapter_status,
    cmd_adapter_stop,
)
from .common import DEFAULT_CAPABILITY_CONFIG, add_resume_context_switches
from .daemon import cmd_daemon
from .gateway_client import cmd_gateway, cmd_gateway_ask, cmd_gateway_result
from .gateway_process import (
    cmd_gateway_install,
    cmd_gateway_logs,
    cmd_gateway_restart,
    cmd_gateway_run,
    cmd_gateway_start,
    cmd_gateway_status,
    cmd_gateway_stop,
    cmd_gateway_uninstall,
)
from .logs import (
    cmd_logs,
    cmd_logs_hunt_ip,
    cmd_logs_ingest,
    cmd_logs_query,
    cmd_logs_status,
    cmd_logs_trace_case,
)
from .scenario import cmd_scenario_test
from .supervisor import (
    cmd_start_all,
    cmd_supervisor_run,
    cmd_supervisor_start,
    cmd_supervisor_status,
    cmd_supervisor_stop,
)


def _add_capability_config_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--capability-config",
        default=str(DEFAULT_CAPABILITY_CONFIG),
        help="能力路由配置文件路径，默认使用 config/capability_config.yaml",
    )


def add_logs_subcommands(sub: argparse._SubParsersAction) -> None:
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
    _add_log_query_filters(logs_query)
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


def _add_log_query_filters(logs_query: argparse.ArgumentParser) -> None:
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


def add_daemon_subcommand(sub: argparse._SubParsersAction) -> None:
    daemon = sub.add_parser("daemon", help="按配置启动前台常驻调度")
    _add_capability_config_arg(daemon)
    daemon.add_argument("--dry-run", action="store_false", dest="apply", default=None, help="覆盖配置：只生成报告，不写回")
    daemon.add_argument("--apply", action="store_true", default=None, help="覆盖配置：写回低风险动作和审计日志")
    daemon.add_argument("--start-runners", action="store_true", dest="start_runners", default=None, help="覆盖配置：配合 apply 调用真实模型执行 runner")
    daemon.add_argument("--no-start-runners", action="store_false", dest="start_runners", help="覆盖配置：不调用真实模型执行 runner")
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



def add_scenario_subcommand(sub: argparse._SubParsersAction) -> None:
    scenario = sub.add_parser("scenario-test", help="跑一轮隔离的真实全流程任务测试")
    _add_capability_config_arg(scenario)
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
    scenario.add_argument("--runner-concurrency", help="仅本次场景测试覆盖 runner_concurrency，例如 5 或 auto")
    scenario.add_argument("--runner-start-rate", help="仅本次场景测试覆盖 runner_start_rate，例如 10 或 auto")
    scenario.add_argument("--model-request-timeout", type=float, help="仅本次场景测试覆盖模型 request_timeout 秒数")
    scenario.add_argument("--dry-run", action="store_true", help="只调度不执行真实 runner API")
    scenario.add_argument("--planner", action="store_true", help="dispatch 时启用父代理 planner")
    scenario.add_argument("--direct", action="store_true", help="不经过 gateway，直接用当前进程跑主代理派工")
    scenario.add_argument("--skill-dir", action="append", help="额外 skill 目录，可多次传入")
    scenario.set_defaults(func=cmd_scenario_test)



def _add_gateway_start_stop_subcommands(gateway_sub):
    gateway_start = gateway_sub.add_parser("start", help="启动后台 gateway")
    gateway_start.add_argument("--force", action="store_true", help="已有 gateway 运行时先尝试停止再启动")
    gateway_start.add_argument("--force-lock", action="store_true", help="传给内部 daemon，强制覆盖已有 dispatch watch lock")
    gateway_start.set_defaults(func=cmd_gateway_start)

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


def _add_gateway_run_subcommand(gateway_sub):
    gateway_run = gateway_sub.add_parser("run", help="内部命令：前台运行 gateway 循环")
    _add_capability_config_arg(gateway_run)
    gateway_run.add_argument("--dry-run", action="store_false", dest="apply", default=None, help="覆盖配置：只生成报告，不写回")
    gateway_run.add_argument("--apply", action="store_true", default=None, help="覆盖配置：写回低风险动作和审计日志")
    gateway_run.add_argument("--start-runners", action="store_true", dest="start_runners", default=None, help="覆盖配置：配合 apply 调用真实模型执行 runner")
    gateway_run.add_argument("--no-start-runners", action="store_false", dest="start_runners", help="覆盖配置：不调用真实模型执行 runner")
    gateway_run.add_argument("--planner", action="store_true", dest="planner", default=None, help="覆盖配置：启用父代理 LLM planner")
    gateway_run.add_argument("--no-planner", action="store_false", dest="planner", help="覆盖配置：关闭父代理 LLM planner")
    gateway_run.add_argument("--interval", type=float, help="覆盖配置：每轮间隔秒数，0 表示不等待")
    gateway_run.add_argument("--max-runners", help="覆盖配置：每轮最多推进多少个 runner；auto 表示保守自适应，0 表示不执行 runner")
    gateway_run.add_argument("--limit", type=int, help="覆盖配置：每个阶段最多处理多少条记录，0 表示不限制")
    gateway_run.add_argument("--max-cycles", type=int, help="覆盖配置：最多循环次数，0 表示持续运行")
    gateway_run.add_argument("--force-lock", action="store_true", help="强制覆盖已有 watch lock")
    gateway_run.add_argument("--workspace-root", default="", help=argparse.SUPPRESS)
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


def _add_gateway_ask_result_subcommands(gateway_sub):
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


def _add_gateway_supervisor_subcommands(gateway_sub):
    supervisor_start = gateway_sub.add_parser("supervisor-start", help="启动 gateway 看门狗进程（自动重启崩溃的 gateway）")
    supervisor_start.set_defaults(func=cmd_supervisor_start)

    supervisor_stop = gateway_sub.add_parser("supervisor-stop", help="停止 gateway 看门狗进程")
    supervisor_stop.add_argument("--timeout", type=float, help="等待停止的秒数")
    supervisor_stop.set_defaults(func=cmd_supervisor_stop)

    supervisor_status = gateway_sub.add_parser("supervisor-status", help="查看 supervisor 和 gateway 状态")
    supervisor_status.set_defaults(func=cmd_supervisor_status)

    supervisor_run = gateway_sub.add_parser("supervisor", help="内部命令：前台运行 supervisor 循环")
    supervisor_run.add_argument("--workspace-root", help="工作区根目录")
    supervisor_run.add_argument("--heartbeat-timeout", type=float, default=120.0, help="心跳超时秒数")
    supervisor_run.add_argument("--check-interval", type=float, default=10.0, help="健康检查间隔秒数")
    supervisor_run.add_argument("--max-restart-attempts", type=int, default=5, help="最大重启次数")
    supervisor_run.add_argument("--restart-cooldown", type=float, default=30.0, help="重启冷却时间（秒）")
    supervisor_run.set_defaults(func=cmd_supervisor_run)


def _add_gateway_service_subcommands(gateway_sub):
    start_all = gateway_sub.add_parser("start-all", help="一键启动 gateway（带 supervisor）+ 所有适配器")
    start_all.add_argument(
        "--adapter",
        choices=["feishu", "qq", "all", "none"],
        default="all",
        help="启动哪些通道适配器；默认 all；none 表示不启动适配器",
    )
    start_all.set_defaults(func=cmd_start_all)

    gateway_install = gateway_sub.add_parser("install", help="安装 gateway 系统服务（Linux systemd 或 macOS launchd）")
    gateway_install.add_argument("--force", action="store_true", help="强制重新安装已存在的服务")
    gateway_install.set_defaults(func=cmd_gateway_install)

    gateway_uninstall = gateway_sub.add_parser("uninstall", help="卸载 gateway 系统服务（Linux systemd 或 macOS launchd）")
    gateway_uninstall.set_defaults(func=cmd_gateway_uninstall)


def add_gateway_subcommands(sub: argparse._SubParsersAction) -> None:
    gateway = sub.add_parser("gateway", help="管理后台 gateway 进程")
    gateway_sub = gateway.add_subparsers(dest="gateway_command")
    gateway.set_defaults(func=cmd_gateway)

    _add_gateway_start_stop_subcommands(gateway_sub)
    _add_gateway_run_subcommand(gateway_sub)
    _add_gateway_ask_result_subcommands(gateway_sub)
    _add_gateway_supervisor_subcommands(gateway_sub)
    _add_gateway_service_subcommands(gateway_sub)


def add_adapter_subcommand(sub: argparse._SubParsersAction) -> None:
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

    adapter_start = adapter_sub.add_parser("start", help="启动通道适配器（feishu / qq / all）")
    adapter_start.add_argument(
        "--channel",
        choices=["feishu", "qq", "all"],
        default="all",
        help="指定要启动的通道，默认 all",
    )
    adapter_start.add_argument("--daemon", action="store_true", help="以后台守护进程模式运行，写入 PID 文件")
    adapter_start.add_argument("--pid-file", help="指定 PID 文件路径；默认为 gateway workspace 下的 adapter.pid")
    adapter_start.set_defaults(func=cmd_adapter_start)

    adapter_status = adapter_sub.add_parser("status", help="查看通道适配器状态")
    adapter_status.add_argument("--pid-file", help="指定 PID 文件路径；默认为 gateway workspace 下的 adapter.pid")
    adapter_status.set_defaults(func=cmd_adapter_status)

    adapter_stop = adapter_sub.add_parser("stop", help="停止所有通道适配器")
    adapter_stop.add_argument("--pid-file", help="指定 PID 文件路径；默认为 gateway workspace 下的 adapter.pid")
    adapter_stop.add_argument("--timeout", type=float, default=10.0, help="等待优雅停止的超时秒数")
    adapter_stop.set_defaults(func=cmd_adapter_stop)
