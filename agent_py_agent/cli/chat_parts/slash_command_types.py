# LLM: 本模块定义 chat slash 命令的结构化目录项与执行上下文；帮助、补全和 dispatcher 必须共享这些事实而不是解析展示文案。
# 模块用途: 保存斜杠命令的名称/用法说明，以及命令处理器需要的 agent、配置和输出入口。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


# LLM: SlashCommandSpec 只描述用户可见命令目录；真正控制权限仍由 typed parser/executor 决定，补全结果不能直接执行动作。
# 类用途: 为帮助页和输入补全提供一条命令的固定用法与简短说明。
@dataclass(frozen=True)
class SlashCommandSpec:
    name: str
    usage: str
    summary: str
    submit_on_enter: bool = False


# LLM: SlashCommandContext 汇总 dispatcher 的可信依赖；普通输入文本之外的控制结果只经 control_executor 返回。
# 类用途: 把 agent、记忆限制、运行注入、提示文件和输出函数交给通用命令处理器。
@dataclass
class SlashCommandContext:
    agent: Any
    memory_limit: int
    runtime_inject: list[str]
    prompt_files: list[str]
    print_line: Callable[[str], None]
    control_executor: Callable[[Any], Any] | None = None
    conversation_id: str = "default"


# LLM: 命令目录属于纯数据，TUI 补全可在不导入 Conversation/Gateway 执行器时读取；执行语义仍由 slash_commands 和 typed parser 拥有。
# 常量用途: 为帮助页、补全菜单和提交行为提供唯一的斜杠命令目录。
CHAT_SLASH_COMMANDS = (
    SlashCommandSpec("help", "/help", "查看帮助和可用命令", submit_on_enter=True),
    SlashCommandSpec(
        "sessions",
        "/sessions",
        "列出当前用户最近会话和精确恢复命令",
        submit_on_enter=True,
    ),
    SlashCommandSpec("status", "/status", "查看当前窗口状态", submit_on_enter=True),
    SlashCommandSpec("context", "/context", "查看真实上下文用量", submit_on_enter=True),
    SlashCommandSpec("model", "/model", "新增或选择模型、接口和上下文窗口", submit_on_enter=True),
    SlashCommandSpec("permissions", "/permissions", "选择默认确认、自主工作或管理员 Full Access（F4）", submit_on_enter=True),
    SlashCommandSpec(
        "compact",
        "/compact [补充要求]",
        "立即压缩已完成的对话历史",
        submit_on_enter=True,
    ),
    SlashCommandSpec(
        "effort",
        "/effort [low|medium|high|max|auto]",
        "查看或设置模型推理强度（接口支持时）",
        submit_on_enter=True,
    ),
    SlashCommandSpec("btw", "/btw <内容>", "向当前运行回合插入一条补充要求"),
    SlashCommandSpec("stop", "/stop", "停止当前运行回合", submit_on_enter=True),
    SlashCommandSpec(
        "goal",
        "/goal <时长> <名称> <任务>",
        "启动一个有名称的持续目标",
    ),
    SlashCommandSpec("goal", "/goal <名称> clear", "停止指定持续目标"),
    SlashCommandSpec("goal", "/goal pause|resume|clear", "控制当前持续目标"),
    SlashCommandSpec("goal", "/goal edit <目标>", "修改当前持续目标"),
    SlashCommandSpec(
        "verbose",
        "/verbose [off|on|full]",
        "查看或切换详细过程显示",
        submit_on_enter=True,
    ),
    SlashCommandSpec("audit", "/audit help", "查看 Audit 命令帮助"),
    SlashCommandSpec("audit", "/audit <名称> prepare <内容>", "准备或修订一个 Audit"),
    SlashCommandSpec("audit", "/audit <时长> <名称> <任务>", "启动一个 Audit"),
    SlashCommandSpec("audit", "/audit <名称> status", "查看指定 Audit"),
    SlashCommandSpec("audit", "/audit <名称> resume", "恢复因额度暂停的 Audit"),
    SlashCommandSpec("audit", "/audit <名称> clear", "停止指定 Audit"),
    SlashCommandSpec(
        "expand",
        "/expand [last|编号]",
        "展开一条已折叠的助手回复",
        submit_on_enter=True,
    ),
    SlashCommandSpec("exit", "/exit", "退出聊天界面", submit_on_enter=True),
    SlashCommandSpec("memory", "/memory [查询内容]", "搜索记忆", submit_on_enter=True),
    SlashCommandSpec("remember", "/remember <内容>", "保存一条记忆"),
    SlashCommandSpec("prompt-file", "/prompt-file <路径>", "添加提示文件"),
    SlashCommandSpec("show-prompt", "/show-prompt <问题>", "显示最终 Prompt 和回答"),
)


__all__ = ["CHAT_SLASH_COMMANDS", "SlashCommandContext", "SlashCommandSpec"]
