# LLM: curator冻结可接C显式时间配置作资格筛选，ref/schema/读取默认语义不变，配置不成为运输授权。
# LLM: 后台运输原ref/摘要与同步末门；长读在门外，原输入T先取，生产C/lease/Root只包短写；P3/P5唯一，callback不成新许可。
# 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。
# 模块用途: 只读冻结原文件或RuntimeDB行来源，按原输入幂等合同提交后台请求；不复制正文作模型prompt。
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath
from ..common.opaque_id import validate_path_segment
from ..common.strict_json import load_strict_json
from ..conversation.models import ProgressPolicy, WakeSignal
from ..memory_store.curator_models import MemoryCuratorState
from ..runtime_errors import DataCorruptionError
from ..user_space.owner_resolver import OwnerIdentity, _owner_home_dir
from .request_owner_binding import REQUEST_OWNER_KEY, frozen_request_owner

# 单份原来源读取预算，单位字节；超限失败，不截断后冒充完整来源。
_SOURCE_MAX_BYTES = 1_048_576
_SCHEMA = "gateway_background_source.v1"
_FIELDS = {"schema_version", "kind", "owner", "source_ref", "source_sha256", "source_exists", "input_revision", "store_ref", "source_id", "thread_id"}


# LLM: 只有精确持久来源漂移使用此typed信号；调用方按异常类型收口运输，不解析普通异常正文作机器决策。
# 类用途: 表示原来源已被处理、删除或换代，当前运输不能消费新来源。
class BackgroundSourceChanged(ValueError):
    reason = "background_source_changed"

    # LLM: 文本只供人读和现有错误断言；业务分流必须读取类型或reason字段。
    # 函数用途: 创建稳定的来源漂移错误。
    def __init__(self):
        super().__init__(self.reason)


# LLM: 规范JSON摘要只服务原来源/输入幂等身份，不将它变成内核代次或执行许可。
# 函数用途: 计算完整结构化来源的稳定摘要。
def source_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


# LLM: owner来自当前C metadata发现或已冻结P5；通过main唯一host owner解码器拒绝非规范投影，不清洗成另一身份或另授来源权限。
# 函数用途: 严格恢复源引用中的规范owner。
def source_owner(value):
    if type(value) is not dict or set(value) != {"provider", "owner_kind", "owner_id"}:
        raise ValueError("background_source_owner_invalid")
    try:
        owner = frozen_request_owner({REQUEST_OWNER_KEY: value})
    except DataCorruptionError as error:
        raise ValueError("background_source_owner_invalid") from error
    for part in (owner.provider, owner.owner_id):
        validate_path_segment(part, kind="owner")
    if owner.owner_kind not in {"user", "group"} and owner != OwnerIdentity.local_main():
        raise ValueError("background_source_remote_owner_required")
    return owner


# LLM: refs限定既有owner canonical文件、原ProcessSession或runtime.db行；DB摘要只绑定该行，不接受任意路径、文件hash或模型字段授后台权。
# 函数用途: 校验路径形状与源引用的精确字段。
def validate_source_ref(value):
    if type(value) is not dict or set(value) != _FIELDS or value["schema_version"] != _SCHEMA:
        raise ValueError("background_source_ref_invalid")
    source_owner(value["owner"])
    for key in ("source_ref", "source_id", "thread_id", "store_ref", "source_sha256", "input_revision"):
        if type(value[key]) is not str:
            raise ValueError("background_source_ref_invalid")
    digest = value["source_sha256"]
    if type(value["source_exists"]) is not bool or (not value["source_exists"] and (value["kind"] not in {"curator", "maintenance"} or digest)):
        raise ValueError("background_source_presence_invalid")
    if value["source_exists"] and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
        raise ValueError("background_source_digest_invalid")
    parts = Path(value["source_ref"]).parts
    if not parts or Path(value["source_ref"]).is_absolute() or Path(value["source_ref"]).as_posix() != value["source_ref"] or any(p in {".", "..", ""} for p in parts):
        raise ValueError("background_source_path_invalid")
    if value['kind'] == 'process_completion':
        from .process_completion_source import validate_process_completion_ref
        validate_process_completion_ref(value)
        return value
    if value['kind'] == 'runtime_wake':
        from .runtime_wake_source import validate_runtime_wake_ref
        validate_runtime_wake_ref(value)
        return value
    if value["kind"] == "learning":
        _validate_learning_ref(value, parts)
        return value
    if value["kind"] == "harvester":
        if (len(parts) != 2 or parts[0] != "watch_state" or not re.fullmatch(r"ws-[0-9a-f]{10}\.json", parts[1])
                or value["source_id"] != Path(parts[1]).stem or value["thread_id"] or value["store_ref"]
                or not re.fullmatch(r"[0-9a-f]{64}", value["input_revision"])):
            raise ValueError("background_source_path_invalid")
        return value
    if value["kind"] == "orphan":
        if (len(parts) < 5 or parts[-4:-2] != ("work", "agents") or parts[-1] != "canonical_state.json"
                or parts[-2] != value["source_id"] or value["thread_id"] or value["store_ref"]
                or not re.fullmatch(r"[0-9a-f]{64}", value["input_revision"])):
            raise ValueError("background_source_path_invalid")
        validate_path_segment(value["source_id"], kind="run_id")
        return value
    if value["kind"] == "maintenance":
        if (value["source_ref"] != "data/maintenance.json" or value["thread_id"] or value["store_ref"]
                or not re.fullmatch(r"[0-9a-f]{64}", value["input_revision"])):
            raise ValueError("background_source_path_invalid")
        if value["source_id"] != source_digest({"state_sha256": digest, "policy_revision": value["input_revision"]}):
            raise ValueError("background_source_identity_changed")
        return value
    if value["kind"] == "curator":
        if (value["source_ref"] != "memory/curator/state.json" or value["store_ref"] or value["thread_id"] or not value["input_revision"]):
            raise ValueError("background_source_path_invalid")
    elif value["kind"] in {"wake", "policy", "observation"}:
        if value["kind"] != "observation" and value["input_revision"]:
            raise ValueError("background_source_ref_invalid")
        _validate_wake_ref(value, parts)
    elif value["kind"] == "scheduler":
        if value["source_ref"] != "data/scheduler/store.json" or value["store_ref"] or value["thread_id"] or value["source_id"] != digest or value["input_revision"]:
            raise ValueError("background_source_path_invalid")
    else:
        raise ValueError("background_source_kind_invalid")
    return value


# LLM: 学习来源只选原FIFO文件或唯一registry维护；完整SHA/同fd时间仅限制运输CAS，不是用户批准或新owner权。
# 函数用途: 验证原自学习来源的规范路径形状。
def _validate_learning_ref(value, parts):
    if parts == ("data", "skill_learning", "registry.json"):
        if (value["thread_id"] or value["store_ref"] or not re.fullmatch(r"-?[0-9]+", value["input_revision"])
                or str(int(value["input_revision"])) != value["input_revision"]
                or value["source_id"] != source_digest({"registry_sha256": value["source_sha256"], "modified_ns": value["input_revision"]})):
            raise ValueError("background_source_path_invalid")
        return
    if (len(parts) != 4 or parts[:3] != ("data", "skill_learning", "requests")
            or not re.fullmatch(r"[0-9]{13}-[0-9a-f]{16}\.json", parts[-1])
            or parts[-1][-21:-5] != value["source_id"] or value["thread_id"]
            or value["store_ref"] or value["input_revision"]):
        raise ValueError("background_source_path_invalid")


# LLM: canonical会话store位置复用原发现层的三种形态；身份ref不允许选择另一个任意目录。
# 函数用途: 校验原wake队列位置与精确信号/线程编号。
def _validate_wake_ref(value, parts):
    validate_path_segment(value["source_id"], kind="background_source_id")
    validate_path_segment(value["thread_id"], kind="thread_id")
    store = Path(value["store_ref"])
    known = store.parts in {("conversations",), ("data", "conversations")}
    workspace = store.parts
    if len(workspace) == 5 and workspace[:3] == ("workspace", "runtime", "workspaces") and workspace[4] == "conversations":
        validate_path_segment(workspace[3], kind="workspace")
        known = True
    if value["kind"] == "observation":
        valid = parts[:-2] == store.parts and parts[-2] == "observations"
        if (value["source_id"] != value["source_sha256"] or not value["input_revision"]
                or parts[-1] != value["thread_id"] + ".jsonl"):
            raise ValueError("background_source_path_invalid")
        if not known or not valid:
            raise ValueError("background_source_path_invalid")
        return
    if value["kind"] == "policy":
        valid = parts[:-2] == store.parts and parts[-2] == "progress_policies"
    else:
        valid = parts[:-3] == store.parts and parts[-3] == "wake_queue" and parts[-2] in {"urgent", "normal"}
    if not known or not valid or parts[-1] != value["source_id"] + ".json":
        raise ValueError("background_source_path_invalid")


# LLM: 文件/ProcessSession沿原no-follow合同；runtime_wake只读原owner DB整行摘要，来源漂移拒旧运输，不能用DB文件变化否定别的行。
# 函数用途: 重读并验证一份精确原来源，返回业务层现存载荷。
def read_source(home_root, ref):
    validate_source_ref(ref)
    if ref['kind'] == 'process_completion':
        from .process_completion_source import read_process_completion_source
        return read_process_completion_source(home_root, ref)
    if ref['kind'] == 'runtime_wake':
        from .runtime_wake_source import read_runtime_wake_source
        return read_runtime_wake_source(home_root, ref)
    owner_home = _owner_home_dir(Path(home_root), source_owner(ref["owner"]))
    if ref["kind"] == "learning" and ref["source_ref"] == "data/skill_learning/registry.json":
        from .multiprocess_background_sources import learning_registry_snapshot

        try:
            raw, revision = learning_registry_snapshot(owner_home, _SOURCE_MAX_BYTES)
        except FileNotFoundError as error:
            raise BackgroundSourceChanged() from error
        if revision != ref["input_revision"]:
            raise BackgroundSourceChanged()
    else:
        raw = read_bytes_beneath(owner_home, Path(ref["source_ref"]).parts,
                                 max_bytes=_SOURCE_MAX_BYTES, require_dir_fd=True)
    if (raw is None) != (not ref["source_exists"]) or (raw is not None and hashlib.sha256(raw).hexdigest() != ref["source_sha256"]):
        raise BackgroundSourceChanged()
    if ref["kind"] == "observation":
        from .multiprocess_background_sources import observation_handled, observation_payload

        handled, revision = observation_handled(owner_home, ref["store_ref"], _SOURCE_MAX_BYTES)
        if revision != ref["input_revision"]:
            raise BackgroundSourceChanged()
        return observation_payload(raw, ref["thread_id"], handled)
    value = {} if raw is None else load_strict_json(raw)
    if type(value) is not dict:
        raise ValueError("background_source_invalid")
    if ref["kind"] == "orphan":
        from ..common.canonical_source_contract import canonical_child_source_contract
        from ..user_space.owner_resolver import _owner_home_result

        owner = source_owner(ref["owner"])
        canonical_child_source_contract().validate(value, owner_home, ref["source_ref"], _owner_home_result(Path(home_root), owner, owner_home).owner_id)
    if ref["kind"] == "maintenance":
        from .multiprocess_background_sources import maintenance_due, maintenance_policy

        policy, revision = maintenance_policy(owner_home, _SOURCE_MAX_BYTES)
        if revision != ref["input_revision"]:
            raise BackgroundSourceChanged()
        maintenance_due(value, policy, 0)
        return {"state": value, "policy": policy.to_dict()}
    _validate_source_payload(ref, value)
    if ref["kind"] == "curator" and curator_input_revision(owner_home) != ref["input_revision"]:
        raise BackgroundSourceChanged()
    return value


# LLM: 校验只认原wake/curator schema及结构化pending代次；不按reason正文或状态口头别名派工。
# 函数用途: 让引用身份与原持久业务身份一一对应。
def _validate_source_payload(ref, value):
    if ref["kind"] == "learning":
        if ref["source_ref"] == "data/skill_learning/registry.json":
            from ..capability.skill_learning_store import _registry_from

            _registry_from(value)
        else:
            _validate_learning_payload(ref, value)
        return
    if ref["kind"] == "harvester":
        from ..ingestion.harvest_execution import (
            harvester_input_revision,
            validate_harvester_payload,
        )
        from ..user_space.owner_resolver import _owner_home_result

        owner = source_owner(ref["owner"])
        validate_harvester_payload(value, owner_id=_owner_home_result(Path("/"), owner, Path("/")).owner_id,
                                   watch_id=ref["source_id"])
        if harvester_input_revision(value) != ref["input_revision"]:
            raise BackgroundSourceChanged()
        return
    if ref["kind"] == "orphan":
        from ..common.canonical_source_contract import canonical_child_source_contract

        if ref["source_id"] != value.get("id") or source_digest(canonical_child_source_contract().identity(value)) != ref["input_revision"]:
            raise ValueError("background_source_identity_changed")
        return
    if ref["kind"] == "wake":
        signal = WakeSignal.from_dict(value)
        if signal.wake_signal_id != ref["source_id"] or signal.thread_id != ref["thread_id"]:
            raise ValueError("background_source_identity_changed")
    elif ref["kind"] == "policy":
        policy = ProgressPolicy.from_dict(value)
        if policy.policy_id != ref["source_id"] or policy.thread_id != ref["thread_id"] or type(value.get("enabled", True)) is not bool:
            raise ValueError("background_source_identity_changed")
    elif ref["kind"] == "curator":
        state = MemoryCuratorState.from_dict(value)
        identity = {"pending_reasons": state.pending_reasons,
                    "pending_reason_generations": state.pending_reason_generations,
                    "pending_requested_at": state.pending_requested_at}
        identity["input_revision"] = ref["input_revision"]
        identity["state_sha256"] = ref["source_sha256"]
        if ref["source_id"] != source_digest(identity):
            raise ValueError("background_source_identity_changed")
    else:
        expected_owner = source_owner(ref["owner"])
        from ..user_space.owner_resolver import _owner_home_result

        canonical = _owner_home_result(Path("/"), expected_owner, Path("/"))
        if value.get("owner") != {"provider": expected_owner.provider, "kind": expected_owner.owner_kind, "id": canonical.owner_id} or type(value.get("jobs")) is not dict or type(value.get("runs")) is not dict:
            raise ValueError("background_source_identity_changed")


# LLM: FIFO沿main request_key/schema，维护沿main唯一registry解析器；完整SHA/时间由read_source核，不引入旧draft状态。
# 函数用途: 核对准确FIFO来源身份，main v1没有pending_generation字段，不能借新版状态拒绝全部请求。
def _validate_learning_payload(ref, value):
    from ..capability.skill_learning_store import REQUEST_SCHEMA_VERSION

    if (value.get("schema_version") != REQUEST_SCHEMA_VERSION
            or value.get("request_key") != ref["source_id"]):
        raise ValueError("background_source_identity_changed")

# LLM: C策展发现显式传原实际时间配置，freeze只是候选引用不授提交权；registry仍同fd SHA/mtime，后继提交重新核来源与资格。
# 函数用途: 从已有wake或策展状态冻结一个只读运输引用。
def freeze_source(home_root, owner, path, kind, *, curator_config=None, timezone_name=""):
    source_owner(asdict(owner))
    home = _owner_home_dir(Path(home_root), owner)
    relative = Path(path).relative_to(home)
    raw = read_bytes_beneath(home, relative.parts, max_bytes=_SOURCE_MAX_BYTES, require_dir_fd=True)
    if raw is None and kind not in {"curator", "maintenance"}:
        raise ValueError("background_source_missing")
    if kind == "curator":
        from ..owner_wake_discovery import _has_pending_memory_curator_work

        if not _has_pending_memory_curator_work(home,curator_config=curator_config,timezone_name=timezone_name):
            raise ValueError("background_source_not_due")
    value = {} if raw is None or kind == "observation" else load_strict_json(raw)
    ref = {"schema_version": _SCHEMA, "kind": kind, "owner": asdict(owner),
           "source_ref": relative.as_posix(), "source_sha256": "" if raw is None else hashlib.sha256(raw).hexdigest(),
           "source_exists": raw is not None, "input_revision": "",
           "store_ref": "", "source_id": "", "thread_id": ""}
    if kind == "learning":
        if relative.parts == ("data", "skill_learning", "registry.json"):
            from .multiprocess_background_sources import learning_registry_snapshot

            raw, revision = learning_registry_snapshot(home, _SOURCE_MAX_BYTES)
            ref.update(source_sha256=hashlib.sha256(raw).hexdigest(), input_revision=revision)
            ref["source_id"] = source_digest({"registry_sha256": ref["source_sha256"], "modified_ns": revision})
        else:
            ref["source_id"] = relative.name[-21:-5]
        read_source(home_root, ref)
        return ref
    if kind == "harvester":
        from ..ingestion.harvest_execution import harvester_input_revision

        ref["source_id"] = relative.stem
        ref["input_revision"] = harvester_input_revision(value)
        read_source(home_root, ref)
        return ref
    if kind == "orphan":
        from ..common.canonical_source_contract import canonical_child_source_contract

        ref["source_id"] = value.get("id")
        ref["input_revision"] = source_digest(canonical_child_source_contract().identity(value))
        read_source(home_root, ref)
        return ref
    if kind == "maintenance":
        from .multiprocess_background_sources import maintenance_policy

        _, revision = maintenance_policy(home, _SOURCE_MAX_BYTES)
        ref["input_revision"] = revision
        ref["source_id"] = source_digest({"state_sha256": ref["source_sha256"], "policy_revision": revision})
        read_source(home_root, ref)
        return ref
    if kind == "observation":
        from .multiprocess_background_sources import observation_handled

        store_ref = relative.parents[1].as_posix()
        _, revision = observation_handled(home, store_ref, _SOURCE_MAX_BYTES)
        ref.update(store_ref=store_ref, source_id=ref["source_sha256"], thread_id=relative.stem, input_revision=revision)
    elif kind == "wake":
        signal = WakeSignal.from_dict(value)
        ref.update(store_ref=relative.parents[2].as_posix(), source_id=signal.wake_signal_id, thread_id=signal.thread_id)
    elif kind == "policy":
        policy = ProgressPolicy.from_dict(value)
        ref.update(store_ref=relative.parents[1].as_posix(), source_id=policy.policy_id, thread_id=policy.thread_id)
    elif kind == "curator":
        state = MemoryCuratorState.from_dict(value)
        ref["input_revision"] = curator_input_revision(home)
        ref["source_id"] = source_digest({"pending_reasons": state.pending_reasons,
            "pending_reason_generations": state.pending_reason_generations,
            "pending_requested_at": state.pending_requested_at, "input_revision": ref["input_revision"],
            "state_sha256": ref["source_sha256"]})
    else:
        ref["source_id"] = ref["source_sha256"]
    read_source(home_root, ref)
    return ref


# LLM: 原curator输入metadata revision只区分发现代次，绝不授权消费；最终原collector/lease仍重读实际输入与覆盖。
# 函数用途: 让周期策展源在原消息/审计/候选或state变化后得到新的运输幂等键。
def curator_input_revision(owner_home):
    from ..owner_wake_discovery import _owner_fact_signature

    parts = _owner_fact_signature(owner_home)
    relevant = [(name, payload) for name, payload in parts
                if name.startswith("messages:") or name in {"audit", "memory/candidates.jsonl", "memory/curator/state.json"}]
    return source_digest(relevant)


# LLM: 可信内部producer从已核canonical ref运输唯一host owner，不借channel重新派owner；source长读在T/C门外，原输入T先于C/lease末门，入队不授P5权，消费者仍重核来源。
# 函数用途: 提交一次规范后台请求，让生产C关闭栅栏覆盖真正落盘，默认接口保原Count0运输。
def enqueue_source(paths, home_root, ref, *, commit_transition=None):
    from .io import write_gateway_request_once
    from .multiprocess_mode import current_worker_partition

    if current_worker_partition() is not None:
        raise ValueError("background_source_coordinator_required")
    read_source(home_root, ref)
    digest = source_digest(ref)
    owner = source_owner(ref["owner"])
    payload = {"id": "background-" + digest, "kind": "background", "status": "pending",
               "source": "gateway_background", "user_id": owner.owner_id, REQUEST_OWNER_KEY: asdict(owner),
               "metadata": {"background_source": ref, "client_input_digest": digest,
                            "channel": owner.provider}, "priority": "background"}
    return write_gateway_request_once(paths, payload, commit_transition=commit_transition)
