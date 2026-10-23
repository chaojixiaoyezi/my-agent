# LLM: 生产组合仅由Gateway构造；不初始化Agent、不复制main执行器，直接HostComposition仍为空。
# 模块用途: 为真实HTTP入口组装四域和普通用户工厂。
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from ..common.directory_lock import locked_private_directory
from ..conversation.store import ConversationStore
from ..gateway_parts.paths import GatewayPaths, gateway_paths_from_root
from ..gateway_parts.plugin_request_source import PluginRequestPorts
from ..plugin_install_store import PluginInstallStore
from ..user_space.local_user_store import LocalUserStore
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .composition import HostComposition
from .contracts import HostDomainPorts
from .conversations import ConversationDomain
from .errors import HostAPIError
from .execution import ExecutionDomain, ExecutionQueuePorts
from .local_users import LocalUsersDomain
from .owner_context import IngressOwnerResolver


# LLM: paths是可信构造参数，不读取HTTP正文；无完整路径的传输单测仍保留空组合。
# 函数用途: 取得同一宿主的明确根目录，不能把启动cwd当owner。
def gateway_host_root(server, paths):
    agent = server.agent if server.agent is not None else server.control_plane
    config = getattr(agent, "config", None)
    configured = getattr(config, "my_agent_home", None)
    if configured:
        return Path(configured).absolute()
    if isinstance(paths, GatewayPaths):
        return paths.root.parent.absolute()
    if getattr(paths, "state", None) is not None:
        state = Path(paths.state).absolute()
        return state.parent.parent if state.parent.name == "gateway" else state.parent
    return None


# LLM: 冷构造只保留路径；每次调用均由A/C核准effective owner后打开规范ConversationStore。
# 类用途: 集中生产owner存储、路径和原激活短提交门。
class GatewayHostResources:
    # LLM: 构造零IO，用户库与安装库均为原权威而非镜像。
    # 函数用途: 保存同一Gateway及其原root。
    def __init__(self, server, root):
        self.server, self.root = server, root
        self.users = LocalUserStore(root)
        self.resolver = IngressOwnerResolver(self.users)
        self.resolver.factory.commit_scope = self.commit_scope

    # LLM: 只为当前已核上下文解析规范会话目录，不创建Agent或初始化目录。
    # 函数用途: 冷打开当前owner的原会话账。
    def store_for(self, context):
        return ConversationStore(self.runtime_paths(context)["conversation_workspace"], initialize=False)

    # LLM: foreign owner清除main所有路径覆盖；只复制路径选项，不复制模型/凭据或管理员权限。
    # 函数用途: 沿原唯一运行路径解析器计算当前owner规范地址。
    def runtime_paths(self, context):
        from ..user_space.runtime_paths import resolve_runtime_paths_for_agent
        home = context.owner_home
        config = SimpleNamespace()
        base = self.server.agent if self.server.agent is not None else self.server.control_plane
        if home.identity == OwnerIdentity.local_main():
            source = getattr(base, "config", None)
            config = SimpleNamespace(**{name: getattr(source, name, "") for name in
                ("conversation_workspace", "gateway_workspace", "local_store_path")})
        scoped = SimpleNamespace(owner_home_dir=home.home_dir, owner_workspace_dir=home.workspace_dir)
        paths = resolve_runtime_paths_for_agent(config, home.home_dir, scoped).paths
        for path in paths.values():
            if ".." in path.parts or not path.is_relative_to(home.home_dir):
                raise HostAPIError("TARGET_DENIED", 403)
            if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(home.home_dir)):
                raise HostAPIError("TARGET_DENIED", 403)
        return paths

    # LLM: 队列属于各owner，不让alice请求落到main通用执行器；同owner所有poll/cancel走同一路径。
    # 函数用途: 返回当前owner的固定Gateway队列。
    def paths_for(self, context):
        return gateway_paths_from_root(self.runtime_paths(context)["gateway_workspace"])

    # LLM: 与PluginInstallStore撤销共用原plugins锁；锁内只做有界提交，不运行模型/进程/审批等待。
    # 函数用途: 在同一激活短提交门里再次核固定来源。
    @contextmanager
    def commit_scope(self, binding):
        owner = resolve_owner_home(self.root, binding.owner)
        with locked_private_directory(owner.root, relative_parts=owner.plugins_dir.relative_to(owner.root).parts,
                                      lock_name=".plugins.lock"):
            entries = PluginInstallStore(owner).snapshot()
            if not any(entry.installation_ref == binding.installation_ref and entry.package_sha256 == binding.package_sha256
                       and entry.activation is not None and entry.activation.phase == "active"
                       and entry.activation.activation_id == binding.activation_id for entry in entries):
                raise HostAPIError("ACTIVATION_REVOKED", 410)
            yield

    # LLM: 只冷读原channel_bindings；默认线程不能归档，缺账不建默认会话。
    # 函数用途: 判断线程是否仍被原通道绑定。
    def default_thread(self, context, thread_id):
        from .memory import _owner_bytes
        from .schemas import strict_json
        raw = _owner_bytes(context, self.store_for(context).storage.bindings_path)
        if not raw:
            return False
        value = strict_json(raw)
        return isinstance(value, dict) and thread_id in value.values()

    # LLM: 用量读取有界原claim/queue，不按目录数量猜active_agents；未测完整磁盘保持null/complete=false。
    # 函数用途: 观察目标owner当前真实请求与claim记录。
    def usage(self, uid):
        from .gateway_runtime import runtime_usage
        return runtime_usage(self, uid)

    # LLM: disable提交后锁外请求取消，资源是否真的停止仍由原canonical/运行时观察，不回报停止成功。
    # 函数用途: 标记该普通用户原队列的准确请求为取消受理。
    def stop_resources(self, uid, revision):
        from .gateway_runtime import stop_owner_requests
        return stop_owner_requests(self, uid, revision)


# LLM: builder只能在可信启动链调用，HTTP参数不能替换端口；返回前不创建目录、线程、模型或锁。
# 函数用途: 将现有四个领域接入真实Gateway，保留无paths旧传输单测。
def build_gateway_host_composition(server, paths):
    root = gateway_host_root(server, paths)
    if root is None:
        return HostComposition()
    resources = GatewayHostResources(server, root)
    composition = HostComposition(owner_resolver=resources.resolver)
    from .gateway_observations import GatewayObservations
    from .gateway_runtime import GatewayPluginRequests
    requests = GatewayPluginRequests(resources)
    conversation = ConversationDomain(resources.store_for, composition.cursors, default_thread=resources.default_thread,
        request_ports=PluginRequestPorts(resources.paths_for, requests.prepare, requests.cancel))
    requests.conversation = conversation
    execution = ExecutionDomain(resources.resolver.factory, ExecutionQueuePorts(
        requests.enqueue, requests.require_thread, requests.read_asset))
    composition.ports = HostDomainPorts(conversation, LocalUsersDomain(resources.resolver.factory,
        resources.usage, resources.stop_resources), execution, GatewayObservations(resources, conversation, composition.cursors))
    return composition
