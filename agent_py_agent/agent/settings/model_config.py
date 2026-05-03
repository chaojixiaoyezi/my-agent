"""LLM: model selection and parameters."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ModelConfig"]


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