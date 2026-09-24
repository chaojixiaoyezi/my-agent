# LLM: 只调用本机 tesseract 可执行文件，不打包 OCR 库或模型、不联网。图片字节先写进宿主给的插件数据目录下的临时文件，
#   无论成败都删除；子进程不 start_new_session，宿主回收插件进程组时一并回收；超时 kill 并等待退出。
#   语言包缺失的判定走结构化事实：tesseract 失败后再跑 --list-langs，对比请求的语言名，不解析 stderr 文案。
# 模块用途: 定位 tesseract、运行 TSV 识别、解析逐行结果和置信度。

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .reading import ImageTextError

UNAVAILABLE_MESSAGE = "OCR 不可用：未找到 tesseract（可在设置 tesseract_path 指定）"
_LIST_LANGS_TIMEOUT = 10


# LLM: 设置为空时在 PATH 中查找；非空时按 shutil.which 规则检查（路径须可执行）。找不到返回 None，调用方给出 OCR_UNAVAILABLE。
# 函数用途: 确定要运行的 tesseract 可执行文件。
def find_tesseract(configured: str) -> str | None:
    return shutil.which(configured or "tesseract")


# LLM: 有副作用：在 data_dir/tmp 下写临时图片并启动 tesseract 子进程；finally 保证删除临时文件。
#   超时抛 OCR_TIMEOUT；非零退出时区分语言包缺失（LANG_UNAVAILABLE，带已安装列表）与其它失败（OCR_FAILED）。
# 函数用途: 对一张图片运行 tesseract，返回解析后的文字结果。
def run_ocr(executable: str, data_dir: Path, content: bytes, suffix: str, lang: str, timeout: int) -> dict:
    tmp_dir = data_dir / "tmp"
    tmp_dir.mkdir(mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="ocr-", suffix="." + suffix, dir=tmp_dir)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        code, stdout = _run([executable, name, "stdout", "--psm", "3", "-l", lang, "tsv"], timeout)
    finally:
        Path(name).unlink(missing_ok=True)
    if code != 0:
        available = list_langs(executable, timeout)
        missing = [item for item in lang.split("+") if item not in available]
        if missing:
            raise ImageTextError("LANG_UNAVAILABLE", f"语言包不可用：{'+'.join(missing)}。已安装语言："
                                 f"{', '.join(available) or '（无）'}。", available_langs=available)
        raise ImageTextError("OCR_FAILED", f"tesseract 识别失败（退出码 {code}）。", exit_code=code)
    return parse_tsv(stdout)


# LLM: 超时 kill 后 communicate 回收管道与僵尸进程，再抛 OCR_TIMEOUT；启动失败（权限、格式）抛 OCR_FAILED。
# 函数用途: 以超时运行一次 tesseract，返回退出码和标准输出文本。
def _run(command: list[str], timeout: int) -> tuple[int, str]:
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
    except OSError as exc:
        raise ImageTextError("OCR_FAILED", "无法启动 tesseract，请检查 tesseract_path。") from exc
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise ImageTextError("OCR_TIMEOUT", f"OCR 超时（超过 {timeout} 秒），已终止 tesseract 进程。",
                             timeout_seconds=timeout) from None
    return process.returncode, stdout.decode("utf-8", errors="replace")


# LLM: tesseract 5 把列表写到 stdout，首行是标题；只保留形如语言名的行。失败或超时返回空列表，不掩盖原错误。
# 函数用途: 取得本机已安装的 tesseract 语言包列表。
def list_langs(executable: str, timeout: int) -> list[str]:
    try:
        code, stdout = _run([executable, "--list-langs"], min(timeout, _LIST_LANGS_TIMEOUT))
    except ImageTextError:
        return []
    if code != 0:
        return []
    names = [line.strip() for line in stdout.splitlines()[1:]]
    return [name for name in names if name and all(ch.isalnum() or ch in "_-+" for ch in name)]


# LLM: 按 (page, block, par, line) 分组 level=5 的词；行外框取 level=4 行；置信度只统计 conf>=0 的非空词，保留两位小数。
#   列数不足或数字列坏的行跳过，不让一行坏数据拖垮整次结果。
# 函数用途: 把 tesseract TSV 输出转成全文、逐行文字/置信度/外框和统计。
def parse_tsv(tsv: str) -> dict:
    boxes: dict[tuple, dict] = {}
    words: dict[tuple, list[tuple[str, float]]] = {}
    for row in tsv.splitlines()[1:]:
        cells = row.split("\t")
        if len(cells) < 12:
            continue
        try:
            level, page, block, par, line = (int(cell) for cell in cells[:5])
            left, top, width, height = (int(cell) for cell in cells[6:10])
            confidence = float(cells[10])
        except ValueError:
            continue
        key, text = (page, block, par, line), cells[11].strip()
        if level == 4:
            boxes[key] = {"left": left, "top": top, "width": width, "height": height}
        elif level == 5 and text:
            words.setdefault(key, []).append((text, confidence))
    lines, scores = [], []
    for key in sorted(words):
        items = words[key]
        known = [score for _, score in items if score >= 0]
        scores.extend(known)
        lines.append({"text": " ".join(text for text, _ in items),
                      "confidence": round(sum(known) / len(known), 2) if known else None,
                      **boxes.get(key, {"left": None, "top": None, "width": None, "height": None})})
    return {"text": "\n".join(item["text"] for item in lines), "lines": lines,
            "word_count": sum(len(items) for items in words.values()),
            "mean_confidence": round(sum(scores) / len(scores), 2) if scores else None}
