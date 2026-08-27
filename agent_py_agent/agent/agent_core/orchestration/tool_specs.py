
from __future__ import annotations

from copy import deepcopy

from ...subagents.role_templates import role_template_index_text
from ...tooling.models import ToolModelHints, ToolModelSpec
from .coordinator_policy import coordinator_tool_boundary_text
from .tool_spec_data import (
    _CREATE_DEPENDENCY_ORDER_RULE,
    _CREATE_DISJOINT_WRITE_SCOPE_RULE,
    _CREATE_EXAMPLES,
    _CREATE_ITEM_PARAMETER_DETAILS,
    _CREATE_KEYWORDS,
    _CREATE_PARAMETER_DETAILS,
    _CREATE_PARAMETERS,
    _CREATE_USE_CASES,
)
from .tool_spec_schemas import (
    _CREATE_PARAMETER_SCHEMA,
    _RESOLVE_CAPABILITY_PARAMETER_SCHEMA,
)


def _input_schema(
    parameters: dict[str, str],
    shapes: dict[str, object],
    *,
    details: dict[str, str] | None = None,
    required: tuple[str, ...] = (),
) -> dict[str, object]:
    descriptions = {**parameters, **(details or {})}
    properties: dict[str, object] = {}
    for name, description in descriptions.items():
        raw_shape = shapes.get(name, {"type": "string"})
        shape = deepcopy(raw_shape) if isinstance(raw_shape, dict) else {"type": "string"}
        shape["description"] = description
        properties[name] = shape
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _hints(
    *,
    use_cases: list[str] | tuple[str, ...] = (),
    avoid_when: list[str] | tuple[str, ...] = (),
    keywords: list[str] | tuple[str, ...] = (),
    examples: list[str] | tuple[str, ...] = (),
) -> ToolModelHints:
    return ToolModelHints(
        category="orchestration",
        use_cases=tuple(use_cases),
        avoid_when=tuple(avoid_when),
        keywords=tuple(keywords),
        examples=tuple(examples),
    )


# LLM: Native nested item properties need the same model-facing descriptions as
# top-level fields; otherwise providers see only bare types and often copy one
# batch description into every child. This helper changes schema guidance only.
# 函数用途: 为 create_subagents 的每个 items[] 字段补齐说明，让模型逐项填写职责短标题。
def _create_subagents_input_schema() -> dict[str, object]:
    details = _with_role_template_index(_CREATE_PARAMETER_DETAILS)
    schema = _input_schema(
        _CREATE_PARAMETERS,
        _CREATE_PARAMETER_SCHEMA,
        details=details,
        required=(),
    )
    properties = schema.get("properties")
    items_shape = properties.get("items") if isinstance(properties, dict) else None
    item_shape = items_shape.get("items") if isinstance(items_shape, dict) else None
    item_properties = item_shape.get("properties") if isinstance(item_shape, dict) else None
    descriptions = {
        **_CREATE_PARAMETERS,
        **details,
        **_CREATE_ITEM_PARAMETER_DETAILS,
    }
    if isinstance(item_properties, dict):
        for name, shape in item_properties.items():
            if isinstance(shape, dict) and name in descriptions:
                shape["description"] = descriptions[name]
    return schema


# LLM: 这是递归创建唯一模型合同；单派 goal 与批量 items 由运行时做 one-of
# 校验，items 每项 goal 仍必填；保持自动启动和事件回传，不重加旧控制参数。
# 函数用途: 构造 create_subagents 给模型看的说明和 JSON Schema。
def build_create_subagents_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="create_subagents",
        description=(
            "把可并行的独立工作交给下级代理。无论当前是主代理、子代理还是孙代理，都使用同一个 "
            "create_subagents；创建成功后下级立即运行，进展、阻塞或完成时系统自动唤醒直接父级，不需要也没有"
            "查询或推进工具。只派一个时传非空 goal；需要多个时传 items，每项都要有独立 goal，顶层 goal "
            "只是可选批次说明。goal 与 items 都没有时会返回可恢复参数错误。"
            + _CREATE_DEPENDENCY_ORDER_RULE
            + _CREATE_DISJOINT_WRITE_SCOPE_RULE
            + "不支持 operations、count 或 max_concurrency 参数。covers 是可选的 "
            "task_progress exact-id 映射：只有 child 与仍 open 项确实是同一工作时才填，提供的未知、已关闭或跨 "
            "item 重复 id 会整批拒绝；省略时 child 按真实 run_id 单独显示，不会给现有 Todo 打勾。返工已关闭项先用 "
            "task_progress 对原 id 传 status=in_progress, correction=true，再绑定原 id；绝不能拿无关 open id 顶替。"
            "output_files 也是可选交付/协调提示，不是权限、完整写集或机器锁；可以省略，同批 child "
            "可以共享父级 task root，但所有路径仍必须位于当前 workspace。真正会改同一文件或模块时，"
            "仍要在 goal 中缩窄独占范围或分批。"
            "普通 child 自动继承父级工作区权限；goal、output_files 和 capability grant 都不能扩到兄弟"
            "目录。不要为了显得忙而派，也不要重复创建同一任务。派工后的统一职责边界："
            + coordinator_tool_boundary_text()
        ),
        input_schema=_create_subagents_input_schema(),
        hints=_hints(
            use_cases=_CREATE_USE_CASES,
            avoid_when=(
                "只是解释思路、不需要真正创建任务时，不要调用；先直接回答即可",
                "单步机械活或一两次工具调用就能完成的简单任务,自己直接做、别拆",
                "别把整个目标原样转给单个子代理(无谓套娃,没真正切分就没价值)",
                "查看已派下级时不要再创建查询代理；宿主会把生命周期事件送回直接父级",
                "查看、发消息或打断已有下级时不要再创建一个新代理",
            ),
            keywords=_CREATE_KEYWORDS,
            examples=_CREATE_EXAMPLES,
        ),
    )


# LLM: Task progress is a soft self-check ledger; child summaries may enrich
# lifecycle events but must not imply a parent-side inspection requirement.
# 函数用途: 构造当前代理记录/读取软进度清单的模型合同。
def build_task_progress_model_spec() -> ToolModelSpec:
    progress_item_schema = _task_progress_item_schema()
    coverage_target_schema = _task_progress_coverage_target_schema()
    parameters = {
        "action": "read/update/create；不填默认 read。create 与 update 等效（账本不存在时自动创建，首次建清单也用 create 或 update），清单内容不会自动续跑普通任务，也不会阻止模型结束当前轮。",
        "run_id": "仅用于 read 时查看另一个明确的历史运行；读取当前任务请省略。即使误传当前 task_id，系统也会归一到当前唯一账本；update 始终写当前运行自己的账本。",
        "summary": "可选。当前整体进展一句话",
        "next_action": "可选。下一步最应该做什么",
        "items": "可选。进度项列表，每项可含 id/title/status/evidence/notes/next",
        "coverage": "可选。覆盖账本，含 goal/dimensions/targets；targets 每项使用 id/title/status/checks/evidence/notes/next。",
    }
    property_schemas = {
        # S-C1 延伸：MiniMax 自然习惯用 action=create 建清单；create 在工具层
        # 归一为 update（账本不存在时自动创建），这里显式列入枚举避免模型盲试。
        "action": {"type": "string", "enum": ["read", "update", "create"]},
        "run_id": {"type": "string"},
        "summary": {"type": "string"},
        "next_action": {"type": "string"},
        "items": {"type": "array", "items": progress_item_schema},
        "coverage": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "dimensions": {"type": "array", "items": {"type": "string"}},
                "targets": {"type": "array", "items": coverage_target_schema},
                "coverage_requirement": {"type": "string"},
                "enforcement": {"type": "string"},
            },
            "additionalProperties": False,
        },
    }
    details = {
        "items": "这是开放清单，不是业务模板。status 只用 pending/in_progress/done/skipped/blocked；completed/read/ok 这类说明写 notes/summary，不要写进 status。长文、长清单、逐章/逐项任务里，优先每个对象写一个 item；notes 写真实读到的短事实，evidence 写文件、offset/行号、artifact_ref 或来源说明。不要只写“章节001-012已覆盖”来代替逐项事实。若已关闭项需要返工，对同一 id 传 status=in_progress 和 correction=true 明确重开；不能拿另一个 pending id 代替。",
        "coverage": "这是开放世界覆盖清单，不限定对象类型。targets 可以是项目、论文、API、日志源、文件、模块或任何当前任务对象；checks 必须是对象映射，键由当前任务自己定义，值只写 pending/in_progress/done/skipped/blocked。长任务里建议边读、边分析、边写报告时更新，不要最后一次性随便打钩；范围进度和逐项事实最好分开写。它只是模型维护的软计划，不是系统完成判定。",
    }
    return ToolModelSpec(
        name="task_progress",
        description="记录或读取当前运行的进度笔记。它只是可选的软账本，不选择会话、不切换工作区、不决定当前轮或后续轮是否继续。【复杂/长任务先建 plan】开工先把任务拆成 items 建一份稳定清单；后续沿用原 id 更新，不能在每次唤醒时另建一套同义清单。每完成一项立即 update 把该项 status 标为 done（界面会逐项打钩显示，模型跨轮也能靠它续接）。把某个仍 open 项原样交给下级时，可用 create_subagents.items[].covers 映射 exact id；不确定或属于额外返工时省略 covers。已关闭项要返工时，先对原 id 传 status=in_progress, correction=true 明确重开，不能拿无关 open id 顶替。清单仍有 open 项且当前还能推进时继续调用工具；只有目标完成或存在真实阻塞时才 final。",
        input_schema=_input_schema(parameters, property_schemas, details=details),
        hints=_task_progress_model_hints(),
    )


# LLM: Keep lengthy usage examples outside the schema builder so the model
# contract remains easy to audit without changing any exposure semantics.
# 函数用途: 构造 task_progress 的使用场景、关键词和示例。
def _task_progress_model_hints() -> ToolModelHints:
    return _hints(
        use_cases=(
            "任务很长，需要记下哪些小块已完成、正在做、下一步是什么",
            "任务要求覆盖多个对象，例如每个项目、每篇论文、每周数据、每个 API 或每个文件",
            "大体量构建任务（功能齐全的应用/多模块系统）：开工先把功能清单立成 items，每项实现→跑通→标 done 附证据，供模型跨轮续接和自查",
            "compact 后要恢复当前代理自己的工作进度",
            "希望生命周期事件给直接父级带回简短进度摘要",
        ),
        avoid_when=("只做一句普通回复、不需要跨轮保存进度时可以不用",),
        keywords=("进度", "清单", "todo", "checkpoint", "继续做", "compact", "任务账本"),
        examples=(
            '{"tool":"task_progress","action":"update","summary":"已读完两个项目","next_action":"继续读第三个项目","items":[{"id":"project-a","title":"阅读项目A","status":"done","evidence":["project-a/README.md","project-a/src/core.py"]}]}',
            '{"tool":"task_progress","action":"update","coverage":{"goal":"每个项目都要读 README、分析模块、写进报告","dimensions":["读 README","分析模块","写进报告"],"targets":[{"id":"project-a","checks":{"读 README":"done","分析模块":"pending"},"evidence":["project-a/README.md"]}]}}',
            '{"tool":"task_progress","action":"update","items":[{"id":"project-a","status":"in_progress","correction":true,"notes":"原实现路径错误，按同一项返工"}]}',
            '{"tool":"task_progress","action":"read"}',
        ),
    )


# LLM: Nested item schema advertises supported fields but deliberately leaves
# required/status validation to the handler so partial updates get rich errors.
# 函数用途: 构造进度项的模型 JSON 形状。
def _task_progress_item_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "status": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"},
            "next": {"type": "string"},
            "result": {"type": "string"},
            "outcome": {"type": "string"},
            "conclusion": {"type": "string"},
            "decision": {"type": "string"},
            "summary": {"type": "string"},
            "owner": {"type": "string"},
            "priority": {},
            "updated_at": {},
            "correction": {"type": "boolean"},
            "overwrite": {"type": "boolean"},
            "replace": {"type": "boolean"},
        },
    }


# LLM: Coverage keys are open-world while values remain strings for handler
# validation; provider-side enums would hide exact bad values from feedback.
# 函数用途: 构造覆盖对象的模型 JSON 形状。
def _task_progress_coverage_target_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "status": {"type": "string"},
            "checks": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "evidence": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"},
            "next": {"type": "string"},
            "owner": {"type": "string"},
            "priority": {},
            "updated_at": {},
            "coverage_kind": {"type": "string"},
            "source_ref": {"type": "string"},
        },
    }


# LLM: 模型取消合同只允许按 run_id 点名直接下级；子树扫描、
# dry-run 查看与进程细节都属于宿主运维私有能力。
# 函数用途: 构造 cancel_subagents 的最小模型参数合同。
def build_cancel_subagents_model_spec() -> ToolModelSpec:
    parameters = {
        "run_id": "可选。单个子代理 run_id。",
        "run_ids": "可选。多个直接子代理 run_id；只停止点名目标，不递归代管孙代理。",
        "reason": "可选。取消原因，会写入子代理 work log 和审计字段。",
    }
    property_schemas = {
        "run_id": {"type": "string"},
        "run_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    }
    return ToolModelSpec(
        name="cancel_subagents",
        description=(
            "取消当前代理直接创建的一个或多个下级；会废弃 active attempt、记录取消审计，有关联 pid 时会尝试终止。"
            "这是父级对直属下级的打断动作，不承担轮询、推动或质量验收。"
        ),
        input_schema=_input_schema(parameters, property_schemas),
        hints=_hints(
            use_cases=(
                "用户要求停止当前代理的某些直接子代理",
                "主代理发现子代理卡死、跑偏或不应继续消耗预算，需要显式收回",
                "后台 runner/channel 已损坏，需要把 agent tree 标成可见的取消/废弃状态",
            ),
            avoid_when=(
                "只是补充说明时用 send_guidance；正常运行时等宿主生命周期事件",
                "只是想知道进度时不要调用；宿主会把生命周期事件送回直接父级",
            ),
            keywords=("取消", "停止", "kill", "cancel", "subagent", "runner", "ABANDONED", "CANCELLED"),
            examples=(
                '{"tool":"cancel_subagents","run_ids":["subagent-1","subagent-2"],"reason":"用户要求停止"}',
                '{"tool":"cancel_subagents","run_id":"subagent-1","reason":"用户要求停止"}',
            ),
        ),
    )


# LLM: capability 裁决是直接父子之间的结构化授权控制，不承担推进、轮询或
# 质量验收；run_id 在 handler 还要经过直接下级授权门。
# 函数用途: 构造批准或拒绝直属子代理权限申请的模型合同。
def build_resolve_capability_requests_model_spec() -> ToolModelSpec:
    parameters = {
        "run_id": "必填。子代理 run_id。",
        "decision": "必填。grant=授权能力申请，deny=显式拒绝能力申请。deny 会把请求置为 CLOSED 并唤醒子代理按现有权限调整方案；不会终止子代理。",
        "reason": "必填。裁决原因，写入审计。",
        "request_id": "可选。grant/deny 时指定单个请求 id；缺省处理该 run 全部未决请求。",
        "write_roots": "可选。grant 文件系统请求时授权的目录列表；缺省用请求自带 path_scope。目录必须落在当前任务工作区或主代理 workspace 内；若请求的是兄弟/越界目录，不要重试 grant，改用 deny 唤醒 child 回现有写区。",
        "tools": "可选。grant 时附加授权的工具名列表；缺省用请求自带 requested_tools。",
    }
    return ToolModelSpec(
        name="resolve_capability_requests",
        description=(
            "主代理对子代理能力申请的裁决入口：grant 授权（可附目录写权限），deny 显式拒绝。"
            "两种处理都会唤醒子代理继续任务，并保留结构化审计记录。grant 不能扩出父级 workspace；"
            "越界请求应 deny，让 child 使用现有目录，不能反复 grant。"
        ),
        input_schema=_input_schema(parameters, _RESOLVE_CAPABILITY_PARAMETER_SCHEMA, required=("run_id", "decision")),
        hints=_hints(
            use_cases=(
                "子代理报告 PENDING_CAPABILITY_REQUEST / capability_request 未决，需要父级解锁目录或授权工具",
                "网络类申请(capability_type=network,子代理撞 NETWORK_PRIVATE_HOST_BLOCKED):底座不提供内网白名单授权,授权侧只能说明访问缺口或换公网来源",
            ),
            avoid_when=("没有直接下级发来的未决请求时不要调用",),
            keywords=("capability", "授权", "解锁", "拒绝", "grant", "deny", "capreq", "权限"),
            examples=(
                '{"tool":"resolve_capability_requests","run_id":"subagent-1","decision":"grant","reason":"解锁产物目录"}',
                '{"tool":"resolve_capability_requests","run_id":"subagent-1","request_id":"capreq-2","decision":"deny","reason":"请求目录在父级 workspace 之外，请改写当前 workspace 内的 output 目录"}',
            ),
        ),
    )


def _with_role_template_index(details: dict[str, str]) -> dict[str, str]:
    return {
        key: value.replace("{role_template_index}", role_template_index_text())
        for key, value in details.items()
    }
