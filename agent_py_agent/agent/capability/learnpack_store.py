# LLM: learnpack 的宿主存储，唯一位置 <owner home>/data/learnpack/（owner 根的 data/ 是 H3 宿主运行状态，模型工具只能读不能写）：
#   - builds/<sha256>.zip 与 builds/<sha256>.json：她打出的包字节与打包记录；"是不是她自己做的"只认这里，且读回时重算 sha256；
#   - orders/<单号>.json 与 orders/<单号>.done.json：开关关着时的待确认安装单，done 文件用独占创建保证一张单只执行一次；
#   - installs.jsonl：每次装、退回的记录，供退回找上一版、判断安装单是否过期；
#   - owned/<包名>.json：她的包进过安装表的包名（判断残留插件数据目录是不是她的）。文件名直接用包名：它和插件数据目录同在
#     owner home 下，大小写规则与数据目录一致；
#   - seq.json（配 .seq.lock）：单子与安装记录共用的递增序号，比较先后只看它（复审：系统时钟回拨会颠倒时间先后）。
#   这里只读写文件，不安装、不判断开关或身份；改布局要同步 path_access_policy 的 H3 说明与 test_learnpack_store。
# 模块用途: 保存她打的包、待确认安装单和安装记录，重启不丢，单号只执行一次。
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common.directory_lock import locked_private_directory
from ..common.json_io import (
    append_private_jsonl_capped,
    read_json_object,
    read_jsonl_objects_report,
    write_private_json_object,
)
from ..plugin_manifest import PluginPackageError
from ..plugin_package import inspect_plugin_package
from .package_build import KIND_CAPABILITY_PACK, BuiltPackage

# 相对 owner home 的存储位置（owner 根 data/ 下，宿主写、模型只读）。
STORE_PARTS = ("data", "learnpack")
# 待确认安装单号：lp- 加 8 位十六进制，短到能整行复制。
ORDER_ID_PATTERN = re.compile(r"lp-[0-9a-f]{8}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# 生成安装单号时撞号的最多重抽次数。
_ORDER_ID_ATTEMPT_COUNT = 8
# 安装记录最多保留 2000 条：超限只丢最旧的，找"上一版"只看最近记录。
_MAX_INSTALL_RECORD_COUNT = 2000
# 找同名旧包时最多看的打包记录文件个数（超出的不看，只影响打包回执里的提醒）。
_MAX_BUILD_SCAN_COUNT = 2000


# LLM: 打包记录只来自宿主打包工具；origin/license 是她声明的来源与许可证（展示与追溯用，不做权限判断）。
# 类用途: 一次打包的结构化事实。
@dataclass(frozen=True)
class BuildRecord:
    sha256: str
    kind: str
    package_id: str
    version: str
    file_count: int
    origin: str
    license: str
    built_at: str
    run_id: str


# LLM: 打包时宿主随包记下的来源事实：origin/license 由她声明，run_id 取自当前运行；只用于追溯与展示。
# 类用途: 一次打包的来源说明。
@dataclass(frozen=True)
class BuildProvenance:
    origin: str
    license: str
    run_id: str


# LLM: 待确认安装单只指向一个已存在的打包记录（sha256），不带路径、命令或权限；确认时宿主按单号重读。seq 是开单时取的递增
#   序号，判断单子是否被后来的单或安装取代只比它，created_at 只用于显示。
# 类用途: 开关关着时生成、等用户确认的一张安装单。
@dataclass(frozen=True)
class InstallOrder:
    order_id: str
    sha256: str
    kind: str
    package_id: str
    version: str
    created_at: str
    seq: int


# LLM: 所有写入都在 root 下、权限 0600；读回打包产物时重算摘要，摘要不符按"没有这个包"处理。
# 类用途: 一个 owner 的 learnpack 存储。
class LearnpackStore:
    # LLM: 只记根路径，不读写；所有位置都在 owner home 的 data/learnpack 下。
    # 函数用途: 绑定 owner home，构造不创建目录。
    def __init__(self, owner_home: str | Path) -> None:
        self.root = Path(owner_home).joinpath(*STORE_PARTS)

    # LLM: 同一字节的包重复打只保留一份（内容寻址）；记录文件每次按最新一次打包覆盖。有写文件副作用。
    # 函数用途: 保存一次打包的字节与记录，返回记录。
    def save_build(self, built: BuiltPackage, provenance: BuildProvenance) -> BuildRecord:
        record = BuildRecord(built.sha256, built.kind, built.package_id, built.version, len(built.files),
                             provenance.origin, provenance.license, _now(), provenance.run_id)
        _write_content_addressed(self._build_zip(built.sha256), built.payload)
        write_private_json_object(self._build_json(built.sha256), asdict(record))
        return record

    # LLM: 只有 zip 字节的 sha256 与文件名一致、记录可读、且记录里的包名/版本/类型与包清单一致时才返回（复审建议的纵深核对）；
    #   任何不符都返回 None（调用方按"不是她做的"拒绝）。只读。
    # 函数用途: 按 sha256 取回她打过的包的记录。
    def build(self, sha256: str) -> BuildRecord | None:
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            return None
        try:
            payload = self._build_zip(sha256).read_bytes()
            raw = read_json_object(self._build_json(sha256))
            record = BuildRecord(**{key: raw[key] for key in BuildRecord.__dataclass_fields__})
            manifest = inspect_plugin_package(payload).manifest
        except (OSError, KeyError, TypeError, ValueError, PluginPackageError):
            return None
        if hashlib.sha256(payload).hexdigest() != sha256 or record.sha256 != sha256:
            return None
        if (manifest.plugin_id, manifest.version) != (record.package_id, record.version):
            return None
        return record if manifest.is_content_only == (record.kind == KIND_CAPABILITY_PACK) else None

    # LLM: 列出同一个包名做过的打包记录：按修改时间取最近的 _MAX_BUILD_SCAN_COUNT 个记录文件（复审：超出上限时要的是最近的，
    #   不是按文件名随便取一批），先读记录筛包名，再逐个用 build() 核对字节与清单，坏的跳过；结果按打包时间从早到晚。
    #   只读，只用于打包回执里的事实与提醒，不做权限判断。
    # 函数用途: 找她以前做过的同名包。
    def builds_for(self, package_id: str) -> list[BuildRecord]:
        found: list[BuildRecord] = []
        newest = sorted((self.root / "builds").glob("*.json"), key=_modified_time, reverse=True)
        for path in newest[:_MAX_BUILD_SCAN_COUNT]:
            try:
                same_package = read_json_object(path).get("package_id") == package_id
            except (OSError, ValueError):
                continue
            record = self.build(path.stem) if same_package else None
            if record is not None:
                found.append(record)
        return sorted(found, key=lambda item: item.built_at)

    # LLM: 打包产物 zip 里的文件名（不含目录项）；读不了返回空元组。调用前应先用 build() 确认字节可信。只读。
    # 函数用途: 列出她打的某个包里有哪些文件（比较上一版少了哪些文件用）。
    def build_files(self, sha256: str) -> tuple[str, ...]:
        try:
            with zipfile.ZipFile(self._build_zip(sha256)) as archive:
                return tuple(name for name in archive.namelist() if not name.endswith("/"))
        except (OSError, zipfile.BadZipFile):
            return ()

    # LLM: 只拼路径；调用前应先用 build() 确认字节与记录可信。
    # 函数用途: 返回某个打包产物在宿主存储里的路径（安装命令从这里读包）。
    def build_path(self, sha256: str) -> Path:
        return self._build_zip(sha256)

    # LLM: 单号随机生成，用独占创建落 orders/<单号>.json，撞号就重抽（不会盖掉别的单）；不检查开关，调用方负责只在需要用户
    #   确认时建单。有写文件副作用。
    # 函数用途: 为一个打包记录开一张待确认安装单。
    def create_order(self, record: BuildRecord) -> InstallOrder:
        (self.root / "orders").mkdir(parents=True, exist_ok=True)
        seq = self._next_seq()
        for _attempt in range(_ORDER_ID_ATTEMPT_COUNT):
            order = InstallOrder(f"lp-{secrets.token_hex(4)}", record.sha256, record.kind, record.package_id,
                                 record.version, _now(), seq)
            if _create_private_json(self.root / "orders" / f"{order.order_id}.json", asdict(order)):
                return order
        raise OSError("安装单号连续撞号，没能开单")

    # LLM: 单号先过格式校验再拼路径（不会越出 orders/）；读不出或字段不全返回 None。只读。
    # 函数用途: 按单号读回待确认安装单；单号格式不对或读不到返回 None。
    def order(self, order_id: str) -> InstallOrder | None:
        if not isinstance(order_id, str) or ORDER_ID_PATTERN.fullmatch(order_id) is None:
            return None
        try:
            raw = read_json_object(self.root / "orders" / f"{order_id}.json")
            return InstallOrder(**{key: raw[key] for key in InstallOrder.__dataclass_fields__})
        except (OSError, KeyError, TypeError, ValueError):
            return None

    # LLM: 用独占创建 done 文件抢占执行权：抢到返回 True（只会有一次），已存在返回 False。写入"执行中"占位，
    #   之后每发一条宿主命令前由 note_order_step 追加进度，执行完由 finish_order 覆盖成结果；进程中途退出时占位与进度保留，
    #   再确认会如实回放"已执行过、结果未确认"和已发出的请求编号。有写文件副作用。
    # 函数用途: 声明"这张单现在开始执行"，保证只执行一次。
    def claim_order(self, order_id: str) -> bool:
        (self.root / "orders").mkdir(parents=True, exist_ok=True)
        return _create_private_json(self.root / "orders" / f"{order_id}.done.json", {"state": "executing", "steps": []})

    # LLM: 只在抢到执行权之后调用；读改写 done 文件，进度只增不减。有写文件副作用。
    # 函数用途: 记下这张单即将发出的一条宿主命令（请求编号与命令文字）。
    def note_order_step(self, order_id: str, request_id: str, command: str) -> None:
        current = self.order_outcome(order_id) or {"state": "executing", "steps": []}
        steps = [*current.get("steps", []), {"request_id": request_id, "command": command}]
        write_private_json_object(self.root / "orders" / f"{order_id}.done.json", {**current, "steps": steps})

    # LLM: 结果覆盖"执行中"占位，保留已记的进度。有写文件副作用。
    # 函数用途: 把一张单的执行结果写进 done 文件。
    def finish_order(self, order_id: str, outcome: dict[str, object]) -> None:
        steps = (self.order_outcome(order_id) or {}).get("steps", [])
        write_private_json_object(self.root / "orders" / f"{order_id}.done.json",
                                  {"state": "finished", "steps": steps, **outcome})

    # LLM: 只读；读不出的单子跳过。用于判断一张单是不是已被同一个包后来的单取代。
    # 函数用途: 列出某个包的全部待确认安装单。
    def orders_for(self, package_id: str) -> list[InstallOrder]:
        return [order for order in self._all_orders() if order.package_id == package_id]

    # LLM: 只读；读不出的单子跳过。
    # 函数用途: 列出全部待确认安装单。
    def _all_orders(self) -> list[InstallOrder]:
        paths = sorted((self.root / "orders").glob("lp-*.json"))
        orders = [self.order(path.name[:-len(".json")]) for path in paths if not path.name.endswith(".done.json")]
        return [order for order in orders if order is not None]

    # LLM: 只读 done 文件；文件不存在表示还没执行过。
    # 函数用途: 读一张已执行单的结果（没执行过或读不出返回 None）。
    def order_outcome(self, order_id: str) -> dict[str, object] | None:
        path = self.root / "orders" / f"{order_id}.done.json"
        try:
            return read_json_object(path) if path.is_file() else None
        except (OSError, ValueError):
            return None

    # LLM: "她装过的包名"的结构化记录（owned/<包名>.json，独占创建、可重复调用、不会被截断），只在她的包进了安装表之后记。
    #   用于判断某个包名留下的插件数据目录是不是她自己的（复审：卸载后残留的同名数据目录不能给她的包继承）。包名先经清单校验，
    #   可安全作文件名。有写文件副作用。
    # 函数用途: 记下她装过这个包名。
    def remember_owned_id(self, package_id: str) -> None:
        (self.root / "owned").mkdir(parents=True, exist_ok=True)
        _create_private_json(self.root / "owned" / f"{package_id}.json", {"package_id": package_id, "at": _now()})

    # LLM: 只读；包名不合规时按"没装过"处理。
    # 函数用途: 判断她是否装过这个包名。
    def owns_id(self, package_id: str) -> bool:
        if not isinstance(package_id, str) or not package_id or "/" in package_id or package_id.startswith("."):
            return False
        return (self.root / "owned" / f"{package_id}.json").is_file()

    # LLM: 记录由宿主组装（安装、确认单、退回三处）；自动补时间和递增序号。有写文件副作用。
    # 函数用途: 追加一条安装或退回记录（只留最近 2000 条）。
    def record_install(self, entry: dict[str, object]) -> None:
        append_private_jsonl_capped(self.root / "installs.jsonl", {"at": _now(), "seq": self._next_seq(), **entry},
                                    max_records=_MAX_INSTALL_RECORD_COUNT)

    # LLM: 在 learnpack 存储目录锁里读改写 seq.json，返回严格递增的序号（跨线程、跨进程唯一）。seq.json 不存在（第一次用，或
    #   被删、备份漏掉）或读坏了，都从已有单子与安装记录里的最大序号接着往上（全新存储为 0），不会倒退（复审 5 轮）。有写文件副作用。
    # 函数用途: 取下一个事件序号。
    def _next_seq(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "seq.json"
        with locked_private_directory(self.root, lock_name=".seq.lock"):
            try:
                current = int(read_json_object(path)["seq"]) if path.is_file() else self._max_recorded_seq()
            except (OSError, KeyError, TypeError, ValueError):
                current = self._max_recorded_seq()
            write_private_json_object(path, {"seq": current + 1})
        return current + 1

    # LLM: 只在 seq.json 不存在或读坏时用；只读。
    # 函数用途: 找出已有单子与安装记录里最大的序号。
    def _max_recorded_seq(self) -> int:
        seqs = [order.seq for order in self._all_orders()]
        seqs += [row["seq"] for row in self.installs() if isinstance(row.get("seq"), int)]
        return max(seqs, default=0)

    # LLM: 坏行跳过不报错（展示用）；返回顺序与写入顺序一致。只读。
    # 函数用途: 读出全部安装记录。
    def installs(self) -> list[dict[str, object]]:
        return list(read_jsonl_objects_report(self.root / "installs.jsonl", context="learnpack.installs").records)

    # LLM: 内容寻址路径，只拼路径不读写。
    # 函数用途: 打包产物 zip 的位置。
    def _build_zip(self, sha256: str) -> Path:
        return self.root / "builds" / f"{sha256}.zip"

    # LLM: 与 zip 同名的打包记录，只拼路径不读写。
    # 函数用途: 打包记录 json 的位置。
    def _build_json(self, sha256: str) -> Path:
        return self.root / "builds" / f"{sha256}.json"


# LLM: 取不到修改时间（文件刚被删）就当最旧，排序不出错。只读。
# 函数用途: 取文件修改时间，供"最近的记录"排序。
def _modified_time(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# LLM: 内容寻址文件：已有且字节摘要相同就不再写；没有或不同（如上次写到一半）就写临时文件再原子替换，出生即 0600。
#   有写文件副作用。
# 函数用途: 把包字节落到按 sha256 命名的私有文件。
def _write_content_addressed(path: Path, payload: bytes) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# LLM: 出生即 0600 的独占创建；已存在返回 False（不覆盖）。有写文件副作用。
# 函数用途: 只在文件不存在时写一个私有 JSON 文件。
def _create_private_json(path: Path, payload: dict[str, object]) -> bool:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    return True


# LLM: 统一时间口径，供记录与单子使用；精确到微秒、位数固定，按文字比大小就是按时间先后（判断单子是否被后来的单或安装取代
#   要分清同一秒里的先后）。纯函数。
# 函数用途: 生成 UTC 微秒级时间戳文字。
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


__all__ = ["ORDER_ID_PATTERN", "STORE_PARTS", "BuildProvenance", "BuildRecord", "InstallOrder", "LearnpackStore"]
