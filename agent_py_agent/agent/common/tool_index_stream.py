# LLM: 所有工具索引消费方共用E11a已证明的预筛与严格解码；各自的过滤、坏行及投影合同仍留在调用方。
# 模块用途: 唯一的工具索引逐行读取与安全字节预筛实现，不保存索引全集或读取artifact正文。
from __future__ import annotations

import codecs
import io
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_PRESCREEN_EXCLUDED_CHARS = frozenset('"\\')
_PRESCREEN_BOOL_LIKE_RUN_IDS = frozenset({"True"})
_PRESCREEN_ESCAPE_PATTERN = re.compile(rb"\\u00|\\/")


# LLM: 只有晋升需要整文件对象合法性，专用异常不能与超长整数等原ValueError混淆。
# 类用途: 表示工具索引含坏JSON或非对象，调用方应拒绝该文件的证据。
class InvalidToolIndexObject(ValueError):
    pass


# LLM: E11a物理LF只预筛；旧整读入口需要所有行的C解码资源错误，不能按词法或调用栈猜测。
#   字节预筛仍减少业务投影，不替代结构化身份判定；不缓存、不持久化。
# 函数用途: 逐行返回文本候选，按原入口合同解码并立即丢弃无关对象。
def iter_prescreened_tool_index_lines(
    path: Path, prescreen: re.Pattern[bytes] | None, *, universal_newlines: bool = False,
) -> Iterator[tuple[int, str]]:
    if not path.exists():
        return
    pending: Exception | None = None
    for number, raw in _iter_index_lines(path, universal_newlines=universal_newlines):
        text = _decode_record_bytes(raw)
        try:
            _check_index_resource_errors(text, universal_newlines)
        except (ValueError, RecursionError) as exc:
            pending = pending or exc
            continue
        if not _skip_record_bytes(raw, text, prescreen):
            yield number, text
    if pending is not None:
        raise pending


# LLM: 需要路径异常或整文件合法性合同的消费者用C解码逐行丢弃，不运行Python词法机，不保留历史全集。
# 函数用途: 共享同源换行读取，返回对象候选；坏文件及资源错误在文件读完后报告。
def iter_decoded_tool_index_objects(
    path: Path, prescreen: re.Pattern[bytes] | None = None, *, validate_objects: bool = False,
) -> Iterator[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return
    invalid = False
    pending: Exception | None = None
    for number, raw in _iter_index_lines(path, universal_newlines=True):
        text = _decode_record_bytes(raw)
        if not text.strip():
            continue
        try:
            row = _decoded_index_value(text)
        except (ValueError, RecursionError) as exc:
            pending = pending or exc
            continue
        invalid = invalid or (validate_objects and not isinstance(row, dict))
        if isinstance(row, dict) and not _skip_record_bytes(raw, text, prescreen):
            yield number, row
        row = None  # 不匹配行立即释放；只有消费方需要的候选能进入列表。
    if pending is not None:
        raise pending
    if invalid:
        raise InvalidToolIndexObject("tool index contains malformed JSON or a non-object row")


# LLM: JSON语法错误沿原宽容策略；整数/递归资源错误不降级为坏行，留给读取器维持晚IO优先。
# 函数用途: 使用标准C解码器解析一行，不运行自建词法校验。
def _decoded_index_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# LLM: 旧整读入口所有非空行都执行C解码后丢弃；不以深度阈值猜测调用栈剩余预算。
# 函数用途: 保持原解析资源异常合同；E11a物理LF入口不额外解码。
def _check_index_resource_errors(text: str, enabled: bool) -> None:
    if not enabled or not text.strip():
        return
    _decoded_index_value(text)


# LLM: with保证正常/异常关闭；没有整文件读取，通用换行也不把NEL/U+2028/U+2029当边界。
# 函数用途: 按消费方原换行合同流式提供索引字节。
def _iter_index_lines(path: Path, *, universal_newlines: bool = False) -> Iterator[tuple[int, bytes | str]]:
    with path.open("rb") as handle:
        lines = _universal_index_lines(handle) if universal_newlines else handle
        yield from enumerate(lines, start=1)


# LLM: 复用标准库增量UTF-8/通用换行，固定块避免每8KiB一次系统读取；不按Unicode分隔符断行。
# 函数用途: 以有界块还原旧read_text换行合同，不因索引总大小增加常驻内存。
def _universal_index_lines(handle: Any) -> Iterator[str]:
    decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder("utf-8")(), translate=True)
    tail = ""
    # 六十四KiB只是本次流式读取的块大小，不是索引或记录长度上限。
    while chunk := handle.read(64 * 1024):
        lines = (tail + decoder.decode(chunk)).split("\n")
        tail = lines.pop()
        yield from lines
    final = tail + decoder.decode(b"", final=True)
    if final:
        yield from final.removesuffix("\n").split("\n")


# LLM: 可见ASCII可能写成unicode或斜杠转义，这类行一律保留；空白与无必要字节的行才跳过。
# 函数用途: 判断已严格解码的索引行是否可以安全省掉JSON解析。
def _skip_record_bytes(raw: bytes | str, text: str, prescreen: re.Pattern[bytes] | None) -> bool:
    if prescreen is not None and isinstance(raw, str):
        raw = raw.encode("utf-8")
    if prescreen is not None and prescreen.search(raw) is None:
        if b"\\" not in raw or _PRESCREEN_ESCAPE_PATTERN.search(raw) is None:
            return True
    return not text.strip()


# LLM: 换行仅删尾部LF/CR，正文Unicode分隔符不动；解码严格、无替换字符降级。
# 函数用途: 将一个索引物理行还原成JSON文本。
def _decode_record_bytes(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw.removesuffix("\n").removesuffix("\r")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    return raw.decode("utf-8")


# LLM: 必要字节条件不是身份判断；任一值含转义字符、非ASCII或str转换差异就退回完整解析。
# 函数用途: 为安全查询值编译字面量OR正则，与转义兜底共同保证不漏匹配。
def tool_index_prescreen_pattern(values: tuple[str, ...]) -> re.Pattern[bytes] | None:
    if not values or not all(_is_prescreen_safe_run_id(value) for value in values):
        return None
    return re.compile(b"|".join(re.escape(value.encode("utf-8")) for value in values))


# LLM: 布尔、数字和数组的str结果可能不在JSON原文中，必须保守退回；字典含引号也被排除。
# 函数用途: 判断查询值是否可以用原始JSON字节作为必要匹配条件。
def _is_prescreen_safe_run_id(value: str) -> bool:
    if not value or value in _PRESCREEN_BOOL_LIKE_RUN_IDS:
        return False
    if _is_numeric_like_run_id(value) or value.startswith("["):
        return False
    return all("!" <= char <= "~" and char not in _PRESCREEN_EXCLUDED_CHARS for char in value)


# LLM: Python数值投影可能改变指数、空格、下划线写法，不能仅按原字节过滤。
# 函数用途: 识别可能被数值转换改写的查询字符串。
def _is_numeric_like_run_id(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True
