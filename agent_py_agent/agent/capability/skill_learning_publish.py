# LLM: 自学习 S3 用户明确确认后的发布闸门；调用方须先验证候选id/revision与真实人类权限。顺序固定：重名/删过的名字/数量上限
#   （新建）或登记在册且磁盘 hash 未被用户改动（更新）→ 字段脱敏 → 暂存目录渲染 SKILL.md 并按 require_frontmatter
#   解析、核对 frontmatter 无损往返 → agent_generated guard（从不 force，caution/dangerous 都拒）→ 原子安装/替换 →
#   版本全文 → 登记表。暂存写读扫描固定FD；失败补偿正文并清理本次新建且未提交的匹配版本，既有历史不删。全程持state_lock。
#   不调用模型；revert/remove 也在这里，供 CLI 使用。同步检查 skill_learning.py、cli/skill_learning_commands.py 与测试。
# 模块用途: 把一次已校验的总结决定安全地发布为 owner 的 skills/learned/<name>/SKILL.md，并提供回滚和删除。
from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import read_text_beneath, unlink_file_beneath
from ..contracts.gates.skill_guard import SkillScanResult, install_decision
from .skill_learning_prompt import DECISION_CREATE, SkillLearningDecision
from .skill_learning_store import (
    CODE_EVIDENCE_INCOMPLETE,
    CODE_MAINTENANCE_OBSERVE_ONLY,
    EVENT_PUBLISHED,
    EVENT_REVERTED,
    EVENT_SKIPPED,
    EVENT_UPDATED,
    MAX_SOURCE_RUN_COUNT,
    LearnedSkill,
    SkillLearningEvent,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
    is_proven_stable_version,
    replace_skill_file,
    skill_family_fingerprint,
    utc_now,
)
from .skill_snapshot import skill_content_sha256
from .skills import MAX_SKILL_BYTES, split_skill_frontmatter

CODE_NAME_TAKEN = "SKILL_LEARNING_NAME_TAKEN"
CODE_NAME_BLOCKED = "SKILL_LEARNING_NAME_BLOCKED"
CODE_FAMILY_BLOCKED = "SKILL_LEARNING_FAMILY_BLOCKED"
CODE_LEARNING_DISABLED = "SKILL_LEARNING_DISABLED"
CODE_LIMIT_REACHED = "SKILL_LEARNING_LIMIT_REACHED"
CODE_TARGET_MISSING = "SKILL_LEARNING_TARGET_MISSING"
CODE_TARGET_USER_OWNED = "SKILL_LEARNING_TARGET_USER_OWNED"
CODE_DRAFT_INVALID = "SKILL_LEARNING_DRAFT_INVALID"
CODE_GUARD_BLOCKED = "SKILL_LEARNING_GUARD_BLOCKED"
CODE_INSTALL_FAILED = "SKILL_LEARNING_INSTALL_FAILED"
CODE_UNCHANGED = "SKILL_LEARNING_UNCHANGED"
CODE_REDACTED = "SKILL_LEARNING_REDACTED"
CODE_NOT_LEARNED = "SKILL_LEARNING_NOT_LEARNED"
CODE_USER_MODIFIED = "SKILL_LEARNING_USER_MODIFIED"
CODE_VERSION_MISSING = "SKILL_LEARNING_VERSION_MISSING"


# LLM: code 是唯一机器判断字段；detail 只放字段名、guard pattern ID 等结构化事实，不复制 Skill 正文。
# 类用途: 表示一次发布、回滚或删除被闸门拒绝。
class SkillLearningGateError(SkillLearningStoreError):
    # LLM: 构造时复制 detail，避免调用方之后修改原字典。
    # 函数用途: 保存结果码与结构化细节。
    def __init__(self, code: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(code, detail=detail)
        self.code = code
        self.reason = code
        self.detail = dict(detail or {})


# LLM: taken_names 是当前快照里所有来源的 Skill 名（含停用的），新建时不能与之重名，避免遮蔽内置/用户 Skill；
#   max_skills<=0 表示不限；protection_provider / config_provider 由宿主在调用锁外注入最新保护与开关政策。
# 类用途: 打包一次发布需要的决定、来源与闸门参数。
@dataclass(frozen=True)
class PublishRequest:
    decision: SkillLearningDecision
    request: dict[str, object] = field(default_factory=dict)
    taken_names: frozenset[str] = frozenset()
    max_skills: int = 0
    guard_config: object | None = None
    expected_revision: int | None = None
    expected_sha256: str = ""
    promotion_authority: str = "user_control"
    verification_receipt: dict = field(default_factory=dict)
    protection_provider: Callable[[], dict] | None = None
    config_provider: Callable[[], object] | None = None

    # LLM: 允许预审流程复制请求而不尝试深拷贝绑定了宿主锁的闭包包。
    # 函数用途: 复制 PublishRequest 并按引用保留宿主 provider 回调。
    def __deepcopy__(self, memo: dict[int, object]) -> PublishRequest:
        return PublishRequest(
            decision=deepcopy(self.decision, memo),
            request=deepcopy(self.request, memo),
            taken_names=deepcopy(self.taken_names, memo),
            max_skills=self.max_skills,
            guard_config=deepcopy(self.guard_config, memo),
            expected_revision=self.expected_revision,
            expected_sha256=self.expected_sha256,
            promotion_authority=self.promotion_authority,
            verification_receipt=deepcopy(self.verification_receipt, memo),
            protection_provider=self.protection_provider,
            config_provider=self.config_provider,
        )


# LLM: 仅发布器内部传递已安装inode和冻结proof；继承显式to_record，不改变外部账本格式。
# 类用途: 把一次发布的物理身份与证明随事件带到读回核验处。
@dataclass(frozen=True)
class _PublishedSkillEvent(SkillLearningEvent):
    target_dir_identity: tuple[int, int] | None = None
    skill_file_identity: tuple[int, int] | None = None
    verification_proof_json: str = ''


# LLM: 入口只接受 create/update；在 state_lock 外先重读最新实时配置与完整保护集合，避免锁重入与死锁。
# 函数用途: 按决定新建或更新一个自学 Skill，被闸门拒绝时抛 SkillLearningGateError。
def publish_learned_skill(store: SkillLearningStore, publish: PublishRequest) -> SkillLearningEvent:
    policy_enable, policy_mode, effective_max, effective_guard = _resolve_live_policy(publish)
    if policy_enable is False:
        raise SkillLearningGateError(CODE_LEARNING_DISABLED, {"enable_self_learning": False})
    effective = replace(publish, max_skills=effective_max, guard_config=effective_guard)
    protection_snapshot = _read_protection_outside_lock(effective.protection_provider)
    store.directory.mkdir(parents=True, exist_ok=True)
    with store.state_lock():
        registry = store._prepare_clean_registry_locked(store.load_registry())
        if effective.decision.decision == DECISION_CREATE:
            return _create_locked(
                store,
                registry,
                effective,
                maintenance_mode=policy_mode,
                protection_snapshot=protection_snapshot,
            )
        return _update_locked(store, registry, effective)


# LLM: 与 skills._parse_meta 往返无损：只写 name/description，when_to_use 与 tags 为空时整行省略
#   （空值会被解析器当成空列表）；正文原样接在 frontmatter 之后。
# 函数用途: 把决定渲染成完整的 SKILL.md 文本。
def render_learned_skill(decision: SkillLearningDecision) -> str:
    lines = ["---", f"name: {decision.name}", f"description: {decision.description}", "category: learned"]
    if decision.when_to_use:
        lines.append(f"when_to_use: {decision.when_to_use}")
    if decision.tags:
        lines.append(f"tags: [{', '.join(decision.tags)}]")
    return "\n".join([*lines, "---", "", decision.body.rstrip(), ""])


# LLM: 名字闸门与家族封禁在前，随后单事务容量准入（按 R-2 确定性选择或报精确失败码），再渲染、检查并安装；
#   若触发置换发布，淘汰归档与新发布在同一次配额/状态锁与 pending_operation 内完成；仅确认未提交才回滚正文。
# 函数用途: 在锁内新建自学 Skill，必要时原子置换淘汰者，失败时不破坏已提交状态。
def _create_locked(
    store: SkillLearningStore,
    registry: SkillLearningRegistry,
    publish: PublishRequest,
    *,
    maintenance_mode: str = "",
    protection_snapshot: dict | None = None,
) -> SkillLearningEvent:
    name = publish.decision.name
    _require_new_name_locked(store, registry, publish)
    reusable = _family_record_locked(store, registry, publish) if publish.promotion_authority == 'autonomous_verified' else None
    if reusable is not None:
        return _event(EVENT_SKIPPED, reusable, publish, 'SKILL_LEARNING_FAMILY_REUSED')
    publish, redacted = _redacted_publish(publish)
    text = render_learned_skill(publish.decision)
    family = skill_family_fingerprint(text)
    if family in registry.blocked_task_families:
        raise SkillLearningGateError(CODE_FAMILY_BLOCKED, {"skill_name": name, "task_family_fingerprint": family})

    # 先验证草稿可往返并通过 guard，再决定是否需要容量淘汰，防止非法草稿挤掉既有 Skill。
    _stage_and_check(store, publish, text)
    victim_name = _admit_capacity_or_select_victim_locked(
        store,
        registry,
        publish,
        maintenance_mode=maintenance_mode,
        protection_snapshot=protection_snapshot,
    )

    now_iso = utc_now()
    ts = time.time()
    maint = _initial_maintenance(
        publish=publish,
        family=family,
        now_ts=ts,
    )
    record = LearnedSkill(
        name,
        1,
        skill_content_sha256(text),
        now_iso,
        now_iso,
        _runs((), publish.request),
        1,
        str(publish.request.get("operation_id") or ""),
        maint,
        "learned",
    )

    if victim_name is not None:
        # 受害者在最后移出前若恰好被用户修改，底层会交出管辖并允许本事务重选下一受害者或按空余槽位直接发布。
        for _attempt in range(max(1, len(registry.skills) + 1)):
            try:
                asset_updated = _registered_publication(store, registry, record, publish)
                _, _, committed_candidate = store._transition_displace_publish_locked(
                    registry,
                    victim_name=victim_name,
                    candidate_record=record,
                    candidate_text=text,
                    candidate_assets=asset_updated.assets,
                    now=ts,
                )
                _, dir_id, file_id = _read_skill_binding(store, committed_candidate.name, record=committed_candidate)
                return _event(
                    EVENT_PUBLISHED,
                    committed_candidate,
                    publish,
                    CODE_REDACTED if redacted else "",
                    binding=(dir_id, file_id),
                )
            except SkillLearningStoreError as exc:
                if exc.code != "SKILL_LEARNING_VICTIM_USER_MODIFIED":
                    raise
                registry = store._prepare_clean_registry_locked(store.load_registry())
                victim_name = _admit_capacity_or_select_victim_locked(
                    store,
                    registry,
                    publish,
                    maintenance_mode=maintenance_mode,
                    protection_snapshot=protection_snapshot,
                )
                if victim_name is None:
                    break

    target = store.learned_root / name
    installed_identity, file_identity = _stage_check_and_move(store, publish, text, target)
    event = _event(
        EVENT_PUBLISHED,
        record,
        publish,
        CODE_REDACTED if redacted else "",
        binding=(installed_identity, file_identity),
    )
    try:
        with ExitStack() as rollback:
            _save_version_with_rollback(store, name, 1, text, rollback)
            store.save_registry(_registered_publication(store, registry, record, publish))
            rollback.pop_all()
    except BaseException as exc:
        committed = False
        try:
            if _publication_uncommitted(store, record, exc):
                _rollback_created_skill(store, target, installed_identity)
            else:
                committed = True
        except BaseException as rollback_error:
            exc.add_note('installed_skill_rollback_failed:' + type(rollback_error).__name__)
            raise exc from rollback_error
        if not isinstance(exc, (OSError, SkillLearningStoreError)):
            raise
        raise SkillLearningGateError(
            CODE_INSTALL_FAILED,
            {"error": type(exc).__name__, "committed": committed},
        ) from exc
    return event


# LLM: 更新须 CAS/磁盘 hash 一致；保留经过正数 established_at 证实的上一稳定版本指针，绝不把未证实试用版写入 previous_stable；
#   更新产生新的 activation_id 而不改变 incarnation_id，并重置本轮激活期计数与打击基线。
# 函数用途: 在锁内更新自学 Skill，不让非 OS 持久化错误留下与 registry 不一致的新正文。
def _update_locked(store: SkillLearningStore, registry: SkillLearningRegistry,
                   publish: PublishRequest) -> SkillLearningEvent:
    current = _owned_record(store, registry, publish.decision.name)
    if publish.promotion_authority == 'autonomous_verified' and current.layout != 'learned':
        raise SkillLearningGateError(CODE_NOT_LEARNED)
    if current.maintenance.get("state") in {"disabled", "archived"}:
        raise SkillLearningGateError("SKILL_LEARNING_TARGET_DISABLED")
    if publish.expected_revision != current.revision or publish.expected_sha256 != current.sha256:
        raise SkillLearningGateError("SKILL_LEARNING_REVISION_CONFLICT")
    publish, redacted = _redacted_publish(publish)
    text = render_learned_skill(publish.decision)
    sha = skill_content_sha256(text)
    if sha == current.sha256:
        _, directory_identity, file_identity = _read_skill_binding(store, current.name, record=current)
        return _event(EVENT_SKIPPED, current, publish, CODE_UNCHANGED, binding=(directory_identity, file_identity))
    family = skill_family_fingerprint(text)
    if family in registry.blocked_task_families:
        raise SkillLearningGateError(
            CODE_FAMILY_BLOCKED,
            {"skill_name": current.name, "task_family_fingerprint": family},
        )
    path = store.skill_path(current.name, record=current)
    previous = read_text_beneath(store.skills_root, path.relative_to(store.skills_root).parts)
    _stage_and_check(store, publish, text)
    ts = time.time()
    next_maint = _updated_maintenance(current, publish=publish, family=family, now_ts=ts)
    record = replace(
        current,
        version=current.revision + 1,
        revision=current.revision + 1,
        sha256=sha,
        updated_at=utc_now(),
        source_run_ids=_runs(current.source_run_ids, publish.request),
        last_operation_id=str(publish.request.get("operation_id") or ""),
        maintenance=next_maint,
    )
    try:
        store.check_write(path, text)
        replace_skill_file(path, text)
        actual, directory_identity, file_identity = _read_skill_binding(store, current.name, record=current)
        if actual != text:
            raise SkillLearningGateError('LEARNING_FORMAL_REVISION_CHANGED', {'carrier': 'skill'})
        event = _event(EVENT_UPDATED, record, publish, CODE_REDACTED if redacted else '',
                       binding=(directory_identity, file_identity))
        with ExitStack() as rollback:
            _save_version_with_rollback(store, current.name, record.version, text, rollback)
            store.save_registry(_registered_publication(store, registry, record, publish))
            rollback.pop_all()
    except BaseException as exc:
        committed = False
        try:
            if _publication_uncommitted(store, record, exc):
                replace_skill_file(path, previous)
            else:
                committed = True
        except BaseException as rollback_error:
            exc.add_note('updated_skill_rollback_failed:' + type(rollback_error).__name__)
            raise exc from rollback_error
        if not isinstance(exc, (OSError, SkillLearningStoreError)):
            raise
        raise SkillLearningGateError(
            CODE_INSTALL_FAILED,
            {"error": type(exc).__name__, "committed": committed},
        ) from exc
    return event


# LLM: 创建/更新补偿在原state锁内读原始registry，不使用磁盘高水位迁移；已提交、身份变化或读回不明时不破坏正文。
# 函数用途: 只在确认本次修订尚未登记时允许回滚正式安装，异常仅附首因备注。
def _publication_uncommitted(store, record, primary):
    try:
        raw = read_text_beneath(store.directory, ('registry.json',))
        current = json.loads(raw or '{}').get('skills', {}).get(record.name, {})
        revision = int(current.get('revision', current.get('version', 0)))
        if revision < record.revision:
            return True
        if revision != record.revision or current.get('sha256') != record.sha256:
            primary.add_note('installed_skill_rollback_skipped:registry_identity_changed')
    except BaseException as read_error:
        primary.add_note('installed_skill_commit_state_unknown:' + type(read_error).__name__)
    return False


# LLM: 调用方持state锁并在registry提交后撤销回调；只清事务前不存在、内容仍匹配且未登记的版本；提交依据是持久化revision，不用磁盘高水位迁移值。
# 函数用途: 写不可变版本并加入原事务回滚，版本写后或登记失败都能干净重试。
def _save_version_with_rollback(store, name, revision, text, rollback):
    if store.read_version(name, revision) is None:
        # LLM: 仍在原state锁内运行；读回不明确或清理失败仅附原异常note，不误删版本或掩盖业务首因。
        # 函数用途: 撤销本次未提交的匹配版本，保留已有或已登记的版本文件。
        def remove_uncommitted(exc_type, primary, traceback):
            try:
                raw = read_text_beneath(store.directory, ('registry.json',))
                current = json.loads(raw or '{}').get('skills', {}).get(name, {})
                if int(current.get('revision', current.get('version', 0))) >= revision:
                    return False
                if store.read_version(name, revision) == text:
                    unlink_file_beneath(store.directory, ('versions', name, f'v{revision}.md'))
            except BaseException as cleanup_error:
                if primary is None:
                    raise
                primary.add_note('uncommitted_version_cleanup_failed:' + type(cleanup_error).__name__)
            return False

        rollback.push(remove_uncommitted)
    store.save_version(name, revision, text)


# LLM: 调用方持发布锁；只保存真实model/adopted的run+hash身份，去重但不把旧版质量带到新版。
# 函数用途: 跨连续更新保留采用过的版本，避免无目标回执遗忘旧版造成误归因。
def _historical_adoptions(current):
    facts = [*current.maintenance.get('historical_adoptions', []), *current.maintenance.get('observations', [])]
    pairs = {(fact.get('run_id'), fact.get('sha256')) for fact in facts if isinstance(fact, dict)
             and fact.get('authority') == 'model' and fact.get('outcome') == 'adopted'
             and fact.get('run_id') and fact.get('sha256')}
    return [{'authority':'model', 'outcome':'adopted', 'run_id':run, 'sha256':sha} for run, sha in sorted(pairs)]


# LLM: 原state锁内只按同规范ID和完整渲染hash复用；family不证明身份，停用/归档/用户改动仍拒绝。
# 函数用途: 查找当前候选的精确已发布版本，不将改名或不同内容映射到旧Skill。
def _family_record_locked(store, registry, publish):
    name = publish.decision.name
    current = registry.skills.get(name)
    if current is None or name in registry.blocked_names or current.maintenance.get('state') in {'disabled', 'archived'}:
        return None
    if current.sha256 != skill_content_sha256(render_learned_skill(publish.decision)):
        return None
    return _owned_record(store, registry, name)


# LLM: 调用方持原发布锁；四类出处投影共用registry，登记失败由原创建/更新事务恢复正文。
# 函数用途: 记录发布方法的语义族与来源，再一次保存当前技能版本。
def _registered_publication(store, registry, record, publish):
    from dataclasses import asdict

    from .asset_promotion import asset_identity, register_asset_locked

    source = _business_method_source(publish.request)
    if not publish.request.get('completion_ref'):
        # 原提案只携带四个请求字段；确认沿准确资产身份和request_key取回同源出处，不猜latest。
        identity = asset_identity('skill', {**asdict(publish.decision), 'name': record.name}, store.skills_root.parent)
        refs = registry.assets.get(identity, {}).get('provenance', ())
        bound = [ref for ref in refs if ref.get('completion_ref')
                 and ref.get('request_key') == publish.request.get('request_key')
                 and ref.get('task_id') == publish.request.get('task_id')]
        if bound:
            source = {**bound[-1], **source}
    registry, _ = register_asset_locked(store, registry, kind='skill', asset_id='owner:'+record.name,
        asset=asdict(publish.decision), sha256=record.sha256, state='trial',
        source=source)
    return replace(registry, skills={**registry.skills, record.name:record})


def _business_method_source(request):
    """只保留宿主方法出处的引用；不写业务正文，不把来源改为user或人格授权。"""
    source = {'origin': 'S3', 'run_id': str(request.get('run_id', ''))}
    if request.get('completion_ref'):
        source.update({key: request[key] for key in (
            'request_key', 'owner_id', 'task_id', 'runtime_task_id', 'attempt_id', 'completion_ref', 'claim_ref',
            'business_context', 'learning_scope', 'result_refs', 'asset_version_refs') if key in request})
    return source


def retain_proposed_business_source(store, publish):
    """FIFO删源前在原资产登记保留待确认出处；不新建账，不改变提案/确认身份或批准结果。"""
    from dataclasses import asdict

    from .asset_promotion import asset_identity, register_asset_locked
    from .skill_learning_proposals import _redacted_publish

    # 与原提案使用同一脱敏草稿计算身份；只保存引用和hash，不复制草稿正文。
    publish, _redacted = _redacted_publish(publish)
    asset = asdict(publish.decision)
    with store.state_lock():
        registry = store.load_registry()
        identity = asset_identity('skill', asset, store.skills_root.parent)
        previous = registry.assets.get(identity, {})
        registry, _ = register_asset_locked(store, registry, kind='skill', asset_id='owner:'+publish.decision.name,
            asset=asset, source=_business_method_source(publish.request),
            sha256=skill_content_sha256(render_learned_skill(publish.decision)),
            state=previous.get('state', 'candidate'))
        store.save_registry(registry)


# 回滚仍持原 state/配额锁；通过 pending_operation 推进可恢复版本替换；v1 回滚等同用户删除。
def revert_learned_skill(store: SkillLearningStore, name: str) -> SkillLearningEvent:
    with store.state_lock():
        registry = store._prepare_clean_registry_locked(store.load_registry())
        current = _owned_record(store, registry, name, code=CODE_USER_MODIFIED)
        if current.version <= 1:
            _, event = store._transition_remove_locked(registry, current.name, reason_code="manual_revert_v1")
            return event
        target_ver = current.version - 1
        text = store.read_version(name, target_ver)
        if text is None:
            raise SkillLearningGateError(CODE_VERSION_MISSING, {"version": target_ver})
        target_sha = skill_content_sha256(text)
        ts = time.time()
        next_maint = dict(current.maintenance)
        hist_adoptions = _historical_adoptions(current)
        stable = next_maint.get("previous_stable")
        if is_proven_stable_version(stable) and stable["version"] == target_ver and stable["sha256"] == target_sha:
            est_at = float(stable["established_at"])
            next_maint.pop("previous_stable", None)
            next_maint.pop("previous_verification", None)
            next_maint.update({
                "state": "active",
                "quality": "established",
                "quality_basis": "rollback_established",
                "established_at": est_at,
                "helped_count": 0,
                "hurt_count": 0,
                "independent_uses": 0,
                "independent_uses_coverage": "exact",
                "uses_since_hurt": 0,
                "strike_epoch": 0,
                "activation_id": uuid.uuid4().hex,
                "epoch_started_at": ts,
                "historical_adoptions": hist_adoptions,
                "observations": [],
                "rolled_back_from": current.sha256,
            })
        else:
            next_maint.update({
                "state": "active",
                "quality": "trial",
                "quality_basis": "manual_revert",
                "helped_count": 0,
                "hurt_count": 0,
                "independent_uses": 0,
                "independent_uses_coverage": "exact",
                "uses_since_hurt": 0,
                "strike_epoch": 0,
                "activation_id": uuid.uuid4().hex,
                "epoch_started_at": ts,
                "historical_adoptions": hist_adoptions,
                "observations": [],
                "rolled_back_from": current.sha256,
            })
            next_maint.pop("established_at", None)
        next_maint["task_family_fingerprint"] = skill_family_fingerprint(text)
        record = replace(
            current,
            version=target_ver,
            revision=current.revision + 1,
            sha256=target_sha,
            updated_at=utc_now(),
            maintenance=next_maint,
        )
        _, committed = store._transition_rollback_locked(
            registry,
            current=current,
            next_record=record,
            target_version=target_ver,
            target_sha256=target_sha,
            target_text=text,
            reason_code="manual_revert",
            now=ts,
        )
    return SkillLearningEvent(EVENT_REVERTED, "", name, committed.version, committed.sha256)


# LLM: 仅允许回退到具备正数 established_at 的已证实上一稳定版本；恢复 established 质量与新 activation_id 并清零打击计数。
# 函数用途: 供三次负反馈或维护路径调用的已证实稳定回滚原语（需已持 state_lock）。
def _rollback_to_proven_stable_locked(
    store: SkillLearningStore,
    registry: SkillLearningRegistry,
    current: LearnedSkill,
    *,
    reason_code: str = "third_strike_rollback",
    now: float | None = None,
) -> tuple[SkillLearningRegistry, LearnedSkill]:
    stable = current.maintenance.get("previous_stable")
    if not is_proven_stable_version(stable):
        raise SkillLearningGateError(
            CODE_VERSION_MISSING,
            {"skill_name": current.name, "reason": "no_proven_stable_version"},
        )
    owned = _owned_record(store, registry, current.name, code=CODE_USER_MODIFIED)
    target_ver = int(stable["version"])
    target_sha = str(stable["sha256"])
    text = store.read_version(current.name, target_ver)
    if text is None or skill_content_sha256(text) != target_sha:
        raise SkillLearningGateError(CODE_VERSION_MISSING, {"skill_name": current.name, "version": target_ver})
    ts = time.time() if now is None else float(now)
    maint = dict(owned.maintenance)
    hist_adoptions = _historical_adoptions(owned)
    maint.pop("previous_stable", None)
    maint.pop("previous_verification", None)
    maint.update({
        "state": "active",
        "quality": "established",
        "quality_basis": "rollback_established",
        "established_at": float(stable["established_at"]),
        "helped_count": 0,
        "hurt_count": 0,
        "independent_uses": 0,
        "independent_uses_coverage": "exact",
        "uses_since_hurt": 0,
        "strike_epoch": 0,
        "activation_id": uuid.uuid4().hex,
        "epoch_started_at": ts,
        "historical_adoptions": hist_adoptions,
        "observations": [],
        "rolled_back_from": owned.sha256,
        "task_family_fingerprint": skill_family_fingerprint(text),
    })
    next_record = replace(
        owned,
        version=target_ver,
        revision=owned.revision + 1,
        sha256=target_sha,
        updated_at=utc_now(),
        maintenance=maint,
    )
    return store._transition_rollback_locked(
        registry,
        current=owned,
        next_record=next_record,
        target_version=target_ver,
        target_sha256=target_sha,
        target_text=text,
        reason_code=reason_code,
        now=ts,
    )


# LLM: 无已证实稳定前驱可退时的三次负反馈退场：安全核验归属后驱逐目录、封禁名字与任务家族、并清理 skill 专属资产行。
# 函数用途: 供三次负反馈或维护路径调用的安全退场原语（需已持 state_lock）。
def _retire_third_strike_locked(
    store: SkillLearningStore,
    registry: SkillLearningRegistry,
    current: LearnedSkill,
    *,
    reason_code: str = "third_strike_retire",
    now: float | None = None,
) -> SkillLearningRegistry:
    try:
        path = store.canonical_skill_path(current.name, record=current)
        root = store.directory if current.maintenance.get("state") == "archived" else store.skills_root
        text = read_text_beneath(root, path.relative_to(root).parts)
        if text is None:
            text = store.read_version(current.name, current.version)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SkillLearningStoreError(
            CODE_EVIDENCE_INCOMPLETE,
            f"unreadable managed skill file for {current.name}",
            detail={"skill_name": current.name, "reason": type(exc).__name__},
        ) from exc
    family = str(current.maintenance.get("task_family_fingerprint") or "")
    if not family and text is not None:
        family = skill_family_fingerprint(text)
    return store._transition_evict_locked(
        registry,
        current.name,
        now=now,
        block=True,
        block_family=family,
        reason_code=reason_code,
    )


# LLM: 删除把目录移到 removed/ 归档（不物理删除），从登记表去掉并把名字与任务家族加入封禁列表，有界清理旧版本；
#   用户改过的 Skill 也允许删除（用户显式动作），但不在登记表里的名字拒绝。
# 函数用途: 删除一个自学 Skill，返回账本事件。
def remove_learned_skill(store: SkillLearningStore, name: str) -> SkillLearningEvent:
    with store.state_lock():
        registry = store._prepare_clean_registry_locked(store.load_registry())
        if name not in registry.skills:
            raise SkillLearningGateError(CODE_NOT_LEARNED, {"skill_name": name})
        _, event = store._transition_remove_locked(registry, name, reason_code="user_removed")
        return event


# LLM: 锁内委托给同一可恢复删除过渡，保留兼容性调用签名。
# 函数用途: 在锁内完成删除并返回事件。
def _remove_locked(store: SkillLearningStore, registry: SkillLearningRegistry,
                   current: LearnedSkill) -> SkillLearningEvent:
    _, event = store._transition_remove_locked(registry, current.name, reason_code="user_removed")
    return event


# LLM: 删过的名字最先判（用户意愿优先），其次快照重名与目录已存在。
# 函数用途: 校验新建名字未被封禁、未重名且目标目录空。
def _require_new_name_locked(store: SkillLearningStore, registry: SkillLearningRegistry, publish: PublishRequest) -> None:
    name = publish.decision.name
    if name in registry.blocked_names:
        raise SkillLearningGateError(CODE_NAME_BLOCKED, {"skill_name": name})
    taken = name in publish.taken_names or name in registry.skills
    if taken or (store.learned_root / name).exists() or (store.learned_root / name).is_symlink():
        raise SkillLearningGateError(CODE_NAME_TAKEN, {"skill_name": name})


# LLM: 最终容量准入（在 state_lock 内执行）：经 store.managed_records + live_count 验证真实在役机器槽位；
#   不完整证据、observe 模式与全员受保护各有独立结构化错误码，不再混为一谈。
# 函数用途: 检查容量或按 R-2 优先级选定一名非受保护的淘汰者。
def _admit_capacity_or_select_victim_locked(
    store: SkillLearningStore,
    registry: SkillLearningRegistry,
    publish: PublishRequest,
    *,
    maintenance_mode: str = "",
    protection_snapshot: dict | None = None,
) -> str | None:
    if publish.max_skills <= 0:
        return None
    try:
        managed = store.managed_records(registry, include_archived=False)
    except SkillLearningStoreError as exc:
        if exc.code == CODE_EVIDENCE_INCOMPLETE:
            raise SkillLearningGateError(CODE_EVIDENCE_INCOMPLETE, exc.detail) from exc
        raise
    live = registry.live_count(managed_names=managed.keys())
    if live < publish.max_skills:
        return None
    if maintenance_mode == "observe":
        raise SkillLearningGateError(
            CODE_MAINTENANCE_OBSERVE_ONLY,
            {"limit": publish.max_skills, "live": live, "mode": "observe"},
        )
    if publish.protection_provider is None:
        raise SkillLearningGateError(CODE_LIMIT_REACHED, {"limit": publish.max_skills, "live": live})
    if not isinstance(protection_snapshot, dict):
        raise SkillLearningGateError(
            CODE_EVIDENCE_INCOMPLETE,
            {"reason": "protection_unavailable", "limit": publish.max_skills, "live": live},
        )
    complete = (
        bool(protection_snapshot["protection_complete"])
        if "protection_complete" in protection_snapshot
        else bool(protection_snapshot.get("complete"))
    )
    if not complete:
        raise SkillLearningGateError(
            CODE_EVIDENCE_INCOMPLETE,
            {"reason": "protection_incomplete", "limit": publish.max_skills, "live": live},
        )
    protected_raw = {str(item) for item in (protection_snapshot.get("protected_ids") or ())}
    for key in ("pinned_ids", "active_ids"):
        protected_raw.update(str(item) for item in (protection_snapshot.get(key) or ()))
    if "*" in protected_raw:
        raise SkillLearningGateError(
            CODE_LIMIT_REACHED,
            {"limit": publish.max_skills, "live": live, "all_protected": True},
        )
    eligible: list[tuple[tuple[object, ...], str]] = []
    for candidate_name, record in sorted(managed.items()):
        maint = record.maintenance
        if (
            maint.get("pinned") is True
            or maint.get("state") in {"disabled", "archived"}
            or candidate_name in protected_raw
            or f"owner:{candidate_name}" in protected_raw
        ):
            continue
        eligible.append((_victim_rank_key(candidate_name, record), candidate_name))
    if not eligible:
        raise SkillLearningGateError(
            CODE_LIMIT_REACHED,
            {"limit": publish.max_skills, "live": live, "all_protected": True},
        )
    eligible.sort(key=lambda item: item[0])
    return eligible[0][1]


def _victim_rank_key(name: str, record: LearnedSkill) -> tuple[int, float, int, float, str, str]:
    maint = record.maintenance
    uses = int(maint.get("independent_uses") or 0)
    helped = int(maint.get("helped_count") or 0)
    last_used = float(maint.get("last_used_at") or 0.0)
    legacy_upper = maint.get("legacy_last_activity_upper_bound")
    has_activity = (
        maint.get("ever_used") is True
        or uses > 0
        or helped > 0
        or last_used > 0.0
        or legacy_upper is not None
    )
    proven_never_used = (
        not has_activity
        and maint.get("independent_uses_coverage") == "exact"
        and maint.get("ever_used") in (False, None)
    )
    created_ts = _iso_ts(record.created_at)
    baseline_ts = float(maint.get("baseline_at") or 0.0) or created_ts
    if proven_never_used:
        return (0, baseline_ts, 0, created_ts, record.created_at, name)
    real_use_ts = last_used or (float(legacy_upper) if legacy_upper is not None else 0.0)
    tier = 1 if maint.get("state") == "cooled" else 2
    return (tier, real_use_ts, uses, created_ts, record.created_at, name)


def _iso_ts(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _initial_maintenance(*, publish: PublishRequest, family: str, now_ts: float) -> dict[str, object]:
    return {
        "state": "trial",
        "quality": "trial",
        "quality_basis": "trial_unproven",
        "pinned": False,
        "baseline_at": float(now_ts),
        "independent_uses": 0,
        "independent_uses_coverage": "exact",
        "ever_used": False,
        "uses_since_hurt": 0,
        "hurt_count": 0,
        "strike_epoch": 0,
        "incarnation_id": uuid.uuid4().hex,
        "activation_id": uuid.uuid4().hex,
        "epoch_started_at": float(now_ts),
        "usage_receipt_sequence": 0,
        "ownership": "machine",
        "observations": [],
        "promotion_authority": publish.promotion_authority,
        "verification_receipt": publish.verification_receipt,
        "promotion_history": ["observed", "candidate", "validated", "trial"],
        "task_family_fingerprint": family,
    }


def _updated_maintenance(
    current: LearnedSkill,
    *,
    publish: PublishRequest,
    family: str,
    now_ts: float,
) -> dict[str, object]:
    cur = dict(current.maintenance)
    est_at = cur.get("established_at")
    current_proven = (
        cur.get("quality") == "established"
        and isinstance(est_at, (int, float))
        and not isinstance(est_at, bool)
        and float(est_at) > 0
    )
    prior_stable = cur.get("previous_stable")
    if current_proven:
        next_stable: dict[str, object] | None = {
            "version": current.version,
            "revision": current.revision,
            "sha256": current.sha256,
            "established_at": float(est_at),
        }
        prev_verif = {key: cur.get(key) for key in ("promotion_authority", "verification_receipt")}
    elif is_proven_stable_version(prior_stable):
        next_stable = dict(prior_stable)
        prev_verif = cur.get("previous_verification", {})
    else:
        next_stable = None
        prev_verif = None

    incarnation_id = str(cur.get("incarnation_id") or "") or uuid.uuid4().hex
    next_maint: dict[str, object] = {
        **cur,
        "state": "trial",
        "quality": "trial",
        "quality_basis": "trial_unproven",
        "baseline_at": float(now_ts),
        "helped_count": 0,
        "hurt_count": 0,
        "uses_since_hurt": 0,
        "independent_uses": 0,
        "independent_uses_coverage": "exact",
        "strike_epoch": 0,
        "epoch_started_at": float(now_ts),
        "incarnation_id": incarnation_id,
        "activation_id": uuid.uuid4().hex,
        "ownership": "machine",
        "promotion_authority": publish.promotion_authority,
        "verification_receipt": publish.verification_receipt,
        "promotion_history": ["observed", "candidate", "validated", "trial"],
        "historical_adoptions": _historical_adoptions(current),
        "observations": [],
        "task_family_fingerprint": family,
    }
    next_maint.pop("established_at", None)
    if next_stable is not None:
        next_maint["previous_stable"] = next_stable
        next_maint["previous_verification"] = prev_verif
    else:
        next_maint.pop("previous_stable", None)
        next_maint.pop("previous_verification", None)
    return next_maint


_MISSING_SENTINEL = object()


def _cfg_lookup(cfg: object, key: str, default: object = _MISSING_SENTINEL) -> object:
    if isinstance(cfg, Mapping):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _resolve_live_policy(publish: PublishRequest) -> tuple[bool, str, int, object | None]:
    if publish.config_provider is None:
        return True, "", publish.max_skills, publish.guard_config
    try:
        cfg = publish.config_provider()
    except Exception as exc:
        raise SkillLearningGateError(
            CODE_EVIDENCE_INCOMPLETE,
            {"reason": "config_provider_failed", "error": type(exc).__name__},
        ) from exc
    if cfg is None:
        return True, "", publish.max_skills, publish.guard_config
    raw_enable = _cfg_lookup(cfg, "enable_self_learning")
    enabled = True if raw_enable is _MISSING_SENTINEL else (raw_enable is not False)
    raw_mode = _cfg_lookup(cfg, "self_learning_maintenance_mode")
    if raw_mode is _MISSING_SENTINEL:
        raw_mode = _cfg_lookup(cfg, "maintenance_mode", "")
    mode = str(raw_mode or "").strip().lower() if raw_mode is not _MISSING_SENTINEL else ""
    raw_max = _cfg_lookup(cfg, "self_learning_max_skills")
    if raw_max is _MISSING_SENTINEL:
        raw_max = _cfg_lookup(cfg, "max_learned_skills")
    if raw_max is _MISSING_SENTINEL:
        raw_max = _cfg_lookup(cfg, "max_skills")
    effective_max = publish.max_skills if raw_max is _MISSING_SENTINEL or raw_max is None else int(raw_max)
    raw_guard = _cfg_lookup(cfg, "guard_config")
    if raw_guard is _MISSING_SENTINEL:
        if (
            _cfg_lookup(cfg, "skill_guard_max_files") is not _MISSING_SENTINEL
            or _cfg_lookup(cfg, "skill_guard_max_size_kb") is not _MISSING_SENTINEL
        ):
            raw_guard = cfg
        else:
            candidate_policy = _cfg_lookup(cfg, "runtime_guard_policy")
            if candidate_policy is None or (
                candidate_policy is not _MISSING_SENTINEL
                and (
                    _cfg_lookup(candidate_policy, "skill_guard_max_files") is not _MISSING_SENTINEL
                    or _cfg_lookup(candidate_policy, "skill_guard_max_size_kb") is not _MISSING_SENTINEL
                )
            ):
                raw_guard = candidate_policy
    effective_guard = publish.guard_config if raw_guard is _MISSING_SENTINEL else raw_guard
    return enabled, mode, effective_max, effective_guard


def _read_protection_outside_lock(provider: Callable[[], dict] | None) -> dict | None:
    if provider is None:
        return None
    try:
        result = provider()
    except Exception:
        return {"protection_complete": False, "protected_ids": ()}
    return result if isinstance(result, dict) else {"protection_complete": False, "protected_ids": ()}


# LLM: 所有权 = 登记在册 + 磁盘 SKILL.md 存在 + 其 hash 等于登记值；优先使用已知 record 避免重复读 registry。
# 函数用途: 取出一个仍归自动流程所有的登记记录。
def _owned_record(store: SkillLearningStore, registry: SkillLearningRegistry, name: str,
                  code: str = CODE_TARGET_USER_OWNED) -> LearnedSkill:
    current = registry.skills.get(name)
    if current is None:
        raise SkillLearningGateError(CODE_TARGET_MISSING, {"skill_name": name})
    path = store.skill_path(name, record=current)
    if path.is_symlink() or not path.is_file():
        raise SkillLearningGateError(code, {"skill_name": name, "state": "missing"})
    body = read_text_beneath(store.skills_root, path.relative_to(store.skills_root).parts)
    if body is None or skill_content_sha256(body) != current.sha256:
        raise SkillLearningGateError(code, {"skill_name": name, "state": "user_modified"})
    return current


# LLM: 已知密钥形态、Authorization 值、URL 口令、私钥等按源码模式遮蔽（不误伤普通赋值示例）；
#   任一字段被改动就返回 redacted=True，发布事件会带 SKILL_LEARNING_REDACTED 供人核查。
# 函数用途: 发布前对 frontmatter 与正文做脱敏，返回换上脱敏决定的发布请求。
def _redacted_publish(publish: PublishRequest) -> tuple[PublishRequest, bool]:
    decision = publish.decision
    safe = replace(
        decision,
        description=redact_sensitive_text(decision.description, code_file=True),
        when_to_use=redact_sensitive_text(decision.when_to_use, code_file=True),
        body=redact_sensitive_text(decision.body, code_file=True),
    )
    return replace(publish, decision=safe), safe != decision


# LLM: state锁内先移到data准备区，stage收尾后才进正式名；移动后核验失败（含目标打开失败）用现有父FD隔离实际对象，不拿原身份删除。
# 函数用途: 发布已扫描Skill；未能验证的对象移出正式发现根，返回原目录及文件身份供回执和补偿。
def _stage_check_and_move(store: SkillLearningStore, publish: PublishRequest, text: str, target: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    from uuid import uuid4

    from ..common.nofollow_fs import _openat_directory, open_directory_beneath
    from .asset_promotion import _stage_dir, _StageFDStack

    moved_identity, placement = None, None
    prepared = store.skills_root.parent / 'data' / 'skill_learning' / ('.publish_' + uuid4().hex)
    try:
        with _StageFDStack() as descriptors:
            with _stage_dir(store.skills_root.parent, with_fd=True) as (stage, stage_fd):
                name, identity = _staged_skill(store, stage, publish, text, stage_fd=stage_fd)
                target_fd = open_directory_beneath(store.skills_root.parent, ('skills', 'learned'), create=True, require_dir_fd=True)
                descriptors.callback(os.close, target_fd)
                staged_fd = _openat_directory(stage_fd, name)
                descriptors.callback(os.close, staged_fd)
                observed = os.fstat(staged_fd)
                staged_text, file_identity = _read_staged_text(staged_fd, with_identity=True)
                if (observed.st_dev, observed.st_ino) != identity or staged_text != text:
                    raise SkillLearningGateError(CODE_DRAFT_INVALID, {'field': 'staged_identity'})
                prepared_fd = open_directory_beneath(store.skills_root.parent, ('data', 'skill_learning'), require_dir_fd=True)
                descriptors.callback(os.close, prepared_fd)
                os.rename(name, prepared.name, src_dir_fd=stage_fd, dst_dir_fd=prepared_fd)
                placement = prepared
                moved_identity = identity
                isolated = os.stat(prepared.name, dir_fd=prepared_fd, follow_symlinks=False)
                if (isolated.st_dev, isolated.st_ino) != identity:
                    raise SkillLearningGateError(CODE_INSTALL_FAILED, {'field': 'prepared_identity'})
            # 原stage清理完成后再发表正式名字；随机隔离名不属于Skill发现根。
            isolated = os.stat(prepared.name, dir_fd=prepared_fd, follow_symlinks=False)
            if (isolated.st_dev, isolated.st_ino) != identity or _read_staged_text(staged_fd, with_identity=True) != (text, file_identity):
                raise SkillLearningGateError(CODE_DRAFT_INVALID, {'field': 'prepared_identity'})
            try:
                os.stat(target.name, dir_fd=target_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SkillLearningGateError(CODE_NAME_TAKEN, {'skill_name': publish.decision.name})
            os.rename(prepared.name, target.name, src_dir_fd=prepared_fd, dst_dir_fd=target_fd)
            placement = target
            installed_fd = None
            try:
                installed_fd = _openat_directory(target_fd, target.name)
                descriptors.callback(os.close, installed_fd)
                installed = os.fstat(installed_fd)
                if ((installed.st_dev, installed.st_ino) != identity
                        or _read_staged_text(installed_fd, with_identity=True) != (text, file_identity)):
                    raise SkillLearningGateError(CODE_INSTALL_FAILED, {'field': 'installed_identity'})
            except BaseException as validation_error:
                # 正式名此前为空且rename已成功；即使没有取得目标FD，也不能交给原身份删除补偿。
                moved_identity = None
                try:
                    _quarantine_unverified_skill(store, target.name, target_fd, prepared_fd, installed_fd)
                except BaseException as quarantine_error:
                    validation_error.add_note('unverified_skill_quarantine_failed:' + type(quarantine_error).__name__)
                    raise validation_error from quarantine_error
                raise
        return moved_identity, file_identity
    except BaseException as exc:
        if moved_identity is not None:
            try:
                _rollback_created_skill(store, placement, moved_identity)
            except BaseException as rollback_error:
                exc.add_note('installed_skill_rollback_failed:' + type(rollback_error).__name__)
                raise exc from rollback_error
        if isinstance(exc, OSError):
            raise SkillLearningGateError(CODE_INSTALL_FAILED, {'error': type(exc).__name__}) from exc
        raise


# LLM: 仅在本次rename前正式名为空、移动后核验失败时调用；有目标FD用fstat，否则父FD下nofollow stat；固定父FD改名核实际inode，不删未知内容，成功或失败均记定位欠账。
# 函数用途: 不依赖再次分配目录FD，将移入正式根的未验证对象原样隔离，避免EMFILE绕过隔离。
def _quarantine_unverified_skill(store, name, target_fd, data_fd, actual_fd):
    from uuid import uuid4

    from ..common.nofollow_fs import write_text_atomic_beneath

    identity = None
    quarantine = '.quarantine_' + uuid4().hex
    location = ['skills', 'learned', name]
    try:
        actual = os.fstat(actual_fd) if actual_fd is not None else os.stat(name, dir_fd=target_fd, follow_symlinks=False)
        identity = (actual.st_dev, actual.st_ino)
        os.rename(name, quarantine, src_dir_fd=target_fd, dst_dir_fd=data_fd)
        location = ['data', 'skill_learning', quarantine]
        moved = os.stat(quarantine, dir_fd=data_fd, follow_symlinks=False)
        if (moved.st_dev, moved.st_ino) != identity:
            raise SkillLearningGateError(CODE_INSTALL_FAILED, {'field': 'quarantine_identity'})
    except BaseException as error:
        try:
            write_text_atomic_beneath(store.skills_root.parent, ('data', 'skill_learning', 'cleanup_pending', quarantine + '.json'),
                json.dumps({'schema_version': 1, 'subject': 'unverified_skill', 'location': location,
                            'identity': identity, 'quarantine': quarantine, 'status': 'cleanup_pending'}))
        except Exception as pending_error:
            error.add_note('quarantine_pending_write_failed:' + type(pending_error).__name__)
        raise
    else:
        write_text_atomic_beneath(store.skills_root.parent, ('data', 'skill_learning', 'cleanup_pending', quarantine + '.json'),
            json.dumps({'schema_version': 1, 'subject': 'unverified_skill', 'location': location,
                        'identity': identity, 'quarantine': quarantine, 'status': 'cleanup_pending'}))


# LLM: 只删除data准备区或learned下本次已验证inode；替代物不删，失败记录位置/身份；正式rename的已知换源由专门隔离函数处理。
# 函数用途: stage收尾或登记失败后撤销已验证安装；未能确认归属的对象保留并登记欠账。
def _rollback_created_skill(store, target, identity):
    import json
    from uuid import uuid4

    from ..common.nofollow_fs import (
        _openat_directory,
        open_directory_beneath,
        write_text_atomic_beneath,
    )
    from .asset_promotion import _purge_directory, _StageFDStack

    tombstone = '.purge_' + target.name + '_' + uuid4().hex
    parent_parts = target.parent.relative_to(store.skills_root.parent).parts
    if parent_parts not in {('skills', 'learned'), ('data', 'skill_learning')}:
        raise SkillLearningGateError(CODE_INSTALL_FAILED, {'field': 'rollback_scope'})
    try:
        with _StageFDStack() as descriptors:
            parent_fd = open_directory_beneath(store.skills_root.parent, parent_parts, require_dir_fd=True)
            descriptors.callback(os.close, parent_fd)
            child = _openat_directory(parent_fd, target.name)
            descriptors.callback(os.close, child)
            _purge_directory(parent_fd, target.name, identity, child, tombstone)
    except BaseException as error:
        try:
            write_text_atomic_beneath(store.skills_root.parent, ('data', 'skill_learning', 'cleanup_pending', tombstone + '.json'),
                json.dumps({'schema_version': 1, 'subject': 'skill_move', 'location': [*parent_parts, target.name],
                            'tombstone': tombstone, 'identity': identity, 'status': 'cleanup_pending'}))
        except (OSError, ValueError) as pending_error:
            error.add_note('rollback_cleanup_pending_write_failed:' + type(pending_error).__name__)
        raise


# LLM: 原state/配额锁内只经本次固定stage FD写读扫描，退出精确清理；write或清理报OSError转为结构化安装错误并保留cleanup_pending。
# 函数用途: 在隔离对象内检查更新正文，不重新解析可变stage路径。
def _stage_and_check(store: SkillLearningStore, publish: PublishRequest, text: str) -> None:
    from .asset_promotion import _stage_dir
    try:
        with _stage_dir(store.skills_root.parent, with_fd=True) as (stage, stage_fd):
            _staged_skill(store, stage, publish, text, stage_fd=stage_fd)
    except OSError as exc:
        # 预检暂存未移动任何正文；写入或清理失败统一给结构化安装错误，清理回执由 _stage_dir 保留。
        raise SkillLearningGateError(CODE_INSTALL_FAILED, {"error": type(exc).__name__, "phase": "stage_check"}) from exc


# LLM: 只经固定stage FD创建/读取单文件Skill；共用frontmatter codec并显式保留description必填门，以agent_generated扫描，返回名字与inode。
# 函数用途: 将正文写入真正暂存对象并核对同一字节的格式/安全门，绝不按可变stage Path写读。
def _staged_skill(store: SkillLearningStore, stage: Path, publish: PublishRequest, text: str, *, stage_fd: int):
    from ..common.nofollow_fs import _openat_directory, _validate_relative_parts
    from .asset_promotion import _StageFDStack, _write_stage_bytes

    decision = publish.decision
    _validate_relative_parts((decision.name,))
    store.check_write(stage / decision.name / 'SKILL.md', text)
    os.mkdir(decision.name, mode=0o700, dir_fd=stage_fd)
    _write_stage_bytes(stage_fd, (decision.name, 'SKILL.md'), text.encode('utf-8'))
    try:
        with _StageFDStack() as descriptors:
            child_fd = _openat_directory(stage_fd, decision.name)
            descriptors.callback(os.close, child_fd)
            current = os.fstat(child_fd)
            identity = (current.st_dev, current.st_ino)
            if os.listdir(child_fd) != ['SKILL.md']:
                raise ValueError('unexpected staged skill members')
            actual = _read_staged_text(child_fd)
        if actual != text or not actual.startswith('---\n'):
            raise ValueError('staged skill content changed')
        meta, _ = split_skill_frontmatter(actual)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SkillLearningGateError(CODE_DRAFT_INVALID, {"error": type(exc).__name__}) from exc
    parsed = (str(meta.get('name') or '').strip(), str(meta.get('description') or '').strip(),
              str(meta.get('when_to_use', '')).strip(), tuple(meta.get('tags', ())))
    if not parsed[1]:
        raise SkillLearningGateError(CODE_DRAFT_INVALID, {"field": "description"})
    if parsed != (decision.name, decision.description, decision.when_to_use, decision.tags):
        raise SkillLearningGateError(CODE_DRAFT_INVALID, {"field": "frontmatter"})
    _require_guard_allowed(_scan_staged_text(actual, publish))
    return decision.name, identity


# LLM: 固定目录FD内读SKILL.md，nofollow单链接门、4MiB预算、首末元数据与叶名inode一致；可同时返回同一FD身份。
# 函数用途: 取得真实字节供guard、移动和回执核验，不用二次按路径stat猜文件身份。
def _read_staged_text(directory_fd, *, with_identity=False):
    from ..common.nofollow_fs import _openat_file
    from .asset_promotion import _StageFDStack

    with _StageFDStack() as descriptors:
        leaf = _openat_file(directory_fd, 'SKILL.md', os.O_RDONLY, 0o600)
        descriptors.callback(os.close, leaf)
        before, pieces, size = os.fstat(leaf), [], 0
        while chunk := os.read(leaf, min(65536, MAX_SKILL_BYTES + 1 - size)):
            pieces.append(chunk)
            size += len(chunk)
            if size > MAX_SKILL_BYTES:
                raise ValueError('staged skill exceeds byte budget')
        after = os.fstat(leaf)
        if any(getattr(before, key) != getattr(after, key) for key in ('st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
            raise ValueError('staged skill changed during read')
        named = os.stat('SKILL.md', dir_fd=directory_fd, follow_symlinks=False)
        identity = (before.st_dev, before.st_ino)
        if (named.st_dev, named.st_ino) != identity:
            raise ValueError('skill file identity changed during read')
        text = b''.join(pieces).decode('utf-8')
        return (text, identity) if with_identity else text


# LLM: 调用方持state锁；按已登记layout逐段nofollow打开，目录FD与同FD下叶读出身份，读后核名字仍是原目录，独立关闭资源。
# 函数用途: 在登记前冻结、或回执读回时取得正文及目录/文件物理身份，不对可变路径分离stat和读取。
def _read_skill_binding(store, name, *, record=None):
    from ..common.nofollow_fs import _openat_directory, open_directory_beneath
    from .asset_promotion import _StageFDStack

    parts = store.skill_path(name, record=record).parent.relative_to(store.skills_root).parts
    with _StageFDStack() as descriptors:
        parent_fd = open_directory_beneath(store.skills_root, parts[:-1], require_dir_fd=True)
        descriptors.callback(os.close, parent_fd)
        directory_fd = _openat_directory(parent_fd, parts[-1])
        descriptors.callback(os.close, directory_fd)
        directory = os.fstat(directory_fd)
        text, file_identity = _read_staged_text(directory_fd, with_identity=True)
        named = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        directory_identity = (directory.st_dev, directory.st_ino)
        if (named.st_dev, named.st_ino) != directory_identity:
            raise ValueError('skill directory identity changed during read')
        return text, directory_identity, file_identity


# LLM: staged Skill已证明恰好一个SKILL.md；复用原guard的限额、模式、严重级和裁决，扫描FD读取的同字节而非重开路径。
# 函数用途: 对单文件learned候选运行原安全规则，不因固定FD改造漏掉内容模式或大小门。
def _scan_staged_text(text, publish):
    from ..contracts.gates.skill_guard import (
        _SKILL_THREAT_PATTERNS,
        SkillGuardFinding,
        _build_summary,
        _determine_verdict,
        _match_pattern_in_lines,
        _skill_guard_limits,
    )

    limits = _skill_guard_limits(publish.guard_config)
    findings = []
    size = len(text.encode('utf-8'))
    if limits.max_size_kb > 0 and size > limits.max_size_kb * 1024:
        findings.append(SkillGuardFinding('oversized_skill', 'high', 'structural', '(directory)', 0,
                                        f'{size // 1024}KB', 'skill exceeds configured size limit'))
    for pattern in _SKILL_THREAT_PATTERNS:
        findings.extend(_match_pattern_in_lines(pattern, text.split('\n'), 'SKILL.md'))
    verdict = _determine_verdict(findings)
    name = publish.decision.name
    return SkillScanResult(name, 'agent_generated', 'agent_generated', verdict, tuple(findings),
                           _build_summary(name, 'agent_generated', verdict, findings))


# LLM: caution/dangerous 都拒绝；细节只输出结论与 pattern ID，不复制命中的原文行。
# 函数用途: 按 guard 结论决定是否放行。
def _require_guard_allowed(scan: SkillScanResult) -> None:
    allowed, _reason = install_decision(scan, force=False)
    if not allowed:
        raise SkillLearningGateError(CODE_GUARD_BLOCKED, {
            "verdict": scan.verdict,
            "findings": sorted({finding.pattern_id for finding in scan.findings}),
        })


# LLM: 来源 run 只保留最近 MAX_SOURCE_RUN_COUNT 个，保持首次出现顺序。
# 函数用途: 把本次请求的 run_id 追加到来源列表。
def _runs(existing: tuple[str, ...], request: dict[str, object]) -> tuple[str, ...]:
    run_id = str(request.get("run_id") or "")
    runs = existing if not run_id or run_id in existing else (*existing, run_id)
    return tuple(runs[-MAX_SOURCE_RUN_COUNT:])


# LLM: 在登记回调前冻结完整proof及安装身份；内部字段不进to_record，reason仍只供人看。
# 函数用途: 生成带本次物理身份和原证明快照的发布或更新事件。
def _event(kind: str, record: LearnedSkill, publish: PublishRequest, code: str, *, binding=(None, None)) -> SkillLearningEvent:
    return _PublishedSkillEvent(
        event=kind,
        code=code,
        skill_name=record.name,
        version=record.version,
        sha256=record.sha256,
        request_key=str(publish.request.get("request_key") or ""),
        run_id=str(publish.request.get("run_id") or ""),
        reason=publish.decision.reason,
        target_dir_identity=binding[0],
        skill_file_identity=binding[1],
        verification_proof_json=json.dumps(record.maintenance.get('verification_receipt', {}), sort_keys=True, ensure_ascii=False),
    )


__all__ = [
    "CODE_DRAFT_INVALID",
    "CODE_FAMILY_BLOCKED",
    "CODE_GUARD_BLOCKED",
    "CODE_INSTALL_FAILED",
    "CODE_LEARNING_DISABLED",
    "CODE_LIMIT_REACHED",
    "CODE_NAME_BLOCKED",
    "CODE_NAME_TAKEN",
    "CODE_NOT_LEARNED",
    "CODE_REDACTED",
    "CODE_TARGET_MISSING",
    "CODE_TARGET_USER_OWNED",
    "CODE_UNCHANGED",
    "CODE_USER_MODIFIED",
    "CODE_VERSION_MISSING",
    "PublishRequest",
    "SkillLearningGateError",
    "publish_learned_skill",
    "remove_learned_skill",
    "render_learned_skill",
    "revert_learned_skill",
]
