# LLM: 该模块迁移上游 production_tool 的作业冻结合同，但不含项目账、子进程或供应商适配器；输入字节只由 SDK 读取上下文取得。
# 模块用途: 规范化媒体作业、冻结输入摘要、生成确认文本并验证私有存储记录。

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import PurePosixPath

from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

from .errors import DramaShellError
from .workspace_io import MAX_INPUT_BYTES, read_workspace_bytes

JOB_SCHEMA = "1.0"
JOB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
SOURCE_ENTRY_RE = re.compile(r"[A-Z][A-Z0-9-]{1,99}")
REF_SLOT_RE = re.compile(r"REF-[A-Z0-9][A-Z0-9-]{0,79}")
ALLOWED_JOB_KEYS = {
    "schema_version", "job_id", "modality", "adapter", "prompt", "source",
    "source_entry", "references", "reference_bindings", "outputs", "parameters", "overwrite",
}
EXECUTION_KEYS = ALLOWED_JOB_KEYS | {"inputs"}
STORED_JOB_KEYS = EXECUTION_KEYS | {"fingerprint", "prepared_at"}
SECRET_KEYS = {
    "authorization", "credential", "credentials", "password", "secret", "token",
    "access_token", "api_key", "apikey",
}
MEDIA_EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".webp"},
    "video": {".mp4", ".mov", ".webm"},
    "tts": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".opus"},
    "music": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".opus"},
}
MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".opus": "audio/ogg",
}
CREATOR_SOURCE_NAMES = {"图片提示词.md": "image", "视频提示词.md": "video", "分镜.md": "image"}


# LLM: prepare 的指纹只覆盖会影响执行的规范化字段和当前输入摘要；时间戳不参与，重复准备同内容得到同指纹。
# 函数用途: 校验原始作业并冻结为可确认的私有记录。
def normalize_job(raw: object, context: WorkspaceReadContext) -> dict:
    if not isinstance(raw, Mapping) or set(raw) - ALLOWED_JOB_KEYS:
        raise DramaShellError("INVALID_JOB", "作业含不支持的字段。")
    scalar = _normalize_scalars(raw)
    bindings = _normalize_reference_bindings(raw.get("reference_bindings"))
    references = _normalize_references(raw, bindings)
    outputs = _normalize_outputs(raw.get("outputs"), scalar["modality"])
    parameters = raw.get("parameters", {})
    if not isinstance(parameters, Mapping) or _contains_secret_key(parameters):
        raise DramaShellError("INVALID_JOB", "作业参数必须是对象且不能含凭据或密钥。")
    try:
        parameter_bytes = canonical(dict(parameters))
    except (TypeError, ValueError) as exc:
        raise DramaShellError("INVALID_JOB", "作业参数必须是有限 JSON 值。") from exc
    if len(parameter_bytes) > 64 * 1024:
        raise DramaShellError("INVALID_JOB", "作业参数超过 64 KiB。")
    input_paths = ([scalar["source"]] if scalar["source"] is not None else []) + references
    if len(input_paths) != len(set(input_paths)):
        raise DramaShellError("INVALID_JOB", "source 和 references 不能重复。")
    execution = {
        "schema_version": JOB_SCHEMA, **scalar, "references": references,
        "reference_bindings": bindings, "outputs": outputs, "parameters": dict(parameters),
        "overwrite": _boolean(raw.get("overwrite", False), "overwrite"),
        "inputs": _hash_inputs(input_paths, context),
    }
    execution["fingerprint"] = sha256_bytes(canonical(execution))
    execution["prepared_at"] = utc_now()
    return execution


# LLM: 私有记录可能跨进程读取，必须重新核对完整字段集和指纹，不能因为目录受管就盲目信任正文。
# 函数用途: 验证私有存储中的作业记录并返回独立副本。
def validate_stored_job(document: object, expected_job_id: str | None = None) -> dict:
    if not isinstance(document, dict) or set(document) != STORED_JOB_KEYS:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业记录字段无效。")
    job_id = document.get("job_id")
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业编号无效。")
    if expected_job_id is not None and job_id != expected_job_id:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业编号与请求不一致。")
    if document.get("schema_version") != JOB_SCHEMA:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业版本无效。")
    fingerprint = document.get("fingerprint")
    execution = {key: document[key] for key in EXECUTION_KEYS}
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业指纹无效。")
    try:
        expected = sha256_bytes(canonical(execution))
    except (TypeError, ValueError) as exc:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业正文不是有限 JSON。") from exc
    if fingerprint != expected:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业指纹不匹配。")
    _validate_stored_shape(document)
    return dict(document)


# LLM: 预览只暴露确认所需的冻结业务字段，不返回输入摘要或插件私有目录信息。
# 函数用途: 生成 prepare 返回值和精确确认文本。
def preview(job: Mapping[str, object]) -> dict:
    return {
        "job_id": job["job_id"], "modality": job["modality"], "adapter": job["adapter"],
        "count": len(job["outputs"]), "prompt": job["prompt"], "source": job["source"],
        "source_entry": job["source_entry"], "references": job["references"],
        "reference_bindings": job["reference_bindings"], "outputs": job["outputs"],
        "parameters": job["parameters"], "overwrite": job["overwrite"],
        "confirmation": confirmation_text(job), "state": "needs_confirmation",
        "generation_success": False,
    }


# LLM: 确认文本绑定完整执行指纹；任何作业字段或输入字节变化都必须重新 prepare 和 confirm。
# 函数用途: 为一份冻结作业生成精确、可人工核对的确认短句。
def confirmation_text(job: Mapping[str, object]) -> str:
    return f"CONFIRM {job['job_id']} {str(job['fingerprint'])[:12]}"


# LLM: status/run 重新读取每个已冻结输入并比较摘要，防止确认后替换来源或参考文件。
# 函数用途: 判断当前工作区输入是否仍与 prepare 时完全相同。
def inputs_current(job: Mapping[str, object], context: WorkspaceReadContext) -> bool:
    inputs = job.get("inputs")
    if not isinstance(inputs, Mapping):
        return False
    try:
        actual = _hash_inputs(list(inputs), context)
    except DramaShellError:
        return False
    return actual == dict(inputs)


# LLM: 规范编码是确认指纹和记录校验的唯一序列化形式；禁止 NaN 以避免跨实现摘要漂移。
# 函数用途: 把 JSON 值编码成稳定 UTF-8 字节。
def canonical(document: object) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


# LLM: 摘要只处理已经有界读取的字节，不重新打开路径。
# 函数用途: 计算稳定的 SHA-256 十六进制摘要。
def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# LLM: 所有作业和运行时间都写明确 UTC，供状态排序和审计复核。
# 函数用途: 返回 ISO-8601 UTC 时间戳。
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# LLM: 标量检查保留上游边界，供应商名称暂可记录但只有 fixture 能在 run 阶段执行。
# 函数用途: 校验编号、模态、适配器、提示词和可选来源字段。
def _normalize_scalars(raw: Mapping) -> dict:
    job_id, modality, adapter, prompt = raw.get("job_id"), raw.get("modality"), raw.get("adapter"), raw.get("prompt")
    if raw.get("schema_version", JOB_SCHEMA) != JOB_SCHEMA:
        raise DramaShellError("INVALID_JOB", "不支持的作业版本。")
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise DramaShellError("INVALID_JOB", "job_id 必须是 1 至 80 位便携标识。")
    if modality not in MEDIA_EXTENSIONS:
        raise DramaShellError("INVALID_JOB", "modality 必须是 image、video、tts 或 music。")
    if not isinstance(adapter, str) or JOB_ID_RE.fullmatch(adapter) is None:
        raise DramaShellError("INVALID_JOB", "adapter 必须是便携名称。")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
        raise DramaShellError("INVALID_JOB", "prompt 必须非空且不超过 100000 字符。")
    source = _relative_path(raw["source"], "source") if raw.get("source") is not None else None
    source_entry = raw.get("source_entry")
    _check_source_entry(source, source_entry, str(modality))
    return {"job_id": job_id, "modality": modality, "adapter": adapter, "prompt": prompt,
            "source": source, "source_entry": source_entry}


# LLM: creator Markdown 入口仍保持上游模态绑定，避免 image/video 条目串用；本批不迁移其正文解析器。
# 函数用途: 校验 source_entry 与规范创作来源路径的关系。
def _check_source_entry(source: str | None, entry: object, modality: str) -> None:
    if entry is not None and (not isinstance(entry, str) or SOURCE_ENTRY_RE.fullmatch(entry) is None):
        raise DramaShellError("INVALID_JOB", "source_entry 必须是可见的大写 Markdown 条目编号。")
    if entry is not None and source is None:
        raise DramaShellError("INVALID_JOB", "source_entry 必须同时提供 source。")
    source_modality = _creator_source_modality(source)
    if source_modality == modality and entry is None:
        raise DramaShellError("INVALID_JOB", "规范创作 Markdown 来源必须指定 source_entry。")
    music_from_video = source_modality == "video" and modality == "music" and entry is None
    if source_modality is not None and source_modality != modality and not music_from_video:
        raise DramaShellError("INVALID_JOB", "创作 Markdown 来源与作业模态不匹配。")
    if entry is not None and source_modality != modality:
        raise DramaShellError("INVALID_JOB", "source_entry 与来源路径或模态不匹配。")
    source_name = PurePosixPath(source).name if source is not None else ""
    prefixes = {"图片提示词.md": "IMG-", "视频提示词.md": "MOTION-", "分镜.md": "SHOT-"}
    if entry is not None and not entry.startswith(prefixes.get(source_name, "!")):
        raise DramaShellError("INVALID_JOB", "source_entry 与作业模态不匹配。")


# LLM: 引用顺序以 binding 为权威，显式 references 存在时必须逐项一致，避免适配器看到两套输入顺序。
# 函数用途: 规范化 references 并与 reference_bindings 对齐。
def _normalize_references(raw: Mapping, bindings: list[dict]) -> list[str]:
    supplied = [_relative_path(item, "references") for item in _string_list(raw.get("references", []), "references", 16)]
    bound = [str(item["path"]) for item in bindings]
    if "references" in raw and bindings and supplied != bound:
        raise DramaShellError("INVALID_JOB", "references 必须与 reference_bindings 顺序一致。")
    return bound if bindings else supplied


# LLM: 输出路径保留上游专用 production 目录和模态后缀边界；最终能否写仍由每次 SDK 写入上下文决定。
# 函数用途: 校验唯一输出路径及其媒体扩展名。
def _normalize_outputs(value: object, modality: str) -> list[str]:
    outputs = [_relative_path(item, "outputs", True) for item in _string_list(value, "outputs", 16)]
    if not outputs or len(outputs) != len(set(outputs)):
        raise DramaShellError("INVALID_JOB", "outputs 必须包含唯一目标路径。")
    for output in outputs:
        if PurePosixPath(output).suffix.casefold() not in MEDIA_EXTENSIONS[modality]:
            raise DramaShellError("INVALID_JOB", f"输出扩展名与 {modality} 不匹配。")
    return outputs


# LLM: binding 字段、连续顺序和互斥控制范围照上游收紧，防止同一参考图被赋予冲突职责。
# 函数用途: 规范化有序参考绑定列表。
def _normalize_reference_bindings(value: object) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise DramaShellError("INVALID_JOB", "reference_bindings 最多包含 16 项。")
    normalized = [_normalize_binding(item, index) for index, item in enumerate(value, 1)]
    normalized.sort(key=lambda item: item["order"])
    if [item["order"] for item in normalized] != list(range(1, len(normalized) + 1)):
        raise DramaShellError("INVALID_JOB", "参考绑定顺序必须从 1 连续递增。")
    for field in ("slot_id", "path"):
        values = [item[field] for item in normalized]
        if len(values) != len(set(values)):
            raise DramaShellError("INVALID_JOB", f"参考绑定 {field} 不得重复。")
    return normalized


# LLM: 单项校验不读取提示词自然语言作判断，只检查明示结构化字段。
# 函数用途: 校验并规范化一项参考绑定。
def _normalize_binding(value: object, index: int) -> dict:
    fields = {"slot_id", "order", "path", "label", "role", "may_control", "must_not_control"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise DramaShellError("INVALID_JOB", f"reference_bindings[{index}] 字段无效。")
    slot, order, label, role = value.get("slot_id"), value.get("order"), value.get("label"), value.get("role")
    if not isinstance(slot, str) or REF_SLOT_RE.fullmatch(slot) is None:
        raise DramaShellError("INVALID_JOB", f"reference_bindings[{index}] slot_id 无效。")
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        raise DramaShellError("INVALID_JOB", f"reference_bindings[{index}] order 无效。")
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        raise DramaShellError("INVALID_JOB", f"reference_bindings[{index}] label 无效。")
    if not isinstance(role, str) or not role.strip() or len(role) > 80:
        raise DramaShellError("INVALID_JOB", f"reference_bindings[{index}] role 无效。")
    may, must_not = _scope_list(value.get("may_control")), _scope_list(value.get("must_not_control"))
    if {item.casefold() for item in may} & {item.casefold() for item in must_not}:
        raise DramaShellError("INVALID_JOB", f"reference_bindings[{index}] 控制范围冲突。")
    return {"slot_id": slot, "order": order, "path": _relative_path(value.get("path"), "binding path"),
            "label": label.strip(), "role": role.strip(), "may_control": may, "must_not_control": must_not}


# LLM: 工作区路径只能是便携相对路径；输出另外限制到生产目录，权限上下文不能替代这个业务边界。
# 函数用途: 规范化一个输入或输出相对路径。
def _relative_path(value: object, label: str, output: bool = False) -> str:
    if not isinstance(value, str):
        raise DramaShellError("INVALID_JOB", f"{label} 路径必须是字符串。")
    raw, pure = value.replace("\\", "/"), PurePosixPath(value.replace("\\", "/"))
    if not raw or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise DramaShellError("INVALID_JOB", f"{label} 不是安全相对路径。")
    if pure.parts[0].casefold() == ".short-drama" or pure.name.casefold() == "short-drama.json":
        raise DramaShellError("INVALID_JOB", f"{label} 不能指向上游操作元数据。")
    if output:
        top = len(pure.parts) >= 2 and pure.parts[0].casefold() == "production"
        episode = (len(pure.parts) >= 4 and pure.parts[0] in {"剧集", "episodes"}
                   and re.fullmatch(r"EP\d{3,}", pure.parts[1], re.IGNORECASE)
                   and pure.parts[2] in {"制作成果", "production"})
        if not top and not episode:
            raise DramaShellError("INVALID_JOB", "媒体输出必须位于 production/ 或剧集制作成果目录。")
    return pure.as_posix()


# LLM: 只接受真正列表，避免字符串被逐字符当路径；边界用于限制一次作业的文件扇出。
# 函数用途: 校验有上限的字符串列表。
def _string_list(value: object, label: str, limit: int) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DramaShellError("INVALID_JOB", f"{label} 必须是字符串列表。")
    if len(value) > limit:
        raise DramaShellError("INVALID_JOB", f"{label} 条目过多。")
    return list(value)


# LLM: 控制范围必须非空、有界且大小写不重复，供机器按结构字段判断。
# 函数用途: 规范化 may_control 或 must_not_control 列表。
def _scope_list(value: object) -> list[str]:
    items = _string_list(value, "control scope", 32)
    if not items or any(not item.strip() or len(item) > 200 for item in items):
        raise DramaShellError("INVALID_JOB", "控制范围必须包含非空有界文本。")
    normalized = [item.strip() for item in items]
    if len({item.casefold() for item in normalized}) != len(normalized):
        raise DramaShellError("INVALID_JOB", "控制范围不得重复。")
    return normalized


# LLM: 凭据键在任意 JSON 深度都拒绝，避免插件私有记录意外保存供应商秘密。
# 函数用途: 递归判断参数中是否含敏感键。
def _contains_secret_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(str(key).casefold() in SECRET_KEYS or _contains_secret_key(child)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_secret_key(child) for child in value)
    return False


# LLM: 哈希前每个路径都经过同一次调用的读取上下文和 SDK no-follow 读取，不能缓存或直读文件系统。
# 函数用途: 计算全部作业输入的有界 SHA-256 映射。
def _hash_inputs(paths: list[str], context: WorkspaceReadContext) -> dict[str, str]:
    return {path: sha256_bytes(read_workspace_bytes(context, path, MAX_INPUT_BYTES)) for path in paths}


# LLM: stored shape 是防私有记录被篡改后的第二层检查；不重新读取工作区，所以状态还能报告 needs_reconfirmation。
# 函数用途: 校验存储记录的关键类型、输出后缀和输入摘要格式。
def _validate_stored_shape(job: Mapping) -> None:
    if job.get("modality") not in MEDIA_EXTENSIONS:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业模态无效。")
    if not isinstance(job.get("adapter"), str) or JOB_ID_RE.fullmatch(str(job["adapter"])) is None:
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业适配器无效。")
    outputs, inputs = job.get("outputs"), job.get("inputs")
    if not isinstance(outputs, list) or not outputs or not all(isinstance(item, str) for item in outputs):
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业输出无效。")
    if not isinstance(inputs, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                           or re.fullmatch(r"[0-9a-f]{64}", value) is None
                                           for key, value in inputs.items()):
        raise DramaShellError("PRIVATE_RECORD_INVALID", "私有作业输入摘要无效。")


# LLM: Python bool 是 int 子类，必须显式排除其他类型，保持 JSON 合同清晰。
# 函数用途: 校验一个严格布尔字段。
def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise DramaShellError("INVALID_JOB", f"{label} 必须是布尔值。")
    return value


# LLM: 只识别上游三种规范创作 Markdown 路径，其他来源保持普通输入文件语义。
# 函数用途: 判断来源文件是否绑定特定媒体模态。
def _creator_source_modality(source: str | None) -> str | None:
    if source is None:
        return None
    path = PurePosixPath(source)
    if len(path.parts) != 3 or path.parts[0] not in {"剧集", "episodes"}:
        return None
    return CREATOR_SOURCE_NAMES.get(path.name)
