# LLM: C Persona显式透传coordinator_io=True并复用同quota；False不添kwargs，原core/其它资源及构造次序不变。
# 模块用途: 为原人格builder补真实C策略，仍返回同一种领域仓库。
# 模块用途: 组装唯一owner记忆依赖，分阶段保留原core的语义通道与聊天快照时点。
from __future__ import annotations

from dataclasses import dataclass

from ..capability.persona_repository import PersonaRepository
from ..local_storage import LocalStore
from ..user_space.owner_quota import OwnerQuotaEnforcer
from .candidates import CandidateService
from .jsonl import JsonlMemory
from .lessons import HotRuleRepository, LessonRepository
from .promotion import (
    ConversationMessageEvidenceVerifier,
    LocalStoreToolEvidenceVerifier,
    MemoryPromotionDependencies,
    MemoryPromotionPolicy,
    MemoryPromotionService,
)


# LLM: 只有调用方实际装配的四个领域引用，不持config/home/run/backend/tools/source，不管理其寿命或创建派生状态。
# 类用途: 将索引、候选、正式记忆和人格仓库引用一起传给原promotion构造。
@dataclass(frozen=True)
class OwnerMemoryResources:
    local_store: LocalStore
    candidates: CandidateService
    long_term: JsonlMemory
    persona: PersonaRepository


# LLM: 原quota公式只读home与已裁决policy，不扫描用量、不重读或创建策略；保持load_errors中quota坏账的fail-closed。
# 函数用途: 为真实owner构造原空间准入器，缺owner root仍返回None。
# LLM: C仅透传默认False策略给原enforcer；公式/坏policy裁决和None保持，不存当前来源。
# 函数用途: 构造同一个owner原配额对象，C每次借用另刷新原字段。
def build_owner_memory_quota(home_paths: object, owner_policy: object, *, coordinator_io: bool = False) -> OwnerQuotaEnforcer | None:
    owner_root = str(getattr(home_paths, "owner_home_dir", "") or "").strip()
    if not owner_root:
        return None
    return OwnerQuotaEnforcer(
        owner_root,
        max_bytes=max(0, int(getattr(owner_policy, "max_disk_mb", 0))) * 1024 * 1024,
        policy_available=not any(
            str(error.get("context") or "") == "owner_policy.quota"
            for error in getattr(owner_policy, "load_errors", ())
            if isinstance(error, dict)
        ),
        **({} if not coordinator_io else {"coordinator_io": True}),
    )


# LLM: 原LocalStore构造含目录和schema副作用，调用方仍负责原初始化权限与时点；此叶不改连接/FTS/timeout。
# 函数用途: 按原runtime paths构造当前owner的真实本地索引。
# LLM: C显式透传到60本地IO，默认调用形状/原paths/FTS设置不变；真正构造仍在borrow当前scope。
# 函数用途: 按原配置创建真实本地索引，不另建冷服务替身。
def build_owner_local_store(config: object, paths: dict[str, object], *, coordinator_io: bool = False) -> LocalStore:
    return LocalStore(paths["local_store_path"], files_dir=paths["local_store_files_dir"],
                      events_path=paths["local_store_events_path"], enable_fts=config.local_store_fts_enabled,
                      **({} if not coordinator_io else {"coordinator_io": True}))


# LLM: 原Candidate构造会准备其父目录；复用传入quota引用，不改唯一候选路径或扫描状态。
# 函数用途: 构造当前owner的原候选服务，保持其准入器对象。
# LLM: C透传65准确出生/末写策略，仍同quota和原候选路径，不克隆候选账。
# 函数用途: 构造原Candidate服务，保持唯一身份及审核算法。
def build_owner_candidates(home_paths: object, quota: OwnerQuotaEnforcer | None, *, coordinator_io: bool = False) -> CandidateService:
    return CandidateService(home_paths.owner_memory_candidates_jsonl, quota_enforcer=quota,
                            **({} if not coordinator_io else {"coordinator_io": True}))


# LLM: LocalStore参数仅保精确旧投影删除及remember审计脱敏，不再授正式memory文本查询/写入；新索引由逻辑库所有。
# 函数用途: 在原聊天快照时点构造唯一冷热正式库，保留Candidate/quota/向量及隐私清理依赖。
def build_owner_long_term(home_paths: object, paths: dict[str, object], quota: OwnerQuotaEnforcer | None, *,
                          local_store: LocalStore, candidates: CandidateService, embedder: object | None,
                          semantic_status: dict[str, str], vector_identity: dict[str, object] | None) -> JsonlMemory:
    return JsonlMemory(paths["memory_path"], local_store=local_store,
        ops_path=getattr(home_paths, "owner_memory_ops_jsonl", None), candidate_service=candidates,
        embedder=embedder, semantic_status=semantic_status, quota_enforcer=quota, vector_identity=vector_identity)


# LLM: C Persona显式透传coordinator_io=True并复用同quota；False不添kwargs，原core/其它资源及构造次序不变。
# 函数用途: 为原人格builder补真实C策略，仍返回同一种领域仓库。
def build_owner_persona(home_paths: object, quota: OwnerQuotaEnforcer | None, *, coordinator_io: bool = False) -> PersonaRepository:
    return PersonaRepository.from_home_paths(home_paths, quota_enforcer=quota, **({} if not coordinator_io else {"coordinator_io": True}))


# LLM: 显式调用方已持真实semantic通道；此便利组合依次调用唯一原构造叶，不伪造Agent或聊天主机快照，原core用分阶段叶保时点。
# 函数用途: 从真实资源参数组装完整authority引用束，构造副作用仍由原仓库实现承担。
def build_owner_memory_authorities(config: object, home_paths: object, paths: dict[str, object],
                                   quota: OwnerQuotaEnforcer | None, *, embedder: object | None,
                                   semantic_status: dict[str, str], vector_identity: dict[str, object] | None) -> OwnerMemoryResources:
    local_store = build_owner_local_store(config, paths)
    candidates = build_owner_candidates(home_paths, quota)
    long_term = build_owner_long_term(home_paths, paths, quota, local_store=local_store, candidates=candidates,
        embedder=embedder, semantic_status=semantic_status, vector_identity=vector_identity)
    persona = build_owner_persona(home_paths, quota)
    return OwnerMemoryResources(local_store, candidates, long_term, persona)


# LLM: Lesson构造会准备原正文与routing父目录，不扫描正式库、不启动curator。
# 函数用途: 在原curator装配前段构造正式lesson仓库。
# LLM: C目录出生由73当前scope负责，原顺序/目录不变，不自动晋升或加载业务Agent。
# 函数用途: 构造原lesson和routing仓库，显式选择C写叶。
def build_owner_lessons(home_paths: object, *, coordinator_io: bool = False) -> LessonRepository:
    return LessonRepository(home_paths.owner_memory_lessons_dir, home_paths.owner_memory_routing_index_md,
                            **({} if not coordinator_io else {"coordinator_io": True}))


# LLM: HOT构造只沿原文件父目录初始化；不加载Agent或自动晋升候选。
# 函数用途: 在lesson之后构造原HOT仓库。
# LLM: C只透传原HOT严格写叶策略，默认旧调用形状，原预算和治理保持。
# 函数用途: 构造当前owner原HOT对象，不创建第二规则权威。
def build_owner_hot(home_paths: object, *, coordinator_io: bool = False) -> HotRuleRepository:
    return HotRuleRepository(home_paths.owner_memory_hot_md, **({} if not coordinator_io else {"coordinator_io": True}))


# LLM: 原Promotion依赖、阈值及verifier引用完全保留；不构造backend/Daily/curator/migration，也不读写候选。
# 函数用途: 用真实authority/Store/lesson/HOT组装原审核晋升服务，保持唯一证据与名单来源。
# LLM: C原verifier/阈值/依赖完全保留，仅策略透传，正式事实仍原服务，不能造Agent/身份或跳condense。
# 函数用途: 装配真实原晋升链，与默认Agent共用算法。
def build_owner_memory_promotion(config: object, home_paths: object, resources: OwnerMemoryResources, *,
                                 conversation_store: object, lessons: LessonRepository,
                                 hot: HotRuleRepository, coordinator_io: bool = False) -> MemoryPromotionService:
    return MemoryPromotionService(
        dependencies=MemoryPromotionDependencies(
            candidates=resources.candidates, long_term=resources.long_term, persona=resources.persona,
            lessons=lessons, hot=hot, message_verifier=ConversationMessageEvidenceVerifier(conversation_store),
            tool_verifier=LocalStoreToolEvidenceVerifier(resources.local_store,
                owner_id=str(home_paths.owner_id or "local/main"), owner_root=home_paths.owner_home_dir),
            exclusion_store=conversation_store,
        ),
        policy=MemoryPromotionPolicy(lesson_min_occurrences=config.memory_lesson_min_occurrences,
                                     hot_min_occurrences=config.memory_hot_min_occurrences),
        **({} if not coordinator_io else {"coordinator_io": True}),
    )
