
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HomeProviderConfigFields:
    my_agent_home: str = "~/.my-agent"
    my_agent_owner_provider: str = "local"
    my_agent_owner_kind: str = "main"
    my_agent_owner_id: str = "main"
    workspace_task_path_template: str = "tasks/{date}/{task_slug}"
    home_runtime_bootstrap_enabled: bool = True
    home_context_enabled: bool = True
    home_lesson_auto_read_limit: int = 3
    daily_memory_mirror_enabled: bool = True
    run_task_workspace_enabled: bool = True
    external_knowledge_index_file_name: str = "MY_AGENT_INDEX.md"
    external_knowledge_directory_roots: list[str] = field(default_factory=list)
    external_knowledge_api_sources: list[str] = field(default_factory=list)
    external_knowledge_database_sources: list[str] = field(default_factory=list)
    provider_space_default_max_storage_mb: int = 2048
    provider_space_max_download_file_mb: int = 200
    provider_space_trash_retention_days: int = 30
    provider_space_destructive_actions_use_trash: bool = True
