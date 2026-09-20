# LLM: 进度策略持久化保持现有 due/失败/退休合同；调度决策由调用方传入，修改须联测后台续作和旧账归档。
# 模块用途: 管理进度策略的读取、到期投影、原子更新和过期归档，不维护额外调度循环。
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import (
    update_json_file_atomic,
    write_json_file_atomic,
)
from ..runtime_errors import runtime_error_report
from .models import (
    ConversationThread,
    ProgressPolicy,
    new_id,
)
from .store_io import (
    LEDGER_ARCHIVE_DIR,
    archive_ledger_file,
    now,
)
from .store_layout import ConversationStorage

# 无进展退避封顶倍数:间隔最多拉长到 8×(streak≥3 封顶),够让卡死任务把资源让给活任务,
# 又不至于把守望任务拖到没响应;有进展即归零复原,永不 disable。
_NO_PROGRESS_MAX_BACKOFF_MULTIPLIER = 8

# 失败续跑记账(问题6「失败自动续跑记账不正确」)机制层根因:失败 run 的异常在
# _consume_with_supply_guard 被吸收后 policy.next_due_at 不动 → 下个 tick 又 due →
# 失败无限重试。修复=失败事实落账:next_due_at 推到 now+backoff(指数退避,抖动由
# 调度层按 policy_id 确定性派生),连续失败达 retire_after 次 → enabled=False 退休
# (等用户,绝不停机式无限重试)。成功路径由调度层 mark_progress_reported(failure_count=0)
# 清零复原,退休 policy 被 disable 后不再出现在 due 扫描,账目保留在 metadata 供复盘。
_POLICY_FAILURE_RETIRE_AFTER = 3


# LLM: 策略解析和建模错误必须保持同一 context、path、policy_id 口径。
# 函数用途: 构造有定位信息的进度策略读失败报告。
def _progress_policy_read_error(path: Path, exc: BaseException) -> dict[str, Any]:
    report = runtime_error_report(exc, context="conversation.progress_policy.read")
    report["path"] = str(path)
    report["policy_id"] = path.stem
    return report


# LLM: 解析与建模拆两步,唯一目的是让读取侧增量索引缓存"磁盘原始负载"而仍复用同一份
# 错误口径(与旧 _read_progress_policy_report 逐字相同的报告:context + path + policy_id,
# 不含 traceback,因此对同一份字节确定性可缓存)。
# 函数用途: 读取并解析一个进度策略文件,返回磁盘原始负载或结构化错误。
def _read_progress_policy_payload(
    path: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"progress policy is {type(data).__name__}, expected object")
        return data, None
    except (OSError, UnicodeError, ValueError) as exc:
        return None, _progress_policy_read_error(path, exc)


# LLM: 建模失败与解析失败共用 _progress_policy_read_error,保证 load_error 集合逐条不变。
# 函数用途: 把一份已解析的策略负载建模成 ProgressPolicy,失败时给出同口径错误报告。
def _progress_policy_from_payload(
    path: Path, payload: dict[str, Any]
) -> tuple[ProgressPolicy | None, dict[str, Any] | None]:
    try:
        return ProgressPolicy.from_dict(payload), None
    except (OSError, UnicodeError, ValueError) as exc:
        return None, _progress_policy_read_error(path, exc)


# LLM: 旧实现的 try 块只覆盖"解析 + 建模"两步,拆开后两步的异常类型与错误报告完全一致,
# 因此 load_error 的 category/error_type/message/context/path/policy_id 逐字不变。
# 函数用途: 读取一个进度策略文件并建模,失败时返回同口径结构化错误。
def _read_progress_policy_report(path: Path) -> tuple[ProgressPolicy | None, dict[str, Any] | None]:
    payload, error = _read_progress_policy_payload(path)
    if payload is None:
        return None, error
    return _progress_policy_from_payload(path, payload)


# LLM: 增量索引与全量扫描共用同一读取器;解析失败时 ok=False,调用方按原语义把它计入
# load_errors,因此索引不会让任何一条策略读取失败被静默跳过。
# 函数用途: 进度策略文件的索引读取器,统一 (ok, 负载, 错误) 形状。
def _read_progress_policy_entry(
    path: Path,
) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    payload, error = _read_progress_policy_payload(path)
    return payload is not None, payload, error


# LLM: 依赖仅为同源目录和线程校验；策略状态不能由模型文案或索引裁决。
# 类用途: 保存进度策略、投影到期队列并执行原更新和归档操作。
class ProgressStore:
    # LLM: 策略落账只依赖原存储与线程校验；调度循环、模型调用和租约均不属于本领域。
    # 函数用途: 初始化进度策略存取能力，不创建策略或后台任务。
    def __init__(
        self, storage: ConversationStorage, *, require_thread: Callable[[str], ConversationThread]
    ) -> None:
        self.storage = storage
        self._require_thread = require_thread

    # LLM: 仍创建新的 policy ID，先验证线程后写入原路径，不暗中改成覆盖式 upsert。
    # 函数用途: 为现有线程保存一条新的进度策略。
    def create(self, request: dict) -> ProgressPolicy:
        thread_id = str(request.get("thread_id") or "")
        self._require_thread(thread_id)
        current = now(request.get("now"))
        interval_seconds = max(0, int(request.get("interval_seconds") or 0))
        policy = ProgressPolicy(
            policy_id=new_id("policy"),
            thread_id=thread_id,
            task_id=str(request.get("task_id") or ""),
            interval_seconds=interval_seconds,
            next_due_at=current + interval_seconds,
            route_channel=str(request.get("route_channel") or "internal"),
            route_target=str(request.get("route_target") or ""),
            metadata=request.get("metadata") or {},
        )
        write_json_file_atomic(self.storage.policy_path(policy.policy_id), policy.to_dict())
        return policy

    # LLM: 坏账保持原只返回对象的入口语义，需严格诊断时使用列表报告。
    # 函数用途: 按精确 ID 读取进度策略。
    def load(self, policy_id: str) -> ProgressPolicy | None:
        policy, _ = _read_progress_policy_report(self.storage.policy_path(policy_id))
        return policy

    # LLM: 仅包装原列表报告，不更新 due 或处理状态。
    # 函数用途: 按原 enabled 开关列出策略对象。
    def list(self, *, enabled_only: bool = False) -> list[ProgressPolicy]:
        policies, _ = self.list_report(enabled_only=enabled_only)
        return policies

    # LLM: 策略目录原先每轮全量解析(即便只为了 enabled 过滤)。这里用同一份增量索引跳过
    # 未变化文件的重复解析;enabled 过滤、按 next_due_at 排序、到期筛选仍由调用方按权威
    # 负载判定,load_error 集合逐条不变。
    # 函数用途: 列出策略及逐条读错误，复用同源扫描索引。
    def list_report(
        self, *, enabled_only: bool = False
    ) -> tuple[list[ProgressPolicy], list[dict[str, Any]]]:
        policies: list[ProgressPolicy] = []
        load_errors: list[dict[str, Any]] = []
        index = self.storage.indexes.get("progress_policies")
        paths = index.ordered_paths(self.storage.policies_dir, "*.json")
        use_index = self.storage.indexes.usable(
            index, key="progress_policies", directory=self.storage.policies_dir, pattern="*.json"
        )
        for path in paths:
            _ok, payload, error = self.storage.indexes.read(
                index,
                path,
                _read_progress_policy_entry,
                identity_key="policy_id",
                use_index=use_index,
            )
            policy: ProgressPolicy | None = None
            if payload is not None:
                # 负载与解析错误互斥,因此这里换成"建模阶段"的同口径错误,一个文件仍只贡献一条错误。
                policy, error = _progress_policy_from_payload(path, payload)
            if policy is not None:
                policies.append(policy)
            if error is not None:
                load_errors.append(error)
        policies = [policy for policy in policies if policy.enabled] if enabled_only else policies
        policies.sort(key=lambda item: item.next_due_at)
        return policies, load_errors

    # LLM: 只选择已启用且到期的原策略，读取不代表领取执行权。
    # 函数用途: 返回当前时间已经到期的进度策略。
    def due(self, *, now: float | None = None) -> list[ProgressPolicy]:
        policies, _ = self.due_report(now=now)
        return policies

    # LLM: 到期判断与错误集合保持原口径，不把损坏策略静默视作未到期。
    # 函数用途: 返回启用的到期策略和读取错误，不进行调度。
    def due_report(
        self, *, now: float | None = None
    ) -> tuple[list[ProgressPolicy], list[dict[str, Any]]]:
        current = now if now is not None else time.time()
        policies, load_errors = self.list_report(enabled_only=True)
        return [policy for policy in policies if policy.next_due_at <= current], load_errors

    # LLM: 仅使用结构化无进展次数与元数据顺延，保留原间隔和退避上限。
    # 函数用途: 保存一次已汇报进度的时间及后续到期位置。
    def mark_reported(
        self,
        policy_id: str,
        *,
        now: float | None = None,
        no_progress_streak: int | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> ProgressPolicy:
        # no_progress_streak(§6-B4 退避):调度器在唤醒轮结束后按【结构化信号】(本轮工具调用
        # 全失败或压根没调工具=无进展)传入连续无进展轮数;间隔按 2^streak 拉长、封顶 8 倍——
        # 卡死任务自动让出资源但【永不停机】(区别于 disable 退休),一有进展 streak 归零复原。
        # None = 旧语义原样(按原 interval 顺延,不碰 streak 账目),供续命/去重等非执行路径用。
        policy = self.load(policy_id)
        if policy is None:
            raise KeyError(f"unknown progress policy: {policy_id}")
        current = now if now is not None else time.time()
        interval = max(0, policy.interval_seconds)
        metadata = dict(policy.metadata or {})
        if metadata_updates:
            metadata.update(metadata_updates)
        if no_progress_streak is not None:
            streak = max(0, int(no_progress_streak))
            metadata["no_progress_streak"] = streak
            interval = interval * min(2**streak, _NO_PROGRESS_MAX_BACKOFF_MULTIPLIER)
        updated = replace(
            policy, last_report_at=current, next_due_at=current + interval, metadata=metadata
        )
        write_json_file_atomic(self.storage.policy_path(policy_id), updated.to_dict())
        return updated

    # LLM: 检查时间不同于模型报告时间，不能把无变化探测计为一次模型汇报。
    # 函数用途: 记录轻量检查并顺延到期时间，保留上次正式汇报。
    def mark_checked(
        self,
        policy_id: str,
        *,
        now: float | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> ProgressPolicy:
        """Snooze an unchanged automatic check without recording a model report."""
        policy = self.load(policy_id)
        if policy is None:
            raise KeyError(f"unknown progress policy: {policy_id}")
        current = now if now is not None else time.time()
        metadata = dict(policy.metadata or {})
        if metadata_updates:
            metadata.update(metadata_updates)
        metadata["last_material_check_at"] = current
        updated = replace(
            policy,
            next_due_at=current + max(0, policy.interval_seconds),
            metadata=metadata,
        )
        write_json_file_atomic(self.storage.policy_path(policy_id), updated.to_dict())
        return updated

    # LLM: 失败次数、退避长度来自调度层；按原阈值退休并保留失败账。
    # 函数用途: 持久记录一次失败续作及下一次重试或禁用状态。
    def mark_failed(
        self,
        policy_id: str,
        *,
        now: float | None = None,
        backoff_seconds: float,
        failure_count: int,
        retire_after: int = _POLICY_FAILURE_RETIRE_AFTER,
    ) -> ProgressPolicy | None:
        """失败续跑记账:写 failure_count/last_failure_at 并退避顺延,超阈值退休。

        backoff_seconds 由调度层按 5min×2^(n-1) 上限 1h 计算(含按 policy_id 确定性
        派生的抖动);本方法只落账,不自己算退避——退避公式是调度策略,存层只持久化。
        连续失败达 retire_after → enabled=False 退休(不再进 due 扫描),失败账目保留
        在 metadata(供复盘)。成功路径 mark_progress_reported 清零复原。
        """
        policy = self.load(policy_id)
        if policy is None:
            return None
        current = now if now is not None else time.time()
        metadata = dict(policy.metadata or {})
        metadata["failure_count"] = max(0, int(failure_count))
        metadata["last_failure_at"] = current
        metadata["last_backoff_seconds"] = float(backoff_seconds)
        retired = int(failure_count) >= max(1, int(retire_after))
        if retired:
            metadata["retired_at"] = current
        updated = replace(
            policy,
            enabled=not retired,
            last_report_at=current,
            next_due_at=current + float(backoff_seconds),
            metadata=metadata,
        )
        write_json_file_atomic(self.storage.policy_path(policy_id), updated.to_dict())
        return updated

    # LLM: 沿原文件锁调用结构化 updater，None 表示明确放弃而不是保存空数据。
    # 函数用途: 执行进度策略 CAS 更新并返回对象、变更和放弃标记。
    def update_atomic(
        self,
        policy_id: str,
        updater: Callable[[ProgressPolicy], ProgressPolicy | None],
    ) -> tuple[ProgressPolicy | None, bool, bool]:
        """锁内 CAS 更新一个进度策略（flock 跨进程互斥，读-改-写原子）。

        使用 gateway_parts.io 的 update_json_file_atomic（fcntl.flock 锁内
        读-改-写）保证同文件系统内任意策略维护者的原子互斥；调用方只提供
        纯结构化 updater，不能从文案猜状态。
        返回值 (policy, changed, aborted)：
        - aborted=True = updater 返回 None（条件不满足，明确放弃，不落盘）
        - changed=True = 锁内比对后内容真正写盘
        - policy=None = 查无此 policy（require_existing 失败）
        """
        changed = False
        aborted = False

        # LLM: 在同一原子更新中建模、比较和序列化，放弃保持原负载。
        # 函数用途: 适配调用方策略更新函数并记录是否真正修改。
        def _wrap(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed, aborted
            policy = ProgressPolicy.from_dict(current)
            updated = updater(policy)
            if updated is None:
                aborted = True
                return current  # 条件不满足: 原样返回, 内容无变更
            result = updated.to_dict()
            changed = result != current
            return result

        try:
            update_json_file_atomic(
                self.storage.policy_path(policy_id),
                _wrap,
                require_existing=True,
            )
        except (FileNotFoundError, KeyError):
            return None, False, False
        return self.load(policy_id), changed, aborted

    # LLM: 仅停用尚启用的精确策略，并沿原 last_report_at 记录退休时间。
    # 函数用途: 让不再需要的进度策略退出到期扫描，保留历史文件。
    def disable(self, policy_id: str, *, now: float | None = None) -> ProgressPolicy | None:
        # 退休一个进度策略：把 enabled 置 False，使它从 due 扫描里彻底消失。
        # 用于回收"被观察任务已终态/早已 stale"的后台 watch 策略，避免它被无限续命、
        # 每个间隔唤醒后台主代理发一次 LLM 进度汇报，把 gateway worker 占满（churn 根因）。
        policy = self.load(policy_id)
        if policy is None:
            return None
        if not policy.enabled:
            return policy
        current = now if now is not None else time.time()
        updated = replace(policy, enabled=False, last_report_at=current)
        write_json_file_atomic(self.storage.policy_path(policy_id), updated.to_dict())
        return updated

    # LLM: The canonical task lifecycle writer calls this exact-task cleanup on every terminal or
    # interrupted transition. Scheduler suppression remains a defensive race/legacy-data backstop.
    # 函数用途: 立即停用一个任务名下全部仍启用的后台续作策略，防止终态任务再次唤醒模型。
    def disable_task(
        self,
        task_id: str,
        *,
        now: float | None = None,
    ) -> tuple[str, ...]:
        selected_task_id = str(task_id or "").strip()
        if not selected_task_id:
            return ()
        retired: list[str] = []
        policies, _load_errors = self.list_report(enabled_only=True)
        for policy in policies:
            if str(policy.task_id or "").strip() != selected_task_id:
                continue
            if self.disable(policy.policy_id, now=now) is not None:
                retired.append(policy.policy_id)
        return tuple(retired)

    # LLM: 只允许把 enabled 策略的 due 单调提前，不重写模型选择的间隔。
    # 函数用途: 按结构化信号提前下一次检查并保存原因与次数。
    def expedite(
        self, policy_id: str, *, due_at: float, reason: str = "", now: float | None = None
    ) -> ProgressPolicy | None:
        # 单调提前一个 enabled 策略的下次触发时间(只往早、绝不往晚推)。调度器按结构信号
        # (如盯守 backlog 有活堆着)给排期封响应上限用:不改 interval_seconds——模型自选的
        # 节奏意图保留,信号消失后自动回到原节奏。目标时间不早于现值时原样返回(幂等不写盘)。
        policy = self.load(policy_id)
        if policy is None or not policy.enabled:
            return None
        if due_at >= policy.next_due_at:
            return policy
        current = now if now is not None else time.time()
        metadata = dict(policy.metadata or {})
        try:
            expedite_count = int(metadata.get("expedite_count") or 0)
        except (TypeError, ValueError):
            expedite_count = 0
        metadata["expedite_count"] = expedite_count + 1
        metadata["expedited_at"] = current
        metadata["expedite_reason"] = str(reason or "")
        updated = replace(policy, next_due_at=float(due_at), metadata=metadata)
        write_json_file_atomic(self.storage.policy_path(policy_id), updated.to_dict())
        return updated

    # LLM: 仅归档已禁用且超过原保留期的策略，继续移动原文件及锁；不得改动仍启用的策略。
    # 函数用途: 将陈旧策略移出扫描目录，并返回本次归档数量。
    def archive_stale(self, *, current: float, retention_seconds: float) -> int:
        archived_policies = 0
        policies, _ = self.list_report()
        for policy in policies:
            if policy.enabled:
                continue
            age = current - max(policy.last_report_at, policy.next_due_at)
            if age <= retention_seconds:
                continue
            if archive_ledger_file(
                self.storage.policy_path(policy.policy_id),
                self.storage.policies_dir.parent / LEDGER_ARCHIVE_DIR / "policies",
            ):
                archived_policies += 1
        return archived_policies
