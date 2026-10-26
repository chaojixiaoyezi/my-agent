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
            thread = _managed_conversations(home).threads.require(applicant_thread_id)
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

    # LLM: review_id索引与target在同一原review锁内重核；终态及非pending拒绝重放，永不重复执行动作。
    # 函数用途: CAS批准并应用审核动作，或带反馈驳回并通知原申请会话。
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
            _require_revision(expected_revision, item["revision"])
            if item["status"] != "pending_admin_review":
                raise HostAPIError("REVISION_CONFLICT", 409)
            item.update(feedback=feedback, decided_by=decided_by)
            if decision == "reject_with_feedback":
                _transition_review(control, item, "rejected")
            else:
                if patch_override is not None:
                    if type(patch_override) is not dict or set(patch_override) & {"owner_id", "target_owner_id", "applicant_owner_id"}:
                        raise ManagedUserScopeDeniedError()
                    item["requested_action"] = {**item["requested_action"], **copy.deepcopy(patch_override)}
                _transition_review(control, item, "approved")
                _transition_review(control, item, "executing")
                try:
                    item["result"] = _execute_review(self.root_dir, home, item)
                except Exception as error:
                    item["result"] = {"ok": False, "error_code": getattr(error, "code", "GOVERNANCE_ACTION_FAILED"),
                                      "error_type": type(error).__name__}
                    _transition_review(control, item, "failed")
                else:
                    _transition_review(control, item, "applied")
            _notify_review(home, item)
            return copy.deepcopy(item)


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
    control.write(("governance_reviews", item["review_id"] + ".json"), item,
                  ("data", "governance_reviews", item["review_id"] + ".json"))


# LLM: 审批只调用固定领域入口，目标永远来自审核项权威归属；命令/提权仅记录获批意图，不执行任意shell。
# 函数用途: 应用已批准的人格、资产或记忆动作，保存其它类型批准记录。
def _execute_review(root: Path, home, item: dict[str, Any]) -> dict[str, Any]:
    action, kind = item["requested_action"], item["review_type"]
    service = ManagedUserGovernanceService(root, actor_owner=OwnerIdentity.local_main())
    if kind == "persona_change":
        result = _apply_review_persona(service, home, action)
    elif kind == "asset_enable":
        result = service.toggle_asset(home.owner_id, action["asset_kind"], action["asset_id"], True)
    elif kind == "memory_promotion":
        result = service.mutate_memories(home.owner_id, "approve_candidate", candidate_id=action["candidate_id"])
        if result.get("ok") is not True:
            raise HostAPIError("MEMORY_PROMOTION_NOT_APPLIED", 409)
    else:
        control = _GovernanceControl(root, home.identity.owner_id)
        record = {"schema_version": "governance_approved_action.v1", "review_id": item["review_id"],
                  "target_owner_id": home.owner_id, "review_type": kind, "requested_action": copy.deepcopy(action),
                  "approved_by": "local/main", "approved_at": now_utc(), "execution_performed": False}
        control.write(("approved_actions", item["review_id"] + ".json"), record)
        result = {"ok": True, "approval_recorded": True, "execution_performed": False}
    return result


# LLM: 原人格纯操作算法保留条目及内容校验，SHA在批准时再次核对；写入走治理CAS，不创建第二份用户版本链。
# 函数用途: 将已审人格请求应用到同一个受管用户文件。
def _apply_review_persona(service, home, action: dict[str, Any]) -> dict[str, Any]:
    from hashlib import sha256

    from ..capability.persona_repository import _apply_operation_to_text
    from .managed_user_governance import _owner_text

    if any(key in action for key in ("soul_md", "agents_md", "user_md")):
        allowed = {"soul_md", "agents_md", "user_md", "persona_mode", "expected_revision"}
        if set(action) - allowed:
            raise ValueError("unsupported persona set fields")
        return service.set_persona(home.owner_id, **action)
    target = action.get("target", "soul")
    if target not in {"soul", "agents", "user"}:
        raise ValueError("invalid persona target")
    operation = action.get("operation", "mutate")
    if operation not in {"mutate", "mutate_batch", "write_text"}:
        raise ValueError("source changes must be resubmitted through the original source authorization")
    current = service.get_persona(home.owner_id)
    previous = current[target.upper() + ".md"]
    expected = action.get("expected_sha256", "")
    if expected and expected != sha256(previous.encode()).hexdigest():
        raise HostAPIError("REVISION_CONFLICT", 409)
    if operation == "write_text":
        next_text = action["content"]
    else:
        next_text = previous
        operations = action.get("operations", [action]) if operation == "mutate_batch" else [action]
        for entry in operations:
            next_text, _prior, _entry, _changed = _apply_operation_to_text(next_text, target=target,
                action=entry.get("action", "add"), content=entry.get("content", ""), entry_id=entry.get("entry_id", ""))
    return service.set_persona(home.owner_id, **{target + "_md": next_text}, expected_revision=current["revision"])


# LLM: 通知是执行结果之后的独立投递；失败不将已应用动作改写为未执行，不重复追加已成功消息。
# 函数用途: 向原申请会话追加结构化批复并发布原owner唤醒。
def _notify_review(home, item: dict[str, Any]) -> None:
    thread_id = item["applicant_thread_id"]
    if not thread_id:
        item["notification_status"] = "not_requested"
        return
    try:
        store = _managed_conversations(home, initialize=True)
        thread = store.threads.require(thread_id)
        if thread.owner_id != home.owner_id:
            raise ManagedUserScopeDeniedError()
        notice = {"event": "governance_review_decision", "review_id": item["review_id"],
                  "target_owner_id": home.owner_id, "status": item["status"], "feedback": item["feedback"],
                  "decided_by": "local/main"}
        key = f"governance-review:{item['review_id']}:{item['status']}"
        store.messages.append_once({"thread_id": thread_id, "role": "system", "channel": "internal",
            "content": json.dumps(notice, ensure_ascii=False), "metadata": {"governance_review": notice}}, dedupe_key=key)
        store.wakes.raise_signal({"thread_id": thread_id, "reason": "governance_review_decision", "dedupe_key": key,
            "summary": json.dumps(notice, ensure_ascii=False), "metadata": {"governance_review": notice, "execution_owner_id": home.owner_id}})
        item["notification_status"] = "queued"
    except Exception as error:
        item["notification_status"] = "failed"
        item["notification_error_type"] = type(error).__name__
    control = _GovernanceControl(home.root, home.identity.owner_id)
    control.write(("governance_reviews", item["review_id"] + ".json"), item,
                  ("data", "governance_reviews", item["review_id"] + ".json"))
