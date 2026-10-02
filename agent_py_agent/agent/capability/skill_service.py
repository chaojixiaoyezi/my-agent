# LLM: 原全局 Skill 根与独立能力包分别装配到同一快照，不能把私有资源目录交给递归 Skill 扫描或名称去重。
# 模块用途: 按 owner、工作区和当前有效安装构造可缓存的发现快照，包读取失败不吞掉无关普通 Skill。
from __future__ import annotations

"""Single owner-scoped Skill discovery, policy, cache, and snapshot service."""

import hashlib
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
from .skills import SkillCard, parse_skill_file

# 扫描技能目录的最大深度，防止递归过深。
_MAX_SCAN_DEPTH = 6
# 每个根目录下最多收录的技能个数。
_MAX_SKILLS_PER_ROOT_COUNT = 2000
# 技能名最大字符数。
_NAME_MAX_CHARS = 128
# 技能描述最大字符数。
_DESCRIPTION_MAX_CHARS = 1024


@dataclass(frozen=True)
class SkillRoot:
    path: Path
    source: str


@dataclass(frozen=True)
class SkillManifestItem:
    root: SkillRoot
    path: Path
    text: str
    content_sha256: str


# LLM: 一个服务是 prompt、工具与子代理的唯一快照来源；能力包提供者只给原安装事实的投影，不给私有扫描根。
# 类用途: 汇集用户可用方法与能力包，固定逐轮身份并在来源变化后重建缓存。
class SkillsService:
    """Owns the one Skill catalog used by prompts, tools, and subagents."""

    # LLM: 两种 provider 每次快照重新取事实；插件 v3 roots 保持原优先级，内容包独立装配且不执行。
    # 函数用途: 绑定 owner 策略、工作区和可选内容包发现来源。
    def __init__(
        self,
        *,
        home_paths: object,
        workspace_root: str | Path,
        policy_provider: Callable[[], EffectiveOwnerPolicy],
        plugin_roots: Callable[[], Iterable[tuple[Path, str]]] | None = None,
        package_provider: Callable[[], Iterable[CapabilityPackageSnapshot]] | None = None,
    ) -> None:
        self.home_paths = home_paths
        # 已启用插件自带的 Skill 目录（来源 plugin:<ID>）；每次快照都重新读取，停用后下一轮自然消失
        self.plugin_roots = plugin_roots
        self.package_provider = package_provider
        self.workspace_root = Path(workspace_root).expanduser().resolve(strict=False)
        self.policy_provider = policy_provider
        self._extra_roots: tuple[Path, ...] = ()
        self._cache: dict[tuple[str, str], SkillSnapshot] = {}
        self._lock = threading.RLock()

    def set_extra_roots(self, roots: Iterable[str | Path]) -> None:
        normalized = tuple(
            Path(root).expanduser().resolve(strict=False)
            for root in roots
            if str(root or "").strip()
        )
        with self._lock:
            self._extra_roots = normalized
            self._cache.clear()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # LLM: 包摘要和激活身份共同进入指纹；Skill 总闸关闭时两种发现都为空，不读取内容包正文。
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
        manifest, discovery_errors = _build_manifest(roots)
        packages, package_errors = self._packages()
        discovery_errors.extend(package_errors)
        fingerprint = _snapshot_fingerprint(manifest, discovery_errors, policy, workspace, packages)
        cache_key = (policy.owner_id, str(workspace))
        if not force_reload:
            with self._lock:
                cached = self._cache.get(cache_key)
            if cached is not None and cached.fingerprint == fingerprint:
                return cached
        snapshot = _build_snapshot(manifest, discovery_errors, policy, workspace, packages)
        with self._lock:
            self._cache[cache_key] = snapshot
        return snapshot

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

    def _skill_roots(
        self,
        workspace_root: Path,
        policy: EffectiveOwnerPolicy,
    ) -> tuple[SkillRoot, ...]:
        enabled = set(policy.enabled_skill_sources)
        roots: list[SkillRoot] = []
        if not enabled or "workspace" in enabled:
            roots.extend(SkillRoot(path, "workspace") for path in self._extra_roots)
            roots.append(SkillRoot(workspace_root / ".agents" / "skills", "workspace"))
        if not enabled or "owner" in enabled:
            roots.append(SkillRoot(Path(self.home_paths.owner_home_dir) / "skills", "owner"))
        if not enabled or "shared" in enabled:
            roots.append(SkillRoot(Path(self.home_paths.shared_skills_dir), "shared"))
        if not enabled or "builtin" in enabled:
            roots.append(SkillRoot(Path(self.home_paths.shared_builtin_dir), "builtin"))
        # 插件 Skill 由插件启用这一显式操作授权，不受旧的来源名单限制（已有 owner 的名单不含 plugin），
        # 但排在最后、优先级最低，不能覆盖同名的工作区/用户/内置 Skill；总闸关闭时上面已返回空快照
        if self.plugin_roots is not None:
            roots.extend(SkillRoot(Path(path), source) for path, source in self.plugin_roots())
        return _dedupe_roots(roots)


def _build_manifest(roots: tuple[SkillRoot, ...]) -> tuple[list[SkillManifestItem], list[SkillLoadError]]:
    items: list[SkillManifestItem] = []
    errors: list[SkillLoadError] = []
    for root in roots:
        discovered, root_errors = _discover_root(root)
        items.extend(discovered)
        errors.extend(root_errors)
    return items, errors


def _discover_root(root: SkillRoot) -> tuple[list[SkillManifestItem], list[SkillLoadError]]:
    if not root.path.is_dir():
        return [], []
    resolved_root = root.path.resolve(strict=False)
    files = [path for path in sorted(root.path.rglob("SKILL.md")) if _visible_skill_path(root.path, path)]
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
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, _error(root, path, "SKILL_READ_FAILED", str(exc))
    return SkillManifestItem(root, resolved, text, skill_content_sha256(text)), None


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


def _entry_from_manifest(
    item: SkillManifestItem,
    policy: EffectiveOwnerPolicy,
) -> tuple[SkillSnapshotEntry | None, SkillLoadError | None]:
    try:
        card = parse_skill_file(
            item.path,
            source=item.root.source,
            require_frontmatter=True,
        )
        _validate_card(card)
    except (OSError, TypeError, ValueError) as exc:
        return None, _error(item.root, item.path, "SKILL_PARSE_FAILED", str(exc))
    decision = evaluate_skill_guard_gate(
        item.path.parent,
        SkillGuardRequest(source=item.root.source, skill_name=card.name),
    )
    if not decision.allowed:
        return None, _error(item.root, item.path, "SKILL_GUARD_DENIED", _guard_codes(decision.to_dict()))
    category = card.category if card.category != "general" else _derived_category(item.root.path, item.path)
    enabled = card.name not in set(policy.disabled_skills)
    if item.root.source == "shared" and policy.enabled_shared_skills:
        enabled = enabled and card.name in set(policy.enabled_shared_skills)
    return _snapshot_entry(item, card, category, enabled), None


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


# LLM: 无包时保持原指纹字节；包摘要覆盖全部私有资源，激活 ID 区分同字节卸载重装，不编码读取回调。
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


def _derived_category(root: Path, path: Path) -> str:
    try:
        return "/".join(path.parent.relative_to(root.resolve(strict=False)).parts[:-1])
    except ValueError:
        return "general"


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
