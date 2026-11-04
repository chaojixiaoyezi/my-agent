"""唯一会话路由设置；原线程事务写入，查询不创建线程或目录。"""
from __future__ import annotations

import hashlib
from dataclasses import replace

from ..runtime_errors import DataCorruptionError
from .control_commands import ConversationControlResult


# LLM: 只接受命令枚举，不接受布尔或自然语言；空参数是只读status。
# 函数用途: 解析唯一route命令参数，非法值不进入写入路径。
def parse_route_mode(value: object) -> bool | str:
    if type(value) is not str or value.strip() not in {"", "status", "true", "false"}:
        raise ValueError("用法：/route true|false|status")
    return {"": "status", "status": "status", "true": True, "false": False}[value.strip()]


# LLM: owner取已认证home，actor只来自宿主参数；同一actor跨入口共享，群中其他actor不继承设置。
# 函数用途: 校验线程归属并生成不含明文发送者的设置键。
def _setting_key(home, thread, actor_id):
    if thread.owner_id != str(home.owner_id):
        raise PermissionError("route owner scope mismatch")
    actor = actor_id or thread.canonical_user_id
    if not isinstance(actor, str) or not actor or len(actor) > 1024:
        raise ValueError("route requires a trusted actor")
    return hashlib.sha256(actor.encode()).hexdigest()


# LLM: 坏设置不能默认为关闭后被覆盖；缺席默认关闭，不以旧每日enable开启永久模式。
# 函数用途: 从准确线程读取当前发送者的唯一route设置。
def _read_setting(home, thread, actor_id):
    if thread is None:
        return {"enabled": False, "source": "default", "revision": 0}
    key = _setting_key(home, thread, actor_id)
    state = thread.metadata.get("route_mode.v1")
    if state is None:
        return {"enabled": False, "source": "default", "revision": 0}
    if (not isinstance(state, dict) or state.get("owner_id") != str(home.owner_id)
            or not isinstance(state.get("actors"), dict)):
        raise DataCorruptionError("route setting is unreadable")
    row = state["actors"].get(key)
    if row is None:
        return {"enabled": False, "source": "default", "revision": 0}
    if (not isinstance(row, dict) or type(row.get("enabled")) is not bool
            or type(row.get("revision")) is not int or row["revision"] < 1):
        raise DataCorruptionError("route setting is invalid")
    return {"enabled": row["enabled"], "source": "thread", "revision": row["revision"]}


# LLM: 重读准确线程防止调用方旧快照；channel仅保留入口形状，不成为第二套配置键。
# 函数用途: 查询线程route设置，不创建缺失的会话。
def route_setting(agent, thread, *, actor_id: str = "", channel: str = "") -> dict:
    if thread is not None:
        _setting_key(agent.home_paths, thread, actor_id)
        thread = agent.conversation_store.threads.require(thread.thread_id)
    return _read_setting(agent.home_paths, thread, actor_id)


# LLM: 原ThreadStore只读初始化和解析，不构造Agent/ConversationStore、锁或目录；路径来自宿主配置。
# 函数用途: 冷Gateway按规范运行目录只读定位当前绑定线程。
def route_thread_for_paths(home, config, scope):
    from ..user_space.runtime_paths import runtime_paths_for_agent
    from .store_layout import ConversationStorage
    from .store_threads import ThreadStore

    root = runtime_paths_for_agent(config, home.owner_home_dir, home)["conversation_workspace"]
    threads = ThreadStore(ConversationStorage(root, initialize=False))
    thread, error = threads.resolve_report(channel=scope.channel,
        channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id)
    if error:
        raise DataCorruptionError("route thread binding is unreadable")
    if thread is not None:
        _setting_key(home, thread, scope.user_id)
    return thread


# LLM: 冷热入口共用同一元数据合同；返回独立标量字典，恢复不得据此重写旧任务。
# 函数用途: 从已定位线程得到冷入口route值。
def route_setting_for_paths(home, thread, *, actor_id: str = "") -> dict:
    return _read_setting(home, thread, actor_id)


# LLM: 原线程revision做CAS，冲突重读重试一次；保留并发metadata及其他线程字段，不改变任务或权限。
# 函数用途: 显式保存route模式并只递增当前发送者设置版本。
def set_route_mode(agent, thread, enabled: bool, *, actor_id: str = "", channel: str = "") -> dict:
    if type(enabled) is not bool or thread is None:
        raise ValueError("route requires a conversation and boolean mode")
    _setting_key(agent.home_paths, thread, actor_id)
    threads = agent.conversation_store.threads
    for attempt in range(2):
        expected = threads.require(thread.thread_id)

        # LLM: 原文件锁内再次校验owner及CAS，不用旧对象覆盖并发写入。
        # 函数用途: 仅更新当前actor路由设置并递增其版本。
        def update(current):
            key = _setting_key(agent.home_paths, current, actor_id)
            if current.revision != expected.revision:
                raise RuntimeError("ROUTE_REVISION_CONFLICT")
            previous = _read_setting(agent.home_paths, current, actor_id)
            state = current.metadata.get("route_mode.v1") or {"owner_id": str(agent.home_paths.owner_id), "actors": {}}
            row = {"enabled": enabled, "revision": previous["revision"] + 1}
            state = {**state, "actors": {**state["actors"], key: row}}
            return replace(current, metadata={**current.metadata, "route_mode.v1": state}, revision=current.revision + 1)

        try:
            saved = threads.update_atomic(thread.thread_id, update)
            return _read_setting(agent.home_paths, saved, actor_id)
        except RuntimeError as error:
            if str(error) != "ROUTE_REVISION_CONFLICT" or attempt:
                raise
    raise RuntimeError("ROUTE_REVISION_CONFLICT")


# LLM: 只在可信线程生效；无效参数和threadless不写；关闭不取消任何已受理任务。
# 函数用途: 执行route控制并返回当前模式及来源。
def execute_route_control(agent, command, thread, *, actor_id: str = "", channel: str = ""):
    if not command.valid:
        return ConversationControlResult("route", False, command.usage)
    if thread is None:
        return ConversationControlResult("route", False, "当前没有可用会话；/route 仅适用于会话线程。")
    try:
        value = parse_route_mode(command.value)
        setting = (route_setting(agent, thread, actor_id=actor_id, channel=channel) if value == "status"
                   else set_route_mode(agent, thread, value, actor_id=actor_id, channel=channel))
        capacity = _route_capacity(agent, thread, actor_id=actor_id, channel=channel)
        limit = '不限' if capacity.get('unlimited') else capacity['limit']
        available = '不限' if capacity.get('unlimited') else capacity['available']
        label = (f"{capacity['used']}/{limit}；剩余 {available}；无归属 {capacity['unassigned']}"
                 if capacity.get('known') else '未知（读取不完整）')
        message = (f"route：{str(setting['enabled']).lower()}；来源：{setting['source']}（当前会话/发送者）。"
                   f"后台 Agent 用量/上限：{label}。")
        if value == 'status':
            message += _legacy_migration_hint(agent, actor_id, channel)
        return ConversationControlResult("route", True, message, details={**setting, 'background_agents': capacity})
    except (OSError, ValueError, RuntimeError) as error:
        return ConversationControlResult("route", False, f"当前会话路由设置不可用：{error}")


# LLM: 从已认证的控制线程建立只读查询身份，复用原配额统计；可推进有界缓存，不能创建身份/任务或把未知补零。
# 函数用途: 为route控制返回已知容量或明确未知的只读投影。
def _route_capacity(agent, thread, *, actor_id: str = '', channel: str = ''):
    from uuid import uuid4

    from .background_agents import BackgroundAgentService
    from .host_actor import HostChannelActor

    try:
        actor = HostChannelActor(str(agent.home_paths.owner_id), thread.thread_id, 'route-' + uuid4().hex,
            channel or 'local', '', actor_id or thread.canonical_user_id)
        summary = BackgroundAgentService(agent).quota_summary(actor=actor)
        return {**summary, 'known': summary['coverage'] == 'complete'}
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
        return {'known': False, 'used': None, 'limit': getattr(agent.config, 'max_background_agents', None),
                'available': None, 'unassigned': None}


# LLM: 旧偏好仅在status按当前可信IM actor懒读；缺失不创建，坏记录只报告提示不可用，绝不写入route。
# 函数用途: 提醒用户显式迁移旧delegate偏好，不自动开启永久路由。
def _legacy_migration_hint(agent, actor_id, channel):
    from ..settings.im_task_preferences import delegation_preference, preference_path
    from .channels import supports_transcript_delivery

    if not actor_id or not channel or supports_transcript_delivery(channel) or not preference_path(agent).exists():
        return ''
    try:
        old = delegation_preference(agent, channel, actor_id)
        if old['revision']:
            return '检测到旧 /delegate 偏好；仅作迁移提示，请显式使用 /route true|false，旧值不控制新任务。'
    except (OSError, ValueError, AttributeError, RuntimeError):
        return '旧 /delegate 偏好不可读；未迁移，当前route设置保持不变。'
    return ''
