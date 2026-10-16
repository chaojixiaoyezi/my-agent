# LLM: 只由候选仓库维护覆盖账本；缺失待补齐与明确不可学分离，不从正文或另一引用推断资格。
# 模块用途: 让多次候选合并可补齐同引用来源，同时永久保留显式来源否决。
import hashlib
import json
import re

from ..conversation.learning_source import merge_source_projections, unknown_projection

_REF_FIELDS = ("evidence_refs", "source_message_refs", "source_tool_refs", "source_artifact_refs",
               "source_task_ids", "source_run_ids")
_TOKEN = re.compile(r"[0-9a-f]{64}")


# LLM: 字段类别和完整结构化引用共同形成精确身份，不能按文本相似度或单个短ID借用覆盖。
# 函数用途: 将已经规范化的来源引用变成不重复保存正文的覆盖键。
def reference_tokens(source: object) -> set[str]:
    return {hashlib.sha256(json.dumps([name, ref], sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()
            for name in _REF_FIELDS for ref in getattr(source, name)}


# LLM: {}仅表示缺失，坏类型/坏投影成为明确unknown；旧候选没有覆盖账本时不猜unknown是否可补齐。
# 函数用途: 从单次观察或旧候选初始化保守的来源覆盖状态。
def initial_source_coverage(source: object) -> dict[str, object]:
    projection = merge_source_projections((source.learning_source,))
    tokens = sorted(reference_tokens(source))
    return {"schema_version": 1, "source": projection,
            "covered": tokens if projection else [], "missing": [] if projection else tokens}


# LLM: 只补精确匹配的缺失引用，显式不可学保留在source聚合中；多个宿主观察可以共同提供完整覆盖。
# 函数用途: 合并一次新观察的来源证明，不把临时缺口永久折叠成显式unknown。
def merge_source_coverage(current: object, observation: object) -> dict[str, object]:
    left = current.learning_source_coverage or initial_source_coverage(current)
    right = initial_source_coverage(observation)
    covered = set(left["covered"]) | set(right["covered"])
    missing = (set(left["missing"]) | set(right["missing"])) - covered
    return {"schema_version": 1, "source": merge_source_projections((left["source"], right["source"])),
            "covered": sorted(covered), "missing": sorted(missing)}


# LLM: 全部空投影保留人工入口兼容；已有来源证明但尚有未覆盖引用时暂时不可学，最终晋升仍复核原证据。
# 函数用途: 从持久覆盖状态计算候选当前的学习资格投影。
def source_coverage_projection(coverage: dict[str, object]) -> dict[str, object]:
    source = coverage["source"]
    if source and coverage["missing"]:
        return merge_source_projections((source, unknown_projection()))
    return dict(source)


# LLM: 已知覆盖字段损坏或与候选引用/投影不一致必须阻断账本读写，不能悄悄丢弃否决或补猜覆盖。
# 函数用途: 校验覆盖账本的结构、精确引用集合以及导出投影。
def validate_source_coverage(candidate: object) -> None:
    coverage = candidate.learning_source_coverage
    if isinstance(coverage, dict) and not coverage:
        return
    if (not isinstance(coverage, dict) or set(coverage) != {"schema_version", "source", "covered", "missing"}
            or type(coverage["schema_version"]) is not int or coverage["schema_version"] != 1
            or not isinstance(coverage["source"], dict)):
        raise ValueError("invalid learning_source_coverage")
    for name in ("covered", "missing"):
        values = coverage[name]
        if (not isinstance(values, list) or any(not isinstance(value, str) or not _TOKEN.fullmatch(value) for value in values)
                or values != sorted(set(values))):
            raise ValueError("invalid learning_source_coverage references")
    covered, missing = set(coverage["covered"]), set(coverage["missing"])
    if covered & missing or covered | missing != reference_tokens(candidate):
        raise ValueError("learning_source_coverage does not match candidate references")
    if (coverage["source"] != merge_source_projections((coverage["source"],))
            or (not coverage["source"] and covered)
            or source_coverage_projection(coverage) != candidate.learning_source):
        raise ValueError("learning_source_coverage projection mismatch")
