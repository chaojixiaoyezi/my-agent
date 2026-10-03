# LLM: 插件共用通道（M 线第一期 B2）：把「每个（owner, 激活代次）一条 MCP 连接」的连接管理、激活代次复核、
#   单在途、空闲关闭、出错退避和超时统一收在这里，面板服务（plugin_display）和事件中心（plugin_events）共用。
#   只做连接与在途管理，不放展示/事件专属逻辑；停用或换代一律按撤销处理，撤销即丢结果并关连接。
#   修改时同步 docs/design/PLUGIN_EVENT_HOOKS.md（B2）与 agent_py_agent/tests/test_plugin_channel_pool.py。
# 模块用途: 让每个插件激活的连接、请求串行与资源回收只实现一次；插件慢、挂、崩只影响它自己的通道。
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 单个请求（含连接启动）的总超时秒数；沿用面板服务原值，超时按失败处理并回退。
REQUEST_TIMEOUT_SECONDS = 3.0
# 连接闲置多少秒后关闭；沿用面板服务原值，释放闲置插件资源。
IDLE_CLOSE_SECONDS = 120.0
# 请求失败后的重试退避秒数；沿用面板服务原值，避免故障插件被风暴重试。
ERROR_BACKOFF_SECONDS = 5.0
# 明确属于「这条连接本身不可用」的错误码：断连、起不来、超时、启动失败。只有这些（外加 OSError 家族
# 与 PluginChannelTimeout）才让整条连接退避；远端对单个请求回的错（MCP_REMOTE_ERROR）不在其中。
_CONNECTION_FAILURE_CODES = frozenset({
    "MCP_CONNECTION_CLOSED", "MCP_SERVER_START_FAILED", "MCP_TIMEOUT", "PLUGIN_CHANNEL_START_FAILED",
})


# LLM: 通道级错误基类；code 只供上层区分展示文案，不参与机器判定。
# 类用途: 标记一次通道调用失败，调用方按具体子类决定丢结果、退避还是展示错误。
class PluginChannelError(Exception):
    code = "PLUGIN_CHANNEL_ERROR"


# LLM: 超时包含连接启动与请求往返；code 沿用 MCP_TIMEOUT，面板错误文案无需新分支。
# 类用途: 标记本次请求超时（排队、启动或响应超时）。
class PluginChannelTimeout(PluginChannelError):
    code = "MCP_TIMEOUT"


# LLM: 停用、卸载、换代或安装表不可读都按撤销处理；不追随新代次、不重试。
# 类用途: 标记本次请求因激活代次失效而作废，结果必须丢弃。
class PluginChannelRevoked(PluginChannelError):
    code = "PLUGIN_CHANNEL_REVOKED"


# LLM: 退避是连接级状态；退避期内的新请求快速失败，不发起任何 I/O。
# 类用途: 标记该激活正处于失败退避期。
class PluginChannelBackoff(PluginChannelError):
    code = "PLUGIN_CHANNEL_BACKOFF"


# LLM: 连接启动本身失败（进程起不来、握手失败等）说明这条连接不可用，必须退避；
#   把原始异常包一层，是为了让退避判定只看结构化类型，不去猜原始异常的类别。
# 类用途: 标记本次请求因连接启动失败而失败，属于连接级故障。
class PluginChannelStartFailed(PluginChannelError):
    code = "PLUGIN_CHANNEL_START_FAILED"


# LLM: 一次通道请求的不可变描述；owner 用于启动客户端，before_send 在启动完成后、发送前做调用方校验
#   （如握手能力声明），其异常按普通失败处理。timeout 为空时用 REQUEST_TIMEOUT_SECONDS。
# 类用途: 打包一次请求的入参，避免 request 方法参数过多。
@dataclass(frozen=True)
class ChannelCall:
    owner: object
    method: str
    params: dict
    timeout: float | None = None
    before_send: Callable[[object], None] | None = None


# LLM: 一个固定激活只对应一条连接；transport 由后台启动线程写入，请求只在持有 request_lock 时发送。
#   start_error 保存启动线程的失败对象并原样抛给等待者；start_event 只表示「本轮启动结束」。
# 类用途: 保存一条插件通道连接的客户端、传输、启动状态与退避状态。
@dataclass
class ChannelConnection:
    key: tuple[str, str]
    installation: object
    client: object | None = None
    transport: object | None = None
    last_used: float = 0.0
    backoff_until: float = 0.0
    # 创建序号：池内单调递增，用来判断"这条连接是在某次快照之前还是之后建的"。
    # 用序号而不是墙钟，避免时钟回拨或精度问题把先后判断搞错。
    created_seq: int = 0
    starting: bool = False
    start_error: BaseException | None = None
    start_event: threading.Event = field(default_factory=threading.Event)
    request_lock: threading.Lock = field(default_factory=threading.Lock)


# LLM: 连接按 (owner_key, 激活编号) 唯一；状态变更都在 _lock 内，stop 一律在锁外做（可能阻塞）。
#   request 串行执行同一条连接上的请求，排队等待也计入该请求自己的超时预算；撤销时先移除连接再上抛。
#   关闭是终态：连接被摘除并置空，后台启动完成后发现连接已不在表里会停掉刚启动的客户端。
#   调用契约（B3 事件投递等新调用方必须照做）：request 本身不刷新 last_used，空闲回收只认 acquire；
#   所以每次请求前都要先 acquire(owner_key, installation, now) 取连接并刷新使用时间，再 request。
#   直接用旧连接对象反复 request 会让它被空闲关闭判成长期未用。
#   客户端的 stop 必须幂等：停用撞上后台启动时，会先由停用方停一次、再由发布失败的启动方停一次
#   （正是第二次兜住了漏关进程），所以同一个客户端被 stop 两次是正常路径，不能当错误。
# 类用途: 管理插件连接的启动、请求、退避、回收与关闭，供面板与事件两类调用方共用。
class PluginChannelPool:
    # LLM: 依赖显式注入便于合同测试；clock 只用于退避与空闲判定（可注入假时钟），
    #   等待超时始终用真实单调时钟，避免假时钟让真实等待失控；_closed 只表示本池已关闭。
    # 函数用途: 创建通道池（不启动任何插件进程）。
    def __init__(self, *, client_factory: Callable[[object, object], object],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._client_factory = client_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._connections: dict[tuple[str, str], ChannelConnection] = {}
        self._seq = 0
        self._closed = False

    # LLM: 连接字典是池的权威状态；只给测试核对「停用后连接已清空」，调用方不要直接改。
    # 函数用途: 返回当前连接表（只读视图）。
    @property
    def connections(self) -> dict[tuple[str, str], ChannelConnection]:
        return self._connections

    # LLM: 模块级的回收函数要用池自己的锁与关闭标记；以只读属性暴露，调用方不要另加锁。
    # 函数用途: 暴露池锁，供回收函数在同一把锁内操作连接表。
    @property
    def lock(self) -> threading.Lock:
        return self._lock

    # LLM: 关闭标记决定 acquire/_ensure_started 是否拒绝；只读写不重置。
    # 函数用途: 池是否已关闭。
    @property
    def closed(self) -> bool:
        return self._closed

    @closed.setter
    def closed(self, value: bool) -> None:
        self._closed = value

    # LLM: 模块级的启动函数要用池的客户端工厂（构造时注入、之后不变）。
    # 函数用途: 暴露创建插件客户端用的工厂。
    @property
    def client_factory(self) -> Callable[[object, object], object]:
        return self._client_factory

    # LLM: 只做取/建连接与最近使用时间更新，不启动进程；同一 key 始终返回同一对象；
    #   已关闭的池一律按撤销拒绝，不再新建连接。调用方每次请求前都必须调本方法刷新 last_used
    #   （request 不会代劳），否则连接会被空闲关闭提前当成没人用。
    # 函数用途: 取得或创建某激活的连接条目，并记录本次使用时间（每次请求前的固定第一步）。
    def acquire(self, owner_key: str, installation: object, now: float) -> ChannelConnection:
        with self._lock:
            if self._closed:
                raise PluginChannelRevoked()
            key = (owner_key, installation.activation.activation_id)
            connection = self._connections.get(key)
            if connection is None:
                self._seq += 1
                connection = ChannelConnection(key=key, installation=installation, created_seq=self._seq)
                self._connections[key] = connection
            connection.last_used = now
            return connection

    # LLM: 供调用方在池锁内取连接（如服务锁内组装输出）；不创建、不更新使用时间。
    # 函数用途: 读取某 key 的当前连接，没有时返回 None。
    def get(self, conn_key: tuple[str, str]) -> ChannelConnection | None:
        with self._lock:
            return self._connections.get(conn_key)

    # LLM: 调用方需要在锁外慢慢遍历连接表时用这个拿不可变快照：直接遍历活字典会被
    #   并发撤销/新建（后台渲染、以后的事件中心）撞成 RuntimeError: dictionary changed size。
    # 函数用途: 在池锁内返回某 owner 当前连接键的不可变快照。
    def owner_keys(self, owner_key: str) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(key for key in self._connections if key[0] == owner_key)

    # LLM: 调用方在读到一份"当前有效激活"的同一时刻取这个序号；之后（序号更大）才建出来的连接
    #   一定不属于本次判定的对象，回收时不能碰——否则会摘掉别的调用方刚为某个新启用激活建的连接。
    #   用池内单调递增序号而不是墙钟，只表达结构化的先后。
    # 函数用途: 返回池当前的连接创建序号（读快照时一起取，作为回收的时间界）。
    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    # LLM: 同连接请求串行（单在途），排队等待也算本次预算；撤销、退避、超时、启动失败分别按各自异常上抛。
    #   退避只给连接级故障（启动失败、超时、连接断开/传输故障），因为那说明这条连接本身不可用；
    #   请求级失败（远端对这一个请求回的错、调用方 before_send 校验拒绝）只算这次请求失败，
    #   由调用方自己退避——共用连接上还有别的调用方（事件、收紧征询），不能被一次请求错误连坐 5 秒。
    #   在途请求撞上停用/关闭时，传输层会先报"连接断开"；但连接已被摘除说明这次请求是被撤销的，
    #   一律按撤销抛（不标退避），否则调用方会把"插件已停用"当成"连接坏了"，按需要确认处理。
    # 函数用途: 在一条连接上执行一次完整请求（启动 + 发送 + 前后复核）。
    def request(self, connection: ChannelConnection, call: ChannelCall) -> object:
        budget = REQUEST_TIMEOUT_SECONDS if call.timeout is None else call.timeout
        deadline = time.monotonic() + budget
        if not connection.request_lock.acquire(timeout=budget):
            raise PluginChannelTimeout("插件通道排队超时")
        try:
            return self._exchange(connection, call, deadline)
        except PluginChannelRevoked:
            self._revoke(connection)
            raise
        except PluginChannelBackoff:
            raise
        except Exception as exc:
            if not self.holds(connection.key, connection):
                # 请求在途时连接被停用/关闭摘除了：这是撤销，不是连接故障，不标退避
                raise PluginChannelRevoked() from exc
            if _is_connection_failure(exc):
                self._mark_backoff(connection)
            raise
        finally:
            connection.request_lock.release()

    # LLM: 启动时间计入 deadline；发送前复核由 transport 的 authority_check 在写帧前执行，
    #   发送后复核在结果返回前执行，任一复核失败都抛撤销。
    # 函数用途: 执行一次请求的退避检查、启动、发送与结果复核。
    def _exchange(self, connection: ChannelConnection, call: ChannelCall, deadline: float) -> object:
        self._check_backoff(connection)
        transport = self._ensure_started(connection, call, deadline)
        if call.before_send is not None:
            call.before_send(self._current_client(connection))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PluginChannelTimeout("插件通道请求超时")
        result = transport.request(call.method, call.params, timeout=remaining,
                                   authority_check=lambda: self._require_current(connection))
        self._require_current(connection)
        return result

    # LLM: 调用方要在锁外拿客户端做校验（如面板能力检查）；必须在池锁内取一次并确认连接还在表里，
    #   否则并发摘除会把 None 交出去，让调用方把"连接被回收"误判成"插件能力声明有问题"。
    # 函数用途: 返回该连接当前仍有效的客户端；连接已被摘除时按撤销处理。
    def _current_client(self, connection: ChannelConnection) -> object:
        with self._lock:
            client = connection.client
            if self._connections.get(connection.key) is not connection or client is None:
                raise PluginChannelRevoked()
            return client

    # LLM: 启动在独立 daemon 线程里做，等待只到 deadline；超时不取消后台启动，完成后下一次请求直接复用。
    #   启动失败一律包成 PluginChannelStartFailed（连接级故障，触发退避），让退避判定只看结构化类型；
    #   池已关闭或连接已被摘除时立即按撤销处理，不拉起新启动。
    # 函数用途: 确保连接已完成启动并返回本次传输，启动时间计入超时预算。
    def _ensure_started(self, connection: ChannelConnection, call: ChannelCall, deadline: float) -> object:
        with self._lock:
            if self._closed or self._connections.get(connection.key) is not connection:
                raise PluginChannelRevoked()
            if connection.transport is not None:
                return connection.transport
            if not connection.starting:
                self._launch_start(connection, call.owner)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not connection.start_event.wait(remaining):
            raise PluginChannelTimeout("插件连接启动超时")
        error = connection.start_error
        if error is not None:
            if isinstance(error, PluginChannelRevoked):
                raise error
            raise PluginChannelStartFailed("插件连接启动失败") from error
        transport = connection.transport
        if transport is None:
            raise PluginChannelRevoked()
        return transport

    # LLM: 只在池锁内调用；重置本轮启动状态并起后台线程，调用方负责等待 start_event。
    # 函数用途: 为一条尚未启动的连接拉起后台启动线程。
    def _launch_start(self, connection: ChannelConnection, owner: object) -> None:
        launch_start(self, connection, owner)

    # LLM: 客户端创建与 require_settled 只在首次启动做；失败后保留客户端供下一次直接重试 start。
    #   发布传输时连接已被摘除（停用或关闭撞上后台启动）就在锁外停掉刚启动的客户端，并按撤销交给等待者。
    # 函数用途: 后台线程主体：创建客户端、复核代次、启动连接、发布传输；连接已失效时停掉客户端。
    def _start_worker(self, connection: ChannelConnection, owner: object) -> None:
        start_worker(self, connection, owner)

    # LLM: 已存在客户端时不重建、不重复 require_settled；只有首次创建才做结清核对。
    # 函数用途: 返回连接的客户端，必要时创建并核对上一代资源已结清。
    def _client_for(self, connection: ChannelConnection, owner: object) -> object:
        return client_for(self, connection, owner)

    # LLM: 连接已被摘除（停用/关闭）或客户端被并发替换时不再发布传输，返回 False 让启动方停掉客户端。
    # 函数用途: 把启动完成的传输写到连接上（仅当连接仍是表中当前对象且客户端未被替换）。
    def _publish_transport(self, connection: ChannelConnection, client: object, transport: object) -> bool:
        return publish_transport(self, connection, client, transport)

    # LLM: 无论成功失败都必须置位事件，否则等待者会一直等到超时。
    # 函数用途: 结束本轮启动：记录错误、清除启动中标志并唤醒等待者。
    def _finish_start(self, connection: ChannelConnection, error: BaseException | None) -> None:
        with self._lock:
            if error is not None:
                connection.start_error = error
            connection.starting = False
            connection.start_event.set()

    # LLM: 退避只在失败后生效，不阻止调用方读取已有缓存（那是调用方的事）；用注入时钟判定。
    # 函数用途: 退避期内直接快速失败。
    def _check_backoff(self, connection: ChannelConnection) -> None:
        with self._lock:
            remaining = connection.backoff_until - self._clock()
        if remaining > 0:
            raise PluginChannelBackoff("插件通道退避中")

    # LLM: 任何非撤销失败都刷新退避，避免故障插件被连续重试。
    # 函数用途: 记下该连接的下一次可请求时间。
    def _mark_backoff(self, connection: ChannelConnection) -> None:
        with self._lock:
            connection.backoff_until = self._clock() + ERROR_BACKOFF_SECONDS

    # LLM: 激活复核以安装表当前快照为准：客户端已被回收（为 None）或没有代次引用、读表失败、代次不同都算撤销，
    #   不追随新代次；绝不能把已回收的连接当成 AttributeError 漏出去。
    # 函数用途: 复核连接仍是同一激活代次，否则抛出撤销。
    def _require_current(self, connection: ChannelConnection) -> None:
        activation_ref = getattr(connection.client, "activation_ref", None)
        if activation_ref is None:
            raise PluginChannelRevoked()
        try:
            current = activation_ref.require()
        except (OSError, ValueError) as exc:
            raise PluginChannelRevoked() from exc
        if getattr(current, "activation", None) != connection.installation.activation:
            raise PluginChannelRevoked()

    # LLM: 撤销是终态：连接先移出表再关客户端，后续请求会重新取一条新连接（若激活仍有效）。
    # 函数用途: 移除并关闭一条已失效的连接。
    def _revoke(self, connection: ChannelConnection) -> None:
        with self._lock:
            if self._connections.get(connection.key) is connection:
                del self._connections[connection.key]
        client = connection.client
        connection.client = None
        connection.transport = None
        _stop_client(client)

    # LLM: 供调用方在写回结果前确认连接仍是当前对象；不要在池锁外直接读连接表。
    # 函数用途: 判断某连接是否仍是该 key 的当前连接。
    def holds(self, conn_key: tuple[str, str], connection: ChannelConnection) -> bool:
        with self._lock:
            return self._connections.get(conn_key) is connection

    # LLM: 失效连接直接删（含在途）；stop 在锁外做，可能阻塞。调用方仍需自行清理它的结果缓存。
    #   共享池上必须带 scope（只回收自己管的、且判定时间界之前就存在的连接），
    #   否则会把别的调用方刚为新启用激活建的连接一起摘掉。
    # 函数用途: 移除该 owner 下激活已失效的连接并关闭其客户端。
    def retire_stale(self, owner_key: str, valid_ids: set[str],
                     scope: RetireScope | None = None) -> None:
        retire_stale_connections(self, owner_key, valid_ids, scope)

    # LLM: 只关客户端、保留连接条目（下次请求重建）；在途、启动中或调用方标记忙时不关。
    # 函数用途: 空闲超时后释放该连接的客户端资源。
    def close_idle(self, conn_key: tuple[str, str], now: float, busy: bool) -> None:
        close_idle_connection(self, conn_key, now, busy)

    # LLM: 关闭是终态：先置已关闭标记（acquire 与 _ensure_started 据此拒绝），再像 retire_stale 一样
    #   摘除每条连接并置空客户端与传输；启动线程完成后发布传输会失败，由发布方停掉刚启动的客户端。
    # 函数用途: 关闭全部连接并清空连接表；之后的新请求一律按撤销拒绝。
    def close(self) -> None:
        close_all_connections(self)


# LLM: 回收逻辑不依赖池的其它行为，抽成模块级函数让池类保持在可读长度内；调用方仍走池上的同名方法。
# 函数用途: 为一条尚未启动的连接拉起后台启动线程（重置本轮启动状态）。
def launch_start(pool: PluginChannelPool, connection: ChannelConnection, owner: object) -> None:
    connection.starting = True
    connection.start_error = None
    connection.start_event.clear()
    thread = threading.Thread(target=pool._start_worker, args=(connection, owner),
                              name="plugin-channel-start", daemon=True)
    thread.start()


# LLM: 客户端创建与 require_settled 只在首次启动做；失败后保留客户端供下一次直接重试 start。
#   发布传输时连接已被摘除（停用或关闭撞上后台启动）就在锁外停掉刚启动的客户端，并按撤销交给等待者。
# 函数用途: 后台线程主体：创建客户端、复核代次、启动连接、发布传输；连接已失效时停掉客户端。
def start_worker(pool: PluginChannelPool, connection: ChannelConnection, owner: object) -> None:
    error: BaseException | None = None
    try:
        client = pool._client_for(connection, owner)
        pool._require_current(connection)
        transport = client.start()
        if not pool._publish_transport(connection, client, transport):
            _stop_client(client)
            error = PluginChannelRevoked()
    except BaseException as exc:  # noqa: BLE001 启动失败要原样交给等待者，不能让线程静默死掉
        error = exc
    pool._finish_start(connection, error)


# LLM: 已存在客户端时不重建、不重复 require_settled；只有首次创建才做结清核对。
# 函数用途: 返回连接的客户端，必要时创建并核对上一代资源已结清。
def client_for(pool: PluginChannelPool, connection: ChannelConnection, owner: object) -> object:
    if connection.client is not None:
        return connection.client
    client = pool.client_factory(owner, connection.installation)
    settled = getattr(client, "require_settled_previous_resources", None)
    if callable(settled):
        settled()
    with pool.lock:
        connection.client = client
    return client


# LLM: 连接已被摘除（停用/关闭）或客户端被并发替换时不再发布传输，返回 False 让启动方停掉客户端。
# 函数用途: 把启动完成的传输写到连接上（仅当连接仍是表中当前对象且客户端未被替换）。
def publish_transport(pool: PluginChannelPool, connection: ChannelConnection,
                      client: object, transport: object) -> bool:
    with pool.lock:
        if pool.connections.get(connection.key) is connection and connection.client is client:
            connection.transport = transport
            return True
        return False


# 函数用途: 摘除一条连接并清空它的客户端与传输，返回被摘掉的客户端。
def _detach_connection(pool: PluginChannelPool, conn_key: tuple[str, str]) -> object | None:
    connection = pool.connections.pop(conn_key, None)
    if connection is None:
        return None
    client = connection.client
    connection.client = None
    connection.transport = None
    return client


# LLM: 失效连接直接删（含在途）；stop 在锁外做，可能阻塞。调用方仍需自行清理它的结果缓存。
#   调用方可以给一个 scope 限定范围（只摘自己管的激活、且只摘判定时间界之前建的连接）——
#   别的调用方在快照之后为新启用激活建的连接不能被误摘。
# 函数用途: 移除该 owner 下激活已失效的连接并关闭其客户端。
def retire_stale_connections(pool: PluginChannelPool, owner_key: str, valid_ids: set[str],
                             scope: RetireScope | None = None) -> None:
    with pool.lock:
        stale = [key for key, connection in pool.connections.items()
                 if key[0] == owner_key and key[1] not in valid_ids and _in_scope(connection, key, scope)]
        clients = [_detach_connection(pool, key) for key in stale]
    for client in clients:
        _stop_client(client)


# LLM: 回收范围：managed 是本调用方管的激活集合（别人的连接一律不碰）；created_before 是读有效集合
#   那一刻的池内创建序号，晚于它建出来的连接不属于本次判定对象。两者都是结构化的先后，不依赖墙钟。
# 类用途: 打包一次回收的限定条件，避免回收函数参数随需求增长。
@dataclass(frozen=True)
class RetireScope:
    managed: frozenset[str]
    created_before: int | None = None


# LLM: 没有 scope 时保持旧语义（只按 valid 判断），有 scope 时必须两条都满足：激活归本调用方管，
#   且连接在判定时间界之前就已存在。
# 函数用途: 判断一条失效连接是否落在此次回收范围内。
def _in_scope(connection: ChannelConnection, key: tuple[str, str], scope: RetireScope | None) -> bool:
    if scope is None:
        return True
    if key[1] not in scope.managed:
        return False
    return scope.created_before is None or connection.created_seq <= scope.created_before


# LLM: 只关客户端、保留连接条目（下次请求重建）；在途、启动中或调用方标记忙时不关。
# 函数用途: 空闲超时后释放该连接的客户端资源。
def close_idle_connection(pool: PluginChannelPool, conn_key: tuple[str, str], now: float, busy: bool) -> None:
    client = None
    with pool.lock:
        connection = pool.connections.get(conn_key)
        if connection is None or busy or connection.starting or connection.request_lock.locked():
            return
        if connection.client is None or now - connection.last_used <= IDLE_CLOSE_SECONDS:
            return
        client = connection.client
        connection.client = None
        connection.transport = None
    _stop_client(client)


# LLM: 关闭是终态：先置已关闭标记（acquire 与 _ensure_started 据此拒绝），再像 retire_stale 一样
#   摘除每条连接并置空客户端与传输；启动线程完成后发布传输会失败，由发布方停掉刚启动的客户端。
# 函数用途: 关闭全部连接并清空连接表；之后的新请求一律按撤销拒绝。
def close_all_connections(pool: PluginChannelPool) -> None:
    with pool.lock:
        pool.closed = True
        clients = [_detach_connection(pool, key) for key in list(pool.connections)]
    for client in clients:
        _stop_client(client)


# LLM: 关闭失败不抛出，未确认的清理由原进程资源账保留；只记日志。
#   调用方（池）保证可能对同一个客户端重复调用 stop，所以客户端的 stop 必须幂等。
# 函数用途: 尽力关闭一个插件客户端。
def _stop_client(client: object | None) -> None:
    if client is None:
        return
    try:
        client.stop()
    except Exception:  # noqa: BLE001 清理未确认留在原资源账，不能影响通道调用
        logger.warning("插件通道连接关闭未确认")


# LLM: 请求级失败与连接级失败必须分开：请求级（远端对这一个请求回的错、调用方校验拒绝）说明
#   连接本身还好，只是这次调用不行，退避整条连接会连坐同一连接上的其它调用方（事件、收紧征询）。
#   判定只看结构化类型与错误码，绝不读错误文字。
#   连接级：超时（含排队/启动/响应）、OSError 家族（断连、管道破裂）、以及明确标记为传输/启动故障的码。
# 函数用途: 判断一个请求异常是否属于「这条连接本身不可用」，据此决定要不要给整条连接退避。
def _is_connection_failure(exc: BaseException) -> bool:
    if isinstance(exc, PluginChannelTimeout):
        return True
    if isinstance(exc, OSError):
        return True
    return getattr(exc, "code", "") in _CONNECTION_FAILURE_CODES
