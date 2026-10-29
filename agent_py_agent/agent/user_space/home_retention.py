# LLM: 原facade仍延迟重导出唯一Memory服务；增长登记是声明，不增加写者或删除权。
# 模块用途: 统一31类生命周期元数据和原保留服务入口，未接线resolver必须明确拒绝。
from __future__ import annotations

"""旧 home-retention import 路径对统一 Memory v2 retention Service 的只读导出。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory_store.retention import (
        MemoryRetentionService,
        OwnerRetentionPlan,
        RetentionAction,
        apply_owner_retention,
        plan_owner_retention,
    )

# LLM: 本模块不得恢复旧 raw_days/hooks_days scanner；所有调用都由 memory_store.retention 的唯一 Service 执行。
# 模块用途: 保持既有 Gateway maintenance 和 CLI import 可用，同时消除重复 retention 实现。
# 循环导入根修: user_space/__init__ 顶层 import 本模块，而 memory_store.daily 又 import
#   user_space.owner_quota → 包加载期 memory_store ↔ user_space 互相触发。用 PEP 562 延迟重导出：
#   包加载期不 import memory_store，只有真正访问 retention 符号时才解析，打破回环且接口不变。

__all__ = [
    "MemoryRetentionService",
    "OwnerRetentionPlan",
    "RetentionAction",
    "apply_owner_retention",
    "plan_owner_retention",
]


def __getattr__(name: str):
    if name in __all__:
        from ..memory_store.retention import (
            MemoryRetentionService,
            OwnerRetentionPlan,
            RetentionAction,
            apply_owner_retention,
            plan_owner_retention,
        )

        return {
            "MemoryRetentionService": MemoryRetentionService,
            "OwnerRetentionPlan": OwnerRetentionPlan,
            "RetentionAction": RetentionAction,
            "apply_owner_retention": apply_owner_retention,
            "plan_owner_retention": plan_owner_retention,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# LLM: 生命周期声明只连接原领域权威，不推导保留许可；resolver必须由原领域显式提供，缺失不能猜目录。
# 类用途: 为一种增长对象登记归属、来源、策略、引用责任和允许动作。
@dataclass(frozen=True)
class StorageKind:
    kind: str
    scope: str
    authority: str
    policy_ref: str
    protected_refs: str
    actions: tuple[str, ...]
    age_source: str = 'original domain lifecycle/timestamp'
    cursor_schema: int = 1

    # LLM: resolver返回原领域规范路径，不在登记层拼owner、目录或授予跨owner访问。
    # 函数用途: 取得已接线领域的规范位置，未接线明确拒绝。
    def resolve_paths(self, resolvers) -> tuple:
        resolver = resolvers.get(self.kind)
        if not callable(resolver):
            raise LookupError('storage_kind_resolver_unavailable:' + self.kind)
        return tuple(resolver())


# 这些是06_STORAGE_GROWTH_REGISTER的稳定类别；policy_ref是原领域策略引用而非第二份保留JSON。
_STORAGE_LIFECYCLES = (
    ('long_term', 'owner', 'memory_store.JsonlMemory', 'memory retention', 'saved/version/forget', ('preserve', 'segment')),
    ('candidates', 'owner', 'memory candidates', 'candidate lifecycle', 'approved/unfulfilled/evidence', ('preserve', 'archive')),
    ('memory_operations', 'owner', 'memory operations/tombstones', 'logical deletion generation', 'forget barrier', ('preserve', 'compact')),
    ('vector_cache', 'owner', 'memory vector/text cache', 'original cache policy', 'model/hash/source generation', ('rebuild', 'reclaim')),
    ('raw_memory_audit', 'owner', 'raw memory source segments', 'audit_days', 'Curator coverage/pins', ('preserve', 'seal')),
    ('daily_curator', 'owner', 'daily/Curator runs', 'memory retention', 'prepared/committed evidence', ('archive', 'policy-delete')),
    ('conversation_messages', 'owner', 'conversation store', 'thread retention', 'compact coverage/user preserve', ('preserve', 'segment')),
    ('compact_checkpoints', 'owner', 'compact ledger', 'compact chain lifecycle', 'summary_base/CAS/recovery', ('preserve', 'index')),
    ('context_bundles', 'owner', 'memory archive snapshots', 'existing snapshot retention', 'latest/task/manifest', ('preserve', 'policy-delete')),
    ('tool_outputs', 'owner', 'tool output manifests', 'task terminal retention', 'conversation/artifact/candidate', ('preserve', 'policy-delete')),
    ('local_records', 'workspace', 'LocalStore records/FTS', 'derived TTL', 'task_ref/protected records', ('rebuild', 'policy-delete')),
    ('local_events', 'workspace', 'LocalStore events/JSONL', 'existing event retention', 'pending recovery IDs/bad bytes', ('compact', 'policy-delete')),
    ('runtime_events', 'owner', 'RuntimeRepository', 'entity lifecycle', 'attempt/UNKNOWN/lease/idempotency', ('preserve', 'archive')),
    ('scheduler_history', 'owner', 'SchedulerRepository', 'scheduler lifecycle', 'active/waiting/unsettled runs', ('preserve', 'segment')),
    ('global_indexes', 'host', 'global_index projections', 'existing compaction policy', 'owner/task/agent/run locators', ('rebuild', 'compact')),
    ('gateway_active', 'gateway', 'Gateway queue and claims', 'original queue lifecycle', 'in-flight/exact claim/replay', ('preserve',)),
    ('gateway_terminal', 'gateway', 'Gateway receipt repository', 'terminal receipt lifecycle', 'pending replay/outbox/UNKNOWN/unconsumed', ('preserve', 'segment')),
    ('gateway_logs', 'gateway', 'Gateway logger/history', 'original log policy', 'diagnostic/request association', ('preserve', 'seal')),
    ('security_audit', 'owner', 'security audit authority', 'security_audit_days', 'operations/unknown/bad rows', ('preserve', 'seal')),
    ('skill_usage', 'owner', 'SkillLearningStore usage', 'usage coverage policy', 'sequence/day/skill/event', ('preserve', 'segment')),
    ('skill_registry', 'owner', 'SkillLearningStore registry', 'existing version policy', 'confirmation/hash/CAS/rollback', ('preserve', 'policy-delete')),
    ('installed_versions', 'owner', 'canonical package/plugin install', 'manifest lifecycle', 'active/frozen/rollback/revocation', ('preserve', 'reclaim')),
    ('task_workspaces', 'owner', 'canonical task/run/audit roots', 'existing terminal retention', 'deliverables/legal hold/parent refs', ('preserve', 'policy-delete')),
    ('subagents', 'owner', 'subagent canonical state', 'canonical lifecycle', 'wake/report/attempt/execution lock', ('preserve', 'archive')),
    ('capability_grants', 'owner', 'capability request/grant authority', 'grant expiry/revocation', 'approval/task/proof', ('preserve', 'archive')),
    ('media_inputs', 'owner', 'input/attachment authority', 'explicit deletion policy', 'original inputs/messages/unfinished tasks', ('preserve', 'explicit-delete')),
    ('persona_versions', 'owner', 'PersonaRepository version ledger', 'version/CAS policy', 'user confirmation/rollback', ('preserve', 'archive')),
    ('outbox_delivery', 'owner', 'outbox/delivery acknowledgments', 'delivery protocol', 'unacked/UNKNOWN/cancel/dedupe', ('preserve', 'archive')),
    ('process_sessions', 'owner', 'managed process/PTY authority', 'idle/count policy', 'PID birth/unknown exit', ('preserve', 'reclaim')),
    ('cache_tmp_trash', 'owner', 'original cache/tmp/trash owners', 'original TTL/tombstone policy', 'live locks/staging/deletion barriers', ('preserve', 'reclaim')),
    ('legacy_layout', 'owner', 'original layout/migration authorities', 'explicit migration policy', 'ownership/references/user data', ('preserve', 'inventory')),
)


# LLM: 返回固定声明的只读值，不扫描生产目录、不加载正文、不把未接线声明冒充维护完成。
# 函数用途: 给原维护入口和各领域适配器同一份31类登记。
def storage_kind_registry() -> tuple[StorageKind, ...]:
    return tuple(StorageKind(*row) for row in _STORAGE_LIFECYCLES)
