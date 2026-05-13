# LLM: External knowledge is optional and config-backed; runtime lookup lives in later modules.
# 模块用途: 暴露外部知识库配置对象和从 AgentConfig 提取配置的入口。

from __future__ import annotations

from .config import ExternalKnowledgeConfig, external_knowledge_config_from_agent

__all__ = ["ExternalKnowledgeConfig", "external_knowledge_config_from_agent"]
