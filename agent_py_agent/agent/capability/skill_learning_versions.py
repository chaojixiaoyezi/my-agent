# LLM: 自学版本引用沿原registry归属和snapshot授权，只固定已存在且hash相符的历史正文；不创建文件或接管手写Skill。
# 模块用途: 为自学Skill绑定不可变正文和同源撤回元信息；提供语义目录投影与有界stat缓存，在途快照跨更新保留原版。
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path

try:
    from ..common import cache_capacity
except ImportError:  # pragma: no cover - 精简测试环境降级兜底
    cache_capacity = None
from ..common.nofollow_fs import read_text_beneath
from .learned_metadata_projection import (
    learned_metadata_batch,
    learned_store,
    metadata_from_registry,
)
from .skill_learning_store import (
    LearnedSkill,
    SkillLearningRegistry,
    SkillLearningStore,
    SkillLearningStoreError,
)
from .skill_snapshot import skill_content_sha256

_BASE_READ_TEXT_BENEATH = read_text_beneath


@dataclass(frozen=True)
class _LearningStateMemo:
    registry_stat: tuple
    registry: SkillLearningRegistry
    version_facts: dict[str, tuple[tuple, str, bool]]
    active_facts: dict[str, tuple[Path, tuple, str]]
    semantic_rows: tuple[object, ...]
    hidden_names: tuple[str, ...]
    base_digest: str
    default_fingerprint: str


_LEARNING_MEMO_LOCK = threading.RLock()
_LEARNING_STATE_MEMO: OrderedDict[tuple[Path, Path], _LearningStateMemo] = OrderedDict()


def _memo_capacity() -> int:
    # 沿用公共 resident_owners 配额，未配置时使用有界默认值 64，不引入无依据全局上限常量。
    configured = cache_capacity.resident_owners(0) if cache_capacity is not None else 0
    return max(1, int(configured or 64))


def _trim_memo_locked() -> None:
    limit = _memo_capacity()
    while len(_LEARNING_STATE_MEMO) > limit:
        _LEARNING_STATE_MEMO.popitem(last=False)


def _memo_key(store: SkillLearningStore) -> tuple[Path, Path]:
    return (
        store.directory.expanduser().resolve(strict=False),
        store.skills_root.expanduser().resolve(strict=False),
    )


def _stat_signature(path: Path) -> tuple:
    try:
        if os.name == "nt":
            target = path.stat()
            link = path.lstat()
        else:
            link = path.lstat()
            target = path.stat() if stat.S_ISLNK(link.st_mode) else link
        return (
            "ok",
            int(target.st_mtime_ns),
            int(target.st_ctime_ns),
            int(target.st_size),
            int(target.st_dev),
            int(target.st_ino),
            int(target.st_mode),
            int(link.st_ino),
            int(link.st_ctime_ns),
            int(link.st_mode),
        )
    except InterruptedError:
        raise
    except OSError as exc:
        return ("err", type(exc).__name__, exc.errno)


def _is_missing_stat(sig: tuple) -> bool:
    return bool(sig and sig[0] == "err" and sig[2] == errno.ENOENT)


def _is_exempt_record(record: LearnedSkill) -> bool:
    ownership = record.maintenance.get("ownership")
    return record.layout == "owner" or ownership in {"owner_layout", "user_modified"}


def _persisted_usage_binding(record: LearnedSkill) -> dict[str, object] | None:
    maint = record.maintenance
    inc = str(maint.get("incarnation_id") or "").strip()
    act = str(maint.get("activation_id") or "").strip()
    sha = str(record.sha256 or "").strip().lower()
    if not inc or not act or not sha:
        return None
    epoch = maint.get("strike_epoch", 0)
    if type(epoch) is not int or epoch < 0:
        return None
    return {
        "incarnation_id": inc,
        "activation_id": act,
        "content_sha256": sha,
        "strike_epoch": epoch,
    }


def _active_marker_for(
    store: SkillLearningStore,
    name: str,
    record: LearnedSkill,
    prev_fact: tuple[Path, tuple, str] | None,
    prev_sha: str | None,
) -> tuple[Path, tuple, str]:
    active_path = store.skill_path(name, record=record)
    sig = _stat_signature(active_path)
    if (
        prev_fact is not None
        and prev_fact[0] == active_path
        and prev_fact[1] == sig
        and prev_sha == record.sha256
    ):
        return prev_fact
    if _is_missing_stat(sig):
        return active_path, sig, "missing"
    if sig[0] != "ok" or stat.S_ISLNK(sig[9]):
        return active_path, sig, "unavailable"
    anchor = store.skills_root if record.layout == "owner" else store.learned_root
    try:
        text = read_text_beneath(anchor, (name, "SKILL.md"))
    except (InterruptedError, SystemExit, KeyboardInterrupt):
        raise
    except (OSError, UnicodeError, ValueError):
        return active_path, sig, "unavailable"
    if text is None:
        return active_path, sig, "missing"
    marker = "match" if skill_content_sha256(text) == record.sha256 else "diff"
    return active_path, sig, marker


def _version_fact_for(
    store: SkillLearningStore,
    name: str,
    record: LearnedSkill,
    prev_fact: tuple[tuple, str, bool] | None,
) -> tuple[tuple, str, bool]:
    ver_name = f"v{record.version}.md"
    ver_path = store.versions_dir / name / ver_name
    ver_stat = _stat_signature(ver_path)
    if (
        prev_fact is not None
        and prev_fact[0] == ver_stat
        and prev_fact[1] == record.sha256
        and read_text_beneath is _BASE_READ_TEXT_BENEATH
    ):
        return prev_fact
    try:
        text = read_text_beneath(store.directory, ("versions", name, ver_name))
        valid = text is not None and skill_content_sha256(text) == record.sha256
    except (InterruptedError, SystemExit, KeyboardInterrupt):
        raise
    except (OSError, UnicodeError, ValueError):
        valid = False
    return ver_stat, record.sha256, valid


def _hidden_import_names(registry: SkillLearningRegistry) -> tuple[str, ...]:
    names = {
        str(name)
        for operation in registry.import_operations.values()
        if isinstance(operation, dict) and operation.get("status") != "committed"
        for name in operation.get("target_names", [])
    }
    return tuple(sorted(names))


def _build_memo(
    store: SkillLearningStore,
    reg_stat: tuple,
    registry: SkillLearningRegistry,
    previous: _LearningStateMemo | None,
) -> _LearningStateMemo:
    prev_versions = previous.version_facts if previous is not None else {}
    prev_actives = previous.active_facts if previous is not None else {}
    prev_skills = previous.registry.skills if previous is not None else {}

    version_facts: dict[str, tuple[tuple, str, bool]] = {}
    active_facts: dict[str, tuple[Path, tuple, str]] = {}
    semantic_rows: list[object] = []

    for name in sorted(registry.skills):
        rec = registry.skills[name]
        prev_rec = prev_skills.get(name)
        prev_sha = prev_rec.sha256 if prev_rec is not None else None
        active_fact = _active_marker_for(store, name, rec, prev_actives.get(name), prev_sha)
        active_facts[name] = active_fact

        maint = rec.maintenance
        state = str(maint.get("state", "trial"))
        exempt = _is_exempt_record(rec)
        if not exempt and state not in {"disabled", "archived"}:
            ver_fact = _version_fact_for(store, name, rec, prev_versions.get(name))
            version_facts[name] = ver_fact
            version_valid = ver_fact[2]
        else:
            version_valid = False

        binding = None if (exempt or state in {"disabled", "archived"}) else _persisted_usage_binding(rec)
        binding_tuple = (
            (binding["incarnation_id"], binding["activation_id"], binding["content_sha256"], binding["strike_epoch"])
            if binding is not None
            else None
        )
        semantic_rows.append((
            name,
            rec.layout,
            str(store.skill_path(name, record=rec)),
            rec.sha256,
            int(rec.version),
            int(rec.revision),
            state,
            bool(maint.get("pinned") is True),
            str(maint.get("ownership") or ""),
            exempt,
            bool(version_valid),
            active_fact[2],
            binding_tuple,
        ))

    hidden = _hidden_import_names(registry)
    base_payload = {
        "schema": "semantic-catalog-projection.v1",
        "dir": str(store.directory),
        "skills_root": str(store.skills_root),
        "hidden": list(hidden),
        "blocked_names": sorted(set(registry.blocked_names)),
        "skills": semantic_rows,
    }
    base_digest = hashlib.sha256(
        json.dumps(base_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    from .skill_learning_maintenance import recommended_learning

    default_rec = sorted(recommended_learning(registry, 50, explore_percent=0))
    default_fingerprint = hashlib.sha256(
        json.dumps({"base": base_digest, "recommended": default_rec}, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _LearningStateMemo(
        registry_stat=reg_stat,
        registry=registry,
        version_facts=version_facts,
        active_facts=active_facts,
        semantic_rows=tuple(semantic_rows),
        hidden_names=hidden,
        base_digest=base_digest,
        default_fingerprint=default_fingerprint,
    )


def _memo_stats_match(store: SkillLearningStore, cached: _LearningStateMemo, reg_stat: tuple) -> bool:
    if cached.registry_stat != reg_stat:
        return False
    for path, expected_stat, _marker in cached.active_facts.values():
        if _stat_signature(path) != expected_stat:
            return False
    for name, (expected_ver_stat, _sha, _valid) in cached.version_facts.items():
        rec = cached.registry.skills.get(name)
        if rec is None:
            return False
        ver_path = store.versions_dir / name / f"v{rec.version}.md"
        if _stat_signature(ver_path) != expected_ver_stat:
            return False
    return True


def invalidate_learning_state_cache(home_paths: object) -> None:
    """显式清除指定 owner 的学习状态 stat 缓存，用于 force_reload 或 clear_cache。"""
    if not getattr(home_paths, "owner_skill_learning_dir", None):
        return
    store = SkillLearningStore.for_home(home_paths)
    key = _memo_key(store)
    with _LEARNING_MEMO_LOCK:
        _LEARNING_STATE_MEMO.pop(key, None)


def _load_memo_or_state(
    home_paths: object,
    *,
    force_reload: bool = False,
) -> tuple[SkillLearningStore | None, _LearningStateMemo | None, str]:
    """返回 (store, memo, status): status 为 'ok' | 'absent' | 'unavailable'。"""
    if not getattr(home_paths, "owner_skill_learning_dir", None):
        return None, None, "absent"
    store = SkillLearningStore.for_home(home_paths)
    key = _memo_key(store)
    reg_stat = _stat_signature(store.registry_path)
    if _is_missing_stat(reg_stat):
        with _LEARNING_MEMO_LOCK:
            _LEARNING_STATE_MEMO.pop(key, None)
        return store, None, "absent"
    if reg_stat[0] != "ok" or stat.S_ISLNK(reg_stat[9]):
        with _LEARNING_MEMO_LOCK:
            _LEARNING_STATE_MEMO.pop(key, None)
        return store, None, "unavailable"

    with _LEARNING_MEMO_LOCK:
        cached = None if force_reload else _LEARNING_STATE_MEMO.get(key)
        if cached is not None:
            _LEARNING_STATE_MEMO.move_to_end(key)

    if cached is not None and _memo_stats_match(store, cached, reg_stat):
        return store, cached, "ok"

    try:
        registry = store.load_registry()
    except (SkillLearningStoreError, OSError, ValueError):
        with _LEARNING_MEMO_LOCK:
            _LEARNING_STATE_MEMO.pop(key, None)
        return store, None, "unavailable"

    memo = _build_memo(store, reg_stat, registry, None if force_reload else cached)
    with _LEARNING_MEMO_LOCK:
        _LEARNING_STATE_MEMO[key] = memo
        _LEARNING_STATE_MEMO.move_to_end(key)
        _trim_memo_locked()
    return store, memo, "ok"


# LLM: 每项必须同时匹配owner来源、canonical活动路径、registry hash、版本文件hash；失败不猜版本或创建副本。
# 函数用途: 在发现快照建立时冻结自学版本路径与宿主使用绑定，普通及owner豁免Skill继续沿原读取行为。
def freeze_learned_snapshot(snapshot, home_paths, *, recommended_limit=50, explore_percent=0):
    if not getattr(home_paths, "owner_skill_learning_dir", None):
        return snapshot
    store, memo, status = _load_memo_or_state(home_paths)
    if status == "unavailable" and store is not None:
        return replace(
            snapshot,
            entries=tuple(entry for entry in snapshot.entries if not Path(entry.path).is_relative_to(store.learned_root)),
        )
    if store is None or status == "absent" or memo is None:
        return snapshot

    registry = memo.registry
    from .skill_learning_maintenance import recommended_learning

    recommended = recommended_learning(registry, recommended_limit, explore_percent=explore_percent)
    hidden = set(memo.hidden_names)
    entries = []
    for entry in snapshot.entries:
        if entry.source == "owner" and entry.name in hidden:
            continue
        current = registry.skills.get(entry.name) if entry.source == "owner" else None
        if current is None:
            entries.append(entry)
            continue
        # 同一次冻结只读一份登记；路径是该已解析record的纯投影，不逐条重载整表。
        expected = store.skill_path(current.name, record=current)
        if _is_exempt_record(current) or Path(entry.path) != expected or entry.content_sha256 != current.sha256:
            entries.append(entry)
            continue
        state = current.maintenance.get("state", "trial")
        if state in {"disabled", "archived"}:
            entries.append(replace(entry, enabled=False, recommended=False))
            continue
        entry = replace(entry, recommended=entry.name in recommended)
        version = store.versions_dir / entry.name / f"v{current.version}.md"
        ver_fact = memo.version_facts.get(entry.name)
        if (
            ver_fact is not None
            and ver_fact[1] == current.sha256
            and read_text_beneath is _BASE_READ_TEXT_BENEATH
            and _stat_signature(version) == ver_fact[0]
        ):
            valid = ver_fact[2]
        else:
            try:
                text = read_text_beneath(store.directory, ("versions", entry.name, version.name))
                valid = text is not None and skill_content_sha256(text) == current.sha256
            except (OSError, UnicodeError):
                valid = False
        if not valid:
            entries.append(entry)
            continue
        asset_fields = dict(entry.asset_fields)
        binding = _persisted_usage_binding(current)
        if binding is not None:
            asset_fields["_learning_usage_binding"] = binding
        else:
            asset_fields.pop("_learning_usage_binding", None)
        entries.append(replace(entry, frozen_path=str(version), asset_fields=asset_fields))
    return replace(snapshot, entries=tuple(entries))


# LLM: 元信息和正文读取共用原托管授权判据；读取registry和路径事实但不读正文，不把在途快照当成永久授权。
# 函数用途: 为自身发现返回自学Skill当前生命周期和是否仍可请求原固定版本。
def frozen_learned_metadata(entry):
    store = learned_store(entry)
    try:
        registry = store.load_registry()
    except SkillLearningStoreError as exc:
        raise OSError("learned skill registry unavailable") from exc
    return metadata_from_registry(entry, store, registry)


# LLM: 仅显示批允许共享registry；原正文仍每次经上面的单项实时复核及nofollow读取。
# 函数用途: 为目录适配批量元信息读取，不授予正文读取权限。
def frozen_learned_metadata_batch(entries):
    return learned_metadata_batch(entries)


# LLM: 固定正文不固定授权；原metadata判据仍拒绝删除、停用和路径替换，不增加fallback或读取活动正文。
# 函数用途: 在同源授权复核通过后读取原固定版本；拒绝语义保持不变。
def read_frozen_learned_body(entry):
    if not frozen_learned_metadata(entry)["authorized"]:
        raise FileNotFoundError("learned skill authorization no longer available")
    frozen = Path(entry.frozen_path)
    store = SkillLearningStore(frozen.parents[2], frozen.parents[4] / "skills")
    return read_text_beneath(store.directory, ("versions", entry.name, frozen.name))


def learning_recommendation_projection(
    home_paths: object,
    *,
    recommended_limit: int = 50,
    explore_percent: int = 0,
) -> str:
    """基于已校验学习投影计算当前动态 (limit, explore_percent) 下的推荐选集摘要，不重读纯计数噪声。"""
    _store, memo, status = _load_memo_or_state(home_paths)
    if status == "unavailable":
        return "learning-state-unavailable"
    if status == "absent" or memo is None:
        return ""
    from .skill_learning_maintenance import recommended_learning

    selected = sorted(recommended_learning(memo.registry, recommended_limit, explore_percent=explore_percent))
    return hashlib.sha256(
        json.dumps(
            {"base": memo.base_digest, "limit": int(recommended_limit), "explore": int(explore_percent), "selected": selected},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


# LLM: 只投影影响发现目录、版本绑定与默认推荐选集的语义事实；纯计数器与时间戳写入不触发失效，坏文件永不缓存。
# 函数用途: 读取语义级学习状态指纹（单参数保持测试兼容），并通过有界 stat memo 避免无变化回合重解析整表。
def learning_state_fingerprint(home_paths):
    _store, memo, status = _load_memo_or_state(home_paths)
    if status == "unavailable":
        return "learning-state-unavailable"
    if status == "absent" or memo is None:
        return ""
    return memo.default_fingerprint


# LLM: 只有当前仍授权且已托管的同stable_id可按原任务冻结hash定位历史，不接受路径或改名；用户停用/删除仍拒绝。
# 函数用途: 在任务/子代理重启恢复时把准确历史引用重新绑定不可变正文，普通来源不走此路径。
def resolve_learned_revision(entry, expected_sha256):
    if not entry.frozen_path or entry.source != "owner" or not entry.enabled:
        return None
    frozen = Path(entry.frozen_path)
    store = SkillLearningStore(frozen.parents[2], frozen.parents[4] / "skills")
    registry = store.load_registry()
    record = registry.skills.get(entry.name)
    if record is None or record.maintenance.get("state") in {"disabled", "archived"} or record.maintenance.get("ownership") == "user_modified":
        return None
    for version in sorted((store.versions_dir / entry.name).glob("v*.md")):
        text = read_text_beneath(store.directory, ("versions", entry.name, version.name))
        if text is not None and skill_content_sha256(text) == expected_sha256:
            cleaned_fields = {k: v for k, v in entry.asset_fields.items() if k != "_learning_usage_binding"}
            return replace(entry, content_sha256=expected_sha256, frozen_path=str(version), asset_fields=cleaned_fields)
    return None
