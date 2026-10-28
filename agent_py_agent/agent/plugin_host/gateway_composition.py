# LLM: 生产组合沿原四端口接通静态扩展和通用底座；不初始化Agent，SSO协议验证只取可信宿主端口。
# 模块用途: 为HTTP入口组装原四域、普通用户工厂和包/调试/认证底座。
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


# LLM: builder只在可信启动链绑定底座/门户和原四端口，不从HTTP指定实现，构造不物化Owner或执行模型。
# 函数用途: 将真实Gateway依赖、观察端口及通用底座组装到同一宿主。
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
    conversation.portal_v2 = GatewayPortalDomain(resources, conversation)
    requests.conversation = conversation
    execution = ExecutionDomain(resources.resolver.factory, ExecutionQueuePorts(
        requests.enqueue, requests.require_thread, requests.read_asset))
    observations = GatewayObservations(resources, conversation, composition.cursors)
    from .substrate import GatewaySubstrateDomain
    conversation.substrate = GatewaySubstrateDomain(resources, conversation, observations)
    composition.ports = HostDomainPorts(conversation, LocalUsersDomain(resources.resolver.factory,
        resources.usage, resources.stop_resources), execution, observations)
    return composition


# LLM: 仅固定W4/W5模块；探测类方法不实例化，不吞内部导入错误；函数桩优先，能力包读口须真实可用。
# 函数用途: 惰性定位门户动作的兄弟领域入口。
def portal_target(action):
    from importlib import import_module

    thread = {"settings.get": "get_thread_settings", "packs.get": "get_thread_settings",
        "settings.update": "update_thread_settings", "packs.set": "update_thread_settings",
        "model.set": "update_thread_settings", "permission_mode.set": "update_thread_settings",
        "usage.get": "get_thread_usage", "compact": "compact_thread", "delete": "delete_thread"}
    routes = {"models.providers.list": ("model_settings_v2_handlers", "list_providers"),
        "models.providers.save": ("model_settings_v2_handlers", "save_provider"),
        "models.providers.delete": ("model_settings_v2_handlers", "delete_provider_or_profile"),
        "models.profiles.delete": ("model_settings_v2_handlers", "delete_provider_or_profile"),
        "plugins.builtin.list": ("builtin_plugins_v2", "list_builtin_plugins"),
        "plugins.builtin.toggle": ("builtin_plugins_v2", "toggle_builtin_plugin"),
        "subagents.list": ("subagents_v2_handlers", "list_subagents"),
        "subagents.get": ("subagents_v2_handlers", "get_subagent"),
        "subagents.steer": ("subagents_v2_handlers", "steer_subagent"),
        "subagents.stop": ("subagents_v2_handlers", "stop_subagent"),
        "processes.background.stop": ("subagents_v2_handlers", "stop_background_process"),
        "capability_packages.list": ("capabilities_v2_handlers", "capability_packages_list")}
    routes.update({"conversations.threads." + key: ("thread_settings_v2", value) for key, value in thread.items()})
    routes.update({"workspace." + key: ("workspace_v2_handlers", value) for key, value in {
        "files.list": "list_workspace_files", "files.read": "read_workspace_file", "files.upload": "upload_workspace_file",
        "files.download": "download_workspace_file", "git.diff": "inspect_workspace_git", "git.inspect": "inspect_workspace_git"}.items()})
    if action not in routes:
        return None
    module_name, method = routes[action]
    qualified = __package__ + "." + module_name
    try:
        module = import_module(qualified)
    except ModuleNotFoundError as error:
        if error.name == qualified:
            return None
        raise
    target = getattr(module, method, None)
    if callable(target):
        return target
    aliases = {"models.providers.delete": "delete_provider", "models.profiles.delete": "delete_profile"}
    target = getattr(module, aliases.get(action, ""), None)
    if callable(target):
        return target
    class_name = {"thread_settings_v2": "ThreadSettingsV2Handlers", "model_settings_v2_handlers": "ModelSettingsV2Handlers"}.get(module_name)
    handler = getattr(module, class_name, None) if class_name else None
    if callable(handler) and callable(getattr(handler, method, None)):
        return handler, method
    if action == "capability_packages.list":
        from ..capability import package_provider
        if callable(getattr(package_provider, "list_installed_capability_packages", None)):
            return _portal_packages
    return None


# LLM: 使用W1原安装读口及W5公共投影，缺导出不假报空列表，不公开reader或安装私有路径。
# 函数用途: 在无同名handler时桥接当前Owner能力包列表。
def _portal_packages(context, payload):
    from ..capability.package_provider import list_installed_capability_packages
    from . import capabilities_v2_handlers

    project = getattr(capabilities_v2_handlers, "_package_dict", None)
    records = list_installed_capability_packages(context.owner_home.home_dir)
    return {"items": [dict(record) if isinstance(record, dict) else (project(record) if callable(project) else record.to_dict()) for record in records]}


# LLM: 在副作用前检查签名；只过滤声明接受的keyword，绝不因handler内部TypeError而再调用一次。
# 函数用途: 给已定位的兄弟函数构造可用关键字参数。
def _portal_keywords(signature, values):
    from inspect import Parameter

    extra = any(p.kind == Parameter.VAR_KEYWORD for p in signature.parameters.values())
    return {name: value for name, value in values.items() if extra or name in signature.parameters
            and signature.parameters[name].kind != Parameter.POSITIONAL_ONLY}


# LLM: bind纯校验、不执行业务；拒绝不兼容签名，不能在未知副作用后猜测另一种调用方式。
# 函数用途: 判定(context,payload)协议是否可调用。
def _portal_binds(signature, args_tuple=(), kwargs_dict=None):
    try:
        signature.bind(*args_tuple, **(kwargs_dict or {}))
        return True
    except TypeError:
        return False


# LLM: 门户只复用当前owner资源和现有线程范围；没有对应函数明确HOST_NOT_READY，不动态构造Agent。
# 类用途: 为四域组合根适配W4/W5兄弟处理器。
class GatewayPortalDomain:
    # LLM: 构造只保留可信资源，不导入兄弟或读写状态；门户写入沿原控制事务而不复制秘密原文。
    # 函数用途: 绑定同一Gateway、会话域及原幂等事务所需资源。
    def __init__(self, resources, conversation):
        from .gateway_observations import GatewayObservationServices

        self.resources, self.conversation = resources, conversation
        self.services = GatewayObservationServices(resources, conversation, conversation.cursor_codec)

    # LLM: 只核静态函数/类方法存在，不实例化或缓存Owner权限；真实调用仍先后重验原授权。
    # 函数用途: 报告身份读口或已合入兄弟函数是否就绪。
    def ready(self, action):
        return action == "user_space.identity.resolve" or portal_target(action) is not None

    # LLM: 参数不能选择Owner；有thread_id必须先核B1范围，兄弟只接当前owner；返回仍由组合根末核。
    # 函数用途: 把门户动作接到准确Owner与兄弟领域的公开函数。
    def call(self, authorized):
        from dataclasses import asdict

        from .action_specs import action_spec
        from .contracts import DomainResult
        from .conversations import read_context
        from .schemas import thaw

        context = read_context(authorized)
        action, payload = authorized.call.action, thaw(authorized.call.payload)
        store = self.resources.store_for(context)
        if "thread_id" in payload:
            self.conversation.require_thread(store, payload["thread_id"], authorized, context)
        if action == "user_space.identity.resolve":
            from ..adapter.feishu_profile import resolve_user_space_identity

            creds = getattr(self.resources, "identity_credentials", None) or getattr(
                getattr(self.resources, "server", None), "identity_credentials", None)
            data = resolve_user_space_identity(context.owner_home.home_dir, targets=payload.get("targets"),
                id_value=payload.get("id", ""), id_type=payload.get("id_type", "auto"), feishu_credentials=creds)
            return DomainResult(200, {"identity": asdict(context.identity), "owner_id": context.owner_home.owner_id, **data})
        target = portal_target(action)
        if target is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        if action_spec(action).requires_idempotency or authorized.call.idempotency_key is not None:
            return portal_write_once(self, authorized, lambda: self._call_target(authorized, target, context, payload))
        return self._call_target(authorized, target, context, payload)

    # LLM: 精确适配W4/W5；handler返回后的撤权按真实副作用标committed，不能让原回执误删成not_started。
    # 函数用途: 适配兄弟函数的明确参数协议并返回统一结果。
    def _call_target(self, authorized, target, context, payload):
        from .composition import _observed_outcome
        from .contracts import DomainResult
        from .conversations import read_context

        read_context(authorized)
        if isinstance(target, tuple):
            result = self._call_class(authorized, target, context, payload)
        else:
            result = self._call_function(authorized, target, context, payload)
        try:
            read_context(authorized)
        except HostAPIError as error:
            raise HostAPIError(error.code, error.status, _observed_outcome(authorized.call.action)) from None
        return result if isinstance(result, DomainResult) else DomainResult(200, result)

    # LLM: 只交付当前Owner原有Agent，不创建或借main/control-plane执行器；显式owner_id冲突亦拒绝。
    # 函数用途: 为compact和子代理处理器提供准确Owner的可选运行时。
    def _owner_agent(self, context):
        agent = getattr(self.resources.server, "agent", None)
        paths = getattr(agent, "home_paths", None)
        if (str(getattr(paths, "owner_home_dir", "")) == str(context.owner_home.home_dir)
                and getattr(paths, "owner_id", context.owner_home.owner_id) == context.owner_home.owner_id):
            return agent
        return None

    # LLM: 类构造只在当前鲜活授权内发生；HomePaths按Owner重建，实例不跨请求缓存。
    # 函数用途: 调用W4线程和模型类式后端的单params方法。
    def _call_class(self, authorized, target, context, payload):
        from ..user_space.home_layout import home_paths as root_paths
        from ..user_space.owner_resolver import home_paths_with_owner

        handler_class, method = target
        if authorized.call.action.startswith("conversations.threads."):
            handler = handler_class(self.resources.store_for(context), context,
                                    agent=self._owner_agent(context), authorized=authorized)
        else:
            handler = handler_class(home_paths_with_owner(root_paths(context.owner_home.root), context.owner_home))
        return getattr(handler, method)(dict(payload))

    # LLM: W4三位置参数和W5两位置参数先bind；文本上传归一化为data_base64；内置列表总候选store再过滤。
    # 函数用途: 在调用之前选择唯一签名，避免TypeError重试产生重复副作用。
    def _call_function(self, authorized, target, context, payload):
        import base64
        from inspect import signature

        if authorized.call.action == "workspace.files.upload" and "content" in payload:
            if "data_base64" in payload:
                raise HostAPIError("INVALID_SCHEMA", 400)
            payload = {**payload, "data_base64": base64.b64encode(payload["content"].encode("utf-8")).decode("ascii")}
        params = signature(target)
        store = self.resources.store_for(context)
        if (authorized.call.action.startswith("conversations.threads.")
                and _portal_binds(params, (store, context, dict(payload)))):
            return target(store, context, dict(payload))
        options = _portal_keywords(params, {"agent": self._owner_agent(context), "store": store})
        if _portal_binds(params, (context, dict(payload)), options):
            return target(context, dict(payload), **options)
        if authorized.call.action.startswith("plugins.builtin."):
            values = dict(payload)
            if authorized.call.action.endswith(".list"):
                values["store"] = store
            values = _portal_keywords(params, values)
            if not _portal_binds(params, (context.owner_home.home_dir,), values):
                raise HostAPIError("HOST_NOT_READY", 503)
            return target(context.owner_home.home_dir, **values)
        if not _portal_binds(params, (context.owner_home,), payload):
            raise HostAPIError("HOST_NOT_READY", 503)
        return target(context.owner_home, **payload)


# LLM: 直接复用已有安全_once：完整payload摘要、不存秘密、末核范围、脱敏结果及原控制回执都不另写一套。
# 函数用途: 将门户写入接入原摘要幂等事务，回执重放前仍检查当前线程资格。
def portal_write_once(portal, authorized, effect):
    from .capabilities_v2_handlers import _once
    from .conversations import read_context

    context = read_context(authorized)

    # LLM: 回执不是永久资源通行证，锁内新写和重放都重核原B1当前可见性。
    # 函数用途: 在已有安全事务内再次确认当前目标线程范围。
    def preflight(saved):
        if "thread_id" in authorized.call.payload:
            portal.conversation.require_thread(portal.resources.store_for(context),
                authorized.call.payload["thread_id"], authorized, context)

    return _once(authorized, context, portal.services, effect, preflight)
