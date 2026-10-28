# LLM: 工作区只绑定执行位置，不改变owner home、宿主控制面或其它owner权限；CLI、Gateway、分叉共用纯校验。
# 模块用途: 为会话目录绑定提供唯一目录校验及只读项目上下文，检查不创建目录或锁。
from __future__ import annotations

from pathlib import Path


# LLM: 路径非法属于稳定参数错误；上层CLI可直接抛出，Gateway可映射自身目录错误码。
# 类用途: 表示显式工作目录不存在、不安全或越过用户边界。
class WorkspaceDirectoryError(ValueError):
    error_code = "WORKSPACE_DIR_INVALID"


# LLM: 纯路径归一化不创建目录；只有原CLI初始化链可要求must_exist=False，运行时入口仍须现存目录，根级目录和文件始终拒绝。
# 函数用途: 区分构造前的待创建目录与运行时的现存目录，保留两者共同的路径安全检查。
def normalize_workspace_directory(value: str | Path, *, must_exist: bool = True) -> Path:
    from ..path_access_policy import inheritable_declared_work_roots

    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise WorkspaceDirectoryError("工作目录必须是非空路径")
    try:
        candidate = Path(value).expanduser()
        path = candidate.resolve(strict=must_exist)
        if not must_exist:
            # 即使叶尚不存在，已有祖先也必须真是目录；不能把文件子路径或悬空链接当待创建目录。
            ancestor = next((item for item in (candidate, *candidate.parents)
                if item.exists() or item.is_symlink()), None)
            if ancestor is None or not ancestor.resolve(strict=True).is_dir():
                raise WorkspaceDirectoryError("工作目录的现存祖先必须是目录")
        if ((must_exist or path.exists()) and not path.is_dir()) or not inheritable_declared_work_roots([str(path)]):
            raise WorkspaceDirectoryError("工作目录必须是现存且可继承的具体目录")
        return path
    except (OSError, RuntimeError) as error:
        raise WorkspaceDirectoryError("工作目录不存在或不可访问") from error


# LLM: 运行时目录切换/查询只接受现存目录；构造期不得通过这个默认严格入口提前创建目录或取消存在性检查。
# 函数用途: 保留会话和Gateway对现存工作目录的统一校验入口。
def existing_workspace_directory(value: str | Path) -> Path:
    return normalize_workspace_directory(value)


# LLM: 默认拒数据根祖先/控制面/其他owner/自身状态；initializing只暂缓存在性，allow_home_ancestor仅供已核Full Access构造根，不授子树权限。
# 函数用途: 检查目录边界；区分待创建项目与现存绑定，项目内私有Home仍由工具路径策略逐子树保护。
def validate_workspace_directory(
    workspace_dir: str | Path, *, owner_home: Path, is_admin: bool, home_root: Path | None = None,
    initializing: bool = False, allow_home_ancestor: bool = False,
) -> Path:
    from ..path_access_policy import (
        HOST_STATE_OWNER_DIRS,
        agent_home_root_for_owner,
        inheritable_declared_work_roots,
    )

    path = normalize_workspace_directory(workspace_dir, must_exist=not initializing)
    home = owner_home.expanduser().resolve(strict=False)
    data_root = home_root or agent_home_root_for_owner(home)
    if data_root is not None:
        data_root = data_root.resolve(strict=False)
        if data_root.is_relative_to(path) and not path.is_relative_to(home) and (not allow_home_ancestor or path == data_root):
            raise WorkspaceDirectoryError("工作目录不能包含宿主数据根")
    if path.is_relative_to(home):
        if path != home and any(path.is_relative_to(home.joinpath(*parts))
                for parts in (*HOST_STATE_OWNER_DIRS, ("runs",))):
            raise WorkspaceDirectoryError("宿主运行状态目录不能作为工作目录")
        return path
    if not is_admin:
        raise WorkspaceDirectoryError("普通用户的工作目录必须在自己的家目录内")
    if data_root is not None and path.is_relative_to(data_root):
        raise WorkspaceDirectoryError("不能绑定宿主控制面或其他用户的家目录")
    if not inheritable_declared_work_roots([str(path)], owner_home=home):
        raise WorkspaceDirectoryError("该目录不能作为显式工作区")
    return path


# LLM: 只消费认证owner/is_admin；分叉显式空ID在领域层独立拒绝，原锁/CAS和已知错误映射保持。
# 函数用途: 执行目录查看/切换或主会话分叉，返回原控制结果结构；Gateway路由须显式调用它。
def execute_workspace_control(store, command, *, thread_id: str, owner_home: Path,
                              is_admin: bool = False, expected_revision: int | None = None):
    from .control_commands import ConversationControlResult

    if command.kind not in {"dir", "fork"} or not command.valid:
        return ConversationControlResult(command.kind, False, command.usage or "不支持的工作区命令")
    try:
        thread = store.threads.require(thread_id)
        if thread.owner_home and Path(thread.owner_home).resolve(strict=False) != owner_home.resolve(strict=False):
            raise WorkspaceDirectoryError("会话不属于当前用户的家目录")
        if command.kind == "fork":
            from .thread_fork_service import ConversationForkError, fork_conversation_thread

            explicit = getattr(command, 'message_id_explicit', False)
            if explicit and not command.value:
                raise ConversationForkError('FORK_MESSAGE_NOT_FOUND')
            details = fork_conversation_thread(store, source_thread_id=thread_id,
                up_to_message_id=command.value if explicit or command.value else None, title=command.name or None,
                owner_home=owner_home, is_admin=is_admin)
            return ConversationControlResult("fork", True, "已创建独立分叉会话", details=details)
        if command.value:
            path = validate_workspace_directory(command.value, owner_home=owner_home, is_admin=is_admin)
            thread = store.threads.switch_workspace(thread_id, str(path), expected_revision=expected_revision)
        details = {"thread_id": thread.thread_id, "cwd": thread.cwd,
            "workspace_root": thread.workspace_root or thread.cwd,
            "runtime_workspace_roots": list(thread.runtime_workspace_roots)}
        return ConversationControlResult("dir", True, f"当前工作目录：{thread.workspace_root or thread.cwd or owner_home}",
            details=details)
    except (ValueError, KeyError) as error:
        return ConversationControlResult(command.kind, False, str(error),
            error_code=getattr(error, "error_code", "WORKSPACE_CONTROL_INVALID"))
    except RuntimeError as error:
        if str(error) != "WORKSPACE_REVISION_CONFLICT":
            raise
        return ConversationControlResult(command.kind, False, "目录已变化，请刷新后重试",
            error_code="WORKSPACE_REVISION_CONFLICT")


# LLM: 返回原线程与资产Store的只读投影，错误显式保留；调用方先提供可信owner和管理员标记，绝不创建.agent或锁。
# 函数用途: 查询某个项目的会话、四类合并资产及规则来源，为门户和会话快照提供统一上下文。
def get_workspace_context(*, owner_home: Path, workspace_root: Path, is_admin: bool = False,
                          store=None, builtin_roots=()) -> dict[str, object]:
    from ..assets.workspace_assets import WorkspaceAssetOverlay
    from .store_layout import ConversationStorage
    from .store_threads import ThreadStore

    path = validate_workspace_directory(workspace_root, owner_home=owner_home, is_admin=is_admin)
    overlay = WorkspaceAssetOverlay(path, owner_home=owner_home, builtin_roots=builtin_roots)
    rows = overlay.list_assets()
    assets = {kind: [row for row in rows if row["kind"] == kind[:-1]]
        for kind in ("skills", "scripts", "workflows", "monitors")}
    threads = store.threads if store is not None else ThreadStore(
        ConversationStorage(owner_home / "conversations", initialize=False))
    records, errors = threads.list_report(workspace_dir=str(path), limit=0)
    records.sort(key=lambda thread: thread.updated_at, reverse=True)
    return {"workspace_root": str(path), "assets": assets,
        "asset_counts": {kind: len(rows) for kind, rows in assets.items()},
        "agents": overlay.list_agents(), "threads": [thread.to_dict() for thread in records],
        "active_thread_count": None if errors else sum(thread.status == "active" for thread in records),
        "load_errors": errors}
