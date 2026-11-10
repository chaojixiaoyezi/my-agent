# LLM: 默认值须与随包 YAML 一致；压缩比例只保留 canonical 字段，不使用描述符、帧检查或赋值联动；旧来源须显式离线迁移。
#   显式后台回执开关只管新结构策略的可用性，不能重解释旧 always 通知或把供应商空响应当静默成功。
#   群引用附件开关沿原通道启动接线，只改变准确父消息资源读取，不授予原发送者身份或跨群文件权限。
#   host_home_snapshot只承运C构造期已解析home；host_only元数据与来源白名单共同排除用户参数，replace不得丢失该原事实。
# LLM: gateway_worker_process_count是宿主拓扑配置；严格校验非负整数，参数中心只读，不从请求推导或静默归零。
# 模块用途: 定义并加载 Agent 配置，统一显式采样、人格、工具反馈、插件管理与用户确认口径。
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

from ..path_access_policy import (
    DEFAULT_DANGEROUS_PATH_ROOTS,
    DEFAULT_PATH_ACCESS_MODE,
    configured_host_source_paths,
)
from ..user_space.home_root import ConfiguredHomeSnapshot
from .config_diagnostics import ConfigParseDiagnostic
from .config_io import format_config_parse_diagnostic, load_simple_yaml, parse_scalar
from .config_sources import (
    INTERNAL_RUNTIME_CONFIG_FIELDS,
    merge_agent_config_sources,
    public_config_keys,
)
from .defaults import (
    DEFAULT_COMMAND_ACCESS_MODE,
    DEFAULT_MODEL_MAX_TOKENS,
)
from .deployment_policy import RuntimeGuardPolicy
from .gateway_topology import validate_gateway_worker_process_count
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
    "自主决策：优先从当前目录、现有代码、用户约束和工具事实补齐信息；"
    "缺少次要实现选择时，不要停下来反问，采用合理默认并继续。"
    "只有缺失信息无法从上下文取得且合理假设会导致实质偏离、越权或不可逆风险时，"
    "才提一个简短关键问题；不要输出多选菜单来代替工作。"
    "任务规模大、耗时长或仅仅有可澄清之处，都不等于阻塞。"
)


# LLM: 持续执行规则服从结构化执行归属；问答可直接交付，执行任务按授权范围和风险验证，不以文本建立完成门或扩展 Goal。
# 配置用途: 共用持续工作与适度验证纪律，明确主子边界，避免把小问题扩成全面审计或重复核验。
_EXECUTION_PERSISTENCE_BODY = (
    "且现有工具、子代理或证据还能推进，就持续推进到实现、验证和清楚交付，不停在分析、骨架或未完成清单上。"
    "工具失败先读错误修正；仅目标端到端解决、用户暂停/改向或存在真实阻塞时才结束本轮。"
)
_EXECUTION_VERIFICATION_SUFFIX = (
    "的可见行为和端到端入口；关键路径失败修正后须重跑同一入口，"
    "不能把 `|| true`、`|| echo` 等忽略失败包装后的外层成功当成内部成功。"
    "有效测试不得仅为变绿而删除、跳过、放宽断言或改成只测存在；无法解决时如实报告。"
    "相关检查通过且无新失败时直接交付，不重复核验。"
)
DEFAULT_EXECUTION_PERSISTENCE = (
    "执行归属：普通回合负责当前用户请求；运行时存在 current_goal 时，本轮负责该目标的完整要求。"
    "每个代理最多一个未结束 Goal，历史目标不是新任务。主子代理目标各自独立，不能替未结束的下级宣布完成。"
    "执行纪律：只要本轮负责的目标仍有你已知的未完成部分，"
    + _EXECUTION_PERSISTENCE_BODY
    + "本轮自己创建的普通子代理只是分工，仍需接收结果、整合验证和交付；"
    + "验证纪律：验证必须覆盖本轮目标实际要求"
    + _EXECUTION_VERIFICATION_SUFFIX
)
DEFAULT_DELEGATED_EXECUTION_PERSISTENCE = (
    "执行纪律：只要直接父级当前 goal 仍有你已知的未完成部分，"
    + _EXECUTION_PERSISTENCE_BODY
    + "直接父级给你的当前 goal 是本轮完整工作边界；保留该 goal 的全部要求，"
    "但不要因为根用户目标更大而实现未交给你的兄弟计划项。"
    "不要停在阶段汇报或一份诚实的未完成清单；只显示欢迎信息的 demo 只能算该 goal 的阶段成果，不能替代该 goal 要求的完整功能。"
    "先在普通 assistant 回复中直接回答或确认用户。"
    + "验证纪律：验证必须覆盖当前 goal 实际要求，安装、构建、启动或关键路径失败时先修复再交付；"
    + _EXECUTION_VERIFICATION_SUFFIX
)


# LLM: 核心提示复用既有执行与验证合同；YAML须逐字同步，用户自定义提示仍原样覆盖，不把文案变成宿主控制门。
# 配置用途: 保留普通请求、独立Goal、委派工作和清单精确绑定的完整默认纪律。
DEFAULT_SYSTEM_PROMPT = (
    "你是 my-agent，一个使用当前授权工具完成真实任务的 CLI 智能体。 "
    + DEFAULT_DECISION_AUTONOMY
    + " "
    + DEFAULT_EXECUTION_PERSISTENCE
    + " 工作方式：先读取与当前目标直接相关的现有代码、约定和测试，再按可验证的小步实现；"
    "已有信息足够可靠时直接回答，不扩大搜索范围。"
    "证据：不编造操作、权限或结果。验证对应当前版本、输入和目标入口，局部或模拟结果不能外推完整链路；"
    "未验证就明确写未验证。"
    "安全：只读请求只做必要读取，修复/构建在授权内推进；主代理仅协调或只读的限制必须保留，不能转交后扩权。"
    "工具可用、角色或 Full Access 不等于新增业务授权；范围外高风险、不可逆及对外动作先确认，"
    "不重复索要已有授权；宿主审批、专用确认和 owner 隔离始终有效。"
    "持续目标：仅用户或直接父级明确要求时建立 Goal，普通任务不自动建立 Goal；已有目标用 update_goal 修订。"
    "任务管理：主子代理可按复杂度自行选择用 task_progress 建清单或直接执行。使用清单时沿用原 id；"
    "委派现有开放项时，create_subagents 对应 item 必须原样复制该项 id 到 covers，不能拿无关 id 顶替。"
    "绑定 child 完成后按原项核对；未绑定 child 不会关闭原 Todo，父级须按证据更新原项。"
    "仍有可推进工作时继续；清单只是模型自查，不是宿主机器验收。"
    "沟通：用用户的语言简短直接回答，不加空洞开场结尾；失败、跳过、未完成和阻塞如实说明。"
)


# LLM: owner/home开关是提示准备的唯一默认源；C内部home快照由来源白名单及host_only元数据共同隔离，不进入参数中心，不改变owner授权。
# 类用途: 保存家目录与人格上下文开关，须与YAML及HomeFieldsService布尔解析同步。
@dataclass
class _HomeProviderConfigFields:
    my_agent_home: str = ""
    host_home_snapshot: ConfiguredHomeSnapshot | None = field(
        default=None, repr=False, compare=False, metadata={"host_only": True})
    # 宿主构造绑定的只读运行值；配置投影保留对象，不接受用户输入或写回配置。
    runtime_guard_policy: RuntimeGuardPolicy | None = field(
        default=None, repr=False, compare=False, metadata={"host_only": True})
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
    thread_prompt_prefix_freeze_enabled: bool = True
    # 业务空间默认可用；显式关闭仍拒绝业务操作，开启不授予空间或插件权限。
    business_spaces_enabled: bool = field(default=True, kw_only=True)
    # 运行时快照投影默认开启；关闭恢复旧粗粒度文本，消费方须按单次请求冻结值。
    prompt_runtime_snapshots_enabled: bool = field(default=True, kw_only=True)
    # 显式来源允许读取用户选择的规则；仍服从当前owner路径许可，不替代正式修改确认。
    agents_source_enabled: bool = True
    # URL加载会发网络请求，默认关闭；只能用户开启且不能扩大owner已有网络权限。
    agents_source_network_enabled: bool = False
    home_lesson_stale_caveat_days: int = 7
    run_task_workspace_enabled: bool = True
    timezone: str = ""  # IANA 时区名(如 Asia/Shanghai、America/New_York);空=服务器本地(审计 #21)
    # 新 IM 定时计划默认只记录内部结果；明确提醒/报告可在 job/rule 的 delivery.mode 设 origin。
    scheduler_im_default_delivery_mode: str = "local"
    week_start: str = "monday"  # 周起始 locale:monday/sunday/saturday,影响"本周"范围计算


# LLM: 工具、插件与 B5 征询预算默认归此组；超时同步 YAML/范围/显式参数边界并由执行入口消费，不用展示配置代替权限。
#   老格式新授权默认收紧，开关不能降级已收紧激活；老格式边界与 v8 事件开关同处登记，联测 YAML 与参数边界。
# 类用途: 保存工具行为与可选诊断的默认值，构造本身不加载插件或执行工具。
@dataclass
class _ToolConfigFields:
    enable_tools: bool = True
    # 仅允许用户显式管理插件；未安装/启用时不加载插件或启动额外进程。
    enable_plugins: bool = True
    # 插件进程 OS 沙箱试点：开启后插件进程只可写自己的数据目录（读范围与网络不变）；沙箱不可用则不启动插件。
    plugin_process_sandbox: bool = False
    # B5 收紧征询的单次总预算，包含排队/启动/换代；用户可配 200–10000 毫秒，模型不能修改。
    plugin_tool_gate_timeout_ms: int = 2000
    # M 线事件插件总开关（B7）：默认关。关着时 v8 事件/收紧插件可以安装但不能启用（plugin_events_disabled，提示去 /settings）；
    # 已启用的在关掉后事件不投、收紧不问。开了才允许启用 v8、投事件、问收紧钩子。属管理员边界项，模型经 user_config 改会被拒。
    plugin_events_enabled: bool = False
    # 老格式新授权默认收紧；管理员关闭只影响以后授权且仍需宽权限确认，不降级已收紧的固定激活。
    plugin_legacy_sandbox_default: bool = True
    # 能力包 MCP 是独立安全边界，默认关闭；与事件插件开关分离，只能用户在 /settings 修改。
    capability_pack_mcp_enabled: bool = False
    # 包服务空闲后的停止秒数；0 表示永不因空闲停止，修改后重启 Gateway。
    capability_pack_mcp_idle_stop_seconds: int = 600
    max_tool_rounds: int | None = None
    # 一次最多同时执行几个工具（原 max_tool_calls_per_round 已并入）：空 = 8，正数 = 上限，0 = 不限制；
    # 任务属性 max_parallel_tool_calls 可单任务覆盖，后台工作片另有 max_tool_calls_per_round 任务属性封顶。
    max_parallel_tool_calls: int | None = None
    # 同owner在本进程内共享的工具worker额度；0保持原行为，任务属性不能覆盖。
    max_parallel_tool_calls_per_owner: int = 0
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
    # 10-08 从 200000 降到 32000（约 8k 英文 token）：参考 Codex 每条工具输出 1 万 token、DeepSeek Harness 8192 字符的上限；
    # 之前每轮上下文被工具结果撑到约 20 万 token。全文仍存归档，模型看预览加引用。
    tool_output_externalize_min_chars: int = 32_000
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
    # SP1-W2：前台命令树的数值降权策略；0秒关闭，构造/replace与YAML共用严格范围。
    tool_shell_demote_after_seconds: int = field(default=1, metadata={"bounds": (0, 3600), "unit": "秒"})
    tool_shell_demoted_nice: int = field(default=15, metadata={"bounds": (0, 19)})
    # PTY 会话空闲兜底回收阈值（分钟，0=不限制）：分页器等按键会让命令永不退出，超过阈值由宿主自动收回。
    pty_session_idle_timeout_minutes: int = 30
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
    # 完整目录和示例可经 tool_search(action="catalog") 或推荐工具详情查看。
    tool_catalog_include_examples: bool = False
    # 渐进式披露:这些 category 仍注册，但不进初始模型 schema；通过 tool_search 加载。
    # 递归代理和持续目标控制属于主链，orchestration/goal 默认首轮直出；显式 allowed_tools 的
    # 结构化后台/runner profile 仍保持全量直出。[] 恢复全量。
    tool_catalog_deferred_categories: list[str] = field(
        default_factory=lambda: ["collaboration", "web", "vision", "meta", "mcp"]
    )
    # 工具在自己规格里声明的“默认收起”（又大又少用的工具）是否生效；默认开（2026-10-02 用户授权、生产开启三天
    # 无异常），前台回合不再每轮发它们的 Schema，目录末尾留“名字：一句用途”的索引，用到时 tool_search 一步加载；
    # 关回 false 即恢复全量直出。显式 allowed_tools 的回合不受影响。
    tool_default_deferral_enabled: bool = True
    # 打开后 tool_search/盲调补表不在缓存段内改原生工具表（换工具表会让整段提示缓存作废），新工具定义随工具结果给出、
    # 模型按名调用，下次 Compact 时并入原生工具表；默认关，验收通过后再改默认值。
    tool_discovery_cache_pin_enabled: bool = False
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
    # always）。档位由本项与 computer_use_enabled 组合决定：总开关关 + 本项开 = 只看档（只交出 observe_window，
    # 适配器不加载上游执行器依赖）；两者都开 = 完整档。任何档位仍受 local/main 与 Full Access 约束。
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


# LLM: 用户默认同步随包配置；连接只作 host_only 目录投影，不成为 YAML/owner/task 输入；媒体及群附件授权不变。
# 类用途: 汇总模型和运行配置；沟通指导可关闭，图片外发有独立开关，有限正时间只提供后续请求默认，不管理阶段时钟。
# LLM: 宿主旧拓扑输入固定为0，__post_init__与YAML规范化均不再允许用户配置开启多进程。
# 类用途: 保存同一配置权威的运行参数和内网白名单；部署拓扑不能由模型或用户会话改写。
@dataclass
class AgentConfig(_HomeProviderConfigFields, _ToolConfigFields, _RuntimeBudgetConfigFields):

    # 只供宿主冻结 SecretRef 文件来源；不接受 YAML、owner/task 参数，也不进入参数中心。
    host_exclusive_path_roots: tuple[str, ...] = field(default=(), kw_only=True, repr=False,
                                                     metadata={"host_only": True})

    # PARAM-W0：范围元数据由构造校验、YAML 归一和登记表共用；model_read_only 不授予部署修改权限。
    gateway_http_listen_backlog: int = field(default=128, metadata={"bounds": (1, 65535), "unit": "个连接", "model_read_only": True})
    gateway_http_max_request_workers: int = field(default=32, metadata={"bounds": (1, 1024), "unit": "个线程", "model_read_only": True})
    gateway_http_max_outstanding_requests: int = field(default=256, metadata={"bounds": (1, None), "range": "≥gateway_http_max_request_workers", "unit": "次", "model_read_only": True})
    gateway_http_control_lane_workers: int = field(default=10, metadata={"bounds": (0, 128), "unit": "个线程"})
    gateway_native_stream_max: int = field(default=128, metadata={"bounds": (0, 4096), "unit": "条流"})
    # 事件中心与其兜底轮询默认关闭；数值范围由构造、归一与登记表共享。
    gateway_event_hub_ring_events: int = field(default=0, metadata={"bounds": (0, 65536), "clamp": True})
    gateway_event_fallback_poll_seconds: float = field(default=0.0, metadata={"bounds": (0.0, 60.0), "clamp": True})
    gateway_http_socket_timeout_seconds: float = field(default=5.0, metadata={"bounds": (0.1, 300.0), "model_read_only": True})
    gateway_http_queue_timeout_seconds: float = field(default=2.0, metadata={"bounds": (0.01, 300.0), "model_read_only": True})
    gateway_http_birth_gate_timeout_seconds: float = field(default=1.0, metadata={"bounds": (0.01, 30.0), "model_read_only": True})
    curator_daily_quota: int = field(default=5000, metadata={"bounds": (0, None), "unit": "次/日", "model_read_only": True})
    runner_role_limits: dict[str, int] = field(default_factory=dict, metadata={"range": "dict[str,int]，值≥0，拒绝bool", "unit": "个/批次"})
    gateway_submission_timeout_cap_seconds: float = field(default=30.0, metadata={"bounds": (0.1, 300.0)})
    retention_scan_slice_entries: int = field(default=128, metadata={"bounds": (1, None), "unit": "条", "model_read_only": True})
    retention_scan_slice_bytes: int = field(default=8388608, metadata={"bounds": (1024, None), "model_read_only": True})
    retention_scan_slice_ms: int = field(default=25, metadata={"bounds": (1, None), "model_read_only": True})
    memory_reference_pin_chunk_bytes: int = field(default=65536, metadata={"bounds": (1024, None), "model_read_only": True})
    reasoning_probe_max_output_tokens: int = field(default=40000, metadata={"bounds": (1, None)})
    reasoning_probe_timeout_seconds: float = field(default=180.0, metadata={"bounds": (0.0, None), "exclusive_minimum": True, "range": ">0"})

    # LLM: 构造/replace共用字段范围与联合校验；宿主拓扑严格拒绝非法类型与负数，不改写合法数量。
    # 函数用途: 校验集中参数、宿主拓扑并清洗私网名单，不读盘或启动服务。
    def __post_init__(self) -> None:
        from .services._normalize import validate_central_parameters

        validate_central_parameters(self)
        if self.tui_default_verbose_level not in {"off", "on", "full", "stream"}:
            raise ValueError("tui_default_verbose_level 必须是 off/on/full/stream")
        validate_gateway_worker_process_count(self.gateway_worker_process_count)
        if type(self.max_background_agents) is not int or self.max_background_agents < 0:
            raise ValueError("max_background_agents 必须是非负整数（0 表示不限制）")
        self.host_exclusive_path_roots = tuple(str(path) for path in configured_host_source_paths(self))
        values = self.allowed_private_hosts
        self.allowed_private_hosts = (
            [item.strip() for item in values if isinstance(item, str) and item.strip()]
            if isinstance(values, (list, tuple, set, frozenset))
            else []
        )

    allowed_private_hosts: list[str] = field(default_factory=list)

    # 决策点诊断记录（被挡下的跳过行 + 到达/未触发原因计数）：关闭时既不在结果日志记 skipped（原因码、无正文），
    # 也不累计 decision_reach_counts 的到达/未触发原因计数。
    decision_skip_records_enabled: bool = True
    # 选模型决策请求里对话摘要（只带语义部分，不带原文锚点段）与当前消息的字符上限；0 表示不截断，截断时如实标注
    # 规划/交付质量/动作候选三个决策点位发给决策模型的当前请求字数预算：更长时取首尾节选并标注；0 表示不截取
    decision_request_max_chars: int = 2000
    agent_name: str = "myagent"
    # 默认沿 终端交互 主链由 TUI 接管滚轮、点击与应用内选区；F6 仍可临时退回宿主终端原生复制。
    # 关闭后备用屏幕收不到物理滚轮，历史只能用 PgUp/Ctrl+Home，因此不再作为开箱默认。
    tui_mouse_capture_default: bool = True
    tui_agent_view_cache_max_count: int = field(default=32, metadata={"bounds": (1, None), "unit": "个"})
    # 每个 TUI 展示 runtime 保留并渲染的已冻结块上限；渲染缓存随之有界，完整历史仍在会话存储中。
    tui_transcript_max_blocks: int = field(default=500, metadata={"bounds": (1, None), "unit": "个"})
    # TUI 完全空闲（无前台/后台任务、后台进程与观察页）时通知、名册、路由与对账线程的最长等待；任何变化或按键立即恢复原节奏。
    tui_idle_poll_max_seconds: float = field(default=30.0, metadata={"bounds": (8.0, 300.0), "unit": "秒"})
    # Gateway 只复用已验证空的普通canonical消息页；不依赖事件中心，其它权限/活动/事件仍逐请求重核，0关闭。
    gateway_idle_notice_reuse_seconds: float = field(default=120.0, metadata={"bounds": (0.0, 3600.0), "unit": "秒"})
    # durable后台派工进程诊断stdout/stderr最多保留的字节；超限继续排空并记录真实丢弃，0显式不限制。
    background_dispatch_log_max_bytes: int = field(default=4194304, metadata={"bounds": (0, 1073741824), "unit": "字节"})
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # 只给普通主会话追加执行中沟通软引导，不额外采样、不改变历史或执行终态。
    assistant_progress_updates_enabled: bool = True
    workspace_root: str | list[str] = ""
    # 未配置时允许打开 Gateway/TUI 和 /model，但不选厂商、不调用模型；echo 仅供显式离线调试。
    model_backend: str = field(default="", metadata={"host_only": True, "model_projection": True})
    memory_path: str = ""
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
    # 四层策略的管理员默认；用户/会话覆盖另存，强制开关仅由管理员全局来源设置。
    memory_compact_trigger_percent: int = 80
    force_compact_trigger_percent: bool = False
    # 闲置压缩：任一为0即关闭；百分比0..95，启用后的最短静默时间60秒。
    memory_compact_idle_trigger_percent: int = 60
    memory_compact_idle_seconds: int = 7200
    # 压缩摘要等待期间重报真实已覆盖进度的间隔；0关闭，不增加模型调用。
    memory_compact_progress_heartbeat_seconds: float = field(default=5.0, metadata={"bounds": (0.0, 300.0), "unit": "秒"})
    # 每个维护拍最多检查的会话数；0关闭该维护拍，不扫描或迁移线程。
    memory_compact_idle_max_per_tick: int = 2
    # 自动压缩触发线的绝对 token 上限，与上面的百分比取小；0 表示不封顶（默认，行为不变）。1M 这类大窗口下百分比最低只到 50%，靠它把压缩提前。
    memory_compact_auto_trigger_max_tokens: int = 300_000
    # 含图历史的压缩策略：auto 默认走归档引用（视觉能力事实接入后按事实选择随图摘要）；archived_refs 固定归档引用；off 保持从首个媒体回合起保护全部后缀。
    compact_media_policy: str = "auto"
    # 当前模型声明的输入模态（如 text、image、video），来自模型档案的 input_modalities；空表示未知，压缩策略会用一次结构化视觉探针判断，不按模型名猜。
    model_input_modalities: list[str] = field(default_factory=list, metadata={"host_only": True, "model_projection": True})
    # 压缩摘要末尾“原话备份”（用户原话优先、最新优先，放不下的那条保留头尾）最多占多少 token；实际上限还不超过模型窗口的 10%。0 表示不附原话，只保留被省略消息的编号与回查说明。
    compact_landmark_max_tokens: int = 20000
    # 压缩摘要末尾是否告诉模型：被压缩的原话仍在会话记录里，可用 session_search 按 message_id 分段读回（本 agent 注册了 session_search 时才附）。
    compact_recall_hint_enabled: bool = True
    # 到达触发线后优先把完整输入收敛到该占比；低于真实触发线的有效候选不会因未达目标而被丢弃。
    memory_compact_recovery_target_percent: int = 60
    # 压缩摘要请求的输出上限（含思考），0 = 沿用主请求的输出上限。预算与实际发送同源（call_runtime.compact_summary_output_reserve_tokens）：
    # 27.2 万窗口按 90% 触发时，单次缓存面请求（窗口 − 本值）要装得下才不会退到不复用缓存的分段路径。
    # 只对压缩调用能降档的后端生效（GPT-6 Responses 走 configuration_update）；DeepSeek/MiniMax 的压缩调用仍按线程档位推理，
    # 推理 token 算进上限，16384 会截断摘要（2026-10-08 生产事故），它们沿用主请求上限。
    memory_compact_summary_max_output_tokens: int = 16_384
    # 压缩摘要调用的思考档位，空 = 沿用会话档位。只在后端能用 configuration_update 项降档时生效（GPT-6 系列 Responses 接口），
    # 请求级 reasoning.effort 不变、前缀缓存不失配；其它后端仍沿用会话档位。
    memory_compact_reasoning_level: str = "low"
    # 服务端压缩开关：后端声明支持时（目前是 ChatGPT 订阅登录的 Responses 接口）压缩改为让服务端返回压缩项，不再发摘要请求；
    # 关掉或后端不支持都走客户端摘要。换到读不了压缩项的后端时程序自动从归档原文重新压缩（conversation/compact_remote）。
    memory_compact_remote_enabled: bool = True
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
    # LLM: 仅控制已合法触发批次的候选尝试，不签正式发布或候选only派发；与YAML/Curator/原确认末门同步。
    # 字段用途: conservative_v1保留结构化候选处理回执，manual_only跳过；两者正式提交均需用户确认。
    memory_curator_auto_promotion_policy: str = "conservative_v1"
    memory_lesson_min_occurrences: int = 2
    memory_hot_min_occurrences: int = 3
    # 用户明确引用且本轮成功读取的文件摘录上限；0关闭关联，不改变消息学习资格。
    memory_curator_task_file_evidence_max_chars: int = 4000
    # 相同指令跨线程广播的去重窗口秒数；0关闭，不折叠单线程重复。
    memory_curator_cross_thread_dedupe_window_seconds: int = 600
    # 正式规则更新仅追加到其它线程请求尾部，冻结系统记忆前缀不变。
    memory_rule_notices_enabled: bool = True
    memory_rule_notices_max_chars: int = 1200
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
    # 管理员配置的每Owner非退役后台主Agent身份上限；0不限，调低不终止已有任务。
    max_background_agents: int = 8
    subagent_workspace: str = ""
    subagent_allowed_tools: list[str] = field(default_factory=list)
    subagent_role_template_dirs: list[str] = field(default_factory=list)
    task_max_subagents: int = 0
    subagent_hierarchy_max_children_per_tool_call: int = 0
    subagent_takeover_chain_max_depth: int = 0
    subagent_debug_trace_level: int = 0
    # 自学习默认关闭；开启后只生成待确认候选，主代理与子代理正式内容都须用户显式确认。
    enable_self_learning: bool = False
    # 只读自我目录沿原owner/run与公开模块读口聚合，关闭时不注册工具或注入使用指引。
    enable_self_map: bool = True
    # 自动总结 Skill 的触发与预算：工具轮数门槛、每 owner 每日模型调用上限（0 不限）、自学 Skill 数量上限（0 不限，只限新建）、单次调用超时秒数。
    self_learning_min_tool_rounds: int = 6
    self_learning_daily_limit: int = 20
    # 同一部署全部owner自学模型调用的UTC日总上限，0不限。
    self_learning_global_daily_limit: int = 2000
    self_learning_max_skills: int = 0
    # 常用推荐数量不限制保存；维护默认只观察，显式autonomous才冷藏/归档。
    self_learning_recommended_limit: int = 50
    self_learning_maintenance_mode: str = "observe"
    self_learning_cool_seconds: int = 2592000
    self_learning_archive_seconds: int = 15552000
    # 后台/定时任务完成也可贡献学习请求；默认关闭，开启仍受同owner日预算。
    self_learning_include_background: bool = False
    # 只清理无固定/在途/监控引用的旧修订；0不限，当前激活版永保留。
    self_learning_keep_versions: int = 5
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
    # 仅保留旧拓扑输入；S2-W6 启动迁移后按单进程运行，校验器仍接受合法旧值。
    gateway_worker_process_count: int = 0
    gateway_workspace: str = ""
    gateway_stop_timeout: int = 20
    gateway_request_timeout: int = 300
    # 网关 ask 两层限流(取代原「全局总 10」单层总闸,接真实 /ask 链路):
    # 每用户「小坑」=单用户同时在飞上限(防一个用户独吞把别人饿死);
    # 全局「大坑」=总在飞上限(高天花板,超出留在 pending 排队、不拒不崩)。
    # 都是先设的限制值(不是吞吐目标),按机器/模型承载力调。执行线程按需起、用后驻留复用。
    gateway_user_inflight_limit: int = 128
    gateway_global_inflight_limit: int = 500
    # Gateway同时在途的不同Owner数量上限；0跟随owner_agent_pool_max_agents，实际准入由Gateway消费。
    gateway_owner_inflight_limit: int = 0
    # 显式诊断：带GIL时追踪分配；free-threaded仅输出RSS/GC/池计数及追踪不可用原因，默认关闭。
    enable_gateway_memory_diagnostics: bool = False
    gateway_processing_timeout_seconds: int = 900
    # 仅计 processing 租约失效的失败次数；服务重启续接不计，0 不限，副作用未知仍禁止盲目重放。
    gateway_request_max_attempts: int = 2
    # 默认开启单次 Gateway 请求线程内有界 SQLite 复用；结束回收、及时提交不变，显式 False 可关闭。
    sqlite_connection_reuse: bool = True
    # 默认开启后台只读内容投影，实际领取与写入仍读取原权威；显式 False 可关闭投影优化。
    background_tick_cheap_checks: bool = True
    # 后台主代理车道对无待办信号（无到期任务/唤醒/进展汇报/待处理观测）的空闲 owner 采用指数退避检查的最大间隔秒数（0=不退避）。
    background_idle_owner_max_backoff_seconds: float = 10.0
    # 前台用户主动发新消息时若存在上一轮中断遗留的 unknown 副作用操作，默认自动登记为待核实并放行本轮（注入结构化核实提示）；False 则保留手动 /recover 硬拦。
    foreground_unknown_auto_recover: bool = True
    # Full Access下其他owner视图：read_only只读，isolate隐藏；合并不含当前owner的兄弟容器，保留本owner原H2/H3保护。
    sandbox_full_access_other_owner_homes: str = "read_only"
    # 供应商临时故障的回合级自动续跑次数上限（0=关闭；跨 Gateway 重启累计，重排不重放工具）。
    provider_transient_turn_resume_max_count: int = 2
    # 后台会话全局线程池上限，默认128；超出留在持久队列，同thread仍由run claim单飞。
    background_owner_workers: int = 128
    # 普通用户每个owner同时运行的后台会话上限，默认2；仍受全局线程池约束。
    background_threads_per_owner: int = 2
    # 管理员local/main同时运行的后台会话上限，默认64；不改变普通用户上限。
    background_admin_threads_per_owner: int = 64
    # per-owner 作用域 agent 实例池上限(原 owner_scoped_pool.py 硬编码 64):有界 LRU,
    # 超出逐出最久未用;千并发多用户时的驻留 agent 数调参入口。
    owner_agent_pool_max_agents: int = 128
    # 空闲实例寿命；只回收无执行租用、无持久工作者，0 关闭，身份和会话不删除。
    owner_agent_idle_seconds: float = 60.0
    # 代次戳始终记录；仅终态拒绝策略热切换，会话 Actor 数量则在启动时读取。
    owner_epoch_fence_enabled: bool = field(default=False, metadata={"range": "true|false", "effect": "immediate"})
    conversation_actor_workers: int = field(default=0, metadata={"bounds": (0, 64), "clamp": True})
    input_media_max_bytes: int = 16 * 1024 * 1024
    input_media_max_files: int = 8
    # 飞书原生图片进入模型前的逐帧累计像素上限；先检查后解码，不按扩展名猜图片。
    input_image_max_pixels: int = 40_000_000
    # owner retention 扫描控制器每拍只处理一个有界页，不创建 Agent、不调用 LLM。
    # 每个 owner 的 retention.json 另有日级执行节流；0 关闭网关自动扫描。
    owner_maintenance_scan_interval_seconds: int = 60
    # 常开定时器以外的磁盘发现兜底；默认保持原120秒节奏。
    background_owner_wake_rescan_seconds: int = field(default=120, metadata={"bounds": (30, 86400), "clamp": True})
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
    # 否则降为匿名 USER。默认空时使用持久 G1 本机凭据，回环地址本身不授予身份。
    gateway_auth_token: str = ""
    gateway_ready_timeout_seconds: int = 3
    # 安全重启第一段：停领新请求后，等本进程在跑回合结束的上限秒数；超时后关闭工具关口，停在工具前的回合由接班进程续跑。
    gateway_restart_turn_wait_seconds: int = 300
    # 安全重启第二段：等执行中的副作用工具归零的上限秒数，0=不限；超时后按 gateway_restart_force_on_drain_timeout 决定强制换进程或取消。
    gateway_restart_drain_timeout_seconds: int = 600
    # 安全重启第二段等待超时后是否仍强制完成交接重启（默认 True，避免单个卡死工具无限期阻断每日维护重启；新进程启动恢复会自动核销未完成操作）。
    gateway_restart_force_on_drain_timeout: bool = True
    # 两次安全重启之间的最短间隔秒数，0=不限；间隔内的请求直接返回冷却中。
    gateway_restart_cooldown_seconds: int = 30
    adapter_workspace: str = ""
    # 飞书适配器配置
    # metadata 声明这四项实际经 SecretRef 解析；环境上界只读取这些来源，不能遍历 prompt 等自然语言字段。
    feishu_app_id: str = field(default="", metadata={"secret_ref": True})
    feishu_app_secret: str = field(default="", metadata={"secret_ref": True})
    feishu_verification_token: str = field(default="", metadata={"secret_ref": True})
    feishu_encrypt_key: str = field(default="", metadata={"secret_ref": True})
    feishu_callback_port: int = 8421
    feishu_connection_mode: str = "long_connection"  # 默认长连接:免公网且支持密码/确认卡片回调；webhook 可显式选择
    feishu_ws_proxy: str = ""  # 长连可选代理(空=走 HTTPS_PROXY 环境变量;TUN/代理环境直连飞书 WS 网关是黑洞,需显式填代理)
    # 群聊只在直接 @机器人时触发；@全员不触发。
    feishu_group_require_bot_mention: bool = True
    # 下载并核验的当前/引用图片作为真实附件交给当前模型；关闭仅保留原附件路径行为，不自动换模型。
    feishu_image_input_enabled: bool = True
    # 在私聊或群里引用文件并触发机器人时，按准确父消息读取同聊天附件；需要飞书消息和资源读取权限。
    feishu_quoted_media_enabled: bool = True
    # 明确引用同聊天文字并触发机器人时，只把正文作为外部资料；当前引用者仍决定身份和权限。
    feishu_quoted_text_enabled: bool = True
    # /verbose stream 整条过程正文的字符上限；只影响展示，不裁剪最终回复。
    im_stream_preview_max_chars: int = 100
    # 只初始化明确TUI客户端的新线程；已有线程和手动/verbose选择优先，不改变CLI/IM/插件。
    tui_default_verbose_level: str = "stream"
    # IM 长工具/模型请求等待刷新间隔（秒）；0 关闭，默认 120，不调用额外 LLM，不表示业务进展。
    im_stream_heartbeat_seconds: float = 120.0
    # 可选话题聚合；默认关闭，沿原引用回复，不把 provider topic 当作应用内部 thread_id。
    im_topic_aggregation_enabled: bool = field(default=False, kw_only=True)
    # 可选执行事件进度卡片；默认关闭，保持原文本首发与展示类型，不改变任务终态。
    im_progress_cards_enabled: bool = field(default=False, kw_only=True)
    # 空查询复核只增加共享模型提示，基线由模型在原权限/预算内执行，不启动自动查询或告警。
    query_zero_result_review_enabled: bool = True
    # 仅显式选择 delivery.reply_policy=explicit 的后台任务使用结构化公开回执；旧 always 策略不变。
    # 正常收口不新增模型请求；公开形态缺失最多借原零工具阶段修复一次，关闭时拒绝该策略。
    background_public_receipt_contract_enabled: bool = True
    feishu_session_lock_enabled: bool = True  # 私聊锁默认开启；首设/闲置锁定都暂存消息，验证成功后续送，异常不放行；群聊不锁
    feishu_personal_idle_lock_seconds: int = 10800  # 私聊闲置多久后锁定(秒,默认 3h);feishu_session_lock_enabled 开启时生效
    # QQ 适配器配置
    qq_app_id: str = field(default="", metadata={"secret_ref": True})
    qq_app_secret: str = field(default="", metadata={"secret_ref": True})
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
    api_base: str = field(default="", metadata={"host_only": True, "model_projection": True})
    api_key: str = field(default="", metadata={"credential_environment_source": True, "host_only": True, "model_projection": True})
    api_key_env: str = field(default="", metadata={"host_only": True, "model_projection": True})
    # 显式兼容头只影响模型请求；认证由 api_key 负责，会话头由宿主逐会话生成。
    model_custom_headers: dict[str, str] = field(default_factory=dict, metadata={"host_only": True, "model_projection": True})
    model_session_header: str = field(default="", metadata={"host_only": True, "model_projection": True})
    # 仅由 /model 的可信 owner 解析填充；OAuth token 不进入运行快照，禁止手填跨用户路径。
    model_auth_ref: dict[str, str] = field(default_factory=dict, metadata={"host_only": True, "model_projection": True})
    model_name: str = field(default="", metadata={"host_only": True, "model_projection": True})
    request_timeout: int = 240
    # 单槽位/繁忙模型的额外排队预算，仅加到首事件等待；0 不额外等待，健康流仍无总墙钟限制。
    model_queue_wait_seconds: float = field(default=0.0, metadata={"host_only": True, "model_projection": True})
    # 每进程每HTTP origin的物理请求上限；0关闭，只按可信owner轮转，不跨进程计额。
    provider_max_concurrent_requests: int = 0
    # Responses会话WebSocket的空闲存活秒数与进程内空闲连接池容量；任一值为0即关闭复用。
    responses_ws_idle_ttl_seconds: int = 300
    responses_ws_max_idle_connections: int = 32
    max_tokens: int = DEFAULT_MODEL_MAX_TOKENS
    # 模型上下文窗口：空 = 用服务商元数据/探测，都拿不到按 128000；填数字 = 就用这个数（原 model_context_window_explicit 已并入）。
    model_context_window_tokens: int | None = field(default=None, metadata={"host_only": True, "model_projection": True})
    # 采样温度：空 = 不发送，用服务商默认；填数字 = 发送（原 model_temperature_explicit 已并入）。
    temperature: str | None = field(default=None, metadata={"host_only": True, "model_projection": True})
    # top_p 留空不覆盖普通模型；已核对的 DeepSeek V4 Flash 使用供应商采样默认。
    top_p: float | None = field(default=None, metadata={"host_only": True, "model_projection": True})
    # 智能程度（推理强度）全局默认档位 auto/off/low/medium/high/xhigh/max/ultra；会话 /effort 与子代理 effort 可覆盖。
    model_reasoning_effort: str = "auto"
    # 当前模型的思考控制方式 auto/effort/budget/none，通常由 /model 档案带入；auto 只对已核对供应商给默认，其余不发参数。
    model_reasoning_control: str = field(default="auto", metadata={"host_only": True, "model_projection": True})
    # 当前模型声明的服务商思考档位（如 low/medium/high/xhigh/max/ultra），通常由 /model 档案带入；
    # effort 协议只发声明内的值，留空表示未声明（Responses 沿通用 low/medium/high，Chat/Messages 沿原四档）。
    model_reasoning_levels: list[str] = field(default_factory=list, metadata={"host_only": True, "model_projection": True})
    # 模型档案的独立思考能力投影；auto按已核端点/精确模型ID，不能用effort档位推断是否能关闭。
    model_thinking_requirement: str = field(default="auto", metadata={"host_only": True, "model_projection": True})
    # 当前模型能不能在 input 里追加 configuration_update 项改思考档位（OpenAI GPT-6 系列的 Responses 接口）：
    # auto = 按已核对的模型名前缀 gpt-6 判断，on/off = 档案显式声明。压缩降档只在支持时才用这个项。
    model_reasoning_update_items: str = field(default="auto", metadata={"host_only": True, "model_projection": True})
    # /effort 设成 auto 以外的档位、而当前模型未声明思考控制方式且解析为不支持时，宿主是否在后台自动检测一次（9 次短请求）。
    reasoning_control_auto_probe: bool = True
    # 「只思考被输出上限截断」的续跑给紧邻那一次调用降一档思考强度（max→high→medium），打破"继续展开推理"
    # 的循环；只影响那一次调用，不改会话或用户设置。关 = 完全按原提示与原档位续跑。
    model_truncation_thinking_downgrade: bool = True
    # 当前模型的结构化输出方式 auto/native/json_object，通常由 /model 档案带入；auto 只对已核对供应商改用 json_object。
    model_structured_output: str = field(default="auto", metadata={"host_only": True, "model_projection": True})
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
    config_warnings: list[str] = field(default_factory=list)
    # 只由正式loader产生，不能从YAML注入；config_warnings保留原字符串API供旧消费者展示。
    config_parse_diagnostics: list[ConfigParseDiagnostic] = field(default_factory=list, metadata={"host_only": True})
    config_path: str = ""
    config_sources: dict[str, dict[str, object]] = field(default_factory=dict)
    config_layers: list[dict[str, object]] = field(default_factory=list)
    # 宿主内部擦洗上界：配置解析/克隆时冻结，不能由 YAML、参数中心或模型覆盖，也不含密钥值。
    host_credential_env_names: tuple[str, ...] = field(default=(), repr=False, metadata={"host_only": True})

    # 新部署字段追加在末尾，保留既有位置参数顺序；0 保持主模型 HTTP 不复用连接。
    model_http_max_idle_connections: int = field(default=0, metadata={"bounds": (0, 256), "unit": "个连接"})
    # full/delta_compat 保留 advisory 锁，delta 仅租约；统一变更流快照，须先升级 PG schema，改动需重启。
    scale_owner_snapshot_mode: str = "full"
    # 仅部署凭据的身份要求热刷新；飞书回调凭据保持启动值，需重启适配器收敛。
    gateway_edge_identity_required: bool = False

    # 兼容模型网关会产生模型用量；默认关闭，仅登记配置，不增加路由或请求。
    openai_compat_gateway_enabled: bool = field(default=False, kw_only=True)
    # 模型终态调用记录默认不持久化；开关不替代所属服务的身份核验。
    llm_call_records_enabled: bool = field(default=False, kw_only=True)
    # 每个本人运行库的调用记录行数上限；0 不封顶，清理由所属服务按批执行。
    llm_call_records_max_rows: int = field(default=50000, kw_only=True, metadata={"bounds": (0, None), "unit": "行"})
    # 旁问会增加模型调用；默认关闭，不创建额外任务或后台执行器。
    side_questions_enabled: bool = field(default=False, kw_only=True)
    # 默认退避与现有模型 HTTP 重试常量一致；各实例独立持有列表。
    model_retry_backoff_seconds: list[float] = field(default_factory=lambda: [2.0, 5.0, 15.0], kw_only=True)
    # 自定义工具沿本人插件权限；默认关闭，不加载或执行任何新工具。
    custom_tools_enabled: bool = field(default=False, kw_only=True)
    # 通知出站投递默认关闭；不改变原有通知路径或网络授权。
    notify_delivery_enabled: bool = field(default=False, kw_only=True)
    # 远程运维更新只预留部署开关；默认关闭且不能由模型自助开启。
    ops_remote_update_enabled: bool = field(default=False, kw_only=True)
    # ACP 桥接会启动外部进程；默认关闭，配置构造本身不启动进程。
    acp_bridge_enabled: bool = field(default=False, kw_only=True)
    # ACP 命令与允许环境变量仅由部署 YAML 定义，参数中心不提供写入口。
    acp_bridge_agents: dict[str, Any] = field(default_factory=dict, kw_only=True)
    # 浏览器终端默认关闭，仍沿原 PTY 生命周期及本人权限边界。
    web_terminal_enabled: bool = field(default=False, kw_only=True)

    # LLM: 配置值/来源入口；公开值与内部诊断分离，日志设置副作用限原进程，联测配置加载。
    # 函数用途: 查询一个配置项的有效来源，不更改其当前值。
    def config_source_for(self, key: str) -> dict[str, object]:
        return dict(self.config_sources.get(key, {}))

    # LLM: 配置值/来源入口；公开值与内部诊断分离，日志设置副作用限原进程，联测配置加载。
    # 函数用途: 复制配置来源快照，避免调用方修改原来源记录。
    def config_source_snapshot(self) -> dict[str, object]:
        return {
            "schema_version": "agent_config_sources.v1",
            "config_path": self.config_path,
            "layers": list(self.config_layers),
            "sources": dict(self.config_sources),
        }

# LLM: 只加载正式部署字段；旧决策不在此转换或提供默认，管理员强制别名仍沿原可信配置规范化。
# 函数用途: 加载部署配置与来源，不隐式导入模型目录或发网络请求。
def load_config(config_path: str | Path) -> AgentConfig:

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    diagnostics: list[ConfigParseDiagnostic] = []
    raw = load_simple_yaml(path, diagnostics=diagnostics)
    # 旧mini-YAML对多行小数项保留文本；按本字段的数字序列schema归一，不改变其它列表或标量的旧解析。
    if "model_retry_backoff_seconds" in raw:
        from .user_config_capability import _non_negative_numbers

        valid, error, delays = _non_negative_numbers(raw["model_retry_backoff_seconds"], yaml_scalars=True)
        if not valid:
            raise ValueError("model_retry_backoff_seconds: " + error)
        raw["model_retry_backoff_seconds"] = delays
    if {"admin_force_compact_trigger_percent", "memory_compact_admin_forced"} & raw.keys():
        from .memory import normalize_memory_settings

        raw["force_compact_trigger_percent"] = normalize_memory_settings(raw)[0].force_compact_trigger_percent
    # 先做类型验证和默认值归一（在过滤未知 key 之前）
    normalized, config_warnings = normalize_agent_config(raw)

    # 过滤未知字段。运行时诊断字段只由 loader 写入，不能从 YAML 注入。
    allowed = _public_config_keys()
    raw_keys = set(raw)
    unknown_keys = [key for key in raw_keys if key not in allowed
                    and key not in {"admin_force_compact_trigger_percent", "memory_compact_admin_forced"}]
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
    config.config_parse_diagnostics = diagnostics
    config.config_warnings = [*config_warnings, *(format_config_parse_diagnostic(item) for item in diagnostics)]
    config.config_sources = effective.sources
    config.config_layers = list(effective.layers)
    config.host_exclusive_path_roots = tuple(str(source) for source in configured_host_source_paths(config))

    normalize_agent_memory_config(config)
    apply_log_level(config)
    from .live_switches import LIVE_SWITCH_KEYS, _ConfigSources

    # 只冻结本次加载值，不登记或重读文件；守卫策略基线仍在宿主装配后的首次注册取得。
    provenance = _ConfigSources(config.config_sources)
    provenance.path = Path(resolved_path)
    provenance.applied = tuple((key, getattr(config, key)) for key in LIVE_SWITCH_KEYS
                               if type(getattr(config, key, None)) is bool)
    provenance.guard_applied = ()
    config.config_sources = provenance
    return config


# LLM: 配置值/来源入口；公开值与内部诊断分离，日志设置副作用限原进程，联测配置加载。
# 函数用途: 取得公开配置键集合，内部诊断字段不进入用户可写范围。
def _public_config_keys() -> set[str]:
    return public_config_keys(AgentConfig)


# LLM: 配置值/来源入口；公开值与内部诊断分离，日志设置副作用限原进程，联测配置加载。
# 函数用途: 将有效日志级别应用到进程日志配置。
def apply_log_level(config: AgentConfig) -> None:
    from ..common.log_redaction import install_log_redaction

    install_log_redaction()
    level_name = str(getattr(config, "log_level", "info") or "info").strip().lower()
    logging.getLogger("agent_py_agent").setLevel(_LOG_LEVELS.get(level_name, logging.INFO))
