# LLM: 治理权威仅在宿主data；owner镜像不授权。公开动作先解析登记身份，再核宿主注入的actor。
# 模块用途: 管理普通本地受管用户的策略、人设、记忆、资产和派单，不接管IM独立用户。
from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from ..common.nofollow_fs import read_bytes_beneath, write_bytes_atomic_beneath
from ..plugin_host.errors import HostAPIError
from .local_user_lifecycle import now_utc
from .local_user_store import LocalUserStore
from .owner_resolver import OwnerHomeResult, OwnerIdentity

_POLICY_SCHEMA = "managed_user_governance_policy.v1"
_PERSONA_MODES = {"admin_locked", "admin_base_plus_user", "user_autonomous"}
_ADMIN_WRITE: ContextVar[bool] = ContextVar("managed_governance_admin_write", default=False)
_POLICY_FIELDS = {
    "memory_governance": {"allow_self_learning": True, "require_admin_review_for_long_term": False,
                          "require_admin_review_for_lesson": False, "admin_locked_memory_ids": []},
    "execution_governance": {"allow_self_created_scripts": False, "allow_self_created_workflows": False,
                             "allow_self_created_monitors": False, "allow_capability_elevation_request": True},
    "review_required_for": {"high_risk_shell": True, "external_network_write": True, "schedule_creation": True,
                            "skill_installation": True, "persona_changes": True},
}


# LLM: 机器码/HTTP与人类信息分开，范围拒绝发生在磁盘写入前。
# 类用途: 表示目标或调用方不属于管理员受管本地用户范围。
class ManagedUserScopeDeniedError(HostAPIError):
    # LLM: 固定403，不泄露其它owner的数据或路径。
    # 函数用途: 创建稳定的范围拒绝。
    def __init__(self):
        super().__init__("MANAGED_USER_SCOPE_DENIED", 403, message="Target owner is outside admin-managed local user scope.")


# LLM: 只规范化三种显式本地引用；账号必须已发布，路径来自原LocalUserStore，不创建用户。
# 函数用途: 将受管用户名解析为唯一登记身份和规范目录。
def resolve_managed_local_user(root_dir: Path, target_user_or_owner_id: str) -> tuple[str, OwnerIdentity, OwnerHomeResult]:
    target = target_user_or_owner_id
    if type(target) is not str:
        raise ManagedUserScopeDeniedError()
    parts = target.split("/")
    if len(parts) == 1:
        uid = parts[0]
    elif len(parts) == 2 and parts[0] == "local":
        uid = parts[1]
    elif len(parts) == 3 and parts[:2] == ["local", "users"]:
        uid = parts[2]
    else:
        raise ManagedUserScopeDeniedError()
    try:
        identity = OwnerIdentity.local_user(uid)
        store = LocalUserStore(root_dir)
        user = store.get(uid)
    except (ValueError, HostAPIError) as error:
        if isinstance(error, HostAPIError) and error.code == "STORE_UNAVAILABLE":
            raise
        raise ManagedUserScopeDeniedError() from error
    if user is None or user.status not in {"enabled", "disabled"}:
        raise ManagedUserScopeDeniedError()
    home = store.owner_home(uid)
    record = store.read_record(uid)
    if store.directory_identity(("owners", "local", uid)) != record["provision"]["home_identity"]:
        raise ManagedUserScopeDeniedError()
    return uid, identity, home


# LLM: 默认只保留旧用户自治；治理必须由显式策略启用，不凭可编辑镜像推断管理员批准。
# 函数用途: 返回尚未设定治理策略时的零修订视图。
def _default_policy(uid: str) -> dict[str, Any]:
    return {"schema_version": _POLICY_SCHEMA, "owner_id": f"local/{uid}", "revision": 0,
            "updated_at": "", "updated_by": "", "persona_mode": "user_autonomous",
            "role_title": "", "role_tags": [], **copy.deepcopy(_POLICY_FIELDS)}


# LLM: 根和UID只来自可信构造及resolver；原LocalUserStore提供nofollow、有界JSON及跨进程锁。
# 类用途: 在宿主唯一权威位置存取治理控制数据，并更新可见镜像。
class _GovernanceControl:
    # LLM: 冷绑定原存储，不创建目录或读取用户镜像。
    # 函数用途: 准备宿主控制数据的路径前缀。
    def __init__(self, root: Path, uid: str):
        self.store = LocalUserStore(root)
        self.uid = uid
        self.prefix = ("data", "managed_user_governance", uid)

    # LLM: 只读host authority；不存在才有revision0默认，损坏不能回退自治。
    # 函数用途: 读取并验证一个用户当前治理策略。
    def policy(self) -> dict[str, Any]:
        value = self.store.read_json((*self.prefix, "governance_policy.json"))
        if value is None:
            return _default_policy(self.uid)
        if (value.get("schema_version") != _POLICY_SCHEMA or value.get("owner_id") != f"local/{self.uid}"
                or type(value.get("revision")) is not int or value["revision"] < 1):
            raise HostAPIError("GOVERNANCE_STORE_INVALID", 503)
        _policy_patch(_default_policy(self.uid), {key: value[key] for key in _editable_policy_keys() if key in value})
        return value

    # LLM: authority先发布，镜像失败须显式报partial；从不以用户镜像恢复或覆盖authority。
    # 函数用途: 保存控制数据并为目标用户投影可见副本。
    def write(self, name: tuple[str, ...], value: dict, mirror: tuple[str, ...] | None = None) -> None:
        self.store.write_json((*self.prefix, *name), value)
        if mirror is not None:
            try:
                self.store.write_json(("owners", "local", self.uid, *mirror), value)
            except HostAPIError as error:
                raise HostAPIError("GOVERNANCE_MIRROR_FAILED", 503, message="Authority committed; visibility mirror failed.") from error

    # LLM: 原跨进程锁覆盖完整read/CAS/write；不持该锁等待模型或执行外部命令。
    # 函数用途: 串行化同一用户的一次控制事务。
    def locked(self, name: str = "policy"):
        return self.store.locked((*self.prefix, f".{name}.lock"))


# LLM: 可编辑字段由现行政策合同声明，修订和归属从不由patch决定。
# 函数用途: 列出策略补丁允许修改的字段。
def _editable_policy_keys() -> set[str]:
    return {*_POLICY_FIELDS, "persona_mode", "role_title", "role_tags"}


# LLM: 嵌套策略的字段和严格类型共用此校验；返回副本，不改变待发表值的所有权。
# 函数用途: 验证记忆、执行和审核开关的局部更新。
def _validated_policy_fields(key: str, value: dict) -> dict:
    if type(value) is not dict or set(value) - set(_POLICY_FIELDS[key]):
        raise ValueError("invalid governance policy fields")
    for field, item in value.items():
        if field == "admin_locked_memory_ids":
            if type(item) is not list or any(type(v) is not str or not v for v in item):
                raise ValueError("invalid locked memory ids")
        elif type(item) is not bool:
            raise ValueError("governance switches must be boolean")
    return copy.deepcopy(value)


# LLM: 只接收补丁白名单中的三个人格元字段；各分支独立返回，列表值复制后交调用方发表。
# 函数用途: 校验人格模式、角色名称或标签，不修改当前策略。
def _validated_policy_metadata(key: str, value: Any) -> str | list[str]:
    if key == "persona_mode":
        if type(value) is not str or value not in _PERSONA_MODES:
            raise ValueError("invalid persona mode")
        return value
    if key == "role_title":
        if type(value) is not str or len(value) > 256:
            raise ValueError("invalid role title")
        return value
    if type(value) is not list or any(type(v) is not str for v in value):
        raise ValueError("invalid role tags")
    return list(value)


# LLM: 严格类型和字段先于写入，两类字段独立委托校验；局部补丁保留未指定值及CAS合同。
# 函数用途: 合并并验证治理策略的局部更新。
def _policy_patch(current: dict, patch: dict) -> dict:
    if type(patch) is not dict or set(patch) - _editable_policy_keys():
        raise ValueError("invalid governance policy patch")
    result = copy.deepcopy(current)
    for key, value in patch.items():
        if key in _POLICY_FIELDS:
            result[key].update(_validated_policy_fields(key, value))
            continue
        result[key] = _validated_policy_metadata(key, value)
    return result


# LLM: None仅供受信宿主省略CAS；显式版本严格int，冲突不写入或隐式重试。
# 函数用途: 对控制记录执行乐观并发检查。
def _require_revision(expected: int | None, actual: int) -> None:
    if expected is not None and (type(expected) is not int or expected < 0):
        raise ValueError("invalid expected_revision")
    if expected is not None and expected != actual:
        raise HostAPIError("REVISION_CONFLICT", 409)


# LLM: 只由已核身份的治理事务进入；可编辑JSON和admin_override布尔值不能自行建立该活栈。
# 函数用途: 在一次管理员人格事务内临时标记受信写入，退出必恢复。
@contextmanager
def _admin_persona_write():
    token = _ADMIN_WRITE.set(True)
    try:
        yield
    finally:
        _ADMIN_WRITE.reset(token)


# LLM: 此值只封装已解析目标上的记忆变更参数，不携带actor或路径，不改变公开领域方法签名。
# 类用途: 把一次记忆写入的内容、操作和CAS版本作为不可变请求传递。
@dataclass(frozen=True)
class _ManagedMemoryMutation:
    operation: str
    entry_id: str
    candidate_id: str
    content: str
    category: str
    locked: bool
    expected_revision: int | None


# LLM: 主服务各公开目标入口先用此resolver；actor来自宿主依赖，updated_by不授予权限。
# 类用途: 对已登记的普通本地用户提供统一治理服务。
class ManagedUserGovernanceService:
    # LLM: root及actor为宿主构造依赖；默认供已鉴权host adapter，不供模型设置actor。
    # 函数用途: 冷绑定主管理员服务，调用时重核目标登记。
    def __init__(self, root_dir: Path, *, actor_owner: OwnerIdentity | None = None):
        self.root_dir = Path(root_dir).resolve()
        self.actor_owner = actor_owner if actor_owner is not None else OwnerIdentity.local_main()

    # LLM: 每次解析当前账号及真实目录，再检查宿主身份；普通用户不能凭updated_by冒充管理员。
    # 函数用途: 校验单次治理操作的管理员与目标边界。
    def _target(self, target_owner_id: str):
        result = resolve_managed_local_user(self.root_dir, target_owner_id)
        if self.actor_owner != OwnerIdentity.local_main():
            raise ManagedUserScopeDeniedError()
        return result

    # LLM: 权威策略锁内CAS后原子发表，镜像只供展示；拒绝从请求伪造审计actor。
    # 函数用途: 更新受管用户治理模式、审核和执行策略。
    def set_policy(self, target_owner_id: str, policy_patch: dict[str, Any], expected_revision: int | None = None,
                   updated_by: str = "local/main") -> dict[str, Any]:
        uid, _identity, _home = self._target(target_owner_id)
        if updated_by != "local/main":
            raise ManagedUserScopeDeniedError()
        control = _GovernanceControl(self.root_dir, uid)
        with control.locked():
            current = control.policy()
            _require_revision(expected_revision, current["revision"])
            value = _policy_patch(current, policy_patch)
            value.update(revision=current["revision"] + 1, updated_at=now_utc(), updated_by=updated_by)
            control.write(("governance_policy.json",), value, ("config", "governance_policy.json"))
            return value

    # LLM: 读取宿主权威政策及原仓库计数，不回显密码或散列；任务只报告有界近期覆盖，不外推全量。
    # 函数用途: 返回单个受管用户的治理概览。
    def get_overview(self, target_owner_id: str) -> dict[str, Any]:
        uid, _identity, home = self._target(target_owner_id)
        return {"owner_id": home.owner_id, "user": LocalUserStore(self.root_dir).get(uid).public_view(),
                "governance_policy": _GovernanceControl(self.root_dir, uid).policy(),
                **_managed_overview_counts(home)}


    # LLM: 正文仅限登记受管用户；模式和修订读取宿主authority，不信任镜像版本。
    # 函数用途: 读取受管人格全文、管理员底线和修订。
    def get_persona(self, target_owner_id: str) -> dict[str, Any]:
        uid, _identity, home = self._target(target_owner_id)
        control = _GovernanceControl(self.root_dir, uid)
        with control.locked("persona"):
            return _persona_view(control, home)

    # LLM: CAS及幂等键在宿主控制锁下检查，updated_by不授予身份；只写本目标人格。
    # 函数用途: 管理员设置人格正文或基础模式并保存修订。
    def set_persona(self, target_owner_id: str, soul_md: str | None = None, agents_md: str | None = None,
                    user_md: str | None = None, persona_mode: str | None = None, updated_by: str = "local/main",
                    expected_revision: int | None = None, idempotency_key: str = "") -> dict[str, Any]:
        uid, _identity, home = self._target(target_owner_id)
        if updated_by != "local/main":
            raise ManagedUserScopeDeniedError()
        payload = {"soul_md": soul_md, "agents_md": agents_md, "user_md": user_md, "persona_mode": persona_mode}
        return _write_managed_persona(self, _GovernanceControl(self.root_dir, uid), home, payload,
                                      expected_revision, idempotency_key)

    # LLM: 只调用目标原记忆和候选仓库；分类/状态过滤不改变权威来源。
    # 函数用途: 列出受管用户正式记忆与候选。
    def list_memories(self, target_owner_id: str, category: str | None = None, include_candidates: bool = True,
                      status_filter: str | None = None) -> dict[str, Any]:
        _uid, _identity, home = self._target(target_owner_id)
        return _list_managed_memories(home, category, include_candidates, status_filter)

    # LLM: 正式写和候选晋升委托原主链；仅内部收拢请求参数，公开Host/工具签名不变。
    # 函数用途: 注入、修正、删除或审核受管用户记忆。
    def mutate_memories(self, target_owner_id: str, operation: str, *, entry_id: str = "", candidate_id: str = "",
                        content: str = "", category: str = "long_term", locked: bool = False,
                        expected_revision: int | None = None) -> dict[str, Any]:
        _uid, _identity, home = self._target(target_owner_id)
        request = _ManagedMemoryMutation(operation, entry_id, candidate_id, content, category, locked, expected_revision)
        return _mutate_managed_memory(home, request)

    # LLM: 闭包沿原资产仓库安装；身份只取已解析home，不向安装层重复传未使用UID；不执行代码。
    # 函数用途: 给受管用户配发工作流、脚本、技能及其依赖。
    def provision_assets(self, target_owner_id: str, asset_kind: str, asset_id: str, auto_enable: bool = False,
                         conflict_strategy: str = "overwrite", note: str = "", payload_override: dict | None = None) -> dict[str, Any]:
        _uid, _identity, home = self._target(target_owner_id)
        if type(auto_enable) is not bool or conflict_strategy not in {"overwrite", "keep_both_rename", "skip"}:
            raise ValueError("invalid provision options")
        return _provision_managed_asset(self.root_dir, home, asset_kind, asset_id, auto_enable,
                                        conflict_strategy, note, payload_override)

    # LLM: 启停只提交目标原资产存储，不改变来源owner或授予执行权限。
    # 函数用途: 切换受管用户资产的启停状态。
    def toggle_asset(self, target_owner_id: str, asset_kind: str, asset_id: str, enabled: bool) -> dict[str, Any]:
        _uid, _identity, home = self._target(target_owner_id)
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        return _toggle_managed_asset(home, asset_kind, asset_id, enabled)

    # LLM: 派单还需enabled账号；消息/任务/唤醒只进目标自身会话Store，不借用管理员运行根。
    # 函数用途: 在目标用户自己的执行身份下排入管理员任务。
    def dispatch_task(self, target_owner_id: str, prompt: str, title: str = "", thread_id: str = "",
                      priority: str = "normal") -> dict[str, Any]:
        uid, _identity, home = self._target(target_owner_id)
        LocalUserStore(self.root_dir).require_enabled(uid)
        return _dispatch_managed_task(home, prompt, title, thread_id, priority)

    # LLM: 只查询目标原任务、子代理及scheduler事实源；有界返回需公开覆盖上限。
    # 函数用途: 查询受管用户任务、子代理和定时计划。
    def list_tasks(self, target_owner_id: str, status_filter: str | None = None, limit: int = 50) -> dict[str, Any]:
        _uid, _identity, home = self._target(target_owner_id)
        return _list_managed_tasks(home, status_filter, limit)


# LLM: 聚合原控制、配额统计和领域仓库；源错误不能当零，近期任务数量必须携带扫描上限。
# 函数用途: 返回治理概览所需的磁盘、记忆、资产、审核和近期任务计数。
def _managed_overview_counts(home: OwnerHomeResult) -> dict[str, Any]:
    from ..assets.script_store import ScriptStore
    from ..assets.workflow_store import WorkflowStore
    from .governance_review_store import GovernanceReviewStore
    from .owner_admin_controls import read_owner_admin_controls
    from .owner_quota import owner_logical_usage_bytes

    memories = _list_managed_memories(home, None, True, None)
    tasks = _list_managed_tasks(home, None, 200)
    if tasks["errors"]:
        raise HostAPIError("GOVERNANCE_TASKS_UNREADABLE", 503)
    reviews = GovernanceReviewStore(home.root).list_reviews(home.owner_id, status_filter="pending_admin_review")
    return {
        "controls": read_owner_admin_controls(_managed_home_paths(home)),
        "disk_usage_bytes": owner_logical_usage_bytes(home.home_dir),
        "memory_counts": {"memories": len(memories["memories"]), "candidates": len(memories["candidates"])},
        "asset_counts": {
            "scripts": len(ScriptStore(home.home_dir).list_scripts()),
            "workflows": len(WorkflowStore(home.home_dir).list_workflows()),
            "monitors": len(_managed_monitor_rows(home)),
            "skills": sum(1 for path in (home.home_dir / "skills").rglob("SKILL.md") if not path.is_symlink()),
        },
        "pending_governance_reviews_count": reviews["count"],
        "task_counts": {
            "recent_count": len(tasks["tasks"]),
            "active_recent_count": sum(row.get("status") in {"queued", "running", "paused", "waiting"} for row in tasks["tasks"]),
            "truncated": tasks["truncated"], "coverage": tasks["coverage"],
        },
    }


# LLM: 只有规范owners/local/uid且已登记者适用治理；IM/main/独立测试目录维持原合同。
# 函数用途: 从人格仓库的可信owner目录找到对应宿主治理控制。
def _persona_governance(owner_home: Path):
    home = Path(owner_home).absolute()
    if home.name == "main" or tuple(home.parts[-3:-1]) != ("owners", "local"):
        return None
    root = home.parents[2]
    if LocalUserStore(root).get(home.name) is None:
        return None
    uid, identity, resolved = resolve_managed_local_user(root, home.name)
    return _GovernanceControl(root, uid), identity, resolved


# LLM: 普通写、批量和原生切源共守宿主authority，不能以confirmed/source或可伪造镜像绕过。
# 函数用途: 锁定的人格变更转待审或拒绝，非受管用户保持原样。
def require_managed_persona_write(owner_home: Path, target: str, requested_action: dict,
                                 *, admin_override: bool = False) -> None:
    if target not in {"soul", "agents"}:
        return
    if admin_override and not _ADMIN_WRITE.get():
        raise ManagedUserScopeDeniedError()
    context = _persona_governance(owner_home)
    if context is None or _ADMIN_WRITE.get():
        return
    control, identity, home = context
    policy = control.policy()
    if policy["persona_mode"] != "admin_locked":
        return
    if not policy["review_required_for"]["persona_changes"]:
        raise HostAPIError("PERSONA_LOCKED_BY_ADMIN", 403)
    from hashlib import sha256

    from .governance_review_store import GovernanceReviewStore

    action = copy.deepcopy(requested_action)
    action["target"] = target
    if action.get("operation", "mutate") == "mutate":
        raw = read_bytes_beneath(control.store.root, ("owners", "local", control.uid, f"{target.upper()}.md"),
                                 max_bytes=2097152) or b""
        action.setdefault("expected_sha256", sha256(raw).hexdigest())
        if not action["expected_sha256"]:
            action["expected_sha256"] = sha256(raw).hexdigest()
    item = GovernanceReviewStore(control.store.root, actor_owner=identity).submit_review(
        home.owner_id, "persona_change", action, reason="managed persona is admin-locked")
    error = HostAPIError("PERSONA_CHANGE_PENDING_ADMIN_REVIEW", 409, message="管理员审核后才能修改。")
    error.review_id = item.review_id
    raise error


# LLM: 管理底线从宿主authority读，不信任用户可写的SOUL_ADMIN_BASE/AGENTS_ADMIN_BASE镜像。
# 函数用途: 在安全加载的人格正文前拼接管理员底线，原物理文件SHA仍用于用户正文CAS。
def prepend_managed_persona_base(repository, target: str, snapshot):
    if target not in {"soul", "agents"}:
        return snapshot
    context = _persona_governance(repository.owner_home)
    if context is None:
        return snapshot
    control, _identity, _home = context
    if control.policy()["persona_mode"] != "admin_base_plus_user":
        return snapshot
    raw = read_bytes_beneath(control.store.root, (*control.prefix, "persona", f"{target.upper()}_ADMIN_BASE.md"),
                             max_bytes=2097152)
    if raw is None:
        return snapshot
    return replace(snapshot, content=raw.decode("utf-8") + "\n\n" + snapshot.content)


# LLM: 文本通过同一宿主根nofollow有界读取；缺文件为空，链接/损坏必须报错。
# 函数用途: 读取目标用户一份可见文本而不物化目录。
def _owner_text(home: OwnerHomeResult, parts: tuple[str, ...]) -> str:
    raw = read_bytes_beneath(home.root, ("owners", "local", home.identity.owner_id, *parts), max_bytes=2097152)
    return raw.decode("utf-8") if raw is not None else ""


# LLM: 修订从宿主人格状态读；基础正文也从宿主authority读，不信任用户镜像。
# 函数用途: 生成管理员可读的人格全文与治理视图。
def _persona_view(control: _GovernanceControl, home: OwnerHomeResult, *, allow_applying: bool = False) -> dict[str, Any]:
    state = control.store.read_json((*control.prefix, "persona_state.json")) or {"revision": 0}
    if state.get("status") == "applying" and not allow_applying:
        raise HostAPIError("GOVERNANCE_OPERATION_INCOMPLETE", 409)
    policy = control.policy()
    result = {"target_owner_id": home.owner_id, "owner_id": home.owner_id, "revision": state["revision"],
              "persona_mode": policy["persona_mode"]}
    for name in ("SOUL.md", "AGENTS.md", "USER.md"):
        result[name] = _owner_text(home, (name,))
    for name in ("SOUL_ADMIN_BASE.md", "AGENTS_ADMIN_BASE.md"):
        raw = read_bytes_beneath(home.root, (*control.prefix, "persona", name), max_bytes=2097152)
        result[name] = raw.decode("utf-8") if raw is not None else ""
    return result


# LLM: 人格CAS/幂等在同锁检查；先发表可恢复applying栅栏，失败不以旧修订暴露半个人格。
# 函数用途: 将管理员人格设定写入authority和受管用户文件。
def _write_managed_persona(service, control, home, payload, expected_revision, idempotency_key):
    from hashlib import sha256

    if type(idempotency_key) is not str or len(idempotency_key) > 256:
        raise ValueError("invalid idempotency key")
    for key in ("soul_md", "agents_md", "user_md"):
        value = payload[key]
        if value is not None and (type(value) is not str or len(value.encode("utf-8")) > 1048576):
            raise ValueError("invalid persona text")
    if payload["persona_mode"] is not None and (type(payload["persona_mode"]) is not str or payload["persona_mode"] not in _PERSONA_MODES):
        raise ValueError("invalid persona mode")
    encoded = json.dumps({**payload, "expected_revision": expected_revision}, sort_keys=True).encode()
    if len(encoded) > 1048576:
        raise ValueError("persona transaction too large")
    intent = sha256(encoded).hexdigest()
    receipt = ("persona_operations", sha256(idempotency_key.encode()).hexdigest() + ".json") if idempotency_key else None
    with control.locked("persona"):
        state = control.store.read_json((*control.prefix, "persona_state.json")) or {"revision": 0}
        if state.get("status") == "applying":
            if state.get("intent") != intent:
                raise HostAPIError("GOVERNANCE_OPERATION_INCOMPLETE", 409)
            return _finish_persona_transaction(service, control, home, state)
        existing = control.store.read_json((*control.prefix, *receipt)) if receipt else None
        if existing:
            if existing["intent"] != intent:
                raise HostAPIError("IDEMPOTENCY_CONFLICT", 409)
            return existing["result"]
        current = _persona_view(control, home)
        _require_revision(expected_revision, current["revision"])
        transaction = {"revision": current["revision"] + 1, "status": "applying", "intent": intent,
                       "payload": payload, "receipt": list(receipt) if receipt else None,
                       "mode": payload["persona_mode"] or current["persona_mode"], "updated_by": "local/main", "updated_at": now_utc()}
        control.write(("persona_state.json",), transaction)
        return _finish_persona_transaction(service, control, home, transaction)


# LLM: 调用方持persona锁；先发表applying修订，读口阻断半像，同意图失败重试只恢复原计划。
# 函数用途: 重放未完成的人格文件同步，以单一完成点解除半提交栅栏。
def _finish_persona_transaction(service, control, home, transaction):
    payload, mode = transaction["payload"], transaction["mode"]
    for target in ("soul", "agents", "user"):
        text = payload[f"{target}_md"]
        if text is None:
            continue
        name, data = f"{target.upper()}.md", text.encode("utf-8")
        if mode == "admin_base_plus_user" and target != "user":
            base_name = f"{target.upper()}_ADMIN_BASE.md"
            write_bytes_atomic_beneath(home.root, (*control.prefix, "persona", base_name), data)
            write_bytes_atomic_beneath(home.root, ("owners", "local", control.uid, "memory", base_name), data)
            if read_bytes_beneath(home.root, ("owners", "local", control.uid, name), max_bytes=2097152) is None:
                write_bytes_atomic_beneath(home.root, ("owners", "local", control.uid, name), b"")
        else:
            write_bytes_atomic_beneath(home.root, (*control.prefix, "persona", name), data)
            write_bytes_atomic_beneath(home.root, ("owners", "local", control.uid, name), data)
    if payload["persona_mode"] is not None and control.policy()["persona_mode"] != mode:
        service.set_policy(home.owner_id, {"persona_mode": mode})
    result = _persona_view(control, home, allow_applying=True)
    if transaction["receipt"]:
        control.write(tuple(transaction["receipt"]), {"intent": transaction["intent"], "status": "applied", "result": result})
    control.write(("persona_state.json",), {"revision": transaction["revision"], "status": "applied",
                  "updated_by": "local/main", "updated_at": now_utc()})
    return result


# LLM: ConversationStore必须使用目标owner/conversations，读写均先检查目录链；不接管理员队列。
# 函数用途: 打开受管用户自己的原会话存储。
def _managed_conversations(home: OwnerHomeResult, *, initialize: bool = False):
    import os

    from ..common.nofollow_fs import ensure_private_dir, open_directory_beneath
    from ..conversation.store import ConversationStore

    if initialize:
        ensure_private_dir(home.home_dir / "conversations", canonical_root=home.root)
    try:
        descriptor = open_directory_beneath(home.root, ("owners", "local", home.identity.owner_id, "conversations"))
    except FileNotFoundError:
        if initialize:
            raise
    else:
        os.close(descriptor)
    return ConversationStore(home.home_dir / "conversations", initialize=initialize)


# LLM: 只创建目标owner线程和原task/wake事实；priority只是队列级别，不扩大用户权限。
# 函数用途: 记录管理员消息、目标身份任务和唤醒，返回真实排队标识。
def _dispatch_managed_task(home, prompt, title, thread_id, priority):
    from ..conversation.models import new_id

    if type(prompt) is not str or not prompt.strip() or len(prompt) > 32000:
        raise ValueError("invalid prompt")
    if type(title) is not str or len(title) > 160 or priority not in {"normal", "high", "urgent", "low"}:
        raise ValueError("invalid dispatch options")
    store = _managed_conversations(home, initialize=True)
    thread = store.threads.require(thread_id) if thread_id else store.threads.create(
        canonical_user_id=home.owner_id, title=title, owner_id=home.owner_id, owner_home=str(home.home_dir),
        workspace_root=str(home.workspace_dir), cwd=str(home.workspace_dir))
    if thread.owner_id != home.owner_id or Path(thread.owner_home) != home.home_dir:
        raise ManagedUserScopeDeniedError()
    request_id, task_id = new_id("request"), new_id("task")
    metadata = {"dispatched_by": "local/main", "target_owner_id": home.owner_id,
                "execution_owner_id": home.owner_id, "request_id": request_id, "task_id": task_id}
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": prompt,
                           "channel": "internal", "metadata": metadata})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "request_id": request_id,
                      "goal": prompt, "status": "queued"})
    store.wakes.raise_signal({"thread_id": thread.thread_id, "reason": "admin_dispatched_task", "root_task_id": task_id,
                              "summary": prompt, "urgency": "urgent" if priority in {"high", "urgent"} else "normal",
                              "metadata": metadata, "dedupe_key": task_id})
    return {"ok": True, **metadata, "thread_id": thread.thread_id, "status": "queued"}


# LLM: 返回原任务链接与可见错误；不把未读/截断当作无任务，最多扫描200个最近线程。
# 函数用途: 查询目标会话任务、子代理状态和定时计划。
def _list_managed_tasks(home, status_filter, limit):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    store = _managed_conversations(home)
    threads = store.threads.list(limit=200)
    tasks, errors = [], []
    for thread in threads:
        if thread.owner_id and thread.owner_id != home.owner_id:
            raise ManagedUserScopeDeniedError()
        rows, failures = store.tasks.list_report(thread.thread_id)
        tasks.extend(row.to_dict() for row in rows)
        errors.extend(failures)
    selected = [row for row in tasks if status_filter is None or row.get("status") == status_filter]
    selected.sort(key=lambda row: row.get("updated_at", 0), reverse=True)
    subagents = []
    for path in sorted(home.agents_dir.glob("*/state.json"))[:200]:
        row = LocalUserStore(home.root).read_json(tuple(path.relative_to(home.root).parts))
        if row and (status_filter is None or row.get("status") == status_filter):
            subagents.append(row)
    monitors = _managed_monitor_rows(home)
    return {"target_owner_id": home.owner_id, "tasks": selected[:limit], "subagent_runs": subagents[:limit],
            "scheduled_jobs": [row for row in monitors if status_filter is None or row.get("status") == status_filter][:limit],
            "errors": errors, "truncated": len(selected) > limit or len(threads) == 200,
            "coverage": {"thread_scan_limit": 200}}


# LLM: 路径投影复用原home_layout/owner_resolver，不物化共享目录，也不继承管理员策略。
# 函数用途: 为原Memory和控制服务取得目标用户的完整home路径对象。
def _managed_home_paths(home: OwnerHomeResult):
    from .home_layout import home_paths
    from .owner_resolver import home_paths_with_owner

    return home_paths_with_owner(home_paths(home.root), home)


# LLM: 正式库绑定原候选、操作流水和目标配额；无模型、向量调用或新事实源。
# 函数用途: 打开受管用户原正式记忆与候选服务。
def _managed_memory_stores(home):
    from ..memory_store.candidates import CandidateService
    from ..memory_store.jsonl import JsonlMemory
    from .owner_quota import owner_quota_enforcer_from_policy

    quota = owner_quota_enforcer_from_policy(home.home_dir, quota_path=home.quota_json)
    candidates = CandidateService(home.memory_candidates_jsonl, quota_enforcer=quota)
    memory = JsonlMemory(home.long_term_jsonl, ops_path=home.memory_ops_jsonl,
                         candidate_service=candidates, quota_enforcer=quota)
    return memory, candidates


# LLM: 分类只选择原仓库，未读候选不当零；正式记录保留稳定entry_id/version供CAS。
# 函数用途: 汇总目标用户的长期记忆、画像、经验规则和候选。
def _list_managed_memories(home, category, include_candidates, status_filter):
    from ..capability.persona_repository import PersonaRepository
    from ..memory_store.lessons import HotRuleRepository, LessonRepository

    if type(include_candidates) is not bool or category is not None and type(category) is not str:
        raise ValueError("invalid memory filters")
    memory, candidates = _managed_memory_stores(home)
    rows = []
    if category not in {"user", "lesson", "hot", "candidates"}:
        rows.extend({**asdict(record), "category": "long_term"} for record in memory.all()
                    if category in {None, "long_term"} or record.kind == category)
    if category in {None, "user"}:
        for row in PersonaRepository.from_owner_result(home).list_entries("user")["entries"]:
            rows.append({**row, "category": "user"})
    if category in {None, "lesson"}:
        rows.extend({**asdict(row), "category": "lesson"} for row in LessonRepository(home.lessons_memory_dir, home.routing_index_md).list())
    if category in {None, "hot"}:
        rows.extend({**asdict(row), "category": "hot"} for row in HotRuleRepository(home.memory_hot_md).list())
    pending = [row.to_dict() for row in candidates.list(statuses={status_filter} if status_filter else None)] if include_candidates else []
    return {"target_owner_id": home.owner_id, "memories": rows, "candidates": pending,
            "query_evidence": {"category": category, "include_candidates": include_candidates,
                               "status_filter": status_filter, "memories_returned": len(rows), "candidates_returned": len(pending)}}


# LLM: 晋升必须核原消息/工具证据及完整候选快照；缺工具verifier拒绝晋升，不采信候选自报success。
# 函数用途: 以目标真实仓库装配唯一MemoryPromotionService。
def _managed_promotion(home, memory, candidates):
    from ..capability.persona_repository import PersonaRepository
    from ..memory_store.lessons import HotRuleRepository, LessonRepository
    from ..memory_store.promotion import (
        ConversationMessageEvidenceVerifier,
        MemoryPromotionDependencies,
        MemoryPromotionService,
    )

    store = _managed_conversations(home)
    return MemoryPromotionService(dependencies=MemoryPromotionDependencies(
        candidates=candidates, long_term=memory, persona=PersonaRepository.from_owner_result(home),
        lessons=LessonRepository(home.lessons_memory_dir, home.routing_index_md), hot=HotRuleRepository(home.memory_hot_md),
        message_verifier=ConversationMessageEvidenceVerifier(store), exclusion_store=store))


# LLM: 每个动作进入原正式库/候选流水；参数由不可变请求传递，UID仅取已解析home，不增加身份来源。
# 函数用途: 完成管理员记忆增改删、候选批准/驳回或陈旧候选清理。
def _mutate_managed_memory(home: OwnerHomeResult, request: _ManagedMemoryMutation):
    from datetime import datetime, timedelta, timezone

    from ..capability.persona_repository import PersonaMutationRequest, PersonaRepository

    uid = home.identity.owner_id
    operation, entry_id, candidate_id = request.operation, request.entry_id, request.candidate_id
    content, category, locked = request.content, request.category, request.locked
    expected_revision = request.expected_revision
    operations = {"add_memory", "edit_memory", "delete_memory", "approve_candidate", "reject_candidate", "clear_stale_candidates"}
    if operation not in operations or type(locked) is not bool:
        raise ValueError("invalid memory operation")
    memory, candidates = _managed_memory_stores(home)
    if operation in {"approve_candidate", "reject_candidate"}:
        if expected_revision is not None:
            raise ValueError("candidate operations use the original complete-snapshot CAS, not a numeric revision")
        candidate = candidates.get(candidate_id)
        if candidate.scope.scope_key != home.owner_id:
            raise ManagedUserScopeDeniedError()
        promotion = _managed_promotion(home, memory, candidates)
        reviewed = promotion.review(candidate, approved=operation == "approve_candidate", reviewer="local/main")
        if operation == "reject_candidate":
            return {"ok": True, "candidate": reviewed.to_dict()}
        result = promotion.promote(candidate_id, reviewer="local/main", confirmed=True, expected_candidate=reviewed)
        return {"ok": result.promoted, "promotion": result.to_dict(), "candidate": candidates.get(candidate_id).to_dict()}
    if operation == "clear_stale_candidates":
        policy = LocalUserStore(home.root).read_json(("owners", "local", uid, "retention.json")) or {}
        if policy.get("legal_hold"):
            return {"ok": True, "cleared": 0, "legal_hold": True}
        days = policy.get("rejected_candidate_days", 30)
        if type(days) is not int or days < 0:
            raise ValueError("invalid candidate retention days")
        if days == 0:
            return {"ok": True, "cleared": 0}
        stale = candidates.retention_candidates(before=datetime.now(timezone.utc) - timedelta(days=days))
        deleted = candidates.delete_terminal([row.candidate_id for row in stale], expected_snapshots={row.candidate_id: row for row in stale})
        return {"ok": True, "cleared": deleted}
    if category == "user":
        request = PersonaMutationRequest(target="user", action={"add_memory": "add", "edit_memory": "replace", "delete_memory": "remove"}[operation],
                                         content=content, entry_id=entry_id, confirmed=True, source="admin_managed_governance")
        if expected_revision is not None:
            raise ValueError("user profile mutations require persona SHA CAS through the persona API")
        with _admin_persona_write():
            result = PersonaRepository.from_owner_result(home).mutate(request)
        return {"ok": True, "entry": result}
    if category != "long_term":
        raise ValueError("lesson/hot writes must use an evidence-verified candidate promotion")
    if operation == "add_memory":
        if expected_revision not in {None, 0}:
            raise HostAPIError("REVISION_CONFLICT", 409)
        entry = memory.add("system", content, kind="fact", source="admin_managed_governance",
                           attributes={"admin_injected": True, "updated_by": "local/main"})
    elif operation == "edit_memory":
        entry = memory.replace(entry_id, content, expected_version=expected_revision, source="admin_managed_governance")
    else:
        entry = memory.remove(entry_id, expected_version=expected_revision, source="admin_managed_governance")
    if locked and operation == "add_memory":
        control = _GovernanceControl(home.root, uid)
        with control.locked():
            policy = control.policy()
            ids = policy["memory_governance"]["admin_locked_memory_ids"]
            if entry.entry_id not in ids:
                ids.append(entry.entry_id)
                policy.update(revision=policy["revision"] + 1, updated_by="local/main", updated_at=now_utc())
                control.write(("governance_policy.json",), policy, ("config", "governance_policy.json"))
    return {"ok": True, "entry": asdict(entry)}


# LLM: 仅绑定目标身份与原scheduler存储；不启动调度器，不取得管理员执行身份。
# 函数用途: 打开受管用户的原定时任务仓库。
def _managed_scheduler(home):
    from ..scheduler.repository import SchedulerRepository
    from .owner_quota import owner_quota_enforcer_from_policy

    return SchedulerRepository(home.scheduler_dir, owner_provider=home.identity.provider, owner_kind=home.identity.owner_kind,
                               owner_id=home.owner_id, due_owner_id=home.identity.owner_id,
                               quota_enforcer=owner_quota_enforcer_from_policy(home.home_dir, quota_path=home.quota_json))


# LLM: 缺原store才返回空；坏job不能被静默漏掉或作为健康零数量。
# 函数用途: 读取目标原定时计划列表。
def _managed_monitor_rows(home):
    if not home.scheduler_store_json.exists():
        return []
    rows, errors = _managed_scheduler(home).list_jobs()
    if errors:
        raise HostAPIError("GOVERNANCE_SCHEDULER_UNREADABLE", 503)
    return rows


# LLM: 资产读取复用各原仓库；技能资源逐叶nofollow读取，不把不透明凭据引用跨owner复制。
# 函数用途: 从管理员原空间读取一份完整资产传输值。
def _managed_asset_payload(home, kind, asset_id):
    from ..assets import safe_asset_id
    from ..assets.datasource_store import DatasourceStore
    from ..assets.script_store import ScriptStore
    from ..assets.workflow_store import WorkflowStore
    from .resource_grants import read_resource_grants

    safe_asset_id(asset_id)
    if kind == "script":
        record = ScriptStore(home.home_dir).get_script(asset_id)
        if record is None:
            raise KeyError(asset_id)
        return record
    if kind == "workflow":
        record = WorkflowStore(home.home_dir).get_workflow(asset_id)
        if record is None:
            raise KeyError(asset_id)
        return record.to_dict()
    if kind == "monitor":
        return _managed_scheduler(home).get_job(asset_id)
    if kind == "datasource":
        return asdict(DatasourceStore(home.home_dir).get_datasource_spec(asset_id))
    if kind == "grant":
        rows = [row for row in read_resource_grants(_managed_home_paths(home)) if row["resource_token"] == asset_id]
        if len(rows) != 1:
            raise ValueError("grant id must identify exactly one resource")
        return rows[0]
    if kind == "skill":
        files = {}
        base = home.home_dir / "skills" / asset_id
        for path in sorted(base.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            parts = tuple(path.relative_to(home.root).parts)
            files[str(path.relative_to(base))] = read_bytes_beneath(home.root, parts, max_bytes=2097152)
        if "SKILL.md" not in files:
            raise KeyError(asset_id)
        return {"files": files}
    raise ValueError("unsupported asset kind")


# LLM: 依赖只来自原结构化refs/steps，先收全图再写目标；循环、缺依赖、未重绑凭据均在安装前失败。
# 类用途: 构建有界管理员资产闭包，不运行任何资产。
class _ManagedAssetClosure:
    # LLM: 源固定local/main，payload_override仅替换根资产值，不改变来源身份。
    # 函数用途: 准备闭包遍历的唯一源和去重状态。
    def __init__(self, root: Path):
        from .owner_resolver import resolve_owner_home

        self.source = resolve_owner_home(root, OwnerIdentity.local_main())
        self.visiting: set[tuple[str, str]] = set()
        self.seen: set[tuple[str, str]] = set()
        self.plan: list[tuple[str, str, dict]] = []

    # LLM: 限64节点防畸形图；递归深度受节点数限制，refs只声明依赖不授执行权。
    # 函数用途: 后序收集工作流/脚本/技能依赖，并验证可安装值。
    def collect(self, kind: str, asset_id: str, payload: dict | None = None):
        from ..assets import canonical_skill_name, safe_asset_id

        safe_asset_id(asset_id)
        key = (kind, asset_id)
        if key in self.visiting or len(self.visiting) + len(self.seen) >= 64:
            raise ValueError("cyclic or oversized asset closure")
        if key in self.seen:
            return
        self.visiting.add(key)
        value = copy.deepcopy(payload) if payload is not None else _managed_asset_payload(self.source, kind, asset_id)
        descriptor = value.get("descriptor", value)
        if kind in {"script", "workflow"}:
            if descriptor.get("id") != asset_id or descriptor.get("kind") != kind:
                raise ValueError("asset identity mismatch")
            if descriptor.get("credential_refs"):
                raise HostAPIError("ASSET_CREDENTIAL_REBIND_REQUIRED", 409)
            refs = descriptor.get("refs", {})
            scripts = [*refs.get("scripts", []), *(step["script_id"] for step in value.get("steps", []) if step.get("script_id"))]
            skills = [*refs.get("skills", []), *(skill for step in value.get("steps", []) for skill in step.get("skill_ids", []))]
            for child in dict.fromkeys(scripts):
                self.collect("script", child)
            for child in dict.fromkeys(skills):
                if not child.startswith("builtin:"):
                    self.collect("skill", canonical_skill_name(child))
            for child in refs.get("workflows", []):
                self.collect("workflow", child)
        self.visiting.remove(key)
        self.seen.add(key)
        self.plan.append((kind, asset_id, value))


# LLM: 查询原资产是否存在，不使用传输值声称的workspace/root；输入ID已严格验证。
# 函数用途: 为冲突策略取得目标当前资产。
def _target_asset_exists(home, kind, asset_id):
    if kind in {"script", "workflow"}:
        try:
            _managed_asset_payload(home, kind, asset_id)
            return True
        except KeyError:
            return False
    if kind == "monitor":
        return any(row["job_id"] == asset_id for row in _managed_monitor_rows(home))
    if kind == "skill":
        return read_bytes_beneath(home.root, ("owners", "local", home.identity.owner_id, "skills", asset_id, "SKILL.md"), max_bytes=2097152) is not None
    if kind == "datasource":
        from ..assets.datasource_store import DatasourceStore

        try:
            DatasourceStore(home.home_dir).get_datasource_spec(asset_id)
            return True
        except KeyError:
            return False
    return False


# LLM: 映射在安装前固定；keep_both同步改工作流步骤及refs，身份只取home，skip不覆盖既有资产。
# 函数用途: 安装完整资产闭包并返回精确目标和管理员来源。
def _provision_managed_asset(root, home, kind, asset_id, auto_enable, strategy, note, payload_override):
    from uuid import uuid4

    closure = _ManagedAssetClosure(root)
    closure.collect(kind, asset_id, payload_override)
    mapping, skipped = {}, set()
    for child_kind, identifier, _value in closure.plan:
        exists = _target_asset_exists(home, child_kind, identifier)
        mapping[(child_kind, identifier)] = identifier + "-admin-" + uuid4().hex[:8] if exists and strategy == "keep_both_rename" else identifier
        if exists and strategy == "skip":
            skipped.add((child_kind, identifier))
    installed = []
    provenance = {"sender_owner_id": "local/main", "provisioned_by_admin": True, "auto_enabled": auto_enable}
    for child_kind, identifier, payload in closure.plan:
        if (child_kind, identifier) in skipped:
            continue
        target_id = mapping[(child_kind, identifier)]
        _install_managed_asset(home, closure.source, child_kind, target_id, payload, mapping,
                               {"enabled": auto_enable, "delivery_provenance": provenance, "admin_note": note})
        installed.append({"asset_kind": child_kind, "asset_id": target_id, "enabled": auto_enable})
    return {"ok": True, "target_owner_id": home.owner_id, "asset_id": mapping[(kind, asset_id)],
            "enabled": auto_enable, "installed": installed, "delivery_provenance": provenance, "skipped": len(skipped)}


# LLM: 只重写显式依赖ID和目标owner，来源字段不授予路径权；保留原资产校验及CAS。
# 函数用途: 将一个闭包节点安全安装到受管用户原资产仓库。
def _install_managed_asset(home, source, kind, asset_id, payload, mapping, metadata):
    from ..assets.descriptor import AssetDescriptor
    from ..assets.script_store import ScriptStore
    from ..assets.workflow_store import WorkflowDefinition, WorkflowStore

    if kind not in {"script", "workflow"}:
        return _install_other_managed_asset(home, source, kind, asset_id, payload, metadata)
    descriptor = copy.deepcopy(payload.get("descriptor", payload))
    descriptor = {key: value for key, value in descriptor.items() if key in AssetDescriptor.__dataclass_fields__ or key == "schema_version"}
    descriptor.update(id=asset_id, owner_id=home.owner_id, scope_layer="owner", source_scope="owner", workspace_root="")
    descriptor["metadata"] = {**descriptor.get("metadata", {}), **metadata}
    for plural, child_kind in (("scripts", "script"), ("workflows", "workflow"), ("skills", "skill")):
        refs = descriptor.get("refs", {})
        if plural in refs:
            refs[plural] = [mapping.get((child_kind, ref), ref) for ref in refs[plural]]
    if kind == "script":
        store = ScriptStore(home.home_dir)
        old = store.get_script(asset_id)
        return store.save_script(AssetDescriptor.from_dict(descriptor), payload["code"],
            entrypoint=payload.get("entrypoint", "main.py"), interpreter=payload.get("interpreter", "python3"),
            timeout_seconds=payload.get("timeout_seconds", 120), expected_revision=old["revision"] if old else 0)
    steps = copy.deepcopy(payload["steps"])
    for step in steps:
        if step.get("script_id"):
            step["script_id"] = mapping.get(("script", step["script_id"]), step["script_id"])
        step["skill_ids"] = [mapping.get(("skill", ref.removeprefix("owner:")), ref) for ref in step.get("skill_ids", [])]
    store = WorkflowStore(home.home_dir)
    old = store.get_workflow(asset_id)
    definition = WorkflowDefinition.from_any({"descriptor": descriptor, "steps": steps, "body_markdown": payload.get("body_markdown", "")})
    return store.save_workflow(definition, expected_revision=old.descriptor.revision if old else 0)


# LLM: 技能/数据源/授权/监控分别使用原存储，默认不激活；不接受闭包传来的任意写路径。
# 函数用途: 安装非脚本工作流的受管资产及来源记录。
def _install_other_managed_asset(home, source, kind, asset_id, payload, metadata):
    from ..assets import safe_asset_id
    from ..assets.datasource_store import DatasourceStore
    from .resource_grants import set_resource_grant

    safe_asset_id(asset_id)
    control = _GovernanceControl(home.root, home.identity.owner_id)
    if kind == "skill":
        for relative, data in payload["files"].items():
            parts = Path(relative).parts
            if not parts or Path(relative).is_absolute() or any(part in {".", ".."} for part in parts):
                raise ValueError("unsafe skill resource path")
            write_bytes_atomic_beneath(home.root, ("owners", "local", home.identity.owner_id, "skills", asset_id, *parts),
                                       data.encode() if isinstance(data, str) else data)
        control.write(("provisioned_assets", "skill-" + asset_id + ".json"), metadata,
                      ("data", "provisioned_assets", "skill-" + asset_id + ".json"))
        return _toggle_managed_asset(home, "skill", asset_id, metadata["enabled"])
    if kind == "monitor":
        return _install_managed_monitor(home, asset_id, payload, metadata)
    if kind == "datasource":
        spec = copy.deepcopy(payload)
        if spec.get("kind") == "sqlite" and spec.get("database_path") not in {None, "", ":memory:"}:
            raise HostAPIError("DATASOURCE_LOCAL_DATA_MIGRATION_REQUIRED", 409)
        source_store = DatasourceStore(source.home_dir)
        secret = source_store.resolve_secret(spec.get("credential_ref", ""))
        spec.update(datasource_id=asset_id, credential_ref="", scope="owner")
        result = DatasourceStore(home.home_dir).save_datasource(spec, secret_payload=secret)
    elif kind == "grant":
        result = set_resource_grant(_managed_home_paths(source), _managed_home_paths(home), payload, revoke=not metadata["enabled"])
    else:
        raise ValueError("unsupported asset kind")
    control.write(("provisioned_assets", kind + "-" + asset_id + ".json"), metadata,
                  ("data", "provisioned_assets", kind + "-" + asset_id + ".json"))
    return result


# LLM: 复用原scheduler构造、验证和单事务写入；paused在首次发表前设置，避免先active再暂停的可执行窗口。
# 函数用途: 将管理员监控定义直接安装到目标自己的调度账本。
def _install_managed_monitor(home, asset_id, payload, metadata):
    import time

    from ..scheduler.event_sources import sync_event_sources
    from ..scheduler.monitor_state import next_alias
    from ..scheduler.repository import SchedulerJobCreateRequest, _new_job_payload

    conversation = _managed_conversations(home, initialize=True)
    thread = conversation.threads.create(owner_id=home.owner_id, owner_home=str(home.home_dir),
                                         workspace_root=str(home.workspace_dir), cwd=str(home.workspace_dir))
    repo = _managed_scheduler(home)
    request = SchedulerJobCreateRequest(name=payload["name"], prompt=payload["prompt"], thread_id=thread.thread_id,
        schedule=payload["schedule"], source_request_id="admin-provision:" + asset_id, skill_refs=payload.get("skill_refs", ()),
        execution={"mode": "followup", "model_policy": "inherit"}, delivery={"mode": "local"},
        policy=payload.get("policy"), precheck=payload.get("precheck"), rules=payload.get("rules"))
    job = sync_event_sources(_new_job_payload(owner=repo.owner, request=request, now=time.time()))
    job.update(job_id=asset_id, status="active" if metadata["enabled"] else "paused", delivery_provenance=metadata["delivery_provenance"])
    with repo._mutation_scope() as (store, admission):
        old = store["jobs"].get(asset_id)
        job["version"] = old["version"] + 1 if old else 1
        job["alias"] = old["alias"] if old else next_alias(store)
        if repo._parse_job(job)[0] is None:
            raise ValueError("invalid managed monitor")
        store["jobs"][asset_id] = job
        repo._write_store_unlocked(store, admission)
    return copy.deepcopy(job)


# LLM: 技能存在性校验及原skill-policy锁内读改写独立收拢；不创建新启停账本，不授予执行权。
# 函数用途: 在受管用户原技能策略中设置一个技能的禁用状态。
def _toggle_managed_skill(home: OwnerHomeResult, asset_id: str, enabled: bool) -> None:
    if not _target_asset_exists(home, "skill", asset_id):
        raise KeyError(asset_id)
    store = LocalUserStore(home.root)
    parts = ("owners", "local", home.identity.owner_id, "skill_policy.json")
    with store.locked(parts):
        policy = store.read_json(parts)
        if policy is None or policy.get("schema_version") != "skill-policy.v1":
            raise ValueError("invalid skill policy")
        disabled = set(policy.get("disabled_skills", [])) - {asset_id, "owner:" + asset_id}
        if not enabled:
            disabled.add(asset_id)
        policy["disabled_skills"] = sorted(disabled)
        store.write_json(parts, policy)


# LLM: script/workflow写回原metadata；skill委托原策略锁事务，monitor用原pause/resume CAS，不执行资产。
# 函数用途: 更新受管资产的原存储启停状态并返回当前值。
def _toggle_managed_asset(home, kind, asset_id, enabled):
    from ..assets import safe_asset_id
    from ..assets.script_store import ScriptStore
    from ..assets.workflow_store import WorkflowStore

    safe_asset_id(asset_id)
    if kind in {"script", "workflow"}:
        payload = _managed_asset_payload(home, kind, asset_id)
        mapping = {}
        _install_managed_asset(home, home, kind, asset_id, payload, mapping, {"enabled": enabled})
    elif kind == "monitor":
        repo = _managed_scheduler(home)
        job = repo.get_job(asset_id)
        method = repo.resume_job if enabled else repo.pause_job
        method(asset_id, expected_version=job["version"])
    elif kind == "skill":
        _toggle_managed_skill(home, asset_id, enabled)
    else:
        raise ValueError("unsupported toggle kind")
    return {"ok": True, "target_owner_id": home.owner_id, "asset_kind": kind, "asset_id": asset_id, "enabled": enabled}


GOVERNANCE_TOOL_ACTIONS = {
    "user_overview": "get_overview", "set_governance_policy": "set_policy", "get_user_persona": "get_persona",
    "set_user_persona": "set_persona", "list_user_memories": "list_memories", "mutate_user_memory": "mutate_memories",
    "provision_user_assets": "provision_assets", "toggle_user_asset": "toggle_asset", "dispatch_user_task": "dispatch_task",
    "list_user_tasks": "list_tasks", "list_governance_reviews": "list_reviews", "decide_governance_review": "decide_review",
}
GOVERNANCE_READ_ACTIONS = ("user_overview", "get_user_persona", "list_user_memories", "list_user_tasks", "list_governance_reviews")


# LLM: 模型只拿领域参数，actor/root/updated_by由宿主固定；不允许通过JSON建立管理员身份。
# 函数用途: 为管理员工具声明12个治理动作的具体输入字段。
def governance_tool_schema() -> dict[str, Any]:
    text = {"type": "string"}
    fields = {name: dict(text) for name in (
        "target_owner_id", "soul_md", "agents_md", "user_md", "category", "status_filter", "entry_id", "candidate_id",
        "content", "asset_id", "conflict_strategy", "note", "prompt", "title", "thread_id", "priority", "review_id",
        "feedback", "review_type_filter", "idempotency_key")}
    fields.update({name: {"type": "boolean"} for name in ("include_candidates", "locked", "auto_enable", "enabled")})
    fields.update({name: {"type": "object"} for name in ("policy_patch", "patch_override", "payload_override")})
    fields["expected_revision"] = {"type": "integer", "minimum": 0}
    fields["persona_mode"] = {"type": "string", "enum": sorted(_PERSONA_MODES)}
    fields["operation"] = {"type": "string", "enum": ["add_memory", "edit_memory", "delete_memory", "approve_candidate", "reject_candidate", "clear_stale_candidates"]}
    fields["asset_kind"] = {"type": "string", "enum": ["workflow", "monitor", "script", "skill", "datasource", "grant"]}
    fields["decision"] = {"type": "string", "enum": ["approve", "reject_with_feedback"]}
    return fields


# LLM: 此函数只由AdminControlsTool已鉴权主代理入口调用；反射只校验固定映射参数，不调用模型给的函数名。
# 函数用途: 将管理员工具动作分派到准确目标的治理服务或审核仓库。
def dispatch_governance_tool(root: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    from inspect import signature

    from .governance_review_store import GovernanceReviewStore

    if action not in GOVERNANCE_TOOL_ACTIONS:
        raise ValueError("unknown governance action")
    values = dict(params)
    values.pop("action", None)
    aliases = [name for name in ("owner_id", "user_id", "target_owner_id") if name in values]
    if len(aliases) > 1:
        raise ValueError("provide exactly one target identity")
    target = values.pop(aliases[0]) if aliases else None
    service = GovernanceReviewStore(root, actor_owner=OwnerIdentity.local_main()) if action in {
        "list_governance_reviews", "decide_governance_review"} else ManagedUserGovernanceService(root, actor_owner=OwnerIdentity.local_main())
    method = getattr(service, GOVERNANCE_TOOL_ACTIONS[action])
    if target is not None:
        values["target_owner_id"] = target
    if set(values) - set(signature(method).parameters):
        raise ValueError("unsupported governance action parameters")
    signature(method).bind(**values)
    return method(**values)
