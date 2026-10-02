# LLM: 能力包 v2 块 3 的宿主核验主流程（第 11 条“宿主证实包检查真跑过”）。三个入口都由宿主在固定缝隙调用，模型没有对应工具：
#   - capture_pack_baseline：本 run 第一次改工作区的工具执行前，对工作区做一次有界快照（输入解析和“本回合改了什么”都靠它）；
#   - verify_written_files：写工具成功后，对写出的、符合钉住包交付物声明的文件跑声明的检查程序，返回有界摘要附进写工具回执；
#   - pack_verification_closeout_block：模型不再调工具准备收尾时，先查输入原件有没有被就地改（块 4，钉住包声明了 preserve_originals
#     时），再对本回合新建或改过的全部匹配交付物各跑一次（shell 写的也算）；两类问题各自最多返工一次，同时出现就合成一条提示。
#   块 4 起基线同时记任务的输入原件清单，task_input 只从原件清单找（原件被改过就交副本）。
#   块 5 起收尾还查必需交付物有没有交（缺或打不开最多返工 2 次）；三段顺序：输入原件 → 交付物存在 → 交付物检查。
#   只看结构化事实：开关、pins、安装项、路径模式和字段匹配、文件摘要、检查程序的 pack_verifier_result.v1；不读模型文字。
#   同样的内容（包、检查程序、目标和各输入的摘要都相同）只跑一次，结果和返工次数都记在本 run 的核验账本里。
#   副作用：读工作区文件、在沙箱里运行包内检查程序、写核验账本和 runtime_events。改动同步 test_pack_verification_service.py。
# 模块用途: 在写工具之后和回合收尾时，用钉住的原版检查程序核验交付物，并决定是否给模型一次返工提示。

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from ..capability_verification_manifest import VerifierDeclaration
from .pack_verification_deliverables import (
    MAX_DELIVERABLE_REWORK_COUNT,
    deliverable_rework_text,
    missing_deliverables,
)
from .pack_verification_inputs import (
    MAX_INPUT_REWORK_COUNT,
    capture_originals_for_run,
    input_rework_text,
    modified_originals,
    preserving_packages,
    task_input_candidates,
)
from .pack_verification_ledger import PackVerificationLedger, ledger_for_run, pack_verification_root
from .pack_verification_matching import (
    WorkspaceScan,
    file_matches,
    file_state,
    scan_workspace,
    workspace_relpath,
)
from .pack_verification_originals import OriginalFile, load_task_originals
from .pack_verification_scope import (
    PinnedVerificationPackage,
    enabled_verification_patterns,
    host_verification_enabled,
    pinned_verification_packages,
    verification_declarations,
    verification_owner,
)
from .pack_verifier_runner import PackVerificationResult, PackVerifierRequest, run_pack_verifier

TRIGGER_POST_WRITE = "post_write"
TRIGGER_CLOSEOUT = "closeout"
INPUT_SOURCE_TASK_INPUT = "task_input"
INPUT_SOURCE_TURN_OUTPUT = "turn_output"
PACK_VERIFICATION_EVENT = "pack_verification_completed"
# 一次写工具调用最多核验的目标文件数（apply_patch 可能一次改很多文件）。
MAX_POST_WRITE_TARGETS_COUNT = 4
# 收尾检查最多核验的目标文件数；超出的记 truncated，不静默当成全查了。
MAX_CLOSEOUT_TARGETS_COUNT = 8
# 收尾检查有错误时最多给几次返工提示（3a 审定：检查程序报错返工 1 次，只有警告不返工）。
MAX_PACK_VERIFICATION_REWORK_COUNT = 1
# 返工提示里最多列几个出错的交付物。
MAX_REWORK_TARGETS_COUNT = 5
# 返工提示里每个交付物最多列几条错误样例（code + 位置）。
MAX_REWORK_SAMPLES_COUNT = 5


# LLM: 一次核验所需的全部宿主事实；current 是按钉住包的模式现扫的工作区（同一次调用内只扫一次）。
# 类用途: 汇总本次核验的 owner、工作区根、账本、钉住包和基线。
@dataclass(frozen=True)
class _RunScope:
    owner: object
    root: Path
    ledger: PackVerificationLedger
    packages: tuple[PinnedVerificationPackage, ...]
    baseline: WorkspaceScan | None
    run_ids: tuple[str, str]
    runtime_db: object = None
    written: frozenset[str] = frozenset()
    pack_root: Path | None = None
    originals: dict[str, OriginalFile] | None = None

    # 函数用途: 按钉住包声明的全部模式扫一次当前工作区。
    @cached_property
    def current(self) -> WorkspaceScan:
        patterns: list[str] = []
        for package in self.packages:
            for declaration in verification_declarations(package.verification):
                patterns.extend(pattern for pattern in declaration.path_patterns if pattern not in patterns)
        return scan_workspace(self.root, tuple(patterns))


# LLM: 只在开关打开、有可信 owner、有本 run 账本时才扫；已有基线就不再扫（基线是本 run 第一次改动前的状态）。
#   模式取全部已启用、声明了核验的包，覆盖回合中途才被钉住的包。读写失败只少一条基线，不抛给工具循环。
# 函数用途: 在本 run 第一次改工作区之前记下工作区基线。
def capture_pack_baseline(agent: object, params: object) -> None:
    if not host_verification_enabled(agent):
        return
    owner = verification_owner(agent)
    ledger = _ledger(agent, params, owner)
    if owner is None or ledger is None or ledger.baseline() is not None:
        return
    patterns = enabled_verification_patterns(owner)
    if patterns:
        root = _workspace_root(agent, params)
        scan = scan_workspace(root, patterns)
        ledger.append({"kind": "baseline", "patterns": list(patterns), "scan": scan.to_payload()})
        capture_originals_for_run((ledger.path.parent, root), owner, scan)


# LLM: paths 来自写工具回执里的宿主字段（绝对路径），只核验工作区内、符合钉住包交付物声明的文件；返回每次检查的有界摘要。
# 函数用途: 写工具成功后马上核验写出的交付物。
def verify_written_files(agent: object, params: object, paths: list[Path]) -> list[dict]:
    scope = _run_scope(agent, params)
    if scope is None:
        return []
    written = [relpath for relpath in (workspace_relpath(path, scope.root) for path in paths) if relpath is not None]
    if written:
        # 写工具回执是“本回合写过它”的结构化证据；基线截断时靠它区分新写的文件和漏扫的老文件
        scope.ledger.append({"kind": "written", "paths": written})
    checked = [item for path in paths[:MAX_POST_WRITE_TARGETS_COUNT] for item in _verify_target(scope, path, TRIGGER_POST_WRITE)]
    return [result.summary() for _, result in checked]


# LLM: 没有基线说明本回合没改过工作区，什么都不查（块 5 的触发条件也就是它）。输入原件检查在前（它的返工提示要模型先恢复原件），
#   交付物存在其次，交付物检查最后；三段各自记账、各自计返工次数，同时需要返工时合成一条提示。
# 函数用途: 收尾时检查输入原件和本回合改过的交付物，必要时返回一次返工提示。
def pack_verification_closeout_block(agent: object, params: object) -> str:
    scope = _run_scope(agent, params)
    if scope is None or scope.baseline is None:
        return ""
    sections = (_input_section(scope), _deliverable_section(scope), _verification_section(scope))
    return "\n\n".join(section for section in sections if section)


# LLM: 被就地改的原件只按结构化摘要判定（不管哪个工具改的）；每次收尾都记一条 input_check（最终事实读最后一条），
#   返工提示先记 input_rework 再返回，记不进账本就不返工。
# 函数用途: 收尾时检查本回合有没有就地改输入原件。
def _input_section(scope: _RunScope) -> str:
    items = modified_originals(scope.originals, scope.baseline, (scope.root, scope.pack_root, preserving_packages(scope.packages)))
    scope.ledger.append({"kind": "input_check", "items": items})
    if not items or scope.ledger.count("input_rework") >= MAX_INPUT_REWORK_COUNT:
        return ""
    if not scope.ledger.append({"kind": "input_rework", "paths": [item["path"] for item in items]}):
        return ""
    return input_rework_text(items)


# LLM: 只按本回合确定新建或改过的文件判断（基线截断时的“不确定”文件不算交付证据，也不算缺）；每次收尾记一条 deliverable_check，
#   返工提示先记 deliverable_rework 再返回，记不进账本就不返工，上限 MAX_DELIVERABLE_REWORK_COUNT。
# 函数用途: 收尾时检查钉住包的必需交付物本回合有没有交。
def _deliverable_section(scope: _RunScope) -> str:
    items = missing_deliverables(scope.packages, _changed_paths(scope), scope.root)
    scope.ledger.append({"kind": "deliverable_check", "items": items})
    if not items or scope.ledger.count("deliverable_rework") >= MAX_DELIVERABLE_REWORK_COUNT:
        return ""
    if not scope.ledger.append({"kind": "deliverable_rework", "deliverables": [item["deliverable_id"] for item in items]}):
        return ""
    return deliverable_rework_text(items)


# LLM: 目标 = 本回合新建或内容变了的、符合钉住包交付物声明的文件（按基线比对，shell 写的也算）。
#   基线截断时，不在基线里、又没有写工具回执证明本回合写过的文件只算“不确定”：照样检查、入账，但它的失败不触发返工（9b 应修 2）。
#   当前快照也截断时记 current_truncated。有 failed 结果且本 run 返工次数没到上限时，先把返工记进账本再返回提示；
#   记不进账本就不返工（避免无界循环）。
# 函数用途: 收尾时核验本回合改过的全部交付物。
def _verification_section(scope: _RunScope) -> str:
    uncertain = set(_uncertain_paths(scope))
    targets = [scope.root / relpath for relpath in sorted({*_changed_paths(scope), *uncertain})
               if _applicable(scope, scope.root / relpath)]
    checked = [item for path in targets[:MAX_CLOSEOUT_TARGETS_COUNT] for item in _verify_target(scope, path, TRIGGER_CLOSEOUT)]
    scope.ledger.append({"kind": "closeout", "keys": [key for key, _ in checked],
                         "target_count": len(targets), "truncated": len(targets) > MAX_CLOSEOUT_TARGETS_COUNT,
                         "uncertain_targets": sorted(rel for rel in uncertain if scope.root / rel in targets),
                         "current_truncated": scope.current.truncated})
    failed = [result for _, result in checked if result.status == "failed" and result.target not in uncertain]
    if not failed or scope.ledger.count("rework") >= MAX_PACK_VERIFICATION_REWORK_COUNT:
        return ""
    if not scope.ledger.append({"kind": "rework", "keys": [key for key, result in checked if result in failed]}):
        return ""
    return _rework_text(failed)


# 函数用途: 组装本次核验的宿主事实；任何前提不满足（开关关、没有 owner/账本/钉住包）返回 None。
def _run_scope(agent: object, params: object) -> _RunScope | None:
    if not host_verification_enabled(agent):
        return None
    owner = verification_owner(agent)
    ledger = _ledger(agent, params, owner)
    if owner is None or ledger is None:
        return None
    packages = tuple(pinned_verification_packages(agent, getattr(params, "task_attributes", None), owner))
    if not packages:
        return None
    baseline = ledger.baseline()
    return _RunScope(owner, _workspace_root(agent, params), ledger, packages,
                     WorkspaceScan.from_payload(baseline.get("scan")) if baseline else None,
                     (str(getattr(params, "run_id", "") or ""), str(getattr(params, "attempt_id", "") or "")),
                     getattr(getattr(agent, "subagents", None), "runtime_db", None), ledger.written_paths(),
                     ledger.path.parent, load_task_originals(ledger.path.parent))


# 函数用途: 对一个目标文件跑全部适用的检查程序，返回 (复用键, 结果) 列表。
def _verify_target(scope: _RunScope, path: Path, trigger: str) -> list[tuple[str, PackVerificationResult]]:
    return [_run_once(scope, pair, path, trigger) for pair in _applicable(scope, path)]


# LLM: 目标先按包的交付物声明认（路径模式 + 字段匹配），再取 applies_to 指向这些交付物的检查程序。
# 函数用途: 列出一个文件适用的 (包, 检查程序)。
def _applicable(scope: _RunScope, path: Path) -> list[tuple[PinnedVerificationPackage, VerifierDeclaration]]:
    relpath = workspace_relpath(path, scope.root)
    if relpath is None:
        return []
    pairs = []
    for package in scope.packages:
        ids = {item.id for item in package.verification.deliverables if file_matches(item, relpath, path)}
        pairs.extend((package, verifier) for verifier in package.verification.verifiers if verifier.applies_to in ids)
    return pairs


# LLM: 复用键相同说明包、检查程序、目标和各输入的内容都没变，直接复用本 run 已有结果，不重跑包内程序。
# 函数用途: 跑一次检查程序（或复用已有结果），入账并写运行事件。
def _run_once(scope: _RunScope, pair: tuple, target: Path, trigger: str) -> tuple[str, PackVerificationResult]:
    package, verifier = pair
    inputs, matches = _resolve_inputs(scope, verifier, target)
    key = _cache_key(package, verifier, target, inputs)
    cached = scope.ledger.cached_fact(key)
    if cached is not None:
        return key, PackVerificationResult.from_fact(cached)
    result = run_pack_verifier(PackVerifierRequest(scope.owner, package.installation, verifier.id, target, scope.root, inputs))
    scope.ledger.append({"kind": "result", "trigger": trigger, "key": key, "input_matches": matches,
                         "fact": result.to_fact()})
    _append_event(scope, result, trigger)
    return key, result


# LLM: 每条声明的输入按来源找候选：task_input = 任务原件清单里的文件（原样就交工作区文件，被改过就交副本）；turn_output = 本回合
#   新建或改过的文件；都排除 target 本身，再按路径模式和字段匹配过滤，恰好一个才交给检查程序。不认识的来源没有候选。
# 函数用途: 为一个检查程序解析关联输入，返回 ((参数名, 路径)…) 和每个参数名的匹配个数。
def _resolve_inputs(scope: _RunScope, verifier: VerifierDeclaration, target: Path) -> tuple[tuple, dict[str, int]]:
    target_relpath = workspace_relpath(target, scope.root)
    resolved, matches = [], {}
    for item in verifier.inputs:
        found = [path for relpath, path in _candidates(scope, item.source)
                 if relpath != target_relpath and file_matches(item, relpath, path)]
        matches[item.flag] = len(found)
        if len(found) == 1:
            resolved.append((item.flag, found[0]))
    return tuple(resolved), matches


# 函数用途: 按来源列出候选 (工作区相对路径, 交给检查程序的路径)，排好序。
def _candidates(scope: _RunScope, source: str) -> list[tuple[str, Path]]:
    if source == INPUT_SOURCE_TASK_INPUT:
        return task_input_candidates(scope.originals, scope.pack_root, scope.root)
    if source == INPUT_SOURCE_TURN_OUTPUT and scope.baseline is not None:
        return [(relpath, scope.root / relpath) for relpath in _changed_paths(scope)]
    return []


# LLM: 和基线比内容变了的文件；不在基线里的文件，只有基线没截断、或写工具回执证明本回合写过它时才算（太大没算摘要的不算）。
# 函数用途: 列出确定是本回合新建或内容变了的文件。
def _changed_paths(scope: _RunScope) -> list[str]:
    baseline = scope.baseline
    known = baseline.files if baseline is not None else {}
    truncated = baseline is not None and baseline.truncated
    return sorted(relpath for relpath, state in scope.current.files.items()
                  if state.sha256 and known.get(relpath) != state
                  and (relpath in known or not truncated or relpath in scope.written))


# 函数用途: 基线截断时，列出不在基线里、也没有写工具回执证明的文件（可能是漏扫的老文件）。
def _uncertain_paths(scope: _RunScope) -> list[str]:
    baseline = scope.baseline
    if baseline is None or not baseline.truncated:
        return []
    return sorted(relpath for relpath, state in scope.current.files.items()
                  if state.sha256 and relpath not in baseline.files and relpath not in scope.written)


# 函数用途: 由包摘要、检查程序、目标和各输入的路径与摘要生成复用键。
def _cache_key(package: PinnedVerificationPackage, verifier: VerifierDeclaration, target: Path, inputs: tuple) -> str:
    state = file_state(target)
    parts = [package.installation.package_sha256, verifier.id, str(target), state.sha256 if state else "",
             [[flag, str(path), getattr(file_state(path), "sha256", "")] for flag, path in inputs]]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:32]


# LLM: 返工提示只列结构化事实（目标、包、检查程序、错误数、错误码和检查程序给的位置），不复述模型的说法，不给修改方案。
# 函数用途: 生成一次返工提示。
def _rework_text(failed: list[PackVerificationResult]) -> str:
    lines = ["宿主用钉住的能力包原版检查程序核验了本回合写出的交付物，下面这些仍有错误："]
    for result in failed[:MAX_REWORK_TARGETS_COUNT]:
        samples = "；".join(f"{item['code']} @ {item['location'] or '-'}" for item in result.error_samples[:MAX_REWORK_SAMPLES_COUNT])
        lines.append(f"- {result.target}（{result.package_id} {result.package_version} · {result.verifier_id}）："
                     f"错误 {sum(result.error_counts.values())} 条，例如 {samples or '、'.join(sorted(result.error_counts))}")
    lines.append("请按这些错误码和位置修正交付物，再结束本回合。检查结论以宿主为准；不要复制、改写或自己编写检查程序来代替。")
    return "\n".join(lines)


# LLM: runtime_events 是可观测投影，账本才是本模块的权威；没有权威库或没有 run 行时跳过，写失败不打断。
# 函数用途: 把一次检查结果写进 runtime_events。
def _append_event(scope: _RunScope, result: PackVerificationResult, trigger: str) -> None:
    run_id, attempt_id = scope.run_ids
    repo = scope.runtime_db
    if repo is None or not run_id or not hasattr(repo, "append_event"):
        return
    try:
        row = repo.agent_run_for_run_id(run_id)
        if row is not None:
            repo.append_event(event_type=PACK_VERIFICATION_EVENT, attempt_id=attempt_id,
                              agent_run_id=str(row["agent_run_id"]), task_run_id=str(row["task_run_id"] or ""),
                              payload={"trigger": trigger, **result.to_fact()})
    except (sqlite3.Error, OSError):
        return


# 函数用途: 找到本 run 的核验账本（<规范任务根>/data/pack_verification/<run>.jsonl）；没有 owner 时为 None。
def _ledger(agent: object, params: object, owner: object) -> PackVerificationLedger | None:
    if owner is None:
        return None
    return ledger_for_run(pack_verification_root(agent, params, owner), str(getattr(params, "run_id", "") or ""))


# LLM: 和工具调用用的是同一个可信 cwd（execution_cwd 优先，其次 Agent 启动时的项目目录），交付物的路径模式相对于它。
# 函数用途: 返回本回合工具调用的工作区根。
def _workspace_root(agent: object, params: object) -> Path:
    from ..agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
    from ..tooling.registry_workspace import effective_registry_cwd

    tools = getattr(agent, "tools", None)
    return effective_registry_cwd(Path(getattr(tools, "workspace_root", ".")), write_boundary_with_runtime_ledger(agent, params))
