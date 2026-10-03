
from __future__ import annotations

# LLM: 默认值须与随包 YAML 一致；语法诊断和各决策点默认关闭，覆盖由原 owner/thread 保存，建议不改变权限或终态。
# 模块用途: 定义并加载 Agent 配置，统一显式采样、人格、工具反馈、插件管理与用户确认口径。
"""智能体配置加载工具。

这个模块干的事情不复杂，但很关键：
- 定义程序运行时到底有哪些配置项
- 从磁盘读取一个简化版 YAML 配置
- 把未知字段过滤掉，避免用户多写了配置就直接把程序搞崩

这里坚持只用标准库，目的是让项目在 Windows / Linux / macOS 上都能轻装运行。
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..path_access_policy import DEFAULT_DANGEROUS_PATH_ROOTS, DEFAULT_PATH_ACCESS_MODE
from .config_io import load_simple_yaml, parse_scalar
from .config_sources import (
    INTERNAL_RUNTIME_CONFIG_FIELDS,
    merge_agent_config_sources,
    public_config_keys,
)
from .defaults import (
    DEFAULT_COMMAND_ACCESS_MODE,
    DEFAULT_MODEL_MAX_TOKENS,
)
from .memory import normalize_agent_memory_config
from .normalize import (
    _coerce_bool_config,
    _coerce_choice_config,
    _coerce_float_config,
    _coerce_int_config,
    normalize_agent_config,
)

__all__ = [
    "AgentConfig",
    "DEFAULT_DECISION_AUTONOMY",
    "DEFAULT_DELEGATED_EXECUTION_PERSISTENCE",
    "DEFAULT_EXECUTION_PERSISTENCE",
    "DEFAULT_SYSTEM_PROMPT",
    "INTERNAL_RUNTIME_CONFIG_FIELDS",
    "_coerce_bool_config",
    "_coerce_choice_config",
    "_coerce_float_config",
    "_coerce_int_config",
    "load_config",
    "load_simple_yaml",
    "parse_scalar",
    "apply_log_level",
    "normalize_agent_config",
]

_INT_PATTERN = re.compile(r"-?[0-9]+")
_FLOAT_PATTERN = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


# LLM: Mirror 会话运行时 Default mode's assumptions-first boundary as model guidance,
# not a host decision gate. Keep this generic: it must never inspect task text,
# choose a domain-specific language, or turn an ordinary clarification into a
# machine status transition.
# 配置用途: 指导主代理在安全可逆的次要选择上自行采用合理默认，只有无法安全推断的关键缺口才向用户提问。
DEFAULT_DECISION_AUTONOMY = (
    "自主决策：用户已经给出明确目标时，优先从当前目录、现有代码、用户约束和工具事实补齐信息；"
    "缺少次要实现选择时，不要停下来反问。若仍有多种安全可行方案，选择合理默认，必要时简短说明假设，"
    "然后继续执行。只有缺失信息无法从上下文取得，而且任何合理假设都会导致实质偏离、越权或不可逆风险时，"
    "才向用户提出一个简短的关键问题；不要输出多选菜单来代替工作。"
    "用户已经明确授权你在可行方案中自行选择时，直接选择并继续。"
    "任务规模大、耗时长或仅仅有可澄清之处，都不等于阻塞。"
)


# LLM: 持续执行规则必须服从当前结构化执行归属；独立 Goal 不等于普通子代理，不能要求每个运行者重复完成整个用户请求。
# 配置用途: 共用持续工作和验证纪律，分别明确主代理与子代理负责的范围；这些是模型指导，不是机器完成门。
_EXECUTION_PERSISTENCE_BODY = (
    "而且现有工具、子代理或可用证据还能继续推进，"
    "就持续工作，不要停在分析、骨架、局部修复或一份诚实的未完成清单上；把工作推进到实现、验证和清楚交付。"
    "工具调用失败时先读真实错误并修正方法，不能把一次可恢复失败当作结束理由。"
    "只有目标已经端到端解决、用户明确暂停或改向，或者存在当前确实无法消除的真实阻塞时，才结束本轮。"
)
_EXECUTION_VERIFICATION_SUFFIX = (
    "的可见行为和端到端入口；模块能导入、文件或类存在、"
    "代码量或数量达标都只能算局部证据。安装、构建、启动或关键路径失败，说明目标仍未解决；"
    "修正后必须重跑同一入口，不能把 `|| true`、`|| echo` 等忽略失败包装后的外层成功当成内部成功。"
    "能暴露当前缺陷的有效测试不得仅为变绿而删除、跳过、放宽断言或改成只测存在；"
    "应该修实现，确实无法解决时保留真实失败并如实报告。"
)
DEFAULT_EXECUTION_PERSISTENCE = (
    "执行归属：普通回合负责当前用户请求；运行时存在 current_goal 时，本轮负责该目标的完整要求。"
    "每个代理最多一个未结束 Goal，历史目标不是本轮的新任务。主子代理目标各自独立，"
    "不能替未结束的下级宣布完成。"
    "用户新消息仍需回应，明确改派时再按新的执行归属工作。"
    "执行纪律：只要本轮负责的目标仍有你已知的未完成部分，"
    + _EXECUTION_PERSISTENCE_BODY
    + "本轮自己创建的普通子代理只是分工，仍需接收结果、整合验证和交付；"
    + "骨架、空壳、最小示例或只显示欢迎信息的 demo 只能算阶段成果，"
    + "不能替代本轮目标要求的完整功能、完整测试和可运行交付。"
    + "验证纪律：验证必须覆盖本轮目标实际要求"
    + _EXECUTION_VERIFICATION_SUFFIX
)
DEFAULT_DELEGATED_EXECUTION_PERSISTENCE = (
    "执行纪律：只要直接父级当前 goal 仍有你已知的未完成部分，"
    + _EXECUTION_PERSISTENCE_BODY
    + "直接父级给你的当前 goal 是本轮完整工作边界；保留该 goal 的全部要求，"
    "但不要因为根用户目标更大而实现未交给你的兄弟计划项；"
    + "骨架、空壳、最小示例或只显示欢迎信息的 demo 只能算该 goal 的阶段成果，"
    + "不能替代该 goal 要求的完整功能、完整测试和可运行交付。"
    + "验证纪律：验证必须覆盖当前 goal 实际要求"
    + _EXECUTION_VERIFICATION_SUFFIX
)


# LLM: The schema default and shipped YAML must remain text-identical. Avoid
# language-specific scaffold instructions: they made large cross-language ports
# stop at a toy skeleton and also changed behavior when a deployment omitted the
# system_prompt key.
# 配置用途: 提供未显式配置 system_prompt 时真正生效的默认人格和执行方式。
DEFAULT_SYSTEM_PROMPT = (
    "你是 my-agent，一个自主的 CLI 智能体，用工具、多通道网关和子代理完成真实工程与运营任务。 "
    + DEFAULT_DECISION_AUTONOMY
    + " "
    + DEFAULT_EXECUTION_PERSISTENCE
    + " 沟通：尽量简短直接。除非用户要详细，否则别长篇、别加空洞开场和结尾、别堆能力清单；"
    "能一两句说清就一两句。 "
    "如实报告：测试失败就说明失败；某步跳过就说跳过；只做了一部分就说清未完成项和阻塞。"
    "只有真正做完并验证后才说完成，绝不拿旧的、局部的或未验证的结果冒充完整交付。 "
    "安全：按当前权限执行已授权范围；未授权的高风险、不可逆或对外动作先确认，不重复索要已有授权。 "
    "工作方式：先读取与当前目标直接相关的现有代码、约定和测试，再按可验证的小步实现；"
    "失败后根据真实结果调整，不机械重复。只做用户要求的范围。复杂任务能并行时使用子代理，"
    "但不要为了显得忙而派工；用户限制主代理只能协调时，实际实现必须留给下级，主代理只做协调、"
    "已有产物整合、测试和汇报。 "
    "持续目标：用户或直接父级明确要求建立 Goal 时，先调用 create_goal 并确认成功；task_progress 只记计划，不会建立 Goal，也不能代替它。普通任务不自动建立 Goal。已有目标需要纠正时用 update_goal 修改，不为换名字另开目标。派工可只给 prompt，也可显式附 persistent_goal。 "
    "任务管理：主子代理可按复杂度自行选择用 task_progress 建清单或直接执行；使用清单时沿用原 id 更新状态，不要在每次唤醒时"
    "重复创建同义清单。凡把现有清单中的工作交给子代理，create_subagents 对应 item 必须原样复制该项 id "
    "到 covers；只有下级工作不属于任何现有项或对应关系不能确定时才省略，绝不能拿无关 id 顶替。显式 "
    "covers 的 child DONE 后系统会按 id 打勾；未绑定 child 不会关闭原 Todo，父级收到完成事件后应立刻用 "
    "task_progress 按 exact id 更新已经由当前证据证实完成的计划项。待办未清空且仍可推进时继续工作；"
    "清单只是模型自查，不是宿主机器验收。"
)


@dataclass
class _HomeProviderConfigFields:
    my_agent_home: str = ""
    # B 切片：显式执行模式（managed / local_unmanaged）。空 = 未显式指定，
    # 挂载逻辑按有无 owner_home_dir 推断（兼容存量调用）。SimpleAgent 构造
    # 透传给 SubAgentManager（manager._attach_runtime_db 落枚举），让工具
    # OperationStore 选择器按显式模式选 store，而不是按 home 隐式推断。
    execution_mode: str = ""
    my_agent_owner_provider: str = "local"
    my_agent_owner_kind: str = "main"
    my_agent_owner_id: str = "main"
    # 仅向模型说明推荐的业务文件整理格式，不决定运行归档或文件权限。
    workspace_task_path_template: str = "tasks/{date}/{task_slug}"
    home_context_enabled: bool = True
    home_lesson_stale_caveat_days: int = 7
    run_task_workspace_enabled: bool = True
    timezone: str = ""  # IANA 时区名(如 Asia/Shanghai、America/New_York);空=服务器本地(审计 #21)
    week_start: str = "monday"  # 周起始 locale:monday/sunday/saturday,影响"本周"范围计算


# LLM: 工具、语法观察和显式插件管理默认归此组；开关同步 YAML、规范化和执行入口，不能用展示配置代替权限。
# 类用途: 保存工具行为与可选诊断的默认值，构造本身不加载插件或执行工具。
@dataclass
class _ToolConfigFields:
    enable_tools: bool = True
    # 仅允许用户显式管理插件；未安装/启用时不加载插件或启动额外进程。
    enable_plugins: bool = True
    # 插件进程 OS 沙箱试点：开启后插件进程只可写自己的数据目录（读范围与网络不变）；沙箱不可用则不启动插件。
    plugin_process_sandbox: bool = False
    max_tool_rounds: int | None = None
    # 一次最多同时执行几个工具（原 max_tool_calls_per_round 已并入）：空 = 8，正数 = 上限，0 = 不限制；
    # 任务属性 max_parallel_tool_calls 可单任务覆盖，后台工作片另有 max_tool_calls_per_round 任务属性封顶。
    max_parallel_tool_calls: int | None = None
    # 后台调度器的周期性孤儿 supervision(reconcile 兜底,零 LLM 成本):每隔此秒数巡查一次
    # "盯守死岗补建接管 + durable 复活 PENDING/PLANNING 停滞孤儿"。事件唤醒覆盖不了
    # 静默死亡(SIGKILL/断电不发 wake),靠这里捡回;0=关闭。
    orphan_supervision_interval_seconds: int = 60
    # 连续多少个 Goal 自动续跑片既没有工具调用，也没有 Goal/任务结构化状态变化时暂停；0=不限。
    goal_continuation_idle_limit: int = 3
    # Todo 仍开放时，把 exact-id 收尾软提醒放进模型上下文；只提示模型在最终回复前
    # 自主核对，不自动打勾、不阻断最终回复，也不增加隐藏模型调用。
    task_progress_closeout_guidance_enabled: bool = True
    # 软引导阈值已降为读取点旁的具名常量（_CHANNEL_HINT_THRESHOLD_COUNT），不再是配置项（2026-09-28 参数减量）。
    # 单个代理最近 10 分钟最多调用几次工具（窗口固定 600 秒）；空或 0 = 关闭，默认关闭。
    tool_agent_budget_max_calls: int | None = None
    # 单个 run 最近 10 分钟最多读取多少字符的归档正文（窗口固定 600 秒）；0 = 不限制。
    tool_artifact_read_budget_max_chars: int = 240_000
    tool_output_externalize_min_chars: int = 200_000
    tool_output_preview_chars: int = 4_000
    # 上下文余量不足时立刻外置本条工具输出并要求下一次请求前压缩；关掉则只按 tool_output_externalize_min_chars 外置。
    tool_output_externalize_on_low_headroom: bool = True
    # 工具上下文保留条数与 PTL 自救重试次数已降为读取点旁的具名常量
    # （DEFAULT_MICROCOMPACT_KEEP_RECENT_COUNT / DEFAULT_PTL_RETRY_MAX_COUNT），不再是配置项（2026-09-28 参数减量）。
    tool_read_max_chars: int = 16_000
    # 三种原生文件修改工具反馈有界 JSON 语法观察；不拦分块写入，不决定任务完成。
    enable_file_syntax_diagnostics: bool = False
    tool_list_max_entries: int = 200
    tool_search_max_matches: int = 50
    tool_web_max_chars: int = 100_000
    tool_http_timeout: int = 30
    path_access_mode: str = DEFAULT_PATH_ACCESS_MODE
    path_dangerous_roots: list[str] = field(default_factory=lambda: list(DEFAULT_DANGEROUS_PATH_ROOTS))
    access_mode: str = DEFAULT_COMMAND_ACCESS_MODE
    tool_shell_timeout: int = 240
    tool_shell_output_max_chars: int = 12_000
    # 后台服务默认只允许监听本机回环；声明 loopback 却绑到局域网地址时由 host 回收（False 只记 listener_warning）。
    background_process_listen_scope_enforce: bool = True
    # macOS 上对非本机管理员的 owner 拒读用户家目录（本 owner 可见范围除外），并把其 Shell 的 HOME 指到 owner home。默认关闭。
    shell_sandbox_hide_user_home: bool = False
    # owner 隔离的 Shell 命令以非零码退出时，结果附带沙箱边界事实（本次允许读写的目录、可能越界的提示与下一步建议），
    # run_command 说明里多一条边界提示；只改变模型看到的内容，不放宽沙箱。默认开启。
    shell_sandbox_boundary_facts: bool = True
    stream_enabled: bool = True
    tool_catalog_mode: str = "compact"
    tool_catalog_categories: list[str] = field(default_factory=list)
    # Default prompt catalog stays compact: examples and long parameter notes
    # remain available through list_tools or the recommended-tool details.
    tool_catalog_include_examples: bool = False
    # 渐进式披露:这些 category 仍注册，但不进初始模型 schema；通过 tool_search 加载。
    # 递归代理和持续目标控制属于主链，orchestration/goal 默认首轮直出；显式 allowed_tools 的
    # 结构化后台/runner profile 仍保持全量直出。[] 恢复全量。
    tool_catalog_deferred_categories: list[str] = field(
        default_factory=lambda: ["collaboration", "web", "vision", "meta", "mcp"]
    )
    # 工具在自己规格里声明的“默认收起”（又大又少用的工具）是否生效；默认关，打开后前台回合不再每轮发它们的 Schema，
    # 目录末尾留“名字：一句用途”的索引，用到时 tool_search 一步加载。显式 allowed_tools 的回合不受影响。
    tool_default_deferral_enabled: bool = False
    # 推荐区和 tool_search 的检索容量已降为 agent/core.py 的具名常量 TOOL_RETRIEVAL_LIMIT_COUNT（2026-09-28 参数减量）。
    # 工具语义检索开关；向量来自与记忆语义召回共用的嵌入服务 embedding_*（没配模型时只走关键词）。
    tool_vector_search_enabled: bool = True
    # MCP 客户端(短板6)：声明要连接的外部 MCP server，把社区现成工具(GitHub/DB/Slack 等)
    # 动态注册成 mcp__<server>__<tool> 前缀的工具。结构：
    #   {server_name: {command: str, args: [..], env: {..}, timeout: int, connect_timeout: int,
    #                  catalog_category: mcp, default_effect: dangerous,
    #                  tool_effects: {tool_name: read_only|mutating|dangerous}}}
    # catalog_category 默认 mcp（渐进披露）；独立分类只改变模型目录，不改变授权或 effect。
    # 未声明 effect 的 MCP 工具默认 dangerous；只有部署配置可逐工具降低风险，server 自报不授权。
    # 默认空 = 不连任何 server、不起任何子进程(零开销)。仅 stdio 传输(JSON-RPC over stdio)。
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    # Computer Use 复用 computer-control-mcp，不在本项目实现鼠标/键盘/OCR。只有 local/main
    # 管理员同时显式 full-access 时才注入；普通 owner 和 WorkspaceOnly 一律不可见。
    computer_use_enabled: bool = False
    # J16 屏幕观察：在 Computer Use 适配器里再注册 observe_window / click_candidate（只读采样 + 候选点击，审批策略
    # always / dangerous）。默认关：工具目录不变；开了也仍受 computer_use_enabled、local/main 与 Full Access 三重约束。
    computer_use_observation_enabled: bool = False


# LLM: 运行预算类字段的默认值组；默认值须与随包 YAML 一致，改动同步规范化与参数登记表。
# 类用途: 保存扫描、会话读取、后台租约等运行预算的默认值，构造本身不产生副作用。
@dataclass
class _RuntimeBudgetConfigFields:
    skill_guard_max_files: int = 50
    skill_guard_max_size_kb: int = 1024
    background_context_max_total_tokens: int = 8000
    # 后台会话执行权的租约秒数；续约心跳按它自动推导（原 background_claim_heartbeat_interval_seconds 已并入）。
    background_claim_ttl_seconds: int = 90
    # CLI 续跑轮数护栏已降为 cli/resume_loop._RESUME_MAX_ROUND_COUNT（2026-09-28 参数减量）。
    background_main_agent_allowed_tools: list[str] = field(default_factory=list)


# LLM: 通用决策默认与 MemorySettings 对齐；Curator 档案只存引用，不拼接聊天连接，不接受模型修改，实验开关也不授予请求许可。
# 类用途: 汇总模型和运行配置；决策默认关闭，有限正时间只为后续请求提供默认，不管理阶段时钟。
@dataclass
class AgentConfig(_HomeProviderConfigFields, _ToolConfigFields, _RuntimeBudgetConfigFields):

    # 决策增强默认关闭；有限正秒数只作用后续请求，不重置已开始阶段的预算。
    decision_enabled: bool = False
    decision_experiment_enabled: bool = False
    # observe 采样（默认关）：打开后只作用于 observe 点位——每个点位每自然小时成功调用满 6 次就不再调用决策模型，
    # 失败和超时不计名额，所以出问题的点位会一直被观察；apply 点位不受影响。
    decision_observe_sampling_enabled: bool = False
    # observe 不挡主链路（默认关）：打开后普通会话范围的 observe 点位把决策调用交给后台单 worker 执行，回复不再等待决策模型，
    # 后台等待上限改用 decision_background_timeout_seconds；apply、实验与用户后台点位不受影响，仍同步等待。
    decision_observe_nonblocking_enabled: bool = False
    # 决策点诊断记录（被挡下的跳过行 + 到达/未触发原因计数）：关闭时既不在结果日志记 skipped（原因码、无正文），
    # 也不累计 decision_reach_counts 的到达/未触发原因计数。
    decision_skip_records_enabled: bool = True
    decision_timeout_seconds: float = 3.0
    # 决策调用复用长连接（仓库默认关，Mac 线上验收后由集成方打开）：打开后同一进程里的决策请求复用已建好的 HTTPS 连接，
    # 省掉每次约 0.9 秒的代理隧道与 TLS 握手；关着就每次新建连接，与改动前逐字节相同。不改请求内容、重试或期限。
    decision_connection_reuse_enabled: bool = False
    # 同一前台阶段累计等待：2026-10-02 由 4 提到 5 秒，与选模型点位默认 5 秒一致（前台点位实际等待取点位期限与它的较小值）。
    decision_stage_timeout_seconds: float = 5.0
    decision_background_timeout_seconds: float = 4.0
    decision_profile_id: str = ""
    decision_model_selection_mode: str = "off"
    # 选模型询问节奏（默认每轮都问）：structure_change 时只在新会话、压缩之后、模型目录或当前模型变化时问决策模型。
    decision_model_selection_cadence: str = "every_turn"
    # 选模型决策请求里对话摘要（只带语义部分，不带原文锚点段）与当前消息的字符上限；0 表示不截断，截断时如实标注
    # 规划/交付质量/动作候选三个决策点位发给决策模型的当前请求字数预算：更长时取首尾节选并标注；0 表示不截取
    decision_request_max_chars: int = 2000
    decision_external_material_order_mode: str = "off"
    decision_planning_mode: str = "off"
    decision_delivery_quality_mode: str = "off"
    decision_action_candidate_mode: str = "off"
    # Skill 提案审核顺序只排 CLI 展示、不授予 Skill/工具权限，且与 enable_self_learning 同属主配置，故不放 CapabilityConfig。
    decision_skill_proposal_review_mode: str = "off"
    agent_name: str = "myagent"
    # 默认沿 终端交互 主链由 TUI 接管滚轮、点击与应用内选区；F6 仍可临时退回宿主终端原生复制。
    # 关闭后备用屏幕收不到物理滚轮，历史只能用 PgUp/Ctrl+Home，因此不再作为开箱默认。
    tui_mouse_capture_default: bool = True
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    workspace_root: str | list[str] = ""
    # 未配置时允许打开 Gateway/TUI 和 /model，但不选厂商、不调用模型；echo 仅供显式离线调试。
    model_backend: str = ""
    memory_path: str = ""
    # 记忆决策默认定义归 MemorySettings；这里镜像供现有 YAML 配置加载与展示。
    memory_decision_pre_recall_mode: str = "off"
    # 召回前补充查询的片段材料（默认只给片段文字）：with_new_facts 时宿主先按原检索预检每个片段能新增的正式事实，
    # 把条数与摘要交给决策模型，补不出新事实的片段不给选，全都补不出就不调用；每个片段多一次查询嵌入。
    memory_decision_pre_recall_fragment_material: str = "query_text"
    memory_decision_recall_mode: str = "off"
    memory_decision_curator_mode: str = "off"
    memory_decision_curator_relation_mode: str = "off"
    memory_top_k: int = 5
    auto_save_memory: bool = True
    # 记忆语义召回(检索拓宽 #1,默认关=现状纯关键词):开后记忆召回在关键词(FTS5/BM25)外再加一路
    # 语义向量召回,RRF 融合,治"换词就召不回"。向量只存各 owner 自己 home 的本地文件(零外部依赖、
    # 不碰共享向量库、per-用户隔离)。需配 embedding_model_profile;没配则自动只走关键词(不崩不退化)。
    memory_semantic_recall: bool = False
    # 嵌入服务:记忆语义召回与工具语义检索共用一个模型档案引用(P13,2026-10-02,替代原 4 个平铺键
    # embedding_model/api_base/api_key/api_key_env,旧键只告警)。档案必须存在、启用且支持 embedding;
    # 空=两边都不用语义,只走关键词。档案指向服务商凭据与端点,属于安全边界,模型不能改。
    embedding_model_profile: str = ""  # /model 里 embedding 用途档案的编号;空=不发请求、只走关键词
    memory_archive_level: int = 3
    memory_hook_enabled: bool = True
    memory_rule_routing_mode: str = "soft"
    memory_rule_auto_read_limit: int = 3
    memory_resume_auto_context_mode: str = "off"
    memory_compact_auto_trigger_percent: int = 90
    # 自动压缩触发线的绝对 token 上限，与上面的百分比取小；0 表示不封顶（默认，行为不变）。1M 这类大窗口下百分比最低只到 50%，靠它把压缩提前。
    memory_compact_auto_trigger_max_tokens: int = 300_000
    # 校准比值跨会话沿用：开着时成功调用写 owner data/context/calibration.json，线程观测对不上时按比值折算；关掉不读不写。
    memory_context_calibration_carry_enabled: bool = True
    # 含图历史的压缩策略：auto 默认走归档引用（视觉能力事实接入后按事实选择随图摘要）；archived_refs 固定归档引用；off 保持从首个媒体回合起保护全部后缀。
    compact_media_policy: str = "auto"
    # 当前模型声明的输入模态（如 text、image、video），来自模型档案的 input_modalities；空表示未知，压缩策略会用一次结构化视觉探针判断，不按模型名猜。
    model_input_modalities: list[str] = field(default_factory=list)
    # 压缩摘要末尾“原话备份”（用户原话优先、最新优先，放不下的那条保留头尾）最多占多少 token；实际上限还不超过模型窗口的 10%。0 表示不附原话，只保留被省略消息的编号与回查说明。
    compact_landmark_max_tokens: int = 20000
    # 压缩摘要末尾是否告诉模型：被压缩的原话仍在会话记录里，可用 session_search 按 message_id 分段读回（本 agent 注册了 session_search 时才附）。
    compact_recall_hint_enabled: bool = True
    # 到达触发线后优先把完整输入收敛到该占比；低于真实触发线的有效候选不会因未达目标而被丢弃。
    memory_compact_recovery_target_percent: int = 60
    # 后台 Memory Curator 只读有界经历并输出严格 daily/candidate JSON；它没有工具循环和写人格权限。
    memory_curator_enabled: bool = True
    # 留空时按消息来源会话的主代理模型整理（2026-10-02 用户拍板），会话没选或不可用时用 owner 默认；填编号则固定用它，
    # 某个 owner 解析不到这个编号时该 owner 改用自己的默认模型（curator_profile_unavailable_fallback）。
    memory_curator_model_profile: str = ""
    memory_curator_interval_seconds: int = 10_800
    memory_curator_turn_threshold: int = 10
    memory_curator_max_input_chars: int = 40_000
    memory_curator_timeout_seconds: int = 90
    # 记忆策展使用独立后台车道；限制并发可避免历史 owner 积压抢占主会话/子代理唤醒线程。
    memory_curator_workers: int = 2
    memory_curator_daily_finalize_hour: int = 23
    memory_curator_auto_promotion_policy: str = "conservative_v1"
    memory_lesson_min_occurrences: int = 2
    memory_hot_min_occurrences: int = 3
    # compact 自动续跑深度硬顶已降为读取点旁的具名常量
    # （finalization_compact_auto._DEFAULT_MAX_COMPACT_AUTO_CONTINUE_DEPTH，2026-09-28 参数减量）。
    # compact 续跑时对卸掉的中段历史做一次 LLM 语义摘要（短板6，长期助手 trajectory_compressor
    # 蓝本）：默认开，保护首尾、只摘要中段；异常、供应商超时或无 backend 时回退机械重建。
    # 摘要不另设不可取消线程超时，直接沿用统一模型传输超时。enabled=false 即完全关闭。
    memory_compact_semantic_summary_enabled: bool = True
    memory_config_warnings: list[dict[str, Any]] = field(default_factory=list)
    local_store_path: str = ""
    local_store_files_dir: str = ""
    local_store_events_path: str = ""
    local_store_fts_enabled: bool = True
    prompt_files: list[str] = field(default_factory=lambda: ["builtin:prompts/default.md"])
    # 额外可写根（沙箱写边界扩展）：任务 work/output 之外的目录也允许模型写入。
    # 会话运行时 对应 sandbox_workspace_write.writable_roots；测试/用户可配置共享工作区。
    additional_write_roots: list[str] = field(default_factory=list)
    # 管理员 Full Access 时提示词里给出的自身开发工作树（空=不启用）；只是工作约定，不授予写权限。
    self_dev_worktree: str = ""
    # 正在运行的安装目录对所有工具只读（含 Full Access），部署是唯一更新方式；只在调试安装本身时关闭。
    protect_running_runtime: bool = True
    enable_subagents: bool = True
    # 当前根会话树可同时保留的未结束子代理数；不同 TUI/根任务互不占槽。
    max_subagents: int = 8
    subagent_workspace: str = ""
    subagent_allowed_tools: list[str] = field(default_factory=list)
    subagent_role_template_dirs: list[str] = field(default_factory=list)
    task_max_subagents: int = 0
    subagent_hierarchy_max_children_per_tool_call: int = 0
    subagent_takeover_chain_max_depth: int = 0
    subagent_debug_trace_level: int = 0
    # 自学习默认关闭；开启后主代理完成的多轮工具任务会在后台自动总结成 owner 的 skills/learned/ Skill（自动闸门代替人工确认），
    # 子代理 lesson 提案也会立即走原确认链自动安装。
    enable_self_learning: bool = False
    # 自动总结 Skill 的触发与预算：工具轮数门槛、每 owner 每日模型调用上限（0 不限）、自学 Skill 数量上限（0 不限，只限新建）、单次调用超时秒数。
    self_learning_min_tool_rounds: int = 6
    self_learning_daily_limit: int = 20
    self_learning_max_skills: int = 50
    self_learning_timeout_seconds: int = 180
    # 主会话代理可用 manage_models 工具直接增删改切 owner 模型目录；关闭后只能用 TUI /model 手动配置。
    enable_model_profile_tool: bool = True
    # 主模型可用 memory_search 只读检索本人正式长期记忆（自动召回漏掉时自查）；默认关，关着不注册该工具。
    enable_memory_search_tool: bool = False
    # 本机管理员主代理可用 restart_gateway 工具安排 Gateway 安全重启（先排空再换进程）；关闭后不注册该工具。
    enable_gateway_restart_tool: bool = True
    # TUI 发现 Gateway 换了安装（runtime_prefix 不同）且自身空闲时在同一终端原地换成同版客户端（同会话、不退出全屏）；关闭后只在 footer 提示。
    tui_follow_gateway_upgrade: bool = True
    # 主会话后台命令完成后进入既有持久唤醒队列；关闭后仍可主动查询或长等待。
    background_process_notifications: bool = True
    # 请求前缀诊断开关已降为 agent_core/tool_model_generation._CACHE_DIAGNOSTICS_ENABLED（2026-09-28 参数减量）。
    dynamic_timeout_min: int = 30
    dynamic_timeout_max: int = 10800
    # 未取得稳定 probe 样本时输出吞吐的保守估计；只决定非流式请求总预算（预填充吞吐与 probe 统计参数已是代码常量）。
    estimated_output_tokens_per_second: float = 20.0
    model_speed_profile_path: str = ""
    user_id: str = "admin"
    # 多租户鉴权配置
    auth_enabled: bool = True
    admin_user_id: str = "admin"
    runner_concurrency: str = "auto"
    runner_start_rate: str = "auto"
    runner_timeout_seconds: str = "off"
    runner_timeout_by_role: dict[str, object] = field(default_factory=dict)
    # 子代理 runner 普通失败后最多自动重跑几次（唯一的家；原 runner_failure_policy 与守卫文件的两个重派上限已并入）；0 = 不自动重跑。
    runner_failure_retry_limit: int = 1
    gateway_workspace: str = ""
    gateway_stop_timeout: int = 20
    gateway_request_timeout: int = 300
    # 网关 ask 两层限流(取代原「全局总 10」单层总闸,接真实 /ask 链路):
    # 每用户「小坑」=单用户同时在飞上限(防一个用户独吞把别人饿死);
    # 全局「大坑」=总在飞上限(高天花板,超出留在 pending 排队、不拒不崩)。
    # 都是先设的限制值(不是吞吐目标),按机器/模型承载力调。执行线程按需起、用后驻留复用。
    gateway_user_inflight_limit: int = 8
    gateway_global_inflight_limit: int = 500
    gateway_processing_timeout_seconds: int = 900
    # 仅计 processing 租约失效的失败次数；服务重启续接不计，0 不限，副作用未知仍禁止盲目重放。
    gateway_request_max_attempts: int = 2
    # 后台会话全局线程池上限；超出留在持久队列，同 thread 仍由 run claim 单飞。
    background_owner_workers: int = 8
    # 单 owner 同时可跑的独立后台会话数；调大会增加并发模型请求。
    background_threads_per_owner: int = 4
    # per-owner 作用域 agent 实例池上限(原 owner_scoped_pool.py 硬编码 64):有界 LRU,
    # 超出逐出最久未用;千并发多用户时的驻留 agent 数调参入口。
    owner_agent_pool_max_agents: int = 64
    # 空闲实例寿命；只回收无执行租用、无持久工作者，0 关闭，身份和会话不删除。
    owner_agent_idle_seconds: float = 60.0
    input_media_max_bytes: int = 16 * 1024 * 1024
    input_media_max_files: int = 8
    # owner retention 扫描控制器每拍只处理一个有界页，不创建 Agent、不调用 LLM。
    # 每个 owner 的 retention.json 另有日级执行节流；0 关闭网关自动扫描。
    owner_maintenance_scan_interval_seconds: int = 60
    gateway_port: int = 8420
    # 多用户通道默认按 X-User-Id/channel 解析独立 owner；远程身份缺失或 owner 创建失败时
    # fail-closed，绝不回退共享 main。单机 CLI 无远程 provider 身份时仍使用 local main。
    gateway_per_user_owner_scoping: bool = True
    # 用管理员密码（本机 CLI 设置）在 IM 一对一私聊里 /admin 绑定过的身份按本机主用户 local/main 运行，
    # Gateway 为这些私聊开启聊天内工具审批（/approve、/deny）。关闭时 /admin 拒绝、已有绑定不生效（文件保留）。
    admin_channel_identity_enabled: bool = True
    # 网关 HTTP 绑定地址:默认 loopback,仅本机可达。绑非 loopback(暴露到网络)时强制要求鉴权,
    # 否则 fail-closed 拒绝启动(防"绑 0.0.0.0 + 无鉴权 = 未认证远程命令执行")。默认仅监听回环地址。
    gateway_bind_host: str = "127.0.0.1"
    # 网关局部信任 token(暴露部署用):非空时,非回环来源须带匹配的 X-Gateway-Token 才被信任,
    # 否则降为匿名 USER。默认空=只靠回环 peer 信任(适配器/CLI 走 127.0.0.1)。
    gateway_auth_token: str = ""
    # G2b 上线闸(默认 false,强制阶段稳定后退场删除):true 时本机客户端读不到凭据(缺失/权限错/内容损坏)
    # 直接拒绝请求(零请求、带原因码);false 时降级为不带凭据继续发送,由 Gateway 按 G2a 计数。
    # 客户端与 Gateway 读同一份配置,两边口径一致。
    gateway_require_local_credential: bool = False
    gateway_ready_timeout_seconds: int = 3
    # 安全重启第一段：停领新请求后，等本进程在跑回合结束的上限秒数；超时后关闭工具关口，停在工具前的回合由接班进程续跑。
    gateway_restart_turn_wait_seconds: int = 300
    # 安全重启第二段：等执行中的副作用工具归零的上限秒数，0=不限；超时取消本次重启并恢复服务，不强杀。
    gateway_restart_drain_timeout_seconds: int = 600
    # 两次安全重启之间的最短间隔秒数，0=不限；间隔内的请求直接返回冷却中。
    gateway_restart_cooldown_seconds: int = 30
    adapter_workspace: str = ""
    # 飞书适配器配置
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_callback_port: int = 8421
    feishu_connection_mode: str = "long_connection"  # 默认长连接:免公网且支持密码/确认卡片回调；webhook 可显式选择
    feishu_ws_proxy: str = ""  # 长连可选代理(空=走 HTTPS_PROXY 环境变量;TUN/代理环境直连飞书 WS 网关是黑洞,需显式填代理)
    feishu_session_lock_enabled: bool = True  # 私聊锁默认开启；首次发设置卡但不吞消息，设密后闲置才拦截，群聊不锁
    feishu_personal_idle_lock_seconds: int = 10800  # 私聊闲置多久后锁定(秒,默认 3h);feishu_session_lock_enabled 开启时生效
    # QQ 适配器配置
    qq_app_id: str = ""
    qq_app_secret: str = ""
    session_workspace: str = ""
    # 长期主代理会话账本目录。它保存 thread/message/task/policy 机器事实，
    # 不保存真实通道凭证，也不把用户任务变成内置 case。
    conversation_workspace: str = ""
    # 多代理协作控制面目录。这里保存 case/request/evidence/decision 的轻量账本，
    # 大日志、大文件、API 返回和截图只通过 evidence_refs 引用，避免把协作层变成业务模板。
    collaboration_workspace: str = ""
    # 协作请求自动唤醒 responder 的最大并发数。普通 dispatch 仍保持默认宽度；
    # 只有已有 collaboration request 等待多个代理响应时，才用这个上限减少串行等待。
    # 0 表示关闭自动放宽，完全按 dispatch/max_runners 原值执行。
    collaboration_auto_dispatch_max_runners: int = 8
    audit_enabled: bool = True
    audit_log_path: str = ""
    daemon_mutate_state: bool = True
    daemon_start_runners: bool = True
    daemon_runner_instruction: str = ""
    lease_stale_without_heartbeat_seconds: int = 300
    log_level: str = "info"
    extension_plugins: list[str] = field(default_factory=list)
    api_base: str = ""
    api_key: str = ""
    api_key_env: str = "AGENT_API_KEY"
    # 显式兼容头只影响模型请求；认证由 api_key 负责，会话头由宿主逐会话生成。
    model_custom_headers: dict[str, str] = field(default_factory=dict)
    model_session_header: str = ""
    # 仅由 /model 的可信 owner 解析填充；OAuth token 不进入运行快照，禁止手填跨用户路径。
    model_auth_ref: dict[str, str] = field(default_factory=dict)
    model_name: str = ""
    request_timeout: int = 240
    # 单槽位/繁忙模型的额外排队预算，仅加到首事件等待；0 不额外等待，健康流仍无总墙钟限制。
    model_queue_wait_seconds: float = 0.0
    max_tokens: int = DEFAULT_MODEL_MAX_TOKENS
    # 模型上下文窗口：空 = 用服务商元数据/探测，都拿不到按 128000；填数字 = 就用这个数（原 model_context_window_explicit 已并入）。
    model_context_window_tokens: int | None = None
    # 采样温度：空 = 不发送，用服务商默认；填数字 = 发送（原 model_temperature_explicit 已并入）。
    temperature: str | None = None
    # top_p 留空不覆盖普通模型；已核对的 DeepSeek V4 Flash 使用供应商采样默认。
    top_p: float | None = None
    # 智能程度（推理强度）全局默认档位 auto/off/low/medium/high/xhigh/max/ultra；会话 /effort 与子代理 effort 可覆盖。
    model_reasoning_effort: str = "auto"
    # 当前模型的思考控制方式 auto/effort/budget/none，通常由 /model 档案带入；auto 只对已核对供应商给默认，其余不发参数。
    model_reasoning_control: str = "auto"
    # 当前模型声明的服务商思考档位（如 low/medium/high/xhigh/max/ultra），通常由 /model 档案带入；
    # effort 协议只发声明内的值，留空表示未声明（Responses 沿通用 low/medium/high，Chat/Messages 沿原四档）。
    model_reasoning_levels: list[str] = field(default_factory=list)
    # /effort 设成 auto 以外的档位、而当前模型未声明思考控制方式且解析为不支持时，宿主是否在后台自动检测一次（9 次短请求）。
    reasoning_control_auto_probe: bool = True
    # 当前模型的结构化输出方式 auto/native/json_object，通常由 /model 档案带入；auto 只对已核对供应商改用 json_object。
    model_structured_output: str = "auto"
    # Anthropic-compatible 原生多轮工具请求是否写 cache_control 断点。仅影响 native
    # 工具循环；普通单次聊天不额外创建主动缓存，兼容端点不支持时可显式关闭。
    anthropic_prompt_cache_enabled: bool = True
    chat_history_max_turns: int = 20
    conversation_history_max_turns: int = 20
    conversation_history_max_chars: int = 48_000
    conversation_terminal_tool_fold_enabled: bool = True
    # 已结束工具回合在这段缓存热期内保留更完整的有界投影，过期后才切换为短折叠。
    # 0 表示立即使用短折叠；不同 provider 的缓存寿命应通过真实 usage 账本校准。
    conversation_terminal_tool_hot_tail_seconds: int = 300
    cli_audit_cleanup_days: int = 90
    config_warnings: list[str] = field(default_factory=list)
    config_path: str = ""
    config_sources: dict[str, dict[str, object]] = field(default_factory=dict)
    config_layers: list[dict[str, object]] = field(default_factory=list)

    def config_source_for(self, key: str) -> dict[str, object]:
        return dict(self.config_sources.get(key, {}))

    def config_source_snapshot(self) -> dict[str, object]:
        return {
            "schema_version": "agent_config_sources.v1",
            "config_path": self.config_path,
            "layers": list(self.config_layers),
            "sources": dict(self.config_sources),
        }


# LLM: 决策字段在原 YAML 读取后严格校验，不用宽松 coercion 把非法时间变成另一有效设置。
# 函数用途: 加载原部署配置与来源，拒绝无效决策字段，不创建 Agent 或发网络请求。
def load_config(config_path: str | Path) -> AgentConfig:

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = load_simple_yaml(path)
    from .decision_settings_defaults import validate_config_decision_fields

    raw.update(validate_config_decision_fields(raw, domain="agent"))

    # 先做类型验证和默认值归一（在过滤未知 key 之前）
    normalized, config_warnings = normalize_agent_config(raw)

    # 过滤未知字段。运行时诊断字段只由 loader 写入，不能从 YAML 注入。
    allowed = _public_config_keys()
    raw_keys = set(raw)
    unknown_keys = [key for key in raw_keys if key not in allowed]
    for key in unknown_keys:
        config_warnings.append(f"unknown config key: {key!r}; ignored")
    resolved_path = str(path.expanduser().resolve())
    effective = merge_agent_config_sources(
        config_cls=AgentConfig,
        normalized=normalized,
        raw_keys=raw_keys,
        path=path,
    )
    config = AgentConfig(**effective.values)
    config.config_path = resolved_path
    config.config_warnings = config_warnings
    config.config_sources = effective.sources
    config.config_layers = list(effective.layers)

    normalize_agent_memory_config(config)
    apply_log_level(config)
    return config


def _public_config_keys() -> set[str]:
    return public_config_keys(AgentConfig)


def apply_log_level(config: AgentConfig) -> None:
    from ..common.log_redaction import install_log_redaction

    install_log_redaction()
    level_name = str(getattr(config, "log_level", "info") or "info").strip().lower()
    logging.getLogger("agent_py_agent").setLevel(_LOG_LEVELS.get(level_name, logging.INFO))
