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
_ADMIN_MEMORY_WRITE: ContextVar[tuple[str, str] | None] = ContextVar("managed_admin_memory_write", default=None)
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
            "updated_at": "", "updated_by": "", "persona_mode": "user_autonomous", "memory_curate_mode": "user_autonomous",
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
    return {*_POLICY_FIELDS, "persona_mode", "memory_curate_mode", "role_title", "role_tags"}


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
    if key == "memory_curate_mode":
        if type(value) is not str or value not in {"user_autonomous", "admin_only"}:
            raise ValueError("invalid memory curate mode")
        return value
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

    # LLM: actor先核验再建立精确目标活栈；反射可见的锁参数保留旧locked优先级；options不授身份。
    # 函数用途: 注入、修正、删除或审核受管用户记忆。
    def mutate_memories(self, target_owner_id: str, operation: str, *, entry_id: str = "", candidate_id: str = "",
                        content: str = "", category: str = "long_term", locked: bool | None = None,
                        expected_revision: int | None = None, lock_memory: bool = True,
                        options: dict[str, Any] | None = None) -> dict[str, Any]:
        _uid, _identity, home = self._target(target_owner_id)
        if options is not None and (type(options) is not dict or options):
            raise ValueError("unsupported memory options")
        if type(lock_memory) is not bool or locked is not None and type(locked) is not bool:
            raise ValueError("memory lock flags must be boolean")
        request = _ManagedMemoryMutation(operation, entry_id, candidate_id, content, category,
                                          lock_memory if locked is None else locked, expected_revision)
        token = _ADMIN_MEMORY_WRITE.set((str(home.root), home.owner_id))
        try:
            return _mutate_managed_memory(home, request)
        finally:
            _ADMIN_MEMORY_WRITE.reset(token)

    # LLM: 闭包在安装前读取并校验依赖；缺源/目标在读取阶段转ASSET_NOT_FOUND，不把部分安装错误洗成未开始。
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

    # LLM: 目标与管理员身份由原resolver核验；票据仅按完整动作消费一次，不在此执行命令或扩权。
    # 函数用途: 为宿主调用方及管理员工具接通原审核票据消费门。
    def consume_approved_review_ticket(self, target_owner_id: str, review_id: str, review_type: str,
                                      action_match: dict[str, Any]) -> dict[str, Any]:
        from .governance_review_store import GovernanceReviewStore

        _uid, _identity, home = self._target(target_owner_id)
        return GovernanceReviewStore(self.root_dir, actor_owner=self.actor_owner).consume_approved_action(
            review_id, target_user=home.owner_id, review_type=review_type, action_match=action_match)


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


# LLM: 核对目标owner/conversations与唯一runtime_paths会话库；读写均先检查nofollow目录链，不接管理员队列。
# 函数用途: 打开受管用户自己的原会话存储，按需定位运行时workspace会话线程。
def _managed_conversations(home: OwnerHomeResult, *, initialize: bool = False, thread_id: str = ""):
    import os
    from types import SimpleNamespace

    from ..common.nofollow_fs import ensure_private_dir, open_directory_beneath
    from ..conversation.store import ConversationStore
    from .runtime_paths import runtime_paths_for_agent

    direct = home.home_dir / "conversations"
    scoped = SimpleNamespace(owner_home_dir=home.home_dir, owner_workspace_dir=home.workspace_dir)
    runtime = runtime_paths_for_agent(SimpleNamespace(), home.home_dir, scoped)["conversation_workspace"]
    if initialize:
        ensure_private_dir(direct, canonical_root=home.root)
    try:
        os.close(open_directory_beneath(home.root, ("owners", "local", home.identity.owner_id, "conversations")))
    except FileNotFoundError:
        if initialize:
            raise
    direct_store = ConversationStore(direct, initialize=initialize)
    if runtime != direct and runtime.exists():
        os.close(open_directory_beneath(home.root, runtime.relative_to(home.root).parts))
        runtime_store = ConversationStore(runtime, initialize=False)
        if thread_id == "*":
            return runtime_store
        if thread_id and direct_store.threads.load(thread_id) is None and runtime_store.threads.load(thread_id) is not None:
            return runtime_store
        if not thread_id and not (direct / "threads").exists() and (runtime / "threads").exists():
            return runtime_store
    return direct_store


# LLM: 只创建目标owner线程和原task/wake事实；priority只是队列级别，不扩大用户权限。
# 函数用途: 记录管理员消息、目标身份任务和唤醒，返回真实排队标识。
def _dispatch_managed_task(home, prompt, title, thread_id, priority):
    from ..conversation.models import new_id

    if type(prompt) is not str or not prompt.strip() or len(prompt) > 32000:
        raise ValueError("invalid prompt")
    if type(title) is not str or len(title) > 160 or priority not in {"normal", "high", "urgent", "low"}:
        raise ValueError("invalid dispatch options")
    store = _managed_conversations(home, initialize=True, thread_id=str(thread_id or ""))
    thread = store.threads.require(thread_id) if thread_id else store.threads.create(
        canonical_user_id=home.owner_id, title=title, owner_id=home.owner_id, owner_home=str(home.home_dir),
        workspace_root=str(home.workspace_dir), cwd=str(home.workspace_dir))
    if thread.owner_id != home.owner_id or Path(thread.owner_home) != home.home_dir:
        raise ManagedUserScopeDeniedError()
    request_id, task_id = new_id("request"), new_id("task")
    metadata = {"dispatched_by": "local/main", "target_owner_id": home.owner_id,
                "execution_owner_id": home.owner_id, "request_id": request_id, "task_id": task_id}
    urgency = "urgent" if priority in {"high", "urgent"} else "normal"
    stores = {store.storage.root: store}
    for extra in (_managed_conversations(home, initialize=False), _managed_conversations(home, initialize=False, thread_id="*")):
        if extra.storage.root.exists():
            stores.setdefault(extra.storage.root, extra)
    for target in stores.values():
        if target.threads.load(thread.thread_id) is None:
            target.threads.write(thread)
        target.messages.append({"thread_id": thread.thread_id, "role": "user", "content": prompt,
                                "channel": "internal", "metadata": metadata})
        target.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "request_id": request_id,
                           "goal": prompt, "status": "queued"})
        target.wakes.raise_signal({"thread_id": thread.thread_id, "reason": "admin_dispatched_task", "root_task_id": task_id,
                                   "summary": prompt, "urgency": urgency, "metadata": metadata, "dedupe_key": task_id})
    return {"ok": True, **metadata, "thread_id": thread.thread_id, "status": "queued"}


# LLM: 返回原任务链接与可见错误；不把未读/截断当作无任务，最多扫描200个最近线程。
# 函数用途: 查询目标会话任务、子代理状态和定时计划。
def _list_managed_tasks(home, status_filter, limit):
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    stores = {_managed_conversations(home).storage.root: _managed_conversations(home)}
    runtime_store = _managed_conversations(home, thread_id="*")
    stores.setdefault(runtime_store.storage.root, runtime_store)
    by_key, errors, total_threads = {}, [], 0
    for store in stores.values():
        threads = store.threads.list(limit=200)
        total_threads = max(total_threads, len(threads))
        for thread in threads:
            if thread.owner_id and thread.owner_id != home.owner_id:
                raise ManagedUserScopeDeniedError()
            rows, failures = store.tasks.list_report(thread.thread_id)
            for row in rows:
                item = row.to_dict()
                key = (item.get("thread_id"), item.get("task_id"))
                if key not in by_key or item.get("updated_at", 0) >= by_key[key].get("updated_at", 0):
                    by_key[key] = item
            errors.extend(failures)
    selected = [row for row in by_key.values() if status_filter is None or row.get("status") == status_filter]
    selected.sort(key=lambda row: row.get("updated_at", 0), reverse=True)
    subagents = []
    for path in sorted(home.agents_dir.glob("*/state.json"))[:200]:
        row = LocalUserStore(home.root).read_json(tuple(path.relative_to(home.root).parts))
        if row and (status_filter is None or row.get("status") == status_filter):
            subagents.append(row)
    monitors = _managed_monitor_rows(home)
    return {"target_owner_id": home.owner_id, "tasks": selected[:limit], "subagent_runs": subagents[:limit],
            "scheduled_jobs": [row for row in monitors if status_filter is None or row.get("status") == status_filter][:limit],
            "errors": errors, "truncated": len(selected) > limit or total_threads == 200,
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
    pending = [row.to_record() for row in candidates.list(statuses={status_filter} if status_filter else None)] if include_candidates else []
    return {"target_owner_id": home.owner_id, "memories": rows, "candidates": pending,
            "query_evidence": {"category": category, "include_candidates": include_candidates,
                               "status_filter": status_filter, "memories_returned": len(rows), "candidates_returned": len(pending)}}


# LLM: 调用方不能用agent_home/source声明管理员；只有已核服务的精确root/owner活栈可绕过锁。
# 函数用途: 核验受管用户正式记忆的管理员锁与仅管理员整理模式。
def require_managed_memory_write(agent_home, owner_home, memory_id=None) -> None:
    context = _persona_governance(Path(getattr(owner_home, "home_dir", owner_home)))
    if context is None:
        return
    control, _identity, home = context
    root = Path(agent_home if isinstance(agent_home, (str, Path)) else agent_home.root).resolve()
    if root != home.root:
        raise ManagedUserScopeDeniedError()
    if _ADMIN_MEMORY_WRITE.get() == (str(home.root), home.owner_id):
        return
    policy = control.policy()
    if (policy.get("memory_curate_mode") == "admin_only"
            or memory_id in policy["memory_governance"]["admin_locked_memory_ids"]):
        raise HostAPIError("MEMORY_LOCKED_BY_ADMIN", 403)


# LLM: 只认规范正式JSONL；调用方先领quota，普通写持policy锁跨正式提交，统一quota→policy→memory锁序。
# 函数用途: 为JSONL唯一批量写口提供不授权的受管锁保护。
@contextmanager
def managed_memory_batch_guard(memory_path: Path, operations):
    path = Path(memory_path).absolute()
    if tuple(path.parts[-3:]) != ("memory", "long_term", "memory.jsonl"):
        yield
        return
    context = _persona_governance(path.parents[2])
    if context is None:
        yield
        return
    control, _identity, home = context
    if _ADMIN_MEMORY_WRITE.get() == (str(home.root), home.owner_id):
        yield
        return
    with control.locked():
        for operation in operations:
            require_managed_memory_write(home.root, home.home_dir, operation.get("entry_id"))
        yield


# LLM: 只读取规范受管正式JSONL对应的宿主策略；非受管目录不新增策略或写入，不把镜像当权威。
# 函数用途: 给浓缩选择器返回管理员锁定的精确正式记忆ID。
def managed_locked_memory_ids(memory_path: Path) -> set[str]:
    path = Path(memory_path).absolute()
    if tuple(path.parts[-3:]) != ("memory", "long_term", "memory.jsonl"):
        return set()
    context = _persona_governance(path.parents[2])
    return set(context[0].policy()["memory_governance"]["admin_locked_memory_ids"]) if context else set()


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


# LLM: 只读目标用户候选库；scope是适用范围而非owner，兼容同owner个人旧键；坏scope/外来来源在任何写入前拒绝。
# 函数用途: 读取候选并分开核验记忆范围合同与学习来源归属。
def _managed_candidate_for_owner(home, candidates, candidate_id):
    from ..memory_store.candidate_models import MemoryScope
    from ..memory_store.candidates import CandidateStoreCorruptError

    try:
        candidate = candidates.get(candidate_id)
        scope = MemoryScope.from_value(candidate.scope)
        if not (scope.scope_type == "personal" and scope.scope_key == home.owner_id):
            MemoryScope.for_new_observation(scope)
    except (ValueError, CandidateStoreCorruptError) as error:
        raise ManagedUserScopeDeniedError() from error
    source = candidate.learning_source
    for field in ("owner", "owner_id"):
        owner = source.get(field) if isinstance(source, dict) else getattr(source, field, None)
        if owner and owner != home.owner_id:
            raise ManagedUserScopeDeniedError()
    return candidate


# LLM: 每个动作进入原正式库/候选流水；已晋升approve在目标库、合法scope和来源owner核验后只返快照，不重复写事实。
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
        candidate = _managed_candidate_for_owner(home, candidates, candidate_id)
        if operation == "approve_candidate" and candidate.status == "promoted":
            return {"ok": True, "already_promoted": True, "candidate": candidate.to_record()}
        promotion = _managed_promotion(home, memory, candidates)
        reviewed = promotion.review(candidate, approved=operation == "approve_candidate", reviewer="local/main")
        if operation == "reject_candidate":
            return {"ok": True, "candidate": reviewed.to_record()}
        result = promotion.promote(candidate_id, reviewer="local/main", confirmed=True, expected_candidate=reviewed)
        return {"ok": result.promoted, "promotion": result.to_dict(), "candidate": candidates.get(candidate_id).to_record()}
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
        stale_ids = [row if isinstance(row, str) else row.candidate_id for row in stale]
        snapshots = {row.candidate_id: row for row in stale if not isinstance(row, str)} or None
        deleted = candidates.delete_terminal(stale_ids, expected_snapshots=snapshots)
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
    return _write_managed_formal_memory(home, memory, request)


# LLM: quota→policy覆盖正式写和锁ID增删；批量提交复用原准入，不再policy→quota或重复领取不可重入锁。
# 函数用途: 写正式记忆并同步管理员锁定或解锁状态。
def _write_managed_formal_memory(home, memory, request: _ManagedMemoryMutation):
    control = _GovernanceControl(home.root, home.identity.owner_id)
    with memory.quota_enforcer.admission() as admission, control.locked():
        if request.operation == "add_memory":
            _require_revision(request.expected_revision, 0)
            operation = {"action": "add", "role": "system", "content": request.content, "kind": "fact",
                         "source": "admin_managed_governance", "attributes": {"admin_injected": True, "updated_by": "local/main"}}
        else:
            operation = {"action": "replace" if request.operation == "edit_memory" else "remove", "entry_id": request.entry_id,
                         "content": request.content, "expected_version": request.expected_revision, "source": "admin_managed_governance"}
        entries = memory.apply_batch([operation], _admission=admission)
        if entries:
            entry = entries[0]
        else:
            from ..memory_store.jsonl import MemoryRecord, _memory_identity_key

            identity = _memory_identity_key(MemoryRecord("system", request.content.strip(), kind="fact"))
            entry = next(record for record in memory.all() if _memory_identity_key(record) == identity)
        policy = control.policy()
        ids = set(policy["memory_governance"]["admin_locked_memory_ids"])
        next_ids = ids | {entry.entry_id} if request.locked and request.operation != "delete_memory" else ids - {entry.entry_id}
        if ids != next_ids:
            policy["memory_governance"]["admin_locked_memory_ids"] = sorted(next_ids)
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


# LLM: 非owner稳定来源不得剥前缀变成私人目录；只解析裸名/owner引用，其它版本引用保持原样。
# 函数用途: 从规范监控引用中识别真正需要复制的管理员私人技能。
def _managed_monitor_owner_skill_name(reference):
    from ..assets import canonical_skill_name

    stable_id = reference["stable_id"]
    if reference.get("kind") == "capability_package" or ":" in stable_id and not stable_id.startswith("owner:"):
        return None
    return canonical_skill_name(stable_id)


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
    # 函数用途: 后序收集工作流/脚本/技能及监控依赖，并验证可安装值。
    def collect(self, kind: str, asset_id: str, payload: dict | None = None):
        from ..assets import canonical_skill_name, safe_asset_id

        safe_asset_id(asset_id)
        key = (kind, asset_id)
        if key in self.visiting or len(self.visiting) + len(self.seen) >= 64:
            raise ValueError("cyclic or oversized asset closure")
        if key in self.seen:
            return
        self.visiting.add(key)
        value = copy.deepcopy(payload) if payload is not None else _require_managed_asset_payload(self.source, kind, asset_id)
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
        elif kind == "monitor":
            self._monitor_dependencies(value)
        self.visiting.remove(key)
        self.seen.add(key)
        self.plan.append((kind, asset_id, value))

    # LLM: 沿scheduler版本引用合同读取stable_id，核冻结正文hash；builtin/包不降级成本地技能，统一闭包去重/限额。
    # 函数用途: 在安装任何节点前补齐监控用到的owner技能和脚本。
    def _monitor_dependencies(self, value):
        from hashlib import sha256

        from ..scheduler.repository import _normalize_skill_refs

        for reference in _normalize_skill_refs(value.get("skill_refs") or []):
            identifier = _managed_monitor_owner_skill_name(reference)
            if identifier is None:
                continue
            self.collect("skill", identifier)
            payload = next(row for kind, name, row in self.plan if kind == "skill" and name == identifier)
            raw = payload["files"]["SKILL.md"]
            if sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest() != reference["content_sha256"]:
                raise HostAPIError("ASSET_SKILL_REFERENCE_STALE", 409)
        for rule in value.get("rules") or ():
            tool = rule.get("tool") or {}
            child = (tool.get("arguments") or {}).get("script_id")
            if tool.get("name") == "scripts" and child:
                self.collect("script", child)


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


# LLM: 同一次原Store提交同步正式enabled与metadata；非脚本节点传递冻结映射，工作流定义层同值。
# 函数用途: 将一个闭包节点安全安装到受管用户原资产仓库。
def _install_managed_asset(home, source, kind, asset_id, payload, mapping, metadata):
    from ..assets.descriptor import AssetDescriptor
    from ..assets.script_store import ScriptStore
    from ..assets.workflow_store import WorkflowDefinition, WorkflowStore

    if kind not in {"script", "workflow"}:
        return _install_other_managed_asset(home, source, kind, asset_id, payload, metadata, mapping)
    descriptor = copy.deepcopy(payload.get("descriptor", payload))
    descriptor = {key: value for key, value in descriptor.items() if key in AssetDescriptor.__dataclass_fields__ or key == "schema_version"}
    descriptor.update(id=asset_id, owner_id=home.owner_id, scope_layer="owner", source_scope="owner", workspace_root="",
                      enabled=metadata["enabled"])
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
    definition = WorkflowDefinition.from_any({"descriptor": descriptor, "steps": steps,
        "body_markdown": payload.get("body_markdown", ""), "enabled": metadata["enabled"]})
    return store.save_workflow(definition, expected_revision=old.descriptor.revision if old else 0)


# LLM: 技能先整体验证并准备身份/初始开关；监控消费已冻结映射，其它资产仍走原存储和身份边界。
# 函数用途: 安装非脚本工作流的受管资产及来源记录。
def _install_other_managed_asset(home, source, kind, asset_id, payload, metadata, mapping):
    from ..assets import safe_asset_id
    from ..assets.datasource_store import DatasourceStore
    from .resource_grants import set_resource_grant

    safe_asset_id(asset_id)
    control = _GovernanceControl(home.root, home.identity.owner_id)
    if kind == "skill":
        files = _prepared_managed_skill_files(payload, asset_id, metadata)
        for relative, data in files.items():
            write_bytes_atomic_beneath(home.root, ("owners", "local", home.identity.owner_id, "skills", asset_id,
                                                  *Path(relative).parts), data)
        control.write(("provisioned_assets", "skill-" + asset_id + ".json"), metadata,
                      ("data", "provisioned_assets", "skill-" + asset_id + ".json"))
        return _toggle_managed_asset(home, "skill", asset_id, metadata["enabled"])
    if kind == "monitor":
        return _install_managed_monitor(home, asset_id, payload, metadata, mapping)
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


# LLM: 所有资源路径及侧元数据先验证，未完成准备不得写任何成员；侧文件先于正文发表，原输入不修改。
# 函数用途: 准备准确技能身份、初始开关和来源，复用原投递frontmatter重命名codec。
def _prepared_managed_skill_files(payload, asset_id, metadata):
    from ..assets.script_store_support import _received_skill_body
    from ..assets.skill_adapter import _descriptor
    from ..capability.skills import split_skill_frontmatter

    files = {}
    for relative, data in payload["files"].items():
        path = Path(relative)
        if not path.parts or path.is_absolute() or any(part in {".", ".."} for part in path.parts):
            raise ValueError("unsafe skill resource path")
        name = str(path)
        if name in files or not isinstance(data, (str, bytes)):
            raise ValueError("invalid or duplicated skill resource")
        files[name] = data.encode("utf-8") if isinstance(data, str) else data
    meta = json.loads(files.get("skill_meta.json", b"{}"))
    if not isinstance(meta, dict) or not isinstance(meta.get("metadata", {}), dict):
        raise ValueError("invalid skill metadata")
    meta.update(id=asset_id, name=asset_id, enabled=bool(metadata["enabled"]),
                metadata={**meta.get("metadata", {}), **metadata})
    body = files.pop("SKILL.md", None)
    if body is not None and split_skill_frontmatter(body.decode("utf-8"))[0].get("name") != asset_id:
        body = _received_skill_body(body, asset_id, bool(metadata["enabled"])).encode("utf-8")
    header = split_skill_frontmatter(body.decode("utf-8"))[0] if body is not None else {}
    _descriptor(Path(asset_id), {**header, **meta}, "")
    files.pop("skill_meta.json", None)
    return {"skill_meta.json": json.dumps(meta, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            **files, **({"SKILL.md": body} if body is not None else {})}


# LLM: 技能引用保留原类型/前缀，改名后按安装正文重钉hash；builtin/包原样保留，脚本仅改scripts规则。
# 函数用途: 将监控中的技能及脚本依赖重绑到本次安装的目标身份。
def _mapped_managed_monitor_refs(home, payload, mapping):
    from hashlib import sha256

    from ..scheduler.repository import _normalize_skill_refs

    refs = []
    for reference in _normalize_skill_refs(payload.get("skill_refs") or []):
        ref = reference["stable_id"]
        name = _managed_monitor_owner_skill_name(reference)
        if name is None:
            refs.append(reference)
            continue
        target = mapping.get(("skill", name), name)
        raw = read_bytes_beneath(home.root, ("owners", "local", home.identity.owner_id,
            "skills", target, "SKILL.md"), max_bytes=2097152)
        if raw is None:
            raise HostAPIError("ASSET_NOT_FOUND", 404)
        refs.append({**reference, "stable_id": "owner:" + target if ref.startswith("owner:") else target,
                     "content_sha256": sha256(raw).hexdigest()})
    rules = copy.deepcopy(payload.get("rules"))
    for rule in rules or ():
        tool = rule.get("tool") or {}
        arguments = tool.get("arguments") or {}
        identifier = arguments.get("script_id")
        if tool.get("name") == "scripts" and identifier:
            arguments["script_id"] = mapping.get(("script", identifier), identifier)
    return refs, rules


# LLM: 配发显式传映射后重绑依赖；原审核省略映射时保留冻结引用/旧签名，复用scheduler事务和初始enabled/status。
# 函数用途: 将管理员监控定义直接安装到目标自己的调度账本。
def _install_managed_monitor(home, asset_id, payload, metadata, mapping=None):
    import time

    from ..scheduler.event_sources import sync_event_sources
    from ..scheduler.monitor_state import next_alias
    from ..scheduler.repository import SchedulerJobCreateRequest, _new_job_payload

    skill_refs, rules = (_mapped_managed_monitor_refs(home, payload, mapping) if mapping is not None
                        else (payload.get("skill_refs", ()), payload.get("rules")))
    conversation = _managed_conversations(home, initialize=True)
    thread = conversation.threads.create(owner_id=home.owner_id, owner_home=str(home.home_dir),
                                         workspace_root=str(home.workspace_dir), cwd=str(home.workspace_dir))
    repo = _managed_scheduler(home)
    request = SchedulerJobCreateRequest(name=payload["name"], prompt=payload["prompt"], thread_id=thread.thread_id,
        schedule=payload["schedule"], source_request_id="admin-provision:" + asset_id, skill_refs=skill_refs,
        execution={"mode": "followup", "model_policy": "inherit"}, delivery={"mode": "local"},
        policy=payload.get("policy"), precheck=payload.get("precheck"), rules=rules)
    job = sync_event_sources(_new_job_payload(owner=repo.owner, request=request, now=time.time()))
    job.update(job_id=asset_id, enabled=metadata["enabled"], status="active" if metadata["enabled"] else "paused",
               delivery_provenance=metadata["delivery_provenance"])
    with repo._mutation_scope() as (store, admission):
        old = store["jobs"].get(asset_id)
        job["version"] = old["version"] + 1 if old else 1
        job["alias"] = old["alias"] if old else next_alias(store)
        if repo._parse_job(job)[0] is None:
            raise ValueError("invalid managed monitor")
        store["jobs"][asset_id] = job
        repo._write_store_unlocked(store, admission)
    return copy.deepcopy(job)


# LLM: 先写SkillAdapter原skill_meta执行门，再同步原发现策略；任一步失败不报成功，不创建影子侧文件。
# 函数用途: 在受管用户原技能策略中设置一个技能的禁用状态。
def _toggle_managed_skill(home: OwnerHomeResult, asset_id: str, enabled: bool) -> None:
    from ..assets.skill_adapter import SkillAdapter

    if not _target_asset_exists(home, "skill", asset_id):
        raise KeyError(asset_id)
    SkillAdapter(home.home_dir).set_enabled(asset_id, enabled)
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


# LLM: 原scheduler单锁事务同步enabled/status，复用原事件退休、待运行取消和下次时间算法，不执行计划。
# 函数用途: 原子更新一个目标用户计划的正式开关及调度状态。
def _toggle_managed_monitor(home: OwnerHomeResult, asset_id: str, enabled: bool) -> None:
    import time

    from ..scheduler.event_source_lifecycle import cancel_retired_event_runs
    from ..scheduler.event_sources import sync_event_sources
    from ..scheduler.monitor_state import resume_monitor
    from ..scheduler.repository import _resume_next_run

    repo, current = _managed_scheduler(home), time.time()
    with repo._mutation_scope() as (store, admission):
        job = repo._require_mutable_job(store, asset_id, None)
        previous = copy.deepcopy(job)
        job = {**job, "enabled": enabled, "updated_at": current, "version": int(job["version"]) + 1}
        cancelled = []
        if enabled:
            if job.get("rules"):
                job = resume_monitor(job, current)
            next_run = float(job["next_run_at"]) if job.get("rules") else _resume_next_run(job, current)
            job.update(status="active", next_run_at=next_run, paused_at=0.0, paused_reason="")
            job = sync_event_sources(job, previous, retire=True)
        else:
            job.update(status="paused", paused_at=current, paused_reason="admin_managed_governance")
            job = cancel_retired_event_runs(store, sync_event_sources(job, retire=True), now=current)
            cancelled = repo._cancel_waiting_runs_unlocked(store, asset_id, current, "job_paused")
        store["jobs"][asset_id] = job
        repo._write_store_unlocked(store, admission, history_records=cancelled)


# LLM: 缺资产在无副作用预读阶段拒绝；script/workflow三层闸同事务同步，skill/monitor写原开关，不执行资产。
# 函数用途: 更新受管资产的原存储启停状态并返回当前值。
def _toggle_managed_asset(home, kind, asset_id, enabled):
    from ..assets import safe_asset_id

    safe_asset_id(asset_id)
    payload = _require_managed_asset_payload(home, kind, asset_id)
    if kind in {"script", "workflow"}:
        mapping = {}
        _install_managed_asset(home, home, kind, asset_id, payload, mapping, {"enabled": enabled})
    elif kind == "monitor":
        _toggle_managed_monitor(home, asset_id, enabled)
    elif kind == "skill":
        _toggle_managed_skill(home, asset_id, enabled)
    else:
        raise ValueError("unsupported toggle kind")
    return {"ok": True, "target_owner_id": home.owner_id, "asset_kind": kind, "asset_id": asset_id, "enabled": enabled}


# LLM: 只在读取源/目标资产的无副作用阶段转换缺失；安装后存储错误不得误标not_started。
# 函数用途: 将资产或依赖缺失投影为可重试的确定领域拒绝。
def _require_managed_asset_payload(home, kind, asset_id):
    from ..scheduler.repository import SchedulerNotFoundError

    try:
        return _managed_asset_payload(home, kind, asset_id)
    except (KeyError, FileNotFoundError, SchedulerNotFoundError):
        raise HostAPIError("ASSET_NOT_FOUND", 404, outcome="not_started") from None


GOVERNANCE_TOOL_ACTIONS = {
    "user_overview": "get_overview", "set_governance_policy": "set_policy", "get_user_persona": "get_persona",
    "set_user_persona": "set_persona", "list_user_memories": "list_memories", "mutate_user_memory": "mutate_memories",
    "provision_user_assets": "provision_assets", "toggle_user_asset": "toggle_asset", "dispatch_user_task": "dispatch_task",
    "list_user_tasks": "list_tasks", "list_governance_reviews": "list_reviews", "decide_governance_review": "decide_review",
    "consume_governance_review_ticket": "consume_approved_review_ticket",
}
GOVERNANCE_READ_ACTIONS = ("user_overview", "get_user_persona", "list_user_memories", "list_user_tasks", "list_governance_reviews")


# LLM: 模型只拿领域参数，actor/root/updated_by由宿主固定；不允许通过JSON建立管理员身份。
# 函数用途: 为管理员工具声明治理与一次性票据消费的具体输入字段。
def governance_tool_schema() -> dict[str, Any]:
    text = {"type": "string"}
    fields = {name: dict(text) for name in (
        "target_owner_id", "soul_md", "agents_md", "user_md", "category", "status_filter", "entry_id", "candidate_id",
        "content", "asset_id", "conflict_strategy", "note", "prompt", "title", "thread_id", "priority", "review_id",
        "feedback", "review_type_filter", "review_type", "idempotency_key")}
    fields.update({name: {"type": "boolean"} for name in ("include_candidates", "locked", "lock_memory", "auto_enable", "enabled")})
    fields.update({name: {"type": "object"} for name in ("policy_patch", "patch_override", "payload_override")})
    fields["action_match"] = {"type": "object", "minProperties": 1}
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
