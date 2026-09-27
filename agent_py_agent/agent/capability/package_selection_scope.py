# LLM: 开关、主执行身份、原工具权限和包快照共同决定可选准备资格；只读元数据，不调用模型、正文或安装器。
# 模块用途: 普通任务晋升与 Goal 新建共用一次选包的初始化条件，缺少资格时不增加持久字段。
from __future__ import annotations

import logging
from concurrent.futures import CancelledError
from dataclasses import dataclass

from ..common.cancellation import ToolCancelled
from ..conversation.capability_selection_state import TaskCapabilitySelection
from ..runtime_context import current_subagent_run_id
from .config import CapabilityConfig
from .runtime_config_reload import capability_config_for_agent
from .skill_snapshot import SkillSnapshot


# LLM: 这是当前 owner/run 的只读快照，不授予读正文或工具执行权；后续准入必须继续走原 ActionPolicy。
# 类用途: 把开关、候选目录和现有工具范围固定给同一次准备使用。
@dataclass(frozen=True)
class PackageSelectionScope:
    config: CapabilityConfig
    skills: SkillSnapshot
    tools: object


# LLM: 已有 run 使用原冻结工具/Skill 快照；Goal 控制入口传可信 cwd，只生成元数据快照，不准备连接或执行资源。
# 函数用途: 判断当前主执行是否允许准备包选择；关闭、孩子、无原读权限或无包时直接跳过。
def package_selection_scope(agent: object, params: object = None, *, workspace_root: object = None) -> PackageSelectionScope | None:
    config = capability_config_for_agent(agent)
    if config is None or not config.enable_capability_package_selection or current_subagent_run_id(agent):
        return None
    agent_config = getattr(agent, "config", None)
    if not (getattr(agent_config, "enable_tools", False) and getattr(agent_config, "enable_plugins", False)):
        return None
    allowed = getattr(params, "allowed_tools", None)
    if allowed is not None and "skill_search" not in allowed:
        return None
    tools = getattr(params, "tool_runtime_snapshot", None)
    if tools is None:
        registry = getattr(agent, "tools", None)
        if registry is None:
            return None
        tools = registry.runtime_snapshot(allowed_tools=allowed, run_id=str(getattr(params, "run_id", "") or ""))
    runtime = tools.runtime("skill_search")
    if runtime is None or not runtime.availability.available or not runtime.exposure.model_visible:
        return None
    skills = (agent.skills_service.snapshot_for(workspace_root)
              if workspace_root is not None else agent.current_skill_snapshot())
    return PackageSelectionScope(config, skills, tools) if skills.packages else None


# LLM: 仅原 TaskStore 真正新建链接时接受此 typed 值，已有任务/回滚缺键不可补写；异常只关闭增强，不使 Goal 创建失败。
# 函数用途: 为符合条件的新主任务提供 pending 初值，不写文件或为旧任务补选。
def new_task_capability_selection(agent: object, params: object = None, *, workspace_root: object = None) -> TaskCapabilitySelection | None:
    try:
        return TaskCapabilitySelection.pending() if package_selection_scope(agent, params, workspace_root=workspace_root) else None
    except (InterruptedError, ToolCancelled, CancelledError, KeyboardInterrupt):
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning("CAPABILITY_SELECTION_SCOPE_UNAVAILABLE: %s", type(exc).__name__)
        return None
