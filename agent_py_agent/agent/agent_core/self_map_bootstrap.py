# LLM: 这里只负责 composition；各领域自行登记 reader，聚合工具和 schema 都不维护固定领域实现清单。
# 模块用途: 在原 Agent 装配阶段接入只读自我目录，不创建服务或触发运行维护。
from ..capability.self_map_memory import register_memory_section
from ..capability.self_map_skills import register_skill_section
from ..conversation.self_map_context import register_context_section
from ..conversation.self_map_work import register_work_section
from ..plugin_self_map import register_install_sections
from ..settings.self_map_settings import register_setting_sections
from ..tooling.self_map_mcp import register_mcp_section
from ..tooling.self_map_registry import SelfMapRegistry
from ..tooling.self_map_tool import SelfMapTool
from ..tooling.self_map_tools import register_tool_section
from ..user_space.self_map_paths import register_path_sections


# LLM: 开关仅在新 Agent 装配生效；main 已绑定 list_tools 的本人公开读口保持原样；子代理仍由父允许快照交集裁剪。
# 函数用途: 按开关登记各领域读者和自我目录入口，不刷新或创建领域服务。
def register_self_map(agent):
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
