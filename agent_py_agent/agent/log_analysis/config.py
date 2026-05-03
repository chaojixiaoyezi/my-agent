from __future__ import annotations

"""LLM: 本模块读取并归一化可选 LOG 配置，所有高风险能力默认关闭，坏值写 warning 后回退安全默认值。

新手说明:
日志分析涉及 worker、自动派工、ML、集群和响应动作，不能因为配置写错就悄悄打开危险功能。
这里负责把 YAML 里的字符串、数字、开关整理成 LogAnalysisConfig，并记录哪些字段被回退。
"""

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..settings.config import load_simple_yaml

_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")
_LEVELS = {"L0", "L1", "L2", "L3", "L4", "L5"}


@dataclass(frozen=True)
class LogAnalysisConfigWarning:
    """LLM: 记录单个配置字段为什么被回退到安全默认值。

    新手说明:
    如果用户把 `query_max_limit` 写成 `"many"`，程序不应该直接崩，也不应该乱猜。
    它会使用默认值，并把 field_name、raw_value、fallback_value、reason 记录成 warning。

    字段说明:
    field_name: 出问题的配置字段名。
    raw_value: 用户原始写入的值。
    fallback_value: 程序实际采用的安全回退值。
    reason: 为什么回退，例如 expected an integer。
    """

    field_name: str
    raw_value: Any
    fallback_value: Any
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """LLM: 把 warning dataclass 转成可序列化 dict。

        新手说明:
        CLI、doctor 和测试更适合处理字典。这个方法不改变 warning，只转换格式。

        参数说明:
        这个方法没有输入参数，只读取当前 warning 的字段。

        返回说明:
        返回包含 field_name、raw_value、fallback_value、reason 的 dict。
        """
        return asdict(self)


@dataclass
class LogAnalysisConfig:
    """LLM: 表示 LOG 配置经过校验后的最终生效值，并把危险能力保持默认关闭。

    新手说明:
    这个类不是原始 YAML，而是“程序最终相信的配置”。如果用户配置有坏值，坏值会被默认值替换。
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
    config_warnings: 配置归一化时产生的 warning 列表。
    """

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
    """LLM: 返回项目默认 LOG 配置文件路径。

    新手说明:
    不传 config_path 时，加载器会去这里找 `config/log_analysis_config.yaml`。

    参数说明:
    这个函数没有输入参数。

    返回说明:
    返回 Path，指向默认 LOG 配置文件。
    """
    return Path(__file__).resolve().parents[2] / "config" / "log_analysis_config.yaml"


def default_log_analysis_workspace_root() -> Path:
    """LLM: 返回 LOG 默认工作区根目录，用于解析相对 data_dir。

    新手说明:
    data_dir 如果是相对路径，需要知道从哪里开始拼。这里给出项目默认根目录。

    参数说明:
    这个函数没有输入参数。

    返回说明:
    返回 Path，通常是 `agent_py_agent` 包所在项目层级。
    """
    return Path(__file__).resolve().parents[2]


def resolve_log_analysis_data_dir(
    data_dir: str | Path,
    *,
    workspace_root: str | Path | None = None,
) -> Path:
    """LLM: 把 LOG data_dir 解析成绝对或工作区相对的 Path。

    新手说明:
    用户可以把 data_dir 写成绝对路径，也可以写成 `data/log_analysis` 这种相对路径。
    这个函数负责把它变成程序真正能用的 Path。

    参数说明:
    data_dir: 配置里的数据目录，可以是 str 或 Path。
    workspace_root: 可选工作区根目录；相对 data_dir 会拼到这个目录下。

    返回说明:
    如果 data_dir 是绝对路径，直接返回它；否则返回 workspace_root / data_dir。
    """
    path = Path(data_dir).expanduser()
    if path.is_absolute():
        return path
    root = Path(workspace_root).expanduser() if workspace_root is not None else default_log_analysis_workspace_root()
    return root / path


def load_log_analysis_config(
    config_path: str | Path | None = None,
    *,
    missing_ok: bool = True,
) -> LogAnalysisConfig:
    """LLM: 从 YAML 读取 LOG 配置并归一化成安全的 LogAnalysisConfig。

    新手说明:
    普通用户可能完全没启用日志分析，所以配置文件不存在时默认返回安全关闭状态。
    如果文件存在，函数会读取 YAML，再调用 normalize_log_analysis_config 校验每个字段。

    参数说明:
    config_path: 可选配置文件路径；不传时使用 default_log_analysis_config_path()。
    missing_ok: True 表示配置文件不存在时返回默认配置；False 表示不存在就抛 FileNotFoundError。

    返回说明:
    返回 LogAnalysisConfig。config.config_warnings 会包含坏值回退记录。

    异常说明:
    missing_ok=False 且文件不存在时抛 FileNotFoundError。YAML 解析错误会由 load_simple_yaml 抛出。
    """

    path = Path(config_path) if config_path is not None else default_log_analysis_config_path()
    if not path.exists():
        if not missing_ok:
            raise FileNotFoundError(f"log analysis config file does not exist: {path}")
        return LogAnalysisConfig()

    raw = load_simple_yaml(path)
    config, warnings = normalize_log_analysis_config(raw)
    config.config_warnings = [warning.to_dict() for warning in warnings]
    return config


def normalize_log_analysis_config(
    values: Mapping[str, Any] | object | None = None,
) -> tuple[LogAnalysisConfig, list[LogAnalysisConfigWarning]]:
    """LLM: 把原始配置对象逐字段校验、类型转换、限制范围，并收集 warning。

    新手说明:
    YAML 读出来的值可能是字符串、数字、布尔值，也可能写错。这个函数统一检查：
    布尔值要像布尔值，整数要在范围内，枚举值要在允许集合里，路径不能是空字符串。

    参数说明:
    values: 原始配置。可以是 Mapping，也可以是带同名属性的对象；None 表示空配置。

    返回说明:
    返回 `(config, warnings)`。config 是最终生效配置，warnings 是 LogAnalysisConfigWarning 列表。

    重要边界:
    如果 query_default_limit 大于 query_max_limit，会回退 query_default_limit，避免默认查询超过最大上限。
    """
    source = values if values is not None else {}
    warnings: list[LogAnalysisConfigWarning] = []
    defaults = LogAnalysisConfig()

    config = LogAnalysisConfig(
        enabled=_coerce_bool("enabled", _lookup(source, "enabled"), default=defaults.enabled, warnings=warnings),
        capability_level=_coerce_choice(
            "capability_level",
            _lookup(source, "capability_level"),
            default=defaults.capability_level,
            choices=_LEVELS,
            warnings=warnings,
            uppercase=True,
        ),
        data_dir=_coerce_path_string(
            "data_dir",
            _lookup(source, "data_dir"),
            default=defaults.data_dir,
            warnings=warnings,
        ),
        config_version=_coerce_int(
            "config_version",
            _lookup(source, "config_version"),
            default=defaults.config_version,
            min_value=1,
            max_value=100,
            warnings=warnings,
        ),
        worker_enabled=_coerce_bool(
            "worker_enabled",
            _lookup(source, "worker_enabled"),
            default=defaults.worker_enabled,
            warnings=warnings,
        ),
        security_prompt_enabled=_coerce_bool(
            "security_prompt_enabled",
            _lookup(source, "security_prompt_enabled"),
            default=defaults.security_prompt_enabled,
            warnings=warnings,
        ),
        auto_dispatch_enabled=_coerce_bool(
            "auto_dispatch_enabled",
            _lookup(source, "auto_dispatch_enabled"),
            default=defaults.auto_dispatch_enabled,
            warnings=warnings,
        ),
        ml_enabled=_coerce_bool(
            "ml_enabled",
            _lookup(source, "ml_enabled"),
            default=defaults.ml_enabled,
            warnings=warnings,
        ),
        cluster_enabled=_coerce_bool(
            "cluster_enabled",
            _lookup(source, "cluster_enabled"),
            default=defaults.cluster_enabled,
            warnings=warnings,
        ),
        response_execution_enabled=_coerce_bool(
            "response_execution_enabled",
            _lookup(source, "response_execution_enabled"),
            default=defaults.response_execution_enabled,
            warnings=warnings,
        ),
        response_mode=_coerce_choice(
            "response_mode",
            _lookup(source, "response_mode"),
            default=defaults.response_mode,
            choices={"recommend", "dry_run", "execute"},
            warnings=warnings,
        ),
        local_store_backend=_coerce_choice(
            "local_store_backend",
            _lookup(source, "local_store_backend"),
            default=defaults.local_store_backend,
            choices={"jsonl", "sqlite", "duckdb", "parquet"},
            warnings=warnings,
        ),
        query_default_limit=_coerce_int(
            "query_default_limit",
            _lookup(source, "query_default_limit"),
            default=defaults.query_default_limit,
            min_value=1,
            max_value=10000,
            warnings=warnings,
        ),
        query_max_limit=_coerce_int(
            "query_max_limit",
            _lookup(source, "query_max_limit"),
            default=defaults.query_max_limit,
            min_value=1,
            max_value=100000,
            warnings=warnings,
        ),
        source_retention_days=_coerce_int(
            "source_retention_days",
            _lookup(source, "source_retention_days"),
            default=defaults.source_retention_days,
            min_value=1,
            max_value=3650,
            warnings=warnings,
        ),
        payload_preview_max_chars=_coerce_int(
            "payload_preview_max_chars",
            _lookup(source, "payload_preview_max_chars"),
            default=defaults.payload_preview_max_chars,
            min_value=0,
            max_value=100000,
            warnings=warnings,
        ),
        max_parallel_analyst_agents=_coerce_int(
            "max_parallel_analyst_agents",
            _lookup(source, "max_parallel_analyst_agents"),
            default=defaults.max_parallel_analyst_agents,
            min_value=0,
            max_value=100,
            warnings=warnings,
        ),
        dispatch_budget_per_hour=_coerce_int(
            "dispatch_budget_per_hour",
            _lookup(source, "dispatch_budget_per_hour"),
            default=defaults.dispatch_budget_per_hour,
            min_value=0,
            max_value=10000,
            warnings=warnings,
        ),
        case_merge_window_minutes=_coerce_int(
            "case_merge_window_minutes",
            _lookup(source, "case_merge_window_minutes"),
            default=defaults.case_merge_window_minutes,
            min_value=1,
            max_value=10080,
            warnings=warnings,
        ),
        detector_window_minutes=_coerce_int(
            "detector_window_minutes",
            _lookup(source, "detector_window_minutes"),
            default=defaults.detector_window_minutes,
            min_value=1,
            max_value=1440,
            warnings=warnings,
        ),
    )

    if config.query_default_limit > config.query_max_limit:
        _warn(
            warnings,
            "query_default_limit",
            config.query_default_limit,
            defaults.query_default_limit,
            "expected value <= query_max_limit",
        )
        config.query_default_limit = defaults.query_default_limit

    config.config_warnings = [warning.to_dict() for warning in warnings]
    return config, warnings


def _lookup(source: Mapping[str, Any] | object, field_name: str) -> Any:
    """LLM: 从 Mapping 或普通对象中读取配置字段，缺失时返回 _MISSING 哨兵。

    新手说明:
    测试和调用方可能传字典，也可能传对象。这个函数统一读取方式。

    参数说明:
    source: 配置来源，可能是 dict 或有属性的对象。
    field_name: 要读取的字段名。

    返回说明:
    找到字段则返回原始值；找不到返回 _MISSING，用来区分“没写”和“写了 None”。
    """
    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


def _warn(
    warnings: list[LogAnalysisConfigWarning],
    field_name: str,
    raw_value: Any,
    fallback_value: Any,
    reason: str,
) -> None:
    """LLM: 追加一条配置 warning，记录原始值、回退值和原因。

    新手说明:
    每次发现坏配置，不直接 print，也不吞掉；统一写进 warnings 列表，最后给 doctor/CLI 展示。

    参数说明:
    warnings: 要追加 warning 的列表，会被原地修改。
    field_name: 出问题的字段名。
    raw_value: 用户写的原始值。
    fallback_value: 程序采用的回退值。
    reason: 回退原因。

    返回说明:
    没有返回值；结果追加到 warnings。
    """
    warnings.append(
        LogAnalysisConfigWarning(
            field_name=field_name,
            raw_value=raw_value,
            fallback_value=fallback_value,
            reason=reason,
        )
    )


def _coerce_bool(
    field_name: str,
    raw_value: Any,
    *,
    default: bool,
    warnings: list[LogAnalysisConfigWarning],
) -> bool:
    """LLM: 把用户配置值安全转换成 bool，坏值回退默认值并写 warning。

    新手说明:
    支持 True/False、0/1、"true"/"false"、"yes"/"no"、"on"/"off"。
    其它值会被认为不清楚，回退 default。

    参数说明:
    field_name: 字段名，用于 warning。
    raw_value: 原始值，可能是 _MISSING、bool、int、str 或坏值。
    default: 缺失或坏值时使用的默认值。
    warnings: warning 列表。

    返回说明:
    返回 bool。
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, int) and raw_value in {0, 1}:
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    _warn(warnings, field_name, raw_value, default, "expected a clear boolean value")
    return default


def _coerce_choice(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    choices: set[str],
    warnings: list[LogAnalysisConfigWarning],
    uppercase: bool = False,
) -> str:
    """LLM: 把用户配置值安全转换成允许集合中的字符串选项。

    新手说明:
    例如 response_mode 只能是 recommend/dry_run/execute，capability_level 只能是 L0-L5。
    如果用户写了别的值，就回退默认值并记录 warning。

    参数说明:
    field_name: 字段名，用于 warning。
    raw_value: 原始值。
    default: 缺失或坏值时使用的默认选项。
    choices: 允许的字符串集合。
    warnings: warning 列表。
    uppercase: True 表示先转大写再比较，适合 L0-L5。

    返回说明:
    返回 choices 中的字符串，或 default。
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        normalized = normalized.upper() if uppercase else normalized.lower()
        if normalized in choices:
            return normalized
    _warn(warnings, field_name, raw_value, default, f"expected one of {sorted(choices)}")
    return default


def _coerce_int(
    field_name: str,
    raw_value: Any,
    *,
    default: int,
    min_value: int,
    max_value: int | None,
    warnings: list[LogAnalysisConfigWarning],
) -> int:
    """LLM: 把用户配置值安全转换成有范围限制的整数。

    新手说明:
    这个函数接受整数，也接受像 "100" 这样的数字字符串；但不接受 True/False，
    因为布尔值在 Python 里也是 int，直接接受会很容易误判。

    参数说明:
    field_name: 字段名，用于 warning。
    raw_value: 原始值。
    default: 缺失或坏值时使用的默认整数。
    min_value: 允许的最小值。
    max_value: 允许的最大值；None 表示没有上限。
    warnings: warning 列表。

    返回说明:
    返回范围内整数；解析失败或越界时返回 default。
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        _warn(warnings, field_name, raw_value, default, "expected an integer, not a boolean")
        return default
    if isinstance(raw_value, int):
        number = raw_value
    elif isinstance(raw_value, str) and _INT_PATTERN.fullmatch(raw_value.strip()):
        number = int(raw_value.strip())
    else:
        _warn(warnings, field_name, raw_value, default, "expected an integer")
        return default

    if number < min_value:
        _warn(warnings, field_name, raw_value, default, f"expected value >= {min_value}")
        return default
    if max_value is not None and number > max_value:
        _warn(warnings, field_name, raw_value, default, f"expected value <= {max_value}")
        return default
    return number


def _coerce_path_string(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    warnings: list[LogAnalysisConfigWarning],
) -> str:
    """LLM: 校验路径字符串非空且不包含明显危险控制字符。

    新手说明:
    路径配置必须是字符串，不能是空值，也不能包含换行或 NUL 字符。
    这不是完整安全沙盒，只是配置层的基础防呆。

    参数说明:
    field_name: 字段名，用于 warning。
    raw_value: 原始值。
    default: 缺失或坏值时使用的默认路径字符串。
    warnings: warning 列表。

    返回说明:
    返回可接受的路径字符串，或 default。
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if normalized and "\x00" not in normalized and "\n" not in normalized and "\r" not in normalized:
            return normalized
    _warn(warnings, field_name, raw_value, default, "expected a non-empty path string")
    return default


__all__ = [
    "LogAnalysisConfig",
    "LogAnalysisConfigWarning",
    "default_log_analysis_config_path",
    "default_log_analysis_workspace_root",
    "load_log_analysis_config",
    "normalize_log_analysis_config",
    "resolve_log_analysis_data_dir",
]
