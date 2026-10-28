# LLM: WP-4扩展目录只声明静态合同；由action_specs在原类型定义完成后加载，不接受插件动态注册。
# 模块用途: 集中声明目录扩展及21个投递/治理动作，保留闭合参数、显式能力及写入幂等门。
from __future__ import annotations

from .action_specs import _spec, id_schema
from .schemas import closed, integer, text


# LLM: 工作目录只是待核输入，不因通过schema获得文件访问权；实际owner和目录安全由领域复核。
# 函数用途: 提供各领域共享的可选目录字段。
def workspace_fields():
    return {"workspace_dir": text(4096, 1)}


# LLM: 原服务负责内容与CAS，目录层只控制scope取值和修订整数，不自动把省略scope改成owner。
# 函数用途: 提供资产保存与提炼提交的可选目标字段。
def asset_target_fields():
    return {**workspace_fields(), "scope": {"enum": ["owner", "workspace"]},
            "expected_revision": integer(2**63 - 1, 1)}


# LLM: 每条引用必须有完整线程和消息，实际可见性在检索/打包之前逐条核对。
# 函数用途: 声明有界且不能夹带身份字段的回合选择数组。
def turn_refs_schema():
    return {"type": "array", "items": closed({"thread_id": id_schema(), "message_id": id_schema()}),
            "minItems": 1, "maxItems": 100, "uniqueItems": True}


# LLM: 原扩展与投递/治理独立分组但只返回一个目录；提炼权限和写入幂等合同不变。
# 函数用途: 返回目录、分叉、数据源、提炼、录制及后台观察的全部新增动作。
def v2_action_specs():
    workspace = workspace_fields()
    target = asset_target_fields()
    thread = {"thread_id": id_schema()}
    recording = {"recording_id": id_schema()}
    proposal = {"proposal_id": id_schema()}
    object_list = {"type": "array", "items": {"type": "object"}, "maxItems": 1000}
    return [
        _spec("conversations.threads.workspace.get", "read", "read", closed({}, thread), "thread_if_present workspace"),
        _spec("conversations.threads.workspace.switch", "write", "threads.manage",
              closed({**thread, **workspace}, {"expected_revision": target["expected_revision"]}), "thread workspace"),
        _spec("assets.workspace.list", "read", "read", closed({}, {**workspace, **thread,
              "kind": {"enum": ["all", "skill", "script", "workflow", "monitor"], "default": "all"}}),
              "thread_if_present workspace"),
        _spec("assets.workspace.promote", "write", "assets.write", closed({**workspace,
              "asset_kind": {"enum": ["skill", "script", "workflow", "monitor"]}, "asset_id": id_schema()},
              {"expected_revision": target["expected_revision"]}), "workspace asset"),
        _spec("conversations.threads.fork", "write", "threads.manage",
              closed({"source_thread_id": id_schema()}, {"up_to_message_id": id_schema(),
                     "source_message_id": id_schema(), "title": text(120), **workspace}), "source_thread workspace"),
        _spec("datasources.list", "read", "datasources.read", closed({}, workspace), "workspace datasource"),
        _spec("datasources.save", "write", "datasources.write", closed({"spec": {"type": "object"}},
              {"secret": {"type": "object"}, **target, "expected_revision": integer(2**63 - 1, 0)}), "workspace datasource"),
        _spec("datasources.test", "read", "datasources.read",
              closed({"datasource_id": id_schema()}, workspace), "workspace datasource"),
        _spec("datasources.schema.inspect", "read", "datasources.read",
              closed({"datasource_id": id_schema()}, workspace), "workspace datasource"),
        _spec("datasources.migrate", "write", "datasources.write",
              closed({"source_id": id_schema(), "target_spec": {"type": "object"}},
                     {**workspace, "mode": {**text(64, 1), "default": "prepare_and_validate"}}), "workspace datasource"),
        _spec("conversations.turns.search", "read", "chat.history", closed({}, {
              "query": {**text(512), "default": ""}, **workspace,
              "thread_ids": {"type": "array", "items": id_schema(), "minItems": 1, "maxItems": 100, "uniqueItems": True},
              "date_from": text(64, 10), "date_to": text(64, 10), "has_tool_calls": {"type": "boolean"},
              "limit": {**integer(100, 1), "default": 50}, "cursor": {**integer(2**31 - 1), "default": 0}}), "threads workspace"),
        _spec("assets.extract.from_turns.propose", "write", "chat.history assets.write",
              closed({"turn_refs": turn_refs_schema()}, {**workspace, "scope": target["scope"],
                     "target_types": {"type": "array", "items": {"enum": ["all", "skill", "script", "workflow", "monitor"]},
                                      "minItems": 1, "maxItems": 5, "uniqueItems": True},
                     "title_hint": {**text(120), "default": ""}}), "threads workspace"),
        _spec("assets.extract.from_turns.get", "read", "read chat.history", closed(proposal), "proposal"),
        _spec("assets.extract.from_turns.commit", "write", "assets.write",
              closed(proposal, {**target, "selected_assets": {**object_list, "maxItems": 100}}), "proposal workspace"),
        _spec("recorder.sessions.start", "write", "recorder.control",
              closed({"start_url": text(4096, 1)}, {**workspace, **thread, "title": {**text(120), "default": ""},
                     "headless": {"type": "boolean", "default": True}, "initial_actions": object_list}),
              "thread_if_present workspace recording"),
        _spec("recorder.sessions.stop", "write", "recorder.control",
              closed(recording, {"final_actions": object_list}), "recording"),
        _spec("recorder.sessions.get", "read", "recorder.control", closed({}, recording), "recording"),
        _spec("recorder.sessions.compile", "write", "recorder.control assets.write", closed(recording, {
              **workspace, "scope": target["scope"], "script_id": id_schema(), "title": text(120),
              "parameter_overrides": {"type": "object"}, "save_to_store": {"type": "boolean", "default": False},
              "generate_workflow": {"type": "boolean", "default": False},
              "generate_skill": {"type": "boolean", "default": False}}), "recording workspace asset"),
        _spec("processes.background.list", "read", "tasks.read",
              closed({}, {**thread, "active_only": {"type": "boolean", "default": False}}), "thread_if_present process"),
        _spec("processes.background.inspect", "read", "tasks.read", closed({"session_id": id_schema()},
              {"tail_lines": {**integer(1000, 1), "default": 80}}), "process"),
    ] + delivery_action_specs() + managed_action_specs()


# LLM: Owner字段是待核选择器而非权限；发送身份只能由Host绑定，接受不开放自动开启。
# 函数用途: 声明九个投递动作及有界闭合参数。
def delivery_action_specs():
    kind = {"enum": ["workflow", "monitor", "script", "skill"]}
    root = {"root_asset_kind": kind, "root_asset_id": id_schema()}
    asset = {"asset_kind": kind, "asset_id": id_schema()}
    delivery = {"delivery_id": id_schema()}
    dependencies = {"include_dependencies": {"type": "boolean", "default": True}}
    return [
        _spec("assets.delivery.targets.list", "read", "read assets.read", closed({})),
        _spec("assets.delivery.preview_bundle", "read", "read assets.read", closed(root, dependencies)),
        _spec("assets.delivery.send", "write", "write assets.write", closed({**root, "recipient_owner_id": text(128, 1)},
              {**dependencies, "note": {**text(2000), "default": ""}})),
        _spec("assets.delivery.inbox.list", "read", "read assets.read", closed({}, {
              "status_filter": {"enum": ["pending", "accepted", "rejected", "all"]}})),
        _spec("assets.delivery.accept", "write", "write assets.write", closed(delivery, {
              "conflict_strategy": {"enum": ["keep_both_rename", "overwrite", "skip"], "default": "keep_both_rename"},
              "auto_enable": {"type": "boolean", "enum": [False], "default": False}})),
        _spec("assets.delivery.reject", "write", "write assets.write", closed(delivery, {"reason": text(2000)})),
        _spec("assets.delivery.toggle_asset", "write", "write assets.write", closed({**asset, "enabled": {"type": "boolean"}})),
        _spec("assets.delivery.blocklist.list", "read", "read assets.read", closed({})),
        _spec("assets.delivery.blocklist.update", "write", "write assets.write", closed({
              "target_owner_id": text(128, 1), "mode": {"enum": ["block", "unblock"]}})),
    ]


# LLM: selector保留完整Owner字符串供领域核范围；治理初始修订为0，CAS不改变Handler的注册与名单复核。
# 函数用途: 为受管用户动作提供目标、修订和分页字段。
def managed_fields():
    return ({"target_owner_id": text(128, 1), "user_id": text(128, 1)},
            {"expected_revision": integer(2**63 - 1, 0)},
            {"limit": {**integer(100, 1), "default": 50}})


# LLM: 治理及压缩管理均要求public_only/admin_owned；强制开关三别名至少一个明确bool，冲突由领域写前拒绝，CAS/身份门不变。
# 函数用途: 声明十二个管理员受管用户动作，与WT2方法签名逐项对应。
def managed_action_specs():
    target, revision, paging = managed_fields()
    compact_patch = closed({}, {"default_trigger_percent": {"type": ["integer", "null"], "minimum": 50, "maximum": 95},
                                "force_locked": {"type": "boolean"}})
    force_aliases = ("force_compact_trigger_percent", "admin_force_compact_trigger_percent", "memory_compact_admin_forced")
    force_policy = closed({}, {**revision, "trigger_percent": integer(95, 50),
                              **{name: {"type": ["boolean", "null"]} for name in force_aliases}})
    force_policy["anyOf"] = [{"required": [name], "properties": {name: {"type": "boolean"}}} for name in force_aliases]
    scope = "public_only admin_owned"
    kind = {"enum": ["workflow", "monitor", "script", "skill"]}
    asset = {"asset_kind": kind, "asset_id": id_schema()}
    return [
        _spec("users.managed.overview.get", "read", "read users.read", closed({}, target), scope),
        _spec("users.managed.compact_policy.get", "read", "read users.read", closed({}, target), scope),
        _spec("users.managed.compact_policy.set", "write", "write users.manage",
              closed({"policy_patch": compact_patch}, {**target, **revision}), scope),
        _spec("users.managed.compact_policy.batch_set", "write", "write users.manage",
              closed({"policy_patch": compact_patch}, {
                  "scope": {"enum": ["all_managed_users"]},
                  "user_ids": {"type": "array", "items": text(128, 1), "minItems": 1, "maxItems": 1000, "uniqueItems": True},
                  "expected_revisions": {"type": "object"}}), scope),
        _spec("settings.compact_policy.admin_force.set", "write", "write users.manage",
              force_policy, scope),
        _spec("users.managed.policy.set", "write", "write users.manage", closed({"policy_patch": {"type": "object"}}, {**target, **revision}), scope),
        _spec("users.managed.persona.get", "read", "read users.read", closed({}, target), scope),
        _spec("users.managed.persona.set", "write", "write users.manage", closed({}, {**target, **revision,
              "soul_md": text(24000), "agents_md": text(24000), "user_md": text(24000),
              "persona_mode": {"enum": ["admin_locked", "admin_base_plus_user", "user_autonomous"]}}), scope),
        _spec("users.managed.memories.list", "read", "read users.read", closed({}, {**target, "category": text(64, 1),
              "include_candidates": {"type": "boolean", "default": True}, "status_filter": text(64, 1)}), scope),
        _spec("users.managed.memories.mutate", "write", "write users.manage", closed({"operation": {"enum": [
              "add_memory", "edit_memory", "delete_memory", "approve_candidate", "reject_candidate", "clear_stale_candidates"]}},
              {**target, **revision, "entry_id": id_schema(), "candidate_id": id_schema(), "content": text(24000),
               "category": text(64, 1), "locked": {"type": "boolean"}}), scope),
        _spec("users.managed.assets.provision", "write", "write users.manage", closed(asset, {**target,
              "auto_enable": {"type": "boolean", "default": False}, "conflict_strategy": {"enum": ["overwrite", "keep_both_rename", "skip"], "default": "overwrite"},
              "note": text(2000)}), scope),
        _spec("users.managed.assets.toggle", "write", "write users.manage", closed({**asset, "enabled": {"type": "boolean"}}, target), scope),
        _spec("users.managed.tasks.dispatch", "write", "write users.manage", closed({"prompt": text(16384, 1)}, {**target,
              "title": text(120), "thread_id": id_schema(), "priority": {"enum": ["normal", "urgent"]}}), scope),
        _spec("users.managed.tasks.list", "read", "read users.read", closed({}, {**target, **paging, "status_filter": text(64, 1)}), scope),
        _spec("governance.reviews.list", "read", "read users.read", closed({}, {**target,
              "status_filter": text(64, 1), "review_type_filter": text(64, 1)}), scope),
        _spec("governance.reviews.decide", "write", "write users.manage", closed({"review_id": id_schema(),
              "decision": {"enum": ["approve", "reject_with_feedback"]}, **revision}, {**target, "feedback": text(2000)}), scope),
    ]


V2_ACTIONS = frozenset(spec.name for spec in v2_action_specs())
