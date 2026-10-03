# LLM: 这里只保存有界的冷却摘要（含连续失败次数）、在途取消索引和宿主关闭标记，不是 worker/资源池；实际退出仍由
#   bounded_call 跟踪。冷却表可经 restore/persist_cooldown_snapshot 落盘到 owner 数据域（自学习状态文件旁），
#   让 CLI 新进程与重启后的 Gateway 沿用上一进程的退避，不再按进程清零；文件只存该 owner 的键、有字节上限，
#   过期条目在加载时清除。
# 模块用途: 隔离决策连接故障并按连续失败逐步加长冷却，同时将同进程设置撤销与宿主关闭转交给原精确中断句柄。
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from ..backends.errors import (
    ProviderConfigurationError,
    ProviderQuotaExhaustedError,
    ProviderUsageLimitError,
)
from ..concurrency.interrupt import InterruptHandle
from ..settings.decision_settings_defaults import decision_defaults
from ..settings.model_profiles import model_profiles_path

_LOCK = threading.Lock()
_SALT = secrets.token_bytes(32)
# 决策模型单连接最多记 512 次失败：防失败计数无限累积。
_MAX_FAILURES_COUNT = 512
# 决策模型同一连接最大活跃 256 条：防止失控并发。
_MAX_ACTIVE_COUNT = 256
# 瞬时失败与超时的首次冷却；同一连接冷却过后再次失败时翻倍，封顶与额度冷却相同。
_BASE_COOLDOWN_SECONDS = 30.0
# 决策模型冷却封顶 300 秒（5 分钟）：失败后最长冷却 5 分钟。
_MAX_COOLDOWN_SECONDS = 300.0
# 值为 (状态, 设置修订, 冷却截止或 None, 连续失败次数)；冷却过期后保留次数，成功或显式重试才清除。
_FAILURES: OrderedDict[tuple[str, ...], tuple[str, str, float | None, int]] = OrderedDict()
_ACTIVE: dict[str, ActiveDecision] = {}
# 设置通知的提交代次：每处理完一次通知就 +1。整套标记在锁外比路由，回来后靠它判断期间有没有更新的通知插进来。
_action_generation = 0
# 整套标记的重算上限：并发通知持续插入时不能无限重算，超过就按“已改变”整批撤销（宁严勿松）。
_MAX_MARK_ATTEMPTS = 3
# 宿主进程开始收尾后置位且不复位：此后不再登记新的在途决策，进程随后退出。
_HOST_SHUTDOWN = False


# LLM: 只读可信 owner 的原路径并哈希，不接受请求指定 owner，不公开路径或凭据。
# 函数用途: 生成同进程设置通知和冷却使用的稳定归属摘要。
def decision_owner_ref(context: object) -> str:
    return hashlib.sha256(str(model_profiles_path(context.home_paths).resolve()).encode()).hexdigest()


# LLM: 进程随机盐的 HMAC 只供版本对比；缓存不保存 api_key、自定义头或完整连接。
# 函数用途: 对原连接快照生成不可直接反查凭据的进程内版本摘要。
def connection_revision(config: dict) -> str:
    raw = json.dumps(config, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return hmac.new(_SALT, raw, hashlib.sha256).hexdigest()


# LLM: 在途引用只活到 caller 收口，不能据此释放存活 worker 的槽位；上下文不复制到持久文件。
#   nonblocking 表示这次 observe 调用交给后台执行器（decision_observe_nonblocking），排队期间也在登记里，
#   设置撤销与宿主关闭照样能标记它；决策服务据此给它独立的有界调用资源键，不和同会话的同步调用互相占位。
# 类用途: 记录应通知的准确 owner/thread/point 和一次性取消句柄。
@dataclass
class ActiveDecision:
    owner_ref: str
    thread_id: str
    point: str
    handle: InterruptHandle
    context: object = field(repr=False)
    settings: dict = field(repr=False)
    settings_cancelled: bool = False
    shutdown_cancelled: bool = False
    nonblocking: bool = False


# LLM: 索引满或宿主已开始关闭都只拒绝本次可选增强，不等待或建立另一池；关闭标记与登记同锁判断，不留竞态窗口。
#   caller 退出必须注销，不替 bounded_call 回收资源。
# 函数用途: 发布一个准备发送的取消目标，覆盖关闭发生在 worker 注册前的窗口。
def register_active(key: str, active: ActiveDecision) -> bool:
    with _LOCK:
        if _HOST_SHUTDOWN or key in _ACTIVE or len(_ACTIVE) >= _MAX_ACTIVE_COUNT:
            return False
        _ACTIVE[key] = active
        return True


# LLM: 宿主进程收尾（Gateway 停止）时调用：先在索引锁内置位关闭标记并标记全部在途决策，再在锁外取消各自句柄。
#   被取消的调用只使建议失效（stale/host_shutdown），不冒充用户停止、不进入连接冷却；worker 仍沿 bounded_call 自行退出，
#   调用账按原取消路径记 DECISION_CANCELLED。调用方：cli/gateway_process 的停止收尾。
# 函数用途: 让正在等待决策模型的前台/后台调用立即回到原方案，并拒绝之后的新决策；返回本次取消的在途数量。
def cancel_active_decisions_for_shutdown() -> int:
    global _HOST_SHUTDOWN
    with _LOCK:
        _HOST_SHUTDOWN = True
        targets = tuple(_ACTIVE.values())
        for active in targets:
            active.shutdown_cancelled = True
    for active in targets:
        active.handle.cancel()
    return len(targets)


# LLM: 只读进程关闭标记；决策服务据此把登记失败区分为"宿主关闭中"与"索引已满"两种固定原因。
# 函数用途: 判断宿主是否已开始关闭、不再接受新的决策请求。
def host_shutdown_started() -> bool:
    with _LOCK:
        return _HOST_SHUTDOWN


# LLM: 只移除同一个在途通知对象；实际 worker 可能仍存活，不触碰其原保留表。
# 函数用途: 在 caller 的 finally 中清理临时设置通知索引。
def unregister_active(key: str, active: ActiveDecision) -> None:
    with _LOCK:
        if _ACTIVE.get(key) is active:
            del _ACTIVE[key]


# LLM: 键是连接键 (owner, profile, 连接摘要)，或在其后加点位名的点位键（超时只冷却本点位，见 decision_service._failure_key）。
#   显式 retry 清除一个准确键的冷却及连续失败次数；配置错误在配置修订改变前不热循环重发。
# 冷却过期只放行本次尝试，不删记录：下一次失败要据此加长冷却，成功才由 record_success 清除。
# 函数用途: 返回当前连接或点位的阻塞原因和剩余冷却秒数，未知键不受其他 owner 故障影响。
def cooldown_state(key: tuple[str, ...], revision: str, *, retry: bool = False) -> tuple[str, float]:
    with _LOCK:
        item = _FAILURES.get(key)
        if item is None:
            return "", 0.0
        status, old_revision, until, _streak = item
        if retry or until is None and revision != old_revision:
            _FAILURES.pop(key, None)
            return "", 0.0
        remaining = max(0.0, until - time.monotonic()) if until is not None else 0.0
        if until is not None and remaining <= 0:
            return "", 0.0
        _FAILURES.move_to_end(key)
        return status, remaining


# LLM: 只按异常类型分类，不能解析中文/服务商正文；不改变主 LLM 配置或其重试策略。
# 冷却期内返回的并发失败属于同一次故障，不加长阶梯；只有冷却过期后的重试再失败才翻倍。
# 函数用途: 记录一个连接的配置等待或有界冷却：额度 300 秒，其他故障 30 秒起、连续失败翻倍、封顶 300 秒。
def record_failure(key: tuple[str, ...], revision: str, error: Exception) -> str:
    if isinstance(error, ProviderConfigurationError):
        with _LOCK:
            _store_failure(key, ("configuration_required", revision, None, 0))
        return "configuration_required"
    quota = isinstance(error, (ProviderQuotaExhaustedError, ProviderUsageLimitError))
    with _LOCK:
        streak, until = _next_cooldown(_FAILURES.get(key), time.monotonic(), quota)
        _store_failure(key, ("cooldown", revision, until, streak))
    return "cooldown"


# LLM: 调用方持有 _LOCK；配置等待不计入阶梯，额度固定 300 秒但仍累计连续失败次数。
# 函数用途: 按上一条记录算出本次失败后的连续次数和冷却截止时刻。
def _next_cooldown(previous: tuple | None, now: float, quota: bool) -> tuple[int, float]:
    cooling = previous is not None and previous[0] == "cooldown" and previous[2] is not None
    if cooling and previous[2] > now:
        return previous[3], max(previous[2], now + _MAX_COOLDOWN_SECONDS) if quota else previous[2]
    streak = previous[3] + 1 if cooling else 1
    delay = _MAX_COOLDOWN_SECONDS if quota else _BASE_COOLDOWN_SECONDS * 2 ** min(streak - 1, 4)
    return streak, now + min(delay, _MAX_COOLDOWN_SECONDS)


# LLM: 调用方持有 _LOCK；表有界，满时淘汰最久未用的连接记录，淘汰只会让该连接从 30 秒重新计起。
# 函数用途: 写入一条连接失败记录并维持表的上限。
def _store_failure(key: tuple[str, ...], item: tuple[str, str, float | None, int]) -> None:
    _FAILURES[key] = item
    _FAILURES.move_to_end(key)
    while len(_FAILURES) > _MAX_FAILURES_COUNT:
        _FAILURES.popitem(last=False)


# LLM: 连接真实返回过响应即清除该连接的冷却和连续失败次数；响应之后的绑定/期限复核失败不属于连接故障。
# 函数用途: 在决策调用成功返回后复位这个连接的退避阶梯。
def record_success(key: tuple[str, ...]) -> None:
    with _LOCK:
        _FAILURES.pop(key, None)


COOLDOWN_FILE_SCHEMA = "decision-cooldown.v1"
# 文件字节上限：进程内表最多 512 条 × 单条约 60 字节的留量，超限拒绝读写，退避回退到进程内。
MAX_COOLDOWN_FILE_BYTES = 32 * 1024


# LLM: 只处理本 owner 的键（键首元是 decision_owner_ref 哈希）；monotonic 时间跨进程无效，落盘前换算成墙上时间，
#   加载时再换回本进程 monotonic。过期条目（截止已过）在加载时直接丢弃（可清理），configuration_required 行
#   （until=None）保留但只在配置修订未变时生效。坏文件、超限或 schema/owner 不符按 0 处理，绝不因此影响决策主链路。
# 函数用途: 把 owner 冷却文件里的退避记录合并进进程内表，返回加载条数。
def restore_cooldown_snapshot(path: Path, owner_ref: str) -> int:
    payload = _read_cooldown_payload(path, owner_ref)
    if payload is None:
        return 0
    now_wall, now_mono = time.time(), time.monotonic()
    with _LOCK:
        return _restore_entries_locked(payload, owner_ref, now_wall, now_mono)


# LLM: 读文件与 schema/owner 校验集中在这里；任何一步失败都返回 None，调用方按 0 处理，不让坏文件影响主链路。
# 函数用途: 读取并校验冷却文件，返回解析后的 payload 或 None。
def _read_cooldown_payload(path: Path, owner_ref: str) -> dict | None:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_COOLDOWN_FILE_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != COOLDOWN_FILE_SCHEMA \
            or str(payload.get("owner") or "") != owner_ref:
        return None
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return None
    return payload


# LLM: 调用方持有 _LOCK；每条记录单独解析与校验，坏条目跳过、其余按剩余冷却换算回本进程 monotonic。
# 函数用途: 在锁内把文件里的条目合入进程内表并计数。
def _restore_entries_locked(payload: dict, owner_ref: str, now_wall: float, now_mono: float) -> int:
    restored = 0
    for key_text, item in payload.get("entries").items():
        entry = _cooldown_entry(key_text, item, owner_ref, (now_wall, now_mono))
        if entry is None:
            continue
        key, status, revision, until, streak = entry
        _store_failure(key, (status, revision, until, streak))
        restored += 1
    return restored


# LLM: 一条记录必须是本 owner 的键且未过期；键 JSON 或字段不合规返回 None。now 收成二元组保持参数在 4 个以内。
# 函数用途: 解析并校验单条冷却快照，返回可合入进程内表的条目或 None。
def _cooldown_entry(
    key_text: object,
    item: object,
    owner_ref: str,
    now: tuple[float, float],
) -> tuple[tuple[str, ...], str, str, float | None, int] | None:
    try:
        key = tuple(json.loads(str(key_text)))
        status, revision, until_wall, streak = _snapshot_item(item, key)
    except (ValueError, TypeError):
        return None
    now_wall, now_mono = now
    if not key or key[0] != owner_ref or (until_wall is not None and until_wall <= now_wall):
        return None
    until = now_mono + max(0.0, until_wall - now_wall) if until_wall is not None else None
    return key, status, revision, until, streak


# LLM: 调用方持锁读表；只写本 owner 的键，直到期换算成墙上时间；原子替换防止半截文件被下次读取。写失败返回 0，
#   冷却丢失只是少一次退避，安全方向，不阻断决策。
# 函数用途: 把进程内表中本 owner 的退避记录原子写入冷却文件，返回写入条数。
def persist_cooldown_snapshot(path: Path, owner_ref: str) -> int:
    now_wall, now_mono = time.time(), time.monotonic()
    with _LOCK:
        entries = _collect_owner_entries(owner_ref, now_wall, now_mono)
    if not entries:
        return 0
    return _write_cooldown_payload(path, owner_ref, now_wall, entries)


# LLM: 调用方持有 _LOCK；只取本 owner 的键，条目形状与进程内表一致，键序列化保持稳定排序。
# 函数用途: 收集进程内表中本 owner 的全部退避记录，返回可写文件的 entries 字典。
def _collect_owner_entries(owner_ref: str, now_wall: float, now_mono: float) -> dict[str, list]:
    entries: dict[str, list] = {}
    for key, item in _FAILURES.items():
        if not key or key[0] != owner_ref:
            continue
        status, revision, until, streak = item
        until_wall = None if until is None else now_wall + max(0.0, until - now_mono)
        entries[json.dumps(key, sort_keys=True)] = [status, revision, until_wall, streak]
    return entries


# LLM: 正文带 schema 与 owner 标识；超字节上限不写；临时文件同目录原子替换，OSError 一律返回 0 不抛出。
# 函数用途: 把冷却 entries 序列化并原子写入冷却文件，返回写入条数。
def _write_cooldown_payload(path: Path, owner_ref: str, now_wall: float, entries: dict[str, list]) -> int:
    body = json.dumps({"schema": COOLDOWN_FILE_SCHEMA, "owner": owner_ref, "saved_at": now_wall,
                       "entries": entries}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(body) > MAX_COOLDOWN_FILE_BYTES:
        return 0
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_bytes(body)
        os.replace(temp, target)
    except OSError:
        return 0
    return len(entries)


# LLM: 单条快照形状固定为 [status, revision, until_wall|None, streak]；任何字段不合规都让整条跳过。
# 函数用途: 校验并解析一条冷却快照记录。
def _snapshot_item(item: object, key: tuple) -> tuple[str, str, float | None, int]:
    if not isinstance(item, (list, tuple)) or len(item) != 4:
        raise ValueError("invalid cooldown snapshot entry")
    status, revision = str(item[0]), str(item[1])
    until_wall = item[2]
    if until_wall is not None:
        until_wall = float(until_wall)
        if until_wall != until_wall:
            raise ValueError("invalid cooldown until")
    streak = int(item[3])
    if streak < 0 or not status or not revision or len(key) < 3:
        raise ValueError("invalid cooldown snapshot entry")
    return status, revision, until_wall, streak


# LLM: 比较有效开关/模式/绑定及 Skill/tool 展示策略；时间变化不延长旧请求，也不在通知中重置时钟。
# 函数用途: 从原模块默认与两层覆盖得到一个接入点的实际路由，供精准设置撤销比较。
def routing_signature(context: object, settings: dict, point: str) -> tuple:
    values, _ = decision_defaults(context)
    values.update(settings["overrides"]["owner"])
    values.update(settings["overrides"]["thread"])
    mode = values[f"points.{point}.mode"] if values["enabled"] else "off"
    routing = (mode, values.get(f"points.{point}.profile_id", values["profile_id"]))
    if point == "skill_tool":
        routing += (values["points.skill_tool.context_policy"], tuple(values["points.skill_tool.optional_categories"]))
    return routing


# LLM: 必须在 owner/thread 文件锁外调用；按提交后的覆盖重算有效值，reset 和 thread 遮蔽不能用字段名猜。
#   两步固定顺序：先把整套受影响请求的 settings_cancelled 与快照一次性标完，再到锁外逐个取消句柄。
#   标记本身不能被打断，否则同一批调用会按线程调度拿到不同结果码（没标到的先被后台 worker 处理，
#   _revoked 看不到撤销、_stale 只读到文件层 off，于是报 disabled 而不是 settings_changed）。
#   打标记的锁段里只做纯字典合并：路由比较会一路走到 decision_defaults，可能新建配置并读能力配置 YAML，
#   绝不能放在全进程的索引锁里——否则设置改动那一刻，所有在途决策线程（登记/注销/撤销检查）都要等它。
# 函数用途: 将实际失效的在途请求标为设置撤销并立即取消精确句柄，不取消其他点或其他用户。
def notify_decision_settings_changed(context: object, result: dict) -> None:
    owner = decision_owner_ref(context)
    scope, thread_id = result["scope"], result["thread_id"]
    for active in _mark_settings_batch(owner, scope, thread_id, result):
        active.handle.cancel()


# LLM: 整套标记的固定顺序：锁内收集快照与通知代次 → 锁外逐条比路由 → 再进锁复核代次后一次写完整批。
#   比路由期间若有更新的通知插进来（代次变了），整批重算；重算超过上限就按“已改变”整批撤销（宁严勿松），
#   绝不死循环。返回真正需要取消句柄的请求，没被影响的请求在锁内就已去掉。
# 函数用途: 在不把路由计算带进索引锁的前提下，一次标完整批受影响的在途请求。
def _mark_settings_batch(owner: str, scope: str, thread_id: str, result: dict) -> tuple[ActiveDecision, ...]:
    batch: tuple[tuple[ActiveDecision, dict, dict], ...] = ()
    for _attempt in range(_MAX_MARK_ATTEMPTS):
        batch, generation = _collect_batch(owner, scope, thread_id, result)
        if not batch:
            return ()
        if _try_commit_collected(batch, generation):
            return _batched_cancels(batch)
    # 重算用尽只说明并发通知太密：宁可把没复核到的请求也按“变了”整批撤销，也不留下可能受影响的在途请求。
    with _LOCK:
        return _commit_batch(batch, batch)


# LLM: 调用方不持锁；比路由必须在锁外。复核通过（代次和快照都还是收集时那份）就本轮提交并返回 True；
#   否则返回 False，交给调用方整批重算。
# 函数用途: 对已收集好的一批做锁外路由比较，通过则在锁内一次提交。
def _try_commit_collected(batch: tuple[tuple[ActiveDecision, dict, dict], ...], generation: int) -> bool:
    changed = tuple(item for item in batch if _routing_changed(item[0], item[1], item[2]))
    with _LOCK:
        if not _batch_still_current(batch, generation):
            return False
        _commit_batch(batch, changed)
        return True


# LLM: 调用方持 _LOCK；这套标记已经原地把 settings_cancelled 写好了，这里只按标记挑出要取消句柄的请求。
# 函数用途: 返回本批里被标成设置撤销的在途请求。
def _batched_cancels(batch: tuple[tuple[ActiveDecision, dict, dict], ...]) -> tuple[ActiveDecision, ...]:
    return tuple(active for active, _previous, _updated in batch if active.settings_cancelled)


# LLM: 调用方不持锁；自己在锁内收集，出锁时把当时的通知代次一并带出，供比完路由后复核。
# 函数用途: 在索引锁内收集本批受影响请求与各自新旧快照，返回它们和当时的通知代次。
def _collect_batch(
    owner: str,
    scope: str,
    thread_id: str,
    result: dict,
) -> tuple[tuple[tuple[ActiveDecision, dict, dict], ...], int]:
    with _LOCK:
        targets = tuple(row for row in _ACTIVE.values() if row.owner_ref == owner and (scope == "owner" or row.thread_id == thread_id))
        return _pending_advances(targets, result), _action_generation


# LLM: 调用方持 _LOCK；只要比路由期间有任何一条快照被换过、或通知代次变了，这一轮结论就作废，必须整批重算。
# 函数用途: 判断刚从锁外比完路由的这批结论是否仍然适用于当前索引状态。
def _batch_still_current(batch: tuple[tuple[ActiveDecision, dict, dict], ...], generation: int) -> bool:
    if generation != _action_generation:
        return False
    return not any(row.settings is not previous for row, previous, _updated in batch)


# LLM: 持锁的纯字典合并；较新通知即使被另一层覆盖也推进快照。不改任何请求状态，撤销决定留给锁外的路由比较。
#   逆序通知（代次不更新）在这里就被挡掉，不下游到路由比较。
# 函数用途: 在索引锁内为每个受影响请求算出合并后的快照，未受影响的不返回。
def _pending_advances(targets: tuple[ActiveDecision, ...], result: dict) -> tuple[tuple[ActiveDecision, dict, dict], ...]:
    scopes = ("owner", "thread") if result["scope"] == "thread" else ("owner",)
    pending = []
    for active in targets:
        previous = active.settings
        advances = [scope for scope in scopes if result["revision"][scope] > previous["revision"][scope]]
        if not advances:
            continue
        updated = {**previous, "revision": dict(previous["revision"]), "overrides": dict(previous["overrides"])}
        for scope in advances:
            updated["revision"][scope] = result["revision"][scope]
            updated["overrides"][scope] = result["overrides"][scope]
        pending.append((active, previous, updated))
    return tuple(pending)


# LLM: 调用方持 _LOCK。batch 里的每条都写回快照（较新通知即使被另一层覆盖也要推进，逆序通知根本走不到这里）；
#   changed 是复核过代次后判定“路由真的变了”的那些，只有它们置 settings_cancelled 并返回去取消句柄。
#   快照已不是比路由时那份的请求跳过不写，绝不用旧结果覆盖更新的落盘。最后 +1 通知代次，
#   让正在锁外比路由的其它通知知道快照已被推进，必须整批重算。
# 函数用途: 在一个锁段里写完整批的快照推进与撤销标记、推进通知代次，返回需要取消句柄的请求。
def _commit_batch(
    batch: tuple[tuple[ActiveDecision, dict, dict], ...],
    changed: tuple[tuple[ActiveDecision, dict, dict], ...],
) -> tuple[ActiveDecision, ...]:
    global _action_generation
    dirty = {id(active) for active, _previous, _updated in changed}
    cancels = []
    for active, previous, updated in batch:
        if active.settings is not previous:
            continue
        active.settings = updated
        if id(active) in dirty:
            active.settings_cancelled = True
            cancels.append(active)
    _action_generation += 1
    return tuple(cancels)


# LLM: 纯比较，不读写进程状态；有效路由算不出来时按“已改变”处理（沿用原来的 fail-closed 方向），
#   宁可多撤销一条也不能把已被设置改动影响到的在途请求留在那里。调用方必须不在索引锁内。
# 函数用途: 判断一次设置提交是否真的改动了这个接入点的有效路由。
def _routing_changed(active: ActiveDecision, previous: dict, updated: dict) -> bool:
    try:
        return routing_signature(active.context, updated, active.point) != routing_signature(active.context, previous, active.point)
    except Exception:
        return True
