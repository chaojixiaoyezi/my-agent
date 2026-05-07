"""model selection and parameters."""

# LLM: 默认模型和温度会影响所有推理请求，改动时同步文档和场景测试。
# 模块用途: 模型后端、模型名和采样参数的配置模型。

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ModelConfig"]


# LLM: ModelConfig 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ModelConfig 配置模型，保存 配置系统 的默认值和可调参数。
@dataclass
class ModelConfig:
    """Model backend configuration."""

    model_backend: str = "echo"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_key_env: str = "AGENT_API_KEY"
    model_name: str = "gpt-4o-mini"
    request_timeout: int = 60
    max_tokens: int = 1024
    temperature: str = "0.2"
    anthropic_version: str = "2023-06-01"
    model_speed_profile_path: str = "data/model_speed_profile.json"
    auto_bench_model_on_first_use: bool = True