# LLM: 仅接已授权宿主上下文；A0路由归W5/W6。设置CAS与原Store共锁，真实Compact不持线程锁调用模型。
# 模块用途: 统一线程设置及别名的领域处理、数值用量、压缩、删除和分叉，不建立第二会话库。
from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import asdict, replace

from ..common.json_io import locked_json_path
from ..common.nofollow_fs import unlink_file_beneath
from ..conversation.context_usage import context_usage_from_thread
from ..conversation.model_metrics import public_model_metrics
from ..conversation.store_threads import _drop_cached_thread
from ..gateway_parts.io import update_json_file_atomic
from ..settings.model_profiles import model_profiles_path, read_model_profiles
from ..user_space.approval_mode import execute_approval_mode_operation
from ..user_space.home_layout import home_paths as root_paths
from ..user_space.owner_resolver import home_paths_with_owner
from .errors import HostAPIError
from .model_settings_v2_handlers import (
    configured_thread,
    permission_mode,
    settings_base_revision,
    settings_fields,
)


# LLM: 直接委托摘要的唯一版本算法，不接受第二个旧设置基数，消息账变化即使同tick也参与CAS。
# 函数用途: 使设置、摘要和四个写入口读取相同修订。
def _effective_revision(row):
    from .conversations import thread_revision
    return thread_revision(row)


# LLM: 调用方负责鲜活授权；可选authorized再核安装交集，省略时仍强核同owner记录，agent只用于真实Compact/用量。
# 类用途: 对原ConversationStore提供可由多个接口别名共用的领域方法。
class ThreadSettingsV2Handlers:
    # LLM: home_paths只由宿主提供，构造不创建配置、模型或线程。
    # 函数用途: 固定本次线程存储、owner与可选运行时。
    def __init__(self, store, owner_context, *, agent=None, home_paths=None, authorized=None):
        self.store, self.context, self.agent, self.authorized = store, owner_context, agent, authorized
        self.home = home_paths or home_paths_with_owner(root_paths(owner_context.owner_home.root), owner_context.owner_home)
        if self.home.owner_home_dir != owner_context.owner_home.home_dir or self.home.owner_id != owner_context.owner_home.owner_id:
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)

    # LLM: owner错误统一404；CAS仅接受当前唯一公共修订，不能用另一种基数绕过消息追加后的冲突。
    # 函数用途: 核验准确线程及可选设置版本。
    def _require(self, params, row=None):
        from .conversations import thread_visible
        if row is None:
            try:
                row, error = self.store.threads.load_report(params.get("thread_id", ""))
            except (ValueError, TypeError):
                raise HostAPIError("RESOURCE_NOT_FOUND", 404) from None
            if error:
                raise HostAPIError("CORRUPT_THREAD", 500)
        home = self.context.owner_home
        if row is None or row.owner_id != home.owner_id or row.owner_home != str(home.home_dir):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        if self.authorized is not None and not thread_visible(row, self.authorized, self.context):
            raise HostAPIError("RESOURCE_NOT_FOUND", 404)
        revision = params.get("expected_revision")
        if revision is not None and (type(revision) is not int or revision < 1):
            raise HostAPIError("INVALID_SCHEMA")
        if revision is not None and revision != _effective_revision(row):
            raise HostAPIError("REVISION_CONFLICT", 409)
        return row

    # LLM: 读取只投影模型名及偏好；无默认模型事实时名称保持未知，不回显连接或凭据。
    # 函数用途: 返回统一设置及Coding/Pentest共用的双名能力包字段。
    def _settings(self, row):
        data = read_model_profiles(model_profiles_path(self.home))
        name = data["profiles"].get(row.model_profile_id, {}).get("model_name", "")
        if row.model_profile_id == "default":
            name = getattr(getattr(self.agent, "config", None), "model_name", "")
        return {"thread_id": row.thread_id, **settings_fields(row, self.home), "model_name": name,
            "workspace_root": row.workspace_root or row.cwd, "revision": _effective_revision(row)}

    # LLM: get与packs.get共用；不写线程或默认配置。
    # 函数用途: 读取一个本owner线程的有效设置。
    def get_thread_settings(self, params):
        return self._settings(self._require(params))

    # LLM: 空thread只接受owner权限默认；真实权限写沿原审批入口，线程设置在原update_atomic锁内CAS。
    # 函数用途: 原子更新选中字段、保留其余元数据并递增线程版本。
    def update_thread_settings(self, params):
        if not params.get("thread_id"):
            if set(params) - {"thread_id", "permission_mode", "expected_revision"} or params.get("expected_revision") is not None:
                raise HostAPIError("INVALID_SCHEMA")
            return execute_approval_mode_operation(self.home, "set", permission_mode(params.get("permission_mode"), self.home))
        self._require(params)
        # LLM: 闭包只生成新快照，原Store校验模型选择及插件回执，异常不会提交半份设置。
        # 函数用途: 在最新版本上合并本次显式偏好。
        def update(row):
            self._require(params, row)
            return replace(configured_thread(row, params, self.home), revision=row.revision + 1, updated_at=time.time())
        return self._settings(self.store.threads.update_atomic(params["thread_id"], update))

    # LLM: 数值只能来自原公开遥测白名单；缺报保留None，agent检查器提供估算而非伪造供应商账本。
    # 函数用途: 投影上下文、累计用量、压缩代次和最后一帧速度。
    def get_thread_usage(self, params):
        row = self._require(params)
        usage, stats = context_usage_from_thread(row), public_model_metrics(row.model_metrics)
        if self.agent is not None:
            from ..conversation.compact import inspect_conversation_context
            usage = {**usage, **asdict(inspect_conversation_context(self.agent, self.store, row))}
        used, window = usage.get("current_tokens", usage.get("projected_tokens")), usage.get("context_window_tokens")
        return {"thread_id": row.thread_id, "context_used_tokens": used, "used_tokens": used,
            "context_max_tokens": window, "context_window_tokens": window,
            "trigger_tokens": usage.get("compact_trigger_tokens", usage.get("trigger_tokens")),
            "cumulative_input_tokens": stats.get("input_tokens"), "cumulative_output_tokens": stats.get("output_tokens"),
            "cached_tokens": stats.get("cache_read_input_tokens") if stats.get("cache_read_reported_calls") else None,
            "compact_count": row.compact_generation, "compact_generation": row.compact_generation,
            "last_turn_elapsed_seconds": None,
            "last_turn_tokens_per_sec": stats.get("output_tps"), "breakdown": usage, "stats": stats}

    # LLM: 原线程锁内先核CAS/活动状态，再释放锁调用真实Compact；没有agent只报503，不伪造边界。
    # 函数用途: 拒绝忙碌线程后强制真实压缩，返回真实代次与统一修订。
    def compact_thread(self, params):
        row = self._require(params)
        instruction = params.get("instruction", "")
        if not isinstance(instruction, str) or len(instruction) > 16384:
            raise HostAPIError("INVALID_SCHEMA")
        from .conversations import ConversationDomain
        with locked_json_path(self.store.storage.thread_path(row.thread_id)), ExitStack() as locks:
            row = self._require(params)
            ConversationDomain(None, None, default_thread=lambda *_: False).check_archive(self.store, row, self.context, locks)
        if self.agent is None:
            raise HostAPIError("HOST_NOT_READY", 503)
        from ..conversation.compact import ConversationCompactOptions, prepare_conversation_context
        result = prepare_conversation_context(self.agent, self.store, row, options=ConversationCompactOptions(
            force=True, bypass_circuit_breaker=True, custom_instructions=instruction))
        if not result.compacted:
            return {"thread_id": row.thread_id, "compacted": False, "compact_generation": result.thread.compact_generation,
                    "revision": _effective_revision(result.thread), "summary_preview": result.thread.summary[:512]}
        # LLM: agent已完成真实提交，不拿旧expected_revision覆盖新代次；边界写前比完整快照且保留消息锚。
        # 函数用途: 保存边界标记及本接口修订，保持并发新增消息和原选择事实。
        def boundary(latest):
            from .conversations import message_revision_state
            self._require({"thread_id": row.thread_id}, latest)
            if latest.to_dict() != result.thread.to_dict():
                raise HostAPIError("REVISION_CONFLICT", 409, "committed", "压缩已经提交，但后续状态已变；请重新读取，不要自动重放。")
            state = dict(latest.metadata.get("plugin_host_v2", {}))
            generation = latest.compact_generation
            state.update(revision=state.get("revision", settings_base_revision(latest) + 1), compact_boundary={"generation": generation, "instruction": instruction})
            return replace(latest, compact_generation=generation, revision=latest.revision + 1,
                metadata={**latest.metadata, "plugin_host_v2": message_revision_state(latest, state)})
        updated = self.store.threads.update_atomic(row.thread_id, boundary)
        return {"thread_id": row.thread_id, "compacted": True, "compact_generation": updated.compact_generation,
                "revision": _effective_revision(updated), "summary_preview": updated.summary[:512]}

    # LLM: canonical线程→goal/claim锁序沿归档合同；删除前重核owner/CAS/活动状态，同锁删除并失效缓存。
    # 函数用途: 删除空闲线程、消息及两份精确索引，不停止活动回合或递归删除其它资源。
    def delete_thread(self, params):
        from .conversations import ConversationDomain
        row = self._require(params)
        path = self.store.storage.thread_path(row.thread_id)
        with locked_json_path(path), ExitStack() as locks:
            row = self._require(params)
            ConversationDomain(None, None, default_thread=lambda *_: False).check_archive(self.store, row, self.context, locks)
            unlink_file_beneath(self.store.storage.root, path.relative_to(self.store.storage.root).parts)
            _drop_cached_thread(path)
            msg_path = self.store.storage.message_path(row.thread_id)
            if msg_path.exists():
                from ..conversation.message_selection import _POSITION_CACHE, _POSITION_CACHE_LOCK
                stat = msg_path.stat()
                unlink_file_beneath(self.store.storage.root, msg_path.relative_to(self.store.storage.root).parts)
                # 当前消息缓存按设备/inode建索引，没有按路径失效函数；只丢弃这份源的投影。
                with _POSITION_CACHE_LOCK:
                    _POSITION_CACHE.pop((stat.st_dev, stat.st_ino), None)
            for index in (self.store.storage.bindings_path, self.store.storage.user_latest_path):
                if index.exists():
                    update_json_file_atomic(index, lambda data: {k: v for k, v in data.items() if v != row.thread_id}
                                            if isinstance(data, dict) else {})
        return {"thread_id": row.thread_id, "deleted": True}

    # LLM: 原分叉服务复制消息与类型化模型；只拷贝偏好，不复制源安装来源、回执或活动任务。
    # 函数用途: 接受两个消息边界别名并保留权限、推理及能力包偏好。
    def fork_thread(self, params):
        from ..conversation.thread_fork_service import fork_conversation_thread
        request = {**params, "thread_id": params["source_thread_id"]}
        source = self._require(request)
        with locked_json_path(self.store.storage.thread_path(source.thread_id)):
            source = self._require(request)
            result = fork_conversation_thread(self.store, source_thread_id=source.thread_id,
                up_to_message_id=params.get("up_to_message_id", params.get("source_message_id")), title=params.get("title"),
                owner_home=self.context.owner_home.home_dir)
        prefs = {key: value for key, value in source.metadata.get("plugin_host_v2", {}).items()
                 if key in {"permission_mode", "preferred_pack_ids"}}
        branch = self.store.threads.update_atomic(result["thread_id"], lambda row: replace(row,
            metadata={**row.metadata, "plugin_host_v2": {**prefs, "revision": 1,
                **({"installation_ref": self.authorized.binding.installation_ref} if self.authorized is not None else {})}}))
        return {**result, "thread": branch.to_dict()}
