# LLM: 唯一owner来源仓库，提交回调仅供受信宿主注入；nonce→来源锁及CAS/正文/意图保持同源。
# 模块用途: 管理默认和显式本地来源的安全加载及受授权写入；URL由独立传输模块加载，不授予额外权限。
import json
import math
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from ..path_access_policy import PathAccessPolicy, owner_home_containing
from ._agents_contract_fields import AgentsContractError, require_digest, require_text
from .agents_source_contract import (
    AgentsSourceSnapshot,
    _validate_locator,
    validate_source_snapshot,
)
from .agents_source_files import (
    AGENTS_CONTROL_MAX_BYTES,
    AGENTS_SOURCE_MAX_BYTES,
    read_descriptor,
    read_source_bytes,
    replace_descriptor_text,
    source_configuration_lock,
    source_descriptor,
)
from .agents_source_limits import AGENTS_WRITE_MAX_CHARS, enforce_agents_write_limit
from .agents_source_load_contract import AgentsSourceLoadResult, validate_source_load
from .agents_source_processes import source_process_boundary
from .agents_write_contract import validate_write_authorization, write_intent_digest

_CONFIG_KEYS = frozenset(("schema_version", "owner", "kind", "value", "revision", "updated_by", "updated_at", "identity"))
_CONTROL_ROOTS = frozenset(("persona", "runs", "agents", "compact", "data", "logs", "memory", "conversations", "permissions", "credentials", "secrets"))
_SECRET_PARTS = frozenset((".ssh", ".aws", ".gnupg", ".kube", "credentials", "secrets"))
_SECRET_NAMES = frozenset((".env", "id_rsa", "id_ed25519", "credentials.json", "credentials.yaml", "permissions.json", "config.yaml"))


# LLM: 仅宿主创建，owner必须对应实际home；一个仓库一个配置文件，缺失默认来源不生成副本或锁件。
# 类用途: 把来源配置、权限检查、原文加载和最终写回集中在同一个服务里。
class AgentsSourceRepository:
    # LLM: callable开关/许可每次重核，file_commit可为空或受信宿主原source；不从配置造许可，不新增默认网络。
    # 函数用途: 绑定可信owner/home和当前权限依赖，不读取模型提供的owner。
    def __init__(self, *, owner: str, owner_home: Path, path_policy: PathAccessPolicy, options=None):
        from .agents_source_options import AgentsSourceOptions
        options = options or AgentsSourceOptions()
        require_text(owner, "owner")
        self.owner = owner
        self.owner_home = Path(owner_home)
        self.default_path = self.owner_home / "AGENTS.md"
        self._path_policy = path_policy
        self._path_policy_provider = options.path_policy_provider
        if not self.owner_home.is_absolute():
            raise AgentsContractError("AGENTS_SOURCE_LOCATOR_INVALID", "owner_home")
        _validate_locator("owner_default", str(self.default_path))
        root = path_policy.agent_home_root
        if root is not None and self.owner_home != root / "owners" / owner:
            raise AgentsContractError("AGENTS_SOURCE_OWNER_MISMATCH", "owner_home")
        self.config_path = self.owner_home / "persona/agents_source.json"
        self.last_good_path = self.owner_home / "persona/agents_last_good.json"
        self._enabled = options.enabled
        self._network_enabled = options.network_enabled
        self._url_allowed = options.url_allowed
        self._fetcher = options.fetcher
        self._url_validator = options.url_validator
        self.prompt_max_chars = AGENTS_WRITE_MAX_CHARS
        self._file_commit = options.file_commit

    # LLM: 普通来源默认原同步路径；管理组合根注入的callback每次核原C/receipt/实际owner，网络不能传进来。
    # 函数用途: 提交一次有界文件事务，不创建第二许可或缓存。
    def commit_file(self, operation):
        return self._file_commit(operation, owner_home=self.owner_home) if self._file_commit is not None else operation()

    # LLM: 只消费宿主依赖；构造时权限不延寿，管理file commit由构造原source绑定，不从model JSON取得。
    # 函数用途: 每次本地来源许可复核都使用当前正常path policy。
    @property
    def path_policy(self):
        return self._path_policy_provider() if self._path_policy_provider is not None else self._path_policy

    # LLM: 只读受控配置，不拉取来源；无配置是revision0默认，坏配置必须失败而非静默fallback。
    # 函数用途: 返回C授权签发/比较使用的完整当前CAS。
    def configuration(self) -> AgentsSourceSnapshot:
        return self._configuration_record()[0]

    # LLM: 只返回不可变v1结果；默认原文不改换行/BOM，本地失败不fallback到其它来源。
    # 函数用途: 加载当前已配置的来源并交回正文、摘要或固定失败码。
    def load(self) -> AgentsSourceLoadResult:
        from .agents_source_network import source_request_budget
        with source_request_budget():
            return self._load_current()

    # LLM: 本地配置和正文在共同来源读边界内；URL取出CAS后释放锁再校验和抓取，不能重入配置flock。
    # 函数用途: 只返回完整本地版本，且不让网络抓取挡住切源。
    def _load_current(self) -> AgentsSourceLoadResult:
        from .agents_source_read_lock import source_read_boundary
        try:
            with source_read_boundary(self.owner_home):
                result = self._load_snapshot_locked()
        except AgentsContractError as error:
            if error.code not in {'AGENTS_SOURCE_UNSAFE_PATH', 'AGENTS_SOURCE_IDENTITY_CHANGED'}:
                raise
            return self._unavailable(self._default_configuration(), error.code)
        if not isinstance(result, AgentsSourceSnapshot):
            return result
        try:
            self.require_permission(result)
        except AgentsContractError as error:
            return self._unavailable(result, error.code)
        return self._load_url(result)

    # LLM: 调用方持来源读锁；本地正文与CAS同一临界区，URL只交回配置身份不发请求。
    # 函数用途: 完成本地安全读取或返回待抓取的URL配置。
    def _load_snapshot_locked(self):
        try:
            snapshot, record = self._configuration_record()
        except AgentsContractError as error:
            if error.code != "AGENTS_SOURCE_UNSAFE_PATH":
                raise
            # 无法安全读取配置时只有不可用诊断，没有默认正文fallback或可签发配置CAS。
            return self._unavailable(self._default_configuration(), error.code)
        try:
            if snapshot.kind == "url":
                return snapshot
            self.require_permission(snapshot)
            raw, _ = self._read_local(snapshot, record)
            return self._ready(snapshot, raw)
        except AgentsContractError as error:
            return self._unavailable(snapshot, error.code)
        except FileNotFoundError:
            return self._unavailable(snapshot, "AGENTS_SOURCE_MISSING")

    # LLM: 默认完整URL/DNS检查；validate_network=False只供已在门外验证的最终file CAS/策略比较，不返回正文或授网络权。
    # 函数用途: 复核同owner/来源CAS和当前路径或网络策略，避免网络等待进入管理短门。
    def require_permission(self, snapshot: AgentsSourceSnapshot, *, validate_network=True) -> None:
        validate_source_snapshot(snapshot, self.owner)
        if snapshot.kind != "owner_default" and not self._enabled():
            raise AgentsContractError("AGENTS_SOURCE_DISABLED", "kind")
        if snapshot.kind == "url":
            if not self._network_enabled() or not self._url_allowed(snapshot.locator):
                raise AgentsContractError("AGENTS_SOURCE_PERMISSION_REVOKED", "locator")
            if validate_network:
                self._validate_url(snapshot.locator)
        else:
            self._require_local_permission(Path(snapshot.locator))

    # LLM: 受管锁先于切源；原nonce/CAS/意图及资源退役不变，管理员底线不能被换来源绕过。
    # 函数用途: 核对受管治理后串行切源与资源启动，保持原来源授权。
    def set_source(self, kind: str, locator: str, *, authorization, context) -> AgentsSourceSnapshot:
        from ..user_space.group_authorization_context import require_group_persona_lease
        from ..user_space.managed_user_governance import require_managed_persona_write
        require_managed_persona_write(self.owner_home, "agents", {"operation": "set_source", "kind": kind, "locator": locator})
        require_group_persona_lease(self.default_path)
        intent = write_intent_digest("set_source", {"kind": kind, "locator": locator})
        validate_write_authorization(authorization, context)
        self._require_binding(context, "set_source", intent)
        if not self._enabled():
            raise AgentsContractError("AGENTS_SOURCE_DISABLED", "kind")
        with source_process_boundary(), source_configuration_lock(self.owner_home, file_commit=self._file_commit):
            from .agents_source_switch import set_source_locked
            return set_source_locked(self, kind, locator, context)

    # LLM: 原生写同样守受管锁；URL只读、nonce及来源CAS不放宽，不能借write_text绕过人格治理。
    # 函数用途: 先核受管用户治理，再把原授权的新正文写入当前本地来源。
    def write_text(self, content: str, *, authorization, context) -> AgentsSourceLoadResult:
        from ..user_space.group_authorization_context import require_group_persona_lease
        from ..user_space.managed_user_governance import require_managed_persona_write
        require_managed_persona_write(self.owner_home, "agents", {"operation": "write_text", "content": content})
        require_group_persona_lease(self.default_path)
        intent = write_intent_digest("write", content)
        validate_write_authorization(authorization, context)
        self._require_binding(context, "write", intent)
        body = content.encode("utf-8")
        if len(body) > AGENTS_SOURCE_MAX_BYTES:
            raise AgentsContractError("AGENTS_SOURCE_TOO_LARGE", "content")
        with source_configuration_lock(self.owner_home, file_commit=self._file_commit):
            from .agents_source_local import write_text_locked
            return write_text_locked(self, content, context)

    # LLM: A重算真实意图，管理callback不替代原授权/CAS/owner；跨owner仍精确指向本仓库。
    # 函数用途: 拒绝换新值、换动作或拿另一owner原授权写入当前owner。
    def _require_binding(self, context, action: str, intent: str) -> None:
        if context.owner != self.owner or context.action != action or context.intent_sha256 != intent:
            raise AgentsContractError("AGENTS_WRITE_BINDING_MISMATCH", "intent_sha256")

    # LLM: 配置由严格来源FD有界读取，schema/owner/计数/身份/定位都精确类型；拒绝额外字段和坏JSON。
    # 函数用途: 读取唯一权威配置，缺失才使用显式revision0默认。
    def _configuration_record(self):
        from .agents_source_configuration import configuration_record
        return configuration_record(self)

    # LLM: 无I/O的revision0来源身份，仅用于缺配置或无法安全读取时的无正文诊断；不是默认读取豁免。
    # 函数用途: 统一默认配置元数据，不将失败伪装为默认正文成功。
    def _default_configuration(self):
        return AgentsSourceSnapshot(1, self.owner, "owner_default", str(self.default_path), 1, 0,
                                    "", None, "", "unavailable", "AGENTS_SOURCE_NOT_LOADED", False, False)

    # LLM: Full Access仅是一般权限，不能跨owner读来源，不能把宿主控制件/凭据/SOUL/USER当AGENTS。
    # 函数用途: 在正常path policy之后执行更严格的来源专用禁区裁决。
    def _require_local_permission(self, path: Path) -> None:
        from .agents_source_local import require_local_permission
        require_local_permission(self, path)

    # LLM: 原文最多2MiB，所有本地源读取同FD；只有无配置且默认文件缺失兼容原空文档行为。
    # 函数用途: 读取当前本地来源并复核首次配置时记录的身份。
    def _read_local(self, snapshot, record):
        try:
            raw, identity = read_source_bytes(Path(snapshot.locator))
        except FileNotFoundError:
            if snapshot.kind == "owner_default" and record is None:
                return b"", ""
            raise
        self._check_identity(record, identity)
        return raw, identity

    # LLM: 只有显式配置带预期身份；默认兼容外部合法编辑，但从不跳过严格nofollow读取。
    # 函数用途: 拒绝任意文件名的父目录或叶子替换。
    def _check_identity(self, record, identity) -> None:
        if record is not None and record["identity"] != identity:
            raise AgentsContractError("AGENTS_SOURCE_IDENTITY_CHANGED", "locator")

    # LLM: 内容摘要取实际正文而非模型参数；URL配置更换只核已保存last-good，不在锁内做网络抓取。
    # 函数用途: 对显式授权的旧正文前像再做一次核对。
    def _require_current_sha(self, snapshot, record, expected) -> None:
        if snapshot.kind == "url":
            cached = self._read_last_good(snapshot)
            digest = cached.snapshot.content_sha256 if cached is not None else ""
        else:
            self.require_permission(snapshot)
            raw, _ = self._read_local(snapshot, record)
            digest = sha256(raw).hexdigest()
        if digest != expected:
            raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")

    # LLM: UTF8严格解码且不规范化换行；错误不带原文，不把坏传输写入last-good。
    # 函数用途: 保留来源字节对应的精确文本。
    def _decode(self, raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeError as error:
            raise AgentsContractError("AGENTS_SOURCE_ENCODING_INVALID", "content") from error

    # LLM: 新来源内容必须通过完整安全扫描；默认persona的逐行兼容清洗仍在PersonaRepository，不改旧字节口径。
    # 函数用途: 在显式来源成功或用户提交之前拒绝危险指令正文。
    def _scan(self, content: str) -> None:
        from ..memory_store.security import scan_memory_content
        if not scan_memory_content(content).safe:
            raise AgentsContractError("AGENTS_SOURCE_CONTENT_UNSAFE", "content")

    # LLM: fetched_at不参与prefix_digest；不隐式裁剪原正文，截断仍由原注入预算入口统一处理。
    # 函数用途: 生成与原始UTF8字节摘要一致的v1成功交接对象。
    def _ready(self, snapshot, raw: bytes, *, fetched_at=None, last_good_ref=""):
        content = self._decode(raw)
        if snapshot.kind != "owner_default":
            self._scan(content)
        ready = replace(snapshot, content_sha256=sha256(raw).hexdigest(), fetched_at=fetched_at or float(time.time()),
                        last_good_ref=last_good_ref, status="ready", failure_code="", used_cache=False, stale=False)
        return validate_source_load(AgentsSourceLoadResult(ready, content), self.owner)

    # LLM: 所有安全/权限失败无可注入正文与摘要，不复用其它配置或owner的缓存。
    # 函数用途: 用固定失败码表示当前来源不可用。
    def _unavailable(self, snapshot, code: str):
        failed = replace(snapshot, content_sha256="", fetched_at=None, last_good_ref="", status="unavailable",
                         failure_code=code, used_cache=False, stale=False)
        return validate_source_load(AgentsSourceLoadResult(failed, None), self.owner)

    # LLM: 元数据从同次加载和当前CAS构造，正文不复制到元数据；固定文本不含时间/revision或私有定位。
    # 函数用途: 给线程冻结保存复核权限所需的owner、来源和路径身份/跳转链。
    def activation_metadata(self, snapshot):
        from .agents_source_cache import activation_metadata
        return activation_metadata(self, snapshot)

    # LLM: active可与pending不同CAS，仍核原权限/身份；管理file提交的网络复核分层不改变此正文消费入口。
    # 函数用途: 在原线程前缀锁内核对持久active元数据是否还允许注入。
    def require_active_permission(self, metadata) -> None:
        from .agents_source_cache import require_active_permission
        require_active_permission(self, metadata)

    # LLM: DNS/public及每跳许可复核由独立网络模块实现；缓存与active来源也走同一个入口。
    # 函数用途: 调用逐URL的当前网络安全检查，不进行HTTP抓取。
    def _validate_url(self, url: str) -> None:
        _validate_locator("url", url)
        if self._url_validator is not None:
            self._url_validator(url, url_allowed=self._url_allowed)
            return
        from .agents_source_network import validate_agents_url
        validate_agents_url(url, url_allowed=self._url_allowed)

    # LLM: 抓取在配置锁外；成功扫描后回锁重核完整CAS和当前所有跳许可，旧响应不能覆盖新配置。
    # 函数用途: 安全抓取HTTPS正文，技术故障才尝试同CAS的owner last-good。
    def _load_url(self, snapshot):
        from .agents_source_cache import load_url
        return load_url(self, snapshot)

    # LLM: 降级与activation元数据共用唯一缓存验证路径，不因展示需要引入第二解析权威。
    # 函数用途: 返回当前来源最后一次完整成功正文，仅供技术故障降级。
    def _read_last_good(self, snapshot):
        return self._last_good_record(snapshot)[1]

    # LLM: 缓存严格nofollow/有界；原owner、完整CAS、摘要、原时间、所有redirect许可均重新核对，错误均为固定码。
    # 函数用途: 读取经完整验证的last-good控制记录及结果，供降级与冻结来源metadata共同消费。
    def _last_good_record(self, snapshot):
        from .agents_source_cache import last_good_record
        return last_good_record(self, snapshot)
