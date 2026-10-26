# LLM: WP-4扩展目录只声明静态合同；由action_specs在原类型定义完成后加载，不接受插件动态注册。
# 模块用途: 集中声明20个新宿主动作，保留原有闭合参数、显式能力及写入幂等门。
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


# LLM: 需求矩阵逐项测试；提炼草稿含历史须read与chat.history双能力，复杂字典由领域schema二次验证。
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
                     "title": text(120), **workspace}), "source_thread workspace"),
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
    ]


V2_ACTIONS = frozenset(spec.name for spec in v2_action_specs())
