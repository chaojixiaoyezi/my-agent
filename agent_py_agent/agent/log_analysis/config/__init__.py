"""LLM: 本包把 LOG 配置拆成 config_model（数据模型和路径工具）与 config_loading（加载和归一化）两个子模块。

新手说明:
从外部 import 时路径不变，仍然可以用 from ...config import LogAnalysisConfig。
__init__.py 把两个子模块的公开名称全部 re-export，保证拆包后上游代码不需要修改。
"""

from .config_loading import (
    load_log_analysis_config,
    normalize_log_analysis_config,
)
from .config_model import (
    LogAnalysisConfig,
    LogAnalysisConfigWarning,
    default_log_analysis_config_path,
    default_log_analysis_workspace_root,
    resolve_log_analysis_data_dir,
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
