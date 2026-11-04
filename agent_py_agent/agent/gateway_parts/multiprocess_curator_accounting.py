# LLM: 原quota是唯一会计，run去重/P3占用保持；裸RMW叶限原同期PID/thread单次消费，callback同步进同Root关闭门，不授权或建reserve账。
# LLM: ControlPlane只显式投影其canonical home_root对应的原typed路径，不动态补Agent字段；原quota/P3短门与全局会计权威不变。
# 模块用途: 为C提供跨worker策展额度与幂等核算，cold准入沿同一根读取；Count0保留v2身份，模式切换不能重扣。
from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..common.global_write_guard import require_global_writer
from ..common.json_io import locked_json_path, write_json_file_atomic_unlocked
from ..common.nofollow_fs import (
    open_directory_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
)
from ..common.strict_json import load_strict_json
from ..memory_store.curator_run_log import CuratorRunRecord
from ..owner_wake_discovery import _owner_home_signature, discover_owner_home_page
from ..user_space.owner_resolver import OwnerIdentity, _owner_home_dir
from .multiprocess_mode import current_worker_partition
from .multiprocess_route import owner_digest

_QUOTA_SCHEMA = "curator_daily_quota.v2"
# 既有策展全局日上限，单位实际运行次数；此前只在CLI默认常量中声明。
CURATOR_DAILY_QUOTA_DEFAULT_COUNT = 5000


# LLM: 此异常只描述quota来源/锁/原未结投影不可核实；不能捕获原P3写入或观察者异常后改称quota故障。
# 类用途: 为原准入门提供明确的策展会计不可核实原因。
class CuratorQuotaUnavailable(RuntimeError):
    pass


# LLM: 原会计按UTC日期；日期只划分当日预算，不成为任务/租约/模型身份或授权期限。
# 函数用途: 返回当前全局额度的日期分片。
def quota_date():
    return datetime.now(timezone.utc).date().isoformat()


# LLM: W不能调用全局会计写者；缺失文件沿原初始count=0，坏字节/类型必须失败，不能抹成空成功。
# 函数用途: 严格读取原额度文件，保留显式v2身份及迁移事实。
def _read_quota(path):
    if current_worker_partition() is not None:
        raise ValueError("curator_quota_coordinator_required")
    raw = read_bytes_beneath(path.parent, (path.name,), require_dir_fd=True)
    value = {} if raw is None else load_strict_json(raw)
    if type(value) is not dict or type(value.get("date", "")) is not str or type(value.get("count", 0)) is not int or value.get("count", 0) < 0:
        raise ValueError("curator_quota_unreadable")
    if "schema_version" in value and value["schema_version"] != _QUOTA_SCHEMA:
        raise ValueError("curator_quota_schema_invalid")
    if value.get("schema_version") == _QUOTA_SCHEMA:
        rows = value.get("accounted_records")
        if type(rows) is not list or len(set(rows)) != len(rows) or any(type(row) is not str or len(row) != 64 or any(char not in "0123456789abcdef" for char in row) for row in rows):
            raise ValueError("curator_quota_records_invalid")
    day = quota_date()
    if value.get("date") != day:
        rollover = {"quota_rollover": value} if value.get("date") else {}
        if value.get("schema_version") == _QUOTA_SCHEMA:
            return {**value, "date": day, "count": 0, "accounted_records": [], **rollover}
        return {"date": day, "count": 0, **rollover}
    return value or {"date": day, "count": 0}


# LLM: 所有模式共用原文件锁与原date/count；额外读取只用于保留已经存在的v2事实，不丢去重身份。
# 函数用途: 取得当日已消费次数。
def read_quota_count(path):
    if current_worker_partition() is not None:
        raise ValueError("curator_quota_coordinator_required")
    with locked_json_path(path):
        return _read_quota(Path(path))["count"]


# LLM: 一次原读取冻结UTC日；跨零点不能打开昨日分片后再按今日过滤，公开签名和原owner/run去重不变。
# 函数用途: 读取同一UTC日的真实final身份，显式日期私有叶供迟到结清复用。
def owner_consumed_records(home_root, owner, *, run_id=None):
    return _owner_records_for_date(home_root, owner, quota_date(), run_id=run_id)


# LLM: day是受信会计冻结的UTC分片，不由请求选路径；原no-follow/严格schema和重复final拒绝保持。
# 函数用途: 为原公开读口和跨日重放读取准确一天的原审计。
def _owner_records_for_date(home_root, owner, day, *, run_id=None):
    if type(day) is not str or datetime.fromisoformat(day).date().isoformat() != day:
        raise ValueError("curator_quota_date_invalid")
    home = _owner_home_dir(Path(home_root), owner)
    relative = ("memory", "curator", "runs", day + ".jsonl")
    try:
        descriptor = open_readonly_file_beneath(home, relative)
    except FileNotFoundError:
        return set()
    found, run_ids = set(), set()
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                raise ValueError("curator_quota_audit_invalid")
            record = CuratorRunRecord.from_record(load_strict_json(line))
            if record.phase == "final" and record.status in {"succeeded", "failed"} and record.finished_at[:10] == day and (run_id is None or record.run_id == run_id):
                if record.run_id in run_ids:
                    raise ValueError("curator_quota_run_binding_ambiguous")
                run_ids.add(record.run_id)
                identity = json.dumps({"owner": asdict(owner), "run_id": record.run_id}, sort_keys=True, separators=(",", ":"))
                found.add(hashlib.sha256(identity.encode()).hexdigest())
    return found


# LLM: 分页必须来自同一原目录版本；不存在的bucket是合法空集，权限/符号链接不能由旧投影的-1吞成空。
# 函数用途: 核对原owner枚举目录可安全读取并取得版本指纹。
def _inventory_version(home_root):
    try:
        descriptor = open_directory_beneath(home_root, ("owners", "providers"), require_dir_fd=True)
    except FileNotFoundError:
        return ()
    os.close(descriptor)
    version = _owner_home_signature(Path(home_root) / "owners" / "providers")
    for provider, kind, _signature in version:
        # 签名区分规范本地用户与旧位置；legacy-users是签名种类，不是目录名。
        bucket = {"user": "users", "group": "groups", "legacy-users": "users"}.get(kind)
        if bucket is None:
            raise ValueError("curator_quota_owner_inventory_invalid")
        relative = (("owners", "local") if (provider, kind) == ("local", "user")
                    else ("owners", "providers", provider, bucket))
        try:
            descriptor = open_directory_beneath(home_root, relative, require_dir_fd=True)
        except FileNotFoundError:
            continue
        os.close(descriptor)
    return version


# LLM: 显式首次v2迁移只读原日审计全集；保旧count且既有记录不会在模式切换后再加一次。
# 函数用途: 收集同一canonical home中所有现存owner的实际当日消耗。
def _all_consumed_records(home_root):
    version = _inventory_version(home_root)
    records = owner_consumed_records(home_root, OwnerIdentity.local_main())
    cursor = None
    while True:
        page = discover_owner_home_page(Path(home_root) / "owners", limit=64, after_cursor=cursor)
        for target in page.targets:
            records.update(owner_consumed_records(home_root, target.identity))
        cursor = page.next_cursor
        if cursor is None:
            if _inventory_version(home_root) != version:
                raise ValueError("curator_quota_owner_inventory_changed")
            return records


# LLM: 原C短门/quota锁保序；实际写点另拒独立executor与W，actual audit同步补账，不借无W分区自授全局写。
# 函数用途: 在同一原文件内合并实际运行，第一次迁移保旧count且后续不重扣。
def _reconcile_unlocked(path, home_root, owners=None):
    require_global_writer("curator_quota")
    value = _read_quota(path)
    previous = dict(value)
    records = (_all_consumed_records(home_root) if owners is None or value.get("schema_version") != _QUOTA_SCHEMA
               else set().union(*(owner_consumed_records(home_root, owner) for owner in owners)))
    if value.get("schema_version") != _QUOTA_SCHEMA:
        value = {"schema_version": _QUOTA_SCHEMA, "date": quota_date(),
                 "count": max(value["count"], len(records)), "accounted_records": sorted(records),
                 "migration": {"from": "legacy-date-count", "preserved_count": value["count"]}}
    else:
        existing = set(value["accounted_records"])
        value["count"] += len(records - existing)
        value["accounted_records"] = sorted(existing | records)
    if value != previous:
        write_json_file_atomic_unlocked(path, value)
    return value


# LLM: 原冻结入口仍消费同Root末门；原审计固定批读取先在热门外完成，callback里只复核CAS及写唯一quota。
# 函数用途: 按给定活跃owner准备增量核算，不再从该入口耗尽注册用户。
def reconcile_quota(control, owners, *, commit_transition=None):
    controller = control.controller()
    if controller is None:
        raise ValueError("curator_quota_coordinator_required")
    return _prepare_quota_step(controller, control.home_root, owners, commit_transition=commit_transition)


# LLM: 真实读取不持T/controller/quota锁；原短提交中的单次cell仍拒fork/异线程/复用/退出后执行。
# 函数用途: 分离有界来源准备与原全局会计的短CAS落盘。
def _prepare_quota_step(controller, home_root, owners, *, commit_transition=None):
    from . import curator_quota_index as index

    home_root = Path(home_root)
    path = home_root / "global_index" / "curator_daily_quota.json"
    with locked_json_path(path):
        previous = _read_quota(path)
    day = previous["date"]
    candidate = index.prepare_batch(previous, home_root, day, tuple(owners), controller=controller)
    return _commit_quota_candidate(controller, path, previous, candidate, commit_transition=commit_transition)


# LLM: 候选只由当前门外原来源读取产生；原窗口限同PID/thread一次消费，跨日或原会计CAS变化必须重新准备。
# 函数用途: 共用原C短门提交迁移进度和迟到完成准备，不在锁内读取历史。
def _commit_quota_candidate(controller, path, previous, candidate, *, commit_transition=None):
    day = previous["date"]
    committed = []
    window = [True, os.getpid(), threading.get_ident(), False]

    # LLM: 本叶只接收当前调用已核的一批候选，锁内不翻页、不读取Owner历史；全局文件变更或跨零点要求重试。
    # 函数用途: 在原短门和原quota锁内原子提交这批身份、总数与游标。
    def update():
        if not window[0] or window[1:3] != [os.getpid(), threading.get_ident()] or window[3]:
            raise ValueError("curator_quota_commit_window_required")
        window[3] = True
        require_global_writer("curator_quota")
        with locked_json_path(path):
            if quota_date() != day or _read_quota(path) != previous:
                raise ValueError("curator_quota_preparation_changed")
            if candidate != previous:
                _write_quota_durable(path, candidate)
            else:
                _sync_quota(path)
            committed.append(candidate["count"])

    try:
        if commit_transition is None:
            decision = controller.launch_owner_writer(owner_digest(OwnerIdentity.local_main()), update)
            if decision.get("status") != "started":
                raise ValueError("curator_quota_coordinator_required")
        else:
            commit_transition(update)
        if not committed:
            raise ValueError("curator_quota_coordinator_required")
        return committed[0]
    finally:
        window[0] = False


# LLM: Count0原写者保v2字段；创建锁前拒W/独立executor，实际run扣费同原审计，不以None分区当许可。
# 函数用途: 原默认模式记录一次已实际消费的策展，不能抹掉多进程已核身份。
def record_legacy_consumption(path, *, home_root=None, owner=None, run_id=None):
    if current_worker_partition() is not None:
        raise ValueError("curator_quota_coordinator_required")
    require_global_writer("curator_quota")
    with locked_json_path(path):
        value = _read_quota(Path(path))
        if value.get("schema_version") == _QUOTA_SCHEMA and run_id:
            records = owner_consumed_records(home_root, owner, run_id=run_id)
            if not records:
                raise ValueError("curator_quota_actual_audit_required")
            from . import curator_quota_index as index

            index.merge_facts(value, records, actual=False)
        else:
            value["count"] += 1
        _write_quota_durable(Path(path), value)
        return value["count"]


# LLM: 原occupied P3与原request完整route/binding必须同源；漂移/未知拒绝会计，pending/closing保责任，业务源sha变化不抹占用。
# 函数用途: 统计尚未释放的原策展请求，不另建quota reservation或扫描计数缓存。
def unsettled_curator_requests(paths):
    from .multiprocess_admission import (
        _UNREADABLE,
        _read_reservation,
        reservations_dir,
        verify_claimable_reservation,
    )
    from .multiprocess_coordinator import coordinator_for_root
    from .multiprocess_executor_active_index import current_index
    from .multiprocess_route import _request_binding

    index = current_index(paths)
    if index is None:
        lease = coordinator_for_root(paths.root)
        if lease is not None and lease.controller is not None:
            raise ValueError("curator_quota_responsibility_index_unknown")
        rows = (_read_reservation(path) for path in sorted(reservations_dir(paths).glob("*.json")))
    else:
        rows = index.active_p3()
    count = 0
    for row in rows:
        if row is _UNREADABLE or row is None:
            raise ValueError("curator_quota_request_unreadable")
        if row["state"] != "occupied":
            continue
        payload = _occupied_request(paths, row["request_id"])
        if not verify_claimable_reservation(payload, row)["ok"] or _request_binding(payload) != row["route"]["binding"]:
            raise ValueError("curator_quota_request_binding_unknown")
        if payload.get("kind") == "background":
            source = payload.get("metadata", {}).get("background_source")
            if type(source) is not dict:
                raise ValueError("curator_quota_request_unreadable")
            count += int(source.get("kind") == "curator")
    return count


# LLM: 未结请求可在原队列/终态投影之间移动；找不到或坏文件保unknown，不把目录间隙当没有占用。
# 函数用途: 从原request ID读取尚未释放预留所绑定的请求载荷。
def _occupied_request(paths, request_id):
    for folder in (paths.inbox, paths.processing, paths.terminal, paths.done, paths.failed):
        relative = folder.relative_to(paths.root).parts + (request_id + ".json",)
        raw = read_bytes_beneath(paths.root, relative, require_dir_fd=True)
        if raw is None:
            continue
        value = load_strict_json(raw)
        if type(value) is not dict or value.get("id") != request_id:
            raise ValueError("curator_quota_request_unreadable")
        return value
    raise ValueError("curator_quota_request_unreadable")


# LLM: 原P3最终短门持quota锁直到预留落盘；pending/lease保原豁免，typed源漂移只准原P5结运输，绝不运行新来源；cold control通过纯typed路径投影读取，不伪装Agent。
# 函数用途: 在实际准入边界检查策展额度，跨线程并发不能把同一剩余额度重复授予。
@contextmanager
def curator_quota_admission(gate, request):
    from .multiprocess_background_request import BackgroundSourceChanged, read_source

    ref = request.get("metadata", {}).get("background_source")
    if request.get("kind") != "background" or type(ref) is not dict or ref.get("kind") != "curator":
        yield None
        return
    home, config = _quota_host_paths(gate.agent)
    try:
        state = read_source(home.root, ref)
    except BackgroundSourceChanged:
        yield None
        return
    except (OSError, ValueError, TypeError) as error:
        raise CuratorQuotaUnavailable("curator_quota_source_unavailable") from error
    if state.get("pending_reasons") or state.get("active_lease"):
        yield None
        return
    path = home.global_index_dir / "curator_daily_quota.json"
    limit = getattr(config, "curator_daily_quota", CURATOR_DAILY_QUOTA_DEFAULT_COUNT)
    if type(limit) is not int or limit <= 0:
        limit = CURATOR_DAILY_QUOTA_DEFAULT_COUNT
    guards = ExitStack()
    try:
        guards.enter_context(locked_json_path(path))
        from . import curator_quota_index as index
        from .multiprocess_background_request import source_owner

        value = _read_quota(path)
        if not index.ready(value, home.root, value["date"]):
            raise ValueError("curator_quota_migration_pending")
        index.owner_facts(value, home.root, source_owner(ref["owner"]), value["date"])
        pending = unsettled_curator_requests(gate.paths)
        rejection = "curator_daily_quota_exhausted" if value["count"] + pending >= limit else None
    except (OSError, ValueError, TypeError) as error:
        guards.close()
        raise CuratorQuotaUnavailable("curator_quota_unavailable") from error
    try:
        yield rejection
    finally:
        guards.close()


# LLM: 纯路径投影不创建文件、不检查/授予P2；实际来源和短提交仍由原gate/controller/lease负责，普通Agent沿原home/config。
# 函数用途: 从真实ControlPlane或原Agent取得同一全局quota根，不添加兼容属性或第二配置。
def _quota_host_paths(host):
    from ..user_space.home_layout import home_paths
    from .control_plane import GatewayControlPlane

    if type(host) is GatewayControlPlane:
        return home_paths(host.home_root), host.config
    return host.home_paths, host.config


# LLM: 原quota唯一原子写者保留；文件和目录都fsync，rename后故障的重放也必须补耐久，不能仅看事实键存在。
# 函数用途: 先确认完成事实与总数都耐久，才允许调用者释放P3。
def _write_quota_durable(path, value):
    if value.get("quota_rollover") is not None:
        value = dict(value)
        _archive_quota_day(path.parent, value.pop("quota_rollover"))
    write_json_file_atomic_unlocked(path, value)
    _sync_quota(path)


# LLM: 只同步既有准确quota，不创建替身记录；读源仍沿原no-follow根，异常必须向上传递。
# 函数用途: 幂等补齐先前rename成功但fsync失败的会计提交。
def _sync_quota(path):
    descriptor = open_readonly_file_beneath(path.parent, (path.name,))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# LLM: 原豁免在锁和quota文件物化前判断，源变化仍交原P5处理；只读准备不能给未核输入签运行许可。
# 函数用途: 在进入admit的T门前推进一批迁移，稳态只核当前准确Owner。
def prepare_quota_admission(gate, request):
    from . import curator_quota_index as index
    from .multiprocess_background_request import BackgroundSourceChanged, read_source, source_owner

    ref = request.get("metadata", {}).get("background_source")
    if request.get("kind") != "background" or type(ref) is not dict or ref.get("kind") != "curator":
        return True
    home, _config = _quota_host_paths(gate.agent)
    try:
        state = read_source(home.root, ref)
    except BackgroundSourceChanged:
        return True
    if state.get("pending_reasons") or state.get("active_lease"):
        return True
    owner = source_owner(ref["owner"])
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        raise ValueError("curator_quota_coordinator_required")
    _prepare_quota_step(controller, home.root, (owner,))
    path = home.global_index_dir / "curator_daily_quota.json"
    with locked_json_path(path):
        value = _read_quota(path)
        if not index.ready(value, home.root, value["date"]):
            return False
        index.owner_facts(value, home.root, owner, value["date"])
    return True


# LLM: 只有规范terminal给出的准确run_id才需要结清；普通失败/disabled/not_due没有实际运行，不伪造消耗。
# 函数用途: 从原终态冻结Owner和实际策展run定位。
def _terminal_curator_run(terminal):
    from .multiprocess_background_request import source_owner, validate_source_ref

    ref = terminal.get("metadata", {}).get("background_source")
    if terminal.get("kind") != "background" or type(ref) is not dict or ref.get("kind") != "curator":
        return None
    validate_source_ref(ref)
    result = terminal.get("terminal_response", {}).get("background_result")
    if type(result) is not dict or result.get("status") not in {"succeeded", "failed"}:
        return None
    run_id = result.get("run_id")
    if type(run_id) is not str or not run_id:
        raise ValueError("curator_quota_actual_run_required")
    return source_owner(ref["owner"]), run_id


# LLM: 调用时尚未持T/controller；这里只准备准确原日账，不将未校验terminal当release许可。
# 函数用途: 为原规范释放准备有界完成核算，失败保留原P3。
def prepare_quota_release(gate, terminal):
    from . import curator_quota_index as index

    original = _terminal_curator_run(terminal)
    if original is None:
        return True
    owner, run_id = original
    home, _config = _quota_host_paths(gate.agent)
    controller = getattr(gate, "_runtime_takeover", None)
    if controller is None:
        raise ValueError("curator_quota_coordinator_required")
    _prepare_quota_step(controller, home.root, (owner,))
    path = home.global_index_dir / "curator_daily_quota.json"
    with locked_json_path(path):
        value = _read_quota(path)
        if not index.ready(value, home.root, value["date"]):
            return False
    candidate, complete = index.prepare_completion(value, home.root, owner, run_id, terminal)
    _commit_quota_candidate(controller, path, value, candidate)
    return complete


# LLM: 原release_admission已在同T门核规范终态；原C/lease末门内再次核Owner、原审计版本，事实和总数原子幂等落盘。
# 函数用途: 在原P3写released之前完成准确run的耐久计账。
def account_completed_terminal(gate, record, terminal):
    from . import curator_quota_index as index

    original = _terminal_curator_run(terminal)
    if original is None:
        return
    owner, run_id = original
    if owner_digest(owner) != record["owner_sha"] or terminal.get("route") != record["route"]:
        raise ValueError("curator_quota_terminal_binding_conflict")
    home, _config = _quota_host_paths(gate.agent)
    path = home.global_index_dir / "curator_daily_quota.json"
    require_global_writer("curator_quota")
    with locked_json_path(path):
        value = _read_quota(path)
        binding = index.completion_binding(terminal, owner, run_id)
        receipt = value.get("quota_completion_receipts", {}).get(binding)
        if receipt is None:
            raise ValueError("curator_quota_completion_pending")
        fact = index.validate_completion(receipt, home.root, owner, run_id, terminal)
        if value.get("quota_rollover") is not None:
            _archive_quota_day(path.parent, value.pop("quota_rollover"))
        if receipt["day"] == value["date"]:
            index.merge_facts(value, (fact["identity"],), actual=True)
        else:
            history_path = _history_path(path.parent, receipt["day"])
            history = _read_history(history_path, receipt["day"])
            index.merge_facts(history, (fact["identity"],), actual=True)
            history_path.parent.mkdir(parents=True, exist_ok=True)
            _write_quota_durable(history_path, history)
        del value["quota_completion_receipts"][binding]
        _write_quota_durable(path, value)


# LLM: 仅规范UTC日期选择原全局会计的历史日分片；当前日仍只有curator_daily_quota这一权威，不存第二个当前总数。
# 函数用途: 给迟到结清和日切保留准确一天的唯一历史会计。
def _history_path(global_index, day):
    if type(day) is not str or datetime.fromisoformat(day).date().isoformat() != day:
        raise ValueError("curator_quota_date_invalid")
    return Path(global_index) / "curator_quota_history" / (day + ".json")


# LLM: 历史文件只在原全局quota锁内读取；原严格schema/身份不放宽，缺分片是尚无已计消费而非原审计不存在。
# 函数用途: 读取指定完成日的去重事实与旧累计下限。
def _read_history(path, day):
    raw = read_bytes_beneath(path.parent, (path.name,), require_dir_fd=True)
    value = ({"schema_version": _QUOTA_SCHEMA, "date": day, "count": 0, "accounted_records": []}
             if raw is None else load_strict_json(raw))
    if (type(value) is not dict or value.get("date") != day or type(value.get("count")) is not int
            or value["count"] < 0 or value.get("schema_version", _QUOTA_SCHEMA) != _QUOTA_SCHEMA):
        raise ValueError("curator_quota_history_invalid")
    records = value.get("accounted_records", [])
    if (type(records) is not list or any(type(row) is not str or len(row) != 64
            or any(char not in "0123456789abcdef" for char in row) for row in records)
            or len(set(records)) != len(records)):
        raise ValueError("curator_quota_history_invalid")
    return {**value, "schema_version": _QUOTA_SCHEMA, "accounted_records": records}


# LLM: 先耐久旧日分片再覆盖当前日；崩溃时旧main仍可重放，已含迟到事实的历史不能被旧快照覆盖或减账。
# 函数用途: 原会计日切时保留旧日总数和事实，释放流程仍用同一原锁串行。
def _archive_quota_day(global_index, value):
    path = _history_path(global_index, value["date"])
    previous = read_bytes_beneath(global_index, ("curator_quota_history", path.name), require_dir_fd=True)
    if previous is not None:
        saved = _read_history(path, value["date"])
        if (saved["count"] < value["count"]
                or not set(value.get("accounted_records", [])) <= set(saved["accounted_records"])):
            raise ValueError("curator_quota_history_conflict")
        _sync_quota(path)
        return
    archive = {key: item for key, item in value.items()
               if key not in {"quota_rollover", "quota_pending_completions", "quota_completion_receipts"}}
    archive.setdefault("schema_version", _QUOTA_SCHEMA)
    archive.setdefault("accounted_records", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_quota_durable(path, archive)
