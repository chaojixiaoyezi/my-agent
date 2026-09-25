
from __future__ import annotations

# LLM: 默认值须与随包 YAML 一致；各决策点独立默认关闭，覆盖由原 owner/thread 保存，召回前建议和外部材料建议都不改变权限或终态。
# 模块用途: 定义并加载 Agent 配置，统一显式采样、人格、持续执行、插件管理与用户确认口径。
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
    DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS,
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
    home_lesson_auto_read_limit: int = 3
    home_lesson_stale_caveat_days: int = 7
    run_task_workspace_enabled: bool = True
    external_knowledge_index_file_name: str = "MY_AGENT_INDEX.md"
    external_knowledge_directory_roots: list[str] = field(default_factory=list)
    external_knowledge_api_sources: list[str] = field(default_factory=list)
    external_knowledge_database_sources: list[str] = field(default_factory=list)
    timezone: str = ""  # IANA 时区名(如 Asia/Shanghai、America/New_York);空=服务器本地(审计 #21)
    week_start: str = "monday"  # 周起始 locale:monday/sunday/saturday,影响"本周"范围计算


# LLM: 工具与显式插件管理的默认配置归此组；修改开关须同步 YAML、规范化和执行入口，不能用展示配置代替权限。
# 类用途: 保存工具行为与管理开关的默认值，构造本身不加载插件或执行工具。
@dataclass
class _ToolConfigFields:
    enable_tools: bool = True
    # 仅允许用户显式管理插件；未安装/启用时不加载插件或启动额外进程。
    enable_plugins: bool = True
    max_tool_rounds: int | None = None
    # 历史字段名保留配置兼容；语义是一次并发执行批次大小，不是丢弃同轮尾部调用。
    max_tool_calls_per_round: int | None = None
    # 单轮内并行执行工具的数量上限(EXEC-01):空值=代码默认 8;正数=上限;
    # 0=不限制(与 max_tool_rounds 显式 0 同约定)。任务属性可单任务覆盖。
    max_parallel_tool_calls: int | None = None
    # 模型输出格式偶发抖动（把工具调用写进正文/代码块/XML 标签）时，
    # 协议违规先给几次结构化修复机会再 break；1=只修一次就断（旧行为）。
    max_protocol_repairs: int = 2
    # 后台调度器的周期性孤儿 supervision(reconcile 兜底,零 LLM 成本):每隔此秒数巡查一次
    # "盯守死岗补建接管 + durable 复活 PENDING/PLANNING 停滞孤儿"。事件唤醒覆盖不了
    # 静默死亡(SIGKILL/断电不发 wake),靠这里捡回;0=关闭。
    orphan_supervision_interval_seconds: int = 60
    # 显式 /goal 与普通任务软收口后的续跑间隔；它不轮询或推动子代理。
    # 子代理只通过真实生命周期事件唤醒直接父级。0 时仍使用内置 180 秒兜底。
    continuation_reminder_seconds: int = 180
    # Todo 仍开放时，把 exact-id 收尾软提醒放进模型上下文；只提示模型在最终回复前
    # 自主核对，不自动打勾、不阻断最终回复，也不增加隐藏模型调用。
    task_progress_closeout_guidance_enabled: bool = True
    # 同一工具的明确网络/能力不可用回执去重后达到阈值，提示核对其它授权来源；
    # 命令非零、参数/状态/权限错误、取消与未知失败不计数。每工具一次，0=关闭。
    tool_failure_channel_hint_threshold: int = 2
    tool_agent_budget_window_seconds: int | None = None
    tool_agent_budget_max_calls: int | None = None
    tool_artifact_read_budget_window_seconds: int = 600
    tool_artifact_read_budget_max_chars: int = 240_000
    tool_output_externalize_min_chars: int = 200_000
    tool_output_preview_chars: int = 4_000
    tool_context_microcompact_keep_recent: int = 8
    tool_context_microcompact_min_chars: int = 1500
    tool_context_ptl_retry_max: int = 3
    tool_read_max_chars: int = 16_000
    tool_write_inline_max_chars: int = DEFAULT_TOOL_WRITE_INLINE_MAX_CHARS
    tool_list_max_entries: int = 200
    tool_search_max_matches: int = 50
    tool_web_max_chars: int = 100_000
    tool_http_timeout: int = 30
    path_access_mode: str = DEFAULT_PATH_ACCESS_MODE
    path_dangerous_roots: list[str] = field(default_factory=lambda: list(DEFAULT_DANGEROUS_PATH_ROOTS))
    access_mode: str = DEFAULT_COMMAND_ACCESS_MODE
    tool_shell_timeout: int = 240
    tool_shell_output_max_chars: int = 12_000
    stream_enabled: bool = True
    tool_catalog_limit: int = 80
    tool_catalog_mode: str = "compact"
    tool_catalog_offset: int = 0
    tool_catalog_categories: list[str] = field(default_factory=list)
    # Default prompt catalog stays compact: examples and long parameter notes
    # remain available through list_tools or the recommended-tool details.
    tool_catalog_include_examples: bool = False
    tool_catalog_entry_max_chars: int = 700
    tool_catalog_show_truncated_notice: bool = True
    # 渐进式披露:这些 category 仍注册，但不进初始模型 schema；通过 tool_search 加载。
    # 递归代理和持续目标控制属于主链，orchestration/goal 默认首轮直出；显式 allowed_tools 的
    # 结构化后台/runner profile 仍保持全量直出。[] 恢复全量。
    tool_catalog_deferred_categories: list[str] = field(
        default_factory=lambda: ["collaboration", "web", "vision", "meta", "mcp"]
    )
    tool_detail_max_chars: int = 4000
    # 推荐区和 tool_search 的检索容量；search 有 score>0 过滤，不相关不会凑数。
    tool_retrieval_limit: int = 12
    tool_vector_search_enabled: bool = True
    tool_embedding_model: str = ""
    tool_embedding_api_base: str = ""
    tool_embedding_api_key: str = ""
    tool_embedding_api_key_env: str = ""
    # 工具调用协议固定 native（EXEC-31b: text 协议已删除——对照组 会话运行时/轻量运行时/
    # 终端交互 均只有 native；不支持原生 tool_use 的模型直接报错，不做文本降级）。
    tool_protocol: str = "native"
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
    lsp_servers: dict[str, Any] = field(default_factory=dict)
    # 视觉理解(短板6)：辅助视觉模型配置,让 analyze_image 工具能看图(分析图片内容)。
    # my-agent 主模型不一定支持视觉,所以走独立的 anthropic_compatible 视觉端点:把图片转成
    # anthropic image block 调它返回分析。默认全空 = 未配视觉模型 → analyze_image 返回
    # TOOL_UNAVAILABLE(可选加法,零默认影响,不影响任何现有工具)。
    # vision_api_base + vision_model_name 都非空才算"已配";vision_api_key 空则复用主模型 api_key
    # (视觉模型与主模型常同源同 key);vision_anthropic_version 空则复用主模型 anthropic_version。
    vision_api_base: str = ""
    vision_api_key: str = ""
    vision_model_name: str = ""
    vision_anthropic_version: str = ""
    vision_request_timeout: int = 120
    vision_max_tokens: int = 1024


@dataclass
class _RuntimeBudgetConfigFields:
    contract_status_max_scan_files: int = 1000
    contract_status_max_report_bytes: int = 2_000_000
    contract_status_recent_findings_limit: int = 20
    skill_guard_max_files: int = 50
    skill_guard_max_size_kb: int = 1024
    small_real_acceptance_max_runtime_seconds: int = 900
    real_run_review_max_report_bytes: int = 5_000_000
    real_run_review_max_log_bytes: int = 1_000_000
    runner_auto_concurrency: int = 8
    conversation_thread_list_limit: int = 100
    conversation_pending_wake_limit: int = 100
    conversation_context_recent_limit: int = 20
    conversation_unhandled_observation_limit: int = 20
    background_pending_wake_prompt_limit: int = 20
    background_context_max_string_chars: int = 1200
    background_context_max_list_items: int = 20
    background_context_max_dict_items: int = 80
    background_context_max_depth: int = 6
    background_context_max_total_tokens: int = 8000
    background_claim_ttl_seconds: int = 90
    background_claim_heartbeat_interval_seconds: int = 0
    background_completion_coalesce_seconds: int = 5
    cli_resume_max_rounds: int = 8
    subagent_watch_interval_seconds: int = 120
    background_main_agent_allowed_tools: list[str] = field(default_factory=list)


# LLM: AgentConfig 拥有通用决策和实验能力默认；实验开关不授予额外请求许可，能力点归 CapabilityConfig，Memory 四点对齐 MemorySettings。
# 类用途: 汇总模型和运行配置；决策默认关闭，有限正时间只为后续请求提供默认，不管理阶段时钟。
@dataclass
class AgentConfig(_HomeProviderConfigFields, _ToolConfigFields, _RuntimeBudgetConfigFields):

    # 决策增强默认关闭；有限正秒数只作用后续请求，不重置已开始阶段的预算。
    decision_enabled: bool = False
    decision_experiment_enabled: bool = False
    decision_timeout_seconds: float = 2.0
    decision_stage_timeout_seconds: float = 4.0
    decision_background_timeout_seconds: float = 4.0
    decision_profile_id: str = ""
    decision_model_selection_mode: str = "off"
    decision_model_selection_timeout_seconds: float | None = None
    decision_model_selection_profile_id: str | None = None
    decision_external_material_order_mode: str = "off"
    decision_external_material_order_timeout_seconds: float | None = None
    decision_external_material_order_profile_id: str | None = None
    decision_planning_mode: str = "off"
    decision_planning_timeout_seconds: float | None = None
    decision_planning_profile_id: str | None = None
    decision_delivery_quality_mode: str = "off"
    decision_delivery_quality_timeout_seconds: float | None = None
    decision_delivery_quality_profile_id: str | None = None
    decision_action_candidate_mode: str = "off"
    decision_action_candidate_timeout_seconds: float | None = None
    decision_action_candidate_profile_id: str | None = None
    # Skill 提案审核顺序只排 CLI 展示、不授予 Skill/工具权限，且与 enable_self_learning 同属主配置，故不放 CapabilityConfig。
    decision_skill_proposal_review_mode: str = "off"
    decision_skill_proposal_review_timeout_seconds: float | None = None
    decision_skill_proposal_review_profile_id: str | None = None
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
    memory_decision_pre_recall_timeout_seconds: float | None = None
    memory_decision_pre_recall_profile_id: str | None = None
    memory_decision_recall_mode: str = "off"
    memory_decision_recall_timeout_seconds: float | None = None
    memory_decision_recall_profile_id: str | None = None
    memory_decision_curator_mode: str = "off"
    memory_decision_curator_timeout_seconds: float | None = None
    memory_decision_curator_profile_id: str | None = None
    memory_decision_curator_relation_mode: str = "off"
    memory_decision_curator_relation_timeout_seconds: float | None = None
    memory_decision_curator_relation_profile_id: str | None = None
    memory_top_k: int = 5
    auto_save_memory: bool = True
    # 记忆语义召回(检索拓宽 #1,默认关=现状纯关键词):开后记忆召回在关键词(FTS5/BM25)外再加一路
    # 语义向量召回,RRF 融合,治"换词就召不回"。向量只存各 owner 自己 home 的本地文件(零外部依赖、
    # 不碰共享向量库、per-用户隔离)。需配 memory_embedding_model(走 agent 同款 api);没配则自动只走
    # 关键词(不崩不退化)。
    memory_semantic_recall: bool = False
    memory_embedding_model: str = ""  # 语义召回用的 embedding 模型名(空=不启用语义,仅关键词);embo* 走 MiniMax 原生协议
    memory_embedding_api_base: str = ""  # embedding 端点 base(缺省沿用 api_base);MiniMax 填 https://api.minimaxi.com/v1
    memory_embedding_api_key: str = ""  # embedding 独立 key(直配);空→读 _env 或回退聊天 key。让 embedding 能用与聊天不同厂的 key
    memory_embedding_api_key_env: str = ""  # embedding key 的环境变量名(生产用,免把密钥写进 yaml);空→回退聊天 key
    memory_archive_level: int = 3
    memory_hook_enabled: bool = True
    memory_hook_archive_level: int = 3
    memory_hook_retention_days: int = 7
    memory_rule_routing_enabled: bool = True
    memory_rule_routing_mode: str = "soft"
    memory_rule_auto_read_limit: int = 3
    memory_rule_receipt_enabled: bool = True
    memory_resume_auto_context_enabled: bool = False
    memory_resume_auto_context_mode: str = "trigger"
    memory_resume_auto_context_limit: int = 5
    memory_compact_auto_trigger_percent: int = 90
    # 含图历史的压缩策略：auto 默认走归档引用（视觉能力事实接入后按事实选择随图摘要）；archived_refs 固定归档引用；off 保持从首个媒体回合起保护全部后缀。
    compact_media_policy: str = "auto"
    # 当前模型声明的输入模态（如 text、image、video），来自模型档案的 input_modalities；空表示未知，压缩策略会用一次结构化视觉探针判断，不按模型名猜。
    model_input_modalities: list[str] = field(default_factory=list)
    # 每个已知图块折进上下文估算的 token 数：预检/自动压缩判定按图块数乘以该值计入；随图摘要时也是每块在摘要预算里的预留。
    input_media_token_reserve: int = 1600
    # 随图摘要一次压缩最多发几次"看图"小请求；含图回合按摘要预算打包，超过次数的图块本次按归档引用（checkpoint 记 vision_digest_partial）。最小 1。
    compact_vision_digest_max_requests: int = 4
    # 到达触发线后优先把完整输入收敛到该占比；低于真实触发线的有效候选不会因未达目标而被丢弃。
    memory_compact_recovery_target_percent: int = 60
    # 后台 Memory Curator 只读有界经历并输出严格 daily/candidate JSON；它没有工具循环和写人格权限。
    memory_curator_enabled: bool = True
    memory_curator_provider: str = "auto"
    memory_curator_model: str = ""
    memory_curator_interval_seconds: int = 10_800
    memory_curator_turn_threshold: int = 10
    memory_curator_batch_message_limit: int = 80
    memory_curator_max_input_chars: int = 40_000
    memory_curator_timeout_seconds: int = 90
    memory_curator_max_retries: int = 1
    # 记忆策展使用独立后台车道；限制并发可避免历史 owner 积压抢占主会话/子代理唤醒线程。
    memory_curator_workers: int = 2
    memory_curator_daily_finalize_hour: int = 23
    memory_curator_auto_promotion_policy: str = "conservative_v1"
    memory_lesson_min_occurrences: int = 2
    memory_hot_min_occurrences: int = 3
    # 单次 run 内 compact→自动续跑的绝对深度硬顶（与 no-tool 软顶并存）。达到即强制 return，
    # 防止持续高于阈值且每轮都调工具的任务无限 compact/续跑（H2）。0 表示沿用内置默认。
    memory_compact_auto_continue_max_depth: int = 50
    # compact 续跑时对卸掉的中段历史做一次 LLM 语义摘要（短板6，长期助手 trajectory_compressor
    # 蓝本）：默认开，保护首尾、只摘要中段；异常、供应商超时或无 backend 时回退机械重建。
    # 摘要不另设不可取消线程超时，直接沿用统一模型传输超时。enabled=false 即完全关闭。
    memory_compact_semantic_summary_enabled: bool = True
    memory_compact_semantic_summary_protect_head: int = 2
    memory_compact_semantic_summary_protect_tail: int = 6
    memory_compact_semantic_summary_min_middle: int = 4
    memory_compact_semantic_summary_max_input_chars: int = 12000
    memory_artifact_default_read_chars: int = 4000
    memory_archive_preview_level_0_chars: int = 2048
    memory_archive_preview_level_1_chars: int = 1024
    memory_archive_preview_level_2_chars: int = 512
    memory_archive_preview_level_3_chars: int = 160
    memory_archive_summary_chars: int = 96
    memory_archive_search_file_limit: int = 30
    memory_query_default_limit: int = 100
    memory_query_default_page_size: int = 100
    memory_query_content_preview_chars: int = 500
    memory_resume_archive_scan_limit: int = 0
    memory_resume_recommended_read_paths_limit: int = 20
    memory_doctor_recent_archive_file_limit: int = 5
    memory_config_warnings: list[dict[str, Any]] = field(default_factory=list)
    local_store_path: str = ""
    local_store_files_dir: str = ""
    local_store_events_path: str = ""
    local_store_fts_enabled: bool = True
    prompt_files: list[str] = field(default_factory=lambda: ["builtin:prompts/default.md"])
    # 额外可写根（沙箱写边界扩展）：任务 work/output 之外的目录也允许模型写入。
    # 会话运行时 对应 sandbox_workspace_write.writable_roots；测试/用户可配置共享工作区。
    additional_write_roots: list[str] = field(default_factory=list)
    enable_subagents: bool = True
    subagent_mode: str = "trusted_local_hardening"
    # 当前根会话树可同时保留的未结束子代理数；不同 TUI/根任务互不占槽。
    max_subagents: int = 8
    subagent_board_limit: int = 5
    subagent_workspace: str = ""
    subagent_allowed_tools: list[str] = field(default_factory=list)
    subagent_role_template_dirs: list[str] = field(default_factory=list)
    task_max_subagents: int = 0
    task_max_grandchildren: int = 0
    subagent_spawn_default_count: int = 3
    subagent_cli_default_limit: int = 20
    subagent_probe_default_limit: int = 20
    subagent_hierarchy_default_max_depth: int = 0
    subagent_hierarchy_recovery_max_nodes: int = 200
    subagent_hierarchy_max_children_per_tool_call: int = 0
    subagent_descendant_scan_limit: int = 128
    subagent_takeover_chain_max_depth: int = 0
    subagent_context_summary_inline_json_chars: int = 900
    subagent_context_summary_inline_text_chars: int = 500
    subagent_debug_trace_level: int = 0
    subagent_memory_retention_policy: str = "parent_review_or_cleanup"
    subagent_memory_delete_after_days: int = 0
    subagent_destroy_summary_required: bool = True
    # 自学习默认关闭；开启后子代理 lesson 只生成待用户 CLI 确认的 Skill 提案，确认前不写正式 Skill。
    enable_self_learning: bool = False
    result_check_execute_tests: bool = False
    result_check_timeout_seconds: int = 120
    dynamic_timeout_safety_margin: float = 2.0
    # 主会话后台命令完成后进入既有持久唤醒队列；关闭后仍可主动查询或长等待。
    background_process_notifications: bool = True
    # 只在调用账本记录无正文请求摘要，不改变请求前缀；服务端缓存状态仍为未知。
    cache_diagnostics_enabled: bool = True
    dynamic_timeout_min: int = 30
    dynamic_timeout_max: int = 10800
    # 未取得稳定 probe 样本时的保守吞吐估计；只用于本次请求的首包/非流式预算。
    estimated_prefill_tokens_per_second: float = 200.0
    estimated_output_tokens_per_second: float = 20.0
    # 门槛4: probe 统计参数——每点最小样本数/滑窗上限/异常值去极值开关
    probe_min_samples: int = 2
    probe_window_samples: int = 5
    probe_outlier_trim: bool = True
    max_auto_split_depth: int = 2
    max_auto_retry_attempts: int = 3
    model_speed_profile_path: str = ""
    auto_bench_model_on_first_use: bool = True
    user_id: str = "admin"
    # 多租户鉴权配置
    auth_enabled: bool = True
    admin_user_id: str = "admin"
    scheduler_mode: str = "auto"
    runner_concurrency: str = "auto"
    runner_start_rate: str = "auto"
    runner_timeout_seconds: str = "off"
    runner_timeout_by_role: dict[str, object] = field(default_factory=dict)
    runner_failure_policy: str = "auto"
    gateway_workspace: str = ""
    gateway_heartbeat_interval: int = 5
    gateway_stale_seconds: int = 120
    gateway_stop_timeout: int = 20
    gateway_request_timeout: int = 300
    gateway_request_poll_interval: float = 0.2
    # 【已废弃,不再接线】原网关 ask 单层总闸(全局工位数),已被下面的两层限流取代;
    # 字段保留只为兼容存量配置文件不报错。
    gateway_request_workers: int = 10
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
    # 后台普通宿主异常的重试间隔；本地缺模型等待配置恢复，保留持久事件，不影响前台或其它会话。
    background_main_error_backoff_seconds: float = 30.0
    # 单 owner 同时可跑的独立后台会话数；调大会增加并发模型请求。
    background_threads_per_owner: int = 4
    # per-owner 作用域 agent 实例池上限(原 owner_scoped_pool.py 硬编码 64):有界 LRU,
    # 超出逐出最久未用;千并发多用户时的驻留 agent 数调参入口。
    owner_agent_pool_max_agents: int = 64
    # 空闲实例寿命；只回收无执行租用、无持久工作者，0 关闭，身份和会话不删除。
    owner_agent_idle_seconds: float = 60.0
    input_media_max_bytes: int = 16 * 1024 * 1024
    input_media_max_files: int = 8
    # 磁盘级 owner 唤醒发现间隔(秒,0=关):后台循环周期性扫 owners/ 把「有 enabled 进度
    # 策略/待处理唤醒信号」的 owner 种回活跃登记表。治网关重启/LRU 逐出后 scoped owner 的
    # 到点唤醒无人消费=盯守睡死(登记表是易失的进程内结构,只有新入站请求才补记)。
    background_owner_wake_rescan_seconds: int = 120
    # owner retention 扫描控制器每拍只处理一个有界页，不创建 Agent、不调用 LLM。
    # 每个 owner 的 retention.json 另有日级执行节流；0 关闭网关自动扫描。
    owner_maintenance_scan_interval_seconds: int = 60
    gateway_port: int = 8420
    # 多用户通道默认按 X-User-Id/channel 解析独立 owner；远程身份缺失或 owner 创建失败时
    # fail-closed，绝不回退共享 main。单机 CLI 无远程 provider 身份时仍使用 local main。
    gateway_per_user_owner_scoping: bool = True
    # 网关 HTTP 绑定地址:默认 loopback,仅本机可达。绑非 loopback(暴露到网络)时强制要求鉴权,
    # 否则 fail-closed 拒绝启动(防"绑 0.0.0.0 + 无鉴权 = 未认证远程命令执行")。默认仅监听回环地址。
    gateway_bind_host: str = "127.0.0.1"
    # 网关局部信任 token(暴露部署用):非空时,非回环来源须带匹配的 X-Gateway-Token 才被信任,
    # 否则降为匿名 USER。默认空=只靠回环 peer 信任(适配器/CLI 走 127.0.0.1)。
    gateway_auth_token: str = ""
    gateway_worker_join_timeout_seconds: int = 2
    gateway_ready_timeout_seconds: int = 3
    gateway_service_command_timeout_seconds: int = 30
    gateway_service_stop_timeout_seconds: int = 90
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
    # 协作请求默认截止时间。模型没有显式传 deadline_at/deadline_seconds 时，
    # 系统会给 request 自动补一个相对 deadline，避免大规模协作无限等全员。
    # 0 表示不自动补截止时间，只使用模型或用户显式给出的 deadline。
    collaboration_default_deadline_seconds: int = 120
    concurrency_lock_enabled: bool = True
    task_lock_timeout_seconds: int = 30
    audit_enabled: bool = True
    audit_log_path: str = ""
    daemon_planner: bool = True
    daemon_mutate_state: bool = True
    daemon_start_runners: bool = True
    daemon_interval: int = 30
    daemon_max_runners: str = "auto"
    daemon_limit: int = 0
    daemon_max_cycles: int = 0
    daemon_max_cards: int = 0
    daemon_probe: bool = True
    daemon_reviewer: str = "parent-daemon"
    daemon_runner_instruction: str = ""
    lease_heartbeat_interval_seconds: int = 60
    lease_stale_without_heartbeat_seconds: int = 300
    log_level: str = "info"
    extensions_dir: str = "extensions"
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
    model_context_window_tokens: int = 128_000
    # /model 保存的用户窗口为显式容量；默认部署仍保留原 provider metadata 优先策略。
    model_context_window_explicit: bool = False
    temperature: str = "0.2"
    # top_p 留空不覆盖普通模型；已核对的 DeepSeek V4 Flash 使用供应商采样默认。
    top_p: float | None = None
    # 三种接口均只发送显式温度；未启用沿用提供方默认，/model 填温度自动启用。
    model_temperature_explicit: bool = False
    anthropic_version: str = "2023-06-01"
    # Anthropic-compatible 原生多轮工具请求是否写 cache_control 断点。仅影响 native
    # 工具循环；普通单次聊天不额外创建主动缓存，兼容端点不支持时可显式关闭。
    anthropic_prompt_cache_enabled: bool = True
    chat_history_max_turns: int = 20
    chat_history_assistant_preview_chars: int = 500
    conversation_history_max_turns: int = 20
    conversation_history_max_chars: int = 48_000
    conversation_terminal_tool_fold_enabled: bool = True
    conversation_terminal_tool_fold_max_chars: int = 6_000
    # 已结束工具回合在这段缓存热期内保留更完整的有界投影，过期后才切换为短折叠。
    # 0 表示立即使用短折叠；不同 provider 的缓存寿命应通过真实 usage 账本校准。
    conversation_terminal_tool_hot_tail_seconds: int = 300
    chat_transcript_max_chars: int = 500_000
    chat_collapse_preview_lines: int = 12
    chat_collapse_preview_chars: int = 900
    chat_context_window_chars: int = 200_000
    chat_transcript_scroll_lines: int = 10
    cli_status_limit: int = 5
    cli_timeline_limit: int = 20
    cli_memory_list_limit: int = 20
    cli_memory_search_limit: int = 5
    cli_chat_memory_limit: int = 5
    cli_memory_archive_limit: int = 20
    cli_memory_route_limit: int = 5
    cli_local_search_limit: int = 5
    cli_local_search_preview_chars: int = 500
    cli_local_doctor_limit: int = 20
    cli_task_list_limit: int = 50
    cli_audit_limit: int = 100
    cli_audit_cleanup_days: int = 90
    # Dispatch 闭环保证配置
    dispatch_max_consecutive_rounds: int = 20
    dispatch_active_interval: int = 5
    dispatch_idle_interval: int = 30
    dispatch_default_max_runners: int = 1
    dispatch_default_limit: int = 20
    dispatch_default_watch_interval: float = 30.0
    dispatch_pending_runner_scan_limit: int = 999
    # Watchdog 配置
    watchdog_enabled: bool = False
    watchdog_interval: int = 60
    watchdog_max_restarts: int = 3
    watchdog_restart_delay: int = 10
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
