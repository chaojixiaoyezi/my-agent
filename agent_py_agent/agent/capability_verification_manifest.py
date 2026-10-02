# LLM: v7 能力包可选的 capability.verification 声明（能力包 v2，第 11 条）：交付物怎么认、宿主可以跑哪些钉住的原版检查程序、
#   是否要求保留输入原件。这里只做形状校验和序列化，不授予执行权：检查程序要在启用时由管理员确认（plugin_enable_tool），
#   运行时再核对同意摘要和成员 sha。格式、运行方式、输入策略都是开放字符串，宿主不认识的只记“不支持”，不拒绝安装。
#   改动须同步 test_capability_verification_manifest.py、docs/design/CAPABILITY_PACKS_V2.md，并保证未声明时旧包字节不变。
# 模块用途: 定义并校验能力包里“交付物、检查程序、输入策略”三类结构化声明，供安装、启用确认和宿主检查共用。

from __future__ import annotations

import re
from dataclasses import dataclass

# 一个包最多声明的交付物条数；声明是给宿主收尾核对用的，几条足够，防止清单膨胀。
MAX_VERIFICATION_DELIVERABLES_COUNT = 8
# 一个包最多声明的检查程序条数；每条在收尾时可能各跑一次，必须有界。
MAX_VERIFICATION_VERIFIERS_COUNT = 8
# 一个交付物最多声明的路径模式条数。
MAX_DELIVERABLE_PATH_PATTERNS_COUNT = 8
# 结构化字段匹配最多列出的取值个数。
MAX_FIELD_MATCH_VALUES_COUNT = 16
# 一个检查程序最多的固定参数个数（含 {target} 占位）。
MAX_VERIFIER_ARGS_COUNT = 16
# 检查程序单次运行的超时上限；包声明的超时不能超过它。
MAX_VERIFIER_TIMEOUT_SECONDS = 120
# 单个模式、参数或取值的字节上限。
MAX_VERIFICATION_TEXT_BYTES = 256

TARGET_PLACEHOLDER = "{target}"
INPUT_POLICY_PRESERVE_ORIGINALS = "preserve_originals"
BASELINE_FROM_INPUT_SAME_DELIVERABLE = "input_same_deliverable"
_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_TOKEN = re.compile(r"[a-z][a-z0-9_.-]{0,31}\Z")
_FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_FLAG = re.compile(r"-{1,2}[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


# LLM: 只认顶层字段等于声明取值；format 是开放字符串，宿主不认识的格式退回“存在、能打开”核对，不在此拒绝。
# 类用途: 描述“文件按某种格式解析后，某个顶层字段等于声明取值才算这个交付物”。
@dataclass(frozen=True)
class DeliverableFieldMatch:
    format: str
    field: str
    equals: tuple[str, ...]

    # 函数用途: 校验格式名、字段名和取值集合的形状。
    def __post_init__(self) -> None:
        if not _matches(_TOKEN, self.format) or not _matches(_FIELD, self.field):
            raise ValueError("交付物字段匹配无效")
        _bounded_texts(self.equals, MAX_FIELD_MATCH_VALUES_COUNT)

    # 函数用途: 序列化为固定字段。
    def to_payload(self) -> dict:
        return {"format": self.format, "field": self.field, "equals": list(self.equals)}

    # LLM: 未知键直接拒绝，不能把未来协议静默当作当前协议。
    # 函数用途: 从包声明恢复字段匹配。
    @classmethod
    def from_payload(cls, value: object) -> DeliverableFieldMatch:
        row = _exact(value, {"format", "field", "equals"})
        return cls(row["format"], row["field"], _string_tuple(row["equals"]))


# LLM: 路径模式是工作区相对的 glob（支持 * ? 和 **），只用于在本任务写过的文件里认交付物，不授予读写权限；
#   字段匹配可选，不声明时按路径认。required 只表示“收尾时没有就返工”，不前置拦截。
# 类用途: 描述一个能力包期望用户最终拿到的交付物。
@dataclass(frozen=True)
class DeliverableDeclaration:
    id: str
    path_patterns: tuple[str, ...]
    field_match: DeliverableFieldMatch | None = None
    required: bool = False

    # 函数用途: 校验编号、路径模式和可选匹配。
    def __post_init__(self) -> None:
        if not _matches(_ID, self.id) or not isinstance(self.required, bool):
            raise ValueError("交付物声明无效")
        if not isinstance(self.path_patterns, tuple) or not 1 <= len(self.path_patterns) <= MAX_DELIVERABLE_PATH_PATTERNS_COUNT:
            raise ValueError("交付物路径模式无效")
        for pattern in self.path_patterns:
            _validate_path_pattern(pattern)
        if self.field_match is not None and not isinstance(self.field_match, DeliverableFieldMatch):
            raise ValueError("交付物字段匹配无效")

    # 函数用途: 序列化；未声明字段匹配时省略该键。
    def to_payload(self) -> dict:
        payload = {"id": self.id, "path_patterns": list(self.path_patterns), "required": self.required}
        if self.field_match is not None:
            payload["field_match"] = self.field_match.to_payload()
        return payload

    # 函数用途: 从包声明恢复交付物。
    @classmethod
    def from_payload(cls, value: object) -> DeliverableDeclaration:
        row = _exact(value, {"id", "path_patterns", "required"}, optional={"field_match"})
        match = DeliverableFieldMatch.from_payload(row["field_match"]) if "field_match" in row else None
        return cls(row["id"], _string_tuple(row["path_patterns"]), match, row["required"])


# LLM: 基线只有一种开放来源名，宿主按名字找“本任务开始前就存在、且符合同一交付物声明的输入文件”；flag 是字面量参数名。
#   来源名不认识时宿主不传基线，不拒绝安装。
# 类用途: 描述检查程序可选的“对照原件”参数。
@dataclass(frozen=True)
class VerifierBaseline:
    source: str
    flag: str

    # 函数用途: 校验来源名与参数名。
    def __post_init__(self) -> None:
        if not _matches(_TOKEN, self.source) or not _matches(_FLAG, self.flag):
            raise ValueError("检查程序基线声明无效")

    # 函数用途: 序列化为固定字段。
    def to_payload(self) -> dict:
        return {"source": self.source, "flag": self.flag}

    # 函数用途: 从包声明恢复基线声明。
    @classmethod
    def from_payload(cls, value: object) -> VerifierBaseline:
        row = _exact(value, {"source", "flag"})
        return cls(row["source"], row["flag"])


# LLM: member 必须是本包 files 里带 sha256 的成员（在 validate_verification_members 里核对），宿主只从安装 blob 取原件跑；
#   args 里恰好一个 {target}，其余只能是不含花括号的字面量，模型和用户都不能注入参数。runtime 是开放字符串。
# 类用途: 描述一个可由宿主在沙箱里运行的包内检查程序。
@dataclass(frozen=True)
class VerifierDeclaration:
    id: str
    member: str
    runtime: str
    applies_to: str
    args: tuple[str, ...]
    timeout_seconds: int
    baseline: VerifierBaseline | None = None

    # 函数用途: 校验编号、成员路径、运行方式、参数模板与超时。
    def __post_init__(self) -> None:
        from .capability_package_manifest import validate_capability_path

        validate_capability_path(self.member)
        if not _matches(_ID, self.id) or not _matches(_ID, self.applies_to) or not _matches(_TOKEN, self.runtime):
            raise ValueError("检查程序声明无效")
        _bounded_texts(self.args, MAX_VERIFIER_ARGS_COUNT)
        if self.args.count(TARGET_PLACEHOLDER) != 1 or any(
                arg != TARGET_PLACEHOLDER and ("{" in arg or "}" in arg) for arg in self.args):
            raise ValueError("检查程序参数模板无效")
        if (type(self.timeout_seconds) is not int
                or not 1 <= self.timeout_seconds <= MAX_VERIFIER_TIMEOUT_SECONDS):
            raise ValueError("检查程序超时无效")
        if self.baseline is not None and not isinstance(self.baseline, VerifierBaseline):
            raise ValueError("检查程序基线声明无效")

    # 函数用途: 序列化；没有基线时省略该键。
    def to_payload(self) -> dict:
        payload = {"id": self.id, "member": self.member, "runtime": self.runtime, "applies_to": self.applies_to,
                   "args": list(self.args), "timeout_seconds": self.timeout_seconds}
        if self.baseline is not None:
            payload["baseline"] = self.baseline.to_payload()
        return payload

    # 函数用途: 从包声明恢复检查程序。
    @classmethod
    def from_payload(cls, value: object) -> VerifierDeclaration:
        row = _exact(value, {"id", "member", "runtime", "applies_to", "args", "timeout_seconds"}, optional={"baseline"})
        baseline = VerifierBaseline.from_payload(row["baseline"]) if "baseline" in row else None
        return cls(row["id"], row["member"], row["runtime"], row["applies_to"], _string_tuple(row["args"]),
                   row["timeout_seconds"], baseline)


# LLM: 三类声明的容器；编号各自唯一，检查程序只能指向本包声明的交付物。input_policy 为空串表示不要求保留原件，
#   其它取值是开放字符串，宿主只执行认识的 preserve_originals。
# 类用途: 表示一个能力包完整的结构化核验声明。
@dataclass(frozen=True)
class VerificationDeclaration:
    deliverables: tuple[DeliverableDeclaration, ...] = ()
    verifiers: tuple[VerifierDeclaration, ...] = ()
    input_policy: str = ""

    # 函数用途: 校验数量上限、编号唯一和检查程序与交付物的指向关系。
    def __post_init__(self) -> None:
        if (not isinstance(self.deliverables, tuple) or len(self.deliverables) > MAX_VERIFICATION_DELIVERABLES_COUNT
                or any(not isinstance(item, DeliverableDeclaration) for item in self.deliverables)
                or not isinstance(self.verifiers, tuple) or len(self.verifiers) > MAX_VERIFICATION_VERIFIERS_COUNT
                or any(not isinstance(item, VerifierDeclaration) for item in self.verifiers)):
            raise ValueError("能力核验声明无效")
        deliverable_ids = [item.id for item in self.deliverables]
        verifier_ids = [item.id for item in self.verifiers]
        if len(set(deliverable_ids)) != len(deliverable_ids) or len(set(verifier_ids)) != len(verifier_ids):
            raise ValueError("能力核验声明编号重复")
        if any(item.applies_to not in deliverable_ids for item in self.verifiers):
            raise ValueError("检查程序指向未声明的交付物")
        if self.input_policy != "" and not _matches(_TOKEN, self.input_policy):
            raise ValueError("输入策略无效")
        if not self.deliverables and not self.verifiers and not self.input_policy:
            raise ValueError("能力核验声明不能为空")

    # LLM: 只序列化非空部分，空块不会出现在包里（构造时已拒绝空声明）。
    # 函数用途: 生成写回包声明的固定字段。
    def to_payload(self) -> dict:
        payload: dict = {}
        if self.deliverables:
            payload["deliverables"] = [item.to_payload() for item in self.deliverables]
        if self.verifiers:
            payload["verifiers"] = [item.to_payload() for item in self.verifiers]
        if self.input_policy:
            payload["input_policy"] = self.input_policy
        return payload

    # 函数用途: 从包声明恢复核验声明，未知键直接拒绝。
    @classmethod
    def from_payload(cls, value: object) -> VerificationDeclaration:
        row = _exact(value, set(), optional={"deliverables", "verifiers", "input_policy"})
        return cls(
            tuple(DeliverableDeclaration.from_payload(item) for item in _list(row.get("deliverables", []))),
            tuple(VerifierDeclaration.from_payload(item) for item in _list(row.get("verifiers", []))),
            row.get("input_policy", ""),
        )

    # LLM: 只有声明了检查程序才意味着宿主会执行包内代码，启用确认据此判断；交付物和输入策略不执行代码。
    # 函数用途: 判断这个包是否需要启用前确认。
    @property
    def runs_package_code(self) -> bool:
        return bool(self.verifiers)


# LLM: 成员必须在 files 里且不能是入口以外的越界路径；sha 由 files 声明给出，运行时再按安装 blob 复核。
# 函数用途: 核对每个检查程序引用的成员都是本包声明过的文件。
def validate_verification_members(verification: VerificationDeclaration, file_paths: set[str]) -> None:
    missing = [item.member for item in verification.verifiers if item.member not in file_paths]
    if missing:
        raise ValueError("检查程序引用了未声明的包文件")


# LLM: glob 只用于匹配工作区相对路径；拒绝绝对路径、反斜杠、.. 段和控制字符，** 只能整段出现。
# 函数用途: 校验一个交付物路径模式。
def _validate_path_pattern(value: object) -> None:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_VERIFICATION_TEXT_BYTES
            or value.startswith("/") or "\\" in value or ":" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("交付物路径模式无效")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or ("**" in part and part != "**") for part in parts):
        raise ValueError("交付物路径模式无效")


# 函数用途: 校验有界字符串元组（非空、无控制字符、单项字节有上限）。
def _bounded_texts(values: object, limit: int) -> None:
    if (not isinstance(values, tuple) or not 1 <= len(values) <= limit
            or any(not isinstance(item, str) or not item or len(item.encode("utf-8")) > MAX_VERIFICATION_TEXT_BYTES
                   or any(ord(char) < 32 or ord(char) == 127 for char in item) for item in values)):
        raise ValueError("能力核验声明文本无效")


# 函数用途: 判断值是字符串且整段匹配给定模式。
def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and bool(pattern.match(value))


# LLM: 必填键必须齐全，可选键之外的键一律拒绝。
# 函数用途: 按固定键集合取出字典字段。
def _exact(value: object, required: set[str], *, optional: frozenset[str] | set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= set(value) or not set(value) <= required | set(optional):
        raise ValueError("能力核验声明字段无效")
    return value


# 函数用途: 要求值是列表并原样返回。
def _list(value: object) -> list:
    if not isinstance(value, list):
        raise ValueError("能力核验声明字段无效")
    return value


# 函数用途: 把字符串列表转成元组，类型不对就拒绝。
def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(_list(value))
