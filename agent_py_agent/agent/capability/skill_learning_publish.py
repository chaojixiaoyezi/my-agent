# LLM: 自学习 S3 的自动闸门与发布（代替人工确认，依据用户 2026-09-26 决定）。顺序固定：重名/删过的名字/数量上限
#   （新建）或登记在册且磁盘 hash 未被用户改动（更新）→ 字段脱敏 → 暂存目录渲染 SKILL.md 并按 require_frontmatter
#   解析、核对 frontmatter 无损往返 → agent_generated guard（从不 force，caution/dangerous 都拒）→ 原子安装/替换 →
#   版本全文 → 登记表。登记表写失败会回滚本次安装，保证“磁盘上的自学 Skill”与登记表一致。全部在 store.state_lock 内完成。
#   不调用模型；revert/remove 也在这里，供 CLI 使用。同步检查 skill_learning.py、cli/skill_learning_commands.py 与测试。
# 模块用途: 把一次已校验的总结决定安全地发布为 owner 的 skills/learned/<name>/SKILL.md，并提供回滚和删除。
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..common.log_redaction import redact_sensitive_text
from ..contracts.gates.skill_guard import SkillScanResult, install_decision, scan_skill
from .skill_learning_prompt import DECISION_CREATE, SkillLearningDecision
from .skill_learning_store import (
    EVENT_PUBLISHED,
    EVENT_REMOVED,
    EVENT_REVERTED,
    EVENT_SKIPPED,
    EVENT_UPDATED,
    MAX_SOURCE_RUNS,
    LearnedSkill,
    SkillLearningEvent,
    SkillLearningRegistry,
    SkillLearningStore,
    replace_skill_file,
    utc_now,
)
from .skill_snapshot import skill_content_sha256
from .skills import parse_skill_file

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
    lines = ["---", f"name: {decision.name}", f"description: {decision.description}"]
    if decision.when_to_use:
        lines.append(f"when_to_use: {decision.when_to_use}")
    if decision.tags:
        lines.append(f"tags: [{', '.join(decision.tags)}]")
    return "\n".join([*lines, "---", "", decision.body.rstrip(), ""])


# LLM: 名字闸门在前（删过的名字、快照重名、目录已存在、数量上限），通过后才渲染、检查、安装；
#   登记表写失败时删除刚装好的目录。副作用：写 skills/learned/<name>/、versions/、registry.json。
# 函数用途: 在锁内新建一个自学 Skill。
def _create_locked(store: SkillLearningStore, registry: SkillLearningRegistry,
                   publish: PublishRequest) -> SkillLearningEvent:
    name = publish.decision.name
    _require_new_name(store, registry, publish)
    publish, redacted = _redacted_publish(publish)
    text = render_learned_skill(publish.decision)
    target = store.learned_root / name
    _stage_check_and_move(store, publish, text, target)
    now = utc_now()
    record = LearnedSkill(name, 1, skill_content_sha256(text), now, now, _runs((), publish.request))
    try:
        store.save_version(name, 1, text)
        store.save_registry(replace(registry, skills={**registry.skills, name: record}))
    except OSError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise SkillLearningGateError(CODE_INSTALL_FAILED, {"error": type(exc).__name__}) from exc
    return _event(EVENT_PUBLISHED, record, publish, CODE_REDACTED if redacted else "")


# LLM: 只更新登记在册、磁盘 hash 等于登记值的自学 Skill；内容没变记 skipped/UNCHANGED；登记表写失败时写回旧版本。
#   副作用：原子替换 SKILL.md（保留同目录其它文件），写 versions/ 与 registry.json。
# 函数用途: 在锁内更新一个自学 Skill。
def _update_locked(store: SkillLearningStore, registry: SkillLearningRegistry,
                   publish: PublishRequest) -> SkillLearningEvent:
    current = _owned_record(store, registry, publish.decision.name)
    publish, redacted = _redacted_publish(publish)
    text = render_learned_skill(publish.decision)
    sha = skill_content_sha256(text)
    if sha == current.sha256:
        return _event(EVENT_SKIPPED, current, publish, CODE_UNCHANGED)
    path = store.skill_path(current.name)
    previous = path.read_text(encoding="utf-8")
    _stage_and_check(store, publish, text)
    record = replace(current, version=current.version + 1, sha256=sha, updated_at=utc_now(),
                     source_run_ids=_runs(current.source_run_ids, publish.request))
    try:
        replace_skill_file(path, text)
        store.save_version(current.name, record.version, text)
        store.save_registry(replace(registry, skills={**registry.skills, current.name: record}))
    except OSError as exc:
        replace_skill_file(path, previous)
        raise SkillLearningGateError(CODE_INSTALL_FAILED, {"error": type(exc).__name__}) from exc
    return _event(EVENT_UPDATED, record, publish, CODE_REDACTED if redacted else "")


# LLM: 回滚只作用于登记在册且未被用户改动的 Skill；v1 回滚等同删除。回到上一版本时版本号减一，hash 取旧版本全文。
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
        record = replace(current, version=current.version - 1, sha256=skill_content_sha256(text), updated_at=utc_now())
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
    if skill_content_sha256(path.read_text(encoding="utf-8")) != current.sha256:
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


# LLM: 新建用：暂存检查通过后把整个暂存 Skill 目录 os.replace 到目标；替换前再查一次目标不存在。
# 函数用途: 暂存、检查并安装一个新 Skill 目录。
def _stage_check_and_move(store: SkillLearningStore, publish: PublishRequest, text: str, target: Path) -> None:
    stage = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=store.directory))
    try:
        staged = _staged_skill(stage, publish, text)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise SkillLearningGateError(CODE_NAME_TAKEN, {"skill_name": publish.decision.name})
        os.replace(staged, target)
    except OSError as exc:
        raise SkillLearningGateError(CODE_INSTALL_FAILED, {"error": type(exc).__name__}) from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# LLM: 更新用：只做暂存检查，不移动；真正替换由调用方对原 SKILL.md 原子写完成。
# 函数用途: 在临时目录检查一份将要写入的 SKILL.md。
def _stage_and_check(store: SkillLearningStore, publish: PublishRequest, text: str) -> None:
    stage = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=store.directory))
    try:
        _staged_skill(stage, publish, text)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# LLM: 解析必须带 frontmatter 且 name/description/when_to_use/tags 与决定逐字一致；guard 以 agent_generated
#   来源扫描、从不 force，文件数与大小上限沿 guard_config。返回暂存的 Skill 目录。
# 函数用途: 在暂存目录写入并通过解析与安全扫描两道检查。
def _staged_skill(stage: Path, publish: PublishRequest, text: str) -> Path:
    decision = publish.decision
    skill_dir = stage / decision.name
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(text, encoding="utf-8")
    try:
        card = parse_skill_file(skill_md, source="owner", require_frontmatter=True)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SkillLearningGateError(CODE_DRAFT_INVALID, {"error": type(exc).__name__}) from exc
    parsed = (card.name, card.description, card.when_to_use, tuple(card.tags))
    if parsed != (decision.name, decision.description, decision.when_to_use, decision.tags):
        raise SkillLearningGateError(CODE_DRAFT_INVALID, {"field": "frontmatter"})
    scan = scan_skill(skill_dir, source="agent_generated", skill_name=decision.name, config=publish.guard_config)
    _require_guard_allowed(scan)
    return skill_dir


# LLM: caution/dangerous 都拒绝；细节只输出结论与 pattern ID，不复制命中的原文行。
# 函数用途: 按 guard 结论决定是否放行。
def _require_guard_allowed(scan: SkillScanResult) -> None:
    allowed, _reason = install_decision(scan, force=False)
    if not allowed:
        raise SkillLearningGateError(CODE_GUARD_BLOCKED, {
            "verdict": scan.verdict,
            "findings": sorted({finding.pattern_id for finding in scan.findings}),
        })


# LLM: 来源 run 只保留最近 MAX_SOURCE_RUNS 个，保持首次出现顺序。
# 函数用途: 把本次请求的 run_id 追加到来源列表。
def _runs(existing: tuple[str, ...], request: dict[str, object]) -> tuple[str, ...]:
    run_id = str(request.get("run_id") or "")
    runs = existing if not run_id or run_id in existing else (*existing, run_id)
    return tuple(runs[-MAX_SOURCE_RUNS:])


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
