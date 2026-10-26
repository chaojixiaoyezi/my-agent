# LLM: 十项动作是闭合静态合同，能力声明不能替代当前enabled/成员/owner/代次复核。
# 模块用途: 定义审计组和左右双Bot的Host v2参数及权限目录。
from .action_specs import _spec, id_schema, page_fields
from .schemas import closed, integer, text

AUDIT_BOT_ACTIONS = frozenset({
    "users.local.audit_group.set", "audit_group.owners.list", "audit_group.assets.summary",
    "audit_group.alerts.list", "audit_group.posture.overview", "audit_group.assets.invoke",
    "conversations.bots.ensure", "conversations.bots.submit", "conversations.bots.rollover",
    "conversations.bots.history.list",
})


# LLM: 只接受两个明确参数名及ops_config别名；一致性由领域归一化验证，不默认覆盖另一参数。
# 函数用途: 声明双Bot模式选择字段。
def bot_mode_fields():
    mode = {"enum": ["query_readonly", "action_avatar", "ops_config"]}
    return {"bot_mode": mode, "bot_kind": mode}


# LLM: page_context仅是有界页面数据，闭合白名单不允许注入Bot身份、代次、禁学或执行权限。
# 函数用途: 声明页面当前关注的owner、告警和资产上下文。
def page_context_schema():
    return closed({}, {"owner_ids": {"type": "array", "items": text(128, 1), "maxItems": 100},
                       "alert_id": id_schema(), "alert_summary": text(512), "asset_id": id_schema(),
                       "page": text(256)})


# LLM: 目录共用原chat/history/manage/execute/users能力，审计只新增read/invoke两项，不授予跨owner文件读取。
# 函数用途: 构造六个审计组标准动作。
def audit_specs():
    owners = {"owner_ids": {"type": "array", "items": text(128, 1), "maxItems": 100}}
    filters = {**owners, **page_fields(), "since": text(40, 1), "until": text(40, 1)}
    return [
        _spec("users.local.audit_group.set", "write", "users.manage", closed({
            "user_id": text(64, 3), "in_audit_group": {"type": "boolean"},
            "expected_revision": integer(2**63 - 1, 1)}), "manageable_user"),
        _spec("audit_group.owners.list", "read", "audit_group.read", closed({}, page_fields()), "audit_group"),
        _spec("audit_group.assets.summary", "read", "audit_group.read", closed({}, {
            **owners, **page_fields(), "kind": {"enum": ["monitor", "workflow", "script", "skill"]}}), "audit_group"),
        _spec("audit_group.alerts.list", "read", "audit_group.read", closed({}, {
            **filters, "severity": text(32, 1)}), "audit_group"),
        _spec("audit_group.posture.overview", "read", "audit_group.read", closed({}, owners), "audit_group"),
        _spec("audit_group.assets.invoke", "async", "audit_group.invoke execute", closed({
            "source_owner_id": text(128, 1), "kind": {"enum": ["monitor", "workflow", "script", "skill"]},
            "asset_id": id_schema()}, {"input_params": {"type": "object", "default": {}}}), "audit_group asset"),
    ]


# LLM: ensure/submit/rollover复用原幂等协议，history仅当前登录owner；线程ID可选但不得替代槽位定位。
# 函数用途: 构造四个双Bot标准动作并合入六个审计动作。
def audit_bot_specs():
    mode = bot_mode_fields()
    generation = integer(2**63 - 1, 1)
    return audit_specs() + [
        _spec("conversations.bots.ensure", "write", "chat", closed({}, mode), "owner bot_slot"),
        _spec("conversations.bots.submit", "async", "chat", closed({"text": text(16384, 1, byte_limit=16384)}, {
            **mode, "thread_id": id_schema(), "expected_generation": generation,
            "auto_rollover": {"type": "boolean", "default": False},
            "page_context": {**page_context_schema(), "default": {}},
            "tool_policy": {"enum": ["none", "bounded"], "default": "bounded"}}), "owner bot_slot"),
        _spec("conversations.bots.rollover", "write", "threads.manage", closed({
            "expected_generation": generation, "expected_revision": generation}, mode), "owner bot_slot"),
        _spec("conversations.bots.history.list", "read", "chat.history", closed({}, {
            **mode, **page_fields(), "mode": {"enum": ["query_readonly", "action_avatar", "ops_config"]},
            "since": integer(2**63 - 1), "until": integer(2**63 - 1),
            "alert_id": id_schema(), "query": text(256), "thread_id": id_schema()}), "owner bot_history"),
    ]
