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
    if value.get("date") != quota_date():
        if value.get("schema_version") == _QUOTA_SCHEMA:
            return {**value, "date": quota_date(), "count": 0, "accounted_records": []}
        return {"date": quota_date(), "count": 0}
    return value or {"date": quota_date(), "count": 0}


# LLM: 所有模式共用原文件锁与原date/count；额外读取只用于保留已经存在的v2事实，不丢去重身份。
# 函数用途: 取得当日已消费次数。
def read_quota_count(path):
    if current_worker_partition() is not None:
        raise ValueError("curator_quota_coordinator_required")
    with locked_json_path(path):
        return _read_quota(Path(path))["count"]


# LLM: C从原owner固定run分片读取实际final审计；单行必须通过原严格schema，缺分片才是零，损坏/权限拒绝不是零。
# 函数用途: 只读提取当前owner当日真正succeeded/failed运行的稳定会计身份。
def owner_consumed_records(home_root, owner, *, run_id=None):
    home = _owner_home_dir(Path(home_root), owner)
    relative = ("memory", "curator", "runs", quota_date() + ".jsonl")
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
            if record.phase == "final" and record.status in {"succeeded", "failed"} and record.finished_at[:10] == quota_date() and (run_id is None or record.run_id == run_id):
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
        relative = ("owners", "providers", provider, {"user": "users", "group": "groups"}[kind])
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


# LLM: 原quota RMW/账不变；实际叶只在本次同PID/thread同步窗口消费一次并finally失效，producer另守同Root末门，默认None保controller/额度锁。
# 函数用途: 合并实际完成身份，并让当前C关闭栅栏覆盖这次原全局会计写。
def reconcile_quota(control, owners, *, commit_transition=None):
    controller = control.controller()
    if controller is None:
        raise ValueError("curator_quota_coordinator_required")
    path = control.home_root / "global_index" / "curator_daily_quota.json"
    committed = []
    window = [True, os.getpid(), threading.get_ident(), False]

    # LLM: 原controller/Root同步消费本次cell；裸叶在任何锁或写前拒异PID/thread、重复及退出后的调用，不接受W回调或模型正文。
    # 函数用途: 在原全局会计锁内合并实际完成身份，防止缓存裸核算叶绕过关闭门。
    def update():
        if not window[0] or window[1:3] != [os.getpid(), threading.get_ident()] or window[3]:
            raise ValueError("curator_quota_commit_window_required")
        window[3] = True
        require_global_writer("curator_quota")
        with locked_json_path(path):
            committed.append(_reconcile_unlocked(path, control.home_root, owners)["count"])

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
            existing = set(value["accounted_records"])
            value["count"] += len(records - existing)
            value["accounted_records"] = sorted(existing | records)
        else:
            value["count"] += 1
        write_json_file_atomic_unlocked(Path(path), value)
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
    from .multiprocess_route import _request_binding

    count = 0
    for path in sorted(reservations_dir(paths).glob("*.json")):
        row = _read_reservation(path)
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
        value = _reconcile_unlocked(path, home.root)
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
