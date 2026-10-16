# LLM: 原URL/active辅助；管理仅缓存文件提交经原source短门，fetch/DNS在门外；普通model cache不强绑C-only。
# 模块用途: 拆平HTTPS成功提交、last-good降级和冻结元数据复核，保持原控制文件与锁。
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from ._agents_contract_fields import AgentsContractError, require_digest
from .agents_source_contract import (
    AgentsSourceSnapshot,
    _validate_locator,
    validate_source_snapshot,
)
from .agents_source_files import (
    AGENTS_CONTROL_MAX_BYTES,
    read_source_bytes,
    source_configuration_lock,
    write_control_json,
)
from .agents_source_load_contract import AgentsSourceLoadResult, validate_source_load


# LLM: 与抓取共享较早网络期限；读取和冻结都不能把mtime或缓存自报状态当长期权限。
# 函数用途: 给线程冻结构造同CAS、同正文摘要的安全来源元数据。
def activation_metadata(repository, snapshot):
    from .agents_source_network import source_request_budget
    from .agents_source_read_lock import source_read_boundary
    with source_request_budget(), source_read_boundary(repository.owner_home):
        return _activation_metadata(repository, snapshot)


# LLM: 正文不复制到metadata；本地FD身份必须匹配原配置授权且读前/读后CAS一致，不能同字节重绑；
# URL跳转链来自完整验证的唯一last-good记录，不为默认只读访问创建控制件。
# 函数用途: 核对当前CAS和本次正文后给出权限复核所需信息。
def _activation_metadata(repository, snapshot):
    current, record = repository._configuration_record()
    validate_source_snapshot(current, repository.owner, snapshot.configuration_cas)
    repository.require_permission(current)
    metadata = {"snapshot": snapshot.to_dict(), "identity": "", "checked_urls": []}
    if snapshot.kind != "url":
        raw, identity = _activation_local_bytes(repository, current, record)
        if sha256(raw).hexdigest() != snapshot.content_sha256:
            raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")
        validate_source_snapshot(repository.configuration(), repository.owner, snapshot.configuration_cas)
        repository.require_permission(current)
        metadata["identity"] = identity
        return metadata
    record, result = last_good_record(repository, snapshot)
    if result is None:
        raise AgentsContractError("AGENTS_SOURCE_CACHE_INVALID", "last_good_ref")
    if result.snapshot.content_sha256 != snapshot.content_sha256:
        raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")
    metadata["checked_urls"] = record["checked_urls"]
    return metadata


# LLM: 共用唯一仓库的原配置身份核验；只有无配置默认缺失仍为空，显式来源消失转固定诊断。
# 函数用途: 从当前安全FD读回冻结所需的本地身份与原字节。
def _activation_local_bytes(repository, snapshot, record):
    try:
        return repository._read_local(snapshot, record)
    except FileNotFoundError:
        raise AgentsContractError("AGENTS_SOURCE_MISSING", "locator") from None


# LLM: active允许保留旧CAS直到刷新点，但每次都重核当前权限；共享期限约束所有旧redirect DNS。
# 函数用途: 核验持久active元数据仍允许注入，不激活pending来源。
def require_active_permission(repository, metadata):
    from .agents_source_network import source_request_budget
    with source_request_budget():
        _require_active_permission(repository, metadata)


# LLM: 不可用/坏形状的metadata不能授予正文；旧无metadata只代表当前owner的历史默认源。
# 函数用途: 拒绝坏active投影，并重核路径身份或完整跳转链。
def _require_active_permission(repository, metadata):
    if metadata is None:
        repository.require_permission(repository._default_configuration())
        return
    if type(metadata) is not dict or set(metadata) != {"snapshot", "identity", "checked_urls"}:
        raise AgentsContractError("AGENTS_SOURCE_RESULT_INVALID", "activation")
    snapshot = AgentsSourceSnapshot.from_dict(metadata["snapshot"], repository.owner)
    identity = metadata["identity"]
    if snapshot.status not in {"ready", "cached"} or type(identity) is not str:
        raise AgentsContractError("AGENTS_SOURCE_RESULT_INVALID", "activation")
    if snapshot.kind == "url" and identity != "":
        raise AgentsContractError("AGENTS_SOURCE_RESULT_INVALID", "activation")
    if snapshot.kind == "local_path" and not identity:
        raise AgentsContractError("AGENTS_SOURCE_RESULT_INVALID", "activation")
    repository.require_permission(snapshot)
    if snapshot.kind == "url":
        urls = _checked_urls(metadata["checked_urls"], snapshot.locator)
        for url in urls:
            repository._validate_url(url)
        return
    if metadata["checked_urls"] != [] or type(metadata["checked_urls"]) is not list:
        raise AgentsContractError("AGENTS_SOURCE_RESULT_INVALID", "activation")
    if identity:
        require_digest(identity, "identity")
        read_source_bytes(Path(snapshot.locator), expected_identity=identity)


# LLM: 安全失败不消费缓存；HTTP技术失败进入唯一同owner/CAS/当前许可缓存验证路径。
# 函数用途: 在配置锁外抓取，完整成功才回锁提交last-good。
def load_url(repository, snapshot):
    try:
        return _fetch_and_commit(repository, snapshot)
    except (AgentsContractError, OSError, TimeoutError) as error:
        code = error.code if isinstance(error, AgentsContractError) else "AGENTS_SOURCE_FETCH_FAILED"
        if code not in {"AGENTS_SOURCE_FETCH_FAILED", "AGENTS_SOURCE_TIMEOUT", "AGENTS_SOURCE_HTTP_FAILED"}:
            return repository._unavailable(snapshot, code)
        return _technical_fallback(repository, snapshot, code)


# LLM: 在同一十五秒总期限内为降级复核留三秒；抓取deadline到期不能让合法缓存永远没有DNS重核机会。
# 函数用途: 缩短抓取子阶段预算，不重起总网络期限。
def _fetch_and_commit(repository, snapshot):
    from .agents_source_network import source_request_budget
    with source_request_budget(reserve_seconds=3):
        return _fetch_with_reserved_budget(repository, snapshot)


# LLM: 门外传输/安全扫描后取原A锁；管理锁FD出生/最终cache file各经原commit，原CAS/每跳许可仍重核。
# 函数用途: 取得当前来源原文并原子提交唯一owner缓存。
def _fetch_with_reserved_budget(repository, snapshot):
    fetcher = repository._fetcher
    if fetcher is None:
        from .agents_source_network import load_agents_url
        fetcher = load_agents_url
    from .agents_source_network import AgentsFetchBudget
    response = fetcher(snapshot.locator, url_allowed=repository._url_allowed, budget=AgentsFetchBudget())
    if type(response.body) is not bytes or len(response.body) > 80_000:
        raise AgentsContractError("AGENTS_SOURCE_TOO_LARGE", "content")
    urls = _checked_urls(response.checked_urls, snapshot.locator, expected_type=tuple)
    result = repository._ready(snapshot, response.body, last_good_ref=str(repository.last_good_path))
    with source_configuration_lock(repository.owner_home, file_commit=repository._file_commit):
        _commit_last_good_locked(repository, snapshot, result, urls)
    return result


# LLM: 原A锁内先在C短门外重做DNS/redirect许可，真正cache commit重核同CAS/当前策略，撤权不落盘。
# 函数用途: 锁内核对后提交最后一次完整成功正文。
def _commit_last_good_locked(repository, snapshot, result, urls):
    current = repository.configuration()
    validate_source_snapshot(current, repository.owner, snapshot.configuration_cas)
    repository.require_permission(current)
    for url in urls:
        repository._validate_url(url)
    # LLM: 原URL/DNS已在门外校验；最后只读同CAS/当前每跳策略并写有界唯一cache，不能续旧抓取许可。
    # 函数用途: 在原管理source短窗保存这次完整响应。
    def commit():
        validate_source_snapshot(repository.configuration(), repository.owner, snapshot.configuration_cas)
        repository.require_permission(snapshot, validate_network=False)
        if any(not repository._url_allowed(url) for url in urls):
            raise AgentsContractError("AGENTS_SOURCE_PERMISSION_REVOKED", "locator")
        write_control_json(repository.owner_home, repository.last_good_path.name,
                           dict(schema_version=1, result=result.to_dict(), checked_urls=list(urls)))
    repository.commit_file(commit)


# LLM: 不换来源不越owner；保留原成功时间和摘要，失败只改状态而不覆盖磁盘成功记录。
# 函数用途: 将技术故障转换为经重新授权的同CAS陈旧缓存，或无正文固定码。
def _technical_fallback(repository, snapshot, code):
    try:
        current = repository.configuration()
        validate_source_snapshot(current, repository.owner, snapshot.configuration_cas)
        repository.require_permission(current)
        cached = repository._read_last_good(current)
    except AgentsContractError as denied:
        return repository._unavailable(snapshot, denied.code)
    if cached is None:
        return repository._unavailable(snapshot, code)
    stale = replace(cached.snapshot, status="cached", failure_code=code, used_cache=True, stale=True)
    return validate_source_load(AgentsSourceLoadResult(stale, cached.content), repository.owner, snapshot.configuration_cas)


# LLM: 严格nofollow/有界，唯一缓存解析器供降级和activation共用；坏内容一律固定错误码。
# 函数用途: 读取完整核验的last-good记录及正文，无文件时返回缺失。
def last_good_record(repository, snapshot):
    from .agents_source_network import source_request_budget
    with source_request_budget():
        return _last_good_record(repository, snapshot)


# LLM: 原owner、完整CAS、摘要、时间、每跳当前许可和扫描全部重核，不能把磁盘自报数据当授权。
# 函数用途: 将原控制件解析成当前来源可消费的成功结果。
def _last_good_record(repository, snapshot):
    try:
        raw, _ = read_source_bytes(repository.last_good_path, limit=AGENTS_CONTROL_MAX_BYTES)
    except FileNotFoundError:
        return None, None
    try:
        record = json.loads(raw.decode("utf-8"))
        return _validate_cache_record(repository, snapshot, record)
    except AgentsContractError:
        raise
    except (ValueError, TypeError, UnicodeError, KeyError) as error:
        raise AgentsContractError("AGENTS_SOURCE_CACHE_INVALID", "last_good_ref") from error


# LLM: 成功记录类型和所有元数据按冻结v1验证，URL字节帽不被本地2MiB帽放宽。
# 函数用途: 精确核验缓存结构后重做安全检查并返回同一原记录。
def _validate_cache_record(repository, snapshot, record):
    result, urls = _cache_contents(repository, snapshot, record)
    repository.require_permission(snapshot)
    for url in urls:
        repository._validate_url(url)
    repository._scan(result.content)
    return record, result


# LLM: 原cache schema/owner/CAS/正文SHA由唯一DTO核；这里只解析有界记录，不做网络或签许可，所有读者共用。
# 函数用途: 拆出缓存完整结构检查供普通加载与最终file CAS比较共同使用。
def _cache_contents(repository, snapshot, record):
    if type(record) is not dict or set(record) != {"schema_version", "result", "checked_urls"}:
        raise ValueError("cache schema")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("cache version")
    result = AgentsSourceLoadResult.from_dict(record["result"], repository.owner)
    validate_source_load(result, repository.owner, snapshot.configuration_cas)
    if result.snapshot.status != "ready" or result.snapshot.last_good_ref != str(repository.last_good_path):
        raise ValueError("cache result")
    if len(result.content.encode()) > 80_000:
        raise ValueError("cache size")
    return result, _checked_urls(record["checked_urls"], snapshot.locator)


# LLM: 仅管理最终配置file事务比较旧cache前像；DNS已在门外原验证，不返回可执行正文，不延长网络许可。
# 函数用途: 在原短窗口重新读取同owner/CAS/正文及当前每跳策略，拒绝退役期间被改的cache。
def require_saved_source_digest(repository, snapshot, expected):
    raw, _ = read_source_bytes(repository.last_good_path, limit=AGENTS_CONTROL_MAX_BYTES)
    try:
        result, urls = _cache_contents(repository, snapshot, json.loads(raw.decode('utf-8')))
    except (ValueError, TypeError, UnicodeError, KeyError) as error:
        raise AgentsContractError('AGENTS_SOURCE_CACHE_INVALID', 'last_good_ref') from error
    repository.require_permission(snapshot, validate_network=False)
    if any(not repository._url_allowed(url) for url in urls):
        raise AgentsContractError('AGENTS_SOURCE_PERMISSION_REVOKED', 'locator')
    repository._scan(result.content)
    if result.snapshot.content_sha256 != expected:
        raise AgentsContractError('AGENTS_SOURCE_CONTENT_MISMATCH', 'content_sha256')


# LLM: 长度、首URL和每项词法严格校验，不能让注入的validator接受坏类型；不授予网络许可。
# 函数用途: 核验成功或缓存的完整有界跳转链。
def _checked_urls(urls, locator, *, expected_type=list):
    if type(urls) is not expected_type or not urls or urls[0] != locator or len(urls) > 10:
        raise AgentsContractError("AGENTS_SOURCE_RESULT_INVALID", "checked_urls")
    for url in urls:
        _validate_locator("url", url)
    return urls
