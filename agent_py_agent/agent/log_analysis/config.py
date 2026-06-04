
"""本模块读取并归一化可选 LOG 配置，所有高风险能力默认关闭，坏值写 warning 后采用安全默认值。

新手说明:
日志分析涉及 worker、自动派工、ML、集群和响应动作，不能因为配置写错就悄悄打开危险功能。
这里负责把 YAML 里的字符串、数字、开关整理成 LogAnalysisConfig，并记录哪些字段被采用默认值。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..settings.config import load_simple_yaml

# Public config symbols from services.
from .services import (
    coerce_bool,
    coerce_choice,
    coerce_int,
    coerce_path_string,
    coercion,
    lookup,
    normalization,
)

__all__ = [
    "LogAnalysisConfig",
    "LogAnalysisConfigWarning",
    "default_log_analysis_config_path",
    "default_log_analysis_workspace_root",
    "load_log_analysis_config",
    "normalize_log_analysis_config",
    "resolve_log_analysis_data_dir",
]

# ----------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------


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


# ----------------------------------------------------------------------
# Path helpers
# ----------------------------------------------------------------------


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


# ----------------------------------------------------------------------
# Loader
# ----------------------------------------------------------------------


def load_log_analysis_config(
    config_path: str | Path | None = None,
    *,
    missing_ok: bool = True,
) -> LogAnalysisConfig:
    """从 YAML 读取 LOG 配置并归一化成安全的 LogAnalysisConfig。

    新手说明:
    普通用户可能完全没启用日志分析，所以配置文件不存在时默认返回安全关闭状态。
    如果文件存在，函数会读取 YAML，再调用 normalize_log_analysis_config 校验每个字段。

    config_path: 可选配置文件路径；不传时使用 default_log_analysis_config_path()。
    missing_ok: True 表示配置文件不存在时返回默认配置；False 表示不存在就抛 FileNotFoundError。

    返回说明:
    返回 LogAnalysisConfig。config.config_warnings 会包含坏值默认值记录。

    异常说明:
    missing_ok=False 且文件不存在时抛 FileNotFoundError。YAML 解析错误会由 load_simple_yaml 抛出。"""
    path = Path(config_path) if config_path is not None else default_log_analysis_config_path()
    if not path.exists():
        if not missing_ok:
            raise FileNotFoundError(f"log analysis config file does not exist: {path}")
        return LogAnalysisConfig()

    raw = load_simple_yaml(path)
    config, warnings = normalize_log_analysis_config(raw)
    config.config_warnings = [warning.to_dict() for warning in warnings]
    return config


# ----------------------------------------------------------------------
# Normalization entry point (thin wrapper over services)
# ----------------------------------------------------------------------


def normalize_log_analysis_config(
    values: Mapping[str, Any] | object | None = None,
) -> tuple[LogAnalysisConfig, list[LogAnalysisConfigWarning]]:
    """把原始配置对象逐字段校验、类型转换、限制范围，并收集 warning。

    新手说明:
    YAML 读出来的值可能是字符串、数字、布尔值，也可能写错。这个函数统一检查：
    布尔值要像布尔值，整数要在范围内，枚举值要在允许集合里，路径不能是空字符串。

    values: 原始配置。可以是 Mapping，也可以是带同名属性的对象；None 表示空配置。

    返回说明:
    返回 `(config, warnings)`。config 是最终生效配置，warnings 是 LogAnalysisConfigWarning 列表。

    重要边界:
    如果 query_default_limit 大于 query_max_limit，会重置 query_default_limit，避免默认查询超过最大上限。"""
    config, warnings = normalization.normalize_all(values)
    config.config_warnings = [warning.to_dict() for warning in warnings]
    return config, warnings
