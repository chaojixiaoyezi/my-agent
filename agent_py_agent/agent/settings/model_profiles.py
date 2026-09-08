# LLM: 本模块是 owner 私有模型配置的唯一文件源；只处理显式菜单操作，密钥不进入公开投影或异常正文。
# 模块用途: 安全保存模型接口、地址、密钥和上下文窗口，并让用户选择后续工作使用的模型。

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from ..common.json_io import locked_json_path

_SCHEMA = "owner_model_profiles.v1"
_BACKENDS = {"openai_compatible", "anthropic_compatible"}


# LLM: 只有这个类型的固定验证文案可以经 HTTP 公开；其它异常必须统一脱敏。
# 类用途: 表示可安全显示给用户的模型配置错误。
class ModelProfileError(ValueError):
    pass


# LLM: 路径只由可信 home/owner 身份决定，不接受客户端指定路径或从模型名拼文件名。
# 函数用途: 返回当前用户配置文件的位置，不创建目录。
def model_profiles_path(home_paths: object) -> Path:
    identity = [str(getattr(home_paths, key, "")) for key in ("owner_provider", "owner_kind", "owner_id")]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
    return Path(home_paths.config_dir) / "model-profiles" / f"{digest}.json"


# LLM: 字段验证只检查类型与协议格式，不发请求，不把错误值（可能含密钥）拼进错误信息。
# 函数用途: 校验用户填写的模型信息，保留大小写敏感的模型名称。
def validate_model_profile(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("model_backend") not in _BACKENDS:
        raise ModelProfileError("请选择 OpenAI 或 Anthropic 接口；Auth 暂未开放。")
    row = {key: str(value.get(key) or "").strip() for key in ("model_name", "api_base", "api_key")}
    if any(not item or len(item) > 4096 or any(ord(c) < 32 for c in item) for item in row.values()):
        raise ModelProfileError("模型名称、地址和密钥不能为空，且不能包含换行或控制字符。")
    try:
        url = urlsplit(row["api_base"])
        valid_url = url.scheme in {"http", "https"} and bool(url.hostname) and not (
            url.username or url.password or url.query or url.fragment
        )
        _ = url.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ModelProfileError("地址须为 http(s) 接口基础地址，不要包含密钥、查询参数或片段。")
    window = value.get("model_context_window_tokens")
    if isinstance(window, bool) or not str(window).isascii() or not str(window).isdigit() or not 4096 <= int(window) <= 2**31 - 1:
        raise ModelProfileError("上下文窗口请填写 4096 至 2147483647 之间的整数 tokens。")
    row.update(model_backend=value["model_backend"], model_context_window_tokens=int(window))
    row["api_base"] = row["api_base"].rstrip("/")
    return row


# LLM: 文件损坏必须显式拒绝，优化模式也校验结构；读取只投影声明字段，不回传文件中的额外秘密。
# 函数用途: 读取完整私有配置，仅供宿主执行与菜单保存使用。
def read_model_profiles(path: Path) -> dict:
    if not path.exists():
        return {"schema": _SCHEMA, "selected": "default", "profiles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema"] != _SCHEMA or not isinstance(data["profiles"], dict):
            raise ModelProfileError("模型配置结构无效。")
        for profile_id, row in data["profiles"].items():
            UUID(profile_id)
            data["profiles"][profile_id] = validate_model_profile(row)
        if data["selected"] != "default" and data["selected"] not in data["profiles"]:
            raise ModelProfileError("模型配置选择无效。")
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelProfileError("模型配置文件损坏，未覆盖已有配置。") from exc
    return data


# LLM: 临时文件在写入第一个 secret 字节之前即为 0600；同目录 replace 保证断电/并发不会留下半份配置。
# 函数用途: 原子保存私有模型配置，失败时保留原文件并清理临时文件。
def _save_profiles(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".models-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


# LLM: 列表仅公开非秘密字段和 has_key，不返回 key/env/config path，默认项来自实际部署快照。
# 函数用途: 给菜单显示当前选择与已有模型，包括不改写原 YAML 的部署默认选项。
def public_model_profiles(data: dict, config: object) -> dict:
    default = {key: getattr(config, key, "") for key in (
        "model_backend", "model_name", "model_context_window_tokens",
    )}
    rows = [{"id": "default", **default, "api_base": "（使用部署配置）", "has_key": True}]
    for profile_id, row in data["profiles"].items():
        rows.append({"id": profile_id, **{key: value for key, value in row.items() if key != "api_key"}, "has_key": bool(row["api_key"])})
    return {"ok": True, "selected": data["selected"], "profiles": rows}


# LLM: 写入只接受 add/select；add ID 由客户端生成以支持同一保存重试，异值重用拒绝，选择和新增不互相覆盖。
# 函数用途: 按用户锁读取、增加或选择模型，返回脱敏菜单；没有任何模型或网络调用。
def execute_model_profile_operation(agent: object, operation: str, payload: dict) -> dict:
    path = model_profiles_path(agent.home_paths)
    if operation == "list":
        return public_model_profiles(read_model_profiles(path), agent.config)
    if operation not in {"add", "select"}:
        raise ModelProfileError("不支持的模型配置操作。")
    profile_id = str(payload.get("profile_id") or "")
    if profile_id != "default" or operation == "add":
        try:
            UUID(profile_id)
        except ValueError as exc:
            raise ModelProfileError("模型配置编号无效。") from exc
    row = validate_model_profile(payload.get("profile")) if operation == "add" else None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with locked_json_path(path):
        data = read_model_profiles(path)
        if operation == "add":
            previous = data["profiles"].get(profile_id)
            if previous is not None and previous != row:
                raise ModelProfileError("这个保存编号已被使用，请重新打开新增模型。")
            data["profiles"][profile_id] = row
        elif profile_id == "default" or profile_id in data["profiles"]:
            data["selected"] = profile_id
        else:
            raise ModelProfileError("模型配置不存在，请刷新列表。")
        _save_profiles(path, data)
    return public_model_profiles(data, agent.config)


# LLM: 模型整组字段共享显式选择的来源与优先级；旧 task overlay 不能拆开模型名、地址、密钥和窗口。
# 函数用途: 在新工作片开始时生成模型配置快照，正在运行的旧快照不受后续菜单操作影响。
def selected_model_config(agent: object, *, profile_id: str | None = None):
    data = read_model_profiles(model_profiles_path(agent.home_paths))
    selected = data["selected"] if profile_id is None else profile_id
    if selected == "default":
        return agent.config
    if selected not in data["profiles"]:
        raise ModelProfileError("任务原模型配置已不存在，不能静默换成其它模型。")
    row = data["profiles"][selected]
    config = replace(agent.config, **row, api_key_env="", model_context_window_explicit=True)
    config.max_tokens = min(config.max_tokens, int(row["model_context_window_tokens"]) // 4)
    fields = {*row, "api_key_env", "model_context_window_explicit", "max_tokens"}
    config.config_sources = {**config.config_sources, **{key: {
        "source": "owner_model_profile", "priority": 90, "profile_id": selected,
    } for key in fields}}
    return config


# LLM: 只读取宿主配置来源中的 stable ID；raw params 中的同名属性必须先移除，不能由模型选择凭证。
# 函数用途: 将子代理的创建时模型引用落入属性，重启后仍可恢复原接口与窗口。
def inherit_model_profile(attrs: dict, agent: object) -> None:
    attrs.pop("host_model_profile.v1", None)
    sources = getattr(getattr(agent, "config", None), "config_sources", {})
    source = sources.get("model_name", {}) if isinstance(sources, dict) else {}
    if source.get("source") == "owner_model_profile" and source.get("profile_id"):
        attrs["host_model_profile.v1"] = {"profile_id": source["profile_id"]}


# LLM: 任务经过 owner/dispatch 授权后才能读此引用；不存在或损坏时失败，不借用其它用户配置。
# 函数用途: 给重启后的子代理恢复它创建时的模型，不采用用户后来选择的新模型。
def inherited_model_config(agent: object, task: object):
    attrs = getattr(task, "attributes", {}) or {}
    ref = attrs.get("host_model_profile.v1")
    if ref is None:
        return agent.config
    if not isinstance(ref, dict) or not isinstance(ref.get("profile_id"), str):
        raise ModelProfileError("子代理模型配置引用损坏。")
    return selected_model_config(agent, profile_id=ref["profile_id"])
