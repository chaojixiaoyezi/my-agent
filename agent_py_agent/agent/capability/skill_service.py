# LLM: 原全局 Skill 根与独立能力包分别装配到同一快照，不能把私有资源目录交给递归 Skill 扫描或名称去重。
# 模块用途: 用同一份正文建立快照；进程仅共享builtin/shared的冻结发现结果，Owner策略仍单独计算。
from __future__ import annotations

"""Single owner-scoped Skill discovery, policy, cache, and snapshot service."""

import hashlib
import json
import stat
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from ..contracts.gates.skill_guard import SkillGuardRequest, evaluate_skill_guard_gate
from ..user_space.owner_policy import EffectiveOwnerPolicy
from .package_snapshot import CapabilityPackageSnapshot
from .skill_snapshot import (
    SkillLoadError,
    SkillSnapshot,
    SkillSnapshotEntry,
    skill_content_sha256,
)
from .skills import (
    SkillCard,
    _as_list,
    _has_frontmatter,
    read_skill_text,
    skill_catalog_roots,
    skill_enabled,
    split_skill_frontmatter,
)

# 扫描技能目录的最大深度，防止递归过深。
_MAX_SCAN_DEPTH = 6
# 每个根目录下最多收录的技能个数。
_MAX_SKILLS_PER_ROOT_COUNT = 2000
# 技能名最大字符数。
_NAME_MAX_CHARS = 128
# 技能描述最大字符数。
_DESCRIPTION_MAX_CHARS = 1024
_PROCESS_SKILL_DIR_CACHE: dict[tuple[str, str], SkillManifestItem] = {}
_PROCESS_BUILTIN_ROOT_CACHE: dict[tuple, _SkillTreeState] = {}
_PROCESS_SKILL_CACHE_LOCK = threading.RLock()
# 进程内单技能目录元数据缓存的最大条目数上限。
_PROCESS_SKILL_CACHE_MAX_ENTRIES = 1024
# 进程内单技能目录元数据缓存的最大累计字节上限。
_PROCESS_SKILL_CACHE_MAX_BYTES = 16 * 1024 * 1024
# 进程内内置技能根树状态缓存的最大根目录条目数上限。
_PROCESS_ROOT_CACHE_MAX_ENTRIES = 128
# 进程内内置技能根树状态缓存允许记录的最大路径总数上限。
_PROCESS_ROOT_CACHE_MAX_PATHS = 65536


@dataclass(frozen=True)
class SkillRoot:
    path: Path
    source: str


# LLM: 正文/hash/磁盘开关同次冻结；公开目录可附不可变卡片与guard错误，绝不缓存Owner enabled策略。
# 类用途: 保存可共享的发现事实，原有位置构造参数保持兼容。
@dataclass(frozen=True)
class SkillManifestItem:
    root: SkillRoot
    path: Path
    text: str
    content_sha256: str
    enabled: bool = True
    parsed_entry: SkillSnapshotEntry | None = None
    load_error: SkillLoadError | None = None


# LLM: stat包含全部guard资源及目录；命中仍逐项复核，不能仅信根mtime来忽略后代改写。
# 类用途: 缓存builtin目录清单与摘要，省去重复rglob而不省略变更检测。
@dataclass(frozen=True)
class _SkillTreeState:
    stats: tuple[tuple, ...]
    digest: str


# LLM: 一个服务是 prompt、工具与子代理的唯一快照来源；能力包提供者只给原安装事实的投影，不给私有扫描根。
# 类用途: 汇集用户可用方法与能力包，固定逐轮身份并在来源变化后重建缓存。
class SkillsService:
    """Owns the one Skill catalog used by prompts, tools, and subagents."""

    # LLM: provider逐次取事实；首次构造不强制丢弃进程公开缓存，显式clear/force则重读正文与guard。
    # 函数用途: 绑定 owner 策略、工作区和可选内容包发现来源。
    def __init__(
        self,
        *,
        home_paths: object,
        workspace_root: str | Path,
        policy_provider: Callable[[], EffectiveOwnerPolicy],
        plugin_roots: Callable[[], Iterable[tuple[Path, str]]] | None = None,
        package_provider: Callable[[], Iterable[CapabilityPackageSnapshot]] | None = None,
        learning_recommended_limit_provider: Callable[[],int] | None = None,
    ) -> None:
        self.home_paths = home_paths
        self.learning_recommended_limit_provider = learning_recommended_limit_provider or (lambda:50)
        # 已启用插件自带的 Skill 目录（来源 plugin:<ID>）；每次快照都重新读取，停用后下一轮自然消失
        self.plugin_roots = plugin_roots
        self.package_provider = package_provider
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.policy_provider = policy_provider
        self._extra_roots: tuple[Path, ...] = ()
        self._cache: dict[tuple[str, str], SkillSnapshot] = {}
        self._manifest_cache: tuple | None = None
        self._force_manifest_reload = False
        self._lock = threading.RLock()

    # LLM: 新根集合须同时清除快照与stat缓存，不能跨来源复用旧manifest。
    # 函数用途: 设置宿主额外根并使发现缓存失效。
    def set_extra_roots(self, roots: Iterable[str | Path]) -> None:
        normalized = tuple(
            Path(root).expanduser().resolve(strict=False)
            for root in roots
            if str(root or "").strip()
        )
        with self._lock:
            self._extra_roots = normalized
            self._cache.clear()
            self._manifest_cache = None

    # LLM: 显式clear使下一次发现绕过公开进程结果，不影响其它Owner策略或主动扫描磁盘。
    # 函数用途: 清空当前服务缓存并安排下一次重新读取正文与guard。
    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._manifest_cache = None
            self._force_manifest_reload = True

    # LLM: 只有stat命中才复用旧快照；目录/guard资源变化即使正文hash相同也须重建，包/策略仍逐次核对。
    # 函数用途: 创建或复用当前 owner 的逐轮快照，并单独报告能力包发现错误。
    def snapshot_for(
        self,
        workspace_root: str | Path | None = None,
        *,
        force_reload: bool = False,
    ) -> SkillSnapshot:
        workspace = Path(workspace_root or self.workspace_root).expanduser().resolve(strict=False)
        policy = self.policy_provider()
        if not policy.skills_enabled:
            # 总闸关闭：空快照（来源名单是细粒度控制，总闸是 effective flag——对称 memory_policy）
            return _build_snapshot((), (), policy, workspace)
        roots = self._skill_roots(workspace, policy)
        manifest, discovery_errors, manifest_unchanged = self._manifest_for(roots, force_reload=force_reload)
        packages, package_errors = self._packages()
        discovery_errors.extend(package_errors)
        from .skill_learning_versions import learning_state_fingerprint
        fingerprint = _snapshot_fingerprint(manifest, discovery_errors, policy, workspace, packages) + learning_state_fingerprint(self.home_paths) + str(self.learning_recommended_limit_provider())
        cache_key = (policy.owner_id, str(workspace))
        if not force_reload and manifest_unchanged:
            with self._lock:
                cached = self._cache.get(cache_key)
            if cached is not None and cached.fingerprint == fingerprint:
                return cached
        snapshot = _build_snapshot(manifest, discovery_errors, policy, workspace, packages)
        from .skill_learning_versions import freeze_learned_snapshot
        snapshot = freeze_learned_snapshot(snapshot, self.home_paths,recommended_limit=self.learning_recommended_limit_provider())
        snapshot = replace(snapshot,fingerprint=fingerprint)
        with self._lock:
            self._cache[cache_key] = snapshot
        return snapshot

    # LLM: 每次核所有guard资源stat；force/clear仅逐出当前根的共享结果，保持原build_manifest单参数读口。
    # 函数用途: 保留每Owner快照缓存，按完整目录签名决定是否重建。
    def _manifest_for(self, roots: tuple[SkillRoot, ...], *, force_reload: bool):
        signature = _roots_signature(roots)
        with self._lock:
            force_reload = force_reload or self._force_manifest_reload
            cached = self._manifest_cache
            if not force_reload and cached is not None and cached[:2] == (roots, signature):
                return cached[2], list(cached[3]), True
            if force_reload:
                _invalidate_public_skill_cache(roots)
            manifest, errors = _build_manifest(roots)
            self._manifest_cache = (roots, signature, manifest, tuple(errors))
            self._force_manifest_reload = False
            return manifest, errors, False

    # LLM: provider 错误只清空内容包集合并进入 errors；普通 Skill 与旧插件 Skill 不因包账损坏而消失。
    # 函数用途: 校验并冻结包提供者返回的元数据，防止重复包 ID 和可变伪快照混进本轮。
    def _packages(self) -> tuple[tuple[CapabilityPackageSnapshot, ...], list[SkillLoadError]]:
        if self.package_provider is None:
            return (), []
        try:
            packages = tuple(self.package_provider())
            if (any(not isinstance(package, CapabilityPackageSnapshot) for package in packages)
                    or len({package.package_id for package in packages}) != len(packages)):
                raise ValueError("能力包快照类型或身份无效")
            return tuple(sorted(packages, key=lambda package: package.package_id)), []
        except (OSError, ValueError, TypeError) as exc:
            return (), [SkillLoadError("", "CAPABILITY_PACKAGE_DISCOVERY_FAILED", type(exc).__name__, "capability_package")]

    # LLM: 本地根和资产共用声明，共享/内置路径来自可信home_paths，extra不能覆盖标准根。
    # 函数用途: 返回当前工作区、默认工作区、Owner、共享与内置的去重根。
    def base_roots(self, workspace_root: Path) -> tuple[SkillRoot, ...]:
        extras = ((Path(self.home_paths.shared_skills_dir), "shared"),
                  (Path(self.home_paths.shared_builtin_dir), "builtin"),
                  *((path, "workspace") for path in self._extra_roots))
        return _dedupe_roots([SkillRoot(path, source) for path, source in skill_catalog_roots(
            self.home_paths.owner_home_dir, workspace_root, extras)])

    # LLM: 原来源过滤与优先级不变；只复用规范根声明，插件根仍最后且只在真实发现入口调用。
    # 函数用途: 给普通方法发现生成原授权根集合，不增加扫描或来源授权。
    def _skill_roots(
        self,
        workspace_root: Path,
        policy: EffectiveOwnerPolicy,
    ) -> tuple[SkillRoot, ...]:
        enabled = set(policy.enabled_skill_sources)
        roots = [root for root in self.base_roots(workspace_root) if not enabled or root.source in enabled]
        # 插件 Skill 由插件启用这一显式操作授权，不受旧的来源名单限制（已有 owner 的名单不含 plugin），
        # 但排在最后、优先级最低，不能覆盖同名的工作区/用户/内置 Skill；总闸关闭时上面已返回空快照
        if self.plugin_roots is not None:
            roots.extend(SkillRoot(Path(path), source) for path, source in self.plugin_roots())
        return _dedupe_roots(roots)


# LLM: 包括guard会扫描的隐藏/深层资源；builtin复用目录清单但逐项stat，不能仅凭根mtime沿用授权。
# 函数用途: 计算每Owner来源指纹，共享builtin目录元信息以免重复遍历。
def _roots_signature(roots: tuple[SkillRoot, ...]) -> str:
    digest = hashlib.sha256()
    for root in roots:
        digest.update(repr((str(root.path), root.source)).encode())
        digest.update(_skill_tree_state(root.path, builtin=root.source == "builtin").digest.encode())
    return digest.hexdigest()


# LLM: 文件/链接两种身份、权限及纳秒时间参与指纹；读不到时保留errno，中断不吞掉。
# 函数用途: 获取不读正文的文件状态，区分不存在、不可读和符号链接目标变化。
def _path_stat_signature(path: Path) -> tuple:
    try:
        stat = path.stat()
        link = path.lstat()
        return (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns,
                stat.st_dev, stat.st_ino, stat.st_mode,
                link.st_ino, link.st_ctime_ns, link.st_mode)
    except InterruptedError:
        raise
    except OSError as exc:
        return str(path), type(exc).__name__, exc.errno


# LLM: 命中复核全部stat；未命中逐目录稳定枚举，最多三次重试，不能发表漏项但带新目录stat的清单。
# 函数用途: 复用builtin目录清单，检测子文件改写和枚举期间增删，不无限重试。
def _skill_tree_state(path: Path, *, builtin: bool = False) -> _SkillTreeState:
    first = _path_stat_signature(path)
    key = (str(path), first[5], first[1]) if len(first) >= 7 else None
    with _PROCESS_SKILL_CACHE_LOCK:
        cached = _PROCESS_BUILTIN_ROOT_CACHE.get(key) if builtin and key is not None else None
    if cached is not None and all(_path_stat_signature(Path(row[0])) == row for row in cached.stats):
        return cached
    for _ in range(3):
        stats = _scan_skill_tree_stats(path)
        if stats is not None:
            break
    else:
        raise OSError("skill directory changed during stat scan")
    state = _SkillTreeState(stats, hashlib.sha256(repr(stats).encode()).hexdigest())
    if builtin and key is not None and len(stats) <= _PROCESS_ROOT_CACHE_MAX_PATHS:
        with _PROCESS_SKILL_CACHE_LOCK:
            _PROCESS_BUILTIN_ROOT_CACHE[key] = state
            while (len(_PROCESS_BUILTIN_ROOT_CACHE) > _PROCESS_ROOT_CACHE_MAX_ENTRIES
                   or sum(len(item.stats) for item in _PROCESS_BUILTIN_ROOT_CACHE.values()) > _PROCESS_ROOT_CACHE_MAX_PATHS):
                _PROCESS_BUILTIN_ROOT_CACHE.pop(next(iter(_PROCESS_BUILTIN_ROOT_CACHE)))
    return state


# LLM: 每个目录在列名首末核同一stat；之后再变会使缓存下一次复核失效，不跟随目录符号链接。
# 函数用途: 获取不会把新增文件永久遗漏的确定性全目录元数据，变化时返回None供有界重扫。
def _scan_skill_tree_stats(root: Path) -> tuple[tuple, ...] | None:
    pending, signatures = [root], []
    while pending:
        path = pending.pop()
        before = _path_stat_signature(path)
        signatures.append(before)
        if len(before) < 7 or not stat.S_ISDIR(before[6]) or stat.S_ISLNK(before[-1]):
            continue
        children = tuple(sorted(path.iterdir()))
        if _path_stat_signature(path) != before:
            return None
        pending.extend(reversed(children))
    return tuple(signatures)


# LLM: 清理的只是可重建公开投影，不影响其它Owner私有根或现有快照；下一次guard必须重核。
# 函数用途: 显式刷新时逐出当前公开根的进程目录缓存。
def _invalidate_public_skill_cache(roots: tuple[SkillRoot, ...]) -> None:
    public = {(str(root.path), root.source) for root in roots if root.source in {"builtin", "shared"}}
    with _PROCESS_SKILL_CACHE_LOCK:
        for key, item in tuple(_PROCESS_SKILL_DIR_CACHE.items()):
            if (str(item.root.path), item.root.source) in public:
                _PROCESS_SKILL_DIR_CACHE.pop(key)


# LLM: 只合并各根发现结果，保持单参数读口，不改变来源优先级或owner边界。
# 函数用途: 为当前根集合构建manifest与隔离的错误列表。
def _build_manifest(roots: tuple[SkillRoot, ...]) -> tuple[list[SkillManifestItem], list[SkillLoadError]]:
    items: list[SkillManifestItem] = []
    errors: list[SkillLoadError] = []
    for root in roots:
        discovered, root_errors = _discover_root(root)
        items.extend(discovered)
        errors.extend(root_errors)
    return items, errors


# LLM: builtin复用已核stat的文件清单，owner不提前递归learned；原可见性、数量、路径拒绝保留。
# 函数用途: 单根发现确定性manifest，公开结果可进程共享，私有根仍独立读取。
def _discover_root(root: SkillRoot) -> tuple[list[SkillManifestItem], list[SkillLoadError]]:
    if not root.path.is_dir():
        return [], []
    resolved_root = root.path.resolve(strict=False)
    candidates = (Path(row[0]) for row in _skill_tree_state(root.path, builtin=True).stats) if root.source == "builtin" else root.path.rglob("SKILL.md")
    files = [path for path in sorted(candidates) if path.name == "SKILL.md" and _visible_skill_path(root.path, path)
             and not (root.source == "owner" and root.path.name == "skills"
                      and path.relative_to(root.path).parts[0] == "learned")]
    errors: list[SkillLoadError] = []
    if len(files) > _MAX_SKILLS_PER_ROOT_COUNT:
        errors.append(_error(root, root.path, "SKILL_ROOT_LIMIT", f"skill count exceeds {_MAX_SKILLS_PER_ROOT_COUNT}"))
        files = files[:_MAX_SKILLS_PER_ROOT_COUNT]
    items: list[SkillManifestItem] = []
    for path in files:
        item, error = _manifest_item(root, resolved_root, path)
        if item is not None:
            items.append(item)
        if error is not None:
            errors.append(error)
    return items, errors


# LLM: 先核路径围栏；仅builtin/shared按全资源stat及来源/分类缓存不可变结果，发布前再核目录避免错键。
# 函数用途: 为公开目录共享同一manifest，私有目录每次独立冻结正文，不缓存Owner策略。
def _manifest_item(
    root: SkillRoot,
    resolved_root: Path,
    path: Path,
) -> tuple[SkillManifestItem | None, SkillLoadError | None]:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        return None, _error(root, path, "SKILL_PATH_ESCAPE", str(exc))
    if root.source not in {"builtin", "shared"}:
        return _read_manifest_item(root, resolved)
    key = _skill_directory_key(root, resolved)
    with _PROCESS_SKILL_CACHE_LOCK:
        if key in _PROCESS_SKILL_DIR_CACHE:
            return _PROCESS_SKILL_DIR_CACHE[key], None
        item, error = _read_manifest_item(root, resolved)
        if item is None:
            return None, error
        entry, error = _parse_manifest_entry(item)
        item = replace(item, parsed_entry=entry, load_error=error)
        if key != _skill_directory_key(root, resolved):
            return None, _error(root, path, "SKILL_SOURCE_CHANGED", "skill directory changed during discovery")
        # 同目录旧版本不再驻留；不同来源/分类已参与签名，不能错用guard信任级别。
        for old in tuple(_PROCESS_SKILL_DIR_CACHE):
            if old[0] == key[0]:
                _PROCESS_SKILL_DIR_CACHE.pop(old)
        _PROCESS_SKILL_DIR_CACHE[key] = item
        while (len(_PROCESS_SKILL_DIR_CACHE) > _PROCESS_SKILL_CACHE_MAX_ENTRIES
               or sum(len(value.text.encode("utf-8")) for value in _PROCESS_SKILL_DIR_CACHE.values()) > _PROCESS_SKILL_CACHE_MAX_BYTES):
            _PROCESS_SKILL_DIR_CACHE.pop(next(iter(_PROCESS_SKILL_DIR_CACHE)))
        return item, None


# LLM: guard来源、分类、实际正文路径与全部后代stat一起绑定，防同目录两个合法SKILL别名错误共用卡片。
# 函数用途: 生成完整Skill目录的缓存身份，不读取正文或执行脚本。
def _skill_directory_key(root: SkillRoot, path: Path) -> tuple[str, str]:
    state = _skill_tree_state(path.parent, builtin=root.source == "builtin")
    context = root.source, str(root.path), str(path), _derived_category(root.path, path), state.digest
    return str(path.parent), hashlib.sha256(repr(context).encode()).hexdigest()


# LLM: 正文只read_skill_text一次；hash和解析后续使用同一字符串，侧开关在此固定，不写盘。
# 函数用途: 有界读取一个Skill的正文、hash与磁盘启停状态。
def _read_manifest_item(root: SkillRoot, resolved: Path):
    try:
        text = read_skill_text(resolved)
        enabled = skill_enabled(resolved, split_skill_frontmatter(text)[0])
    except InterruptedError:
        raise
    except (OSError, ValueError) as exc:
        return None, _error(root, resolved, "SKILL_READ_FAILED", str(exc))
    return SkillManifestItem(root, resolved, text, skill_content_sha256(text), enabled), None


# LLM: 全局按名称优先级去重仅处理 Skill manifest；包集合独立按包 ID 固定，不参与该循环。
# 函数用途: 将已扫描 Skill 与包摘要装到同一个不可变快照中。
def _build_snapshot(
    manifest: list[SkillManifestItem],
    discovery_errors: list[SkillLoadError],
    policy: EffectiveOwnerPolicy,
    workspace_root: Path,
    packages: tuple[CapabilityPackageSnapshot, ...] = (),
) -> SkillSnapshot:
    entries: dict[str, SkillSnapshotEntry] = {}
    errors = list(discovery_errors)
    for item in manifest:
        entry, error = _entry_from_manifest(item, policy)
        if error is not None:
            errors.append(error)
            continue
        if entry is not None and entry.name not in entries:
            entries[entry.name] = entry
    fingerprint = _snapshot_fingerprint(manifest, discovery_errors, policy, workspace_root, packages)
    return SkillSnapshot(
        entries=tuple(sorted(entries.values(), key=lambda entry: (entry.source, entry.name))),
        errors=tuple(errors),
        fingerprint=fingerprint,
        owner_id=policy.owner_id,
        workspace_root=str(workspace_root),
        packages=packages,
    )


# LLM: 进程只共享冻结公开投影；disabled/共享allowlist每Owner重新计算，不能缓存最终enabled。
# 函数用途: 将公开或私有发现事实按当前Owner策略装入快照。
def _entry_from_manifest(
    item: SkillManifestItem,
    policy: EffectiveOwnerPolicy,
) -> tuple[SkillSnapshotEntry | None, SkillLoadError | None]:
    entry, error = item.parsed_entry, item.load_error
    if entry is None and error is None:
        entry, error = _parse_manifest_entry(item)
    if error is not None:
        return None, error
    enabled = entry.enabled and entry.name not in set(policy.disabled_skills)
    if item.root.source == "shared" and policy.enabled_shared_skills:
        enabled = enabled and entry.name in set(policy.enabled_shared_skills)
    return replace(entry, enabled=enabled), None


# LLM: parser使用manifest.text而非磁盘二读；guard仍核整个目录，缓存的是冻结条目/错误而非可变SkillCard。
# 函数用途: 把同一份正文解析、验证并生成与Owner策略无关的Skill投影。
def _parse_manifest_entry(item: SkillManifestItem):
    try:
        card = _card_from_manifest_text(item)
        _validate_card(card)
    except (OSError, TypeError, ValueError) as exc:
        return None, _error(item.root, item.path, "SKILL_PARSE_FAILED", str(exc))
    decision = evaluate_skill_guard_gate(
        item.path.parent,
        SkillGuardRequest(source=item.root.source, skill_name=card.name),
    )
    if not decision.allowed:
        return None, _error(item.root, item.path, "SKILL_GUARD_DENIED", _guard_codes(decision.to_dict()))
    return _snapshot_entry(item, card, card.category, item.enabled and card.enabled), None


# LLM: 复用原有有界frontmatter/list codec及必填规则；只从已冻结文本建卡，磁盘enabled不二读。
# 函数用途: 在不改变文件parser公共签名的情况下消除manifest卡片与hash的双读竞态。
def _card_from_manifest_text(item: SkillManifestItem) -> SkillCard:
    meta, _ = split_skill_frontmatter(item.text)
    if not _has_frontmatter(item.text):
        raise ValueError("missing YAML frontmatter delimited by ---")
    if not str(meta.get("description") or "").strip():
        raise ValueError("missing frontmatter field: description")
    category = meta.get("category")
    return SkillCard(
        name=str(meta.get("name") or item.path.parent.name).strip(),
        description=str(meta["description"]).strip(), path=item.path,
        when_to_use=str(meta.get("when_to_use", "")).strip(),
        category=(str(category).strip() if category is not None else "") or _derived_category(item.root.path, item.path),
        platforms=_as_list(meta.get("platforms")), scope=str(meta.get("scope", "workspace")).strip() or "workspace",
        tags=_as_list(meta.get("tags")), capabilities=_as_list(meta.get("capabilities")),
        tools_required=_as_list(meta.get("tools_required")),
        risk_level=str(meta.get("risk_level", "low")).strip() or "low", source=item.root.source, enabled=item.enabled,
    )


def _snapshot_entry(
    item: SkillManifestItem,
    card: SkillCard,
    category: str,
    enabled: bool,
) -> SkillSnapshotEntry:
    source = item.root.source
    return SkillSnapshotEntry(
        stable_id=f"{source}:{card.name}",
        name=card.name,
        description=card.description,
        path=str(item.path),
        source=source,
        category=category or "general",
        content_sha256=item.content_sha256,
        enabled=enabled,
        when_to_use=card.when_to_use,
        platforms=tuple(card.platforms),
        tags=tuple(card.tags),
        capabilities=tuple(card.capabilities),
        tools_required=tuple(card.tools_required),
        risk_level=card.risk_level,
    )


# LLM: 全启用无包时保原指纹；明确禁用加入指纹防侧文件改动复用旧授权，包摘要/代次仍完整，不编码读取回调。
# 函数用途: 为缓存与在途决策比较生成 owner、策略、公开方法及能力包版本摘要。
def _snapshot_fingerprint(
    manifest: list[SkillManifestItem],
    errors: list[SkillLoadError],
    policy: EffectiveOwnerPolicy,
    workspace_root: Path,
    packages: tuple[CapabilityPackageSnapshot, ...] = (),
) -> str:
    digest = hashlib.sha256()
    digest.update(str(workspace_root).encode("utf-8"))
    digest.update(
        repr(
            (
                policy.owner_id,
                policy.enabled_skill_sources,
                policy.disabled_skills,
                policy.enabled_shared_skills,
            )
        ).encode()
    )
    for item in manifest:
        digest.update(f"{item.root.source}\0{item.path}\0{item.content_sha256}".encode())
        if not item.enabled:
            digest.update(b'\0ASSET_DISABLED_BY_OWNER')
    for error in errors:
        digest.update(repr(error).encode())
    for package in packages:
        digest.update(json.dumps(package.to_ref(), sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def _validate_card(card: SkillCard) -> None:
    if not card.name:
        raise ValueError("missing name")
    if len(card.name) > _NAME_MAX_CHARS:
        raise ValueError(f"name exceeds {_NAME_MAX_CHARS} characters")
    if not card.description:
        raise ValueError("missing description")
    if len(card.description) > _DESCRIPTION_MAX_CHARS:
        raise ValueError(f"description exceeds {_DESCRIPTION_MAX_CHARS} characters")


def _visible_skill_path(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if len(relative.parts) - 1 > _MAX_SCAN_DEPTH:
        return False
    return not any(part.startswith(".") for part in relative.parts[:-1])


# LLM: 只从本根内路径推导缺省分类；独立skills/learned根保留learned，普通分类树保留目录链，平铺或越界回退general。
# 函数用途: 恢复技能根拆分前的目录分类语义，不修改元数据或来源。
def _derived_category(root: Path, path: Path) -> str:
    try:
        rel_parts = path.relative_to(root.resolve(strict=False)).parts
    except ValueError:
        return "general"
    if root.name == "learned" and root.parent.name == "skills":
        return "learned"
    return "/".join(rel_parts[:-2]) or "general"


def _dedupe_roots(roots: list[SkillRoot]) -> tuple[SkillRoot, ...]:
    result: list[SkillRoot] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        key = (root.source, str(root.path.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            result.append(SkillRoot(Path(key[1]), root.source))
    return tuple(result)


def _guard_codes(decision: dict[str, object]) -> str:
    findings = decision.get("findings") if isinstance(decision, dict) else []
    codes = [str(item.get("code") or "") for item in findings if isinstance(item, dict)]
    return ",".join(code for code in codes if code) or "guard denied"


def _error(root: SkillRoot, path: Path, code: str, message: str) -> SkillLoadError:
    return SkillLoadError(path=str(path), code=code, message=message, source=root.source)


__all__ = ["SkillRoot", "SkillsService"]
