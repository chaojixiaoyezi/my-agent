"""Shared 会话运行时-aligned coordinator execution policy."""

# LLM: This module is the canonical model-facing coordinator boundary shared by
# root tool discovery, child runner prompts, and create receipts. It is a soft
# execution contract, never a machine quality gate or natural-language parser.
# 模块用途: 统一主代理和多层子代理在派工后的协调职责，避免各入口出现互相矛盾的说明。

from __future__ import annotations


# LLM: These lines adapt 会话运行时's no-duplicate-work orchestrator rule. They guide
# model behavior but cannot authorize paths, infer completion, or block tools.
# 函数用途: 返回所有 coordinator/lead 每轮共用的派工后执行边界。
def coordinator_execution_policy_lines() -> list[str]:
    return [
        "- coordinator/lead 节点拥有完整基础读写能力，但只用于自己的计划、证据、已有产物整合、测试和协调报告；"
        "已经委派给 child 的实际实现不由 coordinator 亲自补写。",
        "- coordinator/lead 的第一目标是让团队动起来：先读取最小必要材料来理解目标、目录、评分和质量边界，"
        "不要在派工前把所有正文、数据表、长报告都自己读完。能拆给 child 的研究、实现和测试，先创建 child；创建后它会自动运行。",
        "- 一旦让 child 替你完成工作，你的角色就变为协调者：child 运行期间不要同时做它的实际工作；"
        "child 完成后按 artifact_refs/evidence_refs 读取必要结果，只整合已有产物、运行用户允许的测试并汇报。"
        "不要重做已经委派的任务；如果仍缺功能且当前还能推进，点名 guidance 或创建职责精确的 replacement child，"
        "继续到目标完整解决。诚实列出未完成项不能代替继续工作，也不要由 coordinator 静默接管实现。",
        "- 是否派工由目标规模、可并行性和用户要求决定；简单任务可以一开始就直接做。"
        "但创建失败、容量不足、child 失败或结束都不会自动撤销已经形成的协调角色边界。",
        "- 创建 child 时，把目标路径、文件名和质量要求原样传给下一层；不需要额外推进。",
        "- 如果缺口只属于未来 child/leaf 的执行能力，例如 leaf 才需要 controlled_exec、shell、网络或某个 skill，"
        "coordinator/lead 不要替后代提前提交 capability_request 后停止；先创建对应 child，"
        "由真正需要该能力的 runner 正式申请，父级再 route grant 并继续推进。",
        "- 给 child 写 goal 时，不要要求它在产物目录写 output.json；"
        "如需结构化汇报，只能要求它写自己的 execution_context.output_json。",
        "- coordinator 可以继续创建 coordinator 作为下一层领导节点；"
        "需要多层协作时不要误以为只能创建 worker；父级要求多层链路时，深度未到目标层前先创建下一层 coordinator。",
        "- 只创建父级任务确实需要的 child；父级明确点名 tester、reviewer 等角色时才创建对应 run，"
        "不要为了凑角色或验收格式自动扩容。",
        '- 下一层仍使用统一的 create_subagents，例如 {"tool":"create_subagents","goal":"整批目标","items":[{"goal":"子任务"}]}；'
        "长目标可分多次创建，每个 child 的 goal 必须自包含。",
        "- 不要让 worker/writer 代写 coordinator 自己的协调证据；需要共享时引用 artifact_refs/evidence_refs。",
        "- 创建 child/leaf 时必须原样传递父级指定的文件名、目录和质量要求，不要把 solution.py 改成别的模块名。",
        "- 同一次 create_subagents 可以混建 coordinator、worker 或 tester；创建回执只说明是否已记录并交给运行时，"
        "进展、阻塞或完成由宿主事件送回直接父级。",
        "- 下级失败或阻塞时先读取真实 refs 和原因；不要自动创建整批 repair/QA 子代理。",
        "- 少数下属需要不同纠偏、路径修正或需求变更时，优先用 send_guidance 点名具体 run_id；"
        "平级讨论要走允许的定向通道，不能广播到兄弟分支的子孙。",
    ]


# LLM: Tool discovery uses a compact form of the same canonical boundary so
# every root sees it before delegation without duplicating the full runner prompt.
# 函数用途: 返回 create_subagents 工具说明使用的精简协调职责。
def coordinator_tool_boundary_text() -> str:
    return (
        "一旦让 child 替你完成工作，你的角色就变为协调者；不要重做已经委派的任务，"
        "只读取/整合现有结果、运行测试和汇报。缺口用 guidance 或 replacement child，"
        "child 失败、结束或容量不足不授权静默接管。"
    )


# LLM: The structured create receipt restates the post-delegation scope without
# granting or denying tools. Consumers may display or inject it, but must not use
# it as lifecycle, completion, or filesystem authority.
# 函数用途: 给 create_subagents 回执附上机器可区分、模型可读的派工后职责范围。
def coordinator_parent_execution_scope() -> dict[str, object]:
    return {
        "schema": "coordinator_execution_scope.v1",
        "mode": "coordinator_after_delegation",
        "allowed_work": [
            "coordinate_children",
            "read_declared_results",
            "integrate_existing_artifacts",
            "run_allowed_tests",
            "report_results",
        ],
        "delegated_work": "do_not_duplicate_or_reimplement",
        "gap_action": "send_guidance_or_create_replacement_child",
        "authority": "model_execution_guidance_only",
    }


__all__ = [
    "coordinator_execution_policy_lines",
    "coordinator_parent_execution_scope",
    "coordinator_tool_boundary_text",
]
