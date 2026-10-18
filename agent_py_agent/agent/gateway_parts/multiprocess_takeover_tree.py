# LLM: P2纯三态及独立诊断，失败表不完整、关系不证明排除；不发信号或把单PID退出当整树退出。
# 模块用途: 核实原执行树，保留不完整观察中的活成员和消失最后事实。


# LLM: 失败表保持unknown，只补同主机已观测成员及消失关系；诊断绝不变成完整枚举或排除证明，无I/O。
# 函数用途: 返回执行者三态，枚举失败也保留活成员及最后事实供定位。
def evaluate_tree(record: dict, table: dict):
    detail = _evaluate_tree(record, table)
    detail["enumeration_complete"] = bool(not table["failure"] and table["host_id"] and table["platform"] in {"linux", "darwin"})
    return detail


# LLM: 原三态优先级不改；枚举完整性由公共入口独立附加，身份未知不伪装成枚举失败，无I/O。
# 函数用途: 求值登记身份和整树三态，不把诊断完整性当成授权依据。
def _evaluate_tree(record: dict, table: dict):
    identity = record.get("process_identity")
    if identity is None:
        return _detail(record, "unknown", "unregistered_instance_unproven")
    if table["platform"] not in {"linux", "darwin"}:
        return _detail(record, "unknown", "platform_unavailable")
    if table["failure"]:
        return _incomplete_detail(record, table)
    if identity.get("host_id") != table["host_id"]:
        return _detail(record, "unknown", "host_identity_conflict")
    if not _birth_known(identity.get("start_time")):
        return _detail(record, "unknown", "birth_unavailable")
    leader = table["rows"].get(identity["pid"])
    leader_status = _leader_status(identity, leader)
    members = _members(record, table["rows"], leader_status)
    instances = [_instance(row, identity["pid"]) for row in members]
    status, reason = _tree_status(record, members, instances, leader_status)
    return {**_detail(record, status, reason), "leader_status": leader_status, "instances": instances,
            "enumeration_complete": True, "observed_live_members": observed_live_members([{"instances": instances}]),
            "evidence_kinds": ["p1_canonical_record", "kernel_birth", "pgid_sid_enumeration", "parent_closure"]}


# LLM: 表失败不改变三态；只有同主机、已绑定出生的范围可归诊断成员，不声称完整内核树。
# 函数用途: 从核验过的部分行保留成员及消失最后事实，仍返回unknown。
def _incomplete_detail(record: dict, table: dict) -> dict:
    identity = record["process_identity"]
    rows = table.get("partial_rows", table["rows"])
    members = []
    if identity.get("host_id") == table["host_id"] and _birth_known(identity.get("start_time")):
        members = _members(record, rows, _leader_status(identity, rows.get(identity["pid"])))
    instances = [_instance(row, identity["pid"]) for row in members]
    lost = [{**row, "birth": row.get("birth"),
             "relation": _lost_relation(record, row, members, identity.get("host_id") == table["host_id"])}
            for row in table.get("disappeared", [])]
    return {**_detail(record, "unknown", table["failure"]), "instances": instances, "disappeared": lost,
            "enumeration_complete": False, "observed_live_members": observed_live_members([{"instances": instances}])}


# LLM: 只在同host分类数字关联，外主机编号不能匹配；分类不证明排除、血缘或授权，不猜SID。
# 函数用途: 为消失的原快照PID/PGID/父关系提供诊断类别。
def _lost_relation(record: dict, lost: dict, members: list, same_host: bool) -> str:
    if not same_host:
        return "relationship_unconfirmed"
    if lost["pid"] == record["process_identity"]["pid"]:
        return "registered_pid"
    if lost["pgid"] == record["pgid"]:
        return "registered_pgid"
    if lost["ppid"] in {row["pid"] for row in members}:
        return "observed_member_parent"
    return "orphan_parent" if lost["ppid"] <= 1 else "relationship_unconfirmed"


# LLM: 合并全部原观察的可写成员身份，仅作诊断；不覆盖unknown优先级，不合并不同birth实例。
# 函数用途: 将观察过的活成员整理成排序去重PID/birth事实，无I/O。
def observed_live_members(observations: list) -> list:
    rows = {(row["pid"], type(row["birth"]).__name__, str(row["birth"])): {"pid": row["pid"], "birth": row["birth"]}
            for observation in observations for row in observation.get("instances", []) if row["status"] == "live"}
    return [rows[key] for key in sorted(rows)]


# LLM: 缺失 birth 不可与任何值相等，严格类型防 bool 和整数互冒；与 P0 内核出生合同一致。
# 函数用途: 判断出生字段是否具有具体可比较的身份。
def _birth_known(value) -> bool:
    return (type(value) is int and value >= 0 or type(value) is str and 0 < len(value) <= 256
            and value == value.strip() and all(ord(char) >= 32 for char in value))


# LLM: 原PID复用只证明原组长退出；Linux组长Z的活线程事实优先，不跳过原组/会话的活实例。
# 函数用途: 独立判定原线程组身份，而不据此得出整树结论。
def _leader_status(identity: dict, row) -> str:
    if row is None:
        return "absent"
    if not _birth_known(row["birth"]):
        return "unknown"
    if type(row["birth"]) is not type(identity["start_time"]) or row["birth"] != identity["start_time"]:
        return "reused"
    return "stopped" if row["state"] in {"Z", "X"} and not row.get("thread_group_live", False) else "live"


# LLM: PGID 或 SID 命中都纳入；组长已消失仍扫描，同出生组长的父子闭包另捕捉已知逃逸。
# 函数用途: 找全当前快照内可归于原执行树的实例，不信任新进程内存登记表。
def _members(record: dict, rows: dict, leader_status: str) -> list:
    root_pid = record["process_identity"]["pid"]
    selected = {pid for pid, row in rows.items() if row["pgid"] == record["pgid"] or row["sid"] == record["sid"]}
    if leader_status == "reused":
        selected.discard(root_pid)
    elif root_pid in rows:
        selected.add(root_pid)
    children = {}
    for pid, row in rows.items():
        children.setdefault(row["ppid"], []).append(pid)
    pending = list(selected)
    while pending:
        pid = pending.pop()
        fresh = set(children.get(pid, ())) - selected
        selected.update(fresh)
        pending.extend(fresh)
    return [rows[pid] for pid in sorted(selected)]


# LLM: 出生未知必须unknown；组长Z且仍有线程不能签发非写入，不冒领wait/reap退出码。
# 函数用途: 保存具体线程组的核实三态及原始观察字段。
def _instance(row: dict, root_pid: int) -> dict:
    status = "unknown" if not _birth_known(row["birth"]) else "live"
    if status != "unknown" and row["state"] in {"Z", "X"} and not row.get("thread_group_live", False):
        status = "stopped"
    return {"pid": row["pid"], "birth": row["birth"], "pgid": row["pgid"], "sid": row["sid"],
            "relation": "leader" if row["pid"] == root_pid else "descendant", "status": status,
            "thread_group_live": row.get("thread_group_live", False)}


# LLM: 已知逃逸先隔离；仅Z的SID缺失不算活行逃逸，仍遍历其活子代和检查birth，无I/O。
# 函数用途: 归并整树三态；僵尸不写入但不能掩盖其后代，历史不可见逃逸仍保留风险。
def _tree_status(record: dict, members: list, instances: list, leader_status: str) -> tuple:
    escaped = any(row["pgid"] != record["pgid"] and row["sid"] != record["sid"]
                  and not (row["state"] == "Z" and row["sid"] is None) for row in members)
    if escaped:
        return "unknown", "descendant_scope_escape"
    if leader_status == "unknown" or any(row["status"] == "unknown" for row in instances):
        return "unknown", "birth_unavailable"
    if any(row["status"] == "live" for row in instances):
        return "live", "leader_live" if leader_status == "live" else "descendant_live"
    return "stopped", "leader_pid_reused" if leader_status == "reused" else "tree_absent"


# LLM: 早拒绝只确认原P1记录，不虚报尚未执行的内核/闭包检查；无正文，且不是启动/恢复许可。
# 函数用途: 生成明确区分未知、活着和已停的逐实例结果。
def _detail(record: dict, status: str, reason: str) -> dict:
    return {"reservation_id": record["reservation_id"], "status": status, "reason": reason,
            "evidence_kinds": ["p1_canonical_record"],
            "leader_status": "unknown", "instances": []}
