# LLM: 当前owner只装配原C1真实资源，不造Agent/receipt/权限源；C3本地末写已接原scope，source退出/首槽优先于HTTP结果，不缓存授权。
# 模块用途: 保存C记忆主链装配与算法调用点，长业务处于Root外，固定本地提交才进入原HTTP末门。
from dataclasses import dataclass

from ..common.global_write_commit import global_write_commit_scope
from ..memory_store import owner_resources
from ..memory_store.jsonl import JsonlMemory, JsonlMemoryDependencies
from ..memory_store.owner_services import ExplicitRememberServices, remember_explicit
from ..retrieval.embedding_factory import memory_semantic_channel_for_resources
from .client_service import GatewayClientMemoryResult, _bounded_limit, memory_record_payload
from .multiprocess_local_control import _LocalControlWriteSource
from .owner_resources import resolve_owner_resource_configuration


# LLM: 仅真实领域引用/首次配置，不持HTTP/source/actor或事务连接；同owner业务mutex保护promotion惰性装配。
# 类用途: 复用原正式记忆、候选和索引实例，保原缓存及pending。
@dataclass(repr=False)
class _OwnerMemoryDomain:
    config: object
    home: object
    paths: dict
    authorities: object
    promotion: object = None

    # LLM: 返回唯一原JsonlMemory，不能克隆或另建pending表。
    # 函数用途: 为holder逐出判断和原search返回同一个长期记忆实例。
    @property
    def long_term(self):
        return self.authorities.long_term


# LLM: C3-J须在原builder显式透传coordinator_io后才可调用；这里原顺序与semantic identity保持，不能把整构造塞Root或用默认IO回落。
# 函数用途: 用显式配置/home组装原LocalStore、候选、语义通道、正式记忆和人格引用。
# LLM: 本片仅同一Persona builder调用显式True，所有真实构造顺序/引用/holder/source与J3 helper保持已冻结77。
# 函数用途: 让当前owner原Persona实例消费本次scope，不能另建资源容器。
def _assemble_owner_resources(control, owner):
    home, config, paths = resolve_owner_resource_configuration(control, owner)
    from ..user_space.owner_policy import resolve_effective_owner_policy

    quota = owner_resources.build_owner_memory_quota(home, resolve_effective_owner_policy(home), coordinator_io=True)
    local = owner_resources.build_owner_local_store(config, paths, coordinator_io=True)
    candidates = owner_resources.build_owner_candidates(home, quota, coordinator_io=True)
    status = {}
    embedder, identity = memory_semantic_channel_for_resources(config, home, status)
    long_term = JsonlMemory.from_dependencies(paths["memory_path"], JsonlMemoryDependencies(
        local_store=local,
        ops_path=getattr(home, "owner_memory_ops_jsonl", None),
        candidate_service=candidates,
        embedder=embedder,
        semantic_status=status,
        quota_enforcer=quota,
        vector_identity=identity,
        coordinator_io=True,
    ))
    persona = owner_resources.build_owner_persona(home, quota, coordinator_io=True)
    authorities = owner_resources.OwnerMemoryResources(local, candidates, long_term, persona)
    return _OwnerMemoryDomain(config, home, paths, authorities)


# LLM: 只调用原search，成功后同owner/scope固定flush一次；失败不重搜/重嵌或补写，费用与ranking保持原算法。
# 函数用途: 从原正式记忆召回并将本次访问元数据显式落盘。
def _search_current_owner(resources, query, limit):
    records = resources.long_term.search(str(query or "").strip(), _bounded_limit(limit))
    resources.long_term.flush_access_events_from_source()
    return tuple(memory_record_payload(record) for record in records)


# LLM: 仅原lazy if块纯提取，同resources字段/构造次序/当前borrow scope保持；不给C4另建holder或权限入口。
# 函数用途: 按需取得原同一个promotion服务，复用原ConversationStore、lesson/HOT及证据引用。
def ensure_owner_memory_promotion(resources):
    if resources.promotion is None:
        from ..settings.thread_model_selection import default_model_profile_id_for_home
        from .owner_conversation_store import owner_conversation_store_from_config

        home, config = resources.home, resources.config
        conversation = owner_conversation_store_from_config(
            config,
            home,
            initialize=False,
            model_default=lambda: default_model_profile_id_for_home(home),
        )
        lessons = owner_resources.build_owner_lessons(home, coordinator_io=True)
        hot = owner_resources.build_owner_hot(home, coordinator_io=True)
        resources.promotion = owner_resources.build_owner_memory_promotion(
            config,
            home,
            resources.authorities,
            conversation_store=conversation,
            lessons=lessons,
            hot=hot,
            coordinator_io=True,
        )
    return resources.promotion


# LLM: 原ExplicitRememberServices唯一observe/review/promote链，记忆身份和formal→index→mark保持；原lesson/HOT/store只按需真实构造，C3端口缺失不得fallback。
# 函数用途: 在当前owner原实例上保存用户明确要求的事实。
def _remember_current_owner(resources, content, kind):
    ensure_owner_memory_promotion(resources)
    record = remember_explicit(
        ExplicitRememberServices(
            resources.authorities.candidates, resources.promotion, resources.long_term
        ),
        str(content or "").strip(),
        kind=str(kind or "fact"),
    )
    return (memory_record_payload(record),)


# LLM: 仅纯提取原policy公式/坏账判据；调用方须已核当前来源且持同owner borrow mutex，不缓存policy/source或重建quota。
# 函数用途: 更新并返回原enforcer的两个当前策略字段，供client/C4复用唯一公式。
def refresh_owner_memory_quota(resources, policy):
    quota = resources.long_term.quota_enforcer
    if quota is not None:
        quota.max_bytes = max(0, int(getattr(policy, "max_disk_mb", 0))) * 1024 * 1024
        quota.policy_available = not any(
            str(error.get("context") or "") == "owner_policy.quota"
            for error in getattr(policy, "load_errors", ())
            if isinstance(error, dict)
        )
    return quota


# LLM: 固定search/remember由实际HTTP消费原source，同borrow互斥刷新原quota scalar；先完整source退出重抛首native并退pin，再给HTTP投影，不在scope内吞异常。
# 函数用途: 用同次真实HTTP来源调用原记忆算法，容器不保存该来源或callback。
# LLM: 同owner borrow mutex内用本次已核policy更新原quota对象；原scope退出首槽/清理先于HTTP结果，异常原对象不在这里投影。
# 函数用途: 用实际HTTP来源执行原一次搜索或显式记忆，不缓存source。
def execute_coordinator_client_memory(handler, server, body):
    operation = str(body.get("operation") or "").strip().lower()
    with _LocalControlWriteSource.http_client_memory_scope(handler, server, body) as source:
        facts = source.http_origin
        source.bind_client_memory_owner(facts.home)
        with global_write_commit_scope(source.perform):
            with facts.holder.borrow_current_owner(
                facts.home, lambda: _assemble_owner_resources(facts.control, facts.owner)
            ) as resources:
                source._require_current()
                refresh_owner_memory_quota(resources, facts.policy)
                if operation == "search":
                    records = _search_current_owner(
                        resources, body.get("query"), body.get("limit", 5)
                    )
                else:
                    records = _remember_current_owner(
                        resources, body.get("content"), body.get("kind", "fact")
                    )
                source._require_current()
    return GatewayClientMemoryResult(operation, True, records=records)
