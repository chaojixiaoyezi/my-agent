# LLM: 这里只负责 composition；各领域自行登记 reader，聚合工具和 schema 都不维护固定领域实现清单。
# 模块用途: 装配Owner一等公民工具、本机管理员运行治理和只读目录；注册不创建服务、不触发运行维护。
from ..capability.self_map_memory import register_memory_section
from ..capability.self_map_skills import register_skill_section
from ..conversation.self_map_context import register_context_section
from ..conversation.self_map_work import register_work_section
from ..plugin_self_map import register_install_sections
from ..settings.self_map_settings import register_setting_sections
from ..tooling.runtime_governance_tool import RuntimeGovernanceTool
from ..tooling.scripts_tool import ScriptsTool
from ..tooling.self_map_mcp import register_mcp_section
from ..tooling.self_map_registry import SelfMapRegistry
from ..tooling.self_map_tool import SelfMapTool
from ..tooling.self_map_tools import register_tool_section
from ..tooling.workflows_tool import WorkflowsTool
from ..user_space.approval_mode import is_permission_admin
from ..user_space.self_map_paths import register_path_sections


# LLM: main/user原脚本工具保持；运行治理只给可信local/main，执行时另核身份和逐次授权；自我目录开关不改变普通脚本能力。
# 函数用途: 登记Owner脚本/工作流、管理员治理与目录入口，不刷新或启动领域服务。
def register_self_map(agent):
    if str(getattr(agent.tools, "owner_type", "") or "") in {"main_agent", "user"}:
        owner_home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
        if owner_home is not None:
            agent.tools.register(ScriptsTool(owner_home))
            agent.tools.register(WorkflowsTool(owner_home))
            if is_permission_admin(getattr(agent, 'home_paths', None)):
                agent.tools.register(RuntimeGovernanceTool(agent, is_admin=True))
    if not getattr(agent.config, "enable_self_map", True):
        return
    registry = SelfMapRegistry()
    register_path_sections(registry)
    register_tool_section(registry)
    register_skill_section(registry)
    register_install_sections(registry)
    register_mcp_section(registry)
    register_memory_section(registry)
    register_setting_sections(registry)
    register_work_section(registry)
    register_context_section(registry)
    agent.tools.register(SelfMapTool(agent, registry))
