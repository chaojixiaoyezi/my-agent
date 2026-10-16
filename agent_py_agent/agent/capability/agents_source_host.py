# LLM: 构造仅绑定规范owner/home身份锚点；每次来源使用仍读原活权限，坏权限保持拒绝，不能阻断closed宿主恢复。
# 模块用途: 将唯一AGENTS来源服务接进现有宿主身份和实时权限裁决，不签权或新增网络许可。
from ..path_access_policy import PathAccessPolicy
from ..user_space.owner_access import resolve_owner_scope_and_access
from ..user_space.owner_policy import resolve_effective_owner_policy
from ._agents_contract_fields import AgentsContractError
from .agents_source_repository import AgentsSourceRepository


# LLM: 静态normal owner-wall只供repository构造核owner/home，不能替代原path_policy_provider；file_commit仍只来自受信组合根。
# 函数用途: 装配可恢复的owner来源服务；真实本地访问与URL许可仍逐次复核，构造不读取来源正文或发网络请求。
def build_agents_source_repository(agent, *, file_commit=None):
    # LLM: 复用原owner/full-access裁决，不以source配置或构造时的策略快照扩大目录权限。
    # 函数用途: 为本次来源访问读取同一宿主的最新正常路径权限。
    def path_policy():
        policy = resolve_effective_owner_policy(agent.home_paths)
        if any(error.get("context") == "owner_policy.permissions" for error in policy.load_errors):
            raise AgentsContractError("AGENTS_SOURCE_PERMISSION_REVOKED", "locator")
        scope, access = resolve_owner_scope_and_access(agent.home_paths, agent.config, policy)
        mode = "full" if access == "full-access" and not scope else agent.config.path_access_mode
        return PathAccessPolicy.from_values(mode=mode, owner_scope_root=scope,
                                            dangerous_roots=(*agent.config.path_dangerous_roots, *policy.dangerous_paths),
                                            agent_home_root=agent.home_paths.root)

    # LLM: 来源专用网络开关不授予HTTP工具权限；owner禁网、禁web_fetch或相关坏配置全部立即拒绝。
    # 函数用途: 将当前正常网络许可交给逐跳来源抓取和缓存复核。
    def url_allowed(url):
        policy = resolve_effective_owner_policy(agent.home_paths)
        relevant_error = any(error.get("context") in {"owner_policy.permissions", "owner_policy.tool_policy"}
                             for error in policy.load_errors)
        return not relevant_error and policy.network_enabled and "web_fetch" not in policy.disabled_tools

    from .agents_source_options import AgentsSourceOptions
    options = AgentsSourceOptions(file_commit=file_commit, path_policy_provider=path_policy,
                                  enabled=lambda: agent.config.agents_source_enabled is True,
                                  network_enabled=lambda: agent.config.agents_source_network_enabled is True,
                                  url_allowed=url_allowed)
    identity_policy = PathAccessPolicy.from_values(
        mode="normal", owner_scope_root=agent.home_paths.owner_home_dir, agent_home_root=agent.home_paths.root)
    return AgentsSourceRepository(owner=agent.home_paths.owner_id,
                                  owner_home=agent.home_paths.owner_home_dir, path_policy=identity_policy, options=options)
