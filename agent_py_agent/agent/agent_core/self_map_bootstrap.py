# LLM: 这里只负责 composition；各领域自行登记 reader，聚合工具和 schema 都不维护固定领域实现清单。
# 模块用途: 在原 Agent 装配阶段接入 Owner 一等公民工具（scripts/workflows）与只读自我目录，不创建服务或触发运行维护。
from ..capability.self_map_memory import register_memory_section
from ..capability.self_map_skills import register_skill_section
from ..conversation.self_map_context import register_context_section
from ..conversation.self_map_work import register_work_section
from ..plugin_self_map import register_install_sections
from ..settings.self_map_settings import register_setting_sections
from ..tooling.scripts_tool import ScriptsTool
from ..tooling.self_map_mcp import register_mcp_section
from ..tooling.self_map_registry import SelfMapRegistry
from ..tooling.self_map_tool import SelfMapTool
from ..tooling.self_map_tools import register_tool_section
from ..tooling.workflows_tool import WorkflowsTool
from ..user_space.self_map_paths import register_path_sections


# LLM: 开关仅在新 Agent 装配生效；main/user 登记本 Owner 的 scripts/workflows 一等公民工具；子代理仍由父允许快照交集裁剪。
# 函数用途: 按 Owner 范围登记固化脚本、工作流工具与自我目录入口，不刷新或创建领域服务。
def register_self_map(agent):
    if str(getattr(agent.tools, "owner_type", "") or "") in {"main_agent", "user"}:
        owner_home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
        if owner_home is not None:
            agent.tools.register(ScriptsTool(owner_home))
            agent.tools.register(WorkflowsTool(owner_home))
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
