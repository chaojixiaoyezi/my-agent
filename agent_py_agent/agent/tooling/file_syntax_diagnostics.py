# LLM: 文件语法观察只消费候选同一 fd；收集异常只使本次反馈未检查，取消须传播，不改发布事实；联测三个文件入口。
# 模块用途: 用标准库给已提交文件附有限语法反馈，隔离诊断普通异常，不启动检查程序或保存第二份状态。
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from ..common.cancellation import ToolCancelled

if TYPE_CHECKING:
    from .models import ToolHandlerOutcome

MAX_FILE_SYNTAX_BYTES = 256 * 1024
MAX_CALL_SYNTAX_BYTES = 512 * 1024
MAX_SYNTAX_OBSERVATIONS = 16
MAX_SYNTAX_FEEDBACK_CHARS = 4096


# LLM: 不可变观察仅指本次完整候选；位置来自解析器，不能含文件正文或把 valid 升格为业务验收。
# 类用途: 保存有限的语法状态、固定报码及可用的行列位置。
@dataclass(frozen=True)
class FileSyntaxObservation:
    path: str
    status: str
    code: str
    line: int | None = None
    column: int | None = None
    offset: int | None = None

    # LLM: 结构化回执不附原始异常或候选正文；该投影和模型提示共用同一观察。
    # 函数用途: 将一次 JSON 观察变成可归档的有限字段。
    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"path": self.path, "format": "json", "status": self.status, "code": self.code}
        for key in ("line", "column", "offset"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


# LLM: 只区分标准 JSON 以外的常量，不把候选值写入异常，调用方仍允许原字节发布。
# 类用途: 标记 Python JSON 默认接受的 NaN/Infinity 扩展。
class _NonStandardJSONConstant(ValueError):
    pass


# LLM: parse_constant 回调不得执行或改写文件；异常只供语法观察分类。
# 函数用途: 拒绝非标准常量，同时不暴露原始值。
def _reject_json_constant(_value: str) -> None:
    raise _NonStandardJSONConstant()


# LLM: bytes 输入保留标准库的 UTF/BOM 识别，所有合法顶层值均可解析；不加入业务字段或顶层容器限制。
# 函数用途: 解析有界 JSON 字节，不使用 artifact opener 或运行用户程序。
def _parse_json(data: bytes) -> None:
    json.loads(data, parse_constant=_reject_json_constant)


# LLM: 读取及解析能力不足只构成 not_checked，不是写失败；固定报码避免异常文字泄露候选内容。
# 函数用途: 为无法完成的观察生成安全回执。
def unavailable_observation(target: Path) -> FileSyntaxObservation:
    return FileSyntaxObservation(str(target), "not_checked", "SYNTAX_DIAGNOSTIC_UNAVAILABLE")


# LLM: 预算仅属于本次调用；发布后经 update_syntax_diagnostics 登记，收集失效后整份观察不再作为事实输出。
# 类用途: 限制读取、解析及回执条数，汇集已提交观察并记住本次诊断是否不可用。
@dataclass
class FileSyntaxDiagnostics:
    bytes_read: int = 0
    attempt_count: int = 0
    observations: dict[str, FileSyntaxObservation] = field(default_factory=dict)
    omitted_receipts: int = 0
    unavailable: bool = False

    # LLM: source 必须是仍持有的完整候选 fd；收集失效后不再读，不解析截断前缀，ToolCancelled 必须传播。
    # 函数用途: 有界读取候选并观察语法，本次诊断已不可用时直接跳过额外工作。
    def observe_candidate(self, target: Path, source: BinaryIO) -> FileSyntaxObservation | None:
        if self.unavailable or target.suffix.lower() != ".json":
            return None
        if self.attempt_count >= MAX_SYNTAX_OBSERVATIONS:
            return FileSyntaxObservation(str(target), "not_checked", "SYNTAX_OBSERVATION_LIMIT")
        self.attempt_count += 1
        remaining = MAX_CALL_SYNTAX_BYTES - self.bytes_read
        if remaining <= 0:
            return FileSyntaxObservation(str(target), "not_checked", "SYNTAX_CALL_BYTE_LIMIT")
        limit = min(MAX_FILE_SYNTAX_BYTES, remaining - 1)
        try:
            source.seek(0)
            # 先预留读取量，异常可能发生在部分读取之后，不能用反复失败绕过总预算。
            self.bytes_read += limit + 1
            data = source.read(limit + 1)
            self.bytes_read -= limit + 1 - len(data)
            if len(data) > limit:
                code = "SYNTAX_FILE_BYTE_LIMIT" if limit == MAX_FILE_SYNTAX_BYTES else "SYNTAX_CALL_BYTE_LIMIT"
                return FileSyntaxObservation(str(target), "not_checked", code)
            return _observe_json_bytes(target, data)
        except ToolCancelled:
            raise
        except Exception:
            return unavailable_observation(target)

    # LLM: 由 update_syntax_diagnostics 包住收集异常，调用方只交成功发布的观察；列表有界，不保留无限路径集合。
    # 函数用途: 更新目标最后一次提交的语法回执，不改变文件或工具执行状态。
    def record(self, observation: FileSyntaxObservation | None) -> None:
        if observation is None:
            return
        if observation.path in self.observations or len(self.observations) < MAX_SYNTAX_OBSERVATIONS:
            self.observations[observation.path] = observation
        else:
            self.omitted_receipts += 1

    # LLM: 由 update_syntax_diagnostics 包住清理异常，只在实际删除成功后移除地址；移动目标观察独立记录。
    # 函数用途: 避免补丁已经删除的地址继续出现语法观察。
    def discard(self, target: Path) -> None:
        self.observations.pop(str(target), None)


# LLM: 唯一收集异常边界只围住诊断变更，不包文件操作；失败使本调用观察失效，ToolCancelled 和其它取消控制继续传播。
# 函数用途: 在真实发布或删除后更新可选诊断，普通登记异常不能翻转已经发生的文件修改结果。
def update_syntax_diagnostics(
    diagnostics: FileSyntaxDiagnostics | None, observation: FileSyntaxObservation | None = None,
    *, deleted_path: Path | None = None,
) -> None:
    if diagnostics is None or diagnostics.unavailable:
        return
    try:
        if deleted_path is not None:
            diagnostics.discard(deleted_path)
        elif observation is not None:
            diagnostics.record(observation)
    except ToolCancelled:
        raise
    except Exception:
        diagnostics.unavailable = True


# LLM: 仅 JSONDecodeError 及非标准常量是语法无效；编码、资源异常是未检查，统一取消异常必须原样传播。
# 函数用途: 将有界解析结果转成固定状态和定位信息，不输出原文。
def _observe_json_bytes(target: Path, data: bytes) -> FileSyntaxObservation:
    try:
        _parse_json(data)
    except json.JSONDecodeError as exc:
        return FileSyntaxObservation(str(target), "invalid", "JSON_INVALID", exc.lineno, exc.colno, exc.pos)
    except _NonStandardJSONConstant:
        return FileSyntaxObservation(str(target), "invalid", "JSON_NONSTANDARD_CONSTANT")
    except ToolCancelled:
        raise
    except (RecursionError, MemoryError, ValueError):
        return FileSyntaxObservation(str(target), "not_checked", "JSON_PARSER_UNAVAILABLE")
    except Exception:
        return unavailable_observation(target)
    return FileSyntaxObservation(str(target), "valid", "JSON_VALID")


# LLM: 不修改 ok/effect/error；收集失败不能输出过期或已删除地址观察，普通诊断错误降级，ToolCancelled 传播。
# 函数用途: 将有限观察或本次未检查事实附到原回执和模型正文，关闭或未知格式保持旧回执。
def attach_syntax_diagnostics(outcome: ToolHandlerOutcome, diagnostics: FileSyntaxDiagnostics | None) -> ToolHandlerOutcome:
    if diagnostics is None or not (diagnostics.unavailable or diagnostics.observations or diagnostics.omitted_receipts):
        return outcome
    try:
        if diagnostics.unavailable:
            payload = {"observations": [], "status": "not_checked", "code": "SYNTAX_DIAGNOSTIC_UNAVAILABLE"}
        else:
            payload = {
                "observations": [item.to_dict() for item in diagnostics.observations.values()],
                "omitted_receipts": diagnostics.omitted_receipts,
            }
        note = _render_feedback(payload)
    except ToolCancelled:
        raise
    except Exception:
        payload = {"observations": [], "status": "not_checked", "code": "SYNTAX_DIAGNOSTIC_UNAVAILABLE"}
        note = "语法观察：not_checked；诊断暂不可用，原文件修改结果保持不变。"
    outcome.result_envelope["syntax_diagnostics"] = payload
    outcome.output += "\n" + note
    return outcome


# LLM: 展示只消费结构化观察，收集失效只输出未检查；路径 JSON 转义，总字符有上限，未展示不冒称有效。
# 函数用途: 生成有限中文提示，明确诊断不可用或优先展示无效和未检查的候选。
def _render_feedback(payload: dict[str, object]) -> str:
    if payload.get("status") == "not_checked":
        return "语法观察：not_checked；诊断暂不可用，原文件修改结果保持不变。"
    records = payload["observations"]
    omitted = int(payload["omitted_receipts"])
    header = "语法观察（不改变文件修改结果）："
    tail = f"另有 {omitted} 次已提交修改未附观察。" if omitted else ""
    lines = [header]
    order = {"invalid": 0, "not_checked": 1, "valid": 2}
    for row in sorted(records, key=lambda item: order[item["status"]]):
        path = str(row["path"])
        shown_path = path if len(path) <= 240 else path[:100] + "…" + path[-139:]
        line = json.dumps({**row, "path": shown_path}, ensure_ascii=False, separators=(",", ":"))
        if len("\n".join([*lines, line, tail])) + 35 > MAX_SYNTAX_FEEDBACK_CHARS:
            lines.append("其余观察未展示，不能据此推断全部有效。")
            break
        lines.append(line)
    if tail:
        lines.append(tail)
    return "\n".join(lines)[:MAX_SYNTAX_FEEDBACK_CHARS]
