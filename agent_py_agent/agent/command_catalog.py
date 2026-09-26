# LLM: 公共命令目录只拥有声明、词法边界和展示事实；调用方仍负责身份、权限、控制回执及副作用。
# 模块用途: 让会话解析、Gateway 守门、帮助和 TUI 补全共用名称与别名，不导入 UI 或加载插件。

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .command_arguments import ArgumentSpec, CommandActionSpec


# LLM: 尾部正则保留核心自由正文，actions 描述结构化参数；两者不能从 usage/summary 反推或互相替代。
#   sensitive_input 是声明事实：命令正文可能带管理员密码，入口不得持久化原文（适配器不进持久入站队列、
#   TUI 不写输入历史、Gateway 回执只存脱敏正文）；它不授予任何权限。
# 类用途: 保存一种命令的名称、别名、帮助变体、输入行为及可选会话语法，不持有处理器或运行状态。
@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    summary: str
    aliases: tuple[str, ...] = ()
    help_variants: tuple[tuple[str, str], ...] = ()
    submit_on_enter: bool = False
    conversation_suffix: str | None = None
    multiline: bool = False
    namespace_separator: str = ""
    actions: tuple[CommandActionSpec, ...] = ()
    sensitive_input: bool = False


COMMAND_CATALOG = (
    CommandSpec("help", "/help", "查看帮助和可用命令", submit_on_enter=True),
    CommandSpec(
        "sessions", "/sessions", "列出当前用户最近会话和精确恢复命令", submit_on_enter=True
    ),
    CommandSpec(
        "status",
        "/status",
        "查看当前窗口状态",
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(.*))?$",
    ),
    CommandSpec(
        "context",
        "/context",
        "查看真实上下文用量",
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(.*))?$",
    ),
    CommandSpec("attach", '/attach "<图片或视频路径>"', "添加图片或视频附件（TUI）"),
    CommandSpec(
        "model",
        "/model",
        "新增或选择模型、接口和上下文窗口",
        help_variants=(
            ("/model <编号>", "为当前会话选择一个自己的或管理员共享的模型（IM 可用）"),
            ("/model default <编号>", "把一个模型设为新会话默认（IM 可用）"),
        ),
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(.*))?$",
    ),
    CommandSpec(
        "admin",
        "/admin <管理员密码>",
        "在 IM 私聊中验证管理员身份，之后本私聊按本机管理员运行",
        help_variants=(
            ("/admin status", "查看本私聊是否已绑定管理员身份（IM 私聊）"),
            ("/admin logout", "解除本私聊的管理员身份（IM 私聊）"),
        ),
        conversation_suffix=r"(?:\s+(.*))?$",
        sensitive_input=True,
    ),
    CommandSpec(
        "approve",
        "/approve <管理员密码>",
        "在 IM 私聊中批准本会话当前唯一等待确认的工具操作（仅本次）",
        conversation_suffix=r"(?:\s+(.*))?$",
        sensitive_input=True,
    ),
    CommandSpec(
        "deny",
        "/deny",
        "在 IM 私聊中拒绝本会话当前唯一等待确认的工具操作",
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(.*))?$",
        sensitive_input=True,
    ),
    CommandSpec(
        "permissions",
        "/permissions",
        "选择默认确认、自主工作或管理员 Full Access（F4）",
        submit_on_enter=True,
    ),
    CommandSpec(
        "compact",
        "/compact [补充要求]",
        "立即压缩已完成的对话历史",
        submit_on_enter=True,
        conversation_suffix=r"(?:\s*(.*))?$",
        multiline=True,
    ),
    CommandSpec(
        "effort",
        "/effort [auto|off|low|medium|high|max|default]",
        "查看或设置本会话的智能程度（推理强度）",
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(\S+))?\s*$",
    ),
    CommandSpec(
        "btw", "/btw <内容>", "向当前运行回合插入一条补充要求", conversation_suffix=r"(?:\s+(.*))?$"
    ),
    CommandSpec(
        "stop",
        "/stop",
        "停止当前任务并收回其所属资源",
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(.*))?$",
    ),
    CommandSpec(
        "interrupt",
        "/interrupt",
        "中断本轮；活动 Goal 仍可继续",
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(.*))?$",
    ),
    CommandSpec(
        "recover",
        "/recover",
        "查看结果未确认、正在阻塞本会话的上一轮操作",
        help_variants=(
            ("/recover recorded", "已核实操作生效并记下，解除阻塞后接着原任务继续"),
            ("/recover confirmed_noop", "已核实操作没有生效，解除阻塞后接着原任务继续"),
            ("/recover abandoned", "不再核对、接受未知后果，解除阻塞后接着原任务继续"),
        ),
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(.*))?$",
    ),
    CommandSpec(
        "restart",
        "/restart",
        "安全重启 Gateway（仅管理员）：先排空在跑的回合和工具，再换新进程",
        help_variants=(("/restart <原因>", "附带一句原因，写进 Gateway 日志"),),
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(.*))?$",
    ),
    CommandSpec(
        "goal",
        "/goal <时长> <名称> <任务>",
        "启动一个有名称的持续目标",
        help_variants=(
            ("/goal <名称> clear", "清除指定持续目标"),
            ("/goal pause|resume|clear", "控制当前目标的自动续跑"),
            ("/goal edit <目标>", "修改当前持续目标"),
        ),
        conversation_suffix=r"(?:\s+(.*))?$",
    ),
    CommandSpec(
        "verbose",
        "/verbose [off|on|full]",
        "查看或切换详细过程显示",
        aliases=("v",),
        submit_on_enter=True,
        conversation_suffix=r"(?:\s+(\S+))?\s*$",
    ),
    CommandSpec(
        "audit",
        "/audit help",
        "查看 Audit 命令帮助",
        help_variants=(
            ("/audit <名称> prepare <内容>", "准备或修订一个 Audit"),
            ("/audit <时长> <名称> <任务>", "启动一个 Audit"),
            ("/audit <名称> status", "查看指定 Audit"),
            ("/audit <名称> resume", "恢复因额度暂停的 Audit"),
            ("/audit <名称> clear", "停止指定 Audit"),
        ),
        conversation_suffix=r"(?:\s|$)",
    ),
    CommandSpec(
        "experiment",
        "/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>",
        "为本轮授权一次有预算、只观察的决策实验（输入上界为经验值）",
        # apply 仍只观察本轮；另授权宿主在最近证据满足规则时把本会话 skill_tool 改为 apply，用户后改优先。
        help_variants=(("/experiment apply skill_tool <时长> <HTTP次数> <输入token上限> <任务>",
                        "同上，并授权证据满足规则时自动把本会话 skill_tool 改为 apply"),),
        conversation_suffix=r"(?:\s+(.*))?$",
        multiline=True,
    ),
    CommandSpec("expand", "/expand [last|编号]", "展开一条已折叠的助手回复", submit_on_enter=True),
    CommandSpec("exit", "/exit", "退出聊天界面", aliases=("logout", "quit"), submit_on_enter=True),
    CommandSpec("memory", "/memory [查询内容]", "搜索记忆", submit_on_enter=True),
    CommandSpec("remember", "/remember <内容>", "保存一条记忆"),
    CommandSpec("prompt-file", "/prompt-file <路径>", "添加提示文件"),
    CommandSpec("show-prompt", "/show-prompt <问题>", "显示最终 Prompt 和回答"),
    CommandSpec(
        "plugins",
        "/plugins [管理动作]",
        "查看插件、管理本地安装与查询请求",
        help_variants=(("/plugins@<插件ID> [动作] [参数]", "使用已启用插件的动作"),),
        namespace_separator="@",
        actions=(
            CommandActionSpec("help", "查看管理动作的参数说明", (ArgumentSpec("action", "管理动作名称"),)),
            CommandActionSpec("list", "列出已安装插件", (
                ArgumentSpec("enabled", "只列已启用插件", options=("-e", "--enabled"), value_type="boolean"),
            ), available=False),
            CommandActionSpec("info", "查看插件说明和状态", (
                ArgumentSpec("plugin", "插件 ID", required=True),
            ), available=False),
            CommandActionSpec("install", "安装本地包，默认停用", (
                ArgumentSpec("source", "本地包路径", required=True, path=True),
            ), available=False),
            CommandActionSpec("configure", "保存停用插件的完整配置", (
                ArgumentSpec("plugin", "插件 ID", required=True),
                ArgumentSpec("source", "私有 JSON 配置文件", options=("-f", "--file"), required=True, path=True),
            ), available=False),
            CommandActionSpec("status", "查询当前会话的管理请求", (
                ArgumentSpec("request", "原请求编号", required=True),
            ), available=False),
            CommandActionSpec("enable", "启用已安装插件", (
                ArgumentSpec("plugin", "插件 ID", required=True),
                ArgumentSpec("confirm", "确认码：启用含可执行文件或外部解释器的插件时，先看回执再原样填入",
                             options=("--confirm",)),
            ), available=False),
            CommandActionSpec("disable", "停用插件并保留安装包", (
                ArgumentSpec("plugin", "插件 ID", required=True),
            ), available=False),
            CommandActionSpec("remove", "停用并卸载插件，保留用户产物", (
                ArgumentSpec("plugin", "插件 ID", required=True),
            ), available=False),
            CommandActionSpec("update", "用同一插件的新版本包替换已停用插件，配置结构不变时保留私有配置", (
                ArgumentSpec("plugin", "插件 ID", required=True),
                ArgumentSpec("source", "新版本本地包路径", required=True, path=True),
            ), available=False),
        ),
    ),
)


# LLM: 派生索引拒绝重复名称和别名，不能让后声明覆盖原身份；只在模块导入时处理静态数据。
# 函数用途: 构建不可修改的名称索引，让别名解析和入口提示共享同一条声明。
def _command_index(commands: tuple[CommandSpec, ...]) -> Mapping[str, CommandSpec]:
    index: dict[str, CommandSpec] = {}
    for spec in commands:
        for name in (spec.name, *spec.aliases):
            if name in index:
                raise ValueError(f"命令名称或别名重复：{name}")
            index[name] = spec
    return MappingProxyType(index)


COMMAND_INDEX = _command_index(COMMAND_CATALOG)
_CONVERSATION_MATCHERS = tuple(
    (
        spec,
        re.compile(
            r"^/(?:"
            + "|".join(re.escape(name) for name in (spec.name, *spec.aliases))
            + ")"
            + spec.conversation_suffix,
            re.IGNORECASE | (re.DOTALL if spec.multiline else 0),
        ),
    )
    for spec in COMMAND_CATALOG
    if spec.conversation_suffix is not None
)
_NAMESPACE_COMMANDS = tuple(spec for spec in COMMAND_CATALOG if spec.namespace_separator)
_SYSTEM_SLASH = re.compile(r"^/([a-z][a-z0-9_-]*)(?:\s|$)", re.IGNORECASE)


# LLM: 只返回规范名和原捕获正文，不执行 shlex、路径转换或正文修饰；Goal/Audit 的语义仍归会话模块。
# 函数用途: 按目录中的既有语法匹配会话命令，保留无空格 Compact、多行和显式别名行为。
def match_conversation_command(raw: str) -> tuple[str, str | None] | None:
    for spec, pattern in _CONVERSATION_MATCHERS:
        match = pattern.match(raw)
        if match is not None:
            return spec.name, match.group(1) if match.lastindex else None
    return None


# LLM: 这里只认消息开头的协议标记；保留插件后缀大小写且捕获异常 ID，避免错误命令落入模型或插话。
# 函数用途: 给所有入口提供统一的系统命令判据；普通文件路径和正文内斜杠不命中。
def system_slash_command_name(text: object) -> str:
    raw = str(text or "").strip()
    for spec in _NAMESPACE_COMMANDS:
        prefix = "/" + spec.name + spec.namespace_separator
        if raw.lower().startswith(prefix):
            head = raw.split(maxsplit=1)[0]
            return spec.name + head[len(spec.name) + 1 :]
    match = _SYSTEM_SLASH.match(raw)
    return str(match.group(1) or "").lower() if match is not None else ""


# LLM: 只按消息开头的系统命令名查目录声明，不解析参数、不读正文语义；插件命名空间、未知命令和普通文本都返回空串。
# 函数用途: 判断一条输入是否可能带管理员密码，供适配器、TUI 输入历史决定不落原文。
def sensitive_command_name(text: object) -> str:
    spec = COMMAND_INDEX.get(system_slash_command_name(text))
    return spec.name if spec is not None and spec.sensitive_input else ""


# LLM: 此错误只负责未知核心命令；插件参数结果归 plugin_commands，不能再增加第二套命名空间文案。
# 函数用途: 为未知系统命令生成中文提示，不尝试任何执行或聊天回退。
def unavailable_command_message(name: str) -> str:
    return f"不支持的系统命令：/{name}。输入 /help 查看当前界面支持的命令。"
