# LLM: 文件工具共用的文本编码边界；禁止在追加时回退另一编码，BOM 与原字节序必须保持。
# 模块用途: 探测文本格式并严格按原格式编码，无法表示新内容时在写盘前报错。
"""文件编码探测 + 换行风格保留(审计 #23):读写非 UTF-8 企业文件不再失败/不静默改坏。

覆盖几十国企业代码库含大量非 UTF-8 文件:日本 Shift-JIS、中国 GBK/GB18030、西欧 Latin-1,还有
带 BOM 的 utf-8-sig/utf-16。原 read_file 硬编码 utf-8 读、非法即"无法读取";write_file 无条件
按 utf-8 写回,把原编码/CRLF 静默改坏污染 diff、坏构建。本模块:
- BOM/UTF-8 自建探测(确定性,无依赖);难判的多字节编码(Shift-JIS/GBK 等)借成熟库
  charset-normalizer(requests 同款)统计判别——库未装时优雅降级为仅 BOM+UTF-8,绝不破单机路径;
- 换行风格(CRLF/CR/LF)探测与保留,写回按原换行,不把 CRLF 静默改 LF。
检测编码和行尾后按原格式写回。
"""

from __future__ import annotations

import codecs

# 先长后短:utf-32 的 BOM 以 utf-16 BOM 为前缀,必须先判 utf-32。用不带 -le/-be 的名字,
# 让 codec 在 decode 时按 BOM 自动定字节序并剥 BOM、encode 时再补回(round-trip 保 BOM)。
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _bom_encoding(data: bytes) -> str | None:
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return encoding
    return None


def _charset_best(data: bytes) -> tuple[str, str] | None:
    """难判编码交给 charset-normalizer。返回 (解码文本, 编码名);库未装或测不出返回 None。"""
    try:
        from charset_normalizer import from_bytes
    except ImportError:
        return None  # 优雅降级:仅 BOM+UTF-8(不破单机/最小依赖路径)
    match = from_bytes(data).best()
    if match is None:
        return None
    return str(match), (match.encoding or "utf-8")


def decode_bytes(data: bytes) -> tuple[str, str]:
    """探测编码并解码,返回 (文本, 编码名)。顺序:BOM → 严格 UTF-8 → charset-normalizer。

    真二进制/测不出时维持原语义,抛 UnicodeDecodeError(read_file 据此回退"非有效文本无法读取")。
    """
    bom = _bom_encoding(data)
    if bom:
        return data.decode(bom), bom
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        if b"\x00" in data:
            raise  # 含 NUL 且无 BOM → 极可能二进制(各编码文本均无 NUL),不强行读出乱码
        best = _charset_best(data)
        if best is None:
            raise  # 测不出 → 不强行 latin-1 兜底(避免二进制返回乱码)
        return best


def detect_encoding(data: bytes) -> str:
    """只返回探测到的编码名(写回保留原编码用)。测不出退 utf-8。"""
    bom = _bom_encoding(data)
    if bom:
        return bom
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        if b"\x00" in data:
            return "utf-8"  # 二进制:写回不按它"编码"保留,用 utf-8 默认
        best = _charset_best(data)
        return best[1] if best else "utf-8"


# LLM: UTF-16/32 的换行必须先按 BOM 解码；不能把分散在代码单元里的 CR/LF 当成单字节文本。
# 函数用途: 根据文本字符而非 UTF-16 原始字节统计主导换行风格。
def detect_line_ending(data: bytes) -> str:
    """探测主导换行风格:CRLF / CR / LF(多数胜出,无换行则 LF)。"""
    text = data.decode(_bom_encoding(data)) if _bom_encoding(data) else data.decode("latin-1")
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    if crlf and crlf >= lf and crlf >= cr:
        return "\r\n"
    if cr and cr > lf:
        return "\r"
    return "\n"


def _apply_line_ending(content: str, ending: str) -> str:
    unified = content.replace("\r\n", "\n").replace("\r", "\n")  # 先归一到 \n
    return unified if ending == "\n" else unified.replace("\n", ending)


# LLM: 编码失败必须抛出并保持源文件；隐式 UTF-8 回退会让追加文件混合编码。
# 函数用途: 按显式编码和换行风格生成字节；调用方在写盘前处理错误。
def encode_text(content: str, encoding: str, line_ending: str) -> bytes:
    """严格按指定编码和换行风格编码，不静默转换成另一编码。"""
    normalized = _apply_line_ending(content, line_ending)
    return normalized.encode(encoding)


# LLM: 覆盖保持原 BOM 和端序，追加只生成无 BOM 的片段；原文解码失败不得猜测另一格式写回。
# 函数用途: 为 write/edit/patch 生成与源文件一致的文本字节。
def encode_like_original(content: str, original: bytes, *, append: bool = False) -> bytes:
    _text, encoding = decode_bytes(original)
    prefix = b""
    for bom, codec in (
        (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
        (codecs.BOM_UTF8, "utf-8"),
    ):
        if original.startswith(bom):
            encoding, prefix = codec, bom
            break
    body = encode_text(content, encoding, detect_line_ending(original))
    return (b"" if append else prefix) + body
