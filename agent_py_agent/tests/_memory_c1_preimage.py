# LLM: 本fixture保存准确A30原authority装配函数，独立于新factory；只在隔离Echo测试调用，真实仓库/目录副作用仍原实现。
# 模块用途: 为C1提供原构造序列的可移植对照，不从外部冻结树运行代码。
from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store import JsonlMemory
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.settings import AgentConfig


# LLM: 只在测试中绑定原实际core语义wrapper，保同受控传输；不使用namespace模拟Agent。
# 函数用途: 调用真实Agent原语义通道，本fixture不创建客户端替身。
def _memory_semantic_channel(agent, status):
    from agent_py_agent.agent.core import _memory_semantic_channel as actual
    return actual(agent, status)


# LLM: 下方原函数体来自准确A30，除绝对导入外不改算法；用于对照字段赋值/副作用时点。
# 函数用途: 按原顺序组装实际Agent记忆资源，测试保留其真实构造结果。
def _wire_memory_authorities(
    agent: object,
    config: AgentConfig,
    paths: dict[str, object],
) -> None:
    agent.local_store = LocalStore(
        paths["local_store_path"],
        files_dir=paths["local_store_files_dir"],
        events_path=paths["local_store_events_path"],
        enable_fts=config.local_store_fts_enabled,
    )
    agent.memory_candidates = CandidateService(
        agent.home_paths.owner_memory_candidates_jsonl,
        quota_enforcer=agent.owner_quota,
    )
    semantic_status: dict[str, str] = {}
    embedder, vector_identity = _memory_semantic_channel(agent, semantic_status)
    from agent_py_agent.agent.settings.embedding_selection import remember_startup_chat_host

    # 和嵌入客户端同一时刻记下默认对话模型主机；my-agent 自配向量模型只比对这份启动快照（be 复审 S1）。
    remember_startup_chat_host(agent)
    agent.memory = JsonlMemory(
        paths["memory_path"],
        local_store=agent.local_store,
        ops_path=getattr(agent.home_paths, "owner_memory_ops_jsonl", None),
        candidate_service=agent.memory_candidates,
        embedder=embedder,
        semantic_status=semantic_status,
        quota_enforcer=agent.owner_quota,
        vector_identity=vector_identity,
    )
    agent.persona_repository = PersonaRepository.from_home_paths(
        agent.home_paths,
        quota_enforcer=agent.owner_quota,
    )
    from agent_py_agent.agent.capability.agents_source_host import build_agents_source_repository

    agent.agents_source_repository = build_agents_source_repository(agent)
    agent.persona_repository.agents_source = agent.agents_source_repository
