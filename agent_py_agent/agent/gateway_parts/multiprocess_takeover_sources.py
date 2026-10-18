# LLM: P1不是停止证明；generation metadata只定位旧N，P2仍实际核实；角色集合只取规范wN.json，正常心跳temp/lock不属于角色源，逐份原nofollow/旧代拒绝不变。
# 模块用途: 发现原旧域及角色缺口；v2完整关闭范围可解除角色拒绝，非空P1仍需fresh P2，缺旧历史不补封或授信。
import hashlib
import json
import os
import re
from dataclasses import dataclass

from ..common.nofollow_fs import list_names_beneath, open_directory_beneath, read_bytes_beneath
from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_bootstrap_publish import _WORKER_REGISTRY_MAX_BYTES
from .multiprocess_bootstrap_ready import current_worker_identity
from .multiprocess_executor_ledger import ExecutorLedger
from .multiprocess_generation_metadata import read_generation_metadata
from .multiprocess_records import deserialize_record, record_digest, validate_record
from .multiprocess_route import WorkerPartition, owner_digest
from .multiprocess_takeover_ledger import read_ledger_scope
from .multiprocess_takeover_unresolved_source import (
    UnresolvedExecutorSource,
    capture_unresolved_source,
)

# 完整核实明细的有界读回，不截断；超限只拒绝，不把半份明细当成功。
_TAKEOVER_EVIDENCE_READ_MAX_BYTES = 4 * 1024 * 1024


# LLM: 默认调用保持原角色拒绝；正count消费原v2关闭范围，旧role/C/P4完整raw双读，当前W只容原五动态字段，不由stopped/PID授信。
# 函数用途: 查完整规范角色覆盖，拒绝旧角色和读取期间的角色增删，避免正常心跳发表临时名造成永久隔离。
def require_role_coverage(paths, current_c_run_id, *, count=None, captured=(), committed=()):
    if count is not None:
        return _closed_role_coverage(paths, current_c_run_id, count, captured, committed)
    _check_role_marker(paths.root, ("gateway_state.json",), current_c_run_id)
    names = _role_marker_names(paths.root)
    for name in names:
        _check_role_marker(paths.root, ("workers", name), current_c_run_id)
    if _role_marker_names(paths.root) != names:
        raise ValueError("role_namespace_changed")


# LLM: 只投影本reader原exact regex规定的角色文件；不新增兼容名或P1过滤，不读内容/授许可，所有规范项仍由原_check_role_marker完整核对。
# 函数用途: 从原目录枚举提取实际角色标记名称，首末同一规则排除正常写入设施文件。
def _role_marker_names(root) -> list[str]:
    return [name for name in list_names_beneath(root, ("workers",))
            if re.fullmatch(r"w(0|[1-9][0-9]*)\.json", name)]


# LLM: 严格有界nofollow只读；字段缺失/旧代一律unknown，C当前PID不能由其它进程冒领。
# 函数用途: 查单个角色标记是否仍需独立的主体/历史树覆盖证明。
def _check_role_marker(root, parts, current_c_run_id) -> None:
    content = read_bytes_beneath(root, parts, max_bytes=16384, require_dir_fd=True)
    if content is None:
        return
    value = json.loads(content)
    if type(value) is not dict or value.get("c_run_id") != current_c_run_id:
        raise ValueError("old_role_lifecycle_unproven")
    if parts == ("gateway_state.json",) and (type(value.get("pid")) is not int or value["pid"] != os.getpid()):
        raise ValueError("old_role_lifecycle_unproven")


# LLM: 只消费原v2完整范围及controller固定writer raw观察；当前W双读复用原P4身份，仅容原producer动态投影，旧责任/P2不变。
# 函数用途: 在新C覆盖state前及每次刷新核完整旧角色，缺历史/活身份/源变化保持拒绝。
def _closed_role_coverage(paths, current, count, captured, committed=()):
    from .multiprocess_generation_metadata import (
        captured_generation_sources,
        require_closed_generation,
    )

    roles = _role_snapshot(paths.root)
    registry = _discovery_registry(paths.root)
    if registry is not None and registry[0]['c_run_id'] == current:
        from .multiprocess_executor_identity import process_birth
        identity = registry[0]['process_identity']
        if identity['pid'] != os.getpid() or identity['start_time'] != process_birth(os.getpid()):
            raise ValueError('old_role_lifecycle_unproven')
    generations = dict(captured)
    for content in roles.values():
        value = json.loads(content)
        if type(value) is not dict:
            raise ValueError('old_role_lifecycle_unproven')
        generation = value.get('c_run_id')
        if generation != current:
            generations.setdefault(generation, None)
    if registry is not None and registry[0]['c_run_id'] != current:
        generations.setdefault(registry[0]['c_run_id'], None)
    closed = []
    for generation in generations:
        history = read_generation_metadata(paths.root, generation)
        if (history is not None and history[0]['schema_version'] == 'multiprocess_c_generation.v2'
                and history[0]['closure'] is not None):
            closed.append((generation, history[1]))
    coverage = captured_generation_sources(paths.root, tuple(closed))
    for generation, original in tuple(generations.items()):
        history = require_closed_generation(paths, generation, count, current, original, committed=committed, covered=coverage)
        closure = history[0]['closure']
        if set(roles) != set(closure['role_markers']):
            raise ValueError('old_role_lifecycle_unproven')
        _require_closed_markers(roles, closure, generation, current)
        if registry is not None and registry[0]['c_run_id'] == generation:
            if registry[1] != closure['registry_sha256']:
                raise ValueError('generation_closed_registry_changed')
        generations[generation] = history[1]
    for name, content in roles.items():
        value = json.loads(content)
        if value.get('c_run_id') == current:
            _check_role_marker(paths.root, tuple(name.split('/')), current)
        elif value.get('c_run_id') not in generations:
            raise ValueError('old_role_lifecycle_unproven')
    latest = _role_snapshot(paths.root)
    if _discovery_registry(paths.root) != registry:
        raise ValueError('generation_role_sources_changed')
    _require_stable_roles(roles, latest, registry, current, count)
    return tuple(sorted(generations.items()))


# LLM: C及旧role仍按完整raw；当前规范W的两份完整schema均须匹配同一P4，仅原producer五字段可变，集合变化拒绝。
# 函数用途: 区分正常心跳发表与角色/旧代来源漂移，不查活issuer、不把分类结果当ready或关闭授权。
def _require_stable_roles(before, after, registry, current, count):
    if set(before) != set(after):
        raise ValueError('generation_role_sources_changed')
    for name, content in before.items():
        value = json.loads(content)
        match = re.fullmatch(r'workers/w(0|[1-9][0-9]*)\.json', name)
        if match is not None and value.get('c_run_id') == current:
            if registry is None:
                raise ValueError('generation_current_registry_required')
            partition = WorkerPartition(int(match[1]), count, current)
            first = _current_worker_role(registry[0], value, partition)
            last = _current_worker_role(registry[0], json.loads(after[name]), partition)
            if first != last:
                raise ValueError('generation_role_sources_changed')
        elif content != after[name]:
            raise ValueError('generation_role_sources_changed')


# LLM: 先经原完整版本/schema/PID/出生/instance/count/gen核验，再剔除producer确切五动态字段；未知新键仍由原schema拒绝。
# 函数用途: 取得同一规范文件名与P4实例的静态投影，readiness内容仍归原健康门判断。
def _current_worker_role(registry, heartbeat, partition):
    _registry, heartbeat, _entry = current_worker_identity(registry, heartbeat, partition)
    if heartbeat['worker'] != partition.index:
        raise ValueError('generation_role_filename_mismatch')
    dynamic = {'updated_at', 'inflight', 'ready', 'loop_health', 'readiness'}
    return {key: value for key, value in heartbeat.items() if key not in dynamic}


# LLM: 规范marker显式strict snapshot最多3叶尝试，仍完整raw供旧closure/C/P4/当前W分界；目录/硬路径错不重读，不重捕授权。
# 函数用途: 捕获角色原字节，避免setup后用新state洗掉旧范围。
def _role_snapshot(root):
    names = ['gateway_state.json', *(f'workers/{name}' for name in _role_marker_names(root))]
    values = {}
    for name in names:
        content = read_bytes_beneath(root, tuple(name.split('/')), max_bytes=16384, require_dir_fd=True,
                                     snapshot_retries=2)
        if content is not None:
            values[name] = content
    if names != ['gateway_state.json', *(f'workers/{name}' for name in _role_marker_names(root))]:
        raise ValueError('role_namespace_changed')
    return values


# LLM: 尚未被本代原publisher覆盖的旧marker必须逐字节命中原关闭摘要；其它旧代不能借该closure，当前C另走原PID门。
# 函数用途: 检查混代窗口里每个角色的原关闭绑定，不覆盖或删除任何marker。
def _require_closed_markers(roles, closure, generation, current):
    for name, content in roles.items():
        value = json.loads(content)
        if value.get('c_run_id') == generation:
            if hashlib.sha256(content).hexdigest() != closure['role_markers'][name]:
                raise ValueError('generation_closed_role_changed')
        elif value.get('c_run_id') == current:
            continue


# LLM: 组合根传原P1对象和完整旧范围；不持久化为第二账本，不接受屏障文件作为输入。
# 类用途: 聚合一次旧执行域的核实输入。
@dataclass(frozen=True)
class TakeoverTarget:
    ledger: object
    scope: dict
    digests: dict | None = None


# LLM: 缺源与合法空目录不能折叠；严格目录打开且双读身份，缺能力/替换只能拒绝，不创建目录。
# 函数用途: 返回两个规范命名空间的身份与名称，None只表示本次目录确实不存在。
def takeover_namespace_state(paths) -> tuple:
    return tuple(_namespace_state(paths.root, parts)
                 for parts in (("workers", "executors"), ("admission", "reservations")))


# LLM: list_names缺目录返回空列表，因此先严格打开锚定目录；收尾再次打开以发现目录替换和消失。
# 函数用途: 区分缺目录与空目录并保全本次目录身份及完整名称。
def _namespace_state(root, parts):
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        names = tuple(list_names_beneath(root, parts))
        _same_namespace(root, parts, (info.st_dev, info.st_ino))
        return (info.st_dev, info.st_ino, names)
    finally:
        os.close(descriptor)


# LLM: 收尾只核原命名空间身份，不跟随目录替换，不把尚未创建的源当作停止证明。
# 函数用途: 确认收尾路径仍指向同一规范目录。
def _same_namespace(root, parts, identity) -> None:
    descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != identity:
            raise ValueError("executor_namespace_changed")
    finally:
        os.close(descriptor)


# LLM: 旧count只取原不可变generation metadata或同代current registry，完整原源双读；缺失只限制，坏来源拒绝，metadata不产生P2/执行权。
# 函数用途: 从P1唯一源发现可核实范围或不能解除的旧责任，不签发空账屏障。
def discover_takeover_targets(paths, count: int, *, current_c_run_id=None) -> list:
    names = list_names_beneath(paths.root, ("workers", "executors"))
    groups, unresolved, generations = {}, [], {}
    registry = _discovery_registry(paths.root) if current_c_run_id is not None else None
    for name in names:
        _discover_partition(paths.root, count, name, groups, current_c_run_id, registry, unresolved, generations)
    if list_names_beneath(paths.root, ("workers", "executors")) != names:
        raise ValueError("executor_namespace_changed")
    if current_c_run_id is not None and _discovery_registry(paths.root) != registry:
        raise ValueError("executor_registry_changed")
    for generation, metadata in generations.items():
        if read_generation_metadata(paths.root, generation) != metadata:
            raise ValueError("executor_generation_metadata_changed")
    return [_target(paths.root, count, key, rows) for key, rows in sorted(groups.items())] + unresolved


# LLM: P3旧占用而P1源缺失不是首次空账；已补章claim仅原四绑定唯一P1定位旧责任，仍需完整fresh P2；不能凭JSON伪造停止证明。
# 函数用途: 查找尚未被完整P1旧域覆盖的准入占用，坏源由组合根整体隔离。
def uncovered_admission_owners(paths, targets, accepted=None) -> set:
    parts = ("admission", "reservations")
    # P3规范route不存count；分区数只取当前冻结P1目标，不能假造route字段。
    covered = {(target.scope["c_run_id"],
                target.scope["worker_index"] if target.scope["role"] == "W" else "C", owner)
               for target in targets for owner in target.scope["owner_shas"]}
    owners = set()
    names = list_names_beneath(paths.root, parts)
    for filename in names:
        row = _read_admission_source(paths.root, parts, filename)
        if row["state"] == "released" or (accepted or {}).get(row["request_id"]) == record_digest(row):
            continue
        route = row["route"]
        if (route["c_run_id"], route["worker"], row["owner_sha"]) not in covered:
            from .multiprocess_processing_recovery import original_claim_covers_reservation

            if not original_claim_covers_reservation(paths, row, targets):
                owners.add(row["owner_sha"])
    if list_names_beneath(paths.root, parts) != names:
        raise ValueError("admission_namespace_changed")
    return owners


# LLM: 复用记录合同但不使用跟随链接的普通读；完整有界nofollow字节和文件名绑定缺一不可。
# 函数用途: 只读一条规范准入源，不写预留或请求。
def _read_admission_source(root, parts, filename) -> dict:
    if not filename.endswith(".json"):
        raise ValueError("admission_filename_invalid")
    row = deserialize_record(read_bytes_beneath(root, (*parts, filename), max_bytes=16384, require_dir_fd=True))
    if row.get("schema_version") not in {"multiprocess_admission_reservation.v1", "multiprocess_admission_reservation.v2"}:
        raise ValueError("admission_schema_invalid")
    if row["request_id"] + ".json" != filename:
        raise ValueError("admission_filename_binding_conflict")
    return row


# LLM: 原registry完整P0/实际nofollow字节双读才提供旧N；缺失/另一代不补历史，坏记录不能降级成无count。
# 函数用途: 只读原当前登记及完整摘要，供本次发现区分已知/未知旧代count。
def _discovery_registry(root):
    content = read_bytes_beneath(root, ("workers", "registry.json"),
                                 max_bytes=_WORKER_REGISTRY_MAX_BYTES, require_dir_fd=True)
    if content is None:
        return None
    row = deserialize_record(content)
    if row['schema_version'] != 'multiprocess_worker_registry.v1':
        raise ValueError('executor_registry_schema_invalid')
    return row, hashlib.sha256(content).hexdigest()


# LLM: 完整规范目录双读；旧物理桶可超出新N，只作限制，当前代仍须原ledger严格核桶，不削弱业务入口。
# 函数用途: 发现实际旧记录，按完整旧N或未知限制来源分流。
def _discover_partition(root, count, name, groups, current_c_run_id=None, registry=None, unresolved=None,
                        generations=None) -> None:
    if re.fullmatch(r"w(0|[1-9][0-9]*)", name) is None:
        raise ValueError("executor_namespace_unconfirmed")
    if current_c_run_id is None and int(name[1:]) >= count:
        raise ValueError("executor_namespace_unconfirmed")
    parts = ("workers", "executors", name)
    names = list_names_beneath(root, parts)
    if names is None or "ledger.lock" not in names:
        raise ValueError("executor_namespace_unconfirmed")
    for filename in (value for value in names if value != "ledger.lock"):
        row = _discover_record(root, count, parts, filename, current_c_run_id, registry, generations)
        if type(row) is UnresolvedExecutorSource:
            unresolved.append(row)
            continue
        role = "C" if row["owner_sha"] == owner_digest(OwnerIdentity.local_main()) else "W"
        index = None if role == "C" else int(name[1:])
        groups.setdefault((row["c_run_id"], role, index), []).append(row)
    if list_names_beneath(root, parts) != names:
        raise ValueError("executor_namespace_changed")


# LLM: exact原generation metadata/current registry定位旧N，仍_bound_record/P1read严格门；缺旧N只限制，metadata不签P2或执行权。
# 函数用途: 验规范记录并返回可核目标行或限制对象，不改变原ledger写/授权合同。
def _discover_record(root, count, parts, filename, current_c_run_id=None, registry=None, generations=None):
    if re.fullmatch(r"[0-9a-f]{64}\.json", filename) is None:
        raise ValueError("executor_filename_invalid")
    row = deserialize_record(read_bytes_beneath(root, (*parts, filename), max_bytes=16384, require_dir_fd=True))
    identifier = row.get("reservation_id")
    if type(identifier) is not str or hashlib.sha256(identifier.encode()).hexdigest() + ".json" != filename:
        raise ValueError("executor_filename_binding_conflict")
    old = current_c_run_id is not None and row['c_run_id'] != current_c_run_id
    old_count = _original_generation_count(root, row['c_run_id'], registry, generations) if old else None
    if old and old_count is None:
        return capture_unresolved_source(root, (*parts, filename), current_c_run_id)
    bound_count = old_count if old else count
    ledger = ExecutorLedger(root, WorkerPartition(int(parts[-1][1:]), bound_count, row["c_run_id"]))
    ledger._bound_record(row)
    if ledger.read(identifier) != row:
        raise ValueError("executor_readback_changed")
    if old and old_count != count:
        return capture_unresolved_source(root, (*parts, filename), current_c_run_id)
    return row


# LLM: 历史P0只定位exact旧代count，原current registry同代必须一致；缺ref不猜，完整双读只提供范围数据而非P2/ready/执行权。
# 函数用途: 优先读取唯一原generation metadata并绑定本次读回摘要。
def _original_generation_count(root, generation, registry, observed):
    metadata = read_generation_metadata(root, generation)
    if generation in observed and observed[generation] != metadata:
        raise ValueError('executor_generation_metadata_changed')
    observed[generation] = metadata
    registered = (registry[0]['worker_count'] if registry is not None
                  and registry[0]['c_run_id'] == generation else None)
    if metadata is None:
        return registered
    count = metadata[0]['worker_count']
    if registered is not None and count != registered:
        raise ValueError('executor_generation_count_conflict')
    return count


# LLM: 分组覆盖全部已发现owner/ID，P2a仍会枚举规范目录校验是否有漏列或变化。
# 函数用途: 构造显式v2旧范围和绑定旧代的原P1对象。
def _target(root, count, key, rows):
    generation, role, index = key
    scope = {"state_scope": "agent_state", "role": role, "c_run_id": generation,
             "worker_index": index, "worker_count": count,
             "owner_shas": sorted({row["owner_sha"] for row in rows}),
             "executor_reservation_ids": sorted(row["reservation_id"] for row in rows)}
    physical = int(rows[0]["owner_sha"], 16) % count
    return TakeoverTarget(ExecutorLedger(root, WorkerPartition(physical, count, generation)), scope,
                          {row["reservation_id"]: record_digest(row) for row in rows})


# LLM: 只认本次P2a返回值；五字段、v2证明及内容寻址明细须一致，当前P1源摘要仍相等才可交P3。
# 函数用途: 返回当次停止结果的完整明细；任一缺口抛错，不能回退到文件里的旧屏障。
def confirmed_evidence(target, result) -> dict:
    barrier, proof = result["barrier"], validate_record(result["proof"])
    fields = {"schema_version", "c_run_id", "stopped", "ledger_ref", "unresolved_instances"}
    if (set(barrier) != fields or barrier["schema_version"] != "multiprocess_takeover_barrier.v1"
            or barrier["stopped"] is not True or barrier["unresolved_instances"] != [] or result["error"] is not None):
        raise ValueError("takeover_barrier_unconfirmed")
    if (proof["schema_version"] != "multiprocess_takeover_proof.v2" or proof["status"] != "stopped"
            or proof["covered_scope"] != target.scope or barrier["c_run_id"] != target.scope["c_run_id"]):
        raise ValueError("takeover_scope_unconfirmed")
    detail = _read_evidence(target.ledger.root, barrier["ledger_ref"])
    expected = {"c_run_id": proof["c_run_id"], "covered_scope": proof["covered_scope"],
                "platform": proof["platform"], "method": proof["method"], "residual_risks": proof["residual_risks"]}
    if any(detail.get(key) != value for key, value in expected.items()):
        raise ValueError("takeover_evidence_binding_conflict")
    current = read_ledger_scope(target.ledger, target.scope)
    digests = {row["reservation_id"]: record_digest(row) for row in current["records"]}
    if current["failures"] or detail["failures"] or detail["record_digests"] != digests:
        raise ValueError("takeover_evidence_source_changed")
    if {row["reservation_id"] for row in detail["instances"]} != set(target.scope["executor_reservation_ids"]):
        raise ValueError("takeover_evidence_coverage_conflict")
    if any(row["status"] != "stopped" for row in detail["instances"]):
        raise ValueError("takeover_evidence_not_stopped")
    return detail


# LLM: 相对规范三段和内容sha必须一致；nofollow有界读拒绝绝对路径、逃逸或伪造/过期文件。
# 函数用途: 读回本次P2写出的内容寻址明细，不从文件生成屏障或授权。
def _read_evidence(root, reference) -> dict:
    if type(reference) is not str or re.fullmatch(r"workers/takeover_evidence/[0-9a-f]{64}\.json", reference) is None:
        raise ValueError("takeover_evidence_reference_invalid")
    content = read_bytes_beneath(root, tuple(reference.split("/")), max_bytes=_TAKEOVER_EVIDENCE_READ_MAX_BYTES,
                                 require_dir_fd=True)
    if content is None or hashlib.sha256(content).hexdigest() + ".json" != reference.split("/")[-1]:
        raise ValueError("takeover_evidence_hash_conflict")
    detail = json.loads(content)
    if detail.get("schema_version") != "multiprocess_takeover_evidence.v1":
        raise ValueError("takeover_evidence_schema_invalid")
    return detail
