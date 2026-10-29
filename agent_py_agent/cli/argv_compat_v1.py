# LLM: 本模块只声明规范路由并纯解码发布N的旧argv；不加载配置、执行命令或授予权限。
# 模块用途: 在单一规范解析树的输入边界兼容旧CLI，并禁止恢复旧平铺注册。
from __future__ import annotations

import argparse
from collections.abc import Sequence

CANONICAL_ROOTS = frozenset(['adapter', 'admin', 'audit', 'background', 'chat', 'collaboration', 'config', 'context', 'dev', 'feishu', 'gateway', 'guidance', 'home', 'local-store', 'memory', 'resume', 'run', 'runtime', 'skills', 'status', 'subagents', 'task', 'timeline', 'update'])

CLI_ROOT_PATHS = {'status': ('status',),
 'timeline': ('timeline',),
 'run': ('run',),
 'remember': ('memory', 'remember'),
 'memory-list': ('memory', 'list'),
 'memory-search': ('memory', 'search'),
 'chat': ('chat',),
 'resume': ('resume',),
 'context-bundle': ('context', 'bundle'),
 'bench-model': ('dev', 'bench-model'),
 'contracts': ('dev', 'contracts'),
 'memory': ('memory',),
 'home-status': ('home', 'status'),
 'home-retention': ('home', 'retention'),
 'home-index-rebuild': ('home', 'index', 'rebuild'),
 'memory-daily-list': ('memory', 'daily', 'list'),
 'task-workspace-list': ('task', 'workspace', 'list'),
 'runtime-stale-attempts': ('runtime', 'stale-attempts'),
 'memory-route': ('memory', 'route'),
 'memory-doctor': ('memory', 'doctor'),
 'memory-archive-list': ('memory', 'archive', 'list'),
 'memory-archive-search': ('memory', 'archive', 'search'),
 'memory-resume': ('memory', 'archive', 'resume'),
 'memory-artifact-read': ('memory', 'artifact', 'read'),
 'memory-fact-write': ('memory', 'fact', 'write'),
 'memory-compact': ('memory', 'compact'),
 'skills': ('skills',),
 'local-store-status': ('local-store', 'status'),
 'local-search': ('local-store', 'search'),
 'local-index-memory': ('local-store', 'index-memory'),
 'local-doctor': ('local-store', 'doctor'),
 'local-rebuild': ('local-store', 'rebuild'),
 'guidance-send': ('guidance', 'send'),
 'spawn-subagents': ('subagents', 'create'),
 'subagents': ('subagents', 'list'),
 'subagents-leadership-recovery-plan': ('subagents', 'leadership', 'plan'),
 'subagents-leadership-recovery-apply': ('subagents', 'leadership', 'apply'),
 'subagents-hierarchy': ('subagents', 'hierarchy'),
 'subagents-recovery-tree': ('subagents', 'recovery-tree'),
 'subagents-due-check': ('subagents', 'due-check'),
 'subagents-budget': ('subagents', 'budget'),
 'subagents-probe': ('subagents', 'probe'),
 'subagents-plan-actions': ('subagents', 'actions', 'plan'),
 'subagents-apply-actions': ('subagents', 'actions', 'apply'),
 'subagents-route-capabilities': ('subagents', 'route-capabilities'),
 'subagents-tests': ('subagents', 'tests'),
 'subagents-patches': ('subagents', 'patches'),
 'subagents-dispatch': ('subagents', 'dispatch'),
 'subagent-context': ('subagents', 'context'),
 'subagent-run': ('subagents', 'run'),
 'subagent': ('subagents', 'show'),
 'daemon': ('subagents', 'daemon'),
 'scenario-test': ('dev', 'scenario-test'),
 'gateway': ('gateway',),
 'adapter': ('adapter',),
 'task-show': ('task', 'show'),
 'task-list': ('task', 'list'),
 'task-abandon': ('task', 'abandon'),
 'task-pause': ('task', 'pause'),
 'task-resume': ('task', 'resume'),
 'task-search': ('task', 'search'),
 'audit-log': ('audit', 'log'),
 'background-main-agent': ('background',),
 'collaboration': ('collaboration',),
 'update': ('update',),
 'config-set': ('config', 'set'),
 'config-get': ('config', 'get'),
 'feishu': ('feishu',),
 'admin-password': ('admin', 'password'),
 'admin-identities': ('admin', 'identities')}

CLI_V1_PATHS = {('status',): ('status',),
 ('timeline',): ('timeline',),
 ('run',): ('run',),
 ('remember',): ('memory', 'remember'),
 ('memory-list',): ('memory', 'list'),
 ('memory-search',): ('memory', 'search'),
 ('chat',): ('chat',),
 ('resume',): ('resume',),
 ('context-bundle',): ('context', 'bundle'),
 ('context-bundle', 'latest'): ('context', 'bundle', 'latest'),
 ('bench-model',): ('dev', 'bench-model'),
 ('contracts',): ('dev', 'contracts'),
 ('contracts', 'status'): ('dev', 'contracts', 'status'),
 ('memory',): ('memory',),
 ('memory', 'candidates'): ('memory', 'candidates'),
 ('memory', 'candidates', 'list'): ('memory', 'candidates', 'list'),
 ('memory', 'candidates', 'review'): ('memory', 'candidates', 'review'),
 ('memory', 'candidates', 'promote'): ('memory', 'candidates', 'promote'),
 ('memory', 'agents'): ('memory', 'agents'),
 ('memory', 'agents', 'cleanup'): ('memory', 'agents', 'cleanup'),
 ('memory', 'curator'): ('memory', 'curator'),
 ('memory', 'curator', 'status'): ('memory', 'curator', 'status'),
 ('memory', 'curator', 'run'): ('memory', 'curator', 'run'),
 ('memory', 'curator', 'recover'): ('memory', 'curator', 'recover'),
 ('memory', 'retention'): ('memory', 'retention'),
 ('memory', 'retention', 'plan'): ('memory', 'retention', 'plan'),
 ('memory', 'retention', 'apply'): ('memory', 'retention', 'apply'),
 ('memory', 'doctor'): ('memory', 'doctor'),
 ('memory', 'migrate'): ('memory', 'migrate'),
 ('memory', 'vectors'): ('memory', 'vectors'),
 ('memory', 'vectors', 'status'): ('memory', 'vectors', 'status'),
 ('memory', 'vectors', 'rebuild'): ('memory', 'vectors', 'rebuild'),
 ('home-status',): ('home', 'status'),
 ('home-retention',): ('home', 'retention'),
 ('home-index-rebuild',): ('home', 'index', 'rebuild'),
 ('memory-daily-list',): ('memory', 'daily', 'list'),
 ('task-workspace-list',): ('task', 'workspace', 'list'),
 ('runtime-stale-attempts',): ('runtime', 'stale-attempts'),
 ('memory-route',): ('memory', 'route'),
 ('memory-doctor',): ('memory', 'doctor'),
 ('memory-archive-list',): ('memory', 'archive', 'list'),
 ('memory-archive-search',): ('memory', 'archive', 'search'),
 ('memory-resume',): ('memory', 'archive', 'resume'),
 ('memory-artifact-read',): ('memory', 'artifact', 'read'),
 ('memory-fact-write',): ('memory', 'fact', 'write'),
 ('memory-compact',): ('memory', 'compact'),
 ('skills',): ('skills',),
 ('skills', 'learned'): ('skills', 'learned'),
 ('skills', 'learned', 'list'): ('skills', 'learned', 'list'),
 ('skills', 'learned', 'show'): ('skills', 'learned', 'show'),
 ('skills', 'learned', 'revert'): ('skills', 'learned', 'revert'),
 ('skills', 'learned', 'remove'): ('skills', 'learned', 'remove'),
 ('skills', 'learned', 'proposals'): ('skills', 'learned', 'proposals', 'list'),
 ('skills', 'learned', 'proposal-show'): ('skills', 'learned', 'proposals', 'show'),
 ('skills', 'learned', 'confirm'): ('skills', 'learned', 'proposals', 'confirm'),
 ('skills', 'learned', 'reject'): ('skills', 'learned', 'proposals', 'reject'),
 ('skills', 'proposals'): ('skills', 'proposals'),
 ('skills', 'proposals', 'list'): ('skills', 'proposals', 'list'),
 ('skills', 'proposals', 'show'): ('skills', 'proposals', 'show'),
 ('skills', 'proposals', 'confirm'): ('skills', 'proposals', 'confirm'),
 ('skills', 'proposals', 'reject'): ('skills', 'proposals', 'reject'),
 ('local-store-status',): ('local-store', 'status'),
 ('local-search',): ('local-store', 'search'),
 ('local-index-memory',): ('local-store', 'index-memory'),
 ('local-doctor',): ('local-store', 'doctor'),
 ('local-rebuild',): ('local-store', 'rebuild'),
 ('guidance-send',): ('guidance', 'send'),
 ('spawn-subagents',): ('subagents', 'create'),
 ('subagents',): ('subagents', 'list'),
 ('subagents-leadership-recovery-plan',): ('subagents', 'leadership', 'plan'),
 ('subagents-leadership-recovery-apply',): ('subagents', 'leadership', 'apply'),
 ('subagents-hierarchy',): ('subagents', 'hierarchy'),
 ('subagents-recovery-tree',): ('subagents', 'recovery-tree'),
 ('subagents-due-check',): ('subagents', 'due-check'),
 ('subagents-budget',): ('subagents', 'budget'),
 ('subagents-probe',): ('subagents', 'probe'),
 ('subagents-plan-actions',): ('subagents', 'actions', 'plan'),
 ('subagents-apply-actions',): ('subagents', 'actions', 'apply'),
 ('subagents-route-capabilities',): ('subagents', 'route-capabilities'),
 ('subagents-tests',): ('subagents', 'tests'),
 ('subagents-patches',): ('subagents', 'patches'),
 ('subagents-dispatch',): ('subagents', 'dispatch'),
 ('subagent-context',): ('subagents', 'context'),
 ('subagent-run',): ('subagents', 'run'),
 ('subagent',): ('subagents', 'show'),
 ('daemon',): ('subagents', 'daemon'),
 ('scenario-test',): ('dev', 'scenario-test'),
 ('gateway',): ('gateway',),
 ('gateway', 'start'): ('gateway', 'start'),
 ('gateway', 'stop'): ('gateway', 'stop'),
 ('gateway', 'restart'): ('gateway', 'restart'),
 ('gateway', 'logs'): ('gateway', 'logs'),
 ('gateway', 'run'): ('gateway', 'run'),
 ('gateway', 'status'): ('gateway', 'status'),
 ('gateway', 'ask'): ('gateway', 'ask'),
 ('gateway', 'result'): ('gateway', 'result'),
 ('gateway', 'supervisor-install-service'): ('gateway', 'supervisor', 'install-service'),
 ('gateway', 'supervisor-uninstall-service'): ('gateway', 'supervisor', 'uninstall-service'),
 ('gateway', 'maintenance-lock'): ('gateway', 'maintenance-lock'),
 ('gateway', 'maintenance-unlock'): ('gateway', 'maintenance-unlock'),
 ('gateway', 'supervisor-start'): ('gateway', 'supervisor', 'start'),
 ('gateway', 'supervisor-stop'): ('gateway', 'supervisor', 'stop'),
 ('gateway', 'supervisor-status'): ('gateway', 'supervisor', 'status'),
 ('gateway', 'supervisor'): ('gateway', 'supervisor'),
 ('gateway', 'start-all'): ('gateway', 'start-all'),
 ('gateway', 'install'): ('gateway', 'install'),
 ('gateway', 'uninstall'): ('gateway', 'uninstall'),
 ('adapter',): ('adapter',),
 ('adapter', 'file'): ('adapter', 'file'),
 ('adapter', 'start'): ('adapter', 'start'),
 ('adapter', 'status'): ('adapter', 'status'),
 ('adapter', 'stop'): ('adapter', 'stop'),
 ('task-show',): ('task', 'show'),
 ('task-list',): ('task', 'list'),
 ('task-abandon',): ('task', 'abandon'),
 ('task-pause',): ('task', 'pause'),
 ('task-resume',): ('task', 'resume'),
 ('task-search',): ('task', 'search'),
 ('audit-log',): ('audit', 'log'),
 ('background-main-agent',): ('background',),
 ('background-main-agent', 'message'): ('background', 'message'),
 ('background-main-agent', 'bind-task'): ('background', 'bind-task'),
 ('background-main-agent', 'observe'): ('background', 'observe'),
 ('background-main-agent', 'status'): ('background', 'status'),
 ('background-main-agent', 'tick'): ('background', 'tick'),
 ('background-main-agent', 'service'): ('background', 'service'),
 ('collaboration',): ('collaboration',),
 ('collaboration', 'overview'): ('collaboration', 'overview'),
 ('collaboration', 'list'): ('collaboration', 'list'),
 ('collaboration', 'status'): ('collaboration', 'status'),
 ('collaboration', 'update-request'): ('collaboration', 'update-request'),
 ('collaboration', 'update-status'): ('collaboration', 'update-status'),
 ('update',): ('update',),
 ('config-set',): ('config', 'set'),
 ('config-get',): ('config', 'get'),
 ('feishu',): ('feishu',),
 ('feishu', 'connect'): ('feishu', 'connect'),
 ('admin-password',): ('admin', 'password'),
 ('admin-password', 'set'): ('admin', 'password', 'set'),
 ('admin-password', 'status'): ('admin', 'password', 'status'),
 ('admin-password', 'clear'): ('admin', 'password', 'clear'),
 ('admin-identities',): ('admin', 'identities'),
 ('admin-identities', 'list'): ('admin', 'identities', 'list'),
 ('admin-identities', 'remove'): ('admin', 'identities', 'remove')}


# LLM: 只跳过声明的全局选项及其值，不扫描正文、路径值或 -- 后的内容找命令。
# 函数用途: 定位 argv 中真正的根命令，避免把参数值当作历史入口改写。
def _command_offset(argv: Sequence[str], options: dict[str, bool]) -> int:
    index = 0
    while index < len(argv):
        token = argv[index]
        option, equal, _ = token.partition("=")
        if option not in options:
            candidates = [name for name in options if option.startswith("--") and name.startswith(option)]
            if len(candidates) != 1:
                return index
            option = candidates[0]
        index += 1 + int(options[option] and not equal)
    return index


# LLM: 发布N仅接受精确v1路由，N+1可移除此纯转换器；未知输入交给唯一规范parser拒绝。
# 函数用途: 将旧命令前缀转换为规范路径，保留全局选项、业务参数与值的字节内容。
def decode_v1_argv(argv: Sequence[str], options: dict[str, bool], *, canonical_paths=None) -> list[str]:
    result = list(argv)
    offset = _command_offset(result, options)
    words = tuple(result[offset:])
    old = max((p for p in CLI_V1_PATHS if words[:len(p)] == p), key=len, default=())
    if not old:
        return result
    target = CLI_V1_PATHS[old]
    paths = canonical_paths if canonical_paths is not None else {
        path[:length] for path in CLI_V1_PATHS.values() for length in range(1, len(path) + 1)
    }
    canonical = max((p for p in paths if words[:len(p)] == p), key=len, default=())
    group_help = words[len(canonical):len(canonical) + 1] in {("--help",), ("-h",)}
    if canonical and (len(canonical) > len(old) or len(canonical) == len(old) and (old == target or group_help)):
        return result
    tail = result[offset + len(old):]
    if old == ("memory-doctor",):
        tail = [*tail, "--compat-view", "v1"]
    return [*result[:offset], *target, *tail]


# LLM: 从同一真实解析树收集命名空间和扩展路径，不构建或运行任何旧parser。
# 函数用途: 为输入边界识别实际可达的规范子命令。
def _registered_command_paths(parser, prefix=()):
    paths = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                path = (*prefix, name)
                paths.append(path)
                paths.extend(_registered_command_paths(child, path))
    return paths


# LLM: 规范树只有一个argparse根；子解析器不做旧argv重写，namespace只复用同一subparser action。
# 类用途: 为单一规范解析树增加根输入解码及命名空间复用。
class CanonicalArgumentParser(argparse.ArgumentParser):
    # LLM: 注册阶段无旧输入边界；只有build_parser显式标记的根才允许纯解码。
    # 函数用途: 初始化普通解析器及根边界标记。
    def __init__(self, prog=None, **kwargs):
        self.legacy_input_boundary = False
        super().__init__(prog=prog, **kwargs)

    # LLM: 已创建的命名空间不能再拥有第二棵子树；原业务dest由原注册器保持。
    # 函数用途: 复用已存在的子命令集合，供按领域注册的命令共享父节点。
    def add_subparsers(self, **kwargs):
        existing = next((a for a in self._actions if isinstance(a, argparse._SubParsersAction)), None)
        if existing is not None:
            if "dest" in kwargs:
                existing.dest = kwargs["dest"]
            if "required" in kwargs:
                existing.required = kwargs["required"]
            return existing
        return super().add_subparsers(**kwargs)

    # LLM: 仅根解析前做一次版本化转换；不试旧parser、不捕获语法错误后降级重试。
    # 函数用途: 在根输入边界解码旧命令，然后进入同一规范树。
    def parse_known_args(self, args=None, namespace=None):
        if self.legacy_input_boundary:
            import sys
            values = list(sys.argv[1:] if args is None else args)
            if any(action.option_strings and action.nargs not in {None, 0} for action in self._actions):
                raise ValueError("根输入兼容仅支持零值或单值选项，拒绝猜测多值选项边界")
            options = {option: action.nargs != 0 for action in self._actions for option in action.option_strings}
            args = decode_v1_argv(values, options, canonical_paths=_registered_command_paths(self))
        return super().parse_known_args(args, namespace)


# LLM: 注册器把声明直接放进唯一规范树，不先建旧parser再复制；所有处理器仍为原业务函数。
# 类用途: 将现有命令族接入规范命名空间，保留业务Namespace字段但不注册旧平铺入口。
class CanonicalCommandRegistrar:
    # LLM: choices直接指向真实规范根；第三方根扩展仍保留，但不能覆盖内置命名空间。
    # 函数用途: 绑定唯一根解析器并记录已经完成的注册。
    def __init__(self, root):
        self.root = root
        self.choices = root.add_subparsers().choices
        self._registered = set()

    # LLM: 中间节点默认只显示帮助，不能继承裸启动的聊天处理器而发起业务。
    # 函数用途: 沿规范路径找到或创建命名空间，返回唯一实际解析器。
    def add_parser(self, name, **kwargs):
        if name in self._registered:
            raise argparse.ArgumentError(self.root.add_subparsers(), f"命令已注册：{name}")
        aliases = set(kwargs.get("aliases", ()))
        if aliases & (CANONICAL_ROOTS | CLI_ROOT_PATHS.keys() | self.choices.keys()):
            raise argparse.ArgumentError(self.root.add_subparsers(), "别名不得占用规范根或历史路由")
        path = CLI_ROOT_PATHS.get(name, (name,))
        parser = self.root
        for index, part in enumerate(path):
            sub = parser.add_subparsers()
            final = index == len(path) - 1
            if part not in sub.choices:
                parser = sub.add_parser(part, **(kwargs if final else {"help": part}))
                parser.set_defaults(func=_namespace_help, _namespace_parser=parser)
            else:
                parser = sub.choices[part]
                if final and name not in CLI_ROOT_PATHS:
                    raise argparse.ArgumentError(sub, f"命令已注册：{name}")
        parser.set_defaults(command=name)
        self._registered.add(name)
        return parser


# LLM: 只呈现已装配的规范帮助；不能创建Agent、查网络或执行命令。
# 函数用途: 处理未指定动作的合成命名空间。
def _namespace_help(args) -> int:
    args._namespace_parser.print_help()
    return 0
