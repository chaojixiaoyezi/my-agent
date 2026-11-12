# LLM: 原全局 Skill 根与独立能力包分别装配到同一快照，不能把私有资源目录交给递归 Skill 扫描或名称去重。
# 模块用途: 用同一份正文建立快照；进程仅共享builtin/shared的冻结发现结果，Owner策略仍单独计算。
from __future__ import annotations

"""Single owner-scoped Skill discovery, policy, cache, and snapshot service."""

import errno
import hashlib
import json
import os
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError, Future
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from weakref import WeakKeyDictionary, WeakValueDictionary

from ..common.cache_freshness import CACHE_TRUST_AGE_SECONDS, cache_entry_trustworthy
from ..contracts.gates.skill_guard import SkillGuardRequest, evaluate_skill_guard_gate
from ..settings.defaults import owner_agent_slot_count
from ..user_space.owner_policy import EffectiveOwnerPolicy
from .package_snapshot import CapabilityPackageSnapshot
from .skill_snapshot import (
    SkillLoadError,
    SkillSnapshot,
    SkillSnapshotEntry,
    freeze_snapshot_entry,
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

try:
    from ..common import cache_capacity
except ImportError:
    # R0 尚未合入时沿用已有并发容量计算；合入后只消费它的统一推导口。
    cache_capacity = None

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
# 旧的独立签名读口没有服务宿主，状态只随调用方仍持有的 SkillRoot 存活。
_ROOT_SIGNATURE_STATES: WeakKeyDictionary[SkillRoot, dict] = WeakKeyDictionary()
_PROCESS_SKILL_CACHE_LOCK = threading.RLock()
# 服务内发现入口：服务调用 _build_manifest 时设置，按根合流发现并复用未变私有目录；直接调用时为空，行为不变。
_SERVICE_ROOT_DISCOVERY: ContextVar[Callable[[SkillRoot], tuple[list, list]] | None] = ContextVar(
    "skill_service_root_discovery", default=None)
# 进程内单技能目录元数据缓存的最大条目数上限。
_PROCESS_SKILL_CACHE_MAX_ENTRIES = 1024
# 进程内单技能目录元数据缓存的最大累计字节上限。
_PROCESS_SKILL_CACHE_MAX_BYTES = 16 * 1024 * 1024
# 进程内内置技能根树状态缓存的最大根目录条目数上限。
_PROCESS_ROOT_CACHE_MAX_ENTRIES = 128
# 进程内内置技能根树状态缓存允许记录的最大路径总数上限。
_PROCESS_ROOT_CACHE_MAX_PATHS = 65536
# 未登记容量时沿用现有配置默认值，不再新增一份数值配置。
_SERVICE_CACHE_MAX_ENTRIES = owner_agent_slot_count(None)
# 旧宿主可替换 guard 读口；这类显式钩子继续保留原来的即时复核语义。
_STATIC_SKILL_GUARD = evaluate_skill_guard_gate


# LLM: 根路径与来源共同参与发现优先级和缓存身份；该值对象不自行解析路径或授予读取权限。
# 类用途: 用不可变记录传递一个 Skill 扫描根及其来源标记。
@dataclass(frozen=True)
class SkillRoot:
    path: Path
    source: str


# LLM: 正文/hash/磁盘开关同次冻结；公开目录可附不可变卡片与guard错误，绝不缓存Owner enabled策略。
#   frozen_entry/freeze_error 是同一版本按磁盘开关冻结资产绑定的结果；为空时快照构造照原规则冻结。
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
    frozen_entry: SkillSnapshotEntry | None = None
    freeze_error: SkillLoadError | None = None


# 完整目录清单、摘要及校验时刻同次冻结；短窗口外仍按原目录稳定性规则复核。
@dataclass(frozen=True)
class _SkillTreeState:
    stats: tuple[tuple, ...]
    digest: str
    settled: bool = False
    validated_at: float = 0.0


# LLM: part 是该根在根集合签名里的完整分量(路径/来源 + 全部后代 stat 摘要或不可变代次)，一致才可复用。
#   keyed 只给私有根：Skill 目录 → (目录身份, 已解析并冻结的条目)，根变化时只重读身份变了的目录。发布后不再修改。
# 类用途: 保存单个 Skill 根的发现条目、错误与可复用的目录身份。
@dataclass(frozen=True)
class _RootManifest:
    part: bytes
    items: tuple[SkillManifestItem, ...]
    errors: tuple[SkillLoadError, ...]
    keyed: dict[str, tuple[tuple[str, str], SkillManifestItem]]


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
        cache_entries: int | None = None,
    ) -> None:
        self.home_paths = home_paths
        self.learning_recommended_limit_provider = learning_recommended_limit_provider or (lambda:50)
        # 已启用插件自带的 Skill 目录（来源 plugin:<ID>）；每次快照都重新读取，停用后下一轮自然消失
        self.plugin_roots = plugin_roots
        self.package_provider = package_provider
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.policy_provider = policy_provider
        self._extra_roots: tuple[Path, ...] = ()
        # 缺失的工作区根不参与身份，相同技能树共用一份内容，工作区仅在出口绑定。
        self._cache: OrderedDict[tuple[str, str], tuple[str, str, tuple[SkillRoot, ...], SkillSnapshot]] = OrderedDict()
        self._cache_floor = cache_entries if type(cache_entries) is int and cache_entries > 0 else _SERVICE_CACHE_MAX_ENTRIES
        # _manifest_cache 是最近一次使用的 (roots, signature, manifest, errors)。
        self._manifest_cache: tuple | None = None
        # 内容缓存和清单缓存共同持有根引用，最后一个引用释放时回收该根的发现结果。
        self._roots: OrderedDict[SkillRoot, _RootManifest] = OrderedDict()
        self._manifests: OrderedDict[tuple[SkillRoot, ...], tuple] = OrderedDict()
        self._root_refs: dict[SkillRoot, int] = {}
        self._contents: dict[str, SkillSnapshot] = {}
        self._content_refs: dict[str, int] = {}
        self._root_states: OrderedDict[str, tuple[tuple, _SkillTreeState]] = OrderedDict()
        # 兼容已有宿主观察口，只有 _root_states 持有真实状态。
        self._trees = self._root_states
        # 弱引用保留在途工作区投影的对象身份，历史工作区不再占内容缓存槽。
        self._workspace_snapshots: WeakValueDictionary[tuple[str, str], SkillSnapshot] = WeakValueDictionary()
        # 同键发现/快照构建合流；换代(clear/force/改根)后开始的构建才登记结果。
        self._builds: dict[tuple, Future] = {}
        self._generation = 0
        self._force_manifest_reload = False
        self._learning_state: str | None = None
        self._lock = threading.RLock()

    @property
    def _capacity(self) -> int:
        return cache_capacity.owner_agents(self._cache_floor) if cache_capacity is not None else self._cache_floor

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
            self._reset_locked()

    # LLM: 显式clear使下一次发现绕过公开进程结果，不影响其它Owner策略或主动扫描磁盘。
    # 函数用途: 清空当前服务缓存并安排下一次重新读取正文与guard。
    def clear_cache(self) -> None:
        with self._lock:
            self._reset_locked()
            self._force_manifest_reload = True

    # LLM: 调用方持服务锁；换代后在途构建只把结果交给已在等的调用，不再登记进缓存。
    # 函数用途: 清空全部发现、快照与目录状态缓存并换代。
    def _reset_locked(self) -> None:
        self._cache.clear()
        self._manifest_cache = None
        self._roots.clear()
        self._manifests.clear()
        self._root_refs.clear()
        self._contents.clear()
        self._content_refs.clear()
        with _PROCESS_SKILL_CACHE_LOCK:
            self._root_states.clear()
        self._workspace_snapshots.clear()
        self._generation += 1

    # 根签名按校验窗口复用；已检测到的目录或资源变化仍须重建，包和策略每次核对。
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
        from .skill_learning_versions import freeze_learned_snapshot, learning_state_fingerprint
        learning_state = learning_state_fingerprint(self.home_paths)
        with self._lock:
            # 托管发布有明确版本事实，不能让新的 registry 搭配窗口内的旧正文。
            if self._learning_state is not None and self._learning_state != learning_state:
                self._reset_locked()
                self._force_manifest_reload = True
            self._learning_state = learning_state
        roots = self._skill_roots(workspace, policy)
        manifest, discovery_errors, signature, content, generation = self._manifest_for(roots, force_reload=force_reload)
        packages, package_errors = self._packages()
        discovery_errors.extend(package_errors)
        limit = self.learning_recommended_limit_provider()
        learning = learning_state + str(limit)
        fingerprint = _snapshot_fingerprint(manifest, discovery_errors, policy, workspace, packages) + learning
        shared_fingerprint = _snapshot_fingerprint(manifest, discovery_errors, policy, Path(), packages) + learning
        content_key = content + shared_fingerprint
        cache_key = (policy.owner_id, signature)
        with self._lock:
            cached = None if force_reload else self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
        if cached is not None and cached[1] == content_key:
            return self._bind_workspace(cached[3], workspace, fingerprint, content_key)

        def build() -> SkillSnapshot:
            with self._lock:
                ready = None if force_reload else self._contents.get(content_key)
            if ready is not None:  # 上一位领头者刚登记完：直接共用
                return ready
            built = _build_snapshot(manifest, discovery_errors, policy, Path(), packages)
            built = freeze_learned_snapshot(built, self.home_paths, recommended_limit=limit)
            built = replace(built, fingerprint=shared_fingerprint)
            with self._lock:
                if self._generation == generation:
                    self._contents[content_key] = built
            return built

        with self._lock:
            shared = None if force_reload else self._contents.get(content_key)
        if shared is None:
            shared = build() if force_reload else self._shared(("content", content_key, generation), build)
        snapshot = self._bind_workspace(shared, workspace, fingerprint, content_key)
        with self._lock:
            if self._generation == generation:
                value = (signature, content_key, roots, shared)
                self._contents.setdefault(content_key, shared)
                self._hold_locked(value, 1)
                previous = self._cache.pop(cache_key, None)
                self._cache[cache_key] = value
                if previous is not None:
                    self._hold_locked(previous, -1)
                self._trim_caches_locked()
        return snapshot

    def _bind_workspace(self, snapshot: SkillSnapshot, workspace: Path, fingerprint: str, content_key: str) -> SkillSnapshot:
        with self._lock:
            key = (content_key, str(workspace))
            bound = self._workspace_snapshots.get(key)
            if bound is None:
                bound = replace(snapshot, workspace_root=str(workspace), fingerprint=fingerprint)
                self._workspace_snapshots[key] = bound
            return bound

    # 调用方持服务锁；内容缓存撤销自己的根引用，不会提前释放清单缓存仍持有的根。
    # 函数用途: 登记或撤销一个工作区缓存条目对根结果与内容快照的引用。
    def _hold_locked(self, value: tuple, delta: int) -> None:
        _signature, content, roots, _snapshot = value
        count = self._content_refs.get(content, 0) + delta
        if count > 0:
            self._content_refs[content] = count
        else:
            self._content_refs.pop(content, None)
            self._contents.pop(content, None)
        self._hold_roots_locked(roots, delta)

    def _hold_roots_locked(self, roots: tuple[SkillRoot, ...], delta: int) -> None:
        for root in roots:
            count = self._root_refs.get(root, 0) + delta
            if count > 0:
                self._root_refs[root] = count
                continue
            self._root_refs.pop(root, None)
            released = self._roots.pop(root, None)
            with _PROCESS_SKILL_CACHE_LOCK:
                self._root_states.pop(str(root.path), None)
                for directory in (released.keyed if released is not None else ()):
                    self._root_states.pop(directory, None)

    def _trim_caches_locked(self) -> None:
        capacity = self._capacity
        while len(self._cache) > capacity:
            self._hold_locked(self._cache.popitem(last=False)[1], -1)
        while len(self._manifests) > capacity:
            released, _ = self._manifests.popitem(last=False)
            self._hold_roots_locked(released, -1)
        with _PROCESS_SKILL_CACHE_LOCK:
            while len(self._root_states) > capacity:
                self._root_states.popitem(last=False)

    # LLM: 同键并发只构建一次，其余调用等同一结果或原异常；构建在服务锁外，锁只护登记表。领头者被中断时各自重建。
    # 函数用途: 合并同一根发现或同一内容快照的并发构建。
    def _shared(self, key: tuple, build: Callable[[], object]):
        with self._lock:
            future = self._builds.get(key)
            leader = future is None
            if leader:
                future = self._builds[key] = Future()
        if not leader:
            try:
                return future.result()
            except CancelledError:
                return build()
        try:
            result = build()
        except Exception as exc:
            future.set_exception(exc)
            raise
        except BaseException:
            future.cancel()
            raise
        finally:
            with self._lock:
                if self._builds.get(key) is future:
                    self._builds.pop(key)
        future.set_result(result)
        return result

    # 标准根按完整校验时刻限频；显式挂载、guard 钩子和宿主代次保留旧合同，force/clear 必须重扫。
    # 根发现仍合流；私有状态随服务释放，进程仅保留 builtin 树，磁盘 I/O 不占服务锁。
    # 函数用途: 返回当前根集合的manifest、错误、完整签名、只含有内容根的内容签名及所用缓存代次。
    def _manifest_for(self, roots: tuple[SkillRoot, ...], *, force_reload: bool):
        generations = getattr(self.home_paths, 'immutable_skill_root_generations', {})
        generations = generations if isinstance(generations, dict) else {}
        with self._lock:
            force = force_reload or self._force_manifest_reload
            if force:
                self._force_manifest_reload = False
                self._reset_locked()
            generation = self._generation
        if force:
            _invalidate_public_skill_cache(roots)
        immediate_roots = set(self._extra_roots)
        custom_guard = evaluate_skill_guard_gate is not _STATIC_SKILL_GUARD
        # 未接能力包版本读口的旧插件宿主，维持逐轮现场发现的原合同。
        legacy_plugin_host = self.plugin_roots is not None and self.package_provider is None
        parts = {root: _root_signature_part(root, generations, self._root_states,
                 validate=custom_guard or legacy_plugin_host or root.path in immediate_roots) for root in roots}
        signature = hashlib.sha256(b"".join(parts.values())).hexdigest()
        with self._lock:
            self._trim_caches_locked()
            cached = self._manifests.get(roots)
            if cached is not None and cached[0] == signature:
                self._manifests.move_to_end(roots)
                self._manifest_cache = (roots, signature, list(cached[1]), cached[2])
                return list(cached[1]), list(cached[2]), signature, cached[3], generation
            found = {root: self._roots.get(root) for root in roots}
        missing = tuple(root for root in roots if found[root] is None or found[root].part != parts[root])
        if missing:
            # 仍经 _build_manifest 单参数读口发现(一次未命中一次构建)；服务上下文把每个根交给按根合流的发现。
            def discover(root: SkillRoot) -> tuple[list, list]:
                found[root] = self._root_manifest(root, parts[root], generation)
                return list(found[root].items), list(found[root].errors)

            token = _SERVICE_ROOT_DISCOVERY.set(discover)
            try:
                _build_manifest(missing)
            finally:
                _SERVICE_ROOT_DISCOVERY.reset(token)
        results = [found[root] if found[root] is not None and found[root].part == parts[root]
                   else self._root_manifest(root, parts[root], generation) for root in roots]
        manifest = [item for result in results for item in result.items]
        errors = [error for result in results for error in result.errors]
        content = hashlib.sha256(b"".join(result.part for result in results if result.items or result.errors)).hexdigest()
        with self._lock:
            if self._generation == generation:
                self._manifest_cache = (roots, signature, manifest, tuple(errors))
                previous = self._manifests.pop(roots, None)
                self._manifests[roots] = (signature, tuple(manifest), tuple(errors), content)
                if previous is None:
                    self._hold_roots_locked(roots, 1)
                self._trim_caches_locked()
        return manifest, errors, signature, content, generation

    # LLM: 分量一致直接复用；否则同根同分量合流成一次发现，私有根把上一版目录身份交给发现以复用未变目录。
    #   当代的结果才登记；被淘汰目录的私有状态随之丢弃。
    # 函数用途: 取得单个根在当前签名分量下的发现结果。
    def _root_manifest(self, root: SkillRoot, part: bytes, generation: int) -> _RootManifest:
        with self._lock:
            cached = self._roots.get(root)
        if cached is not None and cached.part == part:
            return cached
        previous = cached.keyed if cached is not None else {}

        def discover() -> _RootManifest:
            with self._lock:
                ready = self._roots.get(root)
            if ready is not None and ready.part == part:  # 上一位领头者刚登记完：直接共用
                return ready
            keyed: dict[str, tuple[tuple[str, str], SkillManifestItem]] = {}
            items, errors = _discover_root(root, previous=previous, keyed=keyed, tree_cache=self._root_states)
            result = _RootManifest(part, tuple(items), tuple(errors), keyed)
            with self._lock:
                if self._generation == generation:
                    self._roots[root] = result
                    self._roots.move_to_end(root)
                    with _PROCESS_SKILL_CACHE_LOCK:
                        for directory in previous.keys() - keyed.keys():
                            self._root_states.pop(directory, None)
            return result

        return self._shared(("root", root, part, generation), discover)

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


# 独立签名读口保留立即复核语义；代次参数仅兼容已有显式宿主，不作为正常热路径的开关。
def _roots_signature(roots: tuple[SkillRoot, ...], *, immutable_generations=None, tree_cache=None) -> str:
    generations = immutable_generations if isinstance(immutable_generations, dict) else {}
    parts = []
    for root in roots:
        cache = tree_cache
        if cache is None and root.source != "builtin":
            with _PROCESS_SKILL_CACHE_LOCK:
                cache = _ROOT_SIGNATURE_STATES.setdefault(root, {})
        parts.append(_root_signature_part(root, generations, cache, validate=True))
    return hashlib.sha256(b"".join(parts)).hexdigest()


# 来源和真实存在的路径参与身份；缺失根为空分量，不能让工作区名称挤占内容缓存。
# 函数用途: 计算单个根在根集合签名里的分量，供整组签名与按根复用共用。
def _root_signature_part(root: SkillRoot, generations: dict, tree_cache: dict | None, *, validate: bool = False) -> bytes:
    head = repr((str(root.path), root.source)).encode()
    generation = generations.get(str(root.path)) if root.source in {'builtin', 'shared'} else None
    if isinstance(generation, str) and generation:
        # 根替换/删除仍实时失效；内容不可变性是宿主安装发布合同，不适用于workspace/owner/plugin。
        return head + repr(('immutable-release', generation, _path_stat_signature(root.path))).encode()
    private = tree_cache if root.source != "builtin" else None
    state = _skill_tree_state(root.path, builtin=root.source == "builtin", cache=private, validate=validate)
    return head + state.digest.encode() if state.digest else b""


# LLM: 文件/链接两种身份、权限及纳秒时间参与指纹；读不到时保留errno，中断不吞掉。
#   POSIX先lstat：非链接时stat与lstat描述同一inode，直接复用省一次系统调用；链接才再跟随取目标。
#   Windows的目录联接lstat不报链接，仍保持先stat后lstat的原顺序。
# 函数用途: 获取不读正文的文件状态，区分不存在、不可读和符号链接目标变化。
def _path_stat_signature(path: Path) -> tuple:
    try:
        if os.name == "nt":
            target = path.stat()
            link = path.lstat()
        else:
            link = path.lstat()
            target = path.stat() if stat.S_ISLNK(link.st_mode) else link
        return (str(path), target.st_mtime_ns, target.st_size, target.st_ctime_ns,
                target.st_dev, target.st_ino, target.st_mode,
                link.st_ino, link.st_ctime_ns, link.st_mode)
    except InterruptedError:
        raise
    except OSError as exc:
        return str(path), type(exc).__name__, exc.errno


# 完整根校验限频，根本身的增删和身份变化仍立即失效；发现正文时 validate=True 保留原前后复核。
# 非 builtin 只写服务自己的有界表，直接调用不把私有树登记进进程缓存。
def _skill_tree_state(
    path: Path, *, builtin: bool = False, cache: dict | None = None, validate: bool = False,
) -> _SkillTreeState:
    first = _path_stat_signature(path)
    now = time.monotonic()
    key = first if len(first) >= 7 else None
    if key is None:
        digest = "" if first[2] == errno.ENOENT else hashlib.sha256(repr((first,)).encode()).hexdigest()
        return _SkillTreeState((first,), digest, validated_at=now)
    with _PROCESS_SKILL_CACHE_LOCK:
        if cache is not None:
            slot = cache.get(str(path)) if key is not None else None
            cached = slot[1] if slot is not None and slot[0] == key else None
            if cached is not None and isinstance(cache, OrderedDict):
                cache.move_to_end(str(path))
        else:
            cached = _PROCESS_BUILTIN_ROOT_CACHE.get(key) if builtin and key is not None else None
    if cached is not None and not validate and 0 <= now - cached.validated_at < CACHE_TRUST_AGE_SECONDS:
        return cached
    if (cached is not None and (builtin or cached.settled)
            and all(_path_stat_signature(Path(row[0])) == row for row in cached.stats[1:])):
        state = replace(cached, validated_at=now)
    else:
        scanned = None
        for attempt in range(5):
            scanned = _scan_skill_tree_stats(path)
            if scanned is not None:
                break
            time.sleep(0.005 * (attempt + 1))
        else:
            import logging
            logging.getLogger(__name__).debug("skill directory changed during stat scan; using best-effort snapshot: %s", path)
            scanned = _scan_skill_tree_stats(path, best_effort=True)
            if scanned is None:
                scanned = ((first,), False)
        stats, settled = scanned
        digest = "" if len(stats[0]) < 7 and stats[0][2] == errno.ENOENT else hashlib.sha256(repr(stats).encode()).hexdigest()
        state = _SkillTreeState(stats, digest, settled, now)
        key = stats[0] if stats and len(stats[0]) >= 7 else None
    with _PROCESS_SKILL_CACHE_LOCK:
        if cache is not None and key is not None:
            cache[str(path)] = (key, state)
            capacity = cache_capacity.owner_agents(_SERVICE_CACHE_MAX_ENTRIES) if cache_capacity is not None else _SERVICE_CACHE_MAX_ENTRIES
            while len(cache) > capacity:
                cache.pop(next(iter(cache)))
        elif builtin and key is not None and len(state.stats) <= _PROCESS_ROOT_CACHE_MAX_PATHS:
            for old in tuple(_PROCESS_BUILTIN_ROOT_CACHE):
                if old[0] == str(path) and old != key:
                    _PROCESS_BUILTIN_ROOT_CACHE.pop(old)
            _PROCESS_BUILTIN_ROOT_CACHE[key] = state
            while (len(_PROCESS_BUILTIN_ROOT_CACHE) > _PROCESS_ROOT_CACHE_MAX_ENTRIES
                   or sum(len(item.stats) for item in _PROCESS_BUILTIN_ROOT_CACHE.values()) > _PROCESS_ROOT_CACHE_MAX_PATHS):
                _PROCESS_BUILTIN_ROOT_CACHE.pop(next(iter(_PROCESS_BUILTIN_ROOT_CACHE)))
    return state


# LLM: 每个目录在列名首末核同一stat；之后再变会使缓存下一次复核失效，不跟随目录符号链接。
#   第二项在列名之前逐目录冻结：全部目录mtime已离开2秒窗口才为True，不能在使用时再判。
# 函数用途: 获取不会把新增文件永久遗漏的确定性全目录元数据，变化时返回None供有界重扫。
def _scan_skill_tree_stats(root: Path, *, best_effort: bool = False) -> tuple[tuple[tuple, ...], bool] | None:
    pending, signatures, settled = [root], [], True
    while pending:
        path = pending.pop()
        before = _path_stat_signature(path)
        signatures.append(before)
        if len(before) < 7 or not stat.S_ISDIR(before[6]) or stat.S_ISLNK(before[-1]):
            continue
        settled = settled and cache_entry_trustworthy(before[1])
        try:
            children = tuple(sorted(
                p for p in path.iterdir()
                if not (p.name.endswith(".tmp") or p.name.startswith(".tmp"))
            ))
        except FileNotFoundError:
            continue
        except OSError:
            if best_effort:
                continue
            return None
        if not best_effort and _path_stat_signature(path) != before:
            return None
        pending.extend(reversed(children))
    return tuple(signatures), settled


# LLM: 清理的只是可重建公开投影，不影响其它Owner私有根或现有快照；下一次guard必须重核。
# 函数用途: 显式刷新时逐出当前公开根的进程目录缓存。
def _invalidate_public_skill_cache(roots: tuple[SkillRoot, ...]) -> None:
    public = {(str(root.path), root.source) for root in roots if root.source in {"builtin", "shared"}}
    with _PROCESS_SKILL_CACHE_LOCK:
        builtin = tuple(root.path for root in roots if root.source == "builtin")
        for key in tuple(_PROCESS_BUILTIN_ROOT_CACHE):
            if any(Path(key[0]).is_relative_to(root) for root in builtin):
                _PROCESS_BUILTIN_ROOT_CACHE.pop(key)
        for key, item in tuple(_PROCESS_SKILL_DIR_CACHE.items()):
            if (str(item.root.path), item.root.source) in public:
                _PROCESS_SKILL_DIR_CACHE.pop(key)


# LLM: 只合并各根发现结果，保持单参数读口，不改变来源优先级或owner边界；服务内调用时逐根交给服务的合流发现。
# 函数用途: 为当前根集合构建manifest与隔离的错误列表。
def _build_manifest(roots: tuple[SkillRoot, ...]) -> tuple[list[SkillManifestItem], list[SkillLoadError]]:
    items: list[SkillManifestItem] = []
    errors: list[SkillLoadError] = []
    discover = _SERVICE_ROOT_DISCOVERY.get() or _discover_root
    for root in roots:
        discovered, root_errors = discover(root)
        items.extend(discovered)
        errors.extend(root_errors)
    return items, errors


# LLM: builtin复用已核stat的文件清单，owner不提前递归learned；原可见性、数量、路径拒绝保留。
#   keyed 给出时(服务内发现)私有目录按目录身份复用 previous 中未变的已备条目，并把本次的身份/条目登记进 keyed。
# 函数用途: 单根发现确定性manifest，公开结果可进程共享，私有根仍独立读取。
def _discover_root(
    root: SkillRoot, *, previous: dict | None = None, keyed: dict | None = None, tree_cache: dict | None = None,
) -> tuple[list[SkillManifestItem], list[SkillLoadError]]:
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
        item, error = _manifest_item(root, resolved_root, path, previous=previous, keyed=keyed, tree_cache=tree_cache)
        if item is not None:
            items.append(item)
        if error is not None:
            errors.append(error)
    return items, errors


# LLM: 先核路径围栏；仅builtin/shared按全资源stat及来源/分类缓存不可变结果，发布前再核目录避免错键。
#   读正文/guard/冻结都在进程锁外，锁只护表；并发未命中时先登记者胜出，各Owner拿到同一对象。
#   私有目录：服务内发现(keyed给出)按目录身份复用上一版已备条目，只有身份变化的目录重读；目录身份只在
#   列名时已settled才登记供下次复用。直接调用(keyed为空)保持每次独立冻结正文。均不缓存Owner策略。
# 函数用途: 为公开目录共享同一manifest，私有目录按目录身份在服务内复用，不缓存Owner策略。
def _manifest_item(
    root: SkillRoot,
    resolved_root: Path,
    path: Path,
    *, previous: dict | None = None, keyed: dict | None = None, tree_cache: dict | None = None,
) -> tuple[SkillManifestItem | None, SkillLoadError | None]:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        return None, _error(root, path, "SKILL_PATH_ESCAPE", str(exc))
    if root.source not in {"builtin", "shared"}:
        if keyed is None:
            return _read_manifest_item(root, resolved)
        key, settled = _skill_directory_identity(root, resolved, tree_cache=tree_cache)
        reused = (previous or {}).get(key[0])
        if reused is not None and reused[0] == key:
            keyed[key[0]] = reused
            return reused[1], None
        item, error = _read_manifest_item(root, resolved)
        if item is None:
            return None, error
        item = _prepared_item(item)
        if settled and _skill_directory_identity(root, resolved, tree_cache=tree_cache)[0] == key:
            keyed[key[0]] = (key, item)
        return item, None
    key = _skill_directory_key(root, resolved)
    with _PROCESS_SKILL_CACHE_LOCK:
        cached = _PROCESS_SKILL_DIR_CACHE.get(key)
    if cached is not None:
        return cached, None
    item, error = _read_manifest_item(root, resolved)
    if item is None:
        return None, error
    item = _prepared_item(item)
    if key != _skill_directory_key(root, resolved):
        return None, _error(root, path, "SKILL_SOURCE_CHANGED", "skill directory changed during discovery")
    with _PROCESS_SKILL_CACHE_LOCK:
        cached = _PROCESS_SKILL_DIR_CACHE.get(key)
        if cached is not None:
            return cached, None
        # 同目录旧版本不再驻留；不同来源/分类已参与签名，不能错用guard信任级别。
        for old in tuple(_PROCESS_SKILL_DIR_CACHE):
            if old[0] == key[0]:
                _PROCESS_SKILL_DIR_CACHE.pop(old)
        _PROCESS_SKILL_DIR_CACHE[key] = item
        while (len(_PROCESS_SKILL_DIR_CACHE) > _PROCESS_SKILL_CACHE_MAX_ENTRIES
               or sum(len(value.text.encode("utf-8")) for value in _PROCESS_SKILL_DIR_CACHE.values()) > _PROCESS_SKILL_CACHE_MAX_BYTES):
            _PROCESS_SKILL_DIR_CACHE.pop(next(iter(_PROCESS_SKILL_DIR_CACHE)))
        return item, None


# LLM: 解析、guard与资产绑定冻结都只用同一份已冻结正文，对同一目录版本只做一次；结果与Owner策略无关。
# 函数用途: 为发现得到的条目附上解析结果、guard错误与按磁盘开关冻结的快照条目。
def _prepared_item(item: SkillManifestItem) -> SkillManifestItem:
    entry, error = _parse_manifest_entry(item)
    if entry is None:
        return replace(item, parsed_entry=None, load_error=error)
    frozen, freeze_error = freeze_snapshot_entry(entry)
    return replace(item, parsed_entry=entry, load_error=error, frozen_entry=frozen, freeze_error=freeze_error)


# LLM: guard来源、分类、实际正文路径与全部后代stat一起绑定，防同目录两个合法SKILL别名错误共用卡片。
# 函数用途: 生成完整Skill目录的缓存身份，不读取正文或执行脚本。
def _skill_directory_key(root: SkillRoot, path: Path) -> tuple[str, str]:
    return _skill_directory_identity(root, path)[0]


# LLM: 与 _skill_directory_key 同一身份；私有目录状态进调用方自有表，并报告列名时目录是否已settled。
# 函数用途: 返回Skill目录缓存身份及其目录状态是否可信。
def _skill_directory_identity(root: SkillRoot, path: Path, *, tree_cache: dict | None = None) -> tuple[tuple[str, str], bool]:
    private = tree_cache if root.source != "builtin" else None
    state = _skill_tree_state(path.parent, builtin=root.source == "builtin", cache=private, validate=True)
    context = root.source, str(root.path), str(path), _derived_category(root.path, path), state.digest
    return (str(path.parent), hashlib.sha256(repr(context).encode()).hexdigest()), state.settled or root.source == "builtin"


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
    freeze_errors: dict[str, SkillLoadError] = {}
    errors = list(discovery_errors)
    for item in manifest:
        entry, error, freeze_error = _assembled_entry(item, policy)
        if error is not None:
            errors.append(error)
            continue
        if entry is not None and entry.name not in entries:
            entries[entry.name] = entry
            if freeze_error is not None:
                freeze_errors[entry.name] = freeze_error
    ordered = tuple(sorted(entries.values(), key=lambda entry: (entry.source, entry.name)))
    # 已在发现时冻结失败的条目沿原顺序(排序后的条目序)把错误接在发现/解析错误之后，与快照构造时冻结一致。
    errors.extend(freeze_errors[entry.name] for entry in ordered if entry.name in freeze_errors)
    fingerprint = _snapshot_fingerprint(manifest, discovery_errors, policy, workspace_root, packages)
    return SkillSnapshot(
        entries=ordered,
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


# LLM: 与 _entry_from_manifest 同一启停判定；Owner仍启用且发现时已冻结时直接共用冻结条目(含冻结失败的禁用条目与错误)，
#   被Owner策略禁用的条目不带绑定、不报冻结错误，与快照构造时只冻结启用条目一致。
# 函数用途: 为快照组装取得策略后的条目、跳过原因与发现时的冻结错误。
def _assembled_entry(
    item: SkillManifestItem, policy: EffectiveOwnerPolicy,
) -> tuple[SkillSnapshotEntry | None, SkillLoadError | None, SkillLoadError | None]:
    entry, error = _entry_from_manifest(item, policy)
    if error is not None or entry is None:
        return None, error, None
    if entry.enabled and item.frozen_entry is not None:
        return item.frozen_entry, None, item.freeze_error
    return entry, None, None


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


# LLM: 正文 hash 来自同次 manifest，enabled 沿用上游决策；这里只投影卡片，不重新读盘或计算 Owner 授权。
# 函数用途: 把技能卡片及发现事实转换为字段冻结的 Skill 快照条目。
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


# LLM: 这里只校验名称和说明的必填与字符上限，失败抛出 ValueError；内容安全仍由独立 guard 判定。
# 函数用途: 拒绝缺少名称、说明或超过元数据长度限制的技能卡片。
def _validate_card(card: SkillCard) -> None:
    if not card.name:
        raise ValueError("missing name")
    if len(card.name) > _NAME_MAX_CHARS:
        raise ValueError(f"name exceeds {_NAME_MAX_CHARS} characters")
    if not card.description:
        raise ValueError("missing description")
    if len(card.description) > _DESCRIPTION_MAX_CHARS:
        raise ValueError(f"description exceeds {_DESCRIPTION_MAX_CHARS} characters")


# LLM: 仅筛选根内路径的目录深度和隐藏祖先；符号链接越界还须由 manifest 解析阶段拒绝。
# 函数用途: 判断候选技能文件是否处在允许扫描的可见目录层级内。
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


# LLM: 去重键同时包含来源与解析后的绝对路径，同路径不同来源不会合并，首次出现的顺序保持不变。
# 函数用途: 规范化技能根路径并按来源和路径稳定去重。
def _dedupe_roots(roots: list[SkillRoot]) -> tuple[SkillRoot, ...]:
    result: list[SkillRoot] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        key = (root.source, str(root.path.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            result.append(SkillRoot(Path(key[1]), root.source))
    return tuple(result)


# LLM: 只提取字典 finding 中的非空 code；没有可用代码时给出通用拒绝说明，不重新解释 guard 决策。
# 函数用途: 将安全门的结构化发现汇成逗号分隔的错误说明。
def _guard_codes(decision: dict[str, object]) -> str:
    findings = decision.get("findings") if isinstance(decision, dict) else []
    codes = [str(item.get("code") or "") for item in findings if isinstance(item, dict)]
    return ",".join(code for code in codes if code) or "guard denied"


# LLM: 错误保留原候选路径、来源和调用方给出的代码及消息，不在封装时重试发现或吞掉原因。
# 函数用途: 为技能发现或加载失败构造带来源身份的错误记录。
def _error(root: SkillRoot, path: Path, code: str, message: str) -> SkillLoadError:
    return SkillLoadError(path=str(path), code=code, message=message, source=root.source)


__all__ = ["SkillRoot", "SkillsService"]
