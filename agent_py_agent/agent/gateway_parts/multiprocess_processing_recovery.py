# LLM: 正MP stale恢复只消费当次原P2停止/P3补章/currentC与同owner RuntimePolicy；此source只准元数据，不授P5/模型/工具，Count0无新查询，修改同步CAS/ports/终态回归。
# 模块用途: 把原processing恢复动作封闭在真实旧域停止后的同步短提交窗口；冷C只投影原配置与路径，未知保留原占用和原文件。
from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from ..common.executor_registration import managed_request_execution_required
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
    request_execution_ports_scope,
)
from ..conversation.store import ConversationStore
from ..runtime_db.repository import RuntimeRepository
from ..runtime_db.schema import runtime_db_path
from ..user_space.owner_access import resolve_owner_scope_and_access
from ..user_space.owner_resolver import OwnerIdentity, home_paths_with_owner, resolve_owner_home
from ..user_space.runtime_paths import runtime_paths_for_agent
from .io import gateway_turn_transition, read_json_file_report
from .multiprocess_admission import (
    PersistentAdmission,
    _runtime_commit_scope,
    session_scope_for_request,
)
from .multiprocess_route import validate_published_route
from .multiprocess_supervisor_policy import RecoveryPolicyReadback, require_resume_policy
from .multiprocess_takeover_runtime import CoordinatorTakeover
from .request_binding import gateway_runtime_authority

_CURRENT = ContextVar("original_processing_recovery_source", default=None)
_WINDOW = ContextVar("original_processing_recovery_window", default=None)


# LLM: 普通Count0同样冻结host owner，不能拿它当MP标记；只认实际角色/gate或route/专属绑定版本拒默认路径，本判断不授权限；正MP冷启动没有实际gate也必须保留，Count0 None不新增读盘或P2观察。
# 函数用途: 判断本宿主是否必须取得真实processing恢复来源。
def processing_recovery_required(context, payload=None):
    control = getattr(context.agent, "_gateway_control_plane", None)
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    return (managed_request_execution_required() or context.admission_gate is not None
            or int(getattr(control, "coordinator_worker_count", 0) or 0) > 0
            or (isinstance(payload, dict) and ("route" in payload or any(
                key in metadata
                for key in ("gateway_admin_binding_version",)))))


# LLM: 拒绝携带原旧claim tuple，不读current代次补历史；没有完整claim就只报缺来源，不伪造typed欠账身份。
# 函数用途: 构造未进入恢复短动作的安全错误。
def _rejection(payload, phase):
    keys = ("id", "execution_attempt_id", "lease_epoch", "lease_owner")
    if all(key in payload for key in keys):
        return RequestExecutionOutcomeUncertainError(payload, phase)
    return RequestExecutionContextUnavailable("original_processing_recovery_source_required")


# LLM: 长P2观察在原controller内且T外；冷C启动时确保绑定原执行端口校验器，callback只有原issued/P3末门可进入，P3已补章后拒仍占用。
# 函数用途: 原链确定stale后，核真实旧域停止和运行库，再消费原预算/CAS动作。
def recover_stale_processing(paths, request_path, payload, context, operation):
    if not processing_recovery_required(context, payload):
        return operation(paths, request_path, payload, context)
    gate = context.admission_gate
    controller = getattr(gate, "_runtime_takeover", None)
    if (type(gate) is not PersistentAdmission or type(controller) is not CoordinatorTakeover
            or gate.paths != paths or gate.agent is not context.agent or not controller.active()):
        raise _rejection(payload, "processing_recovery.source")
    from ..common.request_execution_ports import bind_request_context_rejection
    from .request_transactions import (
        missing_request_context_rejection,
        require_original_request_write_source,
    )

    bind_request_context_rejection(
        missing_request_context_rejection,
        source_validator=require_original_request_write_source,
    )
    ownership = _frozen_ownership(payload, gate)
    committed = []

    # LLM: 这是唯一实际P2/P3同步回调；任何保存的source/cell在finally失效，不跨线程或重进。
    # 函数用途: 在真实T/currentC区间内打开原owner只读政策并执行原恢复动作。
    def commit(decision, authority):
        readback = read_json_file_report(request_path, context="gateway.processing_recovery.restamp.readback")
        current = readback.payload
        if readback.load_error is not None or current != decision["request"]:
            raise _rejection(payload, "processing_recovery.publication")
        expected = copy.deepcopy(payload)
        if payload.get("schema_version") != "gateway_terminal_request.v1":
            expected["route"]["c_run_id"] = gate.c_run_id
        if current != expected:
            raise _rejection(payload, "processing_recovery.cas")
        source = OriginalProcessingRecoverySource(gate, controller, authority, current, ownership[0])
        token = _CURRENT.set(source)
        try:
            source.require_original("transaction_scope")
            with request_execution_ports_scope(source), _held_recovery_window(source, paths, request_path.stem):
                result = operation(paths, request_path, current, context)
                committed.append((current, source._terminal_projection, source._failure_projection))
                return result
        finally:
            source._active = False
            _CURRENT.reset(token)

    # LLM: post仅由原issuer在P3/T退出后同步调用；只携带已提交事实，不返回许可或异步closure，旧cell永不复活。
    # 函数用途: 在同原issued区间中逐短门补原InputReceipt/投影，保原锁顺序。
    def finish(result, authority):
        if not committed or committed[0][1] is None:
            return result
        current, terminal, failure = committed[0]
        source = OriginalProcessingRecoverySource(gate, controller, authority, current, ownership[0])
        token = _CURRENT.set(source)
        try:
            with request_execution_ports_scope(source):
                from .recovery import (
                    _complete_gateway_terminal_projection,
                    _project_recovered_terminal_response,
                    _publish_gateway_failure_response,
                )

                turn_id, _store, settlement_ok = terminal
                settled = _complete_gateway_terminal_projection(paths, turn_id, source.conversation_store, settlement_ok)
                with source.short_scope("processing_recovery.terminal_projection"):
                    _project_recovered_terminal_response(paths, turn_id, settled)
                if failure is not None:
                    with source.short_scope("processing_recovery.failure_projection"):
                        _publish_gateway_failure_response(paths, *failure)
                return result
        finally:
            source._active = False
            _CURRENT.reset(token)

    result = controller.recover_processing(gate, request_path, ownership, commit, finish)
    if isinstance(result, dict):
        raise _rejection(payload, "processing_recovery.p2")
    return result


# LLM: 归属仅消费原request/route冻结metadata及原会话投影；旧代字段只供校验定位，不能用它构造当前许可。
# 函数用途: 给实际P2/P3转换提供同一个旧owner与原session。
def _frozen_ownership(payload, gate):
    route = payload.get("route")
    generation = route.get("c_run_id") if isinstance(route, dict) else None
    result = validate_published_route(payload, gate.count, generation)
    if result.get("ok") is not True:
        raise _rejection(payload, "processing_recovery.owner")
    owner = OwnerIdentity(*result["owner"])
    session = session_scope_for_request(payload)
    if session is None:
        raise _rejection(payload, "processing_recovery.session")
    return owner, session


# LLM: 只绑定当次已issued真实对象/T区间；没有Worker/CoordinatorExecution或HostBinding，不许授模型/工具/新请求权限。
# 类用途: 在同一canonical ports cell里运输原恢复元数据短门。
class OriginalProcessingRecoverySource:
    # LLM: 构造只保存原引用和owner纯路径；真实ControlPlane只消费已捕获home_root，active仍由原_CURRENT保证，不能构造/复制授信。
    # 函数用途: 捕获原补章请求与P2签发，冷C不初始化Agent或owner目录，不重新解析环境中的home。
    def __init__(self, gate, controller, authority, payload, owner):
        self._origin, self._pid, self._thread, self._active = self, os.getpid(), threading.get_ident(), True
        self.gate, self.controller, self.authority = gate, controller, authority
        self._gate_origin, self._controller_origin, self._authority_origin = gate, controller, authority
        self.payload = copy.deepcopy(payload)
        self._payload_bytes = _bytes(payload)
        self.home = resolve_owner_home(_recovery_base_home(gate.agent).root, owner)
        self._home_origin = self.home
        self._store = None
        self._window = self._terminal_projection = self._failure_projection = None

    # LLM: 原tuple来自捕获请求，不因源过期读新代次或授重试，phase只诊断。
    # 函数用途: 把恢复元数据拒绝报告为原执行责任待核。
    def rejection(self, phase):
        return _rejection(json.loads(self._payload_bytes), phase)

    # LLM: 最后核current指针/origin/PID/thread/活区间及同原issued对象/lease/P2源；Runtime查询缺行/错误不是null。
    # 函数用途: 拒绝构造、复制、fork、跨线程、退出和政策变化后的元数据消费。
    def require_original(self, phase):
        if (type(self) is not OriginalProcessingRecoverySource or self._origin is not self or not self._active
                or _CURRENT.get() is not self or self._pid != os.getpid() or self._thread != threading.get_ident()
                or self.gate is not self._gate_origin or self.controller is not self._controller_origin
                or self.authority is not self._authority_origin or self.home is not self._home_origin
                or _bytes(self.payload) != self._payload_bytes
                or self.gate._runtime_takeover is not self.controller
                or self.controller.authority_rejection(self.gate, self.authority)):
            raise self.rejection(phase)
        try:
            _require_published_recovery_binding(self)
            _require_runtime_policy(self)
            if self._store is not None:
                _require_store_thread(self, self._store)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RuntimeError) as error:
            raise self.rejection(phase) from error

    # LLM: hot复用原Gate持T/window，post真实先T再controller/lease；不持许可等待锁，不以phase授信或返回异步yield许可。
    # 函数用途: 给元数据叶子保同原来源复核与锁顺序。
    @contextmanager
    def short_scope(self, phase):
        if self._holds_window():
            self.require_writer(phase)
            yield
            return
        with _request_metadata_scope(self, self.gate.paths, self.payload["id"]):
            self.require_writer(phase)
            yield

    # LLM: 每个callback本体必须在原issued同期、实际T/lease短窗内；Count0仍由common原默认路径直接消费。
    # 函数用途: 在真实恢复短窗中提交一次同步元数据叶子。
    def short_operation(self, phase, operation):
        with self.short_scope(phase):
            return operation()

    # LLM: inner仅核当次真实来源，不能在已持业务锁后再获取T；本source不存在窗口外写能力。
    # 函数用途: 确认内部持久写仍位于原恢复短门。
    def require_writer(self, phase):
        self.require_original(phase)
        if not self._holds_window():
            raise self.rejection(phase)
        _require_window_receipt(self, phase)

    # LLM: 只认当前producer发布的原窗口/origin/thread/source，phase文本或source.active不能代替真实T/lease区间。
    # 函数用途: 判断内部写是否可复用当前实际持有的短门。
    def _holds_window(self):
        held = self._window
        return (type(held) is _RecoveryWindow and held is _WINDOW.get() and held.source is self
                and held.origin is held and held.active and held.pid == os.getpid()
                and held.thread == threading.get_ident())

    # LLM: 仅锁内第一次canonical提交后登记描述性数据；不返回可执行closure/许可，原pending marker保post欠账。
    # 函数用途: 把必须在T外完成的原输入结算参数交给同一次issuer后段。
    def defer_terminal_projection(self, turn_id, store, settlement_ok):
        self.require_writer("processing_recovery.defer_terminal")
        if self._terminal_projection is not None or turn_id != self.payload["id"] or store is not self.conversation_store:
            raise self.rejection("processing_recovery.defer_terminal")
        self._terminal_projection = (turn_id, store, settlement_ok)

    # LLM: failure只含已封存的原响应/诊断事实；发布顺序仍在真实输入结算之后，不能用于重试业务。
    # 函数用途: 延后原失败投影到同一issuer的T外后段。
    def defer_failure_projection(self, context, response):
        self.require_writer("processing_recovery.defer_failure")
        if self._failure_projection is not None or self._terminal_projection is None:
            raise self.rejection("processing_recovery.defer_failure")
        self._failure_projection = (context, response)

    # LLM: 冷Store仅原owner路径/initializeFalse，要求原thread/owner与Runtime绑定一致；不得C物化远端Agent或None假空账。
    # 函数用途: 为原guidance/claim收口取得同一个实际会话存储。
    @property
    def conversation_store(self):
        self.require_original("processing_recovery.store")
        if self._store is None:
            self._store = _recovery_store(self)
        return self._store


# LLM: 运行库成功查到同一个持久task/run/attempt才可消费其block=None；初次认领运输半态验证零run，缺binding/库/行/外owner都拒，绝不补当前代次。
# 函数用途: 读同冻结owner原RuntimePolicy，保持unknown必须显式处置的原合同。
def _require_runtime_policy(source):
    if (_complete_transport_source(source.payload, source.payload["route"]["owner_sha"])
            and source.payload["queue_transport_source"]["window"] == "claim"):
        db_file = runtime_db_path(source.home.home_dir)
        if not db_file.is_file():
            raise ValueError("processing_runtime_db_unavailable")
        repository = RuntimeRepository(db_file, read_only=True)
        if (repository.get_task("gateway:" + source.payload["id"]) is not None
                or repository.get_agent_run(source.payload["id"]) is not None):
            raise ValueError("processing_runtime_unexpected_claim_binding")
        return
    binding = gateway_runtime_authority(source.payload, source.payload["id"])
    if not binding:
        raise ValueError("processing_runtime_binding_unavailable")
    repository = RuntimeRepository(runtime_db_path(source.home.home_dir), read_only=True)
    run = repository.get_agent_run(binding["agent_run_id"])
    attempt = repository.get_attempt(binding["attempt_id"])
    if (run is None or attempt is None or run["run_id"] != binding["run_id"]
            or run["current_attempt_id"] != binding["attempt_id"]
            or attempt["agent_run_id"] != binding["agent_run_id"]):
        raise ValueError("processing_runtime_identity_mismatch")
    task_run = repository.get_task_run(run["task_run_id"])
    task = repository.get_task(binding["task_id"])
    if (task_run is None or task is None or task_run["task_id"] != binding["task_id"]
            or task["owner_id"] != source.home.owner_id
            or not isinstance(source.payload.get("conversation_claim"), dict)
            or task["thread_id"] != source.payload["conversation_claim"].get("thread_id")):
        raise ValueError("processing_runtime_owner_mismatch")
    block = repository.agent_run_recovery_block_for_run_id(binding["run_id"])
    require_resume_policy(RecoveryPolicyReadback(source._payload_bytes, _bytes(block)))


# LLM: 仅真实ControlPlane可从原home_root投影路径；此纯选择不检查/签发权限，不调用ensure或补动态Agent属性。
# 函数用途: 从原宿主取得规范基础路径，冷C沿已冻结根，业务Agent保持原home_paths。
def _recovery_base_home(agent):
    from ..user_space.home_layout import home_paths
    from .control_plane import GatewayControlPlane

    return home_paths(agent.home_root) if type(agent) is GatewayControlPlane else agent.home_paths


# LLM: 冷C只用typed config/home_root；foreign清原runtime字段克隆，same-owner保显式覆盖；仍核owner墙、原thread/claim与initializeFalse。
# 函数用途: 组装原owner既有会话库，不造Agent、不建目录；实际写入仍由原ports/T与P2/P3末门负责。
def _recovery_store(source):
    from dataclasses import replace

    from ..user_space.owner_resolver import owner_identity_from_config
    from .control_plane import GatewayControlPlane

    agent = source.gate.agent
    base_home = _recovery_base_home(agent)
    scoped_home = home_paths_with_owner(base_home, source.home)
    config = agent.config
    if type(agent) is GatewayControlPlane:
        if source.home.identity != owner_identity_from_config(config):
            fields = runtime_paths_for_agent(config, base_home.owner_home_dir, base_home)
            config = replace(config, **{name: "" for name in fields if hasattr(config, name)})
    elif getattr(getattr(agent, "runtime_path_resolution", None), "paths", None):
        from .request_worker import _config_without_runtime_paths

        config = _config_without_runtime_paths(agent)
    owner_root, _access = resolve_owner_scope_and_access(scoped_home, config)
    if owner_root:
        workspace = source.home.home_dir
    elif type(agent) is GatewayControlPlane:
        workspace = agent.agent_root
        if workspace is None:
            raise source.rejection("processing_recovery.store_path")
    else:
        workspace = getattr(agent, "effective_workspace_root", agent.root)
    resolution = runtime_paths_for_agent(config, workspace, scoped_home)
    root = resolution["conversation_workspace"]
    if not root.is_relative_to(source.home.home_dir) or not root.is_dir():
        raise source.rejection("processing_recovery.store_path")
    store = ConversationStore(root, initialize=False)
    _require_store_thread(source, store)
    return store


# LLM: 只返回当前真正callback来源，捕获引用不能发布_current，缺源调用方仍须正MP拒门。
# 函数用途: 取得原恢复短窗中的metadata来源。
def current_processing_recovery():
    return _CURRENT.get()


# LLM: 原T锁由实际P3 callback取得；其他所有终态调用仍取得原T，不以role/source文本免锁。
# 函数用途: 元数据锁内步骤只复用当次真实持T来源，默认保原非重入T行为。
@contextmanager
def processing_metadata_scope(paths, request_id):
    source = _CURRENT.get()
    if source is None:
        with gateway_turn_transition(paths, request_id):
            yield
        return
    if source._holds_window() and source._window.request_id == request_id and paths == source.gate.paths:
        source.require_writer("processing_recovery.transition")
        yield
        return
    with _request_metadata_scope(source, paths, request_id):
        yield


# LLM: 字节只用于冻结比较/P7输入，不生成许可；JSON null只有真实查库后才能进入政策读回。
# 函数用途: 统一原只读恢复政策的规范JSON字节。
def _bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# LLM: 原producer只发布已持T/lease的同线程窗口，引用复制不改变current/source/origin；receipt身份只读锚定，不是新账。
# 类用途: 标出本段正在消费的原turn或关联InputReceipt短锁。
class _RecoveryWindow:
    # LLM: 构造不取得锁或权限；只能下面真实producer设置Current并激活，退出共享失效。
    # 函数用途: 保存本段原锁对象关系和不可变receipt投影。
    def __init__(self, source, request_id, receipt):
        self.origin, self.source, self.request_id, self.receipt = self, source, request_id, receipt
        self.pid, self.thread, self.active = os.getpid(), threading.get_ident(), True


# LLM: 仅真实Gate callback或下方先T→controller/lease的producer调用；构造window本身不授任何执行权限。
# 函数用途: 为同canonical cell发布一个真实短窗，finally撤销所有继承引用。
@contextmanager
def _held_recovery_window(source, paths, request_id, receipt=None):
    window = _RecoveryWindow(source, request_id, receipt)
    token = _WINDOW.set(window)
    source._window = window
    try:
        yield
    finally:
        window.active = False
        source._window = None
        _WINDOW.reset(token)


# LLM: 等T期间绝不持controller/lease；最后原owner/P2/Runtime与关联receipt同时核，不能把预检变许可或取另一个request的写权。
# 函数用途: 为恢复后段取得目标T或真实关联输入T，再消费当次元数据写。
@contextmanager
def _request_metadata_scope(source, paths, request_id):
    source.require_original("processing_recovery.short_prepare")
    if paths != source.gate.paths:
        raise source.rejection("processing_recovery.short_path")
    receipt = _associated_receipt(source, request_id)
    from .input_delivery_service import gateway_turn_transition as original_input_turn_transition

    with original_input_turn_transition(paths, request_id):
        with _runtime_commit_scope(source.gate, source.home.identity) as rejection:
            if rejection:
                raise source.rejection("processing_recovery.short_lease")
            source.require_original("processing_recovery.short_commit")
            if _associated_receipt(source, request_id) != receipt:
                raise source.rejection("processing_recovery.receipt_changed")
            with _held_recovery_window(source, paths, request_id, receipt):
                yield


# LLM: 从原v2 receipt/prepared/guidance结构验证同输入、同原owner/peer和目标turn；不存在/缺绑定/另一主体绝不使用新的role/配置补权。
# 函数用途: 只准本turn或其原已绑定输入进入恢复元数据短窗。
def _associated_receipt(source, request_id):
    if request_id == source.payload["id"]:
        return None
    from .input_delivery_service import gateway_input_guidance_binding, read_gateway_input_receipt

    receipt = read_gateway_input_receipt(source.gate.paths, request_id)
    if (receipt is None or receipt.target_turn_id != source.payload["id"]
            or _receipt_peer(receipt.prepared_request, source.home.identity) != _receipt_peer(source.payload, source.home.identity)):
        raise source.rejection("processing_recovery.receipt_owner")
    binding = gateway_input_guidance_binding(receipt, source.conversation_store)
    if (binding is None or binding[1] != source.payload["id"]
            or binding[0].entry.metadata.get("thread_id") != source.payload["conversation_claim"]["thread_id"]
            or (receipt.prepared_request.get("metadata", {}).get("thread_id") is not None
                and receipt.prepared_request["metadata"]["thread_id"] != binding[0].entry.metadata.get("thread_id"))):
        raise source.rejection("processing_recovery.receipt_guidance")
    return _receipt_identity(receipt.to_dict())


# LLM: 状态可前进，完整prepared/目标与可选v3运输须同一原身份；v2无字段编码原样，不重算或忽略transport。
# 函数用途: 得到原关联输入不可变身份字节。
def _receipt_identity(row):
    frozen = {key: row[key] for key in ("schema_version", "request_id", "client_input_digest", "client_message_id",
                   "prepared_request", "prepared_payload_digest", "guidance_dedupe_key", "target_turn_id")}
    if "transport" in row:
        frozen["transport"] = row["transport"]
    return _bytes(frozen)


# LLM: inner已持原入口T/业务锁，nofollow原字节直读不反向取得M/file锁；错误保未知，状态推进不改原身份投影。
# 函数用途: 在关联回执实际写入前复核同一原prepared/目标，不授任意跨请求写权限。
def _require_window_receipt(source, phase):
    window = source._window
    if window.receipt is None:
        return
    from ..common.nofollow_fs import read_bytes_beneath

    try:
        raw = read_bytes_beneath(source.gate.paths.root, ("input_receipts", window.request_id + ".json"),
                                 max_bytes=1048576, require_dir_fd=True)
        row = json.loads(raw)
        if _receipt_identity(row) != window.receipt:
            raise ValueError("processing_receipt_identity_changed")
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise source.rejection(phase) from error


# LLM: 原route绑定使用入口metadata raw actor/group而非限流anonymous键；只比原结构化peer，group归属按同原chat，不能从正文或当前角色补身份。
# 函数用途: 比较关联输入与原已冻结请求的同一渠道/用户或群/外部会话。
def _receipt_peer(payload, owner):
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    channel = metadata.get("channel") or conversation.get("channel")
    provider = metadata.get("provider") or channel
    principal = metadata.get("chat_id") if owner.owner_kind == "group" else metadata.get("user_id") or payload.get("user_id")
    if not channel or not provider or not principal:
        raise ValueError("processing_receipt_peer_unavailable")
    return (provider, channel, principal, metadata.get("chat_id"), conversation.get("channel_conversation_id"))


# LLM: P3路由只是可改代运输；原P1四绑定或运输半态来源唯一命中后仍核完整scope全部记录和旧N，不用子集/JSON生成P2或当前执行权。
# 函数用途: 每轮从规范P1找到仍属于同原claim或同原运输窗口的完整旧执行域；缺失、多代或坏源均拒绝。
def original_processing_target(targets, payload, owner_sha):
    from .multiprocess_takeover_ledger import read_ledger_scope

    if _complete_processing_claim(payload):
        window = "execution"
        attempt_id = payload["execution_attempt_id"]
        epoch = payload["lease_epoch"]
        source_route = None
    elif _complete_transport_source(payload, owner_sha):
        transport = payload["queue_transport_source"]
        window = transport["window"]
        attempt_id = transport.get("execution_attempt_id")
        epoch = transport["lease_epoch"]
        source_route = transport["route"]
    else:
        raise ValueError("original_processing_claim_required")
    found = []
    for target in targets:
        if owner_sha not in target.scope["owner_shas"]:
            continue
        if window == "claim":
            role, index = ("C", None) if source_route.get("worker") == "C" else ("W", source_route.get("worker"))
            if (target.scope["c_run_id"] != source_route.get("c_run_id")
                    or target.scope["role"] != role
                    or target.scope["worker_index"] != index):
                continue
            report = read_ledger_scope(target.ledger, target.scope)
            if report["failures"]:
                raise ValueError("original_processing_executor_source_unreadable")
            found.append(target)
            continue
        report = read_ledger_scope(target.ledger, target.scope)
        if report["failures"]:
            raise ValueError("original_processing_executor_source_unreadable")
        if any(row["request_id"] == payload["id"]
               and row["execution_attempt_id"] == attempt_id
               and row["lease_epoch"] == epoch
               and row["owner_sha"] == owner_sha for row in report["records"]):
            found.append(target)
    if len(found) != 1:
        raise ValueError("original_processing_executor_source_ambiguous")
    return found[0]


# LLM: 仅校验认领或重排运输窗口写下的原始来源投影，绝不伪造执行claim或放宽租约/owner匹配。
# 函数用途: 判断processing或inbox中的pending请求是否携带完整可核验的运输来源。
def _complete_transport_source(payload, owner_sha=None):
    if (not isinstance(payload, dict) or payload.get("status") != "pending"
            or "execution_attempt_id" in payload or "lease_owner" in payload):
        return False
    source = payload.get("queue_transport_source")
    route = payload.get("route")
    source_route = source.get("route") if isinstance(source, dict) else None
    if (not isinstance(source, dict) or not isinstance(route, dict) or not isinstance(source_route, dict)
            or source.get("schema_version") != "gateway_queue_transport_source.v1"
            or type(payload.get("id")) is not str or not payload["id"]
            or source.get("request_id") != payload["id"]
            or type(source.get("lease_epoch")) is not int
            or source["lease_epoch"] != int(payload.get("lease_epoch") or 0)
            or {k: v for k, v in source_route.items() if k != "c_run_id"} != {k: v for k, v in route.items() if k != "c_run_id"}
            or type(source_route.get("c_run_id")) is not str or not source_route["c_run_id"]):
        return False
    if owner_sha is not None and (route.get("owner_sha") != owner_sha or source_route.get("owner_sha") != owner_sha):
        return False
    window = source.get("window")
    if window == "claim":
        return (source["lease_epoch"] >= 0 and "runtime_authority" not in payload
                and "conversation_claim" not in payload and "active_turn_recovery" not in payload)
    if window == "requeue":
        marker = payload.get("active_turn_recovery")
        return (type(source.get("execution_attempt_id")) is str and bool(source["execution_attempt_id"])
                and source.get("lease_owner") == source["execution_attempt_id"]
                and source["lease_epoch"] >= 1
                and isinstance(marker, dict)
                and marker.get("dead_execution_attempt_id") == source["execution_attempt_id"])
    return False


# LLM: 此四元组只匹配原P1和持久claim，不能独自许可恢复；字符串/整数类型必须原样，不补字段或清洗别名。
# 函数用途: 确认请求保留完整原执行来源定位字段。
def _complete_processing_claim(payload):
    return (all(type(payload.get(key)) is str and bool(payload[key])
                for key in ("id", "execution_attempt_id", "lease_owner"))
            and payload["lease_owner"] == payload["execution_attempt_id"]
            and type(payload.get("lease_epoch")) is int and payload["lease_epoch"] >= 1)


# LLM: 恢复只在真实controller._issued同期消费原P2；投影旧P3代次仅复用既有共同校验，实际P1唯一claim定位和完整fresh evidence不可省。
# 函数用途: 允许已补章但未结的同原claim再次核旧域，不放宽普通跨代restamp授权门。
def processing_authority_rejection(gate, record, payload, ownership, authority):
    from .multiprocess_admission import _takeover_authority_rejection

    controller = gate._runtime_takeover
    if controller.authority_rejection(gate, authority):
        return "takeover_authority_context_mismatch"
    try:
        issued = controller._issued.get()
        target = original_processing_target(controller.targets, payload, record["owner_sha"])
        if issued[2].scope != target.scope or record["request_id"] != payload["id"]:
            return "original_processing_executor_source_changed"
        expected = dict(record, route=dict(record["route"], c_run_id=target.scope["c_run_id"]))
        return _takeover_authority_rejection(gate, expected, ownership, authority)
    except (OSError, ValueError, TypeError, KeyError):
        return "original_processing_executor_source_unavailable"


# LLM: 每个真实metadata提交末核原P3/claim/运输来源/runtime/thread同源；canonical仅允许原终态投影，不给JSON新执行/模型/工具权。
# 函数用途: 拒绝来源文件、运输半态来源、原占位或已捕获绑定在短门前变化。
def _require_published_recovery_binding(source):
    from .multiprocess_admission import _read_reservation, reservation_file
    from .multiprocess_records import _canonical_terminal

    rid = source.payload["id"]
    paths = source.gate.paths
    record = _read_reservation(reservation_file(paths, rid))
    if not isinstance(record, dict) or record["owner_sha"] != source.payload["route"]["owner_sha"]:
        raise ValueError("original_processing_reservation_changed")
    path = paths.processing / (rid + ".json")
    if not path.exists():
        path = paths.terminal / path.name
    report = read_json_file_report(path, context="gateway.processing_recovery.source.readback")
    actual = report.payload
    keys = ("id", "execution_attempt_id", "lease_epoch", "lease_owner", "runtime_authority", "conversation_claim", "queue_transport_source")
    if report.load_error is not None or any(actual.get(k) != source.payload.get(k) for k in keys):
        raise ValueError("original_processing_claim_changed")
    if record["route"] != source.payload["route"] or actual.get("route") != source.payload["route"]:
        raise ValueError("original_processing_route_changed")
    if record["state"] == "released":
        _canonical_terminal(record, actual)
        from .multiprocess_records import release_admission
        release_admission(record, actual)
    elif record["state"] != "occupied":
        raise ValueError("original_processing_reservation_unavailable")


# LLM: 覆盖只定位原旧责任、不签stopped/P2；原claim/运输半态与P3完整对应唯一P1全scope才避免把已补章占位误判无来源，fresh P2仍核全旧域。
# 函数用途: 新C丢失旧内存摘要后，只读辨认同一原占位仍有规范P1来源；坏/缺/多源保持unknown。
def original_claim_covers_reservation(paths, record, targets):
    from ..common.nofollow_fs import read_bytes_beneath
    from .multiprocess_admission import _legacy_scope_matches

    try:
        rid = record["request_id"]
        content = read_bytes_beneath(paths.root, (*paths.processing.relative_to(paths.root).parts, rid + ".json"),
                                     max_bytes=16 * 1024 * 1024, require_dir_fd=True)
        if content is None:
            content = read_bytes_beneath(paths.root, (*paths.terminal.relative_to(paths.root).parts, rid + ".json"),
                                         max_bytes=16 * 1024 * 1024, require_dir_fd=True)
        if content is None:
            inbox_content = read_bytes_beneath(paths.root, (*paths.inbox.relative_to(paths.root).parts, rid + ".json"),
                                               max_bytes=16 * 1024 * 1024, require_dir_fd=True)
            if inbox_content is not None:
                candidate = json.loads(inbox_content)
                if _complete_transport_source(candidate, record.get("owner_sha")):
                    content = inbox_content
        if content is None:
            return False
        payload = json.loads(content)
        route = payload.get("route")
        if payload.get("id") != rid or route != record["route"]:
            return False
        target = original_processing_target(targets, payload, record["owner_sha"])
        validation = validate_published_route(payload, target.scope["worker_count"], route["c_run_id"])
        if validation.get("ok") is not True or _legacy_scope_matches(
                payload, record, session_scope_for_request(payload)) is None:
            return False
        worker = "C" if target.scope["role"] == "C" else target.scope["worker_index"]
        return route["worker"] == worker and route["owner_sha"] == record["owner_sha"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


# LLM: 冷Store可复用原对象但线程归属每次真实metadata末门重读；只核原claim固定线程，不resolve另一个fresh线程或创建文件。
# 函数用途: 防止缓存Store掩盖同一原线程归属/路径在提交前变化。
def _require_store_thread(source, store):
    claim = source.payload.get("conversation_claim")
    if (not isinstance(claim, dict) or claim.get("schema_version") != "gateway_conversation_claim.v1"
            or claim.get("request_id") != source.payload["id"]
            or claim.get("task_id") != "gateway:" + source.payload["id"]):
        raise source.rejection("processing_recovery.conversation_claim")
    thread, error = store.threads.load_report(claim.get("thread_id"))
    if (error is not None or thread is None or thread.owner_id != source.home.owner_id
            or thread.owner_home != str(source.home.home_dir)):
        raise source.rejection("processing_recovery.thread")
