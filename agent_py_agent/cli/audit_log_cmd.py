# LLM: CLI surface module; keep argparse/Typer wiring, stdout text, and service-call boundaries stable.
# 模块用途: 提供命令行入口或辅助函数，把用户命令转换成 agent 服务调用。

from __future__ import annotations

import json
import sys
import time

from ..agent.audit import AuditAction, AuditQuery


# LLM: _show_recent_users 属于logs CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _show_recent_users(query: AuditQuery, args) -> int:
    limit = getattr(args, "limit", None)
    if limit is None:
        limit = int(getattr(query.config, "cli_audit_limit", 100) or 0)
    users = query.recent_users(limit=limit)

    if not users:
        print("暂无活跃用户。", file=sys.stdout)
        return 0

    print(f"最近活跃用户（共 {len(users)} 人）：", file=sys.stdout)
    print()

    for i, u in enumerate(users, 1):
        last_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(u["last_action_time"]))
        print(f"{i}. {u['user_id']}", file=sys.stdout)
        print(f"   最后活动: {last_time}", file=sys.stdout)
        print()

    return 0


# LLM: _show_summary 属于logs CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _show_summary(query: AuditQuery, args) -> int:
    user_id = getattr(args, "user", None)
    stats = query.summary(user_id=user_id)

    print(f"审计统计（用户: {user_id or '全部'}）：", file=sys.stdout)
    print(f"  总操作数: {stats['total_actions']}", file=sys.stdout)

    if stats["by_action"]:
        print("  按动作类型：", file=sys.stdout)
        for action, count in sorted(stats["by_action"].items(), key=lambda x: -x[1]):
            print(f"    {action}: {count}", file=sys.stdout)

    if stats["by_status"]:
        print("  按状态：", file=sys.stdout)
        for status, count in sorted(stats["by_status"].items()):
            print(f"    {status}: {count}", file=sys.stdout)

    if stats["last_action_time"]:
        last_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stats["last_action_time"]))
        print(f"  最后操作: {last_str}", file=sys.stdout)

    return 0


# LLM: _show_entries 属于logs CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _show_entries(query: AuditQuery, args) -> int:
    result = query.query(
        user_id=getattr(args, "user", None),
        action=_audit_action_from_args(args),
        target_id=getattr(args, "target", None),
        target_type=getattr(args, "target_type", None),
        status=getattr(args, "status", None),
        limit=_audit_limit(query, args),
        offset=getattr(args, "offset", 0),
    )

    if not result.entries:
        print("没有匹配的审计记录。", file=sys.stdout)
        return 0

    print(f"找到 {result.total_count} 条审计记录（显示 {len(result.entries)} 条）：", file=sys.stdout)
    print(f"查询耗时: {result.query_time_ms:.2f}ms", file=sys.stdout)
    print()
    for entry in result.entries:
        _print_audit_entry(entry)
    return 0


# LLM: _audit_action_from_args 属于logs CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 完成本模块中的转换、分发或状态整理，供相邻流程继续使用。
def _audit_action_from_args(args) -> AuditAction | None:
    action_str = getattr(args, "action", None)
    if not action_str:
        return None
    try:
        return AuditAction(action_str)
    except ValueError:
        return None


# LLM: _print_audit_entry 属于logs CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: 整理 CLI 或报告展示文本，输出文案变化会影响快照断言。
def _print_audit_entry(entry) -> None:
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.timestamp))
    print(f"[{entry.status.upper()}] {entry.action} - {entry.user_id}", file=sys.stdout)
    print(f"  时间: {time_str}", file=sys.stdout)
    print(f"  通道: {entry.channel}", file=sys.stdout)
    print(f"  目标: {entry.target_type}/{entry.target_id}", file=sys.stdout)
    if entry.ip_address:
        print(f"  IP: {entry.ip_address}", file=sys.stdout)
    if entry.details:
        print(f"  详情: {json.dumps(entry.details, ensure_ascii=False)}", file=sys.stdout)
    print()

# LLM: cmd_audit_log 属于logs CLI；改行为前先对齐调用方和快照/单测。
# 函数用途: CLI 子命令入口，连接 argparse 参数、服务调用和最终退出码。
def cmd_audit_log(args) -> int:
    from .common import DEFAULT_CONFIG, load_config, resolve_workspace_root

    config_path = getattr(args, "config", str(DEFAULT_CONFIG))
    config = load_config(config_path)

    root = resolve_workspace_root(config, config_path)

    query = AuditQuery(config)

    # 最近活跃用户
    if getattr(args, "recent_users", False):
        return _show_recent_users(query, args)

    # 摘要统计
    if getattr(args, "summary", False):
        return _show_summary(query, args)

    # 清理旧条目
    if getattr(args, "cleanup", False):
        days = getattr(args, "days", None)
        if days is None:
            days = int(getattr(config, "cli_audit_cleanup_days", 90) or 0)
        count = query.cleanup_old_entries(days=days)
        print(f"已清理 {count} 条超过 {days} 天的审计记录。", file=sys.stdout)
        return 0

    # 查询日志
    return _show_entries(query, args)


# LLM: _audit_limit resolves audit CLI defaults from AgentConfig.
# 函数用途: audit-log 未显式传 limit 时，使用 agent_config.yaml 的 cli_audit_limit。
def _audit_limit(query: AuditQuery, args) -> int:
    value = getattr(args, "limit", None)
    if value is not None:
        return int(value)
    return int(getattr(query.config, "cli_audit_limit", 100) or 0)


__all__ = ["cmd_audit_log"]
