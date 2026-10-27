# LLM: 策展按原owner实际配置/本地时钟发现，在_submit重采与原commit门内末核；失格不新造队列或消费状态。
# LLM: 原C同backgroundThread只读分页发现文件源和RuntimeDB到期行；明确ro且无Agent，T/C/lease/Root末门仍入唯一P3/P5。
# 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。
# 模块用途: 将原会话、调度、学习、维护与watch来源接到生产请求泵，C撤权时停止新提交，原来源保留。
from __future__ import annotations

import math
import os
import threading
import time
from bisect import bisect_right
from pathlib import Path
from types import SimpleNamespace

from ..common.global_write_guard import require_global_writer
from ..owner_wake_discovery import (
    _STORE_ROOT_PATTERNS,
    _has_pending_memory_curator_work,
    discover_owner_home_page,
)
from ..user_space.owner_resolver import OwnerIdentity, _owner_home_dir
from .control_plane import GatewayControlPlane
from .multiprocess_background_request import (
    BackgroundSourceChanged,
    enqueue_source,
    freeze_source,
    read_source,
    source_digest,
)
from .multiprocess_drain_runtime import GatewayDrainRuntime
from .multiprocess_mode import current_worker_partition
from .multiprocess_route import owner_digest


# LLM: 纯内存事实只拒缺C源的旧车道，不授Thread/写权；任意已登记C/fork副本均须真实Root，Count0旧调用没有新增I/O或路径字段要求。
# 函数用途: 判断该背景入口必须走原C元数据生产者，避免丢control/代次后回落旧inline业务。
def coordinator_background_required(control, paths, count):
    from .multiprocess_coordinator import coordinator_executor_required, coordinator_role_present

    require_global_writer("gateway_background_producer")
    root = getattr(paths, 'root', None)
    return (control is not None or count > 0 or coordinator_executor_required()
            or root is not None and coordinator_role_present(root, context=paths))


# LLM: 仅运输本原controller的限制性owner blocked结果；status/reason/owner_sha供内部拒门分流，不自签allow、不从message判断、不证明P2已停。
# 类用途: 区分一个owner隔离与整个C源失效，防单个隔离owner阻塞健康来源发现。
class _OwnerBackgroundBlocked(ValueError):
    # LLM: 调用方只在同原controller锁下已核owner范围/原source不变后创建；字段不是执行权或恢复账。
    # 函数用途: 保存本次确切owner的原结构化拒绝。
    def __init__(self, decision, owner_sha):
        self.status = decision['status']
        self.reason = decision['reason']
        self.error_code = decision['error_code']
        self.owner_sha = owner_sha
        super().__init__('background_owner_blocked')


# LLM: 只准具体owner的原隔离且全域source完整；global quarantine/换源/根关闭不能被降成逐owner跳过，观察仅限制，不授健康owner许可。
# 函数用途: 识别当前原controller在此准确owner上的局部拒绝。
def _owner_only_blocked(controller, owner_sha, decision):
    from .multiprocess_takeover_runtime import _sources_unchanged

    return (decision.get('status') == 'blocked' and decision.get('reason') == 'owner_partition_quarantined'
            and decision.get('error_code') == 'OWNER_PARTITION_QUARANTINED'
            and not controller._all_quarantined
            and (owner_sha in controller._unproven_owners or any(key[-1] == owner_sha for key in controller._quarantined))
            and _sources_unchanged(controller, controller._bootstrap_sources))


# LLM: 只代表当前同步C提交的时间资格失效，不是JSON状态/权限或source损坏；严禁按相同异常文字捕获其它错误。
# 类用途: 安全退出尚未产生队列效果的一次策展提交。
class _CuratorNotDue(Exception):
    pass


# LLM: 仅原C调用，入口可按当前配置重新资格采样；expected之后必须语义配置和timezone相同，原source/hash/revision每次完整重读。
# 函数用途: 在发现或真正提交窗口核对原owner的策展时间资格，不构造业务Agent。
def _curator_submission_config(control, owner, ref, *, expected=None):
    from ..memory_store.curator_models import MemoryCuratorConfig, MemoryCuratorState
    from ..owner_wake_discovery import curator_work_is_due
    from .owner_resources import resolve_owner_resource_configuration

    home, config, _ = resolve_owner_resource_configuration(control,owner)
    timing = (MemoryCuratorConfig.from_agent_config(config),str(getattr(config,'timezone','') or '').strip())
    if expected is not None and timing != expected:
        raise BackgroundSourceChanged()
    value = read_source(control.home_root,ref)
    if not curator_work_is_due(home.owner_home_dir,MemoryCuratorState.from_dict(value),
            curator_config=timing[0],timezone_name=timing[1]):
        raise _CuratorNotDue()
    return timing


# LLM: 原control/Root/lease同源且持续有效才发现/提交；游标仅查询，owner局部blocked不扩散成健康owner禁令，全域失源仍停止，不建权限账。
# 类用途: 在生产C后台节拍中发现原持久源，并给同一P3/P5请求主链提交有限工作。
class CoordinatorBackgroundRequests:
    # LLM: 原C/control/Root/controller/lease必须准确同源；构造仅内存检查，不建远端Agent/线程，不因None或复制Root退回旧车道。
    # 函数用途: 捕获本代原生产发现源与关闭栅栏。
    def __init__(self, control, *, drain_runtime=None):
        require_global_writer("gateway_background_producer")
        if type(control) is not GatewayControlPlane or type(drain_runtime) is not GatewayDrainRuntime:
            raise ValueError("background_original_coordinator_required")
        self.control = self._control_origin = control
        self.runtime = self._runtime_origin = drain_runtime
        self._controller = self._controller_origin = control.controller()
        self._lease_origin = drain_runtime._lease_origin
        self._origin, self._creator_pid = self, os.getpid()
        self._cursor = None
        self._source_cursors = {}
        self._next_poll_at = 0.0
        self._next_compact_at = 0.0
        self.require_original()

    # LLM: 元数据查询不授执行权；源对象/原Root/control/当前lease与PID皆准确核，关闭或替换不能借新C恢复原producer。
    # 函数用途: 在扫描和每次末提交前核对本代生产发现来源。
    def require_original(self):
        require_global_writer("gateway_background_producer")
        if (type(self) is not CoordinatorBackgroundRequests or self._origin is not self
                or self._creator_pid != os.getpid() or current_worker_partition() is not None
                or self.control is not self._control_origin or self.runtime is not self._runtime_origin
                or self._controller is not self._controller_origin or self.control.controller() is not self._controller
                or self.runtime.control is not self.control or self.runtime.controller is not self._controller
                or self._controller._lease is not self._lease_origin):
            raise ValueError("background_original_coordinator_required")
        self.runtime.require_original()
        if self.runtime._closing or not self._controller.active():
            raise ValueError("background_producer_closed")

    # LLM: 同一原backgroundThread承载发现与共享压缩；原60秒配置/0关闭不变，不建立三个旧控制线程或远端Agent。
    # 函数用途: 运行一拍原持久来源发现，并按既有维护节拍扫描共享索引。
    def tick(self):
        queued = self.poll()
        interval = max(0.0, float(getattr(self.control.config, 'owner_maintenance_scan_interval_seconds', 60.0)))
        now = time.monotonic()
        if interval > 0 and now >= self._next_compact_at:
            self.require_original()
            from ..user_space import home_paths
            from .multiprocess_maintenance import CoordinatorIndexCompact

            self._next_compact_at = now + interval
            compact = CoordinatorIndexCompact(self.control, commit_transition=self._commit_main)
            try:
                compact.run(home_paths(self.control.home_root))
            except Exception as error:
                from .multiprocess_global_write import GlobalWriteError

                if not (type(error) is GlobalWriteError and type(error.__cause__) is _OwnerBackgroundBlocked):
                    raise
        return queued

    # LLM: 本method只发起一个新的原同步短门，每次都核源/Root；不把方法引用当持久许可或复活已关闭C。
    # 函数用途: 为原全局会计与共享索引叶提供同一local-main末提交门。
    def _commit_main(self, operation):
        return self.commit(OwnerIdentity.local_main(), operation)

    # LLM: 只读分页扫描在T/C短门外；最终enqueue逐源T先行且由原C/lease/Root同步裁决，发现计数只是运输事实。
    # 函数用途: 提交到期来源供原P3/P5消费者执行，不占C远端模型槽。
    def poll(self):
        self.require_original()
        now = time.monotonic()
        if now < self._next_poll_at:
            return 0
        self._next_poll_at = now + 1.0
        page = discover_owner_home_page(self.control.home_root / "owners", limit=64, after_cursor=self._cursor)
        self._cursor = page.next_cursor
        from .multiprocess_curator_accounting import reconcile_quota

        try:
            reconcile_quota(self.control, [OwnerIdentity.local_main(), *(target.identity for target in page.targets)],
                            commit_transition=self._commit_main)
        except _OwnerBackgroundBlocked as blocked:
            if blocked.owner_sha != owner_digest(OwnerIdentity.local_main()):
                raise
        count = 0
        local = OwnerIdentity.local_main()
        targets = (SimpleNamespace(identity=local, home_dir=_owner_home_dir(self.control.home_root, local)), *page.targets)
        for target in targets:
            for ref in self._owner_sources(target):
                count += int(self._submit(ref))
        return count

    # LLM: 只读原owner来源；learning沿FIFO/registry，冷维护按同代正文last_maintenance_at，mtime仅CAS不作维护时钟。
    # 函数用途: 为一个现存owner列出有限的原后台工作引用。
    def _owner_sources(self, target):
        owner, home = target.identity, target.home_dir
        if owner.owner_kind not in {"user", "group"} and owner != OwnerIdentity.local_main():
            return
        from .owner_conversation_store import owner_conversation_store_from_config
        from .owner_resources import resolve_owner_resource_configuration
        from .runtime_wake_source import freeze_runtime_wake, runtime_wake_reader

        if (home / 'runtime.db').is_file():
            scoped_home, config, _ = resolve_owner_resource_configuration(self.control, owner)
            store = owner_conversation_store_from_config(config, scoped_home, initialize=False)
            for row in runtime_wake_reader(home).due_runtime_wakes(now=time.time()):
                yield freeze_runtime_wake(home, owner, store, row)
        from ..tooling.process_session_store import process_session_store_root
        from .process_completion_source import discover_process_completions

        process_paths = _source_page(self, process_session_store_root(home, home), "bg-*.json")
        yield from discover_process_completions(self.control, owner, process_paths)
        candidates = []
        if float(getattr(self.control.config, "owner_maintenance_scan_interval_seconds", 60.0)) > 0:
            candidates.append(("maintenance", home / "data" / "maintenance.json"))
        candidates.extend(("harvester", path) for path in _source_page(self, home / "watch_state", "ws-??????????.json"))
        candidates.extend(("orphan", path) for path in _source_page(self, home, "**/work/agents/*/canonical_state.json"))
        scheduler = home / "data" / "scheduler" / "store.json"
        if scheduler.is_file():
            candidates.append(("scheduler", scheduler))
        if getattr(self.control.config, "memory_curator_enabled", True):
            from ..memory_store.curator_models import MemoryCuratorConfig

            _, curator_owner_config, _ = resolve_owner_resource_configuration(self.control,owner)
            curator_config = MemoryCuratorConfig.from_agent_config(curator_owner_config)
            timezone_name = str(getattr(curator_owner_config,'timezone','') or '').strip()
            if _has_pending_memory_curator_work(home,curator_config=curator_config,timezone_name=timezone_name):
                candidates.append(("curator", home / "memory" / "curator" / "state.json"))
        if getattr(self.control.config, "enable_self_learning", False):
            from ..capability.skill_learning_store import SkillLearningStore

            learning = SkillLearningStore(home / "data" / "skill_learning", home / "skills")
            pending = learning.pending_requests()
            if pending:
                candidates.append(("learning", pending[0]))
            elif getattr(self.control.config, "self_learning_maintenance_mode", "observe") == "autonomous" and learning.registry_path.is_file():
                candidates.append(("learning", learning.registry_path))
        for pattern in _STORE_ROOT_PATTERNS:
            for root in sorted(home.glob(pattern)):
                for urgency in ("urgent", "normal"):
                    candidates.extend(("wake", path) for path in _source_page(self, root / "wake_queue" / urgency, "*.json"))
                candidates.extend(("observation", path) for path in _source_page(self, root / "observations", "*.jsonl"))
                candidates.extend(("policy", path) for path in _source_page(self, root / "progress_policies", "*.json"))
        for kind, path in candidates:
            try:
                options = {'curator_config':curator_config,'timezone_name':timezone_name} if kind == 'curator' else {}
                ref = freeze_source(self.control.home_root, owner, path, kind, **options)
                if kind == "learning" and ref["source_ref"] == "data/skill_learning/registry.json":
                    from ..capability.skill_learning_maintenance import learning_maintenance_due

                    if not learning_maintenance_due(float(read_source(self.control.home_root, ref).get('last_maintenance_at') or 0.0),
                            self.control.config.self_learning_maintenance_mode, time.time()):
                        continue
                if kind == "harvester":
                    from ..ingestion.harvest_execution import harvester_has_due_work

                    if not harvester_has_due_work(read_source(self.control.home_root, ref), time.time()):
                        continue
                if kind == "orphan":
                    from ..common.canonical_source_contract import canonical_child_source_contract

                    if not canonical_child_source_contract().has_work(read_source(self.control.home_root, ref)):
                        continue
                if kind == "maintenance" and not _maintenance_has_due_work(read_source(self.control.home_root, ref), time.time()):
                    continue
                if kind == "scheduler" and not _scheduler_has_due_work(read_source(self.control.home_root, ref), time.time()):
                    continue
                if kind == "observation" and not read_source(self.control.home_root, ref)["observations"]:
                    continue
                if kind == "policy" and not _policy_has_due_work(read_source(self.control.home_root, ref), time.time()):
                    continue
                yield ref
            except (OSError, ValueError, TypeError) as error:
                from .logging import _report_gateway_side_effect_error

                _report_gateway_side_effect_error("background_source_discovery", owner.owner_id, error)

    # LLM: 原controller/lease末门仅同步commit，Root关闭与实际副作用同锁；不持任何门做模型、网络、join或完整owner发现。
    # 函数用途: 在原C仍有效且未停止producer时完成一个短原写动作。
    def commit(self, owner, operation):
        self.require_original()
        results = []
        live = [True, os.getpid(), threading.get_ident()]

        # LLM: 原controller同步调用，live仅限制本原窗口；不能将该closure缓存、复制或换线程当许可。
        # 函数用途: 在原lease内和原停止门内做最后源核验，再执行实际短写。
        def write():
            with self.runtime._gate:
                if not live[0] or live[1:] != [os.getpid(), threading.get_ident()]:
                    raise ValueError("background_original_commit_window_required")
                self.require_original()
                results.append(operation())

        try:
            owner_sha = owner_digest(owner)
            with self._controller._mutex:
                decision = self._controller.launch_owner_writer(owner_sha, write)
                if _owner_only_blocked(self._controller, owner_sha, decision):
                    raise _OwnerBackgroundBlocked(decision, owner_sha)
                if decision.get("status") != "started" or not results:
                    raise ValueError("background_original_coordinator_required")
            return results[0]
        finally:
            live[0] = False

    # LLM: ref不含配置权；策展在_submit入口重采实际配置/source资格并冻结本次预期，原T→controller/lease/runtime门内再核同配置及当下资格，W/Count0协议不变。
    # 函数用途: 沿唯一输入回执提交一次后台来源，重复发现返回原请求。
    def _submit(self, ref):
        self.require_original()
        owner = target_owner(ref)
        try:
            timing = _curator_submission_config(self.control,owner,ref) if ref['kind'] == 'curator' else None
        except _CuratorNotDue:
            return False
        frozen_digest = source_digest(ref)
        live = [True, os.getpid(), threading.get_ident()]

        # LLM: 仅原enqueue_source的当前同步输入T调用；返回/异常即失效，不接wire或可重放授权。
        # 函数用途: 把实际输入落盘短动作运输到同一原C停止门。
        def transition(operation):
            if not live[0] or live[1:] != [os.getpid(), threading.get_ident()]:
                raise ValueError("background_original_commit_window_required")
            if source_digest(ref) != frozen_digest:
                raise BackgroundSourceChanged()
            # LLM: 真正controller/lease/runtime门等待后再核时钟/配置/source；回调必须返回原IO tuple，失格仅抛本私有控制流。
            # 函数用途: 在最后队列写入之前拒绝已经过期的候选资格。
            def guarded_operation():
                if timing is not None:
                    _curator_submission_config(self.control,owner,ref,expected=timing)
                return operation()
            return self.commit(owner, guarded_operation)

        try:
            return enqueue_source(self.control.paths, self.control.home_root, ref,
                                  commit_transition=transition)[1]
        except _CuratorNotDue:
            return False
        except _OwnerBackgroundBlocked as blocked:
            if blocked.owner_sha != owner_digest(owner):
                raise
            return False
        finally:
            live[0] = False


# LLM: cursor仅为进程内metadata查询位置，绝非处理/授权账；处理完、disabled或坏文件也推进，有限预算不能固定遮挡后页。
# 函数用途: 每个原来源目录轮转最多64项，重启从头仍会继续后续页，不删除业务历史。
def _source_page(discovery, folder, pattern):
    cursors = getattr(discovery, "_source_cursors", None)
    if cursors is None:
        cursors = discovery._source_cursors = {}
    key = (str(folder), pattern)
    paths = sorted(folder.glob(pattern))
    names = [path.relative_to(folder).as_posix() for path in paths]
    start = bisect_right(names, cursors.get(key, ""))
    if start >= len(paths):
        start = 0
    page = paths[start:start + 64]
    if page and start + len(page) < len(paths):
        cursors[key] = page[-1].relative_to(folder).as_posix()
    else:
        cursors.pop(key, None)
    return page


# LLM: 只恢复已校验ref的owner，仍由原controller独立检查；不依赖展示目录或自然语言来源。
# 函数用途: 为当前C短门投影规范owner。
def target_owner(ref):
    from .multiprocess_background_request import source_owner

    return source_owner(ref["owner"])


# LLM: 纯metadata预筛，最终reserve_due_runs仍在原W P5/owner仓库重核；索引空值或非法时间不授执行权。
# 函数用途: 判断原scheduler账是否包含到期的active作业。
def _scheduler_has_due_work(payload, now):
    if any(type(row) is dict and row.get("status") == "queued" for row in payload["runs"].values()):
        return True
    for row in payload["jobs"].values():
        if type(row) is not dict or row.get("status") != "active":
            continue
        due = row.get("next_run_at")
        if type(due) in (int, float) and math.isfinite(due) and 0 < due <= now:
            return True
    return False


# LLM: 纯原policy字段预筛；原tick_thread在P5里再次读取enabled/next_due及业务claim，不从报告文字派发。
# 函数用途: 判断一份原周期策略目前是否到期。
def _policy_has_due_work(payload, now):
    due = payload.get("next_due_at")
    return payload.get("enabled", True) is True and type(due) in (int, float) and math.isfinite(due) and due <= now


# LLM: 仅metadata预筛原v2策略与原maintenance状态；真正执行仍由原claim/逐写原T及固定policy fingerprint裁决。
# 函数用途: 判断一个原owner维护来源是否到期。
def _maintenance_has_due_work(payload, now):
    from ..memory_store.retention_models import MemoryRetentionPolicy
    from .multiprocess_background_sources import maintenance_due

    return maintenance_due(payload["state"], MemoryRetentionPolicy.from_payload(payload["policy"]), now)
