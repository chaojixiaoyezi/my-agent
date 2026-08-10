"""R2 执行与发布权威操作（3.txt F/G/H 节落地）。

本 mixin 在 RuntimeRepository 上补充：
- 三层 fence 校验（F.6/F.7）：claim/renew/handler 前/settle/PublishOperation
  五处同时验证 current_attempt_id + current_attempt_generation +
  workspace_epoch + tool_operation_generation；旧 attempt 靠旧行不得获权。
- ToolOperation 状态机（G.1-G.5）：CLAIMED→EXECUTING（CAS 写
  handler_started_at）→SUCCEEDED/FAILED/CANCELLED/UNKNOWN。
- 资源锁（G.6-G.9）：holder instance + PID/start token + attempt generation +
  workspace_epoch + lease；renew 也 CAS；多资源按 canonical scope 排序
  一次取得全部锁，未取得全部时 handler=0。
- 资源 mutation 账（G.12-G.14）：canonical scope + version + 状态；
  unknown/unscoped 写置 DIRTY → 阻止发布/验收/交付。
- PublishOperation（H.2-H.9）：staging→共享唯一通道；preimage CAS、
  原子 replace、崩溃只落 COMMITTED 或 DIRTY/UNKNOWN；完成后才写
  ArtifactRecord。

所有状态转换 fail-closed：CAS rowcount≠1 一律 RuntimeConflictError。
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from ..common.id_generator import new_id
from ..common.opaque_id import OpaqueIdError, validate_opaque_id

# G.1 ToolOperation 六态。
OP_CLAIMED = "CLAIMED"
OP_EXECUTING = "EXECUTING"
OP_SUCCEEDED = "SUCCEEDED"
OP_FAILED = "FAILED"
OP_CANCELLED = "CANCELLED"
OP_UNKNOWN = "UNKNOWN"
_OPERATION_STATES = frozenset(
    {OP_CLAIMED, OP_EXECUTING, OP_SUCCEEDED, OP_FAILED, OP_CANCELLED, OP_UNKNOWN}
)
#: G.5：CANCELLED 只允许能证明 handler 未启动（handler_started_at=0）的操作。
_CANCELLABLE_FROM = (OP_CLAIMED,)

# G.12 mutation 状态。
MUT_MUTATING = "MUTATING"
MUT_STABLE = "STABLE"
MUT_DIRTY = "DIRTY"

# H.7 publish 状态：崩溃只允许 COMMITTED 或 DIRTY/UNKNOWN。
PUB_STAGING = "STAGING"
PUB_COMMITTED = "COMMITTED"
PUB_DIRTY = "DIRTY"
PUB_UNKNOWN = "UNKNOWN"

#: 测试 10：同资源同 preimage 恰一发布，另一争用发布方收到此冲突码。
RESOURCE_VERSION_CONFLICT = "RESOURCE_VERSION_CONFLICT"

# I.9 ValidatorOperation 状态：sandbox 不可用 fail closed 为
# UNAVAILABLE/BLOCKED，不降级为 advisory。
VOP_PENDING = "PENDING"
VOP_RUNNING = "RUNNING"
VOP_VERIFIED = "VERIFIED"
VOP_FAILED = "FAILED"
VOP_UNAVAILABLE = "UNAVAILABLE"
VOP_BLOCKED = "BLOCKED"

# I.6 current_contract_id CAS 分叉冲突码。
CONTRACT_DIVERGED = "CONTRACT_DIVERGED"

# R4（K 节）outbox 状态机（K.3/K.4/K.6）：
# PENDING → IN_FLIGHT（claim 租约）→ ACKED（provider 确认）| FAILED（退避重试）
# FAILED 重试超限 → DEAD_LETTER（可查询、可人工重放，K.6）。
# reconcile（K.3）：provider query 确认实际副作用 —— confirmed 落 ACKED，
# absent/unknown 回 PENDING 退避（at-least-once：宁可重发不可丢）。
OUTBOX_PENDING = "PENDING"
OUTBOX_IN_FLIGHT = "IN_FLIGHT"
OUTBOX_ACKED = "ACKED"
OUTBOX_FAILED = "FAILED"
OUTBOX_DEAD_LETTER = "DEAD_LETTER"

#: K.6：投递重试上限，超限进 dead-letter（完整证据保留，可人工重放）。
MAX_DELIVERY_ATTEMPTS = 8

# inbox 状态（K.4）：重复投递由 effect_key UNIQUE 去重，只处理一次。
INBOX_RECEIVED = "RECEIVED"
INBOX_PROCESSED = "PROCESSED"

# 收口语义（K.5/测试 17）：required acceptance 未全 VERIFIED 不得成功交付。
NOT_VERIFIED = "NOT_VERIFIED"

#: 默认资源锁租期（秒）。lease 过期 ≠ 持有者死亡（G.8），reconcile 前不得
#: 直接把资源交给第二 writer。
DEFAULT_LEASE_SECONDS = 60


class RuntimeConflictError(RuntimeError):
    """权威库内状态冲突（CAS 失败/资源已被占/版本失配）。"""


def sha256_of(path: Path) -> str:
    """文件内容 digest（preimage/postimage/ArtifactRecord 共用）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_manifest_rel_path(rel: str) -> None:
    """H.3：publish manifest 路径必须是共享根下的安全相对路径。

    拒绝：绝对路径、点段（. / ..）、反斜杠、控制字符与空白（lstrip("/")
    只剥前导斜杠，不足以挡 ../ 逃逸）。合法形态是斜杠分隔的相对段，
    每段为非空且不含点段。
    """
    if rel.startswith("/") or "\\" in rel:
        raise RuntimeConflictError(f"manifest 路径非法: {rel!r}")
    segments = rel.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise RuntimeConflictError(f"manifest 路径非法(空段/点段): {rel!r}")
    if any(ord(ch) < 32 for ch in rel):
        raise RuntimeConflictError(f"manifest 路径非法(含控制字符): {rel!r}")


def _assert_publish_path_contained(root: Path, rel: str, label: str) -> None:
    """G3：发布路径执行级文件系统校验（探针 symlink_publish_wrote_outside）。

    stage 的字符串检查（H.3/_validate_manifest_rel_path）挡不住 DB 直改
    与未来新增的写入路径；apply 前对 staging/shared 两侧逐项做第二道
    拒绝式防线：
    - symlink 组件拒绝：路径链上任何组件（含最后组件）是 symlink 即拒。
      内核路径解析沿中间组件的 symlink 出根（os.replace 会把文件写到
      链接指向的外部位置）；且 symlink 是 TOCTOU 面——resolve 校验后
      链接被替换就绕过。最后组件是 symlink 同样拒绝：preimage 校验的
      sha256_of 跟随链接读外部，os.replace 替换的只是链接本身（内容
      寻址断裂）。
    - 根包含：resolve 后必须仍在根内（B.2 拒绝式，防 .. 逃逸与 DB
      直改注入的越界路径）。
    fail-closed：任一检查不过 → 抛 RuntimeConflictError，publish 保持
    STAGING，零副作用。
    """
    candidate = root / rel
    cur: Path | None = candidate
    while cur is not None and cur != root:
        if cur.is_symlink():
            raise RuntimeConflictError(
                f"发布路径 {rel!r} 含 symlink 组件（{label} 侧）: {cur}"
            )
        cur = cur.parent
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeConflictError(
            f"发布路径 {rel!r} 逃逸出 {label}（resolve 后 {resolved}）"
        )


def _sha256_fd(fd: int) -> str:
    """从已打开的 fd 计算内容 digest（G3 补：digest 与后续复制强绑定
    同一 fd，算 digest 与读内容之间无路径重解析窗口）。"""
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _resolve_parent_fd(root_fd: int, rel: str, *, create_dirs: bool) -> tuple[int, str]:
    """G3 补：从根 fd 逐段 openat（O_NOFOLLOW|O_DIRECTORY）到 rel 的父目录。

    返回 (父目录 fd, 末段名)。父 fd 持有后，后续 read/rename/unlink 都
    用 dir_fd 引用同一目录 inode——路径字符串不再被重新解析，CAS 与
    apply 之间即使有人把目录换成 symlink 也不影响（3.txt G3 首选方案）。

    fail-closed：
    - 任一段是 symlink → openat(O_NOFOLLOW) ELOOP → RuntimeConflictError；
    - 空段/点段/".." → 拒绝（防 DB 直改注入逃逸，openat 不跟 ".."）；
    - 父目录不存在且 create_dirs → mkdirat 逐段创建（新建目录不可能是
      symlink，创建后再 open O_NOFOLLOW 仍防并发替换）；
    - 其余 OSError 上抛。
    """
    parts = rel.split("/")
    if not parts or parts[0] == "" or any(seg in ("", ".", "..") for seg in parts):
        raise RuntimeConflictError(f"发布路径非法段: {rel!r}")
    fd = root_fd
    for seg in parts[:-1]:
        try:
            fd = os.open(seg, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError as exc:
            if create_dirs and exc.errno == errno.ENOENT:
                os.mkdir(seg, dir_fd=fd)
                fd = os.open(seg, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                continue
            if exc.errno == errno.ELOOP:
                raise RuntimeConflictError(f"发布路径 {rel!r} 含 symlink 组件: {seg}") from exc
            raise
    name = parts[-1]
    if name in ("", ".", ".."):
        raise RuntimeConflictError(f"发布路径非法末段: {rel!r}")
    return fd, name


def _exists_at(parent_fd: int, name: str, rel: str) -> bool:
    """G3 补：父 fd 内 openat(O_NOFOLLOW) 探测末段是否存在。

    不存在 → False；末段是 symlink → ELOOP → RuntimeConflictError
    （G3：拒绝而非跟随，防 preimage 校验读外部）。
    """
    try:
        probe = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return False
        if exc.errno == errno.ELOOP:
            raise RuntimeConflictError(f"发布路径 {rel!r} 含 symlink 组件: {name}") from exc
        raise
    os.close(probe)
    return True


def _digest_at(parent_fd: int, name: str, rel: str) -> str:
    """G3 补：父 fd 内 openat(O_NOFOLLOW) 打开末段并读 digest。

    ENOENT → RuntimeConflictError（preimage 不存在）；末段 symlink →
    ELOOP → RuntimeConflictError（不跟随）。
    """
    try:
        probe = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise RuntimeConflictError(f"{RESOURCE_VERSION_CONFLICT}: {rel} 不存在/非文件") from exc
        if exc.errno == errno.ELOOP:
            raise RuntimeConflictError(f"发布路径 {rel!r} 含 symlink 组件: {name}") from exc
        raise
    try:
        return _sha256_fd(probe)
    finally:
        os.close(probe)


def _open_staged_file(parent_fd: int, name: str, rel: str) -> int:
    """G3 补：打开 staging 文件（ENOENT → RuntimeConflictError 保持
    「staging 缺失」语义；symlink → ELOOP → 拒绝）。"""
    try:
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise RuntimeConflictError(f"staging 缺失发布文件: {rel}") from exc
        if exc.errno == errno.ELOOP:
            raise RuntimeConflictError(f"发布路径 {rel!r} 含 symlink 组件: {name}") from exc
        raise


def _write_store_from_fd(store_file: Path, src_fd: int) -> None:
    """G2 补+G3 补：内容寻址落盘（同文件系统原子，H.5 同款写法）。

    写 .tmp-{pid}-{uuid} 再 os.replace 到最终名：任何时刻 store 里要么
    无该 digest 文件、要么是完整内容，绝无半写。数据源是已打开的 fd
    （CAS 算 digest 的同一 fd）——复制内容与 digest 强绑定，staging
    路径被并发替换也不影响。失败清理临时文件后上抛 → publish 停在
    STAGING。
    """
    tmp = store_file.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "wb") as out:
            os.lseek(src_fd, 0, os.SEEK_SET)
            while True:
                chunk = os.read(src_fd, 1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp, store_file)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def directory_id_for_opaque(opaque_id: str, *, kind: str) -> str:
    """G1 补（B.3）：opaque ID → 框架目录 ID。

    ID 不直接成为物理路径权威：拼路径前先过 validate_opaque_id（拒绝式，
    非法抛 OpaqueIdError，fail-closed），映射登记在 id_path_mapping。
    directory_id 由框架决定——框架生成的 ID 形态本身即安全路径段
    （subagent-1780127110-469bfd0e），且存量任务目录即按 ID 命名，直接
    复用保证零破坏；目录名轮换时只改本函数 + 映射表迁移，不碰调用方。
    """
    return validate_opaque_id(opaque_id, kind=kind)


class RuntimeOperationsMixin:
    # ------------------------------------------------------------- 三层 fence
    def register_id_path(self, opaque_id: str, *, kind: str) -> str:
        """B.3：登记 opaque ID → 目录 ID 映射（幂等）。

        先拒绝式校验（非法 → OpaqueIdError 上抛，调用方 fail-closed）；
        登记后返回 directory_id 供路径拼接。重复登记幂等，返回首次
        登记的 directory_id。
        """
        directory_id = directory_id_for_opaque(opaque_id, kind=kind)
        now = time.time()
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO id_path_mapping(opaque_id, id_kind,
                                                      directory_id, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (opaque_id, kind, directory_id, now),
            )
            conn.commit()
        return directory_id

    def directory_id_for(self, opaque_id: str, *, kind: str) -> str:
        """B.3：查映射；未登记先登记（路径拼接统一入口）。

        ID 本身不直接当路径权威——查不到映射时先 register（校验+登记），
        返回 DB 里固定下来的 directory_id。
        """
        with self._runtime_connection() as conn:
            row = conn.execute(
                "SELECT directory_id FROM id_path_mapping WHERE opaque_id = ?",
                (opaque_id,),
            ).fetchone()
        if row is not None:
            return str(row["directory_id"])
        return self.register_id_path(opaque_id, kind=kind)

    def current_fence(self, agent_run_id: str) -> dict[str, Any]:
        """当前权威 fence 快照（F.6 校验基准，调用方不得自行拼装）。"""
        with self._runtime_connection() as conn:
            run = conn.execute(
                "SELECT current_attempt_id, current_attempt_generation, workspace_epoch "
                "FROM agent_runs WHERE agent_run_id = ?",
                (agent_run_id,),
            ).fetchone()
        if run is None:
            raise KeyError(f"agent_run 不存在: {agent_run_id}")
        return {
            "agent_run_id": agent_run_id,
            "current_attempt_id": str(run["current_attempt_id"] or ""),
            "current_attempt_generation": int(run["current_attempt_generation"]),
            "workspace_epoch": int(run["workspace_epoch"]),
        }

    def verify_fence(
        self,
        *,
        agent_run_id: str,
        attempt_id: str | None = None,
        workspace_epoch: int | None = None,
        tool_operation_id: str | None = None,
        tool_operation_generation: int | None = None,
    ) -> sqlite3.Row:
        """F.6：claim/renew/handler 前/settle/PublishOperation 五处统一校验。

        校验项：
        - attempt_id 必须是当前 current pointer（F.7：旧 attempt 靠旧行不获权）；
        - attempt 行的 attempt_generation 必须等于 current_attempt_generation；
        - workspace_epoch 必须等于 agent_runs.workspace_epoch（F.1）；
        - tool_operation_id（给定时）对应行必须存在、属于当前 run/attempt、
          代数 ≥ 1；tool_operation_generation（给定时）必须等于该操作行
          自己的 generation —— 调用方拿错代数（旧操作行/凭空代数）即拒绝。

        任一失配 → RuntimeConflictError（fail-closed）。返回 attempt 行供
        调用方继续使用。
        """
        run = self.current_fence(agent_run_id)
        if attempt_id is not None:
            current_id = run["current_attempt_id"]
            if current_id != str(attempt_id):
                raise RuntimeConflictError(
                    f"fence 失败: attempt {attempt_id!r} 已不是 current pointer "
                    f"(当前 {current_id!r})"
                )
            attempt = self.get_attempt(str(attempt_id))
            if attempt is None:
                raise RuntimeConflictError(f"fence 失败: attempt 不存在: {attempt_id}")
            if int(attempt["attempt_generation"]) != run["current_attempt_generation"]:
                raise RuntimeConflictError(
                    f"fence 失败: attempt generation {attempt['attempt_generation']} "
                    f"≠ current {run['current_attempt_generation']}"
                )
        if workspace_epoch is not None and int(workspace_epoch) != run["workspace_epoch"]:
            raise RuntimeConflictError(
                f"fence 失败: workspace_epoch {workspace_epoch} ≠ "
                f"current {run['workspace_epoch']}"
            )
        if tool_operation_id is not None:
            op = self.get_operation(tool_operation_id)
            if op is None:
                raise RuntimeConflictError(
                    f"fence 失败: tool_operation 不存在: {tool_operation_id}"
                )
            if str(op["agent_run_id"]) != str(agent_run_id):
                raise RuntimeConflictError(
                    f"fence 失败: tool_operation 属于其它 run: {op['agent_run_id']}"
                )
            if attempt_id is not None:
                if str(op["attempt_id"]) != str(attempt_id):
                    raise RuntimeConflictError(
                        f"fence 失败: tool_operation 属于其它 attempt: {op['attempt_id']}"
                    )
                if int(op["attempt_generation"]) != run["current_attempt_generation"]:
                    raise RuntimeConflictError(
                        f"fence 失败: tool_operation attempt_generation "
                        f"{op['attempt_generation']} ≠ current "
                        f"{run['current_attempt_generation']}"
                    )
            op_generation = int(op["tool_operation_generation"])
            if op_generation < 1:
                raise RuntimeConflictError(
                    f"fence 失败: 非法 tool_operation_generation {op_generation}"
                )
            if tool_operation_generation is not None and int(
                tool_operation_generation
            ) != op_generation:
                raise RuntimeConflictError(
                    f"fence 失败: tool_operation_generation "
                    f"{tool_operation_generation} ≠ 行自身 {op_generation}"
                )
        elif tool_operation_generation is not None:
            # 只有代数没有操作行 → 无从对照自身代数，拒绝（防凭空代数）。
            raise RuntimeConflictError(
                "fence 失败: tool_operation_generation 必须配 tool_operation_id"
            )
        if attempt_id is None:
            return None
        return self.get_attempt(str(attempt_id))

    # --------------------------------------------------------- ToolOperation
    def create_tool_operation(
        self,
        *,
        agent_run_id: str,
        attempt_id: str,
        operation_type: str,
        canonical_scope: str = "",
    ) -> sqlite3.Row:
        """创建 CLAIMED 操作（A.6：必须先有 current attempt 才能执行工具）。

        tool_operation_generation = 该 attempt 下已建操作数 + 1（F.5 独立代数）。
        """
        self.verify_fence(agent_run_id=agent_run_id, attempt_id=attempt_id)
        now = time.time()
        operation_id = new_id("session_id")  # 操作 ID 复用 session 前缀（框架 ID 族）
        with self._runtime_connection() as conn:
            attempt = conn.execute(
                "SELECT attempt_generation FROM agent_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            generation = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tool_operations WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()[0]
            ) + 1
            conn.execute(
                """
                INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id,
                                             attempt_generation, tool_operation_generation,
                                             operation_type, canonical_scope, status,
                                             handler_started_at, settled_at, outcome_json,
                                             created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, 'CLAIMED', 0, 0, '{}', ?, ?)
                """,
                (
                    operation_id,
                    agent_run_id,
                    attempt_id,
                    int(attempt["attempt_generation"]),
                    generation,
                    operation_type,
                    canonical_scope,
                    now,
                    now,
                ),
            )
            conn.commit()
        row = self.get_operation(operation_id)
        assert row is not None
        return row

    def get_operation(self, operation_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM tool_operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()

    def operations_for_attempt(self, attempt_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_operations WHERE attempt_id = ? "
                "ORDER BY tool_operation_generation",
                (attempt_id,),
            ).fetchall()
        return list(rows)

    def mark_operation_executing(self, operation_id: str) -> sqlite3.Row:
        """G.2：CLAIMED→EXECUTING，CAS 写 handler_started_at（调 handler 前）。"""
        now = time.time()
        with self._runtime_connection() as conn:
            updated = conn.execute(
                """
                UPDATE tool_operations
                SET status = 'EXECUTING', handler_started_at = ?, updated_at = ?
                WHERE operation_id = ? AND status = 'CLAIMED' AND handler_started_at = 0
                """,
                (now, now, operation_id),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"operation 无法进入 EXECUTING: {operation_id} 非 CLAIMED 或已启动"
                )
            conn.commit()
        row = self.get_operation(operation_id)
        assert row is not None
        return row

    def settle_operation(
        self,
        operation_id: str,
        outcome: str,
        details: dict[str, Any] | None = None,
    ) -> sqlite3.Row:
        """G.3/G.5：settle 为 SUCCEEDED/FAILED/CANCELLED。

        - CANCELLED 只允许 handler 未启动（started_at=0）且仍 CLAIMED/未 settle；
          已 EXECUTING 的操作无法证明零副作用 → 必须 UNKNOWN（G.4）。
        - 已 settle 的操作不可再次 settle（状态机单向，fail-closed）。
        - G4：settle 前按操作行自身的 run/attempt/generation 重校验权威
          fence（F.6 基准）——takeover/换代后旧 attempt 的操作不得结算：
          attempt 已不是 current pointer、代数失配 → fail-closed。旧副作用
          无法归因到 current attempt，只能留给 recovery 标 UNKNOWN。
        """
        outcome = str(outcome or "").strip().upper()
        if outcome not in {OP_SUCCEEDED, OP_FAILED, OP_CANCELLED}:
            raise RuntimeConflictError(f"非法 settle 结果: {outcome!r}")
        now = time.time()
        with self._runtime_connection() as conn:
            op = conn.execute(
                "SELECT status, handler_started_at, agent_run_id, attempt_id, "
                "tool_operation_generation FROM tool_operations "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if op is None:
                raise KeyError(f"operation 不存在: {operation_id}")
            if outcome == OP_CANCELLED and int(op["handler_started_at"]) != 0:
                raise RuntimeConflictError(
                    f"operation {operation_id} 已启动 handler(无法证明零副作用),"
                    f"禁止 CANCELLED,只能 UNKNOWN"
                )
        # G4：settle 前重校验（用操作行自带的权威字段；fence 失配即拒绝结算）。
        self.verify_fence(
            agent_run_id=str(op["agent_run_id"]),
            attempt_id=str(op["attempt_id"]),
            tool_operation_id=operation_id,
            tool_operation_generation=int(op["tool_operation_generation"]),
        )
        with self._runtime_connection() as conn:
            updated = conn.execute(
                """
                UPDATE tool_operations
                SET status = ?, settled_at = ?, outcome_json = ?, updated_at = ?
                WHERE operation_id = ? AND settled_at = 0
                """,
                (
                    outcome,
                    now,
                    json.dumps(details or {}, ensure_ascii=False),
                    now,
                    operation_id,
                ),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(f"operation 已 settle,禁止二次 settle: {operation_id}")
            conn.commit()
        row = self.get_operation(operation_id)
        assert row is not None
        return row

    def mark_operation_unknown(
        self,
        operation_id: str,
        reason: str,
    ) -> sqlite3.Row:
        """G.4：EXECUTING owner 消失/timeout/无法证明零副作用 → UNKNOWN。"""
        now = time.time()
        with self._runtime_connection() as conn:
            updated = conn.execute(
                """
                UPDATE tool_operations
                SET status = 'UNKNOWN', outcome_json = ?, updated_at = ?
                WHERE operation_id = ? AND settled_at = 0
                """,
                (json.dumps({"reason": reason}, ensure_ascii=False), now, operation_id),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"operation 已 settled,禁止标记 UNKNOWN: {operation_id}"
                )
            conn.commit()
        row = self.get_operation(operation_id)
        assert row is not None
        return row

    def can_reopen_operation(self, operation_id: str) -> bool:
        """G.3：仅 CLAIMED 且 handler 未启动时可安全 reopen。"""
        op = self.get_operation(operation_id)
        return (
            op is not None
            and str(op["status"]) == OP_CLAIMED
            and int(op["handler_started_at"]) == 0
        )

    # ------------------------------------------------------------ 资源锁
    def acquire_locks(
        self,
        scopes: list[str],
        *,
        holder_instance: str,
        attempt_id: str,
        attempt_generation: int,
        workspace_epoch: int,
        pid: int = 0,
        start_token: str = "",
        tool_operation_generation: int = 0,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> list[sqlite3.Row]:
        """G.9：canonical scope 排序，一次取得全部锁；任一被占 → 全不取。

        单事务内逐条 INSERT，UNIQUE(canonical_scope) 冲突 → 事务回滚，本次
        未取得任何锁（handler=0 语义由调用方执行）。
        """
        ordered = sorted({str(s).strip() for s in scopes if str(s).strip()})
        if not ordered:
            return []
        now = time.time()
        lease_until = now + max(1, int(lease_seconds))
        try:
            with self._runtime_connection() as conn:
                for scope in ordered:
                    conn.execute(
                        """
                        INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance,
                                                   pid, start_token, attempt_id,
                                                   attempt_generation, workspace_epoch,
                                                   tool_operation_generation, lease_expires_at,
                                                   created_at, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            scope,
                            holder_instance,
                            int(pid),
                            start_token,
                            attempt_id,
                            int(attempt_generation),
                            int(workspace_epoch),
                            int(tool_operation_generation),
                            lease_until,
                            now,
                            now,
                        ),
                    )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise RuntimeConflictError(
                f"资源锁冲突: 未取得全部锁（{', '.join(ordered)}）: {exc}"
            ) from exc
        return [self.lock_for_scope(scope) for scope in ordered]

    def lock_for_scope(self, canonical_scope: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM resource_locks WHERE canonical_scope = ?",
                (canonical_scope,),
            ).fetchone()

    def renew_lock(
        self,
        *,
        canonical_scope: str,
        holder_instance: str,
        attempt_id: str,
        attempt_generation: int,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> sqlite3.Row:
        """G.7：renew 必须 CAS；holder 不符 / attempt 不符 / 已失去 current
        pointer → 拒绝。"""
        now = time.time()
        lease_until = now + max(1, int(lease_seconds))
        with self._runtime_connection() as conn:
            run = conn.execute(
                "SELECT current_attempt_id, current_attempt_generation FROM agent_runs "
                "WHERE current_attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if run is None or int(run["current_attempt_generation"]) != int(attempt_generation):
                raise RuntimeConflictError(
                    f"renew 拒绝: attempt {attempt_id} 已失去 current pointer"
                )
            updated = conn.execute(
                """
                UPDATE resource_locks
                SET lease_expires_at = ?, updated_at = ?
                WHERE canonical_scope = ? AND holder_instance = ?
                  AND attempt_id = ? AND attempt_generation = ?
                """,
                (lease_until, now, canonical_scope, holder_instance,
                 attempt_id, int(attempt_generation)),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"renew CAS 失败: scope={canonical_scope} holder={holder_instance!r}"
                )
            conn.commit()
        row = self.lock_for_scope(canonical_scope)
        assert row is not None
        return row

    def release_locks(
        self,
        scopes: list[str],
        *,
        holder_instance: str,
    ) -> int:
        """释放本 holder 的资源锁（幂等：无锁也返回 0）。"""
        ordered = sorted({str(s).strip() for s in scopes if str(s).strip()})
        if not ordered:
            return 0
        with self._runtime_connection() as conn:
            released = 0
            for scope in ordered:
                released += conn.execute(
                    "DELETE FROM resource_locks WHERE canonical_scope = ? AND holder_instance = ?",
                    (scope, holder_instance),
                ).rowcount
            conn.commit()
        return released

    def expired_locks(self, now: float | None = None) -> list[sqlite3.Row]:
        """G.8：lease 过期的锁。过期 ≠ 持有者死亡——第二 writer 必须先在
        进程层终止/对账旧持有者，否则标 UNKNOWN/dirty。"""
        now = time.time() if now is None else now
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM resource_locks WHERE lease_expires_at < ?",
                (now,),
            ).fetchall()
        return list(rows)

    # --------------------------------------------------------- 资源 mutation
    def begin_mutation(self, *, canonical_scope: str, attempt_id: str) -> sqlite3.Row:
        """STABLE→MUTATING（CAS）。对应 G.12：writer 动身前先声明。"""
        now = time.time()
        with self._runtime_connection() as conn:
            existing = conn.execute(
                "SELECT mutation_id, state FROM resource_mutations "
                "WHERE canonical_scope = ?",
                (canonical_scope,),
            ).fetchone()
            if existing is None:
                mutation_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO resource_mutations(mutation_id, canonical_scope, version,
                                                   state, dirty_reason, attempt_id, updated_at)
                    VALUES(?, ?, 0, 'MUTATING', '', ?, ?)
                    """,
                    (mutation_id, canonical_scope, attempt_id, now),
                )
            else:
                mutation_id = str(existing["mutation_id"])
                updated = conn.execute(
                    """
                    UPDATE resource_mutations
                    SET state = 'MUTATING', attempt_id = ?, updated_at = ?
                    WHERE mutation_id = ? AND state = 'STABLE'
                    """,
                    (attempt_id, now, mutation_id),
                ).rowcount
                if updated != 1:
                    raise RuntimeConflictError(
                        f"begin_mutation 失败: {canonical_scope} 非 STABLE 状态"
                    )
            conn.commit()
        row = self.mutation_for_scope(canonical_scope)
        assert row is not None
        return row

    def mark_mutation_stable(self, canonical_scope: str) -> sqlite3.Row:
        """MUTATING→STABLE（CAS）。"""
        now = time.time()
        with self._runtime_connection() as conn:
            updated = conn.execute(
                """
                UPDATE resource_mutations
                SET state = 'STABLE', updated_at = ?
                WHERE canonical_scope = ? AND state = 'MUTATING'
                """,
                (now, canonical_scope),
            ).rowcount
            if updated != 1:
                raise RuntimeConflictError(
                    f"mark_mutation_stable 失败: {canonical_scope} 非 MUTATING"
                )
            conn.commit()
        row = self.mutation_for_scope(canonical_scope)
        assert row is not None
        return row

    def mark_mutation_dirty(
        self,
        *,
        canonical_scope: str,
        reason: str,
        attempt_id: str = "",
    ) -> sqlite3.Row:
        """G.14：unknown/unscoped 写置 DIRTY；reconcile 前阻止发布/验收/交付。"""
        now = time.time()
        with self._runtime_connection() as conn:
            existing = conn.execute(
                "SELECT mutation_id FROM resource_mutations WHERE canonical_scope = ?",
                (canonical_scope,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO resource_mutations(mutation_id, canonical_scope, version,
                                                   state, dirty_reason, attempt_id, updated_at)
                    VALUES(?, ?, 0, 'DIRTY', ?, ?, ?)
                    """,
                    (uuid.uuid4().hex, canonical_scope, reason, attempt_id, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE resource_mutations
                    SET state = 'DIRTY', dirty_reason = ?, attempt_id = ?, updated_at = ?
                    WHERE mutation_id = ?
                    """,
                    (reason, attempt_id, now, str(existing["mutation_id"])),
                )
            conn.commit()
        row = self.mutation_for_scope(canonical_scope)
        assert row is not None
        return row

    def mutation_for_scope(self, canonical_scope: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM resource_mutations WHERE canonical_scope = ?",
                (canonical_scope,),
            ).fetchone()

    def workspace_dirty(self) -> list[sqlite3.Row]:
        """任何 DIRTY mutation → 阻止发布/验收/交付（G.14）。"""
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM resource_mutations WHERE state = 'DIRTY'"
            ).fetchall()
        return list(rows)

    # --------------------------------------------------- PublishOperation
    def create_publish(
        self,
        *,
        binding_id: str,
        agent_run_id: str,
        attempt_id: str,
        workspace_epoch: int,
    ) -> sqlite3.Row:
        """H.2：publish 的唯一入口——先立 STAGING 记录，再写 manifest。"""
        self.verify_fence(
            agent_run_id=agent_run_id, attempt_id=attempt_id, workspace_epoch=workspace_epoch
        )
        publish_id = new_id("delegation_id")  # publish 复用 delegation 前缀（框架 ID 族）
        now = time.time()
        with self._runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO publish_operations(publish_id, binding_id, agent_run_id,
                                               attempt_id, workspace_epoch, status,
                                               manifest_json, preimage_digests_json,
                                               postimage_digests_json, committed_at,
                                               created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, 'STAGING', '[]', '{}', '{}', 0, ?, ?)
                """,
                (publish_id, binding_id, agent_run_id, attempt_id,
                 int(workspace_epoch), now, now),
            )
            conn.commit()
        row = self.get_publish(publish_id)
        assert row is not None
        return row

    def get_publish(self, publish_id: str) -> sqlite3.Row | None:
        with self._runtime_connection() as conn:
            return conn.execute(
                "SELECT * FROM publish_operations WHERE publish_id = ?", (publish_id,)
            ).fetchone()

    def publishes_for_attempt(self, attempt_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM publish_operations WHERE attempt_id = ? ORDER BY created_at",
                (attempt_id,),
            ).fetchall()
        return list(rows)

    def stage_publish_manifest(
        self,
        publish_id: str,
        manifest: list[dict[str, Any]],
    ) -> sqlite3.Row:
        """H.3：publish_manifest 记录路径/kind/preimage/postimage/权限。

        B.2/H.3：manifest 路径是共享根下的相对路径——拒绝绝对路径、
        点段（. / ..）、反斜杠与控制字符，防止发布路径逃逸出共享根。
        """
        normalized: list[dict[str, Any]] = []
        for item in manifest:
            raw = str(item.get("path") or "").strip()
            if not raw:
                raise RuntimeConflictError("manifest 项缺少 path")
            # 校验必须在剥前导斜杠之前：lstrip("/") 会把绝对路径伪装成相对路径。
            _validate_manifest_rel_path(raw)
            rel = raw.lstrip("/")
            kind = str(item.get("kind") or "updated").strip()
            if kind not in {"created", "updated", "deleted"}:
                raise RuntimeConflictError(f"manifest 项非法 kind: {kind!r}")
            normalized.append(
                {
                    "path": rel,
                    "kind": kind,
                    "preimage_digest": str(item.get("preimage_digest") or "").strip(),
                    "postimage_digest": str(item.get("postimage_digest") or "").strip(),
                    "permissions": int(item.get("permissions") or 0),
                }
            )
        with self._runtime_connection() as conn:
            conn.execute(
                """
                UPDATE publish_operations
                SET manifest_json = ?, updated_at = ?
                WHERE publish_id = ? AND status = 'STAGING'
                """,
                (json.dumps(normalized, ensure_ascii=False), time.time(), publish_id),
            )
            conn.commit()
        row = self.get_publish(publish_id)
        assert row is not None
        return row

    def publish(
        self,
        *,
        publish_id: str,
        staging_root: str | Path,
        shared_root: str | Path,
        crash_point: str = "",
        artifact_store_root: str | Path | None = None,
    ) -> sqlite3.Row:
        """H.4-H.8：staging→共享唯一通道，preimage CAS + 原子 apply。

        流程（fail-closed）：
        1. 重验三层 fence（F.6 publish 点）+ workspace_dirty 检查（G.14）；
        2. preimage CAS：逐项比对共享现文件 digest 与 manifest 声明的
           preimage（created=必须不存在；updated/deleted=必须存在且匹配），
           任一失配 → RESOURCE_VERSION_CONFLICT（测试 10）；
        3. G2 补（H.8）：内容寻址落盘——apply 前把每个 postimage 复制进
           artifact store（不可变、同 digest 单副本，写时先重算 staging
           实际 digest 与 manifest 声明比对，防「声明 A 实为 B」）；
           store 写入失败 → 停在 STAGING（无副作用，可重试）；
        4. crash_point 注入（测试 13 用）：after_fence / after_preimage /
           mid_apply → 落 DIRTY 返回，不继续；
        5. 逐项原子 apply：fd-relative os.rename（G3 补，同文件系统原子
           H.5，父目录 fd 在 CAS 前已固定，无 TOCTOU）；删除项 unlink；
           文件系统侧无法整树原子时库内 manifest 即 journal（H.6）；
        6. 全部成功后 status=COMMITTED + committed_at（H.7），随后写
           ArtifactRecord（H.8：发布完成才写；主键独立 artifact_record_id，
           content_digest 只是内容 hash，同内容不同路径各一条记录）。

        崩溃（真实进程死/注入）只可能观测到 COMMITTED 或 DIRTY/UNKNOWN。
        artifact_store_root 缺省 = 共享区同级的 .artifacts（owner 级元数据，
        不混入共享区交付内容）。
        """
        publish_row = self.get_publish(publish_id)
        if publish_row is None:
            raise KeyError(f"publish 不存在: {publish_id}")
        if str(publish_row["status"]) not in {PUB_STAGING}:
            raise RuntimeConflictError(
                f"publish {publish_id} 已处于 {publish_row['status']},不可重放"
            )
        manifest: list[dict[str, Any]] = json.loads(publish_row["manifest_json"] or "[]")
        self.verify_fence(
            agent_run_id=str(publish_row["agent_run_id"]),
            attempt_id=str(publish_row["attempt_id"]),
            workspace_epoch=int(publish_row["workspace_epoch"]),
        )
        if crash_point == "after_fence":
            return self._set_publish_state(publish_id, PUB_DIRTY, {"stage": crash_point})
        if self.workspace_dirty():
            raise RuntimeConflictError(
                f"workspace 有 DIRTY mutation,阻止发布: "
                f"{[r['canonical_scope'] for r in self.workspace_dirty()]}"
            )
        staging = Path(staging_root).resolve()
        shared = Path(shared_root).resolve()
        # G3：执行级路径防线——每项（staging/shared 两侧）resolve 根包含 +
        # symlink 组件拒绝（第一层防线，挡 DB 直改注入的越界路径）。随后
        # G3 补 fd-relative 化：从根 fd 逐段 openat/O_NOFOLLOW，检查与使用
        # 共用同一父 fd，路径不再重解析（TOCTOU 面消除）。
        for item in manifest:
            rel = str(item["path"])
            _assert_publish_path_contained(shared, rel, "共享根")
            if str(item["kind"]) != "deleted":
                _assert_publish_path_contained(staging, rel, "staging")
        open_fds: list[int] = []
        try:
            # G3 补：持根 fd（O_NOFOLLOW 防根本身被换 symlink），逐段 openat
            # 预解析每项父目录 fd；父 fd 固定后 CAS/apply 全程不重解析路径。
            shared_fd = os.open(shared, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            staging_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            open_fds += [shared_fd, staging_fd]
            resolved_items: list[tuple[int | None, str, int, str]] = []
            for item in manifest:
                rel = str(item["path"])
                kind = str(item["kind"])
                if kind == "deleted":
                    s_parent: int | None = None
                    s_name = ""
                else:
                    try:
                        s_parent, s_name = _resolve_parent_fd(staging_fd, rel, create_dirs=False)
                    except FileNotFoundError:
                        raise RuntimeConflictError(
                            f"staging 缺失发布文件: {rel}"
                        ) from None
                    open_fds.append(s_parent)
                try:
                    d_parent, d_name = _resolve_parent_fd(
                        shared_fd, rel, create_dirs=(kind != "deleted")
                    )
                except FileNotFoundError:
                    raise RuntimeConflictError(
                        f"{RESOURCE_VERSION_CONFLICT}: {rel} 不存在/非文件"
                    ) from None
                open_fds.append(d_parent)
                resolved_items.append((s_parent, s_name, d_parent, d_name))
            # H.4：preimage CAS（fd-relative：created 探测与 updated/deleted
            # 读 digest 都走已固定的父 fd + openat(O_NOFOLLOW)，无路径重解析）。
            for (s_parent, s_name, d_parent, d_name), item in zip(resolved_items, manifest):
                rel = str(item["path"])
                expected = str(item["preimage_digest"] or "").strip()
                kind = str(item["kind"])
                if kind == "created":
                    if _exists_at(d_parent, d_name, rel):
                        raise RuntimeConflictError(
                            f"{RESOURCE_VERSION_CONFLICT}: {rel} 已存在(预期 created)"
                        )
                else:
                    actual = _digest_at(d_parent, d_name, rel)
                    if expected and actual != expected:
                        raise RuntimeConflictError(
                            f"{RESOURCE_VERSION_CONFLICT}: {rel} preimage digest 失配 "
                            f"(期望 {expected[:12]}…,实际 {actual[:12]}…)"
                        )
            if crash_point == "after_preimage":
                return self._set_publish_state(publish_id, PUB_DIRTY, {"stage": crash_point})
            # G2 补（H.8）：内容寻址落盘——发布即冻结。apply 前完成，失败停在
            # STAGING（无副作用，可重试）。落盘内容以重算 digest 为权威：manifest
            # 的 postimage_digest 是 staging 阶段算的，若 staging 被改则声明失配
            # → fail-closed，绝不让「声明 A 实为 B」进 store/验收。
            # G3 补：digest 计算与 store 复制共用同一 staging fd——「算的
            # 内容」与「复制的内容」强绑定，无复制窗口。
            if artifact_store_root is None:
                artifact_store_root = Path(shared).parent / ".artifacts"
            store_root = Path(artifact_store_root).resolve()
            store_root.mkdir(parents=True, exist_ok=True)
            for (s_parent, s_name, d_parent, d_name), item in zip(resolved_items, manifest):
                if str(item["kind"]) == "deleted":
                    continue
                staged_fd = _open_staged_file(s_parent, s_name, str(item["path"]))
                open_fds.append(staged_fd)
                actual = _sha256_fd(staged_fd)
                declared = str(item.get("postimage_digest") or "").strip()
                if declared and actual != declared:
                    raise RuntimeConflictError(
                        f"postimage digest 失配(staging 被改): {str(item['path'])} "
                        f"(声明 {declared[:12]}…,实际 {actual[:12]}…)"
                    )
                store_file = store_root / actual
                if not store_file.exists():
                    _write_store_from_fd(store_file, staged_fd)
                item["_store_path"] = str(store_file)
                item["_store_digest"] = actual
            # H.6：逐项 apply（库内 manifest 即 journal；中间崩 → 部分项已写，
            # 状态由 _set_publish_state 落 DIRTY，reconcile 用 journal 对账）。
            # G3 补：fd-relative 原子替换——rename/unlink 都指已固定的父 fd
            # 与末段名，目标目录/源目录不再按路径字符串解析。
            applied = 0
            for (s_parent, s_name, d_parent, d_name), item in zip(resolved_items, manifest):
                rel = str(item["path"])
                kind = str(item["kind"])
                if kind == "deleted":
                    try:
                        os.unlink(d_name, dir_fd=d_parent)
                    except FileNotFoundError:
                        pass  # 现代码 unlink(missing_ok=True)
                else:
                    try:
                        # 同文件系统原子（H.5）；父 fd 固定，不重解析路径。
                        os.rename(s_name, d_name, src_dir_fd=s_parent, dst_dir_fd=d_parent)
                    except OSError as exc:
                        if exc.errno == errno.ENOENT:
                            raise RuntimeConflictError(
                                f"staging 缺失发布文件: {rel}"
                            ) from exc
                        raise
                    if int(item.get("permissions") or 0):
                        tfd = os.open(d_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=d_parent)
                        try:
                            os.fchmod(tfd, int(item["permissions"]))
                        finally:
                            os.close(tfd)
                applied += 1
                if crash_point == "mid_apply" and applied == 1:
                    return self._set_publish_state(publish_id, PUB_DIRTY, {"stage": crash_point})
        except RuntimeConflictError:
            raise
        except OSError as exc:
            # 文件系统侧失败：无法证明零副作用 → DIRTY（H.7）。
            return self._set_publish_state(
                publish_id, PUB_DIRTY, {"stage": "apply", "error": str(exc)}
            )
        finally:
            for fd in open_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
        # H.7/H.8：全量成功 → COMMITTED + ArtifactRecord。
        with self._runtime_connection() as conn:
            conn.execute(
                """
                UPDATE publish_operations
                SET status = 'COMMITTED', committed_at = ?, updated_at = ?
                WHERE publish_id = ?
                """,
                (time.time(), time.time(), publish_id),
            )
            for item in manifest:
                if str(item["kind"]) == "deleted":
                    continue
                store_path = str(item.get("_store_path") or "")
                content_digest = str(item.get("_store_digest") or "")
                if not store_path or not content_digest:
                    raise RuntimeConflictError(
                        f"artifact 未落盘（发布流程缺陷）: {str(item['path'])}"
                    )
                conn.execute(
                    """
                    INSERT INTO artifact_records(artifact_record_id, attempt_id,
                                                 agent_run_id, publish_id, rel_path,
                                                 content_digest, size, content_path,
                                                 created_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("artifact_record_id"),
                        str(publish_row["attempt_id"]),
                        str(publish_row["agent_run_id"]),
                        publish_id,
                        str(item["path"]),
                        content_digest,
                        Path(store_path).stat().st_size,
                        store_path,
                        time.time(),
                    ),
                )
            conn.commit()
        self.append_event(
            event_type="publish.committed",
            attempt_id=str(publish_row["attempt_id"]),
            agent_run_id=str(publish_row["agent_run_id"]),
            payload={"publish_id": publish_id, "items": len(manifest)},
        )
        row = self.get_publish(publish_id)
        assert row is not None
        return row

    def _set_publish_state(
        self,
        publish_id: str,
        state: str,
        details: dict[str, Any],
    ) -> sqlite3.Row:
        with self._runtime_connection() as conn:
            conn.execute(
                """
                UPDATE publish_operations
                SET status = ?, updated_at = ?
                WHERE publish_id = ?
                """,
                (state, time.time(), publish_id),
            )
            conn.commit()
        row = self.get_publish(publish_id)
        assert row is not None
        # A.8：崩溃/失败也进权威事件流（append-only，追到 attempt）。
        self.append_event(
            event_type=f"publish.{state.lower()}",
            attempt_id=str(row["attempt_id"]),
            agent_run_id=str(row["agent_run_id"]),
            payload={"publish_id": publish_id, **details},
        )
        return row

    def artifacts_for_attempt(self, attempt_id: str) -> list[sqlite3.Row]:
        with self._runtime_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM artifact_records WHERE attempt_id = ? ORDER BY created_at",
                (attempt_id,),
            ).fetchall()
        return list(rows)
