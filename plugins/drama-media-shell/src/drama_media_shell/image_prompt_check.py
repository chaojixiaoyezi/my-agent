# LLM: 固定迁移 drama-skills 图像提示词校验语义；文件读取已移到 server 的 SDK 逐次上下文入口，本模块保持纯数据校验。
# 模块用途: 校验图像提示词记录的引用、结构、文字策略和供应商中立性，不生成媒体也不读写文件。
"""Validate standalone image-prompt specs without generating media."""

from __future__ import annotations

import re
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# REFERENCE RESOLVER -- reference implementation.
#
# Each skill checker carries its own copy of this block. The suite has no shared
# library on purpose: a skill must stay runnable after copying only its own
# directory, so duplicating these few lines across skills is the correct shape.
# Copy the block verbatim; do not import it.
# ---------------------------------------------------------------------------

SOURCES_RECORD_TYPE = "sources"
SOURCES_SCHEMA_VERSION = "1.0.0"


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 类用途: 保存紧凑或展开引用解析后的上游身份。
class ResolvedRef(NamedTuple):
    """An upstream reference with its snapshot resolved, whichever form it used."""

    owner: str
    artifact: str
    record_id: str | None
    field: str | None
    authority: str | None


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 类用途: 保存引用对象的结构缺陷。
class RefFinding(NamedTuple):
    """A structural defect in a reference object."""

    code: str
    location: str
    detail: str


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 读取已解析文档中的 sources 声明。
def load_sources(document: Any) -> dict[str, dict[str, Any]]:
    """Return the ``sources`` declaration of a parsed file, or ``{}`` if absent.

    Accepts a parsed ``.json`` document (a dict) or the parsed record list of a
    ``.jsonl`` file, whose declaration lives on the first record.
    """
    if isinstance(document, list):
        document = document[0] if document else None
    if not isinstance(document, dict):
        return {}
    declared = document.get("sources")
    if not isinstance(declared, dict):
        return {}
    return {key: value for key, value in declared.items() if isinstance(value, dict)}


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 把紧凑或展开引用统一解析为上游引用。
def resolve_ref(
    ref: Any, sources: dict[str, dict[str, Any]], location: str
) -> tuple[ResolvedRef | None, RefFinding | None]:
    """Resolve a reference object written in either the compact or expanded form."""
    if not isinstance(ref, dict):
        return None, RefFinding("REF_IS_NOT_AN_OBJECT", location, f"got {type(ref).__name__}")
    src = ref.get("src")
    if isinstance(src, str):
        entry = sources.get(src)
        if entry is None:
            return None, RefFinding(
                "REF_SRC_IS_NOT_DECLARED", location, f"src {src!r} has no sources entry"
            )
        owner, artifact = entry.get("owner"), entry.get("artifact")
        if not (isinstance(owner, str) and isinstance(artifact, str)):
            return None, RefFinding(
                "SOURCE_ENTRY_IS_INCOMPLETE", location, f"sources[{src!r}] needs owner/artifact"
            )
    elif all(isinstance(ref.get(key), str) for key in ("owner", "artifact")):
        owner, artifact = ref["owner"], ref["artifact"]
    else:
        return None, RefFinding(
            "REF_HAS_NO_UPSTREAM_BINDING", location, "needs src, or owner+artifact"
        )
    optional = {
        key: ref[key] for key in ("record_id", "field", "authority") if isinstance(ref.get(key), str)
    }
    return (
        ResolvedRef(
        owner,
        artifact,
            optional.get("record_id"),
            optional.get("field"),
            optional.get("authority"),
        ),
        None,
    )


# ---------------------------------------------------------------------------
# END REFERENCE RESOLVER
# ---------------------------------------------------------------------------

HASH_RE = re.compile(r"[0-9a-f]{64}")
# `common-recipe.md` forbids weight syntax and any one engine's control words: a
# generic prompt that carries provider control syntax has stopped being generic.
# This checks syntax only. Whether the prose leans on generic quality language is
# `IMG-02`, a `craft_default` a creator may override with a reason, so it is a
# reviewer's call citing what the description actually lacks — see
# `common-recipe.md`, which forbids blocking delivery on a fixed word list.
ENGINE_SYNTAX_RE = re.compile(
    r"(?:^|[\s,，(（])--(?:ar|v|q|niji|style|no|seed|cref|sref)\b"
    r"|::-?\d"
    # Weight syntax is written (x:1.2); prose writes "(aperture: 1.8)" with a space.
    r"|:[01]\.\d\s*[)）]",
    re.IGNORECASE,
)
PURPOSES = {
    "character_sheet",
    "location_plate",
    "prop_plate",
    "look_state_variant",
    "edit_delta",
    "lookdev_frame",
}
VENDOR_FIELDS = {
    "authorization",
    "credential",
    "credentials",
    "model",
    "model_id",
    "model_name",
    "provider",
    "provider_id",
    "api_key",
    "task_id",
    "remote_id",
    "access_token",
    "token",
    "secret",
    "password",
}
NORMALIZED_VENDOR_FIELDS = {re.sub(r"[^a-z0-9]", "", key) for key in VENDOR_FIELDS}


# The source policy -> render treatment mapping from `common-recipe.md`. A
# treatment outside its policy's row is a structural defect: the fix is a
# creator override that changes the accepted policy, never a quiet widening here.
ALLOWED_TREATMENTS = {
    "exact_readable": {"readable", "postproduction"},
    "graphic_only": {"symbolic"},
    "no_readable_text": {"blank", "symbolic"},
    "pending_creator_text": {"postproduction"},
}

# A blanket "no text in the image" instruction, however it is phrased. Matching
# two literals (`no text` / `无文字`) let the reference document's own worked
# counter-example through: `画面中不要任何文字` contains neither.
NO_TEXT_CONSTRAINT = re.compile(
    r"(无任何(文字|字|文本)|无文字|不要(任何)?(文字|字|文本)|不出现(任何)?(文字|字)"
    r"|没有(任何)?(文字|字)|不含(任何)?(文字|字)|禁止(出现)?(文字|字)"
    r"|no\s+(visible\s+)?(text|lettering|writing|words|typography)"
    r"|without\s+(any\s+)?text|text[-\s]free)",
    re.IGNORECASE,
)


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 类用途: 表示图像提示词记录无法安全交付。
class ValidationError(ValueError):
    pass


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 取得必填非空文本字段。
def text(record: dict[str, Any], key: str, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}: {key} must be non-empty text")
    return value


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 取得必填非空列表字段。
def nonempty_list(record: dict[str, Any], key: str, label: str) -> list[Any]:
    value = record.get(key)
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{label}: {key} must be a non-empty list")
    return value


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 校验一条引用具备有效上游绑定。
def validate_ref(
    value: Any,
    sources: dict[str, dict[str, Any]],
    label: str,
    *,
    field_allowed: bool = True,
) -> None:
    if not isinstance(value, dict):
        raise ValidationError(f"{label}: reference must be an object")
    resolved, finding = resolve_ref(value, sources, label)
    if finding is not None:
        raise ValidationError(f"{label}: {finding.code}: {finding.detail}")
    if resolved is None:
        raise ValidationError(f"{label}: reference could not be resolved")
    if not resolved.owner.strip() or not resolved.artifact.strip():
        raise ValidationError(f"{label}: owner and artifact must be non-empty text")
    if "record_id" not in value and (not field_allowed or "field" not in value):
        raise ValidationError(f"{label}: record_id or field is required")


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 校验有序参考槽位及其控制边界。
def validate_reference_bindings(
    record: dict[str, Any], sources: dict[str, dict[str, Any]], label: str
) -> None:
    bindings = record.get("reference_bindings", [])
    if not isinstance(bindings, list):
        raise ValidationError(f"{label}: reference_bindings must be a list")
    slots: set[str] = set()
    orders: set[int] = set()
    for index, binding in enumerate(bindings, 1):
        item = f"{label}.reference_bindings[{index}]"
        if not isinstance(binding, dict):
            raise ValidationError(f"{item}: binding must be an object")
        slot = text(binding, "slot_id", item)
        order = binding.get("order")
        if slot in slots:
            raise ValidationError(f"{item}: duplicate slot_id {slot}")
        if not isinstance(order, int) or order < 1 or order in orders:
            raise ValidationError(f"{item}: order must be a unique positive integer")
        slots.add(slot)
        orders.add(order)
        validate_ref(binding.get("artifact_ref"), sources, f"{item}.artifact_ref")
        text(binding, "role", item)
        nonempty_list(binding, "may_control", item)
        nonempty_list(binding, "must_not_control", item)
        admission = binding.get("admission_status")
        if admission not in {"unverified", "creator_described", "visually_inspected"}:
            raise ValidationError(f"{item}: invalid admission_status")
        if admission == "unverified" and not binding.get("unresolved_risks"):
            raise ValidationError(f"{item}: unverified references need unresolved_risks")


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 递归找出泄漏的供应商执行字段。
def vendor_field_paths(value: object, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        return _vendor_mapping_paths(value, prefix)
    if isinstance(value, list):
        return [path for index, child in enumerate(value)
                for path in vendor_field_paths(child, f"{prefix}[{index}]")]
    return []


# LLM: 映射遍历独立出来以限制递归入口的嵌套；路径与上游拼接规则保持不变。
# 函数用途: 收集一个映射及其子值中的供应商字段路径。
def _vendor_mapping_paths(value: dict[Any, Any], prefix: str) -> list[str]:
    leaked: list[str] = []
    for key, child in value.items():
        name = str(key)
        path = f"{prefix}.{name}" if prefix else name
        normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
        leaked.extend([path] if normalized in NORMALIZED_VENDOR_FIELDS else [])
        leaked.extend(vendor_field_paths(child, path))
    return leaked


# LLM: 文本策略单独校验以限制资产入口嵌套；仍只读结构化策略字段，不猜提示词自然语言意图。
# 函数用途: 核对资产规范中的文字来源策略、渲染方式和负面约束。
def _validate_text_handling(handling: dict[str, Any], record: dict[str, Any],
                            sources: dict[str, dict[str, Any]], label: str) -> None:
    if "source_policy_ref" in handling:
        validate_ref(handling.get("source_policy_ref"), sources,
                     f"{label}.text_handling.source_policy_ref")
    treatment = handling.get("render_treatment")
    if not isinstance(treatment, dict):
        raise ValidationError(f"{label}: text_handling.render_treatment is required")
    source_mode = handling.get("source_mode")
    allowed = ALLOWED_TREATMENTS.get(source_mode) if isinstance(source_mode, str) else None
    mode = treatment.get("mode")
    if allowed is not None and mode not in allowed:
        raise ValidationError(
            f"{label}: text policy {source_mode!r} allows "
            f"{' or '.join(sorted(allowed))}, not {mode!r}; a creator "
            f"override must change the policy rather than the treatment"
        )
    if source_mode != "exact_readable" or mode != "readable":
        return
    text(treatment, "exact_text", f"{label}.text_handling.render_treatment")
    negatives = " ".join(str(item) for item in record.get("negative_constraints", []))
    if NO_TEXT_CONSTRAINT.search(negatives):
        raise ValidationError(f"{label}: readable text conflicts with a no-text constraint")


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 校验资产类图像提示词规范。
def validate_asset_spec(
    record: dict[str, Any], sources: dict[str, dict[str, Any]], label: str
) -> None:
    binding = record.get("asset_binding")
    if not isinstance(binding, dict):
        raise ValidationError(f"{label}: asset_binding must be an object")
    validate_ref(binding.get("identity_ref"), sources, f"{label}.asset_binding.identity_ref")
    validate_ref(binding.get("variant_ref"), sources, f"{label}.asset_binding.variant_ref")
    for index, ref in enumerate(nonempty_list(record, "source_refs", label), 1):
        validate_ref(ref, sources, f"{label}.source_refs[{index}]")
    nonempty_list(record, "identity_or_form_anchors", label)

    if record["purpose"] == "edit_delta":
        edit = record.get("edit")
        if not isinstance(edit, dict):
            raise ValidationError(f"{label}: edit_delta requires edit")
        validate_ref(edit.get("target_ref"), sources, f"{label}.edit.target_ref")
        nonempty_list(edit, "changes", f"{label}.edit")
        nonempty_list(edit, "preserve", f"{label}.edit")
        text(edit, "continuity_impact", f"{label}.edit")

    handling = record.get("text_handling")
    if isinstance(handling, dict):
        _validate_text_handling(handling, record, sources, label)


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 校验视觉开发帧提示词规范。
def validate_lookdev_spec(
    record: dict[str, Any], sources: dict[str, dict[str, Any]], label: str
) -> None:
    validate_ref(record.get("direction_ref"), sources, f"{label}.direction_ref")
    validate_ref(record.get("production_profile_ref"), sources, f"{label}.production_profile_ref")
    subjects = nonempty_list(record, "subject_bindings", label)
    for index, subject in enumerate(subjects, 1):
        if not isinstance(subject, dict):
            raise ValidationError(f"{label}.subject_bindings[{index}]: must be an object")
        item = f"{label}.subject_bindings[{index}]"
        validate_ref(subject.get("identity_ref"), sources, f"{item}.identity_ref")
        if "variant_ref" in subject:
            validate_ref(subject.get("variant_ref"), sources, f"{item}.variant_ref")
        text(subject, "role", item)
    text(record, "test_question", label)
    nonempty_list(record, "stable_visual_rules", label)
    if record.get("lookdev_axis") == "high_pressure_scene":
        refs = nonempty_list(record, "story_context_refs", label)
        for index, ref in enumerate(refs, 1):
            validate_ref(ref, sources, f"{label}.story_context_refs[{index}]")


# LLM: 该入口迁移自固定上游校验器；保持原字段、错误与数值语义，只消费调用方已安全读取的数据。
# 函数用途: 校验整批图像提示词记录并返回检查摘要。
def validate_records(
    records: list[dict[str, Any]], sources: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    sources = sources or {}
    identifiers: set[str] = set()
    for index, record in enumerate(records, 1):
        label = f"spec[{index}]"
        spec_id = text(record, "spec_id", label)
        if spec_id in identifiers:
            raise ValidationError(f"{label}: duplicate spec_id {spec_id}")
        identifiers.add(spec_id)
        purpose = record.get("purpose")
        if purpose not in PURPOSES:
            raise ValidationError(
                f"{label}: invalid purpose {purpose!r}; "
                f"use one of {', '.join(sorted(PURPOSES))}"
            )
        if record.get("status") not in {"candidate", "accepted"}:
            raise ValidationError(f"{label}: status must be candidate or accepted")
        leaked = sorted(vendor_field_paths(record))
        if leaked:
            raise ValidationError(f"{label}: provider execution fields are forbidden: {', '.join(leaked)}")
        prompt = text(record, "generic_prompt", label)
        if HASH_RE.search(prompt) or "<sha256>" in prompt:
            raise ValidationError(f"{label}: generic_prompt leaks internal hashes")
        engine_syntax = ENGINE_SYNTAX_RE.search(prompt)
        if engine_syntax:
            raise ValidationError(
                f"{label}: generic_prompt carries engine-specific syntax "
                f"{engine_syntax.group(0).strip()!r}; keep it in a provider adapter"
            )
        validate_reference_bindings(record, sources, label)
        if purpose == "lookdev_frame":
            validate_lookdev_spec(record, sources, label)
        else:
            validate_asset_spec(record, sources, label)
    return {
        "status": "valid",
        "specs": len(records),
        "sources": len(sources),
        "checks": [
            "unique_ids",
            "accepted_bindings",
            # Named for what it does: every reference names a snapshot this
            # file declares. Whether that record exists in the target artifact
            # is not knowable here -- the checker is handed this file only.
            "source_declaration",
            "reference_slots",
            "prompt_hygiene",
        ],
    }
