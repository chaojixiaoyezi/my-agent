from __future__ import annotations

"""LLM contract: SubAgentPatchMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
"""

import difflib
import json
import shlex
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .models import SubAgentTask
from .reports import PatchApplyRecord, PatchApplyReport, PatchReviewRecord, PatchReviewReport
from .rendering import (
    render_patch_apply_markdown,
    render_patch_apply_record_markdown,
    render_patch_review_markdown,
    render_patch_review_record_markdown,
)
from .runner_rendering import _render_runner_item_line
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _action_for_issue,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _make_due_issue,
    _risk_weight,
    _route_card_payload,
    _severity_weight,
    _runner_next_action,
    _select_capability_hits,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)
from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl
from ..tooling.write_boundary import validate_write_boundary

if TYPE_CHECKING:
    from ..local_store import LocalStore

_PATCH_APPLY_WRITE_TYPES = {"write_file"}
_PATCH_TEST_ALLOWED_PREFIXES = {"python", "python3", "pytest"}
_PATCH_TEST_TIMEOUT_SECONDS = 120
_PATCH_TEST_BLOCKED_CHARS = {"&", "|", ">", "<", "`"}

class SubAgentPatchMixin:
    def review_patches(
        self,
        run_ids: list[str] | None = None,
        *,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> PatchReviewReport:
        """审核 runner 输出里的 patch 记录。

        这里不直接应用任意 patch，只审核 runner 已声明的 patch 状态。
        `planned` / `blocked` patch 会被拦住，防止未处理改动进入 DONE。
        """

        selected = self._select_runs(run_ids)
        records: list[PatchReviewRecord] = []
        for task in selected:
            output = _read_json_object(Path(task.output_json))
            patches = _dict_list(output.get("patches", []))
            if run_ids is None and not patches:
                continue
            records.append(
                self._review_patch_task(
                    task,
                    output=output,
                    patches=patches,
                    apply=apply,
                    reviewer=reviewer,
                    note=note,
                )
            )
            if limit > 0 and len(records) >= limit:
                break

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary[record.decision] = summary.get(record.decision, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied",
                0,
            ) + 1
        return PatchReviewReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def write_patch_review_report(
        self,
        run_ids: list[str] | None = None,
        *,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> PatchReviewReport:
        """写出 patch 审核报告。"""

        report = self.review_patches(
            run_ids,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        (self.workspace / "subagent_patch_review_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_PATCH_REVIEW.md").write_text(
            render_patch_review_markdown(report),
            encoding="utf-8",
        )
        for record in report.records:
            self._write_patch_review_record_files(record)
            self._index_patch_review(record)
            if apply:
                self._append_patch_review_log(record)
        self._index_report(
            "subagent_patch_review_report",
            "latest",
            "Subagent patch review report",
            report,
            event_type="subagent_patch_review_report_written",
        )
        return report

    def apply_patches(
        self,
        run_ids: list[str] | None = None,
        *,
        apply: bool = False,
        applier: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> PatchApplyReport:
        """LLM: execute the independent patch-apply audit chain for runner-declared file writes.

        人话说明：
        这条链路和验收器分开：先审 patch 能不能安全落地，再决定是否真正写文件。
        dry-run 只展示将要写入的 diff；真实 apply 才会改文件、跑测试、记录回滚。
        """

        selected = self._select_runs(run_ids)
        records: list[PatchApplyRecord] = []
        for task in selected:
            output = _read_json_object(Path(task.output_json))
            patches = _dict_list(output.get("patches", []))
            if run_ids is None and not patches:
                continue
            records.append(
                self._apply_patch_task(
                    task,
                    output=output,
                    patches=patches,
                    apply=apply,
                    applier=applier,
                    note=note,
                )
            )
            if limit > 0 and len(records) >= limit:
                break

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary[record.decision] = summary.get(record.decision, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied",
                0,
            ) + 1
            if record.rollback_performed:
                summary["rolled_back"] = summary.get("rolled_back", 0) + 1
        return PatchApplyReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def write_patch_apply_report(
        self,
        run_ids: list[str] | None = None,
        *,
        apply: bool = False,
        applier: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> PatchApplyReport:
        """写出 patch apply 报告。"""

        report = self.apply_patches(
            run_ids,
            apply=apply,
            applier=applier,
            note=note,
            limit=limit,
        )
        (self.workspace / "subagent_patch_apply_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_PATCH_APPLY.md").write_text(
            render_patch_apply_markdown(report),
            encoding="utf-8",
        )
        for record in report.records:
            self._write_patch_apply_record_files(record)
            self._index_dataclass_record(
                "subagent_patch_apply",
                record.id,
                f"Patch apply {record.run_id} {record.decision}",
                record,
                "subagent_patch_apply_logged",
            )
            if apply:
                self._append_patch_apply_log(record)
        self._index_report(
            "subagent_patch_apply_report",
            "latest",
            "Subagent patch apply report",
            report,
            event_type="subagent_patch_apply_report_written",
        )
        return report

    def _review_patch_task(
        self,
        task: SubAgentTask,
        *,
        output: dict[str, object],
        patches: list[dict[str, object]],
        apply: bool,
        reviewer: str,
        note: str,
    ) -> PatchReviewRecord:
        """审核单个任务的 patch 输出。"""

        now = time.time()
        patch_count = len(patches)
        valid_patch_statuses = {"applied", "planned", "blocked"}
        blocked = [
            item
            for item in patches
            if str(item.get("status", "")).lower() in {"planned", "blocked"}
        ]
        invalid = [
            item
            for item in patches
            if str(item.get("status", "")).lower() not in valid_patch_statuses
        ]
        applied_patches = [
            item for item in patches if str(item.get("status", "")).lower() == "applied"
        ]
        ok = bool(patches) and not blocked and not invalid
        decision = "APPROVE" if ok else "REJECT"
        if not patches:
            decision = "NO_PATCHES"
            message = "没有 patch 需要审核。"
        elif blocked or invalid:
            parts = []
            if blocked:
                parts.append(f"{len(blocked)} 个 patch 处于 planned/blocked")
            if invalid:
                parts.append(f"{len(invalid)} 个 patch 状态未知")
            message = "；".join(parts) + "，不能审核通过。"
        else:
            message = f"{len(applied_patches)} 个 patch 已声明 applied，可审核通过。"

        applied = False
        reviewed_patches = [dict(item) for item in patches]
        if apply and patches:
            if ok:
                for item in reviewed_patches:
                    item["review_status"] = "APPROVED"
                    item["reviewed_by"] = reviewer
                    item["reviewed_at"] = now
                    if note:
                        item["review_note"] = note
                output["patches"] = reviewed_patches
                Path(task.output_json).write_text(
                    json.dumps(output, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._append_task_work_log(
                    task,
                    f"patch_review: approved={len(reviewed_patches)} reviewer={reviewer}",
                )
                applied = True
            else:
                for item in reviewed_patches:
                    if str(item.get("status", "")).lower() != "applied":
                        item["review_status"] = "NEEDS_ACTION"
                        item["reviewed_by"] = reviewer
                        item["reviewed_at"] = now
                        if note:
                            item["review_note"] = note
                output["patches"] = reviewed_patches
                Path(task.output_json).write_text(
                    json.dumps(output, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._append_task_work_log(
                    task,
                    f"patch_review: blocked={len(blocked) + len(invalid)} reviewer={reviewer}",
                )
                applied = True

        return PatchReviewRecord(
            id=_new_id("patchreview"),
            run_id=task.id,
            dry_run=not apply,
            applied=applied,
            ok=ok,
            decision=decision,
            message=message,
            patch_count=patch_count,
            approved_count=len(reviewed_patches) if ok else 0,
            blocked_count=len(blocked) + len(invalid),
            reviewer=reviewer,
            note=note,
            evidence_paths=[task.output_json, task.work_log_file],
            patches=reviewed_patches,
            created_at=now,
        )

    def _apply_patch_task(
        self,
        task: SubAgentTask,
        *,
        output: dict[str, object],
        patches: list[dict[str, object]],
        apply: bool,
        applier: str,
        note: str,
    ) -> PatchApplyRecord:
        """执行单个任务的 patch apply dry-run 或真实 apply。"""

        now = time.time()
        patch_entries: list[dict[str, object]] = []
        blocked_count = 0
        patch_specs: list[dict[str, object]] = []
        review_status_updates = [dict(item) for item in patches]
        touched_files: dict[Path, dict[str, object]] = {}

        for index, item in enumerate(review_status_updates):
            spec = self._normalize_patch_apply_spec(task, item)
            patch_entries.append(spec["audit"])
            if spec["ok"]:
                patch_specs.append(spec)
            else:
                blocked_count += 1

        test_commands, blocked_test_reasons = self._patch_apply_test_commands(task, output)
        if blocked_test_reasons:
            blocked_count += len(blocked_test_reasons)
            patch_entries.extend(
                {
                    "path": "",
                    "status": "test_command",
                    "apply_status": "BLOCKED",
                    "message": reason,
                }
                for reason in blocked_test_reasons
            )

        patch_count = len(patches)
        if not patches:
            decision = "NO_PATCHES"
            ok = False
            message = "没有 patch 可以 apply。"
        elif blocked_count:
            decision = "REJECT"
            ok = False
            message = f"{blocked_count} 项 patch/test 不满足 apply 条件。"
        elif not apply:
            decision = "WOULD_APPLY"
            ok = True
            message = f"dry-run: 将 apply {len(patch_specs)} 个 patch。"
        else:
            decision = "APPLIED"
            ok = True
            message = f"已 apply {len(patch_specs)} 个 patch。"

        rollback_performed = False
        test_results: list[dict[str, object]] = []
        applied_count = 0

        if apply and ok and patch_specs:
            try:
                for spec in patch_specs:
                    target = spec["target"]
                    before_exists = target.exists()
                    before_text = target.read_text(encoding="utf-8") if before_exists else ""
                    if target not in touched_files:
                        touched_files[target] = {
                            "before_exists": before_exists,
                            "before_text": before_text,
                        }
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(spec["content"], encoding="utf-8")
                    spec["patch_ref"]["status"] = "applied"
                    spec["patch_ref"]["apply_status"] = "APPLIED"
                    spec["patch_ref"]["applied_by"] = applier
                    spec["patch_ref"]["applied_at"] = now
                    spec["patch_ref"]["review_status"] = "APPROVED"
                    spec["patch_ref"]["reviewed_by"] = applier
                    spec["patch_ref"]["reviewed_at"] = now
                    if note:
                        spec["patch_ref"]["apply_note"] = note
                        spec["patch_ref"]["review_note"] = note
                    actual_diff = self._build_unified_diff(
                        spec["path"],
                        before_text,
                        spec["content"],
                    )
                    spec["audit"]["apply_status"] = "APPLIED"
                    spec["audit"]["actual_diff"] = actual_diff
                    spec["audit"]["message"] = "patch 已写入文件。"
                    applied_count += 1

                if test_commands:
                    test_results = self._run_patch_apply_tests(test_commands)
                    failed = [item for item in test_results if not item.get("ok")]
                    if failed:
                        raise RuntimeError(f"{len(failed)} 个 apply 后测试失败。")

                output["patches"] = review_status_updates
                Path(task.output_json).write_text(
                    json.dumps(output, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._append_task_work_log(
                    task,
                    f"patch_apply: applied={applied_count} tests={len(test_results)} applier={applier}",
                )
            except Exception as exc:
                rollback_performed = bool(touched_files)
                self._rollback_patch_apply(touched_files)
                for spec in patch_specs:
                    spec["audit"]["apply_status"] = "ROLLED_BACK" if rollback_performed else "FAILED"
                    spec["audit"]["message"] = f"apply 失败: {exc}"
                    spec["patch_ref"]["apply_status"] = spec["audit"]["apply_status"]
                decision = "ROLLBACK"
                ok = False
                message = f"patch apply 失败，已回滚: {exc}"
                self._append_task_work_log(
                    task,
                    f"patch_apply: rollback applier={applier} error={exc}",
                )

        evidence_paths = [task.output_json, task.work_log_file]
        if apply:
            evidence_paths.append(str(self.workspace / "subagent_patch_apply_log.jsonl"))

        return PatchApplyRecord(
            id=_new_id("patchapply"),
            run_id=task.id,
            dry_run=not apply,
            applied=apply and ok,
            ok=ok,
            decision=decision,
            message=message,
            patch_count=patch_count,
            applied_count=applied_count,
            blocked_count=blocked_count,
            rollback_performed=rollback_performed,
            applier=applier,
            note=note,
            evidence_paths=evidence_paths,
            test_commands=test_commands,
            test_results=test_results,
            patches=patch_entries,
            created_at=now,
        )

    def _normalize_patch_apply_spec(
        self,
        task: SubAgentTask,
        patch: dict[str, object],
    ) -> dict[str, object]:
        raw_path = str(patch.get("path") or "").strip()
        status = str(patch.get("status") or "").strip().lower()
        patch_type = str(
            patch.get("tool")
            or patch.get("type")
            or ("write_file" if any(key in patch for key in ("content", "new_content", "file_content", "after")) else "")
        ).strip().lower()
        content = patch.get("content")
        if content is None:
            for key in ("new_content", "file_content", "desired_content", "after"):
                if patch.get(key) is not None:
                    content = patch.get(key)
                    break
        diff_text = ""
        for key in ("diff", "patch", "patch_diff", "unified_diff"):
            value = patch.get(key)
            if isinstance(value, str) and value.strip():
                diff_text = value
                break

        audit = {
            "path": raw_path,
            "status": status or "unknown",
            "review_status": str(patch.get("review_status") or "UNREVIEWED"),
            "summary": str(patch.get("summary") or ""),
            "patch_type": patch_type or "unknown",
            "apply_status": "PENDING",
            "diff_preview": "",
            "actual_diff": "",
            "message": "",
        }
        if not raw_path:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = "patch 缺少 path。"
            return {"ok": False, "audit": audit, "patch_ref": patch}
        if status not in {"planned", "applied"}:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = f"patch status={status or 'unknown'} 不能进入 apply。"
            return {"ok": False, "audit": audit, "patch_ref": patch}
        if patch_type and patch_type not in _PATCH_APPLY_WRITE_TYPES:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = f"只支持 write_file patch，当前类型是 {patch_type}。"
            return {"ok": False, "audit": audit, "patch_ref": patch}
        if not isinstance(content, str):
            audit["apply_status"] = "BLOCKED"
            audit["message"] = "write_file patch 缺少完整 content，不能安全 apply。"
            audit["diff_preview"] = diff_text
            return {"ok": False, "audit": audit, "patch_ref": patch}

        boundary_error = validate_write_boundary(
            "write_file",
            {"path": raw_path},
            workspace_root=self.workspace_root,
            write_boundary={
                "allowed_write_roots": task.allowed_write_roots,
                "forbidden_write_roots": task.forbidden_write_roots,
                "locked_files": task.locked_files,
            },
        )
        if boundary_error:
            audit["apply_status"] = "BLOCKED"
            audit["message"] = boundary_error
            return {"ok": False, "audit": audit, "patch_ref": patch}

        target = self._resolve_patch_target(raw_path)
        before_text = target.read_text(encoding="utf-8") if target.exists() else ""
        audit["diff_preview"] = diff_text or self._build_unified_diff(raw_path, before_text, content)
        audit["message"] = "patch 可以进入 apply。"
        return {
            "ok": True,
            "audit": audit,
            "patch_ref": patch,
            "target": target,
            "content": content,
            "path": raw_path,
        }

    def _resolve_patch_target(self, raw_path: str) -> Path:
        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            target = self.workspace_root / target
        return target.resolve(strict=False)

    def resolve_patch_target(self, raw_path: str) -> Path:
        """公开的补丁目标路径解析方法（委托给 _resolve_patch_target）。"""
        # 委托给内部实现 _resolve_patch_target
        return self._resolve_patch_target(raw_path)

    @staticmethod
    def _build_unified_diff(path: str, before_text: str, after_text: str) -> str:
        lines = list(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        return "".join(lines)

    def _patch_apply_test_commands(
        self,
        task: SubAgentTask,
        output: dict[str, object],
    ) -> tuple[list[str], list[str]]:
        commands: list[str] = []
        blocked: list[str] = []
        for check in task.acceptance_checks:
            command = self._extract_patch_test_command(check)
            if not command:
                continue
            problem = self._validate_patch_test_command(command)
            if problem:
                blocked.append(problem)
            elif command not in commands:
                commands.append(command)
        for test in _dict_list(output.get("tests", [])):
            command = str(test.get("command") or "").strip()
            if not command:
                continue
            problem = self._validate_patch_test_command(command)
            if problem:
                blocked.append(problem)
            elif command not in commands:
                commands.append(command)
        return commands, blocked

    @staticmethod
    def _extract_patch_test_command(check: str) -> str:
        text = str(check or "").strip()
        lowered = text.lower()
        for prefix in ("command:", "test:", "run:"):
            if lowered.startswith(prefix):
                return text[len(prefix):].strip()
        if text.startswith("`") and text.endswith("`") and len(text) > 2:
            return text[1:-1].strip()
        return ""

    @staticmethod
    def _validate_patch_test_command(command: str) -> str:
        stripped = command.strip()
        if not stripped:
            return "空测试命令不能进入 patch apply。"
        if any(char in stripped for char in _PATCH_TEST_BLOCKED_CHARS):
            return f"测试命令包含高风险 shell 字符，已阻止: {command}"
        try:
            argv = shlex.split(stripped)
        except ValueError as exc:
            return f"测试命令解析失败，已阻止: {exc}"
        if not argv:
            return "空测试命令不能进入 patch apply。"
        if argv[0] not in _PATCH_TEST_ALLOWED_PREFIXES:
            return f"测试命令不在 allowlist 内，已阻止: {command}"
        return ""

    def _run_patch_apply_tests(self, commands: list[str]) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for command in commands:
            argv = shlex.split(command)
            try:
                completed = subprocess.run(
                    argv,
                    cwd=self.workspace_root,
                    capture_output=True,
                    text=True,
                    timeout=_PATCH_TEST_TIMEOUT_SECONDS,
                    check=False,
                )
                ok = completed.returncode == 0
                results.append(
                    {
                        "command": command,
                        "ok": ok,
                        "returncode": completed.returncode,
                        "stdout": completed.stdout[-4000:],
                        "stderr": completed.stderr[-4000:],
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "command": command,
                        "ok": False,
                        "returncode": -1,
                        "stdout": "",
                        "stderr": str(exc),
                    }
                )
        return results

    @staticmethod
    def _rollback_patch_apply(touched_files: dict[Path, dict[str, object]]) -> None:
        for path, snapshot in touched_files.items():
            if snapshot.get("before_exists"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(snapshot.get("before_text") or ""), encoding="utf-8")
            elif path.exists():
                path.unlink()

    def _write_patch_review_record_files(self, record: PatchReviewRecord) -> None:
        """把单个 patch 审核记录写入对应任务目录。"""

        try:
            task = self.load(record.run_id)
        except FileNotFoundError:
            return
        record_json = Path(task.reports_dir) / "patch_review.json"
        record_md = Path(task.task_dir) / "PATCH_REVIEW.md"
        record_json.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        record_md.write_text(render_patch_review_record_markdown(record), encoding="utf-8")

    def _write_patch_apply_record_files(self, record: PatchApplyRecord) -> None:
        """把单个 patch apply 记录写入对应任务目录。"""

        try:
            task = self.load(record.run_id)
        except FileNotFoundError:
            return
        record_json = Path(task.reports_dir) / "patch_apply.json"
        record_md = Path(task.task_dir) / "PATCH_APPLY.md"
        record_json.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        record_md.write_text(render_patch_apply_record_markdown(record), encoding="utf-8")

    def _append_patch_review_log(self, record: PatchReviewRecord) -> None:
        """写入全局 patch 审核日志。"""

        jsonl = self.workspace / "subagent_patch_review_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "PATCH_REVIEW_LOG.md"
        if not markdown.exists():
            markdown.write_text("# PATCH REVIEW LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
                f"applied={record.applied} message={record.message}\n"
            )
        self._index_patch_review(record)

    def _append_patch_apply_log(self, record: PatchApplyRecord) -> None:
        """写入全局 patch apply 审计日志。"""

        jsonl = self.workspace / "subagent_patch_apply_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "PATCH_APPLY_LOG.md"
        if not markdown.exists():
            markdown.write_text("# PATCH APPLY LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
                f"rollback={record.rollback_performed} message={record.message}\n"
            )
