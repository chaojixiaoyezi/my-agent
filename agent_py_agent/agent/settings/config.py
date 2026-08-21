
from __future__ import annotations

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
    workspace_task_path_template: str = "tasks/{date}/{task_slug}"
    workspace_task_llm_title_enabled: bool = False
    workspace_task_llm_title_input_chars: int = 2_000
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


@dataclass
class _ToolConfigFields:
    enable_tools: bool = True
    max_tool_rounds: int | None = None
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
    # 派工出口的机制层监督提醒(治"说了登记提醒却没真调"实锤:owner store 的
    # progress_policies/ 为空=长窗口零定时唤醒,中途上报只能等完成事件):
    # create_subagents 成功后若该任务+线程没有 enabled 循环提醒,机制层自动按此间隔
    # 登记一条监督 policy(复用 wait 的收口退休/去重/无进展退避,不造 churn 回路);
    # 模型显式调过 wait 的不覆盖。0=关闭。
    dispatch_supervision_reminder_seconds: int = 180
    # 启动恢复自动调和崩溃卡死任务(审计 #18,opt-in 默认关):true=把"非终态但进程已退出"的崩溃任务
    # 自动转 ABANDONED(只改状态,进程已死不杀任何东西);默认 false=维持现有"检测+提示用户手动"策略不变。
    startup_auto_reconcile_crashed_tasks: bool = False
    # 检索完备性软引导(R5b/R6c 实锤:单一渠道失败即下"不存在"绝对结论):同一工具
    # 系统失败累计达此阈值时注入"枚举未试渠道再下结论"软提示(每工具一次);0=关闭。
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
    # 显式 allowed_tools 的结构化后台/runner profile 保持全量直出。[] 恢复全量。
    tool_catalog_deferred_categories: list[str] = field(
        default_factory=lambda: ["collaboration", "orchestration", "goal", "web", "vision", "meta", "mcp"]
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
    #                  default_effect: dangerous, tool_effects: {tool_name: read_only|mutating|dangerous}}}
    # 未声明 effect 的 MCP 工具默认 dangerous；只有部署配置可逐工具降低风险，server 自报不授权。
    # 默认空 = 不连任何 server、不起任何子进程(零开销)。仅 stdio 传输(JSON-RPC over stdio)。
    mcp_servers: dict[str, Any] = field(default_factory=dict)
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
    runner_auto_concurrency: int = 4
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


# LLM: AgentConfig is the public configuration authority; Memory Curator defaults must mirror YAML and MemorySettings.
# 类用途: 汇总主模型、工具、Gateway、Memory 和子代理运行时配置。
@dataclass
class AgentConfig(_HomeProviderConfigFields, _ToolConfigFields, _RuntimeBudgetConfigFields):

    agent_name: str = "myagent"
    system_prompt: str = (
        "你是 my-agent，一个自主的 CLI 智能体，用工具、多通道网关和子代理完成真实工程与运营任务。"
        "You are a coding agent. For coding, rewriting, or replication tasks: write a minimal "
        "working version (go.mod + main.go) in your first 1-2 turns, compile it immediately, "
        "then add features one by one and compile after each. Do not pre-read the entire source "
        "codebase; read a specific source file only when the feature you are writing needs it."
        "接到写代码/重写/复刻/建程序类任务时你就是编码 agent：开工 1-2 轮内就 write_file 写出能编译启动的"
        "最小骨架（主程序+依赖配置），马上编译验证；之后按清单逐块补齐、每块写完编译一次；卡在某个功能时才"
        "按需读对应源码，别先通读全部源码、别把源码逐段抄进笔记。\n"
        "沟通：尽量简短直接。除非用户要详细，否则别长篇、别加开场白和结尾总结、别堆能力清单、别用 emoji；"
        "能一两句说清就一两句。\n"
        "如实报告（最重要）：测试失败就贴失败输出；某步跳过了就说跳过；只做了一部分就说哪些没做完、卡在哪；"
        "做完且自己验证过了才说“完成”，直接说、不夸大不含糊；绝不拿旧的/局部的/没验证的结果冒充完整交付。\n"
        "别造轮子——先查你已有的能力：你是一个完整产品，不是从零写脚本的人。遇到“接通道/搭网关/做监控/"
        "调度/多用户”这类需求，先调 list_capabilities 查清自己有没有内置能力再动手；绝不自己搭外部服务、"
        "绝不把任务转给别的大模型——你就是那个大脑。\n"
"安全：破坏性、不可逆、对外发送类操作先确认；不做未授权或越界的事；如实报告你做了什么。\n"
        "工作方式：复杂任务先判断它的“形状”——要动多个文件/多个目标、能并行、或需要独立验证时，"
        "优先派子代理分头干、自己只收结论（别什么都自己埋头做，也别因为自觉“我能干”就不派）；"
        "已知单一改动点、一两步能完的就自己直接做。边做边按事实更新判断，失败了自省调参再试、别机械重复；"
        "改代码先看现有风格和约定、写出来要像周围的代码；只做被要求的，不多不少。"
    )
    workspace_root: str | list[str] = ""
    auto_detect_work_on_startup: bool = True
    model_backend: str = "echo"
    memory_path: str = ""
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
    memory_curator_daily_finalize_hour: int = 23
    memory_curator_auto_promotion_policy: str = "conservative_v1"
    memory_lesson_min_occurrences: int = 2
    memory_hot_min_occurrences: int = 3
    # 单次 run 内 compact→自动续跑的绝对深度硬顶（与 no-tool 软顶并存）。达到即强制 return，
    # 防止持续高于阈值且每轮都调工具的任务无限 compact/续跑（H2）。0 表示沿用内置默认。
    memory_compact_auto_continue_max_depth: int = 50
    # 普通 standalone 任务 task_progress 账本驱动自动续跑上限（2026-08-07 假 DONE 根修）：
    # 模型一轮工具调用后回中间汇报即被 terminal 收口的根修参数。0 表示沿用内置默认(3)；
    # 显式 >0 时覆盖。task_attributes["task_progress_continue_limit"] 优先于此。
    task_progress_auto_continue_limit: int = 0
    # compact 续跑时对卸掉的中段历史做一次 LLM 语义摘要（短板6，长期助手 trajectory_compressor
    # 蓝本）：默认开，保护首尾、只摘要中段；摘要失败/超时/无 backend 一律静默回退机械重建，
    # 不影响 compact/resume 正常路径与可恢复性。enabled=false 即完全关闭、走纯机械重建。
    memory_compact_semantic_summary_enabled: bool = True
    memory_compact_semantic_summary_protect_head: int = 2
    memory_compact_semantic_summary_protect_tail: int = 6
    memory_compact_semantic_summary_min_middle: int = 4
    memory_compact_semantic_summary_timeout_seconds: float = 20.0
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
    max_subagents: int = 6
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
    subagent_hierarchy_max_children_per_tool_call: int = 4
    subagent_descendant_scan_limit: int = 128
    subagent_takeover_chain_max_depth: int = 0
    subagent_context_summary_inline_json_chars: int = 900
    subagent_context_summary_inline_text_chars: int = 500
    subagent_debug_trace_level: int = 0
    subagent_memory_retention_policy: str = "parent_review_or_cleanup"
    subagent_memory_delete_after_days: int = 0
    subagent_destroy_summary_required: bool = True
    result_check_execute_tests: bool = False
    result_check_timeout_seconds: int = 120
    dynamic_timeout_safety_margin: float = 2.0
    dynamic_timeout_min: int = 30
    dynamic_timeout_max: int = 600
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
    gateway_request_max_attempts: int = 2
    # 后台 owner 整合 tick 线程池上限(原 cli/gateway_loops.py 硬编码 8):每个活跃 scoped
    # owner 的整合/唤醒 tick 独立线程,防一个卡死 turn 饿死其他 owner;超出排队下一轮。
    background_owner_workers: int = 8
    # per-owner 作用域 agent 实例池上限(原 owner_scoped_pool.py 硬编码 64):有界 LRU,
    # 超出逐出最久未用;千并发多用户时的驻留 agent 数调参入口。
    owner_agent_pool_max_agents: int = 64
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
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "AGENT_API_KEY"
    model_name: str = "gpt-4o-mini"
    request_timeout: int = 240
    max_tokens: int = DEFAULT_MODEL_MAX_TOKENS
    model_context_window_tokens: int = 128_000
    temperature: str = "0.2"
    anthropic_version: str = "2023-06-01"
    chat_history_max_turns: int = 20
    chat_history_assistant_preview_chars: int = 500
    conversation_history_max_turns: int = 20
    conversation_history_max_chars: int = 48_000
    conversation_history_message_max_chars: int = 12_000
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


def load_config(config_path: str | Path) -> AgentConfig:

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = load_simple_yaml(path)

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
