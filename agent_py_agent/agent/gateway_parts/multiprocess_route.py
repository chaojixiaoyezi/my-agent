# LLM: C/W 路由只投影 main 认证入口唯一的 host_request_owner.v1；不得另建 owner、actor 或准入权威。
#   只读纯盖章/校验，不启动 worker；受保护 metadata 断言须与宿主 owner 一致，admission 另核原 receipt，不能从变更队列自证归属。
# 模块用途: 将已有宿主归属、请求内容和启动分区绑定为路由，校验只返回结构化事实，不读配置、写盘或执行模型工具。
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal

from ..runtime_errors import DataCorruptionError
from ..user_space.owner_resolver import OwnerIdentity
from .io import (
    GATEWAY_REQUEST_FINGERPRINT_SCHEMA,
    gateway_request_fingerprint,
    validated_gateway_request_fingerprint,
)
from .request_owner_binding import REQUEST_OWNER_KEY, frozen_request_owner


# LLM: 错误只保存协议原因，不回显请求、owner 或凭据；盖章失败不能成为可执行请求。
# 类用途: 为可信盖章调用方提供与校验同源的结构化拒绝。
class OwnerRouteMismatchError(ValueError):
    error_code = "OWNER_ROUTE_MISMATCH"

    # LLM: reason 来自有限内部校验，不从正文生成；不发生 I/O。
    # 函数用途: 保存当前路由拒绝原因。
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)

    # LLM: 返回独立错误投影；它不授予重试、取消或释放准入的权限。
    # 函数用途: 给后续运输调用方返回稳定错误码和细分原因。
    def to_payload(self) -> dict:
        return {"ok": False, "error_code": self.error_code, "reason": self.reason}


# LLM: 盖章上下文只由 C 可信启动提供，不开放为请求参数或用户配置。
# 类用途: 保存本次固定分区数、代次、时间和管理员绑定版本。
@dataclass(frozen=True)
class RouteStamp:
    count: int
    c_run_id: str
    stamped_at: float
    admin_binding_version: str


# LLM: 分区身份由原进程启动冻结，不能从请求路由自行取值来证明执行权。
# 类用途: 保存校验方的可信当前分区；C 角色与整数 worker 互斥。
@dataclass(frozen=True)
class WorkerPartition:
    index: int | Literal["C"]
    count: int
    c_run_id: str


# LLM: owner 来自原 OwnerIdentity；跨进程统一 UTF-8/无空白 JSON，不用随机 Python hash。
# 函数用途: 计算规范 owner 三元组的完整 SHA256。
def owner_digest(owner: OwnerIdentity) -> str:
    return _digest([owner.provider, owner.owner_kind, owner.owner_id])


# LLM: 管理员映射已由原认证入口决定；这里只对冻结 owner 分区，不读取最新绑定或创建 owner。
# 函数用途: 将本机主用户固定到 C，其余 owner 映射到合法整数分区。
def owner_worker(owner: OwnerIdentity, count: int) -> int | Literal["C"]:
    if type(count) is not int or count <= 0:
        raise ValueError("worker count must be positive")
    if owner == OwnerIdentity.local_main():
        return "C"
    return int(owner_digest(owner), 16) % count


# LLM: owner与管理员版本须已在首次prepared摘要前冻结；这里只校验同值，不补metadata或重签原指纹，完整摘要/actor/media保持原样。
#   已发表route不得重盖；返回副本供原临界区持久化，route.owner只是投影，禁止接到客户端自盖章入口。
# 函数用途: 把既有不可变输入投影为当前代次路由；缺版本或版本冲突即拒绝，本函数不写盘。
def freeze_request_route(request: dict, owner: OwnerIdentity, stamp: RouteStamp) -> dict:
    try:
        if "route" in request:
            raise OwnerRouteMismatchError("already_published_route")
        if _request_owner(request) != owner:
            raise OwnerRouteMismatchError("request_owner")
        if (request.get("request_fingerprint_schema") != GATEWAY_REQUEST_FINGERPRINT_SCHEMA
                or not request.get("request_fingerprint")):
            raise OwnerRouteMismatchError("request_identity")
        validated_gateway_request_fingerprint(request, request["id"])
        metadata = request.get("metadata")
        if (type(metadata) is not dict or type(stamp.admin_binding_version) is not str
                or not stamp.admin_binding_version.strip()
                or metadata.get("gateway_admin_binding_version") != stamp.admin_binding_version):
            raise OwnerRouteMismatchError("admin_binding_version")
        result = copy.deepcopy(request)
        result["route"] = {
            "worker": owner_worker(owner, stamp.count), "owner": _owner_parts(owner),
            "owner_sha": owner_digest(owner), "c_run_id": stamp.c_run_id, "stamped_at": stamp.stamped_at,
            "binding": _request_binding(result),
        }
        if not isinstance(stamp.admin_binding_version, str) or not _complete_route(
            result["route"], WorkerPartition(result["route"]["worker"], stamp.count, stamp.c_run_id),
            coordinator=owner == OwnerIdentity.local_main(),
        ):
            raise OwnerRouteMismatchError("incomplete_record")
        return result
    except DataCorruptionError as exc:
        raise OwnerRouteMismatchError("request_identity") from exc
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError) as exc:
        if isinstance(exc, OwnerRouteMismatchError):
            raise
        raise OwnerRouteMismatchError("incomplete_record") from exc


# LLM: 校验在物化 owner/模型/工具之前使用；没有原宿主归属不能回退 main 或重新解析热配置。
# 函数用途: 校验冻结请求属于当前 worker，失败返回统一拒绝码。
def validate_worker_route(request: dict, partition: WorkerPartition) -> dict:
    return _validate_route(request, partition, coordinator=False)


# LLM: C 同样核对原 owner、请求和启动代次，不以管理员投影替代原认证事实。
# 函数用途: 只允许协调者使用完整且冻结为 local/main 的请求。
def validate_coordinator_route(request: dict, count: int, c_run_id: str) -> dict:
    return _validate_route(request, WorkerPartition("C", count, c_run_id), coordinator=True)


# LLM: 此入口只检查已发表记录的内部一致性；生产执行必须另外提供原 worker 分区，不能从此投影获得执行权。
# 函数用途: 按已有 C/W 投影复核盖章，用于未来派发和只读查询。
def validate_published_route(request: dict, count: int, c_run_id: str) -> dict:
    route = request.get("route") if isinstance(request, dict) else None
    worker = route.get("worker") if isinstance(route, dict) else None
    if worker == "C":
        return validate_coordinator_route(request, count, c_run_id)
    return validate_worker_route(request, WorkerPartition(worker, count, c_run_id))


# LLM: 原宿主 owner 和指纹的损坏统一失败关闭；错误投影不包含输入正文，不产生文件或授权副作用。
# 函数用途: 为 C/W 共用准确校验顺序与错误回执。
def _validate_route(request: dict, partition: WorkerPartition, *, coordinator: bool) -> dict:
    try:
        owner = _request_owner(request)
        reason = _route_rejection(request, owner, partition, coordinator=coordinator)
    except OwnerRouteMismatchError as exc:
        reason = exc.reason
    except DataCorruptionError:
        reason = "request_identity"
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
        reason = "incomplete_record"
    if reason:
        return OwnerRouteMismatchError(reason).to_payload()
    return {"ok": True, "owner": _owner_parts(owner)}


# LLM: route 不能自证归属；先核原 host owner 与主线指纹，再从它们重算绑定，时间不作执行权限。
# 函数用途: 返回首个失败条件，空字符串表示冻结事实与当前分区一致。
def _route_rejection(request: dict, owner: OwnerIdentity, partition: WorkerPartition, *, coordinator: bool) -> str:
    route = request["route"]
    if not _complete_route(route, partition, coordinator=coordinator):
        return "incomplete_record"
    if route["c_run_id"] != partition.c_run_id:
        return "old_generation"
    if (owner == OwnerIdentity.local_main()) != coordinator:
        return "worker_owner" if coordinator else "coordinator_owner"
    if route["owner"] != _owner_parts(owner):
        return "request_owner"
    if route["owner_sha"] != owner_digest(owner):
        return "owner_digest"
    if route["worker"] != partition.index or owner_worker(owner, partition.count) != partition.index:
        return "wrong_partition"
    if (request["request_fingerprint_schema"] != GATEWAY_REQUEST_FINGERPRINT_SCHEMA
            or request["request_fingerprint"] != gateway_request_fingerprint(request, request["id"])):
        return "request_fingerprint"
    if route["binding"] != _request_binding(request):
        return "request_binding"
    return ""


# LLM: bool 不能充当编号，NaN/无穷/未知代次不构成协议；校验方的分区来自原启动上下文。
# 函数用途: 检查最小路由字段和 C/W 互斥的可信分区。
def _complete_route(route: dict, partition: WorkerPartition, *, coordinator: bool) -> bool:
    if (type(partition.count) is not int or partition.count <= 0
            or type(partition.c_run_id) is not str or not partition.c_run_id.strip()):
        return False
    if coordinator:
        if partition.index != "C":
            return False
    elif type(partition.index) is not int or not 0 <= partition.index < partition.count:
        return False
    required = {"worker", "owner", "owner_sha", "c_run_id", "stamped_at", "binding"}
    if not isinstance(route, dict) or not required <= route.keys():
        return False
    worker_valid = route["worker"] == "C" if coordinator else type(route["worker"]) is int
    time_value = route["stamped_at"]
    return (worker_valid and type(time_value) in {int, float}
            and math.isfinite(time_value) and time_value >= 0
            and type(route["c_run_id"]) is str and bool(route["c_run_id"].strip()))


# LLM: 原 main 顶层owner仍唯一；存在的metadata断言已受原指纹保护，只比较、不派生身份。旧无断言纯合同兼容，不据此声称新C身份已完整冻结。
# 函数用途: 读取原宿主身份并核对其受保护断言，防止只改顶层owner后重新自洽盖章。
def _request_owner(request: dict) -> OwnerIdentity:
    owner = frozen_request_owner(request)
    if owner is None:
        raise OwnerRouteMismatchError("missing_request_owner")
    metadata = request.get("metadata")
    if (isinstance(metadata, dict) and metadata.get(REQUEST_OWNER_KEY) is not None
            and frozen_request_owner({REQUEST_OWNER_KEY: metadata[REQUEST_OWNER_KEY]}) != owner):
        raise OwnerRouteMismatchError("request_owner")
    return owner


# LLM: 数组只作跨进程投影，不替代原 OwnerIdentity 或宿主请求字段。
# 函数用途: 返回规范 owner 的 JSON 三元组。
def _owner_parts(owner: OwnerIdentity) -> list[str]:
    return [owner.provider, owner.owner_kind, owner.owner_id]


# LLM: 入口字段来自已认证原请求；完整 metadata、宿主 actor 和媒体引用另由主线指纹保护，不重新查询绑定。
# 函数用途: 提取与来源版本一致的入口身份及冻结管理员版本。
def _ingress_identity(request: dict) -> dict:
    metadata = request["metadata"]
    return {"provider": metadata.get("provider") or metadata.get("channel"),
            "channel": metadata.get("channel"), "user_id": metadata.get("user_id") or request.get("user_id"),
            "chat_id": metadata.get("chat_id"),
            "admin_binding_version": metadata["gateway_admin_binding_version"]}


# LLM: 绑定读取原宿主 owner 和主线完整指纹，不以 route 自身字段生成自洽的错误归属证明。
# 函数用途: 计算请求内容、入口身份和原归属的独立冻结绑定。
def _request_binding(request: dict) -> str:
    return _digest({"request_id": request["id"],
                    "request_fingerprint": gateway_request_fingerprint(request, request["id"]),
                    "ingress_identity": _ingress_identity(request),
                    "owner": _owner_parts(_request_owner(request))})


# LLM: 所有路由摘要统一规范 JSON 编码，不接受 NaN 或对象默认转换，不发生 I/O。
# 函数用途: 生成跨进程一致的完整 SHA256。
def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
