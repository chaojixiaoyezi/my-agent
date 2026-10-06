# LLM: learnpack 的宿主存储，唯一位置 <owner home>/data/learnpack/（owner 根的 data/ 是 H3 宿主运行状态，模型工具只能读不能写）：
#   - builds/<sha256>.zip 与 builds/<sha256>.json：她打出的包字节与打包记录；"是不是她自己做的"只认这里，且读回时重算 sha256；
#   - orders/<单号>.json 与 orders/<单号>.done.json：开关关着时的待确认安装单，done 文件用独占创建保证一张单只执行一次；
#   - installs.jsonl：每次装、退回的记录，供 /plugins# 标"她做的"和找上一版。
#   这里只读写文件，不安装、不判断开关或身份；改布局要同步 path_access_policy 的 H3 说明与 test_learnpack_store。
# 模块用途: 保存她打的包、待确认安装单和安装记录，重启不丢，单号只执行一次。
from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common.json_io import (
    append_private_jsonl_capped,
    read_json_object,
    read_jsonl_objects_report,
    write_private_json_object,
)
from .package_build import BuiltPackage

# 相对 owner home 的存储位置（owner 根 data/ 下，宿主写、模型只读）。
STORE_PARTS = ("data", "learnpack")
# 待确认安装单号：lp- 加 8 位十六进制，短到能整行复制。
ORDER_ID_PATTERN = re.compile(r"lp-[0-9a-f]{8}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# 安装记录最多保留 2000 条：超限只丢最旧的，找"上一版"只看最近记录。
_MAX_INSTALL_RECORD_COUNT = 2000


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


# LLM: 待确认安装单只指向一个已存在的打包记录（sha256），不带路径、命令或权限；确认时宿主按单号重读。
# 类用途: 开关关着时生成、等用户确认的一张安装单。
@dataclass(frozen=True)
class InstallOrder:
    order_id: str
    sha256: str
    kind: str
    package_id: str
    version: str
    created_at: str


# LLM: 所有写入都在 root 下、权限 0600；读回打包产物时重算摘要，摘要不符按"没有这个包"处理。
# 类用途: 一个 owner 的 learnpack 存储。
class LearnpackStore:
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

    # LLM: 只有 zip 字节的 sha256 与文件名一致、记录可读且自洽时才返回；任何不符都返回 None（调用方按"不是她做的"拒绝）。
    # 函数用途: 按 sha256 取回她打过的包的记录。
    def build(self, sha256: str) -> BuildRecord | None:
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            return None
        try:
            payload = self._build_zip(sha256).read_bytes()
            raw = read_json_object(self._build_json(sha256))
            record = BuildRecord(**{key: raw[key] for key in BuildRecord.__dataclass_fields__})
        except (OSError, KeyError, TypeError, ValueError):
            return None
        if hashlib.sha256(payload).hexdigest() != sha256 or record.sha256 != sha256:
            return None
        return record

    # 函数用途: 返回某个打包产物在宿主存储里的路径（安装命令从这里读包）。
    def build_path(self, sha256: str) -> Path:
        return self._build_zip(sha256)

    # LLM: 单号随机生成，写入 orders/<单号>.json；不检查开关，调用方负责只在开关关着时建单。有写文件副作用。
    # 函数用途: 为一个打包记录开一张待确认安装单。
    def create_order(self, record: BuildRecord) -> InstallOrder:
        order = InstallOrder(f"lp-{secrets.token_hex(4)}", record.sha256, record.kind, record.package_id,
                             record.version, _now())
        write_private_json_object(self.root / "orders" / f"{order.order_id}.json", asdict(order))
        return order

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
    #   执行完由 finish_order 覆盖成结果；进程中途退出时占位保留，再确认会如实说"已执行过、结果未确认"。有写文件副作用。
    # 函数用途: 声明"这张单现在开始执行"，保证只执行一次。
    def claim_order(self, order_id: str) -> bool:
        path = self.root / "orders" / f"{order_id}.done.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write('{"state": "executing"}\n')
        return True

    # 函数用途: 把一张单的执行结果写进 done 文件（覆盖"执行中"占位）。有写文件副作用。
    def finish_order(self, order_id: str, outcome: dict[str, object]) -> None:
        write_private_json_object(self.root / "orders" / f"{order_id}.done.json", {"state": "finished", **outcome})

    # 函数用途: 读一张已执行单的结果（没执行过或读不出返回 None）。
    def order_outcome(self, order_id: str) -> dict[str, object] | None:
        path = self.root / "orders" / f"{order_id}.done.json"
        try:
            return read_json_object(path) if path.is_file() else None
        except (OSError, ValueError):
            return None

    # 函数用途: 追加一条安装或退回记录（只留最近 2000 条）。有写文件副作用。
    def record_install(self, entry: dict[str, object]) -> None:
        append_private_jsonl_capped(self.root / "installs.jsonl", {"at": _now(), **entry},
                                    max_records=_MAX_INSTALL_RECORD_COUNT)

    # LLM: 坏行跳过不报错（展示用）；返回顺序与写入顺序一致。只读。
    # 函数用途: 读出全部安装记录。
    def installs(self) -> list[dict[str, object]]:
        return list(read_jsonl_objects_report(self.root / "installs.jsonl", context="learnpack.installs").records)

    def _build_zip(self, sha256: str) -> Path:
        return self.root / "builds" / f"{sha256}.zip"

    def _build_json(self, sha256: str) -> Path:
        return self.root / "builds" / f"{sha256}.json"


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


# 函数用途: 生成 UTC 秒级时间戳文字。
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = ["ORDER_ID_PATTERN", "STORE_PARTS", "BuildProvenance", "BuildRecord", "InstallOrder", "LearnpackStore"]
