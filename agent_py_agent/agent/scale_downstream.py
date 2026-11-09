# LLM: 规模入口复用 Gateway 的持久请求、认领、精确回合与终态主链；owner 快照先提交再投递，不另造执行旁路。
# 模块用途: 在租户和用户隔离的工作副本中处理消息，保存会话后回复原消息；异常交回现有队列。

from __future__ import annotations

import atexit
import copy
import hashlib
import json
import os
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.protocol import feishu_conversation_id
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.io import (
    gateway_response_path,
    read_json_file,
    write_gateway_request_once,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_request_path
from agent_py_agent.agent.observability.tracing import TraceContext
from agent_py_agent.agent.owner_object_store import OWNER_SNAPSHOT_MODES, owner_store_from_runtime
from agent_py_agent.agent.scale_runtime import ScaleRole, ScaleRuntimeConfig
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.services.runtime_config_env import (
    apply_runtime_config_environment,
)
from agent_py_agent.agent.storage_backend import StorageBackend
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


@dataclass(frozen=True)
class ScaleMessage:
    tenant: str
    owner_kind: str
    owner_id: str
    prompt: str
    message_id: str
    # 新字段放末尾并给默认值，保持原五个位置参数的源码兼容；真实运行仍会 fail-closed 校验。
    conversation_id: str = ""
    channel_user_id: str = ""


@dataclass(frozen=True)
class _ExecutionPaths:
    home_root: Path
    workspace: Path
    owner_home: Path


def _extract_message(payload: dict) -> ScaleMessage:
    tenant = str(payload.get("tenant") or "").strip()
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    open_id = str(sender_id.get("open_id") or sender_id.get("user_id") or "").strip()
    chat_id = str(message.get("chat_id") or "").strip()
    chat_type = str(message.get("chat_type") or "").strip().lower()
    owner_kind = "user" if chat_type in {"p2p", "private"} else "group"
    owner_id = open_id if owner_kind == "user" else chat_id
    raw_content = message.get("content")
    prompt = _message_text(raw_content)
    message_id = str(message.get("message_id") or "").strip()
    conversation_id = feishu_conversation_id(message)
    if not tenant or not owner_id or not open_id or not prompt or not message_id or not conversation_id:
        raise ValueError(
            "规模飞书消息缺 tenant/owner/channel_user/prompt/message_id/conversation_id，拒绝进入 Agent"
        )
    return ScaleMessage(
        tenant,
        owner_kind,
        owner_id,
        prompt,
        message_id,
        conversation_id,
        open_id,
    )


def _message_text(raw: object) -> str:
    if isinstance(raw, dict):
        return str(raw.get("text") or "").strip()
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return text
    return str(decoded.get("text") or "").strip() if isinstance(decoded, dict) else text


# LLM: 锁保护单个 owner 执行；pins 在池锁下计数，覆盖运行、等待和淘汰阶段。
# 类用途: 保存 owner 的唯一进程内锁和占用计数。
class _AgentSlot:
    # LLM: 新 slot 尚无人持有，调用方须在池锁下增加 pin。
    # 函数用途: 创建 owner 串行锁与初始占用计数。
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pins = 0


# LLM: 每个 owner 串行 checkout/commit，Gateway 自己维护每个请求的执行代次；不得共享 owner 的可变 Agent。
# 类用途: 管理规模 worker 中的用户隔离运行及结果投递，任何失败保留给上游重试判断。
class ScaleAgentPool:
    """同进程 owner 锁配合租约、可证明的变更流与温缓存。"""

    _snapshot_mode = "full"
    _lease_ttl_seconds = 90.0

    # LLM: 配置只在组合根读取一次；未知模式保持带 advisory 的默认路径，TTL 复用既有后台执行租约参数。
    # 函数用途: 初始化 owner 池、快照模式和存储依赖。
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: OrderedDict[tuple[str, str, str], _AgentSlot] = OrderedDict()
        self._limit = max(1, int(os.environ.get("SCALE_AGENT_CACHE_SIZE", "32")))
        default_config = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
        config_path = Path(os.environ.get("MY_AGENT_CONFIG") or default_config)
        self._base_config = apply_runtime_config_environment(load_config(config_path))
        mode = str(getattr(self._base_config, "scale_owner_snapshot_mode", "full") or "full").strip().lower()
        self._snapshot_mode = mode if mode in OWNER_SNAPSHOT_MODES else "full"
        self._lease_ttl_seconds = float(getattr(self._base_config, "background_claim_ttl_seconds", 90) or 90)
        self._workspaces = Path(os.environ["MY_AGENT_SCALE_WORKSPACE_ROOT"]).expanduser().resolve()
        runtime = ScaleRuntimeConfig.from_env(ScaleRole.WORKER, os.environ)
        self._store = owner_store_from_runtime(runtime, StorageBackend(runtime.database_url))
        atexit.register(self.close)

    # LLM: slot 从选中到等待和执行结束始终带 pin，异常也在池锁下归还。
    # 函数用途: 串行运行 owner 回合并释放占用。
    def run(self, message: ScaleMessage, trace: TraceContext) -> int:
        slot = self._slot(message)
        try:
            with slot.lock:
                return self._run_locked(message, trace)
        finally:
            with self._lock:
                slot.pins -= 1

    # LLM: 温缓存仅由 slot 生命周期管理，回合只删除 trace workspace；快照提交必须早于外部回复。
    # 函数用途: 在持有 owner 锁时复用温缓存执行消息。
    def _run_locked(self, message: ScaleMessage, trace: TraceContext) -> int:
        execution = self._execution_root(message, trace)
        workspace = execution / "workspace"
        warm = self._warm_dir((message.tenant, message.owner_kind, message.owner_id))
        home_root = warm / "home"
        identity = (
            OwnerIdentity.provider_user("feishu", message.owner_id)
            if message.owner_kind == "user"
            else OwnerIdentity.provider_group("feishu", message.owner_id)
        )
        owner_home = resolve_owner_home(home_root, identity).home_dir
        try:
            paths = _ExecutionPaths(home_root, workspace, owner_home)
            response, tokens = self._execute_and_commit(message, trace, paths)
            if not _adapter().reply_message(message.message_id, response):
                raise RuntimeError("飞书回复失败，队列消息将重试")
            return tokens
        finally:
            shutil.rmtree(execution, ignore_errors=True)

    # LLM: Gateway 的公开结构化响应是唯一投递来源；必须先提交 owner 快照，不能退回原始模型正文。
    # 函数用途: 在用户快照事务内执行一轮并返回回复和用量，空回复不确认队列消息。
    def _execute_and_commit(
        self,
        message: ScaleMessage,
        trace: TraceContext,
        paths: _ExecutionPaths,
    ) -> tuple[str, int]:
        snapshot = self._store.checkout_delta(
            message.tenant, message.owner_kind, message.owner_id, paths.owner_home,
            warm_dir=paths.home_root.parent, lease_ttl_seconds=self._lease_ttl_seconds,
            hold_advisory_lock=self._snapshot_mode != "delta",
        )
        with snapshot:
            agent = self._build_agent(message, paths.home_root, paths.workspace)
            result = self._run_conversation_turn(agent, message, trace)
            response = str(result.get("response") or "").strip()
            if not response:
                raise RuntimeError("Agent 返回空响应，拒绝确认队列消息")
            tokens = max(0, int(result.get("turn_token_estimate") or result.get("prompt_token_estimate") or 0))
            return response, tokens

    # LLM: 普通和规模入口必须走相同持久队列认领；相同 trace 重放不得覆盖请求或重复执行已完成的工具。
    # 函数用途: 将一条结构化通道消息交给现有 Gateway worker；不启动额外 Gateway，不绕过 active turn 校验。
    def _run_conversation_turn(
        self,
        agent: SimpleAgent,
        message: ScaleMessage,
        trace: TraceContext,
    ) -> dict:
        conversation_id = str(message.conversation_id or "").strip()
        channel_user_id = str(message.channel_user_id or "").strip()
        if not conversation_id or not channel_user_id:
            raise ValueError("规模飞书会话缺 conversation_id/channel_user_id，拒绝运行")
        is_group = message.owner_kind == "group"
        from .session_lock.admission import admission_from_config

        admission = admission_from_config(getattr(self, '_base_config', agent.config),
            channel_user_id, is_group=is_group)
        if not admission.allowed:
            raise RuntimeError(admission.reason_code)
        canonical_user_id = message.owner_id if is_group else channel_user_id
        binding_user_id = message.owner_id if is_group else channel_user_id
        request = {
            "id": trace.trace_id,
            "kind": "ask",
            "user_id": channel_user_id,
            "prompt": message.prompt,
            "save": True,
            "source": "scale_feishu",
            "status": "pending",
            "metadata": {
                "channel": "feishu",
                "message_id": message.message_id,
                "tenant": message.tenant,
                "trace_id": trace.trace_id,
                "channel_conversation_id": conversation_id,
                "channel_user_id": channel_user_id,
                "channel_chat_type": "group" if is_group else "p2p",
                "channel_chat_id": message.owner_id if is_group else conversation_id,
            },
            "conversation": {
                "channel": "feishu",
                "channel_conversation_id": conversation_id,
                "channel_user_id": binding_user_id,
                # 群聊 transcript 归群主体共享，实际操作者仍保留在 channel_user_id；
                # 不能让首位发言人成为 owner，也不能把同群成员拆成互不相干会话。
                "canonical_user_id": canonical_user_id,
                "lane": "chat",
            },
        }
        request["metadata"]["client_input_digest"] = hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        queue_paths = gateway_paths(agent)
        ensure_gateway_folders(queue_paths)
        request_path, _created = write_gateway_request_once(queue_paths, request)
        if request_path.parent == queue_paths.inbox:
            _process_gateway_request_path(agent, queue_paths, request_path, f"scale-{trace.trace_id}")
        response = read_json_file(gateway_response_path(queue_paths, trace.trace_id))
        if not response.get("ok"):
            code = str(response.get("error_code") or "GATEWAY_RESPONSE_UNAVAILABLE")
            raise RuntimeError(f"规模 Gateway 请求未成功：{code}")
        return response

    # LLM: 池锁只修改表和 pins；淘汰在独立 slot 锁内做 IO，重新到达的同 owner 仍复用原 slot。
    # 函数用途: 取得已 pin 的 owner slot 并清理可淘汰的最旧温缓存。
    def _slot(self, message: ScaleMessage) -> _AgentSlot:
        key = (message.tenant, message.owner_kind, message.owner_id)
        evictions: list[tuple[tuple[str, str, str], _AgentSlot]] = []
        with self._lock:
            slot = self._slots.pop(key, None)
            if slot is None:
                slot = _AgentSlot()
            self._slots[key] = slot
            slot.pins += 1
            while len(self._slots) - len(evictions) > self._limit:
                candidate = next(((ref, entry) for ref, entry in self._slots.items()
                                  if entry.pins == 0 and not entry.lock.locked()), None)
                if candidate is None:
                    break
                ref, entry = candidate
                entry.pins += 1
                evictions.append((ref, entry))
        for ref, evicted in evictions:
            with evicted.lock:
                try:
                    warm = self._warm_dir(ref)
                    release = getattr(self._store, 'release_warm', None)
                    if callable(release):
                        release(warm)
                    shutil.rmtree(warm, ignore_errors=True)
                finally:
                    with self._lock:
                        evicted.pins -= 1
                        if evicted.pins == 0 and self._slots.get(ref) is evicted:
                            del self._slots[ref]
        return slot

    def start(self):
        self._store.start()

    def close(self):
        close = getattr(self._store, 'close', None)
        if callable(close):
            close()

    # LLM: 缓存根由租户和 owner 三元组摘要决定，不由消息正文或 trace 决定。
    # 函数用途: 返回 owner 独立的温缓存目录。
    def _warm_dir(self, key: tuple[str, str, str]) -> Path:
        digest = hashlib.sha256("\x1f".join(key).encode()).hexdigest()
        return self._workspaces / ".owner-warm" / digest

    def _build_agent(self, message: ScaleMessage, home_root: Path, workspace: Path) -> SimpleAgent:
        config = copy.deepcopy(self._base_config)
        config.my_agent_home = str(home_root)
        config.my_agent_owner_provider = "feishu"
        config.my_agent_owner_kind = message.owner_kind
        config.my_agent_owner_id = message.owner_id
        workspace.mkdir(parents=True, exist_ok=True)
        config.workspace_root = str(workspace)
        return SimpleAgent(config, workspace, workspace_roots=[workspace])

    def _execution_root(self, message: ScaleMessage, trace: TraceContext) -> Path:
        digest = hashlib.sha256(
            f"{message.tenant}\x1f{message.owner_kind}\x1f{message.owner_id}\x1f{trace.trace_id}".encode()
        ).hexdigest()
        root = self._workspaces / ".owner-executions" / digest
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=False)
        return root


_POOL: ScaleAgentPool | None = None
_POOL_LOCK = threading.Lock()
_ADAPTER: FeishuAdapter | None = None
_ADAPTER_LOCK = threading.Lock()


def _pool() -> ScaleAgentPool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = ScaleAgentPool()
        return _POOL


def _adapter() -> FeishuAdapter:
    global _ADAPTER
    with _ADAPTER_LOCK:
        if _ADAPTER is None:
            _ADAPTER = FeishuAdapter(
                {
                    "feishu_app_id": os.environ["FEISHU_APP_ID"],
                    "feishu_app_secret": os.environ["FEISHU_APP_SECRET"],
                }
            )
        return _ADAPTER


def run(payload: dict, trace: TraceContext) -> int:
    return _pool().run(_extract_message(payload), trace)


def start_pool():
    _pool().start()


def close_pool():
    global _POOL
    with _POOL_LOCK:
        pool = _POOL
        _POOL = None
    if pool is not None:
        pool.close()


# handler 的显式生命周期能力；worker 只读取函数属性，不解析 WORKER_HANDLER 文本作控制。
run.startup = start_pool
run.close = close_pool


__all__ = ["ScaleAgentPool", "ScaleMessage", "run"]
