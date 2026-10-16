# LLM: 第一期 A 的来源快照合同；本轮不实现加载、不读取来源、不改变固定前缀。
# 模块用途: 冻结来源加载、缓存、配置 CAS 与前缀交接使用的数据结构。

import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from ._agents_contract_fields import (
    AgentsContractError,
    decode_fields,
    require_counter,
    require_digest,
    require_text,
    validate_scalars,
)


# LLM: 标量快照不可变；所有身份来自 A 的可信加载器，而不是模型参数。
# 类用途: 表达一次来源加载的结果，包括失败与上次成功缓存。
@dataclass(frozen=True, slots=True)
class AgentsSourceSnapshot:
    schema_version: int
    owner: str
    kind: str
    locator: str
    config_version: int
    revision: int
    content_sha256: str
    fetched_at: float | None
    last_good_ref: str
    status: str
    failure_code: str
    used_cache: bool
    stale: bool

    # LLM: JSON 只描述快照，不授予路径或网络权限。
    # 函数用途: 生成独立的序列化副本。
    def to_dict(self) -> dict[str, object]:
        _validate_source_shape(self)
        return asdict(self)

    # LLM: 解析必须绑定当前规范 owner；绝不能靠 JSON 自报身份。
    # 函数用途: 解析并核对来源快照。
    @classmethod
    def from_dict(cls, payload: object, expected_owner: str) -> "AgentsSourceSnapshot":
        return validate_source_snapshot(cls(**decode_fields(cls, payload)), expected_owner)

    # LLM: 仅内容摘要进入稳定前缀身份，不包含抓取时间、状态或 revision。
    # 函数用途: 返回已加载内容对应的固定前缀摘要。
    @property
    def prefix_digest(self) -> str:
        _validate_source_shape(self)
        if self.status == "unavailable":
            raise AgentsContractError("AGENTS_SOURCE_UNAVAILABLE", "status")
        return self.content_sha256

    # LLM: CAS 是 owner 与完整来源配置身份；内容写入另绑定 content_sha256。
    # 函数用途: 给 A/C 一个不可变的配置核对元组。
    @property
    def configuration_cas(self) -> tuple[object, ...]:
        _validate_source_shape(self)
        return (self.owner, self.kind, self.locator, self.config_version, self.revision)


# LLM: 纯校验不替代权限；期望CAS先验精确类型，不接受bool/float的数值别名。
# 函数用途: 检查来源身份及规范配置 CAS，不读写文件或网络。
def validate_source_snapshot(snapshot, expected_owner, expected_cas=None):
    if type(snapshot) is not AgentsSourceSnapshot:
        raise AgentsContractError("AGENTS_CONTRACT_OBJECT_REQUIRED", "snapshot")
    _validate_source_shape(snapshot)
    require_text(expected_owner, "expected_owner")
    if snapshot.owner != expected_owner:
        raise AgentsContractError("AGENTS_SOURCE_OWNER_MISMATCH", "owner")
    if expected_cas is not None:
        _validate_expected_cas(expected_cas)
        if snapshot.configuration_cas != expected_cas:
            raise AgentsContractError("AGENTS_SOURCE_CONFIG_CONFLICT", "revision")
    return snapshot


# LLM: CAS来源为可信配置读取点；纯结构检查不把外来对象的相等运算视为身份。
# 函数用途: 要求五字段原生tuple，版本计数必须原生int。
def _validate_expected_cas(cas) -> None:
    if type(cas) is not tuple or len(cas) != 5:
        raise AgentsContractError("AGENTS_SOURCE_CAS_INVALID", "expected_cas")
    if not all(type(value) is kind for value, kind in zip(cas, (str, str, str, int, int))):
        raise AgentsContractError("AGENTS_SOURCE_CAS_INVALID", "expected_cas")


# LLM: 纯词法检查不能作为 nofollow、SSRF、代理地址固定或权限证据；这些由 A 接线。
# 函数用途: 拒绝非绝对路径、路径漂移和不规范的 HTTPS 定位。
def _validate_locator(kind: str, locator: str) -> None:
    require_text(locator, "locator")
    if kind == "url":
        _validate_url_locator(locator)
        return
    path = PureWindowsPath(locator) if "\\" in locator else PurePosixPath(locator)
    if not path.is_absolute() or ".." in path.parts or str(path) != locator:
        raise AgentsContractError("AGENTS_SOURCE_LOCATOR_INVALID", "locator")


# LLM: 不解析DNS或连接目标；拒绝非法URI和重组漂移，不静默归一化已签定位。
# 函数用途: 校验规范HTTPS定位的词法结构，不代表网络或owner权限已经通过。
def _validate_url_locator(locator: str) -> None:
    try:
        parsed = urlsplit(locator)
        invalid = _url_parts_invalid(parsed, locator)
        _ = parsed.port
    except ValueError:
        invalid = True
    if invalid:
        raise AgentsContractError("AGENTS_SOURCE_LOCATOR_INVALID", "locator")


# LLM: URI采用RFC字符集和完整百分号转义，国际化定位应由A先IDNA/百分号编码；无I/O。
# 函数用途: 拒绝裸反斜杠、坏转义、凭据或片段以及不稳定的URI表示。
def _url_parts_invalid(parsed, locator: str) -> bool:
    return any((
        parsed.scheme != "https", not parsed.hostname, parsed.username is not None,
        parsed.password is not None, bool(parsed.fragment),
        parsed.netloc != parsed.netloc.lower(), parsed.geturl() != locator,
        re.search(r"[^A-Za-z0-9:/?#\[\]@!$&'()*+,;=._~%\-]", locator) is not None,
        re.search(r"%(?![0-9A-Fa-f]{2})", locator) is not None,
    ))


# LLM: 不可用结果不能携带可注入的正文摘要；缓存结果必须有失败诊断和 last-good 引用。
# 函数用途: 校验成功、缓存降级和首次不可用三种互斥结果。
def _validate_source_outcome(snapshot: AgentsSourceSnapshot) -> None:
    if snapshot.status == "unavailable":
        invalid = (snapshot.content_sha256 != "" or snapshot.fetched_at is not None
                   or snapshot.last_good_ref != "" or snapshot.used_cache or snapshot.stale)
    else:
        require_digest(snapshot.content_sha256, "content_sha256")
        if snapshot.fetched_at is None or snapshot.fetched_at <= 0:
            raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "fetched_at")
        invalid = _loaded_outcome_invalid(snapshot)
    if invalid:
        raise AgentsContractError("AGENTS_SOURCE_RESULT_INVALID", "status")
    if snapshot.status != "ready":
        require_text(snapshot.failure_code, "failure_code")


# LLM: 元数据状态必须与实际使用缓存的标志相符；不能把抓取失败说成新鲜成功。
# 函数用途: 判定有内容的结果是否自相矛盾。
def _loaded_outcome_invalid(snapshot: AgentsSourceSnapshot) -> bool:
    if snapshot.status == "ready":
        return bool(snapshot.failure_code or snapshot.used_cache or snapshot.stale)
    return not (snapshot.used_cache and snapshot.stale and snapshot.last_good_ref)


# LLM: 所有入口只接受精确快照类型；直构或子类也不绕过序列化、前缀或校验。
# 函数用途: 检查快照的完整结构，不访问任何实际文件或缓存。
def _validate_source_shape(snapshot: AgentsSourceSnapshot) -> None:
    if type(snapshot) is not AgentsSourceSnapshot:
        raise AgentsContractError("AGENTS_CONTRACT_OBJECT_REQUIRED", "snapshot")
    validate_scalars(snapshot)
    require_text(snapshot.owner, "owner")
    require_counter(snapshot.config_version, "config_version")
    require_counter(snapshot.revision, "revision", allow_zero=True)
    if snapshot.kind not in {"owner_default", "local_path", "url"}:
        raise AgentsContractError("AGENTS_SOURCE_KIND_UNKNOWN", "kind")
    if snapshot.status not in {"ready", "cached", "unavailable"}:
        raise AgentsContractError("AGENTS_SOURCE_STATUS_UNKNOWN", "status")
    _validate_locator(snapshot.kind, snapshot.locator)
    require_text(snapshot.last_good_ref, "last_good_ref", allow_empty=True)
    require_text(snapshot.failure_code, "failure_code", allow_empty=True)
    _validate_source_outcome(snapshot)
