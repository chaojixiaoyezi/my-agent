# LLM: 审核状态仅宿主data/managed_user_governance权威；owner副本只读展示，决定不得从镜像恢复批准。
# 模块用途: 保存受管用户通用提审、带CAS批复、按目标应用与原会话通知。
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..common.nofollow_fs import open_directory_beneath
from ..plugin_host.errors import HostAPIError
from .local_user_lifecycle import now_utc
from .local_user_store import LocalUserStore
from .managed_user_governance import (
    ManagedUserGovernanceService,
    ManagedUserScopeDeniedError,
    _GovernanceControl,
    _managed_conversations,
    _require_revision,
    resolve_managed_local_user,
)
from .owner_resolver import OwnerIdentity

_REVIEW_TYPES = frozenset({"capability_elevation", "high_risk_command", "asset_enable", "schedule_create", "memory_promotion", "persona_change"})
_REVIEW_ID = re.compile(r"grv-[0-9a-f]{32}\Z")
_SCHEMA = "governance_review_item.v1"


# LLM: item是完整冻结快照，requested_action只是数据，不签发权限；调用者不能把DTO当批准令牌。
# 类用途: 表示一个用户提交的通用审核单及其修订。
@dataclass(frozen=True)
class GovernanceReviewItem:
    review_id: str
    applicant_owner_id: str
    review_type: str
    requested_action: dict[str, Any]
    reason: str = ""
    risk_summary: dict[str, Any] | None = None
    applicant_thread_id: str = ""
    applicant_task_id: str = ""
    applicant_run_id: str = ""
    status: str = "pending_admin_review"
    revision: int = 1
    feedback: str = ""
    created_at: str = ""
    updated_at: str = ""
    decided_by: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    history: tuple[dict[str, Any], ...] = ()
    schema_version: str = _SCHEMA
    decision_intent: dict[str, Any] = field(default_factory=dict)
    effect_applied_at: str = ""

    # LLM: 投影为独立JSON容器，target_owner_id始终由申请身份派生，不能独立改写。
    # 函数用途: 为Host和工具返回完整审核单及准确目标。
    def to_dict(self) -> dict[str, Any]:
        return {**copy.deepcopy(asdict(self)), "target_owner_id": self.applicant_owner_id}


# LLM: 构造依赖必须由已认证host注入；普通申请者只可向自己提审，只有local/main可读取全量及决定。
# 类用途: 管理受管用户的唯一审核状态机。
class GovernanceReviewStore:
    # LLM: root与actor都是宿主依赖，不读取模型提供的actor/updated_by作为权限。
    # 函数用途: 冷绑定审核根和真实调用方。
    def __init__(self, root_dir: Path, *, actor_owner: OwnerIdentity | None = None):
        self.root_dir = Path(root_dir).resolve()
        self.actor_owner = actor_owner if actor_owner is not None else OwnerIdentity.local_main()

    # LLM: 决定和全量查询必须由主管理员调用，调用适配层仍须拒绝子代理。
    # 函数用途: 复核宿主注入的管理员身份。
    def _require_admin(self) -> None:
        if self.actor_owner != OwnerIdentity.local_main():
            raise ManagedUserScopeDeniedError()

    # LLM: 只登记申请，不执行requested_action；index和authority均在宿主控制根，镜像不能建立新单。
    # 函数用途: 为登记的受管用户创建待管理员审核项。
    def submit_review(self, applicant_owner_id: str, review_type: str, requested_action: dict[str, Any], reason: str = "",
                      risk_summary: dict[str, Any] | None = None, applicant_thread_id: str = "",
                      applicant_task_id: str = "", applicant_run_id: str = "") -> GovernanceReviewItem:
        uid, identity, home = resolve_managed_local_user(self.root_dir, applicant_owner_id)
        if self.actor_owner not in {OwnerIdentity.local_main(), identity}:
            raise ManagedUserScopeDeniedError()
        if review_type not in _REVIEW_TYPES or type(requested_action) is not dict:
            raise ValueError("invalid review type or requested action")
        if any(type(value) is not str for value in (reason, applicant_thread_id, applicant_task_id, applicant_run_id)):
            raise ValueError("invalid applicant metadata")
        if risk_summary is not None and type(risk_summary) is not dict:
            raise ValueError("invalid risk summary")
        action = json.loads(json.dumps(requested_action, allow_nan=False))
        if applicant_thread_id:
            thread = _managed_conversations(home, thread_id=applicant_thread_id).threads.require(applicant_thread_id)
            if thread.owner_id != home.owner_id:
                raise ManagedUserScopeDeniedError()
        control = _GovernanceControl(self.root_dir, uid)
        if (review_type == "capability_elevation" and self.actor_owner != OwnerIdentity.local_main()
                and not control.policy()["execution_governance"]["allow_capability_elevation_request"]):
            raise HostAPIError("CAPABILITY_ELEVATION_REQUEST_DISABLED", 403)
        timestamp, review_id = now_utc(), f"grv-{uuid4().hex}"
        item = GovernanceReviewItem(review_id, home.owner_id, review_type, action, reason, copy.deepcopy(risk_summary),
                                    applicant_thread_id, applicant_task_id, applicant_run_id, created_at=timestamp, updated_at=timestamp,
                                    history=({"status": "pending_admin_review", "at": timestamp},))
        with control.locked(review_id):
            control.store.write_json(("data", "managed_user_governance", ".review_index", review_id + ".json"), {"user_id": uid})
            control.write(("governance_reviews", review_id + ".json"), item.to_dict(), ("data", "governance_reviews", review_id + ".json"))
        return item

    # LLM: 列表仅扫描登记本地用户的宿主权威目录，拒绝链接和坏项，不把坏数据当空队列。
    # 函数用途: 按准确目标、状态和审核类型查询治理审核项。
    def list_reviews(self, target_owner_id: str | None = None, status_filter: str | None = None,
                     review_type_filter: str | None = None) -> dict[str, Any]:
        self._require_admin()
        if target_owner_id is not None:
            users = [resolve_managed_local_user(self.root_dir, target_owner_id)]
        else:
            users = [resolve_managed_local_user(self.root_dir, row.user_id) for row in LocalUserStore(self.root_dir).list_users()
                     if row.status in {"enabled", "disabled"}]
        items = []
        for uid, _identity, _home in users:
            control = _GovernanceControl(self.root_dir, uid)
            try:
                descriptor = open_directory_beneath(self.root_dir, (*control.prefix, "governance_reviews"))
            except FileNotFoundError:
                continue
            try:
                names = [entry.name for entry in os.scandir(descriptor) if entry.name.endswith(".json")]
            finally:
                os.close(descriptor)
            for name in sorted(names):
                item = _read_review(control, name[:-5])
                if ((status_filter is None or item["status"] == status_filter)
                        and (review_type_filter is None or item["review_type"] == review_type_filter)):
                    items.append(item)
        items.sort(key=lambda item: (item["created_at"], item["review_id"]))
        return {"items": items, "count": len(items)}

    # LLM: review/target同锁校验；同意图中间态恢复，终态重入只修镜像/通知，异意图或错误CAS拒绝。
    # 函数用途: 批复或恢复一个目标绑定的审核事务。
    def decide_review(self, review_id: str, decision: str, feedback: str = "", expected_revision: int | None = None,
                      decided_by: str = "local/main", patch_override: dict[str, Any] | None = None,
                      *, target_owner_id: str | None = None) -> dict[str, Any]:
        self._require_admin()
        if not _REVIEW_ID.fullmatch(review_id) or decision not in {"approve", "reject_with_feedback"}:
            raise ValueError("invalid review decision")
        if decided_by != "local/main":
            raise ManagedUserScopeDeniedError()
        if type(feedback) is not str or decision == "reject_with_feedback" and not feedback.strip():
            raise HostAPIError("REVIEW_FEEDBACK_REQUIRED", 400)
        store = LocalUserStore(self.root_dir)
        index_parts = ("data", "managed_user_governance", ".review_index", review_id + ".json")
        index = store.read_json(index_parts)
        if index is None:
            raise HostAPIError("REVIEW_NOT_FOUND", 404)
        uid, _identity, home = resolve_managed_local_user(self.root_dir, index["user_id"])
        control = _GovernanceControl(self.root_dir, uid)
        with control.locked(review_id):
            if store.read_json(index_parts) != index:
                raise HostAPIError("REVISION_CONFLICT", 409)
            item = _read_review(control, review_id)
            if target_owner_id is not None and resolve_managed_local_user(self.root_dir, target_owner_id)[0] != uid:
                raise ManagedUserScopeDeniedError()
            request = {"decision": decision, "feedback": feedback, "decided_by": decided_by,
                       "patch_override": copy.deepcopy(patch_override)}
            return _decide_review_locked(control, home, item, request, expected_revision)

    # LLM: 消费者必须同owner/类型且提供非空完整动作或SHA摘要；省略匹配不得消费，原review锁内一次性发表。
    # 函数用途: 核验并消费管理员签发的精确动作票据，拒绝跨用户和重复消费。
    def consume_approved_action(self, review_id: str, *, target_user: str, review_type: str,
                                action_match: dict[str, Any] | str | None = None) -> dict[str, Any]:
        uid, identity, _home = resolve_managed_local_user(self.root_dir, target_user)
        if self.actor_owner not in {OwnerIdentity.local_main(), identity}:
            raise ManagedUserScopeDeniedError()
        control = _GovernanceControl(self.root_dir, uid)
        with control.locked(review_id):
            item = _read_review(control, review_id)
            record = control.store.read_json((*control.prefix, "approved_actions", review_id + ".json"))
            if (item["status"] != "applied" or review_type not in {"capability_elevation", "high_risk_command"}
                    or item["review_type"] != review_type or not record or record.get("target_owner_id") != f"local/{uid}"
                    or record.get("review_type") != review_type or not record.get("approval_ticket")):
                raise HostAPIError("APPROVAL_ACTION_MISMATCH", 409)
            if type(action_match) not in {dict, str} or not action_match:
                raise HostAPIError("APPROVAL_ACTION_MISMATCH", 409)
            try:
                expected = _action_digest(action_match) if type(action_match) is dict else action_match
            except (ValueError, TypeError):
                raise HostAPIError("APPROVAL_ACTION_MISMATCH", 409) from None
            if expected != record["action_sha256"]:
                raise HostAPIError("APPROVAL_ACTION_MISMATCH", 409)
            if record.get("consumed_at"):
                raise HostAPIError("APPROVAL_ALREADY_CONSUMED", 409)
            record.update(consumed_at=now_utc(), consumed_by=self.actor_owner.owner_id)
            control.write(("approved_actions", review_id + ".json"), record)
            return copy.deepcopy(record)


# LLM: 决策补丁必须有限JSON且不能改归属；恢复意图包含补丁、actor、反馈与决策前修订。
# 函数用途: 验证初次CAS或幂等重入意图。
def _prepare_decision(item, request, expected_revision):
    patch = request["patch_override"]
    if patch is not None and (type(patch) is not dict or set(patch) & {"owner_id", "target_owner_id", "applicant_owner_id"}):
        raise ManagedUserScopeDeniedError()
    request = json.loads(json.dumps(request, allow_nan=False))
    if item["status"] == "pending_admin_review":
        _require_revision(expected_revision, item["revision"])
        item.update(feedback=request["feedback"], decided_by=request["decided_by"],
                    decision_intent={**request, "pre_decision_revision": item["revision"]})
        if patch:
            item["requested_action"] = {**item["requested_action"], **patch}
        return
    intent = item.get("decision_intent", {})
    if (item["status"] not in {"approved", "executing", "applied", "rejected"}
            or any(intent.get(key) != value for key, value in request.items())):
        raise HostAPIError("REVISION_CONFLICT", 409)
    _require_revision(expected_revision, expected_revision)
    if expected_revision is not None and expected_revision not in {item["revision"], intent["pre_decision_revision"]}:
        raise HostAPIError("REVISION_CONFLICT", 409)


# LLM: 调用方持review锁；中断保留权威中间态，不回滚pending；终态重入不重复应用领域效果。
# 函数用途: 驱动批复恢复、可见镜像和通知修复。
def _decide_review_locked(control, home, item, request, expected_revision):
    _prepare_decision(item, request, expected_revision)
    if item["status"] == "pending_admin_review":
        _transition_review(control, item, "rejected" if request["decision"] == "reject_with_feedback" else "approved")
    if item["status"] == "approved":
        _transition_review(control, item, "executing")
    if item["status"] == "executing":
        _apply_approved_review(control, home, item)
    _write_review(control, item)
    _notify_review(home, item)
    return copy.deepcopy(item)


# LLM: 动作前冻结恢复计划；完成立即持久化检查点；503/IO中断不伪装确定失败，留原事务恢复。
# 函数用途: 应用领域效果并推进已应用或确定失败状态。
def _apply_approved_review(control, home, item):
    if not item.get("effect_applied_at"):
        try:
            if item["review_type"] == "persona_change" and "effect_action" not in item:
                service = ManagedUserGovernanceService(home.root)
                item["effect_action"] = _prepare_review_persona_action(service, home, item["requested_action"])
                _write_review(control, item)
            item["result"] = _execute_review(home.root, home, item)
        except (OSError, HostAPIError) as error:
            if isinstance(error, OSError) or error.status >= 500:
                raise
            _fail_review(control, item, error)
            return
        except Exception as error:
            _fail_review(control, item, error)
            return
        item["effect_applied_at"] = now_utc()
        _write_review(control, item)
    _transition_review(control, item, "applied")


# LLM: 只记录确定性领域失败，不把存储中断当作未发生副作用。
# 函数用途: 为不可应用审核记录失败码和终态。
def _fail_review(control, item, error):
    item["result"] = {"ok": False, "error_code": getattr(error, "code", "GOVERNANCE_ACTION_FAILED"),
                      "error_type": type(error).__name__}
    _transition_review(control, item, "failed")


# LLM: 权威记录发表后同步镜像；恢复可重放此动作，不增加修订或执行领域副作用。
# 函数用途: 保存审核检查点并修复可见副本。
def _write_review(control, item):
    control.write(("governance_reviews", item["review_id"] + ".json"), item,
                  ("data", "governance_reviews", item["review_id"] + ".json"))


# LLM: 宿主控制项同样校验schema/归属和固定ID，损坏不降级为待审或批准。
# 函数用途: 读取一份完整权威审核项。
def _read_review(control: _GovernanceControl, review_id: str) -> dict[str, Any]:
    if not _REVIEW_ID.fullmatch(review_id):
        raise HostAPIError("GOVERNANCE_STORE_INVALID", 503)
    item = control.store.read_json((*control.prefix, "governance_reviews", review_id + ".json"))
    if (item is None or item.get("schema_version") != _SCHEMA or item.get("review_id") != review_id
            or item.get("applicant_owner_id") != f"local/{control.uid}" or type(item.get("revision")) is not int):
        raise HostAPIError("GOVERNANCE_STORE_INVALID", 503)
    return item


# LLM: 原状态图是唯一转换权威，持review锁时逐次原子发表，镜像失败保持已发表事实。
# 函数用途: 推进审核修订和历史记录。
def _transition_review(control: _GovernanceControl, item: dict[str, Any], status: str) -> None:
    transitions = {"pending_admin_review": {"approved", "rejected"}, "approved": {"executing"}, "executing": {"applied", "failed"}}
    if status not in transitions.get(item["status"], set()):
        raise HostAPIError("REVISION_CONFLICT", 409)
    timestamp = now_utc()
    item.update(status=status, revision=item["revision"] + 1, updated_at=timestamp)
    item["history"] = [*item["history"], {"status": status, "at": timestamp}]
    _write_review(control, item)


# LLM: 只调用固定目标领域；人格用原计划幂等键，调度实际创建，命令/提权签发一次性票据但不执行shell。
# 函数用途: 应用审核效果并返回可核验领域结果。
def _execute_review(root: Path, home, item: dict[str, Any]) -> dict[str, Any]:
    action, kind = item["requested_action"], item["review_type"]
    service = ManagedUserGovernanceService(root, actor_owner=OwnerIdentity.local_main())
    if kind == "persona_change":
        return service.set_persona(home.owner_id, **item["effect_action"], idempotency_key="review:" + item["review_id"])
    if kind == "asset_enable":
        return service.toggle_asset(home.owner_id, action["asset_kind"], action["asset_id"], True)
    if kind == "memory_promotion":
        result = service.mutate_memories(home.owner_id, "approve_candidate", candidate_id=action["candidate_id"])
        if result.get("ok") is not True:
            raise HostAPIError("MEMORY_PROMOTION_NOT_APPLIED", 409)
        return result
    if kind == "schedule_create":
        return _apply_review_schedule(home, item)
    if kind in {"capability_elevation", "high_risk_command"}:
        return _issue_review_ticket(root, home, item)
    raise ValueError("unsupported review effect")


# LLM: 两种请求的显式修订与单目标SHA均核对同一current快照；冻结后不透传SHA，沿原set_persona幂等恢复。
# 函数用途: 为审核人格请求计算绑定快照的持久写入参数。
def _prepare_review_persona_action(service, home, action: dict[str, Any]) -> dict[str, Any]:
    from hashlib import sha256

    from ..capability.persona_repository import _apply_operation_to_text

    current = service.get_persona(home.owner_id)
    _require_revision(action.get("expected_revision"), current["revision"])
    targets = [key for key in ("soul_md", "agents_md", "user_md") if key in action]
    if targets:
        allowed = {"soul_md", "agents_md", "user_md", "persona_mode", "expected_revision"}
        if len(targets) == 1:
            allowed.add("expected_sha256")
        if set(action) - allowed:
            raise ValueError("unsupported persona set fields")
        if "expected_sha256" in action and action["expected_sha256"] != sha256(current[targets[0].removesuffix("_md").upper() + ".md"].encode()).hexdigest():
            raise HostAPIError("REVISION_CONFLICT", 409)
        return {**{key: value for key, value in action.items() if key != "expected_sha256"}, "expected_revision": current["revision"]}
    target = action.get("target", "soul")
    if target not in {"soul", "agents", "user"}:
        raise ValueError("invalid persona target")
    operation = action.get("operation", "mutate")
    if operation not in {"mutate", "mutate_batch", "write_text"}:
        raise ValueError("source changes must be resubmitted through the original source authorization")
    previous = current[target.upper() + ".md"]
    if "expected_sha256" in action and action["expected_sha256"] != sha256(previous.encode()).hexdigest():
        raise HostAPIError("REVISION_CONFLICT", 409)
    if operation == "write_text":
        next_text = action["content"]
    else:
        next_text = previous
        operations = action.get("operations", [action]) if operation == "mutate_batch" else [action]
        for entry in operations:
            next_text, _prior, _entry, _changed = _apply_operation_to_text(next_text, target=target,
                action=entry.get("action", "add"), content=entry.get("content", ""), entry_id=entry.get("entry_id", ""))
    return {target + "_md": next_text, "expected_revision": current["revision"]}


# LLM: 通知是执行结果之后的独立投递；失败不将已应用动作改写为未执行，不重复追加已成功消息。
# 函数用途: 向原申请会话追加结构化批复并发布原owner唤醒。
def _notify_review(home, item: dict[str, Any]) -> None:
    control = _GovernanceControl(home.root, home.identity.owner_id)
    thread_id = item["applicant_thread_id"]
    if not thread_id:
        item["notification_status"] = "not_requested"
        _write_review(control, item)
        return
    try:
        store = _managed_conversations(home, initialize=True, thread_id=thread_id)
        thread = store.threads.require(thread_id)
        if thread.owner_id != home.owner_id:
            raise ManagedUserScopeDeniedError()
        notice = {"event": "governance_review_decision", "review_id": item["review_id"],
                  "target_owner_id": home.owner_id, "status": item["status"], "feedback": item["feedback"],
                  "decided_by": "local/main"}
        key = f"governance-review:{item['review_id']}:{item['status']}"
        stores = {store.storage.root: store}
        rt_store = _managed_conversations(home, initialize=False, thread_id="*")
        if rt_store.storage.root.exists() and rt_store.threads.load(thread_id) is not None:
            stores.setdefault(rt_store.storage.root, rt_store)
        for target in stores.values():
            target.messages.append_once({"thread_id": thread_id, "role": "system", "channel": "internal",
                "content": json.dumps(notice, ensure_ascii=False), "metadata": {"governance_review": notice}}, dedupe_key=key)
            target.wakes.raise_signal({"thread_id": thread_id, "reason": "governance_review_decision", "dedupe_key": key,
                "summary": json.dumps(notice, ensure_ascii=False), "metadata": {"governance_review": notice, "execution_owner_id": home.owner_id}})
        item["notification_status"] = "queued"
        item.pop("notification_error_type", None)
    except Exception as error:
        item["notification_status"] = "failed"
        item["notification_error_type"] = type(error).__name__
    _write_review(control, item)


# LLM: 摘要只绑定有限JSON动作数据，包含所有字段；不按自然语言含义匹配批准范围。
# 函数用途: 计算一次批准动作的稳定内容摘要。
def _action_digest(action: dict[str, Any]) -> str:
    from hashlib import sha256

    raw = json.dumps(action, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return sha256(raw.encode()).hexdigest()


# LLM: 票据只在host authority签发，按review固定身份/类型/动作；恢复不换票、不清已消费状态，不执行shell。
# 函数用途: 把提权或高危命令批准落实为可核验的一次性授权。
def _issue_review_ticket(root: Path, home, item) -> dict[str, Any]:
    control = _GovernanceControl(root, home.identity.owner_id)
    parts = ("approved_actions", item["review_id"] + ".json")
    record = control.store.read_json((*control.prefix, *parts))
    binding = {"review_id": item["review_id"], "target_owner_id": home.owner_id, "review_type": item["review_type"],
               "action_sha256": _action_digest(item["requested_action"])}
    if record is not None:
        if any(record.get(key) != value for key, value in binding.items()) or not record.get("approval_ticket"):
            raise HostAPIError("APPROVAL_ACTION_MISMATCH", 409)
    else:
        record = {"schema_version": "governance_approved_action.v1", **binding,
                  "requested_action": copy.deepcopy(item["requested_action"]), "approval_ticket": uuid4().hex,
                  "approved_by": "local/main", "approved_at": now_utc(), "consumed_at": "",
                  "execution_performed": True, "authorization_applied": True, "command_executed": False}
        control.write(parts, record)
    return {"ok": True, **copy.deepcopy(record)}


# LLM: 原scheduler负责正式创建；review确定job_id且检查来源摘要，副作用后中断重试不再建线程或计划。
# 函数用途: 将定时提审真正应用到目标用户调度仓库，并返回可消费的任务标识。
def _apply_review_schedule(home, item) -> dict[str, Any]:
    from .managed_user_governance import _install_managed_monitor, _managed_monitor_rows

    job_id = "job_review_" + item["review_id"].removeprefix("grv-")
    digest = _action_digest(item["requested_action"])
    jobs = [row for row in _managed_monitor_rows(home) if row["job_id"] == job_id]
    provenance = {"sender_owner_id": "local/main", "provisioned_by_admin": True, "auto_enabled": True,
                  "governance_review_id": item["review_id"], "action_sha256": digest}
    if jobs:
        if jobs[0].get("delivery_provenance") != provenance:
            raise HostAPIError("APPROVAL_ACTION_MISMATCH", 409)
    else:
        _install_managed_monitor(home, job_id, item["requested_action"], {"enabled": True, "delivery_provenance": provenance})
    return {"ok": True, "execution_performed": True, "job_id": job_id}
