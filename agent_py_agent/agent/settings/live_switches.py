"""按文件代次刷新已注册配置的布尔开关；不写配置、不授予写权限。"""
from __future__ import annotations

import threading
import weakref
from copy import copy
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from ..common.cache_freshness import cache_entry_trustworthy, cache_stat_signature
from .config import AgentConfig, load_config, normalize_agent_memory_config
from .memory import MemorySettings
from .runtime_guard_config import RuntimeGuardPolicy, runtime_guard_policy

DEPLOY_LIVE_SWITCH_KEYS = frozenset({
    "admin_channel_identity_enabled", "audit_enabled", "auth_enabled",
    "daemon_mutate_state", "daemon_start_runners", "enable_gateway_memory_diagnostics",
    "feishu_bot_subscriptions_enabled", "feishu_cli_backend_enabled",
    "feishu_cloud_verify_sheet_writes_enabled", "feishu_user_oauth_enabled",
    "gateway_per_user_owner_scoping", "gateway_restart_force_on_drain_timeout",
    "hard_failure_halt_enabled", "im_group_authorization_required", "im_model_cards_enabled",
    "plugin_process_sandbox", "protect_running_runtime", "terminal_block_enabled",
})
LIVE_SWITCH_KEYS = frozenset({
    "action_candidate_auto_execute_enabled", "agents_source_enabled", "agents_source_network_enabled",
    "anthropic_prompt_cache_enabled", "assistant_progress_updates_enabled", "auto_save_memory",
    "background_process_listen_scope_enforce", "background_process_notifications",
    "background_public_receipt_contract_enabled", "background_user_action_notice_enabled",
    "business_spaces_enabled", "capability_pack_host_verification_enabled", "capability_pack_mcp_enabled",
    "capability_pack_self_install_enabled", "compact_recall_hint_enabled", "computer_use_enabled",
    "computer_use_observation_enabled", "conversation_method_carry_enabled",
    "conversation_terminal_tool_fold_enabled", "decision_skip_records_enabled",
    "enable_capability_package_recommendations", "enable_capability_package_selection",
    "enable_file_syntax_diagnostics", "enable_gateway_restart_tool", "enable_memory_search_tool",
    "enable_model_profile_tool", "enable_plugins", "enable_self_learning", "enable_self_map",
    "enable_subagents", "enable_tools", "feishu_chat_history_enabled",
    "feishu_cloud_collaborator_authorization_enabled", "feishu_cloud_tools_enabled",
    "feishu_group_require_bot_mention", "feishu_image_input_enabled", "feishu_quoted_media_enabled",
    "feishu_quoted_text_enabled", "feishu_session_lock_enabled", "force_compact_trigger_percent",
    "foreground_unknown_auto_recover", "home_context_enabled", "im_group_shared_credentials_enabled",
    "im_progress_cards_enabled", "im_scheduled_tasks_background", "im_task_credentials_enabled",
    "im_task_delegation_enabled", "im_tool_approvals_enabled", "im_topic_aggregation_enabled",
    "memory_compact_remote_enabled", "memory_compact_semantic_summary_enabled", "memory_curator_enabled",
    "memory_hook_enabled", "memory_rule_notices_enabled", "memory_semantic_recall",
    "model_truncation_thinking_downgrade", "plugin_events_enabled", "plugin_legacy_sandbox_default",
    "plugin_self_install_enabled", "prompt_runtime_snapshots_enabled", "public_capability_packages_enabled",
    "query_zero_result_review_enabled", "reasoning_control_auto_probe", "run_task_workspace_enabled",
    "self_learning_include_background", "session_messaging_admin_enabled", "session_messaging_user_enabled",
    "session_task_admin_enabled", "shell_sandbox_boundary_facts", "shell_sandbox_hide_user_home",
    "skill_transfers_enabled", "stream_enabled", "subagent_activity_notices_enabled",
    "subagent_failure_auto_split_enabled", "subagent_input_media_enabled",
    "subagent_stream_activity_projection_enabled", "subagent_takeover_hint_enabled",
    "task_progress_closeout_guidance_enabled", "tool_catalog_include_examples",
    "tool_output_externalize_on_low_headroom", "tool_program_enabled", "tool_vector_search_enabled",
    "tui_follow_gateway_upgrade", "tui_mouse_capture_default",
}) | DEPLOY_LIVE_SWITCH_KEYS

_AUTH_KEYS = frozenset({"auth_enabled", "gateway_per_user_owner_scoping", "admin_channel_identity_enabled"})
_GUARD_KEYS = frozenset({"terminal_block_enabled", "hard_failure_halt_enabled"})
_MEMORY_KEYS = LIVE_SWITCH_KEYS & frozenset(item.name for item in fields(MemorySettings))
_LOCK = threading.RLock()
_AUTH_PIPELINE_PRESENT = threading.Event()


@dataclass(eq=False)
class _FileConfigs:
    path: Path
    fresh: AgentConfig
    signature: tuple | None
    configs: weakref.WeakSet = field(default_factory=weakref.WeakSet)
    policies: dict = field(default_factory=dict)


@dataclass(eq=False)
class _ConfigBinding:
    config: weakref.ReferenceType
    source: _FileConfigs
    applied: dict[str, bool]
    guard_applied: dict[str, bool]
    raw_path: str | Path


# 来源字典的公开键值不变；replace 保留本进程来源对象，下一份配置可继承原文件基线。
# 基线放在实例属性，不参与 JSON/asdict 的字典项，也不引用配置对象。
class _ConfigSources(dict):
    pass


# AgentConfig 不可哈希：WeakSet 保存身份绑定，配置对象仅被 weakref 引用。
# 绑定不是 dataclass 字段，replace 得到的副本必须单独注册；最后一个配置释放后，整组缓存也释放。
_FILES: weakref.WeakValueDictionary[Path, _FileConfigs] = weakref.WeakValueDictionary()


# LLM: 仅登记真实文件配置；空路径的 managed owner 不参与，内部绑定不被 replace 复制；不写文件。
# 函数用途: 将一个配置副本加入同源文件的弱引用组，保留每个字段上次采用的文件值。
def register_live_config(config) -> None:
    with _LOCK:
        _register(config)


# LLM: 调用方持有模块锁；用文件值建立三方基线，不把已有 owner 覆盖误当作文件默认。
# 函数用途: 取得或创建配置对象的独立身份绑定。
def _register(config) -> _ConfigBinding | None:
    raw_path = getattr(config, "config_path", "")
    if not isinstance(config, AgentConfig) or not isinstance(raw_path, (str, Path)) or not raw_path:
        return None
    binding = getattr(config, "_live_switch_binding", None)
    if isinstance(binding, _ConfigBinding) and binding.config() is config and binding.raw_path == raw_path:
        return binding
    path = Path(raw_path).expanduser().resolve()
    source = _FILES.get(path)
    if source is None:
        stat = path.stat()
        fresh = load_config(path)
        source = _FileConfigs(path, fresh, cache_stat_signature(stat) if cache_entry_trustworthy(stat.st_mtime_ns) else None)
        _FILES[path] = source
    policy = config.runtime_guard_policy
    provenance = config.config_sources
    inherited = isinstance(provenance, _ConfigSources) and getattr(provenance, "path", None) == path
    applied = dict(provenance.applied) if inherited else {
        key: getattr(source.fresh, key) for key in LIVE_SWITCH_KEYS if type(getattr(source.fresh, key, None)) is bool
    }
    guard_applied = dict(provenance.guard_applied) if inherited else (
        {key: getattr(policy, key) for key in _GUARD_KEYS} if isinstance(policy, RuntimeGuardPolicy) else {}
    )
    binding = _ConfigBinding(weakref.ref(config), source, applied, guard_applied, raw_path)
    _publish_provenance(config, binding)
    source.configs.add(binding)
    config._live_switch_binding = binding
    return binding


# LLM: 每次发布独立、只含不可变键值对的基线；延迟注册的 replace 副本保留克隆时那一代，不受父配置后续刷新污染。
# 函数用途: 更新配置的进程内来源元数据，公开来源字典内容不变。
def _publish_provenance(config, binding) -> None:
    applied = tuple(binding.applied.items())
    guard_applied = tuple(binding.guard_applied.items())
    previous = config.config_sources
    if (isinstance(previous, _ConfigSources) and getattr(previous, "path", None) == binding.source.path
            and previous.applied == applied and previous.guard_applied == guard_applied):
        return
    provenance = _ConfigSources(previous)
    provenance.path = binding.source.path
    provenance.applied = applied
    provenance.guard_applied = guard_applied
    config.config_sources = provenance


# LLM: AuthManager 构造时声明本进程具备认证流水线；不创建 middleware，不增加身份或写权限。
# 函数用途: 允许随后刷新已校验的认证开关。
def note_auth_pipeline_present() -> None:
    with _LOCK:
        _AUTH_PIPELINE_PRESENT.set()


# LLM: 同一锁内按文件代次解析并整值赋值；未变 YAML 一次 stat、零解析，近期 mtime 不缓存；只改 bool 与守卫快照。
# 函数用途: 把最新文件开关应用到所有已注册副本，并返回实际变化的键。
def refresh_live_switches(config=None, *, path=None, force=False) -> frozenset[str]:
    with _LOCK:
        binding = _register(config) if config is not None else None
        if path is None:
            source = binding.source if binding is not None else None
        else:
            source = _FILES.get(Path(path).expanduser().resolve())
        if source is None:
            return frozenset()
        stat = source.path.stat()
        signature = cache_stat_signature(stat)
        trustworthy = cache_entry_trustworthy(stat.st_mtime_ns)
        if force or not trustworthy or signature != source.signature:
            fresh = load_config(source.path)
            source.fresh = fresh
            source.signature = signature if trustworthy else None
        changed = set()
        checked_policies = {}
        for registered in tuple(source.configs):
            candidate = registered.config()
            if candidate is None:
                continue
            try:
                changed.update(_apply_switches(candidate, registered, source.fresh))
                changed.update(_refresh_guard(candidate, registered, checked_policies, force=force))
            finally:
                # 策略加载失败不能丢掉已经采用的 YAML 基线；无变化时发布器不分配新来源对象。
                _publish_provenance(candidate, registered)
        return frozenset(changed)


# LLM: 认证三项作为一组校验，既核新文件，也核 owner 覆盖后的实际组合；错误仅投影固定原因，不回显凭据。
# 函数用途: 拒绝不安全的认证变化，并返回去重用的告警。
def _auth_warning(fresh, candidate) -> str | None:
    from ..auth.configuration import AuthConfigurationError, validate_gateway_auth_configuration

    try:
        validate_gateway_auth_configuration(fresh)
        validate_gateway_auth_configuration(candidate)
    except AuthConfigurationError as exc:
        return f"live config auth update rejected: {exc.reason_code}"
    if not _AUTH_PIPELINE_PRESENT.is_set():
        return "live config auth update rejected: auth_pipeline_missing"
    return None


# LLM: 每副本只覆盖仍等于上次文件值的字段；局部覆盖不推进其基线，反向切换也不会意外丢覆盖。
# 函数用途: 应用 agent 家族开关，必要时按原入口校验 Memory 镜像。
def _apply_switches(config, binding, fresh) -> set[str]:
    updates = {
        key: getattr(fresh, key) for key, previous in binding.applied.items()
        if getattr(config, key) == previous and getattr(fresh, key) != previous
    }
    if any(getattr(config, key) != updates.get(key, getattr(config, key)) for key in _AUTH_KEYS):
        candidate = copy(config)
        for key in _AUTH_KEYS & updates.keys():
            setattr(candidate, key, updates[key])
        warning = _auth_warning(fresh, candidate)
        if warning is not None:
            if warning not in config.config_warnings:
                config.config_warnings = [*config.config_warnings, warning]
            updates = {key: value for key, value in updates.items() if key not in _AUTH_KEYS}
    changed = set()
    for key, value in updates.items():
        # 局部值在准备阶段变化时不提交；更重要的是，不为未变化字段制造覆盖写入。
        if getattr(config, key) != binding.applied[key]:
            continue
        setattr(config, key, value)
        binding.applied[key] = value
        changed.add(key)
    if changed & _MEMORY_KEYS:
        # 原归一器会写全部 Memory 数字项；在独立浅副本上运行，只采用本次布尔镜像及诊断。
        normalized = copy(config)
        normalize_agent_memory_config(normalized)
        for key in changed & _MEMORY_KEYS:
            setattr(config, key, getattr(normalized, key))
        config.memory_config_warnings = normalized.memory_config_warnings
    return changed


# LLM: 仅重核已绑定宿主策略的来源，每个策略文件每次刷新只 stat 一次；数字预算保持原快照，不从 YAML 猜宿主根。
# 函数用途: 检测部署策略文件变化，将两个守卫 bool 的新快照交给配置消费者。
def _refresh_guard(config, binding, checked, *, force) -> set[str]:
    policy = config.runtime_guard_policy
    if not isinstance(policy, RuntimeGuardPolicy) or not policy.source_path:
        return set()
    path = Path(policy.source_path)
    source = binding.source
    if path not in checked:
        try:
            stat = path.stat()
        except FileNotFoundError:
            signature, trustworthy = None, False
        else:
            signature = cache_stat_signature(stat)
            trustworthy = cache_entry_trustworthy(stat.st_mtime_ns)
        cached = source.policies.get(path)
        if force or not trustworthy or cached is None or cached[0] != signature:
            fresh = runtime_guard_policy(path, home_root=path.parent.parent)
            if trustworthy:
                source.policies[path] = (signature, fresh)
            else:
                source.policies.pop(path, None)
        else:
            fresh = cached[1]
        checked[path] = fresh
    fresh = checked[path]
    updates = {
        key: getattr(fresh, key) for key in _GUARD_KEYS
        if getattr(policy, key) == binding.guard_applied.get(key, getattr(policy, key))
    }
    changed = {key for key, value in updates.items() if getattr(policy, key) != value}
    if changed:
        replacement = replace(policy, **updates)
        for name in ("_source_path", "_loaded_at", "_configured_fields"):
            object.__setattr__(replacement, name, getattr(fresh, name))
        config.runtime_guard_policy = replacement
    binding.guard_applied.update(updates)
    return changed
