# LLM: 唯一来源仓库的配置解析辅助；不持另一个仓库实例，不授予权限，所有结果交回原repository。
# 模块用途: 有界严格读回owner配置并精确验证唯一记录。
import json
import math
from dataclasses import replace

from ._agents_contract_fields import AgentsContractError, require_digest, require_text
from .agents_source_contract import validate_source_snapshot
from .agents_source_files import AGENTS_CONTROL_MAX_BYTES, read_source_bytes

_CONFIG_KEYS = frozenset(("schema_version", "owner", "kind", "value", "revision", "updated_by", "updated_at", "identity"))


# LLM: 来源配置必须严格FD读取；只有不存在才返回revision0默认，坏配置不能fallback默认正文。
# 函数用途: 从owner唯一权威记录取得配置及完整解析记录。
def configuration_record(repository):
    base = repository._default_configuration()
    try:
        raw, _ = read_source_bytes(repository.config_path, limit=AGENTS_CONTROL_MAX_BYTES)
    except FileNotFoundError:
        return base, None
    try:
        return _parse_configuration(repository, raw, base)
    except (ValueError, UnicodeError, KeyError, TypeError) as error:
        if isinstance(error, AgentsContractError) and error.code == "AGENTS_SOURCE_OWNER_MISMATCH":
            raise
        raise AgentsContractError("AGENTS_SOURCE_CONFIG_INVALID", "configuration") from error


# LLM: 字段集合及类型精确匹配v1；owner由宿主repository给定，不信配置内自报身份。
# 函数用途: 核对配置schema、owner、revision、时间、定位和身份。
def _parse_configuration(repository, raw: bytes, base):
    record = json.loads(raw.decode("utf-8"))
    if type(record) is not dict or set(record) != _CONFIG_KEYS:
        raise ValueError("bad configuration")
    if record["owner"] != repository.owner:
        raise AgentsContractError("AGENTS_SOURCE_OWNER_MISMATCH", "owner")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("bad schema")
    if type(record["revision"]) is not int or record["revision"] <= 0:
        raise ValueError("bad revision")
    require_text(record["updated_by"], "updated_by")
    if type(record["updated_at"]) is not float or not math.isfinite(record["updated_at"]) or record["updated_at"] <= 0:
        raise ValueError("bad time")
    if record["kind"] != "url":
        require_digest(record["identity"], "identity")
    elif record["identity"] != "":
        raise ValueError("bad identity")
    current = replace(base, kind=record["kind"], locator=record["value"], revision=record["revision"])
    validate_source_snapshot(current, repository.owner)
    if current.kind == "owner_default" and current.locator != str(repository.default_path):
        raise AgentsContractError("AGENTS_SOURCE_OWNER_MISMATCH", "locator")
    return current, record
