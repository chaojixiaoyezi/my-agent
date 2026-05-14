# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、查看看板、执行 dispatch 都在这里，真实业务再转给 SimpleAgent 和 SubAgentManager。
"""

import json
import re
from typing import TYPE_CHECKING

from ..action_protocol import subagent_schedule_envelope_from_payload
from ..subagents.models import SubAgentBoardOptions
from ..subagents.services.base import CreateRunParams, _extract_write_dirs
from ..tools import BaseTool, ToolExecutionResult
from .coordinator_seed_tools import explicit_root_allowed_tools
from .hierarchy_tools import ScheduleChildSubagentsTool
from .orchestration_board_payload import (
    board_actionable_run_ids,
    board_status_filter,
    clip_board_text,
)
from .orchestration_dispatch_tool import DispatchSubagentsTool
from .orchestration_root_contract import explicit_root_goal_with_user_contract
from .orchestration_tool_specs import (
    build_create_subagents_spec,
    build_subagent_board_spec,
)
from .orchestration_workflow_mode import tool_workflow_mode as _tool_workflow_mode
from .orchestration_write_guard import external_write_target_error
from .parameters import _positive_int, _string_list
from .spawn_role_seed import is_explicit_root_role

if TYPE_CHECKING:
    from ..core import SimpleAgent


READ_ONLY_SUBAGENT_TOOLS = ["list_files", "read_file", "search_text"]
CODING_SUBAGENT_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "write_file",
    "append_file",
    "replace_in_file",
    "capability_request",
]
_CODING_TOOL_PRESETS = {"coding", "frontend-dev", "frontend", "web", "web-dev", "file-edit", "edit"}
_VAGUE_PRODUCT_TARGET_WORDS = (
    "目标目录",
    "同一目录",
    "当前目录",
    "任务目录",
    "产物目录",
    "输出目录",
    "build 目录",
    "build目录",
    "deliverables 目录",
    "deliverables目录",
    "target directory",
    "same directory",
    "current directory",
    "task directory",
    "output directory",
)
_CONCRETE_FILE_TARGET_RE = re.compile(
    r"[\w.-]+\.(?:html|css|js|mjs|cjs|ts|tsx|jsx|py|md|json|yaml|yml|txt|csv|vue|svelte)\b",
    re.IGNORECASE,
)
_NON_WORKER_ROLES = {
    "acceptor",
    "bug_finder",
    "coordinator",
    "critic",
    "qa",
    "reviewer",
    "root",
    "tester",
    "verifier",
}


# LLM: _subagent_allowed_tools 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理子代理allowed工具相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _subagent_allowed_tools(params: dict[str, object]) -> list[str] | None:
    allowed_tools = _string_list(params.get("allowed_tools"))
    preset = str(params.get("tool_preset") or "").strip().lower()
    preset_tools = _preset_allowed_tools(preset) if preset else None
    if allowed_tools:
        if preset_tools:
            return list(dict.fromkeys([*allowed_tools, *preset_tools]))
        return allowed_tools
    if "tool_preset" not in params:
        return None
    return _preset_allowed_tools(preset or "read_only")


# LLM: _preset_allowed_tools maps semantic task presets to minimum tool grants.
# 函数用途: 把 frontend-dev/coding 等模型常用预设转成稳定工具包；未知预设回退给角色模板自动判断。
def _preset_allowed_tools(preset: str) -> list[str] | None:
    if preset in _CODING_TOOL_PRESETS:
        return list(CODING_SUBAGENT_TOOLS)
    if preset == "read_only":
        return list(READ_ONLY_SUBAGENT_TOOLS)
    if preset == "none":
        return []
    return None


# LLM: _create_run_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建参数所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _create_run_params(
    agent,
    raw_params: dict[str, object],
    goal: str,
    allowed_tools: list[str] | None,
):
    workflow_mode = _tool_workflow_mode(raw_params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    role = _role_from_create_intent(raw_params, goal, agent)
    is_explicit_root = is_explicit_root_role(role)
    if is_explicit_root:
        workflow_mode = "off"
        allowed_tools = explicit_root_allowed_tools(allowed_tools)
        goal = explicit_root_goal_with_user_contract(agent, goal)
    elif _should_disable_generic_workflow_for_concrete_worker(raw_params, goal, role, workflow_mode):
        workflow_mode = "off"
    extra_write_roots = _merged_extra_write_roots(raw_params, goal)
    return CreateRunParams(
        goal=goal,
        thought=str(raw_params.get("thought") or "根据父代理派工执行，并保留可验收证据。").strip(),
        plan=_string_list(raw_params.get("plan")) or ["理解目标", "执行任务", "产出证据", "等待父代理验收"],
        agent_name=_root_agent_name(raw_params, role),
        role=role,
        allowed_tools=allowed_tools,
        owner=str(raw_params.get("owner") or "").strip(),
        supervisor=str(raw_params.get("supervisor") or "parent").strip(),
        final_owner=str(raw_params.get("final_owner") or "").strip(),
        acceptance_checks=_string_list(raw_params.get("acceptance_checks")),
        extra_write_roots=extra_write_roots,
        workflow_mode=workflow_mode,
    )


# LLM: _role_from_create_intent repairs obvious structured-argument slips before workflow expansion.
# 函数用途: 当模型把“创建 root/coordinator 并继续派下一层”的任务误填为 worker 时，按目标意图纠偏，避免自动 workflow 额外造孩子。
def _role_from_create_intent(raw_params: dict[str, object], goal: str, agent) -> str:
    role = str(raw_params.get("role") or "worker").strip() or "worker"
    if _role_field_is_lineage_agent_name(role):
        return _role_from_lineage_agent_name(role)
    if is_explicit_root_role(role):
        return role
    if _has_coordinator_seed_intent(raw_params, goal, agent):
        return "coordinator"
    if _has_user_style_delegation_intent(raw_params, goal, agent):
        return "coordinator"
    return role


# LLM: _should_disable_generic_workflow_for_concrete_worker prevents simple deliverable workers from growing workflow children.
# 函数用途: 当模型已创建普通 worker 且目标是明确文件交付时，关闭自动 producer/critic/repair 展开；质量闭环交给父级按实际完成状态再派 QA/验收。
def _should_disable_generic_workflow_for_concrete_worker(
    raw_params: dict[str, object],
    goal: str,
    role: str,
    workflow_mode: str,
) -> bool:
    if workflow_mode != "auto":
        return False
    if not _role_allows_direct_product_work(role):
        return False
    if _positive_int(raw_params.get("count"), default=1) <= 0:
        return False
    return _goal_has_concrete_file_target(goal)


# LLM: _role_allows_direct_product_work keeps coordinator/tester/acceptor semantics out of worker-only workflow repair.
# 函数用途: 判断当前 role 是否是会直接产出业务文件的普通 worker 类角色；找错、测试、验收和协调角色不走这个分支。
def _role_allows_direct_product_work(role: str) -> bool:
    normalized = str(role or "worker").strip().lower().replace("-", "_")
    if not normalized:
        return True
    if normalized in _NON_WORKER_ROLES:
        return False
    return not any(part in normalized for part in _NON_WORKER_ROLES)


# LLM: _goal_has_concrete_file_target treats named files as direct deliverables rather than workflow containers.
# 函数用途: 识别 index.html、report.md 这类明确文件目标；命中后 worker 应直接完成文件，不先制造通用子流程。
def _goal_has_concrete_file_target(goal: str) -> bool:
    return bool(_CONCRETE_FILE_TARGET_RE.search(str(goal or "")))


# LLM: _goal_has_single_concrete_file_target prevents broad user delegation hints from changing a one-file worker into a coordinator.
# 函数用途: 判断当前 create 调用是否只交付一个明确文件；这种小任务保持 worker，让父级是否派 QA/验收后置决定。
def _goal_has_single_concrete_file_target(goal: str) -> bool:
    return len(set(_CONCRETE_FILE_TARGET_RE.findall(str(goal or "")))) == 1


# LLM: _has_coordinator_seed_intent keeps repair narrow: coordinator marker plus child-spawn intent.
# 函数用途: 只在参数自身明显表达“协调者/继续派工”时修 role；普通 worker 不能仅因用户大任务提到层级就被改成 coordinator。
def _has_coordinator_seed_intent(raw_params: dict[str, object], goal: str, agent) -> bool:
    local_text = _local_create_intent_text(raw_params, goal)
    if not _contains_any(local_text, _COORDINATOR_SEED_MARKERS):
        return False
    if _contains_any(local_text, _CHILD_SPAWN_INTENT_MARKERS):
        return True
    user_text = str(getattr(agent, "_current_user_prompt", "") or "")
    return _contains_any(local_text + "\n" + user_text, _HIERARCHY_CONTRACT_MARKERS)


# LLM: _has_user_style_delegation_intent catches natural "小傻妞找小小傻妞" prompts without API names.
# 函数用途: 用户用自然语言要求第一层可继续派下一层时，把顶层 worker 纠成 coordinator，避免自动通用 workflow 残留空子任务。
def _has_user_style_delegation_intent(raw_params: dict[str, object], goal: str, agent) -> bool:
    user_text = str(getattr(agent, "_current_user_prompt", "") or "").lower()
    if not _contains_any(user_text, _USER_STYLE_DELEGATION_MARKERS):
        return False
    if _goal_has_single_concrete_file_target(goal):
        return False
    local_text = _local_create_intent_text(raw_params, goal)
    explicit_count = _positive_int(raw_params.get("count"), default=1)
    return explicit_count == 1 and _contains_any(user_text + "\n" + local_text, _USER_STYLE_TOP_AGENT_MARKERS)


# LLM: _local_create_intent_text extracts only the current tool call, not the whole conversation.
# 函数用途: 收集 create_subagents 本次参数里的 goal、name、thought、plan、工具等字段，作为角色纠偏证据。
def _local_create_intent_text(raw_params: dict[str, object], goal: str) -> str:
    parts = [
        goal,
        str(raw_params.get("agent_name") or ""),
        str(raw_params.get("thought") or ""),
        " ".join(_string_list(raw_params.get("plan"))),
        " ".join(_string_list(raw_params.get("allowed_tools"))),
    ]
    return "\n".join(part for part in parts if part).lower()


# LLM: _contains_any centralizes small multilingual marker checks for role intent repair.
# 函数用途: 检查文本中是否出现任一标记；只服务 create_subagents 的保守纠偏逻辑。
def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


# LLM: _role_field_is_lineage_agent_name catches display names that leaked into structured role.
# 函数用途: 判断模型是否把“小傻妞-xxx”这类代理名字误填进 role 字段；role 应保持模板 id。
def _role_field_is_lineage_agent_name(role: str) -> bool:
    text = str(role or "").strip()
    return "小傻妞" in text


# LLM: _role_from_lineage_agent_name maps leaked display names back to role-template ids.
# 函数用途: 把带层级前缀的代理名字还原成标准 role，避免任务状态里出现不可复用的动态 role。
def _role_from_lineage_agent_name(role: str) -> str:
    text = str(role or "").strip().lower()
    if "coordinator" in text or "协调" in text:
        return "coordinator"
    if "tester" in text or "测试" in text:
        return "tester"
    if "accept" in text or "验收" in text:
        return "acceptor"
    if "critic" in text or "bug" in text or "找茬" in text:
        return "bug_finder"
    return "worker"


# LLM: _root_agent_name gives top-level spawned agents the same lineage naming contract as descendants.
# 函数用途: create_subagents 未传 agent_name 时，用“小傻妞-role”兜底，避免真实测试落成 general。
def _root_agent_name(raw_params: dict[str, object], role: str) -> str:
    explicit = str(raw_params.get("agent_name") or "").strip().strip("-")
    if explicit:
        return explicit
    role_name = _agent_name_from_role_field(raw_params)
    if role_name:
        return role_name
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child"}:
        suffix = "worker"
    return f"小傻妞-{suffix}"


# LLM: _agent_name_from_role_field preserves user-facing lineage names when the model used role wrongly.
# 函数用途: 如果 role 字段里其实是“小傻妞-xxx”显示名，就转存为 agent_name，避免再次加前缀。
def _agent_name_from_role_field(raw_params: dict[str, object]) -> str:
    role_text = str(raw_params.get("role") or "").strip().strip("-")
    if not _role_field_is_lineage_agent_name(role_text):
        return ""
    return role_text.replace("_", "-")


_COORDINATOR_SEED_MARKERS = (
    "coordinator",
    "coordination",
    "root",
    "lead",
    "协调",
    "统筹",
    "小傻妞-root",
)
_CHILD_SPAWN_INTENT_MARKERS = (
    "schedule_child_subagents",
    "dispatch_subagents",
    "create child",
    "spawn child",
    "children must be created",
    "创建下一层",
    "继续创建",
    "派下一层",
    "派发下一层",
    "下级",
    "下层",
    "孩子",
    "子代理",
    "不能自己写最终产物",
    "不要写最终产物",
)
_HIERARCHY_CONTRACT_MARKERS = (
    "主代理 -> 小傻妞",
    "root -> 子",
    "depth=1",
    "小小傻妞",
    "child-coordinator",
    "child_coordinator",
)
_USER_STYLE_DELEGATION_MARKERS = (
    "小傻妞再找小小傻妞",
    "小傻妞再派小小傻妞",
    "小傻妞派小小傻妞",
    "子代理再派",
    "子代理派下一层",
    "下一层子代理",
    "下一级子代理",
)
_USER_STYLE_TOP_AGENT_MARKERS = (
    "派小傻妞",
    "让小傻妞",
    "不要你自己亲自写",
    "不要自己亲自写",
    "不要你亲自写",
    "小小傻妞",
)


# LLM: _merged_extra_write_roots 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 更新mergedextrawriteroots对应的任务或运行状态，并保留既有字段语义；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _merged_extra_write_roots(params: dict[str, object], goal: str) -> list[str]:
    roots: list[str] = []
    for item in [*_string_list(params.get("extra_write_roots")), *_extract_write_dirs(goal)]:
        text = str(item or "").strip()
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: explicit_root_missing_write_root_error prevents product paths from drifting into agent workspaces.
# 函数用途: 显式 root/coordinator 要交付文件但没带产物写入根时拒绝创建，要求模型带 extra_write_roots 重试。
def explicit_root_missing_write_root_error(params: dict[str, object], goal: str) -> str:
    role = str(params.get("role") or "worker").strip()
    if _merged_extra_write_roots(params, goal):
        return ""
    if not _goal_needs_product_write_root(goal):
        return ""
    if not is_explicit_root_role(role) and not _goal_has_vague_product_target(goal):
        return ""
    return (
        "要交付文件或网站时，必须提供真实产物写入根，"
        "否则下级会误把 agent-run workspace 当成 build 目录。"
        "请重新调用 create_subagents，并在顶层传入 extra_write_roots，"
        "例如 extra_write_roots=[\"/Users/.../deliverables/.../build\"]；"
        "不要只在 goal 里写“目标目录”“同一目录”或“build 目录”。"
    )


# LLM: _goal_needs_product_write_root detects concrete deliverable tasks without parsing prose too broadly.
# 函数用途: 判断目标是否像文件/网站交付任务；只用于缺写入根时的保守拦截，不用于授权。
def _goal_needs_product_write_root(goal: str) -> bool:
    lowered = goal.lower()
    if not any(word in lowered for word in ("交付", "deliver", "build", "网站", "demo", "文件")):
        return False
    return any(suffix in lowered for suffix in (".html", ".css", ".js", ".py", ".md", ".json", ".txt"))


# LLM: _goal_has_vague_product_target blocks path drift before a worker silently writes into task_dir.
# 函数用途: 识别“目标目录/任务目录”等模糊产物位置；没有 extra_write_roots 时要求模型重试并带真实目录。
def _goal_has_vague_product_target(goal: str) -> bool:
    lowered = goal.lower()
    return any(word in lowered for word in _VAGUE_PRODUCT_TARGET_WORDS)


# LLM: _ambiguous_repeated_product_goal_error rejects cloned workers for the same concrete deliverable target.
# 函数用途: 防止 count=2 复制同一个 index1/index2 多文件 goal，导致多个 worker 抢同一批产物。
def _ambiguous_repeated_product_goal_error(goal: str, count: int, role: str) -> str:
    if count <= 1 or not _role_allows_direct_product_work(role):
        return ""
    file_targets = sorted(set(_CONCRETE_FILE_TARGET_RE.findall(str(goal or ""))))
    if not file_targets:
        return ""
    files_text = ", ".join(file_targets[:6])
    return (
        "ambiguous_repeated_product_goal: 不要用 count 复制同一个带具体文件名的交付任务。"
        f"本次 goal 提到了 {files_text}，count={count} 会让多个 worker 抢同一批文件。"
        "请改成二选一：1) 创建 count=1 的 coordinator，让它按文件继续拆给下一层；"
        "2) 多次调用 create_subagents，每次只给一个 worker 一个明确文件目标。"
    )


# LLM: _delegation_constraint_conflict_error keeps child goals from weakening explicit user constraints.
# 函数用途: 主代理派工时如果把“不要失灵/不要失效/不要注释”反向改写，直接拒绝创建任务并要求重写派工目标。
def _delegation_constraint_conflict_error(agent, goal: str) -> str:
    user_text = str(getattr(agent, "_current_user_prompt", "") or "")
    goal_text = str(goal or "")
    conflicts: list[str] = []
    if _user_requires_working_buttons(user_text) and _goal_allows_dead_buttons(goal_text):
        conflicts.append("用户要求不要有失灵按钮，但子任务目标允许按钮指向 #。")
    if _user_requires_no_broken_images(user_text) and _goal_requires_unverified_remote_images(goal_text):
        conflicts.append("用户要求不要出现失效图片链接，但子任务目标要求使用未验证的远程图片 URL。")
    if _user_requires_no_comments(user_text) and _goal_requests_comments(goal_text):
        conflicts.append("用户要求不要注释，但子任务目标要求写注释。")
    if not conflicts:
        return ""
    return (
        "delegation_constraint_conflict: 派工目标不能削弱或反向改写用户原始约束。"
        + " ".join(conflicts)
        + "请重新调用 create_subagents：保留用户约束原文，删除冲突要求；"
        "图片可用 CSS/本地/内联视觉替代，按钮必须执行真实交互或跳到页面内真实锚点。"
    )


# LLM: _user_requires_working_buttons detects the natural-language no-dead-buttons contract.
# 函数用途: 识别用户不希望 href=#、空按钮或假交互的约束。
def _user_requires_working_buttons(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("不要有失灵按钮", "不要失灵按钮", "no broken buttons", "no dead buttons"))


# LLM: _goal_allows_dead_buttons catches common weakening phrases models add during delegation.
# 函数用途: 判断派工目标是否允许 # 空链接或假按钮。
def _goal_allows_dead_buttons(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in ('href="#"', "指向 #", "指向#", "可指向 #", "#锚点", "# 锚点", "空锚点", "hash anchor", "can point to #")
    )


# LLM: _user_requires_no_broken_images detects image reliability constraints in plain language.
# 函数用途: 识别用户要求图片不要失效、不要坏链的约束。
def _user_requires_no_broken_images(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("不要出现失效图片", "不要有失效图片", "no broken image"))


# LLM: _goal_requires_unverified_remote_images treats remote image mandates as risky unless the user asked for them.
# 函数用途: 子任务目标主动要求 Unsplash/远程图片 URL 时，如果用户要求不失效图片，就拒绝这类弱化约束。
def _goal_requires_unverified_remote_images(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("unsplash", "images.unsplash", "图片 url", "image url", "http"))


# LLM: _user_requires_no_comments detects simple no-comment deliverable requests.
# 函数用途: 识别用户明确不要注释的交付约束。
def _user_requires_no_comments(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("不要注释", "不要有注释", "no comments"))


# LLM: _goal_requests_comments catches delegated tasks that reintroduce comments.
# 函数用途: 判断派工目标是否要求代码注释或注释说明。
def _goal_requests_comments(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("有注释", "写注释", "代码注释", "with comments"))


# LLM: CreateSubagentsTool 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 提供create子代理工具模型工具入口，把结构化参数转为子代理操作；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class CreateSubagentsTool(BaseTool):

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_create_subagents_spec()

    # LLM: execute 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进execute的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        if not self.agent.config.enable_subagents:
            return ToolExecutionResult("create_subagents", False, "配置已禁用 subagent。")

        goal = str(params.get("goal") or "").strip()
        if not goal:
            return ToolExecutionResult("create_subagents", False, "缺少必填参数 goal。")

        count = self._requested_count(params)
        if isinstance(count, ToolExecutionResult):
            return count

        allowed_tools = _subagent_allowed_tools(params)
        missing_write_root = explicit_root_missing_write_root_error(params, goal)
        if missing_write_root:
            return ToolExecutionResult("create_subagents", False, missing_write_root)
        target_error = external_write_target_error(
            self.agent,
            goal,
            allowed_tools or CODING_SUBAGENT_TOOLS,
        )
        if target_error:
            return ToolExecutionResult("create_subagents", False, target_error)
        constraint_conflict = _delegation_constraint_conflict_error(self.agent, goal)
        if constraint_conflict:
            return ToolExecutionResult("create_subagents", False, constraint_conflict)

        run_params = _create_run_params(self.agent, params, goal, allowed_tools)
        ambiguous_product_count = _ambiguous_repeated_product_goal_error(goal, count, run_params.role)
        if ambiguous_product_count:
            return ToolExecutionResult("create_subagents", False, ambiguous_product_count)
        tasks = self._create_tasks(goal, count, run_params)
        payload = self._create_payload(tasks, allowed_tools)
        return ToolExecutionResult(
            "create_subagents",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    # LLM: _requested_count 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 发送requested数量请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _requested_count(self, params: dict[str, object]) -> int | ToolExecutionResult:
        count = _positive_int(params.get("count"), default=1)
        if count <= 0:
            return ToolExecutionResult("create_subagents", False, "count 必须大于 0。")
        if self.agent.config.max_subagents > 0:
            count = min(count, self.agent.config.max_subagents)
        return count

    # LLM: _create_tasks 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建tasks所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _create_tasks(self, goal: str, count: int, run_params: CreateRunParams):
        tasks = []
        for index in range(1, count + 1):
            task_goal = run_params.goal if count == 1 else f"{run_params.goal} / 子任务{index}"
            task_params = CreateRunParams(**{**run_params.__dict__, "goal": task_goal})
            task = self.agent.subagents.create_run(
                params=task_params,
            )
            tasks.append(task)
        return tasks

    # LLM: _create_payload 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建载荷所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _create_payload(self, tasks, allowed_tools: list[str] | None) -> dict[str, object]:
        payload: dict[str, object] = {
            "created": len(tasks),
            "ids": [task.id for task in tasks],
            "allowed_tools": allowed_tools or "automatic",
            "subagent_workspace": str(self.agent.subagents.workspace),
            "tasks": [
                {
                    "id": task.id,
                    "goal": task.goal,
                    "status": task.status,
                    "verification_status": task.verification_status,
                    "task_dir": task.task_dir,
                }
                for task in tasks
            ],
        }
        payload["typed_envelope"] = subagent_schedule_envelope_from_payload(
            payload,
            tool="create_subagents",
        ).to_dict()
        return payload


# LLM: SubagentBoardTool 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 提供子代理看板工具模型工具入口，把结构化参数转为子代理操作；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class SubagentBoardTool(BaseTool):

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_subagent_board_spec()

    # LLM: execute 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进execute的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        limit = _positive_int(params.get("limit"), default=10)
        status_filter = board_status_filter(params.get("status"))
        board = self.agent.subagents.write_board(
            options=SubAgentBoardOptions(recent_limit=max(1, limit)),
        )
        items = board.items
        if status_filter:
            items = [item for item in items if item.status.upper() == status_filter]
        items = items[:limit]
        payload = {
            "summary": board.summary,
            "returned": len(items),
            "actionable_run_ids": board_actionable_run_ids(items),
            "subagent_workspace": str(self.agent.subagents.workspace),
            "items": [
                {
                    "id": item.id,
                    "root_id": str(getattr(item, "root_id", "") or ""),
                    "parent_id": str(getattr(item, "parent_id", "") or ""),
                    "depth": int(getattr(item, "depth", 0) or 0),
                    "agent_name": str(getattr(item, "agent_name", "") or ""),
                    "role": str(getattr(item, "role", "") or ""),
                    "goal": clip_board_text(item.goal),
                    "status": item.status,
                    "verification_status": item.verification_status,
                    "channel_status": item.channel_status,
                    "risk_flags": item.risk_flags,
                    "evidence_count": item.evidence_count,
                    "child_count": int(getattr(item, "child_count", 0) or 0),
                    "child_status_counts": dict(getattr(item, "child_status_counts", {}) or {}),
                    "open_request_count": item.open_request_count,
                    "open_gap_count": item.open_gap_count,
                    "latest_summary": clip_board_text(str(getattr(item, "latest_summary", "") or ""), limit=180),
                    "blocker_count": int(getattr(item, "blocker_count", 0) or 0),
                    "task_dir": item.task_dir,
                    "output_json": str(getattr(item, "output_json", "") or ""),
                }
                for item in items
            ],
            "board_json": str(self.agent.subagents.workspace / "subagent_board.json"),
            "board_md": str(self.agent.subagents.workspace / "SUBAGENT_BOARD.md"),
        }
        return ToolExecutionResult("subagent_board", True, json.dumps(payload, ensure_ascii=False, indent=2))
