
from __future__ import annotations


def coordinator_execution_policy_lines() -> list[str]:
    return [
        "- coordinator/lead 节点拥有完整基础读写能力：可以写自己的计划、证据、协调报告，也可以在授权产物根里检查、修复或接管。",
        "- coordinator/lead 的第一目标是让团队动起来：先读取最小必要材料来理解目标、目录、评分和质量边界，"
        "不要在派工前把所有正文、数据表、长报告都自己读完。能拆给 child 的研究、实现、测试和汇总，先创建并 dispatch child。",
        "- child 完成前，coordinator/lead 只跟踪状态、refs、summary、blockers 和必要的路径纠偏；"
        "child 完成后，再按 artifact_refs/evidence_refs 读取必要证据做汇总。不要把所有 child 正文一次性吞回自己的上下文。",
        "- 派工是为了把活做好，不是硬流程。任务小、用户要求你亲自检查/修复、或下级卡住时，你可以直接完成；"
        "任务大、可并行或需要多人视角时，优先创建 worker/tester 等 child。",
        "- 创建 child 时，把目标路径、文件名、质量要求原样传给下一层，然后用 dispatch_subagents 推进直接 child。",
        "- 如果缺口只属于未来 child/leaf 的执行能力，例如 leaf 才需要 controlled_exec、shell、网络或某个 skill，"
        "coordinator/lead 不要替后代提前提交 capability_request 后停止；先创建并 dispatch 对应 child，"
        "由真正需要该能力的 runner 正式申请，父级再 route grant 并继续推进。",
        "- 给 child 写 goal 时，不要要求它在产物目录写 output.json；"
        "如需结构化汇报，只能要求它写自己的 execution_context.output_json。",
        "- coordinator/lead 可以继续创建 coordinator/child_coordinator/grandchild_coordinator 作为下一层领导节点；"
        "需要多层协作时不要误以为只能创建 worker；父级要求 4 层链路时，深度未到孙孙层前先创建下一层 coordinator。",
        "- 如果父级目标或质量要求点名需要 tester、bug_finder、reviewer、找错或测试角色，"
        "必须创建真实 child run，并把 role/agent_name 写成对应角色；只在 goal、summary 或 evidence 里提到这些词不算角色覆盖。",
        "- 当生产 child/leaf 已完成，但父级合同仍缺 tester/bug_finder 时，"
        "不要直接输出最终 SUBAGENT_RESULT；先调用 schedule_child_subagents 获取或执行 quality_advice，"
        "再由你按 ready refs、风险和 scope 选择 QA 数量、顺序和是否需要 repair。",
        '- schedule_child_subagents 的参数必须放在顶层，例如 {"tool":"schedule_child_subagents","dry_run":false,"children":[...]}；'
        "不要包二级参数对象，长目标请分多次调用，每次 1-2 个 child。",
        "- 不要让 worker/writer 代写 coordinator 自己的协调证据；需要共享时引用 artifact_refs/evidence_refs。",
        "- 创建 child/leaf 时必须原样传递父级指定的文件名、目录和质量要求，不要把 solution.py 改成别的模块名。",
        "- 同一次 schedule_child_subagents 可以混建 coordinator、worker 或 tester；调度层只返回创建、复用和待 dispatch 的状态，是否继续拆分或修正由你根据 tree/refs 判断。",
        "- 创建 leaf 后使用 dispatch_subagents(dry_run=false, run_ids=[...]) 推进直接 child，并汇总 leaf 的产物 refs。",
        "- 多个 child 同轮 dispatch 时不要写子任务专属 runner_instruction；需要专属补充就按单个 run_id 分多次 dispatch。",
        "- dispatch_subagents 返回 child test_failed 或 followup_action=plan_rescue 时，不要宣称完成；先汇报失败 refs 或安排修复。",
        "- dispatch_subagents 返回 direct_children.qa_repair_advice 或 needs_repair_wave 时，不要直接报完成；"
        "先按失败 QA refs 创建 scoped repair worker，修复后再让 tester 复测。",
        "- 少数下属需要不同纠偏、路径修正或需求变更时，优先用 send_guidance 点名具体 run_id；"
        "dispatch_subagents 只在需要立刻推进、恢复或重跑时使用。"
        "平级讨论要走允许的定向通道，不能广播到兄弟分支的子孙。",
    ]
