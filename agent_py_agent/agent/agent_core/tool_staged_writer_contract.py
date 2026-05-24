# LLM: Staged writer contract guard enforces artifact builder/writer channels before tools mutate files.
# 模块用途: 根据 delivery_contract 的结构化 staging 字段阻止普通文件工具绕过 checkpoint/builder 工具。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..tools import ToolExecutionResult
from ._runtime_params import ToolLoopExecuteParams

_DIRECT_FILE_WRITE_TOOLS = {"append_file", "file_write_session", "replace_in_file", "write_file"}
_STRUCTURED_JSON_WRITER = "write_structured_json"
_STRUCTURED_JSON_SOURCE_WRITERS = frozenset({_STRUCTURED_JSON_WRITER, "api_json_collection"})
_JSON_SOURCE_KEYS = ("source_json_ref",)
_KNOWN_BUILDER_OUTPUT_KEYS = ("workbook_ref", "pdf_ref", "output_ref", "artifact_ref")
_READ_ONLY_TOOL_PREFIXES = ("read", "list", "search", "fetch", "http", "web_search")
_WRITE_INTENT_FRAGMENTS = ("write", "create", "build", "export", "save", "convert", "render")


# LLM: WriterContractIndex keeps path-to-tool facts separate from error rendering.
# 类用途: 保存 staged artifact 路径与必须使用的工具集合，避免后续从 prompt 文案推断写法。
@dataclass(frozen=True)
class WriterContractIndex:
    required_tools_by_ref: dict[str, set[str]] = field(default_factory=dict)

    # LLM: add 是 agent_py_agent/agent/agent_core/tool_staged_writer_contract.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
    # 函数用途: 处理 add 相关的结构化数据、路径或 finding，供当前合同链路调用。
    def add(self, ref: object, tool_name: object) -> None:
        ref_text = _ref_text(ref)
        tool_text = _tool_text(tool_name)
        if not ref_text or not tool_text:
            return
        self.required_tools_by_ref.setdefault(ref_text, set()).add(tool_text)


# LLM: staged_writer_contract_result blocks a mismatched tool before the registry executes it.
# 函数用途: 若 delivery_contract 声明某路径必须由 writer/builder 工具生成，则其它写入工具不能改该路径。
def staged_writer_contract_result(
    params: ToolLoopExecuteParams,
    payload: dict[str, object],
) -> ToolExecutionResult | None:
    if not isinstance(payload, dict):
        return None
    tool = _tool_text(payload.get("tool"))
    target_path = _target_path_for_tool(tool, payload)
    if not tool or not target_path:
        return None

    index = _writer_contract_index(_delivery_contract(params))
    for ref, required_tools in index.required_tools_by_ref.items():
        if tool in required_tools or not _same_path_ref(target_path, ref):
            continue
        return ToolExecutionResult(
            tool,
            False,
            (
                "STAGED_WRITER_TOOL_MISMATCH: staged target requires a declared writer/builder tool. "
                f"target={target_path} required_tool={_display_required_tools(required_tools)} actual_tool={tool}"
            ),
            error_code="STAGED_WRITER_TOOL_MISMATCH",
            recommended_action="use_declared_staged_writer_tool",
        )
    return None


# LLM: _delivery_contract mirrors closeout lookup order without reading rendered prompt text.
# 函数用途: 优先读取运行参数里的 delivery_contract，兼容 task_attributes 内保存的同名机器字段。
def _delivery_contract(params: ToolLoopExecuteParams) -> dict[str, Any]:
    if isinstance(params.delivery_contract, dict):
        return params.delivery_contract
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    value = attrs.get("delivery_contract")
    return value if isinstance(value, dict) else {}


# LLM: _writer_contract_index derives protected writer refs from staging, bootstrap and recovery action fields.
# 函数用途: 汇总 source_json_ref/checkpoint_ref/output_ref 等结构化字段，不读取任务 goal 或自然语言说明。
def _writer_contract_index(contract: dict[str, Any]) -> WriterContractIndex:
    index = WriterContractIndex()
    _index_artifact_staging_contracts(index, contract.get("artifacts"))
    _index_bootstrap_actions(index, contract.get("bootstrap_contract"))
    _index_declared_actions(index, contract)
    return index


# LLM: _index_artifact_staging_contracts reads artifact.validation_contract.staging_contract only.
# 函数用途: 从交付物 staging 合同中提取 JSON checkpoint writer 和 builder output writer。
def _index_artifact_staging_contracts(index: WriterContractIndex, artifacts: object) -> None:
    if not isinstance(artifacts, list):
        return
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        validation = artifact.get("validation_contract")
        staging = validation.get("staging_contract") if isinstance(validation, dict) else None
        if not isinstance(staging, dict):
            continue
        _index_json_sources(index, staging)
        _index_shape_hint_json_checkpoints(index, staging)
        _index_builder_outputs(index, staging)


# LLM: JSON source refs are machine checkpoints and must use the structured JSON writer.
# 函数用途: 将 source_json_ref 和 JSON checkpoint_refs 绑定到 write_structured_json。
def _index_json_sources(index: WriterContractIndex, staging: dict[str, object]) -> None:
    for key in _source_ref_keys(staging):
        _add_json_source_writers(index, staging.get(key))
    checkpoint_refs = staging.get("checkpoint_refs")
    if not isinstance(checkpoint_refs, list):
        return
    for ref in checkpoint_refs:
        if _ref_text(ref).lower().endswith(".json"):
            _add_json_source_writers(index, ref)


# LLM: Shape hints are structured writer facts for JSON checkpoints.
# 函数用途: 如果 checkpoint_shape_hints 声明了 JSON 形状，该路径只能通过 write_structured_json 生成。
def _index_shape_hint_json_checkpoints(index: WriterContractIndex, staging: dict[str, object]) -> None:
    hints = staging.get("checkpoint_shape_hints")
    if not isinstance(hints, dict):
        return
    for ref in hints:
        if _ref_text(ref).lower().endswith(".json"):
            _add_json_source_writers(index, ref)


# LLM: JSON checkpoints can be produced by either direct structured data or API-backed collection tools.
# 函数用途: 将 JSON checkpoint ref 绑定到所有声明的结构化来源写入工具。
def _add_json_source_writers(index: WriterContractIndex, ref: object) -> None:
    for tool_name in _STRUCTURED_JSON_SOURCE_WRITERS:
        index.add(ref, tool_name)


# LLM: Builder output refs are protected by the declared builder tool.
# 函数用途: 将 workbook_ref/pdf_ref/output_ref 绑定到 data_to_workbook/markdown_to_pdf 等 builder_tool。
def _index_builder_outputs(index: WriterContractIndex, staging: dict[str, object]) -> None:
    builder_tool = _tool_text(staging.get("builder_tool"))
    if not builder_tool:
        return
    for key in _builder_output_keys(staging):
        index.add(staging.get(key), builder_tool)


# LLM: Bootstrap startup actions expose the same writer facts before closeout exists.
# 函数用途: 开工阶段也能拦住普通文件工具写 JSON checkpoint 或 builder 输出。
def _index_bootstrap_actions(index: WriterContractIndex, bootstrap: object) -> None:
    actions = bootstrap.get("startup_actions") if isinstance(bootstrap, dict) else None
    if not isinstance(actions, list):
        return
    for action in actions:
        if not isinstance(action, dict):
            continue
        checkpoint_ref = _ref_text(action.get("checkpoint_ref"))
        if checkpoint_ref.lower().endswith(".json"):
            _add_json_source_writers(index, checkpoint_ref)
        if _tool_text(action.get("builder_tool")):
            index.add(action.get("output_ref"), action.get("builder_tool"))


# LLM: Recovery actions may arrive inside nested contract payloads after a failed attempt.
# 函数用途: 递归扫描结构化 action 字段，只接受 writer_tool/checkpoint_ref 与 builder_tool/output_ref 配对。
def _index_declared_actions(index: WriterContractIndex, value: object) -> None:
    if isinstance(value, dict):
        writer_tool = value.get("writer_tool")
        checkpoint_ref = value.get("checkpoint_ref")
        if _tool_text(writer_tool) and _ref_text(checkpoint_ref):
            index.add(checkpoint_ref, writer_tool)
        builder_tool = value.get("builder_tool")
        output_ref = value.get("output_ref")
        if _tool_text(builder_tool) and _ref_text(output_ref):
            index.add(output_ref, builder_tool)
        for child in value.values():
            _index_declared_actions(index, child)
        return
    if isinstance(value, list):
        for item in value:
            _index_declared_actions(index, item)


# LLM: _target_path_for_tool extracts only machine path parameters that represent writes.
# 函数用途: 识别本次工具会写哪个目标路径；source_json_path 这类读取参数不会被当成写入目标。
def _target_path_for_tool(tool: str, payload: dict[str, object]) -> str:
    if _looks_read_only_tool(tool):
        return ""
    if tool in _DIRECT_FILE_WRITE_TOOLS:
        return _first_path(payload, ("path", "file_path", "target_path"))
    if tool in {*_STRUCTURED_JSON_SOURCE_WRITERS, "data_to_workbook", "markdown_to_pdf"}:
        return _first_path(payload, ("path", "output_path", "target_path", "artifact_path"))
    if any(fragment in tool for fragment in _WRITE_INTENT_FRAGMENTS):
        return _first_path(payload, ("path", "output_path", "target_path", "artifact_path", "file_path"))
    return ""


def _source_ref_keys(staging: dict[str, object]) -> tuple[str, ...]:
    keys = [
        str(staging.get("source_ref_key") or "").strip(),
        str(staging.get("input_ref_key") or "").strip(),
        *_JSON_SOURCE_KEYS,
        "source_ref",
        "input_ref",
    ]
    return tuple(dict.fromkeys(key for key in keys if key))


def _builder_output_keys(staging: dict[str, object]) -> tuple[str, ...]:
    keys = [
        str(staging.get("output_ref_key") or "").strip(),
        *_KNOWN_BUILDER_OUTPUT_KEYS,
    ]
    return tuple(dict.fromkeys(key for key in keys if key))


def _looks_read_only_tool(tool: str) -> bool:
    return bool(tool) and tool.startswith(_READ_ONLY_TOOL_PREFIXES)


# LLM: _first_path 是 agent_py_agent/agent/agent_core/tool_staged_writer_contract.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 first path 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _first_path(payload: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _ref_text(payload.get(key))
        if value:
            return value
    return ""


# LLM: _tool_text 是 agent_py_agent/agent/agent_core/tool_staged_writer_contract.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 tool text 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _tool_text(value: object) -> str:
    return str(value or "").strip()


# LLM: _ref_text 是 agent_py_agent/agent/agent_core/tool_staged_writer_contract.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 ref text 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _ref_text(value: object) -> str:
    return str(value or "").strip().replace("\\", "/")


# LLM: _same_path_ref matches absolute and workspace-relative refs without parsing prose.
# 函数用途: 让 /tmp/workspace/outputs/a.json 与 outputs/a.json 指向同一合同目标。
def _same_path_ref(path: str, ref: str) -> bool:
    normalized_path = _ref_text(path).rstrip("/")
    normalized_ref = _ref_text(ref).strip("/")
    return (
        normalized_path == normalized_ref
        or normalized_path.endswith(f"/{normalized_ref}")
        or normalized_ref.endswith(f"/{normalized_path}")
    )


# LLM: _display_required_tools 是 agent_py_agent/agent/agent_core/tool_staged_writer_contract.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 display required tools 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _display_required_tools(required_tools: set[str]) -> str:
    return ",".join(sorted(required_tools))


__all__ = ["staged_writer_contract_result"]
