# LLM: 自学习 S1 的唯一 Skill 提案权威。只接受带精确 task/run 来源的子代理 lesson Candidate，按固定模板
#   （不调用模型）渲染草稿，存为 <owner_home>/data/skill_proposals/<proposal_id>.json；正式 SKILL.md 只能由
#   用户经 CLI confirm 后，在同一 owner 目录锁内复核提案版本、来源 hash、目标不存在、frontmatter 解析与
#   agent_generated guard（从不 force）再原子安装。目录名不得改成 learning_drafts：Curator 每次持 lease 前的
#   Memory 迁移会递归迁走并删除该名字的目录。不要为本模块注册任何模型可调用的确认工具。
# 模块用途: 生成、列出、查看、确认或拒绝自学习 Skill 提案；任何确认失败都不写目标 Skill 并保持提案待确认。
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_json_object_report,
    write_json_file_atomic_unlocked,
)
from ..contracts.gates.skill_guard import SkillScanResult, install_decision, scan_skill
from ..memory_store.candidate_models import MemoryCandidate
from ..memory_store.candidates import CandidateService, CandidateStoreCorruptError
from .skill_snapshot import skill_content_sha256
from .skills import parse_skill_file

SKILL_PROPOSAL_SCHEMA_VERSION = "my-agent.skill-proposal.v1"
SKILL_PROPOSAL_TRIGGER = "subagent_lesson_candidate"
PROPOSAL_PENDING = "pending_confirmation"
PROPOSAL_COMMITTED = "committed"
PROPOSAL_REJECTED = "rejected"
PROPOSAL_STATUSES = frozenset({PROPOSAL_PENDING, PROPOSAL_COMMITTED, PROPOSAL_REJECTED})

# 结构化结果码：调用方和 CLI 只按这些码判断，不解析 message。
CODE_COMMITTED = "SKILL_PROPOSAL_COMMITTED"
CODE_REJECTED = "SKILL_PROPOSAL_REJECTED"
CODE_INVALID_ID = "SKILL_PROPOSAL_INVALID_ID"
CODE_NOT_FOUND = "SKILL_PROPOSAL_NOT_FOUND"
CODE_CORRUPT = "SKILL_PROPOSAL_CORRUPT"
CODE_NOT_PENDING = "SKILL_PROPOSAL_NOT_PENDING"
CODE_REVISION_MISMATCH = "SKILL_PROPOSAL_REVISION_MISMATCH"
CODE_DRAFT_MISMATCH = "SKILL_PROPOSAL_DRAFT_MISMATCH"
CODE_DRAFT_INVALID = "SKILL_PROPOSAL_DRAFT_INVALID"
CODE_SOURCE_MISSING = "SKILL_PROPOSAL_SOURCE_MISSING"
CODE_SOURCE_UNREADABLE = "SKILL_PROPOSAL_SOURCE_UNREADABLE"
CODE_SOURCE_REDACTED = "SKILL_PROPOSAL_SOURCE_REDACTED"
CODE_SOURCE_CHANGED = "SKILL_PROPOSAL_SOURCE_CHANGED"
CODE_SOURCE_INACTIVE = "SKILL_PROPOSAL_SOURCE_INACTIVE"
CODE_TARGET_EXISTS = "SKILL_PROPOSAL_TARGET_EXISTS"
CODE_GUARD_BLOCKED = "SKILL_PROPOSAL_GUARD_BLOCKED"
CODE_INSTALL_FAILED = "SKILL_PROPOSAL_INSTALL_FAILED"
CODE_RECORD_WRITE_FAILED = "SKILL_PROPOSAL_RECORD_WRITE_FAILED"

# 来源 Candidate 仍能支撑提案的状态：rejected/superseded/expired 是审核或保留期的否定结论，blocked_* 表示
# 证据或冲突未解决；promoted 只说明它已成为正式 lesson，用户仍可选择另装为 Skill。
_ACTIVE_SOURCE_STATUSES = frozenset({"observed", "pending_review", "approved", "promoted"})
_PROPOSAL_ID_RE = re.compile(r"[0-9a-f]{24}")
_SKILL_NAME_RE = re.compile(r"lesson-[0-9a-f]{12}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_LOCK_NAME = ".proposals"
_STAGING_PREFIX = ".staging-"
_DESCRIPTION_PREFIX = "子代理经验："
_DESCRIPTION_EXCERPT_CHARS = 160
_SCENARIO_EXCERPT_CHARS = 240
_BODY_TEMPLATE = """# 子代理经验 {skill_name}

本 Skill 由宿主按固定模板从一条子代理经验候选生成，经用户确认后安装；宿主没有调用模型改写内容。

## 经验

{lesson}

## 适用场景

{scenario}

## 使用方式

把上面的经验当作参考做法。应用前先核对当前任务的实际文件、工具结果和用户要求；与当前事实冲突时以当前事实为准。

## 来源

- 触发：{trigger}
- 记忆候选：{candidate_id}
- 来源任务：{task_ids}
- 来源运行：{run_ids}
"""


# LLM: code 是唯一机器判断字段；message 只供人读，detail 只放 ID、状态、版本、guard 结论等结构化事实。
# 类用途: 表示一次被拒绝的提案读取、确认或拒绝；确认链内抛出后由服务转成结构化结果。
class SkillProposalError(Exception):
    # LLM: 构造时复制 detail，调用方之后修改原字典不能改写已经抛出的拒绝事实。
    # 函数用途: 保存结果码、人读说明与结构化细节。
    def __init__(self, code: str, message: str = "", *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.detail = dict(detail or {})


# LLM: 来源只保存 Candidate 精确身份、正文 hash 与 task/run ID；正文本身不复制进来源字段。
# 类用途: 说明提案来自哪条子代理经验候选、哪个任务和哪次运行。
@dataclass(frozen=True)
class SkillProposalSource:
    candidate_id: str
    content_hash: str
    task_ids: tuple[str, ...]
    run_ids: tuple[str, ...]


# LLM: before 固定为 absent：S1 只新增 owner Skill，从不覆盖、合并或修改任何已有 Skill。
# 类用途: 说明确认后要安装的 owner Skill 名称和安装前必须满足的状态。
@dataclass(frozen=True)
class SkillProposalTarget:
    skill_name: str
    before: str = "absent"


# LLM: sha256 覆盖 render_skill_markdown 的完整文本，也等于安装后 Skill 快照条目的 content_sha256。
# 类用途: 保存宿主按固定模板生成的 Skill 草稿（描述、适用场景、正文与校验值）。
@dataclass(frozen=True)
class SkillProposalDraft:
    description: str
    when_to_use: str
    body: str
    sha256: str


# LLM: 一条提案对应一个文件；revision 只在确认或拒绝时加一，status 只在待确认/已提交/已拒绝三态间单向变化。
#   receipt 是确认或拒绝回执，只保存时间、安装路径、hash 与 guard 结论等结构化事实。
# 类用途: 表示一条等待用户确认的自学习 Skill 提案及其当前状态。
@dataclass(frozen=True)
class SkillProposal:
    proposal_id: str
    revision: int
    status: str
    source: SkillProposalSource
    target: SkillProposalTarget
    draft: SkillProposalDraft
    created_at: str
    updated_at: str
    trigger: str = SKILL_PROPOSAL_TRIGGER
    receipt: dict[str, object] = field(default_factory=dict)
    schema_version: str = SKILL_PROPOSAL_SCHEMA_VERSION

    # LLM: 输出字段名是持久化协议；来源/目标/草稿保持嵌套对象，ID 序列写成 JSON 数组。
    # 函数用途: 把提案转换成写入 JSON 文件与 CLI 输出共用的字典。
    def to_record(self) -> dict[str, object]:
        record = asdict(self)
        record["source"]["task_ids"] = list(self.source.task_ids)
        record["source"]["run_ids"] = list(self.source.run_ids)
        return record

    # LLM: 严格解析 schema、状态、ID 绑定、来源、目标与草稿 hash；任何不合规都抛 ValueError，读取方报告为
    #   SKILL_PROPOSAL_CORRUPT，不能部分加载后继续确认。
    # 函数用途: 从提案 JSON 恢复并校验一条提案。
    @classmethod
    def from_record(cls, payload: object) -> SkillProposal:
        if not isinstance(payload, dict) or payload.get("schema_version") != SKILL_PROPOSAL_SCHEMA_VERSION:
            raise ValueError("unsupported skill proposal schema")
        proposal = cls(
            proposal_id=_required_text(payload, "proposal_id"),
            revision=_revision(payload.get("revision")),
            status=_required_text(payload, "status"),
            source=_source_from(payload.get("source")),
            target=_target_from(payload.get("target")),
            draft=_draft_from(payload.get("draft")),
            created_at=_required_text(payload, "created_at"),
            updated_at=_required_text(payload, "updated_at"),
            trigger=_required_text(payload, "trigger"),
            receipt=_mapping(payload.get("receipt"), "receipt"),
        )
        _validate_proposal(proposal)
        return proposal


# LLM: ok/code 是确认与拒绝的结构化结果；proposal 为操作后的当前记录（被拒时为 None），detail 只放结构化事实。
# 类用途: 返回一次确认或拒绝是否成功、结果码和相关细节。
@dataclass(frozen=True)
class SkillProposalOutcome:
    ok: bool
    code: str
    proposal: SkillProposal | None = None
    detail: dict[str, object] = field(default_factory=dict)


# LLM: 服务只新增 <owner_home>/data/skill_proposals 下的提案与 <owner_home>/skills/lesson-* 下的 Skill；
#   所有写操作都在同一 owner 目录锁内完成。Candidate 当前态仍由 owner candidates.jsonl 权威持有，这里只读。
#   构造不创建目录；只有真正生成第一条提案时才创建提案目录。
# 类用途: 为一个 owner 生成、列出、查看、确认或拒绝自学习 Skill 提案。
class SkillProposalService:
    # LLM: 路径全部来自 owner 规范布局；skills 根与 SkillsService 的 owner 根同一推导（owner_home/skills）。
    # 函数用途: 绑定当前 owner 的提案目录、Skill 根、候选账本和 guard 配置。
    def __init__(self, home_paths: object, *, config: object | None = None) -> None:
        self.directory = Path(home_paths.owner_skill_proposals_dir)
        self.skills_root = Path(home_paths.owner_home_dir) / "skills"
        self.candidates_path = Path(home_paths.owner_memory_candidates_jsonl)
        self.config = config

    # LLM: 只处理 lesson_candidate_eligible 通过的候选；同一 candidate+hash 的提案用 O_EXCL 创建，已存在（含已拒绝）
    #   即跳过，所以重放、重复 run 或用户拒绝后都不会重复提案。副作用：首次创建提案目录与提案文件。
    # 函数用途: 把本批子代理 lesson 候选转成新的待确认提案，返回本次新建的提案。
    def propose_from_candidates(self, candidates: Iterable[object]) -> list[SkillProposal]:
        eligible = [item for item in candidates if lesson_candidate_eligible(item)]
        if not eligible:
            return []
        now = _utc_now()
        with self._locked():
            created = [self._create_if_absent(build_lesson_proposal(item, now=now)) for item in eligible]
        return [item for item in created if item is not None]

    # LLM: 目录不存在时直接返回空列表且不创建目录；任何损坏文件都抛 SKILL_PROPOSAL_CORRUPT，不跳过坏记录。
    # 函数用途: 按创建时间列出当前 owner 的提案，可按状态过滤。
    def list(self, status: str | None = None) -> list[SkillProposal]:
        if not self.directory.is_dir():
            return []
        with self._locked():
            records = [_read_proposal(path) for path in _proposal_files(self.directory)]
        selected = [item for item in records if status is None or item.status == status]
        return sorted(selected, key=lambda item: (item.created_at, item.proposal_id))

    # LLM: 先校验 ID 形状再拼路径，防止路径穿越；只读，不创建目录。
    # 函数用途: 读取一条提案的完整内容供用户确认前查看。
    def show(self, proposal_id: str) -> SkillProposal:
        path = self._existing_path(proposal_id)
        with self._locked():
            return _read_proposal(path)

    # LLM: 只供用户显式确认入口调用；必须带用户看到的 revision。成功时安装 Skill 并把提案标为 committed（revision+1）。
    # 函数用途: 用户确认一条提案，复核全部前提后安装为正式 owner Skill。
    def confirm(self, proposal_id: str, expected_revision: int) -> SkillProposalOutcome:
        return self._resolve(proposal_id, expected_revision, self._commit_locked)

    # LLM: 拒绝只改提案状态（revision+1），从不读写 Skill 目录或 Candidate 账本。
    # 函数用途: 用户拒绝一条提案，之后同一候选内容不会再生成新提案。
    def reject(self, proposal_id: str, expected_revision: int) -> SkillProposalOutcome:
        return self._resolve(proposal_id, expected_revision, _reject_locked)

    # LLM: 确认/拒绝共用的锁内骨架：重读提案、要求 pending 与 revision 相等后才执行动作；任何拒绝都转成结构化结果。
    # 函数用途: 在 owner 锁内执行一次确认或拒绝，并把失败统一包装为结果码。
    def _resolve(
        self,
        proposal_id: str,
        expected_revision: int,
        action: Callable[[Path, SkillProposal], SkillProposalOutcome],
    ) -> SkillProposalOutcome:
        try:
            path = self._existing_path(proposal_id)
            with self._locked():
                current = _read_proposal(path)
                _require_pending(current, expected_revision)
                return action(path, current)
        except SkillProposalError as exc:
            return SkillProposalOutcome(False, exc.code, detail=exc.detail)

    # LLM: 顺序固定：草稿 hash→来源 Candidate→目标不存在→临时目录写入并解析→guard→os.replace 安装→写 committed；
    #   写 committed 失败时删除刚安装的目标，保证失败不留下正式 Skill。副作用：写 skills/<name>/ 与提案文件。
    # 函数用途: 在锁内复核并安装一条待确认提案。
    def _commit_locked(self, path: Path, proposal: SkillProposal) -> SkillProposalOutcome:
        _verify_draft(proposal)
        _verify_source(CandidateService(self.candidates_path), proposal.source)
        target = self.skills_root / proposal.target.skill_name
        _require_absent(target)
        stage = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=self.directory))
        try:
            staged, scan = _stage_and_scan(stage, proposal, self.config)
            _install(staged, target)
            committed = _committed_proposal(proposal, target, scan)
            try:
                _write_record(path, committed)
            except SkillProposalError:
                shutil.rmtree(target, ignore_errors=True)
                raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return SkillProposalOutcome(True, CODE_COMMITTED, committed, {"skill_path": str(target / "SKILL.md")})

    # LLM: O_EXCL 保证同一提案只创建一次；写入异常时删除本次刚创建的半成品文件，不影响已存在的提案。
    # 函数用途: 提案文件不存在时创建它并返回提案，已存在时返回 None。
    def _create_if_absent(self, proposal: SkillProposal) -> SkillProposal | None:
        path = self.directory / f"{proposal.proposal_id}.json"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            return None
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_encode_record(proposal))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return proposal

    # LLM: 提案 ID 必须是 24 位小写十六进制，才能拼出提案路径；符号链接或非普通文件按不存在处理。
    # 函数用途: 把用户输入的提案 ID 转成已存在的提案文件路径。
    def _existing_path(self, proposal_id: str) -> Path:
        text = str(proposal_id or "").strip()
        if not _PROPOSAL_ID_RE.fullmatch(text):
            raise SkillProposalError(CODE_INVALID_ID, detail={"proposal_id": text[:64]})
        path = self.directory / f"{text}.json"
        if path.is_symlink() or not path.is_file():
            raise SkillProposalError(CODE_NOT_FOUND, detail={"proposal_id": text})
        return path

    # LLM: 同一 owner 的提案读写共用这把线程锁 + 文件锁；锁文件位于提案目录，首次加锁会创建该目录。
    # 函数用途: 返回当前 owner 提案目录的互斥临界区。
    def _locked(self):
        return locked_json_path(self.directory / _LOCK_NAME)


# LLM: 资格只看结构化字段：MemoryCandidate、lesson 类型、subagent_lesson 来源（排除 model_inferred 自省）、同时具备
#   task 与 run 来源、状态仍有效、正文未脱敏且 hash 形状合法；不读取正文做任何判断。
# 函数用途: 判断一条记忆候选能否生成 Skill 提案。
def lesson_candidate_eligible(candidate: object) -> bool:
    if not isinstance(candidate, MemoryCandidate):
        return False
    return (
        candidate.candidate_type == "lesson"
        and candidate.origin == "subagent_lesson"
        and bool(candidate.source_task_ids)
        and bool(candidate.source_run_ids)
        and candidate.status in _ACTIVE_SOURCE_STATUSES
        and not candidate.content_redacted_at
        and bool(str(candidate.content or "").strip())
        and bool(_SHA256_RE.fullmatch(str(candidate.content_hash or "")))
    )


# LLM: proposal_id 由 candidate_id 与正文 hash 确定（候选身份或正文变化都得到新 ID），目标名只取正文 hash 前 12 位，
#   让不同任务里的同一经验指向同一个 Skill 名，先确认的一条安装后其余会得到 TARGET_EXISTS。
# 函数用途: 从一条合格 lesson 候选构造 revision=1 的待确认提案。
def build_lesson_proposal(candidate: MemoryCandidate, *, now: str) -> SkillProposal:
    skill_name = lesson_skill_name(candidate.content_hash)
    return SkillProposal(
        proposal_id=skill_proposal_id(candidate.candidate_id, candidate.content_hash),
        revision=1,
        status=PROPOSAL_PENDING,
        source=SkillProposalSource(
            candidate_id=candidate.candidate_id,
            content_hash=candidate.content_hash,
            task_ids=tuple(candidate.source_task_ids),
            run_ids=tuple(candidate.source_run_ids),
        ),
        target=SkillProposalTarget(skill_name=skill_name),
        draft=render_lesson_draft(candidate, skill_name),
        created_at=now,
        updated_at=now,
    )


# LLM: 截断到 24 位十六进制只作文件名与 CLI 参数；身份绑定仍由 from_record 重新计算校验。
# 函数用途: 计算提案稳定编号 sha256(candidate_id + content_hash) 的前 24 位。
def skill_proposal_id(candidate_id: str, content_hash: str) -> str:
    return hashlib.sha256(f"{candidate_id}{content_hash}".encode()).hexdigest()[:24]


# LLM: 目标名只由正文 hash 决定，格式固定为 lesson-<12 位十六进制>，不会含路径分隔符。
# 函数用途: 计算提案要安装的 owner Skill 目录名。
def lesson_skill_name(content_hash: str) -> str:
    return f"lesson-{content_hash[:12]}"


# LLM: 固定模板、不调用模型；frontmatter 值压成单行并去掉解析器会截断的 # 与会被剥掉的尾部引号，保证安装后解析
#   得到的 description/when_to_use 与草稿完全一致。适用场景来自 Candidate scope.applies_when（只供人读）。
# 函数用途: 按固定模板把一条 lesson 候选渲染成 Skill 草稿并计算完整 SKILL.md 的 sha256。
def render_lesson_draft(candidate: MemoryCandidate, skill_name: str) -> SkillProposalDraft:
    lesson = _normalized_newlines(candidate.content).strip()
    scenario = _one_line(candidate.scope.get("applies_when", ""))
    unsigned = SkillProposalDraft(
        description=_frontmatter_value(_DESCRIPTION_PREFIX + _excerpt(_one_line(lesson), _DESCRIPTION_EXCERPT_CHARS)),
        when_to_use=_frontmatter_value(_when_to_use_text(scenario)),
        body=_BODY_TEMPLATE.format(
            skill_name=skill_name,
            lesson=lesson,
            scenario=scenario or "与来源子代理任务相似的工作。",
            trigger=SKILL_PROPOSAL_TRIGGER,
            candidate_id=candidate.candidate_id,
            task_ids="、".join(candidate.source_task_ids),
            run_ids="、".join(candidate.source_run_ids),
        ),
        sha256="",
    )
    return replace(unsigned, sha256=skill_content_sha256(render_skill_markdown(skill_name, unsigned)))


# LLM: 确认安装与草稿 hash 共用这一个渲染函数；只使用 name/description/when_to_use 三个 frontmatter 字段。
# 函数用途: 把草稿渲染成要写入的完整 SKILL.md 文本。
def render_skill_markdown(skill_name: str, draft: SkillProposalDraft) -> str:
    return (
        f"---\nname: {skill_name}\ndescription: {draft.description}\n"
        f"when_to_use: {draft.when_to_use}\n---\n\n{draft.body.rstrip()}\n"
    )


# LLM: 适用场景只作人读说明；没有来源任务目标时使用固定文案，不从正文推断场景。
# 函数用途: 生成 frontmatter 的 when_to_use 文案。
def _when_to_use_text(scenario: str) -> str:
    if not scenario:
        return "处理与来源子代理任务相似的工作时参考。"
    return "处理与来源任务相似的工作时参考；来源任务目标：" + _excerpt(scenario, _SCENARIO_EXCERPT_CHARS)


# LLM: 与 skills._parse_meta 的极简 YAML 子集对齐：# 之后会被当作注释截掉，值两端引号会被剥掉。
# 函数用途: 把任意文本转成可无损写入单行 frontmatter 的值。
def _frontmatter_value(text: str) -> str:
    return _one_line(text).replace("#", "＃").rstrip("\"' ")


# LLM: 非打印字符和各种空白一律压成单个空格，避免换行或分隔符破坏 frontmatter 行结构。
# 函数用途: 把文本压成单行。
def _one_line(value: object) -> str:
    printable = "".join(char if char.isprintable() else " " for char in str(value or ""))
    return " ".join(printable.split())


# LLM: 摘录只用于短描述，完整经验仍原样保留在正文里。
# 函数用途: 超长文本截断并加省略号。
def _excerpt(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# LLM: 统一成 LF，保证写入文本与 SkillsService 读回（通用换行）后的 hash 一致。
# 函数用途: 把 CRLF/CR 换行统一为 LF。
def _normalized_newlines(value: object) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


# LLM: 用户确认的是看过的草稿；文件内容被改动但 hash 未同步时拒绝，防止安装未经确认的内容。
# 函数用途: 校验草稿重新渲染后的 sha256 与记录一致。
def _verify_draft(proposal: SkillProposal) -> None:
    actual = skill_content_sha256(render_skill_markdown(proposal.target.skill_name, proposal.draft))
    if actual != proposal.draft.sha256:
        raise SkillProposalError(CODE_DRAFT_MISMATCH, detail={"expected": proposal.draft.sha256, "actual": actual})


# LLM: 来源必须仍存在、未脱敏、正文 hash 未变且状态有效；账本损坏时 fail closed，不猜测替代来源。
# 函数用途: 确认前重读来源 Candidate 并核对它仍能支撑这条提案。
def _verify_source(candidates: CandidateService, source: SkillProposalSource) -> None:
    detail: dict[str, object] = {"candidate_id": source.candidate_id}
    try:
        candidate = candidates.get(source.candidate_id)
    except KeyError as exc:
        raise SkillProposalError(CODE_SOURCE_MISSING, detail=detail) from exc
    except (CandidateStoreCorruptError, OSError) as exc:
        raise SkillProposalError(CODE_SOURCE_UNREADABLE, str(exc), detail=detail) from exc
    detail["candidate_status"] = candidate.status
    if candidate.content_redacted_at or not str(candidate.content or "").strip():
        raise SkillProposalError(CODE_SOURCE_REDACTED, detail=detail)
    if candidate.content_hash != source.content_hash:
        raise SkillProposalError(CODE_SOURCE_CHANGED, detail=detail)
    if candidate.status not in _ACTIVE_SOURCE_STATUSES:
        raise SkillProposalError(CODE_SOURCE_INACTIVE, detail=detail)


# LLM: 目标（含悬空符号链接）只要存在就拒绝；S1 从不覆盖已有 Skill。
# 函数用途: 确认目标 Skill 目录尚不存在。
def _require_absent(target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise SkillProposalError(CODE_TARGET_EXISTS, detail={"skill_path": str(target)})


# LLM: 在提案目录的临时目录里写 SKILL.md，先按 require_frontmatter 解析并核对名称，再以 agent_generated 来源扫描；
#   install_decision 从不 force，caution/dangerous 都被拒绝。返回暂存 Skill 目录与扫描结论。
# 函数用途: 暂存草稿并通过解析和安全扫描两道检查。
def _stage_and_scan(stage: Path, proposal: SkillProposal, config: object | None) -> tuple[Path, SkillScanResult]:
    name = proposal.target.skill_name
    skill_dir = stage / name
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_bytes(render_skill_markdown(name, proposal.draft).encode("utf-8"))
    try:
        card = parse_skill_file(skill_md, source="owner", require_frontmatter=True)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SkillProposalError(CODE_DRAFT_INVALID, str(exc)) from exc
    if card.name != name:
        raise SkillProposalError(CODE_DRAFT_INVALID, detail={"parsed_name": card.name})
    scan = scan_skill(skill_dir, source="agent_generated", skill_name=name, config=config)
    allowed, reason = install_decision(scan, force=False)
    if not allowed:
        raise SkillProposalError(CODE_GUARD_BLOCKED, reason, detail=_scan_detail(scan))
    return skill_dir, scan


# LLM: guard 细节只输出结论与 pattern ID，不复制命中的原文行。
# 函数用途: 把扫描结果压成结构化细节。
def _scan_detail(scan: SkillScanResult) -> dict[str, object]:
    return {
        "verdict": scan.verdict,
        "trust_level": scan.trust_level,
        "findings": [finding.pattern_id for finding in scan.findings],
    }


# LLM: os.replace 整目录移动；替换前在锁内再查一次目标不存在。任何 OS 错误都报 INSTALL_FAILED，目标保持原样。
# 函数用途: 把暂存 Skill 目录原子移动到 owner skills 目录。
def _install(staged: Path, target: Path) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _require_absent(target)
        os.replace(staged, target)
    except OSError as exc:
        raise SkillProposalError(CODE_INSTALL_FAILED, f"{type(exc).__name__}: {exc}") from exc


# LLM: 回执记录安装路径、内容 hash 与 guard 结论；revision 加一让旧版本号的重复确认被拒。
# 函数用途: 生成已提交状态的新提案记录。
def _committed_proposal(proposal: SkillProposal, target: Path, scan: SkillScanResult) -> SkillProposal:
    now = _utc_now()
    receipt = {
        "committed_at": now,
        "skill_path": str(target / "SKILL.md"),
        "skill_sha256": proposal.draft.sha256,
        "guard_verdict": scan.verdict,
        "guard_trust_level": scan.trust_level,
    }
    return replace(proposal, revision=proposal.revision + 1, status=PROPOSAL_COMMITTED, updated_at=now, receipt=receipt)


# LLM: 拒绝与确认共用 pending/revision 前置检查；这里只写提案文件，不碰 Skill 与 Candidate。
# 函数用途: 在锁内把提案标为已拒绝。
def _reject_locked(path: Path, proposal: SkillProposal) -> SkillProposalOutcome:
    now = _utc_now()
    rejected = replace(
        proposal,
        revision=proposal.revision + 1,
        status=PROPOSAL_REJECTED,
        updated_at=now,
        receipt={"rejected_at": now},
    )
    _write_record(path, rejected)
    return SkillProposalOutcome(True, CODE_REJECTED, rejected)


# LLM: 只有待确认且版本与用户看到的一致才允许继续；bool 不能冒充版本号。
# 函数用途: 校验提案仍待确认且 revision 匹配。
def _require_pending(proposal: SkillProposal, expected_revision: int) -> None:
    detail: dict[str, object] = {
        "proposal_id": proposal.proposal_id,
        "status": proposal.status,
        "revision": proposal.revision,
    }
    if proposal.status != PROPOSAL_PENDING:
        raise SkillProposalError(CODE_NOT_PENDING, detail=detail)
    if isinstance(expected_revision, bool) or expected_revision != proposal.revision:
        raise SkillProposalError(CODE_REVISION_MISMATCH, detail={**detail, "expected_revision": expected_revision})


# LLM: 读取失败、JSON 损坏、schema 不合规或文件名与 proposal_id 不一致都报 CORRUPT；文件消失报 NOT_FOUND。
# 函数用途: 严格读取一条提案文件。
def _read_proposal(path: Path) -> SkillProposal:
    if not path.is_file():
        raise SkillProposalError(CODE_NOT_FOUND, detail={"proposal_id": path.stem})
    report = read_json_object_report(path, context="skill_proposals.read")
    try:
        if report.load_error is not None:
            raise ValueError("unreadable skill proposal JSON")
        proposal = SkillProposal.from_record(report.payload)
    except (TypeError, ValueError) as exc:
        raise SkillProposalError(CODE_CORRUPT, str(exc), detail={"path": str(path)}) from exc
    if proposal.proposal_id != path.stem:
        raise SkillProposalError(CODE_CORRUPT, "proposal_id does not match file name", detail={"path": str(path)})
    return proposal


# LLM: 调用方已持 owner 锁；temp+replace 原子替换，写失败统一报 RECORD_WRITE_FAILED。
# 函数用途: 原子覆盖一条提案文件。
def _write_record(path: Path, proposal: SkillProposal) -> None:
    try:
        write_json_file_atomic_unlocked(path, proposal.to_record())
    except (OSError, TypeError, ValueError) as exc:
        raise SkillProposalError(CODE_RECORD_WRITE_FAILED, f"{type(exc).__name__}: {exc}") from exc


# LLM: 与 write_json_file_atomic_unlocked 同一格式（indent=2、sort_keys、非 ASCII 原样），保证创建与更新文件格式一致。
# 函数用途: 把提案编码成 UTF-8 JSON 字节。
def _encode_record(proposal: SkillProposal) -> bytes:
    return (json.dumps(proposal.to_record(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


# LLM: 只列出 <24 位 ID>.json；锁文件、原子写临时文件和 .staging-* 暂存目录都以点开头，一律排除。
# 函数用途: 返回提案目录里的提案文件路径。
def _proposal_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.json") if not path.name.startswith("."))


# LLM: 规范化失败直接报错，不回退为宽松解析。
# 函数用途: 读取一个必填且非空的字符串字段。
def _required_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"skill proposal field {key} must be a non-empty string")
    return value


# LLM: revision 必须是正整数，bool 不能冒充。
# 函数用途: 读取提案版本号。
def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("skill proposal revision must be a positive integer")
    return value


# LLM: 缺省视为空对象；存在但不是对象时报错。
# 函数用途: 读取一个 JSON 对象字段。
def _mapping(value: object, key: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"skill proposal field {key} must be an object")
    return dict(value)


# LLM: ID 序列只接受非空字符串数组。
# 函数用途: 读取 task/run ID 列表。
def _string_tuple(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"skill proposal field {key} must be a list of ids")
    return tuple(value)


# LLM: 来源字段形状在这里解析，语义约束（ID 格式、来源齐全）由 _validate_proposal 统一检查。
# 函数用途: 解析提案来源对象。
def _source_from(value: object) -> SkillProposalSource:
    data = _mapping(value, "source")
    return SkillProposalSource(
        candidate_id=_required_text(data, "candidate_id"),
        content_hash=_required_text(data, "content_hash"),
        task_ids=_string_tuple(data.get("task_ids"), "source.task_ids"),
        run_ids=_string_tuple(data.get("run_ids"), "source.run_ids"),
    )


# LLM: 目标字段形状在这里解析，名称格式与 before 取值由 _validate_proposal 检查。
# 函数用途: 解析提案目标对象。
def _target_from(value: object) -> SkillProposalTarget:
    data = _mapping(value, "target")
    return SkillProposalTarget(
        skill_name=_required_text(data, "skill_name"),
        before=_required_text(data, "before"),
    )


# LLM: 草稿四个字段都必须是非空字符串；sha256 形状由 _validate_proposal 检查。
# 函数用途: 解析提案草稿对象。
def _draft_from(value: object) -> SkillProposalDraft:
    data = _mapping(value, "draft")
    return SkillProposalDraft(
        description=_required_text(data, "description"),
        when_to_use=_required_text(data, "when_to_use"),
        body=_required_text(data, "body"),
        sha256=_required_text(data, "sha256"),
    )


# LLM: 语义约束集中一处：ID 形状与来源绑定、状态枚举、触发原因、候选 ID 前缀、正文 hash、来源齐全、目标名绑定、
#   before=absent 与草稿 hash 形状；任一不满足都视为损坏。
# 函数用途: 校验已解析提案的全部结构化约束。
def _validate_proposal(proposal: SkillProposal) -> None:
    source = proposal.source
    checks = (
        (bool(_PROPOSAL_ID_RE.fullmatch(proposal.proposal_id)), "proposal_id"),
        (proposal.proposal_id == skill_proposal_id(source.candidate_id, source.content_hash), "proposal_id_binding"),
        (proposal.status in PROPOSAL_STATUSES, "status"),
        (proposal.trigger == SKILL_PROPOSAL_TRIGGER, "trigger"),
        (source.candidate_id.startswith("memory-candidate-"), "source.candidate_id"),
        (bool(_SHA256_RE.fullmatch(source.content_hash)), "source.content_hash"),
        (bool(source.task_ids) and bool(source.run_ids), "source.provenance"),
        (bool(_SKILL_NAME_RE.fullmatch(proposal.target.skill_name)), "target.skill_name"),
        (proposal.target.skill_name == lesson_skill_name(source.content_hash), "target.binding"),
        (proposal.target.before == "absent", "target.before"),
        (bool(_SHA256_RE.fullmatch(proposal.draft.sha256)), "draft.sha256"),
    )
    failed = [name for ok, name in checks if not ok]
    if failed:
        raise ValueError("invalid skill proposal field: " + ",".join(failed))


# LLM: 统一 UTC ISO 时间，只作展示与排序，不参与状态机判断。
# 函数用途: 返回当前 UTC 时间字符串。
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CODE_COMMITTED",
    "CODE_CORRUPT",
    "CODE_DRAFT_INVALID",
    "CODE_DRAFT_MISMATCH",
    "CODE_GUARD_BLOCKED",
    "CODE_INSTALL_FAILED",
    "CODE_INVALID_ID",
    "CODE_NOT_FOUND",
    "CODE_NOT_PENDING",
    "CODE_RECORD_WRITE_FAILED",
    "CODE_REJECTED",
    "CODE_REVISION_MISMATCH",
    "CODE_SOURCE_CHANGED",
    "CODE_SOURCE_INACTIVE",
    "CODE_SOURCE_MISSING",
    "CODE_SOURCE_REDACTED",
    "CODE_SOURCE_UNREADABLE",
    "CODE_TARGET_EXISTS",
    "PROPOSAL_COMMITTED",
    "PROPOSAL_PENDING",
    "PROPOSAL_REJECTED",
    "PROPOSAL_STATUSES",
    "SKILL_PROPOSAL_SCHEMA_VERSION",
    "SKILL_PROPOSAL_TRIGGER",
    "SkillProposal",
    "SkillProposalDraft",
    "SkillProposalError",
    "SkillProposalOutcome",
    "SkillProposalService",
    "SkillProposalSource",
    "SkillProposalTarget",
    "build_lesson_proposal",
    "lesson_candidate_eligible",
    "lesson_skill_name",
    "render_lesson_draft",
    "render_skill_markdown",
    "skill_proposal_id",
]
