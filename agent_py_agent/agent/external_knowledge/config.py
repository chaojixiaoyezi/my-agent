
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ExternalKnowledgeConfig:
    index_file_name: str = "MY_AGENT_INDEX.md"
    directory_roots: tuple[str, ...] = ()
    api_sources: tuple[str, ...] = ()
    database_sources: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.directory_roots or self.api_sources or self.database_sources)

    def sources_in_lookup_order(self) -> list[tuple[str, str]]:
        return [
            *[("directory", item) for item in self.directory_roots],
            *[("api", item) for item in self.api_sources],
            *[("database", item) for item in self.database_sources],
        ]


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


def _as_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw: Iterable[object] = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = ()
    return tuple(str(item).strip() for item in raw if item is not None and str(item).strip())


def _non_empty_string(value: object, *, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default
