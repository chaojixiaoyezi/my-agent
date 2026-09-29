# LLM: 逐轮 Skill 与能力包共用一个快照和引用合同；公开 Skill 与包内资源保持独立命名域，不能展平后再过滤。
# 模块用途: 固定本轮可发现的方法身份，按原哈希和明确包代次读取，并为委派生成不扩权的子集。
from __future__ import annotations

"""Immutable per-turn Skill catalog and guarded body reads."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ..contracts.gates.skill_guard import SkillGuardRequest, evaluate_skill_guard_gate
from ..runtime_errors import is_structured_error_code
from .package_snapshot import CapabilityPackageSnapshot
from .skills import SkillCard

# 抛出点传入的码形状不合格时 SkillSnapshotError 回落到这个固定码；出现它说明抛出点本身有缺陷。
SKILL_SNAPSHOT_ERROR_CODE_INVALID = "SKILL_SNAPSHOT_ERROR_CODE_INVALID"

PACKAGE_PIN_ERROR_MESSAGES = {
    "CAPABILITY_PACKAGE_PIN_UNAVAILABLE": "原任务固定的能力包当前不可用。",
    "CAPABILITY_PACKAGE_PIN_STALE": "当前同名能力包与原任务固定的版本或激活代次不符。",
}


# LLM: error_code 是这族异常唯一的机器可读码（大写下划线常量），调用方、后台失败分类和测试只读它，不解析消息；
#   消息仍是"码 + 可选明细"的原格式，只给人看。抛出点的第一个参数必须是码常量或上游异常的结构化码属性，
#   由 test_skill_snapshot_error_codes 的 AST 守卫钉住。reason 是可选的结构化内部原因，只用于诊断，
#   不参与授权或恢复判定。改动时联查 skill_search_tool._snapshot_unavailable 与 conversation.wake_poison。
# 类用途: 表示当轮快照里选定的 Skill 或能力包已不能读取，带结构化错误码，并可附带读取失败的内部原因。
class SkillSnapshotError(RuntimeError):
    """A selected Skill can no longer be read from the immutable turn snapshot."""

    # LLM: detail 只拼进给人看的消息（例如 package=…、skill=…），不进入 error_code；reason 只能由宿主内部异常的
    #   结构化字段填入，不从消息或模型文本解析。码的形状运行时再核一次（is_structured_error_code），不合格时
    #   error_code 回落到固定的 SKILL_SNAPSHOT_ERROR_CODE_INVALID，原值只放进给人看的消息。
    # 函数用途: 保存结构化错误码、原格式消息和可选的内部原因。
    def __init__(self, error_code: str, detail: str = "", *, reason: str = "") -> None:
        code = str(error_code or "").strip()
        if not is_structured_error_code(code):
            detail = f"invalid_code={error_code!r} {detail}".rstrip()
            code = SKILL_SNAPSHOT_ERROR_CODE_INVALID
        super().__init__(f"{code} {detail}" if detail else code)
        self.error_code = code
        self.reason = reason


@dataclass(frozen=True)
class SkillLoadError:
    path: str
    code: str
    message: str
    source: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
            "source": self.source,
        }


# LLM: 原公开 Skill 元数据与正文摘要保持原语义；to_ref 只投影现有任务授权账，不含路径。
# 类用途: 固定一个全局可发现 Skill 的身份与正文版本，供索引、读取和委派共同使用。
@dataclass(frozen=True)
class SkillSnapshotEntry:
    stable_id: str
    name: str
    description: str
    path: str
    source: str
    category: str
    content_sha256: str
    enabled: bool = True
    when_to_use: str = ""
    platforms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    tools_required: tuple[str, ...] = ()
    risk_level: str = "low"

    # LLM: 与原 skill_snapshot_refs 四字段一致；不把展示文案或文件路径当权限来源。
    # 函数用途: 返回可保存到现有任务和授权记录的正文引用。
    def to_ref(self) -> dict[str, str]:
        return {"stable_id": self.stable_id, "name": self.name,
                "source": self.source, "content_sha256": self.content_sha256}

    def to_card(self) -> SkillCard:
        return SkillCard(
            name=self.name,
            description=self.description,
            path=Path(self.path),
            when_to_use=self.when_to_use,
            category=self.category,
            platforms=list(self.platforms),
            scope=self.source,
            tags=list(self.tags),
            capabilities=list(self.capabilities),
            tools_required=list(self.tools_required),
            risk_level=self.risk_level,
            source=self.source,
        )

    def to_dict(self, *, include_path: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "stable_id": self.stable_id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "category": self.category,
            "enabled": self.enabled,
            "content_sha256": self.content_sha256,
        }
        if include_path:
            payload["path"] = self.path
        return payload


# LLM: entries 只包含原公开 Skill，packages 只包含包级摘要与私有声明；授权消费者使用 resolve_reference 而非展平成员。
# 类用途: 保存当前轮统一能力快照，公开发现、包内读取和子代理限制都沿同一身份集合。
@dataclass(frozen=True)
class SkillSnapshot:
    entries: tuple[SkillSnapshotEntry, ...]
    errors: tuple[SkillLoadError, ...]
    fingerprint: str
    owner_id: str
    workspace_root: str
    packages: tuple[CapabilityPackageSnapshot, ...] = ()

    def enabled_entries(self) -> tuple[SkillSnapshotEntry, ...]:
        return tuple(entry for entry in self.entries if entry.enabled)

    def resolve(self, reference: str, *, enabled_only: bool = True) -> SkillSnapshotEntry | None:
        value = str(reference or "").strip()
        if not value:
            return None
        candidates = self.enabled_entries() if enabled_only else self.entries
        for entry in candidates:
            if value in {entry.stable_id, entry.name}:
                return entry
        return None

    # LLM: 只有包本身成为公开引用，成员永不加入本集合；原 enabled_entries 及 Skill 计数保持不变。
    # 函数用途: 为授权和决策消费者列出可见的公开 Skill 与能力包摘要。
    def reference_entries(self) -> tuple[SkillSnapshotEntry | CapabilityPackageSnapshot, ...]:
        return (*self.enabled_entries(), *self.packages)

    # LLM: capability: 是保留包命名域，先于普通 Skill 名称解析；不存在的包不能被同名公开 Skill 冒充。
    # 函数用途: 在原授权入口解析公开 Skill 或包级引用，包内文件必须另行明确作用域。
    def resolve_reference(self, reference: str, *, enabled_only: bool = True):
        value = str(reference or "").strip()
        if value.startswith("capability:"):
            return next((package for package in self.packages if package.stable_id == value), None)
        return self.resolve(value, enabled_only=enabled_only)

    # LLM: package_id 必须明确指定并在当前授权快照内；不从文件名或模型文本推断包归属。
    # 函数用途: 获取当前范围内的指定能力包。
    def resolve_package(self, package_id: str) -> CapabilityPackageSnapshot | None:
        return next((package for package in self.packages if package.package_id == package_id), None)

    # LLM: 只按包 ID 与声明相对路径组合解析；不会回退到全局 Skill 或宿主文件系统。
    # 函数用途: 查找一个明确能力包内的私有文件声明。
    def resolve_in_package(self, package_id: str, member_path: str = ""):
        package = self.resolve_package(package_id)
        return package.resolve(member_path) if package is not None else None

    # LLM: 读取经过原 owner/代次校验；InterruptedError 是停止信号，不能因继承 OSError 被转换成可恢复读失败。
    # 函数用途: 读取受摘要保护的私有成员；路径、完整性和撤销失败转成快照错误，中断继续传播。
    def read_in_package(self, package_id: str, member_path: str = "") -> bytes:
        package = self.resolve_package(package_id)
        if package is None:
            raise SkillSnapshotError("CAPABILITY_PACKAGE_NOT_AVAILABLE", f"package={package_id}")
        try:
            return package.read(member_path)
        except InterruptedError:
            raise
        except (OSError, ValueError) as exc:
            code = str(getattr(exc, "code", "") or "CAPABILITY_RESOURCE_UNAVAILABLE")
            reason = str(getattr(exc, "reason", "") or "")
            raise SkillSnapshotError(code, f"package={package_id}", reason=reason) from exc

    # LLM: 仅主任务合法旧 pin 的可用性投影使用此入口；剔除同 ID 的当前包但不写 pin，也不影响公开 Skill 或其它包。
    # 函数用途: 生成带结构化失效诊断的新快照和指纹，让正常对话继续而旧包不能静默换代。
    def without_unavailable_packages(self, unavailable: Mapping[str, str]) -> SkillSnapshot:
        if not unavailable:
            return self
        if any(not reference.startswith("capability:") or code not in PACKAGE_PIN_ERROR_MESSAGES
               for reference, code in unavailable.items()):
            raise ValueError("SKILL_PACKAGE_AVAILABILITY_INVALID")
        diagnostics = tuple(SkillLoadError(reference, code, PACKAGE_PIN_ERROR_MESSAGES[code], "capability_package")
                            for reference, code in sorted(unavailable.items()))
        packages = tuple(package for package in self.packages if package.stable_id not in unavailable)
        errors = tuple(error for error in self.errors
                       if not (error.source == "capability_package" and error.path in unavailable
                               and error.code in PACKAGE_PIN_ERROR_MESSAGES)) + diagnostics
        if packages == self.packages and errors == self.errors:
            return self
        material = {"packages": [package.to_ref() for package in packages],
                    "unavailable": [error.to_dict() for error in diagnostics]}
        fingerprint = _content_sha256(self.fingerprint + "\0" + json.dumps(material, sort_keys=True, ensure_ascii=False))
        return replace(self, packages=packages, errors=errors, fingerprint=fingerprint)

    def read_body(self, reference: str, *, max_chars: int = 0) -> str:
        entry = self.resolve(reference)
        if entry is None:
            raise SkillSnapshotError("SKILL_NOT_AVAILABLE", f"reference={reference}")
        path = Path(entry.path)
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillSnapshotError("SKILL_READ_FAILED", f"skill={entry.stable_id}: {exc}") from exc
        if _content_sha256(body) != entry.content_sha256:
            raise SkillSnapshotError("SKILL_SNAPSHOT_STALE", f"skill={entry.stable_id}")
        decision = evaluate_skill_guard_gate(
            path.parent,
            SkillGuardRequest(source=entry.source, skill_name=entry.name),
        )
        if not decision.allowed:
            raise SkillSnapshotError("SKILL_GUARD_DENIED", f"skill={entry.stable_id}")
        if max_chars and len(body) > max_chars:
            return body[:max_chars] + "\n... 已截断"
        return body

    # LLM: 子集沿同一 allowed_skills/ref 账裁剪两个公开命名域；显式 expected_refs 时每个包都须有精确激活引用。
    # 函数用途: 为委派和续跑生成不扩权快照，拒绝正文换版及同包重装后旧引用复活。
    def restricted(
        self,
        references: Iterable[str],
        *,
        expected_sha256: Mapping[str, str] | None = None,
        expected_refs: Iterable[Mapping[str, object]] | None = None,
    ) -> SkillSnapshot:
        """Return a fail-closed subset for a delegated runner."""

        expected = dict(expected_sha256 or {})
        selected: list[SkillSnapshotEntry] = []
        packages: list[CapabilityPackageSnapshot] = []
        refs = _index_expected_refs(expected_refs)
        seen: set[str] = set()
        for raw in references:
            reference = str(raw or "").strip()
            if not reference:
                continue
            entry = self.resolve_reference(reference)
            if entry is None:
                raise SkillSnapshotError("SKILL_NOT_AVAILABLE", f"reference={reference}")
            wanted_sha = expected.get(entry.stable_id) or expected.get(reference)
            if wanted_sha and wanted_sha != entry.content_sha256:
                raise SkillSnapshotError("SKILL_SNAPSHOT_STALE", f"skill={entry.stable_id}")
            if isinstance(entry, CapabilityPackageSnapshot):
                _validate_package_reference(entry, refs)
            if entry.stable_id not in seen:
                (packages if isinstance(entry, CapabilityPackageSnapshot) else selected).append(entry)
                seen.add(entry.stable_id)
        fingerprint = _content_sha256(
            self.fingerprint + "\0" + "\0".join(entry.stable_id for entry in (*selected, *packages))
        )
        return SkillSnapshot(
            entries=tuple(selected),
            errors=self.errors,
            fingerprint=fingerprint,
            owner_id=self.owner_id,
            workspace_root=self.workspace_root,
            packages=tuple(packages),
        )


# LLM: 普通 Skill 保留原引用集合语义；同包的矛盾身份不能按输入先后覆盖，卡片展示字段不参与授权身份。
# 函数用途: 给子集校验索引已有引用，拒绝混入的另一包版本或代次。
def _index_expected_refs(values: Iterable[Mapping[str, object]] | None) -> dict[str, Mapping] | None:
    if values is None:
        return None
    refs: dict[str, Mapping] = {}
    keys = ("kind", "package_id", "activation_id", "content_sha256")
    for row in values:
        if not isinstance(row, Mapping):
            continue
        stable_id = str(row.get("stable_id") or "")
        previous = refs.get(stable_id)
        if stable_id.startswith("capability:") and previous is not None:
            if any(previous.get(key) != row.get(key) for key in keys):
                raise SkillSnapshotError("SKILL_PACKAGE_REFERENCE_CONFLICT", f"reference={stable_id}")
        refs[stable_id] = row
    return refs


# LLM: None 仅供宿主首次筛选；任务/子代理传来的引用须同时锁住包摘要和 activation_id，不以同名或同内容替代。
# 函数用途: 检查已保存的包授权仍对应同一安装代次。
def _validate_package_reference(package: CapabilityPackageSnapshot, refs: Mapping | None) -> None:
    if refs is None:
        return
    row = refs.get(package.stable_id)
    if not row or any(not row.get(key) for key in ("package_id", "activation_id", "content_sha256")):
        raise SkillSnapshotError("SKILL_PACKAGE_REFERENCE_REQUIRED", f"package={package.package_id}")
    if any(row.get(key) != value for key, value in (
        ("kind", "capability_package"), ("package_id", package.package_id),
        ("activation_id", package.activation_id), ("content_sha256", package.package_sha256),
    )):
        raise SkillSnapshotError("SKILL_SNAPSHOT_STALE", f"package={package.package_id}")


def skill_content_sha256(text: str) -> str:
    return _content_sha256(text)


def _content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "SKILL_SNAPSHOT_ERROR_CODE_INVALID",
    "SkillLoadError",
    "SkillSnapshot",
    "SkillSnapshotEntry",
    "SkillSnapshotError",
    "skill_content_sha256",
]
