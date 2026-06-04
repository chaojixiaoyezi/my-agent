
"""本模块定义 LOG 配置的数据模型、默认值和路径解析，所有高风险能力默认关闭。

新手说明:
这里包含 LogAnalysisConfigWarning（配置默认值警告）、LogAnalysisConfig（最终生效配置）
以及 default_log_analysis_config_path、default_log_analysis_workspace_root、
resolve_log_analysis_data_dir 三个路径工具函数。
这些类和函数不负责校验，只描述"配置长什么样"和"默认路径在哪"。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LogAnalysisConfigWarning:
    """记录单个配置字段为什么采用安全默认值。

    新手说明:
    如果用户把 `query_max_limit` 写成 `"many"`，程序不应该直接崩，也不应该乱猜。
    它会使用默认值，并把 field_name、raw_value、default_value、reason 记录成 warning。

    字段说明:
    field_name: 出问题的配置字段名。
    raw_value: 用户原始写入的值。
    default_value: 程序实际采用的安全默认值。
    reason: 为什么采用默认值，例如 expected an integer。"""

    field_name: str
    raw_value: Any
    default_value: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """把 warning dataclass 转成可序列化 dict。

        新手说明:
        CLI、doctor 和测试更适合处理字典。这个方法不改变 warning，只转换格式。

        这个方法没有输入参数，只读取当前 warning 的字段。

        返回说明:
        返回包含 field_name、raw_value、default_value、reason 的 dict。"""
        return asdict(self)


@dataclass
class LogAnalysisConfig:
    """表示 LOG 配置经过校验后的最终生效值，并把危险能力保持默认关闭。

    新手说明:
    这个类不是原始 YAML，而是"程序最终相信的配置"。如果用户配置有坏值，坏值会被默认值替换。
    日志分析模块默认关闭；能启动 worker、自动派工、加载 ML/集群或执行响应动作的字段也默认关闭。

    字段说明:
    enabled: LOG 模块总开关，默认 False。
    capability_level: 能力等级 L0-L5，默认 L0，等级越高代表可用能力越多。
    data_dir: LOG 数据目录，相对路径会按 workspace root 解析。
    config_version: 配置版本号，方便以后迁移。
    worker_enabled: 是否允许后台 worker。
    security_prompt_enabled: 是否允许安全分析 prompt 能力。
    auto_dispatch_enabled: 是否允许自动派工 analyst/reviewer。
    ml_enabled: 是否允许 ML 后端。
    cluster_enabled: 是否允许集群后端。
    response_execution_enabled: 是否允许执行响应动作；默认关闭。
    response_mode: 响应模式，recommend/dry_run/execute 三选一。
    local_store_backend: 本地存储后端，当前默认 jsonl。
    query_default_limit: 查询默认返回/持久化行数。
    query_max_limit: 查询允许的最大上限，防止一次读太多日志。
    source_retention_days: 源日志保留天数。
    payload_preview_max_chars: payload 预览最大字符数，避免 prompt 过长。
    max_parallel_analyst_agents: 最多并行 analyst 数，0 表示关闭自动 analyst 并发。
    dispatch_budget_per_hour: 每小时派工预算，0 表示不自动派工。
    case_merge_window_minutes: case 合并时间窗口。
    detector_window_minutes: detector 查询/聚合时间窗口。
    config_warnings: 配置归一化时产生的 warning 列表。"""

    enabled: bool = False
    capability_level: str = "L0"
    data_dir: str = "data/log_analysis"
    config_version: int = 1

    worker_enabled: bool = False
    security_prompt_enabled: bool = False
    auto_dispatch_enabled: bool = False
    ml_enabled: bool = False
    cluster_enabled: bool = False
    response_execution_enabled: bool = False

    response_mode: str = "recommend"
    local_store_backend: str = "jsonl"

    query_default_limit: int = 100
    query_max_limit: int = 1000
    source_retention_days: int = 30
    payload_preview_max_chars: int = 2048

    max_parallel_analyst_agents: int = 0
    dispatch_budget_per_hour: int = 0
    case_merge_window_minutes: int = 60
    detector_window_minutes: int = 15

    config_warnings: list[dict[str, Any]] = field(default_factory=list)


def default_log_analysis_config_path() -> Path:
    """返回项目默认 LOG 配置文件路径。

    新手说明:
    不传 config_path 时，加载器会去这里找 `config/log_analysis_config.yaml`。

    这个函数没有输入参数。

    返回说明:
    返回 Path，指向默认 LOG 配置文件。"""
    return Path(__file__).resolve().parents[2] / "config" / "log_analysis_config.yaml"


def default_log_analysis_workspace_root() -> Path:
    """返回 LOG 默认工作区根目录，用于解析相对 data_dir。

    新手说明:
    data_dir 如果是相对路径，需要知道从哪里开始拼。这里给出项目默认根目录。

    这个函数没有输入参数。

    返回说明:
    返回 Path，通常是 `agent_py_agent` 包所在项目层级。"""
    return Path(__file__).resolve().parents[2]


def resolve_log_analysis_data_dir(
    data_dir: str | Path,
    *,
    workspace_root: str | Path | None = None,
) -> Path:
    """把 LOG data_dir 解析成绝对或工作区相对的 Path。

    新手说明:
    用户可以把 data_dir 写成绝对路径，也可以写成 `data/log_analysis` 这种相对路径。
    这个函数负责把它变成程序真正能用的 Path。

    data_dir: 配置里的数据目录，可以是 str 或 Path。
    workspace_root: 可选工作区根目录；相对 data_dir 会拼到这个目录下。

    返回说明:
    如果 data_dir 是绝对路径，直接返回它；否则返回 workspace_root / data_dir。"""
    path = Path(data_dir).expanduser()
    if path.is_absolute():
        return path
    root = Path(workspace_root).expanduser() if workspace_root is not None else default_log_analysis_workspace_root()
    return root / path
