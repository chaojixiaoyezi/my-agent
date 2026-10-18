# LLM: P2只读平台接口，无信号；消失携birth但仍失败，部分行只诊断；同源复核防PID复用。
# 模块用途: 枚举内核整树及校验部分观察，不拿消失关系或僵尸掩盖活成员。
import errno
import sys
from pathlib import Path

from .daemon_metadata import process_host_id
from .multiprocess_takeover_linux import thread_group_live


# LLM: 两平台及受信注入探针共用的类型化不一致标记；核心保留unknown，不解析异常文案，无I/O。
# 类用途: 标记本次进程快照有成员在细节读取前消失，禁止把剩余行当完整表。
class UnstableProcessSnapshot(ValueError):
    pass


# LLM: 消失的同快照关系与其余完整行一起运输，只有范围核对者能豁免无关缺口；不靠异常文案决策。
# 类用途: 保留消失时的内核字段，不把部分表直接宣布为完整表。
class DisappearedProcessSnapshot(UnstableProcessSnapshot):
    # LLM: rows只含已核验行，disappeared只含同次快照pid/ppid/pgid；None代表关系不可核实。
    # 函数用途: 携带只读观察事实，供核心按当前账本范围判断相关性。
    def __init__(self, rows=None, disappeared=None):
        super().__init__("process_disappeared_during_observation")
        self.rows = rows
        self.disappeared = disappeared


# LLM: 纯解析不会以空表兜底；无副作用。
# 函数用途: 解码一个Linux stat结构化事实。
def linux_process_row(content: str, pid: int):
    try:
        raw_pid = int(content.split("(", 1)[0].strip())
        fields = content.rsplit(")", 1)[1].split()
        parent, group, session, birth = (int(fields[index]) for index in (1, 2, 3, 19))
    except (ValueError, IndexError, TypeError) as exc:
        raise ValueError("process_stat_invalid") from exc
    if raw_pid != pid or pid <= 0 or min(parent, group, session, birth) < 0 or len(fields[0]) != 1:
        raise ValueError("process_stat_invalid")
    return {"pid": pid, "ppid": parent, "pgid": group, "sid": session, "birth": birth, "state": fields[0]}


# LLM: 平台不支持、读取失败或消失标记不能变成成功空表；丢弃整表，等待核心有限的下一次观察，无进程副作用。
# 函数用途: 读取完整平台进程表，只运输内核身份字段。
def observe_processes():
    table = {"platform": sys.platform, "host_id": "", "rows": {}, "failure": "platform_unavailable"}
    if sys.platform not in {"linux", "darwin"}:
        return table
    try:
        table["host_id"] = process_host_id()
        if sys.platform == "linux":
            _linux_visibility()
            rows = _linux_rows(Path("/proc"))
        else:
            from .multiprocess_takeover_darwin import darwin_process_rows
            rows = darwin_process_rows()
        if not rows:
            raise ValueError("enumeration_empty")
        table.update(rows=rows, failure="")
    except DisappearedProcessSnapshot as exc:
        table["failure"] = "enumeration_unstable"
        if exc.rows is not None and exc.disappeared:
            table.update(partial_rows=exc.rows, disappeared=exc.disappeared)
    except UnstableProcessSnapshot:
        table["failure"] = "enumeration_unstable"
    except OSError as exc:
        table["failure"] = "enumeration_denied" if exc.errno in {errno.EPERM, errno.EACCES} else "enumeration_unavailable"
    except (ValueError, UnicodeError, AttributeError, OverflowError):
        table["failure"] = "enumeration_incomplete"
    return table


# LLM: hidepid=1/2/4可能隐藏其它PID；同命名空间的/proc范围证明不能冒充宿主全局或cgroup证明。
# 函数用途: 核对proc挂载未开启隐藏PID，无法读取挂载事实也直接未知。
def _linux_visibility() -> None:
    mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    lines = [line for line in mounts.splitlines() if len(line.split()) >= 6 and line.split()[4] == "/proc"]
    if len(lines) != 1 or " - proc " not in lines[0]:
        raise ValueError("proc_visibility_unconfirmed")
    flags = lines[0].replace(",", " ").split()
    if any(flag.startswith("hidepid=") and flag != "hidepid=0" for flag in flags):
        raise PermissionError(errno.EACCES, "proc_visibility_restricted")


# LLM: 没有同快照Z事实的消失使整表不一致；不发布残缺表，核心最多观察两次，无信号。
# 函数用途: 读取Linux同命名空间身份，保留已知僵尸但不略掉可能写入的成员。
def _linux_rows(root: Path) -> dict:
    rows, unstable = {}, False
    for entry in tuple(root.iterdir()):
        if not entry.name.isascii() or not entry.name.isdigit():
            continue
        row = _read_linux_row(entry)
        if row is None:
            unstable = True
        else:
            rows[row["pid"]] = row
    if unstable:
        raise UnstableProcessSnapshot("process_disappeared_during_observation")
    return rows


# LLM: 组长Z必须在两次stat身份复核之间读Threads；保留原状态和独立活线程事实，错误/复用不能发布stopped。
# 函数用途: 读取单个Linux线程组身份，避免pthread_exit组长Z掩盖活线程。
def _read_linux_row(entry: Path):
    try:
        row = linux_process_row((entry / "stat").read_text(encoding="utf-8"), int(entry.name))
    except (FileNotFoundError, ProcessLookupError):
        return None
    if row["state"] == "Z":
        row["thread_group_live"] = thread_group_live(entry, row["pid"])
    try:
        after = linux_process_row((entry / "stat").read_text(encoding="utf-8"), int(entry.name))
    except (FileNotFoundError, ProcessLookupError):
        return row if row["state"] == "Z" else None
    if after["birth"] != row["birth"]:
        raise UnstableProcessSnapshot("process_reused_during_observation")
    if row["state"] == "Z" and after["state"] != "Z":
        row["thread_group_live"] = True
    return row


# LLM: 信任结构化探针而非任意字典；失败表可运输unknown，健康表必须有平台/主机及逐行绑定事实。
# 函数用途: 在核实入口拒绝空主机、坏字段或错PID行，避免异常和不完整表冒充完整枚举。
def valid_process_table(table) -> bool:
    if (type(table) is not dict or set(table) not in ({"platform", "host_id", "rows", "failure"},
            {"platform", "host_id", "rows", "failure", "partial_rows", "disappeared"})
            or any(type(table[key]) is not str for key in ("platform", "host_id", "failure"))
            or type(table["rows"]) is not dict):
        return False
    if not table["failure"] and (table["platform"] not in {"linux", "darwin"} or not table["host_id"]):
        return False
    return (all(_valid_row(pid, row) for pid, row in table["rows"].items())
            and _valid_disappearance(table))


# LLM: 部分表只运输明确消失失败的诊断；重复PID/冲突/坏birth拒绝，任何关系都不授予排除。
# 函数用途: 校验原快照消失事实，缺birth明确未知，不能变成完整观察。
def _valid_disappearance(table: dict) -> bool:
    if "disappeared" not in table:
        return True
    rows, lost = table["partial_rows"], table["disappeared"]
    if (table["failure"] != "enumeration_unstable" or type(rows) is not dict
            or type(lost) is not list or not lost or not table["host_id"]):
        return False
    if not all(_valid_row(pid, row) for pid, row in rows.items()):
        return False
    if not all(_valid_lost_row(row, rows) for row in lost):
        return False
    return len({row["pid"] for row in lost}) == len(lost)


# LLM: 旧受信探针缺birth只用于未知诊断；新原生事实携同源birth，bool/对象不能冒身份。
# 函数用途: 只读检查消失行字段和身份类型，缺失不是排除证据。
def _valid_lost_row(row, rows: dict) -> bool:
    if type(row) is not dict or set(row) not in ({"pid", "ppid", "pgid"}, {"pid", "ppid", "pgid", "birth"}):
        return False
    birth = row.get("birth")
    return (all(type(row[key]) is int and row[key] >= 0 for key in ("pid", "ppid", "pgid"))
            and row["pid"] > 0 and row["pid"] not in rows
            and (birth is None or type(birth) is int and birth >= 0
                 or type(birth) is str and 0 < len(birth) <= 256 and birth == birth.strip()
                 and all(ord(char) >= 32 for char in birth)))


# LLM: bool不是PID；仅Z可用None显式缺SID，不用0或PGID冒充；未知birth仍进入unknown，无副作用。
# 函数用途: 校验六字段内核事实，僵尸缺会话不能放宽活行。
def _valid_row(pid, row) -> bool:
    if type(pid) is not int or pid <= 0 or type(row) is not dict:
        return False
    if set(row) not in ({"pid", "ppid", "pgid", "sid", "birth", "state"},
                       {"pid", "ppid", "pgid", "sid", "birth", "state", "thread_group_live"}) or row["pid"] != pid:
        return False
    if "thread_group_live" in row and (row["state"] != "Z" or type(row["thread_group_live"]) is not bool):
        return False
    return (all(type(row[key]) is int and row[key] >= 0 for key in ("pid", "ppid", "pgid"))
            and (type(row["sid"]) is int and row["sid"] >= 0 or row["sid"] is None and row["state"] == "Z")
            and (row["birth"] is None or type(row["birth"]) in {str, int})
            and type(row["state"]) is str and row["state"] in {"R", "S", "D", "Z", "T", "t", "W", "X", "x", "I", "P", "K", "U"})
