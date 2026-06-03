from __future__ import annotations


def test_external_knowledge_config_normalizes_flat_fields():
    from agent_py_agent.agent.external_knowledge.config import external_knowledge_config_from_agent
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.config_normalize import normalize_agent_config

    normalized, warnings = normalize_agent_config(
        {
            "external_knowledge_index_file_name": "KB_INDEX.md",
            "external_knowledge_directory_roots": ["/docs", " /wiki ", ""],
            "external_knowledge_api_sources": ["https://kb.example/search"],
            "external_knowledge_database_sources": [],
        }
    )

    config = AgentConfig(**normalized)
    knowledge = external_knowledge_config_from_agent(config)

    assert warnings == []
    assert knowledge.index_file_name == "KB_INDEX.md"
    assert knowledge.sources_in_lookup_order() == [
        ("directory", "/docs"),
        ("directory", "/wiki"),
        ("api", "https://kb.example/search"),
    ]


def test_external_knowledge_config_defaults_to_disabled():
    from agent_py_agent.agent.external_knowledge.config import external_knowledge_config_from_agent
    from agent_py_agent.agent.settings.config import AgentConfig

    knowledge = external_knowledge_config_from_agent(AgentConfig())

    assert knowledge.index_file_name == "MY_AGENT_INDEX.md"
    assert not knowledge.enabled
