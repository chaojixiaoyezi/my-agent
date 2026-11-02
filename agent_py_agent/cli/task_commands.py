
from __future__ import annotations

"""CLI commands for task lifecycle management.

给人看的解释：
这里提供 `my-agent task list/show/abandon/pause/resume/search` 命令，
让用户能控制任务生命周期（ABANDONED/PAUSED/RESUMED）。
"""

import copy

from ..agent.task_registry import format_task_list, get_task_summary
from .common import int_arg_or_default, make_agent, make_lightweight_cli_agent_context
from .models import TaskIdOptions, TaskListOptions, TaskSearchOptions

# 参数减量第 3 批：任务列表默认条数不再是配置项，--limit 仍优先；home 运行时命令共用同一常量。
CLI_TASK_LIST_COUNT = 50
_CONTROL_VERBS = {"pause": "暂停", "abandon": "放弃", "resume": "恢复"}


# LLM: 只读查询走实际配置 owner 的同一 LocalStore 构造叶（TaskRegistry 投影），不按工作区另拼一个库，也不创建执行服务。
# 函数用途: 返回当前配置 owner 的任务投影库。
def _task_store(args):
    return make_lightweight_cli_agent_context(args).local_store


def cmd_task_list(args) -> int:

    options = _task_list_options(args)
    store = _task_store(args)

    tasks = store.task_registry.query_tasks(
        user_id=options.user_id,
        status=options.status,
        limit=options.limit,
    )

    result = format_task_list(tasks)
    print(result)
    return 0


def cmd_task_show(args) -> int:

    options = _task_id_options(args)
    summary = get_task_summary(_task_store(args), options.task_id)

    print(summary)
    return 0


def cmd_task_abandon(args) -> int:
    return _control_task(args, "abandon")


def cmd_task_pause(args) -> int:
    return _control_task(args, "pause")


def cmd_task_resume(args) -> int:
    return _control_task(args, "resume")


# LLM: 控制命令按实际配置 owner/home/store/workspace 构造原宿主（托管权威库挂载失败即拒绝，不退回投影写入），
#   全部状态变化经宿主正式生命周期入口里 control_task 的原授权、回执、撤权与清理；TaskRegistry 只随 canonical 投影刷新，这里绝不直写。
# 函数用途: 执行一次 task pause/abandon/resume，并把结构化回执翻译成用户提示与退出码。
def _control_task(args, action: str) -> int:
    options = _task_id_options(args)
    verb = _CONTROL_VERBS[action]
    host_args = copy.copy(args)
    host_args._defer_mcp_connect = True
    try:
        host = make_agent(host_args)
    except Exception as exc:  # 构造失败即无可用权威，不能退回未托管或投影控制。
        print(f"无法打开配置的任务权威，未{verb}任务 {options.task_id}：{type(exc).__name__}: {exc}")
        return 1
    try:
        report = host.subagents.lifecycle.control_task(host, options.task_id, action=action)
    except FileNotFoundError:
        print(f"任务 {options.task_id} 不存在。")
        return 1
    except PermissionError as exc:
        print(f"无权{verb}任务 {options.task_id}：{exc}")
        return 1
    except RuntimeError as exc:  # TaskControlError 携带拒绝码；其余运行时冲突即控制未确认。
        code = str(getattr(exc, "code", "") or "")
        if code:
            print(f"任务 {options.task_id} 不能{verb}：{exc}（{code}）")
        else:
            print(f"任务 {options.task_id} {verb}未确认：{type(exc).__name__}: {exc}")
        return 1
    print(_control_message(options.task_id, verb, report))
    return 0 if report.get("ok") else 1


# 函数用途: 把控制回执翻译成一行用户提示；清理确认只引用原宿主写入的回执状态。
def _control_message(task_id: str, verb: str, report: dict[str, object]) -> str:
    status = report.get("status")
    outcome = report.get("outcome")
    if outcome == "already_applied":
        return f"任务 {task_id} 已经是 {status} 状态。"
    if outcome == "not_controllable":
        return f"任务 {task_id} 当前状态是 {status}，不能{verb}。"
    if not report.get("ok"):
        failed = report.get("failed") or []
        return f"任务 {task_id} {verb}未完成（{outcome}，状态 {status}）：{failed}"
    if report.get("action") == "resume":
        return f"任务 {task_id} 已恢复为 {status}，将由原调度续跑同一 run。"
    cleanup = report.get("cleanup_state")
    hint = "原执行轮无需清理" if cleanup == "not_required" else (
        "原执行轮已确认清理" if cleanup == "confirmed" else "等待原执行轮宿主确认清理")
    tail = "，确认后可用 task-resume 恢复" if status == "PAUSED" else ""
    return f"任务 {task_id} 已{verb}，状态 {status}；执行权已关闭，{hint}{tail}。"


def cmd_task_search(args) -> int:

    options = _task_search_options(args)
    store = _task_store(args)

    # 获取所有任务，用 LLM 搜索（简单实现：关键词匹配）
    all_tasks = store.task_registry.query_tasks(limit=200)
    query = options.query.lower()

    matched = []
    for task in all_tasks:
        goal = task.get("goal", "").lower()
        task_id = task.get("task_id", "").lower()
        # 简单模糊匹配：query 是 goal 的子串，或 query 长度 >= 3 时 goal 包含 query
        if query in goal or query in task_id or len(query) >= 3 and any(word in goal for word in query.split()):
            matched.append(task)

    if not matched:
        print(f"没有找到匹配 \"{options.query}\" 的任务。")
        return 0

    result = format_task_list(matched)
    print(result)
    return 0


def _task_list_options(args) -> TaskListOptions:
    return TaskListOptions(
        config=args.config,
        user_id=args.user_id,
        status=args.status,
        limit=int_arg_or_default(args, "limit", CLI_TASK_LIST_COUNT),
    )


def _task_id_options(args) -> TaskIdOptions:
    return TaskIdOptions(config=args.config, task_id=args.task_id)


def _task_search_options(args) -> TaskSearchOptions:
    return TaskSearchOptions(config=args.config, query=args.query)
