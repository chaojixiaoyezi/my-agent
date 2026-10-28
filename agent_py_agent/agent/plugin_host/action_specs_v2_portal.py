# LLM: W7门户目录只声明静态合同；真实owner、线程范围、CAS及后端就绪仍由授权和领域核验。
# 模块用途: 统一门户通用宿主动作与新会话/通知控制，不增加第二会话、调度或分发账。
from __future__ import annotations

from .action_specs import _spec, id_schema
from .schemas import closed, integer, text


# LLM: 列表只收有界JSON，不授包、路径或媒体读取权，领域必须核实际归属。
# 函数用途: 构造小型引用数组的共同合同。
def references(items=None):
    return {"type": "array", "items": items or id_schema(), "maxItems": 32}


# LLM: 设置只接受显式可写字段，不接受owner/权限来源对象；修订及包名归属由W4原Store再核。
# 函数用途: 声明线程设置及新会话生命周期，写前仍核owner、范围及修订。
def _thread_specs():
    thread = {"thread_id": id_schema()}
    revision = {"expected_revision": integer(2**63 - 1, 0)}
    setting = {"model_profile_id": text(256, 1), "reasoning_effort": {"enum": [
        "auto", "off", "low", "medium", "high", "xhigh", "max", "ultra"]},
        "permission_mode": {"enum": ["ask", "auto", "full-access"]},
        "preferred_pack_ids": references(), "pack_ids": references()}
    scope = "threads.scope"
    return [
        _spec("conversations.threads.settings.get", "read", "read", closed(thread), scope),
        _spec("conversations.threads.settings.update", "write", "threads.manage", closed(thread, {**revision, **setting}), scope),
        _spec("conversations.threads.packs.get", "read", "read", closed(thread), scope),
        _spec("conversations.threads.packs.set", "write", "threads.manage", closed(thread, {**revision,
            "pack_ids": references(), "preferred_pack_ids": references()}), scope),
        _spec("conversations.threads.model.set", "write", "threads.manage", closed({**thread,
            "model_profile_id": setting["model_profile_id"]}, {**revision, "reasoning_effort": setting["reasoning_effort"]}), scope),
        _spec("conversations.threads.permission_mode.set", "write", "threads.manage",
            closed({"permission_mode": setting["permission_mode"]}, {**thread, **revision})),
        _spec("conversations.threads.usage.get", "read", "read", closed(thread), scope),
        _spec("conversations.threads.compact", "execute", "chat threads.manage",
            closed(thread, {**revision, "instruction": text(4000)}), scope),
        _spec("conversations.threads.delete", "write", "threads.manage", closed(thread, revision), scope),
        _spec("conversations.threads.new", "write", "threads.manage", closed(thread, {**revision, "title": text(200)}), scope),
    ]


# LLM: 插话/控制只收结构化目标，缺真实运行句柄不可伪造停止；可选幂等字段不替代外层原幂等协议。
# 函数用途: 声明插话、子代理和准确后台停止合同。
def _control_specs():
    thread = {"thread_id": id_schema()}
    target = {"subagent_id": id_schema()}
    revision = {"expected_revision": integer(2**63 - 1, 0)}
    return [
        _spec("conversations.requests.interject", "execute", "chat", closed(thread, {
            "request_id": id_schema(), "text": text(16384, 1), "message": text(16384, 1),
            "mode": {"enum": ["steer", "queue", "interrupt"], "default": "steer"},
            "input_artifact_refs": references({"type": "object"})}), "threads.scope"),
        _spec("subagents.list", "read", "tasks.read", closed(thread, {
            "root_task_id": id_schema(), "include_finished": {"type": "boolean", "default": True}}), "threads.scope"),
        _spec("subagents.get", "read", "tasks.read", closed(target, thread)),
        _spec("subagents.steer", "execute", "tasks.control", closed(target, {
            **thread, **revision, "message": text(16384, 1), "text": text(16384, 1)})),
        _spec("subagents.stop", "execute", "tasks.control", closed(target, {**thread, **revision, "reason": text(256)})),
        _spec("processes.background.stop", "execute", "tasks.control", closed({"session_id": id_schema()}, {
            "force": {"type": "boolean", "default": False}})),
    ]


# LLM: 秘密仅写入宿主模型专用端口，响应仍须由W4脱敏；不从插件正文选择owner。
# 函数用途: 声明服务商、内置插件及身份和能力包桥接读口。
def _settings_specs():
    revision = {"expected_revision": integer(2**63 - 1, 0)}
    return [
        _spec("models.providers.list", "read", "read", closed({}, {
            "capability_filter": {"enum": ["agentic", "all"], "default": "agentic"}})),
        _spec("models.providers.save", "write", "write", closed({"provider_id": id_schema(),
            "api_base": text(4096, 1), "models": references({"type": "object"})}, {
            **revision, "display_name": text(256), "api_key": text(8192), "session_header": text(128), "custom_headers": {"type": "object"}})),
        _spec("models.providers.delete", "write", "write", closed({"provider_id": id_schema()}, {
            **revision, "profile_id": id_schema()})),
        _spec("models.profiles.delete", "write", "write", closed({"profile_id": id_schema()}, revision)),
        _spec("plugins.builtin.list", "read", "read", closed({}, {"thread_id": id_schema()})),
        _spec("plugins.builtin.toggle", "write", "write", closed({"plugin_id": id_schema(), "enabled": {"type": "boolean"}}, {
            "thread_id": id_schema(), **revision})),
        _spec("user_space.identity.resolve", "read", "read", closed({}, {
            "id": text(256), "id_type": {"enum": ["auto", "user", "chat"], "default": "auto"},
            "targets": {"type": "array", "maxItems": 64, "items": closed(
                {"id": text(256)}, {"id_type": {"enum": ["auto", "user", "chat"], "default": "auto"}})}})),
        _spec("capability_packages.list", "read", "read", closed({}, {
            "thread_id": id_schema(), "query": text(512), "limit": integer(200, 1)})),
    ]


# LLM: 路径只是待核输入，W5仍必须限制真实workspace/nofollow；载荷受宿主通用HTTP字节门约束。
# 函数用途: 声明通用工作区文件和Git动作，不开放任意shell。
def _workspace_specs():
    workspace = {"workspace_dir": text(4096, 1)}
    file = {**workspace, "path": text(4096, 1)}
    return [
        _spec("workspace.files.list", "read", "read", closed(workspace, {
            "relative_path": text(4096), "query": text(512), "limit": integer(200, 1)})),
        _spec("workspace.files.read", "read", "read", closed(file, {
            "start_line": integer(2**31 - 1, 1), "end_line": integer(2**31 - 1, 1), "max_bytes": integer(262144, 1)})),
        _spec("workspace.files.upload", "write", "write", closed(file, {
            "content": text(65536), "data_base64": text(65536), "expected_revision": integer(2**63 - 1, 0)})),
        _spec("workspace.files.download", "read", "read", closed(file, {"max_bytes": integer(20 * 1024 * 1024, 1)})),
        *[_spec("workspace.git." + name, "read", "read", closed(workspace, {"path": text(4096)}))
          for name in ("diff", "inspect")],
    ]


# LLM: 告警策略复用原schema及monitor授权范围，不能由参数选择owner或任意地址。
# 函数用途: 声明已认证目标目录和原监控通知方式的读写入口。
def _delivery_specs():
    from ..scheduler.delivery_policy import DELIVERY_SCHEMA
    monitor = {"monitor_id": id_schema()}
    return [
        _spec("monitors.delivery.targets.list", "read", "read assets.read", closed({})),
        _spec("monitors.delivery.get", "read", "read assets.read", closed(monitor)),
        _spec("monitors.delivery.update", "write", "write assets.write", closed({**monitor,
            "expected_version": integer(2**63 - 1, 1), "delivery_policy": DELIVERY_SCHEMA})),
    ]


# LLM: 单一动作目录合并时拒绝重名；返回新spec列表，不缓存动态授权或运行就绪事实。
# 函数用途: 将门户和通知动作交给既有Host目录统一发行，不绕过通用授权。
def portal_v2_action_specs():
    return _thread_specs() + _control_specs() + _settings_specs() + _workspace_specs() + _delivery_specs()


PORTAL_V2_ACTIONS = frozenset(spec.name for spec in portal_v2_action_specs())
