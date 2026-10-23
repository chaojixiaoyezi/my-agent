# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 模块用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
# 模块用途: 安全加载并按用户隔离地修改 SOUL/USER/AGENTS，同时保存首次修改前基线和后续版本。
"""Owner-scoped Persona repository with guarded loading and versioned writes."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
)
from ..user_space.owner_quota import (
    OwnerQuotaAdmission,
    OwnerQuotaChange,
    OwnerQuotaEnforcer,
    owner_quota_enforcer_from_policy,
)
from ..user_space.persona_templates import (
    FACTORY_AGENTS_SEED_VERSION,
    factory_agents_seed_for_sha256,
)
from .agents_source_contract import AgentsContractError, AgentsSourceSnapshot
from .agents_source_limits import enforce_agents_write_limit
from .persona_storage_io import _PERSONA_NATIVE_ERRORS, persona_io, versions_lock
from .persona_storage_io import append_version_records as append_private_jsonl_records
from .persona_version_ledger import _ledger_image, read_version_records

_TARGET_ATTR = {
    "soul": "owner_soul_md",
    "user": "owner_user_md",
    "agents": "owner_agents_md",
}
# 人设文件最大字节数，防止损坏文件拖垮加载。
_MAX_PERSONA_FILE_BYTES = 2 * 1024 * 1024
# 人设提示词默认最大字符数，超长截断。
_DEFAULT_PROMPT_MAX_CHARS = 20_000
_LEGACY_TEMPLATE_PLACEHOLDER_VALUES = {
    "(简短结论 / 详细解释 / 带步骤)",
    "(格式、长度、要不要代码/表格/要点)",
    "(预算 / 合规 / 设备 / 不能碰的)",
}


class PersonaRepositoryError(RuntimeError):
    """Base error for guarded Persona operations."""


class PersonaConflictError(PersonaRepositoryError):
    """The caller's file snapshot is stale."""


class PersonaEntryNotFoundError(PersonaRepositoryError):
    """A stable Persona entry id no longer exists."""


class PersonaSecurityError(PersonaRepositoryError):
    """A Persona file failed a boundary or injection guard."""


@dataclass(frozen=True)
class PersonaLoadDiagnostic:
    target: str
    state: str
    sha256: str = ""
    bytes: int = 0
    chars: int = 0
    truncated: bool = False
    blocked_lines: int = 0
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: 正文和诊断来自同一加载结果，来源metadata只描述冻结版本不授予权限；同步检查prompt冻结与撤权。
# 类用途: 保存一次人格加载的正文、诊断及AGENTS来源信息。
@dataclass(frozen=True)
class PersonaDocumentSnapshot:
    target: str
    content: str
    diagnostic: PersonaLoadDiagnostic
    agents_source: AgentsSourceSnapshot | None = None
    agents_source_metadata: dict | None = None


@dataclass(frozen=True)
class PersonaMutationRequest:
    """Typed mutation options keep the repository API and lock path compact."""

    target: str
    action: str
    content: str = ""
    entry_id: str = ""
    source_quote: str = ""
    confirmed: bool = False
    expected_sha256: str = ""
    rollback_version: int | None = None
    source: str = "agent_tool"

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", str(self.target or "").strip().lower())
        object.__setattr__(self, "action", str(self.action or "add").strip().lower())
        if self.rollback_version is not None:
            object.__setattr__(self, "rollback_version", int(self.rollback_version))


@dataclass(frozen=True)
class PersonaBatchMutationRequest:
    """Atomic ordered Persona mutations against one owner-scoped target."""

    target: str
    operations: tuple[PersonaMutationRequest, ...]
    expected_sha256: str = ""
    source: str = "agent_tool"
    # LLM: 迁移工具在删除前核对「版本账本尾」（文件长度+最后一条记录摘要）——计划之后任何写入，
    #   即使正文 sha 恰好恢复原值（删除后重添同文），也会让前提失效；空串表示普通调用方不核对。
    expected_versions_tail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", str(self.target or "").strip().lower())
        object.__setattr__(self, "operations", tuple(self.operations))


@dataclass(frozen=True)
class _PreparedPersonaBaseline:
    """Immutable version zero captured immediately before the first real mutation."""

    content: str
    sha256: str
    backup_path: Path
    record: dict[str, object]


@dataclass(frozen=True)
class _PreparedPersonaMutation:
    """Complete multi-file mutation projected before any authority write."""

    previous: str
    previous_sha: str
    next_text: str
    next_sha: str
    prior_content: str
    next_entry_id: str
    version: int
    backup_path: Path
    record: dict[str, object]
    operation_results: tuple[dict[str, object], ...] = ()
    baseline: _PreparedPersonaBaseline | None = None


# LLM: source正文/SHA来自同次严格加载；仅首次owner_default可投影已知工厂种子，显式/已改来源不变，原SHA仍冻结来源。
# 函数用途: 将AGENTS来源转成安全提示投影，保留原件、来源授权与版本身份。
def _load_agents_source_document(repository):
    try:
        result = repository.agents_source.load()
        snapshot = result.snapshot
        if result.content is None:
            return PersonaDocumentSnapshot("agents", "", PersonaLoadDiagnostic("agents", "security", detail=snapshot.failure_code),
                                           agents_source=snapshot)
        metadata = repository.agents_source.activation_metadata(snapshot)
    except AgentsContractError as error:
        return PersonaDocumentSnapshot("agents", "", PersonaLoadDiagnostic("agents", "security", detail=error.code))
    raw = result.content
    display = raw.replace("\r\n", "\n").replace("\r", "\n") if snapshot.kind == "owner_default" else raw
    prompt_content = display
    if snapshot.kind == "owner_default" and snapshot.config_version == 1 and snapshot.revision == 0 and snapshot.status == "ready":
        prompt_content = _factory_agents_prompt(repository, display, snapshot.content_sha256)
    sanitized, blocked = _sanitize_persona_text(prompt_content)
    content, truncated = _bounded_prompt_content(sanitized, repository.prompt_max_chars)
    state = "blocked" if blocked else ("truncated" if truncated else "ok")
    detail = f"factory_seed:v{FACTORY_AGENTS_SEED_VERSION}; original_preserved" if prompt_content != display else ""
    diagnostic = PersonaLoadDiagnostic("agents", state, _sha256_text(display), len(raw.encode()), len(display), truncated, blocked, detail)
    return PersonaDocumentSnapshot("agents", content, diagnostic, snapshot, metadata)


# LLM: 列举/摘要/版本及旧变更路径共用；显式来源不能退回默认文件，URL不能经本地写入绕过只读。
# 函数用途: 防止旧Persona API修改没有被本次来源授权指向的默认原件。
def _require_legacy_persona_target(repository, target: str) -> None:
    if target != "agents" or repository.agents_source is None:
        return
    if repository.agents_source.configuration().kind != "owner_default":
        raise PersonaRepositoryError("AGENTS_SOURCE_NATIVE_API_REQUIRED")


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 类用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
class PersonaRepository:
    """Single owner-local authority for SOUL/USER/AGENTS loading and mutation."""

    # LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
    # 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
    def __init__(
        self,
        *,
        owner_home: str | Path,
        soul_path: str | Path,
        user_path: str | Path,
        agents_path: str | Path,
        prompt_max_chars: int = _DEFAULT_PROMPT_MAX_CHARS,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
        coordinator_io: bool = False,
    ) -> None:
        if coordinator_io and (quota_enforcer is None or not quota_enforcer.coordinator_io):
            raise ValueError("C Persona requires the original coordinator quota dependency")
        self.coordinator_io = coordinator_io
        self.owner_home = Path(owner_home).resolve(strict=False)
        self.paths = {
            "soul": Path(soul_path).absolute(),
            "user": Path(user_path).absolute(),
            "agents": Path(agents_path).absolute(),
        }
        self.prompt_max_chars = max(256, int(prompt_max_chars))
        self.versions_path = self.owner_home / "persona" / "versions.jsonl"
        self.backups_dir = self.owner_home / "persona" / "backups"
        self.quota_enforcer = quota_enforcer or owner_quota_enforcer_from_policy(self.owner_home)
        self.agents_source = None

    # LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
    # 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
    @classmethod
    def from_home_paths(
        cls,
        home_paths: object,
        *,
        prompt_max_chars: int = _DEFAULT_PROMPT_MAX_CHARS,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
        coordinator_io: bool = False,
    ):
        soul_path = getattr(home_paths, "owner_soul_md", "")
        user_path = getattr(home_paths, "owner_user_md", "")
        agents_path = getattr(home_paths, "owner_agents_md", "")
        owner_home = getattr(home_paths, "owner_home_dir", "")
        if not owner_home:
            candidates = [
                Path(value).parent for value in (soul_path, user_path, agents_path) if value
            ]
            owner_home = candidates[0] if candidates else ""
        if not owner_home:
            raise PersonaRepositoryError("owner home is unavailable")
        return cls(
            owner_home=owner_home,
            soul_path=soul_path,
            user_path=user_path,
            agents_path=agents_path,
            prompt_max_chars=prompt_max_chars,
            quota_enforcer=quota_enforcer,
            **({} if not coordinator_io else {"coordinator_io": True}),
        )

    # LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
    # 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
    @classmethod
    def from_owner_result(
        cls,
        owner: object,
        *,
        prompt_max_chars: int = _DEFAULT_PROMPT_MAX_CHARS,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
        coordinator_io: bool = False,
    ):
        if coordinator_io and quota_enforcer is None:
            raise ValueError("C Persona requires the original coordinator quota dependency")
        return cls(
            owner_home=getattr(owner, "home_dir", ""),
            soul_path=getattr(owner, "soul_md", ""),
            user_path=getattr(owner, "user_md", ""),
            agents_path=getattr(owner, "agents_md", ""),
            prompt_max_chars=prompt_max_chars,
            quota_enforcer=quota_enforcer
            or owner_quota_enforcer_from_policy(
                getattr(owner, "home_dir", ""),
                quota_path=getattr(owner, "quota_json", None),
            ),
            **({} if not coordinator_io else {"coordinator_io": True}),
        )

    # LLM: 显式来源不允许旧API按默认路径读写；唯一来源授权入口在AgentsSourceRepository，默认兼容不退化。
    # 函数用途: 返回原owner默认人格路径，显式AGENTS来源要求原生来源API。
    def path_for(self, target: str) -> Path:
        _require_legacy_persona_target(self, target)
        if target not in self.paths:
            raise ValueError(f"unknown persona target: {target}")
        path = self.paths[target]
        if not str(path):
            raise PersonaRepositoryError(f"{target} path is unavailable")
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.owner_home)
        except ValueError as exc:
            raise PersonaSecurityError(f"{target} path escapes owner home") from exc
        return path

    def snapshot(self) -> dict[str, PersonaDocumentSnapshot]:
        return {target: self.load(target) for target in ("agents", "soul", "user")}

    # LLM: 只汇总既有安全加载诊断，不暴露正文或私有路径。
    # 函数用途: 返回运行时人格来源健康状态。
    def runtime_snapshot(self) -> dict[str, object]:
        return _persona_runtime_snapshot(self)

    # LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
    # 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
    def load(self, target: str) -> PersonaDocumentSnapshot:
        if target == "agents" and self.agents_source is not None:
            return _load_agents_source_document(self)
        try:
            path = self.path_for(target)
            if not path.exists():
                return _empty_persona_snapshot(target, "missing")
            with locked_json_path(path, **({} if not self.coordinator_io else {"birth_commit": _persona_birth_commit})):
                return _load_persona_unlocked(self, target)
        except (PersonaSecurityError, OSError) as exc:
            if self.coordinator_io and isinstance(exc, _PERSONA_NATIVE_ERRORS):
                raise
            state = "security" if isinstance(exc, PersonaSecurityError) else "io"
            return PersonaDocumentSnapshot(target, "", PersonaLoadDiagnostic(target=target, state=state, detail=str(exc)))

    # LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
    # 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
    def list_entries(self, target: str) -> dict[str, object]:
        with locked_json_path(self.path_for(target), **({} if not self.coordinator_io else {"birth_commit": _persona_birth_commit})):
            return self._list_entries_locked(target)

    # LLM: 迁移等调用者已持目标锁；再持来源读边界，防原生来源写者绕过目标锁修改正文。
    # 函数用途: 为条目、诊断和摘要取得同一完整版本，不重入目标flock。
    def _list_entries_locked(self, target: str) -> dict[str, object]:
        with _persona_source_read_boundary(self, target):
            return self._list_entries_source_locked(target)

    # LLM: 目标和可选来源读边界均已持有；历史读取仍按target→source→versions锁序。
    # 函数用途: 将同一版本正文和账本信息投影为可展示条目。
    def _list_entries_source_locked(self, target: str) -> dict[str, object]:
        path = self.path_for(target)
        snapshot = _load_persona_unlocked(self, target)
        if snapshot.diagnostic.state in {"security", "io"}:
            raise PersonaRepositoryError(snapshot.diagnostic.detail or snapshot.diagnostic.state)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        return {
            "target": target,
            # Listing is model-visible. Unsafe source lines retain a stable id
            # so they can be removed, but their untrusted text is never echoed.
            "entries": _persona_entries_for_output(raw, target),
            "sha256": _sha256_text(raw),
            "version": self._current_version(target, _sha256_text(raw)),
            "diagnostic": snapshot.diagnostic.to_dict(),
        }

    # LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
    # 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
    def history(self, target: str) -> list[dict[str, object]]:
        self.path_for(target)
        with versions_lock(self.versions_path, **({} if not self.coordinator_io else {"coordinator_io": True})):
            records = read_version_records(self.versions_path)
        return [row for row in records if str(row.get("target") or "") == target]

    # LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
    # 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
    def current_sha256(self, target: str) -> str:
        path = self.path_for(target)
        with locked_json_path(path, **({} if not self.coordinator_io else {"birth_commit": _persona_birth_commit})), _persona_source_read_boundary(self, target):
            self.path_for(target)
            if target == 'agents' and self.agents_source is not None:
                return _agents_source_sha256(self)
            raw = path.read_text(encoding="utf-8") if path.exists() else ""
        return _sha256_text(raw)

    # LLM: 群人格变更继续消费main原活主体能力；来源或批量API不能绕过群租约。
    # 函数用途: 核对原群权限后按同一配额和版本事务修改人格。
    def mutate(self, request: PersonaMutationRequest) -> dict[str, object]:
        from ..user_space.group_authorization_context import require_group_persona_lease

        require_group_persona_lease(self.path_for(request.target))
        with self.quota_enforcer.admission() as admission:
            return _mutate_persona_document(self, admission, request)

    # LLM: 批量与单项共用main原群能力门；锁内仍按完整CAS一次提交，不放宽owner边界。
    # 函数用途: 核对原群权限后原子提交一组人格变更。
    def mutate_batch(self, request: PersonaBatchMutationRequest) -> dict[str, object]:
        from ..user_space.group_authorization_context import require_group_persona_lease

        require_group_persona_lease(self.path_for(request.target))
        with self.quota_enforcer.admission() as admission:
            return _mutate_persona_batch_document(self, admission, request)

    # LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
    # 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
    def _write_version_snapshot(
        self,
        target: str,
        version: int,
        sha256: str,
        content: str,
    ) -> Path:
        from .persona_snapshot_transaction import write_version_snapshot
        return write_version_snapshot(self._version_snapshot_path(target, version, sha256), content, **({} if not self.coordinator_io else {"coordinator_io": True}))

    def _version_snapshot_path(self, target: str, version: int, sha256: str) -> Path:
        return self.backups_dir / target / f"{version:08d}-{sha256}.md"

    def _content_for_version(self, target: str, version: int) -> str:
        rows = [row for row in self.history(target) if int(row.get("version") or 0) == version]
        if not rows:
            raise PersonaEntryNotFoundError(f"persona version {version} not found")
        ref = str(rows[-1].get("backup_ref") or "")
        path = (self.owner_home / ref).resolve(strict=False)
        try:
            path.relative_to(self.backups_dir.resolve(strict=False))
        except ValueError as exc:
            raise PersonaSecurityError("persona backup path escapes owner backup root") from exc
        content = path.read_text(encoding="utf-8")
        if _sha256_text(content) != str(rows[-1].get("sha256") or ""):
            raise PersonaSecurityError("persona backup hash mismatch")
        return content

    def _next_version(self, target: str) -> int:
        versions = [int(row.get("version") or 0) for row in self.history(target)]
        return max(versions, default=0) + 1

    def _current_version(self, target: str, current_sha: str) -> int:
        rows = [row for row in self.history(target) if str(row.get("sha256") or "") == current_sha]
        return max((int(row.get("version") or 0) for row in rows), default=0)


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _mutate_persona_document(
    repository: PersonaRepository,
    admission: OwnerQuotaAdmission,
    request: PersonaMutationRequest,
) -> dict[str, object]:
    if request.action not in {"add", "replace", "remove", "rollback"}:
        raise ValueError(f"unsupported persona action: {request.action}")
    path = repository.path_for(request.target)
    with locked_json_path(path, **({} if not repository.coordinator_io else {"birth_commit": _persona_birth_commit})):
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise PersonaSecurityError("persona target must be a regular file")
        document_existed = path.exists()
        previous = path.read_text(encoding="utf-8") if document_existed else ""
        prepared = _prepare_persona_mutation(repository, request, previous)
        if isinstance(prepared, dict):
            return prepared
        _commit_persona_mutation(repository, prepared, _PersonaCommitContext(path, document_existed, admission))
    return _persona_mutation_result(request, prepared)


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _mutate_persona_batch_document(
    repository: PersonaRepository,
    admission: OwnerQuotaAdmission,
    request: PersonaBatchMutationRequest,
) -> dict[str, object]:
    if len(request.operations) > 32:
        raise ValueError("persona batch exceeds 32 operations")
    with locked_json_path(repository.path_for(request.target), **({} if not repository.coordinator_io else {"birth_commit": _persona_birth_commit})):
        return _mutate_persona_batch_locked(repository, admission, request)


# LLM: 调用方必须已持 quota admission 和本目标的人格锁；仍执行完整 CAS、版本尾、内容与配额校验。
# 普通入口的 32 操作限制留在锁外入口；可信迁移可在同一持锁区内一次提交全部已重判定删除。
# 函数用途: 不重复取锁地准备并提交一个原子批次，返回锁内采样的账本尾。
# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _mutate_persona_batch_locked(
    repository: PersonaRepository,
    admission: OwnerQuotaAdmission,
    request: PersonaBatchMutationRequest,
) -> dict[str, object]:
    if request.target not in _TARGET_ATTR:
        raise ValueError(f"unknown persona target: {request.target}")
    if not request.operations:
        raise ValueError("persona batch operations are required")
    for operation in request.operations:
        if operation.target != request.target:
            raise ValueError("all persona batch operations must use the batch target")
        if operation.action not in {"add", "replace", "remove"}:
            raise ValueError("persona batch supports add/replace/remove only")
    path = repository.path_for(request.target)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise PersonaSecurityError("persona target must be a regular file")
    document_existed = path.exists()
    previous = path.read_text(encoding="utf-8") if document_existed else ""
    prepared = _prepare_persona_batch_mutation(repository, request, previous)
    if isinstance(prepared, dict):
        return {**prepared, "versions_tail": versions_ledger_tail(repository.versions_path, **({} if not repository.coordinator_io else {"coordinator_io": True}))}
    _commit_persona_mutation(repository, prepared, _PersonaCommitContext(path, document_existed, admission))
    committed_tail = versions_ledger_tail(repository.versions_path, **({} if not repository.coordinator_io else {"coordinator_io": True}))
    result = _persona_batch_mutation_result(request, prepared)
    result["versions_tail"] = committed_tail
    return result


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _prepare_persona_batch_mutation(
    repository: PersonaRepository,
    request: PersonaBatchMutationRequest,
    previous: str,
) -> _PreparedPersonaMutation | dict[str, object]:
    previous_sha = _sha256_text(previous)
    if request.expected_sha256 and request.expected_sha256 != previous_sha:
        raise PersonaConflictError(
            f"persona changed: expected {request.expected_sha256}, current {previous_sha}"
        )
    # LLM: 迁移的版本账本前提核对在人格锁内：计划之后任何一次写入（含「删除后重添同文」这种
    #   正文 sha 恢复原值的情况）都会改变账本尾，这里直接拒绝，要求重新空跑。
    if request.expected_versions_tail:
        current_tail = versions_ledger_tail(repository.versions_path, **({} if not repository.coordinator_io else {"coordinator_io": True}))
        if current_tail != request.expected_versions_tail:
            raise PersonaConflictError(
                "persona version ledger changed since the plan was taken"
            )
    working = previous
    operation_results: list[dict[str, object]] = []
    for operation in request.operations:
        if operation.expected_sha256 and operation.expected_sha256 != previous_sha:
            raise PersonaConflictError(
                f"persona changed: expected {operation.expected_sha256}, current {previous_sha}"
            )
        working, prior_content, next_entry_id, changed = _apply_operation_to_text(
            working,
            target=request.target,
            action=operation.action,
            content=operation.content,
            entry_id=operation.entry_id,
        )
        operation_results.append(
            {
                "action": operation.action,
                "entry_id": next_entry_id or operation.entry_id,
                "prior_content": prior_content,
                "content": operation.content
                if operation.action in {"add", "replace"}
                else "",
                "changed": changed,
            }
        )
    if working == previous:
        return {
            "ok": True,
            "changed": False,
            "action": "batch",
            "target": request.target,
            "operations": operation_results,
            "sha256": previous_sha,
            "version": repository._current_version(request.target, previous_sha),
        }
    if len(working.encode("utf-8")) > _MAX_PERSONA_FILE_BYTES:
        raise ValueError("persona file exceeds maximum size")
    next_sha = _sha256_text(working)
    version = repository._next_version(request.target)
    backup_path = repository._version_snapshot_path(request.target, version, next_sha)
    audit_operations = [
        {
            "action": operation.action,
            "entry_id": result["entry_id"],
            "source_quote": operation.source_quote,
            "changed": result["changed"],
        }
        for operation, result in zip(request.operations, operation_results, strict=True)
    ]
    record: dict[str, object] = {
        "target": request.target,
        "version": version,
        "sha256": next_sha,
        "previous_sha256": previous_sha,
        "source_quote": "",
        "confirmed": True,
        "created_at": time.time(),
        "backup_ref": str(backup_path.relative_to(repository.owner_home)),
        "action": "batch",
        "entry_id": "",
        "operations": audit_operations,
        "source": request.source,
    }
    baseline = _prepare_persona_baseline(repository, request.target, previous)
    return _PreparedPersonaMutation(
        previous=previous,
        previous_sha=previous_sha,
        next_text=working,
        next_sha=next_sha,
        prior_content="",
        next_entry_id="",
        version=version,
        backup_path=backup_path,
        record=record,
        operation_results=tuple(operation_results),
        baseline=baseline,
    )


# LLM: Preparation derives the next authority bytes and audit record without writing any file.
# 函数用途: 在配额检查前完成 CAS、操作应用、版本号和备份位置计算。
def _prepare_persona_mutation(
    repository: PersonaRepository,
    request: PersonaMutationRequest,
    previous: str,
) -> _PreparedPersonaMutation | dict[str, object]:
    previous_sha = _sha256_text(previous)
    if request.expected_sha256 and request.expected_sha256 != previous_sha:
        raise PersonaConflictError(
            f"persona changed: expected {request.expected_sha256}, current {previous_sha}"
        )
    if request.action == "rollback":
        next_text, prior_content, next_entry_id = _prepare_persona_rollback(repository, request)
    else:
        next_text, prior_content, next_entry_id, changed = _apply_operation_to_text(
            previous,
            target=request.target,
            action=request.action,
            content=request.content,
            entry_id=request.entry_id,
        )
        if not changed:
            return _unchanged_persona_result(repository, request, previous_sha, next_entry_id)
    if len(next_text.encode("utf-8")) > _MAX_PERSONA_FILE_BYTES:
        raise ValueError("persona file exceeds maximum size")
    next_sha = _sha256_text(next_text)
    version = repository._next_version(request.target)
    backup_path = repository._version_snapshot_path(request.target, version, next_sha)
    record = _persona_version_record(
        repository, request, previous_sha, next_sha, version, backup_path, next_entry_id
    )
    baseline = _prepare_persona_baseline(repository, request.target, previous)
    return _PreparedPersonaMutation(
        previous=previous,
        previous_sha=previous_sha,
        next_text=next_text,
        next_sha=next_sha,
        prior_content=prior_content,
        next_entry_id=next_entry_id,
        version=version,
        backup_path=backup_path,
        record=record,
        baseline=baseline,
    )


# LLM: Version zero is materialized only alongside the first successful mutation for one target.
# 函数用途: 为首次 Persona 修改准备“修改前”快照，让新用户也能回滚，而不改变首个修改版仍为 1 的兼容语义。
def _prepare_persona_baseline(
    repository: PersonaRepository,
    target: str,
    previous: str,
) -> _PreparedPersonaBaseline | None:
    if repository.history(target):
        return None
    sha256 = _sha256_text(previous)
    backup_path = repository._version_snapshot_path(target, 0, sha256)
    return _PreparedPersonaBaseline(
        content=previous,
        sha256=sha256,
        backup_path=backup_path,
        record={
            "target": target,
            "version": 0,
            "sha256": sha256,
            "previous_sha256": sha256,
            "source_quote": "",
            "confirmed": True,
            "created_at": time.time(),
            "backup_ref": str(backup_path.relative_to(repository.owner_home)),
            "action": "baseline",
            "entry_id": "",
            "source": "system_baseline",
        },
    )


def _prepare_persona_rollback(
    repository: PersonaRepository,
    request: PersonaMutationRequest,
) -> tuple[str, str, str]:
    if request.rollback_version is None:
        raise ValueError("rollback_version is required")
    next_text = repository._content_for_version(request.target, request.rollback_version)
    from ..memory_store.security import scan_memory_content

    if not scan_memory_content(next_text).safe:
        raise PersonaSecurityError("rollback snapshot failed persona threat scan")
    return next_text, "", ""


def _unchanged_persona_result(
    repository: PersonaRepository,
    request: PersonaMutationRequest,
    previous_sha: str,
    next_entry_id: str,
) -> dict[str, object]:
    return {
        "ok": True,
        "changed": False,
        "action": request.action,
        "target": request.target,
        "entry_id": next_entry_id,
        "sha256": previous_sha,
        "version": repository._current_version(request.target, previous_sha),
    }


def _persona_version_record(
    repository: PersonaRepository,
    request: PersonaMutationRequest,
    previous_sha: str,
    next_sha: str,
    version: int,
    backup_path: Path,
    next_entry_id: str,
) -> dict[str, object]:
    return {
        "target": request.target,
        "version": version,
        "sha256": next_sha,
        "previous_sha256": previous_sha,
        "source_quote": request.source_quote,
        "confirmed": request.confirmed,
        "created_at": time.time(),
        "backup_ref": str(backup_path.relative_to(repository.owner_home)),
        "action": request.action,
        "entry_id": next_entry_id or request.entry_id,
        "source": request.source,
    }


# LLM: 来源摘要只消费严格加载诊断，调用方已经持目标和来源读锁。
# 函数用途: 拒绝不安全或不可读来源，并保留合法空文件的摘要口径。
def _agents_source_sha256(repository):
    snapshot = _load_agents_source_document(repository)
    if snapshot.diagnostic.state in {'security', 'io'}:
        raise PersonaRepositoryError(snapshot.diagnostic.detail or snapshot.diagnostic.state)
    return snapshot.diagnostic.sha256 or _sha256_text('')


# LLM: 绑定同一次目标锁下确定的路径、存在状态和配额准入，不可跨事务重用。
# 类用途: 将正文提交的目的地与准入上下文作为一个整体传递。
@dataclass(frozen=True)
class _PersonaCommitContext:
    path: Path
    document_existed: bool
    admission: OwnerQuotaAdmission


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _admit_persona_mutation(
    repository: PersonaRepository,
    context: _PersonaCommitContext,
    prepared: _PreparedPersonaMutation,
    original: bytes,
) -> None:
    admission, path = context.admission, context.path
    if prepared.record["target"] == "agents":
        enforce_agents_write_limit(prepared.previous, prepared.next_text, repository.prompt_max_chars)
    from .persona_version_ledger import retained_ledger_bytes

    version_records = [prepared.record]
    changes = [
        OwnerQuotaChange(path, max(len(prepared.next_text.encode("utf-8")), len(original))),
        OwnerQuotaChange(prepared.backup_path, 2 * len(prepared.next_text.encode("utf-8")), append=True),
        OwnerQuotaChange(_persona_preimage_path(repository, prepared, original), 2 * len(original), append=True),
    ]
    if prepared.baseline is not None:
        version_records.insert(0, prepared.baseline.record)
        changes.append(
            OwnerQuotaChange(
                prepared.baseline.backup_path,
                2 * len(prepared.baseline.content.encode("utf-8")),
                append=True,
            )
        )
    version_blob = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in version_records
    )
    changes.append(
        OwnerQuotaChange(
            repository.versions_path,
            2 * (len(version_blob.encode("utf-8")) + retained_ledger_bytes(repository.versions_path, **({} if not repository.coordinator_io else {"coordinator_io": True}))),
            append=True,
        )
    )
    admission.check(changes)


# LLM: 只计算owner内按完整原始SHA去重的恢复路径，不将它加入成功版本账本。
# 函数用途: 给账外手工修改也留下可定位、不覆盖的真实前像。
def _persona_preimage_path(repository, prepared, original):
    return repository.backups_dir / prepared.record['target'] / 'preimages' / (hashlib.sha256(original).hexdigest() + '.md')


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _commit_persona_mutation(
    repository: PersonaRepository,
    prepared: _PreparedPersonaMutation,
    context: _PersonaCommitContext,
) -> None:
    from .persona_snapshot_transaction import snapshot_transaction
    from .persona_version_ledger import PersonaLedgerPublishedError, append_persona_versions

    path, document_existed = context.path, context.document_existed
    with snapshot_transaction(**({} if not repository.coordinator_io else {"coordinator_io": True})) as snapshots, _persona_mutation_writer(
        repository, path, prepared.previous, document_existed,
    ) as write_content:
        original = write_content.original if document_existed else b''
        _admit_persona_mutation(repository, context, prepared, original)
        recovery = _retain_persona_preimage(repository, prepared, original)
        try:
            records = _write_persona_mutation_versions(repository, prepared)
            write_content(prepared.next_text)
            append_persona_versions(repository.versions_path, records, append_private_jsonl_records,
                                    validate=lambda: _verify_persona_commit(write_content, snapshots),
                                    **({} if not repository.coordinator_io else {"coordinator_io": True}))
        except PersonaLedgerPublishedError:
            raise
        except Exception as error:
            if repository.coordinator_io and isinstance(error, _PERSONA_NATIVE_ERRORS):
                raise
            _note_persona_preimage(error, repository, recovery)
            _restore_persona_mutation_files(path, prepared, (document_existed, snapshots), write_content, **({} if not repository.coordinator_io else {"coordinator_io": True}))
            raise


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _retain_persona_preimage(repository, prepared, original):
    if not original:
        return None
    from .persona_snapshot_transaction import write_version_snapshot
    recovery = _persona_preimage_path(repository, prepared, original)
    write_version_snapshot(recovery, original.decode('utf-8'), retain_on_failure=True, **({} if not repository.coordinator_io else {'coordinator_io': True}))
    return recovery


# LLM: 只报告已经发布的前像引用，不改变异常类别或把恢复材料当作成功版本。
# 函数用途: 让失败操作能定位自己保存的原始正文。
def _note_persona_preimage(error, repository, recovery):
    if recovery is not None:
        error.add_note(f'Persona recovery preimage: {recovery.relative_to(repository.owner_home)}')


def _persona_mutation_result(
    request: PersonaMutationRequest,
    prepared: _PreparedPersonaMutation,
) -> dict[str, object]:
    return {
        "ok": True,
        "changed": True,
        "action": request.action,
        "target": request.target,
        "entry_id": prepared.next_entry_id or request.entry_id,
        "prior_content": prepared.prior_content,
        "content": request.content if request.action in {"add", "replace"} else "",
        "sha256": prepared.next_sha,
        "version": prepared.version,
        "backup_ref": prepared.record["backup_ref"],
    }


def _persona_batch_mutation_result(
    request: PersonaBatchMutationRequest,
    prepared: _PreparedPersonaMutation,
) -> dict[str, object]:
    return {
        "ok": True,
        "changed": True,
        "action": "batch",
        "target": request.target,
        "operations": list(prepared.operation_results),
        "sha256": prepared.next_sha,
        "version": prepared.version,
        "backup_ref": prepared.record["backup_ref"],
    }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# LLM: 「版本账本尾」= 文件字节长度 + 最后一条非空记录的 sha256；任何一次追加（哪怕正文 sha
#   恢复原值）都会改变它，用于迁移删除前的锁内前提核对。文件缺失返回空串（无账本可核对）。
# 函数用途: 与发布共用路径锁计算完整账本尾部指纹，不采样发布空窗。
# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def versions_ledger_tail(path: Path, *, coordinator_io=False) -> str:
    with versions_lock(path, **({} if not coordinator_io else {"coordinator_io": True})):
        image = _ledger_image(path)
        if image is None:
            return ""
        data = image[1]
    lines = [line for line in data.splitlines() if line.strip()]
    last = lines[-1] if lines else b""
    return f"{len(data)}:{hashlib.sha256(last).hexdigest()}"


def persona_entry_id(target: str, content: str) -> str:
    digest = hashlib.sha256(f"{target}\0{content}".encode()).hexdigest()[:16]
    return f"persona-{digest}"


def _persona_entries_from_text(text: str, target: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        content = stripped[2:].strip()
        if _is_empty_persona_template_content(content):
            continue
        if content:
            entries.append({"entry_id": persona_entry_id(target, content), "content": content})
    return entries


def _persona_entries_for_output(
    text: str,
    target: str,
) -> list[dict[str, str | bool]]:
    """Return model-visible entries without replaying poisoned source text."""

    entries: list[dict[str, str | bool]] = []
    for entry in _persona_entries_from_text(text, target):
        content = entry["content"]
        from ..memory_store.security import scan_memory_content
        if scan_memory_content(content).safe:
            entries.append(entry)
            continue
        entries.append(
            {
                "entry_id": entry["entry_id"],
                "content": "[BLOCKED unsafe persona entry]",
                "blocked": True,
            }
        )
    return entries


def _apply_operation_to_text(
    existing: str,
    *,
    target: str,
    action: str,
    content: str,
    entry_id: str,
) -> tuple[str, str, str, bool]:
    if action in {"add", "replace"}:
        content = str(content or "").strip()
        if not content:
            raise ValueError("persona content is required")
        from ..memory_store.security import scan_memory_content
        scan = scan_memory_content(content)
        if not scan.safe:
            raise PersonaSecurityError(scan.reason())
    entries = _persona_entries_from_text(existing, target)
    if action == "add":
        next_id = persona_entry_id(target, content)
        if any(row["entry_id"] == next_id for row in entries):
            return existing, "", next_id, False
        filled = _fill_matching_empty_persona_slot(existing, content)
        if filled is not None:
            return filled, "", next_id, True
        separator = "" if not existing or existing.endswith("\n") else "\n"
        return f"{existing}{separator}- {content}\n", "", next_id, True
    lines = existing.splitlines(keepends=True)
    match_index = -1
    prior_content = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        candidate = stripped[2:].strip()
        if persona_entry_id(target, candidate) == entry_id:
            match_index = index
            prior_content = candidate
            break
    if match_index < 0:
        raise PersonaEntryNotFoundError(entry_id)
    if action == "remove":
        del lines[match_index]
        return "".join(lines), prior_content, entry_id, True
    newline = "\n" if lines[match_index].endswith("\n") else ""
    lines[match_index] = f"- {content}{newline}"
    return "".join(lines), prior_content, persona_entry_id(target, content), True


def _fill_matching_empty_persona_slot(existing: str, content: str) -> str | None:
    """Fill an existing ``- key:`` template slot instead of appending a duplicate key."""

    incoming = _persona_key_value(content)
    if incoming is None or not incoming[1]:
        return None
    incoming_key = incoming[0].casefold()
    lines = existing.splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        current = _persona_key_value(stripped[2:].strip())
        if current is None or current[0].casefold() != incoming_key:
            continue
        if current[1] and current[1] not in _LEGACY_TEMPLATE_PLACEHOLDER_VALUES:
            continue
        indentation = line[: len(line) - len(line.lstrip())]
        newline = "\n" if line.endswith("\n") else ""
        lines[index] = f"{indentation}- {content}{newline}"
        return "".join(lines)
    return None


def _persona_key_value(content: str) -> tuple[str, str] | None:
    positions = [position for separator in (":", "：") if (position := content.find(separator)) >= 0]
    if not positions:
        return None
    position = min(positions)
    key = content[:position].strip()
    if not key:
        return None
    return key, content[position + 1 :].strip()


def _sanitize_persona_text(content: str) -> tuple[str, int]:
    lines: list[str] = []
    blocked = 0
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("- ") and _is_empty_persona_template_content(
            stripped[2:].strip()
        ):
            continue
        from ..memory_store.security import scan_memory_content
        scan = scan_memory_content(line)
        if scan.safe:
            lines.append(line)
            continue
        blocked += 1
        suffix = "\n" if line.endswith("\n") else ""
        pattern_ids = ",".join(finding.pattern_id for finding in scan.findings[:4])
        lines.append(f"[BLOCKED persona line: {pattern_ids}]{suffix}")
    return "".join(lines), blocked


def _is_empty_persona_template_content(content: str) -> bool:
    key_value = _persona_key_value(content)
    if key_value is None:
        return False
    value = key_value[1]
    return not value or value in _LEGACY_TEMPLATE_PLACEHOLDER_VALUES


# LLM: 预算含原标记；首尾配额可重分配，单行超过总正文预算才字符兜底，绝不超过max_chars。
# 函数用途: 不在普通规则中间截断人格提示；极小预算宁缺正文，原文件不受影响。
def _bounded_prompt_content(content: str, max_chars: int) -> tuple[str, bool]:
    max_chars = max(0, max_chars)
    if len(content) <= max_chars:
        return content, False
    marker = "\n[... persona content truncated by configured prompt budget ...]\n"
    if max_chars < len(marker):
        return "", True
    budget = max_chars - len(marker)
    head = int(budget * 0.75)
    first_line = content.find("\n") + 1
    last_end = len(content) - 1 if content.endswith("\n") else len(content)
    last_line = len(content) - content.rfind("\n", 0, last_end) - 1
    if 0 < first_line <= budget:
        head = max(head, first_line)
    # 首尾都能保留时先给完整末行留位；两端竞争时优先首部，不制造半条规则。
    if last_line <= budget and (first_line > budget or first_line + last_line <= budget):
        head = min(head, budget - last_line)
    boundary = content.rfind("\n", 0, head)
    prefix = content[:boundary + 1] if boundary >= 0 else ""
    if boundary < 0 and (first_line == 0 or first_line > budget):
        prefix = content[:head]
    tail = budget - len(prefix)
    suffix = _persona_tail_at_boundary(content, tail, allow_partial=last_line > budget)
    return prefix + marker + suffix, True


# LLM: 调用方按总正文预算决定是否允许末行片段；不因剩余配额不足自动截断可完整保留的行。
# 函数用途: 在尾部预算内从完整行开头保留剩余人格内容。
def _persona_tail_at_boundary(content: str, budget: int, *, allow_partial: bool = True) -> str:
    if not budget:
        return ""
    start = len(content) - budget
    if start == 0 or content[start - 1] == "\n":
        return content[start:]
    boundary = content.find("\n", start)
    if 0 <= boundary < len(content) - 1:
        return content[boundary + 1:]
    return content[-budget:] if allow_partial else ""


# LLM: 仅默认AGENTS、精确原始字节SHA、没有任何人格版本账本时给新工厂视图；不写原文/版本/审批记录。
# 函数用途: 保守识别未修改出厂种子；用户定制、历史账本或无法核实的状态均原样显示。
def _factory_agents_prompt(repository, raw: str, raw_sha256: str) -> str:
    candidate = factory_agents_seed_for_sha256(raw_sha256)
    if candidate is None or repository.paths["agents"] != repository.owner_home / "AGENTS.md":
        return raw
    if repository.versions_path.parent.is_symlink():
        return raw
    for ledger in (repository.versions_path, repository.versions_path.parent / "agents_source.json"):
        try:
            ledger.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return raw
        return raw
    return candidate


__all__ = [
    "PersonaBatchMutationRequest",
    "PersonaConflictError",
    "PersonaDocumentSnapshot",
    "PersonaEntryNotFoundError",
    "PersonaLoadDiagnostic",
    "PersonaMutationRequest",
    "PersonaRepository",
    "PersonaRepositoryError",
    "PersonaSecurityError",
    "persona_entry_id",
    "versions_ledger_tail",
]


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
@contextmanager
def _persona_mutation_writer(repository: PersonaRepository, path: Path, previous: str, document_existed: bool):
    source = repository.agents_source
    if source is None or path != source.default_path:
        from .persona_body_transaction import persona_body_writer
        with persona_body_writer(path, previous, document_existed, **({} if not repository.coordinator_io else {"coordinator_io": True})) as writer:
            yield writer
        return
    if not document_existed:
        from .agents_source_create import missing_default_writer
        with missing_default_writer(source, path, previous) as writer:
            yield writer
        return
    from .agents_source_local import default_persona_writer
    with default_persona_writer(source, path, previous) as writer:
        yield writer


# LLM: 新快照及首次基线写入顺序不变；写入器登记排他创建身份，失败按实际归属回滚。
# 函数用途: 提交当前Persona操作的版本快照并交回同批账本记录。
def _write_persona_mutation_versions(repository: PersonaRepository, prepared: _PreparedPersonaMutation) -> list[dict]:
    records = []
    target = str(prepared.record["target"])
    if prepared.baseline is not None:
        repository._write_version_snapshot(target, 0, prepared.baseline.sha256, prepared.baseline.content)
        records.append(prepared.baseline.record)
    repository._write_version_snapshot(target, prepared.version, prepared.next_sha, prepared.next_text)
    records.append(prepared.record)
    return records


# LLM: 三种正文写入器都提供身份/字节核验；快照核验也覆盖复用的既有对象。
# 函数用途: 在私有账本已写好后，最后复核即将登记的正文和快照。
def _verify_persona_commit(writer, snapshots):
    from .persona_snapshot_transaction import verify_snapshots
    writer.verify()
    verify_snapshots(snapshots)


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _restore_persona_mutation_files(path: Path, prepared: _PreparedPersonaMutation, state: tuple, write_content, *, coordinator_io=False) -> None:
    from .persona_snapshot_transaction import rollback_snapshots
    if coordinator_io:
        try:
            write_content.restore()
        except _PERSONA_NATIVE_ERRORS:
            raise
        except BaseException:
            rollback_snapshots(state[1], coordinator_io=True)
            raise
        rollback_snapshots(state[1], coordinator_io=True)
        return
    try:
        write_content.restore()
    finally:
        rollback_snapshots(state[1])


# LLM: 调用者持目标锁；原始字节摘要只用于已知工厂投影，诊断/CAS保留旧换行口径，来源服务仍走原同步。
# 函数用途: 在一致原件上安全加载并可选投影工厂种子，不修改文件或重复取锁。
def _load_persona_unlocked(self, target: str) -> PersonaDocumentSnapshot:
    if target == "agents" and self.agents_source is not None:
        return _load_agents_source_document(self)
    try:
        path = self.path_for(target)
    except PersonaSecurityError as exc:
        return _empty_persona_snapshot(target, "security", str(exc))
    if not path.exists():
        return _empty_persona_snapshot(target, "missing")
    try:
        if path.is_symlink() or not path.is_file():
            return _empty_persona_snapshot(target, "security", "persona source must be a regular file, not a symlink")
        stat = path.stat()
        if stat.st_size > _MAX_PERSONA_FILE_BYTES:
            return _empty_persona_snapshot(target, "security", "persona source exceeds guarded file-size limit", int(stat.st_size))
        data = path.read_bytes()
        raw = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeDecodeError) as exc:
        return _empty_persona_snapshot(target, "io", str(exc))
    projected = _factory_agents_prompt(self, raw, hashlib.sha256(data).hexdigest()) if target == "agents" else raw
    return _loaded_persona_snapshot(target, raw, stat, self.prompt_max_chars, prompt_content=projected)


# LLM: 保持缺失/拒绝/I/O诊断不返回正文，尺寸字段只在已读stat时提供。
# 函数用途: 共用人格读取失败的空正文投影。
def _empty_persona_snapshot(target, state, detail="", byte_count=0):
    diagnostic = PersonaLoadDiagnostic(target=target, state=state, detail=detail, bytes=byte_count)
    return PersonaDocumentSnapshot(target, "", diagnostic)


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _persona_runtime_snapshot(self) -> dict[str, object]:
    """Project guarded Persona load health without content or private paths."""

    try:
        snapshots = self.snapshot()
        targets = []
        for target in ("agents", "soul", "user"):
            diagnostic = snapshots[target].diagnostic
            targets.append(
                {
                    "target": target,
                    "state": diagnostic.state,
                    "truncated": diagnostic.truncated,
                    "blocked_lines": diagnostic.blocked_lines,
                    "version": self._current_version(target, diagnostic.sha256),
                }
            )
    except Exception as exc:
        if self.coordinator_io and isinstance(exc, _PERSONA_NATIVE_ERRORS):
            raise
        return {
            "state": "unavailable",
            "health": "unavailable",
            "targets": [],
            "load_error_codes": [type(exc).__name__],
        }
    degraded = any(row["state"] not in {"ok", "missing"} for row in targets)
    return {
        "state": "available",
        "health": "degraded" if degraded else "healthy",
        "targets": targets,
        "confirmation_required": ["soul"],
        "autonomous_targets": ["user"],
        "versioned": True,
    }


# LLM: 诊断/CAS摘要仍绑定原文；工厂提示视图独立清洗并标版本，不伪造原文件修订。
# 函数用途: 将原件及可选工厂视图转换为有界提示与可追溯诊断。
def _loaded_persona_snapshot(target, raw, stat, prompt_max_chars, *, prompt_content=None):
    sha256 = _sha256_text(raw)
    projected = raw if prompt_content is None else prompt_content
    sanitized, blocked = _sanitize_persona_text(projected)
    content, truncated = _bounded_prompt_content(sanitized, prompt_max_chars)
    state = "blocked" if blocked else ("truncated" if truncated else "ok")
    return PersonaDocumentSnapshot(
        target,
        content,
        PersonaLoadDiagnostic(
            target=target,
            state=state,
            sha256=sha256,
            bytes=int(stat.st_size),
            chars=len(raw),
            truncated=truncated,
            blocked_lines=blocked,
            detail=f"factory_seed:v{FACTORY_AGENTS_SEED_VERSION}; original_preserved" if projected != raw else "",
        ),
    )


# LLM: 仅AGENTS来源服务的本地读者需要来源SH边界；调用方已持目标锁，绝不反向获取它。
# 函数用途: 列表与摘要跟原生来源写者同步，普通USER/SOUL保持原锁合同。
@contextmanager
def _persona_source_read_boundary(repository, target):
    if target != 'agents' or repository.agents_source is None:
        yield
        return
    from .agents_source_read_lock import source_read_boundary
    with source_read_boundary(repository.owner_home):
        yield


# LLM: C只运输bool并复用同原quota，目标/版本锁出生消费当前scope，实际native先透传不补偿；版本CAS/权限/默认False合同不变。
# 函数用途: 让原USER/SOUL版本事务选用受当前来源约束的本地写叶，默认算法和调用形状保持。
def _persona_birth_commit(operation):
    return persona_io(operation, coordinator_io=True)
