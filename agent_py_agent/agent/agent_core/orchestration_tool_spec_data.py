# LLM: Orchestration tool-spec data only; keep descriptions generic and not task-specific.
# 模块用途: 存放 create/dispatch/watch 等 orchestration 工具说明文本和参数字典。

from __future__ import annotations

_CREATE_USE_CASES = [
    "用户要求拆分任务、派多个子代理、开工单或让子代理分别处理事项",
    "需要把聊天里的计划落盘并立即让子代理开始工作",
    "材料很多且用户要求派工时，先读 README/目标/评分/目录等最小必要信息，再用 items 派小傻妞分别读取和分析正文",
]
_CREATE_KEYWORDS = ["子代理", "派工", "拆分", "工单", "任务", "subagent", "delegate", "spawn", "assign"]
_CREATE_PARAMETERS = {
    "goal": "单任务模式的总目标或任务描述；如果传 items，可省略",
    "items": "批量模式：独立子任务对象列表，每项必须有 goal，可单独写 role/agent_name/plan/acceptance_checks",
    "count": "单任务模式创建多少个同目标子代理，默认 1，受 max_subagents 限制；不同切片请用 items",
    "role": "子代理角色模板 id；默认 worker",
    "tool_preset": "默认 automatic；显式 read_only/coding/none 时才覆盖自动工具策略",
    "allowed_tools": "工具偏好提示；一般省略。系统会自动补齐基础读写工具，模型少填工具不能把子代理变成无写入能力。",
    "acceptance_checks": "验收标准列表",
    "plan": "每个子代理的初始步骤列表",
    "input_refs": "子代理需要读取的资料、文件、URL 或 artifact refs；批量 items 里建议写在对应 item 上",
    "context_manifest": "refs-first 上下文清单；可放 input refs/task_pack_refs/omitted_context",
    "context_packs": "refs-only 上下文包列表；每项只放摘要和 path/ref，不放大正文",
    "output_files": "子代理必须写出的目标文件路径列表；知道文件名时必须填，系统会把它写入机器合同",
    "output_refs": "output_files 的语义别名，用于引用交付物路径或产物 ref",
    "artifact_refs": "交付物 refs 列表；适合引用已经存在或后续要验收的产物",
    "replacement_for_run_ids": "可选：新子代理要接管/替换哪些旧 run_id；系统会把旧 run 标记为已被接管，避免父代理继续把旧 run 当活跃任务推进",
    "workflow_mode": "off/plan/auto；决定是否在建工单时挂 workflow 计划",
    "defer_start": "默认 false。可写在顶层或单个 item 上；顶层 true 表示整批只建不跑，item true 表示只挂起这个子代理",
}
_CREATE_PARAMETER_DETAILS = {
    "goal": (
        "写清楚子代理要交付什么，不要只写一个空泛标题。"
        "必须保留用户原始硬约束，不得反向改写：例如用户说不要失灵按钮，就不能写“按钮可指向 #”或“可以用 #锚点”；"
        "用户说不要失效图片，就不要擅自要求远程图片 URL。"
    ),
    "count": (
        "例如 3 表示创建 3 个同目标并列子任务。不同工作切片不要用 count 复制同一个 goal；"
        "优先传 items，每项写独立 goal/agent_name；只有确实需要多个同质 worker 时才用 count。"
    ),
    "items": (
        "推荐批量入口："
        "[{\"goal\":\"研究市场\",\"role\":\"worker\",\"agent_name\":\"小傻妞-市场\"},"
        "{\"goal\":\"研究竞争\",\"role\":\"worker\",\"agent_name\":\"小傻妞-竞争\"}]。"
        "create_subagents 默认创建后立刻启动子代理；只有传 defer_start=true 才只建任务记录。"
        "如果任务材料很多，不要由 root 先读完所有正文再派工；root 只读最小必要信息，"
        "把具体正文、数据表和长报告的读取分析写进各 item 的 goal 或 item.input_refs。"
        "例如“子代理1读文件1、子代理2读文件2”时，把文件路径分别写进各自 item，"
        "不要放到顶层。"
    ),
    "role": "优先用模板角色，而不是临时造小角色。可用角色模板索引：\n{role_template_index}",
    "tool_preset": "省略时自动：由 role template、任务目标和调度器决定工具；角色模板默认保留基础读写/汇报能力。`none` 只表示不覆盖自动策略。",
    "allowed_tools": "一般省略。若模型写了 [\"read_file\"] 这类不完整列表，系统仍会补齐基础读写工具；不要把它当安全限制。",
    "acceptance_checks": "JSON 数组或多行文本，说明父代理后续怎样判断任务完成。",
    "plan": "JSON 数组或多行文本，给子代理的初始执行步骤。",
    "input_refs": (
        "输入资料线索，例如 [\"README.md\",\"data/market.md\"] 或 artifact ref。"
        "这不是 root 要立刻读取的清单，也不是 runner 启动硬门，而是交给对应小傻妞读取和分析的线索。"
        "批量模式下，顶层 input_refs 只表示每个子代理都需要的公共资料；"
        "单个子代理自己的输入文件要放在对应 item.input_refs。"
    ),
    "context_manifest": (
        "结构化资料索引，用来放 input refs、task pack refs 和 omitted context。"
        "这些引用会出现在子代理 runner prompt 的 Context Manifest 中，子代理自己读正文；"
        "root 不必先把所有材料正文读进自己的上下文。"
    ),
    "context_packs": (
        "小型上下文包索引，例如 [{\"kind\":\"brief\",\"summary\":\"评分标准\",\"path\":\"rubric.md\"}]。"
        "只放摘要和路径，不放长正文。它不是实时共享内存或消息队列，不要用空的 shared_context、"
        "previous_discoveries 之类字段假装以后会自动同步；需要其他代理协助时，让发现者在运行时使用 "
        "raise_collaboration，响应者用 inspect_collaboration/submit_collaboration_result。"
    ),
    "output_files": (
        "只要用户给了明确保存路径，就把路径放进这里，例如 [\"outputs/page/index.html\"]。"
        "不要只把保存路径写在 goal 里；goal 是给人看的任务描述，output_files 才是系统后续调度、验收、"
        "恢复和去重会读取的机器事实。"
    ),
    "output_refs": "同 output_files；当上游系统已经叫它 refs 时可用这个字段，系统会统一归入 task.attributes。",
    "artifact_refs": "用于交付物已经有 ref 或需要跨任务传递的情况；普通写新文件优先用 output_files。",
    "replacement_for_run_ids": (
        "当旧子代理卡住、超时或已经被新策略接管时使用。"
        "例如新建一个修复/接管 worker，并写 replacement_for_run_ids:[\"旧run_id\"]；"
        "系统只记录结构化替换关系和旧 run 状态，不会靠自然语言猜谁替谁。"
    ),
    "workflow_mode": "默认建议省略或写 off。只有用户明确要求 workflow/工作流时才写 plan/auto；明确文件交付 worker 会强制 off。",
    "defer_start": (
        "普通生产任务默认不要传，让子代理创建后自动开始工作。"
        "测试、找错、验收、汇总这类依赖前置产物的 item 可以传 true；"
        "等产物 refs 出现后，父代理再用 dispatch_subagents 显式启动这些 run_id。"
    ),
}
_CREATE_EXAMPLES = [
    (
        '{"tool":"create_subagents","items":[{"goal":"用单文件 HTML 完成用户指定页面",'
        '"role":"worker","agent_name":"小傻妞-页面",'
        '"output_files":["outputs/page/index.html"]}]}'
    ),
    (
        '{"tool":"create_subagents","items":['
        '{"goal":"读取资料 A 并输出证据摘要","role":"worker","agent_name":"小傻妞-资料A"},'
        '{"goal":"读取资料 B 并输出证据摘要","role":"worker","agent_name":"小傻妞-资料B"},'
        '{"goal":"整合多个资料摘要并输出风险","role":"coordinator","agent_name":"小傻妞-整合"}],'
        '"acceptance_checks":["必须有证据","必须标注未确认信息"]}'
    ),
    (
        '{"tool":"create_subagents","items":['
        '{"goal":"读取并分析 data/a.md，输出证据摘要","role":"worker",'
        '"agent_name":"小傻妞-资料A","input_refs":["data/a.md","rubric.md"]},'
        '{"goal":"读取并分析 data/b.md，输出证据摘要","role":"worker",'
        '"agent_name":"小傻妞-资料B","input_refs":["data/b.md","rubric.md"]}]}'
    ),
    '{"tool":"create_subagents","goal":"在隔离 fixture 项目里实现三个小功能并写报告","count":3,"role":"worker","workflow_mode":"off","acceptance_checks":["必须有文件证据","必须说明测试结果"]}',
    '{"tool":"create_subagents","goal":"实现用户指定项目的 HTML 骨架和 data.json","count":1,"role":"worker","agent_name":"小傻妞-基础结构","output_files":["/workspace/deliverables/app/build/index.html","/workspace/deliverables/app/build/data.json"]}',
    '{"tool":"create_subagents","goal":"实现用户指定项目的 styles.css 和 app.js 交互","count":1,"role":"worker","agent_name":"小傻妞-样式交互","output_files":["/workspace/deliverables/app/build/styles.css","/workspace/deliverables/app/build/app.js"]}',
    '{"tool":"create_subagents","goal":"检查多个 worker 的项目实现","count":1,"role":"bug_finder"}',
]

_INSPECT_TREE_PARAMETERS = {
    "root_id": "可选：只查看某棵 root run 下面的主/子/孙代理树",
    "run_id": "可选：查看某个 run 或以它为根的子树",
    "scope": "root_tree/own_subtree/subtree/all；默认按 root_id/run_id 自动选择",
}
_OBSERVATION_PARAMETERS = {
    "thread_id": "会话线程 ID；不知道时可以传 task_id 让系统从任务绑定反查",
    "task_id": "任务 ID；thread_id 为空时用于反查会话线程，也会作为 root_task_id 兜底",
    "event_type": "事件类型，如 progress/blocker/runtime_alert/needs_review；不要写业务专项枚举",
    "summary": "结构化事实摘要，说明发生了什么、为什么需要主代理知道",
    "urgency": "urgent 会立即唤醒主代理；其他值记录为普通观察，等下一次后台 tick 处理",
    "severity": "可选严重程度，保留原始值，不作为封闭枚举硬拒",
    "source_agent_id": "上报事件的子/孙代理 run_id",
    "parent_agent_id": "上报者的父代理 run_id",
    "root_task_id": "整棵任务树的根任务 ID；省略时用 task_id",
    "evidence_refs": "证据引用列表，如工具结果、产物、日志或报告 ref",
    "requires_main_agent": "是否需要主代理做判断、调度或汇报",
    "requires_llm_report": "是否需要主代理用 LLM 生成面向用户的汇报",
    "dedupe_key": "可选幂等键，避免同一事件重复叫醒主代理",
}

_DISPATCH_PARAMETERS = {
    "dry_run": "统一预览开关；true 只预览不执行，false 真实推进 runner。模型只需要填写这一项。",
    "planner": "是否启用父代理 planner，默认 false",
    "workflow_mode": "off/plan/auto；是否在 dispatch 前补做 workflow 规划或自动派工",
    "max_runners": "本轮最多推进多少个 runner，默认 1；0 表示不执行 runner",
    "limit": "每阶段最多处理多少条记录，默认 20；0 表示不限制",
    "run_ids": "精确指定本轮要推进的 run_id 列表，按给定顺序执行；direct_children=true 表示当前作用域的直接孩子，不是 run_id 字符串。",
    "runner_instruction": "给单个 runner 的额外指令并随本次 dispatch 推进；只想补充一句话时优先用 send_guidance",
    "take_over_by": "显式指定执行接管/重挂动作的 leader run_id；用于 coordinator 挂掉后的领导权恢复",
    "locked_files": "本轮接管或重派时需要保守锁定的文件列表，避免恢复动作和仍在运行的分支互相覆盖",
}
_DISPATCH_PARAMETER_DETAILS = {
    "dry_run": (
        "推荐优先使用这个字段。dry_run=true 表示只看计划/状态，不启动 runner；"
        "dry_run=false 表示真实推进；不要再填写第二套执行开关。"
    ),
    "planner": "true 会额外调用父代理 LLM planner；适合长任务统筹，但会多消耗一次模型调用。",
    "workflow_mode": "plan 只把 workflow 计划写回父任务；auto 会在计划 OK 时落成 worker 子工单；未知值保守按 off 处理。",
    "max_runners": "用来限制本轮推进数量；顶层默认 1，runner 内部默认 6，避免父节点只推进一个孩子就超时。",
    "run_ids": "适合父 runner 用 schedule_child_subagents/create_subagents 返回的 created_run_ids/dispatch_run_ids 指定本轮孩子，例如先跑 phase-a/phase-b，再跑 phase-c/review；direct_children=true 只表示直接孩子作用域。",
    "runner_instruction": "只适合单个 run_id 且需要立刻推进的补充说明。多个不同子任务一起跑时不要写子任务专属内容；只补充提示不推进时用 send_guidance。",
    "take_over_by": (
        "只在恢复动作需要新 leader 时填写。先用 inspect_agent_tree 或 due-check 找到可接管的已有 coordinator/leader run_id，"
        "再把它传给 dispatch_subagents；runner 内未填写时默认当前父 run 接管。不要凭空编 run_id。"
    ),
    "locked_files": "JSON 数组，填写相对或绝对文件路径；用于恢复/重派时向调度器声明这些文件暂时不能被其他分支并发修改。",
}

_SCHEDULE_CHILD_USE_CASES = [
    "当前 subagent runner 需要把自己的任务继续拆给下一层子/孙代理",
    "需要保持 main -> child -> grandchild 的层级边界，而不是外层直接创建叶子节点",
]
_SCHEDULE_CHILD_KEYWORDS = [
    "下一层",
    "子节点",
    "孙代理",
    "层级",
    "hierarchy",
    "child",
    "grandchild",
]
_SCHEDULE_CHILD_PARAMETERS = {
    "children": "下一层子任务列表，每项包含 goal/role/agent_name 等字段，必填",
    "dry_run": "统一预览开关；true 只预览不创建，false 真实创建；runner 内省略时默认 false",
    "max_depth": "允许创建到的最大 depth；省略或 0 表示不限制",
    "max_children": "父节点最多能拥有多少直接 child，0 表示不限制",
}
_SCHEDULE_CHILD_PARAMETER_DETAILS = {
    "children": (
        "JSON 数组。每项可含 role、agent_name、goal、plan、allowed_tools、allowed_skills、"
        "acceptance_checks、output_files。参数必须在 tool JSON 顶层，不要再包一层二级字段；"
        "长目标请分多次调用，每次 1-2 个 child。多层领导节点可用 role=coordinator/child_coordinator/grandchild_coordinator。"
        "优先从这些角色模板索引里选 role：\n{role_template_index}"
    ),
    "dry_run": "runner 内省略时默认 false，会真实创建自己的下一层 child；显式 true 只返回会创建什么，适合先检查。",
    "max_depth": "显式正数才限制层级；普通任务建议省略，让上级按任务需要决定。",
    "max_children": "显式正数才限制直接孩子数量；普通任务建议省略或 0。",
}
_SCHEDULE_CHILD_EXAMPLES = [
    (
        '{"tool":"schedule_child_subagents","dry_run":false,'
        '"children":[{"role":"child_coordinator","agent_name":"module-lead",'
        '"goal":"继续拆分目标子模块实现任务",'
        '"allowed_tools":["schedule_child_subagents","dispatch_subagents","inspect_agent_tree","read_file"]}]}'
    ),
    (
        '{"tool":"schedule_child_subagents","dry_run":false,'
        '"children":[{"role":"bug_finder","agent_name":"qa-finder","goal":"检查多个 worker 的实现和证据"},'
        '{"role":"tester","agent_name":"qa-tester","goal":"测试关键交互流程和质量要求"}]}'
    ),
]
_SCHEDULE_CHILD_COORDINATOR_RULES = (
    "coordinator/lead 的权限应覆盖下级，便于检查、接管和救援；小任务、用户明确要求或下级卡住时也可以亲自完成。"
    "请用本工具创建 worker/writer/leaf_worker，并把父级收口条件原样传下去。"
    "如果父级要求 4 层链路，深度未到孙孙层前先创建下一层 coordinator。"
    "需要给下级补充提示时用 send_guidance 点名 run；dispatch_subagents 只负责推进、恢复或重跑。"
    "平级讨论要走允许的定向通道，不能越权通知别的分支。"
)
