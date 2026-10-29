# LLM: 自学习 S3 用户明确确认后的发布闸门；调用方须先验证候选id/revision与真实人类权限。顺序固定：重名/删过的名字/数量上限
#   （新建）或登记在册且磁盘 hash 未被用户改动（更新）→ 字段脱敏 → 暂存目录渲染 SKILL.md 并按 require_frontmatter
#   解析、核对 frontmatter 无损往返 → agent_generated guard（从不 force，caution/dangerous 都拒）→ 原子安装/替换 →
#   版本全文 → 登记表。暂存写读扫描固定FD；移动后清理/登记失败仅按本次inode回滚，保磁盘与登记一致。全程持state_lock。
#   不调用模型；revert/remove 也在这里，供 CLI 使用。同步检查 skill_learning.py、cli/skill_learning_commands.py 与测试。
# 模块用途: 把一次已校验的总结决定安全地发布为 owner 的 skills/learned/<name>/SKILL.md，并提供回滚和删除。
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..common.log_redaction import redact_sensitive_text
from ..common.nofollow_fs import read_text_beneath
from ..contracts.gates.skill_guard import SkillScanResult, install_decision
from .skill_learning_prompt import DECISION_CREATE, SkillLearningDecision
from .skill_learning_store import (
    EVENT_PUBLISHED,
    EVENT_REMOVED,
    EVENT_REVERTED,
    EVENT_SKIPPED,
    EVENT_UPDATED,
    MAX_SOURCE_RUN_COUNT,
    LearnedSkill,
    SkillLearningEvent,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
    replace_skill_file,
    utc_now,
)
from .skill_snapshot import skill_content_sha256
from .skills import MAX_SKILL_BYTES, split_skill_frontmatter

CODE_NAME_TAKEN = "SKILL_LEARNING_NAME_TAKEN"
CODE_NAME_BLOCKED = "SKILL_LEARNING_NAME_BLOCKED"
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
_STAGING_PREFIX = ".staging-"


# LLM: code 是唯一机器判断字段；detail 只放字段名、guard pattern ID 等结构化事实，不复制 Skill 正文。
# 类用途: 表示一次发布、回滚或删除被闸门拒绝。
class SkillLearningGateError(Exception):
    # LLM: 构造时复制 detail，避免调用方之后修改原字典。
    # 函数用途: 保存结果码与结构化细节。
    def __init__(self, code: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = dict(detail or {})


# LLM: taken_names 是当前快照里所有来源的 Skill 名（含停用的），新建时不能与之重名，避免遮蔽内置/用户 Skill；
#   max_skills<=0 表示不限；request 只取 request_key/run_id 写账本和来源。
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


# LLM: 入口只接受 create/update；skip 不应走到这里。整个发布在 state_lock 内完成，返回写账本用的事件。
# 函数用途: 按决定新建或更新一个自学 Skill，被闸门拒绝时抛 SkillLearningGateError。
def publish_learned_skill(store: SkillLearningStore, publish: PublishRequest) -> SkillLearningEvent:
    store.directory.mkdir(parents=True, exist_ok=True)
    with store.state_lock():
        registry = store.load_registry()
        if publish.decision.decision == DECISION_CREATE:
            return _create_locked(store, registry, publish)
        return _update_locked(store, registry, publish)


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


# LLM: 名字闸门在前（删过的名字、快照重名、目录已存在、数量上限），通过后才渲染、检查、安装；
#   元数据先准备再移动；移动/收尾由安装器按原inode回滚，登记前任意异常也补偿本次对象，取消首因不改类。
# 函数用途: 在锁内新建自学Skill，普通错误、取消或stage收尾失败都不绕过准确安装回滚。
def _create_locked(store: SkillLearningStore, registry: SkillLearningRegistry,
                   publish: PublishRequest) -> SkillLearningEvent:
    name = publish.decision.name
    _require_new_name(store, registry, publish)
    reusable = _family_record_locked(store, registry, publish) if publish.promotion_authority == 'autonomous_verified' else None
    if reusable is not None:
        return _event(EVENT_SKIPPED, reusable, publish, 'SKILL_LEARNING_FAMILY_REUSED')
    publish, redacted = _redacted_publish(publish)
    text = render_learned_skill(publish.decision)
    target = store.learned_root / name
    now = utc_now()
    record = LearnedSkill(name, 1, skill_content_sha256(text), now, now, _runs((), publish.request), last_operation_id=str(publish.request.get("operation_id") or ""))
    record = replace(record, maintenance={'state':'trial','quality':'trial',
        'promotion_authority':publish.promotion_authority,'verification_receipt':publish.verification_receipt,
        'promotion_history':['observed','candidate','validated','trial']})
    installed_identity = _stage_check_and_move(store, publish, text, target)
    try:
        store.save_version(name, 1, text)
        store.save_registry(_registered_publication(store, registry, record, publish))
    except BaseException as exc:
        try:
            _rollback_created_skill(store, target, installed_identity)
        except BaseException as rollback_error:
            exc.add_note('installed_skill_rollback_failed:' + type(rollback_error).__name__)
            raise exc from rollback_error
        if not isinstance(exc, (OSError, SkillLearningStoreError)):
            raise
        raise SkillLearningGateError(CODE_INSTALL_FAILED, {"error": type(exc).__name__}) from exc
    return _event(EVENT_PUBLISHED, record, publish, CODE_REDACTED if redacted else "")


# LLM: 更新须CAS/磁盘hash一致，保稳定快照/历史身份；替换后任意登记前异常恢复旧文，取消/首因保持原类。
#   副作用：原子替换SKILL.md且保同目录其它文件，写versions/registry，失败补偿正文。
# 函数用途: 在锁内更新自学Skill，不让非OS持久化错误留下与registry不一致的新正文。
def _update_locked(store: SkillLearningStore, registry: SkillLearningRegistry,
                   publish: PublishRequest) -> SkillLearningEvent:
    current = _owned_record(store, registry, publish.decision.name)
    if publish.promotion_authority == 'autonomous_verified' and current.layout != 'learned':
        raise SkillLearningGateError(CODE_NOT_LEARNED)
    if current.maintenance.get("state") in {"disabled","archived"}:
        raise SkillLearningGateError("SKILL_LEARNING_TARGET_DISABLED")
    if publish.expected_revision != current.revision or publish.expected_sha256 != current.sha256:
        raise SkillLearningGateError("SKILL_LEARNING_REVISION_CONFLICT")
    publish, redacted = _redacted_publish(publish)
    text = render_learned_skill(publish.decision)
    sha = skill_content_sha256(text)
    if sha == current.sha256:
        return _event(EVENT_SKIPPED, current, publish, CODE_UNCHANGED)
    path = store.skill_path(current.name)
    previous = read_text_beneath(store.skills_root, path.relative_to(store.skills_root).parts)
    _stage_and_check(store, publish, text)
    stable = current.maintenance.get('previous_stable') if current.maintenance.get('state') == 'trial' else None
    record = replace(current, version=current.revision + 1, revision=current.revision + 1, sha256=sha, updated_at=utc_now(),
                     source_run_ids=_runs(current.source_run_ids, publish.request), last_operation_id=str(publish.request.get("operation_id") or ""),
                     maintenance={**current.maintenance, 'state':'trial', 'quality':'trial', 'helped_count':0, 'hurt_count':0,
                                   'promotion_authority':publish.promotion_authority,'verification_receipt':publish.verification_receipt,
                                   'promotion_history':['observed','candidate','validated','trial'],
                                   'historical_adoptions':_historical_adoptions(current),
                                   'previous_verification':current.maintenance.get('previous_verification', {}) if stable else {key:current.maintenance.get(key) for key in ('promotion_authority','verification_receipt')},
                                  'observations':[], 'previous_stable':stable or {'version':current.version,'revision':current.revision,'sha256':current.sha256}})
    try:
        store.check_write(path,text)
        replace_skill_file(path, text)
        store.save_version(current.name, record.version, text)
        store.save_registry(_registered_publication(store, registry, record, publish))
    except BaseException as exc:
        try:
            replace_skill_file(path, previous)
        except BaseException as rollback_error:
            exc.add_note('updated_skill_rollback_failed:' + type(rollback_error).__name__)
            raise exc from rollback_error
        if not isinstance(exc, (OSError, SkillLearningStoreError)):
            raise
        raise SkillLearningGateError(CODE_INSTALL_FAILED, {"error": type(exc).__name__}) from exc
    return _event(EVENT_UPDATED, record, publish, CODE_REDACTED if redacted else "")


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

    from .asset_promotion import register_asset_locked

    registry, _ = register_asset_locked(store, registry, kind='skill', asset_id='owner:'+record.name,
        asset=asdict(publish.decision), sha256=record.sha256, state='trial',
        source={'origin':'S3','run_id':str(publish.request.get('run_id', ''))})
    return replace(registry, skills={**registry.skills, record.name:record})


# LLM: 回滚只作用于登记在册且未被用户改动的Skill；v1回滚等同删除。激活版本回退但revision单调递增，历史文件不可重用。
# 函数用途: 把一个自学 Skill 退回上一个版本，返回账本事件。
def revert_learned_skill(store: SkillLearningStore, name: str) -> SkillLearningEvent:
    with store.state_lock():
        registry = store.load_registry()
        current = _owned_record(store, registry, name, code=CODE_USER_MODIFIED)
        if current.version <= 1:
            return _remove_locked(store, registry, current)
        text = store.read_version(name, current.version - 1)
        if text is None:
            raise SkillLearningGateError(CODE_VERSION_MISSING, {"version": current.version - 1})
        record = replace(current, version=current.version - 1, revision=current.revision + 1, sha256=skill_content_sha256(text), updated_at=utc_now())
        store.save_version(name, record.revision, text)
        replace_skill_file(store.skill_path(name), text)
        store.save_registry(replace(registry, skills={**registry.skills, name: record}))
    return SkillLearningEvent(EVENT_REVERTED, "", name, record.version, record.sha256)


# LLM: 删除把目录移到 removed/ 归档（不物理删除），从登记表去掉并把名字加入 blocked_names；
#   用户改过的 Skill 也允许删除（用户显式动作），但不在登记表里的名字拒绝。
# 函数用途: 删除一个自学 Skill，返回账本事件。
def remove_learned_skill(store: SkillLearningStore, name: str) -> SkillLearningEvent:
    with store.state_lock():
        registry = store.load_registry()
        current = registry.skills.get(name)
        if current is None:
            raise SkillLearningGateError(CODE_NOT_LEARNED, {"skill_name": name})
        return _remove_locked(store, registry, current)


# LLM: 调用方已持 state_lock；先移目录再写登记表，目录不存在（用户手动删了）也照样完成登记。
# 函数用途: 在锁内完成删除并返回事件。
def _remove_locked(store: SkillLearningStore, registry: SkillLearningRegistry,
                   current: LearnedSkill) -> SkillLearningEvent:
    store.archive_removed(current.name)
    skills = {key: value for key, value in registry.skills.items() if key != current.name}
    store.save_registry(replace(registry, skills=skills, blocked_names=(*registry.blocked_names, current.name)))
    return SkillLearningEvent(EVENT_REMOVED, "", current.name, current.version, current.sha256)


# LLM: 删过的名字最先判（用户意愿优先），其次快照重名与目录已存在，最后数量上限（只限新建）。
# 函数用途: 校验新建名字可用。
def _require_new_name(store: SkillLearningStore, registry: SkillLearningRegistry, publish: PublishRequest) -> None:
    name = publish.decision.name
    if name in registry.blocked_names:
        raise SkillLearningGateError(CODE_NAME_BLOCKED, {"skill_name": name})
    taken = name in publish.taken_names or name in registry.skills
    if taken or (store.learned_root / name).exists() or (store.learned_root / name).is_symlink():
        raise SkillLearningGateError(CODE_NAME_TAKEN, {"skill_name": name})
    if publish.max_skills > 0 and len(registry.skills) >= publish.max_skills:
        raise SkillLearningGateError(CODE_LIMIT_REACHED, {"limit": publish.max_skills})


# LLM: 所有权 = 登记在册 + 磁盘 SKILL.md 存在 + 其 hash 等于登记值；任一不满足都视为用户所有，自动流程不碰。
# 函数用途: 取出一个仍归自动流程所有的登记记录。
def _owned_record(store: SkillLearningStore, registry: SkillLearningRegistry, name: str,
                  code: str = CODE_TARGET_USER_OWNED) -> LearnedSkill:
    current = registry.skills.get(name)
    if current is None:
        raise SkillLearningGateError(CODE_TARGET_MISSING, {"skill_name": name})
    path = store.skill_path(name)
    if path.is_symlink() or not path.is_file():
        raise SkillLearningGateError(code, {"skill_name": name, "state": "missing"})
    if skill_content_sha256(read_text_beneath(store.skills_root, path.relative_to(store.skills_root).parts)) != current.sha256:
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


# LLM: 固定FD先移到data下随机隔离名核inode，stage成功收尾后才进正式名；独立关闭保首因，失败按准确移动对象回滚。
# 函数用途: 不把未扫描的源替代物直接暴露为正式Skill，返回本次安装身份供登记失败补偿。
def _stage_check_and_move(store: SkillLearningStore, publish: PublishRequest, text: str, target: Path) -> tuple[int, int]:
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
                if (observed.st_dev, observed.st_ino) != identity or _read_staged_text(staged_fd) != text:
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
            if (isolated.st_dev, isolated.st_ino) != identity or _read_staged_text(staged_fd) != text:
                raise SkillLearningGateError(CODE_DRAFT_INVALID, {'field': 'prepared_identity'})
            try:
                os.stat(target.name, dir_fd=target_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise SkillLearningGateError(CODE_NAME_TAKEN, {'skill_name': publish.decision.name})
            os.rename(prepared.name, target.name, src_dir_fd=prepared_fd, dst_dir_fd=target_fd)
            placement = target
            installed = os.stat(target.name, dir_fd=target_fd, follow_symlinks=False)
            if (installed.st_dev, installed.st_ino) != identity:
                raise SkillLearningGateError(CODE_INSTALL_FAILED, {'field': 'installed_identity'})
        return moved_identity
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


# LLM: 只回滚data隔离区或learned下本次移动的inode；替代物不删，失败记录原位置/身份，不扩大到其它owner目录。
# 函数用途: stage收尾或登记失败后撤销本次新移动，让正式名称可重试，未知对象保留隔离并记欠账。
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


# LLM: 原state/配额锁内只经本次固定stage FD写读扫描，退出精确清理；不创建第二份持久状态或复制整库。
# 函数用途: 在隔离对象内检查更新正文，不重新解析可变stage路径。
def _stage_and_check(store: SkillLearningStore, publish: PublishRequest, text: str) -> None:
    from .asset_promotion import _stage_dir
    with _stage_dir(store.skills_root.parent, with_fd=True) as (stage, stage_fd):
        _staged_skill(store, stage, publish, text, stage_fd=stage_fd)


# LLM: 只经固定stage FD创建/读取单文件Skill；共用原frontmatter codec及guard规则，以agent_generated扫描，返回名字与inode。
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
    if parsed != (decision.name, decision.description, decision.when_to_use, decision.tags):
        raise SkillLearningGateError(CODE_DRAFT_INVALID, {"field": "frontmatter"})
    _require_guard_allowed(_scan_staged_text(actual, publish))
    return decision.name, identity


# LLM: 只读固定Skill目录FD内SKILL.md，原nofollow单链接门、4MiB预算和首末元数据一致，UTF-8损坏显式拒绝。
# 函数用途: 取得真正暂存字节用于解析、guard与移动前重核，不跟随stage名字变化。
def _read_staged_text(directory_fd):
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
        return b''.join(pieces).decode('utf-8')


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


# LLM: 事件只带结构化字段与有界 reason；reason 来自模型，只供人看。
# 函数用途: 生成发布或更新事件。
def _event(kind: str, record: LearnedSkill, publish: PublishRequest, code: str) -> SkillLearningEvent:
    return SkillLearningEvent(
        event=kind,
        code=code,
        skill_name=record.name,
        version=record.version,
        sha256=record.sha256,
        request_key=str(publish.request.get("request_key") or ""),
        run_id=str(publish.request.get("run_id") or ""),
        reason=publish.decision.reason,
    )


__all__ = [
    "CODE_DRAFT_INVALID",
    "CODE_GUARD_BLOCKED",
    "CODE_INSTALL_FAILED",
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
