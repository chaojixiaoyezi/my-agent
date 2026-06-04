
"""LOG 配置公共入口。

新手说明:
config_model 放数据模型和路径工具，config_loading 放加载和归一化。
外部代码从本包导入当前公开 API。
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
