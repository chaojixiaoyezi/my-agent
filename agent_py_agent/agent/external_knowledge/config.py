# LLM: External knowledge config keeps future directory/API/database lookup pluggable without hard-coded order.
# 模块用途: 定义外部知识库配置，并按目录、接口、数据库的固定顺序提供来源列表。

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


# LLM: ExternalKnowledgeConfig is the small bundle passed to future knowledge lookup services.
# 类用途: 保存外部知识库索引文件名，以及目录/API/数据库三类来源。
@dataclass(frozen=True)
class ExternalKnowledgeConfig:
    index_file_name: str = "MY_AGENT_INDEX.md"
    directory_roots: tuple[str, ...] = ()
    api_sources: tuple[str, ...] = ()
    database_sources: tuple[str, ...] = ()

    # LLM: enabled lets callers skip lookup work when every source list is empty.
    # 函数用途: 判断外部知识库是否配置了任意来源。
    @property
    def enabled(self) -> bool:
        return bool(self.directory_roots or self.api_sources or self.database_sources)

    # LLM: sources_in_lookup_order makes the user's preferred simple order explicit and testable.
    # 函数用途: 按目录、接口、数据库顺序返回可查询来源。
    def sources_in_lookup_order(self) -> list[tuple[str, str]]:
        return [
            *[("directory", item) for item in self.directory_roots],
            *[("api", item) for item in self.api_sources],
            *[("database", item) for item in self.database_sources],
        ]


# LLM: external_knowledge_config_from_agent isolates AgentConfig field names from lookup callers.
# 函数用途: 从 AgentConfig 或兼容对象中提取外部知识库配置。
def external_knowledge_config_from_agent(config: object) -> ExternalKnowledgeConfig:
    return ExternalKnowledgeConfig(
        index_file_name=_non_empty_string(
            getattr(config, "external_knowledge_index_file_name", ""),
            default="MY_AGENT_INDEX.md",
        ),
        directory_roots=_as_tuple(getattr(config, "external_knowledge_directory_roots", ())),
        api_sources=_as_tuple(getattr(config, "external_knowledge_api_sources", ())),
        database_sources=_as_tuple(getattr(config, "external_knowledge_database_sources", ())),
    )


# LLM: _as_tuple mirrors config normalization for callers that pass lightweight test doubles.
# 函数用途: 把字符串、列表或元组来源归一为去空白 tuple。
def _as_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw: Iterable[object] = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = ()
    return tuple(str(item).strip() for item in raw if item is not None and str(item).strip())


# LLM: _non_empty_string keeps index file naming resilient to bad config values.
# 函数用途: 返回非空字符串，非法值回退默认索引文件名。
def _non_empty_string(value: object, *, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default
