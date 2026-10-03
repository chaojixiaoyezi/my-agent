# LLM: 能力包检查程序的 error_samples.location 是包作者写的任意文字，宿主原样转给模型定位前必须脱敏宿主路径（9b 复审 F4：
#   检查程序把 sys.argv[1]、__file__、sys.executable 写进 location，工作区绝对路径、宿主临时目录、解释器路径就进了回执、
#   返工提示、账本和 channel_delivery）。做法：按已知宿主路径做替换（目标和输入换成工作区相对路径，工作区根前缀去掉，
#   本次临时目录换成 <verifier>，宿主解释器换成 <python>），长的先换；换完仍含宿主路径的整条置成 <redacted>。
#   “仍含宿主路径”只看结构化事实：以 ~/、盘符或 /<段> 开头的片段，且 /<段> 在本机根目录下真实存在（JSON Pointer 如 /shots/0
#   的首段在本机不存在，不误伤）。9b 复核补的变形：片段前只要不是词字符、点或连字符就算分隔（反向写法，file:///Users、
#   a;/Users、a&/Users、#/Users 都算；词字符按 Unicode，中文目录名不误伤）；`..` 通过零宽定宽后查匹配，避免逐段扫描时
#   消耗路径内容并依赖 `/..` 恰好存在（../../Users）；判断前先做一次 URL 解码
#   （%2FUsers%2F）。故意编码（两次 %-编码、%00、base64、~user/ 等）不在防护范围：信任边界是“能力包由管理员审过才装”，
#   这里防的是 sys.argv、__file__、异常信息这类无意带出的路径。代价：JSON Pointer 首段恰好是本机根目录名（/home、/Users、/tmp）时整条置成
#   <redacted>，只丢定位、不泄露。只改 location，不改 code、计数和状态。改动同步 test_pack_verification_service.py。
# 模块用途: 不让检查程序借 location 把宿主路径带进模型可见的回执和提示。

from __future__ import annotations

import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

REDACTED_LOCATION = "<redacted>"
# location 里可能的宿主路径片段：`..` 用零宽后查，避免扫描 /.. 时吃掉下一段前的边界；路径内容只匹配 ~/、盘符或 /<首段>。
_HOST_PATH_TOKEN = re.compile(r"(?:^|[^\w.\-]|(?<=\.\.))(~[/\\]|[A-Za-z]:[\\/]|/([^/\s\"'|<>()\[\],;]+))")


# LLM: 只在有错误样例时才计算替换表；target / inputs 的相对形式沿用运行器入账口径（工作区外的只留文件名）。
# 函数用途: 返回 location 已脱敏的检查结果。
def redact_error_samples(result: object, request: object, temp_dir: str) -> object:
    samples = getattr(result, "error_samples", ())
    if not samples:
        return result
    replacements = _path_replacements(request, temp_dir)
    return replace(result, error_samples=tuple(
        {"code": item["code"], "location": redact_location(item["location"], replacements)} for item in samples))


# 函数用途: 按替换表逐个替换，再把仍含宿主路径的整条置成 <redacted>。
def redact_location(text: str, replacements: list[tuple[str, str]]) -> str:
    for old, new in replacements:
        text = text.replace(old, new)
    return REDACTED_LOCATION if _contains_host_path(text) else text


# LLM: 同一路径同时给原样和 resolve 后的两种写法（macOS /var 与 /private/var 是同一处）；长的先换，避免短前缀先吃掉长路径。
# 函数用途: 生成本次检查的宿主路径替换表。
def _path_replacements(request: object, temp_dir: str) -> list[tuple[str, str]]:
    root = Path(request.workspace_root)
    pairs: list[tuple[str, str]] = []
    for path in (request.target, *(path for _, path in getattr(request, "inputs", ()))):
        pairs.extend((form, _relative(Path(path), root)) for form in _forms(path))
    pairs.extend((form, "<verifier>") for form in _forms(temp_dir))
    pairs.extend((form, "<python>") for form in _forms(sys.executable))
    pairs.extend((form + os.sep, "") for form in _forms(root))
    return sorted(dict(pairs).items(), key=lambda item: -len(item[0]))


# 函数用途: 返回一个路径的原样写法和 resolve 后的写法（去重）。
def _forms(path: object) -> list[str]:
    text = str(path)
    try:
        resolved = str(Path(text).resolve(strict=False))
    except (OSError, RuntimeError):
        resolved = text
    return list(dict.fromkeys(form for form in (text, resolved) if form))


# 函数用途: 工作区内的路径换成相对路径，工作区外的只留文件名。
def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except (OSError, ValueError):
        return path.name


# LLM: 先 URL 解码一次再判断（%2FUsers%2F 这类写法）；只用于判断，返回给模型的仍是替换后的原文或 <redacted>。
# 函数用途: 判断文字里是否仍含宿主路径片段（~/、盘符，或首段在本机根目录下真实存在的 /<段>）。
def _contains_host_path(text: str) -> bool:
    for match in _HOST_PATH_TOKEN.finditer(unquote(text)):
        if match.group(2) is None or os.path.isdir(os.sep + match.group(2)):
            return True
    return False
