# LLM: 静态动作目录是v2唯一分发和能力来源，不接受插件注册方法或动态getattr。
# 模块用途: 定义42个动作的闭合参数、条件权限与资源核验要求。
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType

from .errors import HostAPIError
from .schemas import canonical_json, closed, integer, quota_schema, text, validate_object


# LLM: schema序列化冻结，公开返回副本；scope必须由领域权威复核，目录本身不证明资源归属。
# 类用途: 保存单个动作的静态合同。
@dataclass(frozen=True)
class ActionSpec:
    name: str
    mode: str
    capabilities: frozenset[str]
    schema_json: str
    scope_requirements: tuple[str, ...]
    any_capabilities: frozenset[str] = frozenset()

    # LLM: 变更返回副本不改变中央目录。
    # 函数用途: 取得动作的闭合JSON schema。
    @property
    def payload_schema(self) -> dict:
        return json.loads(self.schema_json)

    # LLM: 写操作与异步提交同样需要原幂等账，流不创建业务请求。
    # 函数用途: 表明调用是否必须提供幂等键。
    @property
    def requires_idempotency(self) -> bool:
        return self.mode in {"write", "async"}

    # LLM: 只验证参数，不创建用户或检索资源。
    # 函数用途: 返回应用明确默认值后的闭合参数。
    def validate_payload(self, payload: object) -> dict:
        return validate_object(payload, self.payload_schema)


# LLM: ID仍须由领域原validator复验，不把它直接拼成路径。
# 函数用途: 构造UTF8有界资源编号规则。
def id_schema() -> dict:
    return text(128, 1, byte_limit=128)


# LLM: 游标是opaque MAC字符串，不是底层offset。
# 函数用途: 构造有界游标规则。
def cursor_schema() -> dict:
    return text(2048, 1)


# LLM: 默认50条，不允许超限参数被静默裁剪。
# 函数用途: 返回通用列表分页字段。
def page_fields() -> dict:
    return {"limit": {**integer(100, 1), "default": 50}, "cursor": cursor_schema()}


# LLM: 所有动作明确权限和范围，未提供scope不从名称猜安全边界。
# 函数用途: 构造一个不可变动作声明。
def _spec(name: str, mode: str, caps: str, schema: dict, scopes: str = "", alternatives: str = "") -> ActionSpec:
    return ActionSpec(name, mode, frozenset(caps.split()), canonical_json(schema),
                      tuple(scopes.split()), frozenset(alternatives.split()))


# LLM: 会话与请求共享线程授权；poll/stream的实际kind权限由原请求元数据二次核对。
# 函数用途: 声明发现、线程、历史及请求动作。
def _conversation_specs() -> list[ActionSpec]:
    thread, request = {"thread_id": id_schema()}, {"request_id": id_schema()}
    revision = {"expected_revision": integer(2**63 - 1, 1)}
    reason = {"reason": {**text(256), "default": ""}}
    return [
        _spec("host.capabilities.get", "read", "", closed({})),
        _spec("conversations.threads.create", "write", "chat", closed({}, {"title": {**text(120), "default": ""}})),
        _spec("conversations.threads.get", "read", "read", closed(thread), "thread"),
        _spec("conversations.threads.list", "read", "read", closed({}, {
            **page_fields(), "status": {"enum": ["active", "archived", "all"], "default": "active"}}), "threads"),
        _spec("conversations.threads.rename", "write", "threads.manage",
              closed({**thread, **revision, "title": text(120, 1)}), "thread"),
        _spec("conversations.threads.archive", "write", "threads.manage", closed({**thread, **revision}), "thread"),
        _spec("conversations.threads.restore", "write", "threads.manage", closed({**thread, **revision}), "thread"),
        _spec("conversations.messages.list", "read", "chat.history", closed(thread, page_fields()), "thread"),
        _spec("conversations.messages.send", "async", "chat", closed({**thread, "text": text(16384, 1, byte_limit=16384)}, {
            "tool_policy": {"enum": ["none", "bounded"], "default": "none"},
            "input_artifact_refs": {"type": "array", "items": id_schema(), "maxItems": 8, "default": []}}), "thread artifacts"),
        _spec("conversations.requests.poll", "read", "", closed(request, {
            "event_cursor": cursor_schema(), "reply_cursor": cursor_schema(),
            "event_limit": {**integer(100, 1), "default": 50}}), "request request_kind", "chat execute"),
        _spec("conversations.requests.stream", "stream", "", closed(request, {"event_cursor": cursor_schema()}),
              "request request_kind", "chat execute"),
        _spec("conversations.requests.cancel", "async", "requests.cancel", closed({**request, **revision}, reason), "request"),
    ]


# LLM: 管理用户与代用户访问不混用，UID仅是管理目标。
# 函数用途: 声明本地用户生命周期动作。
def _user_specs() -> list[ActionSpec]:
    uid = {"user_id": text(64, 3, r"^[a-z][a-z0-9_-]{2,63}$")}
    revision = {"expected_revision": integer(2**63 - 1, 1)}
    return [
        _spec("users.local.create", "write", "users.manage", closed(uid, {
            "display_name": {**text(80), "default": ""}, "quota": quota_schema()}), "creatable_user manageable_user"),
        _spec("users.local.list", "read", "users.manage", closed({}, {
            **page_fields(), "status": {"enum": ["all", "enabled", "disabled"], "default": "all"}}), "manageable_users"),
        _spec("users.local.get", "read", "users.manage", closed(uid), "manageable_user"),
        _spec("users.local.disable", "write", "users.manage", closed({**uid, **revision}, {
            "reason": {**text(256), "default": ""}}), "manageable_user"),
        _spec("users.local.enable", "write", "users.manage", closed({**uid, **revision}), "manageable_user"),
        _spec("users.local.quota.get", "read", "users.manage", closed(uid), "manageable_user"),
    ]


# LLM: 工具参数仍需领域工具schema验证；静态层先阻断身份注入。
# 函数用途: 声明仅可代普通用户执行的四个动作。
def _execution_specs() -> list[ActionSpec]:
    thread = {"thread_id": id_schema()}
    revision = {"expected_revision": integer(2**63 - 1, 1)}
    inputs = {"inputs": {"type": "object", "default": {}}}
    asset_id = text(128, 1, r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    return [
        _spec("execute.tool", "async", "execute act_as_user", closed({**thread, "tool_name": text(96, 1, r"^[A-Za-z0-9_]{1,96}$"),
              "arguments": {"type": "object"}}), "thread tool"),
        _spec("execute.script", "async", "execute act_as_user",
              closed({**thread, **revision, "script_id": asset_id}, inputs), "thread asset"),
        _spec("execute.workflow", "async", "execute act_as_user",
              closed({**thread, **revision, "workflow_id": asset_id}, inputs), "thread asset"),
        _spec("execute.workflow_resume", "async", "execute act_as_user",
              closed({**thread, **revision, "workflow_run_id": id_schema(), "from_step_id": id_schema()}), "thread task asset"),
    ]


# LLM: 查询只给安全元数据；响应仅转交真实人票据，不接受decision/秘密字段。
# 函数用途: 声明待审批和输入的读写动作。
def _interaction_specs() -> list[ActionSpec]:
    listing = closed({"thread_id": id_schema()}, page_fields())
    response = closed({"interaction_id": id_schema(), "expected_revision": integer(2**63 - 1, 1),
                       "response_ticket": text(4096, 1)})
    return [
        _spec("interactions.pending_approvals.list", "read", "interactions.read", listing, "thread interactions"),
        _spec("interactions.pending_inputs.list", "read", "interactions.read", listing, "thread interactions"),
        _spec("interactions.approvals.respond", "write", "interactions.respond", response, "interaction human_ticket"),
        _spec("interactions.inputs.respond", "write", "interactions.respond", response, "interaction human_ticket"),
    ]


# LLM: Goal控制不含完成改写或预算重置，取消任务不隐式清目标。
# 函数用途: 声明任务与Goal的有限动作。
def _task_goal_specs() -> list[ActionSpec]:
    thread, task = {"thread_id": id_schema()}, {"task_id": id_schema()}
    goal = {**thread, "goal_id": id_schema()}
    revision = {"expected_revision": integer(2**63 - 1, 1)}
    rows = [
        _spec("tasks.list", "read", "tasks.read", closed(thread, page_fields()), "thread tasks"),
        _spec("tasks.get", "read", "tasks.read", closed(task), "task"),
        _spec("tasks.progress", "read", "tasks.read", closed(task, page_fields()), "task"),
        _spec("tasks.cancel", "async", "tasks.control", closed({**task, **revision}, {
            "include_children": {"type": "boolean", "default": False}, "reason": {**text(256), "default": ""}}), "task"),
        _spec("goals.list", "read", "goals.read", closed(thread, page_fields()), "thread goals"),
        _spec("goals.get", "read", "goals.read", closed(goal), "thread goal"),
        _spec("goals.create", "write", "goals.control chat", closed({**thread, "objective": text(4000, 1)}, {
            "name": {**text(120), "default": ""}, "duration_seconds": integer(604800, 1)}), "thread"),
        _spec("goals.update", "write", "goals.control", closed({**goal, **revision, "objective": text(4000, 1)}), "thread goal"),
    ]
    rows.extend(_spec("goals." + name, "write", "goals.control chat" if name == "resume" else "goals.control",
                      closed({**goal, **revision}), "thread goal") for name in ("pause", "resume", "clear"))
    return rows


# LLM: 制品只取注册ref且哈希固定；记忆不开放写口。
# 函数用途: 声明正式记忆和制品读取动作。
def _read_specs() -> list[ActionSpec]:
    artifact = {"artifact_ref": id_schema()}
    return [
        _spec("memory.search", "read", "memory.read", closed({"query": text(512, 1)}, page_fields()), "memory"),
        _spec("memory.get", "read", "memory.read", closed({"entry_id": id_schema()}, {"cursor": cursor_schema()}), "memory"),
        _spec("workspace.artifacts.list", "read", "artifacts.read", closed({"task_id": id_schema()}, page_fields()), "task"),
        _spec("workspace.artifacts.get", "read", "artifacts.read", closed(artifact), "artifact task"),
        _spec("workspace.artifacts.read", "read", "artifacts.read", closed({**artifact,
            "expected_sha256": text(64, 64, r"^[0-9a-f]{64}$")}, {
            "offset_bytes": {**integer(2**63 - 1), "default": 0},
            "max_bytes": {**integer(65536, 1), "default": 65536}}), "artifact task"),
    ]


# LLM: 缓存只含不可变声明，不含调用者、令牌或owner状态。
# 函数用途: 返回不能被运行方覆盖的中央目录。
@lru_cache(maxsize=1)
def action_specs():
    rows = _conversation_specs() + _user_specs() + _execution_specs() + _interaction_specs() + _task_goal_specs() + _read_specs()
    return MappingProxyType({row.name: row for row in rows})


# LLM: 不接受未登记动作，不按方法名动态导入。
# 函数用途: 查找一个动作的规范合同。
def action_spec(action: str) -> ActionSpec:
    if not isinstance(action, str) or action not in action_specs():
        raise HostAPIError("UNSUPPORTED_ACTION")
    return action_specs()[action]


# LLM: 返回AND集合；poll/stream的OR准入在any_capabilities，最终kind必须由领域核验。
# 函数用途: 计算动作的静态及参数条件权限。
def required_capabilities(action: str, payload: dict) -> frozenset[str]:
    required = set(action_spec(action).capabilities)
    if action == "conversations.messages.send":
        if payload.get("tool_policy") == "bounded":
            required.add("execute")
        if payload.get("input_artifact_refs"):
            required.add("artifacts.read")
    return frozenset(required)
